"""AH 溢价率服务单元测试。"""
import polars as pl

from app.services.ah_premium import (
    HKD_CNY_FX,
    attach_ah_premium,
    build_ah_pairs,
    compute_premium,
    get_ah_premium_snapshot,
    premium_map,
)


def _instruments(rows):
    return pl.DataFrame(
        rows,
        schema={"symbol": pl.Utf8, "name": pl.Utf8, "exchange": pl.Utf8},
        orient="row",
    )


def test_build_ah_pairs_one_to_one():
    df = _instruments([
        ("600036.SH", "招商银行", "SH"),
        ("03968.HK", "招商银行", "HK"),
        ("600519.SH", "贵州茅台", "SH"),
    ])
    a_to_h, h_to_a = build_ah_pairs(df)
    assert a_to_h == {"600036.SH": "03968.HK"}
    assert h_to_a == {"03968.HK": "600036.SH"}


def test_build_ah_pairs_ambiguous_dropped():
    # 同名 A 股两只 (例如拆股/A+B 异常) 应判为歧义并丢弃
    df = _instruments([
        ("000001.SZ", "平安银行", "SZ"),
        ("000002.SZ", "平安银行", "SZ"),
        ("09999.HK", "平安银行", "HK"),
    ])
    a_to_h, _ = build_ah_pairs(df)
    assert a_to_h == {}


def test_build_ah_pairs_missing_cols_fail_closed():
    df = pl.DataFrame({"symbol": ["600036.SH"], "exchange": ["SH"]})
    a_to_h, h_to_a = build_ah_pairs(df)
    assert a_to_h == {}
    assert h_to_a == {}


def test_compute_premium_numeric():
    # A=46.0 CNY, H=50.0 HKD, fx=0.92 -> 46/(50*0.92)-1 = 0.0
    assert abs(compute_premium(46.0, 50.0) - 0.0) < 1e-9
    # A 相对 H 溢价 15%
    assert abs(compute_premium(52.9, 50.0) - (52.9 / (50.0 * HKD_CNY_FX) - 1)) < 1e-9


def test_compute_premium_invalid_returns_none():
    assert compute_premium(None, 50.0) is None
    assert compute_premium(46.0, None) is None
    assert compute_premium(0.0, 50.0) is None
    assert compute_premium(-1.0, 50.0) is None
    assert compute_premium(46.0, 0.0) is None
    assert compute_premium(float("nan"), 50.0) is None
    assert compute_premium(46.0, 50.0, fx=0.0) is None


class _FakeRepo:
    def __init__(self, instruments, enriched):
        self._instruments = instruments
        self._enriched = enriched

    def get_instruments(self):
        return self._instruments

    def get_enriched_latest(self):
        return self._enriched, __import__("datetime").date(2026, 9, 11)


def test_get_ah_premium_snapshot_happy_path():
    repo = _FakeRepo(
        _instruments([
            ("600036.SH", "招商银行", "SH"),
            ("03968.HK", "招商银行", "HK"),
        ]),
        pl.DataFrame({
            "symbol": ["600036.SH", "03968.HK"],
            "raw_close": [46.0, 50.0],
        }),
    )
    snap = get_ah_premium_snapshot(repo, "600036.SH")
    assert snap is not None
    assert snap["h_symbol"] == "03968.HK"
    assert abs(snap["premium"] - 0.0) < 1e-9
    assert snap["date"] == "2026-09-11"


def test_get_ah_premium_snapshot_no_pair():
    repo = _FakeRepo(
        _instruments([("600519.SH", "贵州茅台", "SH")]),
        pl.DataFrame({"symbol": ["600519.SH"], "raw_close": [1700.0]}),
    )
    assert get_ah_premium_snapshot(repo, "600519.SH") is None


def test_get_ah_premium_snapshot_missing_price():
    repo = _FakeRepo(
        _instruments([
            ("600036.SH", "招商银行", "SH"),
            ("03968.HK", "招商银行", "HK"),
        ]),
        pl.DataFrame({"symbol": ["600036.SH"], "raw_close": [46.0]}),
    )
    assert get_ah_premium_snapshot(repo, "600036.SH") is None


def _frame(symbols, raw_closes, dates=None):
    data = {"symbol": symbols, "raw_close": raw_closes}
    if dates is not None:
        data["date"] = dates
    return pl.DataFrame(data)


def test_attach_ah_premium_single_day():
    inst = _instruments([
        ("600036.SH", "招商银行", "SH"),
        ("03968.HK", "招商银行", "HK"),
        ("600519.SH", "贵州茅台", "SH"),
    ])
    df = _frame(["600036.SH", "03968.HK", "600519.SH"], [46.0, 50.0, 1700.0])
    out = attach_ah_premium(df, inst)
    assert "ah_premium" in out.columns
    a_row = out.filter(pl.col("symbol") == "600036.SH")
    assert abs(a_row["ah_premium"][0] - 0.0) < 1e-9
    assert out.filter(pl.col("symbol") == "03968.HK")["ah_premium"][0] is None
    assert out.filter(pl.col("symbol") == "600519.SH")["ah_premium"][0] is None


def test_attach_ah_premium_multi_day():
    inst = _instruments([
        ("600036.SH", "招商银行", "SH"),
        ("03968.HK", "招商银行", "HK"),
    ])
    df = _frame(
        ["600036.SH", "03968.HK", "600036.SH", "03968.HK"],
        [46.0, 50.0, 47.0, 51.0],
        dates=["2026-09-10", "2026-09-10", "2026-09-11", "2026-09-11"],
    )
    out = attach_ah_premium(df, inst)
    assert "ah_premium" in out.columns
    d10 = out.filter((pl.col("symbol") == "600036.SH") & (pl.col("date") == "2026-09-10"))
    assert abs(d10["ah_premium"][0] - 0.0) < 1e-9
    d11 = out.filter((pl.col("symbol") == "600036.SH") & (pl.col("date") == "2026-09-11"))
    assert abs(d11["ah_premium"][0] - (47.0 / (51.0 * HKD_CNY_FX) - 1)) < 1e-9


def test_attach_ah_premium_no_h_rows_unchanged():
    inst = _instruments([
        ("600036.SH", "招商银行", "SH"),
        ("03968.HK", "招商银行", "HK"),
    ])
    df = _frame(["600036.SH"], [46.0])
    out = attach_ah_premium(df, inst)
    assert "ah_premium" not in out.columns


def test_attach_ah_premium_missing_raw_close_unchanged():
    inst = _instruments([
        ("600036.SH", "招商银行", "SH"),
        ("03968.HK", "招商银行", "HK"),
    ])
    df = pl.DataFrame({"symbol": ["600036.SH", "03968.HK"]})
    out = attach_ah_premium(df, inst)
    assert "ah_premium" not in out.columns


def test_premium_map_returns_a_symbol_mapping():
    inst = _instruments([
        ("600036.SH", "招商银行", "SH"),
        ("03968.HK", "招商银行", "HK"),
        ("600519.SH", "贵州茅台", "SH"),
    ])
    df = _frame(["600036.SH", "03968.HK", "600519.SH"], [46.0, 50.0, 1700.0])
    m = premium_map(df, inst)
    assert m == {"600036.SH": 46.0 / (50.0 * HKD_CNY_FX) - 1.0}


def test_premium_map_no_pairs_returns_empty():
    inst = _instruments([
        ("600519.SH", "贵州茅台", "SH"),
        ("600000.SH", "浦发银行", "SH"),
    ])
    df = _frame(["600519.SH", "600000.SH"], [1700.0, 12.0])
    assert premium_map(df, inst) == {}


def test_premium_map_missing_raw_close_returns_empty():
    inst = _instruments([
        ("600036.SH", "招商银行", "SH"),
        ("03968.HK", "招商银行", "HK"),
    ])
    df = pl.DataFrame({"symbol": ["600036.SH", "03968.HK"]})
    assert premium_map(df, inst) == {}
