"""Pure legacy frame rules shared by data normalization and enriched features."""

import polars as pl


def filter_halt_days(df: pl.DataFrame) -> pl.DataFrame:
    """保留 v1 停牌过滤口径：开盘/最高为零，或历史填价但成交量/额全零。"""
    if df.is_empty() or "open" not in df.columns or "high" not in df.columns:
        return df
    return df.filter(~halted_day(df))


def halted_day(df: pl.DataFrame) -> pl.Expr:
    """停牌判据 (DataFrame 路径)。

    🔴 ``amount`` 必须 ``fill_null(0)`` 再比, 不能裸比 —— **港股日K没有成交额列**
    (腾讯 ifzq 的 kline 端点不返回 amount), 而 ``DAILY_STORAGE_SCHEMA`` 含该列, 故
    ``scan_daily_parquet`` 会给港股补出一列**全 null** 的 amount。裸 ``amount == 0``
    求值为 null, ``True & null`` 仍是 null, 而 ``filter(~null)`` 在 polars 里**丢行**
    —— 零成交日会被三值逻辑「意外」剔除, 而不是被本判据剔除。填 0 后判据变成显式
    ``True``, **存活集合完全一致** (6 种 volume/amount/OHLC 组合逐一实测), 但语义
    不再依赖 null 传播: 上游哪天开始返回真实 amount, 行为不会静默翻转。
    港股实测规模: 2838 只 / 233,704 行日K 里有 36,445 行 (15.6%) 是零成交日,
    其中 17 只**每一天**都零成交 ⇒ enriched 0 行。

    标量路径 :func:`halted_daily_values` 不做填充: 其入参是已解析的 Python 数值,
    ``None == 0`` 在 Python 里本就是 ``False``, 不存在三值逻辑。
    """
    if "volume" in df.columns and "amount" in df.columns:
        return halted_daily_values(
            pl.col("open"), pl.col("high"), pl.col("volume"), pl.col("amount").fill_null(0)
        )
    return (pl.col("open") == 0) & (pl.col("high") == 0)


def halted_daily_values(open_price, high, volume, amount):
    """The v1 halt predicate accepts either numeric values or Polars expressions."""
    return ((open_price == 0) & (high == 0)) | ((volume == 0) & (amount == 0))
