"""新浪财经公开接口 HTTP 客户端。P1-13 第三批: 个股资金流向(日频历史)。

职责: 限速(令牌桶+抖动)、重试退避、JSON 解析。不知道 provider / services 层。
限速纪律与 tencent/eastmoney 插件同款(插件自包含, 不跨插件 import)。

接口 (2026-11-01 真机实测):
  MoneyFlow.ssl_qsfx_zjlrqs — 个股资金流日频历史, 免 Referer, sh/sz/bj 三市都支持。
  响应为 JSON 数组, 数值全是字符串; 每行字段:
    opendate 日期 / trade 收盘价 / changeratio 涨跌幅(小数) / netamount 净流入(元) /
    ratioamount 净流入占比(小数) / r0_net 主力(超大单)净额(元) / r0_ratio 主力净占比(小数) /
    cate_ra 行业涨跌幅(小数) / cate_na 行业净额(元)
  无数据时返回文本 "null"(合法空结果); 服务不存在等返回 {"__ERROR":n, ...}(抛错)。
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time

import httpx

logger = logging.getLogger(__name__)

SINA_BASE = "https://vip.stock.finance.sina.com.cn"

#: 行情快照主机 (与 SINA_BASE 互不相通: 不同主机、不同 Referer、GBK 编码)。
#: 港股指数快照走这里, 见 :mod:`app.plugins.sina.hk_index`。
HQ_BASE = "https://hq.sinajs.cn"

# 资金流历史路径(json_v2 网关 + MoneyFlow 服务)
_MONEY_FLOW_PATH = "/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    # 实测不带 Referer 也通, 但免费源不赌, 带上更稳
    "Referer": "https://finance.sina.com.cn/",
}


class SinaError(Exception):
    """新浪接口错误(网络失败 / 响应异常 / 重试耗尽)。"""


class _TokenBucket:
    """令牌桶限速器(与 tencent 插件同实现, 插件自包含不跨插件复用)。

    rate: 每秒补充令牌数(= 允许的持续 QPS)。sleeper 可注入, 供测试免等待。
    """

    def __init__(self, rate: float, sleeper=time.sleep) -> None:
        self._interval = 1.0 / max(rate, 0.1)
        self._sleep = sleeper
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self._interval
        if wait > 0:
            self._sleep(wait)


class SinaClient:
    """新浪行情公开接口客户端(目前仅资金流历史)。

    sleeper 可注入(测试免等待); transport 可注入 httpx MockTransport。
    """

    def __init__(
        self,
        *,
        qps: float = 2.0,
        max_retries: int = 3,
        sleeper=time.sleep,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._max_retries = max_retries
        self._sleep = sleeper
        self._bucket = _TokenBucket(qps, sleeper)
        self._http = httpx.Client(timeout=timeout, transport=transport, headers=_HEADERS)

    def close(self) -> None:
        self._http.close()

    def money_flow_daily(self, daima: str, num: int = 60) -> list[dict]:
        """个股资金流日频历史(最近 num 个交易日, 倒序)。

        daima: 带前缀代码(sz000651/sh600519/bj920002)。
        返回原始行 dict 列表(数值是字符串, 归一化在服务层); 无数据返回 []。
        """
        params = {"page": 1, "num": num, "sort": "opendate", "asc": 0, "daima": daima}
        text = self._get_text(SINA_BASE, _MONEY_FLOW_PATH, params)
        stripped = text.strip()
        # 无数据时新浪返回裸文本 null(不是 JSON null 字面量), 按合法空结果处理
        if not stripped or stripped in {"null", "[]"}:
            return []
        try:
            payload = json.loads(stripped)
        except ValueError as e:
            raise SinaError(f"资金流响应非 JSON: {stripped[:80]}") from e
        if isinstance(payload, dict):
            # {"__ERROR":3,"__ERRORMSG":"Service not found"} 这类错误信封
            raise SinaError(f"资金流接口错误: {payload.get('__ERRORMSG') or payload}")
        if not isinstance(payload, list):
            raise SinaError(f"资金流响应类型异常: {type(payload).__name__}")
        return [row for row in payload if isinstance(row, dict)]

    def _get_text(
        self,
        base: str,
        path: str,
        params: dict | None = None,
        *,
        encoding: str | None = None,
        referer: str | None = None,
    ) -> str:
        """GET + 限速 + 抖动 + 重试退避。返回响应文本。

        base: 主机根 (新浪有多个互不相通的主机 — ``vip.stock`` 走 json_v2 网关,
            ``hq.sinajs.cn`` 走行情快照), 与 tencent 插件的 ``_get_text`` 同款签名。
        encoding: 显式字符集。``hq.sinajs.cn`` 返回 **GBK** 且不带 charset 声明,
            httpx 会按 UTF-8 解出乱码 → 中文指数名必须显式指定 (同 tencent 快照)。
        referer: 覆盖默认 Referer。``hq.sinajs.cn`` 实测要求
            ``https://vip.stock.finance.sina.com.cn/``, 与资金流端点不同。

        ⚠️ 令牌桶是**全客户端共享**而非按主机分桶 (与 tencent 不同, 刻意如此):
        新浪对行情快照端点有明确的 IP 封禁策略 (akshare 该接口 docstring 原话
        「大量采集会被目标网站服务器封禁 IP」), 共享一个保守预算比每主机各自
        跑满更不容易触发封禁。

        可重试症状: 传输错误 / HTTP 429、5xx / 空响应体。
        退避: 0.5s → 1.5s → 3.5s (指数 + 随机偏移), 重试耗尽抛 SinaError。
        """
        url = base + path
        headers = {"Referer": referer} if referer else None
        last_err = "unknown"
        for attempt in range(self._max_retries):
            self._bucket.acquire()
            # 随机抖动: 打散请求节奏, 降低被识别为机器流量的概率
            self._sleep(random.uniform(0.05, 0.2))
            try:
                resp = self._http.get(url, params=params, headers=headers)
            except httpx.HTTPError as e:
                last_err = f"网络请求失败: {e}"
            else:
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_err = f"HTTP {resp.status_code}(限流/服务端错误)"
                elif resp.status_code != 200:
                    # 4xx 等确定性错误不重试(参数问题, 重试无意义)
                    raise SinaError(f"HTTP {resp.status_code}: {path[:60]}")
                elif not resp.content.strip():
                    last_err = "空响应体(疑似限流)"
                else:
                    # encoding 显式给定时不走 httpx 的字符集猜测(hq.sinajs.cn 是 GBK
                    # 且不声明 charset, 猜出来是 UTF-8 → 中文名全乱)
                    return resp.content.decode(encoding) if encoding else resp.text
            if attempt < self._max_retries - 1:
                backoff = 0.5 * (2**attempt) + 0.5 * random.random()
                logger.debug("新浪 %s 第 %d 次重试(%.1fs 后): %s", path[:60], attempt + 1, backoff, last_err)
                self._sleep(backoff)
        raise SinaError(f"{path[:60]} 重试 {self._max_retries} 次仍失败: {last_err}")
