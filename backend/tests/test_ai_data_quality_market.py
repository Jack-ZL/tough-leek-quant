# AI 数据体检的 market 维 (方案 §9.2)。
#
# 🔴 钉住的是一类**静默错误**: 体检模块原来只有 A 股口径 —— 定版线写死 15:35、
# 日历取 fuyao A 股年份表、附加检查用 `asset_type == "stock"` 做闸。港股代码经
# `repository.resolve_asset_type` 兜底正好返回 "stock", 于是港股被当成
# 「缺财务的 A 股」, 且**每个交易日 15:35 之后**都被误判「日K停更」。
# 停更告警会随落后天数升级到 high 硬伤 → hard=True → 用户开启
# ai_quality_block_enabled 时直接拒绝分析。
#
# 与 test_ai_data_quality.py 的分工: 那边钉**评分规则本身**(A 股口径, 一行未改),
# 这边钉**市场分派** —— 同一份数据在两个市场必须得出不同结论, 且港股真停更时
# 照样要报 hard (不能为了消误报把检查阉割掉)。
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.market import Market
from app.services import ai_data_quality as adq

# ===== 工具 =====

def make_df(n: int, end: date, vol: int = 1000, ma20: float | None = 10.0) -> pl.DataFrame:
    """构造 n 根工作日日K (最后一天 = end)。与 test_ai_data_quality.make_df 同口径。"""
    dates: list[date] = []
    d = end
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d -= timedelta(days=1)
    dates.reverse()
    return pl.DataFrame({
        "date": dates,
        "close": [10.0] * n,
        "volume": [vol] * n,
        "ma20": [ma20] * n,
    })


HK = "00700.HK"
CN = "600519.SH"
EMPTY_FINS: dict[str, list[dict]] = {"metrics": [], "income": []}

# 2026-02-04 是周三。15:50 这个时刻是全部误报的来源:
#   A 股 15:00 收盘, 15:35 定版线已过 → 期望最后交易日 = 02-04 (当天)
#   港股 16:00 才收盘, 16:40 定版线未到 → 期望最后交易日 = 02-03 (昨天)
# 同一份「最后一根 = 02-03」的帧, 两个市场的正确结论**必须不同**。
AT_1550 = datetime(2026, 2, 4, 15, 50)
AT_1620 = datetime(2026, 2, 4, 16, 20)   # 港股仍未到 16:40 定版线
AT_2000 = datetime(2026, 2, 4, 20, 0)     # 两个市场定版线都已过
PREV_DAY = date(2026, 2, 3)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例: 清结果缓存 + 工作日日历 (不依赖 fuyao 年份表)。"""
    adq._cache.clear()
    monkeypatch.setattr(adq, "_trading_calendar", lambda: None)
    yield
    adq._cache.clear()


def score(tmp_path: Path, symbol: str, df: pl.DataFrame, *, now: datetime,
          fins=None, asset_type: str = "stock", **kw) -> dict:
    return adq.score_symbol(
        tmp_path, symbol, df,
        EMPTY_FINS if fins is None else fins,
        asset_type, now=now, use_cache=False, **kw,
    )


def codes(quality: dict) -> set[str]:
    return {i["code"] for i in quality["issues"]}


def severities(quality: dict, code: str) -> list[str]:
    return [i["severity"] for i in quality["issues"] if i["code"] == code]


# ===== 定版线: 港股不再被 A 股 15:35 误判停更 =====

def test_hk_not_stale_before_hk_cutoff_but_cn_is(tmp_path):
    """🔴 15:50 同一份帧: A 股报停更, 港股不报。

    这是本 PR 修的核心误报。旧实现把 (15, 35) 写死在 _expected_last_trading_day
    函数体内, 港股在**每个交易日 15:35-16:40 之间**都会拿到一条 kline_stale;
    半日市 (12:00 收盘) 整个下午都在误判。
    """
    df = make_df(90, PREV_DAY)
    hk = score(tmp_path, HK, df, now=AT_1550)
    cn = score(tmp_path, CN, df, now=AT_1550)

    assert "kline_stale" not in codes(hk), f"港股 15:50 尚未收盘, 不该判停更: {hk['issues']}"
    assert hk["score"] == 100
    # A 股口径必须**原样保留** —— 15:35 已过, 当天数据该到而没到就是停更
    assert severities(cn, "kline_stale") == ["low"]
    assert cn["score"] == 97


def test_hk_still_not_stale_at_1620(tmp_path):
    """16:20 港股已收盘但同步管道 (默认 16:40) 还没跑 → 仍不算停更。

    定版线取的是**数据落地时刻**而不是收盘时刻: 16:00 收盘到 16:40 落盘之间
    读到的最后一根本来就是昨天, 这不是数据问题。
    """
    hk = score(tmp_path, HK, make_df(90, PREV_DAY), now=AT_1620)
    assert "kline_stale" not in codes(hk)


def test_hk_stale_after_hk_cutoff(tmp_path):
    """过了港股自己的定版线 (16:40) 还缺当天数据 → 照样报停更。

    🔴 反向断言: 修误报不能把港股的停更检查一起关掉, 否则等于用「永不告警」
    换「不再误告警」。
    """
    hk = score(tmp_path, HK, make_df(90, PREV_DAY), now=AT_2000)
    assert severities(hk, "kline_stale") == ["low"]


def test_hk_real_staleness_still_hard(tmp_path):
    """港股真停更 8 个交易日 → high 硬伤 + hard=True (与 A 股同档)。"""
    stale_end = date(2026, 1, 23)   # 距 02-04 共 8 个工作日
    hk = score(tmp_path, HK, make_df(90, stale_end), now=AT_2000)
    assert severities(hk, "kline_stale") == ["high"]
    assert hk["hard"] is True
    # high 扣 25 → 75 分, 正好压在 B 档下沿 (GRADE_THRESHOLDS: A≥90 / B≥75 / C≥60)
    assert hk["score"] == 75 and hk["grade"] == "B"
    # 与 A 股同帧同档: 停更严重度不因市场而降级
    cn = score(tmp_path, CN, make_df(90, stale_end), now=AT_2000)
    assert severities(cn, "kline_stale") == ["high"]
    assert cn["hard"] is True


def test_cutoff_table_covers_every_market():
    """定版线表必须覆盖全部市场。

    🔴 漏一个市场 → `_DATA_CUTOFF[market]` 抛 KeyError (fail-closed), 而不是
    静默套用 A 股 15:35。新增市场时这条会先红, 逼人显式填该市场的管道时刻。
    """
    assert set(adq._DATA_CUTOFF) == set(Market)
    # A 股口径不得漂移: 15:00 收盘 + 35 分钟管道缓冲, 与 jobs/daily_pipeline 同源
    assert adq._DATA_CUTOFF[Market.CN] == (15, 35)
    # 港股 16:00 收盘, 同步默认 16:40 (preferences._HK_SYNC_DEFAULTS)
    assert adq._DATA_CUTOFF[Market.HK] == (16, 40)


# ===== 附加体检项: A 股专属存储不得套到港股头上 =====

def test_hk_skips_ashare_only_stores(tmp_path):
    """港股不跑财务/分钟/全市场体检 —— 这三项都是 A 股专属存储。

    🔴 旧实现用 `asset_type == "stock"` 做闸, 而港股 resolve_asset_type 兜底
    正是 "stock" → 港股被当成「缺财务的 A 股」, 报 fin_missing(文案还写着
    「Free 模式或尚未同步」, 对港股是错误建议)、去 A 股分钟快照里找它、
    并把 A 股全市场体检的问题注入港股分析的提示词。
    """
    hk = score(tmp_path, HK, make_df(90, AT_2000.date()), now=AT_2000)
    assert "fin_missing" not in codes(hk)
    assert "minute_snapshot_stale" not in codes(hk)
    assert "minute_gaps" not in codes(hk)
    assert not any(c.startswith("integrity_") for c in codes(hk))
    # 诚实降级: 留一条 info 说明「本市场没有这条链路」, 而不是伪装成该标的缺数据
    assert severities(hk, "checks_not_applicable") == ["info"]


def test_hk_extra_checks_never_deduct(tmp_path):
    """港股附加项恒为 info (0 扣分) —— 没有链路不该影响评级。"""
    hk = score(tmp_path, HK, make_df(90, AT_2000.date()), now=AT_2000)
    assert all(i["severity"] == "info" for i in hk["issues"])
    assert hk["score"] == 100 and hk["grade"] == "A" and hk["hard"] is False


def test_cn_still_runs_all_three_stores(tmp_path):
    """A 股三项照跑 (既有契约, 一行未改)。"""
    cn = score(tmp_path, CN, make_df(90, AT_2000.date()), now=AT_2000)
    assert "fin_missing" in codes(cn)
    assert "minute_snapshot_stale" in codes(cn)


def test_asset_type_gate_is_orthogonal_to_market(tmp_path):
    """🔴 asset_type 闸与 market 维**正交**, 加了 market 不能把 asset_type 闸抹掉。

    A 股 ETF 依旧跳过财务+分钟 (test_etf_skips_financial_and_minute_checks 钉着),
    但全市场体检报告历来在闸外, ETF 同样要跑。
    """
    etf = score(tmp_path, "510300.SH", make_df(90, AT_2000.date()),
                now=AT_2000, asset_type="etf")
    assert "fin_missing" not in codes(etf)
    assert "minute_snapshot_stale" not in codes(etf)

    idx = score(tmp_path, "000001.SH", make_df(90, AT_2000.date()),
                now=AT_2000, asset_type="index")
    assert "fin_missing" not in codes(idx)


def test_extra_checks_registry_covers_every_market():
    """附加体检注册表必须覆盖全部市场 (漏 → score_symbol 抛 ValueError)。"""
    assert set(adq._EXTRA_CHECKS) == set(Market)


def test_unregistered_market_fails_closed(tmp_path, monkeypatch):
    """未注册市场 → ValueError, 不静默回落 A 股。

    回落会让港股按 15:35 定版线误判停更、并跑 A 股专属存储检查 —— 正是要修的 bug。
    """
    monkeypatch.setitem(adq._EXTRA_CHECKS, Market.HK, None)
    monkeypatch.setattr(adq, "_EXTRA_CHECKS",
                        {k: v for k, v in adq._EXTRA_CHECKS.items() if v is not None})
    with pytest.raises(ValueError, match="未注册"):
        score(tmp_path, HK, make_df(90, AT_2000.date()), now=AT_2000)


# ===== 日历: 港股无日历源 → 工作日近似 (诚实降级) =====

def test_calendar_registry_covers_every_market():
    """日历注册表键必须覆盖全部市场; 值为 None 表示该市场无日历端点。"""
    assert set(adq._CALENDAR_SOURCES) == set(Market)
    assert adq._CALENDAR_SOURCES[Market.HK] is None


def test_hk_has_no_calendar_so_weekday_approximation(tmp_path):
    """港股日历为 None → _calendar_for 返回 None → 回退周几近似。

    🔴 这是**已记录的降级**不是 bug: fuyao 无港股日历端点 (见 trading_day 的
    探测链表)。港股独有假日会被当成交易日 → 停更天数偏高 (偏保守)。
    拿 A 股日历算港股错得没有方向, 两害相权取其轻。
    """
    assert adq._calendar_for(Market.HK) is None
    # 周六不是交易日 (周几近似生效)
    assert adq._is_trading_day(date(2026, 2, 7), None) is False
    assert adq._is_trading_day(date(2026, 2, 4), None) is True


def test_cn_calendar_still_zero_arg_and_patchable(monkeypatch):
    """🔴 _trading_calendar() 必须保持**零参**且可 monkeypatch。

    既有 A 股用例 (test_ai_data_quality.py 的 autouse fixture) 靠
    `monkeypatch.setattr(adq, "_trading_calendar", lambda: None)` 摆脱 fuyao
    年份表依赖。若把它改成必传 market、或让 _CALENDAR_SOURCES 在 import 时
    绑定原函数对象, 补丁就失效 → 既有测试被迫修改, 违反 CONTRIBUTING §9
    「既有 A 股测试不改一行也必须通过」。
    """
    monkeypatch.setattr(adq, "_trading_calendar", lambda: {date(2026, 2, 4)})
    # CN 走 lambda 延迟查模块全局 → 补丁生效
    assert adq._calendar_for(Market.CN) == {date(2026, 2, 4)}
    # 港股不受 A 股补丁影响 (无日历源)
    assert adq._calendar_for(Market.HK) is None


def test_trading_days_between_uses_market_calendar():
    """_trading_days_between 按市场取日历; 港股无日历 → 周几近似。"""
    # 02-03(周二) → 02-04(周三): 1 个交易日, 两个市场一致
    assert adq._trading_days_between(date(2026, 2, 3), date(2026, 2, 4), Market.CN) == 1
    assert adq._trading_days_between(date(2026, 2, 3), date(2026, 2, 4), Market.HK) == 1
    # 跨周末: 02-06(周五) → 02-09(周一) = 1 个交易日 (周几近似)
    assert adq._trading_days_between(date(2026, 2, 6), date(2026, 2, 9), Market.HK) == 1
    # start >= end → 0
    assert adq._trading_days_between(date(2026, 2, 4), date(2026, 2, 4), Market.HK) == 0


# ===== market 入参: 推断 / 别名归一 / 非法值 =====

def test_market_inferred_from_symbol(tmp_path):
    """不传 market → 从 symbol 推断 (与 stock_analyzer._load_kline 同口径)。"""
    df = make_df(90, PREV_DAY)
    inferred = score(tmp_path, HK, df, now=AT_1550)
    explicit = score(tmp_path, HK, df, now=AT_1550, market=Market.HK)
    assert codes(inferred) == codes(explicit)
    assert inferred["score"] == explicit["score"]


@pytest.mark.parametrize("alias", ["hk", "HK", "Hk"])
def test_market_alias_normalized(tmp_path, alias):
    """market 传字符串别名 → rules_for 归一, 与推断结果一致。"""
    df = make_df(90, PREV_DAY)
    got = score(tmp_path, HK, df, now=AT_1550, market=alias)
    assert "kline_stale" not in codes(got)


def test_explicit_market_overrides_inference(tmp_path):
    """显式 market 覆盖推断 —— 调用方 (如回测/矩阵) 可强制口径。

    🔴 这里故意用「A 股代码 + market=HK」的错配组合: 断言的是**分派真的听了
    入参**, 而不是偷偷按 symbol 走。错配本身不是合法业务用法, 仅用于钉分派优先级。
    """
    df = make_df(90, PREV_DAY)
    forced = score(tmp_path, CN, df, now=AT_1550, market=Market.HK)
    assert "kline_stale" not in codes(forced)
    assert "checks_not_applicable" in codes(forced)


def test_invalid_market_raises(tmp_path):
    """非法 market → ValueError (fail-closed), 不静默当 A 股。"""
    with pytest.raises(ValueError):
        score(tmp_path, HK, make_df(90, PREV_DAY), now=AT_1550, market="US")


# ===== 缓存 =====

def test_cache_key_needs_no_market_dimension(tmp_path):
    """🔴 缓存键是 symbol 本身, 而 market 由 symbol 唯一决定 → 不会串市场。

    与前端 queryKey 必须显式带 market 的情况**不同**: 那里同一个关键词在两个市场
    都是合法查询 (搜 "007" 在 A 股/港股各有结果), 这里 symbol 已经自带市场后缀。
    这条测试钉住这个前提 —— 若将来出现「同 symbol 跨市场」的口径, 缓存键必须改。
    """
    df = make_df(90, AT_2000.date())
    first = adq.score_symbol(tmp_path, HK, df, EMPTY_FINS, "stock",
                             now=AT_2000, use_cache=True)
    assert adq._cache.get(HK) is not None
    second = adq.score_symbol(tmp_path, HK, df, EMPTY_FINS, "stock",
                              now=AT_2000, use_cache=True)
    assert second == first
    # A 股同 symbol 前缀不共用缓存条目 (键是完整 symbol)
    adq.score_symbol(tmp_path, CN, df, EMPTY_FINS, "stock", now=AT_2000, use_cache=True)
    assert set(adq._cache) == {HK, CN}
    assert "checks_not_applicable" in codes(adq._cache[HK][2])
    assert "fin_missing" in codes(adq._cache[CN][2])


# ===== 下游: 体检块注入 =====

def test_prompt_block_empty_for_healthy_hk(tmp_path):
    """健康的港股 → prompt_block 空串 (info 被过滤, 不污染提示词)。"""
    hk = score(tmp_path, HK, make_df(90, AT_2000.date()), now=AT_2000)
    assert adq.prompt_block(hk) == ""


def test_prompt_block_reports_real_hk_staleness(tmp_path):
    """港股真停更 → 体检块照实注入 (降级不等于不告警)。"""
    hk = score(tmp_path, HK, make_df(90, date(2026, 1, 23)), now=AT_2000)
    block = adq.prompt_block(hk)
    assert "数据体检" in block
    assert "停更" in block
