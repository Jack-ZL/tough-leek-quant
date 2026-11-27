"""交易日探针 (trading_day oracle) 与两个消费方接入的测试。

不依赖真实网络: 探测函数 (_probe_fuyao / _probe_tickflow) 全部 monkeypatch。
覆盖: 周末零成本直判、探测链优先级 (fuyao 日历权威, 无开盘缓冲问题)、
tickflow 戳的 OR 语义与开盘缓冲窗、失败/无权限 → None、TTL 缓存、
实时行情门控与分钟增量 gate_reason 的 holiday 分支。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time as dt_time, timezone, timedelta, UTC

import pytest

from app.market import (
    CN_RULES, HK_RULES, Market, clear_half_days, half_day_close, rules_for,
    trading_total_minutes,
)
from app.services import trading_day
from app.services.trading_day import is_trading_day, load_half_days_config, reset_cache

CN = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_cache()
    yield
    reset_cache()


def _no_probes(monkeypatch):
    """探测函数替换为爆炸 — 用于验证未被打到。"""
    monkeypatch.setattr(trading_day, "_probe_fuyao", lambda now: (_ for _ in ()).throw(AssertionError("不应探测")))
    monkeypatch.setattr(trading_day, "_probe_tickflow", lambda now: (_ for _ in ()).throw(AssertionError("不应探测")))


# ---- 周末零成本直判 ----

def test_weekend_returns_false_without_probing(monkeypatch):
    sat = datetime(2026, 8, 29, 10, 0, tzinfo=CN)  # 周六
    sun = datetime(2026, 8, 30, 10, 0, tzinfo=CN)  # 周日
    _no_probes(monkeypatch)
    assert is_trading_day(sat) is False
    assert is_trading_day(sun) is False


# ---- 探测链优先级 ----

def test_fuyao_calendar_is_authoritative_even_before_open(monkeypatch):
    """fuyao 日历结论无时段依赖: 开盘缓冲窗内也直接生效。"""
    holiday_mon = datetime(2026, 9, 7, 9, 31, tzinfo=CN)  # 周一 (缓冲窗内)
    monkeypatch.setattr(trading_day, "_probe_fuyao", lambda now: False)
    monkeypatch.setattr(
        trading_day, "_probe_tickflow",
        lambda now: (_ for _ in ()).throw(AssertionError("fuyao 已有结论不应继续探测")),
    )
    assert is_trading_day(holiday_mon) is False


def test_chain_falls_through_to_tickflow_when_fuyao_unknown(monkeypatch):
    monday = datetime(2026, 9, 7, 10, 0, tzinfo=CN)
    monkeypatch.setattr(trading_day, "_probe_fuyao", lambda now: None)
    monkeypatch.setattr(trading_day, "_probe_tickflow", lambda now: True)
    assert is_trading_day(monday) is True


def test_all_probes_unknown_returns_none(monkeypatch):
    monday = datetime(2026, 9, 7, 10, 0, tzinfo=CN)
    monkeypatch.setattr(trading_day, "_probe_fuyao", lambda now: None)
    monkeypatch.setattr(trading_day, "_probe_tickflow", lambda now: None)
    assert is_trading_day(monday) is None


def test_concurrent_cold_oracle_requests_share_one_upstream_probe(monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(20)
    calls = []
    monday = datetime(2026, 9, 7, 10, 0, tzinfo=CN)

    def probe(now):
        calls.append(1)
        time.sleep(0.05)
        return False

    monkeypatch.setattr(trading_day, "_probe_fuyao", probe)
    monkeypatch.setattr(trading_day, "_probe_tickflow", lambda now: None)

    def request(_):
        barrier.wait(3)
        return is_trading_day(monday)

    with ThreadPoolExecutor(20) as pool:
        assert list(pool.map(request, range(20))) == [False] * 20
    assert calls == [1]


def test_slow_oracle_followers_degrade_to_unknown_without_duplicate_probes(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered, release = threading.Event(), threading.Event()
    calls = []
    monday = datetime(2026, 9, 7, 10, 0, tzinfo=CN)

    def probe(now):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return False

    monkeypatch.setattr(trading_day, "_probe_fuyao", probe)
    with ThreadPoolExecutor(1) as pool:
        owner = pool.submit(is_trading_day, monday)
        assert entered.wait(1)
        try:
            assert is_trading_day(monday) is None
            assert calls == [1]
        finally:
            release.set()
        assert owner.result(1) is False
    assert is_trading_day(monday) is False


# ---- tickflow 戳语义 ----

def test_tickflow_stale_stamp_before_buffer_is_unknown(monkeypatch):
    """开盘缓冲窗内戳停在昨日: 可能是集合竞价未翻新, 保守判未知。"""
    monday_935 = datetime(2026, 9, 7, 9, 35, tzinfo=CN)
    import app.tickflow.client as tf_client_mod

    class _FakeQuotes:
        def get(self, symbols):
            # 周一 9:35 拉到上周五 15:30 的戳
            return [{"symbol": s, "timestamp": 1787902251001} for s in symbols]

    class _FakeClient:
        quotes = _FakeQuotes()

    monkeypatch.setattr(tf_client_mod, "get_client", lambda: _FakeClient())
    monkeypatch.setattr(trading_day, "_probe_fuyao", lambda now: None)
    assert trading_day._probe_tickflow(monday_935) is None


def test_tickflow_stale_stamp_after_buffer_is_holiday(monkeypatch):
    monday_1041 = datetime(2026, 9, 7, 10, 41, tzinfo=CN)
    import app.tickflow.client as tf_client_mod

    class _FakeQuotes:
        def get(self, symbols):
            return [{"symbol": s, "timestamp": 1787902251001} for s in symbols]  # 上周五

    class _FakeClient:
        quotes = _FakeQuotes()

    monkeypatch.setattr(tf_client_mod, "get_client", lambda: _FakeClient())
    assert trading_day._probe_tickflow(monday_1041) is False


def test_tickflow_fresh_stamp_is_trading_anytime(monkeypatch):
    """缓冲窗内只要戳是今天就判交易日 (OR 语义, 任一翻新即可)。"""
    monday_920 = datetime(2026, 9, 7, 9, 20, tzinfo=CN)
    import app.tickflow.client as tf_client_mod

    class _FakeQuotes:
        def get(self, symbols):
            # 一只翻新 + 其余停在周五 → max 为今日
            fresh_ms = int(monday_920.timestamp() * 1000)
            return [
                {"symbol": "000001.SZ", "timestamp": fresh_ms},
                {"symbol": "600519.SH", "timestamp": 1787902251001},
            ]

    class _FakeClient:
        quotes = _FakeQuotes()

    monkeypatch.setattr(tf_client_mod, "get_client", lambda: _FakeClient())
    assert trading_day._probe_tickflow(monday_920) is True


def test_tickflow_error_returns_none(monkeypatch):
    monday = datetime(2026, 9, 7, 10, 41, tzinfo=CN)
    import app.tickflow.client as tf_client_mod

    def _boom():
        raise RuntimeError("无实时权限 (free 档)")

    monkeypatch.setattr(tf_client_mod, "get_client", _boom)
    assert trading_day._probe_tickflow(monday) is None


# ---- TTL 缓存 ----

def test_verdict_cached_within_ttl(monkeypatch):
    monday = datetime(2026, 9, 7, 10, 0, tzinfo=CN)
    calls = {"n": 0}

    def _counting_probe(now):
        calls["n"] += 1
        return True

    monkeypatch.setattr(trading_day, "_probe_fuyao", _counting_probe)
    assert is_trading_day(monday) is True
    assert is_trading_day(monday) is True
    assert is_trading_day(monday) is True
    assert calls["n"] == 1  # 命中缓存, 只探一次


def test_unknown_verdict_retries_after_short_ttl(monkeypatch):
    monday = datetime(2026, 9, 7, 10, 0, tzinfo=CN)
    calls = {"n": 0}

    def _counting_probe(now):
        calls["n"] += 1
        return None

    monkeypatch.setattr(trading_day, "_probe_fuyao", _counting_probe)
    monkeypatch.setattr(trading_day, "_probe_tickflow", lambda now: None)  # 隔离真实网络
    assert is_trading_day(monday) is None
    # 手动把缓存时间拨回 10 分钟前 (超过 unknown TTL 300s) → 重探
    with trading_day._CACHE_LOCK:
        trading_day._CACHE.probed_at -= 600
    assert is_trading_day(monday) is None
    assert calls["n"] == 2


# ---- 消费方 1: 实时行情门控 ----

def test_quote_service_holiday_gate_blocks_polling(monkeypatch):
    from app.services.quote_service import QuoteService

    qs = QuoteService()
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: False)
    assert qs._should_poll_for_phase("morning") is False
    assert qs._should_poll_for_phase("preopen") is False
    # final 定版同样被剔除: 休市日没有需要定版的当日行情
    assert qs._should_poll_for_phase("close_final") is False


def test_quote_service_polls_when_trading_or_unknown(monkeypatch):
    from app.services.quote_service import QuoteService

    qs = QuoteService()
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: True)
    assert qs._should_poll_for_phase("morning") is True
    # 未知 → 维持现状 (周几近似)
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: None)
    assert qs._should_poll_for_phase("morning") is True


# ---- 消费方 2: 分钟增量 gate_reason ----

def _minute_service(monkeypatch):
    from app.services.minute_refresh import MinuteRefreshService

    svc = MinuteRefreshService.__new__(MinuteRefreshService)
    svc._app_state = None
    monkeypatch.setattr(
        "app.services.minute_refresh.preferences.get_minute_refresh_enabled",
        lambda: True,
    )
    monkeypatch.setattr(svc, "capability_ok", lambda: True)
    return svc


def test_minute_refresh_gate_returns_holiday(monkeypatch):
    """周几+时段门控放行 (周一盘中) 但探针判休市 → holiday。"""
    svc = _minute_service(monkeypatch)
    monday_1030 = datetime(2026, 9, 7, 10, 30, tzinfo=CN)
    monkeypatch.setattr(
        "app.services.minute_refresh._in_continuous_session", lambda now=None: True
    )
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: False)
    assert svc._gate_reason() == "holiday"


def test_minute_refresh_gate_passes_when_trading(monkeypatch):
    svc = _minute_service(monkeypatch)
    monkeypatch.setattr(
        "app.services.minute_refresh._in_continuous_session", lambda now=None: True
    )
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: True)
    assert svc._gate_reason() is None


# ---- fuyao 日历解析 ----

def test_fuyao_provider_trading_days_conversion(monkeypatch):
    from app.plugins.fuyao.provider import FuyaoProvider
    from app.plugins.fuyao import provider as fp

    class _CalClient:
        def trading_days(self):
            # 上海零点戳按 provider 的 _ms_of_date 口径构造
            def _midnight_ms(d):
                import calendar
                return (calendar.timegm(d.timetuple()) - 28800) * 1000

            d4, d7 = date(2026, 9, 4), date(2026, 9, 7)
            return [
                {"date": "20260904", "date_ms": _midnight_ms(d4)},
                {"date": "20260907", "date_ms": _midnight_ms(d7)},
            ]

    monkeypatch.setattr(
        fp, "fuyao_client", type("M", (), {"FuyaoClient": lambda **kw: _CalClient()})
    )
    monkeypatch.setattr(fp, "get_api_key", lambda: "test-key")
    days = FuyaoProvider().trading_days()
    assert days == {date(2026, 9, 4), date(2026, 9, 7)}


def test_unknown_verdict_is_cached_within_short_ttl(monkeypatch):
    """未知结论也要按 _TTL_UNKNOWN_S 缓存: 轮询每拍重探会重复打 tickflow 请求。"""
    monday = datetime(2026, 9, 7, 10, 0, tzinfo=CN)
    calls = {"fuyao": 0, "tickflow": 0}

    def _fuyao(now):
        calls["fuyao"] += 1
        return None

    def _tickflow(now):
        calls["tickflow"] += 1
        return None

    monkeypatch.setattr(trading_day, "_probe_fuyao", _fuyao)
    monkeypatch.setattr(trading_day, "_probe_tickflow", _tickflow)

    assert is_trading_day(monday) is None
    assert is_trading_day(monday) is None
    assert is_trading_day(monday) is None
    assert calls == {"fuyao": 1, "tickflow": 1}


# ---- 下一交易日 (侧栏轻提示, 周末跳过; 未来节假日按周几近似) ----

def test_next_trading_day_from_friday_is_monday():
    friday = datetime(2026, 10, 16, 20, 0, tzinfo=CN)  # 周五
    assert trading_day.next_trading_day(friday) == date(2026, 10, 19)


def test_next_trading_day_from_weekend_is_monday():
    sat = datetime(2026, 10, 17, 10, 0, tzinfo=CN)  # 周六
    sun = datetime(2026, 10, 18, 10, 0, tzinfo=CN)  # 周日
    assert trading_day.next_trading_day(sat) == date(2026, 10, 19)
    assert trading_day.next_trading_day(sun) == date(2026, 10, 19)


def test_next_trading_day_from_weekday_is_next_weekday():
    thu = datetime(2026, 10, 15, 20, 0, tzinfo=CN)  # 周四 (节假日近似)
    assert trading_day.next_trading_day(thu) == date(2026, 10, 16)


# ---- 多市场隔离 (PR1 地基: A 股零影响 + 港股不串桶) ----
#
# 这里钉住两个曾经的「串市场」隐患, 两者都**不报错**, 只静默给出错市场的结论:
#   1. 缓存曾是单个模块级 _Cache 实例 → CN 先探得 True, 同日 HK 直接读走 CN 的
#      结论。CN/HK 节假日并不重合 (国庆 CN 休一周、HK 只休 10/1; 农历新年 HK
#      休得更长; 圣诞/复活节 HK 休而 CN 不休), 于是港股要么在假日空转轮询,
#      要么把交易日误判为休市而**整天不拉行情**。
#   2. 单飞键曾是 (now.date(), epoch) 不含市场 → 同日两市场共用一个条目,
#      后到的市场拿走先到市场的结论。

HK_TZ = CN  # 港股同为 UTC+8 固定偏移 (app/market.py: 刻意不用 ZoneInfo)
MONDAY = datetime(2026, 9, 7, 10, 0, tzinfo=CN)


def _stub(monkeypatch, name, verdict):
    """把探针换成「记录调用并返回固定结论」的桩, 返回调用记录列表。"""
    calls: list[datetime] = []

    def _probe(now):
        calls.append(now)
        return verdict

    monkeypatch.setattr(trading_day, name, _probe)
    return calls


def _boom(monkeypatch, name):
    """把探针换成爆炸桩 — 用于验证该市场的链**不含**它。"""
    def _probe(now):
        raise AssertionError(f"{name} 不应被调用")

    monkeypatch.setattr(trading_day, name, _probe)


def test_default_market_is_cn(monkeypatch):
    """不传 market 必须等价于传 CN, 且走 A 股探测链 —— 这是「A 股零影响」的实现方式。"""
    fuyao = _stub(monkeypatch, "_probe_fuyao", True)
    _boom(monkeypatch, "_probe_tickflow_hk")

    assert is_trading_day(MONDAY) is True
    assert is_trading_day(MONDAY, market=Market.CN) is True
    assert len(fuyao) == 1, "第二次应命中缓存, 且默认市场就是 CN"


def test_cn_and_hk_verdicts_do_not_contaminate_each_other(monkeypatch):
    """⭐ 核心回归: 同一天 CN=True / HK=False 必须能共存。"""
    _stub(monkeypatch, "_probe_fuyao", None)      # fuyao 未配置 → 落到 tickflow
    _stub(monkeypatch, "_probe_tickflow", True)   # A 股在交易
    _stub(monkeypatch, "_probe_tickflow_hk", False)  # 港股休市 (如 10/1)

    assert is_trading_day(MONDAY, market=Market.CN) is True
    assert is_trading_day(MONDAY, market=Market.HK) is False
    # 反向顺序也要成立 (缓存写入顺序不应影响结论)
    reset_cache()
    assert is_trading_day(MONDAY, market=Market.HK) is False
    assert is_trading_day(MONDAY, market=Market.CN) is True


def test_cache_hit_does_not_reprobe_per_market(monkeypatch):
    """每个市场各自缓存: 重复调用不重探, 且两市场互不顶掉对方的缓存。"""
    cn = _stub(monkeypatch, "_probe_fuyao", True)
    hk = _stub(monkeypatch, "_probe_tickflow_hk", False)

    for _ in range(3):
        assert is_trading_day(MONDAY, market=Market.CN) is True
        assert is_trading_day(MONDAY, market=Market.HK) is False
    assert len(cn) == 1
    assert len(hk) == 1


def test_reset_cache_scoped_to_one_market(monkeypatch):
    """``reset_cache(market)`` 只清该市场, 另一市场的结论仍走缓存。"""
    cn = _stub(monkeypatch, "_probe_fuyao", True)
    hk = _stub(monkeypatch, "_probe_tickflow_hk", False)
    assert is_trading_day(MONDAY, market=Market.CN) is True
    assert is_trading_day(MONDAY, market=Market.HK) is False

    reset_cache(Market.CN)
    assert is_trading_day(MONDAY, market=Market.CN) is True
    assert is_trading_day(MONDAY, market=Market.HK) is False
    assert len(cn) == 2, "CN 被清了, 应重探一次"
    assert len(hk) == 1, "HK 未被清, 不得重探"


def test_reset_cache_without_market_clears_all(monkeypatch):
    """``reset_cache()`` 清所有市场 (autouse fixture 依赖此语义做用例间隔离)。"""
    cn = _stub(monkeypatch, "_probe_fuyao", True)
    hk = _stub(monkeypatch, "_probe_tickflow_hk", False)
    assert is_trading_day(MONDAY, market=Market.CN) is True
    assert is_trading_day(MONDAY, market=Market.HK) is False

    reset_cache()
    assert is_trading_day(MONDAY, market=Market.CN) is True
    assert is_trading_day(MONDAY, market=Market.HK) is False
    assert len(cn) == 2
    assert len(hk) == 2


def test_hk_chain_excludes_fuyao(monkeypatch):
    """港股链**不含** fuyao: 其日历端点是 /api/a-share/calendar/trading-days, A 股专属。

    用「不入链」而不是「入链后判市场」—— 后者每加一个市场都要改函数体。
    """
    _boom(monkeypatch, "_probe_fuyao")
    hk = _stub(monkeypatch, "_probe_tickflow_hk", True)

    assert is_trading_day(MONDAY, market=Market.HK) is True
    assert len(hk) == 1


def test_cn_chain_tries_fuyao_before_tickflow(monkeypatch):
    """A 股链保持原有优先级: fuyao 日历权威, 给出结论后不再打 tickflow。"""
    _stub(monkeypatch, "_probe_fuyao", False)
    _boom(monkeypatch, "_probe_tickflow")

    assert is_trading_day(MONDAY, market=Market.CN) is False


def test_probe_chain_registry_covers_every_market():
    """每个市场都必须有链, 且链上的名字都真实存在 (拼错会在运行时才炸)。"""
    import sys

    module = sys.modules[trading_day.__name__]
    assert set(trading_day._PROBE_CHAIN_NAMES) == set(Market)
    for market, names in trading_day._PROBE_CHAIN_NAMES.items():
        assert names, f"{market} 的探测链为空"
        for name in names:
            assert callable(getattr(module, name, None)), f"{market} 链上的 {name} 不存在"


@pytest.mark.parametrize("bad", ["US", "us", None, 123, "", "CN.HK"])
def test_unknown_market_raises_value_error(bad):
    """未知市场必须报错, 不得静默回落到 A 股规则。

    静默回落的后果是用 ±10% 涨跌停 / T+1 / 100 股一手去算港股, 每个数字都
    「看起来合理」, 没有任何信号。
    """
    with pytest.raises(ValueError):
        is_trading_day(MONDAY, market=bad)
    with pytest.raises(ValueError):
        trading_day.cached_verdict(MONDAY, market=bad)
    with pytest.raises(ValueError):
        trading_day.next_trading_day(MONDAY, market=bad)


@pytest.mark.parametrize("alias", ["cn", "CN", Market.CN, "hk", "HK", Market.HK])
def test_market_argument_accepts_str_and_enum(alias, monkeypatch):
    """``market`` 同时接受枚举与字符串 (API query 参数天然是字符串)。"""
    expected = Market.HK if str(alias).lower() == "hk" else Market.CN
    assert rules_for(alias).market is expected

    # 端到端: 传字符串别名必须走到对应市场的探针, 而不是默认 A 股。
    probed: list[str] = []
    monkeypatch.setattr(trading_day, "_probe_fuyao", lambda now: probed.append("cn") or True)
    monkeypatch.setattr(trading_day, "_probe_tickflow_hk", lambda now: probed.append("hk") or False)
    assert is_trading_day(MONDAY, market=alias) is (expected is Market.CN)
    assert probed == ["hk" if expected is Market.HK else "cn"]


def test_cache_alias_points_at_live_cn_bucket():
    """``_CACHE`` 是 A 股桶的别名, 且 ``reset_cache`` 后仍指向同一个活对象。

    本文件多处直接拨 ``trading_day._CACHE.probed_at``; 若 reset_cache 改成
    重新绑定字典值 (``_CACHES[m] = _Cache()``), 别名就会指向已被丢弃的实例,
    那些用例会静默失去作用。
    """
    assert trading_day._CACHE is trading_day._CACHES[Market.CN]
    reset_cache()
    assert trading_day._CACHE is trading_day._CACHES[Market.CN]
    assert trading_day._CACHE.day is None


def test_hk_weekend_returns_false_without_probing(monkeypatch):
    """港股周末同样零成本直判, 且用的是港股桶。"""
    _boom(monkeypatch, "_probe_tickflow_hk")
    sat = datetime(2026, 8, 29, 10, 0, tzinfo=HK_TZ)
    assert is_trading_day(sat, market=Market.HK) is False
    assert trading_day.cached_verdict(sat, market=Market.HK) is False


def test_cached_verdict_is_market_scoped(monkeypatch):
    """只读接口用的 ``cached_verdict`` 也必须分市场, 否则 API 会读到别市场的结论。"""
    _stub(monkeypatch, "_probe_fuyao", True)
    _stub(monkeypatch, "_probe_tickflow_hk", False)
    assert trading_day.cached_verdict(MONDAY, market=Market.CN) is None  # 尚未探测
    assert is_trading_day(MONDAY, market=Market.CN) is True
    assert is_trading_day(MONDAY, market=Market.HK) is False

    assert trading_day.cached_verdict(MONDAY, market=Market.CN) is True
    assert trading_day.cached_verdict(MONDAY, market=Market.HK) is False


# ---- tickflow 探针的市场属性 (篮子 / 时区 / 缓冲窗都取自 rules) ----

class _FakeQuotes:
    def __init__(self, rows, sink):
        self._rows = rows
        self._sink = sink

    def get(self, symbols):
        self._sink.extend(symbols)
        return self._rows


class _FakeClient:
    def __init__(self, rows, sink):
        self.quotes = _FakeQuotes(rows, sink)


def _fake_tickflow(monkeypatch, rows):
    """替换 tickflow client (探针内部是延迟 import, 打模块属性即可), 返回被请求的 symbols。"""
    import app.tickflow.client as tf_client

    sink: list[str] = []
    monkeypatch.setattr(tf_client, "get_client", lambda: _FakeClient(rows, sink))
    return sink


def test_tickflow_probe_uses_its_own_market_basket(monkeypatch):
    """⭐ 探针篮子必须是**该市场**的流动性票。

    拿 A 股票探港股: 港股休市日 A 股照常交易, 时间戳永远是今天 → 恒判 True,
    港股节假日门控彻底失效 (且失效方向是「以为在交易」, 最难发现)。
    """
    stamp = int(MONDAY.timestamp() * 1000)
    cn_sink = _fake_tickflow(monkeypatch, [{"timestamp": stamp}])
    assert trading_day._probe_tickflow(MONDAY) is True
    assert tuple(cn_sink) == CN_RULES.probe_basket

    reset_cache()
    hk_sink = _fake_tickflow(monkeypatch, [{"timestamp": stamp}])
    assert trading_day._probe_tickflow_hk(MONDAY) is True
    assert tuple(hk_sink) == HK_RULES.probe_basket
    assert not (set(hk_sink) & set(cn_sink)), "两市场篮子不得重叠"


def test_tickflow_probe_interprets_stamp_in_market_timezone(monkeypatch):
    """时间戳按 ``rules.tz`` (UTC+8) 解释, 不是 UTC / 不是 naive。

    16:30Z = 次日 00:30 (UTC+8)。若误用 UTC, 日期会算成前一天, 结论从 True
    变成 None (落在开盘缓冲窗内), 门控静默失效。
    """
    utc_stamp = int(datetime(2026, 9, 7, 16, 30, tzinfo=UTC).timestamp() * 1000)
    _fake_tickflow(monkeypatch, [{"timestamp": utc_stamp}])
    next_day = datetime(2026, 9, 8, 0, 30, tzinfo=CN)
    assert trading_day._probe_tickflow(next_day) is True


def test_tickflow_probe_stale_stamp_respects_buffer_window(monkeypatch):
    """戳停在上一交易日: 缓冲窗内 → None (可能只是竞价未翻新), 窗后 → False。"""
    prev_stamp = int(datetime(2026, 9, 4, 15, 0, tzinfo=CN).timestamp() * 1000)
    _fake_tickflow(monkeypatch, [{"timestamp": prev_stamp}])

    in_buffer = datetime(2026, 9, 7, 9, 35, tzinfo=CN)   # < 09:40
    after_buffer = datetime(2026, 9, 7, 9, 45, tzinfo=CN)  # > 09:40
    assert trading_day._probe_tickflow(in_buffer) is None
    assert trading_day._probe_tickflow(after_buffer) is False


def test_tickflow_probe_without_permission_returns_none(monkeypatch):
    """无实时权限 / 空响应 / 无有效戳 → None (退回周几近似), 不得抛异常。"""
    for rows in ([], [{}], [{"timestamp": None}], [{"timestamp": 0}]):
        _fake_tickflow(monkeypatch, rows)
        assert trading_day._probe_tickflow_hk(MONDAY) is None


def test_hk_tickflow_probe_currently_returns_none(monkeypatch):
    """现状钉住: tickflow 尚无港股实时权限 → 港股退回周几近似。

    接入港股行情源后本用例会失败, 那时应把它改成断言真实结论 —— 失败即提醒,
    这正是钉住现状的目的。
    """
    import types

    import app.tickflow.client as tf_client

    def _deny(symbols):
        raise PermissionError("no hk realtime entitlement")

    fake = types.SimpleNamespace(quotes=types.SimpleNamespace(get=_deny))
    monkeypatch.setattr(tf_client, "get_client", lambda: fake)
    assert trading_day._probe_tickflow_hk(MONDAY) is None


def test_concurrent_cn_and_hk_probes_stay_isolated(monkeypatch):
    """⭐ 单飞键含市场: 两市场同日并发探测必须各跑各的, 不得共用条目。

    两个探针都在 barrier 上会合 —— 若键不含市场, 后到者会等在前到者的条目上
    而不是执行自己的探针, barrier 凑不齐 → 超时破碎 → 用例失败。
    """
    import threading
    import time as stdlib_time

    barrier = threading.Barrier(2, timeout=5)

    def _make(verdict):
        def _probe(now):
            barrier.wait()
            stdlib_time.sleep(0.02)
            return verdict
        return _probe

    monkeypatch.setattr(trading_day, "_probe_fuyao", lambda now: None)
    monkeypatch.setattr(trading_day, "_probe_tickflow", _make(True))
    monkeypatch.setattr(trading_day, "_probe_tickflow_hk", _make(False))

    out: dict[str, object] = {}

    def _worker(key, market):
        try:
            out[key] = is_trading_day(MONDAY, market=market)
        except BaseException as exc:  # noqa: BLE001 — 失败原因交给断言呈现
            out[key] = exc

    threads = [
        threading.Thread(target=_worker, args=("cn", Market.CN)),
        threading.Thread(target=_worker, args=("hk", Market.HK)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not any(t.is_alive() for t in threads), "探测未结束 (单飞等待/死锁)"
    assert out["cn"] is True, f"CN 结论被污染: {out['cn']!r}"
    assert out["hk"] is False, f"HK 结论被污染: {out['hk']!r}"


def test_next_trading_day_accepts_market():
    """``next_trading_day`` 也按市场取规则 (周几近似阶段两市场周末相同)。"""
    friday = datetime(2026, 10, 16, 20, 0, tzinfo=CN)
    assert trading_day.next_trading_day(friday, market=Market.HK) == date(2026, 10, 19)
    assert trading_day.next_trading_day(friday, market=Market.CN) == date(2026, 10, 19)


# ================================================================
# 半日市日历配置载入 (load_half_days_config)
#
# 桥接「data_dir/half_days.json → register_half_days」。核心安全方向与
# app.market 一致: 宁可漏标不可误标 ⇒ 任何读取/解析/市场/日期失败都 fail-open
# (不注册, 退化全日市), 绝不因配置问题阻断启动或误标半日市丢掉下午行情。
# ================================================================


class TestLoadHalfDaysConfig:
    @pytest.fixture(autouse=True)
    def _clean_half_days(self):
        """半日市表是模块级可变状态, 本类每个测试前后清空防泄漏。"""
        clear_half_days()
        yield
        clear_half_days()

    def _write(self, tmp_path, doc):
        (tmp_path / "half_days.json").write_text(json.dumps(doc), encoding="utf-8")

    def test_missing_file_registers_nothing(self, tmp_path):
        assert load_half_days_config(tmp_path) == 0
        assert half_day_close(Market.HK, date(2026, 12, 24)) is None

    def test_valid_config_registers_and_truncates(self, tmp_path):
        self._write(tmp_path, {
            "_comment": "经 HKEX 公告核实",
            "HK": {
                "dates": ["2026-12-24", "2026-12-31"],
                "provenance": "HKEX 公告", "verified_at": "2026-01-01",
            },
        })
        assert load_half_days_config(tmp_path) == 2
        assert half_day_close(Market.HK, date(2026, 12, 24)) == dt_time(12, 0)
        assert trading_total_minutes(Market.HK, date(2026, 12, 24)) == 150
        # 非半日市日不受影响
        assert trading_total_minutes(Market.HK, date(2026, 12, 23)) == 330
        # 不得泄漏到 A 股
        assert trading_total_minutes(Market.CN, date(2026, 12, 24)) == 240
        assert half_day_close(Market.CN, date(2026, 12, 24)) is None

    def test_bare_list_accepted(self, tmp_path):
        self._write(tmp_path, {"HK": ["2026-12-24"]})
        assert load_half_days_config(tmp_path) == 1
        assert half_day_close(Market.HK, date(2026, 12, 24)) == dt_time(12, 0)

    def test_malformed_json_fail_open(self, tmp_path):
        (tmp_path / "half_days.json").write_text("{not json", encoding="utf-8")
        assert load_half_days_config(tmp_path) == 0
        assert half_day_close(Market.HK, date(2026, 12, 24)) is None

    def test_top_level_not_object_fail_open(self, tmp_path):
        self._write(tmp_path, ["2026-12-24"])
        assert load_half_days_config(tmp_path) == 0
        assert half_day_close(Market.HK, date(2026, 12, 24)) is None

    def test_unknown_market_skipped_others_registered(self, tmp_path):
        self._write(tmp_path, {
            "XX": {"dates": ["2026-12-24"]},
            "HK": {"dates": ["2026-12-31"]},
        })
        assert load_half_days_config(tmp_path) == 1
        assert half_day_close(Market.HK, date(2026, 12, 31)) == dt_time(12, 0)

    def test_invalid_date_skips_market_no_partial(self, tmp_path):
        self._write(tmp_path, {"HK": {"dates": ["2026-12-24", "not-a-date"]}})
        assert load_half_days_config(tmp_path) == 0
        # 整体校验后再注册 ⇒ 合法的那条也不得半注册
        assert half_day_close(Market.HK, date(2026, 12, 24)) is None

    def test_metadata_keys_ignored(self, tmp_path):
        self._write(tmp_path, {
            "_comment": "x", "_schema": 1,
            "HK": {"dates": ["2026-12-24"]},
        })
        assert load_half_days_config(tmp_path) == 1
        assert half_day_close(Market.HK, date(2026, 12, 24)) == dt_time(12, 0)

    def test_dates_not_list_skipped(self, tmp_path):
        self._write(tmp_path, {"HK": {"dates": "2026-12-24"}})
        assert load_half_days_config(tmp_path) == 0
        assert half_day_close(Market.HK, date(2026, 12, 24)) is None

    def test_idempotent_reload(self, tmp_path):
        self._write(tmp_path, {"HK": {"dates": ["2026-12-24"]}})
        assert load_half_days_config(tmp_path) == 1
        assert load_half_days_config(tmp_path) == 1  # 幂等, 不重复累加
        assert half_day_close(Market.HK, date(2026, 12, 24)) == dt_time(12, 0)
