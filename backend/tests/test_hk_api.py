"""港股 API 契约测试: 维表读取/过滤、日K读取的单位明示、同步入口的错误口径。

用最小 FastAPI app 只挂被测 router (项目惯例, 见 ``test_guardian_api.py``),
repo 用真实 ``KlineRepository(DataStore(tmp_path))`` 以便走通 DuckDB 视图路径。
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import hk as hk_api
from app.plugins.eastmoney import hk_financials as hf
from app.services import financial_sync_hk as fs
from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture()
def env(tmp_path, monkeypatch):
    repo = KlineRepository(DataStore(tmp_path))
    app = FastAPI()
    app.include_router(hk_api.router)
    app.state.repo = repo
    app.state.quote_service = None
    return TestClient(app), repo, tmp_path


def _seed_dim(tmp_path, rows: list[dict]) -> None:
    out = tmp_path / HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame(rows), out)


def _dim(symbol: str, name: str, *, cny: bool = False, listed=None, industry=None) -> dict:
    return {
        "symbol": symbol, "name": name, "is_cny_counter": cny,
        "hkd_counter": None if not cny else symbol.replace("8", "0", 1),
        "listing_date": listed, "industry": industry,
    }


# ---------------------------------------------------------------------------
# GET /api/hk/instruments
# ---------------------------------------------------------------------------

def test_instruments_empty_before_first_sync(env):
    """还没同步过 → 200 + synced=false, 不是 500。

    直读 parquet 而非 ``instruments_hk`` 视图: 视图在空目录下不注册
    (read_parquet 无匹配文件会抛), 走视图会让「首次使用」变成报错。
    """
    client, _, _ = env
    r = client.get("/api/hk/instruments")
    assert r.status_code == 200
    assert r.json() == {"total": 0, "items": [], "synced": False}


def test_instruments_excludes_cny_counter_by_default(env):
    client, _, tmp_path = env
    _seed_dim(tmp_path, [
        _dim("00700.HK", "腾讯控股"),
        _dim("09988.HK", "阿里巴巴-W"),
        _dim("89988.HK", "阿里巴巴-WR", cny=True),
    ])
    body = client.get("/api/hk/instruments").json()
    assert body["total"] == 2
    assert [i["symbol"] for i in body["items"]] == ["00700.HK", "09988.HK"]


def test_instruments_can_include_cny_counter(env):
    client, _, tmp_path = env
    _seed_dim(tmp_path, [_dim("09988.HK", "阿里巴巴-W"), _dim("89988.HK", "阿里巴巴-WR", cny=True)])
    body = client.get("/api/hk/instruments", params={"include_cny_counter": True}).json()
    assert body["total"] == 2


def test_instruments_q_filters_by_code_and_name(env):
    client, _, tmp_path = env
    _seed_dim(tmp_path, [_dim("00700.HK", "腾讯控股"), _dim("09988.HK", "阿里巴巴-W")])
    assert client.get("/api/hk/instruments", params={"q": "00700"}).json()["total"] == 1
    assert client.get("/api/hk/instruments", params={"q": "阿里"}).json()["total"] == 1
    assert client.get("/api/hk/instruments", params={"q": "   "}).json()["total"] == 2


def test_instruments_pagination_reports_full_total(env):
    """total 是过滤后的全量, items 是当前页 —— 前端分页器依赖这个区分。"""
    client, _, tmp_path = env
    _seed_dim(tmp_path, [_dim(f"{i:05d}.HK", f"股{i}") for i in range(1, 11)])
    body = client.get("/api/hk/instruments", params={"limit": 3, "offset": 3}).json()
    assert body["total"] == 10
    assert len(body["items"]) == 3
    assert body["items"][0]["symbol"] == "00004.HK"


def test_instruments_serializes_dates_as_iso(env):
    """listing_date 是 date 对象, 不转 ISO 串 FastAPI 会序列化失败。"""
    client, _, tmp_path = env
    _seed_dim(tmp_path, [_dim("00700.HK", "腾讯控股", listed=dt.date(2004, 6, 16))])
    item = client.get("/api/hk/instruments").json()["items"][0]
    assert item["listing_date"] == "2004-06-16"


# ---------------------------------------------------------------------------
# POST /api/hk/sync_instruments
# ---------------------------------------------------------------------------

def test_sync_instruments_returns_count_and_refreshes_views(env, monkeypatch):
    client, repo, _ = env
    monkeypatch.setattr(hk_api.instrument_sync_hk, "sync_instruments_hk", lambda data_dir: 3)
    calls = []
    monkeypatch.setattr(repo, "rebuild_views", lambda: calls.append(True))

    r = client.post("/api/hk/sync_instruments")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "count": 3}
    assert calls == [True], "同步后必须重挂视图, 否则读到旧数据"


def test_sync_instruments_reports_empty_status(env, monkeypatch):
    """count=0 (拉取失败保留历史) 要如实报 empty, 不能伪装成 ok。"""
    client, repo, _ = env
    monkeypatch.setattr(hk_api.instrument_sync_hk, "sync_instruments_hk", lambda data_dir: 0)
    monkeypatch.setattr(repo, "rebuild_views", lambda: None)
    assert client.post("/api/hk/sync_instruments").json()["status"] == "empty"


# ---------------------------------------------------------------------------
# GET /api/hk/daily
# ---------------------------------------------------------------------------

def test_daily_rejects_non_hk_symbol(env):
    """A 股代码打到港股端点 → 400, 不静默返回空 (静默空会让前端以为「没数据」)。"""
    client, _, _ = env
    for bad in ("600000.SH", "00700", "AAPL.US", ""):
        assert client.get("/api/hk/daily", params={"symbol": bad}).status_code == 400


def test_daily_declares_volume_unit_and_currency(env):
    """🔴 响应必须明示 volume 单位是股、币种是 HKD。

    前端与 A 股共用组件时最容易按「手」的习惯乘 100 —— 把单位写进契约里,
    比指望调用方记住文档可靠。
    """
    client, repo, _ = env
    repo.append_hk_daily(pl.DataFrame({
        "symbol": ["00700.HK"], "date": [dt.date(2026, 11, 13)],
        "open": [393.0], "high": [395.2], "low": [385.6], "close": [386.6],
        "volume": [31493353.0], "amount": [12251162810.0],
    }))
    repo.rebuild_views()

    body = client.get("/api/hk/daily", params={"symbol": "00700.HK"}).json()
    assert body["volume_unit"] == "shares"
    assert body["currency"] == "HKD"
    assert body["rows"][0]["volume"] == 31493353.0, "volume 被换算过"
    assert body["rows"][0]["date"] == "2026-11-13"


def test_daily_is_empty_before_sync_not_500(env):
    client, _, _ = env
    r = client.get("/api/hk/daily", params={"symbol": "00700.HK"})
    assert r.status_code == 200
    assert r.json()["rows"] == []


def test_daily_respects_date_bounds_and_order(env):
    client, repo, _ = env
    repo.append_hk_daily(pl.DataFrame({
        "symbol": ["00700.HK"] * 3,
        "date": [dt.date(2026, 11, 11), dt.date(2026, 11, 12), dt.date(2026, 11, 13)],
        "open": [1.0] * 3, "high": [1.0] * 3, "low": [1.0] * 3, "close": [1.0] * 3,
        "volume": [1.0] * 3, "amount": [1.0] * 3,
    }))
    repo.rebuild_views()

    rows = client.get("/api/hk/daily", params={
        "symbol": "00700.HK", "start": "2026-11-12", "end": "2026-11-13"
    }).json()["rows"]
    assert [r["date"] for r in rows] == ["2026-11-13", "2026-11-12"], "应按日期倒序"


# ---------------------------------------------------------------------------
# POST /api/hk/sync_daily
# ---------------------------------------------------------------------------

def test_sync_daily_conflicts_when_dim_table_empty(env):
    """维表为空 → 409 并指明先同步维表, 而不是起一个拉 0 只标的的任务。"""
    client, _, _ = env
    r = client.post("/api/hk/sync_daily", json={})
    assert r.status_code == 409
    assert "sync_instruments" in r.json()["detail"]


def test_sync_daily_rejects_empty_symbols_list(env):
    """``symbols: []`` 是调用方 bug (与「不传=全市场」语义不同), 必须 400。"""
    client, _, _ = env
    assert client.post("/api/hk/sync_daily", json={"symbols": []}).status_code == 400


def test_sync_daily_submits_job_with_explicit_symbols(env, monkeypatch):
    client, _, _ = env
    seen = {}

    async def fake_submit(repo, quote_service, make_run):
        seen["run"] = make_run(lambda *a, **k: None)
        return {"job_id": "hkjob1", "reused": False}

    monkeypatch.setattr(hk_api, "_submit_job", fake_submit)
    r = client.post("/api/hk/sync_daily", json={"symbols": ["00700.HK", "09988.HK"]})
    assert r.status_code == 200
    assert r.json()["job_id"] == "hkjob1"

    # 闭包必须真的把 symbols 透传给同步函数, 而不是只在请求体里躺着
    captured = {}
    monkeypatch.setattr(
        hk_api.kline_sync_hk, "sync_hk_daily",
        lambda repo, **kw: captured.update(kw) or 7,
    )
    monkeypatch.setattr(hk_api.kline_sync_hk, "sync_hk_enriched", lambda repo, **kw: 0)
    assert seen["run"]() == {"symbols": 2, "rows_written": 7, "enriched_rows": 0}
    assert captured["symbols"] == ["00700.HK", "09988.HK"]
    assert captured["incremental"] is True


def test_sync_daily_uses_dim_table_when_symbols_omitted(env, monkeypatch, tmp_path):
    client, _, _ = env
    _seed_dim(tmp_path, [_dim("00700.HK", "腾讯控股"), _dim("89988.HK", "阿里-WR", cny=True)])

    seen = {}

    async def fake_submit(repo, quote_service, make_run):
        seen["run"] = make_run(lambda *a, **k: None)
        return {"job_id": "j", "reused": False}

    monkeypatch.setattr(hk_api, "_submit_job", fake_submit)
    captured = {}
    monkeypatch.setattr(
        hk_api.kline_sync_hk, "sync_hk_daily",
        lambda repo, **kw: captured.update(kw) or 0,
    )
    assert client.post("/api/hk/sync_daily", json={}).status_code == 200
    seen["run"]()
    assert captured["symbols"] == ["00700.HK"], "默认应排除人民币柜台"


def test_sync_daily_days_bound_only_applies_when_not_incremental(env, monkeypatch, tmp_path):
    """``days`` 只在关掉增量时生效: 增量模式下起点由已落库最大日期决定, 传 days 也不该覆盖它。"""
    client, _, _ = env
    _seed_dim(tmp_path, [_dim("00700.HK", "腾讯控股")])

    seen = {}

    async def fake_submit(repo, quote_service, make_run):
        seen["run"] = make_run(lambda *a, **k: None)
        return {"job_id": "j", "reused": False}

    monkeypatch.setattr(hk_api, "_submit_job", fake_submit)
    captured = {}
    monkeypatch.setattr(
        hk_api.kline_sync_hk, "sync_hk_daily",
        lambda repo, **kw: captured.update(kw) or 0,
    )

    client.post("/api/hk/sync_daily", json={"days": 30})
    seen["run"]()
    assert captured["start"] is None, "增量模式下不该用 days 覆盖起点"
    assert captured["incremental"] is True

    captured.clear()
    client.post("/api/hk/sync_daily", json={"days": 30, "incremental": False})
    seen["run"]()
    assert captured["start"] == dt.date.today() - dt.timedelta(days=30)
    assert captured["incremental"] is False


def test_sync_daily_runs_enriched_stage_by_default(env, monkeypatch, tmp_path):
    """日K 同步后**默认**紧接着重算 enriched, 且用同一份标的清单。

    钉住「常规路径无从忘记」这个设计意图: 只写原始日K 而不生成 enriched 的话,
    换手率/连板这些下游要用的列根本不存在 (kline_hk_enriched/ 恒空)。
    """
    client, _, _ = env
    _seed_dim(tmp_path, [_dim("00700.HK", "腾讯控股")])

    seen = {}

    async def fake_submit(repo, quote_service, make_run):
        seen["run"] = make_run(lambda *a, **k: None)
        return {"job_id": "j", "reused": False}

    monkeypatch.setattr(hk_api, "_submit_job", fake_submit)
    monkeypatch.setattr(hk_api.kline_sync_hk, "sync_hk_daily", lambda repo, **kw: 5)
    cap = {}
    monkeypatch.setattr(
        hk_api.kline_sync_hk, "sync_hk_enriched",
        lambda repo, **kw: cap.update(kw) or 5,
    )

    assert client.post("/api/hk/sync_daily", json={}).status_code == 200
    assert seen["run"]() == {"symbols": 1, "rows_written": 5, "enriched_rows": 5}
    assert cap["symbols"] == ["00700.HK"], "enriched 阶段应复用同一份标的清单"
    # .get 而非 []: 路由不显式传 start, 断言的是**有效值**(全量发布)而非传参形式
    assert cap.get("start") is None, "全量发布 (按日分区 merge-upsert 幂等)"
    assert callable(cap["on_progress"])


def test_sync_daily_enriched_stage_can_be_disabled(env, monkeypatch, tmp_path):
    """``enriched=false`` → 只同步日K, 不碰 enriched (供「先抢数据后补算」的场景)。"""
    client, _, _ = env
    _seed_dim(tmp_path, [_dim("00700.HK", "腾讯控股")])

    seen = {}

    async def fake_submit(repo, quote_service, make_run):
        seen["run"] = make_run(lambda *a, **k: None)
        return {"job_id": "j", "reused": False}

    monkeypatch.setattr(hk_api, "_submit_job", fake_submit)
    monkeypatch.setattr(hk_api.kline_sync_hk, "sync_hk_daily", lambda repo, **kw: 5)
    called = []
    monkeypatch.setattr(
        hk_api.kline_sync_hk, "sync_hk_enriched",
        lambda repo, **kw: called.append(kw) or 99,
    )

    client.post("/api/hk/sync_daily", json={"enriched": False})
    assert seen["run"]() == {"symbols": 1, "rows_written": 5, "enriched_rows": 0}
    assert called == [], "关掉开关后不该调用 enriched"


# ---------------------------------------------------------------------------
# POST /api/hk/sync_enriched
# ---------------------------------------------------------------------------

def _capture_enriched_job(monkeypatch):
    """挂掉 _submit_job 与 sync_hk_enriched, 返回 (seen, captured)。"""
    seen, captured = {}, {}

    async def fake_submit(repo, quote_service, make_run):
        seen["run"] = make_run(lambda *a, **k: None)
        return {"job_id": "hkenr1", "reused": False}

    monkeypatch.setattr(hk_api, "_submit_job", fake_submit)
    monkeypatch.setattr(
        hk_api.kline_sync_hk, "sync_hk_enriched",
        lambda repo, **kw: captured.update(kw) or 12,
    )
    return seen, captured


def test_sync_enriched_submits_job_and_passes_params(env, monkeypatch):
    """独立重算入口: symbols / batch_size 必须真的透传到服务层。"""
    client, _, _ = env
    seen, captured = _capture_enriched_job(monkeypatch)

    r = client.post("/api/hk/sync_enriched", json={
        "symbols": ["00700.HK"], "batch_size": 50,
    })
    assert r.status_code == 200
    assert r.json()["job_id"] == "hkenr1"
    assert seen["run"]() == {"rows_written": 12}
    assert captured["symbols"] == ["00700.HK"]
    assert captured["batch_size"] == 50
    assert captured["start"] is None, "没给 days 就该全量发布"
    assert callable(captured["on_progress"])


def test_sync_enriched_days_maps_to_start(env, monkeypatch):
    """``days`` 只裁剪**发布**范围 → 映射成 start 日期传给服务层。"""
    client, _, _ = env
    seen, captured = _capture_enriched_job(monkeypatch)

    assert client.post("/api/hk/sync_enriched", json={"days": 30}).status_code == 200
    seen["run"]()
    assert captured["start"] == dt.date.today() - dt.timedelta(days=30)
    assert captured["symbols"] is None, "未点名 = 全市场"


def test_sync_enriched_rejects_empty_symbols_list(env):
    """``symbols: []`` 是调用方写错 (空列表 ≠ 全市场), 400 而不是静默全量重算。"""
    client, _, _ = env
    r = client.post("/api/hk/sync_enriched", json={"symbols": []})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 财务 (PR2b)
# ---------------------------------------------------------------------------

def _fin_payload(code="00700", name="腾讯控股", report_date="2026-06-30", **over) -> dict:
    row = {
        "SECUCODE": f"{code}.HK", "SECURITY_CODE": code, "SECURITY_NAME_ABBR": name,
        "ORG_TYPE": "一般企业", "REPORT_DATE": f"{report_date} 00:00:00",
        "DATE_TYPE_CODE": "002", "REPORT_TYPE": "2026年中报", "FISCAL_YEAR": "12-31",
        "CURRENCY": "HKD", "IS_CNY_CODE": 0, "PER_SHARES": 100,
        "BASIC_EPS": 12.6, "ROE_AVG": 9.97, "PE_TTM": 13.17, "PB_TTM": 2.69,
        "OPERATE_INCOME": 4.01e11, "TOTAL_ASSETS": 2.16e12,
        "TOTAL_LIABILITIES": 9.37e11, "TOTAL_PARENT_EQUITY": 1.14e12,
        "HOLDER_PROFIT": 1.14e11, "PRETAX_PROFIT": 1.44e11,
        "NETCASH_OPERATE": 1.54e11, "NETCASH_INVEST": -3.2e10,
        "NETCASH_FINANCE": -4.1e10,
    }
    row.update(over)
    return row


def _seed_financials(tmp_path, payloads: list[dict]) -> None:
    out = fs.hk_financials_path(tmp_path, "main_indicator")
    out.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(hf.hk_main_indicator_rows(payloads)).select(
        list(hf.HK_MAIN_INDICATOR_COLUMNS)
    )
    atomic_write_parquet(frame, out)


def _seed_statement(tmp_path, table: str, rows: list[dict]) -> None:
    out = fs.hk_financials_path(tmp_path, table)
    out.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(hf.hk_statement_rows(rows)).select(list(hf.HK_STATEMENT_COLUMNS))
    atomic_write_parquet(frame, out)


def _stmt_payload(code="00700", item="004001001", name="营业额",
                  report_date="2026-06-30", **over) -> dict:
    row = {
        "SECUCODE": f"{code}.HK", "SECURITY_CODE": code, "SECURITY_NAME_ABBR": "腾讯控股",
        "ORG_TYPE": "一般企业", "REPORT_DATE": f"{report_date} 00:00:00",
        "DATE_TYPE_CODE": "002", "REPORT_TYPE": "中报", "FISCAL_YEAR": "12-31",
        "ACCOUNT_STANDARD": "国际会计准则", "CURRENCY": "人民币", "CURRENCY_CODE": "CNY",
        "STD_ITEM_CODE": item, "ITEM_NAME": name, "AMOUNT": 3.96e11, "YOY_RATIO": 9.64,
    }
    row.update(over)
    return row


# --- GET /api/hk/financials/status ---

def test_financials_status_before_sync(env):
    """从未同步 → synced=false 且各计数为 0, 不是 500。"""
    client, _, _ = env
    body = client.get("/api/hk/financials/status").json()
    assert body["synced"] is False
    assert body["rows"] == 0 and body["symbols"] == 0
    assert body["latest_report_date"] is None
    assert body["tables"] == {"income": 0, "balance": 0}


def test_financials_status_after_sync(env, tmp_path):
    client, _, _ = env
    _seed_financials(tmp_path, [
        _fin_payload("00700", report_date="2026-06-30"),
        _fin_payload("00700", report_date="2025-12-31", DATE_TYPE_CODE="001"),
        _fin_payload("09988", name="阿里巴巴-W", PER_SHARES=100),
    ])
    body = client.get("/api/hk/financials/status").json()
    assert body["synced"] is True
    assert body["rows"] == 3
    assert body["symbols"] == 2
    assert body["latest_report_date"] == "2026-06-30"
    assert body["lot_size_coverage"] == 2
    assert body["period_names"] == ["中报", "年报"]


# --- GET /api/hk/financials ---

@pytest.mark.parametrize("bad", ["600519.SH", "600519", "", "abc"])
def test_financials_rejects_non_hk_symbol(env, bad):
    client, _, _ = env
    assert client.get("/api/hk/financials", params={"symbol": bad}).status_code == 400


def test_financials_returns_four_canonical_tables(env, tmp_path):
    client, _, _ = env
    _seed_financials(tmp_path, [_fin_payload("00700")])
    body = client.get("/api/hk/financials", params={"symbol": "00700.HK"}).json()
    assert body["synced"] is True
    assert set(body["tables"]) == {"metrics", "income", "balance_sheet", "cash_flow"}
    assert body["name"] == "腾讯控股"
    assert body["org_type"] == "一般企业"
    assert body["pricing_currency"] == "HKD"
    assert body["lot_size"] == 100
    assert body["latest_report_date"] == "2026-06-30"


def test_financials_carries_currency_warning(env, tmp_path):
    """🔴 币种警示必须随响应下发: 金额列是编报币种, 估值列是港币,
    且不同公司编报币种不同 —— 前端漏了这条就会做出跨币种相加的错误汇总。"""
    client, _, _ = env
    _seed_financials(tmp_path, [_fin_payload("00700")])
    body = client.get("/api/hk/financials", params={"symbol": "00700.HK"}).json()
    assert "编报币种" in body["currency_warning"]
    assert "港币" in body["currency_warning"]


def test_financials_announce_date_is_null_not_faked(env, tmp_path):
    """港股宽表无公告日, 必须落 null 而非拿报告期冒充 (CONTRIBUTING:586)。"""
    client, _, _ = env
    _seed_financials(tmp_path, [_fin_payload("00700")])
    row = client.get("/api/hk/financials", params={"symbol": "00700.HK"}).json()["tables"]["metrics"][0]
    assert row["announce_date"] is None
    assert row["report_date_ms"] is None
    assert row["period_end"] == "2026-06-30"
    assert row["fiscal_year"] == 2026  # Int64 年份, 不是东财的 "12-31"


def test_financials_report_currency_comes_from_statement(env, tmp_path):
    """编报币种只在长表里 (``CURRENCY_CODE``), 宽表的 ``CURRENCY`` 是计价币种。"""
    client, _, _ = env
    _seed_financials(tmp_path, [_fin_payload("00700", CURRENCY="HKD")])
    _seed_statement(tmp_path, "income", [_stmt_payload("00700", CURRENCY_CODE="CNY")])
    body = client.get("/api/hk/financials", params={"symbol": "00700.HK"}).json()
    assert body["pricing_currency"] == "HKD"
    assert body["report_currency"] == "CNY"


def test_financials_unknown_symbol_reports_not_synced(env, tmp_path):
    client, _, _ = env
    _seed_financials(tmp_path, [_fin_payload("00700")])
    body = client.get("/api/hk/financials", params={"symbol": "01234.HK"}).json()
    assert body["synced"] is False
    assert body["tables"]["metrics"] == []


def test_financials_before_any_sync(env):
    """空库也要返回四张表的键 (前端按固定键取, 缺键会崩)。"""
    client, _, _ = env
    body = client.get("/api/hk/financials", params={"symbol": "00700.HK"}).json()
    assert body["synced"] is False
    assert set(body["tables"]) == {"metrics", "income", "balance_sheet", "cash_flow"}


def test_financials_limit_bounds_periods(env, tmp_path):
    client, _, _ = env
    _seed_financials(tmp_path, [
        _fin_payload("00700", report_date=d, DATE_TYPE_CODE="001")
        for d in ("2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31")
    ])
    rows = client.get(
        "/api/hk/financials", params={"symbol": "00700.HK", "limit": 2}
    ).json()["tables"]["metrics"]
    assert [r["period_end"] for r in rows] == ["2025-12-31", "2024-12-31"]


def test_financials_limit_validation(env):
    client, _, _ = env
    assert client.get("/api/hk/financials", params={"symbol": "00700.HK", "limit": 0}).status_code == 422
    assert client.get("/api/hk/financials", params={"symbol": "00700.HK", "limit": 999}).status_code == 422


def test_financials_align_fills_ashare_columns(env, tmp_path):
    """``align=true`` 时按 A 股真实列集补 null, 使两市场帧列集一致 (共用页面前提)。"""
    client, _, tmp = env
    _seed_financials(tmp_path, [_fin_payload("00700")])
    ref = tmp / "financials" / "metrics" / "part.parquet"
    ref.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame({
        "symbol": ["600519.SH"], "period_end": ["2026-06-30"],
        "announce_date": ["2026-08-01"], "eps_basic": [1.0],
        "cash_ratio": [2.5], "quick_ratio": [2.4],
    }), ref)

    plain = client.get("/api/hk/financials", params={"symbol": "00700.HK"}).json()["tables"]["metrics"][0]
    aligned = client.get(
        "/api/hk/financials", params={"symbol": "00700.HK", "align": "true"}
    ).json()["tables"]["metrics"][0]
    assert "cash_ratio" not in plain
    assert aligned["cash_ratio"] is None
    assert aligned["quick_ratio"] is None
    # 港股独有列仍保留 (附在末尾)
    assert aligned["pe_ttm"] == 13.17


def test_financials_align_without_ashare_data_is_passthrough(env, tmp_path):
    """A 股财务未同步时不能猜列名, 原样返回 (不 500)。"""
    client, _, _ = env
    _seed_financials(tmp_path, [_fin_payload("00700")])
    body = client.get(
        "/api/hk/financials", params={"symbol": "00700.HK", "align": "true"}
    ).json()
    assert body["synced"] is True
    assert body["tables"]["metrics"][0]["pe_ttm"] == 13.17


# --- GET /api/hk/financials/statement ---

def test_statement_rejects_bad_table(env):
    client, _, _ = env
    r = client.get("/api/hk/financials/statement", params={"symbol": "00700.HK", "table": "cash_flow"})
    assert r.status_code == 400


def test_statement_rejects_non_hk_symbol(env):
    client, _, _ = env
    r = client.get("/api/hk/financials/statement", params={"symbol": "600519.SH"})
    assert r.status_code == 400


def test_statement_before_sync(env):
    """未同步 → synced=false + 空 items, 由前端引导触发按标的同步。"""
    client, _, _ = env
    body = client.get("/api/hk/financials/statement", params={"symbol": "00700.HK"}).json()
    assert body == {"symbol": "00700.HK", "table": "income", "synced": False, "items": []}


def test_statement_returns_items_with_hk_ifrs_names(env, tmp_path):
    """🔴 科目名是港式/IFRS 表述, 前端不能复用 A 股标签, 故原样下发不翻译。"""
    client, _, _ = env
    _seed_statement(tmp_path, "income", [
        _stmt_payload(item="004001001", name="营业额", AMOUNT=3.96e11),
        _stmt_payload(item="004007999", name="毛利", AMOUNT=2.30e11),
        _stmt_payload(item="004010999", name="经营溢利", AMOUNT=1.35e11),
    ])
    body = client.get("/api/hk/financials/statement", params={"symbol": "00700.HK"}).json()
    assert body["synced"] is True
    assert body["report_currency"] == "CNY"
    assert body["account_standard"] == "国际会计准则"
    assert [i["item_name"] for i in body["items"]] == ["营业额", "毛利", "经营溢利"]
    assert body["report_dates"] == ["2026-06-30"]


def test_statement_filters_by_report_date(env, tmp_path):
    client, _, _ = env
    _seed_statement(tmp_path, "income", [
        _stmt_payload(report_date="2026-06-30"),
        _stmt_payload(report_date="2025-12-31", DATE_TYPE_CODE="001"),
    ])
    body = client.get("/api/hk/financials/statement", params={
        "symbol": "00700.HK", "report_date": "2025-12-31",
    }).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["report_date"] == "2025-12-31"


def test_statement_other_symbol_not_synced(env, tmp_path):
    client, _, _ = env
    _seed_statement(tmp_path, "income", [_stmt_payload("00700")])
    body = client.get("/api/hk/financials/statement", params={"symbol": "09988.HK"}).json()
    assert body["synced"] is False


def test_statement_balance_table(env, tmp_path):
    client, _, _ = env
    _seed_statement(tmp_path, "balance", [_stmt_payload(item="005001001", name="资产总值")])
    body = client.get(
        "/api/hk/financials/statement", params={"symbol": "00700.HK", "table": "balance"}
    ).json()
    assert body["table"] == "balance"
    assert body["items"][0]["item_name"] == "资产总值"


# --- POST /api/hk/financials/statement/sync ---

def test_statement_sync_validates_symbol_and_table(env):
    client, _, _ = env
    assert client.post("/api/hk/financials/statement/sync", params={"symbol": "600519.SH"}).status_code == 400
    assert client.post("/api/hk/financials/statement/sync", params={
        "symbol": "00700.HK", "table": "nope"}).status_code == 400


def test_statement_sync_calls_service(env, monkeypatch):
    """单标的长表同步是秒级操作, 同步执行而不占用重任务槽位。"""
    client, _, _ = env
    captured = {}
    monkeypatch.setattr(
        hk_api.financial_sync_hk, "sync_hk_statement",
        lambda data_dir, symbol, *, table="income": captured.update(
            symbol=symbol, table=table) or 162,
    )
    body = client.post("/api/hk/financials/statement/sync", params={
        "symbol": "00700.HK", "table": "balance"}).json()
    assert body == {"symbol": "00700.HK", "table": "balance", "status": "ok", "rows": 162}
    assert captured == {"symbol": "00700.HK", "table": "balance"}


def test_statement_sync_reports_empty(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setattr(
        hk_api.financial_sync_hk, "sync_hk_statement", lambda *a, **k: 0
    )
    body = client.post("/api/hk/financials/statement/sync", params={"symbol": "00700.HK"}).json()
    assert body["status"] == "empty"


# --- POST /api/hk/sync_financials ---

def test_sync_financials_rejects_empty_symbol_list(env):
    client, _, _ = env
    r = client.post("/api/hk/sync_financials", json={"symbols": []})
    assert r.status_code == 400


def test_sync_financials_submits_job_and_passes_params(env, monkeypatch):
    """闭包必须真的把参数透传给同步函数, 而不是只在请求体里躺着。"""
    client, _, _ = env
    seen = {}

    async def fake_submit(repo, quote_service, make_run):
        seen["run"] = make_run(lambda *a, **k: None)
        return {"job_id": "hkfin1", "reused": False}

    monkeypatch.setattr(hk_api, "_submit_job", fake_submit)
    captured = {}
    monkeypatch.setattr(
        hk_api.financial_sync_hk, "sync_hk_main_indicator",
        lambda data_dir, **kw: captured.update(kw) or 43,
    )

    r = client.post("/api/hk/sync_financials", json={
        "symbols": ["00700.HK", "09988.HK"], "years": 3,
        "annual_only": True, "backfill_lot_size": False,
    })
    assert r.status_code == 200
    assert r.json()["job_id"] == "hkfin1"

    assert seen["run"]() == {"symbols": 2, "rows_written": 43}
    assert captured["secucodes"] == ["00700.HK", "09988.HK"]
    assert captured["date_type_code"] == hf.HK_DATE_TYPE_ANNUAL
    assert captured["backfill_lot_size"] is False
    assert captured["since"] == fs.default_hk_fin_since(years=3)


def test_sync_financials_defaults_to_whole_market(env, monkeypatch):
    """不给 symbols = 全市场分页 (实测 6 年窗口约 60~70 页覆盖全部港股)。"""
    client, _, _ = env
    seen = {}

    async def fake_submit(repo, quote_service, make_run):
        seen["run"] = make_run(lambda *a, **k: None)
        return {"job_id": "j", "reused": False}

    monkeypatch.setattr(hk_api, "_submit_job", fake_submit)
    captured = {}
    monkeypatch.setattr(
        hk_api.financial_sync_hk, "sync_hk_main_indicator",
        lambda data_dir, **kw: captured.update(kw) or 0,
    )
    assert client.post("/api/hk/sync_financials", json={}).status_code == 200
    seen["run"]()
    assert captured["secucodes"] is None
    assert captured["date_type_code"] is None
    assert captured["backfill_lot_size"] is True
    assert captured["since"] is None  # 缺省窗口由服务层决定


def test_sync_financials_progress_is_monotonic(env, monkeypatch):
    """进度回调必须转成 pipeline 的 ``(stage, pct, msg)`` 形态, 否则进度条不动。"""
    client, _, _ = env
    seen = {}

    async def fake_submit(repo, quote_service, make_run):
        seen["run"] = make_run(
            lambda stage, pct, msg, **k: seen.setdefault("calls", []).append((stage, pct))
        )
        return {"job_id": "j", "reused": False}

    def fake_sync(data_dir, **kw):
        cb = kw["on_progress"]
        for pct in (5, 40, 80, 100):
            cb("hk_financials", pct, f"{pct}%")
        return 1

    monkeypatch.setattr(hk_api, "_submit_job", fake_submit)
    monkeypatch.setattr(hk_api.financial_sync_hk, "sync_hk_main_indicator", fake_sync)
    client.post("/api/hk/sync_financials", json={})
    seen["run"]()
    assert seen["calls"] == [("hk_financials", p) for p in (5, 40, 80, 100)]
