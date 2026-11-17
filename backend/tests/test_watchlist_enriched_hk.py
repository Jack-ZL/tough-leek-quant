"""自选 enriched 端点的**港股行**契约测试。

背景
----
自选是**一张跨市场平表** (``services/watchlist.py`` 无 market 列)。在港股读取器
(:meth:`KlineRepository.get_hk_enriched_latest`) 落地之前, 港股代码会落进
``stock_symbols`` 去 LEFT JOIN A 股 enriched 缓存 —— 结果是一整行 null, 前端渲染
"—", 看起来像「没同步」而不是「读错了表」。

本文件钉住三件事:
1. 港股行走**港股**快照, 指标/换手率/名称/流通股本都拿得到;
2. 🔴 港股行**不得**被套上 A 股涨跌停价 (港股无涨跌停制度, 而 ``price_limits``
   只认 A 股规则 —— 算出来是一个「看起来对」的 ±10%, 正是 CONTRIBUTING §3 要拦的);
3. A 股/ETF/指数分支行为不变, 且纯 A 股自选**不触发**港股读取器。

用真实 ``KlineRepository`` 跑港股侧 (读取器 + 指标重算全链路), 只对 A 股侧做定点
stub —— 本文件的被测对象是「分流与合并」, 不是 A 股 enriched 的生成。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import watchlist as wl_api
from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import DataStore, KlineRepository

END = dt.date(2026, 11, 13)
HK = "00700.HK"
CN = "600519.SH"


@pytest.fixture()
def repo(tmp_path) -> KlineRepository:
    return KlineRepository(DataStore(tmp_path))


def _request(repo):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def _enriched_frame(
    symbol: str, dates: list[dt.date], closes: list[float], *, volumes: list[float] | None = None
) -> pl.DataFrame:
    """存储窄表 enriched (14 列, dtype 与 ``ENRICHED_STORAGE_SCHEMA`` 对齐)。"""
    n = len(dates)
    vols = volumes or [1_000_000.0] * n
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "date": dates,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": vols,
        "amount": [c * v for c, v in zip(closes, vols, strict=True)],
        "raw_close": closes, "raw_high": closes, "raw_low": closes,
        "turnover_rate": [None] * n,
        "consecutive_limit_ups": [0] * n,
        "consecutive_limit_downs": [0] * n,
        "quote_ts": [0] * n,
    }).with_columns(
        pl.col("turnover_rate").cast(pl.Float64),
        pl.col("consecutive_limit_ups").cast(pl.UInt32),
        pl.col("consecutive_limit_downs").cast(pl.UInt32),
        pl.col("quote_ts").cast(pl.Int64),
    )


def _dates(n: int, end: dt.date = END) -> list[dt.date]:
    return [end - dt.timedelta(days=i) for i in range(n - 1, -1, -1)]


def _ramp(n: int, start_price: float = 100.0, step: float = 0.5) -> list[float]:
    return [start_price + i * step for i in range(n)]


def _seed_hk_dim(tmp_path, *, float_shares: float = 1_000_000_000.0) -> None:
    out = tmp_path / HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame([{
        "symbol": HK, "name": "腾讯控股", "is_cny_counter": False,
        "hkd_counter": None, "listing_date": None, "industry": None,
        "float_shares": float_shares,
    }]), out)


def _seed_hk(repo, *, n: int = 80, volumes: list[float] | None = None) -> None:
    repo.append_hk_enriched(_enriched_frame(HK, _dates(n), _ramp(n), volumes=volumes))


def _watchlist(monkeypatch, symbols: list[str]) -> None:
    monkeypatch.setattr(
        wl_api.watchlist, "list_symbols",
        lambda: [{"symbol": s, "added_at": None} for s in symbols],
    )


def _stub_cn_enriched(
    monkeypatch, repo, rows: list[dict], as_of: dt.date = END
) -> None:
    """定点 stub A 股最新日缓存 (本文件不测 A 股 enriched 的生成)。

    🔴 ``as_of`` 必须是 ``date`` 而不是 ISO 串: 真实 ``get_enriched_latest`` 返回 date,
    而端点会对各资产日期做 ``min()`` —— 传串会在混排场景炸 TypeError。
    """
    frame = pl.DataFrame(rows) if rows else pl.DataFrame(schema={"symbol": pl.Utf8})
    monkeypatch.setattr(repo, "get_enriched_latest", lambda **kw: (frame, as_of), raising=True)


def _call(repo) -> dict:
    return wl_api.watchlist_enriched(_request(repo), ext_columns=None)


def _row(res: dict, symbol: str) -> dict:
    hits = [r for r in res["rows"] if r["symbol"] == symbol]
    assert len(hits) == 1, f"{symbol} 应恰好一行, 实得 {len(hits)}: {[r['symbol'] for r in res['rows']]}"
    return hits[0]


# ---------------------------------------------------------------------------
# 港股行走港股快照
# ---------------------------------------------------------------------------

def test_hk_symbol_served_from_hk_snapshot(repo, monkeypatch):
    """🔴 核心回归: 港股自选必须拿到指标, 不是一整行 null。"""
    _watchlist(monkeypatch, [HK])
    _seed_hk(repo)
    _stub_cn_enriched(monkeypatch, repo, [])

    res = _call(repo)
    row = _row(res, HK)
    assert row["close"] == pytest.approx(_ramp(80)[-1]), "港股收盘价没读到"
    assert row["ma20"] is not None, "港股指标未重算 (走错缓存的典型症状)"
    assert row["prev_close"] is not None
    assert res["dates"]["hk"] == END.isoformat()
    assert res["as_of"] == END.isoformat()


def test_hk_row_asset_type_is_stock(repo, monkeypatch):
    """港股没有 ETF/指数分支, 一律 asset_type="stock" (前端徽标/列降级据此)。"""
    _watchlist(monkeypatch, [HK])
    _seed_hk(repo)
    _stub_cn_enriched(monkeypatch, repo, [])
    assert _row(_call(repo), HK)["asset_type"] == "stock"


def test_hk_name_and_float_shares_come_from_hk_dim(repo, monkeypatch, tmp_path):
    """🔴 ``get_name_map``/``get_instruments`` 只覆盖 A 股三份维表。

    不从港股维表补, 自选里的港股行名称恒为 null, 前端只能显示代码。
    """
    _watchlist(monkeypatch, [HK])
    _seed_hk_dim(tmp_path)
    _seed_hk(repo)
    _stub_cn_enriched(monkeypatch, repo, [])

    row = _row(_call(repo), HK)
    assert row["name"] == "腾讯控股", "港股名称未从港股维表解析"
    assert row["float_shares"] == pytest.approx(1_000_000_000.0)


def test_hk_turnover_uses_share_unit_end_to_end(repo, monkeypatch, tmp_path):
    """换手率「股」口径要一路活到 API 响应 (A 股按「手」, 差 100 倍)。"""
    _watchlist(monkeypatch, [HK])
    _seed_hk_dim(tmp_path)
    _seed_hk(repo, n=30, volumes=[31_493_353.0] * 30)
    _stub_cn_enriched(monkeypatch, repo, [])

    row = _row(_call(repo), HK)
    assert row["turnover_rate"] == pytest.approx(31_493_353.0 / 1_000_000_000.0 * 100, rel=1e-6)


# ---------------------------------------------------------------------------
# 🔴 港股不得被套 A 股涨跌停规则
# ---------------------------------------------------------------------------

def test_hk_limit_prices_are_null_not_ashare_rules(repo, monkeypatch, tmp_path):
    """港股无涨跌停 → 限价列必须 null。

    ``polars_price_limit_pct`` 只认 A 股规则; 若港股行漏进那个 ``when``, 会算出一个
    「看起来对」的 ±10% 限价 —— 比 null 危险得多 (CONTRIBUTING §3)。
    """
    _watchlist(monkeypatch, [HK])
    _seed_hk_dim(tmp_path)
    _seed_hk(repo)
    _stub_cn_enriched(monkeypatch, repo, [])

    row = _row(_call(repo), HK)
    assert row["prev_close"] is not None, "前置条件: 有 prev_close 才会进限价计算分支"
    assert row["asset_type"] == "stock", "前置条件: asset_type 是 stock 才会进限价计算分支"
    assert row["limit_up_price"] is None, f"港股被套上了 A 股涨停价 {row['limit_up_price']}"
    assert row["limit_down_price"] is None, f"港股被套上了 A 股跌停价 {row['limit_down_price']}"


def test_ashare_limit_prices_still_computed(repo, monkeypatch):
    """对照组: A 股行的限价照算 (证明上一条不是「整块被关掉」的假通过)。"""
    _watchlist(monkeypatch, [CN])
    _stub_cn_enriched(monkeypatch, repo, [{
        "symbol": CN, "close": 1800.0, "prev_close": 1780.0,
        "change_pct": 1.12, "amount": 1e9, "turnover_rate": 1.0,
    }])
    monkeypatch.setattr(repo, "get_name_map", lambda syms=None: {CN: "贵州茅台"}, raising=True)

    row = _row(_call(repo), CN)
    assert row["limit_up_price"] == pytest.approx(1958.0), "A 股涨停价没算出来"
    assert row["limit_down_price"] == pytest.approx(1602.0)


# ---------------------------------------------------------------------------
# 混排与隔离
# ---------------------------------------------------------------------------

def test_mixed_market_watchlist_returns_both_rows(repo, monkeypatch):
    """一张平表里 A 股 + 港股并存: 两行都返回, 各读各的缓存, 互不污染。"""
    _watchlist(monkeypatch, [CN, HK])
    _seed_hk(repo)
    _stub_cn_enriched(monkeypatch, repo, [{
        "symbol": CN, "close": 1800.0, "prev_close": 1780.0,
        "change_pct": 1.12, "amount": 1e9, "turnover_rate": 1.0,
    }])

    res = _call(repo)
    assert len(res["rows"]) == 2
    hk_close = _ramp(80)[-1]
    assert _row(res, HK)["close"] == pytest.approx(hk_close)
    assert _row(res, CN)["close"] == pytest.approx(1800.0)
    assert res["dates"]["hk"] == END.isoformat()
    assert res["dates"]["stock"] == "2026-11-13"


def test_watchlist_order_preserved_across_markets(repo, monkeypatch):
    """行序按自选添加顺序, 不因市场分流被打乱。"""
    _watchlist(monkeypatch, [HK, CN, "09988.HK"])
    _seed_hk(repo)
    _stub_cn_enriched(monkeypatch, repo, [])

    res = _call(repo)
    assert [r["symbol"] for r in res["rows"]] == [HK, CN, "09988.HK"]


def test_unsynced_hk_symbol_still_returns_placeholder_row(repo, monkeypatch):
    """自选了但港股从未同步 → 仍返回一行 (指标 null), 不静默丢弃。"""
    _watchlist(monkeypatch, [HK])
    _stub_cn_enriched(monkeypatch, repo, [])

    res = _call(repo)
    row = _row(res, HK)
    # 用 .get: 全表皆空时 df 只有 symbol 列, 指标键**不存在** (与 A 股既有行为一致,
    # 见 test_watchlist_enriched_join.test_all_watchlist_missing_from_enriched)。
    # 混排场景下 diagonal_relaxed 会补出 null, 两种形态前端都渲染 "—"。
    assert row.get("close") is None
    assert res["dates"]["hk"] is None, "无缓存时 hk 日期必须为 null (前端 fail-closed)"


def test_pure_ashare_watchlist_never_touches_hk_reader(repo, monkeypatch):
    """🔴 纯 A 股自选不得触发港股读取器 (冷缓存会做一次全量 scan+compute)。"""
    _watchlist(monkeypatch, [CN])
    _stub_cn_enriched(monkeypatch, repo, [{
        "symbol": CN, "close": 1800.0, "prev_close": 1780.0,
        "change_pct": 1.12, "amount": 1e9, "turnover_rate": 1.0,
    }])

    def _boom(**kwargs):
        raise AssertionError("纯 A 股自选却调用了 get_hk_enriched_latest")

    monkeypatch.setattr(repo, "get_hk_enriched_latest", _boom, raising=True)
    res = _call(repo)
    assert res["dates"]["hk"] is None
    assert len(res["rows"]) == 1


def test_pct_since_added_is_null_for_hk(repo, monkeypatch):
    """「加入后涨跌幅」基准价只覆盖 A 股历史缓存 → 港股如实为 null, 不凑数。"""
    _watchlist(monkeypatch, [HK])
    _seed_hk(repo)
    _stub_cn_enriched(monkeypatch, repo, [])

    row = _row(_call(repo), HK)
    assert "pct_since_added" in row, "列必须存在 (否则前端排序/筛选静默失效)"
    assert row["pct_since_added"] is None


# ---------------------------------------------------------------------------
# 响应契约
# ---------------------------------------------------------------------------

def test_dates_always_carries_hk_key(repo, monkeypatch):
    """``dates`` 恒含 hk 键 —— 前端不必判键是否存在 (缺键会静默当「不新鲜」)。"""
    _watchlist(monkeypatch, [])
    res = _call(repo)
    assert res["rows"] == []
    assert set(res["dates"]) == {"stock", "etf", "index", "hk"}
    assert res["dates"]["hk"] is None


def test_as_of_is_min_across_markets(repo, monkeypatch):
    """as_of 取各资产缓存中较旧者: 港股停在旧日时不得把整体 as_of 抬到新日。"""
    _watchlist(monkeypatch, [CN, HK])
    stale = END - dt.timedelta(days=4)
    repo.append_hk_enriched(_enriched_frame(HK, _dates(30, stale), _ramp(30)))
    _stub_cn_enriched(monkeypatch, repo, [{
        "symbol": CN, "close": 1800.0, "prev_close": 1780.0,
        "change_pct": 1.12, "amount": 1e9, "turnover_rate": 1.0,
    }])

    res = _call(repo)
    assert res["as_of"] == stale.isoformat(), f"as_of 应取较旧者, 实得 {res['as_of']}"
    assert res["dates"]["hk"] == stale.isoformat()
    assert res["dates"]["stock"] == "2026-11-13"


# ---------------------------------------------------------------------------
# 行情延时标注 (QuoteDelayBadge 的唯一数据来源)
# ---------------------------------------------------------------------------

def test_hk_row_carries_quote_delay_from_rules(repo, monkeypatch):
    """🔴 港股行必须自带 ``quote_delay_minutes``, 且值取自 ``rules_for`` 而非字面量。

    前端 QuoteDelayBadge 只认接口字段 (写死 15 会在源侧调整延时后静默说谎)。
    延时是**交易所授权的正常状态**, 不是故障, 所以走独立字段而不是 quote_freshness。
    """
    from app.market import Market, rules_for

    _watchlist(monkeypatch, [HK])
    _seed_hk(repo)
    _stub_cn_enriched(monkeypatch, repo, [])

    row = _row(_call(repo), HK)
    expected = rules_for(Market.HK).quote_delay_minutes
    assert expected == 15, "前置条件: 港股规则表的延时分钟数变了, 本测试需同步复核"
    assert row["quote_delay_minutes"] == expected


def test_ashare_row_delay_is_zero(repo, monkeypatch):
    """A 股实时行情 → 0, 徽章不渲染 (不给 A 股凭空多出延时标注)。"""
    from app.market import Market, rules_for

    _watchlist(monkeypatch, [CN])
    _stub_cn_enriched(monkeypatch, repo, [{
        "symbol": CN, "close": 1800.0, "prev_close": 1780.0,
        "change_pct": 1.12, "amount": 1e9, "turnover_rate": 1.0,
    }])

    row = _row(_call(repo), CN)
    assert row["quote_delay_minutes"] == rules_for(Market.CN).quote_delay_minutes == 0


def test_delay_column_survives_mixed_market_rows(repo, monkeypatch):
    """混排时逐行各标其延时, 不被对角合并抹平。"""
    _watchlist(monkeypatch, [CN, HK])
    _seed_hk(repo)
    _stub_cn_enriched(monkeypatch, repo, [{
        "symbol": CN, "close": 1800.0, "prev_close": 1780.0,
        "change_pct": 1.12, "amount": 1e9, "turnover_rate": 1.0,
    }])

    res = _call(repo)
    assert _row(res, HK)["quote_delay_minutes"] == 15
    assert _row(res, CN)["quote_delay_minutes"] == 0


# ---------------------------------------------------------------------------
# 自选列表响应 (GET/POST /api/watchlist) 的名称解析
# ---------------------------------------------------------------------------

def test_with_names_resolves_hk_names(repo, tmp_path):
    """🔴 ``_with_names`` 走 get_name_map, 只覆盖 A 股三份维表 → 港股名要另补。

    不补的话: 加自选成功后返回的列表里港股 name 恒为 null, 前端刚加进去的那一行
    只显示代码, 刷新后才 (从 enriched 端点) 拿到名字 —— 看起来像加了个坏标的。
    """
    _seed_hk_dim(tmp_path)
    rows = [{"symbol": HK, "added_at": None}, {"symbol": CN, "added_at": None}]

    by = {r["symbol"]: r["name"] for r in wl_api._with_names(rows, _request(repo))}
    assert by[HK] == "腾讯控股"
    assert by[CN] is None, "前置条件: A 股维表未同步, 名称本就为 null"


def test_pure_ashare_list_never_reads_hk_dim(repo, monkeypatch):
    """纯 A 股自选不得触发港股维表读取 (没同步过港股的用户会吃一条无谓 warning)。"""
    calls: list[int] = []
    monkeypatch.setattr(
        repo, "get_hk_instruments", lambda: calls.append(1) or pl.DataFrame(), raising=True
    )

    wl_api._with_names([{"symbol": CN, "added_at": None}], _request(repo))
    assert calls == [], "纯 A 股列表却读了港股维表"
