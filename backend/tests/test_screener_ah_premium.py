"""AH 溢价率在 screener 选股器的接入回归测试。

screener 加载日频 enriched 帧时 (含 A/H 双市场行) 注入 ah_premium 列,
A 股行带值、无 H 股配对的行与港股行为 None; 港股行随后被 _exclude_hk 剔除。
盘中单标的帧无 H 股行, attach 内部 fail-closed 不加列 (下游按缺列处理)。
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import polars as pl

from app.services.screener import ScreenerService


def _instruments() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600036.SH", "03968.HK", "600519.SH"],
        "name": ["招商银行", "招商银行", "贵州茅台"],
        "exchange": ["SH", "HK", "SH"],
    })


def _service(frame: pl.DataFrame, target: date) -> ScreenerService:
    repo = MagicMock()
    repo.get_enriched_latest_asset.return_value = (frame, target)
    repo.get_instruments_asset.return_value = _instruments()
    return ScreenerService(repo, asset_type="stock")


def test_load_enriched_for_date_injects_ah_premium_and_excludes_hk() -> None:
    target = date(2026, 9, 11)
    frame = pl.DataFrame({
        "symbol": ["600036.SH", "03968.HK", "600519.SH"],
        "raw_close": [46.0, 50.0, 1700.0],
    })
    svc = _service(frame, target)

    df = svc._load_enriched_for_date(target)
    symbols = set(df["symbol"].to_list())
    assert symbols == {"600036.SH", "600519.SH"}  # 港股被排除
    assert "ah_premium" in df.columns
    a_row = df.filter(pl.col("symbol") == "600036.SH").row(0, named=True)
    # 46 / (50 * 0.92) - 1 == 0.0
    assert a_row["ah_premium"] == 0.0
    # 无 H 股配对的 A 股为 None
    m_row = df.filter(pl.col("symbol") == "600519.SH").row(0, named=True)
    assert m_row["ah_premium"] is None


def test_load_enriched_for_date_no_h_rows_omits_column() -> None:
    target = date(2026, 9, 11)
    frame = pl.DataFrame({
        "symbol": ["600519.SH"],
        "raw_close": [1700.0],
    })
    svc = _service(frame, target)

    df = svc._load_enriched_for_date(target)
    assert "ah_premium" not in df.columns  # fail-closed: 无 H 股行 → 不加列


def test_run_custom_sql_filters_on_ah_premium() -> None:
    target = date(2026, 9, 11)
    frame = pl.DataFrame({
        "symbol": ["600036.SH", "03968.HK", "600519.SH"],
        "raw_close": [46.0, 50.0, 1700.0],
    })
    svc = _service(frame, target)

    result = svc.run(target, ["ah_premium > -0.01"], limit=10)
    # 600036 (0.0) 命中; 600519 (None) 被 SQL NULL 语义过滤; 港股被排除
    assert [r["symbol"] for r in result.rows] == ["600036.SH"]
