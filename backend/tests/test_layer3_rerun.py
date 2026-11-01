"""P1-12 layer3: 批量任务级备源重跑 — 清单/路由/执行/接口 测试。

覆盖:
  1. rerun_manifest 清单读写 (记录/并集/清除/截断/损坏容错)
  2. resolve_backup 备源解析 (baostock 优先, 排除当前主源)
  3. sync_and_persist_daily_batch / sync_adj_factor 的 provider_override
     原子路由 (不回退 tickflow, 未声明数据集直接报错) + 自定义源异常上浮 failed_out
  4. run_backup_rerun 执行语义 (清旧标记/只用备源/仍失败重新记回)
  5. daily_pipeline._mark_rerun_failures 记录钩子 (含写清单异常不反噬主流程)
  6. API 端点 (GET 清单 / POST 重跑参数校验 / POST 忽略)
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import rerun_manifest as rm
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


CAPSET = SimpleNamespace(has=lambda cap: True, limits=lambda cap: None)


# ---------------------------------------------------------------------------
# 1. 清单读写
# ---------------------------------------------------------------------------

class TestManifest:
    def test_record_and_load(self, data_dir):
        rm.record_failures(data_dir, "daily", ["600519.SH", "000001.SZ"], "tickflow")
        m = rm.load(data_dir)
        entry = m["daily"]
        assert entry["symbols"] == ["600519.SH", "000001.SZ"]
        assert entry["source"] == "tickflow"
        assert entry["recorded_at"]

    def test_record_union_dedup(self, data_dir):
        rm.record_failures(data_dir, "daily", ["A", "B"], "tickflow")
        rm.record_failures(data_dir, "daily", ["B", "C"], "fuyao")
        entry = rm.load(data_dir)["daily"]
        assert entry["symbols"] == ["A", "B", "C"]  # 并集去重保序
        assert entry["source"] == "fuyao"            # 最近一次失败的主源

    def test_record_ignores_unknown_dataset_and_empty(self, data_dir):
        rm.record_failures(data_dir, "minute", ["A"], "x")
        rm.record_failures(data_dir, "daily", [], "x")
        assert rm.load(data_dir) == {}

    def test_record_truncates_at_cap(self, data_dir, monkeypatch):
        monkeypatch.setattr(rm, "MAX_SYMBOLS", 5)
        rm.record_failures(data_dir, "daily", [f"S{i}" for i in range(10)], "t")
        assert len(rm.load(data_dir)["daily"]["symbols"]) == 5

    def test_clear_whole_dataset(self, data_dir):
        rm.record_failures(data_dir, "daily", ["A"], "t")
        rm.record_failures(data_dir, "adj_factor", ["B"], "t")
        rm.clear(data_dir, "daily")
        m = rm.load(data_dir)
        assert "daily" not in m and "adj_factor" in m

    def test_clear_subset(self, data_dir):
        rm.record_failures(data_dir, "daily", ["A", "B", "C"], "t")
        rm.clear(data_dir, "daily", ["B"])
        assert rm.load(data_dir)["daily"]["symbols"] == ["A", "C"]
        rm.clear(data_dir, "daily", ["A", "C"])
        assert "daily" not in rm.load(data_dir)  # 清空后整条移除

    def test_corrupt_file_loads_empty(self, data_dir):
        (data_dir / "rerun_manifest.json").write_text("{broken", encoding="utf-8")
        assert rm.load(data_dir) == {}

    def test_persisted_as_json_file(self, data_dir):
        rm.record_failures(data_dir, "daily", ["A"], "t")
        raw = json.loads((data_dir / "rerun_manifest.json").read_text(encoding="utf-8"))
        assert raw["daily"]["symbols"] == ["A"]


# ---------------------------------------------------------------------------
# 2. 备源解析
# ---------------------------------------------------------------------------

class TestResolveBackup:
    def test_prefers_baostock_and_excludes_primary(self, monkeypatch):
        from app.services import preferences
        monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")
        captured = {}

        def fake_finder(dataset, exclude=None, prefer=None):
            captured.update(dataset=dataset, exclude=exclude, prefer=prefer)
            return "baostock"

        monkeypatch.setattr(
            "app.data_providers.custom.find_complement_provider", fake_finder)
        assert rm.resolve_backup("daily") == "baostock"
        assert captured == {"dataset": "daily", "exclude": "tickflow",
                            "prefer": ("baostock",)}

    def test_adj_factor_uses_adj_primary(self, monkeypatch):
        from app.services import preferences
        monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "fuyao")
        captured = {}

        def fake_finder(dataset, exclude=None, prefer=None):
            captured.update(exclude=exclude)
            return "baostock"

        monkeypatch.setattr(
            "app.data_providers.custom.find_complement_provider", fake_finder)
        assert rm.resolve_backup("adj_factor") == "baostock"
        assert captured["exclude"] == "fuyao"

    def test_unknown_dataset_none(self):
        assert rm.resolve_backup("minute") is None


# ---------------------------------------------------------------------------
# 3. provider_override 原子路由 + 自定义源异常上浮
# ---------------------------------------------------------------------------

def _patch_custom(monkeypatch, provider, has_dataset=True):
    """把 custom 源解析三件套 patch 掉, 返回调用记录。"""
    calls = {"get_daily": [], "get_adj": []}
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset", lambda name, ds: has_dataset)
    monkeypatch.setattr(
        "app.data_providers.custom.get_provider", lambda name: provider)
    return calls


class TestProviderOverride:
    def test_daily_override_skips_prefs_and_uses_given_source(self, monkeypatch, repo):
        from app.services import preferences
        # 偏好解析若被调用直接炸 — 证明 override 完全绕过偏好/互补路由
        monkeypatch.setattr(
            preferences, "get_daily_data_provider",
            lambda: pytest.fail("override 不应读偏好"))
        got = {}

        class FakeProvider:
            def get_daily(self, symbols, **kw):
                got.update(symbols=symbols, kw=kw)
                return pl.DataFrame()

        _patch_custom(monkeypatch, FakeProvider())
        from app.services import kline_sync
        written = kline_sync.sync_and_persist_daily_batch(
            ["600519.SH"], repo, CAPSET, provider_override="baostock")
        assert written == 0
        assert got["symbols"] == ["600519.SH"]

    def test_daily_override_unavailable_dataset_raises(self, monkeypatch, repo):
        _patch_custom(monkeypatch, SimpleNamespace(), has_dataset=False)
        from app.services import kline_sync
        with pytest.raises(ValueError, match="未声明 daily"):
            kline_sync.sync_and_persist_daily_batch(
                ["600519.SH"], repo, CAPSET, provider_override="baostock")

    def test_daily_custom_exception_surfaces_failed_out(self, monkeypatch, repo):
        class BoomProvider:
            def get_daily(self, symbols, **kw):
                raise RuntimeError("网络炸了")

        _patch_custom(monkeypatch, BoomProvider())
        from app.services import kline_sync
        failed: list[str] = []
        written = kline_sync.sync_and_persist_daily_batch(
            ["600519.SH", "000001.SZ"], repo, CAPSET,
            provider_override="baostock", failed_out=failed)
        # 整体异常 → 软失败: 全量记 failed_out, 返回 0 行, 不向上抛
        assert written == 0
        assert failed == ["600519.SH", "000001.SZ"]

    def test_adj_override_unavailable_dataset_raises(self, monkeypatch, repo):
        _patch_custom(monkeypatch, SimpleNamespace(), has_dataset=False)
        from app.services import kline_sync
        with pytest.raises(ValueError, match="未声明 adj_factor"):
            kline_sync.sync_adj_factor(
                ["600519.SH"], repo, CAPSET, provider_override="baostock")

    def test_adj_custom_exception_surfaces_failed_out(self, monkeypatch, repo):
        class BoomProvider:
            def get_adj_factors(self, symbols, **kw):
                raise RuntimeError("网络炸了")

        _patch_custom(monkeypatch, BoomProvider())
        from app.services import kline_sync
        failed: list[str] = []
        added, affected = kline_sync.sync_adj_factor(
            ["600519.SH"], repo, CAPSET,
            provider_override="baostock", failed_out=failed)
        assert (added, affected) == (0, [])
        assert failed == ["600519.SH"]


# ---------------------------------------------------------------------------
# 4. run_backup_rerun 执行语义
# ---------------------------------------------------------------------------

class TestRunBackupRerun:
    def test_daily_rerun_uses_backup_and_clears_manifest(self, monkeypatch, repo):
        rm.record_failures(repo.store.data_dir, "daily", ["A", "B"], "tickflow")
        monkeypatch.setattr(rm, "resolve_backup", lambda ds: "baostock")
        captured = {}

        def fake_sync(symbols, repo_, capset_, **kw):
            captured.update(symbols=symbols, override=kw.get("provider_override"),
                            start=kw.get("start_date"), end=kw.get("end_date"))
            return 42

        monkeypatch.setattr(
            "app.services.kline_sync.sync_and_persist_daily_batch", fake_sync)
        result = rm.run_backup_rerun(repo, CAPSET, "daily")
        assert result == {"dataset": "daily", "backup": "baostock", "requested": 2,
                          "written_rows": 42, "still_failed": 0}
        assert captured["symbols"] == ["A", "B"]
        assert captured["override"] == "baostock"  # 原子性: 全程只用备源
        # 窗口 ≈ 30 天
        assert (captured["end"] - captured["start"]).days == rm.RERUN_WINDOW_DAYS["daily"]
        assert rm.load(repo.store.data_dir) == {}  # 成功后清单清空

    def test_still_failed_symbols_re_recorded_with_backup_source(self, monkeypatch, repo):
        rm.record_failures(repo.store.data_dir, "daily", ["A", "B"], "tickflow")
        monkeypatch.setattr(rm, "resolve_backup", lambda ds: "baostock")

        def fake_sync(symbols, repo_, capset_, **kw):
            kw["failed_out"].append("B")  # 备源也救不回 B
            return 10

        monkeypatch.setattr(
            "app.services.kline_sync.sync_and_persist_daily_batch", fake_sync)
        result = rm.run_backup_rerun(repo, CAPSET, "daily")
        assert result["still_failed"] == 1
        entry = rm.load(repo.store.data_dir)["daily"]
        assert entry["symbols"] == ["B"]
        assert entry["source"] == "baostock"  # 如实记录本轮用的源

    def test_adj_rerun_refreshes_view_when_affected(self, monkeypatch, repo):
        rm.record_failures(repo.store.data_dir, "adj_factor", ["A"], "tickflow")
        monkeypatch.setattr(rm, "resolve_backup", lambda ds: "baostock")
        refreshed = []
        monkeypatch.setattr(
            "app.services.kline_sync.sync_adj_factor",
            lambda symbols, repo_, capset_, **kw: (3, ["A"]))
        monkeypatch.setattr(
            "app.jobs.daily_pipeline._refresh_single_view",
            lambda repo_, name: refreshed.append(name))
        result = rm.run_backup_rerun(repo, CAPSET, "adj_factor")
        assert result["written_rows"] == 3 and result["affected"] == 1
        assert refreshed == ["adj_factor"]

    def test_empty_manifest_returns_error(self, repo):
        result = rm.run_backup_rerun(repo, CAPSET, "daily")
        assert "error" in result

    def test_no_backup_returns_error(self, monkeypatch, repo):
        rm.record_failures(repo.store.data_dir, "daily", ["A"], "tickflow")
        monkeypatch.setattr(rm, "resolve_backup", lambda ds: None)
        result = rm.run_backup_rerun(repo, CAPSET, "daily")
        assert "无可用备源" in result["error"]
        # 未执行重跑 → 清单原样保留
        assert rm.load(repo.store.data_dir)["daily"]["symbols"] == ["A"]

    def test_unknown_dataset_returns_error(self, repo):
        assert "error" in rm.run_backup_rerun(repo, CAPSET, "minute")


# ---------------------------------------------------------------------------
# 5. daily_pipeline 记录钩子
# ---------------------------------------------------------------------------

class TestMarkRerunFailures:
    def test_records_with_configured_primary(self, monkeypatch, tmp_path):
        from app.jobs import daily_pipeline as dp
        fake_repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
        monkeypatch.setattr(dp._prefs, "get_daily_data_provider", lambda: "fuyao")
        dp._mark_rerun_failures(fake_repo, "daily", ["A"])
        entry = rm.load(tmp_path)["daily"]
        assert entry["symbols"] == ["A"] and entry["source"] == "fuyao"

    def test_adj_uses_adj_pref(self, monkeypatch, tmp_path):
        from app.jobs import daily_pipeline as dp
        fake_repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
        monkeypatch.setattr(dp._prefs, "get_adj_factor_provider", lambda: "tickflow")
        dp._mark_rerun_failures(fake_repo, "adj_factor", ["B"])
        assert rm.load(tmp_path)["adj_factor"]["source"] == "tickflow"

    def test_empty_symbols_noop(self, tmp_path):
        from app.jobs import daily_pipeline as dp
        fake_repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
        dp._mark_rerun_failures(fake_repo, "daily", [])
        assert rm.load(tmp_path) == {}

    def test_manifest_write_error_does_not_raise(self, monkeypatch, tmp_path):
        from app.jobs import daily_pipeline as dp

        def boom(*a, **k):
            raise OSError("磁盘炸了")

        monkeypatch.setattr(rm, "record_failures", boom)
        fake_repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
        dp._mark_rerun_failures(fake_repo, "daily", ["A"])  # 不抛 = 通过


# ---------------------------------------------------------------------------
# 6. API 端点
# ---------------------------------------------------------------------------

class FakeRequest:
    """最小 Request 替身: 只支持 await request.json() 与 app.state。"""

    def __init__(self, repo, body=None):
        self.app = SimpleNamespace(
            state=SimpleNamespace(repo=repo, capabilities=CAPSET))
        self._body = body or {}

    async def json(self):
        return self._body


class TestEndpoints:
    def test_get_manifest_shape(self, monkeypatch, repo):
        from app.api.kline import get_rerun_manifest
        rm.record_failures(repo.store.data_dir, "daily", ["A", "B"], "tickflow")
        monkeypatch.setattr(rm, "resolve_backup", lambda ds: "baostock")
        resp = asyncio.run(get_rerun_manifest(FakeRequest(repo)))
        entry = resp["datasets"]["daily"]
        assert entry["count"] == 2
        assert entry["symbols_sample"] == ["A", "B"]
        assert entry["source"] == "tickflow"
        assert entry["backup"] == "baostock"
        assert "adj_factor" not in resp["datasets"]

    def test_get_manifest_empty(self, repo):
        from app.api.kline import get_rerun_manifest
        resp = asyncio.run(get_rerun_manifest(FakeRequest(repo)))
        assert resp == {"datasets": {}}

    def test_rerun_rejects_bad_dataset(self, repo):
        from fastapi import HTTPException

        from app.api.kline import rerun_with_backup
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rerun_with_backup(
                FakeRequest(repo, {"dataset": "minute"})))
        assert ei.value.status_code == 400

    def test_rerun_rejects_empty_manifest(self, repo):
        from fastapi import HTTPException

        from app.api.kline import rerun_with_backup
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rerun_with_backup(FakeRequest(repo, {"dataset": "daily"})))
        assert ei.value.status_code == 400

    def test_rerun_409_when_no_backup(self, monkeypatch, repo):
        from fastapi import HTTPException

        from app.api.kline import rerun_with_backup
        rm.record_failures(repo.store.data_dir, "daily", ["A"], "tickflow")
        monkeypatch.setattr(rm, "resolve_backup", lambda ds: None)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rerun_with_backup(FakeRequest(repo, {"dataset": "daily"})))
        assert ei.value.status_code == 409

    def test_clear_endpoint(self, repo):
        from app.api.kline import clear_rerun_manifest
        rm.record_failures(repo.store.data_dir, "daily", ["A"], "tickflow")
        resp = asyncio.run(clear_rerun_manifest(FakeRequest(repo, {"dataset": "daily"})))
        assert resp == {"status": "cleared", "dataset": "daily"}
        assert rm.load(repo.store.data_dir) == {}

    def test_clear_rejects_bad_dataset(self, repo):
        from fastapi import HTTPException

        from app.api.kline import clear_rerun_manifest
        with pytest.raises(HTTPException) as ei:
            asyncio.run(clear_rerun_manifest(FakeRequest(repo, {"dataset": "x"})))
        assert ei.value.status_code == 400
