"""港股涨幅轮动矩阵 + 另两个轮动端点的 fail-closed (PR6-2a)。

钉住四件事:

1. **港股轮动读港股目录**。``_rotation_hk`` 走 ``services/hk_enriched_read``, 绝不碰
   A 股 enriched 缓存 —— 港股不进 unified 视图 (``test_unified_views_exclude_hk``),
   一旦「简化」成复用 ``_change_pct_range`` 就会静默返回 A 股涨跌幅, 症状是港股
   轮动矩阵里全是 A 股概念名, 比报错难查得多。
2. **恒生行业是唯一维度**。港股没有概念数据源, ``kind=concept`` 必须 fail-closed
   返回空矩阵而不是拿 A 股概念成分去套港股代码; 恒生行业是单层分类, ``level``
   传 1/2/3 必须得到同一份结果。
3. **能力位而不是市场名**。``/api/sector-rotation`` (无分钟K源) 与
   ``/api/rps/rotation-analyze`` (无大盘背景源) 的拒绝判据来自
   ``MarketTradingRules.has_minute_bars`` / ``has_market_overview``, 由
   ``test_market_architecture_guard`` 强制不得写成 ``if market == "HK"``。
4. **人民币柜台不重复计数**。与行业板块页快照同一套判据: 同一公司的两个柜台是
   同一份基本面, 都算进行业均值会把该公司计两次。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import rps as rps_api
from app.api import sector_rotation as sector_rotation_api
from app.indicators.pipeline import ENRICHED_STORAGE_COLS
from app.market import CN_RULES, HK_RULES, Market, rules_for
from app.parquet import ENRICHED_STORAGE_SCHEMA
from app.services import rps_rotation
from app.services import sector_rotation as sector_rotation_svc
from app.services.concept_rotation_analyzer import (
    ANALYZE_UNSUPPORTED_NO_OVERVIEW,
    analyze_rotation_stream,
)
from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import DataStore, KlineRepository, enriched_dirname

_D0 = date(2026, 11, 20)
_HK_DIR = enriched_dirname("stock", market=Market.HK)
_CN_DIR = enriched_dirname("stock")
#: 播种天数: 比请求窗口 (days=12) 长, 首日 change_pct 为 null (无前收) 会被
#: 矩阵过滤掉, 剩下的仍 ≥ 12 天, 便于断言「切到最近 N 天」。
_SEED_DAYS = 20


@pytest.fixture(autouse=True)
def _clear_caches():
    """轮动结果缓存与维度映射缓存都是进程级的, 不清会跨用例串味。"""
    rps_rotation.invalidate_cache()
    rps_rotation._map_cache.clear()
    sector_rotation_svc._cache.clear()
    yield
    rps_rotation.invalidate_cache()
    rps_rotation._map_cache.clear()
    sector_rotation_svc._cache.clear()


@pytest.fixture()
def env(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    app = FastAPI()
    app.include_router(rps_api.router)
    app.include_router(sector_rotation_api.router)
    app.state.repo = repo
    return TestClient(app), repo, tmp_path


def _rec(symbol: str, d: date, close: float, *, volume: float = 1_000_000.0) -> dict:
    """一条 enriched 存储行 (dtype 与 ``ENRICHED_STORAGE_SCHEMA`` 对齐)。"""
    return {
        "symbol": symbol, "date": d,
        "open": close, "high": close, "low": close, "close": close,
        "volume": volume, "amount": close * volume,
        "raw_close": close, "raw_high": close, "raw_low": close,
        "turnover_rate": 1.5,
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


def _dim(symbol: str, name: str, industry: str | None, *, cny: bool = False) -> dict:
    return {"symbol": symbol, "name": name, "industry": industry,
            "is_cny_counter": cny, "total_shares": 1e9, "float_shares": 1e9}


def _series(symbol: str, pct: float, days: int = _SEED_DAYS, close0: float = 10.0) -> list[dict]:
    """连续 ``days`` 个自然日, 每日涨跌幅恒为 ``pct`` (小数制)。"""
    out = []
    for i in range(days):
        d = _D0 - timedelta(days=days - 1 - i)
        out.append(_rec(symbol, d, close0 * (1.0 + pct) ** i))
    return out


def _seed_two_industries(root: Path, *, extra_recs: list[dict] = (), extra_dims: list[dict] = ()) -> None:
    """资讯科技器材 (+4%, +2% → 均值 +3%) 与 银行 (+1%, -1% → 均值 0%)。

    🔴 ``extra_*`` 必须走**同一次** ``_seed_enriched``: 该 helper 按 ``date=`` 分区
    写 ``part.parquet``, 分两次调用会让第二次**覆盖**第一次的同日分区。
    """
    recs: list[dict] = []
    recs += _series("00700.HK", 0.04)
    recs += _series("09988.HK", 0.02)
    recs += _series("00005.HK", 0.01)
    recs += _series("01398.HK", -0.01)
    recs += list(extra_recs)
    _seed_enriched(root, _HK_DIR, recs)
    _seed_hk_dim(root, [
        _dim("00700.HK", "腾讯控股", "资讯科技器材"),
        _dim("09988.HK", "阿里巴巴-W", "资讯科技器材"),
        _dim("00005.HK", "汇丰控股", "银行"),
        _dim("01398.HK", "工商银行", "银行"),
        *extra_dims,
    ])


# ================================================================
# 规则表: 能力位是市场差异的唯一住所
# ================================================================


def test_rules_declare_minute_and_overview_capabilities():
    """🔴 两个能力位必须**显式**声明。

    它们是 ``/api/sector-rotation`` 与 ``/api/rps/rotation-analyze`` 拒绝港股的
    唯一判据; 若哪天被「简化」成默认值, 拒绝就会静默失效, 端点开始拿 A 股分钟/
    大盘背景去算港股 —— 那正是 QuantMind 印花税静默归零的形状。
    """
    assert CN_RULES.has_minute_bars is True
    assert CN_RULES.has_market_overview is True
    assert HK_RULES.has_minute_bars is False
    assert HK_RULES.has_market_overview is False


def test_every_market_declares_both_capabilities():
    """注册表完整性: 新增市场必须对两项能力表态, 不能靠默认值蒙混。"""
    for market in Market:
        rules = rules_for(market)
        assert isinstance(rules.has_minute_bars, bool), market
        assert isinstance(rules.has_market_overview, bool), market


# ================================================================
# 港股轮动矩阵
# ================================================================


def test_hk_industry_rotation_groups_and_sorts(tmp_path):
    """恒生行业聚合 + 每列各自按涨幅降序 + 日期最新在最前。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_two_industries(tmp_path)

    out = rps_rotation.build_rps_rotation(repo, 12, "industry", None, market=Market.HK)

    assert out["concept_count"] == 2, "去重行业数, 不是股票数"
    assert out["dates"][0] == _D0.isoformat(), "最新日期必须排最前"
    assert len(out["dates"]) == 12, "切到请求的 N 天"
    assert out["dates"] == sorted(out["dates"], reverse=True)

    top = out["columns"][_D0.isoformat()]
    assert [name for name, _ in top] == ["资讯科技器材", "银行"], "每列各自按涨幅降序"
    assert top[0][1] == pytest.approx(0.03, abs=1e-9), "行业内等权平均: (4% + 2%) / 2"
    assert top[1][1] == pytest.approx(0.0, abs=1e-9), "(+1% + -1%) / 2"


def test_hk_rotation_reads_hk_dir_not_cn(tmp_path):
    """🔴 串市场 pin: A 股目录里有**更新**的数据也不得被港股轮动读到。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_two_industries(tmp_path)
    _seed_enriched(tmp_path, _CN_DIR, _series("000001.SZ", 0.09, days=5))

    out = rps_rotation.build_rps_rotation(repo, 12, "industry", None, market=Market.HK)

    names = {n for col in out["columns"].values() for n, _ in col}
    assert names == {"资讯科技器材", "银行"}
    assert out["dates"][0] == _D0.isoformat(), "右端必须是港股自己的最新日"


def test_hk_rotation_excludes_cny_counter(tmp_path):
    """人民币柜台不进行业均值 —— 否则同一公司被计两次, 均值被拉偏。"""
    repo = KlineRepository(DataStore(tmp_path))
    # 腾讯的人民币柜台, 挂在同一行业, 涨幅夸张到 +50%: 若被计入, 行业均值会
    # 从 3% 跳到 (4+2+50)/3 = 18.67%, 一眼可辨。
    _seed_two_industries(
        tmp_path,
        extra_recs=_series("80700.HK", 0.50),
        extra_dims=[_dim("80700.HK", "腾讯控股-R", "资讯科技器材", cny=True)],
    )

    out = rps_rotation.build_rps_rotation(repo, 12, "industry", None, market=Market.HK)

    top = out["columns"][_D0.isoformat()]
    assert top[0][0] == "资讯科技器材"
    assert top[0][1] == pytest.approx(0.03, abs=1e-9), "人民币柜台不得参与行业均值"
    assert out["concept_count"] == 2


@pytest.mark.parametrize("level", [1, 2, 3, None])
def test_hk_rotation_ignores_level(tmp_path, level):
    """恒生行业是单层分类, 没有 "一级-二级-三级" 路径可拆 → level 无意义。

    🔴 四个取值必须给出**同一份**结果: 若哪天有人给港股接上拆层逻辑, 恒生行业名
    会被 ``str.split("-")`` 切碎 (行业名本身可能含 "-"), 矩阵会静默变成一堆碎片。
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_two_industries(tmp_path)

    out = rps_rotation.build_rps_rotation(repo, 12, "industry", level, market=Market.HK)

    baseline = rps_rotation.build_rps_rotation(repo, 12, "industry", None, market=Market.HK)
    assert out == baseline


def test_hk_concept_fails_closed(tmp_path):
    """港股无概念数据源 → 空矩阵, 而不是拿 A 股概念成分套港股代码。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_two_industries(tmp_path)

    out = rps_rotation.build_rps_rotation(repo, 12, "concept", None, market=Market.HK)

    assert out == {"dates": [], "columns": {}, "concept_count": 0}


def test_hk_never_synced_returns_empty(tmp_path):
    """从没同步过港股 → 空矩阵 (不抛错, 也不返回 A 股数据)。"""
    repo = KlineRepository(DataStore(tmp_path))

    out = rps_rotation.build_rps_rotation(repo, 12, "industry", None, market=Market.HK)

    assert out == {"dates": [], "columns": {}, "concept_count": 0}


def test_hk_enriched_without_dim_returns_empty(tmp_path):
    """有行情但没维表 → 空矩阵。

    🔴 行情是真实的, 但**没有行业分类就无法聚合**。此时退回「按 symbol 各自成组」
    会渲染出一张全是 5 位代码的假矩阵 —— 宁可空 (CONTRIBUTING §4)。
    """
    repo = KlineRepository(DataStore(tmp_path))
    recs = _series("00700.HK", 0.04) + _series("00005.HK", 0.01)
    _seed_enriched(tmp_path, _HK_DIR, recs)

    out = rps_rotation.build_rps_rotation(repo, 12, "industry", None, market=Market.HK)

    assert out == {"dates": [], "columns": {}, "concept_count": 0}


def test_hk_dim_without_industry_column_returns_empty(tmp_path):
    """维表存在但缺 ``industry`` 列 (旧版同步产物) → 空矩阵, 不 KeyError。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_enriched(tmp_path, _HK_DIR, _series("00700.HK", 0.04))
    _seed_hk_dim(tmp_path, [{"symbol": "00700.HK", "name": "腾讯控股", "is_cny_counter": False}])

    out = rps_rotation.build_rps_rotation(repo, 12, "industry", None, market=Market.HK)

    assert out == {"dates": [], "columns": {}, "concept_count": 0}


def test_hk_dim_rows_without_industry_are_dropped(tmp_path):
    """维表里 ``industry`` 为 null 的标的 (东财把 "-" 清成 null) 不进矩阵。"""
    repo = KlineRepository(DataStore(tmp_path))
    recs = _series("00700.HK", 0.04) + _series("00005.HK", 0.01)
    _seed_enriched(tmp_path, _HK_DIR, recs)
    _seed_hk_dim(tmp_path, [
        _dim("00700.HK", "腾讯控股", "资讯科技器材"),
        _dim("00005.HK", "汇丰控股", None),
    ])

    out = rps_rotation.build_rps_rotation(repo, 12, "industry", None, market=Market.HK)

    assert out["concept_count"] == 1
    names = {n for col in out["columns"].values() for n, _ in col}
    assert names == {"资讯科技器材"}, "无行业的标的不得被塞进「未知」桶"


def test_hk_rotation_warmup_covers_window_first_day(tmp_path):
    """🔴 暖机窗口 pin: 请求窗口**第一天**的涨跌幅不得为 null。

    稀疏播种 (每 7 天一根 bar), 让窗口首日的前一根 bar 落在窗口之外 —— 这正是
    ``_HK_ROTATION_WARMUP_DAYS`` 存在的理由。暖机被去掉的症状是矩阵最左边一天
    整列消失, 看起来像「那天休市」, 而不是报错。
    """
    repo = KlineRepository(DataStore(tmp_path))
    # days=7 → start = _D0 - 24 天; _D0-28 那根只作暖机, _D0-35 连暖机都读不到。
    recs = [_rec("00700.HK", _D0 - timedelta(days=7 * i), 10.0 * 1.04 ** (5 - i))
            for i in range(6)]
    _seed_enriched(tmp_path, _HK_DIR, recs)
    _seed_hk_dim(tmp_path, [_dim("00700.HK", "腾讯控股", "资讯科技器材")])

    out = rps_rotation.build_rps_rotation(repo, 7, "industry", None, market=Market.HK)

    dates = out["dates"]
    assert (_D0 - timedelta(days=21)).isoformat() in dates, "窗口首日必须算得出涨跌幅"
    assert (_D0 - timedelta(days=28)).isoformat() not in dates, "暖机行不属于窗口, 必须裁掉"
    assert len(dates) == 4
    assert out["columns"][_D0.isoformat()][0][1] == pytest.approx(0.04, abs=1e-9)


def test_unregistered_market_raises(tmp_path):
    """未注册的市场**抛错**, 不兜底 CN。

    🔴 静默回退会让拼错的市场参数变成「悄悄按 A 股算」, 与 ``parse_market`` 同理。
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_two_industries(tmp_path)

    with pytest.raises(ValueError, match="未注册轮动读取器的市场"):
        rps_rotation.build_rps_rotation(repo, 12, "industry", None, market="US")


def test_cn_and_hk_result_caches_do_not_collide(tmp_path, monkeypatch):
    """两个市场的最新日相同时, 结果缓存键必须靠 market 分开。

    🔴 键里漏掉 market 的症状是「先请求谁就永远返回谁」, 而 TTL 只有 120s,
    手工点两下很难复现 —— 只能靠测试钉住。这里刻意让**两个市场都有非空矩阵且
    latest 同为 ``_D0``**: 若 CN 是空的, 它在取缓存之前就 return 了, 根本走不到
    键冲突那一步, 测试会假绿。
    """
    import types

    repo = KlineRepository(DataStore(tmp_path))
    _seed_two_industries(tmp_path)  # 港股: 资讯科技器材 +3% / 银行 0%

    # A 股替身: 概念维度走扩展数据, 这里 patch 掉读盘, 聚焦缓存键本身。
    monkeypatch.setattr(rps_rotation.ExtConfigStore, "load_all",
                        lambda self: [types.SimpleNamespace(id="ext_gn_ths")])
    monkeypatch.setattr(rps_rotation, "_dimension_field",
                        lambda cfg, kind: "所属概念" if kind == "concept" else "申万行业")
    monkeypatch.setattr(rps_rotation, "_read_ext_rows", lambda data_dir, cfg, field: [
        {"symbol": "000001.SZ", "申万行业": "人工智能"},
        {"symbol": "600000.SH", "申万行业": "银行"},
    ])
    monkeypatch.setattr(rps_rotation, "_symbol_keys", lambda row, cfg: [row["symbol"].upper()])
    monkeypatch.setattr(rps_rotation, "_latest_enriched_date", lambda repo: _D0)
    cn_pct = (
        pl.DataFrame(_series("000001.SZ", 0.09) + _series("600000.SH", 0.01))
        .select(["symbol", "date", "close"])
        .sort(["symbol", "date"])
        .with_columns(
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("change_pct")
        )
        .select(["symbol", "date", "change_pct"])
        .drop_nulls()
    )
    monkeypatch.setattr(rps_rotation, "_change_pct_range", lambda repo, s, e: cn_pct)

    # 🔴 两个市场必须用**同一个 kind**: 缓存键含 kind, 用 concept/industry 各测一边
    # 就永远撞不上, 键里漏掉 market 也测不出来。
    hk = rps_rotation.build_rps_rotation(repo, 12, "industry", None, market=Market.HK)
    cn = rps_rotation.build_rps_rotation(repo, 12, "industry", None, market=Market.CN)
    hk_again = rps_rotation.build_rps_rotation(repo, 12, "industry", None, market=Market.HK)
    cn_again = rps_rotation.build_rps_rotation(repo, 12, "industry", None, market=Market.CN)

    assert hk["columns"][_D0.isoformat()][0][0] == "资讯科技器材"
    assert cn["columns"][_D0.isoformat()][0][0] == "人工智能", "CN 不得拿到港股的矩阵"
    assert hk_again == hk, "第二次请求命中缓存也必须是港股自己的结果"
    assert cn_again == cn
    assert len(rps_rotation._cache) == 2, "两个市场各占一条缓存, 不得共用键"


def test_default_market_stays_cn(tmp_path):
    """不传 market 的既有调用方 (concept_rotation_analyzer) 行为逐字不变。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_two_industries(tmp_path)

    out = rps_rotation.build_rps_rotation(repo, 12, "industry", None)

    assert out == {"dates": [], "columns": {}, "concept_count": 0}, (
        "缺省必须是 A 股: 港股数据在场却返回空, 证明没有偷偷分派到 HK"
    )


# ================================================================
# GET /api/rps/rotation
# ================================================================


def test_rotation_endpoint_defaults_to_cn(env):
    client, _repo, tmp_path = env
    _seed_two_industries(tmp_path)

    resp = client.get("/api/rps/rotation", params={"kind": "industry", "days": 12})

    assert resp.status_code == 200
    assert resp.json() == {"dates": [], "columns": {}, "concept_count": 0}


def test_rotation_endpoint_hk_industry(env):
    client, _repo, tmp_path = env
    _seed_two_industries(tmp_path)

    resp = client.get("/api/rps/rotation", params={"market": "HK", "kind": "industry", "days": 12})

    assert resp.status_code == 200
    body = resp.json()
    assert body["concept_count"] == 2
    assert body["dates"][0] == _D0.isoformat()
    assert body["columns"][_D0.isoformat()][0][0] == "资讯科技器材"


def test_rotation_endpoint_accepts_lowercase_market(env):
    """``parse_market`` 的别名表要生效: 前端传 "hk" 不该 400。"""
    client, _repo, tmp_path = env
    _seed_two_industries(tmp_path)

    resp = client.get("/api/rps/rotation", params={"market": "hk", "kind": "industry"})

    assert resp.status_code == 200
    assert resp.json()["concept_count"] == 2


def test_rotation_endpoint_rejects_unknown_market(env):
    """🔴 非法 market 必须 400 且**报出支持清单**, 不静默回退 CN。"""
    client, _repo, tmp_path = env
    _seed_two_industries(tmp_path)

    resp = client.get("/api/rps/rotation", params={"market": "US", "kind": "industry"})

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "US" in detail
    for name in ("CN", "HK"):
        assert name in detail, f"400 文案必须列出 {name}"


def test_rotation_endpoint_direct_call_without_market_arg(env):
    """🔴 直接调用端点函数 (本仓库测试的既有做法) 时缺省值必须是真 ``None``。

    用 ``market: str | None = Query(None)`` 写法时默认值是 Query 对象本身,
    ``market is None`` 恒假 → 一律 400。这条钉住 Annotated 写法不被改回去。
    """
    _client, repo, tmp_path = env
    _seed_two_industries(tmp_path)

    request = type("R", (), {"app": type("A", (), {"state": type("S", (), {"repo": repo})()})()})()
    out = rps_api.get_rotation(request, days=12, kind="industry", level=None)

    assert out == {"dates": [], "columns": {}, "concept_count": 0}


def test_resolve_market_helper_rejects_blank():
    """空串不是「没传」, 必须 400 —— 否则 ``?market=`` 会静默变成 A 股。"""
    with pytest.raises(HTTPException) as exc:
        rps_api._resolve_market("")
    assert exc.value.status_code == 400
    assert rps_api._resolve_market(None) is Market.CN


# ================================================================
# GET /api/sector-rotation — 港股无分钟K源
# ================================================================


def test_sector_rotation_hk_fails_closed(env):
    client, _repo, _tmp_path = env

    resp = client.get("/api/sector-rotation", params={"market": "HK", "kind": "industry"})

    assert resp.status_code == 200, "fail-closed 是**数据状态**不是错误, 不该 4xx/5xx"
    body = resp.json()
    assert body["status"] == "no_data"
    assert body["reason"] == "no_minute_source"
    assert body["market"] == "HK"
    assert body["kind"] == "industry"


def test_sector_rotation_hk_fails_closed_even_with_full_cn_data(env):
    """🔴 港股拒绝与 A 股数据是否齐全**无关** —— 判据是能力位, 不是「碰巧没数据」。"""
    client, _repo, tmp_path = env
    _seed_two_industries(tmp_path)
    minute_dir = tmp_path / "kline_minute" / f"date={_D0.isoformat()}"
    minute_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(
        pl.DataFrame({"symbol": ["000001.SZ"], "datetime": [_D0], "close": [10.0], "volume": [1.0]}),
        minute_dir / "part.parquet",
    )

    resp = client.get("/api/sector-rotation", params={"market": "HK"})

    assert resp.json()["reason"] == "no_minute_source"


def test_sector_rotation_cn_still_reports_minute_missing(env):
    """A 股走原路径: 没有分钟分区时是 ``minute_missing`` 而不是 ``no_minute_source``。

    🔴 两个 reason 必须**不同**: 前者是「今天还没落盘, 等盘中同步」, 后者是
    「这个市场根本没有分钟源」。合并成一个, 前端就没法给出正确的指引。
    """
    client, _repo, _tmp_path = env

    resp = client.get("/api/sector-rotation")

    body = resp.json()
    assert body["status"] == "no_data"
    assert body["reason"] == "minute_missing"
    assert body.get("market") is None, "A 股是缺省市场, 不额外下发 market 字段"


def test_sector_rotation_rejects_unknown_market(env):
    client, _repo, _tmp_path = env

    resp = client.get("/api/sector-rotation", params={"market": "US"})

    assert resp.status_code == 400
    assert "US" in resp.json()["detail"]


def test_sector_rotation_validates_params_before_market_guard(env):
    """🔴 非法参数在哪个市场都该报错, 不能被市场分支吞掉。"""
    with pytest.raises(ValueError, match="不支持的板块维度"):
        sector_rotation_svc.build_sector_rotation(
            object(), kind="theme", market=Market.HK,
        )


# ================================================================
# POST /api/rps/rotation-analyze — 港股无大盘背景源
# ================================================================


async def test_analyze_stream_hk_emits_single_error_event(tmp_path, monkeypatch):
    """港股 AI 分析: 只推一条 error 事件, 且**不碰**矩阵与大盘背景。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_two_industries(tmp_path)

    def _boom(*a, **k):  # pragma: no cover - 被调用即失败
        raise AssertionError("港股 fail-closed 路径不得调用被 patch 的函数")

    monkeypatch.setattr("app.services.rps_rotation.build_rps_rotation", _boom)
    monkeypatch.setattr("app.services.market_overview_builder.build_market_overview", _boom)

    events = [json.loads(x) async for x in analyze_rotation_stream(repo, market=Market.HK)]

    assert len(events) == 1, "拒绝就只有一条 error, 不该有 meta/delta/done"
    assert events[0]["type"] == "error"
    assert events[0]["message"] == ANALYZE_UNSUPPORTED_NO_OVERVIEW.format(label=HK_RULES.label)
    assert "港股" in events[0]["message"], "文案里的市场名来自 rules.label"
    assert "涨幅RPS轮动" in events[0]["message"], "必须告诉用户去哪看港股轮动"


async def test_analyze_stream_cn_is_not_blocked_by_the_guard(tmp_path, monkeypatch):
    """🔴 反向 pin: 能力位守卫不得误伤 A 股。

    A 股无数据时走的是**既有**的「暂无行业轮动数据」分支, 不是新的能力位拒绝。
    """
    repo = KlineRepository(DataStore(tmp_path))

    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("空矩阵应在取大盘背景之前就返回")

    monkeypatch.setattr("app.services.market_overview_builder.build_market_overview", _boom)

    events = [json.loads(x) async for x in analyze_rotation_stream(repo, 12, "", None, None, "industry")]

    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "暂无行业轮动数据" in events[0]["message"]


def test_analyze_endpoint_hk_streams_error(env):
    """🔴 流式端点已经回了 200, 拒绝必须走 NDJSON 的 error 事件而不是 4xx。

    抛 HTTPException 的话前端 ``rotationAnalyzeStream`` 只会看到一条断掉的空流,
    用户什么提示都得不到。
    """
    client, _repo, tmp_path = env
    _seed_two_industries(tmp_path)

    resp = client.post("/api/rps/rotation-analyze", json={"market": "HK", "kind": "industry"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(ln) for ln in resp.text.splitlines() if ln.strip()]
    events = [e for e in events if e.get("type") != "heartbeat"]
    assert events, "必须至少推一条事件, 空流等于静默失败"
    assert events[0]["type"] == "error"
    assert "港股" in events[0]["message"]


def test_analyze_endpoint_rejects_unknown_market(env):
    """非法 market 在**开流之前**就该 400 —— 这时还没回 200, 可以正常报错。"""
    client, _repo, _tmp_path = env

    resp = client.post("/api/rps/rotation-analyze", json={"market": "US"})

    assert resp.status_code == 400
    assert "US" in resp.json()["detail"]


def test_analyze_request_defaults_to_cn():
    """既有前端不传 market, 请求体默认值必须解析成 A 股。"""
    assert rps_api.AnalyzeRequest().market is None
    assert rps_api._resolve_market(rps_api.AnalyzeRequest().market) is Market.CN
