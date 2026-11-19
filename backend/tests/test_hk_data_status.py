"""港股数据画像 (``GET /api/hk/status``) 与「清空数据」的港股覆盖。

画像的核心契约有三条, 本文件逐条钉死:

1. **空态不是 500**。港股视图只在目录里有 parquet 时才注册, 走视图会让「还没同步
   过」变成异常。所有聚合必须直读目录/文件, 没数据如实返 ``None``。
2. **两种行数口径是有意为之**。个股日K/enriched 的 ``rows`` 恒为 0 (「没统计」,
   与 A 股 ``_safe_aggregate_daily`` 同约定), 指数表则是精确行数 (万行级, 读得起)。
   混为一谈会让「0 行」在两种表上表达相反的意思。
3. **fail-closed 闸门的前置条件必须可见**。``adj_factor_hk/coverage.json`` 缺失或
   版本不符时 ``sync_hk_enriched`` 会整轮/逐标的拒算, 而 ``sync_hk_adj_factor``
   没有独立 HTTP 入口 —— 画像不露出 ``has_coverage``/``uncovered``, 用户就无从
   判断「enriched 行数偏少」是被闸门挡了还是数据源没数据。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import data as data_api
from app.api import hk as hk_api
from app.services import kline_sync_hk
from app.services.fs_utils import atomic_write_parquet
from app.services.hk_data_status import (
    hk_data_status,
    hk_kline_dirnames,
    hk_storage_dirnames,
)
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import DataStore, KlineRepository, daily_dirname, enriched_dirname


@pytest.fixture()
def env(tmp_path):
    """最小 app 只挂 hk router (项目惯例, 见 ``test_hk_api.py``)。"""
    repo = KlineRepository(DataStore(tmp_path))
    app = FastAPI()
    app.include_router(hk_api.router)
    app.state.repo = repo
    app.state.quote_service = None
    return TestClient(app), repo, tmp_path


def _seed_dim(root: Path, rows: list[dict]) -> None:
    out = root / HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame(rows), out)


def _dim(symbol: str, name: str, *, cny: bool = False, industry: str | None = None) -> dict:
    return {
        "symbol": symbol, "name": name, "is_cny_counter": cny,
        "industry": industry, "as_of": date(2026, 11, 19),
    }


def _seed_partition(root: Path, dirname: str, ds: str, symbols: list[str], *, extra: int = 0) -> None:
    d = root / dirname / f"date={ds}"
    d.mkdir(parents=True, exist_ok=True)
    cols: dict[str, list] = {
        "symbol": symbols,
        "date": [ds] * len(symbols),
        "close": [1.0] * len(symbols),
    }
    for i in range(extra):
        cols[f"ind_{i}"] = [0.0] * len(symbols)
    atomic_write_parquet(pl.DataFrame(cols), d / "part.parquet")


def _seed_coverage(root: Path, *, covered: list[str], uncovered: list[str], version: int | None = None) -> None:
    path = root / kline_sync_hk.HK_ADJ_COVERAGE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": kline_sync_hk.HK_ADJ_COVERAGE_VERSION if version is None else version,
        "covered": covered,
        "uncovered": uncovered,
        "updated_at": "2026-11-19T10:00:00+08:00",
    }), encoding="utf-8")


# ---------------------------------------------------------------------------
# 空态: 契约 1
# ---------------------------------------------------------------------------

def test_status_all_null_before_first_sync(env):
    """全新数据目录 → 200 + 各块 None, 不是 500。

    🔴 这条是「首次使用」的全部体验: 视图未注册时走 SQL 会抛, 端点必须直读文件。
    """
    client, _, _ = env
    r = client.get("/api/hk/status")
    assert r.status_code == 200
    body = r.json()
    for key in ("instruments", "daily", "enriched", "index_daily", "index_enriched", "adj_factor"):
        assert body[key] is None, key
    assert body["checked_at"]


def test_status_financials_block_is_unsynced_not_null(env):
    """财务块用 ``synced=false`` 表达空态, 而不是 None。

    与其它块的口径不同是**刻意**的: 该形状逐字复用既有 ``GET /financials/status``
    的响应, 前端 ``financial-market-adapter`` 已经按它归一化, 改成 None 会同时
    破坏两处。
    """
    client, _, _ = env
    body = client.get("/api/hk/status").json()
    assert body["financials"] == {
        "synced": False, "rows": 0, "symbols": 0,
        "latest_report_date": None, "lot_size_coverage": None,
        "tables": {"income": 0, "balance": 0},
    }


def test_status_storage_zero_when_empty(env):
    client, _, _ = env
    storage = client.get("/api/hk/status").json()["storage"]
    assert storage["total_size_mb"] == 0.0
    for key in hk_storage_dirnames():
        assert storage[f"{key}_files"] == 0
        assert storage[f"{key}_size_mb"] == 0.0


def test_corrupt_dim_yields_null_not_500(env):
    """维表文件损坏 → 该块 None, 其余块照常聚合。

    一块坏数据不该让整张画像 500 —— 那样用户连「哪块是好的」都看不到。
    """
    client, _, root = env
    path = root / HK_INSTRUMENT_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not-a-parquet")
    _seed_partition(root, "kline_hk_daily", "2026-11-19", ["00700.HK"])

    body = client.get("/api/hk/status").json()
    assert body["instruments"] is None
    assert body["daily"]["trading_days"] == 1


# ---------------------------------------------------------------------------
# 维表: 双柜台分开计数
# ---------------------------------------------------------------------------

def test_instruments_splits_dual_counters(env):
    """🔴 港币/人民币柜台必须分开报数。

    两个柜台是同一份基本面, 全市场口径只算港币柜台
    (``test_hk_dual_counter.py`` 钉死了这条策略)。只报 ``rows`` 会让人以为
    港股有 rows 家上市公司。
    """
    client, _, root = env
    _seed_dim(root, [
        _dim("00700.HK", "腾讯控股", industry="资讯科技"),
        _dim("00005.HK", "汇丰控股", industry="银行"),
        _dim("80700.HK", "腾讯控股-R", cny=True, industry="资讯科技"),
    ])
    inst = client.get("/api/hk/status").json()["instruments"]
    assert inst["rows"] == 3
    assert inst["hkd_counters"] == 2
    assert inst["cny_counters"] == 1
    assert inst["industries"] == 2
    assert inst["named"] == 3
    assert inst["latest_as_of"] == "2026-11-19"


def test_instruments_survives_cny_flag_dtype_drift(env):
    """``is_cny_counter`` 是 Int8 而非 Boolean 时仍要算得出来。

    历史维表由不同版本的拉取代码写过, 该列类型漂移过。直接 ``fill_null(False)``
    会因类型不符抛错 → 整块返 None → 画像显示「未同步」, 而数据其实在。
    """
    client, _, root = env
    _seed_dim(root, [
        {"symbol": "00700.HK", "name": "腾讯控股", "is_cny_counter": 0},
        {"symbol": "80700.HK", "name": "腾讯控股-R", "is_cny_counter": 1},
    ])
    inst = client.get("/api/hk/status").json()["instruments"]
    assert inst is not None
    assert (inst["hkd_counters"], inst["cny_counters"]) == (1, 1)


def test_instruments_missing_optional_columns(env):
    """只有 symbol 一列也算已同步 —— 可选列缺失降级为 0/None, 不整块失败。"""
    client, _, root = env
    _seed_dim(root, [{"symbol": "00700.HK"}, {"symbol": "00005.HK"}])
    inst = client.get("/api/hk/status").json()["instruments"]
    assert inst["rows"] == 2
    assert inst["named"] == 0
    assert inst["industries"] == 0
    assert inst["latest_as_of"] is None
    assert inst["cny_counters"] == 0


# ---------------------------------------------------------------------------
# 个股日K / enriched: 契约 2 的前半
# ---------------------------------------------------------------------------

def test_daily_rows_zero_means_not_counted(env):
    """``rows == 0`` 是「没统计」的约定值, 与 A 股 ``_safe_aggregate_daily`` 一致。

    全历史行数要打开上千个 parquet; 画像卡回答的是「覆盖到哪天、多少只」。
    前端据 ``trading_days`` 而非 ``rows`` 判断有无数据。
    """
    client, _, root = env
    _seed_partition(root, "kline_hk_daily", "2026-11-18", ["00700.HK"])
    _seed_partition(root, "kline_hk_daily", "2026-11-19", ["00700.HK", "00005.HK"])
    daily = client.get("/api/hk/status").json()["daily"]
    assert daily["rows"] == 0
    assert daily["trading_days"] == 2
    assert (daily["earliest_date"], daily["latest_date"]) == ("2026-11-18", "2026-11-19")


def test_daily_symbols_covered_is_latest_partition_only(env):
    """🔴 ``symbols_covered`` 是**最新交易日**的标的数, 不是全历史 distinct。

    钉死这个语义: 若哪天有人为了「更准」改成全表 distinct, 画像就会从毫秒级
    退化成扫全表 (百万行级), 而卡片上那个数字的含义也会悄悄变掉。
    """
    client, _, root = env
    _seed_partition(root, "kline_hk_daily", "2026-11-18", ["00700.HK", "00005.HK", "09988.HK"])
    _seed_partition(root, "kline_hk_daily", "2026-11-19", ["00700.HK"])
    assert client.get("/api/hk/status").json()["daily"]["symbols_covered"] == 1


def test_daily_symbols_zero_when_latest_partition_unreadable(env):
    """最新分区是空目录 (写入中断) → 标的数 0, 但日期范围仍如实给出。"""
    client, _, root = env
    _seed_partition(root, "kline_hk_daily", "2026-11-18", ["00700.HK"])
    (root / "kline_hk_daily" / "date=2026-11-19").mkdir(parents=True)
    daily = client.get("/api/hk/status").json()["daily"]
    assert daily["latest_date"] == "2026-11-19"
    assert daily["symbols_covered"] == 0


def test_enriched_reports_field_count_from_schema(env):
    """enriched 多一个 ``fields``, 且来自 parquet footer 而非数据扫描。"""
    client, _, root = env
    _seed_partition(root, "kline_hk_enriched", "2026-11-19", ["00700.HK"], extra=4)
    enr = client.get("/api/hk/status").json()["enriched"]
    assert enr["fields"] == 7  # symbol + date + close + ind_0..3
    assert enr["rows"] == 0


def test_daily_has_no_fields_key(env):
    """原始日K不带 ``fields`` —— 前端据此区分「原始」与「已计算」两张卡。"""
    client, _, root = env
    _seed_partition(root, "kline_hk_daily", "2026-11-19", ["00700.HK"])
    assert "fields" not in client.get("/api/hk/status").json()["daily"]


# ---------------------------------------------------------------------------
# 指数表: 契约 2 的后半 (精确口径)
# ---------------------------------------------------------------------------

def test_index_daily_rows_are_exact(env):
    """指数表报**真实行数** —— 与个股表的 0 形成有意的对比。

    港股指数只有白名单 27 只 × ``HK_INDEX_DAILY_COUNT`` 天 (万行级), 读得起;
    个股是百万行级, 读不起。同一个字段名在两类表上口径不同, 是规模差异导致的
    显式取舍, 不是疏漏。
    """
    client, _, root = env
    _seed_partition(root, "kline_hk_index_daily", "2026-11-18", ["HSI.HK", "HSCEI.HK"])
    _seed_partition(root, "kline_hk_index_daily", "2026-11-19", ["HSI.HK"])
    idx = client.get("/api/hk/status").json()["index_daily"]
    assert idx["rows"] == 3
    assert idx["symbols_covered"] == 2  # 全历史 distinct, 不是最新分区的 1
    assert idx["trading_days"] == 2


def test_index_enriched_null_when_dir_absent(env):
    """目录不存在 → None (不是 0 行)。None 与「有目录但空」都表达未同步。"""
    client, _, root = env
    _seed_partition(root, "kline_hk_index_daily", "2026-11-19", ["HSI.HK"])
    body = client.get("/api/hk/status").json()
    assert body["index_daily"] is not None
    assert body["index_enriched"] is None


def test_empty_partition_dir_is_null_not_zero(env):
    """有目录但没有任何 ``date=`` 分区 → None。

    ``KlineRepository`` 构造时就会 mkdir 全部港股目录, 所以「目录存在」绝不等于
    「同步过」—— 这条正是那个前提的直接后果。
    """
    client, _, root = env
    assert (root / "kline_hk_daily").exists()  # repo 初始化即建目录
    (root / "kline_hk_daily").mkdir(parents=True, exist_ok=True)
    assert client.get("/api/hk/status").json()["daily"] is None


# ---------------------------------------------------------------------------
# 复权因子凭据: 契约 3
# ---------------------------------------------------------------------------

def test_adj_factor_without_coverage_is_visible(env):
    """🔴 有因子库但**无凭据** → ``has_coverage=false``, 而不是把这块藏成 None。

    凭据缺失时 ``sync_hk_enriched`` 整轮返 0 行。画像若只显示「因子 N 行」,
    用户会以为一切正常, 然后对着「enriched 0 行」百思不得其解。
    """
    client, _, root = env
    fp = root / kline_sync_hk.HK_ADJ_FACTOR_RELPATH
    fp.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame({
        "symbol": ["00700.HK", "00005.HK"],
        "trade_date": ["2025-01-01", "2025-06-01"],
        "factor": [1.0, 1.0],
    }), fp)

    adj = client.get("/api/hk/status").json()["adj_factor"]
    assert adj["rows"] == 2
    assert adj["symbols_covered"] == 2
    assert adj["has_coverage"] is False
    assert adj["coverage_unreadable"] is False  # 文件根本不存在, 不是「读不出来」
    assert (adj["covered"], adj["uncovered"]) == (0, 0)
    assert adj["coverage_updated_at"] is None


def test_adj_factor_reports_uncovered_symbols(env):
    """凭据存在 → covered/uncovered 真实数字 + 更新时间。"""
    client, _, root = env
    _seed_coverage(root, covered=["00700.HK", "00005.HK"], uncovered=["09988.HK"])
    adj = client.get("/api/hk/status").json()["adj_factor"]
    assert adj["has_coverage"] is True
    assert (adj["covered"], adj["uncovered"]) == (2, 1)
    assert adj["coverage_updated_at"] == "2026-11-19T10:00:00+08:00"
    # 只有凭据没有因子库时也要露出来 (rows=0 但 has_coverage=True)
    assert adj["rows"] == 0


def test_adj_factor_wrong_coverage_version_fails_closed(env):
    """凭据 schema 版本不符 → 当「无凭据」处理, 但**如实报告文件在**。

    🔴 ``has_coverage=false`` + ``coverage_unreadable=true`` 与「从来没跑过」
    (两个都 false) 必须能区分: 前者说明 ``sync_hk_adj_factor`` 跑过而产物已失效,
    用户要去查版本迁移; 合并成一种就会把「凭据损坏」静默成「还没同步」。
    """
    client, _, root = env
    _seed_coverage(
        root, covered=["00700.HK"], uncovered=[],
        version=kline_sync_hk.HK_ADJ_COVERAGE_VERSION + 1,
    )
    adj = client.get("/api/hk/status").json()["adj_factor"]
    assert adj is not None
    assert adj["has_coverage"] is False
    assert adj["coverage_unreadable"] is True
    assert adj["covered"] == 0


def test_adj_factor_corrupt_coverage_is_unreadable(env):
    """凭据是坏 JSON → 同样归入 ``coverage_unreadable``, 不抛给调用方。"""
    client, _, root = env
    path = root / kline_sync_hk.HK_ADJ_COVERAGE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    adj = client.get("/api/hk/status").json()["adj_factor"]
    assert adj["has_coverage"] is False
    assert adj["coverage_unreadable"] is True


def test_adj_factor_null_when_nothing_at_all(env):
    """因子库与凭据都不存在 → None (真的什么都没同步过)。"""
    client, _, _ = env
    assert client.get("/api/hk/status").json()["adj_factor"] is None


# ---------------------------------------------------------------------------
# 财务块与既有端点共用实现
# ---------------------------------------------------------------------------

def test_financials_block_matches_dedicated_endpoint(env):
    """🔴 ``status.financials`` 必须与 ``GET /financials/status`` **逐字相同**。

    实现搬进 ``hk_data_status`` 就是为了只有一份; 这条断言是防回归的护栏 ——
    两处各算一遍的话, 「画像卡说已同步、财务页说没同步」迟早会出现。
    """
    client, _, _ = env
    a = client.get("/api/hk/status").json()["financials"]
    b = client.get("/api/hk/financials/status").json()
    assert a == b


# ---------------------------------------------------------------------------
# 目录名权威性与存储统计
# ---------------------------------------------------------------------------

def test_kline_dirnames_come_from_repository_mapping():
    """K 线目录名取自 ``repository`` 的权威映射, 不在本模块抄字面量。

    抄两份就会漂移, 而漂移的后果是「画像永远显示未同步」这种静默错误。
    """
    from app.market import Market

    assert hk_kline_dirnames() == {
        "daily": daily_dirname("stock", market=Market.HK),
        "enriched": enriched_dirname("stock", market=Market.HK),
        "index_daily": daily_dirname("index", market=Market.HK),
        "index_enriched": enriched_dirname("index", market=Market.HK),
    }
    assert set(hk_kline_dirnames().values()) == {
        "kline_hk_daily", "kline_hk_enriched",
        "kline_hk_index_daily", "kline_hk_index_enriched",
    }


def test_storage_dirnames_cover_every_hk_dir():
    """存储统计覆盖全部 7 个港股目录 (方案 §4 闸3 的隔离清单 + 因子 + 财务)。"""
    assert set(hk_storage_dirnames().values()) == {
        "instruments_hk", "kline_hk_daily", "kline_hk_enriched",
        "kline_hk_index_daily", "kline_hk_index_enriched",
        "adj_factor_hk", "financials_hk",
    }


def test_storage_counts_files_and_sums_total(env):
    client, _, root = env
    _seed_dim(root, [_dim("00700.HK", "腾讯控股")])
    _seed_partition(root, "kline_hk_daily", "2026-11-19", ["00700.HK"])
    _seed_coverage(root, covered=["00700.HK"], uncovered=[])

    storage = client.get("/api/hk/status").json()["storage"]
    assert storage["instruments_files"] == 1
    assert storage["daily_files"] == 1
    # adj_factor_hk 下只有 coverage.json (因子库未建)
    assert storage["adj_factor_files"] == 1
    assert storage["financials_files"] == 0
    assert storage["total_size_mb"] == round(
        sum(v for k, v in storage.items() if k.endswith("_size_mb") and k != "total_size_mb"), 2
    )


def test_status_is_pure_function_of_data_dir(tmp_path):
    """聚合函数只依赖 ``data_dir``, 不需要 repo/DuckDB 连接。

    这是「不走视图」的直接收益: 可以在没有 app、没有 DuckDB 的纯单测里调用。
    """
    _seed_partition(tmp_path, "kline_hk_daily", "2026-11-19", ["00700.HK"])
    out = hk_data_status(tmp_path)
    assert out["daily"]["trading_days"] == 1
    assert out["instruments"] is None


# ---------------------------------------------------------------------------
# 清空数据覆盖港股 (与画像同批修: 卡片与「清空数据」同页, 漏清就是明面上的假象)
# ---------------------------------------------------------------------------

class _RepoStub:
    def __init__(self, data_dir: Path) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)

    def clear_cache(self) -> None: ...
    def refresh_cache(self) -> None: ...
    def rebuild_views(self) -> None: ...


def _stub_clear_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import overview
    from app.services import alert_store, pipeline_jobs
    from app.services.pipeline_jobs import job_store
    from app.services.screener import ScreenerService

    monkeypatch.setattr(job_store, "clear", lambda: None)
    monkeypatch.setattr(alert_store, "clear", lambda _data_dir: None)
    monkeypatch.setattr(ScreenerService, "clear_history_cache", lambda: None)
    monkeypatch.setattr(overview, "invalidate_overview_cache", lambda: None)
    monkeypatch.setattr(data_api, "invalidate_data_cache", lambda _table=None, **_kw: None)
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)


def test_clear_data_removes_every_hk_dir(tmp_path, monkeypatch):
    """🔴 「清空数据」必须覆盖全部港股目录。

    此前 ``_clear_data_impl`` 的目录清单只有 A 股: 点完清空, 港股画像卡还显示
    有数据 —— 属 CONTRIBUTING §12 的拒收情形「写入后存在已知缓存不一致」。
    清单取自 ``hk_storage_dirnames()``, 所以新增港股目录会自动被覆盖。
    """
    _stub_clear_side_effects(monkeypatch)
    seeded: list[Path] = []
    for dirname in hk_storage_dirnames().values():
        f = tmp_path / dirname / "date=2026-11-19" / "part.parquet"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"parquet-placeholder")
        seeded.append(f)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=_RepoStub(tmp_path))))
    result = data_api.clear_data(request)

    assert result["deleted_files"] >= len(seeded)
    for f in seeded:
        assert not f.exists(), f


def test_clear_data_leaves_a_share_list_intact(tmp_path, monkeypatch):
    """加港股目录不得改变 A 股的清除行为 (存量回归护栏)。"""
    _stub_clear_side_effects(monkeypatch)
    cn = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    cn.parent.mkdir(parents=True, exist_ok=True)
    cn.write_bytes(b"parquet-placeholder")

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=_RepoStub(tmp_path))))
    assert data_api.clear_data(request) == {"deleted_files": 1}
    assert not cn.exists()


# ── 局部损坏不拖垮整体 ──────────────────────────────────────────────


def test_corrupt_block_is_isolated(env):
    """一个目录里的 parquet 损坏 → 该块 null, 其余块照常返回, 不 500。

    🔴 画像的价值在于「坏在哪看得见」: 整页 500 会让用户连
    「财务同步过没有」都查不到, 只能去翻日志。
    """
    client, _, root = env
    _seed_dim(root, [_dim("00700.HK", "腾讯控股"), _dim("09988.HK", "阿里巴巴-W")])
    _seed_partition(root, "kline_hk_daily", "2026-11-19", ["00700.HK"])
    bad = root / "kline_hk_index_daily" / "date=2026-11-19" / "part.parquet"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not a parquet file")

    resp = client.get("/api/hk/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["index_daily"] is None
    assert body["instruments"]["rows"] == 2
    assert body["daily"]["trading_days"] == 1
    assert body["storage"]["index_daily_files"] == 1  # 坏文件也占空间, 照实算
