from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.api import kline
from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.matrix import (
    build_market_matrix_from_signals,
    load_market_data_matrix_from_parquet,
    make_signal_matrix,
)
from app.indicators import pipeline
from app.market import Market
from app.price_limits import (
    numpy_limit_price,
    numpy_price_limit_matrix,
    polars_is_risk_warning_name,
    polars_limit_price,
    polars_price_limit_pct,
    price_limit_pct,
)
from app.strategy import custom_signals


@pytest.fixture(autouse=True)
def _limit_signal_definitions(monkeypatch):
    """注入涨跌停信号定义 (与 data/user_data/custom_signals 种子同语义),
    使 signal_limit_* 列断言不依赖运行目录的用户数据。"""
    def _cond(left, op, right):
        return {"left": left, "op": op, "right": right, "leftDays": 0, "rightDays": 0}

    sigs = [
        {"id": "limit_up", "column": "signal_limit_up",
         "conditions": [_cond("raw_close", ">", "0"),
                        _cond("raw_close", ">=", "field:limit_up_price")]},
        {"id": "limit_down", "column": "signal_limit_down",
         "conditions": [_cond("raw_close", ">", "0"),
                        _cond("raw_close", "<=", "field:limit_down_price")]},
        {"id": "limit_down_recovery", "column": "signal_limit_down_recovery",
         "conditions": [_cond("raw_low", ">", "0"),
                        _cond("raw_close", ">", "field:limit_down_price"),
                        _cond("raw_low", "<=", "field:limit_down_price"),
                        _cond("close", ">", "field:open")]},
    ]
    monkeypatch.setattr(
        pipeline, "_custom_signal_exprs", custom_signals.build_expressions(
            [{**s, "name": s["id"], "kind": "both", "enabled": True} for s in sigs]
        )
    )


@pytest.mark.parametrize(
    ("symbol", "trade_date", "is_st", "expected"),
    [
        ("600001.SH", date(2026, 7, 3), True, 0.05),
        ("600001.SH", date(2026, 7, 6), True, 0.10),
        ("000001.SZ", date(2026, 7, 3), False, 0.10),
        ("300001.SZ", date(2026, 7, 3), True, 0.20),
        ("688001.SH", date(2026, 7, 3), True, 0.20),
        ("689001.SH", date(2026, 7, 3), True, 0.20),
        ("830001.BJ", date(2026, 7, 3), True, 0.30),
    ],
)
def test_scalar_price_limit_rules(symbol, trade_date, is_st, expected):
    assert price_limit_pct(
        symbol,
        trade_date,
        is_risk_warning=is_st,
    ) == pytest.approx(expected)


def test_polars_and_numpy_price_limit_rules_match():
    dates = [date(2026, 7, 3), date(2026, 7, 6)]
    symbols = ["600001.SH", "300001.SZ", "689001.SH", "830001.BJ"]
    names = ["*st主板", "*ST创业", "科创ST", "北交ST"]
    panel = pl.DataFrame({
        "date": [value for value in dates for _ in symbols],
        "symbol": symbols * len(dates),
        "name": names * len(dates),
    }).with_columns(
        polars_is_risk_warning_name(pl.col("name")).alias("is_st")
    ).with_columns(
        polars_price_limit_pct(
            pl.col("symbol"), pl.col("date"), pl.col("is_st"),
        ).alias("limit_pct")
    )
    polars_values = panel["limit_pct"].to_numpy().reshape(len(dates), len(symbols))
    numpy_values = numpy_price_limit_matrix(dates, symbols, names)
    np.testing.assert_allclose(polars_values, numpy_values)


def test_polars_and_numpy_limit_prices_use_identical_half_up_rounding():
    previous = np.array([18.90, 10.00], dtype=np.float64)
    limits = np.array([0.05, 0.10], dtype=np.float64)
    frame = pl.DataFrame({"previous": previous, "limit": limits})

    for up in (True, False):
        polars_values = frame.select(
            polars_limit_price(
                pl.col("previous"), pl.col("limit"), up=up,
            ).alias("price")
        )["price"].to_numpy()
        numpy_values = numpy_limit_price(previous, limits, up=up)
        np.testing.assert_allclose(polars_values, numpy_values)
    assert numpy_limit_price(previous, limits, up=False)[0] == pytest.approx(17.96)


def test_matrix_uses_date_specific_st_limits_across_change(tmp_path):
    root = tmp_path / "market"
    rows = [
        (date(2026, 7, 2), 10.0),
        (date(2026, 7, 3), 10.5),
        (date(2026, 7, 6), 11.03),
    ]
    for trade_date, close in rows:
        partition = root / f"date={trade_date.isoformat()}"
        partition.mkdir(parents=True)
        pl.DataFrame({
            "symbol": ["600001.SH"],
            "date": [trade_date],
            "open": [close],
            "high": [close],
            "low": [close],
            "close": [close],
            "raw_close": [close],
            "volume": [1000.0],
        }).write_parquet(partition / "part.parquet")

    market = load_market_data_matrix_from_parquet(
        root,
        rows[0][0],
        rows[-1][0],
        field_columns={"raw_close", "price_limit_pct"},
        instruments=pl.DataFrame({
            "symbol": ["600001.SH"],
            "name": ["*ST主板"],
        }),
        cache_root=tmp_path / "cache",
    )
    np.testing.assert_allclose(
        market.field("price_limit_pct")[:, 0],
        np.array([0.05, 0.05, 0.10], dtype=np.float32),
    )
    assert market.limit_up_locked[:, 0].tolist() == [0, 1, 0]


class _InstrumentRepo:
    def get_instruments_asset(self, asset_type: str) -> pl.DataFrame:
        assert asset_type == "stock"
        return pl.DataFrame({
            "symbol": ["600001.SH"],
            "limit_up": [10.88],
            "limit_down": [8.90],
        })


def test_minute_price_limit_prefers_authoritative_prices_only_today(monkeypatch):
    today = date(2026, 7, 18)
    monkeypatch.setattr(kline, "cn_today", lambda: today)
    current = kline._get_price_limit_info(
        _InstrumentRepo(), "600001.SH", today, "stock", "*ST主板",
    )
    historical = kline._get_price_limit_info(
        _InstrumentRepo(), "600001.SH", date(2026, 7, 3), "stock", "*ST主板",
    )

    assert current == {
        "rate": 0.10,
        "limit_up": 10.88,
        "limit_down": 8.90,
        "no_limit": False,
        "source": "instrument",
    }
    assert historical == {
        "rate": 0.05,
        "limit_up": None,
        "limit_down": None,
        "no_limit": False,
        "source": "rule",
    }


@pytest.mark.parametrize(
    ("quoted_date", "quoted_up", "expected"),
    [
        (date(2026, 7, 17), 10.65, (None, False, "rule")),
        (date(2026, 7, 21), 10.65, (None, False, "rule")),
        (None, 10.65, (None, False, "rule")),
        (date(2026, 7, 20), 10.65, (10.65, False, "instrument")),
        (date(2026, 7, 17), 100000.0, (None, False, "rule")),
        (date(2026, 7, 20), 100000.0, (None, True, "instrument")),
    ],
)
def test_minute_price_limit_uses_the_quoted_date(monkeypatch, quoted_date, quoted_up, expected):
    today = date(2026, 7, 20)
    monkeypatch.setattr(kline, "cn_today", lambda: today)

    class Repo(_InstrumentRepo):
        def get_instruments_asset(self, asset_type):
            return super().get_instruments_asset(asset_type).with_columns(
                pl.lit(quoted_date, dtype=pl.Date).alias("as_of"),
                pl.lit(quoted_up).alias("limit_up"),
            )

    info = kline._get_price_limit_info(Repo(), "600001.SH", today, "stock", None)
    assert (info["limit_up"], info["no_limit"], info["source"]) == expected
    if expected[0] is None:
        assert info["limit_down"] is None


class _NewStockRepo:
    """listing_date 在无涨跌幅窗口内的注册制新股维表 (C沈鼓场景)。"""

    def __init__(self, listing: date):
        self.listing = listing

    def get_instruments_asset(self, asset_type: str) -> pl.DataFrame:
        assert asset_type == "stock"
        return pl.DataFrame({
            "symbol": ["601091.SH"],
            "name": ["C沈鼓"],
            "limit_up": [100000.0],   # 哨兵值
            "limit_down": [None],
            "listing_date": [self.listing],
        })


def test_minute_price_limit_no_limit_window_overrides_rate_and_sentinel():
    """listing_date 命中窗口: 历史日 (哨兵/as_of 均失效) 也返回 no_limit=True。"""
    listing = date(2026, 9, 17)
    repo = _NewStockRepo(listing)

    # 行情日 = 上市次日 (窗口内), 维表 as_of 与行情日无关 (无 as_of 列)
    info = kline._get_price_limit_info(repo, "601091.SH", date(2026, 9, 18), "stock", "C沈鼓")

    assert info is not None
    assert info["no_limit"] is True
    assert info["limit_up"] is None
    assert info["limit_down"] is None

    # 窗口外 (第 6 个交易日之后) 恢复 rate 口径
    after = kline._get_price_limit_info(repo, "601091.SH", date(2026, 10, 15), "stock", "沈鼓能源")
    assert after is not None
    assert after["no_limit"] is False
    assert after["rate"] == 0.10


def _daily_limit_rows(current_close: float) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600001.SH", "600001.SH"],
        "date": [date(2026, 7, 17), date(2026, 7, 20)],
        "open": [10.0, current_close],
        "high": [10.0, current_close],
        "low": [10.0, current_close],
        "close": [10.0, current_close],
        "raw_close": [10.0, current_close],
        "raw_high": [10.0, current_close],
    })


def _write_limit_partitions(root, panel):
    for (day,), rows in panel.group_by("date"):
        partition = root / f"date={day.isoformat()}"
        partition.mkdir(parents=True, exist_ok=True)
        rows.with_columns(pl.lit(1000.0).alias("volume")).write_parquet(partition / "part.parquet")


@pytest.mark.parametrize("cached", [False, True])
def test_matrix_uses_only_valid_limits_for_the_matching_trading_date(tmp_path, cached):
    root = tmp_path / "market"
    later = date(2026, 7, 21)
    older, day = date(2026, 7, 17), date(2026, 7, 20)
    cases = [
        # Current close, instrument as_of, quoted bounds, expected locks.
        (10.05, day, 0.0, 0.0, (0, 0)),
        (9.00, day, 0.0, 0.0, (0, 1)),
        (10.05, older, 10.05, 9.10, (0, 0)),
        (10.05, later, 10.05, 9.10, (0, 0)),
        (10.05, None, 10.05, 9.10, (0, 0)),
        (10.05, day, 10.05, 9.10, (1, 0)),
        (12.00, day, 100000.0, 0.0, (0, 0)),
    ]
    symbols = [f"60000{i}.SH" for i in range(len(cases))]
    panel = pl.concat([
        _daily_limit_rows(close).with_columns(pl.lit(symbol).alias("symbol"))
        for symbol, (close, *_rest) in zip(symbols, cases, strict=True)
    ])
    _write_limit_partitions(root, panel)
    instruments = pl.DataFrame({
        "symbol": symbols,
        "as_of": [case[1] for case in cases],
        "limit_up": [case[2] for case in cases],
        "limit_down": [case[3] for case in cases],
    })
    cache = tmp_path / "cache" if cached else None
    market = load_market_data_matrix_from_parquet(
        root, older, day, instruments=instruments, field_columns={"raw_close"}, cache_root=cache,
    )
    actual = list(zip(market.limit_up_locked[-1], market.limit_down_locked[-1], strict=True))
    assert actual == [case[-1] for case in cases]

    # The dated quotes still govern the same session in a longer window.
    _write_limit_partitions(root, panel.filter(pl.col("date") == day).with_columns(
        pl.lit(later).alias("date"),
    ))
    longer = load_market_data_matrix_from_parquet(
        root, older, later, instruments=instruments, field_columns={"raw_close"}, cache_root=cache,
    )
    np.testing.assert_array_equal(longer.limit_up_locked[1], market.limit_up_locked[-1])
    np.testing.assert_array_equal(longer.limit_down_locked[1], market.limit_down_locked[-1])
    entries = np.zeros(longer.shape, dtype=np.uint8)
    entries[1, [0, 5, 6]] = 1
    execution = build_market_matrix_from_signals(
        longer, make_signal_matrix(longer.shape, entry=entries),
    )
    result = BacktestEngine(repo=None).simulate_independent_market_matrix(
        execution, 3, MatcherConfig(matching="close_t", fees_pct=0, slippage_bps=0, max_hold_days=1),
    )
    assert {trade.symbol for trade in result.trades} == {symbols[0], symbols[6]}
    assert all(trade.entry_date == day.isoformat() for trade in result.trades)


@pytest.mark.parametrize("legacy_undated", [False, True])
def test_matrix_cache_cannot_reuse_limits_after_the_quoted_date_changes(tmp_path, legacy_undated):
    root = tmp_path / "market"
    panel = _daily_limit_rows(10.05)
    _write_limit_partitions(root, panel)
    start, end = panel["date"].to_list()
    instruments = pl.DataFrame({"symbol": ["600001.SH"], "limit_up": [10.05]})
    if not legacy_undated:
        instruments = instruments.with_columns(pl.lit(end).alias("as_of"))

    def load(instrument_rows):
        return load_market_data_matrix_from_parquet(
            root, start, end, instruments=instrument_rows, field_columns={"raw_close"},
            cache_root=tmp_path / "cache",
        )

    first = load(instruments)
    assert first.limit_up_locked[-1, 0] == 1
    if legacy_undated:
        next_day = panel.tail(1).with_columns(pl.lit(date(2026, 7, 21)).alias("date"))
        _write_limit_partitions(root, next_day)
    else:
        instruments = instruments.with_columns(pl.lit(date(2026, 7, 21)).alias("as_of"))
    changed = load(instruments)
    assert changed.limit_up_locked[-1, 0] == 0
    assert changed.cache_path != first.cache_path
    assert first.limit_up_locked[-1, 0] == 1


@pytest.mark.parametrize(
    ("instrument_as_of", "expected"),
    [
        (date(2026, 7, 17), False),
        (date(2026, 7, 20), True),
        (None, True),
    ],
)
def test_daily_limit_prices_require_matching_instrument_date(instrument_as_of, expected):
    instrument_data = {
        "symbol": ["600001.SH"],
        "name": ["普通股"],
        "limit_up": [10.90],
        "limit_down": [9.10],
    }
    if instrument_as_of is not None:
        instrument_data["as_of"] = [instrument_as_of]

    result = pipeline.compute_limit_signals(
        _daily_limit_rows(9.10),
        pl.DataFrame(instrument_data),
        needed={"signal_limit_down"},
    )

    assert result["signal_limit_down"][-1] is expected
    assert "_instrument_as_of" not in result.columns


def test_daily_limit_prices_ignore_zero_placeholder_and_match_realtime():
    """维表涨跌停价为 0 (数据源未提供该字段的占位值) 时必须回退理论价。

    直接采用 0 会让「raw_close >= 0 - 0.005」恒成立, 当日所有标的被判涨停,
    连板数一路累加; 跌停侧反过来永远判不出跌停。实时路径
    (_compute_limit_signals_today) 已有 >0 守卫, 冷路径必须同口径。
    """
    instruments = pl.DataFrame({
        "symbol": ["600001.SH"],
        "name": ["普通股"],
        "limit_up": [0.0],
        "limit_down": [0.0],
        "as_of": [date(2026, 7, 20)],
    })

    # 只涨 0.5%: 不是涨停
    mild = pipeline.compute_limit_signals(
        _daily_limit_rows(10.05),
        instruments,
        needed={"signal_limit_up", "consecutive_limit_ups"},
    )
    assert mild["signal_limit_up"][-1] is False
    assert mild["consecutive_limit_ups"][-1] == 0

    # 真涨停 11.00 = 10.00 x 1.1: 理论价兜底后仍须判出
    sealed = pipeline.compute_limit_signals(
        _daily_limit_rows(11.00),
        instruments,
        needed={"signal_limit_up", "consecutive_limit_ups"},
    )
    assert sealed["signal_limit_up"][-1] is True
    assert sealed["consecutive_limit_ups"][-1] == 1

    # 真跌停 9.00 = 10.00 x 0.9: 占位 0 不得让跌停漏判
    floored = pipeline.compute_limit_signals(
        _daily_limit_rows(9.00),
        instruments,
        needed={"signal_limit_down"},
    )
    assert floored["signal_limit_down"][-1] is True

    # 与实时路径同一份维表同一结论
    realtime = pipeline._compute_limit_signals_today(
        pl.DataFrame({
            "symbol": ["600001.SH"],
            "date": [date(2026, 7, 20)],
            "open": [10.05],
            "high": [10.05],
            "low": [10.05],
            "close": [10.05],
            "raw_close": [10.05],
            "raw_high": [10.05],
            "raw_low": [10.05],
            "_prev_close_raw": [10.0],
            "volume": [1000.0],
        }),
        instruments,
    )
    # 实时路径不注入信号列 (定义注入在 compute_enriched_today 末尾), 按引擎判定价对拍
    assert realtime["limit_up_price"][0] == pytest.approx(10.995)
    assert realtime["consecutive_limit_ups"][0] == 0
    assert mild["limit_up_price"][-1] == realtime["limit_up_price"][0]
    assert mild["consecutive_limit_ups"][-1] == realtime["consecutive_limit_ups"][0]


def test_realtime_limit_prices_ignore_stale_instrument_date():
    today = date(2026, 7, 20)
    rows = pl.DataFrame({
        "symbol": ["600001.SH"],
        "date": [today],
        "open": [9.10],
        "high": [9.10],
        "low": [9.10],
        "close": [9.10],
        "raw_close": [9.10],
        "raw_high": [9.10],
        "raw_low": [9.10],
        "_prev_close_raw": [10.0],
        "volume": [1000.0],
    })
    instruments = pl.DataFrame({
        "symbol": ["600001.SH"],
        "name": ["普通股"],
        "limit_up": [10.90],
        "limit_down": [9.10],
        "as_of": [date(2026, 7, 17)],
    })

    result = pipeline._compute_limit_signals_today(rows, instruments)

    # 过期维表 → 回退理论跌停价 9.005 (而非权威 9.105), 9.10 未触及 → 不判跌停
    assert result["limit_down_price"][0] == pytest.approx(9.005)
    assert result["consecutive_limit_downs"][0] == 0
    assert "_instrument_as_of" not in result.columns


def test_limit_down_recovery_uses_raw_low_under_later_ex_div():
    """除权事件之后重算历史时, 跌停翘板"曾触及跌停"必须用原始价 low 判断。

    day2 (历史日): 原始 low 9.30 未触及跌停价 9.00, 不应触发翘板;
    但 day3 除权 (ex_factor=2) 使 day2 前复权 low 变为 4.65,
    若误用复权 low 对比原始口径跌停价会误报翘板。
    day3 (除权日, 最新日不复权): 涨跌停基准切换为前复权昨收 4.825 → 跌停价 4.34,
    原始 low 4.34 触及且收阳未封死 → 真翘板。
    """
    raw = pl.DataFrame({
        "symbol": ["600001.SH"] * 3,
        "date": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        "open": [10.00, 9.60, 4.30],
        "high": [10.10, 9.70, 4.45],
        "low": [9.90, 9.30, 4.34],
        "close": [10.00, 9.65, 4.42],
        "volume": [10000.0, 10000.0, 10000.0],
        "amount": [1.0e7, 1.0e7, 1.0e7],
    })
    factors = pl.DataFrame({
        "symbol": ["600001.SH"],
        "trade_date": [date(2024, 1, 4)],
        "ex_factor": [2.0],
    })
    instruments = pl.DataFrame({
        "symbol": ["600001.SH"],
        "name": ["普通股"],
        "float_shares": [1.0e8],
    })

    df = pipeline.compute_enriched(raw, factors=factors, instruments=instruments)

    day2 = df.filter(pl.col("date") == date(2024, 1, 3))
    assert day2["signal_limit_down_recovery"][0] is False
    day3 = df.filter(pl.col("date") == date(2024, 1, 4))
    assert day3["signal_limit_down_recovery"][0] is True


def test_matrix_limit_lock_uses_exchange_reference_on_ex_div_with_later_event(tmp_path):
    """除权日涨跌停判定价必须用交易所除权参考价, 不能直接用前复权昨收。

    事件: d3 除权 (ex_factor=1.25), d6 再除权 (ex_factor=1.25)。
    d3 的交易所参考价 = raw昨收/ex_factor = 10/1.25 = 8.00, 涨停价 8.80;
    前复权昨收 = 10/1.5625 = 6.40 (被 d6 事件额外缩放)。旧口径拿 6.40 当参考
    → 限价 7.04, raw 8.79 被误标"涨停锁死"。
    """
    root = tmp_path / "market"
    spec = [
        # (date, raw_close, F(d) = 该日之后所有事件因子乘积)
        (date(2024, 1, 2), 10.00, 1.5625),
        (date(2024, 1, 3), 10.00, 1.5625),
        (date(2024, 1, 4), 8.79, 1.25),   # ex1 除权日: 真涨停价 8.80, 未触及
        (date(2024, 1, 5), 8.70, 1.25),
        (date(2024, 1, 8), 8.70, 1.25),
        (date(2024, 1, 9), 7.00, 1.0),    # ex2 除权日 (窗口末日)
    ]
    for trade_date, raw, f in spec:
        partition = root / f"date={trade_date.isoformat()}"
        partition.mkdir(parents=True)
        pl.DataFrame({
            "symbol": ["600001.SH"],
            "date": [trade_date],
            "open": [raw], "high": [raw], "low": [raw],
            "close": [raw / f], "raw_close": [raw], "volume": [1000.0],
        }).write_parquet(partition / "part.parquet")

    market = load_market_data_matrix_from_parquet(
        root, spec[0][0], spec[-1][0],
        field_columns={"raw_close"},
        instruments=pl.DataFrame({"symbol": ["600001.SH"], "name": ["普通股"]}),
        cache_root=tmp_path / "cache",
    )
    # d3 除权日: 参考价 8.00 → 涨停 8.80 / 跌停 7.20, raw 8.79 均未触及
    assert market.limit_up_locked[2, 0] == 0
    assert market.limit_down_locked[2, 0] == 0
    # d6 第二次除权日 (窗口末日, F=1): 参考价 8.70/1.25 = 6.96,
    # 涨停 7.66 / 跌停 6.26, raw 7.00 未封板
    assert market.limit_up_locked[5, 0] == 0
    assert market.limit_down_locked[5, 0] == 0


def test_limit_price_on_ex_div_with_later_event_uses_exchange_reference():
    """enriched 路径同口径: 除权日 limit_up/down_price 用除权参考价, 不是前复权昨收。

    事件: d3 除权 (f=1.25), d4 再除权 (f=1.25)。
    d3 参考价 = 10/1.25 = 8.00 (前复权昨收 6.40 被事件额外缩放, 旧口径算出
    涨停价 7.04 → raw 8.79 误判涨停); d4 参考价 = 8.79/1.25 = 7.032,
    raw 7.74 恰为涨停价 → 正常检出 (防过度修正)。
    """
    raw = pl.DataFrame({
        "symbol": ["600001.SH"] * 4,
        "date": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)],
        "open": [10.00, 10.00, 8.79, 7.74],
        "high": [10.00, 10.00, 8.79, 7.74],
        "low": [10.00, 10.00, 8.79, 7.74],
        "close": [10.00, 10.00, 8.79, 7.74],
        "volume": [10000.0] * 4,
        "amount": [1.0e7] * 4,
    })
    factors = pl.DataFrame({
        "symbol": ["600001.SH"] * 2,
        "trade_date": [date(2024, 1, 4), date(2024, 1, 5)],
        "ex_factor": [1.25, 1.25],
    })
    instruments = pl.DataFrame({
        "symbol": ["600001.SH"], "name": ["普通股"], "float_shares": [1.0e8],
    })

    df = pipeline.compute_enriched(raw, factors=factors, instruments=instruments)

    d3 = df.filter(pl.col("date") == date(2024, 1, 4))
    # 落库列为含容差的交易所生效价: 涨停 8.80-0.005 / 跌停 7.20+0.005
    assert d3["limit_up_price"][0] == pytest.approx(8.795)
    assert d3["limit_down_price"][0] == pytest.approx(7.205)
    assert d3["signal_limit_up"][0] is False
    assert d3["consecutive_limit_ups"][0] == 0

    d4 = df.filter(pl.col("date") == date(2024, 1, 5))
    assert d4["limit_up_price"][0] == pytest.approx(7.725)
    assert d4["signal_limit_up"][0] is True
    assert d4["consecutive_limit_ups"][0] == 1


def test_chinext_302_codes_use_growth_board_rules():
    """深交所创业板已启用 302 号段 (如 2025-02-17 起的 302132.SZ 中航成飞),
    与 300/301 同为 ±20%, 注册制新股同样有 5 个交易日无涨跌幅窗口。"""
    assert price_limit_pct("302132.SZ", date(2026, 10, 16)) == pytest.approx(0.20)

    df = pl.DataFrame({
        "symbol": ["302132.SZ", "000001.SZ"],
        "trade_date": [date(2026, 10, 16)] * 2,
        "is_risk_warning": [False, False],
    })
    pct = df.select(
        polars_price_limit_pct(pl.col("symbol"), pl.col("trade_date"), pl.col("is_risk_warning"))
    ).to_series()
    assert pct.to_list() == pytest.approx([0.20, 0.10])


def test_chinext_302_no_limit_window_follows_gem_registration_date():
    from app.price_limits import no_limit_window_days

    # 2023-02-17 主板注册制之前、2020-08-24 创业板注册制之后上市
    assert no_limit_window_days("302001.SZ", date(2021, 6, 1)) == 5


# ---- 港股 (PR1 地基): 无涨跌停市场 + A 股专属函数的护栏 ----
#
# 港股没有涨跌幅限制, 因此 limit_pct 必须是 **None / null**, 不是 0 也不是一个
# 很大的数: 0 会让涨停价 == 跌停价 == 昨收, 大数会让涨停判定永不触发。两者都
# 不报错, 只是产出一个看起来合理的错误数字。
# A 股专属规则 (板块涨跌幅、注册制新股无涨跌幅窗口) 对港股无意义, 必须在入口
# 当场报错, 而不是静默套用 A 股规则。

HK_SYMBOL = "00700.HK"
HK_TRADE_DATE = date(2026, 10, 16)


@pytest.mark.parametrize(
    ("fn_name", "args"),
    [
        ("board_limit_pct", (HK_SYMBOL,)),
        ("no_limit_window_days", (HK_SYMBOL, date(2020, 1, 1))),
        ("no_limit_window_margin_days", (HK_SYMBOL,)),
        ("is_no_limit_day", (HK_SYMBOL, date(2020, 1, 1), HK_TRADE_DATE)),
    ],
)
def test_a_share_only_functions_reject_hk_symbols(fn_name, args):
    """A 股专属函数拿到港股代码必须抛错。

    ``is_no_limit_day`` 自身没有护栏, 靠内部调 ``no_limit_window_days`` 间接触达
    —— 一并参数化, 免得日后有人把它改成不走那条路而静默失去护栏。
    """
    from app import price_limits as price_limits_mod
    from app.market import SymbolMarketMismatchError

    with pytest.raises(SymbolMarketMismatchError):
        getattr(price_limits_mod, fn_name)(*args)


@pytest.mark.parametrize("market_arg", ["HK", "hk", Market.HK])
def test_price_limit_pct_is_none_for_hk(market_arg):
    """港股 limit_pct = None; market 接受枚举与字符串别名 (API query 天然是字符串)。"""
    assert price_limit_pct(HK_SYMBOL, HK_TRADE_DATE, market=market_arg) is None


def test_cross_market_declaration_raises_instead_of_returning_none():
    """⭐ 护栏必须在 ``has_price_limit`` 提前返回**之前**。

    顺序反了的话, A 股代码配上 market=HK 会静默拿到 None (「该市场无涨跌停」):
    涨停价算不出来、涨停判定静默失效, 且没有任何报错。反向 (港股代码配 A 股
    市场) 同样必须报错 —— 默认市场就是 CN, 漏传 market 是最常见的失误。
    """
    from app.market import SymbolMarketMismatchError

    with pytest.raises(SymbolMarketMismatchError):
        price_limit_pct("600519.SH", HK_TRADE_DATE, market="HK")
    with pytest.raises(SymbolMarketMismatchError):
        price_limit_pct(HK_SYMBOL, HK_TRADE_DATE)                 # 默认 market=CN
    with pytest.raises(SymbolMarketMismatchError):
        price_limit_pct(HK_SYMBOL, HK_TRADE_DATE, market="CN")


def test_polars_hk_branch_is_null_and_full_length_in_both_contexts():
    """⭐ 港股分支必须恒 null **且长度跟随 symbol 列**, 在 select 与 with_columns 下都对。

    裸 ``pl.lit(None, dtype=pl.Float64)`` 是长度 1 的标量: 在 ``with_columns``
    里会广播到帧高所以看不出问题, 一旦调用方改用 ``select`` 就静默只出 1 行
    (本文件上方的 A 股用例正是 select 写法, 说明这是活的调用形态)。
    """
    df = pl.DataFrame({
        "symbol": [HK_SYMBOL, "00005.HK", "09988.HK"],
        "trade_date": [HK_TRADE_DATE] * 3,
        "is_risk_warning": [False] * 3,
    })
    expr = polars_price_limit_pct(
        pl.col("symbol"), pl.col("trade_date"), pl.col("is_risk_warning"), market="HK"
    )

    selected = df.select(expr).to_series()
    assert len(selected) == 3, "select 上下文长度塌成 1 就是裸 pl.lit(None) 回归"
    assert selected.to_list() == [None, None, None]
    assert selected.dtype == pl.Float64, "null 列必须仍是 Float64, 否则下游算术会炸类型"

    joined = df.with_columns(expr.alias("pct"))
    assert joined.height == 3
    assert joined["pct"].to_list() == [None, None, None]


def test_polars_hk_null_propagates_to_limit_prices():
    """null limit_pct 必须让 limit_up/down 落 null, 而不是 0 或昨收本身。

    ⚠️ 必须显式 ``.alias()``: ``polars_limit_price`` 返回的表达式根名是
    ``prev_close``, 位置传进 ``with_columns`` 会按根名自动命名 → **静默覆盖
    昨收列**。这不是港股专属陷阱, 但港股分支产出 null, 覆盖后整列昨收变 null,
    下游算术全废, 所以这里顺带钉住。
    """
    df = pl.DataFrame({
        "symbol": [HK_SYMBOL, "00005.HK"],
        "trade_date": [HK_TRADE_DATE] * 2,
        "is_risk_warning": [False] * 2,
        "prev_close": [500.0, 100.0],
    })
    pct = polars_price_limit_pct(
        pl.col("symbol"), pl.col("trade_date"), pl.col("is_risk_warning"), market="HK"
    )
    out = df.with_columns(
        polars_limit_price(pl.col("prev_close"), pct, up=True).alias("limit_up_price"),
        polars_limit_price(pl.col("prev_close"), pct, up=False).alias("limit_down_price"),
    )

    assert out["prev_close"].to_list() == [500.0, 100.0], "昨收不得被改写"
    for col in ("limit_up_price", "limit_down_price"):
        assert out[col].to_list() == [None, None]
        assert out[col].dtype == pl.Float64, f"{col} 必须仍是 Float64, 否则下游算术炸类型"


def test_a_share_polars_path_is_unchanged():
    """A 股默认路径零回归: 不传 market 时行为与改造前逐位一致。"""
    df = pl.DataFrame({
        "symbol": ["600519.SH", "300750.SZ"],
        "trade_date": [HK_TRADE_DATE] * 2,
        "is_risk_warning": [False] * 2,
        "prev_close": [100.0, 100.0],
    })
    pct = polars_price_limit_pct(pl.col("symbol"), pl.col("trade_date"), pl.col("is_risk_warning"))
    out = df.with_columns(
        pct.alias("pct"),
        polars_limit_price(pl.col("prev_close"), pct, up=True).alias("limit_up_price"),
    )
    assert out["pct"].to_list() == pytest.approx([0.10, 0.20])
    assert out["limit_up_price"].to_list() == pytest.approx([110.0, 120.0])


def test_numpy_chain_rejects_mixed_market_input():
    """numpy 链经 ``board_limit_pct`` 免费继承护栏: 混合市场输入当场报错。

    护栏只加在 ``board_limit_pct`` 一处, 就覆盖了
    numpy_limit_pct_vectors → write_numpy_price_limit_matrix → numpy_price_limit_matrix
    整条链 (它们都逐 symbol 调它), 无需新增遍历。
    """
    from app.market import SymbolMarketMismatchError
    from app.price_limits import numpy_limit_pct_vectors

    names = ["贵州茅台", "腾讯控股"]
    with pytest.raises(SymbolMarketMismatchError):
        numpy_limit_pct_vectors(["600519.SH", HK_SYMBOL], names)
    with pytest.raises(SymbolMarketMismatchError):
        numpy_price_limit_matrix([HK_TRADE_DATE], ["600519.SH", HK_SYMBOL], names)


def test_numpy_matrix_a_share_date_semantics_unchanged():
    """A 股矩阵的三段日期语义零回归 (创业板注册制前回落主板 / ST 改限日前 5%)。"""
    from app.price_limits import (
        GEM_20PCT_LIMIT_START_DATE,
        MAIN_BOARD_ST_LIMIT_CHANGE_DATE,
    )

    symbols = ["600519.SH", "300750.SZ", "832000.BJ", "600519.SH"]
    names = ["贵州茅台", "宁德时代", "北交所票", "ST邯郸"]
    dates = [
        GEM_20PCT_LIMIT_START_DATE - timedelta(days=1),   # 创业板注册制前
        MAIN_BOARD_ST_LIMIT_CHANGE_DATE - timedelta(days=1),  # ST 改限前
        MAIN_BOARD_ST_LIMIT_CHANGE_DATE,                  # ST 改限当日
    ]
    matrix = numpy_price_limit_matrix(dates, symbols, names)

    assert matrix.shape == (3, 4)
    assert matrix.dtype == np.float32
    # 注册制前: 创业板回落主板 10%; 主板 ST 5%; 北交所 30%
    assert list(matrix[0]) == pytest.approx([0.10, 0.10, 0.30, 0.05])
    # 注册制后、ST 改限前: 创业板 20%; ST 仍 5%
    assert list(matrix[1]) == pytest.approx([0.10, 0.20, 0.30, 0.05])
    # ST 改限日起: 主板 ST 回到 10%
    assert list(matrix[2]) == pytest.approx([0.10, 0.20, 0.30, 0.10])
