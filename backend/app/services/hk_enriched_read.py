"""港股 enriched 日K 的共享读取口径。

为什么单独一个模块: ``market_snapshot`` (行业板块页快照) 与 ``rps_rotation``
(涨幅轮动矩阵) 都要「扫港股 enriched 分区 → 按日期窗口取 → 现算派生指标」。
两处各写一遍必然漂移, 尤其是**暖机窗口**和 ``collect_schema()`` 的用法 ——
前者错了窗口头几天的 ``change_pct`` 全是 null, 后者在 polars 1.44 上对 LazyFrame
取 ``.schema`` 会抛 PerformanceWarning 且可能触发整表扫描。

🔴 港股**不进统一视图** (``test_unified_views_exclude_hk`` 禁止把 HK UNION 进
``kline_daily_all``/``instruments_all``), 所以只能按目录分派读取。本模块就是那个
分派的**读端**; 写端见 ``services/kline_sync_hk.py``。目录名的唯一事实来源是
``tickflow/repository.py`` 的 ``_DIRNAMES``, 这里只经 ``enriched_dirname`` 取用。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.market import Market
from app.tickflow.repository import enriched_dirname

logger = logging.getLogger(__name__)

#: 指标暖机窗口 (自然日)。与 ``ScreenerService._compute_enriched_full`` 同值 ——
#: ``vol_ratio_5d`` 只需 5 根, 但取宽窗口是为了与 A 股**同口径**: 同名指标在两个
#: 市场必须算出同一种东西, 否则前端的热度分不可比。
WARMUP_DAYS = 150

#: enriched 存储列里需要读出来做暖机的部分。``turnover_rate`` 与
#: ``consecutive_limit_ups`` 是**存储列** (依赖当时的流通股本/递推状态, 不可回推),
#: 必须随行透传; ``change_pct``/``vol_ratio_5d`` 由 ``compute_indicators`` 现算。
READ_COLS = (
    "symbol", "date", "open", "high", "low", "close", "volume", "amount",
    "raw_close", "raw_high", "raw_low", "turnover_rate", "consecutive_limit_ups",
)


def hk_enriched_dir(data_dir: Path) -> Path:
    """港股个股 enriched 的 parquet 目录 (``kline_hk_enriched``)。"""
    return data_dir / enriched_dirname("stock", market=Market.HK)


def hk_latest_enriched_date(data_dir: Path) -> date | None:
    """港股 enriched 分区里的最新交易日; ``None`` = 还没同步或目录不可读。

    🔴 返回 ``None`` 与「返回一个日期但窗口内无行」是**两种状态**, 调用方必须区分:
    前者该提示去同步, 后者该提示数据缺口。合并成一种就会退化成「暂无数据」。
    """
    from app.parquet import scan_enriched_parquet

    enriched_dir = hk_enriched_dir(data_dir)
    try:
        lf = scan_enriched_parquet(str(enriched_dir / "**" / "*.parquet"))
        # 🔴 collect_schema() 而不是 .schema: polars 1.44 对 LazyFrame 取 .schema
        # 会告警且可能触发整表扫描。这里只需要确认 date 列存在。
        if "date" not in lf.collect_schema().names():
            return None
        return lf.select(pl.col("date").max()).collect().item()
    except Exception as e:  # noqa: BLE001 — 无分区/无匹配文件时 polars 抛错, 等同「还没同步」
        logger.debug("港股 enriched 不可读 (%s): %s", enriched_dir, e)
        return None


def read_hk_enriched(
    data_dir: Path,
    *,
    start: date,
    end: date,
    needed: set[str],
    warmup_days: int = WARMUP_DAYS,
) -> pl.DataFrame:
    """``[start, end]`` 的港股 enriched 日K, 并现算 ``needed`` 里的派生指标。

    Args:
        start/end: 目标窗口 (含端点)。
        needed: 交给 ``compute_indicators`` 的指标名集合, 依赖列由它自己展开。
        warmup_days: 在 ``start`` 之前**额外**多读的自然日数, 用于指标暖机。

    🔴 暖机不可省: ``change_pct`` 要前一交易日收盘, ``vol_ratio_5d`` 要前 5 根量。
    直接按 ``[start, end]`` 读会让窗口头几天的指标是 null —— 对轮动矩阵来说就是
    最早那几天整列消失, 症状是「矩阵右边有左边没有」, 很容易被误读成数据缺失。

    🔴 只算 ``needed``, 不调 ``compute_signals``/``compute_limit_signals``: 港股无
    涨跌停 (``HK_RULES.has_price_limit=False``), 限价与连板恒 null/0, 而
    ``consecutive_limit_ups`` 本来就是 enriched 存储列, 透传即可。

    Returns:
        窗口内的 DataFrame (可能为空)。目录不可读/还没同步时返回空 DataFrame ——
        要区分「没同步」请先调 :func:`hk_latest_enriched_date`。
    """
    from app.indicators.pipeline import compute_indicators
    from app.parquet import scan_enriched_parquet

    enriched_dir = hk_enriched_dir(data_dir)
    try:
        lf = scan_enriched_parquet(str(enriched_dir / "**" / "*.parquet"))
        schema = lf.collect_schema().names()
    except Exception as e:  # noqa: BLE001 — 同上, 不可读等同「还没同步」
        logger.debug("港股 enriched 不可读 (%s): %s", enriched_dir, e)
        return pl.DataFrame()

    available = [c for c in READ_COLS if c in schema]
    warm_start = start - timedelta(days=warmup_days)
    df = (
        lf.filter((pl.col("date") >= warm_start) & (pl.col("date") <= end))
        .sort(["symbol", "date"])
        .select(available)
        .collect()
    )
    if df.is_empty():
        return df

    df = compute_indicators(df, needed=needed)
    # 裁回暖机窗口: 暖机行只是给指标当前值, 不属于调用方要的数据
    return df.filter((pl.col("date") >= start) & (pl.col("date") <= end))
