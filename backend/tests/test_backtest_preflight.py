"""P0-6 回测入口检查员 (app/backtest/preflight.py) 测试。

覆盖三层:
  1. 校验器单测 — 日期/资产类型/数值参数/数据覆盖四类检查的 error/warning 判定;
  2. enforce_plan 契约 — error 抛 HTTP 400 (detail 含"问题+建议"), warning 返回文本列表;
  3. API 入口接线 — 代表性入口传坏计划得到 400, 合法计划放行且 warning 挂到结果上。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.backtest import router
from app.backtest.preflight import (
    PreflightIssue,
    attach_warnings,
    enforce_plan,
    validate_plan,
)


# ------------------------------------------------------------------
# 测试替身
# ------------------------------------------------------------------


class FakeRepo:
    """最小 Repository 替身: 只回应 preflight 用到的两种查询。

    - execute_one("SELECT min(date) ...")  → (earliest,) 或 (None,)
    - execute_all("SELECT DISTINCT symbol ... IN (...)") → 命中标的行
    """

    def __init__(self, earliest: date | None = None, symbols: tuple[str, ...] = (),
                 *, fail: bool = False):
        self.earliest = earliest
        self.symbols = set(symbols)
        self.fail = fail

    def execute_one(self, sql, params=None):
        if self.fail:
            raise RuntimeError("db busy")
        return (self.earliest,)

    def execute_all(self, sql, params=None):
        if self.fail:
            raise RuntimeError("db busy")
        wanted = params or []
        return [(s,) for s in wanted if s in self.symbols]


def _levels(issues: list[PreflightIssue]) -> list[str]:
    return [i.level for i in issues]


def _texts(issues: list[PreflightIssue]) -> str:
    return " ".join(i.render() for i in issues)


# ------------------------------------------------------------------
# 1. 日期检查
# ------------------------------------------------------------------


def test_start_after_end_is_error():
    issues = validate_plan(start=date(2024, 5, 1), end=date(2024, 1, 1))
    assert "error" in _levels(issues)
    assert "晚于结束日期" in _texts(issues)
    assert "建议" in _texts(issues)


def test_short_window_is_warning_not_error():
    issues = validate_plan(start=date(2024, 1, 1), end=date(2024, 1, 20))
    assert _levels(issues) == ["warning"]
    assert "样本太少" in _texts(issues)


def test_end_in_future_is_warning():
    future = date.today() + timedelta(days=30)
    issues = validate_plan(start=future - timedelta(days=60), end=future)
    assert "warning" in _levels(issues)
    assert "晚于今天" in _texts(issues)


def test_healthy_window_has_no_issues():
    end = date.today()
    issues = validate_plan(start=end - timedelta(days=365), end=end)
    assert issues == []


# ------------------------------------------------------------------
# 2. 资产类型检查
# ------------------------------------------------------------------


def test_unknown_asset_type_is_error():
    issues = validate_plan(asset_type="crypto")
    assert "error" in _levels(issues)
    assert "不支持的资产类型" in _texts(issues)


@pytest.mark.parametrize("asset_type", ["stock", "etf", "index"])
def test_known_asset_types_pass(asset_type):
    assert validate_plan(asset_type=asset_type) == []


# ------------------------------------------------------------------
# 3. 数值参数检查
# ------------------------------------------------------------------


@pytest.mark.parametrize("kwargs,keyword", [
    ({"fees_pct": 2.5}, "fees_pct"),            # 把万2.5填成2.5的经典错误
    ({"fees_pct": -0.001}, "fees_pct"),
    ({"commission_pct": 0.05}, "commission_pct"),
    ({"stamp_tax_pct": 1.0}, "stamp_tax_pct"),
    ({"slippage_bps": 500}, "slippage_bps"),
    ({"slippage_bps": -1}, "slippage_bps"),
    ({"initial_capital": 0}, "initial_capital"),
    ({"initial_capital": -100000}, "initial_capital"),
    ({"initial_capital": 1e15}, "initial_capital"),
    ({"max_positions": 0}, "max_positions"),
    ({"max_positions": 501}, "max_positions"),
    ({"holding_days": 0}, "holding_days"),
    ({"holding_days": 251}, "holding_days"),
    ({"max_exposure_pct": 0}, "max_exposure_pct"),
    ({"max_exposure_pct": 1.5}, "max_exposure_pct"),
])
def test_bad_numeric_params_are_errors(kwargs, keyword):
    issues = validate_plan(**kwargs)
    assert "error" in _levels(issues), kwargs
    assert keyword in _texts(issues)
    # 每条 error 都必须带"怎么改"建议
    assert all(i.suggestion for i in issues if i.level == "error")


def test_reasonable_numeric_params_pass():
    issues = validate_plan(
        fees_pct=0.00025,
        commission_pct=0.00025,
        stamp_tax_pct=0.0005,
        slippage_bps=5,
        initial_capital=1_000_000,
        max_positions=10,
        holding_days=5,
        max_exposure_pct=1.0,
    )
    assert issues == []


# ------------------------------------------------------------------
# 4. 数据覆盖检查
# ------------------------------------------------------------------


def test_no_local_daily_data_is_error():
    repo = FakeRepo(earliest=None)
    issues = validate_plan(start=date(2024, 1, 1), end=date(2024, 6, 1), repo=repo)
    assert "error" in _levels(issues)
    assert "没有A股日K数据" in _texts(issues)
    assert "同步" in _texts(issues)


def test_start_before_earliest_is_warning():
    repo = FakeRepo(earliest=date(2024, 3, 1))
    issues = validate_plan(start=date(2024, 1, 1), end=date(2024, 6, 1), repo=repo)
    assert _levels(issues) == ["warning"]
    assert "2024-03-01" in _texts(issues)


def test_missing_symbols_is_error_and_lists_them():
    repo = FakeRepo(earliest=date(2020, 1, 1), symbols=("000001",))
    issues = validate_plan(
        start=date(2024, 1, 1), end=date(2024, 6, 1),
        repo=repo, symbols=["000001", "600519", "300750"],
    )
    assert "error" in _levels(issues)
    text = _texts(issues)
    assert "600519" in text and "300750" in text
    assert "000001" not in text.split("无A股日K数据:")[1]  # 有数据的标的不进缺失清单


def test_missing_symbols_list_truncated_over_10():
    repo = FakeRepo(earliest=date(2020, 1, 1), symbols=())
    symbols = [f"{i:06d}" for i in range(15)]
    issues = validate_plan(
        start=date(2024, 1, 1), end=date(2024, 6, 1), repo=repo, symbols=symbols,
    )
    text = _texts(issues)
    assert "等 15 只" in text


def test_repo_failure_fails_open():
    """检查员自己出故障时必须放行 — 不能成为新的故障点 (引擎层还有兜底)。"""
    repo = FakeRepo(fail=True)
    issues = validate_plan(
        start=date(2024, 1, 1), end=date(2024, 6, 1), repo=repo, symbols=["000001"],
    )
    assert issues == []


def test_etf_asset_type_uses_etf_view():
    """ETF 计划查 kline_etf_daily 视图 (SQL 里带视图名), 无数据时报 ETF 而非 A股。"""
    repo = FakeRepo(earliest=None)
    issues = validate_plan(
        start=date(2024, 1, 1), end=date(2024, 6, 1), repo=repo, asset_type="etf",
    )
    assert "没有ETF日K数据" in _texts(issues)


# ------------------------------------------------------------------
# 5. enforce_plan / attach_warnings 契约
# ------------------------------------------------------------------


def test_enforce_plan_raises_400_with_problem_and_fix_lines():
    with pytest.raises(HTTPException) as exc_info:
        enforce_plan(start=date(2024, 5, 1), end=date(2024, 1, 1), fees_pct=2.5)
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert "回测计划未通过检查" in detail
    # 两条 error 各占一行, 且都带建议
    lines = [ln for ln in detail.splitlines() if ln.startswith("- ")]
    assert len(lines) == 2
    assert all("建议" in ln for ln in lines)


def test_enforce_plan_returns_warnings_without_raising():
    warnings = enforce_plan(start=date(2024, 1, 1), end=date(2024, 1, 20))
    assert len(warnings) == 1
    assert "样本太少" in warnings[0]


def test_attach_warnings():
    assert attach_warnings({"a": 1}, ["w"]) == {"a": 1, "preflight_warnings": ["w"]}
    # 空列表不挂键 — 前端无感
    assert "preflight_warnings" not in attach_warnings({"a": 1}, [])
    # 非 dict 结果原样返回
    assert attach_warnings(None, ["w"]) is None


# ------------------------------------------------------------------
# 6. API 入口接线
# ------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = FakeRepo(earliest=date(2015, 1, 1), symbols=("000001", "600519"))
    return TestClient(app, raise_server_exceptions=False)


def test_run_rejects_reversed_dates(client):
    resp = client.post("/api/backtest/run", json={
        "symbols": ["000001"], "start": "2024-05-01", "end": "2024-01-01",
    })
    assert resp.status_code == 400, resp.text
    assert "回测计划未通过检查" in resp.json()["detail"]


def test_run_rejects_missing_symbol(client):
    resp = client.post("/api/backtest/run", json={
        "symbols": ["000001", "999999"], "start": "2024-01-01", "end": "2024-06-01",
    })
    assert resp.status_code == 400, resp.text
    assert "999999" in resp.json()["detail"]


def test_strategy_run_rejects_absurd_fees(client):
    resp = client.post("/api/backtest/strategy/run", json={
        "strategy_id": "ma_cross", "fees_pct": 2.5,
        "start": "2024-01-01", "end": "2024-06-01",
    })
    assert resp.status_code == 400, resp.text
    assert "fees_pct" in resp.json()["detail"]


def test_strategy_stream_rejects_bad_plan_before_job(client):
    """SSE 入口在建立事件流之前就以 400 拒绝 (前端 fetch 预检可直接展示 detail)。"""
    resp = client.get("/api/backtest/strategy/stream", params={
        "strategy_id": "ma_cross", "start": "2024-05-01", "end": "2024-01-01",
    })
    assert resp.status_code == 400, resp.text
    assert "回测计划未通过检查" in resp.json()["detail"]


def test_optimize_robust_stream_rejects_bad_plan(client):
    resp = client.get("/api/backtest/optimize-robust/stream", params={
        "strategy_id": "ma_cross", "param_grid": '{"n": [5]}',
        "start": "2024-01-01", "end": "2024-06-01", "initial_capital": -1,
    })
    assert resp.status_code == 400, resp.text
    assert "initial_capital" in resp.json()["detail"]


def test_walkforward_stream_rejects_bad_plan(client):
    resp = client.get("/api/backtest/walkforward/stream", params={
        "strategy_id": "ma_cross", "param_grid": '{"n": [5]}',
        "start": "2024-05-01", "end": "2024-01-01",
    })
    assert resp.status_code == 400, resp.text
    assert "回测计划未通过检查" in resp.json()["detail"]


def test_factor_batch_rejects_bad_plan(client, monkeypatch):
    # 绕过因子白名单 (那是 factor 层自己的守卫, 不属于检查员职责)
    import app.factors.registry as registry

    monkeypatch.setattr(
        registry, "factor_columns_view", lambda: [{"id": "mom_20d"}],
    )
    resp = client.post("/api/backtest/factor/batch", json={
        "factor_names": ["mom_20d"], "start": "2024-05-01", "end": "2024-01-01",
    })
    assert resp.status_code == 400, resp.text
    assert "回测计划未通过检查" in resp.json()["detail"]


def test_run_attaches_warnings_to_result(client, monkeypatch):
    """合法但区间过短的计划: 放行, 且 warning 挂在结果 preflight_warnings 上。"""
    import app.api.backtest as backtest_api

    class FakeSvc:
        def __init__(self, repo):
            pass

        def run(self, cfg):
            return {"metrics": {"total_return": 0.01}}

    monkeypatch.setattr(backtest_api, "BacktestService", FakeSvc)
    monkeypatch.setattr(backtest_api, "_daily_data_revisions", lambda: {})
    resp = client.post("/api/backtest/run", json={
        "symbols": ["000001"], "start": "2024-01-01", "end": "2024-01-20",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("preflight_warnings"), body
    assert any("样本太少" in w for w in body["preflight_warnings"])


def test_run_no_warnings_key_when_plan_healthy(client, monkeypatch):
    import app.api.backtest as backtest_api

    class FakeSvc:
        def __init__(self, repo):
            pass

        def run(self, cfg):
            return {"metrics": {}}

    monkeypatch.setattr(backtest_api, "BacktestService", FakeSvc)
    monkeypatch.setattr(backtest_api, "_daily_data_revisions", lambda: {})
    end = date.today()
    resp = client.post("/api/backtest/run", json={
        "symbols": ["000001"],
        "start": (end - timedelta(days=365)).isoformat(),
        "end": end.isoformat(),
    })
    assert resp.status_code == 200, resp.text
    assert "preflight_warnings" not in resp.json()


def test_strategy_run_attaches_warnings_to_result(client, monkeypatch):
    """/strategy/run 走 worker 路径, 同样要把 warning 挂到 preflight_warnings。

    合并 main 时这一行曾被 main 的 `return _stamp_daily_data(...)` 覆盖
    (pf_warnings 算了却不用), 用本测试锁住。
    """
    import app.api.backtest as backtest_api
    import app.backtest.worker as worker

    monkeypatch.setattr(backtest_api, "admit_request", lambda *_a, **_k: None)
    monkeypatch.setattr(backtest_api, "_daily_data_revisions", lambda: {})
    monkeypatch.setattr(worker, "make_worker_task", lambda *a, **k: {"kind": "backtest"})
    monkeypatch.setattr(worker, "run_worker_task", lambda task: {"metrics": {}})

    resp = client.post("/api/backtest/strategy/run", json={
        "strategy_id": "ma_cross", "start": "2024-01-01", "end": "2024-01-20",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("preflight_warnings"), body
    assert any("样本太少" in w for w in body["preflight_warnings"])
