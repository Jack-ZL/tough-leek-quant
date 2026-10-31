"""东方财富公开行情接口 HTTP 客户端。P1-12: 内置免 Key 数据源。

职责: 限速(令牌桶+抖动)、重试退避、熔断、统一信封解包。不知道 provider / services 层。

三个主机(各自独立限速与熔断, 封禁池不共享 — 实测 push2 被限流时 datacenter 仍可用):
  - push2.eastmoney.com       全市场快照 clist / 单标的盘口 stock/get
  - push2his.eastmoney.com    历史K线 kline/get
  - datacenter-web.eastmoney.com  分红送配事件 RPT_SHAREBONUS_DET

信封口径(2026-11 实测):
  - push2/push2his: {"rc":0, "data":{...}} — rc!=0 抛错; data=null 视为空结果(如无效 secid)
  - datacenter: {"success":true, "result":{"pages":N, "data":[...]}, "message":...}

限速纪律(实测教训): 短时间 ~6 个请求即触发 push2 的 IP 级临时封禁(HTTP 连接直接被拒),
因此默认 3 QPS 令牌桶 + 每请求随机抖动, 空响应体/429/5xx 指数退避, 连续失败熔断冷却。
不携带 ut 令牌 — 实测过期 ut 会导致空响应, 匿名参数即可正常返回。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import date

import httpx

logger = logging.getLogger(__name__)

PUSH2_BASE = "https://push2.eastmoney.com"
PUSH2HIS_BASE = "https://push2his.eastmoney.com"
DATACENTER_BASE = "https://datacenter-web.eastmoney.com"

# 全市场 A 股 clist 过滤器: 深主板 + 创业板 + 沪主板 + 科创板 + 北交所
FS_ALL_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

# 快照字段(实测返回): f2最新价 f3涨跌幅% f4涨跌额 f5成交量(手) f6成交额(元)
# f7振幅% f8换手率% f12代码 f13市场(1沪/0深) f14名称 f15高 f16低 f17今开 f18昨收
# f20总市值 f21流通市值 f124行情时间戳(秒, 可能缺失)
SPOT_FIELDS = "f2,f3,f4,f5,f6,f7,f8,f12,f13,f14,f15,f16,f17,f18,f20,f21,f124"

# 单标的盘口字段: 买一~买五价/量 = f19/f20, f17/f18, f15/f16, f13/f14, f11/f12
# 卖一~卖五价/量 = f31/f32, f33/f34, f35/f36, f37/f38, f39/f40 (量单位: 手)
DEPTH_FIELDS = (
    "f11,f12,f13,f14,f15,f16,f17,f18,f19,f20,"
    "f31,f32,f33,f34,f35,f36,f37,f38,f39,f40,f43,f57,f58"
)

_SPOT_PAGE_SIZE = 1000  # clist 单页条数(全市场 ~6 页拉完, 页间隔由令牌桶自然节流)
_SPOT_MAX_PAGES = 30
_KLINE_FIELDS1 = "f1,f2,f3,f4,f5,f6"
_KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
_DIVIDEND_PAGE_SIZE = 500
_DIVIDEND_COLUMNS = (
    "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,EX_DIVIDEND_DATE,"
    "PRETAX_BONUS_RMB,BONUS_IT_RATIO,BONUS_RATIO,IT_RATIO,ASSIGN_PROGRESS,IMPL_PLAN_PROFILE"
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


class EastmoneyError(Exception):
    """东财接口错误(网络失败 / 信封异常 / 熔断中)。"""


class _TokenBucket:
    """令牌桶限速器。

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

    东财对超限 IP 直接拒绝连接, 熔断避免封禁期内继续打接口延长封禁。
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
        """熔断开启期间抛 EastmoneyError(快速失败, 不发请求)。"""
        with self._lock:
            if self._open_until and self._clock() < self._open_until:
                remain = int(self._open_until - self._clock())
                raise EastmoneyError(f"东财接口熔断中(连续失败触发, 约 {remain}s 后自动恢复)")

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
                    "东财接口连续 %d 次失败, 熔断 %.0f 秒(疑似 IP 限流)",
                    self._threshold,
                    self._cooldown,
                )


class EastmoneyClient:
    """东财公开接口客户端。每个主机独立令牌桶 + 熔断(封禁池不共享)。

    线程安全: httpx.Client 可并发复用; 限速/熔断状态均有锁保护。
    """

    def __init__(
        self,
        timeout: float = 15.0,
        *,
        qps: float = 3.0,
        jitter_s: tuple[float, float] = (0.1, 0.3),
        max_retries: int = 3,
        clock=time.monotonic,
        sleeper=time.sleep,
    ) -> None:
        self._http = httpx.Client(headers=_HEADERS, timeout=timeout)
        self._jitter = jitter_s
        self._max_retries = max_retries
        self._clock = clock
        self._sleep = sleeper
        # 每主机一套限速+熔断: push2 被封时 datacenter(分红)仍可拉
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
    def _get_json(self, base: str, path: str, params: dict) -> dict:
        """GET + 限速 + 抖动 + 重试退避 + 熔断。返回解析后的 JSON dict。

        可重试症状: 传输错误 / HTTP 429、5xx / 空响应体(东财限流的典型表现)。
        退避: 0.5s → 1.5s → 3.5s (指数 + 固定偏移), 重试耗尽抛 EastmoneyError。
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
                    raise EastmoneyError(f"HTTP {resp.status_code}: {path}")
                elif not resp.text.strip():
                    # 空响应体 = 东财 IP 限流的典型症状(实测), 按可重试处理
                    last_err = "空响应体(疑似 IP 限流)"
                    breaker.on_failure()
                else:
                    try:
                        payload = resp.json()
                    except ValueError as e:
                        last_err = f"响应非 JSON: {e}"
                        breaker.on_failure()
                    else:
                        breaker.on_success()
                        return payload
            if attempt < self._max_retries - 1:
                backoff = 0.5 * (2**attempt) + 0.5 * random.random()
                logger.debug("东财 %s 第 %d 次重试(%.1fs 后): %s", path, attempt + 1, backoff, last_err)
                self._sleep(backoff)
        raise EastmoneyError(f"{path} 重试 {self._max_retries} 次仍失败: {last_err}")

    # ---- push2 信封 ----
    def _push2_data(self, base: str, path: str, params: dict) -> dict | None:
        """push2/push2his 信封解包: rc!=0 抛错; data=null 返回 None(无效 secid 等)。"""
        payload = self._get_json(base, path, params)
        rc = payload.get("rc", 0)
        if rc != 0:
            raise EastmoneyError(f"接口返回 rc={rc}: {path}")
        return payload.get("data")

    # ---- 全市场快照 ----
    def spot_page(self, pn: int = 1, pz: int = _SPOT_PAGE_SIZE, fs: str = FS_ALL_A) -> tuple[list[dict], int]:
        """clist 快照单页 → (行列表, 全市场总数)。行字段见 SPOT_FIELDS 注释。"""
        data = self._push2_data(
            PUSH2_BASE,
            "/api/qt/clist/get",
            {
                "pn": pn,
                "pz": pz,
                "po": 1,
                "np": 1,
                "fltt": 2,  # 价格类字段返回浮点(否则为 ×100 整数)
                "invt": 2,
                "fid": "f3",
                "fs": fs,
                "fields": SPOT_FIELDS,
            },
        )
        if not data:
            return [], 0
        diff = data.get("diff") or []
        # 老版本接口 diff 为 {序号: 行} 字典, 新版为数组 — 两者兼容
        rows = list(diff.values()) if isinstance(diff, dict) else list(diff)
        return rows, int(data.get("total") or 0)

    def spot_all(self, fs: str = FS_ALL_A) -> list[dict]:
        """分页拉全市场快照。总数以首页 total 为准, 拉满或空页即停。"""
        out: list[dict] = []
        total = 0
        for pn in range(1, _SPOT_MAX_PAGES + 1):
            rows, total = self.spot_page(pn=pn, fs=fs)
            if not rows:
                break
            out.extend(rows)
            if total and len(out) >= total:
                break
        if not out:
            raise EastmoneyError("全市场快照为空")
        return out

    # ---- 指数快照 ----
    def index_spot(self, secids: list[str]) -> list[dict]:
        """按 secid 列表拉指数快照(单次 clist, fs=i: 前缀)。空结果返回 []。"""
        if not secids:
            return []
        data = self._push2_data(
            PUSH2_BASE,
            "/api/qt/clist/get",
            {
                "pn": 1,
                "pz": len(secids),
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": ",".join(f"i:{s}" for s in secids),
                "fields": SPOT_FIELDS,
            },
        )
        if not data:
            return []
        diff = data.get("diff") or []
        return list(diff.values()) if isinstance(diff, dict) else list(diff)

    # ---- 历史K线 ----
    def kline(
        self,
        secid: str,
        beg: str,
        end: str,
        *,
        klt: int = 101,
        fqt: int = 0,
    ) -> list[str]:
        """单标的K线 → CSV 行列表(原样返回, 解析在 provider 层)。

        klt: 101=日线 1=1分钟; fqt: 0=不复权(项目契约: 落库必须原始价) 1=前复权 2=后复权。
        beg/end: "YYYYMMDD"。行格式(实测): 日期,开,收,高,低,成交量(手),成交额(元),振幅%,涨跌幅%,涨跌额,换手率%
        """
        data = self._push2_data(
            PUSH2HIS_BASE,
            "/api/qt/stock/kline/get",
            {
                "secid": secid,
                "klt": klt,
                "fqt": fqt,
                "beg": beg,
                "end": end,
                "fields1": _KLINE_FIELDS1,
                "fields2": _KLINE_FIELDS2,
            },
        )
        if not data:
            return []
        return list(data.get("klines") or [])

    # ---- 单标的盘口 ----
    def quote_depth(self, secid: str) -> dict | None:
        """stock/get 单标的五档盘口 → 原始字段 dict。data=null(无效 secid)返回 None。"""
        return self._push2_data(
            PUSH2_BASE,
            "/api/qt/stock/get",
            {"secid": secid, "invt": 2, "fltt": 2, "fields": DEPTH_FIELDS},
        )

    # ---- 分红送配事件 ----
    def dividend_events(self, start: date, end: date) -> list[dict]:
        """按除权除息日窗口拉全市场分红送配事件(datacenter 分页)。

        返回行字段: SECUCODE(带 .SH/.SZ/.BJ 后缀) / PRETAX_BONUS_RMB(每10股派息,税前) /
        BONUS_IT_RATIO(每10股送转合计) / BONUS_RATIO(送股) / IT_RATIO(转增) /
        EX_DIVIDEND_DATE / ASSIGN_PROGRESS / IMPL_PLAN_PROFILE。
        """
        out: list[dict] = []
        flt = (
            f"(EX_DIVIDEND_DATE>='{start.isoformat()}')(EX_DIVIDEND_DATE<='{end.isoformat()}')"
        )
        page = 1
        while True:
            payload = self._get_json(
                DATACENTER_BASE,
                "/api/data/v1/get",
                {
                    "reportName": "RPT_SHAREBONUS_DET",
                    "columns": _DIVIDEND_COLUMNS,
                    "filter": flt,
                    "pageNumber": page,
                    "pageSize": _DIVIDEND_PAGE_SIZE,
                    "sortColumns": "EX_DIVIDEND_DATE",
                    "sortTypes": 1,
                    "source": "WEB",
                    "client": "WEB",
                },
            )
            if not payload.get("success"):
                raise EastmoneyError(f"分红事件接口失败: {payload.get('message')}")
            result = payload.get("result") or {}
            rows = result.get("data") or []
            out.extend(rows)
            pages = int(result.get("pages") or 0)
            if not rows or page >= pages:
                break
            page += 1
        return out
