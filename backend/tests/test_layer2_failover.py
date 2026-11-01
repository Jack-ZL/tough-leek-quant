"""P1-12 layer2: 实时行情热备 (运行时故障切换) 回归测试。

契约 (分层保险·第二层): 只有实时行情做运行时主备切换 (无状态快照, 切源
零污染风险)。主源连续失败 N 轮 → 熔断打开粘滞切备源 → 冷却到期半开探测
主源 → 成功切回 / 失败重新熔断。覆盖:

- RealtimeFailover 状态机 (假时钟, 纯单元);
- 备源解析 (auto/off/指定名 三种偏好);
- preferences.get_realtime_backup_provider 校验回退;
- quote_service._fetch_full_market_quotes 集成 (假 provider, 熔断→切备源);
- /capability-matrix 状态透出 + 偏好保存端点重置状态机。

全部用假时钟/假 provider, 不依赖真实网络。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.data_providers import custom as custom_sources
from app.services import preferences
from app.services.realtime_failover import RealtimeFailover, get_failover


# ---------------------------------------------------------------------------
# 测试桩
# ---------------------------------------------------------------------------

class FakeClock:
    """可手动拨动的单调时钟。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _make_fo(monkeypatch, backup: str | None = "tencent", threshold: int = 3,
             cooldown: float = 300.0) -> tuple[RealtimeFailover, FakeClock]:
    """构造状态机: 备源解析打桩为固定值, 时钟可拨动。"""
    clock = FakeClock()
    fo = RealtimeFailover(failure_threshold=threshold, cooldown_seconds=cooldown,
                          clock=clock)
    monkeypatch.setattr(fo, "_resolve_backup", lambda primary: backup)
    return fo, clock


# ---------------------------------------------------------------------------
# 状态机: 熔断 → 粘滞 → 半开 → 切回
# ---------------------------------------------------------------------------

def test_healthy_primary_used_directly(monkeypatch):
    fo, _ = _make_fo(monkeypatch)
    assert fo.resolve("fuyao") == "fuyao"
    assert fo.status()["state"] == "closed"


def test_opens_after_threshold_failures(monkeypatch):
    fo, _ = _make_fo(monkeypatch, threshold=3)
    for _ in range(3):
        fo.on_failure("fuyao")
    assert fo.resolve("fuyao") == "tencent"  # 粘滞切备源
    st = fo.status()
    assert st["state"] == "open" and st["active"] == "tencent"
    assert st["primary"] == "fuyao" and st["backup"] == "tencent"


def test_below_threshold_stays_closed(monkeypatch):
    fo, _ = _make_fo(monkeypatch, threshold=3)
    fo.on_failure("fuyao")
    fo.on_failure("fuyao")
    assert fo.resolve("fuyao") == "fuyao"
    # 成功一轮 → 计数清零, 再失败两次仍不熔断
    fo.on_success("fuyao")
    fo.on_failure("fuyao")
    fo.on_failure("fuyao")
    assert fo.resolve("fuyao") == "fuyao"


def test_sticky_during_cooldown(monkeypatch):
    fo, clock = _make_fo(monkeypatch, cooldown=300.0)
    for _ in range(3):
        fo.on_failure("fuyao")
    clock.advance(299.0)
    assert fo.resolve("fuyao") == "tencent"  # 冷却未到 → 继续备源
    assert fo.status()["cooldown_remaining"] == pytest.approx(1.0, abs=0.2)


def test_half_open_probe_after_cooldown(monkeypatch):
    fo, clock = _make_fo(monkeypatch, cooldown=300.0)
    for _ in range(3):
        fo.on_failure("fuyao")
    clock.advance(300.0)
    assert fo.resolve("fuyao") == "fuyao"  # 冷却到期 → 放主源探测
    assert fo.status()["state"] == "half_open"


def test_probe_success_closes_breaker(monkeypatch):
    fo, clock = _make_fo(monkeypatch)
    for _ in range(3):
        fo.on_failure("fuyao")
    clock.advance(300.0)
    fo.resolve("fuyao")          # 进入半开
    fo.on_success("fuyao")       # 探测成功
    assert fo.status()["state"] == "closed"
    assert fo.resolve("fuyao") == "fuyao"


def test_probe_failure_reopens_breaker(monkeypatch):
    fo, clock = _make_fo(monkeypatch)
    for _ in range(3):
        fo.on_failure("fuyao")
    clock.advance(300.0)
    fo.resolve("fuyao")          # 半开探测
    fo.on_failure("fuyao")       # 探测失败 → 重新熔断
    assert fo.resolve("fuyao") == "tencent"
    # 新熔断的冷却从头计: 299s 内仍是备源, 再到期又可探测
    clock.advance(299.0)
    assert fo.resolve("fuyao") == "tencent"
    clock.advance(1.0)
    assert fo.resolve("fuyao") == "fuyao"


def test_backup_failure_while_open_keeps_open(monkeypatch):
    fo, clock = _make_fo(monkeypatch)
    for _ in range(3):
        fo.on_failure("fuyao")
    fo.on_failure("tencent")     # 备源也失败 → 只记日志, 状态不变
    assert fo.resolve("fuyao") == "tencent"
    assert fo.status()["state"] == "open"
    # 冷却到期照常探测主源 (备源失败不阻塞探测窗口)
    clock.advance(300.0)
    assert fo.resolve("fuyao") == "fuyao"


def test_no_backup_never_switches(monkeypatch):
    fo, _ = _make_fo(monkeypatch, backup=None)
    for _ in range(5):
        fo.on_failure("fuyao")
    assert fo.resolve("fuyao") == "fuyao"  # 无备源 → 只能继续主源
    st = fo.status()
    assert st["backup"] is None and st["active"] == "fuyao"


def test_primary_change_resets_state(monkeypatch):
    fo, _ = _make_fo(monkeypatch)
    for _ in range(3):
        fo.on_failure("fuyao")
    assert fo.resolve("fuyao") == "tencent"
    # 用户设置页切了主源 → 旧熔断作废, 新主源从头计
    assert fo.resolve("tickflow") == "tickflow"
    assert fo.status()["state"] == "closed"


def test_reset_clears_everything(monkeypatch):
    fo, _ = _make_fo(monkeypatch)
    for _ in range(3):
        fo.on_failure("fuyao")
    fo.reset()
    st = fo.status()
    assert st["state"] == "closed" and st["primary"] is None and st["backup"] is None


# ---------------------------------------------------------------------------
# 备源解析: auto / off / 指定名
# ---------------------------------------------------------------------------

def test_resolve_backup_off(monkeypatch):
    monkeypatch.setattr(preferences, "get_realtime_backup_provider", lambda: "off")
    fo = RealtimeFailover()
    assert fo._resolve_backup("fuyao") is None


def test_resolve_backup_auto_prefers_tencent(monkeypatch):
    monkeypatch.setattr(preferences, "get_realtime_backup_provider", lambda: "auto")
    spy = MagicMock(return_value="tencent")
    monkeypatch.setattr(custom_sources, "find_complement_provider", spy)
    fo = RealtimeFailover()
    assert fo._resolve_backup("fuyao") == "tencent"
    # 腾讯优先 + 排除主源自己
    spy.assert_called_once_with("realtime", exclude="fuyao", prefer=("tencent",))


def test_resolve_backup_auto_guards_self(monkeypatch):
    """测试桩/异常情况下 finder 返回主源自己 → 视为无备源。"""
    monkeypatch.setattr(preferences, "get_realtime_backup_provider", lambda: "auto")
    monkeypatch.setattr(custom_sources, "find_complement_provider",
                        lambda *a, **k: "fuyao")
    fo = RealtimeFailover()
    assert fo._resolve_backup("fuyao") is None


def test_resolve_backup_explicit_name(monkeypatch):
    monkeypatch.setattr(preferences, "get_realtime_backup_provider", lambda: "eastmoney")
    monkeypatch.setattr(custom_sources, "provider_has_dataset",
                        lambda name, ds: name == "eastmoney" and ds == "realtime")
    fo = RealtimeFailover()
    assert fo._resolve_backup("fuyao") == "eastmoney"
    # 指定名 == 主源自己 → 无热备 (不擅自换别的源)
    assert fo._resolve_backup("eastmoney") is None


def test_resolve_backup_explicit_unavailable(monkeypatch):
    monkeypatch.setattr(preferences, "get_realtime_backup_provider", lambda: "eastmoney")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, ds: False)
    fo = RealtimeFailover()
    assert fo._resolve_backup("fuyao") is None


# ---------------------------------------------------------------------------
# preferences.get_realtime_backup_provider 校验回退
# ---------------------------------------------------------------------------

def test_backup_pref_default_auto(monkeypatch):
    monkeypatch.setattr(preferences, "load", lambda: {})
    assert preferences.get_realtime_backup_provider() == "auto"


def test_backup_pref_off(monkeypatch):
    monkeypatch.setattr(preferences, "load", lambda: {"realtime_backup_provider": "off"})
    assert preferences.get_realtime_backup_provider() == "off"


def test_backup_pref_valid_name(monkeypatch):
    monkeypatch.setattr(preferences, "load",
                        lambda: {"realtime_backup_provider": "tencent"})
    monkeypatch.setattr(custom_sources, "provider_has_dataset",
                        lambda name, ds: name == "tencent" and ds == "realtime")
    assert preferences.get_realtime_backup_provider() == "tencent"


def test_backup_pref_invalid_falls_back_auto(monkeypatch):
    monkeypatch.setattr(preferences, "load",
                        lambda: {"realtime_backup_provider": "ghost"})
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, ds: False)
    assert preferences.get_realtime_backup_provider() == "auto"
    # tickflow 不允许当备源 (备源只从自定义/插件源里选)
    monkeypatch.setattr(preferences, "load",
                        lambda: {"realtime_backup_provider": "tickflow"})
    assert preferences.get_realtime_backup_provider() == "auto"


# ---------------------------------------------------------------------------
# quote_service 集成: 主源连挂 → 熔断 → 备源接管
# ---------------------------------------------------------------------------

def _install_realtime_env(monkeypatch, primary: str, primary_impl, backup_impl):
    """搭好 _fetch_full_market_quotes 的假环境: 双源 + auto 备源偏好。"""
    from app.services import quote_service as qs

    providers = {"fuyao": primary_impl, "tencent": backup_impl}
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: primary)
    monkeypatch.setattr(preferences, "get_realtime_backup_provider", lambda: "auto")
    monkeypatch.setattr(custom_sources, "provider_has_dataset",
                        lambda name, ds: name in providers and ds == "realtime")
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: providers[name])
    monkeypatch.setattr(custom_sources, "find_complement_provider",
                        lambda dataset, exclude=None, prefer=None:
                        "tencent" if dataset == "realtime" and exclude != "tencent" else None)
    service = qs.QuoteService()
    captured: list[list[dict]] = []
    monkeypatch.setattr(
        service, "_process_full_market_records",
        lambda records, *, t0, now_ts, replace_index_cache=True, final_boundary_ms=None: (
            captured.append(records), None,
        )[1],
    )
    return service, captured


def test_fetch_switches_to_backup_after_failures(monkeypatch):
    """主源连挂 3 轮 → 熔断打开 → 第 4 轮起备源接管取数。"""
    failing = SimpleNamespace(get_realtime=MagicMock(side_effect=RuntimeError("boom")))
    healthy = SimpleNamespace(get_realtime=MagicMock(
        return_value=[{"symbol": "600519.SH", "last_price": 1480.0}]))
    service, captured = _install_realtime_env(monkeypatch, "fuyao", failing, healthy)

    for _ in range(3):
        service._fetch_full_market_quotes()
    assert failing.get_realtime.call_count == 3
    assert get_failover().status()["state"] == "open"

    service._fetch_full_market_quotes()  # 第 4 轮 → 备源
    healthy.get_realtime.assert_called_once()
    assert captured and captured[0][0]["symbol"] == "600519.SH"
    # 主源熔断期内不再被碰
    assert failing.get_realtime.call_count == 3


def test_fetch_success_never_switches(monkeypatch):
    healthy = SimpleNamespace(get_realtime=MagicMock(
        return_value=[{"symbol": "600519.SH", "last_price": 1480.0}]))
    backup = SimpleNamespace(get_realtime=MagicMock(return_value=[]))
    service, captured = _install_realtime_env(monkeypatch, "fuyao", healthy, backup)

    for _ in range(5):
        service._fetch_full_market_quotes()
    assert healthy.get_realtime.call_count == 5
    backup.get_realtime.assert_not_called()
    assert get_failover().status()["state"] == "closed"
    assert len(captured) == 5


def test_fetch_empty_snapshot_counts_as_failure(monkeypatch):
    """插件软失败约定 (网络错 → []) → 空快照同样计入熔断。"""
    empty = SimpleNamespace(get_realtime=MagicMock(return_value=[]))
    healthy = SimpleNamespace(get_realtime=MagicMock(
        return_value=[{"symbol": "000001.SZ", "last_price": 10.0}]))
    service, _ = _install_realtime_env(monkeypatch, "fuyao", empty, healthy)

    for _ in range(3):
        service._fetch_full_market_quotes()
    assert get_failover().status()["state"] == "open"
    service._fetch_full_market_quotes()
    healthy.get_realtime.assert_called_once()


def test_fetch_backup_off_keeps_hammering_primary(monkeypatch):
    """备源偏好 off → 不切换, 主源失败只记日志。"""
    failing = SimpleNamespace(get_realtime=MagicMock(side_effect=RuntimeError("boom")))
    backup = SimpleNamespace(get_realtime=MagicMock(return_value=[]))
    service, _ = _install_realtime_env(monkeypatch, "fuyao", failing, backup)
    monkeypatch.setattr(preferences, "get_realtime_backup_provider", lambda: "off")

    for _ in range(5):
        service._fetch_full_market_quotes()
    assert failing.get_realtime.call_count == 5
    backup.get_realtime.assert_not_called()


# ---------------------------------------------------------------------------
# API 透出: /capability-matrix 状态 + 偏好保存重置
# ---------------------------------------------------------------------------

def test_capability_matrix_exposes_failover(monkeypatch):
    from app.api.settings import get_capability_matrix

    monkeypatch.setattr("app.data_providers.capabilities.build_capability_matrix",
                        lambda current, tickflow_tier="none": {
                            "tickflow_tier": tickflow_tier, "capabilities": []})
    monkeypatch.setattr("app.tickflow.policy.base_tier_name", lambda: "free")
    monkeypatch.setattr(preferences, "get_realtime_backup_provider", lambda: "auto")

    result = get_capability_matrix()
    fo = result["realtime_failover"]
    assert fo["state"] == "closed" and fo["threshold"] == 3
    assert result["realtime_backup_provider"] == "auto"


def test_update_data_providers_resets_failover(monkeypatch):
    """保存实时主源/备源偏好 → 热备状态机作废重算。"""
    from app.api import settings as settings_api

    fo = get_failover()
    monkeypatch.setattr(fo, "_resolve_backup", lambda primary: "tencent")
    for _ in range(3):
        fo.on_failure("fuyao")
    fo._primary = "fuyao"
    assert fo.status()["state"] == "open"

    monkeypatch.setattr(preferences, "save", lambda updates: updates)
    monkeypatch.setattr(settings_api, "detect_capabilities", lambda: None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    req = settings_api.DataProvidersIn(realtime_backup_provider="off")
    settings_api.update_data_providers(req, request)
    assert fo.status()["state"] == "closed"
    assert settings_api.DataProvidersIn(realtime_backup_provider="off").model_dump(
        exclude_none=True) == {"realtime_backup_provider": "off"}
