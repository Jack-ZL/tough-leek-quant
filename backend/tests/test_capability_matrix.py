"""能力路由矩阵契约测试。

覆盖: 注册表与路由偏好字段一一对应、候选按各源 datasets 声明过滤、
候选只含当前可用源 (未就绪插件进 pending 并携带原因)、TickFlow 候选
按当前订阅档位过滤、偏好指向 tickflow 但档位不足时的 tf_available
标记、usable 跟随生效源 (各页能力门控的统一判定)、每能力独立路由
(「跟随日K」特殊值已下线, 存量旧值由 preferences getter 自愈回退)、
偏好指向未知源时的回退形态。全部用假插件/自定义源,
不依赖真实网络与本地 data/ 目录。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.data_providers import custom as custom_sources
from app.data_providers.capabilities import CAPABILITY_REGISTRY, build_capability_matrix


@pytest.fixture(autouse=True)
def _no_static_complement(monkeypatch):
    """P1-12 layer1 隔离: 本文件聚焦既有矩阵语义, 静态互补源一律置空
    (互补行为由 test_layer1_complement.py 专门覆盖), 避免测试机上真实
    内置免费插件 (东财/腾讯/baostock) 把 usable 翻转造成串扰。"""
    monkeypatch.setattr(custom_sources, "find_complement_provider", lambda *a, **k: None)

DEFAULT_CURRENT = {
    "daily_data_provider": "tickflow",
    "adj_factor_provider": "tickflow",
    "minute_data_provider": "tickflow",
    "full_minute_data_provider": "tickflow",
    "depth5_data_provider": "tickflow",
    "realtime_data_provider": "tickflow",
    "financial_data_provider": "tickflow",
}


def _fake_sources(monkeypatch, plugins: list[dict], customs: list[dict] | None = None) -> None:
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: plugins)
    monkeypatch.setattr(custom_sources, "list_sources", lambda: customs or [])


def _by_id(matrix: dict) -> dict[str, dict]:
    return {c["id"]: c for c in matrix["capabilities"]}


def test_registry_covers_all_routing_fields():
    """注册表是能力的单一权威: 可路由能力与偏好键一一对应、无重复;
    full_minute 为不可路由能力 (field=None, 仅 TickFlow Expert 提供)。"""
    routable = [c["field"] for c in CAPABILITY_REGISTRY if c["field"] is not None]
    assert sorted(routable) == sorted(DEFAULT_CURRENT)
    assert len(set(routable)) == len(routable)
    assert {c["id"] for c in CAPABILITY_REGISTRY} == {
        "realtime", "daily", "minute", "full_minute", "depth5", "adj_factor", "financial",
    }
    full_minute = next(c for c in CAPABILITY_REGISTRY if c["id"] == "full_minute")
    assert full_minute["field"] == "full_minute_data_provider"
    assert full_minute["tf_tier"] == "expert"
    for cap in CAPABILITY_REGISTRY:
        assert cap["default"] == "tickflow"
        assert cap["tf_tier"] in ("none", "starter", "pro", "expert")
        assert "follow" not in cap


def test_matrix_without_third_party_sources(monkeypatch):
    """无插件无自定义源: 每个能力只剩 TickFlow 候选, 默认路由全部生效。"""
    _fake_sources(monkeypatch, [])
    matrix = build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="expert")
    assert matrix["tickflow_tier"] == "expert"
    assert len(matrix["capabilities"]) == 7
    for cap in matrix["capabilities"]:
        names = [c["name"] for c in cap["candidates"]]
        assert names == ["tickflow"]
        assert cap["candidates"][0]["kind"] == "builtin"
        assert cap["tf_available"] is True
        assert cap["usable"] is True
        assert cap["pending"] == []
        assert cap["current"] == cap["effective"] == "tickflow"


def test_candidates_only_available_unready_goes_pending(monkeypatch):
    """候选只包含声明了该能力且可用的源; 未就绪插件进 pending 并携带原因。"""
    _fake_sources(
        monkeypatch,
        [
            {"name": "fuyao", "display_name": "fuyao", "datasets": ["realtime"],
             "available": True, "status": "ok"},
            {"name": "sdk", "display_name": "SDK", "datasets": ["daily", "minute"],
             "available": False, "status": "依赖未安装"},
        ],
        [{"name": "myhttp", "display_name": "MyHTTP", "datasets": ["financial", "realtime"]}],
    )
    caps = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="expert"))
    assert [c["name"] for c in caps["realtime"]["candidates"]] == ["tickflow", "fuyao", "myhttp"]
    assert [c["name"] for c in caps["daily"]["candidates"]] == ["tickflow"]
    assert [c["name"] for c in caps["daily"]["pending"]] == ["sdk"]
    assert caps["daily"]["pending"][0]["available"] is False
    assert caps["daily"]["pending"][0]["note"] == "依赖未安装"
    assert [c["name"] for c in caps["financial"]["candidates"]] == ["tickflow", "myhttp"]
    assert caps["financial"]["candidates"][1]["kind"] == "custom"
    assert [c["name"] for c in caps["adj_factor"]["candidates"]] == ["tickflow"]


def test_tickflow_candidates_filtered_by_tier(monkeypatch):
    """TickFlow 只出现在当前档位确实提供的能力候选里 (free: 仅日K)。"""
    _fake_sources(monkeypatch, [])
    caps = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="free"))
    assert caps["daily"]["tf_available"] is True
    assert [c["name"] for c in caps["daily"]["candidates"]] == ["tickflow"]
    for cap_id in ("realtime", "minute", "depth5", "adj_factor", "financial"):
        assert caps[cap_id]["tf_available"] is False
        assert [c["name"] for c in caps[cap_id]["candidates"]] == []
    # starter 解锁实时与除权, 分钟/五档/财务仍锁
    caps = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="starter"))
    assert caps["realtime"]["tf_available"] is True
    assert caps["adj_factor"]["tf_available"] is True
    assert caps["minute"]["tf_available"] is False
    assert caps["depth5"]["tf_available"] is False
    assert caps["financial"]["tf_available"] is False


def test_current_tickflow_unmet_tier_flagged(monkeypatch):
    """偏好仍指向 tickflow 但档位不足: current 不动, tf_available=False 供前端警示。"""
    _fake_sources(monkeypatch, [])
    cap = _by_id(
        build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="free"),
    )["realtime"]
    assert cap["current"] == cap["effective"] == "tickflow"
    assert cap["tf_available"] is False
    assert cap["usable"] is False
    assert [c["name"] for c in cap["candidates"]] == []


def test_usable_follows_effective_provider(monkeypatch):
    """usable 跟随生效源而非 TickFlow 套餐: 各页能力门控的统一判定。

    - 生效源是可用插件 → usable (即使 TickFlow 档位不足);
    - 生效源是未就绪插件 → 不可用 (即使有其他可用候选);
    - 除权独立路由, 档位门槛/插件可用性同样生效。
    """
    _fake_sources(
        monkeypatch,
        [
            {"name": "fuyao", "display_name": "fuyao", "datasets": ["realtime", "minute"],
             "available": True, "status": "ok"},
            {"name": "sdk", "display_name": "SDK", "datasets": ["daily", "adj_factor", "minute"],
             "available": False, "status": "依赖未安装"},
        ],
    )
    caps = _by_id(
        build_capability_matrix(
            dict(DEFAULT_CURRENT, realtime_data_provider="fuyao", minute_data_provider="sdk"),
            tickflow_tier="free",
        ),
    )
    # 路由到可用插件: TickFlow 档位不足不影响 usable
    assert caps["realtime"]["tf_available"] is False
    assert caps["realtime"]["usable"] is True
    # 路由到未就绪插件: 有可用候选 (fuyao) 也不算 usable
    minute = caps["minute"]
    assert [c["name"] for c in minute["candidates"]] == ["fuyao"]
    assert minute["usable"] is False
    # 除权默认路由 tickflow: 自身档位门槛 (starter+) 生效, free 档不可用
    # (独立路由, 不再随日K联动)
    assert caps["adj_factor"]["effective"] == "tickflow"
    assert caps["adj_factor"]["tf_available"] is False
    assert caps["adj_factor"]["usable"] is False
    # 除权显式路由到未就绪 sdk → 不可用 (有 tickflow 候选也不算, 日K不受影响)
    caps = _by_id(
        build_capability_matrix(
            dict(DEFAULT_CURRENT, adj_factor_provider="sdk"), tickflow_tier="expert",
        ),
    )
    assert [c["name"] for c in caps["adj_factor"]["candidates"]] == ["tickflow"]
    assert caps["adj_factor"]["effective"] == "sdk"
    assert caps["adj_factor"]["usable"] is False
    assert caps["daily"]["usable"] is True


def test_unknown_or_empty_tier_fails_closed(monkeypatch):
    """未知档 (探测缺失) 与空档按 none 处理: 仅全档位能力 (日K) 保留 TickFlow。"""
    _fake_sources(monkeypatch, [])
    for tier in ("", "unknown", None):
        matrix = build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier=tier)
        caps = _by_id(matrix)
        assert [c["name"] for c in caps["daily"]["candidates"]] == ["tickflow"]
        assert caps["realtime"]["tf_available"] is False
        assert caps["realtime"]["candidates"] == []
        assert caps["minute"]["tf_available"] is False


def test_adj_factor_routes_independently(monkeypatch):
    """除权独立路由 (跟随日K已下线): 显式切到声明除权的插件即生效, 与日K当前源无关。

    历史遗留值 same_as_daily 不再被矩阵特判 — 存量配置经 preferences getter
    按非法源回退 tickflow (getter 层自愈), 矩阵只信任注入值。
    """
    _fake_sources(
        monkeypatch,
        [{"name": "sdk", "display_name": "SDK", "datasets": ["adj_factor"],
          "available": True, "status": "ok"}],
    )
    # 日K走 tickflow, 除权显式走 sdk → 互不影响 (TickFlow none 档下插件照常可用)
    caps = _by_id(
        build_capability_matrix(
            dict(DEFAULT_CURRENT, adj_factor_provider="sdk"), tickflow_tier="none",
        ),
    )
    adj = caps["adj_factor"]
    assert adj["current"] == adj["effective"] == "sdk"
    assert adj["usable"] is True
    assert caps["daily"]["effective"] == "tickflow"
    assert caps["daily"]["usable"] is True


def test_depth5_capability_semantics(monkeypatch):
    """五档可独立路由到声明 depth5 的插件, 不受 TickFlow 档位限制。"""
    _fake_sources(
        monkeypatch,
        [{"name": "depth_src", "display_name": "Depth", "datasets": ["depth5"],
          "available": True, "status": "ok"}],
    )
    # pro 档: TickFlow 进候选, 默认路由 tickflow → usable
    cap = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="pro"))["depth5"]
    assert cap["tf_available"] is True
    assert [c["name"] for c in cap["candidates"]] == ["tickflow", "depth_src"]
    assert cap["usable"] is True
    # starter 档: TickFlow 不可供, 但显式路由到插件后仍可用
    current = dict(DEFAULT_CURRENT, depth5_data_provider="depth_src")
    cap = _by_id(build_capability_matrix(current, tickflow_tier="starter"))["depth5"]
    assert cap["tf_available"] is False
    assert [c["name"] for c in cap["candidates"]] == ["depth_src"]
    assert cap["effective"] == "depth_src"
    assert cap["usable"] is True


def test_unknown_current_display_falls_back_to_name(monkeypatch):
    """偏好指向未注册源 (正常经 getters 校验不会发生): 展示回退为原始名, 不抛异常。"""
    _fake_sources(monkeypatch, [])
    caps = _by_id(
        build_capability_matrix(
            dict(DEFAULT_CURRENT, realtime_data_provider="ghost"), tickflow_tier="expert",
        ),
    )
    assert caps["realtime"]["current"] == "ghost"
    assert caps["realtime"]["current_display"] == "ghost"
    assert caps["realtime"]["effective_display"] == "ghost"


def test_full_minute_routable_like_other_capabilities(monkeypatch):
    """全量分钟行: 与其他能力同样可路由 — 声明 full_minute 数据集的源进候选,
    路由到它则 usable=True (TickFlow 档位不足也不拦); 未路由且档位不足才不可用。"""
    _fake_sources(
        monkeypatch,
        [],
        [{"name": "myfm", "display_name": "MyFM", "datasets": ["full_minute"]}],
    )
    caps = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="expert"))
    fm = caps["full_minute"]
    assert fm["field"] == "full_minute_data_provider"
    assert [c["name"] for c in fm["candidates"]] == ["tickflow", "myfm"]
    assert fm["usable"] is True
    assert fm["tf_available"] is True
    assert fm["effective"] == "tickflow"

    # TickFlow 档位不足 (pro) 但路由到声明该数据集的自定义源 → 同样可用
    routed = dict(DEFAULT_CURRENT, full_minute_data_provider="myfm")
    caps_pro = _by_id(build_capability_matrix(routed, tickflow_tier="pro"))
    fm_pro = caps_pro["full_minute"]
    assert [c["name"] for c in fm_pro["candidates"]] == ["myfm"]
    assert fm_pro["usable"] is True
    assert fm_pro["tf_available"] is False
    assert fm_pro["effective"] == "myfm"

    # 档位不足且未路由 → 不可用
    caps_pro_default = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="pro"))
    fm_default = caps_pro_default["full_minute"]
    assert [c["name"] for c in fm_default["candidates"]] == ["myfm"]
    assert fm_default["usable"] is False


def test_api_endpoint_injects_all_routing_preferences(monkeypatch):
    """回归 (#301): get_capability_matrix 组装 current 必须覆盖注册表全部路由字段。

    full_minute_data_provider 曾漏传 → 矩阵 effective 恒回退默认 tickflow,
    低档位用户即使已路由自定义源, 卡片仍恒显「不可用」(真实路由读 preferences
    不受影响, 纯展示层)。此处捕获 API 层实际传给 build_capability_matrix 的
    current, 断言它与注册表路由字段一一对应且取自对应 getter。
    """
    from app.api import settings as settings_api
    from app.data_providers import capabilities as capabilities_mod
    from app.services import preferences
    from app.tickflow import policy

    getter_values = {
        "realtime_data_provider": "rt-src",
        "daily_data_provider": "daily-src",
        "minute_data_provider": "min-src",
        "full_minute_data_provider": "fm-src",
        "depth5_data_provider": "d5-src",
        "adj_factor_provider": "adj-src",
        "financial_data_provider": "fin-src",
    }
    getter_names = {
        "realtime_data_provider": "get_realtime_data_provider",
        "daily_data_provider": "get_daily_data_provider",
        "minute_data_provider": "get_minute_data_provider",
        "full_minute_data_provider": "get_full_minute_data_provider",
        "depth5_data_provider": "get_depth5_data_provider",
        "adj_factor_provider": "get_adj_factor_provider",
        "financial_data_provider": "get_financial_provider",
    }
    for field, getter in getter_names.items():
        monkeypatch.setattr(preferences, getter, lambda f=field: getter_values[f])
    monkeypatch.setattr(policy, "base_tier_name", lambda: "pro")

    captured: dict[str, Any] = {}

    def _spy(current, tickflow_tier="none", market="CN"):
        captured["current"] = dict(current)
        captured["market"] = market
        return build_capability_matrix(current, tickflow_tier=tickflow_tier, market=market)

    monkeypatch.setattr(capabilities_mod, "build_capability_matrix", _spy)
    settings_api.get_capability_matrix()

    routable_fields = {c["field"] for c in CAPABILITY_REGISTRY if c["field"] is not None}
    assert routable_fields == set(getter_values)
    # 注入完整: 每个路由字段都来自对应 getter, 不允许缺键 (缺键会在构建侧静默回退默认)
    assert captured["current"] == getter_values
    # 不传 market → CN (设置页现有调用零改动)
    assert captured["market"] == "CN"


def test_api_endpoint_full_minute_usable_follows_preference(monkeypatch):
    """端到端回归 (#301 用户场景): 低档位 + 已路由自定义源 → 全量分钟卡片可用。

    修复前: API 层漏传偏好, effective 恒 tickflow, 档位不足 → usable=False。
    """
    from app.api import settings as settings_api
    from app.services import preferences
    from app.tickflow import policy

    _fake_sources(
        monkeypatch,
        [],
        [{"name": "eltdx", "display_name": "ELTDX", "datasets": ["full_minute"]}],
    )
    for name in (
        "get_realtime_data_provider", "get_daily_data_provider",
        "get_minute_data_provider", "get_depth5_data_provider",
        "get_adj_factor_provider", "get_financial_provider",
    ):
        monkeypatch.setattr(preferences, name, lambda: "tickflow")
    monkeypatch.setattr(preferences, "get_full_minute_data_provider", lambda: "eltdx")
    monkeypatch.setattr(policy, "base_tier_name", lambda: "pro")

    fm = _by_id(settings_api.get_capability_matrix())["full_minute"]
    assert fm["effective"] == "eltdx"
    assert fm["usable"] is True
    assert fm["tf_available"] is False


# ============================================================
# market 维 (方案 §6): 注册表逐项声明市场支持度
# ============================================================

_HK_UNSUPPORTED = {"minute", "depth5", "full_minute"}
_HK_SUPPORTED = {"daily", "adj_factor", "financial", "realtime"}

#: 港股各能力的**固定生效源** (直连, 不走偏好路由)。逐项写死而不是统一断言
#: "eastmoney": 实时行情走腾讯 (东财 push2 集群 IP 级封禁), 源写错必须被测出来。
_HK_SOURCE = {
    "daily": "eastmoney",
    "adj_factor": "eastmoney",
    "financial": "eastmoney",
    "realtime": "tencent",
}


def test_registry_declares_every_market():
    """🔴 每个能力必须为**每个**市场声明支持度; 不支持必须给 reason。

    缺声明会在 build 侧被当成「不支持」(fail-closed), 但那是兜底不是设计:
    静默缺失会让新市场的能力在设置页整片显示成不可用, 排查成本极高。
    """
    from app.data_providers.capabilities import SUPPORTED_MARKETS, market_decl

    assert set(SUPPORTED_MARKETS) == {"CN", "HK"}
    for cap in CAPABILITY_REGISTRY:
        assert "markets" in cap, f"{cap['id']} 缺 markets 声明"
        for market in SUPPORTED_MARKETS:
            decl = market_decl(cap, market)
            assert "supported" in decl, f"{cap['id']}/{market} 缺 supported"
            if decl["supported"]:
                # 直连市场(非 ROUTED)声明支持时必须写清源, 否则前端显示可用却无从取数
                if market not in {"CN"}:
                    assert decl.get("source"), f"{cap['id']}/{market} 支持但未声明 source"
            else:
                assert decl.get("reason"), f"{cap['id']}/{market} 不支持但未给 reason"
                assert "注册表未声明" not in decl["reason"], f"{cap['id']}/{market} 声明缺失"


def test_missing_market_declaration_fails_closed(monkeypatch):
    """🔴 注册表漏声明某市场 → 判为不支持 + 可诊断 reason, 绝不 fail-open。

    静默缺失的后果是设置页显示「可用」、点进去是空数据; 这条测试钉住
    market_decl 的兜底分支 (完整注册表下永远不会走到, 所以必须单独构造)。
    """
    from app.data_providers.capabilities import market_decl

    _fake_sources(monkeypatch, [])
    cap = next(c for c in CAPABILITY_REGISTRY if c["id"] == "daily")

    # 整个 markets 键丢失
    monkeypatch.setitem(cap, "markets", {})
    decl = market_decl(cap, "HK")
    assert decl["supported"] is False
    assert "注册表未声明" in decl["reason"]
    assert "daily" in decl["reason"]

    row = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="expert", market="HK"))["daily"]
    assert row["market_supported"] is False
    assert row["usable"] is False
    assert "注册表未声明" in row["market_reason"]

    # 只丢一个市场 → 另一个市场不受影响
    monkeypatch.setitem(cap, "markets", {"CN": {"supported": True}})
    assert market_decl(cap, "CN")["supported"] is True
    assert market_decl(cap, "HK")["supported"] is False
    cn_row = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="expert"))["daily"]
    assert cn_row["market_supported"] is True


def test_matrix_defaults_to_cn(monkeypatch):
    """不传 market → CN, 且与显式传 CN 逐键相同(设置页现有调用零改动)。"""
    _fake_sources(monkeypatch, [])
    implicit = build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="expert")
    explicit = build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="expert", market="CN")
    assert implicit == explicit
    assert implicit["market"] == "CN"
    assert implicit["routed"] is True
    for cap in implicit["capabilities"]:
        assert cap["market"] == "CN"
        assert cap["routed"] is True
        assert cap["market_supported"] is True
        assert cap["market_reason"] is None
        assert cap["usable"] is True


def test_unknown_market_raises_instead_of_falling_back_to_cn(monkeypatch):
    """🔴 非法 market 必须抛, 不得静默回退 CN(方案 §7 / parse_market 同款教条)。"""
    from app.data_providers.capabilities import resolve_capability_sources

    _fake_sources(monkeypatch, [])
    for bad in ("XX", "cn ", "US", ""):
        with pytest.raises(ValueError):
            build_capability_matrix(dict(DEFAULT_CURRENT), market=bad)
        with pytest.raises(ValueError):
            resolve_capability_sources("expert", bad)


def test_hk_matrix_is_declarative_not_routed(monkeypatch):
    """港股不走偏好路由: routed=False, 无候选/无档位/无偏好字段, usable 直接取声明。"""
    _fake_sources(
        monkeypatch,
        [{"name": "fuyao", "display_name": "fuyao", "datasets": ["realtime", "daily"],
          "available": True, "status": "ok"}],
    )
    matrix = build_capability_matrix(
        dict(DEFAULT_CURRENT, daily_data_provider="fuyao"), tickflow_tier="expert", market="HK"
    )
    assert matrix["market"] == "HK"
    assert matrix["routed"] is False
    for cap in matrix["capabilities"]:
        assert cap["market"] == "HK"
        assert cap["routed"] is False
        # 港股没有偏好字段可路由, 也不吃 TickFlow 档位与插件声明
        assert cap["field"] is None
        assert cap["candidates"] == []
        assert cap["pending"] == []
        assert cap["tf_available"] is False
        assert cap["complement"] is None
        assert cap["usable"] is cap["market_supported"]


def test_hk_supported_capabilities_ignore_tickflow_tier(monkeypatch):
    """港股已落地的四项(日K/复权/财务/实时)在 TickFlow none 档下依然 usable。

    🔴 港股是直连市场, 生效源与 TickFlow 档位无关 —— none 档 (无 Key) 也必须可用,
    否则免费用户会看到「港股日K 不可用」这种与事实相反的提示。
    """
    _fake_sources(monkeypatch, [])
    caps = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="none", market="HK"))
    assert {cid for cid, c in caps.items() if c["market_supported"]} == _HK_SUPPORTED
    for cid in _HK_SUPPORTED:
        assert caps[cid]["usable"] is True
        assert caps[cid]["market_source"] == _HK_SOURCE[cid]
        assert caps[cid]["effective"] == _HK_SOURCE[cid]
        assert caps[cid]["market_reason"] is None


def test_hk_unsupported_capabilities_carry_reason(monkeypatch):
    """🔴 港股不支持的能力: usable=False + 非空 reason(前端隐藏入口并说明原因)。"""
    _fake_sources(monkeypatch, [])
    caps = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="expert", market="HK"))
    assert {cid for cid, c in caps.items() if not c["market_supported"]} == _HK_UNSUPPORTED
    for cid in _HK_UNSUPPORTED:
        assert caps[cid]["usable"] is False, f"{cid} 在港股必须不可用"
        assert caps[cid]["market_source"] is None
        assert caps[cid]["effective"] is None
        assert caps[cid]["effective_display"] is None
        reason = caps[cid]["market_reason"]
        assert isinstance(reason, str) and len(reason) >= 8, f"{cid} 的 reason 太短: {reason!r}"


def test_market_dim_is_hard_gate_even_for_routed_market(monkeypatch):
    """🔴 市场维是硬闸: CN 某能力被声明为不支持时, 路由到谁都不算可用。"""
    _fake_sources(monkeypatch, [])
    cap = next(c for c in CAPABILITY_REGISTRY if c["id"] == "daily")
    monkeypatch.setitem(cap["markets"]["CN"], "supported", False)
    monkeypatch.setitem(cap["markets"]["CN"], "reason", "测试用: 临时声明不支持")
    row = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT), tickflow_tier="expert"))["daily"]
    assert row["market_supported"] is False
    assert row["usable"] is False
    # 候选仍如实列出(设置页要显示「源是有的, 是这个市场不支持」), 但 usable 为假
    assert [c["name"] for c in row["candidates"]] == ["tickflow"]


def test_supported_without_source_in_direct_market_fails_closed(monkeypatch):
    """直连市场声明支持却漏填 source → 判为不支持并给出可诊断 reason。"""
    _fake_sources(monkeypatch, [])
    cap = next(c for c in CAPABILITY_REGISTRY if c["id"] == "daily")
    monkeypatch.setitem(cap["markets"]["HK"], "source", None)
    row = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT), market="HK"))["daily"]
    assert row["market_supported"] is False
    assert row["usable"] is False
    assert "未填 source" in row["market_reason"]


def test_resolve_capability_sources_hk(monkeypatch):
    """门禁视角同样带 market 维: 港股行的 source 是声明的直连源。"""
    from app.data_providers.capabilities import resolve_capability_sources

    _fake_sources(monkeypatch, [])
    rows = {r["id"]: r for r in resolve_capability_sources("none", "HK")}
    assert rows["daily"]["usable"] is True
    assert rows["daily"]["source"] == "eastmoney"
    assert rows["daily"]["market"] == "HK"
    assert rows["depth5"]["usable"] is False
    assert rows["depth5"]["source"] is None
    assert rows["depth5"]["market_reason"]
    # 港股不可路由 → field 一律 None, 前端不会渲染路由下拉
    assert all(r["field"] is None for r in rows.values())


def test_hk_realtime_is_tencent_direct_with_scope_note(monkeypatch):
    """🔴 港股实时行情: 源是**腾讯**不是东财, 且 note 必须写明覆盖范围。

    东财 push2 集群对本机 IP 级封禁 (2026-11 实测), 港股实时唯一可用源是
    ``qt.gtimg.cn`` (归一化在 ``plugins/tencent/hk_spot.py``)。把 source 写回
    eastmoney 会让设置页显示一个当前根本取不到数的源。

    note 非空且提到「未接入全市场轮询」是刻意的硬断言: 港股 realtime 目前只
    服务「按代码列表取数」(自选/详情), 声明成与 A 股等价的全市场快照会诱导
    后续代码去调不存在的全市场链路。范围说明被删掉 = 过度声明, 必须被测出来。
    """
    _fake_sources(monkeypatch, [])
    row = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT), market="HK"))["realtime"]
    assert row["market_supported"] is True
    assert row["market_source"] == "tencent"
    assert row["effective"] == "tencent"
    assert row["usable"] is True
    assert row["market_reason"] is None
    # 直连市场: 无偏好字段可路由, 前端不渲染下拉
    assert row["field"] is None
    assert row["routed"] is False
    assert row["candidates"] == []
    note = row["market_note"]
    assert isinstance(note, str) and note
    assert "hk_spot" in note
    assert "全市场轮询" in note


def test_hk_depth5_reason_records_zero_five_depth_evidence(monkeypatch):
    """港股五档不支持的 reason 要带上实测证据, 而不是笼统一句「没有源」。

    腾讯快照对港股**确实返回**五档字段位 (9-28), 只是值恒 0 —— 不写清楚会让
    后来人以为「换个源就有五档」, 反复去试同一个端点。
    """
    _fake_sources(monkeypatch, [])
    row = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT), market="HK"))["depth5"]
    assert row["market_supported"] is False
    assert "恒 0" in row["market_reason"]


def test_capability_gate_market(monkeypatch):
    """capability_gate 的日志标签要一眼看出「这个市场不支持」。"""
    from app.data_providers.capabilities import capability_gate
    _fake_sources(monkeypatch, [])
    ok, label = capability_gate("daily", "none", "HK")
    assert ok is True
    assert "日K" in label

    ok, label = capability_gate("depth5", "expert", "HK")
    assert ok is False
    assert "HK不支持" in label
    assert "五档盘口" in label

    # 未知能力仍然 fail-closed
    ok, label = capability_gate("ghost", "expert", "HK")
    assert ok is False
    assert "未知能力" in label


def test_api_endpoint_market_param(monkeypatch):
    """API: 缺省 CN / 显式 HK / 非法值 400(不静默回退)。"""
    from fastapi import HTTPException

    from app.api import settings as settings_api
    from app.services import preferences
    from app.tickflow import policy

    _fake_sources(monkeypatch, [])
    for name in (
        "get_realtime_data_provider", "get_daily_data_provider", "get_minute_data_provider",
        "get_full_minute_data_provider", "get_depth5_data_provider", "get_adj_factor_provider",
        "get_financial_provider",
    ):
        monkeypatch.setattr(preferences, name, lambda: "tickflow")
    monkeypatch.setattr(policy, "base_tier_name", lambda: "none")

    assert settings_api.get_capability_matrix()["market"] == "CN"
    assert settings_api.get_capability_matrix("hk")["market"] == "HK"
    with pytest.raises(HTTPException) as ei:
        settings_api.get_capability_matrix("XX")
    assert ei.value.status_code == 400
