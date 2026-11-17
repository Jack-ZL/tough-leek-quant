"""AI 链路的市场分派测试 (PR4 f1)。

钉住 ``stock_analyzer._load_kline`` 按 symbol 推断市场并分派到对应存储。

🔴 这里守的是一个**静默错误**: ``resolve_asset_type("00700.HK")`` 返回 ``"stock"``
(港股既不在 ETF 也不在指数符号集里), 于是旧实现会拿港股代码去读 **A 股** enriched
存储, 得到空帧, 分析直接报「暂无日 K 数据, 请先同步」。用户看到的是「没同步」,
真实原因是「读错了表」, 去点同步按钮永远修不好 —— 正是 CONTRIBUTING §3 警告的
「看起来对但错」。

个股分析与多空辩论**共用** ``_load_kline`` (``stock_debate.build_materials`` 直接
调它), 所以分派只需落一处; 本文件同时钉住这个共用关系不被悄悄打破。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.market import Market
from app.services import stock_analyzer
from app.services.stock_analyzer import _KLINE_WINDOW, _load_kline

#: B008: 默认值里不许调函数 → 用模块级哨兵表示「调用方没传 market」
_NO_ARG = object()


def _frame(n: int, *, start: date = date(2026, 1, 1), base: float = 100.0) -> pl.DataFrame:
    """n 根升序日K, 列够 compute_levels / _clean_rows 用。"""
    return pl.DataFrame({
        "date": [start + timedelta(days=i) for i in range(n)],
        "open": [base + i * 0.1 for i in range(n)],
        "high": [base + i * 0.1 + 1 for i in range(n)],
        "low": [base + i * 0.1 - 1 for i in range(n)],
        "close": [base + i * 0.2 for i in range(n)],
        "volume": [1_000_000 + i for i in range(n)],
    })


class _FakeRepo:
    """记录两条读取路径各自被调了几次 —— 「没被调」和「被调了」同样是要断言的事实。"""

    def __init__(self, cn: pl.DataFrame | None = None, hk: pl.DataFrame | None = None,
                 asset_type: str = "stock"):
        self._cn = cn if cn is not None else pl.DataFrame()
        self._hk = hk if hk is not None else pl.DataFrame()
        self._asset_type = asset_type
        self.cn_calls: list[tuple] = []
        self.hk_calls: list[tuple] = []

    def resolve_asset_type(self, symbol: str) -> str:
        return self._asset_type

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        self.cn_calls.append((asset_type, symbol, start, end))
        return self._cn

    def get_hk_daily(self, symbol, start, end):
        self.hk_calls.append((symbol, start, end))
        return self._hk


# ===== 核心: 港股不得读 A 股存储 =====

def test_hk_symbol_reads_hk_store_not_ashare():
    """🔴 港股代码必须走 get_hk_daily, 且**完全不碰** get_daily_asset。"""
    repo = _FakeRepo(cn=_frame(5), hk=_frame(5, base=300.0))
    df = _load_kline(repo, "00700.HK")

    assert len(repo.hk_calls) == 1
    assert repo.hk_calls[0][0] == "00700.HK"
    assert repo.cn_calls == [], "港股读了 A 股 enriched 存储 = 串市场"
    assert float(df["close"][0]) == 300.0, "拿到的是 A 股那份帧, 不是港股的"


def test_ashare_symbol_never_touches_hk_store():
    """A 股代码走原路径, 且不得读港股存储 (纯 A 股用户零额外开销)。"""
    repo = _FakeRepo(cn=_frame(5, base=50.0), hk=_frame(5, base=300.0))
    df = _load_kline(repo, "600519.SH")

    assert len(repo.cn_calls) == 1
    assert repo.hk_calls == []
    assert float(df["close"][0]) == 50.0


def test_hk_lowercase_suffix_also_routes_to_hk():
    """``infer_market`` 是宽松判据 (大小写归一), ``00700.hk`` 同样必须走港股存储。

    若这里回退成 A 股, 就是 CONTRIBUTING §3.5 点名的那个坑: 严格判大小写会让
    ``00700.hk`` 被判成「不是港股」—— 恰恰是护栏最该抓住的形态。
    """
    repo = _FakeRepo(cn=_frame(5), hk=_frame(5, base=300.0))
    _load_kline(repo, "00700.hk")
    assert len(repo.hk_calls) == 1
    assert repo.cn_calls == []


# ===== A 股既有行为零回归 =====

def test_cn_asset_type_routing_unchanged():
    """ETF/指数仍按 resolve_asset_type 分流, 分派表没有吃掉这层。"""
    for asset_type in ("stock", "etf", "index"):
        repo = _FakeRepo(cn=_frame(3), asset_type=asset_type)
        _load_kline(repo, "510300.SH")
        assert repo.cn_calls[0][0] == asset_type


def test_window_tail_applied_on_both_paths():
    """两条路径都只留最近 _KLINE_WINDOW 根 (提示词 token 预算一致)。"""
    n = _KLINE_WINDOW + 30
    cn_repo = _FakeRepo(cn=_frame(n))
    hk_repo = _FakeRepo(hk=_frame(n))

    assert len(_load_kline(cn_repo, "600519.SH")) == _KLINE_WINDOW
    assert len(_load_kline(hk_repo, "00700.HK")) == _KLINE_WINDOW


def test_empty_frame_passthrough_no_tail_crash():
    """空帧原样返回 (调用方靠 is_empty() 报「暂无数据」), 不得在 tail 上炸。"""
    assert _load_kline(_FakeRepo(hk=pl.DataFrame()), "00700.HK").is_empty()
    assert _load_kline(_FakeRepo(cn=pl.DataFrame()), "600519.SH").is_empty()


def test_query_window_uses_market_today(monkeypatch):
    """查询右端取**该市场**的今天: 港股走 market_today(HK), A 股走 cn_today。

    港股与 A 股同为 UTC+8 所以当前数值相同, 但口径必须对: 将来接入别的时区市场时,
    用错市场的「今天」会让窗口整体偏移一天。

    🔴 A 股那半刻意钉 ``cn_today`` 而不是 ``market_today(Market.CN)`` —— 两者取值
    逐字相同, 但 ``tests/test_stock_analysis_beijing_today.py`` 靠 monkeypatch
    ``stock_analyzer.cn_today`` 证明「窗口右端是北京日期而非服务器本地 date.today()」
    (CONTRIBUTING §3.3)。改成 market_today 会让那个既有补丁钉到空气上:
    契约仍在, **证明没了**。见 _TODAY_BY_MARKET 的注释。
    """
    seen: list[object] = []

    def _fake_market_today(market=_NO_ARG):
        seen.append(market)
        return date(2026, 11, 17)

    def _fake_cn_today():
        seen.append("CN-via-cn_today")
        return date(2026, 11, 17)

    monkeypatch.setattr(stock_analyzer, "market_today", _fake_market_today)
    monkeypatch.setattr(stock_analyzer, "cn_today", _fake_cn_today)
    repo = _FakeRepo(cn=_frame(3), hk=_frame(3))
    _load_kline(repo, "00700.HK")
    _load_kline(repo, "600519.SH")

    # 港股必须带 market 参数问 market_today; A 股必须走 cn_today (既有 pin 的落点)
    assert [str(m) for m in seen] == ["HK", "CN-via-cn_today"]
    assert repo.hk_calls[0][2] == date(2026, 11, 17)
    assert repo.cn_calls[0][3] == date(2026, 11, 17)


def test_today_registry_covers_every_market():
    """「今天」注册表必须覆盖全部市场 (漏 → _load_kline 抛 ValueError)。

    与 _KLINE_LOADERS 同一口径: 新增市场时这条先红, 逼人显式填该市场的时区口径,
    而不是静默沿用 A 股的北京日期。
    """
    assert set(stock_analyzer._TODAY_BY_MARKET) == set(Market)
    assert set(stock_analyzer._KLINE_LOADERS) == set(Market)


def test_cn_today_registry_entry_is_late_bound(monkeypatch):
    """🔴 _TODAY_BY_MARKET[CN] 必须**延迟**查模块全局, 不能在 import 时绑死函数对象。

    字典若直接存 ``cn_today``, monkeypatch 改模块属性就影响不到字典里那份原函数 →
    ``test_stock_analysis_beijing_today`` 静默失效 (它用 raising=False, 钉空了也不报错,
    只会让断言拿到真实今天而失败)。这条把「补丁真的生效」变成显式断言。
    """
    monkeypatch.setattr(stock_analyzer, "cn_today", lambda: date(2026, 3, 2))
    assert stock_analyzer._TODAY_BY_MARKET[Market.CN]() == date(2026, 3, 2)


def test_unregistered_market_raises_instead_of_falling_back(monkeypatch):
    """新市场忘了注册读取器 → 当场 ValueError, 不静默回退 A 股。

    与 ``rules_for(None)`` 抛错、``core_index_names`` 对未注册市场抛 ValueError
    同一口径: 静默回退会拿 A 股规则算出「看起来对」的错数。
    """
    from app.market import Market

    monkeypatch.setitem(stock_analyzer._KLINE_LOADERS, Market.HK, None)
    monkeypatch.setattr(stock_analyzer, "infer_market", lambda _s: Market.HK)
    with pytest.raises(ValueError, match="未注册 AI 日K读取器"):
        _load_kline(_FakeRepo(hk=_frame(3)), "00700.HK")


def test_unregistered_today_raises_instead_of_falling_back(monkeypatch):
    """读取器注册了但「今天」的取法漏注册 → 同样当场 ValueError。

    🔴 两张表**分别** fail-closed: 若这里回落 ``cn_today()``, 新时区市场的日K窗口
    右端会静默用北京日期 —— 整个窗口偏移一天, 而偏移一天的窗口照样能取到数据、
    照样能算出指标, 是典型的「看起来对但错」(CONTRIBUTING §3)。
    """
    monkeypatch.setitem(stock_analyzer._TODAY_BY_MARKET, Market.HK, None)
    monkeypatch.setattr(
        stock_analyzer, "_TODAY_BY_MARKET",
        {k: v for k, v in stock_analyzer._TODAY_BY_MARKET.items() if v is not None},
    )
    with pytest.raises(ValueError, match="未注册 AI 日K窗口右端取法"):
        _load_kline(_FakeRepo(hk=_frame(3)), "00700.HK")


# ===== 个股分析与多空辩论共用同一条读取路径 =====

def test_debate_materials_read_hk_store(tmp_path):
    """``stock_debate.build_materials`` 走的是同一个 ``_load_kline``。

    辩论与分析「必须看同一份材料」是既有约束; 若哪天辩论自己另起一套读取,
    多空双方与个股分析就会看到不同市场的数据 —— 这条测试是那个约束的钉子。
    """
    from app.services import stock_debate

    repo = _FakeRepo(cn=_frame(5), hk=_frame(70, base=300.0))
    materials = stock_debate.build_materials(repo, tmp_path, "00700.HK")

    assert materials is not None
    assert repo.cn_calls == [], "辩论备料读了 A 股存储 = 与分析口径分叉"
    assert len(repo.hk_calls) == 1
    assert materials["kline_rows"], "港股材料为空, 辩论会拿不到任何K线"
    assert materials["close"] == pytest.approx(float(_frame(70, base=300.0)["close"][-1]))
