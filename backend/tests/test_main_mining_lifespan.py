from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import app.main as main_module


def _app_stub() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace())


def test_lifespan_holds_mining_process_lock_around_application(monkeypatch) -> None:
    events: list[str] = []

    class LockStub:
        def __init__(self, data_dir) -> None:
            del data_dir
            events.append("lock_created")
            self.acquired = False

        def acquire(self) -> None:
            self.acquired = True
            events.append("lock_acquired")

        def release(self) -> None:
            self.acquired = False
            events.append("lock_released")

    @asynccontextmanager
    async def application_lifespan(_app):
        events.append("application_started")
        try:
            yield
        finally:
            events.append("application_stopped")

    monkeypatch.setattr(main_module, "MiningProcessLock", LockStub)
    monkeypatch.setattr(main_module, "_application_lifespan", application_lifespan)

    app = _app_stub()

    async def exercise() -> None:
        async with main_module.lifespan(app):
            events.append("request_serving")

    asyncio.run(exercise())

    assert events == [
        "lock_created",
        "lock_acquired",
        "application_started",
        "request_serving",
        "application_stopped",
        "lock_released",
    ]
    assert app.state.mining_lock_acquired is True
    assert app.state.mining_process_lock is not None


def test_lifespan_releases_lock_when_application_shutdown_raises(monkeypatch) -> None:
    events: list[str] = []

    class LockStub:
        def __init__(self, data_dir) -> None:
            del data_dir
            self.acquired = False

        def acquire(self) -> None:
            self.acquired = True
            events.append("acquired")

        def release(self) -> None:
            self.acquired = False
            events.append("released")

    @asynccontextmanager
    async def application_lifespan(_app):
        try:
            yield
        finally:
            raise RuntimeError("shutdown failed")

    monkeypatch.setattr(main_module, "MiningProcessLock", LockStub)
    monkeypatch.setattr(main_module, "_application_lifespan", application_lifespan)

    async def exercise() -> None:
        async with main_module.lifespan(_app_stub()):
            pass

    with pytest.raises(RuntimeError, match="shutdown failed"):
        asyncio.run(exercise())

    assert events == ["acquired", "released"]


def test_lifespan_degrades_instead_of_failing_when_lock_held(
    monkeypatch, caplog
) -> None:
    """抢锁失败: 不再让 lifespan 上抛 (整个后端起不来), 改为降级启动。

    面板照常可用, 挖掘启动入口返回 409; WARNING 日志留排查线索;
    未获锁不得在 shutdown 时误调 release。
    """
    from app.services.mining_process_lock import MiningProcessLockError

    events: list[str] = []

    class LockStub:
        def __init__(self, data_dir) -> None:
            del data_dir
            self.acquired = False

        def acquire(self) -> None:
            raise MiningProcessLockError("another process owns mining")

        def release(self) -> None:
            raise AssertionError("acquire 失败后不应调用 release")

    @asynccontextmanager
    async def application_lifespan(_app):
        events.append("application_started")
        yield
        events.append("application_stopped")

    monkeypatch.setattr(main_module, "MiningProcessLock", LockStub)
    monkeypatch.setattr(main_module, "_application_lifespan", application_lifespan)

    app = _app_stub()

    async def exercise() -> None:
        async with main_module.lifespan(app):
            events.append("request_serving")

    with caplog.at_level("WARNING", logger="app.main"):
        asyncio.run(exercise())  # 不再抛出

    assert events == ["application_started", "request_serving", "application_stopped"]
    assert "挖掘锁" in caplog.text
    assert "降级" in caplog.text
    assert app.state.mining_lock_acquired is False


@pytest.mark.parametrize("acquired", [False, True])
def test_application_only_recovers_runs_when_it_owns_lock(tmp_path, monkeypatch, acquired):
    from app.services.mining_jobs import MiningRunStore

    store = MiningRunStore(tmp_path)
    store.create({}, "fixture", run_id="existing_run")
    store.transition_status("existing_run", "running")
    manifest = store.runs_root / "existing_run" / "manifest.json"
    before = manifest.read_bytes()

    class StopAfterManagerInitializationError(Exception):
        pass

    def stop(**kwargs):
        raise StopAfterManagerInitializationError

    monkeypatch.setattr(main_module, "DataStore", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(main_module, "KlineRepository", lambda _: SimpleNamespace(refresh_cache=stop))
    monkeypatch.setattr(main_module.settings, "backtest_matrix_disk_cache_enabled", False)
    monkeypatch.setattr("app.services.auth.bootstrap_from_env", lambda: None)
    monkeypatch.setattr("app.strategy.custom_signals.seed_if_empty", lambda _: 0)
    monkeypatch.setattr("app.factors.store.load_into_registry", lambda _: 0)
    app = SimpleNamespace(state=SimpleNamespace(mining_lock_acquired=acquired))

    async def exercise():
        async with main_module._application_lifespan(app):
            pytest.fail("fixture should stop before background services start")

    with pytest.raises(StopAfterManagerInitializationError):
        asyncio.run(exercise())
    assert store.get("existing_run")["status"] == ("interrupted" if acquired else "running")
    if not acquired:
        assert manifest.read_bytes() == before


def test_all_main_imports_resolve() -> None:
    """main.py 里每个 `from X import Y` 的 Y 都必须真实存在于 X。

    回归防护: shutdown 分支曾 `from app.services.quote_service import
    shutdown_webhook_executor`, 但该模块只有 shutdown_delivery_executors —
    名字对不上, 只在应用真正关闭、执行到那行时才 ImportError, 平时导入
    main 不会触发。这里静态遍历 AST 逐个解析, 把这类"惰性坏导入"挡在测试里。
    """
    import ast
    import importlib
    from pathlib import Path

    src = Path(main_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        # 只查绝对导入 (level==0); 相对导入在本模块内不出现
        if not isinstance(node, ast.ImportFrom) or node.level != 0 or not node.module:
            continue
        try:
            mod = importlib.import_module(node.module)
        except Exception:  # noqa: BLE001
            # 可选依赖/平台相关模块导入失败不算坏名字, 跳过
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            if hasattr(mod, alias.name):
                continue
            # 允许 `from pkg import submodule` 形式 (属性未预加载时再试导入)
            try:
                importlib.import_module(f"{node.module}.{alias.name}")
                continue
            except Exception:  # noqa: BLE001
                pass
            raise AssertionError(
                f"main.py L{node.lineno}: {node.module}.{alias.name} 不存在 (坏导入)"
            )
