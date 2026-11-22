"""港股人气榜 (东方财富股吧个股人气榜) —— **只读**聚合。

为什么这个功能不落库
------------------
人气榜是**每 10 分钟重算一次的实时快照** (实测 ``calcTime`` 以 10 分钟为步长
推进), 且上游一次性给全前 100 名, 单次请求 ≈0.3s。落库只会引入「同步没跑 →
榜单是旧的」这一类失败模式, 而**关注度恰恰是时效性最强的维度** —— 一张昨天的
人气榜几乎没有解读价值 (§4)。

因此本模块**不写任何存储**, 与 :mod:`app.services.hk_connect_flow` /
:mod:`app.services.ah_premium` 同一取舍: 上游限速/熔断由 client 负责, 这里不另加
缓存, 前端 TanStack Query 的 ``staleTime`` 挡住重复打开页面造成的重复请求。

单标的的**日频历史** (``getHisHkUsList``, 实测 120 个交易日) 同样不落库: 它是
上游维护的滚动窗口, 每次都能整段取回, 本地存一份只会与上游的窗口滑动产生分歧。

🔴 为什么榜单里**没有最新价/涨跌幅**
----------------------------------
akshare 的 ``stock_hk_hot_rank_em`` 会额外打一次 ``push2.eastmoney.com`` 补行情,
本模块**刻意不做**, 两个理由:

1. **push2 在本集群已被 IP 封禁** (见 :mod:`app.plugins.eastmoney.hsgt` 模块
   docstring), 那一路必然失败。
2. 即便换腾讯快照补价, 也会造成**同一只股票在本应用里有两个价格口径** ——
   看盘/自选/详情走 ``quote_service`` (带缓存与新鲜度判定), 这里直连
   ``fetch_hk_spot`` (无缓存), 两者刷新时刻不同就会显示不同的价格, 而用户无法
   判断哪个对 (§4「同一个数字不得有两个口径」)。

价格属于**行情**维度, 已由既有页面负责; 人气榜只负责**关注度**维度。要看价格
点进详情页即可 (那里走的是正确的 quote 链路)。

名称来自**本地维表** ``instruments_hk``
------------------------------------
上游榜单只给 ``"HK|00700"`` 这样的代码, 不给名称。名称从本地维表左连接取得
(``/api/hk/instruments`` 的同一份数据, 是本应用里港股名称的**唯一权威来源**),
因此:

- 维表**未同步**时名称为 ``null``, 榜单本身照常返回 (优雅降级) —— 名称缺失
  不影响名次这个核心事实。响应里的 ``names_synced`` 如实告知前端是哪种情况,
  让页面能说「维表未同步, 名称暂缺」而不是静默显示一片空白。
- 🔴 因为读了本地维表, 前端 query key **必须**登记进
  ``HK_DATA_QUERY_PREFIXES``: 维表同步后名称应当刷新。这与 ``hk-connect-flow``
  (纯上游读, 刻意**不**登记) 的取舍相反, 别照抄。

排名的分母: ``market_all_count``
------------------------------
上游榜单端点**不给**分母, 只有单标的端点 ``getCurrentHkUsLatest`` 给
(实测 2811)。没有分母的排名无法解读 —— 「第 100 名」在 100 个标的里是垫底,
在 2811 个里是前 3.6%。故 :func:`hk_hot_rank` 会用榜首代码**额外换一次**这个
全市场级字段 (用哪只标的问都一样, 详见
:func:`app.plugins.eastmoney.hk_hot_rank.fetch_hk_hot_rank_market_size`)。

该额外请求**失败不致命**: 分母拿不到时 ``market_all_count`` 与所有
``rank_percentile`` 一律为 ``None`` (fail closed, 不拿 100 当分母凑数),
榜单本身照常返回 200。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

import polars as pl

from app.plugins.eastmoney.client import EastmoneyClient
from app.plugins.eastmoney.hk_hot_rank import (
    HkHotRankEntry,
    fetch_hk_hot_rank_history,
    fetch_hk_hot_rank_intraday,
    fetch_hk_hot_rank_latest,
    fetch_hk_hot_rank_list,
    fetch_hk_hot_rank_market_size,
    hot_rank_code,
)
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH

logger = logging.getLogger(__name__)

#: 数据源标识 (进 API 响应, 让口径可见)。
HK_HOT_RANK_SOURCE = "eastmoney_guba_hot_rank"

#: 榜单列 schema。``items`` 按 ``rank`` **升序** (第 1 名在最前)。
HK_HOT_RANK_COLS = (
    "rank",
    "symbol",
    "code",
    "name",
    "rank_change",
    "his_rank_change",
    "rank_percentile",
)

#: 排名序列列 schema (日频历史与当日盘中同构, 只是 ``calc_time`` 格式不同)。
HK_HOT_RANK_SERIES_COLS = ("calc_time", "rank")


class HkHotRankResult(NamedTuple):
    """:func:`hk_hot_rank` 的结果: 前 100 名榜单 + 口径元信息。"""

    #: 列为 :data:`HK_HOT_RANK_COLS` 的表, 按 ``rank`` 升序。
    items: pl.DataFrame
    #: 榜单条数 (= ``items`` 行数)。上游硬上限 100。
    count: int
    #: 参与排名的**全市场标的数** (排名分母, 实测 2811)。拿不到时 ``None``。
    market_all_count: int | None
    #: 上游计算时刻 (原样透传, 形如 ``2026-11-22 13:00:00``)。
    #: ⚠️ 这是**人气榜的重算时刻**, 不是行情时刻, 也不是交易日 —— 实测非交易日
    #: (周日) 仍有值, 不可当「最新交易日」用。
    calc_time: str | None
    #: 本地维表是否可用 (``False`` = 名称全为 null, 需如实告知用户)。
    names_synced: bool
    #: 数据源标识。
    source: str


class HkHotRankDetailResult(NamedTuple):
    """:func:`hk_hot_rank_detail` 的结果: 单标的排名 + 两条时间序列。"""

    symbol: str
    #: 本地维表里的名称; 维表未同步或该标的不在维表时为 ``None``。
    name: str | None
    #: 上游是否收录该标的。``False`` = **榜外/未收录**, 是合法答案而非错误
    #: (实测 ``99999.HK`` 返回 200 + 全 null 字段)。
    found: bool
    rank: int | None
    #: 短周期名次变动。正 = 名次上升 (数字变小)。
    rank_change: int | None
    #: 长周期(历史)名次变动。正 = 名次上升。
    his_rank_change: int | None
    #: 上游给的另一个名次口径, 语义未文档化, 原样透传。
    his_rank_change_rank: int | None
    market_all_count: int | None
    calc_time: str | None
    #: ``rank / market_all_count * 100``, 即「前 X%」。分母缺失时 ``None``。
    rank_percentile: float | None
    #: 日频排名历史 (实测约 120 行), 列为 :data:`HK_HOT_RANK_SERIES_COLS`, 按
    #: ``calc_time`` **升序**。
    history: pl.DataFrame
    #: 当日 10 分钟粒度排名 (实测约 79 行), 列同上, 按 ``calc_time`` 升序。
    intraday: pl.DataFrame
    source: str


def _items_schema() -> dict:
    return {
        "rank": pl.Int64,
        "symbol": pl.String,
        "code": pl.String,
        "name": pl.String,
        "rank_change": pl.Int64,
        "his_rank_change": pl.Int64,
        "rank_percentile": pl.Float64,
    }


def _empty_items() -> pl.DataFrame:
    return pl.DataFrame(schema=_items_schema())


def _empty_series() -> pl.DataFrame:
    return pl.DataFrame(schema={"calc_time": pl.String, "rank": pl.Int64})


def _series_frame(points: list) -> pl.DataFrame:
    """排名序列 → DataFrame, 按 ``calc_time`` 升序 (前端图表无需再反转)。

    ``calc_time`` 保持**字符串**: 日频是 ``"2026-07-26"``, 盘中是
    ``"2026-11-22 00:00:00"``, 两种粒度强行统一成 datetime 会逼着日频补一个
    假的 ``00:00:00``, 而这个假时刻会被前端 tooltip 如实显示出来 (§13 不伪造)。
    字符串按 ISO 格式排序即等于按时间排序, 无需解析。
    """
    if not points:
        return _empty_series()
    df = pl.DataFrame(
        {"calc_time": [p.calc_time for p in points], "rank": [p.rank for p in points]},
        schema={"calc_time": pl.String, "rank": pl.Int64},
    )
    return df.sort("calc_time")


def _read_hk_names(data_dir: Path) -> dict[str, str]:
    """本地维表 → ``{symbol: name}``。维表缺失/不可读时返回 ``{}`` (不抛错)。

    直读 parquet 而不走 ``instruments_hk`` 视图, 与 :mod:`app.services.ah_premium`
    同一理由: 视图在目录为空时不注册, 查询会 500 而不是如实表达「还没同步维表」。
    """
    path = data_dir / HK_INSTRUMENT_RELPATH
    if not path.exists():
        return {}
    try:
        df = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("港股维表读取失败, 人气榜将只有代码没有名称: %s", e)
        return {}
    if "symbol" not in df.columns or "name" not in df.columns:
        return {}
    # 维表按构造 symbol 唯一; unique 兜掉重复键 (重复会让下面的 dict 静默丢名称)
    df = df.select("symbol", "name").unique(subset=["symbol"], keep="first")
    return {
        r["symbol"]: r["name"]
        for r in df.to_dicts()
        if r.get("symbol") and r.get("name")
    }


def _percentile(rank: int | None, total: int | None) -> float | None:
    """排名 → 「前 X%」。分母缺失/为 0 或排名缺失时 ``None`` (不猜、不拿 100 凑)。"""
    if rank is None or not total or total <= 0:
        return None
    return round(rank / total * 100.0, 4)


def hk_hot_rank(
    *,
    data_dir: Path,
    client: EastmoneyClient | None = None,
) -> HkHotRankResult:
    """港股人气榜前 100 名 (含本地维表名称与排名分位)。

    Args:
        data_dir: 数据根目录, 用于读 ``instruments_hk`` 维表取名称。
        client: 复用已有的东财客户端; ``None`` 时自建并在结束时关闭。

    Returns:
        :class:`HkHotRankResult`。上游返回空榜单时是**空表 + count=0**, 不抛错
        (「此刻没有榜单」是合法答案)。

    Raises:
        EastmoneyError: 上游榜单请求失败 (限流/熔断/信封 code 非 0)。
            **不得**降级成空表 —— 那会把「拉不到」伪装成「港股此刻没有人气数据」,
            两者是完全不同的两件事 (§4)。由 API 层转 502。
    """
    own_client = client is None
    if own_client:
        client = EastmoneyClient()
    try:
        entries: list[HkHotRankEntry] = fetch_hk_hot_rank_list(client)
        # 分母只在单标的端点里, 用榜首额外换一次; 失败返回 (None, None) 不抛错
        market_all_count, calc_time = fetch_hk_hot_rank_market_size(client, entries)
    finally:
        if own_client:
            client.close()

    names = _read_hk_names(data_dir)
    if not entries:
        return HkHotRankResult(
            items=_empty_items(),
            count=0,
            market_all_count=market_all_count,
            calc_time=calc_time,
            names_synced=bool(names),
            source=HK_HOT_RANK_SOURCE,
        )

    rows = [
        {
            "rank": e.rank,
            "symbol": e.symbol,
            "code": e.code,
            "name": names.get(e.symbol),
            "rank_change": e.rank_change,
            "his_rank_change": e.his_rank_change,
            "rank_percentile": _percentile(e.rank, market_all_count),
        }
        for e in entries
    ]
    items = pl.DataFrame(rows, schema=_items_schema()).sort("rank")
    missing = sum(1 for r in rows if r["name"] is None)
    if missing and names:
        # 维表在但这几只查不到名称: 可能是新上市/维表过期, 记 warning 便于排查
        logger.warning("人气榜有 %d 只在本地维表里查不到名称", missing)
    return HkHotRankResult(
        items=items,
        count=items.height,
        market_all_count=market_all_count,
        calc_time=calc_time,
        names_synced=bool(names),
        source=HK_HOT_RANK_SOURCE,
    )


def hk_hot_rank_detail(
    *,
    symbol: str,
    data_dir: Path,
    client: EastmoneyClient | None = None,
    with_intraday: bool = True,
) -> HkHotRankDetailResult:
    """单标的的人气排名 + 日频历史 (+ 可选当日盘中序列)。

    Args:
        symbol: 内部港股 symbol (``00700.HK``)。
        data_dir: 数据根目录, 用于读维表取名称。
        client: 复用已有的东财客户端; ``None`` 时自建并在结束时关闭。
        with_intraday: 是否额外拉当日 10 分钟序列 (多一次上游请求)。

    Returns:
        :class:`HkHotRankDetailResult`。**榜外/未收录标的返回 ``found=False`` +
        全 null + 空序列, 不抛错** —— 上游对这种输入返回 200 + 全 null 字段,
        「这只股票不在人气榜体系里」是合法答案, 不是故障。

    Raises:
        EastmoneyError: 上游请求失败 (限流/熔断/信封错误)。由 API 层转 502。
    """
    code = hot_rank_code(symbol)
    names = _read_hk_names(data_dir)
    name = names.get(symbol) if code else None

    own_client = client is None
    if own_client:
        client = EastmoneyClient()
    try:
        if code is None:
            # 非法 symbol: 不发请求 (发了也是白问), 直接如实返回未收录
            latest = None
            history: list = []
            intraday: list = []
        else:
            latest = fetch_hk_hot_rank_latest(client, symbol)
            history = fetch_hk_hot_rank_history(client, symbol)
            intraday = fetch_hk_hot_rank_intraday(client, symbol) if with_intraday else []
    finally:
        if own_client:
            client.close()

    if latest is None:
        return HkHotRankDetailResult(
            symbol=symbol,
            name=name,
            found=False,
            rank=None,
            rank_change=None,
            his_rank_change=None,
            his_rank_change_rank=None,
            market_all_count=None,
            calc_time=None,
            rank_percentile=None,
            history=_series_frame(history),
            intraday=_series_frame(intraday),
            source=HK_HOT_RANK_SOURCE,
        )

    return HkHotRankDetailResult(
        symbol=symbol,
        name=name,
        found=True,
        rank=latest.rank,
        rank_change=latest.rank_change,
        his_rank_change=latest.his_rank_change,
        his_rank_change_rank=latest.his_rank_change_rank,
        market_all_count=latest.market_all_count,
        calc_time=latest.calc_time,
        rank_percentile=_percentile(latest.rank, latest.market_all_count),
        history=_series_frame(history),
        intraday=_series_frame(intraday),
        source=HK_HOT_RANK_SOURCE,
    )
