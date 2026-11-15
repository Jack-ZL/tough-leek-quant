"""维表涨停价为「无涨跌停限制」哨兵值 (>= 10000) 时, 盘后全量与实时路径同口径不判涨跌停。

注册制新股上市前 5 个交易日无涨跌幅限制, 数据源在维表 limit_up 填上万的占位值。
识别该哨兵后涨跌停判定价 (limit_up_price / limit_down_price) 为 null —— 引用判定价的
signal_limit_* 自定义信号条件一律不成立, 连板数归零; 否则盘后全量 (compute_limit_signals)
回退按 10%/20%/30% 理论价判断, 新股当日涨超 10% 就被记为涨停、连板数 +1, 收盘后看板
涨停家数、连板梯队与涨停类策略都会把它算进去, 与盘中结论相反。

listing_date 窗口判定 (price_limits.is_no_limit_day) 补齐哨兵覆盖不到的部分:
维表 as_of 只命中一个行情日, 窗口内其余交易日与 as_of 漂移后的历史重算由
listing_date 判定兜底 (601091.SH 2026-09-18: 昨收 20.8 收 57.77 被误记涨停+1 连板)。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.indicators import pipeline
from app.market import Market
from app.price_limits import (
    BJ_OPENING_DATE,
    GEM_REGISTRATION_DATE,
    MAIN_BOARD_REGISTRATION_DATE,
    is_no_limit_day,
    no_limit_window_days,
)

YESTERDAY = date(2026, 9, 16)
TODAY = date(2026, 9, 17)


def _rows(prev_close: float, *, open_: float, high: float, low: float, close: float) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["001234.SZ", "001234.SZ"],
        "date": [YESTERDAY, TODAY],
        "open": [prev_close, open_],
        "high": [prev_close, high],
        "low": [prev_close, low],
        "close": [prev_close, close],
        "raw_close": [prev_close, close],
        "raw_high": [prev_close, high],
        "raw_low": [prev_close, low],
        "volume": [1000.0, 1000.0],
    })


def _instruments(limit_up: float, limit_down: float, as_of: date = TODAY, symbol: str = "001234.SZ") -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol],
        "name": ["新股"],
        "limit_up": [limit_up],
        "limit_down": [limit_down],
        "as_of": [as_of],
    })


def _realtime(rows: pl.DataFrame, instruments: pl.DataFrame) -> dict:
    today = rows.filter(pl.col("date") == TODAY).with_columns(
        pl.lit(rows["close"][0]).alias("_prev_close_raw"),
    )
    return pipeline._compute_limit_signals_today(today, instruments).row(0, named=True)


# 昨收 20.00, 主板理论涨停 22.00 / 跌停 18.00; 判定价含 ∓0.005 容差
CASES = {
    "up_close": dict(open_=21.0, high=23.5, low=20.5, close=23.4),        # 涨超 10% 收盘
    "up_touch_fade": dict(open_=21.0, high=22.5, low=20.5, close=21.5),   # 冲过理论涨停价后回落
    "down_close": dict(open_=19.0, high=19.5, low=17.5, close=17.6),      # 跌超 10% 收盘
    "down_touch_up": dict(open_=18.2, high=19.0, low=17.8, close=18.8),   # 跌破理论跌停价后收阳
}
LIMIT_PRICES = ("limit_up_price", "limit_down_price")


@pytest.mark.parametrize("case", list(CASES))
def test_full_path_no_price_limit_sentinel_matches_realtime(case):
    rows = _rows(20.0, **CASES[case])
    instruments = _instruments(limit_up=100000.0, limit_down=0.0)

    full = pipeline.compute_limit_signals(rows, instruments).row(-1, named=True)
    realtime = _realtime(rows, instruments)

    for price in LIMIT_PRICES:
        assert realtime[price] is None, price
        assert full[price] is None, price
    assert full["consecutive_limit_ups"] == 0
    assert full["consecutive_limit_downs"] == 0


def test_down_signals_alone_also_respect_sentinel():
    rows = _rows(20.0, **CASES["down_close"])
    instruments = _instruments(limit_up=100000.0, limit_down=0.0)

    full = pipeline.compute_limit_signals(
        rows, instruments, needed={"limit_down_price", "consecutive_limit_downs"},
    ).row(-1, named=True)

    assert full["limit_down_price"] is None
    assert full["consecutive_limit_downs"] == 0


def test_sentinel_only_applies_to_matching_instrument_date():
    """维表日期不是该行情日 (历史行) 时无法得知是否无限制, 保持理论价口径。"""
    rows = _rows(20.0, **CASES["up_close"])
    stale = _instruments(limit_up=100000.0, limit_down=0.0, as_of=YESTERDAY)

    full = pipeline.compute_limit_signals(rows, stale).row(-1, named=True)

    assert full["limit_up_price"] == pytest.approx(21.995)
    assert full["consecutive_limit_ups"] == 1


def test_regular_stock_limit_up_unchanged():
    rows = _rows(20.0, open_=21.0, high=22.0, low=20.5, close=22.0)
    instruments = _instruments(limit_up=22.0, limit_down=18.0)

    full = pipeline.compute_limit_signals(rows, instruments).row(-1, named=True)

    assert full["limit_up_price"] == pytest.approx(21.995)
    assert full["raw_close"] >= full["limit_up_price"]
    assert full["consecutive_limit_ups"] == 1


# ================================================================
# listing_date 无涨跌幅窗口判定 (哨兵覆盖不到的历史行兜底)
# ================================================================

def test_window_days_by_board_and_listing_date():
    listing = date(2026, 9, 17)
    # 沪主板 / 创业板 / 科创板 / 北交所: 注册制后上市 → 窗口 5 / 5 / 5 / 1
    assert no_limit_window_days("601091.SH", listing) == 5
    assert no_limit_window_days("301234.SZ", listing) == 5
    assert no_limit_window_days("688001.SH", listing) == 5
    assert no_limit_window_days("920001.BJ", listing) == 1
    # 注册制改革前上市的老股 → 不适用
    assert no_limit_window_days("600001.SH", MAIN_BOARD_REGISTRATION_DATE) == 5
    assert no_limit_window_days("600001.SH", date(2023, 2, 16)) == 0
    assert no_limit_window_days("300001.SZ", GEM_REGISTRATION_DATE) == 5
    assert no_limit_window_days("300001.SZ", date(2020, 8, 23)) == 0
    assert no_limit_window_days("920001.BJ", BJ_OPENING_DATE) == 1
    assert no_limit_window_days("600001.SH", None) == 0


def test_is_no_limit_day_with_day_rank_exact():
    listing = date(2026, 9, 10)
    # 序号精确门控: 前 5 个交易日内 True, 第 6 日起 False
    assert is_no_limit_day("601091.SH", listing, date(2026, 9, 10), day_rank=1) is True
    assert is_no_limit_day("601091.SH", listing, date(2026, 9, 16), day_rank=5) is True
    assert is_no_limit_day("601091.SH", listing, date(2026, 9, 17), day_rank=6) is False
    # 北交所仅首日
    assert is_no_limit_day("920001.BJ", listing, listing, day_rank=1) is True
    assert is_no_limit_day("920001.BJ", listing, date(2026, 9, 11), day_rank=2) is False


def test_full_path_listing_date_window_marks_no_limit_on_stale_as_of():
    """C沈鼓场景: 维表 as_of (09-20) 与行情日 (09-17) 不一致, 哨兵失效;
    listing_date 窗口兜底, 涨超 10% 不再被误记涨停。"""
    rows = _rows(20.0, **CASES["up_close"])
    instruments = _instruments(limit_up=0.0, limit_down=0.0, as_of=date(2026, 9, 20))
    instruments = instruments.with_columns(
        pl.lit(TODAY).cast(pl.Date).alias("listing_date")
    )

    full = pipeline.compute_limit_signals(rows, instruments).row(-1, named=True)

    for price in LIMIT_PRICES:
        assert full[price] is None, price
    assert full["consecutive_limit_ups"] == 0
    assert full["consecutive_limit_downs"] == 0


def test_sixth_trading_day_after_window_resumes_limit_signals():
    """窗口第 6 个交易日起恢复正常涨跌停判定 (601091: 上市 09-10, 09-17 是第 6 日)。"""
    dates = [date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 14),
             date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)]
    closes = [20.0, 20.0, 20.0, 20.0, 20.0, 22.0]  # 第 6 日收盘 = 理论涨停 22.00
    rows = pl.DataFrame({
        "symbol": ["601091.SH"] * 6,
        "date": dates,
        "open": closes, "high": closes, "low": [19.8] * 5 + [21.0], "close": closes,
        "raw_close": closes, "raw_high": closes, "raw_low": [19.8] * 5 + [21.0],
        "volume": [1000.0] * 6,
    })
    instruments = _instruments(limit_up=0.0, limit_down=0.0, as_of=date(2026, 9, 20), symbol="601091.SH")
    instruments = instruments.with_columns(
        pl.lit(date(2026, 9, 10)).cast(pl.Date).alias("listing_date")
    )

    full = pipeline.compute_limit_signals(rows, instruments).row(-1, named=True)

    assert full["limit_up_price"] == pytest.approx(21.995)
    assert full["consecutive_limit_ups"] == 1


def test_pre_registration_listing_keeps_theoretical_limit():
    """注册制改革前上市的老股不受窗口判定影响, 理论价照判。"""
    rows = _rows(20.0, **CASES["up_close"])
    instruments = _instruments(limit_up=0.0, limit_down=0.0, as_of=date(2026, 9, 20))
    instruments = instruments.with_columns(
        pl.lit(date(2019, 6, 1)).cast(pl.Date).alias("listing_date")
    )

    full = pipeline.compute_limit_signals(rows, instruments).row(-1, named=True)

    assert full["limit_up_price"] == pytest.approx(21.995)
    assert full["consecutive_limit_ups"] == 1


def test_partial_local_history_old_stock_not_marked_no_limit():
    """listing 近但本地日K首行晚于窗口边界 (历史被裁剪) 时序号不可信, 不标记。"""
    # 上市 2026-09-10, 本地历史从 09-16 开始 (首行在窗口外) → 回退理论价
    rows = _rows(20.0, **CASES["up_close"])  # 行情日 09-16/09-17
    instruments = _instruments(limit_up=0.0, limit_down=0.0, as_of=date(2026, 9, 20))
    instruments = instruments.with_columns(
        pl.lit(date(2026, 9, 1)).cast(pl.Date).alias("listing_date")
    )

    full = pipeline.compute_limit_signals(rows, instruments).row(-1, named=True)

    assert full["limit_up_price"] == pytest.approx(21.995)


def test_realtime_path_listing_date_window_without_sentinel():
    """实时路径: 维表无哨兵 (limit_up=0) 但 listing_date 命中窗口 → 不判涨停。"""
    rows = _rows(20.0, **CASES["up_close"])
    instruments = _instruments(limit_up=0.0, limit_down=0.0).with_columns(
        pl.lit(TODAY).cast(pl.Date).alias("listing_date")
    )

    realtime = _realtime(rows, instruments)

    for price in LIMIT_PRICES:
        assert realtime[price] is None, price


# ---------------------------------------------------------------------------
# 港股: 无涨跌停是**市场规则**级事实, 不是维表哨兵
# ---------------------------------------------------------------------------

def _hk_rows(prev_close: float, *, close: float) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["00700.HK", "00700.HK"],
        "date": [YESTERDAY, TODAY],
        "open": [prev_close, prev_close],
        "high": [prev_close, close * 1.01],
        "low": [prev_close, close * 0.99],
        "close": [prev_close, close],
        "raw_close": [prev_close, close],
        "raw_high": [prev_close, close * 1.01],
        "raw_low": [prev_close, close * 0.99],
        "volume": [1_000_000.0, 1_000_000.0],
    })


def _hk_instruments(float_shares: float = 200_000_000.0) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["00700.HK"],
        "name": ["腾讯控股"],
        # 港股无涨跌停 → 维表哨兵列恒 None (见 plugins/eastmoney/hk.py:hk_instrument_rows)
        "limit_up": pl.Series([None], dtype=pl.Float64),
        "limit_down": pl.Series([None], dtype=pl.Float64),
        "listing_date": pl.Series([None], dtype=pl.Date),
        "as_of": [TODAY],
        "float_shares": [float_shares],
    })


def test_hk_big_move_not_limit_up_on_both_paths():
    """港股 +30% 在**批量与实时两条路径**都不判涨停, 限价列恒 null。

    A 股的「无涨跌停」靠维表哨兵值 (>=10000) 或 listing_date 窗口识别; 港股是
    **市场规则**级无涨跌停 (``HK_RULES.has_price_limit=False``), 维表 limit_up /
    limit_down 恒 None, 没有哨兵可认。

    🔴 两条路径都必须把 ``market`` 透传到 ``polars_price_limit_pct``。它是**表达式级**
    函数, 无法逐行抛错 (见其 docstring), 所以漏传 market **不会报错**, 而是按 A 股
    主板 10% 构造表达式 —— ``00700.HK`` 不匹配 300/301/302/688/689/.BJ 任何前缀,
    落到默认 10%, 于是 +30% 被记成涨停、连板数 +1。变异检验实测: 删掉批量路径的
    ``market=market`` 后, 港股 enriched 的 121 项测试**全绿** (因为 limit_up_price
    不在 ENRICHED_STORAGE_COLS 里, 落盘看不出), 只有本用例能抓到。
    """
    rows = _hk_rows(100.0, close=130.0)  # +30%, 远超 A 股任何板块涨跌幅
    inst = _hk_instruments()

    full = pipeline.compute_limit_signals(rows, inst, market=Market.HK).row(-1, named=True)
    today = rows.filter(pl.col("date") == TODAY).with_columns(
        pl.lit(rows["close"][0]).alias("_prev_close_raw"),
    )
    realtime = pipeline._compute_limit_signals_today(
        today, inst, market=Market.HK
    ).row(0, named=True)

    for label, r in (("批量", full), ("实时", realtime)):
        for price in LIMIT_PRICES:
            assert r[price] is None, f"{label}路径 {price} 应为 null, 实得 {r[price]}"
        assert r["consecutive_limit_ups"] == 0, f"{label}路径不该记涨停连板"
        assert r["consecutive_limit_downs"] == 0, f"{label}路径不该记跌停连板"


def test_hk_realtime_turnover_uses_share_unit():
    """实时路径的港股换手率也走「股」单位 (与批量路径同系数)。

    volume=1e6 股, float_shares=2e8 → 0.5%。若误用 A 股系数 (手→股 再 ×100)
    会得 50%, 差 100 倍。
    """
    rows = _hk_rows(100.0, close=101.0)
    inst = _hk_instruments(float_shares=200_000_000.0)
    today = rows.filter(pl.col("date") == TODAY).with_columns(
        pl.lit(rows["close"][0]).alias("_prev_close_raw"),
    )

    out = pipeline._compute_limit_signals_today(today, inst, market=Market.HK).row(0, named=True)
    assert out["turnover_rate"] == pytest.approx(0.5), out["turnover_rate"]
