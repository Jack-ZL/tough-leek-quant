"""app.market_time 的多市场改造测试。

改造把时段常量换成了 :mod:`app.market` 规则表查表。本文件钉住两件事:

1. **A 股逐位不变** — 边界时刻的 ``in_continuous_session`` 与
   ``trading_minutes_elapsed_from_dt`` 必须与改造前的硬编码实现完全一致。
   这些值是量比、盘中轮询窗口、分钟增量的分母, 差一分钟就会让指标偏移。
2. **港股与半日市正确** — 330 分钟全日市 / 150 分钟半日市, 且港A 在
   11:30-12:00 与 15:00-16:00 两段**必须给出不同答案** (这正是改造的目的)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.market import Market, clear_half_days, register_half_days
from app.market_time import (
    CN_TZ,
    cn_now,
    cn_today,
    in_continuous_session,
    market_now,
    market_today,
    trading_minutes_elapsed,
    trading_minutes_elapsed_from_dt,
    trading_minutes_elapsed_from_ts,
)

UTC8 = timezone(timedelta(hours=8))
# 2026-09-07 是周一 (非节假日), 用于所有时段断言
MON = date(2026, 9, 7)
SAT = date(2026, 8, 29)


def _dt(d: date, h: int, m: int, s: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=UTC8)


@pytest.fixture(autouse=True)
def _clean_half_days():
    clear_half_days()
    yield
    clear_half_days()


# ================================================================
# 向后兼容: 既有导出与默认行为
# ================================================================

def test_all_pre_refactor_symbols_still_exported():
    """全仓 51 个文件引用本模块, 改造不得删任何既有导出。"""
    from app import market_time

    for name in (
        "CN_TZ", "cn_now", "cn_today", "in_continuous_session",
        "trading_minutes_elapsed", "trading_minutes_elapsed_from_dt",
        "trading_minutes_elapsed_from_ts",
        # 旧常量: 已不是判定依据, 但可能有外部引用, 保留
        "_TRADING_TOTAL_MINUTES", "_MORNING_START", "_MORNING_END",
        "_AFTERNOON_START", "_AFTERNOON_END",
    ):
        assert hasattr(market_time, name), f"丢失既有导出: {name}"


def test_cn_tz_is_still_utc_plus_8():
    """44 处引用 CN_TZ 做时间戳落地, 偏移量变了会整体错 8 小时。"""
    assert CN_TZ.utcoffset(None) == timedelta(hours=8)
    assert cn_now().tzinfo is CN_TZ
    assert market_now().tzinfo == CN_TZ
    assert market_now(Market.HK).utcoffset() == timedelta(hours=8), "港股同为 UTC+8"


def test_legacy_constants_match_rules_table():
    """保留的旧常量必须与规则表一致, 否则读旧常量的代码会与新代码分叉。"""
    from app.market import CN_RULES
    from app.market_time import (
        _AFTERNOON_END,
        _AFTERNOON_START,
        _MORNING_END,
        _MORNING_START,
        _TRADING_TOTAL_MINUTES,
    )

    am, pm = CN_RULES.sessions
    assert (am.start, am.end) == (_MORNING_START, _MORNING_END)
    assert (pm.start, pm.end) == (_AFTERNOON_START, _AFTERNOON_END)
    assert CN_RULES.trading_total_minutes == _TRADING_TOTAL_MINUTES == 240


def test_default_market_is_cn_everywhere():
    """不传 market 必须等价于传 CN —— 这是「A 股零影响」的实现方式。"""
    now = _dt(MON, 10, 0)
    assert in_continuous_session(now) == in_continuous_session(now, Market.CN)
    assert in_continuous_session(now) == in_continuous_session(now, "cn")
    assert trading_minutes_elapsed_from_dt(now) == trading_minutes_elapsed_from_dt(now, Market.CN)
    assert trading_minutes_elapsed_from_dt(now) == trading_minutes_elapsed_from_dt(now, "cn")
    assert cn_today() == market_today() == market_today(Market.CN)


def test_market_today_and_cn_today_agree():
    assert market_today(Market.CN) == cn_today()
    assert market_today(Market.HK) == cn_today(), "沪深港同为 UTC+8, 日期必然相同"


# ================================================================
# A 股: 与改造前逐位一致
# ================================================================

@pytest.mark.parametrize("h,m,s,expected", [
    (9, 29, 59, False), (9, 30, 0, True), (10, 0, 0, True),
    (11, 29, 59, True), (11, 30, 0, True), (11, 30, 1, False),
    (12, 0, 0, False), (12, 59, 59, False),
    (13, 0, 0, True), (14, 0, 0, True),
    (14, 59, 59, True), (15, 0, 0, True), (15, 0, 1, False),
    (16, 0, 0, False), (8, 0, 0, False), (20, 0, 0, False),
])
def test_cn_in_continuous_session_boundaries(h, m, s, expected):
    """边界含端点 (``start <= t <= end``): 9:30:00 与 15:00:00 都算盘中。"""
    assert in_continuous_session(_dt(MON, h, m, s)) is expected


def test_cn_weekend_is_never_in_session():
    for h in range(24):
        assert in_continuous_session(_dt(SAT, h, 30)) is False


@pytest.mark.parametrize("h,m,s,expected", [
    (9, 0, 0, 0.0),          # 开盘前
    (9, 29, 59, 0.0),
    (9, 30, 0, 0.0),         # 刚开盘
    (10, 0, 0, 30.0),
    (10, 30, 0, 60.0),
    (11, 29, 0, 119.0),
    (11, 30, 0, 120.0),      # 早盘收
    (11, 30, 1, 120.0),      # 越过端点仍是 120, 不会变 120.016
    (12, 0, 0, 120.0),       # 午休
    (12, 59, 59, 120.0),
    (13, 0, 0, 120.0),       # 午盘开
    (13, 30, 0, 150.0),
    (14, 0, 0, 180.0),
    (14, 59, 0, 239.0),
    (15, 0, 0, 240.0),       # 收盘
    (15, 0, 1, 240.0),
    (16, 0, 0, 240.0),
    (23, 59, 59, 240.0),
])
def test_cn_trading_minutes_elapsed_boundaries(h, m, s, expected):
    assert trading_minutes_elapsed_from_dt(_dt(MON, h, m, s)) == pytest.approx(expected, abs=1e-9)


def test_cn_seconds_are_fractional_minutes():
    assert trading_minutes_elapsed_from_dt(_dt(MON, 10, 0, 30)) == pytest.approx(30.5)
    assert trading_minutes_elapsed_from_dt(_dt(MON, 14, 59, 59)) == pytest.approx(239 + 59 / 60)


def test_cn_weekend_returns_full_day():
    """周末返回**全天** 240 而不是 0 — 否则量比分母为 0 会被折算成 0。"""
    assert trading_minutes_elapsed_from_dt(_dt(SAT, 3, 0)) == 240.0
    assert trading_minutes_elapsed_from_dt(_dt(SAT, 23, 0)) == 240.0


def test_cn_total_is_240_at_close():
    assert trading_minutes_elapsed_from_dt(_dt(MON, 15, 0)) == 240.0


# ================================================================
# 港股: 与 A 股必须分叉
# ================================================================

@pytest.mark.parametrize("h,m,s,expected", [
    (9, 29, 59, False), (9, 30, 0, True),
    (11, 30, 0, True),      # A 股此刻已收早盘
    (11, 45, 0, True),      # ⭐ A 股 False, 港股 True
    (12, 0, 0, True),       # 港股早盘到 12:00 (含端点)
    (12, 0, 1, False),
    (12, 30, 0, False),     # 港股午休 12:00-13:00
    (13, 0, 0, True),
    (15, 0, 0, True),       # ⭐ A 股此刻已收盘
    (15, 30, 0, True),      # ⭐ A 股 False, 港股 True
    (16, 0, 0, True),       # 港股收盘 16:00 (含端点)
    (16, 0, 1, False),
])
def test_hk_in_continuous_session_boundaries(h, m, s, expected):
    assert in_continuous_session(_dt(MON, h, m, s), Market.HK) is expected


@pytest.mark.parametrize("h,m,expected", [
    (9, 0, 0.0), (9, 30, 0.0),
    (10, 30, 60.0),
    (11, 30, 120.0),     # ⭐ A 股此刻已是午休平台 120, 港股仍在早盘计数
    (12, 0, 150.0),      # 早盘收 = 150
    (12, 30, 150.0),     # 午休
    (13, 0, 150.0),
    (14, 0, 210.0),
    (15, 0, 270.0),      # ⭐ A 股此刻已封顶 240
    (16, 0, 330.0),      # 收盘 = 330
    (17, 0, 330.0),
])
def test_hk_trading_minutes_elapsed_boundaries(h, m, expected):
    assert trading_minutes_elapsed_from_dt(
        _dt(MON, h, m), Market.HK
    ) == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("h,m", [(11, 45), (15, 30), (12, 0), (16, 0)])
def test_cn_and_hk_disagree_where_they_must(h, m):
    """改造的目的: 同一时刻两个市场给出不同答案。

    若这里相等, 说明 market 参数没有真正生效 (例如仍读模块级 A 股常量)。
    """
    now = _dt(MON, h, m)
    cn, hk = in_continuous_session(now), in_continuous_session(now, Market.HK)
    assert cn is False and hk is True, f"{h}:{m} 应 A股休市/港股盘中, 实际 cn={cn} hk={hk}"
    assert trading_minutes_elapsed_from_dt(now) != trading_minutes_elapsed_from_dt(now, Market.HK)


def test_hk_weekend_returns_hk_full_day_not_cn():
    """周末兜底也必须按市场取总分钟数 (330 而不是 240)。"""
    assert trading_minutes_elapsed_from_dt(_dt(SAT, 3, 0), Market.HK) == 330.0


def test_hk_string_and_enum_market_agree():
    now = _dt(MON, 15, 30)
    assert in_continuous_session(now, "hk") is in_continuous_session(now, Market.HK)
    assert trading_minutes_elapsed_from_dt(now, "hk") == pytest.approx(
        trading_minutes_elapsed_from_dt(now, Market.HK)
    )


# ================================================================
# 半日市
# ================================================================

HALF_DAY = date(2026, 12, 24)   # 圣诞前夕, HKEX 惯例半日市


def test_hk_half_day_closes_at_noon():
    register_half_days(Market.HK, [HALF_DAY])
    assert in_continuous_session(_dt(HALF_DAY, 11, 45), Market.HK) is True
    assert in_continuous_session(_dt(HALF_DAY, 12, 0), Market.HK) is True
    assert in_continuous_session(_dt(HALF_DAY, 13, 0), Market.HK) is False, "半日市下午休市"
    assert in_continuous_session(_dt(HALF_DAY, 15, 30), Market.HK) is False


def test_hk_half_day_caps_minutes_at_morning_session():
    """量比分母必须是 150, 不是 330 —— 否则量比低估约 2.2 倍。"""
    register_half_days(Market.HK, [HALF_DAY])
    assert trading_minutes_elapsed_from_dt(_dt(HALF_DAY, 12, 0), Market.HK) == 150.0
    assert trading_minutes_elapsed_from_dt(_dt(HALF_DAY, 13, 0), Market.HK) == 150.0
    assert trading_minutes_elapsed_from_dt(_dt(HALF_DAY, 16, 0), Market.HK) == 150.0
    assert trading_minutes_elapsed_from_dt(_dt(HALF_DAY, 23, 0), Market.HK) == 150.0


def test_hk_half_day_does_not_change_other_days():
    register_half_days(Market.HK, [HALF_DAY])
    other = date(2026, 12, 23)
    assert trading_minutes_elapsed_from_dt(_dt(other, 16, 0), Market.HK) == 330.0
    assert in_continuous_session(_dt(other, 15, 30), Market.HK) is True


def test_cn_unaffected_by_hk_half_day():
    """注册港股半日市不得改动 A 股任何时刻的判定。"""
    before = [
        (in_continuous_session(_dt(HALF_DAY, h, m)), trading_minutes_elapsed_from_dt(_dt(HALF_DAY, h, m)))
        for h, m in [(9, 30), (11, 30), (12, 0), (13, 0), (15, 0), (16, 0)]
    ]
    register_half_days(Market.HK, [HALF_DAY])
    after = [
        (in_continuous_session(_dt(HALF_DAY, h, m)), trading_minutes_elapsed_from_dt(_dt(HALF_DAY, h, m)))
        for h, m in [(9, 30), (11, 30), (12, 0), (13, 0), (15, 0), (16, 0)]
    ]
    assert before == after
    assert trading_minutes_elapsed_from_dt(_dt(HALF_DAY, 15, 0)) == 240.0


def test_default_empty_half_day_table_degrades_to_full_day():
    """默认空表 → 退化为全日市。这是刻意的安全方向 (见 app.market 文档)。"""
    assert trading_minutes_elapsed_from_dt(_dt(HALF_DAY, 16, 0), Market.HK) == 330.0
    assert in_continuous_session(_dt(HALF_DAY, 15, 30), Market.HK) is True


# ================================================================
# 时间戳入口
# ================================================================

def test_from_ts_uses_market_timezone_and_sessions():
    """``from_ts`` 是唯一做时区换算的入口 (其余函数要求传入已是市场本地时间)。"""
    # 2026-09-07 15:30 UTC+8 == 07:30 UTC
    ts_ms = int(datetime(2026, 9, 7, 7, 30, tzinfo=UTC).timestamp() * 1000)
    cn = trading_minutes_elapsed_from_ts(ts_ms)
    hk = trading_minutes_elapsed_from_ts(ts_ms, Market.HK)
    assert cn == 240.0, "A 股 15:30 已收盘 → 240"
    assert hk == 300.0, "港股 15:30 = 150 + 150 → 300"


@pytest.mark.parametrize("bad", [None, 0, "", "not-a-number"])
def test_from_ts_invalid_returns_market_full_day(bad):
    """无效戳返回**该市场**的全天分钟数, 不是 0 (否则量比被折算成 0)。"""
    assert trading_minutes_elapsed_from_ts(bad) == 240.0
    assert trading_minutes_elapsed_from_ts(bad, Market.HK) == 330.0


def test_from_ts_rejects_unknown_market():
    ts_ms = int(datetime(2026, 9, 7, 7, 30, tzinfo=UTC).timestamp() * 1000)
    with pytest.raises(ValueError, match="未知市场"):
        trading_minutes_elapsed_from_ts(ts_ms, "us")


def test_trading_minutes_elapsed_accepts_market():
    """无参入口 (服务端当前时间) 也必须支持 market。"""
    cn, hk = trading_minutes_elapsed(), trading_minutes_elapsed(Market.HK)
    assert 0.0 <= cn <= 240.0
    assert 0.0 <= hk <= 330.0


# ================================================================
# 契约: 不做隐式时区换算
# ================================================================

def test_naive_and_utc_datetimes_are_taken_at_face_value():
    """本模块**不**换算时区 (与改造前一致): 传什么钟点就按什么钟点算。

    钉住这条是为了防止后人「顺手加个 astimezone」—— 那会让 46 处既有调用方
    (它们传的已经是北京时间) 整体偏移 8 小时。需要换算请在调用侧做。
    """
    naive = datetime(2026, 9, 7, 10, 0)                      # 无 tzinfo
    as_utc = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)  # 同一钟点, 不同 tz
    assert trading_minutes_elapsed_from_dt(naive) == 30.0
    assert trading_minutes_elapsed_from_dt(as_utc) == 30.0, "不得隐式换算时区"
    assert in_continuous_session(naive) is True
    assert in_continuous_session(as_utc) is True


def test_unknown_market_raises_rather_than_defaulting_to_cn():
    now = _dt(MON, 10, 0)
    for bad in ("us", "US", "xx", None):
        with pytest.raises(ValueError, match="未知市场"):
            in_continuous_session(now, bad)
        with pytest.raises(ValueError, match="未知市场"):
            trading_minutes_elapsed_from_dt(now, bad)
