"""东方财富股吧「个股人气榜」— 港股市场 — 直连扩展模块。

**为什么需要本模块**: 港股面板此前有行情、分红、南向资金、AH 溢价、波动率情绪,
但缺**关注度**这一维。人气榜是散户/游资注意力的直接代理变量 —— 港股流动性高度
集中在少数标的, 「谁正在被围观」往往先于「谁正在被交易」, 是短线情绪的唯一
免费公开来源。

数据源: ``emappdata.eastmoney.com/stockrank/*``, 即股吧排行榜
(https://guba.eastmoney.com/rank/) 的后端。与 :mod:`app.plugins.eastmoney.hsgt`
的 ``datacenter-web`` 及被封禁的 ``push2`` **都不是同一主机**, 因此有独立的
令牌桶与熔断池 (见 :class:`~app.plugins.eastmoney.client.EastmoneyClient`)。

**为什么直连而不用 akshare**: 本仓库刻意不引入 akshare
(见 :mod:`app.plugins.eastmoney.hk_financials` 模块 docstring)。akshare 的
``stock_hk_hot_rank_em`` 系列就是对本模块这四个端点的薄封装, 且它为了补
「最新价/涨跌幅」还会**额外**打一次 ``push2.eastmoney.com/api/qt/ulist.np/get``
—— 而 push2 在本集群已被 IP 封禁。本模块只取排名本身, 名称走本地维表,
行情走既有腾讯快照链路, 不引入这个必然失败的依赖。

实测口径 (2026-11-22 活体验证, 全部结论均可复现):

- 🔴 **``pageSize`` 上限是 100, 且超限是「静默返回空」而不是报错**:
  ``pageSize=100`` → 100 行; ``pageSize=200`` / ``500`` → **``data: []``**;
  ``pageNo=2`` (配合 ``pageSize=100``) → 同样 **``[]``**。也就是说这个榜单
  **只有前 100 名, 不可翻页**。故 :data:`HOT_RANK_MAX_PAGE_SIZE` 写死,
  函数签名**刻意不暴露** ``page_size`` / ``page_no`` —— 暴露了就会有人传 200,
  然后得到「港股今天没有人气数据」的错误结论 (与 :mod:`app.plugins.eastmoney.hsgt`
  里「错格式会响亮报错」相反, 这里是**静默错**, 更危险)。
- 🔴 **榜外代码返回 200 + 全 ``null`` 字段, 不是 404 也不是空 dict**:
  ``srcSecurityCode="HK|99999"`` → ``code=0, message="OK"``, 但 ``data`` 里
  ``rank`` / ``calcTime`` / ``marketAllCount`` **全是 ``null``**。因此
  「查不到」必须靠 ``rank is None`` 判定 (:func:`fetch_hk_hot_rank_latest`
  返回 ``None``), 绝不能用 HTTP 状态码或信封 ``code`` 判定。
- ``marketAllCount`` = **参与排名的全市场标的数** (实测 2811)。这是解读排名的
  必要分母: 「第 100 名」在 100 个标的里是垫底, 在 2811 个里是前 3.6%。
  它**只在** ``getCurrentHkUsLatest`` (单标的端点) 里出现, 榜单端点没有 ——
  所以 :func:`fetch_hk_hot_rank_market_size` 用榜首代码额外换一次这个分母。
- ``rankChange`` (``rc``) 与 ``hisRankChange`` (``hisRc``) 是两个不同口径,
  实测前 100 名里分别有 14 / 90 个非零值, 都有信息量, 故都保留:
  ``rc`` 是**短周期**名次变动, ``hisRc`` 是**长周期**(历史)名次变动。
  上游未文档化两者的具体窗口, 故响应里**原样透传不做二次加工**, 并在 API
  层用中性字段名 (``rank_change`` / ``his_rank_change``) 而非猜测性的
  「日变动/周变动」。正负号语义实测为**正=名次上升(数字变小)**, 与
  ``rank`` 本身「小=靠前」一致。
- ``getHisHkUsList`` 返回**日频**历史 (实测 120 行, ``calcTime`` 形如
  ``"2026-07-26"``); ``getCurrentHkUsList`` 返回**当日 10 分钟粒度**
  (实测 78 行, ``calcTime`` 形如 ``"2026-11-22 00:00:00"``)。两者 ``calcTime``
  格式不同, 本模块**原样透传字符串不解析** —— 解析成 date/datetime 会强迫
  两种粒度共用一个类型, 而它们本就是两张不同的图。
- ``calcTime`` 是**上游的计算时刻**, 不是行情时刻, 也不是交易日历意义上的
  交易日 (实测非交易日 2026-11-22 周日仍有值)。故不可拿它当「最新交易日」用。
- 代码格式: 上游用 ``"HK|00700"``, 内部用 ``"00700.HK"``。转换复用
  :func:`app.plugins.eastmoney.hk.hk_symbol` (含 ``zfill(5)`` 与合法性校验),
  不另写一套 —— 两套代码转换迟早分叉。
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from app.plugins.eastmoney.client import EastmoneyClient, EastmoneyError
from app.plugins.eastmoney.hk import hk_symbol

logger = logging.getLogger(__name__)

#: 股吧人气榜后端主机。独立于 push2 (已封禁) 与 datacenter-web (分红/南向)。
EMAPPDATA_BASE = "https://emappdata.eastmoney.com"

#: 上游要求的固定 appId。非鉴权凭据 (浏览器里明文可见), 缺失会被拒。
HOT_RANK_APP_ID = "appId01"

#: 上游要求的固定 globalId (访客标识)。同样非机密, 来自股吧页面本身。
HOT_RANK_GLOBAL_ID = "786e4c21-70dc-435a-93bb-38"

#: ``marketType``: ``000003`` = 港股市场 (沪深京 A 股是另一个值)。
MARKET_TYPE_HK = "000003"

#: 🔴 榜单条数硬上限。实测 >100 静默返回空列表, 见模块 docstring。
HOT_RANK_MAX_PAGE_SIZE = 100

#: 上游 ``sc`` 字段的市场前缀, 形如 ``"HK|00700"``。
_SC_MARKET_PREFIX = "HK"


class HkHotRankEntry(NamedTuple):
    """人气榜一行。

    ``rank`` 小 = 靠前 (第 1 名最热门)。``rank_change`` / ``his_rank_change``
    正 = 名次上升。``name`` 不在本类型里 —— 名称走本地维表, 不由上游给
    (上游给名称就得再打一次 push2, 见模块 docstring)。
    """

    rank: int
    symbol: str
    code: str
    rank_change: int | None
    his_rank_change: int | None


class HkHotRankLatest(NamedTuple):
    """单标的的最新排名快照 (``getCurrentHkUsLatest``)。

    ``market_all_count`` 是**全市场参与排名的标的数**, 即排名的分母。
    ``his_rank_change_rank`` 是上游给的另一个名次口径, 语义未文档化,
    原样透传 (``None`` 表示上游没给)。
    """

    symbol: str
    rank: int | None
    rank_change: int | None
    his_rank_change: int | None
    his_rank_change_rank: int | None
    market_all_count: int | None
    calc_time: str | None
    flag: int | None


class HkHotRankPoint(NamedTuple):
    """排名时间序列一点。``calc_time`` 原样透传上游字符串 (日频/10分钟频格式不同)。"""

    calc_time: str
    rank: int | None


def hot_rank_code(symbol: str) -> str | None:
    """内部港股 symbol → 上游 ``srcSecurityCode`` 的代码部分。``00700.HK`` → ``00700``。

    与 :func:`app.plugins.eastmoney.hk.hk_secid` 同一套硬校验 (5 位数字 + ``.HK``
    后缀), 非法返回 ``None`` 而不抛错。5 位是硬要求: 放过 ``700.HK`` 会拼出
    ``HK|700``, 上游可能不报错却解析到**另一只**标的 —— 静默取错数据比取不到
    数据危险得多。
    """
    s = (symbol or "").strip().upper()
    if "." not in s:
        return None
    code, suffix = s.rsplit(".", 1)
    if suffix != "HK" or not code.isdigit() or len(code) != 5:
        return None
    return code


def _payload(**extra: Any) -> dict:
    """拼上游要求的固定字段 + 本次调用的额外字段。"""
    body: dict[str, Any] = {
        "appId": HOT_RANK_APP_ID,
        "globalId": HOT_RANK_GLOBAL_ID,
        "marketType": MARKET_TYPE_HK,
    }
    body.update(extra)
    return body


def _unwrap(payload: dict, path: str) -> Any:
    """解 ``{globalId, message, status, code, data, stack}`` 信封, 返回 ``data``。

    🔴 ``code != 0`` 才是失败。**榜外代码是 ``code=0`` + ``data`` 内字段全
    ``null``**, 会正常走到这里并返回那个全 null 的 dict —— 判空是调用方的责任
    (见模块 docstring 第 2 条)。
    """
    if not isinstance(payload, dict):
        raise EastmoneyError(f"人气榜 {path}: 响应不是 JSON 对象")
    code = payload.get("code")
    if code not in (0, None):
        raise EastmoneyError(
            f"人气榜 {path} 返回 code={code} message={payload.get('message')!r}"
        )
    return payload.get("data")


def _to_int(v: Any) -> int | None:
    """上游数值 → int。``None``/非数字/bool 一律 ``None`` (不猜、不填 0)。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _split_sc(sc: Any) -> str | None:
    """``"HK|00700"`` → ``"00700"``。格式不符返回 ``None``。"""
    if not isinstance(sc, str) or "|" not in sc:
        return None
    prefix, _, code = sc.partition("|")
    if prefix.strip().upper() != _SC_MARKET_PREFIX or not code.strip():
        return None
    return code.strip()


def fetch_hk_hot_rank_list(client: EastmoneyClient) -> list[HkHotRankEntry]:
    """拉取港股人气榜前 100 名。

    🔴 **不可翻页、不可调条数**: ``pageSize`` 写死 :data:`HOT_RANK_MAX_PAGE_SIZE`,
    因为上游对 >100 或 ``pageNo=2`` **静默返回空列表** (实测), 暴露参数等于
    埋一个「看起来像没数据」的坑。

    返回按 ``rank`` 升序。无法解析的行 (``sc`` 格式异常 / 代码非法) 被跳过并记
    warning —— 榜单少一行不影响其余行的名次, 整批失败才抛错。

    抛出: :class:`EastmoneyError` (传输/HTTP/信封 code 非 0/重试耗尽)。
    """
    data = _unwrap(
        client._post_json(
            EMAPPDATA_BASE,
            "/stockrank/getAllCurrHkUsList",
            _payload(pageNo=1, pageSize=HOT_RANK_MAX_PAGE_SIZE),
        ),
        "getAllCurrHkUsList",
    )
    if not isinstance(data, list):
        raise EastmoneyError(f"人气榜榜单 data 不是列表: {type(data).__name__}")

    out: list[HkHotRankEntry] = []
    skipped = 0
    for row in data:
        if not isinstance(row, dict):
            skipped += 1
            continue
        code = _split_sc(row.get("sc"))
        symbol = hk_symbol(code) if code else None
        rank = _to_int(row.get("rk"))
        if symbol is None or rank is None:
            skipped += 1
            continue
        out.append(
            HkHotRankEntry(
                rank=rank,
                symbol=symbol,
                code=code or "",
                rank_change=_to_int(row.get("rc")),
                his_rank_change=_to_int(row.get("hisRc")),
            )
        )
    if skipped:
        logger.warning("人气榜榜单跳过 %d 行 (sc/rk 无法解析)", skipped)
    out.sort(key=lambda e: e.rank)
    return out


def fetch_hk_hot_rank_latest(
    client: EastmoneyClient, symbol: str
) -> HkHotRankLatest | None:
    """单标的的最新排名。

    返回 ``None`` 的情形 (**全部 fail closed, 不伪造**):
    - ``symbol`` 不是合法港股代码 (:func:`hot_rank_code` 返回 ``None``);
    - 上游 ``data`` 缺失, 或 ``rank`` 为 ``null`` —— 即**该标的不在人气榜体系内**
      (实测 ``HK|99999`` 返回 200 + 全 null)。

    抛出: :class:`EastmoneyError` (传输/HTTP/信封错误)。
    """
    code = hot_rank_code(symbol)
    if code is None:
        logger.warning("人气榜最新排名: %r 不是合法港股 symbol", symbol)
        return None
    data = _unwrap(
        client._post_json(
            EMAPPDATA_BASE,
            "/stockrank/getCurrentHkUsLatest",
            _payload(srcSecurityCode=f"{_SC_MARKET_PREFIX}|{code}"),
        ),
        "getCurrentHkUsLatest",
    )
    if not isinstance(data, dict):
        return None
    rank = _to_int(data.get("rank"))
    if rank is None:
        # 榜外/未收录: 上游给 200 + 全 null, 见模块 docstring
        logger.debug("人气榜最新排名: %s 未被上游收录 (rank=null)", symbol)
        return None
    return HkHotRankLatest(
        symbol=symbol,
        rank=rank,
        rank_change=_to_int(data.get("rankChange")),
        his_rank_change=_to_int(data.get("hisRankChange")),
        his_rank_change_rank=_to_int(data.get("hisRankChange_rank")),
        market_all_count=_to_int(data.get("marketAllCount")),
        calc_time=data.get("calcTime") if isinstance(data.get("calcTime"), str) else None,
        flag=_to_int(data.get("flag")),
    )


def fetch_hk_hot_rank_market_size(
    client: EastmoneyClient, entries: list[HkHotRankEntry]
) -> tuple[int | None, str | None]:
    """从榜单反查「全市场参与排名的标的数」与上游计算时刻。

    🔴 **为什么要这个额外请求**: ``marketAllCount`` (排名分母, 实测 2811) 与
    ``calcTime`` **只存在于单标的端点** ``getCurrentHkUsLatest``, 榜单端点不给。
    而没有分母的排名无法解读 (「第 100 名」在 100 个里是垫底, 在 2811 个里是
    前 3.6%)。故用**榜首代码**换一次这个上下文 —— 该字段是全市场级的, 用哪只
    标的问都一样, 选榜首只是因为它必然存在且必然被收录。

    返回 ``(market_all_count, calc_time)``; 拿不到时返回 ``(None, None)`` 而
    **不抛错**: 分母是锦上添花的上下文, 不该因为它失败就让整个榜单 502。
    """
    if not entries:
        return None, None
    top = min(entries, key=lambda e: e.rank)
    try:
        latest = fetch_hk_hot_rank_latest(client, top.symbol)
    except EastmoneyError as e:
        logger.warning("人气榜分母查询失败 (%s): %s", top.symbol, e)
        return None, None
    if latest is None:
        return None, None
    return latest.market_all_count, latest.calc_time


def _fetch_points(client: EastmoneyClient, path: str, symbol: str) -> list[HkHotRankPoint]:
    """``getHisHkUsList`` / ``getCurrentHkUsList`` 的公共实现 (两者响应同构)。"""
    code = hot_rank_code(symbol)
    if code is None:
        logger.warning("人气榜序列 %s: %r 不是合法港股 symbol", path, symbol)
        return []
    data = _unwrap(
        client._post_json(
            EMAPPDATA_BASE,
            path,
            _payload(srcSecurityCode=f"{_SC_MARKET_PREFIX}|{code}"),
        ),
        path,
    )
    if not isinstance(data, list):
        return []
    out: list[HkHotRankPoint] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        ct = row.get("calcTime")
        if not isinstance(ct, str) or not ct.strip():
            continue
        out.append(HkHotRankPoint(calc_time=ct.strip(), rank=_to_int(row.get("rank"))))
    return out


def fetch_hk_hot_rank_history(
    client: EastmoneyClient, symbol: str
) -> list[HkHotRankPoint]:
    """单标的的**日频**排名历史 (实测约 120 个交易日, ``calcTime`` 形如 ``2026-07-26``)。"""
    return _fetch_points(client, "/stockrank/getHisHkUsList", symbol)


def fetch_hk_hot_rank_intraday(
    client: EastmoneyClient, symbol: str
) -> list[HkHotRankPoint]:
    """单标的的**当日 10 分钟粒度**排名 (实测约 78 点, ``calcTime`` 形如 ``2026-11-22 00:00:00``)。"""
    return _fetch_points(client, "/stockrank/getCurrentHkUsList", symbol)
