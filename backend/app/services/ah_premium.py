"""AH 溢价率服务 — A 股与其对应 H 股的跨市场比价。

职责:
  1. 从 instruments 维表按公司中文名精确匹配 A↔H 双上市配对 (仅 1:1 无歧义)。
  2. 基于 enriched 的 raw_close (未复权收盘价) 计算 AH 溢价率:
       AH溢价率 = A_raw_close / (H_raw_close x HKD_CNY_FX) - 1
     (A 股为人民币, H 股为港币, 先按汇率换算再比较; 正数 = A 股相对 H 股溢价)。

口径要点:
  - 比价必须用 raw_close (原始价), 不能用前复权 close —— 复权因子在 A/H 两个市场
    不同, 会扭曲跨市场价差。
  - 汇率使用固定近似常量 HKD_CNY_FX (港币挂钩美元, 波动小, 误差 <1%); 需要精确时
    再接入实时汇率源。

不知道: HTTP、前端、配置持久化。
"""
from __future__ import annotations

import logging
import math
from datetime import date

import polars as pl

logger = logging.getLogger(__name__)

# 港币→人民币 固定近似汇率 (HKD 挂钩美元, 波动小, 误差 <1%)
HKD_CNY_FX = 0.92

_HK_EXCHANGE = "HK"
_A_EXCHANGES = ("SH", "SZ", "BJ")


def build_ah_pairs(instruments: pl.DataFrame) -> tuple[dict[str, str], dict[str, str]]:
    """按公司中文名精确匹配 A↔H 双上市配对, 返回 (a_to_h, h_to_a)。

    仅保留 1:1 无歧义配对 (同名 A 股唯一且 H 股唯一), 其余丢弃。
    维表缺列或为空时返回空映射 (fail-closed)。
    """
    if instruments.is_empty():
        return {}, {}
    if not {"exchange", "symbol", "name"}.issubset(instruments.columns):
        return {}, {}

    a = instruments.filter(pl.col("exchange").is_in(_A_EXCHANGES))
    hk = instruments.filter(pl.col("exchange") == _HK_EXCHANGE)

    a_by_name: dict[str, list[str]] = {}
    h_by_name: dict[str, list[str]] = {}
    for symbol, name in a.select(["symbol", "name"]).iter_rows():
        a_by_name.setdefault(name, []).append(symbol)
    for symbol, name in hk.select(["symbol", "name"]).iter_rows():
        h_by_name.setdefault(name, []).append(symbol)

    a_to_h: dict[str, str] = {}
    h_to_a: dict[str, str] = {}
    for name, a_syms in a_by_name.items():
        h_syms = h_by_name.get(name)
        if h_syms is None or len(a_syms) != 1 or len(h_syms) != 1:
            continue
        a_to_h[a_syms[0]] = h_syms[0]
        h_to_a[h_syms[0]] = a_syms[0]
    return a_to_h, h_to_a


def compute_premium(
    a_raw_close: float | None,
    h_raw_close: float | None,
    fx: float = HKD_CNY_FX,
) -> float | None:
    """计算 AH 溢价率 (小数): A / (H x fx) - 1。任一输入无效返回 None。

    无效 = None / 非有限值 / <=0 价格 / 汇率 <=0。
    """
    if a_raw_close is None or h_raw_close is None or fx is None:
        return None
    try:
        a = float(a_raw_close)
        h = float(h_raw_close)
        f = float(fx)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(a) and math.isfinite(h) and math.isfinite(f)):
        return None
    if a <= 0 or h <= 0 or f <= 0:
        return None
    return a / (h * f) - 1.0


def attach_ah_premium(
    df: pl.DataFrame,
    instruments: pl.DataFrame,
    fx: float = HKD_CNY_FX,
) -> pl.DataFrame:
    """把 AH 溢价率列 attach 到同时含 A/H 两市场行的帧上 (A 股行带值, 其余为 None)。

    比价必须用 raw_close (未复权), 与 A/H 两市场复权因子无关。多日帧按 (symbol, date)
    对齐 H 股价格, 单日帧 (无 date 列) 按 symbol 对齐。

    fail-closed: 帧缺 symbol/raw_close、维表为空、无 1:1 配对、或帧内无对应 H 股行时,
    原样返回 (不加列), 由下游按缺列语义处理。
    """
    if df.is_empty() or "symbol" not in df.columns or "raw_close" not in df.columns:
        return df
    a_to_h, _ = build_ah_pairs(instruments)
    if not a_to_h:
        return df

    a_symbols = set(a_to_h.keys())
    h_symbols = set(a_to_h.values())
    if not df["symbol"].is_in(a_symbols).any() or not df["symbol"].is_in(h_symbols).any():
        return df

    pairs = pl.DataFrame(
        {"symbol": list(a_to_h.keys()), "_h_symbol": list(a_to_h.values())}
    )
    has_date = "date" in df.columns
    h_price_cols = ["symbol", "raw_close"] + (["date"] if has_date else [])
    h_prices = (
        df.filter(pl.col("symbol").is_in(h_symbols))
        .select(h_price_cols)
        .rename({"symbol": "_h_symbol", "raw_close": "_h_raw_close"})
    )
    join_keys = ["_h_symbol"] + (["date"] if has_date else [])

    joined = df.join(pairs, on="symbol", how="left")
    joined = joined.join(h_prices, on=join_keys, how="left")

    return joined.with_columns(
        pl.when(
            pl.col("_h_raw_close").is_not_null()
            & (pl.col("raw_close") > 0)
            & (pl.col("_h_raw_close") > 0)
        )
        .then(pl.col("raw_close") / (pl.col("_h_raw_close") * fx) - 1.0)
        .otherwise(None)
        .alias("ah_premium")
    ).drop(["_h_symbol", "_h_raw_close"])


def premium_map(
    df: pl.DataFrame,
    instruments: pl.DataFrame,
    fx: float = HKD_CNY_FX,
) -> dict[str, float]:
    """从含 A/H 双市场行的帧算 A 股 symbol → AH 溢价率映射。

    供盘中监控注入 lagged 日频溢价率使用: 输入为「上一完整收盘日」的全市场帧
    (A/H 同刻收盘价), 输出 A 股 symbol 到溢价率的映射。帧内无 A↔H 配对、缺列或
    计算失败时返回空 dict (fail-closed)。
    """
    attached = attach_ah_premium(df, instruments, fx)
    if "ah_premium" not in attached.columns:
        return {}
    rows = attached.filter(pl.col("ah_premium").is_not_null()).select(
        ["symbol", "ah_premium"]
    )
    return {r["symbol"]: r["ah_premium"] for r in rows.iter_rows(named=True)}


def get_ah_premium_snapshot(repo, a_symbol: str) -> dict | None:
    """取 A 股最新的 AH 溢价快照。

    返回 dict: {a_symbol, h_symbol, a_close, h_close, fx, premium, date},
    或 None (无 H 股配对 / enriched 未就绪 / 缺少 raw_close)。
    """
    instruments = repo.get_instruments()
    if instruments.is_empty():
        return None
    a_to_h, _ = build_ah_pairs(instruments)
    h_symbol = a_to_h.get(a_symbol)
    if not h_symbol:
        return None

    df, latest_date = repo.get_enriched_latest()
    if df.is_empty() or "raw_close" not in df.columns:
        return None
    px = df.filter(pl.col("symbol").is_in([a_symbol, h_symbol])).select(
        ["symbol", "raw_close"]
    )
    px_map = {r["symbol"]: r["raw_close"] for r in px.iter_rows(named=True)}
    a_close = px_map.get(a_symbol)
    h_close = px_map.get(h_symbol)
    premium = compute_premium(a_close, h_close)
    if premium is None:
        return None
    return {
        "a_symbol": a_symbol,
        "h_symbol": h_symbol,
        "a_close": a_close,
        "h_close": h_close,
        "fx": HKD_CNY_FX,
        "premium": premium,
        "date": latest_date.isoformat() if isinstance(latest_date, date) else None,
    }
