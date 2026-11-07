"""P1-15: 声明式模型能力表 (学 astock capabilities.py)。

把各厂商/模型的"怪癖"收进一张不可变小表, 避免 ai_provider 每接一个新模型
就长一堆 if 和子串特判 (此前散点: _SEARCH_UNSUPPORTED_MODEL_HINTS、
_DEEPSEEK_V4_MODELS + hostname 判断等, 均已收编到本表)。

设计原则:
- 表里只登记已确认的事实 (官方文档明确 / 本项目实测过), 不做猜测性登记;
- 未登记模型返回保守默认值 (能力全开、无怪癖), 行为与建表前完全一致;
  意外被上游拒绝的参数仍由 ai_provider._openai_retry_kwargs 事后自适应
  重试兜底 (只在 400 明确指认参数时移除, 不靠模型名猜);
- 条目只增不改语义: 新模型有怪癖 → 在这里补一行登记, 调用链不用动;
- 合并语义: 按表内顺序, 命中的规则逐字段覆盖 (后命中优先),
  known_issues 累积去重。同族模型 (如 deepseek-v4 命中 "deepseek")
  自动继承家族级登记。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelCapabilities:
    """单个模型 (或家族) 的能力/怪癖登记项。全部字段都有保守默认值。"""

    # 是否支持 enable_search 请求参数 (OpenAI 兼容协议联网搜索开关)。
    # False 时请求不传该参数, 提示词也退回无联网变体 (web_search_supported)。
    supports_web_search: bool = True
    # 是否支持 temperature 请求参数。thinking 系模型 (kimi-k2.6/k2.7/k3 等)
    # 温度固定 (官方文档), 传其他值报 400, 建议不显式传 → False 时请求不带。
    supports_temperature: bool = True
    # 模型是否默认开启思考模式 (如 DeepSeek V4 官方 API)。
    default_thinking: bool = False
    # 思考 token 是否计入 max_tokens 预算 — 计入时显式限长会挤占正文
    # 甚至全部吃光 (正文 0 字 + finish=length), 长分析类调用应放开限制。
    thinking_shares_max_tokens: bool = False
    # 人类可读的已知怪癖清单 (设置页档案展示用)。
    known_issues: tuple[str, ...] = ()
    # 是否命中了登记表 (未登记模型为 False, 前端据此决定是否展示)。
    registered: bool = False


@dataclass(frozen=True)
class _Rule:
    """一条登记规则: 模型名子串 + (可选) base_url 主机名限定。"""

    model_hint: str  # 规范化 (strip+lower) 后的模型名子串匹配
    host: str = ""  # 非空 = 还要求 base_url 主机名精确相等 (怪癖仅限官方 API 时)
    caps: ModelCapabilities = ModelCapabilities()


# 登记表 (顺序即合并优先级, 后命中的规则覆盖先命中的字段):
_TABLE: tuple[_Rule, ...] = (
    # DeepSeek 官方聊天 API 无 enable_search 参数 (联网需 /v1/tools/search
    # 自行编排), 全家族命中即不传该参数。
    _Rule(
        "deepseek",
        caps=ModelCapabilities(
            supports_web_search=False,
            known_issues=("官方聊天 API 不支持 enable_search 联网参数",),
        ),
    ),
    # 推理型模型 (deepseek-reasoner 系等): 思考 token 计入 max_tokens 预算,
    # 显式限制会挤占正文 (见 generate_ai_text 文档), 同样无 enable_search。
    _Rule(
        "reasoner",
        caps=ModelCapabilities(
            supports_web_search=False,
            thinking_shares_max_tokens=True,
            known_issues=("思考 token 计入 max_tokens 预算, 长分析类调用应放开输出限制",),
        ),
    ),
    # DeepSeek V4 官方 API: 默认开启思考模式, 思考与正文共享 max_tokens —
    # 报告类任务 (prefer_final_answer) 显式禁用思考, 防止推理吃光预算后
    # 正文 0 字。host 限定官方域名: 第三方网关对 thinking extra_body 的
    # 支持不一, 不下发 (被拒时也有 _openai_retry_kwargs 兜底)。
    _Rule(
        "deepseek-v4-flash",
        host="api.deepseek.com",
        caps=ModelCapabilities(
            supports_web_search=False,
            default_thinking=True,
            thinking_shares_max_tokens=True,
            known_issues=("默认思考模式; 报告类任务会显式禁用思考防止挤占正文预算",),
        ),
    ),
    _Rule(
        "deepseek-v4-pro",
        host="api.deepseek.com",
        caps=ModelCapabilities(
            supports_web_search=False,
            default_thinking=True,
            thinking_shares_max_tokens=True,
            known_issues=("默认思考模式; 报告类任务会显式禁用思考防止挤占正文预算",),
        ),
    ),
    # Kimi thinking 系 temperature 固定 (官方模型参数参考): k2.6 思考 1.0 /
    # 非思考 0.6, k2.7-code 与 k3 固定 1.0, 传其他值报错, 建议不显式传入 → 不传。
    _Rule(
        "kimi-k2.6",
        caps=ModelCapabilities(
            supports_temperature=False,
            known_issues=("temperature 固定 (思考 1.0 / 非思考 0.6), 请求不传该参数",),
        ),
    ),
    _Rule(
        "kimi-k2.7",
        caps=ModelCapabilities(
            supports_temperature=False,
            known_issues=("temperature 固定 1.0, 请求不传该参数",),
        ),
    ),
    _Rule(
        "kimi-k3",
        caps=ModelCapabilities(
            supports_temperature=False,
            known_issues=("temperature 固定 1.0, 请求不传该参数",),
        ),
    ),
)


def lookup(model: str, base_url: str = "") -> ModelCapabilities:
    """按模型名查能力表; 未登记模型返回保守默认值 (与建表前行为一致)。

    匹配规则: 模型名 strip+lower 后做子串匹配; 规则带 host 时还要求
    base_url 的 hostname 精确相等 (大小写不敏感)。多条命中按表内顺序
    逐字段覆盖, known_issues 累积去重。
    """
    normalized = (model or "").strip().lower()
    if not normalized:
        return ModelCapabilities()
    url = (base_url or "").strip()
    host = (urlsplit(url).hostname or "").lower() if url else ""
    merged = ModelCapabilities()
    issues: list[str] = []
    for rule in _TABLE:
        if rule.model_hint not in normalized:
            continue
        if rule.host and rule.host != host:
            continue
        merged = replace(
            merged,
            supports_web_search=rule.caps.supports_web_search,
            supports_temperature=rule.caps.supports_temperature,
            default_thinking=rule.caps.default_thinking,
            thinking_shares_max_tokens=rule.caps.thinking_shares_max_tokens,
            registered=True,
        )
        issues.extend(i for i in rule.caps.known_issues if i not in issues)
    return replace(merged, known_issues=tuple(issues))


def describe(model: str, base_url: str = "") -> dict:
    """给设置页展示用: 模型在能力表中的登记情况 (未登记 registered=False)。"""
    caps = lookup(model, base_url)
    return {
        "model": (model or "").strip(),
        "registered": caps.registered,
        "supports_web_search": caps.supports_web_search,
        "supports_temperature": caps.supports_temperature,
        "default_thinking": caps.default_thinking,
        "thinking_shares_max_tokens": caps.thinking_shares_max_tokens,
        "known_issues": list(caps.known_issues),
    }
