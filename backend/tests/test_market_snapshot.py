"""全市场轻量行情快照按 market 分派 (``GET /api/screener/market-snapshot``)。

钉住三件事:

1. **港股读港股目录**。``_snapshot_hk`` 直读 ``kline_hk_enriched``, 绝不碰 A 股存储
   —— 港股不进 unified 视图 (``test_unified_views_exclude_hk``), 一旦「简化」成走
   ``ScreenerService`` 就会静默返回 A 股行情, 症状是行业板块页显示一堆 A 股代码,
   比报错难查得多。
2. **列契约**。``change_pct``/``vol_ratio_5d`` 不是 enriched 存储列, 必须现算;
   ``consecutive_limit_ups`` 恒 0 是**正确结果**(港股无涨跌停)而不是缺数据;
   港股额外带 ``industry`` (恒生行业), A 股不带 (A 股行业走扩展数据表)。
3. **人民币柜台不重复计数**。同一公司的两个柜台是同一份基本面, 都算进行业聚合会
   把该公司计两次, 行业涨跌幅均值与成交额随之失真。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import screener as screener_api
from app.indicators.pipeline import ENRICHED_STORAGE_COLS
from app.market import Market
from app.parquet import ENRICHED_STORAGE_SCHEMA
from app.services import market_snapshot as ms
from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import DataStore, KlineRepository, enriched_dirname

_D0 = date(2026, 11, 20)
_HK_DIR = enriched_dirname("stock", market=Market.HK)
_CN_DIR = enriched_dirname("stock")


@pytest.fixture()
def env(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    app = FastAPI()
    app.include_router(screener_api.router)
    app.state.repo = repo
    return TestClient(app), repo, tmp_path


def _rec(symbol: str, d: date, close: float, *, volume: float = 1_000_000.0,
         amount: float | None = None, turnover: float = 1.5) -> dict:
    """一条 enriched 存储行 (15 列全给, dtype 与 ``ENRICHED_STORAGE_SCHEMA`` 对齐)。"""
    return {
        "symbol": symbol, "date": d,
        "open": close, "high": close, "low": close, "close": close,
        "volume": volume, "amount": close * volume if amount is None else amount,
        "raw_close": close, "raw_high": close, "raw_low": close,
        "turnover_rate": turnover,
        # 港股无涨跌停 → 连板恒 0, 这是 sync_hk_enriched 写出来的真实值
        "consecutive_limit_ups": 0, "consecutive_limit_downs": 0, "quote_ts": 0,
    }


def _seed_enriched(root: Path, dirname: str, recs: list[dict]) -> None:
    """按 ``date=`` 分区落 enriched parquet。"""
    assert set(ENRICHED_STORAGE_COLS) <= set(recs[0]), "存储列不齐会被 scan 补 null, 测不出真问题"
    df = pl.DataFrame(recs).cast(ENRICHED_STORAGE_SCHEMA)
    for d in df.get_column("date").unique().sort():
        out = root / dirname / f"date={d.isoformat()}"
        out.mkdir(parents=True, exist_ok=True)
        atomic_write_parquet(df.filter(pl.col("date") == d), out / "part.parquet")


def _seed_hk_dim(root: Path, rows: list[dict]) -> None:
    out = root / HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame(rows), out)


def _dim(symbol: str, name: str, industry: str | None, *, cny: bool = False,
         shares: float = 1_000_000_000.0) -> dict:
    return {"symbol": symbol, "name": name, "industry": industry,
            "is_cny_counter": cny, "total_shares": shares, "float_shares": shares}


def _hk_recs(symbol: str, days: int = 12, close0: float = 10.0) -> list[dict]:
    """连续 ``days`` 个交易日, 末日收盘比前一日高 10% (便于断言 change_pct)。"""
    out = []
    for i in range(days):
        d = _D0 - timedelta(days=days - 1 - i)
        close = close0 * (1.1 if i == days - 1 else 1.0)
        out.append(_rec(symbol, d, close, volume=1_000_000.0 * (2.0 if i == days - 1 else 1.0)))
    return out


# ================================================================
# 港股读取器
# ================================================================


def test_hk_snapshot_reads_hk_dir_not_cn(tmp_path):
    """🔴 串市场 pin: A 股目录里有**更新**的数据也不得被港股快照读到。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_enriched(tmp_path, _CN_DIR, [_rec("000001.SZ", _D0 + timedelta(days=9), 12.0)])
    _seed_enriched(tmp_path, _HK_DIR, _hk_recs("00700.HK"))
    _seed_hk_dim(tmp_path, [_dim("00700.HK", "腾讯控股", "资讯科技器材")])

    out = ms.build_market_snapshot(repo, Market.HK)

    assert out["as_of"] == _D0.isoformat(), "as_of 必须是港股自己的最新日, 不是 A 股的"
    assert [r["symbol"] for r in out["rows"]] == ["00700.HK"]
    assert "000001.SZ" not in {r["symbol"] for r in out["rows"]}


def test_hk_snapshot_carries_industry_and_name(tmp_path):
    """恒生行业来自 instruments_hk (东财 f100), 随快照下发供前端分组。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_enriched(tmp_path, _HK_DIR, _hk_recs("00700.HK"))
    _seed_hk_dim(tmp_path, [_dim("00700.HK", "腾讯控股", "资讯科技器材")])

    row = ms.build_market_snapshot(repo, Market.HK)["rows"][0]

    assert row["industry"] == "资讯科技器材"
    assert row["name"] == "腾讯控股"


def test_cn_snapshot_has_no_industry_column(tmp_path, monkeypatch):
    """A 股维表**没有** industry 列 (行业分类走扩展数据表), 不得凭空造一个。"""
    repo = KlineRepository(DataStore(tmp_path))
    frame = pl.DataFrame({
        "symbol": ["000001.SZ"], "name": ["平安银行"], "close": [12.0],
        "change_pct": [0.01], "amount": [1e9], "volume": [1e6],
        "turnover_rate": [1.0], "vol_ratio_5d": [1.0],
        "total_shares": [1e9], "float_shares": [1e9],
        "consecutive_limit_ups": [0],
    })
    monkeypatch.setattr("app.services.screener.ScreenerService", _stub_service(frame, _D0))

    rows = ms.build_market_snapshot(repo, Market.CN)["rows"]

    assert "industry" not in rows[0]


def test_hk_snapshot_excludes_cny_counter(tmp_path):
    """🔴 人民币柜台不得进行业聚合: 同一公司两个柜台会把该公司计两次。"""
    repo = KlineRepository(DataStore(tmp_path))
    # enriched 里两个柜台都有 (include_cny_counter=True 的局部重跑会写成这样)
    _seed_enriched(tmp_path, _HK_DIR, _hk_recs("00700.HK") + _hk_recs("80700.HK"))
    _seed_hk_dim(tmp_path, [
        _dim("00700.HK", "腾讯控股", "资讯科技器材"),
        _dim("80700.HK", "腾讯控股-R", "资讯科技器材", cny=True),
    ])

    out = ms.build_market_snapshot(repo, Market.HK)

    assert [r["symbol"] for r in out["rows"]] == ["00700.HK"]


def test_hk_snapshot_computes_derived_indicators(tmp_path):
    """``change_pct``/``vol_ratio_5d`` 不是存储列, 必须靠暖机窗口现算出来。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_enriched(tmp_path, _HK_DIR, _hk_recs("00700.HK"))
    _seed_hk_dim(tmp_path, [_dim("00700.HK", "腾讯控股", "资讯科技器材")])

    row = ms.build_market_snapshot(repo, Market.HK)["rows"][0]

    assert row["change_pct"] == pytest.approx(0.1, abs=1e-9)
    assert row["vol_ratio_5d"] is not None, "量比需要 ≥6 根暖机, 窗口不够会静默变 null"
    assert row["vol_ratio_5d"] == pytest.approx(2.0 / 1.0, rel=0.2)
    assert row["turnover_rate"] == pytest.approx(1.5), "存储列必须随行透传"


def test_hk_snapshot_limit_ups_zero_is_correct_not_missing(tmp_path):
    """港股无涨跌停 → 连板恒 0。0 是**算出来的正确值**, 不得当成缺数据抹成 null。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_enriched(tmp_path, _HK_DIR, _hk_recs("00700.HK"))
    _seed_hk_dim(tmp_path, [_dim("00700.HK", "腾讯控股", "资讯科技器材")])

    row = ms.build_market_snapshot(repo, Market.HK)["rows"][0]

    assert row["consecutive_limit_ups"] == 0


def test_hk_snapshot_derives_market_cap(tmp_path):
    """市值 = close × 股本 (港股 close 是 HKD, 与 A 股 CNY 不可直接比大小)。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_enriched(tmp_path, _HK_DIR, _hk_recs("00700.HK"))
    _seed_hk_dim(tmp_path, [_dim("00700.HK", "腾讯控股", "资讯科技器材", shares=2_000_000_000.0)])

    row = ms.build_market_snapshot(repo, Market.HK)["rows"][0]

    assert row["market_cap"] == pytest.approx(11.0 * 2_000_000_000.0)
    assert row["float_market_cap"] == pytest.approx(11.0 * 2_000_000_000.0)


def test_hk_snapshot_empty_when_never_synced(tmp_path):
    """从没同步过 → ``as_of=None`` 空态, 不是 500 (港股目录在真实环境里就是空的)。"""
    repo = KlineRepository(DataStore(tmp_path))

    assert ms.build_market_snapshot(repo, Market.HK) == {"as_of": None, "rows": []}


def test_hk_snapshot_without_dim_still_returns_quotes(tmp_path):
    """维表读不到时**仍返回行情行**: 行情是真实数据, 抹掉它是另一种谎报。

    缺 industry → 前端按行业分组自然得到空结果, 落到「未匹配到行业数据」空态,
    如实说明缺的是分类而不是行情。
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_enriched(tmp_path, _HK_DIR, _hk_recs("00700.HK"))

    out = ms.build_market_snapshot(repo, Market.HK)

    assert out["as_of"] == _D0.isoformat()
    assert len(out["rows"]) == 1
    assert out["rows"][0]["change_pct"] == pytest.approx(0.1, abs=1e-9)
    assert "industry" not in out["rows"][0], "维表缺失时不得造一个 industry 列"


def test_hk_snapshot_sanitizes_non_finite(tmp_path):
    """NaN/Inf → None: JSON 没有这两个值, 前端 num() 也只认有限数。"""
    repo = KlineRepository(DataStore(tmp_path))
    recs = _hk_recs("00700.HK")
    recs[-1]["turnover_rate"] = float("nan")
    _seed_enriched(tmp_path, _HK_DIR, recs)
    _seed_hk_dim(tmp_path, [_dim("00700.HK", "腾讯控股", "资讯科技器材")])

    row = ms.build_market_snapshot(repo, Market.HK)["rows"][0]

    assert row["turnover_rate"] is None


def test_unregistered_market_raises(tmp_path, monkeypatch):
    """新市场忘了注册 → 当场报错, 不静默按 A 股读 (回退等于用 A 股行情冒充)。"""
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(ms, "_SNAPSHOT_BUILDERS", {Market.CN: ms._snapshot_cn})

    with pytest.raises(ValueError, match="未注册行情快照读取器"):
        ms.build_market_snapshot(repo, Market.HK)


# ================================================================
# A 股契约不变 + 端点分派
# ================================================================


def _stub_service(frame: pl.DataFrame, as_of: date | None):
    """替身 ScreenerService: 只钉快照的**输出契约**, 不背 A 股整套数据管线。"""

    class _Stub:
        def __init__(self, repo, asset_type="stock"):
            self.repo = repo
            self.asset_type = asset_type

        def latest_date(self):
            return as_of

        def _load_enriched_for_date(self, target_date):
            return frame

    return _Stub


def test_cn_snapshot_contract_unchanged(tmp_path, monkeypatch):
    """A 股: 列裁剪、市值派生、非有限值清洗三项行为逐字不变。"""
    repo = KlineRepository(DataStore(tmp_path))
    frame = pl.DataFrame({
        "symbol": ["000001.SZ"], "name": ["平安银行"], "close": [12.0],
        "change_pct": [float("nan")], "amount": [1e9], "volume": [1e6],
        "turnover_rate": [1.0], "vol_ratio_5d": [1.0],
        "total_shares": [1e9], "float_shares": [1e9],
        "consecutive_limit_ups": [2],
        "ma5": [11.0],  # 契约外的列必须被裁掉
    })
    monkeypatch.setattr("app.services.screener.ScreenerService", _stub_service(frame, _D0))

    out = ms.build_market_snapshot(repo, Market.CN)

    assert out["as_of"] == _D0.isoformat()
    row = out["rows"][0]
    assert "ma5" not in row
    assert row["change_pct"] is None, "NaN 必须洗成 None"
    assert row["market_cap"] == pytest.approx(12.0 * 1e9)
    assert row["float_market_cap"] == pytest.approx(12.0 * 1e9)
    assert row["consecutive_limit_ups"] == 2


def test_cn_snapshot_empty_when_no_data(tmp_path, monkeypatch):
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr("app.services.screener.ScreenerService", _stub_service(pl.DataFrame(), None))

    assert ms.build_market_snapshot(repo, Market.CN) == {"as_of": None, "rows": []}


def test_endpoint_defaults_to_cn(env, monkeypatch):
    """既有调用方不传 market → 走 A 股, 行为不变。"""
    client, _repo, _tmp_path = env
    frame = pl.DataFrame({"symbol": ["000001.SZ"], "close": [12.0], "change_pct": [0.01]})
    monkeypatch.setattr("app.services.screener.ScreenerService", _stub_service(frame, _D0))

    out = client.get("/api/screener/market-snapshot").json()

    assert out["as_of"] == _D0.isoformat()
    assert out["rows"][0]["symbol"] == "000001.SZ"


def test_endpoint_hk_returns_industry(env):
    client, _repo, tmp_path = env
    _seed_enriched(tmp_path, _HK_DIR, _hk_recs("00700.HK"))
    _seed_hk_dim(tmp_path, [_dim("00700.HK", "腾讯控股", "资讯科技器材")])

    out = client.get("/api/screener/market-snapshot", params={"market": "HK"}).json()

    assert out["rows"][0]["industry"] == "资讯科技器材"


def test_endpoint_rejects_unknown_market(env):
    """🔴 非法 market → 400, 不静默回退 CN (回退会让用户以为看的是港股)。"""
    client, _, _ = env

    r = client.get("/api/screener/market-snapshot", params={"market": "US"})

    assert r.status_code == 400
    assert "US" in r.json()["detail"]
    assert "CN" in r.json()["detail"] and "HK" in r.json()["detail"], "400 文案要列出支持的市场"


def test_endpoint_accepts_lowercase_market(env):
    """``parse_market`` 大小写不敏感, query 参数不该比它更严。"""
    client, _repo, tmp_path = env
    _seed_enriched(tmp_path, _HK_DIR, _hk_recs("00700.HK"))
    _seed_hk_dim(tmp_path, [_dim("00700.HK", "腾讯控股", "资讯科技器材")])

    out = client.get("/api/screener/market-snapshot", params={"market": "hk"}).json()

    assert out["rows"][0]["symbol"] == "00700.HK"
