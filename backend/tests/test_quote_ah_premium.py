"""监控股票轮 AH 溢价率注入 + 港股排除测试。"""
from __future__ import annotations

import polars as pl

from app.services.quote_service import QuoteService


class _FakeRepo:
    """提供 get_latest_completed_enriched / get_instruments 的最小实现。"""

    def __init__(self, lagged_df: pl.DataFrame, lagged_date, instruments: pl.DataFrame) -> None:
        self._lagged_df = lagged_df
        self._lagged_date = lagged_date
        self._instruments = instruments
        self.latest_completed_calls = 0

    def get_latest_completed_enriched(self):
        self.latest_completed_calls += 1
        return self._lagged_df, self._lagged_date

    def get_instruments(self) -> pl.DataFrame:
        return self._instruments


def _inst(rows):
    return pl.DataFrame(
        rows,
        schema={"symbol": pl.Utf8, "name": pl.Utf8, "exchange": pl.Utf8},
        orient="row",
    )


def _lagged_frame():
    return pl.DataFrame({
        "symbol": ["600036.SH", "03968.HK", "600519.SH"],
        "raw_close": [46.0, 50.0, 1700.0],
    })


def test_exclude_hk_drops_hk_rows():
    df = pl.DataFrame({"symbol": ["600036.SH", "03968.HK", "000001.SZ"]})
    out = QuoteService._exclude_hk(df)
    assert out["symbol"].to_list() == ["600036.SH", "000001.SZ"]


def test_exclude_hk_empty_or_no_symbol_unchanged():
    assert QuoteService._exclude_hk(pl.DataFrame()).is_empty()
    df = pl.DataFrame({"x": [1]})
    assert QuoteService._exclude_hk(df)["x"].to_list() == [1]


def test_attach_ah_premium_injects_mapping():
    svc = QuoteService()
    svc._repo = _FakeRepo(_lagged_frame(), "2026-09-11", _inst([
        ("600036.SH", "招商银行", "SH"),
        ("03968.HK", "招商银行", "HK"),
        ("600519.SH", "贵州茅台", "SH"),
    ]))
    df = pl.DataFrame({"symbol": ["600036.SH", "600519.SH", "000001.SZ"]})
    out = svc._attach_ah_premium(df)
    assert "ah_premium" in out.columns
    # 招商银行 premium = 46/(50*0.92)-1 = 0.0 ; 贵州茅台/000001 无 H 配对 → null
    zsy = out.filter(pl.col("symbol") == "600036.SH")["ah_premium"][0]
    assert abs(zsy - 0.0) < 1e-9
    assert out.filter(pl.col("symbol") == "600519.SH")["ah_premium"][0] is None
    assert out.filter(pl.col("symbol") == "000001.SZ")["ah_premium"][0] is None


def test_attach_ah_premium_caches_per_date():
    svc = QuoteService()
    repo = _FakeRepo(_lagged_frame(), "2026-09-11", _inst([
        ("600036.SH", "招商银行", "SH"),
        ("03968.HK", "招商银行", "HK"),
    ]))
    svc._repo = repo
    df = pl.DataFrame({"symbol": ["600036.SH"]})
    svc._attach_ah_premium(df)
    svc._attach_ah_premium(df)
    # lagged 帧按日缓存, 但每次仍取最新收盘日 (轻量, 无重算); 这里验证不抛异常且复用映射
    assert svc._ah_premium_map_date == "2026-09-11"
    assert "600036.SH" in svc._ah_premium_map
    out = svc._attach_ah_premium(df)
    assert abs(out["ah_premium"][0] - 0.0) < 1e-9


def test_attach_ah_premium_no_repo_or_no_lagged_returns_unchanged():
    svc = QuoteService()
    df = pl.DataFrame({"symbol": ["600036.SH"]})
    assert svc._attach_ah_premium(df)["symbol"].to_list() == ["600036.SH"]
    # repo 返回空 lagged 帧
    svc._repo = _FakeRepo(pl.DataFrame(), None, pl.DataFrame())
    out = svc._attach_ah_premium(df)
    assert "ah_premium" not in out.columns
