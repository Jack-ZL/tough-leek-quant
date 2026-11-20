"""全市场轻量行情快照 — 按 market 分派读各自的 enriched。

供板块/概念聚合分析使用 (``GET /api/screener/market-snapshot``): 一次给出全市场
最新交易日的涨跌幅、成交额、换手率、量比与市值, 前端据此算行业/概念热度。

为什么单独一个模块
------------------
快照的实现原本内联在 ``api/screener.py`` 里, 且只读 A 股 enriched。行业板块页要
支持港股就必须能读港股 enriched —— 而港股**不进 unified 视图**
(``test_unified_views_exclude_hk`` 钉死: 混进去会让所有按 asset_type 过滤的存量
查询突然多出港股行, 全市场统计/选股器/板块轮动的分母都被改变), 只能按 market
分派到各自目录。

🔴 为什么不直接给 ``ScreenerService`` 加 market 参数
``ScreenerService`` 有四处 **A 股专有**依赖, 逐个改成 market 感知要动
``repository.py`` 三个方法加热路径缓存, 风险远大于收益:

* ``repo.get_enriched_latest_asset`` — 进程内 A 股/ETF/指数缓存
* ``repo.get_enriched_history`` — docstring 明写「仅 stock: 该缓存为股票专用」
* ``repo.enriched_latest_date`` 与 ``kline_enriched`` 视图 — A 股目录/A 股视图名
* ``daily_health.assert_usable`` — A 股完整性快照, 不跟踪港股目录

而快照只需要 ``change_pct`` / ``vol_ratio_5d`` 两个派生列 (其余全是 enriched 存储列
或维表列), 直接读 parquet + ``compute_indicators`` 就够, 不必背上整套选股器状态。
港股 enriched 的读一致性由**写入侧**的两道 fail-closed 闸门保证
(见 ``kline_sync_hk.sync_hk_enriched``: 缺复权因子库/缺完整性凭据即整轮中止),
读端不再重复校验。

🔴 用**字典分派**而不是 ``if market == "HK"``
后者会被 AST 架构守卫 (``tests/test_market_architecture_guard.py``) 判为串市场风险,
且新增市场时应该加一行注册而不是改分支。同构范式见 ``api/kline.py:_SEARCH_FRAMES``、
``services/financial_analyzer.py:_FINANCIAL_LOADERS``。

🔴 两个市场的输出**列契约刻意不同一处**: 港股多一列 ``industry`` (恒生行业)。
A 股行业分类走扩展数据表 (``ext_hy_ths`` 同花顺行业), 港股没有对应的扩展表 ——
东财板块中心的地域/行业/概念三类板块实测**全是 A 股** (见前端
``lib/board-market-support.ts`` 的实测结论), 但港股维表本来就带东财 f100 的恒生
行业, 且 ``instrument_sync_hk._HK_PRESERVE_FIELDS`` 专门保护它不被上游抖动掏空。
所以港股的分类随快照下发, 前端按市场选维度源, 不在这两列上互相冒充。
"""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING

import polars as pl

from app.market import Market

if TYPE_CHECKING:
    from app.tickflow.repository import KlineRepository

#: 快照输出列 (A 股)。前端行业/概念板块页消费的正是这几列:
#: ``change_pct``/``amount``/``turnover_rate``/``vol_ratio_5d`` 进热度与龙头打分,
#: ``market_cap``/``float_market_cap`` 进龙头打分的市值项。
_SNAPSHOT_COLS = (
    "symbol", "name", "close", "change_pct", "amount", "volume",
    "turnover_rate", "vol_ratio_5d", "total_shares", "float_shares",
    "market_cap", "float_market_cap", "consecutive_limit_ups",
)

#: 港股额外下发的分类列 (恒生行业)。见模块 docstring 的列契约说明。
_HK_EXTRA_COLS = ("industry",)


def _finite_rows(rows: list[dict]) -> list[dict]:
    """把 NaN/Inf 换成 None —— JSON 没有这两个值, 前端 ``num()`` 也只认有限数。"""
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None
    return rows


def _add_market_cap(df):
    """close × 股本 → 市值列 (仅在缺列时补, 与 A 股原实现同条件)。

    🔴 币种随市场: A 股是 CNY, 港股是 HKD (``HK_RULES.currency``)。两个市场的市值
    **不可直接比大小**; 前端龙头打分的市值阈值是按 A 股量级标定的, 港股同一阈值下
    得分偏低属口径差异, 不是数据错误 —— 同市场内部排序不受影响。
    """
    if "close" in df.columns and "total_shares" in df.columns and "market_cap" not in df.columns:
        df = df.with_columns((pl.col("close") * pl.col("total_shares")).alias("market_cap"))
    if "close" in df.columns and "float_shares" in df.columns and "float_market_cap" not in df.columns:
        df = df.with_columns((pl.col("close") * pl.col("float_shares")).alias("float_market_cap"))
    return df


def _snapshot_cn(repo: KlineRepository) -> dict:
    """A 股: 走 ``ScreenerService`` 的最新日 enriched 缓存 (含完整指标)。

    逐字沿用内联在 ``api/screener.py`` 里的原实现 —— 存量调用方 (概念/行业板块页、
    维度成分股弹窗) 的行为必须一字不变。
    """
    from app.services.screener import ScreenerService

    svc = ScreenerService(repo)
    as_of = svc.latest_date()
    if not as_of:
        return {"as_of": None, "rows": []}

    df = svc._load_enriched_for_date(as_of)
    if df.is_empty():
        return {"as_of": str(as_of), "rows": []}

    df = _add_market_cap(df)
    df = df.select([c for c in _SNAPSHOT_COLS if c in df.columns])
    return {"as_of": str(as_of), "rows": _finite_rows(df.to_dicts())}


def _snapshot_hk(repo: KlineRepository) -> dict:
    """港股: 直接读 ``kline_hk_enriched`` parquet, 现算两个派生列后拼维表。

    🔴 **排除人民币柜台** (``is_cny_counter``): 同一公司的港币/人民币两个柜台是同一
    份基本面, 都算进行业聚合会把该公司计两次, 行业涨跌幅均值与成交额随之失真。
    ``kline_sync_hk`` 默认就不写人民币柜台, 但 ``include_cny_counter=True`` 的局部
    重跑会写 —— 所以在**读端**再过滤一次, 不依赖写入侧的默认值。
    与 ``api/hk.py`` 的 ``/instruments`` 同一套判据。

    🔴 维表读不到时**仍然返回行情行** (``name``/``industry`` 为 null), 不整页返空:
    行情是真实存在的数据, 抹掉它是另一种谎报。缺 ``industry`` 时前端按行业分组自然
    得到空结果, 落到「未匹配到行业数据」空态 —— 如实说明缺的是分类而不是行情。

    扫描/暖机/现算指标的口径在 ``services/hk_enriched_read.py`` (与涨幅轮动矩阵共用)。
    """
    from app.services.hk_enriched_read import hk_latest_enriched_date, read_hk_enriched
    from app.services.kline_sync_hk import hk_enriched_instruments

    data_dir = repo.store.data_dir
    as_of = hk_latest_enriched_date(data_dir)
    if as_of is None:
        return {"as_of": None, "rows": []}

    df = read_hk_enriched(
        data_dir, start=as_of, end=as_of, needed={"change_pct", "vol_ratio_5d"},
    )
    if df.is_empty():
        return {"as_of": str(as_of), "rows": []}

    inst = hk_enriched_instruments(data_dir)
    if not inst.is_empty():
        want = [c for c in ("symbol", "name", "industry", "total_shares",
                            "float_shares", "is_cny_counter") if c in inst.columns]
        df = df.join(inst.select(want), on="symbol", how="left", maintain_order="left")
        if "is_cny_counter" in df.columns:
            df = df.filter(~pl.col("is_cny_counter").fill_null(False))

    df = _add_market_cap(df)
    cols = [c for c in (*_SNAPSHOT_COLS, *_HK_EXTRA_COLS) if c in df.columns]
    return {"as_of": str(as_of), "rows": _finite_rows(df.select(cols).to_dicts())}


#: market → 快照读取器。🔴 新市场忘了注册会在 ``build_market_snapshot`` 当场报错,
#: 不静默回退 A 股 (回退等于用 A 股行情冒充港股, 正是本模块要防的事)。
_SNAPSHOT_BUILDERS: dict[Market, Callable[[KlineRepository], dict]] = {
    Market.CN: _snapshot_cn,
    Market.HK: _snapshot_hk,
}


def supported_markets() -> tuple[Market, ...]:
    """已注册快照读取器的市场 (供 API 层拼 400 文案, 不另维护一份清单)。"""
    return tuple(_SNAPSHOT_BUILDERS)


def build_market_snapshot(repo: KlineRepository, market: Market) -> dict:
    """按 market 取最新全市场轻量行情快照 → ``{as_of, rows}``。

    ``as_of`` 为 ``None`` 表示该市场还没有 enriched 数据 (从没同步过)。
    """
    builder = _SNAPSHOT_BUILDERS.get(market)
    if builder is None:
        raise ValueError(f"市场 {market} 未注册行情快照读取器")
    return builder(repo)
