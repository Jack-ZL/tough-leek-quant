"""板块资金流向数据源客户端 (东财多节点 + 新浪二级降级, 自建 HTTP)。

数据契约 (实测 2026-09):
- 东方财富 clist 接口: /api/qt/clist/get, 返回全市场板块涨跌/资金流排行。
- 字段口径 (与 akshare stock_sector_fund_flow_rank 一致):
  f12=板块代码, f14=名称, f3=今日涨跌幅(百分比原值, 0.52=+0.52%), f62=主力净流入(元),
  f184=主力净占比(百分比原值), f66/f72/f78/f84=超大单/大单/中单/小单净额(元)。
- 新浪 MoneyFlow.ssl_bkzj_bk (二级降级): 行业/概念资金流, 字段已为归一化目标制
  (avg_changeratio 小数涨跌幅, netamount 元, ratioamount 小数占比), 无分档净额。
- 归一化到统一契约: change_pct/净占比小数制, 净额保持「元」, 缺省为 None。
- 主 push2 节点在本环境被 IP 墙, push2delay(延迟行情) 可用; 按节点顺序降级重试。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 东财板块资金流节点 (延迟行情优先, 盘后快照用延迟数据足够; 其余为 CDN/历史备用节点)
_EASTMONEY_HOSTS = (
    "push2delay.eastmoney.com",
    "push2.eastmoney.com",
    "82.push2.eastmoney.com",
    "push2his.eastmoney.com",
)

# 板块类型 → fs 中的 t 值 (2=行业, 3=概念)
_KIND_TO_T = {"industry": "2", "concept": "3"}

# 需要的字段 (只取排名展示与归一化契约所需, 不含分档占比)
_FIELDS = "f12,f14,f3,f62,f184,f66,f72,f78,f84"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}

_PAGE_SIZE = 100  # 东财 clist 单页上限, 越大也会被截到 100


class SectorFundFlowError(Exception):
    """板块资金流数据源不可用 (所有节点均失败)。"""


def _to_float(v: Any) -> float | None:
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_decimal(v: Any) -> float | None:
    """东财百分比原值 (0.52 表示 +0.52%) → 小数制 (0.0052)。"""
    f = _to_float(v)
    return f / 100.0 if f is not None else None


def _normalize(row: dict) -> dict:
    return {
        "name": row.get("f14"),
        "code": row.get("f12"),
        "change_pct": _to_decimal(row.get("f3")),
        "main_net_inflow": _to_float(row.get("f62")),
        "main_net_ratio": _to_decimal(row.get("f184")),
        "super_net": _to_float(row.get("f66")),
        "large_net": _to_float(row.get("f72")),
        "medium_net": _to_float(row.get("f78")),
        "small_net": _to_float(row.get("f84")),
    }


class EastmoneyClient:
    """东财板块资金流客户端, 按 host 顺序降级重试。"""

    def __init__(self, hosts: tuple[str, ...] = _EASTMONEY_HOSTS, timeout: float = 15.0) -> None:
        self._hosts = hosts
        self._timeout = timeout

    def fetch(self) -> dict:
        """拉取行业 + 概念两维的资金流排名 (各取净流入前 100 与净流出前 100)。"""
        return {
            "industry": self._fetch_kind("industry"),
            "concept": self._fetch_kind("concept"),
        }

    def _fetch_kind(self, kind: str) -> dict:
        fs = f"m:90+t:{_KIND_TO_T[kind]}+f:!50"
        return {
            "inflow": self._fetch_page(fs, po=1),   # 净流入降序前 100
            "outflow": self._fetch_page(fs, po=0),  # 净流入升序 (净流出前 100)
        }

    def _fetch_page(self, fs: str, po: int) -> list[dict]:
        last: Exception | None = None
        for host in self._hosts:
            try:
                return self._request(host, fs, po)
            except SectorFundFlowError as e:
                last = e
                logger.warning("东财板块资金流节点 %s 失败: %s", host, e)
        raise SectorFundFlowError(f"东财板块资金流全部节点失败: {last}") from last

    def _request(self, host: str, fs: str, po: int) -> list[dict]:
        params = {
            "pn": 1,
            "pz": _PAGE_SIZE,
            "po": po,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f62",
            "fs": fs,
            "fields": _FIELDS,
        }
        url = f"https://{host}/api/qt/clist/get"
        try:
            resp = httpx.get(url, params=params, headers=_HEADERS, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise SectorFundFlowError(str(e)) from e

        payload = data.get("data") or {}
        diff = payload.get("diff")
        if isinstance(diff, list):
            return [_normalize(r) for r in diff if isinstance(r, dict)]
        # 某些节点返回 {0: {...}, 1: {...}} 字典结构
        if isinstance(diff, dict):
            return [_normalize(r) for r in diff.values() if isinstance(r, dict)]
        raise SectorFundFlowError("响应缺少 data.diff 列表")


def fetch_sector_fund_flow() -> dict:
    """便捷入口: 拉取行业+概念资金流 (测试可直接 monkeypatch 本函数)。"""
    return EastmoneyClient().fetch()


# ---- 新浪二级降级源 ----
# MoneyFlow.ssl_bkzj_bk 返回标准 JSON 数组, 字段名语义自明 (实测 2026-09):
#   name=板块名, category=代码, avg_changeratio=涨跌幅(小数), netamount=主力净流入(元, 带符号),
#   ratioamount=主力净占比(小数), inamount/outamount=流入/流出额(元), turnover=换手率,
#   ts_symbol/ts_name/ts_trade/ts_changeratio=领涨股代码/名称/现价/涨跌幅(小数)。
# fenlei: 0=行业, 1=概念。新浪只给净额与占比, 无超大/大/中/小单分档。
_SINA_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "MoneyFlow.ssl_bkzj_bk"
)
_SINA_KIND_TO_FENLEI = {"industry": "0", "concept": "1"}
_SINA_NUM = 1000  # 行业 48 / 概念 181, 远小于上限, 一次拉全
_SINA_TOP = 100  # 与东财契约一致: 各取净流入/净流出前 100

_SINA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn/",
}


def _normalize_sina(row: dict) -> dict:
    """新浪字段 → 统一契约 (已为小数制/元, 无需 /100 转换; 分档净额缺省为 None)。"""
    return {
        "name": row.get("name"),
        "code": row.get("category"),
        "change_pct": _to_float(row.get("avg_changeratio")),
        "main_net_inflow": _to_float(row.get("netamount")),
        "main_net_ratio": _to_float(row.get("ratioamount")),
        "super_net": None,
        "large_net": None,
        "medium_net": None,
        "small_net": None,
        "leader_name": row.get("ts_name"),
    }


class SinaClient:
    """新浪板块资金流客户端 (行业 fenlei=0, 概念 fenlei=1)。"""

    def __init__(self, timeout: float = 15.0) -> None:
        self._timeout = timeout

    def fetch(self) -> dict:
        """拉取行业 + 概念两维, 各按净流入/净流出拆成前 100。"""
        return {
            "industry": self._fetch_kind("industry"),
            "concept": self._fetch_kind("concept"),
        }

    def _fetch_kind(self, kind: str) -> dict:
        rows = self._request(kind)
        inflow = [r for r in rows if (r["main_net_inflow"] or 0) > 0]
        outflow = [r for r in rows if (r["main_net_inflow"] or 0) < 0]
        inflow.sort(key=lambda r: r["main_net_inflow"], reverse=True)
        outflow.sort(key=lambda r: r["main_net_inflow"])
        return {"inflow": inflow[:_SINA_TOP], "outflow": outflow[:_SINA_TOP]}

    def _request(self, kind: str) -> list[dict]:
        params = {
            "page": 1,
            "num": _SINA_NUM,
            "sort": "netamount",
            "asc": 0,
            "fenlei": _SINA_KIND_TO_FENLEI[kind],
        }
        try:
            resp = httpx.get(
                _SINA_URL, params=params, headers=_SINA_HEADERS, timeout=self._timeout
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise SectorFundFlowError(f"新浪板块资金流请求失败: {e}") from e
        if not isinstance(data, list):
            raise SectorFundFlowError("新浪板块资金流响应格式异常")
        return [_normalize_sina(r) for r in data if isinstance(r, dict)]


def fetch_sina_sector_fund_flow() -> dict:
    """便捷入口: 新浪行业+概念资金流 (二级降级源)。"""
    return SinaClient().fetch()
