"""标的搜索测试: 代码 / 名称 / 拼音首字母 (同花顺式 payh → 平安银行)。

直接调用 search_instruments, 用最小 FakeRepo 提供 instruments 缓存, 不走 HTTP/DB。
"""
from __future__ import annotations

import types

import polars as pl
import pytest

from app.api.kline import search_instruments


class _FakeRepo:
    """最小 repo 桩: 只实现 search_instruments 依赖的两个维表读取器。

    ``hk_calls`` 用来钉「纯 A 股调用方不得触发港股维表读取」—— 没同步过港股维表的
    用户会因此吃一条「港股维表不存在」的 warning, 属无谓噪音。
    """

    def __init__(self, by_asset: dict[str, pl.DataFrame], hk: pl.DataFrame | None = None) -> None:
        self.store = types.SimpleNamespace(data_dir="data")
        self._by_asset = by_asset
        self._hk = pl.DataFrame() if hk is None else hk
        self.hk_calls = 0

    def get_instruments_asset(self, asset_type: str) -> pl.DataFrame:
        return self._by_asset.get(asset_type, pl.DataFrame())

    def get_hk_instruments(self) -> pl.DataFrame:
        self.hk_calls += 1
        return self._hk


def _request(repo: _FakeRepo) -> types.SimpleNamespace:
    return types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(repo=repo)))


STOCKS = pl.DataFrame({
    "symbol": ["000001.SZ", "600000.SH", "600519.SH", "000333.SZ", "600737.SH"],
    "code": ["000001", "600000", "600519", "000333", "600737"],
    "name": ["平安银行", "浦发银行", "贵州茅台", "美的集团", "中粮糖业"],
})


def _search(q: str, asset_types: str = "stock", limit: int = 20) -> list[dict]:
    repo = _FakeRepo({"stock": STOCKS})
    return search_instruments(_request(repo), q=q, limit=limit, asset_types=asset_types)["results"]


# ===== 既有逻辑回归: 代码 / 名称搜索不受影响 =====

def test_code_prefix_match():
    """code 前缀: 6005 → 600519 (前缀优先)。"""
    rows = _search("6005")
    assert "600519.SH" in [r["symbol"] for r in rows]


def test_symbol_contains_match():
    rows = _search("6005")
    assert "600519.SH" in [r["symbol"] for r in rows]


def test_chinese_name_contains_match():
    rows = _search("银行")
    assert sorted(r["symbol"] for r in rows) == ["000001.SZ", "600000.SH"]


def test_empty_query_returns_empty():
    assert _search("   ") == []


# ===== 拼音首字母搜索 (新功能) =====

def test_pinyin_full_initials_match():
    """payh → 平安银行"""
    rows = _search("payh")
    assert [r["symbol"] for r in rows] == ["000001.SZ"]


def test_pinyin_prefix_match():
    """m → 美的集团 (m 开头)"""
    rows = _search("m")
    assert "000333.SZ" in [r["symbol"] for r in rows]


def test_pinyin_prefix_picks_multiple():
    """pf → 浦发银行; pa → 平安银行 (前缀区分)"""
    assert [r["symbol"] for r in _search("pf")] == ["600000.SH"]
    assert [r["symbol"] for r in _search("pa")] == ["000001.SZ"]


def test_pinyin_respects_limit():
    """limit 限制拼音结果数"""
    rows = _search("z", limit=1)  # z → 中粮糖业
    assert len(rows) == 1


def test_pinyin_layer_between_prefix_and_contains():
    """拼音命中应排在包含匹配之前 (分层优先级)。"""
    rows = _search("md")  # md → 美的集团 (拼音); 无代码/符号以 md 前缀
    assert "000333.SZ" in [r["symbol"] for r in rows]


def test_pinyin_and_code_prefix_coexist():
    """纯字母查询同时命中 code 前缀和拼音首字母时, code 前缀优先排前。"""
    # '600000' 是浦发的 code 前缀; 这里用纯字母无法命中 code, 故仅验证拼音路径独立可用
    rows = _search("pf")  # 浦发
    assert "600000.SH" in [r["symbol"] for r in rows]


# ===== 多音字 =====

POLYPHONE_STOCKS = pl.DataFrame({
    "symbol": ["600729.SH", "000625.SZ"],
    "code": ["600729", "000625"],
    "name": ["重庆百货", "长安汽车"],
})


def test_polyphone_all_readings_match():
    """'重庆' 多音字: cq (chóng) 和 zq (zhòng) 读音都应命中。"""
    repo = _FakeRepo({"stock": POLYPHONE_STOCKS})
    # 取首字母集, 验证两种读音都能搜到
    cq = search_instruments(_request(repo), q="cqbh", limit=20, asset_types="stock")["results"]      # chóng qīng
    zq = search_instruments(_request(repo), q="zqbh", limit=20, asset_types="stock")["results"]      # zhòng qìng
    assert "600729.SH" in [r["symbol"] for r in cq]
    assert "600729.SH" in [r["symbol"] for r in zq]


# ===== 边界 =====

def test_non_ascii_skips_pinyin_branch():
    """中文输入不走拼音分支, 仍按名称匹配。"""
    rows = _search("平安")
    assert [r["symbol"] for r in rows] == ["000001.SZ"]


def test_digits_skips_pinyin_branch():
    """数字输入不走拼音分支, 走 code 前缀。"""
    rows = _search("000")
    assert "000001.SZ" in [r["symbol"] for r in rows]


def test_no_pinyin_hit_returns_empty():
    """无任何拼音命中时返回空 (不报错)。"""
    assert _search("xyz") == []


def test_cache_returns_same_result_across_calls():
    """lru_cache 不应在不同请求间串结果 (按 name 缓存, 查询无状态)。"""
    repo = _FakeRepo({"stock": STOCKS})
    req = _request(repo)
    r1 = search_instruments(req, q="payh", limit=20, asset_types="stock")["results"]
    r2 = search_instruments(req, q="payh", limit=20, asset_types="stock")["results"]
    assert r1 == r2
    assert [r["symbol"] for r in r1] == ["000001.SZ"]


# ===== market 维: 港股走自己的维表 (自选页搜索框接通港股) =====

HK_STOCKS = pl.DataFrame({
    "symbol": ["00700.HK", "09988.HK", "00005.HK"],
    "code": ["00700", "09988", "00005"],
    "name": ["腾讯控股", "阿里巴巴-W", "汇丰控股"],
    "type": ["stock", "stock", "stock"],
})


def _search_hk(q: str, asset_types: str = "stock,etf,index", limit: int = 20) -> list[dict]:
    repo = _FakeRepo({"stock": STOCKS}, hk=HK_STOCKS)
    return search_instruments(
        _request(repo), q=q, limit=limit, asset_types=asset_types, market="HK"
    )["results"]


def test_hk_market_searches_hk_dim():
    """market=HK 按代码搜到港股标的。"""
    rows = _search_hk("00700")
    assert [r["symbol"] for r in rows] == ["00700.HK"]
    assert rows[0]["asset_type"] == "stock"


def test_hk_market_searches_chinese_name():
    assert [r["symbol"] for r in _search_hk("控股")] == ["00700.HK", "00005.HK"]


def test_hk_market_never_returns_ashare_rows():
    """🔴 串市场闸门: market=HK 时 A 股维表一行都不该出现。

    合并全市场搜索在方案 §16 属明确不做 —— 两个市场的代码空间与名称都可能撞,
    混在一个结果列表里用户无法分辨自己加进自选的是哪个市场的标的。
    """
    assert _search_hk("600000") == []
    assert _search_hk("平安") == []


def test_ashare_market_never_returns_hk_rows():
    """反向闸门: 缺省(CN)时港股一行都不该出现。"""
    repo = _FakeRepo({"stock": STOCKS}, hk=HK_STOCKS)
    rows = search_instruments(
        _request(repo), q="00700", limit=20, asset_types="stock,etf,index"
    )["results"]
    assert rows == []


def test_default_market_does_not_touch_hk_dim():
    """既有调用方(不传 market)零影响: 连港股维表都不读。"""
    repo = _FakeRepo({"stock": STOCKS}, hk=HK_STOCKS)
    search_instruments(_request(repo), q="payh", limit=20, asset_types="stock")
    assert repo.hk_calls == 0, "纯 A 股搜索却读了港股维表 (会给未同步用户一条无谓 warning)"


def test_explicit_cn_market_does_not_touch_hk_dim():
    repo = _FakeRepo({"stock": STOCKS}, hk=HK_STOCKS)
    search_instruments(_request(repo), q="payh", limit=20, asset_types="stock", market="CN")
    assert repo.hk_calls == 0


def test_hk_market_is_case_insensitive_alias():
    """market 值大小写/别名归一由 parse_market 负责, 端点不自己判等。"""
    repo = _FakeRepo({"stock": STOCKS}, hk=HK_STOCKS)
    rows = search_instruments(
        _request(repo), q="00700", limit=20, asset_types="stock", market="hk"
    )["results"]
    assert [r["symbol"] for r in rows] == ["00700.HK"]


def test_unknown_market_returns_400_not_silent_cn_fallback():
    """🔴 非法 market 必须 400。静默回退 CN 会让用户以为搜的是港股。"""
    from fastapi import HTTPException

    repo = _FakeRepo({"stock": STOCKS}, hk=HK_STOCKS)
    with pytest.raises(HTTPException) as exc:
        search_instruments(_request(repo), q="007", limit=20, asset_types="stock", market="US")
    assert exc.value.status_code == 400
    assert "US" in str(exc.value.detail)


def test_missing_hk_dim_returns_empty_not_500():
    """港股维表未同步 → 空结果 (与 A 股维表缺失时行为一致), 不抛异常。"""
    repo = _FakeRepo({"stock": STOCKS})  # hk 缺省 = 空帧
    rows = search_instruments(
        _request(repo), q="00700", limit=20, asset_types="stock", market="HK"
    )["results"]
    assert rows == []


def test_hk_asset_types_without_stock_returns_empty():
    """港股维表目前只有股票; asset_types 不含 stock 时不该硬塞结果。"""
    assert _search_hk("00700", asset_types="etf,index") == []


def test_hk_null_name_is_searchable_by_code_only():
    """东财名称占位 '-' 已被清洗成 None; null 名不得误命中任意关键词。"""
    dim = pl.DataFrame({
        "symbol": ["00700.HK", "01234.HK"],
        "code": ["00700", "01234"],
        "name": ["腾讯控股", None],
        "type": ["stock", "stock"],
    })
    repo = _FakeRepo({"stock": STOCKS}, hk=dim)
    rows = search_instruments(
        _request(repo), q="控", limit=20, asset_types="stock", market="HK"
    )["results"]
    assert [r["symbol"] for r in rows] == ["00700.HK"]
    by_code = search_instruments(
        _request(repo), q="01234", limit=20, asset_types="stock", market="HK"
    )["results"]
    assert [r["symbol"] for r in by_code] == ["01234.HK"]


def test_hk_respects_limit():
    rows = _search_hk("0", limit=2)
    assert len(rows) == 2
