"""P1-12 layer1: 静态互补源回归测试。

契约 (分层保险·第一层): TickFlow 档位缺某能力、且用户选定的自定义源也未
声明该数据集时, 由「免费内置源」(无 Key、billing=free 的内置插件, 如
东财/腾讯/baostock) 静态补授能力并接管取数:

- CapabilitySet.grant_complement/complement_of: 补授并记录来源, 不覆盖原生能力;
- loader.find_complement_provider: 互补源资格 (内置+可用+免Key+免费+声明数据集)
  与优先级排序;
- loader.resolve_with_complement: 选定源(已声明) → TickFlow(原生) → 互补源;
- policy._augment_custom_sources: 探测期补授 (原生已有能力时不补);
- tickflow_native_serves: 档位原生能力判定 (无 capset 的取数点用);
- 各取数点路由: 分钟源解析 / 实时模式与限速 / 全市场行情接管 / 五档 / 财务;
- 能力矩阵与门禁: usable 放宽 + complement 字段透出。

全部用假插件/假 provider, 不依赖真实网络。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.data_providers import custom as custom_sources
from app.data_providers.custom import loader as loader_mod
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


# ---------------------------------------------------------------------------
# 测试桩
# ---------------------------------------------------------------------------

def _fake_plugin_provider(datasets: list[str]) -> SimpleNamespace:
    """模拟内置插件 provider: 只需 config.datasets (loader 判定用)。"""
    return SimpleNamespace(config=SimpleNamespace(datasets=dict.fromkeys(datasets)))


def _install_plugins(monkeypatch, plugins: dict[str, dict]) -> None:
    """替换 loader 的 _PROVIDERS/_PLUGIN_STATUS 为假插件表。

    plugins: {name: {"datasets": [...], "api_key_env": "", "billing": "free"}}
    """
    providers = {
        name: _fake_plugin_provider(cfg.get("datasets", []))
        for name, cfg in plugins.items()
    }
    status = {
        name: {
            "api_key_env": cfg.get("api_key_env", ""),
            "billing": cfg.get("billing", "free"),
        }
        for name, cfg in plugins.items()
    }
    monkeypatch.setattr(loader_mod, "_PROVIDERS", providers)
    monkeypatch.setattr(loader_mod, "_PLUGIN_STATUS", status)


_FREE_THREE = {
    "eastmoney": {"datasets": ["realtime", "daily", "adj_factor", "depth5"]},
    "tencent": {"datasets": ["realtime", "depth5", "minute"]},
    "baostock": {"datasets": ["daily", "adj_factor"]},
}


# ---------------------------------------------------------------------------
# CapabilitySet 互补授权
# ---------------------------------------------------------------------------

def test_grant_complement_records_source():
    capset = CapabilitySet()
    capset.grant_complement(Cap.ADJ_FACTOR, "eastmoney")
    assert capset.has(Cap.ADJ_FACTOR)
    assert capset.complement_of(Cap.ADJ_FACTOR) == "eastmoney"


def test_grant_complement_never_overrides_native():
    """TickFlow 原生已有能力 → 不记录互补源 (取数仍走 TickFlow)。"""
    capset = CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits(batch=50, rpm=30)})
    capset.grant_complement(Cap.ADJ_FACTOR, "eastmoney")
    assert capset.complement_of(Cap.ADJ_FACTOR) is None
    # 原生 limits 不被覆盖
    assert capset.limits(Cap.ADJ_FACTOR).batch == 50


def test_complement_of_unknown_cap_is_none():
    assert CapabilitySet().complement_of(Cap.FINANCIAL) is None


# ---------------------------------------------------------------------------
# find_complement_provider: 资格与优先级
# ---------------------------------------------------------------------------

def test_complement_priority_eastmoney_first(monkeypatch):
    _install_plugins(monkeypatch, _FREE_THREE)
    # realtime 三家都声明 → 按优先级取东财
    assert loader_mod.find_complement_provider("realtime") == "eastmoney"
    # minute 只有腾讯声明
    assert loader_mod.find_complement_provider("minute") == "tencent"
    # adj_factor: 东财优先于 baostock
    assert loader_mod.find_complement_provider("adj_factor") == "eastmoney"


def test_complement_exclude_skips_current(monkeypatch):
    _install_plugins(monkeypatch, _FREE_THREE)
    assert loader_mod.find_complement_provider("realtime", exclude="eastmoney") == "tencent"


def test_complement_requires_declared_dataset(monkeypatch):
    _install_plugins(monkeypatch, _FREE_THREE)
    # 没有任何免费源声明 financial/full_minute → None
    assert loader_mod.find_complement_provider("financial") is None
    assert loader_mod.find_complement_provider("full_minute") is None


def test_complement_skips_key_and_paid_plugins(monkeypatch):
    """需 Key (api_key_env) 或计费 (billing≠free) 的内置插件不自动接管。"""
    _install_plugins(monkeypatch, {
        "fuyao": {"datasets": ["realtime"], "api_key_env": "FUYAO_TOKEN"},
        "tdxquant": {"datasets": ["realtime"], "billing": "points"},
        "tencent": {"datasets": ["realtime"]},
    })
    assert loader_mod.find_complement_provider("realtime") == "tencent"


def test_complement_skips_user_http_sources(monkeypatch):
    """用户自配 HTTP 源 (无 _PLUGIN_STATUS 条目) 不参与自动接管。"""
    monkeypatch.setattr(loader_mod, "_PROVIDERS", {
        "my_http": _fake_plugin_provider(["realtime"]),
    })
    monkeypatch.setattr(loader_mod, "_PLUGIN_STATUS", {})
    assert loader_mod.find_complement_provider("realtime") is None


def test_complement_never_returns_tickflow(monkeypatch):
    _install_plugins(monkeypatch, {"tickflow": {"datasets": ["realtime"]}})
    assert loader_mod.find_complement_provider("realtime") is None


# ---------------------------------------------------------------------------
# resolve_with_complement: 三级路由
# ---------------------------------------------------------------------------

def test_resolve_keeps_declared_selected(monkeypatch):
    monkeypatch.setattr(loader_mod, "provider_has_dataset",
                        lambda name, ds: name == "fuyao" and ds == "realtime")
    capset = CapabilitySet()
    capset.grant_complement(Cap.KLINE_MINUTE_BATCH, "tencent")
    assert loader_mod.resolve_with_complement(
        "realtime", Cap.KLINE_MINUTE_BATCH, capset, "fuyao") == "fuyao"


def test_resolve_routes_to_complement(monkeypatch):
    monkeypatch.setattr(loader_mod, "provider_has_dataset",
                        lambda name, ds: name == "tencent" and ds == "minute")
    capset = CapabilitySet()
    capset.grant_complement(Cap.KLINE_MINUTE_BATCH, "tencent")
    assert loader_mod.resolve_with_complement(
        "minute", Cap.KLINE_MINUTE_BATCH, capset, "tickflow") == "tencent"


def test_resolve_complement_must_declare_dataset(monkeypatch):
    """记录的互补源若未声明该数据集 (防御) → 原样返回, 不误路由。"""
    monkeypatch.setattr(loader_mod, "provider_has_dataset", lambda name, ds: False)
    capset = CapabilitySet()
    capset.grant_complement(Cap.KLINE_MINUTE_BATCH, "tencent")
    assert loader_mod.resolve_with_complement(
        "minute", Cap.KLINE_MINUTE_BATCH, capset, "tickflow") == "tickflow"


def test_resolve_tolerates_capset_without_complement_api(monkeypatch):
    """兼容不带 complement_of 的旧 capset 替身 (测试桩/鸭子类型)。"""
    monkeypatch.setattr(loader_mod, "provider_has_dataset", lambda name, ds: False)

    class _LegacyCaps:
        def has(self, cap):  # pragma: no cover - 桩
            return False

    assert loader_mod.resolve_with_complement(
        "daily", Cap.KLINE_DAILY_BATCH, _LegacyCaps(), "tickflow") == "tickflow"


# ---------------------------------------------------------------------------
# tickflow_native_serves: 档位原生能力判定
# ---------------------------------------------------------------------------

def test_tickflow_native_serves_matrix():
    from app.data_providers.capabilities import tickflow_native_serves

    # daily: none 档 (free-api) 原生就有
    assert tickflow_native_serves("daily", "none") is True
    # realtime: none/free 无, starter+ 有
    assert tickflow_native_serves("realtime", "free") is False
    assert tickflow_native_serves("realtime", "starter") is True
    # minute: pro+ 才有
    assert tickflow_native_serves("minute", "starter") is False
    assert tickflow_native_serves("minute", "pro") is True
    # financial: expert 才有
    assert tickflow_native_serves("financial", "pro") is False
    assert tickflow_native_serves("financial", "expert") is True
    # 未知能力 fail-closed
    assert tickflow_native_serves("nope", "expert") is False


# ---------------------------------------------------------------------------
# policy._augment_custom_sources: 探测期互补补授
# ---------------------------------------------------------------------------

def _set_all_tickflow(monkeypatch) -> None:
    from app.services import preferences
    for getter in ("get_daily_data_provider", "get_adj_factor_provider",
                   "get_minute_data_provider", "get_depth5_data_provider",
                   "get_financial_provider", "get_full_minute_data_provider"):
        monkeypatch.setattr(preferences, getter, lambda: "tickflow")


def test_augment_grants_complement_when_native_missing(monkeypatch):
    """TickFlow 缺能力 + 无选定自定义源 → 免费内置源补授并记录来源。"""
    from app.tickflow.policy import _augment_custom_sources

    _set_all_tickflow(monkeypatch)
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, ds: False)
    _install_plugins(monkeypatch, _FREE_THREE)

    capset = CapabilitySet()  # 模拟 none 档: 无任何原生能力
    _augment_custom_sources(capset)
    assert capset.has(Cap.ADJ_FACTOR)
    assert capset.complement_of(Cap.ADJ_FACTOR) == "eastmoney"
    assert capset.complement_of(Cap.DEPTH5_BATCH) == "eastmoney"
    assert capset.complement_of(Cap.KLINE_MINUTE_BATCH) == "tencent"
    # 无免费源声明的能力不补
    assert not capset.has(Cap.FINANCIAL)
    assert not capset.has(Cap.INTRADAY_UNIVERSE)


def test_augment_no_complement_when_native_present(monkeypatch):
    """TickFlow 原生已有能力 → 不记录互补源 (取数仍走 TickFlow)。"""
    from app.tickflow.policy import _augment_custom_sources

    _set_all_tickflow(monkeypatch)
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, ds: False)
    _install_plugins(monkeypatch, _FREE_THREE)

    capset = CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits()})
    _augment_custom_sources(capset)
    assert capset.complement_of(Cap.ADJ_FACTOR) is None


def test_augment_selected_custom_wins_over_complement(monkeypatch):
    """选定源声明了数据集 → 走既有 grant 路径, 不记录互补。"""
    from app.services import preferences
    from app.tickflow.policy import _augment_custom_sources

    _set_all_tickflow(monkeypatch)
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "fuyao")
    monkeypatch.setattr(custom_sources, "provider_has_dataset",
                        lambda name, ds: name == "fuyao" and ds == "adj_factor")
    _install_plugins(monkeypatch, _FREE_THREE)

    capset = CapabilitySet()
    _augment_custom_sources(capset)
    assert capset.has(Cap.ADJ_FACTOR)
    assert capset.complement_of(Cap.ADJ_FACTOR) is None


# ---------------------------------------------------------------------------
# 分钟源解析: 静态互补注入点
# ---------------------------------------------------------------------------

def test_minute_resolver_complement_on_free_tier(monkeypatch):
    """none 档 (TickFlow 无分钟能力) → 腾讯 m1 分钟线自动接管。"""
    from app.services import kline_sync
    from app.services import preferences
    from app.tickflow import policy

    monkeypatch.setattr(preferences, "load", lambda: {"minute_data_provider": "tickflow"})
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr(policy, "base_tier_name", lambda: "none")
    _install_plugins(monkeypatch, _FREE_THREE)
    assert kline_sync._selected_minute_provider_name() == "tencent"


def test_minute_resolver_keeps_tickflow_on_pro(monkeypatch):
    """pro 档 TickFlow 原生有分钟能力 → 不注入互补源。"""
    from app.services import kline_sync
    from app.services import preferences
    from app.tickflow import policy

    monkeypatch.setattr(preferences, "load", lambda: {"minute_data_provider": "tickflow"})
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr(policy, "base_tier_name", lambda: "pro")
    _install_plugins(monkeypatch, _FREE_THREE)
    assert kline_sync._selected_minute_provider_name() == "tickflow"


def test_minute_resolver_unavailable_prefix_wins(monkeypatch):
    """用户明确选过自定义源但已失效 → 保持 fail-closed, 互补源不掩盖。"""
    from app.services import kline_sync
    from app.services import preferences
    from app.tickflow import policy

    monkeypatch.setattr(preferences, "load", lambda: {"minute_data_provider": "fuyao"})
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr(policy, "base_tier_name", lambda: "none")
    _install_plugins(monkeypatch, _FREE_THREE)
    assert kline_sync._selected_minute_provider_name() == "unavailable:fuyao"


# ---------------------------------------------------------------------------
# 实时行情: 模式判定 / 限速 / 全市场接管
# ---------------------------------------------------------------------------

def test_realtime_mode_full_market_via_complement(monkeypatch):
    from app.services import preferences
    from app.services.quote_service import QuoteService

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tickflow")
    monkeypatch.setattr(QuoteService, "_current_tier", lambda: "none")
    _install_plugins(monkeypatch, _FREE_THREE)
    assert QuoteService.realtime_mode() == "full_market"
    assert QuoteService.is_realtime_allowed() is True
    # 互补源顶实时时 TickFlow 档位限速不适用 → 通用 1s 下限
    assert QuoteService._tier_min_interval() == QuoteService.CUSTOM_PROVIDER_MIN_INTERVAL


def test_realtime_mode_none_without_complement(monkeypatch):
    from app.services import preferences
    from app.services.quote_service import QuoteService

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tickflow")
    monkeypatch.setattr(QuoteService, "_current_tier", lambda: "free")
    _install_plugins(monkeypatch, {})
    assert QuoteService.realtime_mode() == "none"


def test_full_market_fetch_taken_over_by_complement(monkeypatch):
    """无付费 Key → 全市场行情轮由静态互补源接管, 不再直接放弃。"""
    from app.services import preferences
    from app.services import quote_service as qs

    provider = SimpleNamespace(get_realtime=MagicMock(
        return_value=[{"symbol": "600519.SH", "last_price": 1480.0}]))
    service = qs.QuoteService()
    captured: list[list[dict]] = []
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tickflow")
    monkeypatch.setattr(custom_sources, "provider_has_dataset",
                        lambda name, ds: name == "tencent" and ds == "realtime")
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)
    # P1-12 layer2: 热备状态机也会调 find_complement_provider (带 prefer 关键字)
    monkeypatch.setattr(custom_sources, "find_complement_provider",
                        lambda dataset, exclude=None, prefer=None:
                        "tencent" if dataset == "realtime" else None)
    monkeypatch.setattr("app.tickflow.client.get_paid_realtime_client", lambda: None)
    monkeypatch.setattr(
        service, "_process_full_market_records",
        lambda records, *, t0, now_ts, replace_index_cache=True, final_boundary_ms=None: (
            captured.append(records), None,
        )[1],
    )

    service._fetch_full_market_quotes()
    provider.get_realtime.assert_called_once()
    assert captured and captured[0][0]["symbol"] == "600519.SH"


def test_full_market_fetch_prefers_paid_tickflow(monkeypatch):
    """配置了付费 Key → 仍走 TickFlow, 互补源不抢路由。"""
    from app.services import preferences
    from app.services import quote_service as qs

    service = qs.QuoteService()
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tickflow")
    # P1-12 layer2: 关掉热备, 让本测试只考察 layer1 语义 (互补源不抢付费 Key 的路由)
    monkeypatch.setattr(preferences, "get_realtime_backup_provider", lambda: "off")
    monkeypatch.setattr("app.tickflow.client.get_paid_realtime_client",
                        lambda: SimpleNamespace(quotes=SimpleNamespace(
                            get_by_universes=MagicMock(return_value=[]),
                            get=MagicMock(return_value=[]),
                        )))
    complement_spy = MagicMock(return_value=None)
    monkeypatch.setattr(custom_sources, "find_complement_provider", complement_spy)
    monkeypatch.setattr(service, "_collect_monitor_index_symbols", lambda: set())
    monkeypatch.setattr(service, "_repo", None)

    service._fetch_full_market_quotes()  # resp 为空 → warn 返回, 不抛异常
    complement_spy.assert_not_called()


# ---------------------------------------------------------------------------
# 五档盘口路由
# ---------------------------------------------------------------------------

def test_depth_batch_routes_to_complement(monkeypatch):
    from app.services import depth_service as depth_module
    from app.services.depth_service import DepthService

    provider = SimpleNamespace(get_depth_batch=MagicMock(
        side_effect=lambda symbols: {s: {"ask_volumes": [0]} for s in symbols}))
    service = DepthService()
    capset = CapabilitySet({Cap.DEPTH5_BATCH: CapabilityLimits(batch=100, rpm=30)})
    capset._caps.pop(Cap.DEPTH5_BATCH)  # 模拟原生缺能力后由互补补授
    capset.grant_complement(Cap.DEPTH5_BATCH, "tencent")
    service._app_state = SimpleNamespace(capabilities=capset)

    monkeypatch.setattr("app.services.preferences.get_depth5_data_provider",
                        lambda: "tickflow")
    monkeypatch.setattr(custom_sources, "provider_has_dataset",
                        lambda name, ds: name == "tencent" and ds == "depth5")
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)
    monkeypatch.setattr(depth_module, "sleep_between_batches", MagicMock())

    result = service._call_depth_batch(["A", "B"])
    assert set(result) == {"A", "B"}
    provider.get_depth_batch.assert_called_once_with(["A", "B"])


# ---------------------------------------------------------------------------
# 财务表路由 (通用钩子: 当前无免费源声明 financial)
# ---------------------------------------------------------------------------

def test_financial_fetch_table_uses_complement(monkeypatch):
    from app.services import financial_sync
    from app.services import preferences

    provider = SimpleNamespace(get_financials=MagicMock(return_value=pl.DataFrame(
        {"symbol": ["600519.SH"], "net_income": [1.0]})))
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "tickflow")
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)

    capset = CapabilitySet()
    capset.grant_complement(Cap.FINANCIAL, "mock_fin")
    df = financial_sync._fetch_table("income", ["600519.SH"], capset)
    provider.get_financials.assert_called_once_with(
        "income", ["600519.SH"], latest_only=True)
    assert df["symbol"][0] == "600519.SH"


def test_financial_fetch_table_skips_without_any_source(monkeypatch):
    """无原生能力、无选定源、无互补源 → 维持跳过语义。"""
    from app.services import financial_sync
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "tickflow")
    df = financial_sync._fetch_table("income", ["600519.SH"], CapabilitySet())
    assert df.is_empty()


# ---------------------------------------------------------------------------
# 能力矩阵 / 门禁: usable 放宽 + complement 字段透出
# ---------------------------------------------------------------------------

def test_matrix_exposes_complement(monkeypatch):
    from app.data_providers.capabilities import build_capability_matrix

    monkeypatch.setattr(custom_sources, "list_plugins", lambda: [
        {"name": "tencent", "display_name": "腾讯", "datasets": ["realtime"],
         "available": True, "status": "ok"},
    ])
    monkeypatch.setattr(custom_sources, "list_sources", lambda: [])
    monkeypatch.setattr(custom_sources, "find_complement_provider",
                        lambda dataset, exclude=None: "tencent" if dataset == "realtime" else None)

    current = {
        "daily_data_provider": "tickflow", "adj_factor_provider": "tickflow",
        "minute_data_provider": "tickflow", "full_minute_data_provider": "tickflow",
        "depth5_data_provider": "tickflow", "realtime_data_provider": "tickflow",
        "financial_data_provider": "tickflow",
    }
    caps = {c["id"]: c for c in build_capability_matrix(current, tickflow_tier="free")[
        "capabilities"]}
    rt = caps["realtime"]
    assert rt["usable"] is True           # 互补源顶上了 → 能力实际可用
    assert rt["complement"] == "tencent"
    assert rt["complement_display"] == "腾讯"
    # 无互补源的能力维持不可用
    assert caps["minute"]["usable"] is False
    assert caps["minute"]["complement"] is None


def test_capability_gate_label_marks_complement(monkeypatch):
    from app.data_providers.capabilities import capability_gate

    monkeypatch.setattr(custom_sources, "list_plugins", lambda: [
        {"name": "tencent", "display_name": "腾讯", "datasets": ["realtime"],
         "available": True, "status": "ok"},
    ])
    monkeypatch.setattr(custom_sources, "list_sources", lambda: [])
    monkeypatch.setattr(custom_sources, "find_complement_provider",
                        lambda dataset, exclude=None: "tencent" if dataset == "realtime" else None)

    usable, label = capability_gate("realtime", tickflow_tier="free")
    assert usable is True
    assert "静态互补" in label and "腾讯" in label
