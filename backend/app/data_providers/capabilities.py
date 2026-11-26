"""能力注册表与能力路由矩阵 — 数据集维度的单一权威定义。

能力 (capability) = 一个标准化数据集 (CONTRIBUTING「数据源插件化要求」):
daily / adj_factor / realtime / minute / depth5 / financial / full_minute (注册表顺序即设置页卡片顺序)。注册表集中声明每个
能力的展示元数据、路由偏好字段与 TickFlow 档位要求, 前端设置页不再各自硬编码。
depth5 与其他数据集一样可由插件声明并独立路由; 五档不可用时连板梯队封单/
看板封单通过 usable 给出缺数据提示。

build_capability_matrix 把注册表、插件/自定义源的能力声明 (datasets) 和当前
路由偏好合并为一个矩阵, 供设置页一次拉全。当前偏好由 API 层注入
(preferences getters 自带合法源校验), 本模块不反向依赖 services 层。

候选契约: 每个能力的 candidates 只包含「当前确实可提供该能力」的源 —
TickFlow 按当前订阅档位过滤 (日K全档位, 其余按注册表 tf_tier 门槛),
未就绪的插件/自定义源 (依赖未装/Key 未配) 放入 pending 并携带原因,
供前端置灰提示。其他页面可以把 candidates 直接当作可用提供方名单。

usable 契约: 每个能力额外给出 usable = 生效源当前能否真正提供该能力
(生效源在 candidates 中)。各页面的能力门控 (缺能力提示 → 数据源配置)
统一以 usable 为准, 而不是 TickFlow 套餐视角 — 路由到可用插件时同样可用,
路由到 TickFlow 但档位不足时同样不可用。

market 维 (方案 §6): 注册表每项**必须**为每个市场声明支持度 (``markets`` 键),
缺声明按不支持处理 (fail-closed)。市场分两类 —— 走偏好路由的 (CN, 见
:data:`ROUTED_MARKETS`) 与直连固定源的 (HK): 后者没有偏好字段可路由, 矩阵是
**声明式**的 (``routed=False``、candidates 恒空、``usable`` 直接取声明的
``supported``、``effective`` 取声明的 ``source``)。不支持的能力**必须**带
``reason``, 前端据此隐藏入口并说明原因, 而不是渲染一块必然为空的面板。

⚠️ 前端 `lib/kline-market-adapter.ts` 的 `KlineCapabilities` **不是**本注册表的
副本: 它描述详情弹窗的 UI 入口就绪度, 含 `watchlist`/`monitor`/`abnormal` 这类
没有后端能力 id 的纯前端语义, 无法由 `/capability-matrix` 推导。两侧唯一重叠的
是分钟链路(`minute`/`full_minute`), 以后端为准。改任一侧前先读那处的分工说明。
"""

from __future__ import annotations

from app.data_providers import custom as custom_sources
from app.market import Market

#: 注册表**必须**为每个市场逐项声明支持度 (方案 §6)。缺声明按「不支持」处理
#: (fail-closed), 并由 ``test_capability_matrix.py`` 钉住完整性 —— 静默缺失会让
#: 新市场的能力在设置页显示成「可用」, 点进去却是空数据。
SUPPORTED_MARKETS: tuple[str, ...] = tuple(m.value for m in Market)

#: 走**偏好路由**的市场: 能力生效源由 ``preferences`` 的 ``*_data_provider`` 决定,
#: 矩阵给出 candidates / pending / tf_available / complement。
#:
#: 🔴 港股**不在**其中: 港股各能力都是「直连固定源」(日K/复权/财务走东财, 指数走
#: 腾讯+新浪), 没有偏好字段可路由。对这类市场矩阵是**声明式**的 —— ``usable``
#: 直接等于注册表声明的 ``supported``, ``routed=False`` 告诉前端不要渲染路由下拉。
ROUTED_MARKETS: frozenset[str] = frozenset({Market.CN.value})

CAPABILITY_REGISTRY: list[dict] = [
    {
        "id": "daily",
        "label": "日K",
        "desc": "历史K线与实时覆写",
        "field": "daily_data_provider",
        "default": "tickflow",
        "tf_tier": "none",
        "markets": {
            "CN": {"supported": True},
            "HK": {
                "supported": True,
                "source": "eastmoney",
                "note": "直连东财 push2 → kline_hk_daily; 不走偏好路由",
            },
        },
    },
    {
        "id": "adj_factor",
        "label": "除权因子",
        "desc": "前复权计算基准",
        "field": "adj_factor_provider",
        "default": "tickflow",
        "tf_tier": "starter",
        # 独立路由 (曾经的「跟随日K」特殊值已下线: 每个能力单独配置,
        # 复权口径一致性改由未来的一致性警示保障, 不做路由耦合)
        "markets": {
            "CN": {"supported": True},
            "HK": {
                "supported": True,
                "source": "eastmoney",
                "note": (
                    "F10 分红事件的加性偏移(非乘性); 完整性凭据 "
                    "adj_factor_hk/coverage.json 逐标的拒算, 凭据缺失整轮中止"
                ),
            },
        },
    },
    {
        "id": "realtime",
        "label": "实时行情",
        "desc": "全市场实时快照",
        "field": "realtime_data_provider",
        "default": "tickflow",
        "tf_tier": "starter",
        "markets": {
            "CN": {"supported": True},
            "HK": {
                "supported": True,
                "source": "tencent",
                "note": (
                    "腾讯 qt.gtimg.cn 批量快照(≤300 只/请求, GBK), 归一化在 "
                    "plugins/tencent/hk_spot.py; 东财 push2 集群 IP 级封禁(2026-11 实测)"
                    "故不走东财。🔴 覆盖范围是「按代码列表取实时行情」(自选/详情), "
                    "尚未接入 quote_service 的全市场轮询与监控引擎; 港股该端点五档恒 0, "
                    "volume 单位是股不是手, amount 是元不是万元"
                ),
            },
        },
    },
    {
        "id": "minute",
        "label": "分钟K",
        "desc": "分时图与分钟回测",
        "field": "minute_data_provider",
        "default": "tickflow",
        "tf_tier": "pro",
        "markets": {
            "CN": {"supported": True},
            "HK": {
                "supported": False,
                "reason": (
                    "港股分钟链路未接入: 腾讯 ifzq 分钟单标的上限约 320 根且无复权口径, "
                    "不足以支撑分时图与分钟回测; 详情弹窗的分时入口对港股隐藏"
                ),
            },
        },
    },
    {
        "id": "depth5",
        "label": "五档盘口",
        "desc": "连板梯队封单与盘口深度",
        "field": "depth5_data_provider",
        "default": "tickflow",
        "tf_tier": "pro",
        "markets": {
            "CN": {"supported": True},
            "HK": {
                "supported": False,
                "reason": (
                    "港股无五档盘口源: 腾讯 qt.gtimg.cn 快照的五档字段(9-28)对港股恒 0 "
                    "(2026-11 实测 hk00700/hk09988/hkHSI 三行均为 0), 东财 push2 被封; "
                    "且连板/涨停/封单是 A 股独有语义(港股无涨跌停), "
                    "相关 enriched 列对港股恒 null 不伪造"
                ),
            },
        },
    },
    {
        "id": "financial",
        "label": "财务数据",
        "desc": "财务指标与三大报表",
        "field": "financial_data_provider",
        "default": "tickflow",
        "tf_tier": "expert",
        "markets": {
            "CN": {"supported": True},
            "HK": {
                "supported": True,
                "source": "eastmoney",
                "note": (
                    "datacenter-web F10 主要指标 + 三大报表(push2 被封不影响该主机); "
                    "🔴 报告货币非统一 CNY, 估值须按 CURRENCY/IS_CNY_CODE 显式判定"
                ),
            },
        },
    },
    {
        "id": "full_minute",
        "label": "全量分钟",
        "desc": "盘中全市场当日分钟落盘 (冷启动全天 + 标的池增量)",
        "field": "full_minute_data_provider",
        "default": "tickflow",
        "tf_tier": "expert",
        # TickFlow 侧需 Expert 档; 插件/自定义源声明 full_minute 数据集即可提供
        # (插件实现 get_intraday_batch / 可选 get_intraday_latest, YAML 仅修复轮)
        "markets": {
            "CN": {"supported": True},
            "HK": {
                "supported": False,
                "reason": "同「分钟K」: 港股无分钟落盘链路, 盘中全量同步无从谈起",
            },
        },
    },
]

_TICKFLOW_CANDIDATE = {
    "name": "tickflow",
    "display": "TickFlow",
    "kind": "builtin",
    "available": True,
    "status": "ok",
    "note": None,
}

# 档位排序: none 最低 (无 Key/无效 Key, 仅免费通道历史日K), 未知档按 none 处理 (fail-closed)
_TIER_RANK = {"none": -1, "free": 0, "starter": 1, "pro": 2, "expert": 3}


def _tier_base(tier: str) -> str:
    """归一化档位输入为基础名: "Pro +" -> "pro"; 空值归为 none。"""
    text = str(tier or "").strip().lower()
    if not text:
        return "none"
    return text.split()[0].split("+")[0]


def market_decl(cap: dict, market: str) -> dict:
    """某能力在某市场的声明。

    🔴 **缺声明按「不支持」处理**(fail-closed), 并带上可诊断的 reason —— 静默
    缺失会让新市场的能力在设置页显示成可用, 点进去却是空数据。完整性由
    ``test_capability_matrix.py::test_registry_declares_every_market`` 钉住。
    """
    decl = (cap.get("markets") or {}).get(market)
    if not isinstance(decl, dict):
        return {
            "supported": False,
            "reason": f"注册表未声明 {market} 市场的支持度(能力 {cap.get('id')})",
        }
    return decl


def _declared_sources() -> list[dict]:
    """插件 + 自定义源 → 统一能力声明视图。未注册 (hidden/加载失败) 的源不会出现。"""
    rows: list[dict] = []
    for plugin in custom_sources.list_plugins():
        rows.append({
            "name": plugin["name"],
            "display": plugin.get("display_name") or plugin["name"],
            "datasets": set(plugin.get("datasets") or []),
            "available": bool(plugin.get("available")),
            "status": str(plugin.get("status") or ""),
            "kind": "plugin",
        })
    for source in custom_sources.list_sources():
        rows.append({
            "name": source["name"],
            "display": source.get("display_name") or source["name"],
            "datasets": set(source.get("datasets") or []),
            # 自定义源注册即已通过加载校验, 视为可用
            "available": True,
            "status": "ok",
            "kind": "custom",
        })
    return rows


def _display_of(sources: list[dict], name: str) -> str:
    if name == "tickflow":
        return "TickFlow"
    for s in sources:
        if s["name"] == name:
            return s["display"]
    return name


def tickflow_native_serves(cap_id: str, tier: str) -> bool:
    """P1-12 layer1: TickFlow 档位是否原生包含某能力 (不看路由偏好、不看互补源)。

    cap_id 用注册表 id (daily/adj_factor/realtime/minute/depth5/financial/full_minute)。
    供无 capset 的取数点 (如分钟源解析) 判断「TickFlow 自己顶不顶得上」,
    顶不上才轮到静态互补源接管。未知能力按不可用处理 (fail-closed)。
    """
    for cap in CAPABILITY_REGISTRY:
        if cap["id"] == cap_id:
            return _TIER_RANK.get(_tier_base(tier), -1) >= _TIER_RANK[cap["tf_tier"]]
    return False


def build_capability_matrix(
    current: dict[str, str], tickflow_tier: str = "none", market: str = Market.CN.value
) -> dict:
    """注册表 + 源能力声明 + 当前偏好 → 能力路由矩阵。

    current 为 {偏好字段: 当前值}, 由 API 层经 preferences getters 注入;
    getters 已把非法值 (未注册源) 回退为默认, 这里直接信任。effective
    即当前值本身 (每个能力独立路由, 无跟随/派生特殊值)。

    tickflow_tier 为 TickFlow 当前档位基础名 (none/free/starter/pro/expert),
    由 API 层从 tickflow policy 注入。当前档位不提供的能力里 TickFlow
    不进候选, 但偏好仍指向 tickflow 时以 tf_available=False 标记,
    供前端提示「档位不足」。未知档按 none 处理。

    market 维度 (方案 §6)
    ---------------------

    ``market`` 必须是 :data:`SUPPORTED_MARKETS` 之一, 否则 ``ValueError`` ——
    🔴 **刻意不兜底 CN**: 传了非法 market 却静默返回 A 股矩阵, 用户会以为看到的
    是港股能力 (与 ``app.market.parse_market`` 同一条教条)。

    市场分两类, 由 :data:`ROUTED_MARKETS` 区分:

    * **走偏好路由** (CN): ``current``/``effective``/``candidates``/``pending``/
      ``tf_available``/``complement`` 全部按路由偏好与源声明计算, ``usable`` =
      生效源当前能否真正提供 **且** 注册表声明该市场支持。
    * **直连固定源** (HK): 没有偏好字段可路由, 矩阵是**声明式**的 ——
      ``routed=False``、``candidates``/``pending`` 恒空、``tf_available=False``
      (TickFlow 不服务港股)、``effective`` = 注册表声明的 ``source``,
      ``usable`` = 注册表声明的 ``supported``。前端据 ``routed=False`` 不渲染
      路由下拉, 据 ``market_reason`` 说明「为什么没有」。

    🔴 不支持的能力**必须**带 ``reason`` (方案 §6:「明确声明不支持 + 前端隐藏 +
    说明原因, 不显示假数据」), 由测试钉住。
    """
    if market not in SUPPORTED_MARKETS:
        raise ValueError(
            f"能力矩阵: 未知市场 {market!r} (支持: {', '.join(SUPPORTED_MARKETS)})"
        )
    routed = market in ROUTED_MARKETS
    tier_base = _tier_base(tickflow_tier)
    tier_rank = _TIER_RANK.get(tier_base, -1)
    sources = _declared_sources()

    capabilities = []
    for cap in CAPABILITY_REGISTRY:
        decl = market_decl(cap, market)
        supported = bool(decl.get("supported"))
        reason = decl.get("reason")
        market_source = decl.get("source")
        if supported and not routed and not market_source:
            # 直连市场声明支持却没写源 = 注册表漏填; 前端会显示「可用」却无从取数。
            reason = f"注册表声明 {market} 支持但未填 source (能力 {cap['id']})"
            supported = False

        if routed:
            # field=None → 不可路由能力 (仅 TickFlow 提供, 无路由偏好), 生效源恒为默认
            effective = current.get(cap["field"], cap["default"]) if cap["field"] else cap["default"]
            tf_available = tier_rank >= _TIER_RANK[cap["tf_tier"]]
            candidates: list[dict] = []
            pending: list[dict] = []
            if tf_available:
                candidates.append(dict(_TICKFLOW_CANDIDATE))
            for s in sources:
                if cap["id"] not in s["datasets"]:
                    continue
                entry = {
                    "name": s["name"],
                    "display": s["display"],
                    "kind": s["kind"],
                    "available": s["available"],
                    "status": s["status"],
                    "note": None if s["available"] else (s["status"] or "不可用"),
                }
                (candidates if s["available"] else pending).append(entry)
            usable = any(c["name"] == effective for c in candidates)
            # P1-12 layer1: 生效源顶不上时查免费内置互补源; 找到则能力实际可用
            # (取数点会路由过去), 前端可据 complement 字段展示「由 XX 静态互补」。
            complement = None
            if not usable:
                complement = custom_sources.find_complement_provider(cap["id"], exclude=effective)
                usable = complement is not None
            # 🔴 市场维是**硬闸**: 该市场不支持时, 路由到谁都不算可用。
            usable = usable and supported
        else:
            effective = market_source
            tf_available = False
            candidates = []
            pending = []
            complement = None
            usable = supported

        capabilities.append({
            "id": cap["id"],
            "label": cap["label"],
            "desc": cap["desc"],
            "field": cap["field"] if routed else None,
            "default": cap["default"],
            "tf_tier": cap["tf_tier"],
            "tf_available": tf_available,
            "usable": usable,
            "complement": complement,
            "complement_display": _display_of(sources, complement) if complement else None,
            "current": effective,
            "current_display": _display_of(sources, effective) if effective else None,
            "effective": effective,
            "effective_display": _display_of(sources, effective) if effective else None,
            "candidates": candidates,
            "pending": pending,
            # ── market 维 ──
            "market": market,
            "routed": routed,
            "market_supported": supported,
            "market_source": market_source,
            "market_reason": None if supported else reason,
            "market_note": decl.get("note"),
        })
    return {
        "tickflow_tier": tier_base,
        "market": market,
        "routed": routed,
        "capabilities": capabilities,
    }


# ---------------------------------------------------------------------------
# 门禁视角 (面向各服务的启动/调度判断与日志)
# ---------------------------------------------------------------------------

def _cap_specs_by_id() -> dict[str, dict]:
    return {c["id"]: c for c in CAPABILITY_REGISTRY}


def _current_routing() -> dict[str, str]:
    """偏好字段 → 当前生效源 (经 preferences getters, 非法值已回退默认)。

    与 api/settings.py 的 capability-matrix 端点同一组 getter; 显式列全,
    不用 getattr 拼函数名 (拼错会静默回退, 难排查)。
    """
    from app.services import preferences

    return {
        "realtime_data_provider": preferences.get_realtime_data_provider(),
        "daily_data_provider": preferences.get_daily_data_provider(),
        "minute_data_provider": preferences.get_minute_data_provider(),
        "full_minute_data_provider": preferences.get_full_minute_data_provider(),
        "depth5_data_provider": preferences.get_depth5_data_provider(),
        "adj_factor_provider": preferences.get_adj_factor_provider(),
        "financial_data_provider": preferences.get_financial_provider(),
    }


def resolve_capability_sources(
    tickflow_tier: str = "none", market: str = Market.CN.value
) -> list[dict]:
    """每个能力一行: 生效源 + 展示名 + 该源当前能否真正提供 (usable)。

    与 build_capability_matrix 同口径 (复用同一 candidates 组装), 供服务层
    的门禁判断与日志标签使用 —— 门禁必须看「生效源能否提供」, 而不是只看
    TickFlow 套餐 (路由到插件时 TickFlow 档位无关; 同理路由到 TickFlow 而
    档位不足时, 插件声明了也不该被算作可用)。

    ``market`` 语义与 :func:`build_capability_matrix` 一致: 非路由市场 (HK) 的
    ``usable`` 直接取注册表声明, ``source`` 为声明的直连源 (可能为 None)。

    返回 [{id, label, field, source, source_display, usable, tf_available,
    market, market_supported, market_reason}]。
    """
    if market not in SUPPORTED_MARKETS:
        raise ValueError(
            f"能力门禁: 未知市场 {market!r} (支持: {', '.join(SUPPORTED_MARKETS)})"
        )
    routed = market in ROUTED_MARKETS
    tier_rank = _TIER_RANK.get(_tier_base(tickflow_tier), -1)
    sources = _declared_sources()
    current = _current_routing() if routed else {}

    rows: list[dict] = []
    for cap in CAPABILITY_REGISTRY:
        decl = market_decl(cap, market)
        supported = bool(decl.get("supported"))
        reason = decl.get("reason")
        market_source = decl.get("source")
        if supported and not routed and not market_source:
            reason = f"注册表声明 {market} 支持但未填 source (能力 {cap['id']})"
            supported = False

        field = cap.get("field")
        if not routed:
            rows.append({
                "id": cap["id"],
                "label": cap["label"],
                "field": None,
                "source": market_source,
                "source_display": _display_of(sources, market_source) if market_source else None,
                "usable": supported,
                "tf_available": False,
                "complement": None,
                "complement_display": None,
                "market": market,
                "market_supported": supported,
                "market_reason": None if supported else reason,
            })
            continue
        if not field:
            continue
        effective = current.get(field, cap["default"])
        tf_available = tier_rank >= _TIER_RANK[cap["tf_tier"]]
        provider_names = {"tickflow"} if tf_available else set()
        provider_names |= {
            s["name"] for s in sources
            if cap["id"] in s["datasets"] and s["available"]
        }
        usable = effective in provider_names
        # P1-12 layer1: 生效源自己顶不上时, 查免费内置互补源 (注册表 id 即数据集名)。
        # 找到 → 该能力实际可用 (取数点会路由到互补源), usable 放宽并单列 complement
        # 字段, 门禁日志/前端可明示「由谁顶替」, 不静默。
        complement = None
        if not usable:
            complement = custom_sources.find_complement_provider(cap["id"], exclude=effective)
            usable = complement is not None
        rows.append({
            "id": cap["id"],
            "label": cap["label"],
            "field": field,
            "source": effective,
            "source_display": _display_of(sources, effective),
            # 🔴 市场维是硬闸: 该市场不支持时路由到谁都不算可用。
            "usable": usable and supported,
            "tf_available": tf_available,
            "complement": complement,
            "complement_display": _display_of(sources, complement) if complement else None,
            "market": market,
            "market_supported": supported,
            "market_reason": None if supported else reason,
        })
    return rows


def capability_gate(
    cap_id: str, tickflow_tier: str | None = None, market: str = Market.CN.value
) -> tuple[bool, str]:
    """单个能力的门禁: (是否可用, 日志标签)。

    标签形如 "日K ← fuyao" / "财务数据 ← TickFlow(档位不足)" /
    "五档盘口 ← HK不支持(港股无五档盘口源…)", 让「为什么没跑」在日志里一眼可读,
    而不必回设置页对照路由。

    可用性以生效源为准 (与设置页 usable 同口径): 路由到已声明的插件即可用,
    路由到 TickFlow 但档位不够则不可用。市场不支持时**一律不可用**并给出原因。
    """
    if tickflow_tier is None:
        from app.tickflow import policy
        tickflow_tier = policy.base_tier_name()

    for row in resolve_capability_sources(tickflow_tier, market):
        if row["id"] == cap_id:
            if not row["market_supported"]:
                return False, f"{row['label']} ← {market}不支持({row['market_reason']})"
            # P1-12 layer1: 生效源顶不上、由免费内置互补源接管时, 标签直指互补源,
            # 让「为什么没跑/谁在顶」在日志里一眼可读。
            if row.get("complement"):
                return True, f"{row['label']} ← {row['complement_display']}(静态互补)"
            label = f"{row['label']} ← {row['source_display']}"
            if not row["usable"] and row["source"] == "tickflow" and not row["tf_available"]:
                label += "(档位不足)"
            elif not row["usable"]:
                label += "(当前源不可用)"
            return row["usable"], label
    return False, f"{cap_id}(未知能力)"
