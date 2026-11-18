"""``GET /api/stock-analysis/levels`` 必须按 market 分派日K读取器。

钉住的缺陷 (方案 §9, f2 起登记为「刻意不做」, 本次修复):

``get_levels`` 曾经自己写一遍
``repo.get_daily_asset(repo.resolve_asset_type(symbol), symbol, start, end)``。
``resolve_asset_type("00700.HK")`` 返回 ``"stock"`` (港股既不在 ETF 也不在指数
符号集里), 于是去读 **A 股** enriched 存储 → 空帧 → 走 ``summary="无数据"``
分支 → 港股图表的 11 类 markLine 与布林/Keltner/ATR 曲线**全空**。

🔴 这个缺陷的危险之处在于它**看起来像「没同步」**: 前端显示「无数据」,
用户去点同步按钮永远修不好, 因为同步写的是 ``kline_hk_enriched``,
读的是 A 股表。静默错表比抛错难查得多。

同时钉住 ``cn_today()`` 这条 monkeypatch 缝隙不被 refactor 弄丢
(既有 ``test_stock_analysis_beijing_today`` 用 ``raising=False``, 钉空了不报错,
只会让断言拿到真实今天而失败 —— 本文件把「缝隙真的还在」变成显式断言)。
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import stock_analysis as stock_analysis_api
from app.market import Market, rules_for
from app.market_time import CN_TZ
from app.services import stock_analyzer
from app.services.stock_analyzer import _KLINE_WINDOW, load_kline_window

BJ = date(2026, 3, 2)  # 钉死的北京日期, 不会碰巧等于跑测试那天的 date.today()

#: 11 类关键价位的 key, 空帧分支必须一个不少地返回 (前端按 key 渲染开关按钮)
_LEVEL_KEYS = {"sr", "pivot", "extreme", "boll", "keltner_s", "keltner_m",
               "keltner_l", "atr_stop", "gap", "fib", "round"}


def _frame(n: int, *, start: date = date(2025, 1, 1), base: float = 100.0) -> pl.DataFrame:
    """n 根升序日K, 含 _build_series 需要的预计算指标列。

    带上 boll/ma/atr 列是为了让「港股曲线不再为空」可断言 —— 只给 OHLCV 的话
    ``_build_series`` 返回 ``{}``, 空与非空就区分不出来了。
    """
    close = [base + i * 0.2 for i in range(n)]
    return pl.DataFrame({
        "date": [start + timedelta(days=i) for i in range(n)],
        "open": [base + i * 0.1 for i in range(n)],
        "high": [c + 1.0 for c in close],
        "low": [c - 1.0 for c in close],
        "close": close,
        "volume": [1_000_000 + i for i in range(n)],
        "ma20": close,
        "ma60": close,
        "atr_14": [1.5] * n,
        "boll_upper": [c + 2.0 for c in close],
        "boll_lower": [c - 2.0 for c in close],
    })


class _Repo:
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


def _call_levels(repo, symbol: str, days: int = 120) -> dict:
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))
    return stock_analysis_api.get_levels(req, symbol=symbol, days=days)


# ===== 核心: 港股不得读 A 股存储 =====

def test_hk_levels_read_hk_store_not_ashare():
    """🔴 港股 symbol 必须走 ``get_hk_daily``, 一次都不碰 A 股 enriched 存储。"""
    repo = _Repo(cn=pl.DataFrame(), hk=_frame(150))
    _call_levels(repo, "00700.HK")
    assert repo.hk_calls, "港股必须读 kline_hk_enriched"
    assert not repo.cn_calls, (
        f"港股不得读 A 股存储, 实际调了 get_daily_asset{repo.cn_calls} "
        "→ resolve_asset_type('00700.HK')=='stock' 会静默读错表"
    )


def test_hk_levels_return_non_empty_payload():
    """港股有数据时不得走「无数据」空帧分支 (缺陷的用户可见症状)。"""
    out = _call_levels(_Repo(hk=_frame(150)), "00700.HK")
    assert out["summary"] != "无数据"
    assert out["close"] is not None
    assert len(out["dates"]) == 150
    # 11 类 markLine 至少算出了若干类
    assert any(out["levels"][k] for k in _LEVEL_KEYS), out["levels"]
    # 布林/Keltner/ATR 曲线也要有 (前端画通道用)
    assert out["series"], "港股带状曲线不得为空"
    assert "boll" in out["series"] and "atr" in out["series"]


def test_ashare_levels_still_read_daily_asset():
    """A 股路径逐字不变: 仍按资产类型分流到 ``get_daily_asset``。"""
    repo = _Repo(cn=_frame(150), asset_type="etf")
    out = _call_levels(repo, "510300.SH")
    assert not repo.hk_calls, "A 股不得触发港股读取"
    assert repo.cn_calls, "A 股必须走 get_daily_asset"
    asset_type, symbol, _start, _end = repo.cn_calls[0]
    assert asset_type == "etf", "资产类型分流必须保留 (ETF/指数走独立 enriched)"
    assert symbol == "510300.SH"
    assert out["summary"] != "无数据"


def test_levels_dispatch_matches_ai_chain():
    """``/levels`` 与 AI 链路必须用**同一个**分派点 (否则又会各写一遍、各自漂移)。"""
    hk_repo = _Repo(hk=_frame(150))
    cn_repo = _Repo(cn=_frame(150))
    _call_levels(hk_repo, "00700.HK")
    _call_levels(cn_repo, "600519.SH")
    stock_analyzer._load_kline(_Repo(hk=_frame(150)), "00700.HK")
    stock_analyzer._load_kline(_Repo(cn=_frame(150)), "600519.SH")
    # 两条路径对同一市场选中的读取器必须一致
    assert bool(hk_repo.hk_calls) and not hk_repo.cn_calls
    assert bool(cn_repo.cn_calls) and not cn_repo.hk_calls


# ===== 窗口口径 =====

def test_levels_window_honours_days_param():
    """``start == end - days*2`` 的既有换算不得被 refactor 改掉。"""
    for days in (30, 120, 500):
        repo = _Repo(hk=_frame(10))
        _call_levels(repo, "00700.HK", days=days)
        _symbol, start, end = repo.hk_calls[0]
        assert end - start == timedelta(days=days * 2), days


def test_levels_window_right_end_is_patchable_cn_today(monkeypatch):
    """🔴 窗口右端必须仍走**本模块**的 ``cn_today`` (monkeypatch 缝隙)。

    ``load_kline_window`` 支持显式 ``end``, 就是为了保住这条缝隙: 若 ``get_levels``
    改成走 ``stock_analyzer._TODAY_BY_MARKET``, 既有
    ``test_levels_window_ends_on_beijing_today`` 的补丁会钉到空气上。
    """
    monkeypatch.setattr(stock_analysis_api, "cn_today", lambda: BJ)
    for symbol, attr in (("600000.SH", "cn_calls"), ("00700.HK", "hk_calls")):
        repo = _Repo(cn=_frame(10), hk=_frame(10))
        _call_levels(repo, symbol)
        calls = getattr(repo, attr)
        assert calls, f"{symbol} 应查询日K"
        end = calls[0][-1]
        assert end == BJ, f"{symbol} 窗口右端必须是北京日期 {BJ}, 实际 {end}"


def test_cn_today_seam_is_valid_for_every_registered_market():
    """🔴 ``get_levels`` 用 ``cn_today()`` 当**所有**市场的窗口右端, 只在
    「每个已注册市场的时区都是 UTC+8」时成立。

    这不是凑巧: ``market_time.CN_TZ`` 的文档明说「沪深与港交所同为 UTC+8,
    因此该常量对两个市场都适用」。但一旦加了非 UTC+8 的市场 (如美股),
    levels 的右端就会整体偏移一天 —— 而偏移一天的窗口照样能取到数据、
    照样能算出价位, 是典型的「看起来对但错」(CONTRIBUTING §3)。
    这条让该假设**可执行**: 新市场时区不同 → 本条先红, 逼人把 levels 的
    右端也改成按市场注册。
    """
    cn_offset = CN_TZ.utcoffset(None)
    for m in Market:
        assert rules_for(m).tz.utcoffset(None) == cn_offset, (
            f"{m} 不是 UTC+8, get_levels 的 cn_today() 右端对它不再成立"
        )
        assert stock_analyzer._TODAY_BY_MARKET[m]() == stock_analyzer.cn_today(), m


# ===== 空帧与降级 =====

def test_levels_empty_frame_returns_all_eleven_keys():
    """空帧分支必须返回完整的 11 个 key (前端按 key 渲染开关按钮, 缺 key 会崩)。"""
    out = _call_levels(_Repo(cn=pl.DataFrame(), hk=pl.DataFrame()), "00700.HK")
    assert out["summary"] == "无数据"
    assert out["close"] is None
    assert out["dates"] == [] and out["series"] == {}
    assert set(out["levels"]) == _LEVEL_KEYS
    assert all(out["levels"][k] == [] for k in _LEVEL_KEYS)


def test_levels_rejects_blank_symbol():
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _call_levels(_Repo(), "")


# ===== load_kline_window 本身的契约 =====

def test_load_kline_window_does_not_tail():
    """``load_kline_window`` **不截断** (截断是 ``_load_kline`` 的口径)。

    🔴 若这里也 tail, ``/levels`` 的 A 股行为会变: ``days*2`` 个日历天能取到
    多于 ``days`` 根交易日 K, 历史上端点是全量返回的。
    """
    n = _KLINE_WINDOW + 40
    repo = _Repo(cn=_frame(n))
    assert len(load_kline_window(repo, "600519.SH", 300, end=BJ)) == n
    # 对照: AI 链路仍截断到 _KLINE_WINDOW
    assert len(stock_analyzer._load_kline(_Repo(cn=_frame(n)), "600519.SH")) == _KLINE_WINDOW


def test_load_kline_window_end_defaults_to_market_registry(monkeypatch):
    """不传 ``end`` → 按市场注册表取「今天」, 且注册表漏项当场报错。"""
    monkeypatch.setattr(stock_analyzer, "cn_today", lambda: BJ)
    repo = _Repo(cn=_frame(10))
    load_kline_window(repo, "600519.SH", 120)
    assert repo.cn_calls[0][3] == BJ

    monkeypatch.setitem(stock_analyzer._TODAY_BY_MARKET, Market.HK, None)
    monkeypatch.setattr(
        stock_analyzer, "_TODAY_BY_MARKET",
        {k: v for k, v in stock_analyzer._TODAY_BY_MARKET.items() if v is not None},
    )
    with pytest.raises(ValueError, match="未注册 AI 日K窗口右端取法"):
        load_kline_window(_Repo(hk=_frame(10)), "00700.HK", 120)


def test_load_kline_window_explicit_end_skips_today_registry(monkeypatch):
    """显式传 ``end`` 时不查注册表 —— 这正是 levels 保住 monkeypatch 缝隙的机制。"""
    monkeypatch.setitem(stock_analyzer._TODAY_BY_MARKET, Market.HK, None)
    monkeypatch.setattr(
        stock_analyzer, "_TODAY_BY_MARKET",
        {k: v for k, v in stock_analyzer._TODAY_BY_MARKET.items() if v is not None},
    )
    repo = _Repo(hk=_frame(10))
    load_kline_window(repo, "00700.HK", 120, end=BJ)  # 不抛错
    assert repo.hk_calls[0][2] == BJ


def test_load_kline_window_unregistered_loader_raises(monkeypatch):
    """读取器漏注册 → 当场 ValueError, 不静默回退 A 股。"""
    monkeypatch.setitem(stock_analyzer._KLINE_LOADERS, Market.HK, None)
    monkeypatch.setattr(stock_analyzer, "infer_market", lambda _s: Market.HK)
    with pytest.raises(ValueError, match="未注册 AI 日K读取器"):
        load_kline_window(_Repo(hk=_frame(10)), "00700.HK", 120, end=BJ)
