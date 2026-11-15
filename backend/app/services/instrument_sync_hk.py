"""港股标的维表同步服务。

写入 ``data/instruments_hk/instruments_hk.parquet`` —— 与 A 股
``instruments/instruments.parquet`` **物理分离** (方案 §4 闸3)。分离的收益是:
A 股那 11 处 ``kline_daily/**/*.parquet`` 式的递归 glob、以及 ``instruments_all``
统一视图, 都不会因为「多了一个 market 分支没改到」而静默混入港股行。

与 :mod:`app.services.instrument_sync` 的关系: 复用其 ``_preserve_instrument_fields``
的逐行 coalesce 逻辑 (港股要补的字段集不同, 通过参数覆写), 其余独立实现。
刻意**不**把港股塞进 ``sync_instruments``: 那个函数是 A 股盘前 9:10 的关键路径,
港股拉取失败绝不能连累它。

数据源: 东财 clist (``m:128``), 免 Key。实测口径与陷阱见
``app/plugins/eastmoney/hk.py`` 模块 docstring 与 ``docs/hk-market-plan.md`` §3.1。
"""
from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.plugins.eastmoney import hk
from app.plugins.eastmoney.client import EastmoneyClient, EastmoneyError
from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync import _preserve_instrument_fields

logger = logging.getLogger(__name__)

#: 维表落盘相对路径 (相对 data_dir)。
HK_INSTRUMENT_RELPATH = "instruments_hk/instruments_hk.parquet"

#: 需要从历史维表逐行补全的字段。
#:
#: 比 A 股的 ``_SHARE_FIELDS`` 多一个 ``industry``: 东财 f100 (恒生行业) 偶发返回
#: ``"-"``, 而行业是概念/行业板块功能的主键, 不能被一次抖动掏空。
#: **不含** ``limit_up``/``limit_down`` —— 港股无涨跌停, 两列恒 None (保留列只为
#: 与 A 股 schema 对齐), 没有「历史值」可言。
#: **含** ``lot_size`` —— 东财 clist 不提供每手股数 (``hk_instrument_rows`` 恒写
#: None), 该列由财务主指标表的 ``PER_SHARES`` 独立回填 (见
#: :func:`app.services.financial_sync_hk.backfill_hk_lot_size`)。若不列入保留集,
#: 每次重跑维表同步都会把已回填的每手股数冲回 None。
_HK_PRESERVE_FIELDS = (
    "listing_date", "total_shares", "float_shares", "tick_size", "industry", "lot_size",
)

#: 本次拉取行数 / 历史维表行数 低于此比例 → 判定「拉取不完整」而非「大批退市」,
#: 保留历史行。
#:
#: 回归背景 (A 股同款): 某交易所 get_instruments 瞬时 connection reset 时, 旧实现把
#: 残缺结果全量覆盖写盘, 整个交易所的标的从维表消失, 搜索/标的池/名称表随之全瞎 ——
#: 比用旧一天的维表严重得多。港股只有一个交易所, 没有「按交易所分区保留」可用,
#: 因此改用行数比例判据: 港股一年退市数十家 (约 1%), 一夜之间少一半只可能是拉取失败。
_MIN_FRESH_RATIO = 0.5


def fetch_hk_instrument_rows(
    *,
    fs: str = hk.FS_HK_EQUITY,
    client: EastmoneyClient | None = None,
) -> list[dict]:
    """拉港股快照 → ``instruments_hk`` 维表行 (未落库)。

    拉取失败抛 :class:`EastmoneyError`, **不返回空列表** —— 空列表和「真的没有标的」
    在调用方无法区分, 而后者会导致覆盖写盘掏空维表。
    """
    own_client = client is None
    if own_client:
        client = EastmoneyClient()
    try:
        rows = hk.fetch_hk_spot(client, fs=fs)
    finally:
        if own_client:
            client.close()
    return hk.hk_instrument_rows(rows)


def _carry_over_when_incomplete(
    df: pl.DataFrame, data_dir: Path
) -> tuple[pl.DataFrame, int]:
    """本次拉取疑似不完整时, 用历史维表补齐缺失的 symbol。

    返回 (合并后的 df, 补回的行数)。补回 0 行表示本次拉取完整 (或无历史可用)。
    """
    path = data_dir / HK_INSTRUMENT_RELPATH
    if not path.exists():
        return df, 0
    try:
        existing = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("instruments_hk 历史维表读取失败, 无法防掏空: %s", e)
        return df, 0
    if existing.is_empty() or "symbol" not in existing.columns:
        return df, 0

    ratio = df.height / existing.height if existing.height else 1.0
    if ratio >= _MIN_FRESH_RATIO:
        return df, 0

    missing = existing.filter(~pl.col("symbol").is_in(df["symbol"].to_list()))
    if missing.is_empty():
        return df, 0
    merged = (
        pl.concat([df, missing], how="diagonal_relaxed")
        .unique(subset=["symbol"], keep="first")
    )
    carried = merged.height - df.height
    logger.warning(
        "instruments_hk 本次仅拉到 %d 行 (历史 %d 行, 占比 %.0f%% < %.0f%%), "
        "判定为拉取不完整而非大批退市, 从历史维表补回 %d 行 (总 %d 行)",
        df.height, existing.height, ratio * 100, _MIN_FRESH_RATIO * 100,
        carried, merged.height,
    )
    return merged, carried


def sync_instruments_hk(data_dir: Path, *, fs: str = hk.FS_HK_EQUITY) -> int:
    """全量同步港股标的维表 → ``data/instruments_hk/instruments_hk.parquet``。

    返回写入的行数; 拉取失败返回 **0 且不写盘** (保留上一次的维表)。
    """
    try:
        rows = fetch_hk_instrument_rows(fs=fs)
    except EastmoneyError as e:
        logger.warning("港股标的维表拉取失败, 保留历史维表不覆盖: %s", e)
        return 0

    if not rows:
        logger.warning("港股标的维表返回 0 行, 保留历史维表不覆盖")
        return 0

    df = pl.DataFrame(rows)
    df = _preserve_instrument_fields(
        df,
        data_dir,
        relpath=HK_INSTRUMENT_RELPATH,
        preserve_fields=_HK_PRESERVE_FIELDS,
        label="instruments_hk",
    )
    # as_of 用**中国大陆日期**: 港股与 A 股同为 UTC+8, 且本仓所有 as_of 语义都是
    # 「同步动作发生日」而非「标的所属市场的交易日」, 保持一致以免读端分叉。
    df = df.with_columns(pl.lit(cn_today()).alias("as_of"))

    fresh_height = df.height
    df, carried = _carry_over_when_incomplete(df, data_dir)
    if carried:
        logger.info(
            "instruments_hk 本次新鲜行 %d, 补回历史行 %d, 落库 %d",
            fresh_height, carried, df.height,
        )

    out = data_dir / HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(df, out)

    cny = int(df.get_column("is_cny_counter").sum()) if "is_cny_counter" in df.columns else 0
    logger.info(
        "instruments_hk synced: %d rows (含人民币柜台 %d) → %s",
        df.height, cny, out,
    )
    return df.height
