"""腾讯行情公开接口 HTTP 客户端。P1-12: 内置免 Key 数据源。

职责: 限速(令牌桶+抖动)、重试退避、熔断、GBK 解码、响应解析。不知道 provider / services 层。
限速/熔断纪律与 eastmoney 插件同款(插件自包含, 不跨插件 import — 保证可独立启停)。

两个主机(各自独立限速与熔断, 封禁池不共享):
  - qt.gtimg.cn        实时快照(股票/指数/北交所同一端点, GBK 编码, 批量 ≤300 只/请求)
  - ifzq.gtimg.cn      分钟K线 mkline (注意: web.ifzq.gtimg.cn 对 mkline 返回 301, 必须用裸域)

快照响应格式(2026-11 实测): `v_sh600000="f0~f1~...~f51";` 每标的一行, 字段 ~ 分隔。
无效代码不报错、直接不返回对应行(批量 60 只实测返回 47 行)。
指数(如 sh000001)同一端点同一字段布局, 仅五档字段恒为 0。

m1 分钟K响应: JSON {"code":0, "data":{"sh600000":{"m1":[["YYYYMMDDHHMM",开,收,高,低,量(手),{},?],...]}}}
— 列序为 开/收/高/低(与腾讯日K同款陷阱, 收盘在第 3 列), 上限 320 根(约 1.3 个交易日),
  第 7/8 列含义未知(疑似占位/换手), 不使用。
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time

import httpx

logger = logging.getLogger(__name__)

QT_BASE = "https://qt.gtimg.cn"
IFZQ_BASE = "https://ifzq.gtimg.cn"

# 批量快照单请求代码数上限: 实测 300 只正常返回, 800 只返回空(URL 过长被网关拒绝),
# 取 300 留足余量。全市场 ~5900 只 ≈ 20 请求/轮。
SNAPSHOT_BATCH = 300

# m1 分钟K单请求根数上限(实测请求 1400 仍只返回 320)
MKLINE_M1_MAX = 320

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com/",
}

# 快照行解析: v_sh600000="..."; (代码段含字母数字, 值内无引号)
_SNAPSHOT_LINE = re.compile(r'v_(\w+)="([^"]*)"')


class TencentError(Exception):
    """腾讯接口错误(网络失败 / 响应异常 / 熔断中)。"""


class _TokenBucket:
    """令牌桶限速器(与 eastmoney 插件同实现, 插件自包含不跨插件复用)。

    rate: 每秒补充令牌数(= 允许的持续 QPS); capacity: 桶容量(允许的瞬时突发)。
    clock/sleeper 可注入, 供测试用虚拟时钟验证限速节奏。
    """

    def __init__(
        self,
        rate: float,
        capacity: int,
        *,
        clock=time.monotonic,
        sleeper=time.sleep,
    ) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = float(capacity)
        self._updated = clock()
        self._clock = clock
        self._sleep = sleeper
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """阻塞直到取到一枚令牌。"""
        while True:
            with self._lock:
                now = self._clock()
                # 按流逝时间补充令牌, 上限为桶容量
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            self._sleep(wait)


class _Breaker:
    """连续失败熔断器: 连续 fail_threshold 次失败 → 冷却 cooldown_s 秒内快速失败。

    避免上游异常/限流期内继续打接口延长封禁(与 eastmoney 插件同纪律)。
    """

    def __init__(
        self,
        fail_threshold: int = 5,
        cooldown_s: float = 300.0,
        *,
        clock=time.monotonic,
    ) -> None:
        self._threshold = fail_threshold
        self._cooldown = cooldown_s
        self._clock = clock
        self._streak = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def check(self) -> None:
        """熔断开启期间抛 TencentError(快速失败, 不发请求)。"""
        with self._lock:
            if self._open_until and self._clock() < self._open_until:
                remain = int(self._open_until - self._clock())
                raise TencentError(f"腾讯接口熔断中(连续失败触发, 约 {remain}s 后自动恢复)")

    def on_success(self) -> None:
        with self._lock:
            self._streak = 0
            self._open_until = 0.0

    def on_failure(self) -> None:
        with self._lock:
            self._streak += 1
            if self._streak >= self._threshold:
                self._open_until = self._clock() + self._cooldown
                self._streak = 0
                logger.warning(
                    "腾讯接口连续 %d 次失败, 熔断 %.0f 秒(疑似限流)",
                    self._threshold,
                    self._cooldown,
                )


class TencentClient:
    """腾讯公开接口客户端。每个主机独立令牌桶 + 熔断。

    线程安全: httpx.Client 可并发复用; 限速/熔断状态均有锁保护。
    """

    def __init__(
        self,
        timeout: float = 15.0,
        *,
        qps: float = 8.0,
        jitter_s: tuple[float, float] = (0.05, 0.15),
        max_retries: int = 3,
        clock=time.monotonic,
        sleeper=time.sleep,
        http: httpx.Client | None = None,
    ) -> None:
        # http 可注入, 供测试替换 FakeHttp
        self._http = http or httpx.Client(headers=_HEADERS, timeout=timeout)
        self._jitter = jitter_s
        self._max_retries = max_retries
        self._clock = clock
        self._sleep = sleeper
        # 每主机一套限速+熔断: qt(快照) 与 ifzq(分钟K) 互不牵连
        self._buckets: dict[str, _TokenBucket] = {}
        self._breakers: dict[str, _Breaker] = {}
        self._qps = qps
        self._state_lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    # ---- 限速/熔断状态 ----
    def _bucket(self, host: str) -> _TokenBucket:
        with self._state_lock:
            b = self._buckets.get(host)
            if b is None:
                b = _TokenBucket(self._qps, max(1, int(self._qps)), clock=self._clock, sleeper=self._sleep)
                self._buckets[host] = b
            return b

    def _breaker(self, host: str) -> _Breaker:
        with self._state_lock:
            b = self._breakers.get(host)
            if b is None:
                b = _Breaker(clock=self._clock)
                self._breakers[host] = b
            return b

    # ---- 底层请求 ----
    def _get_text(self, base: str, path: str, params: dict, *, encoding: str | None = None) -> str:
        """GET + 限速 + 抖动 + 重试退避 + 熔断。返回响应文本。

        encoding: 显式字符集(快照为 GBK — httpx 自动猜测不可靠, 中文会乱码);
        None 时用 httpx 默认解码(JSON 端点为 UTF-8)。
        可重试症状: 传输错误 / HTTP 429、5xx / 空响应体。
        退避: 0.5s → 1.5s → 3.5s (指数 + 随机偏移), 重试耗尽抛 TencentError。
        """
        host = base.split("//", 1)[-1].split("/", 1)[0]
        breaker = self._breaker(host)
        bucket = self._bucket(host)
        url = base + path
        last_err: str = "unknown"
        for attempt in range(self._max_retries):
            breaker.check()  # 熔断期内快速失败(不消耗令牌、不发请求)
            bucket.acquire()
            # 随机抖动: 打散请求节奏, 降低被识别为机器流量的概率
            self._sleep(random.uniform(*self._jitter))
            try:
                resp = self._http.get(url, params=params)
            except httpx.HTTPError as e:
                last_err = f"网络请求失败: {e}"
                breaker.on_failure()
            else:
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_err = f"HTTP {resp.status_code}(限流/服务端错误)"
                    breaker.on_failure()
                elif resp.status_code != 200:
                    # 4xx 等确定性错误不重试(参数问题, 重试无意义)
                    breaker.on_success()
                    raise TencentError(f"HTTP {resp.status_code}: {path}")
                elif not resp.content.strip():
                    # 空响应体按可重试处理(限流典型症状)
                    last_err = "空响应体(疑似限流)"
                    breaker.on_failure()
                else:
                    breaker.on_success()
                    if encoding:
                        # errors="replace": 个别生僻字乱码不致命, 不让整批快照失败
                        return resp.content.decode(encoding, errors="replace")
                    return resp.text
            if attempt < self._max_retries - 1:
                backoff = 0.5 * (2**attempt) + 0.5 * random.random()
                logger.debug("腾讯 %s 第 %d 次重试(%.1fs 后): %s", path, attempt + 1, backoff, last_err)
                self._sleep(backoff)
        raise TencentError(f"{path} 重试 {self._max_retries} 次仍失败: {last_err}")

    # ---- 实时快照 ----
    def snapshot(self, codes: list[str]) -> dict[str, list[str]]:
        """批量快照 → {带前缀代码: 字段列表}。字段按 ~ 分隔, 0 起编号(映射见 provider)。

        codes 为腾讯格式带前缀代码(sh600000/sz000001/bj920002/sh000001 指数同端点)。
        单请求超过 SNAPSHOT_BATCH 会被网关拒绝(实测 800 只返回空), 调用方负责分批。
        无效代码上游直接不返回对应行, 不报错。
        """
        if not codes:
            return {}
        if len(codes) > SNAPSHOT_BATCH:
            raise TencentError(f"快照单批 {len(codes)} 只超过上限 {SNAPSHOT_BATCH}, 调用方需分批")
        # 快照为 GBK 编码, 必须显式解码(httpx 自动猜测会把中文名解成乱码)
        text = self._get_text(QT_BASE, "", {"q": ",".join(codes)}, encoding="gbk")
        out: dict[str, list[str]] = {}
        for code, payload in _SNAPSHOT_LINE.findall(text):
            fields = payload.split("~")
            # 无效代码返回 v_pv_none_match="1" 等占位行, 字段数不足直接跳过
            if len(fields) < 36:
                continue
            out[code] = fields
        return out

    # ---- m1 分钟K ----
    def kline_m1(self, code: str, count: int = MKLINE_M1_MAX) -> list[list]:
        """单标的 1 分钟K → 原始行列表 [time, 开, 收, 高, 低, 量(手), {}, ?]。

        上限 320 根(约 1.3 个交易日, 请求更多仍只返回 320 — 实测)。
        注意列序为 开/收/高/低(收盘在第 3 列, 与常见 OHLC 序不同)。
        必须用裸域 ifzq.gtimg.cn — web.ifzq 子域对 mkline 返回 301。
        """
        count = max(1, min(int(count), MKLINE_M1_MAX))
        text = self._get_text(
            IFZQ_BASE,
            "/appstock/app/kline/mkline",
            {"param": f"{code},m1,,{count}"},
        )
        try:
            payload = json.loads(text)
        except ValueError as e:
            raise TencentError(f"mkline 响应非 JSON: {e}") from e
        if payload.get("code") != 0:
            raise TencentError(f"mkline 返回 code={payload.get('code')}: {payload.get('msg')}")
        node = (payload.get("data") or {}).get(code) or {}
        rows = node.get("m1") or []
        return rows if isinstance(rows, list) else []
