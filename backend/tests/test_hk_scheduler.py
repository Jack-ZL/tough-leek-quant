"""港股定时同步 job 测试 (app.jobs.hk_pipeline)。

覆盖三件事, 按重要性排序:

1. **调度隔离** —— 港股 job 不进 ``job_store``、不占执行槽, 且**单向让路**。
   这是方案 §14「港股失败不能危及 A 股关键路径」的落地, 也是最容易被后人
   "顺手复用 _run_tracked" 破坏的一条, 因此单独钉死。
2. **fail-closed 闸门** —— 复权因子部分失败时必须跳过 enriched, 否则失败标的
   会被 ``fill_null(1.0)`` 当成「从无分红」, 前复权静默失真。
3. **阶段依赖与资源清理** —— 维表软失败继续 / 空维表硬中止 / 日K失败中止后续 /
   client 必定 close / 视图必定重建。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.base import ConflictingIdError
from apscheduler.triggers.cron import CronTrigger

from app.jobs import hk_pipeline
from app.services import preferences

# ============================================================
# 测试替身
# ============================================================


class _FakeStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class _FakeRepo:
    """只需 ``store.data_dir`` 与 ``rebuild_views()``, 与真实 repository 同接口。"""

    def __init__(self, data_dir: Path) -> None:
        self.store = _FakeStore(data_dir)
        self.rebuild_views_calls = 0

    def rebuild_views(self) -> None:
        self.rebuild_views_calls += 1


class _FakeClient:
    """记录 close 调用; 类本身记录实例化次数。"""

    instances: ClassVar[list[_FakeClient]] = []

    def __init__(self) -> None:
        self.closed = False
        type(self).instances.append(self)

    def close(self) -> None:
        self.closed = True


class _RecordingScheduler:
    """记录 add_job/remove_job 调用, 不启事件循环。

    ⚠️ 必须复刻 APScheduler 的 **id 冲突语义**: 同 id 重复注册且未带
    ``replace_existing=True`` 时抛 ``ConflictingIdError``。若这里简单地覆盖 dict,
    "丢掉 replace_existing" 这个变异体就杀不掉 —— 而真实后果是 settings 改时间时
    第二次注册直接报错, 港股 job 停在旧时刻。
    """

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.removed: list[str] = []

    def add_job(self, func, **kwargs) -> None:
        job_id = kwargs["id"]
        if job_id in self.jobs and not kwargs.get("replace_existing", False):
            raise ConflictingIdError(
                f"Job with id '{job_id}' already exists and replace_existing is False"
            )
        self.jobs[job_id] = {"func": func, **kwargs}

    def remove_job(self, job_id: str) -> None:
        if job_id not in self.jobs:
            raise JobLookupError(job_id)  # 与 APScheduler 行为对齐
        del self.jobs[job_id]
        self.removed.append(job_id)


class _FakeJobStore:
    def __init__(self, active: str | None = None) -> None:
        self._active = active
        self.created = 0

    def active_id(self) -> str | None:
        return self._active

    def create(self, *a, **kw):  # pragma: no cover - 港股路径绝不该调用
        self.created += 1
        raise AssertionError("港股 job 不得调用 job_store.create()")


class _RaisingJobStore:
    def active_id(self) -> str | None:
        raise RuntimeError("job store 炸了")


@pytest.fixture
def fake_client(monkeypatch):
    _FakeClient.instances = []
    import app.plugins.eastmoney.client as em_client

    monkeypatch.setattr(em_client, "EastmoneyClient", _FakeClient)
    return _FakeClient


@pytest.fixture
def stub_services(monkeypatch):
    """把港股同步各步全部换成可编排的桩, 返回调用记录 + 行为控制开关。"""
    from app.services import index_sync_hk, instrument_sync_hk, kline_sync_hk

    calls: dict[str, Any] = {
        "order": [],
        "instruments": 0,
        "universe": ["00700", "00005"],
        "daily_written": 10,
        "adj_rows": 3,
        "adj_failed": [],
        "enriched_written": 10,
        "index_written": 7,
        "index_failed": [],
        "raise": {},
        "kwargs": {},
    }

    def _maybe_raise(stage: str) -> None:
        exc = calls["raise"].get(stage)
        if exc is not None:
            raise exc

    def fake_instruments(data_dir, **kw):
        calls["order"].append("instruments")
        _maybe_raise("instruments")
        return calls["instruments"]

    def fake_universe(data_dir, *, include_cny_counter=False):
        calls["kwargs"]["universe_cny"] = include_cny_counter
        return list(calls["universe"])

    def fake_daily(repo, **kw):
        calls["order"].append("daily")
        calls["kwargs"]["daily"] = kw
        _maybe_raise("daily")
        return calls["daily_written"]

    def fake_adj(repo, **kw):
        calls["order"].append("adj_factor")
        calls["kwargs"]["adj"] = kw
        _maybe_raise("adj_factor")
        failed_out = kw.get("failed_out")
        if failed_out is not None:
            failed_out.extend(calls["adj_failed"])
        return calls["adj_rows"], []

    def fake_enriched(repo, **kw):
        calls["order"].append("enriched")
        calls["kwargs"]["enriched"] = kw
        _maybe_raise("enriched")
        return calls["enriched_written"]

    def fake_index_daily(repo, **kw):
        calls["order"].append("index_daily")
        calls["kwargs"]["index_daily"] = kw
        _maybe_raise("index_daily")
        return calls["index_written"], list(calls["index_failed"])

    monkeypatch.setattr(instrument_sync_hk, "sync_instruments_hk", fake_instruments)
    monkeypatch.setattr(kline_sync_hk, "hk_daily_universe", fake_universe)
    monkeypatch.setattr(kline_sync_hk, "sync_hk_daily", fake_daily)
    monkeypatch.setattr(kline_sync_hk, "sync_hk_adj_factor", fake_adj)
    monkeypatch.setattr(kline_sync_hk, "sync_hk_enriched", fake_enriched)
    monkeypatch.setattr(index_sync_hk, "sync_hk_index_daily", fake_index_daily)
    return calls


@pytest.fixture(autouse=True)
def _isolated_prefs(tmp_path, monkeypatch):
    """preferences 落到 tmp, 避免污染真实 user_data/preferences.json。"""
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    yield path
    preferences._invalidate_cache()


@pytest.fixture
def no_active_job(monkeypatch):
    import app.services.pipeline_jobs as pj

    store = _FakeJobStore(active=None)
    monkeypatch.setattr(pj, "job_store", store)
    return store


# ============================================================
# 1. preferences: get/set_hk_sync_schedule
# ============================================================


def test_hk_schedule_defaults_are_disabled():
    """默认必须关闭 —— 港股是增量功能且维表口径待复测, 不该默认每天拉 2900 标的。"""
    assert preferences.get_hk_sync_schedule() == {
        "enabled": False, "hour": 16, "minute": 40, "include_cny_counter": False,
    }


def test_hk_schedule_roundtrip():
    saved = preferences.set_hk_sync_schedule(True, 17, 5, True)
    assert saved == {
        "enabled": True, "hour": 17, "minute": 5, "include_cny_counter": True,
    }
    assert preferences.get_hk_sync_schedule() == saved


def test_hk_schedule_clamps_out_of_range():
    assert preferences.set_hk_sync_schedule(True, 99, -5)["hour"] == 23
    assert preferences.set_hk_sync_schedule(True, 99, -5)["minute"] == 0
    assert preferences.set_hk_sync_schedule(True, -3, 77)["hour"] == 0
    assert preferences.set_hk_sync_schedule(True, -3, 77)["minute"] == 59


def test_hk_schedule_has_no_time_floor_unlike_review():
    """与 set_review_schedule 的刻意差异: 港股有半日市(12:00 收盘), 不设 15:00 下限。

    这条断言钉住"不要照抄复盘的下限逻辑"—— 若有人给港股也加下限, 半日市/盘中
    快照的用法就被堵死了。
    """
    assert preferences.set_hk_sync_schedule(True, 9, 30) == {
        "enabled": True, "hour": 9, "minute": 30, "include_cny_counter": False,
    }
    # 对照: 复盘同样输入会被抬到 15:00
    assert preferences.set_review_schedule(True, 9, 30)["hour"] == 15


def test_hk_schedule_disabled_still_persists_time():
    """关闭时时间仍保存, 下次开启可沿用 (与 review_schedule 同语义)。"""
    preferences.set_hk_sync_schedule(True, 18, 20)
    preferences.set_hk_sync_schedule(False, 18, 20)
    d = preferences.get_hk_sync_schedule()
    assert d["enabled"] is False
    assert (d["hour"], d["minute"]) == (18, 20)


def test_hk_schedule_tolerates_garbage_stored_value(_isolated_prefs):
    """存了非 dict 的垃圾值时回退默认, 不抛 (历史配置损坏不能拖垮启动)。"""
    _isolated_prefs.write_text(
        json.dumps({"hk_sync_schedule": "不是字典"}), encoding="utf-8"
    )
    preferences._invalidate_cache()
    assert preferences.get_hk_sync_schedule()["enabled"] is False
    assert preferences.get_hk_sync_schedule()["hour"] == 16


def test_hk_schedule_partial_dict_fills_missing_fields(_isolated_prefs):
    """老版本只存了 enabled/hour 时, 缺失字段补默认而非 KeyError。"""
    _isolated_prefs.write_text(
        json.dumps({"hk_sync_schedule": {"enabled": True, "hour": 20}}), encoding="utf-8"
    )
    preferences._invalidate_cache()
    d = preferences.get_hk_sync_schedule()
    assert d == {
        "enabled": True, "hour": 20, "minute": 40, "include_cny_counter": False,
    }


# ============================================================
# 2. 调度隔离: 单向让路 (核心保证)
# ============================================================


def test_yield_returns_none_when_no_active_job(no_active_job):
    assert hk_pipeline._yield_to_ashare() is None


def test_yield_returns_reason_when_ashare_job_active(monkeypatch):
    import app.services.pipeline_jobs as pj

    monkeypatch.setattr(pj, "job_store", _FakeJobStore(active="job-abc"))
    reason = hk_pipeline._yield_to_ashare()
    assert reason is not None
    assert "job-abc" in reason


def test_yield_is_conservative_when_job_store_raises(monkeypatch):
    """让路检查自身出错时必须**保守跳过**, 不能因为查不到状态就并发抢跑。"""
    import app.services.pipeline_jobs as pj

    monkeypatch.setattr(pj, "job_store", _RaisingJobStore())
    reason = hk_pipeline._yield_to_ashare()
    assert reason is not None
    assert "保守让路" in reason


def test_hk_task_skips_entirely_when_ashare_active(monkeypatch, stub_services):
    """A 股在跑时港股整轮跳过, run_hk_sync 一次都不进。"""
    import app.services.pipeline_jobs as pj

    monkeypatch.setattr(pj, "job_store", _FakeJobStore(active="ashare-job"))
    monkeypatch.setattr(
        hk_pipeline, "run_hk_sync",
        lambda *a, **kw: pytest.fail("A 股活跃时不得执行港股同步"),
    )
    assert hk_pipeline.hk_sync_task(_FakeRepo(Path("/tmp/x"))) is None
    assert stub_services["order"] == []


def test_hk_task_never_touches_job_store_or_run_slot(monkeypatch, no_active_job,
                                                    stub_services, fake_client):
    """🔴 港股绝不能进 job_store / 抢执行槽 —— 否则 A 股盘后管道会被单飞判定跳过。

    这是"港股不危及 A 股关键路径"的实现核心: 隔离靠的是**不留痕迹**, 不是靠错开时间。
    """
    import app.services.pipeline_jobs as pj

    slot_calls: list[str] = []
    monkeypatch.setattr(
        pj, "try_acquire_run_slot",
        lambda owner="": slot_calls.append(owner) or True,
    )
    monkeypatch.setattr(
        pj, "release_run_slot",
        lambda owner=None: slot_calls.append(f"release:{owner}"),
    )

    hk_pipeline.hk_sync_task(_FakeRepo(Path("/tmp/x")))

    assert no_active_job.created == 0, "港股不得调用 job_store.create()"
    assert slot_calls == [], f"港股不得占用重任务执行槽, 实际: {slot_calls}"


def test_hk_task_swallows_unexpected_exception(monkeypatch, no_active_job, stub_services):
    """港股任何未预期异常都只记日志, 绝不抛回 APScheduler。"""
    def boom(*a, **kw):
        raise RuntimeError("港股炸了")

    monkeypatch.setattr(hk_pipeline, "run_hk_sync", boom)
    result = hk_pipeline.hk_sync_task(_FakeRepo(Path("/tmp/x")))  # 不抛
    # 🔴 个股阶段炸了, 指数阶段**仍须执行**: 两条链路源不同(东财 vs 腾讯)、表不同、
    # 失败语义不同, 把指数挂在个股后面会让东财被封时指数页一起静默变陈旧。
    assert "index_daily" in stub_services["order"]
    assert result is not None
    assert result["index_daily_written"] == stub_services["index_written"]


def test_hk_task_returns_none_only_when_both_stages_raise(monkeypatch, no_active_job):
    """个股与指数**都**炸时才返回 None —— 且仍然不抛。"""
    def boom(*a, **kw):
        raise RuntimeError("炸了")

    monkeypatch.setattr(hk_pipeline, "run_hk_sync", boom)
    monkeypatch.setattr(hk_pipeline, "run_hk_index_sync", boom)
    assert hk_pipeline.hk_sync_task(_FakeRepo(Path("/tmp/x"))) is None


def test_hk_task_merges_stock_and_index_results(monkeypatch, no_active_job, stub_services,
                                                fake_client):
    """返回 dict 同时含个股与指数计数(供日志/测试断言, 调度路径不消费)。"""
    result = hk_pipeline.hk_sync_task(_FakeRepo(Path("/tmp/x")))
    assert result is not None
    assert result["daily_written"] == stub_services["daily_written"]
    assert result["enriched_written"] == stub_services["enriched_written"]
    assert result["index_daily_written"] == stub_services["index_written"]
    assert result["index_failed"] == []
    # 个股先、指数后: 指数不依赖个股产物, 但顺序固定便于日志比对
    order = stub_services["order"]
    assert order.index("index_daily") == len(order) - 1


def test_hk_task_reads_include_cny_counter_from_prefs(monkeypatch, no_active_job,
                                                     stub_services, fake_client):
    preferences.set_hk_sync_schedule(True, 16, 40, True)
    seen: dict[str, Any] = {}

    def spy(repo, **kw):
        seen.update(kw)
        return {}

    monkeypatch.setattr(hk_pipeline, "run_hk_sync", spy)
    hk_pipeline.hk_sync_task(_FakeRepo(Path("/tmp/x")))
    assert seen["include_cny_counter"] is True


# ============================================================
# 2b. run_hk_index_sync: 指数链路与个股链路解耦
# ============================================================


def test_index_sync_runs_even_when_universe_empty(tmp_path, stub_services, fake_client,
                                                  no_active_job):
    """🔴 个股硬中止(维表空)时指数仍同步 —— 指数不依赖个股维表。"""
    stub_services["universe"] = []
    result = hk_pipeline.hk_sync_task(_FakeRepo(tmp_path))
    assert any("维表为空" in e for e in result["errors"]), "个股阶段应已硬中止"
    assert "index_daily" in stub_services["order"]
    assert result["index_daily_written"] == stub_services["index_written"]


def test_index_sync_runs_even_when_adj_factor_incomplete(tmp_path, stub_services,
                                                         fake_client, no_active_job):
    """🔴 复权因子不完整 → 个股 fail-closed 跳过 enriched, 但指数照跑。

    指数不做复权, 不存在「因子不完整 → 指标失真」这条依赖链。
    """
    stub_services["adj_failed"] = ["00700"]
    result = hk_pipeline.hk_sync_task(_FakeRepo(tmp_path))
    assert "enriched(复权因子不完整)" in result["skipped"]
    assert "index_daily" in stub_services["order"]
    assert result["index_daily_written"] == stub_services["index_written"]


def test_index_sync_reports_written_and_failed(tmp_path, stub_services):
    stub_services["index_written"] = 42
    stub_services["index_failed"] = ["HSI", "HSTECH"]
    result = hk_pipeline.run_hk_index_sync(_FakeRepo(tmp_path))
    assert result == {"index_daily_written": 42, "index_failed": ["HSI", "HSTECH"]}


def test_index_sync_swallows_service_exception(tmp_path, stub_services):
    """指数阶段异常只记日志 + 进 errors, 绝不上抛(与个股阶段同款纪律)。"""
    stub_services["raise"]["index_daily"] = RuntimeError("腾讯炸了")
    result = hk_pipeline.run_hk_index_sync(_FakeRepo(tmp_path))
    assert result["index_daily_written"] == 0
    assert result["index_failed"] == []
    assert any("指数" in e for e in result["errors"])


def test_index_sync_passes_throttled_progress(tmp_path, stub_services):
    """on_progress 必须接上节流日志器, 否则 27 个标的的进度无处可查。"""
    hk_pipeline.run_hk_index_sync(_FakeRepo(tmp_path))
    assert callable(stub_services["kwargs"]["index_daily"]["on_progress"])


def test_index_sync_does_not_touch_job_store_or_run_slot(tmp_path, stub_services,
                                                         no_active_job, monkeypatch):
    """指数阶段同样不得进 job_store / 抢执行槽(隔离纪律覆盖整条港股链路)。"""
    import app.services.pipeline_jobs as pj

    slot_calls: list[str] = []
    monkeypatch.setattr(
        pj, "try_acquire_run_slot", lambda owner="": slot_calls.append(owner) or True
    )
    hk_pipeline.run_hk_index_sync(_FakeRepo(tmp_path))
    assert no_active_job.created == 0
    assert slot_calls == []


# ============================================================
# 3. run_hk_sync: fail-closed 闸门
# ============================================================


def test_enriched_skipped_when_adj_factor_partially_failed(
    tmp_path, stub_services, fake_client, no_active_job
):
    """🔴 复权因子有标的失败 → 必须跳过 enriched。

    sync_hk_adj_factor 是逐批 merge 落盘的, 部分失败时因子库**非空但不完整**,
    load_hk_adj_factors 的三态闸门会放行; 失败标的在 _apply_adj_factor 里拿到
    fill_null(1.0), 被当成"从无分红" → 前复权静默偏高。宁可本轮不出 enriched。
    """
    stub_services["adj_failed"] = ["00005", "09988"]
    repo = _FakeRepo(tmp_path)

    result = hk_pipeline.run_hk_sync(repo)

    assert "enriched" not in stub_services["order"]
    assert result["enriched_written"] == 0
    assert result["adj_factor_failed"] == 2
    assert any(s == "enriched(复权因子不完整)" for s in result["skipped"])
    assert any("fail closed" in e for e in result["errors"])


def test_enriched_skipped_when_adj_factor_raises(
    tmp_path, stub_services, fake_client, no_active_job
):
    stub_services["raise"]["adj_factor"] = RuntimeError("datacenter 502")
    result = hk_pipeline.run_hk_sync(_FakeRepo(tmp_path))

    assert "enriched" not in stub_services["order"]
    assert any(s == "enriched(复权因子异常)" for s in result["skipped"])


def test_enriched_runs_when_adj_factor_fully_succeeded(
    tmp_path, stub_services, fake_client, no_active_job
):
    stub_services["adj_failed"] = []
    result = hk_pipeline.run_hk_sync(_FakeRepo(tmp_path))

    assert "enriched" in stub_services["order"]
    assert result["enriched_written"] == 10
    assert result["skipped"] == []


def test_enriched_keeps_require_adj_factor_true(
    tmp_path, stub_services, fake_client, no_active_job
):
    """enriched 必须带 require_adj_factor=True —— 服务层那道闸门不能被 job 关掉。"""
    hk_pipeline.run_hk_sync(_FakeRepo(tmp_path))
    assert stub_services["kwargs"]["enriched"]["require_adj_factor"] is True


# ============================================================
# 4. run_hk_sync: 阶段依赖
# ============================================================


def test_empty_universe_aborts_hard(tmp_path, stub_services, fake_client, no_active_job):
    """维表为空 → 硬中止, 日K/因子/enriched 一步都不跑。"""
    stub_services["universe"] = []
    repo = _FakeRepo(tmp_path)

    result = hk_pipeline.run_hk_sync(repo)

    assert stub_services["order"] == ["instruments"]
    assert result["universe"] == 0
    assert any("维表为空" in e for e in result["errors"])
    assert repo.rebuild_views_calls == 0


def test_instruments_zero_is_soft_failure_and_continues(
    tmp_path, stub_services, fake_client, no_active_job
):
    """维表返回 0 (拉取失败保留旧维表) → 记 warning 但**继续**, 旧维表仍可用。"""
    stub_services["instruments"] = 0
    result = hk_pipeline.run_hk_sync(_FakeRepo(tmp_path))

    assert stub_services["order"] == ["instruments", "daily", "adj_factor", "enriched"]
    assert any("沿用现有维表继续" in e for e in result["errors"])
    assert result["enriched_written"] == 10


def test_instruments_exception_is_soft_failure(
    tmp_path, stub_services, fake_client, no_active_job
):
    stub_services["raise"]["instruments"] = RuntimeError("push2 拒绝连接")
    result = hk_pipeline.run_hk_sync(_FakeRepo(tmp_path))

    assert "daily" in stub_services["order"], "维表异常不该中断整轮"
    assert any("维表同步异常" in e for e in result["errors"])


def test_daily_failure_aborts_enriched(tmp_path, stub_services, fake_client, no_active_job):
    stub_services["raise"]["daily"] = RuntimeError("网络中断")
    repo = _FakeRepo(tmp_path)

    result = hk_pipeline.run_hk_sync(repo)

    assert stub_services["order"] == ["instruments", "daily"]
    assert any(s == "enriched(日K失败)" for s in result["skipped"])
    assert repo.rebuild_views_calls == 0


def test_enriched_failure_recorded_not_raised(
    tmp_path, stub_services, fake_client, no_active_job
):
    stub_services["raise"]["enriched"] = RuntimeError("polars OOM")
    result = hk_pipeline.run_hk_sync(_FakeRepo(tmp_path))

    assert result["enriched_written"] == 0
    assert any("enriched 生成失败" in e for e in result["errors"])


def test_happy_path_runs_all_stages_in_order(
    tmp_path, stub_services, fake_client, no_active_job
):
    stub_services["instruments"] = 2931
    repo = _FakeRepo(tmp_path)

    result = hk_pipeline.run_hk_sync(repo)

    assert stub_services["order"] == [
        "instruments", "daily", "adj_factor", "enriched",
    ]
    assert result["instruments"] == 2931
    assert result["universe"] == 2
    assert result["daily_written"] == 10
    assert result["adj_factor_rows"] == 3
    assert result["enriched_written"] == 10
    assert result["errors"] == []
    assert repo.rebuild_views_calls == 1


def test_rebuild_views_called_even_when_enriched_wrote_zero(
    tmp_path, stub_services, fake_client, no_active_job
):
    """日K写了但 enriched 写 0 行时仍要刷视图。

    sync_hk_enriched 只在 written>0 时刷视图, sync_hk_daily 从不刷 —— 漏了就是
    CONTRIBUTING 明列的拒收情形「写入后存在已知缓存不一致」。
    """
    stub_services["enriched_written"] = 0
    repo = _FakeRepo(tmp_path)

    hk_pipeline.run_hk_sync(repo)

    assert repo.rebuild_views_calls == 1


def test_rebuild_views_failure_is_recorded(
    tmp_path, stub_services, fake_client, no_active_job
):
    repo = _FakeRepo(tmp_path)
    repo.rebuild_views = lambda: (_ for _ in ()).throw(RuntimeError("duckdb 锁"))

    result = hk_pipeline.run_hk_sync(repo)

    assert any("重建视图失败" in e for e in result["errors"])


def test_include_cny_counter_propagates(tmp_path, stub_services, fake_client, no_active_job):
    hk_pipeline.run_hk_sync(_FakeRepo(tmp_path), include_cny_counter=True)
    assert stub_services["kwargs"]["universe_cny"] is True
    assert stub_services["kwargs"]["daily"]["include_cny_counter"] is True


def test_daily_uses_incremental_and_shared_client(
    tmp_path, stub_services, fake_client, no_active_job
):
    """日K必须增量(否则每天全量回填), 且三阶段共用同一个 client 复用连接池。"""
    hk_pipeline.run_hk_sync(_FakeRepo(tmp_path))
    daily_kw = stub_services["kwargs"]["daily"]
    assert daily_kw["incremental"] is True
    assert daily_kw["client"] is stub_services["kwargs"]["adj"]["client"]
    assert len(fake_client.instances) == 1


# ============================================================
# 5. 资源清理
# ============================================================


def test_client_closed_on_success(tmp_path, stub_services, fake_client, no_active_job):
    hk_pipeline.run_hk_sync(_FakeRepo(tmp_path))
    assert len(fake_client.instances) == 1
    assert fake_client.instances[0].closed is True


@pytest.mark.parametrize("stage", ["daily", "adj_factor", "enriched"])
def test_client_closed_on_stage_failure(tmp_path, stub_services, fake_client,
                                       no_active_job, stage):
    """任一阶段抛异常, client 都必须 close (finally 保证, 否则连接泄漏)。"""
    stub_services["raise"][stage] = RuntimeError("boom")
    hk_pipeline.run_hk_sync(_FakeRepo(tmp_path))
    assert fake_client.instances[0].closed is True


def test_client_closed_when_universe_empty(tmp_path, stub_services, fake_client,
                                          no_active_job):
    stub_services["universe"] = []
    hk_pipeline.run_hk_sync(_FakeRepo(tmp_path))
    assert fake_client.instances[0].closed is True


# ============================================================
# 6. job 注册
# ============================================================


def test_register_skipped_when_disabled():
    preferences.set_hk_sync_schedule(False, 16, 40)
    sched = _RecordingScheduler()
    assert hk_pipeline.register_hk_job(sched, _FakeRepo(Path("/tmp/x"))) is False
    assert sched.jobs == {}


def test_register_uses_correct_id_and_cron():
    preferences.set_hk_sync_schedule(True, 17, 25)
    sched = _RecordingScheduler()

    assert hk_pipeline.register_hk_job(sched, _FakeRepo(Path("/tmp/x"))) is True
    job = sched.jobs[hk_pipeline.HK_SYNC_JOB_ID]
    trigger = job["trigger"]

    assert isinstance(trigger, CronTrigger)
    assert str(trigger.fields[trigger.FIELD_NAMES.index("day_of_week")]) == "mon-fri"
    assert str(trigger.fields[trigger.FIELD_NAMES.index("hour")]) == "17"
    assert str(trigger.fields[trigger.FIELD_NAMES.index("minute")]) == "25"
    assert str(trigger.timezone) == "Asia/Hong_Kong"


def test_registered_func_is_not_a_lambda():
    """daily_pipeline:1418 的坑: 传 lambda 会被当同步函数丢线程池, 协程不被 await。

    这里 hk_sync_task 是同步函数, 但仍钉住"传的是函数对象本身", 防后人改坏。
    """
    preferences.set_hk_sync_schedule(True, 16, 40)
    sched = _RecordingScheduler()
    hk_pipeline.register_hk_job(sched, _FakeRepo(Path("/tmp/x")))

    func = sched.jobs[hk_pipeline.HK_SYNC_JOB_ID]["func"]
    assert func is hk_pipeline.hk_sync_task
    assert not isinstance(func, type(lambda: None)) or func.__name__ != "<lambda>"


def test_register_passes_repo_as_arg():
    preferences.set_hk_sync_schedule(True, 16, 40)
    sched = _RecordingScheduler()
    repo = _FakeRepo(Path("/tmp/x"))
    hk_pipeline.register_hk_job(sched, repo)
    assert sched.jobs[hk_pipeline.HK_SYNC_JOB_ID]["args"] == [repo]


def test_reregister_replaces_instead_of_duplicating():
    """replace_existing=True: settings 改时间时重复注册只更新 trigger。

    丢掉 replace_existing 的真实后果是第二次注册抛 ConflictingIdError, 港股 job
    停在旧时刻 —— 下面的 _RecordingScheduler 复刻了这个语义, 所以本断言有牙齿。
    """
    sched = _RecordingScheduler()
    preferences.set_hk_sync_schedule(True, 16, 40)
    hk_pipeline.register_hk_job(sched, _FakeRepo(Path("/tmp/x")))
    preferences.set_hk_sync_schedule(True, 18, 0)
    hk_pipeline.register_hk_job(sched, _FakeRepo(Path("/tmp/x")))

    assert len(sched.jobs) == 1
    job = sched.jobs[hk_pipeline.HK_SYNC_JOB_ID]
    assert job["replace_existing"] is True
    trigger = job["trigger"]
    assert str(trigger.fields[trigger.FIELD_NAMES.index("hour")]) == "18"


def test_recording_scheduler_emulates_conflicting_id_error():
    """守卫测试替身本身: 假调度器若不复刻 id 冲突语义, 上面那条断言就是摆设。"""
    sched = _RecordingScheduler()
    sched.add_job(lambda: None, id="dup")
    with pytest.raises(ConflictingIdError):
        sched.add_job(lambda: None, id="dup")
    sched.add_job(lambda: None, id="dup", replace_existing=True)  # 不抛
    with pytest.raises(JobLookupError):
        sched.remove_job("不存在")


def test_unregister_removes_job():
    preferences.set_hk_sync_schedule(True, 16, 40)
    sched = _RecordingScheduler()
    hk_pipeline.register_hk_job(sched, _FakeRepo(Path("/tmp/x")))

    assert hk_pipeline.unregister_hk_job(sched) is True
    assert hk_pipeline.HK_SYNC_JOB_ID not in sched.jobs


def test_unregister_missing_job_returns_false_without_raising():
    """从未开启过时 remove_job 抛 JobLookupError, 必须吞掉返回 False。"""
    assert hk_pipeline.unregister_hk_job(_RecordingScheduler()) is False


def test_misfire_grace_time_is_bounded():
    """错过太久不该补跑: 深夜/盘中补一次全量拉取毫无意义还抢限流。"""
    preferences.set_hk_sync_schedule(True, 16, 40)
    sched = _RecordingScheduler()
    hk_pipeline.register_hk_job(sched, _FakeRepo(Path("/tmp/x")))
    assert sched.jobs[hk_pipeline.HK_SYNC_JOB_ID]["misfire_grace_time"] == 7200


# ============================================================
# 7. start_scheduler 集成: 港股注册失败不得影响 A 股
# ============================================================


def test_start_scheduler_survives_hk_registration_failure(monkeypatch, tmp_path):
    """港股注册抛异常时, A 股调度仍必须正常 start (隔离在注册点也要成立)。"""
    from app.jobs import daily_pipeline

    started: list[bool] = []

    class _Sched:
        timezone = None

        def __init__(self, *a, **kw) -> None:
            self.added: list[str] = []

        def add_job(self, func, **kw) -> None:
            self.added.append(kw.get("id", ""))

        def start(self) -> None:
            started.append(True)

    monkeypatch.setattr(daily_pipeline, "AsyncIOScheduler", _Sched)
    monkeypatch.setattr(
        hk_pipeline, "register_hk_job",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("港股注册炸了")),
    )

    repo = _FakeRepo(tmp_path)
    sched = daily_pipeline.start_scheduler(repo, capset=None)

    assert started == [True], "港股注册失败不得阻止 scheduler.start()"
    assert "daily_pipeline" in sched.added, "A 股盘后管道必须照常注册"
    assert "pre_market_instruments" in sched.added


def test_start_scheduler_registers_hk_when_enabled(monkeypatch, tmp_path):
    from app.jobs import daily_pipeline

    class _Sched:
        timezone = None

        def __init__(self, *a, **kw) -> None:
            self.added: list[str] = []

        def add_job(self, func, **kw) -> None:
            self.added.append(kw.get("id", ""))

        def start(self) -> None:
            pass

    monkeypatch.setattr(daily_pipeline, "AsyncIOScheduler", _Sched)
    preferences.set_hk_sync_schedule(True, 16, 40)

    sched = daily_pipeline.start_scheduler(_FakeRepo(tmp_path), capset=None)

    assert hk_pipeline.HK_SYNC_JOB_ID in sched.added


def test_start_scheduler_skips_hk_when_disabled(monkeypatch, tmp_path):
    from app.jobs import daily_pipeline

    class _Sched:
        timezone = None

        def __init__(self, *a, **kw) -> None:
            self.added: list[str] = []

        def add_job(self, func, **kw) -> None:
            self.added.append(kw.get("id", ""))

        def start(self) -> None:
            pass

    monkeypatch.setattr(daily_pipeline, "AsyncIOScheduler", _Sched)
    preferences.set_hk_sync_schedule(False, 16, 40)

    sched = daily_pipeline.start_scheduler(_FakeRepo(tmp_path), capset=None)

    assert hk_pipeline.HK_SYNC_JOB_ID not in sched.added
    assert "daily_pipeline" in sched.added


# ============================================================
# 8. settings API 契约
# ============================================================


@pytest.fixture
def api(tmp_path, monkeypatch):
    """最小 FastAPI app 只挂 settings router (项目惯例, 见 test_hk_api.py)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import settings as settings_api

    app = FastAPI()
    app.include_router(settings_api.router)
    app.state.repo = _FakeRepo(tmp_path)
    app.state.scheduler = _RecordingScheduler()
    return TestClient(app), app


def test_put_hk_schedule_registers_job(api):
    client, app = api
    r = client.put("/api/settings/preferences/hk-sync-schedule",
                   json={"enabled": True, "hour": 17, "minute": 10})
    assert r.status_code == 200
    assert r.json() == {
        "enabled": True, "hour": 17, "minute": 10, "include_cny_counter": False,
    }
    assert hk_pipeline.HK_SYNC_JOB_ID in app.state.scheduler.jobs
    assert preferences.get_hk_sync_schedule()["hour"] == 17


def test_put_hk_schedule_disabled_removes_job(api):
    client, app = api
    client.put("/api/settings/preferences/hk-sync-schedule",
               json={"enabled": True, "hour": 17, "minute": 10})
    assert hk_pipeline.HK_SYNC_JOB_ID in app.state.scheduler.jobs

    r = client.put("/api/settings/preferences/hk-sync-schedule",
                   json={"enabled": False, "hour": 17, "minute": 10})
    assert r.status_code == 200
    assert hk_pipeline.HK_SYNC_JOB_ID not in app.state.scheduler.jobs


def test_put_hk_schedule_never_disabled_does_not_raise(api):
    """从未开启过时点"关闭": remove_job 抛 JobLookupError, 端点必须吞掉返回 200。"""
    client, _ = api
    r = client.put("/api/settings/preferences/hk-sync-schedule",
                   json={"enabled": False, "hour": 16, "minute": 40})
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_put_hk_schedule_clamps_and_persists_cny_flag(api):
    client, _ = api
    r = client.put("/api/settings/preferences/hk-sync-schedule", json={
        "enabled": True, "hour": 99, "minute": -5, "include_cny_counter": True,
    })
    assert r.status_code == 200
    assert r.json() == {
        "enabled": True, "hour": 23, "minute": 0, "include_cny_counter": True,
    }
    assert preferences.get_hk_sync_schedule()["include_cny_counter"] is True


def test_put_hk_schedule_without_scheduler_still_persists(api):
    """app.state.scheduler 缺失(启动早期)时只落偏好, 不 500。"""
    client, app = api
    app.state.scheduler = None
    r = client.put("/api/settings/preferences/hk-sync-schedule",
                   json={"enabled": True, "hour": 16, "minute": 40})
    assert r.status_code == 200
    assert preferences.get_hk_sync_schedule()["enabled"] is True


def test_preferences_summary_includes_hk_schedule(api):
    """GET /preferences 必须带上港股调度, 否则前端设置页读不到当前状态。"""
    client, _ = api
    preferences.set_hk_sync_schedule(True, 18, 30, True)
    body = client.get("/api/settings/preferences").json()
    assert body["hk_sync_schedule"] == {
        "enabled": True, "hour": 18, "minute": 30, "include_cny_counter": True,
    }
