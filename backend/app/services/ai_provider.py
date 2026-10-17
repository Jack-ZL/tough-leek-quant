"""AI provider adapter for OpenAI-compatible APIs and local Codex CLI."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from types import TracebackType
from urllib.parse import urlsplit, urlunsplit

from app import secrets_store
from app.config import settings

OPENAI_COMPAT_PROVIDER = "openai_compat"
OPENAI_PROVIDER = "openai"
CODEX_CLI_PROVIDER = "codex_cli"
CODEX_DEFAULT_COMMAND = "codex"
CODEX_SUPPORTED_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
OPENAI_DEFAULT_REASONING_EFFORT = "high"
# OpenAI 系两种请求协议: chat.completions (兼容面最广) 与 Responses (官方新协议)。
CHAT_COMPLETIONS_PROTOCOL = "chat_completions"
RESPONSES_PROTOCOL = "responses"
_SUPPORTED_PROTOCOLS = {CHAT_COMPLETIONS_PROTOCOL, RESPONSES_PROTOCOL}

_CODEX_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "TEMP",
    "TMP",
    "TMPDIR",
    "SHELL",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)

Message = dict[str, str]

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# ----------------------------------------------------------------
# 用户 focus 输入规范化。交易建议类表达不会被静默丢弃,而是由统一提示词
# 转换成客观价位、风险和情景分析,避免历史报告显示了 focus、模型却没有收到。
# ----------------------------------------------------------------
_FOCUS_BLOCKLIST = re.compile(
    r"买入|卖出|加仓|减仓|轻仓|重仓|半仓|全仓|仓位|止损|止盈|"
    r"操作建议|买卖点|买卖区间|建仓|平仓|清仓|调仓|"
    r"追高|低吸|反包|抄底|逃顶|进攻|防守|"
    r"激进|稳健|保守|目标价|能涨|会跌|预测涨|预测跌|"
    r"荐股|推荐买|推荐卖|值得投资|现在买|可以买|能买|要不要买|买吗|卖吗|"
    r"明日基调|交易计划|下单",
    re.IGNORECASE,
)


def sanitize_focus(focus: str) -> str:
    """规范化 focus 中的首尾空白与连续换行。"""
    if not focus:
        return ""
    text = re.sub(r"\s+", " ", focus).strip()
    return text


def build_focus_instruction(focus: str, *, report_name: str = "分析报告") -> str:
    """构建所有报告共用的关注重点指令。

    有关注点时要求模型在固定报告结构之前先直接回应。若原问题涉及交易
    建议,保留问题语义但要求转换成中立的数据分析,不再无提示地整段丢弃。
    """
    text = sanitize_focus(focus)
    if not text:
        return ""

    lines = [
        "## 用户关注重点(必须优先回应)",
        f"用户关注: {text}",
        f"请在完整{report_name}最前面先输出 `### 0. 🔎 关注重点回应`,"
        "用 2-4 条带具体数据的结论直接回应;随后继续完成既定报告结构,"
        "并在相关章节加深分析。不要只复述问题。",
    ]
    if _FOCUS_BLOCKLIST.search(text):
        lines.append(
            "该关注点含有买卖、仓位、目标价或预测类表达。不得给出相应操作结论;"
            "请将其转换为客观的技术/财务状态、关键价位、风险因素和条件情景后回应。"
        )
    return "\n".join(lines)


def current_ai_provider() -> str:
    return secrets_store.get_ai_config("ai_provider", settings.ai_provider) or OPENAI_COMPAT_PROVIDER


def current_ai_protocol() -> str:
    """当前 OpenAI 系请求协议: chat_completions (默认) | responses。

    仅对 openai/openai_compat 两种 provider 有意义; Codex CLI 走本地子进程,
    恒为 chat_completions。非法存储值一律回退默认, 不抛错。
    """
    if is_codex_cli_provider():
        return CHAT_COMPLETIONS_PROTOCOL
    value = str(
        secrets_store.get_ai_config("ai_protocol", settings.ai_protocol) or ""
    ).strip().lower()
    return value if value in _SUPPORTED_PROTOCOLS else CHAT_COMPLETIONS_PROTOCOL


def is_responses_protocol() -> bool:
    return current_ai_protocol() == RESPONSES_PROTOCOL


def current_openai_model() -> str:
    return secrets_store.get_ai_config("ai_model", settings.ai_model)


def current_codex_model() -> str:
    stored = secrets_store.load()
    model = stored.get("ai_codex_model")
    # 旧版本的两种 provider 共用 ai_model。仅在旧配置仍启用 Codex 时回退读取,
    # 避免把正常的 OpenAI-compatible 模型误当作 Codex 模型。
    if model is None and current_ai_provider() == CODEX_CLI_PROVIDER:
        model = stored.get("ai_model")
    return normalize_codex_model(str(model or ""))


def current_ai_model() -> str:
    if current_ai_provider() == CODEX_CLI_PROVIDER:
        return current_codex_model()
    return current_openai_model()


def current_openai_reasoning_effort() -> str:
    stored = secrets_store.load()
    if "ai_reasoning_effort" not in stored:
        return OPENAI_DEFAULT_REASONING_EFFORT
    return str(stored.get("ai_reasoning_effort") or "").strip()


def current_ai_max_output_tokens() -> int:
    """当前 AI 输出上限 (max_tokens): secrets.json 优先, 否则 config 默认。"""
    return secrets_store.get_ai_config_int("ai_max_output_tokens", settings.ai_max_output_tokens)


def current_ai_context_window() -> int:
    """当前 AI 输入上下文窗口上限 (约 token): secrets.json 优先, 否则 config 默认。"""
    return secrets_store.get_ai_config_int("ai_context_window", settings.ai_context_window)


def current_ai_round_checkpoint() -> int:
    """AI 助手工具轮次检查点 (0=不检查): secrets.json 优先, 否则 config 默认。"""
    return secrets_store.get_ai_config_int("ai_round_checkpoint", settings.ai_round_checkpoint)


def current_ai_web_search() -> bool:
    """用户开关: 聊天请求是否要求联网搜索 (enable_search)。"""
    return secrets_store.get_ai_config_bool("ai_web_search", settings.ai_web_search)


# DeepSeek 官方聊天 API 无 enable_search 参数 (联网需 /v1/tools/search 自行编排),
# 命中该关键字时不传参数, 提示词也退回无联网变体。
_SEARCH_UNSUPPORTED_MODEL_HINTS = ("deepseek",)


def web_search_supported() -> bool:
    """当前配置下能否真正启用联网搜索 (请求参数与提示词变体共用同一判定)。

    条件: 开关开启 + OpenAI 系 chat_completions 协议 + 模型不在不支持名单。
    Responses 协议的联网走 tools=[{"type":"web_search"}], 暂未接入;
    网关若拒绝 enable_search, 由 _openai_retry_kwargs 移除该参数后重试。
    """
    if not current_ai_web_search():
        return False
    if is_codex_cli_provider() or is_responses_protocol():
        return False
    model = current_ai_model().strip().lower()
    return not any(h in model for h in _SEARCH_UNSUPPORTED_MODEL_HINTS)


# 厂商关键词映射: 输入端友好叫法 (中英文/别名) → 厂商标识。
# 档案归属哪个厂商由其 名称/模型名/codex模型/API地址/服务商 字段中出现的关键词判定,
# 因此消息里写 "千问"、"qwen"、"智谱"、"glm" 等都能命中对应档案, 无需与配置名完全一致。
_VENDOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "qwen": ("qwen", "千问", "通义", "tongyi", "dashscope"),
    "deepseek": ("deepseek", "深度求索"),
    "kimi": ("kimi", "moonshot"),
    "glm": ("glm", "智谱", "zhipu", "chatglm", "bigmodel"),
    "doubao": ("doubao", "豆包", "volcengine", "火山引擎"),
    "claude": ("claude", "anthropic"),
    "gpt": ("chatgpt", "gpt", "openai"),
    "gemini": ("gemini", "google"),
    "grok": ("grok",),
    "minimax": ("minimax", "abab"),
    "hunyuan": ("hunyuan", "混元"),
    "ernie": ("ernie", "文心", "qianfan", "千帆"),
    "baichuan": ("baichuan", "百川"),
    "stepfun": ("stepfun", "阶跃"),
    "codex": ("codex",),
}


def _profile_vendors(prof: dict) -> set[str]:
    """根据档案字段判定其所属厂商集合 (可能命中多个, 如 codex 档案含 openai 地址)。

    provider 为 openai_compat 时是协议标记而非厂商, 不参与判定,
    避免所有兼容模式的档案都被误判为 OpenAI。
    """
    blob = " ".join(
        str(prof.get(k) or "").lower() for k in ("name", "model", "codex_model", "base_url")
    )
    provider = str(prof.get("provider") or "").lower()
    if provider and provider != OPENAI_COMPAT_PROVIDER:
        blob += " " + provider
    return {
        vendor
        for vendor, keywords in _VENDOR_KEYWORDS.items()
        if any(kw in blob for kw in keywords)
    }


def match_profile_in_text(text: str) -> tuple[dict | None, str]:
    """在消息文本中查找已配置模型档案的别名, 返回 (档案含 id, 移除别名后的文本)。

    两轮匹配 (均忽略大小写, 最长优先):
    ① 精确别名: 档案名/模型名/codex模型名 原样出现在消息中;
    ② 厂商关键词: "千问/qwen/智谱/glm/deepseek/kimi..." 等友好叫法 →
       归属该厂商的唯一档案 (同厂商多档案时优先激活档案, 否则取第一个)。
    未命中返回 (None, 原文); 供 IM 机器人 "@机器人 模型名称 标的名称" 场景使用,
    未命中时调用方回退激活档案。
    """
    profiles = secrets_store.list_ai_profiles()
    low_text = (text or "").lower()

    def _hit(alias: str, prof: dict) -> tuple[dict, str]:
        idx = low_text.find(alias)
        cleaned = (text[:idx] + text[idx + len(alias):]).strip()
        return prof, cleaned

    # ① 精确别名 (最长优先)
    candidates: list[tuple[str, dict]] = []
    for prof in profiles:
        seen: set[str] = set()
        for key in ("name", "model", "codex_model"):
            alias = str(prof.get(key) or "").strip().lower()
            if len(alias) >= 2 and alias not in seen:
                seen.add(alias)
                candidates.append((alias, prof))
    candidates.sort(key=lambda item: len(item[0]), reverse=True)
    for alias, prof in candidates:
        if alias in low_text:
            return _hit(alias, prof)

    # ② 厂商关键词 (最长优先); 关键词 → 归属该厂商的档案
    vendor_profiles: dict[str, list[dict]] = {}
    for prof in profiles:
        for vendor in _profile_vendors(prof):
            vendor_profiles.setdefault(vendor, []).append(prof)
    active_id = secrets_store.get_active_ai_profile_id()
    keyword_candidates: list[tuple[str, str]] = [
        (kw, vendor)
        for vendor in vendor_profiles
        for kw in _VENDOR_KEYWORDS[vendor]
    ]
    keyword_candidates.sort(key=lambda item: len(item[0]), reverse=True)
    for kw, vendor in keyword_candidates:
        if kw not in low_text:
            continue
        matched = vendor_profiles[vendor]
        prof = next((p for p in matched if p.get("id") == active_id), matched[0])
        return _hit(kw, prof)
    return None, text


@contextlib.contextmanager
def profile_override(profile: dict | None):
    """当前异步上下文内临时按指定档案执行 AI 请求 (不落盘、不改激活档案)。

    profile 为 None 时等价无覆盖; 机器人消息未命中已配置模型名时以此回退激活模型。
    """
    if not profile:
        yield None
        return
    token = secrets_store.set_runtime_override(secrets_store.flat_from_profile(profile))
    try:
        yield profile
    finally:
        secrets_store.reset_runtime_override(token)


def _resolve_max_tokens(max_tokens: int | None) -> int | None:
    """显式传入的 max_tokens 钳制到配置输出上限; None 保持 None。

    None = 不传该参数(推理型模型的思考 token 计入 max_tokens 预算,
    显式限制会挤占正文, 语义见 generate_ai_text), 不能映射成配置上限。
    """
    if max_tokens is None:
        return None
    cap = current_ai_max_output_tokens()
    return max(1, min(int(max_tokens), cap))


def _estimate_input_tokens(messages: Sequence[Message]) -> int:
    """粗略估算输入 token 数: 中文按 1 字 1 token, 其余按 4 字符 1 token。"""
    total = 0
    for m in messages:
        text = str(m.get("content") or "")
        cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
        total += cjk + (len(text) - cjk) // 4 + 1
    return max(1, total)


def _check_input_budget(messages: Sequence[Message], *, max_tokens: int | None) -> None:
    """输入估算超出上下文窗口时给出明确报错, 避免上游 400 或静默截断。

    token 计数不精确, 仅作安全网: 用中/英文混合估算, 只有明显超窗才拒绝;
    用户可在 AI 设置里调大『上下文窗口』。max_tokens=None(输出放开)时用
    配置上限作输出预留估计。
    """
    context_window = current_ai_context_window()
    if context_window <= 0:
        return
    output_reserve = max_tokens if max_tokens is not None else current_ai_max_output_tokens()
    est = _estimate_input_tokens(messages)
    if est + output_reserve > context_window:
        raise ValueError(
            f"输入过长: 估算输入约 {est} tokens, 加上输出预算 {max_tokens} tokens, "
            f"超过上下文窗口 {context_window}。请缩短输入, 或在 AI 设置中调大『上下文窗口』。"
        )


def current_codex_command() -> str:
    return normalize_codex_command(
        secrets_store.get_ai_config("ai_codex_command", settings.ai_codex_command),
        strict=False,
    )


def current_codex_reasoning_effort() -> str:
    return normalize_codex_reasoning_effort(
        secrets_store.get_ai_config(
            "ai_codex_reasoning_effort",
            settings.ai_codex_reasoning_effort,
        )
    )


def is_codex_cli_provider(provider: str | None = None) -> bool:
    return (provider or current_ai_provider()) == CODEX_CLI_PROVIDER


def normalize_codex_model(model: str) -> str:
    value = model.strip()
    aliases = {
        "gpt5.5": "gpt-5.5",
        "gpt5.6": "gpt-5.6-sol",
        "gpt5.6-sol": "gpt-5.6-sol",
        "gpt5.6-terra": "gpt-5.6-terra",
        "gpt5.6-luna": "gpt-5.6-luna",
    }
    return aliases.get(value.lower(), value)


def normalize_codex_reasoning_effort(effort: str | None) -> str:
    value = (effort or "").strip().lower()
    return value if value in CODEX_SUPPORTED_REASONING_EFFORTS else ""


def normalize_codex_command(command: str | None, *, strict: bool = True) -> str:
    value = (command or "").strip()
    if not value or value.lower() == CODEX_DEFAULT_COMMAND:
        return CODEX_DEFAULT_COMMAND
    if strict:
        raise ValueError("Codex CLI 仅支持使用默认 codex 命令自动解析, 不支持自定义可执行路径")
    return CODEX_DEFAULT_COMMAND


_VERSION_SEGMENT_RE = re.compile(r"/v\d+(?:\.\d+)?$", re.IGNORECASE)


def normalize_openai_base_url(url: str) -> str:
    """Return the OpenAI-compatible base URL expected by the OpenAI SDK.

    识别 URL 中已有的版本段 (/v1、/v2、/v4 等) 时保持原样 —— 部分 OpenAI 兼容
    服务用非 v1 的版本号 (如智谱 GLM 用 /api/paas/v4), 旧实现无条件补 /v1 会拼成
    不存在的 /api/paas/v4/v1/chat/completions 导致 404。仅在无版本段时才补 /v1。
    """
    base = (url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")].rstrip("/")
    if _VERSION_SEGMENT_RE.search(base):
        return base
    return f"{base}/v1"


def codex_cli_available() -> bool:
    """Codex CLI 是否真正可用: 解析命令后实跑一次 --version。

    仅 which 到二进制不够 — npm 壳缺平台原生二进制时同样存在于 PATH,
    但一运行就报错(如 "Codex CLI not available"), 状态页会误报已连接。
    """
    try:
        base = _codex_base_command()
    except RuntimeError:
        return False
    try:
        proc = subprocess.run(
            [*base, "--version"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def ai_configured(provider: str | None = None) -> bool:
    provider = provider or current_ai_provider()
    if is_codex_cli_provider(provider):
        return codex_cli_available()
    return bool(secrets_store.get_ai_key())


async def generate_ai_text(
    messages: Sequence[Message],
    *,
    temperature: float | None = 0.3,
    max_tokens: int | None = 3000,
    timeout: float = 180.0,
) -> str:
    """Return a complete AI response from the currently configured provider.

    max_tokens=None 表示不传该参数(输出上限交给服务端默认) — 推理型模型
    (如 deepseek reasoner 系)的思考 token 计入 max_tokens 预算, 显式限制
    会挤占正文甚至全部吃光(正文 0 字 + finish=length), 长分析类调用应放开。
    显式传入的数值会被钳制到配置的输出上限 (AI 设置可调)。
    """
    max_tokens = _resolve_max_tokens(max_tokens)
    _check_input_budget(messages, max_tokens=max_tokens)
    if is_codex_cli_provider():
        return await _run_codex_cli(messages, max_tokens=max_tokens, timeout=max(timeout, 600.0))
    if is_responses_protocol():
        from app.services import ai_responses

        return await ai_responses.run_text(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    return await _run_openai_once(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


async def generate_ai_text_with_tools(
    messages: Sequence[Message],
    tools: Sequence[dict],
    *,
    execute_tool,
    temperature: float | None = 0.3,
    max_tokens: int | None = 3000,
    timeout: float = 180.0,
    max_rounds: int = 4,
) -> list[dict]:
    """OpenAI 原生 tools 有界循环: 调用 → 逐条执行工具 → role:tool 回填 → 循环。

    与 generate_ai_text 的差异: 这里返回「完整 messages」(含 tool 往返), 最后一条
    assistant 消息承载最终文本; 调用方可扫描 role:tool 消息重建逐轮证据 (回测指标)。
    execute_tool 为 async (name, args) -> dict, 返回 {"ok": bool, "result"|"error"},
    由调用方注入 (见 services.tool_catalog.execute_tool)。tools 是硬能力依赖
    (Codex CLI 无 tools= 协议), 故入口 fail-closed, 不做纯文本降级。
    """
    if is_codex_cli_provider():
        raise RuntimeError("当前 AI 供应商不支持工具调用迭代, 请改用 OpenAI 兼容模型")

    max_tokens = _resolve_max_tokens(max_tokens)
    if not tools:
        raise ValueError("generate_ai_text_with_tools 需要至少一个工具 schema")

    req_messages: list[dict] = [dict(m) for m in messages]
    tool_schemas = list(tools)

    for _ in range(max_rounds):
        _check_input_budget(req_messages, max_tokens=max_tokens)
        if is_responses_protocol():
            from app.services import ai_responses

            message = await ai_responses.run_message_once(
                req_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                tools=tool_schemas,
            )
        else:
            message = await _run_openai_message_once(
                req_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                tools=tool_schemas,
            )
        if message is None:
            return req_messages

        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            # 模型不再请求工具, 产出最终文本, 结束循环。
            req_messages.append({"role": "assistant", "content": message.content or ""})
            return req_messages

        assistant = {
            "role": "assistant",
            "tool_calls": [_assistant_tool_call(tc) for tc in tool_calls],
        }
        if message.content:
            assistant["content"] = message.content
        req_messages.append(assistant)

        for tc in tool_calls:
            tool_result = await execute_tool(
                tc.function.name,
                _parse_tool_arguments(tc.function.arguments),
            )
            req_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_result, ensure_ascii=False),
                }
            )

    return req_messages


def _assistant_tool_call(tc) -> dict:
    """SDK 返回的 tool_call 转成回传用的 dict; Gemini 3 的 extra_content
    (thought_signature) 需原样带回, 否则下一轮被上游 400 拒绝。"""
    item = {
        "id": tc.id,
        "type": getattr(tc, "type", "function"),
        "function": {
            "name": tc.function.name,
            "arguments": tc.function.arguments,
        },
    }
    extra = getattr(tc, "extra_content", None)
    if extra:
        item["extra_content"] = extra
    return item


def _parse_tool_arguments(raw: str) -> dict:
    """把 tool call 的 arguments JSON 字符串解析为 dict, 解析失败返回空 dict。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


async def stream_ai_text(
    messages: Sequence[Message],
    *,
    temperature: float | None = 0.5,
    max_tokens: int | None = 4000,
    timeout: float = 180.0,
    prefer_final_answer: bool = False,
) -> AsyncIterator[str]:
    """Yield text deltas from the configured provider.

    Codex CLI only exposes the final assistant message for this use case, so it
    yields one complete chunk after the command exits. ``prefer_final_answer``
    lets compatible providers prioritize visible content over hidden reasoning.

    max_tokens=None 表示不限制输出(同 generate_ai_text 的说明)。
    """
    max_tokens = _resolve_max_tokens(max_tokens)
    _check_input_budget(messages, max_tokens=max_tokens)
    if is_codex_cli_provider():
        yield await _run_codex_cli(messages, max_tokens=max_tokens, timeout=max(timeout, 600.0))
        return

    if is_responses_protocol():
        from app.services import ai_responses

        async for chunk in ai_responses.stream_text(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        ):
            yield chunk
        return

    async for chunk in _stream_openai(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        prefer_final_answer=prefer_final_answer,
    ):
        yield chunk


async def _run_openai_once(
    messages: Sequence[Message],
    *,
    temperature: float | None,
    max_tokens: int | None,
    timeout: float,
) -> str:
    message = await _run_openai_message_once(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    if message is None:
        return ""
    return (message.content or "").strip()


async def _run_openai_message_once(
    messages: Sequence[Message],
    *,
    temperature: float | None,
    max_tokens: int | None,
    timeout: float,
    tools: Sequence[dict] | None = None,
):
    """一次 OpenAI create() 调用, 返回完整 message (含 tool_calls, 而非仅 content)。

    _run_openai_once 只取 .content; 工具循环需要 .tool_calls 以回填 role:tool,
    故在此返回完整消息对象。tools 非空时以 tools= 透传, 否则走纯文本路径。
    """
    ai_key = secrets_store.get_ai_key()
    if not ai_key:
        raise RuntimeError("AI API Key 未配置, 请在设置页配置")

    client = _openai_client(ai_key, timeout)
    model = current_ai_model()
    req_messages = list(messages)
    kwargs = _openai_kwargs(
        temperature=temperature,
        max_tokens=max_tokens,
        tools=tools,
        web_search=web_search_supported(),
    )
    while True:
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=req_messages,
                **kwargs,
            )
            break
        except Exception as exc:
            retry_kwargs = _openai_retry_kwargs(exc, kwargs)
            if retry_kwargs is not None:
                kwargs = retry_kwargs
                continue
            if _is_openai_transport_error(exc):
                raise RuntimeError(_format_openai_error(exc)) from exc
            raise
    if not resp.choices:
        return None
    return resp.choices[0].message


async def _stream_openai(
    messages: Sequence[Message],
    *,
    temperature: float | None,
    max_tokens: int | None,
    timeout: float,
    prefer_final_answer: bool,
) -> AsyncIterator[str]:
    ai_key = secrets_store.get_ai_key()
    if not ai_key:
        raise RuntimeError("AI API Key 未配置, 请在设置页配置")

    client = _openai_client(ai_key, timeout)
    model = current_ai_model()
    base_url = secrets_store.get_ai_config("ai_base_url", settings.ai_base_url)
    req_messages = list(messages)

    kwargs = _openai_kwargs(
        temperature=temperature,
        max_tokens=max_tokens,
        model=model,
        base_url=base_url,
        prefer_final_answer=prefer_final_answer,
        web_search=web_search_supported(),
    )
    while True:
        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=req_messages,
                **kwargs,
                stream=True,
            )
            break
        except Exception as exc:
            # 流尚未开始 yield, 可安全移除被拒绝的可选参数后重建。
            retry_kwargs = _openai_retry_kwargs(exc, kwargs)
            if retry_kwargs is not None:
                kwargs = retry_kwargs
                continue
            if _is_openai_transport_error(exc):
                raise RuntimeError(_format_openai_error(exc)) from exc
            raise

    try:
        async for piece in _iter_openai_text(stream):
            yield piece
    except Exception as exc:
        if _is_openai_transport_error(exc):
            raise RuntimeError(_format_openai_error(exc)) from exc
        raise


_LENGTH_FINISH_REASONS = {"length", "max_tokens", "max_output_tokens"}


async def _iter_openai_text(stream) -> AsyncIterator[str]:
    """Normalize an OpenAI-compatible stream into complete text deltas.

    Reasoning models may spend the entire completion budget on
    ``reasoning_content`` and finish with HTTP 200 but no user-visible text.
    Treat that response, and any length-truncated partial response, as a
    terminal generation error instead of silently reporting success.
    """
    content_seen = False
    reasoning_seen = False
    finish_reason = ""

    async for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        choice = choices[0]
        reason = getattr(choice, "finish_reason", None)
        if reason:
            finish_reason = str(reason)

        delta = getattr(choice, "delta", None)
        if delta is None:
            continue
        if getattr(delta, "reasoning_content", None):
            reasoning_seen = True
        content = getattr(delta, "content", None)
        if content:
            content_seen = True
            yield content

    if finish_reason in _LENGTH_FINISH_REASONS:
        if reasoning_seen and not content_seen:
            raise RuntimeError(
                "AI 推理达到输出长度上限, 未生成正文; 请提高输出 Token 上限或改用非推理模型"
            )
        raise RuntimeError("AI 输出达到长度上限, 内容不完整; 请提高输出 Token 上限后重试")

    if not content_seen:
        if reasoning_seen:
            raise RuntimeError("AI 仅返回推理内容, 未生成正文; 请检查模型配置或改用非推理模型")
        raise RuntimeError("AI 服务未返回正文内容; 请检查模型配置或稍后重试")


def _openai_client(api_key: str, timeout: float):
    from openai import AsyncOpenAI

    user_agent = secrets_store.get_ai_config("ai_user_agent", "") or settings.ai_user_agent
    return AsyncOpenAI(
        api_key=api_key,
        base_url=normalize_openai_base_url(secrets_store.get_ai_config("ai_base_url", settings.ai_base_url)),
        timeout=timeout,
        # SDK 层重试仅覆盖首包前的连接错误/超时/429/5xx, 此时尚未产出任何内容,
        # 重试安全; 首 chunk 之后的断流不在此列, 由上层协议报错处理。
        # 根因: DeepSeek 等上游高峰过载时首包失败率高, max_retries=0 导致一次抖动即终止。
        max_retries=2,
        default_headers={"User-Agent": user_agent},
    )


async def probe_ai_config(
    *,
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    protocol: str = "",
    timeout: float = 20.0,
) -> str:
    """草稿配置一次性连通性探测 — 不读写任何配置状态。

    「测试」必须与「保存」解耦: 旧实现先保存草稿再测试, 测试失败时坏配置
    已经落盘并被状态栏展示为已连接。本函数用表单值直接建临时客户端发一条
    最小消息, 成功与否都不触碰 secrets。

    与正式调用共用 base URL 规范化与协议分支 (chat.completions / responses);
    不传输出上限/温度等可选参数, 规避 max_tokens 与 max_completion_tokens 等
    模型差异。返回模型可见文本 (推理模型可能为空串 — 200 即证明鉴权/端点/
    模型可用); 传输层异常原样抛出, 由调用方格式化后回报。
    """
    if provider == CODEX_CLI_PROVIDER:
        if not codex_cli_available():
            raise RuntimeError("Codex CLI 不可用: 未找到可运行的 codex 命令")
        return ""
    if not api_key:
        raise RuntimeError("未配置 API Key")
    if not base_url:
        raise ValueError("请先填写 API 地址")
    if not model:
        raise ValueError("请先填写模型名称")

    from openai import AsyncOpenAI

    user_agent = secrets_store.get_ai_config("ai_user_agent", "") or settings.ai_user_agent
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=normalize_openai_base_url(base_url),
        timeout=timeout,
        max_retries=0,
        default_headers={"User-Agent": user_agent},
    )
    prompt = [{"role": "user", "content": "Reply exactly: OK"}]
    if protocol == RESPONSES_PROTOCOL:
        resp = await client.responses.create(model=model, input=prompt)
        return str(getattr(resp, "output_text", "") or "")
    resp = await client.chat.completions.create(model=model, messages=prompt)
    if not getattr(resp, "choices", None):
        return ""
    return str(resp.choices[0].message.content or "")


# 不同模型可能拒绝 temperature 或 reasoning_effort。这里不靠模型名猜测,
# 只在 400 明确指出对应参数时移除该参数并重试; 每个参数最多移除一次。
_TEMP_REJECT_HINTS = ("temperature", "only 1 is allowed")
_REASONING_EFFORT_REJECT_HINTS = ("reasoning_effort", "reasoning effort")
_THINKING_BODY_REJECT_HINTS = ("thinking",)
_SEARCH_BODY_REJECT_HINTS = ("enable_search", "search_options", "web_search", "web search")


def _is_temperature_rejected(exc: Exception) -> bool:
    """True if the upstream 400 is specifically about the temperature param."""
    if getattr(exc, "status_code", None) != 400:
        return False
    text = _openai_error_detail(exc) or str(exc)
    return _openai_error_param(exc) == "temperature" or any(
        h in text.lower() for h in _TEMP_REJECT_HINTS
    )


def _is_reasoning_effort_rejected(exc: Exception) -> bool:
    """True if the upstream 400 specifically rejects reasoning_effort."""
    if getattr(exc, "status_code", None) != 400:
        return False
    text = _openai_error_detail(exc) or str(exc)
    return _openai_error_param(exc) == "reasoning_effort" or any(
        h in text.lower() for h in _REASONING_EFFORT_REJECT_HINTS
    )


def _is_thinking_body_rejected(exc: Exception) -> bool:
    """True if the upstream 400 specifically rejects the thinking extra_body."""
    if getattr(exc, "status_code", None) != 400:
        return False
    text = _openai_error_detail(exc) or str(exc)
    return _openai_error_param(exc) == "thinking" or any(
        h in text.lower() for h in _THINKING_BODY_REJECT_HINTS
    )


def _is_search_body_rejected(exc: Exception) -> bool:
    """True if the upstream 400 specifically rejects the enable_search extra_body."""
    if getattr(exc, "status_code", None) != 400:
        return False
    text = _openai_error_detail(exc) or str(exc)
    return _openai_error_param(exc) == "enable_search" or any(
        h in text.lower() for h in _SEARCH_BODY_REJECT_HINTS
    )


def _openai_error_param(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        body = error
    return str(body.get("param") or "").strip().lower()


def _openai_retry_kwargs(exc: Exception, kwargs: dict) -> dict | None:
    """Remove one explicitly rejected optional argument for a bounded retry."""
    retry_kwargs = dict(kwargs)
    if "temperature" in retry_kwargs and _is_temperature_rejected(exc):
        retry_kwargs.pop("temperature")
        return retry_kwargs
    if "reasoning_effort" in retry_kwargs and _is_reasoning_effort_rejected(exc):
        retry_kwargs.pop("reasoning_effort")
        return retry_kwargs
    if "extra_body" in retry_kwargs and _is_search_body_rejected(exc):
        # 网关/模型不支持联网搜索参数: 仅移除搜索相关键重试,
        # 保留 extra_body 其它键 (如 thinking 禁用)。
        body = retry_kwargs["extra_body"]
        _search_keys = ("enable_search", "search_options")
        if isinstance(body, dict) and any(k in body for k in _search_keys):
            rest = {k: v for k, v in body.items() if k not in _search_keys}
            if rest:
                retry_kwargs["extra_body"] = rest
            else:
                retry_kwargs.pop("extra_body")
            return retry_kwargs
    if "extra_body" in retry_kwargs and _is_thinking_body_rejected(exc):
        # DeepSeek thinking 禁用参数被拒 (模型/API 版本差异): 回退默认思考模式
        # 重试; 报告若因此被推理挤占正文, 由 _iter_openai_text 显式报错。
        retry_kwargs.pop("extra_body")
        return retry_kwargs
    return None


_DEEPSEEK_V4_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}


def _openai_kwargs(
    *,
    temperature: float | None,
    max_tokens: int | None,
    model: str = "",
    base_url: str = "",
    prefer_final_answer: bool = False,
    tools: Sequence[dict] | None = None,
    web_search: bool = False,
) -> dict:
    """Build OpenAI create() kwargs and map supported provider capabilities.

    max_tokens=None 时不传 — 由服务端默认上限管理(推理模型的思考 token 也
    计入该参数预算, 限制会挤占正文, 见 stream_ai_text 文档)。
    web_search=True 时附带 enable_search (千问/百炼系联网搜索开关)。
    """
    kwargs: dict = {}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if temperature is not None:
        kwargs["temperature"] = temperature
    if current_ai_provider() == OPENAI_PROVIDER:
        reasoning_effort = current_openai_reasoning_effort()
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
    extra_body: dict = {}
    if (
        prefer_final_answer
        and model.strip().lower() in _DEEPSEEK_V4_MODELS
        and urlsplit(base_url.strip()).hostname == "api.deepseek.com"
    ):
        # DeepSeek V4 defaults to thinking mode. For report-style tasks the
        # hidden reasoning shares max_tokens with the final answer and can
        # exhaust the budget before any visible content is emitted.
        extra_body["thinking"] = {"type": "disabled"}
    if web_search:
        # 千问/百炼 OpenAI 兼容模式的联网搜索参数; 不支持的网关会 400 拒绝,
        # 由 _openai_retry_kwargs 移除后重试 (见 _is_search_body_rejected)。
        extra_body["enable_search"] = True
        # 强制搜索 + 最大检索量: 实测长提示词下模型常自行跳过搜索或检索
        # 预算内放弃 (输出"未检索到相关消息"), forced_search 保证必搜。
        extra_body["search_options"] = {"forced_search": True, "search_strategy": "max"}
    if extra_body:
        kwargs["extra_body"] = extra_body
    if tools:
        kwargs["tools"] = list(tools)
    return kwargs


def _is_openai_transport_error(exc: Exception) -> bool:
    try:
        import openai
    except ImportError:
        openai = None

    if openai is not None and isinstance(exc, openai.APIError):
        return True

    try:
        import httpx
    except ImportError:
        return False

    return isinstance(exc, httpx.HTTPError)


def _format_openai_error(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)

    class_name = exc.__class__.__name__
    if "Timeout" in class_name:
        return "AI 服务请求超时, 请稍后重试或检查 AI Base URL / 网络"
    if "Connection" in class_name:
        return "AI 服务连接失败, 请检查 AI Base URL / 网络"

    detail = _openai_error_detail(exc)
    status_messages = {
        400: "请求参数无效, 请检查模型名称和上下文长度",
        401: "API Key 无效或无权限, 请检查设置页配置",
        403: "AI 服务拒绝访问, 请检查账号权限或网关配置",
        404: "模型或接口地址不存在, 请检查 AI Base URL 和模型名称",
        408: "AI 服务请求超时, 请稍后重试",
        429: "AI 服务限流或额度不足, 请稍后重试或检查额度",
        500: "AI 服务内部错误, 请稍后重试",
        502: "AI 网关返回错误, 请稍后重试或检查 AI Base URL",
        503: "AI 服务暂时不可用, 请稍后重试",
        504: "AI 上游服务超时, 请稍后重试或检查 AI Base URL / 网络",
    }
    # 优先透出上游真实错误 (如 Moonshot 的 "model not found"), 仅在没有
    # 可读 detail 时才回落到按状态码的通用文案, 避免吞掉排障关键信息。
    message = detail or status_messages.get(status) or "请稍后重试或检查 AI 服务配置"
    if status:
        return f"AI 服务请求失败({status}): {message}"
    return f"AI 服务请求失败: {message}"


def _openai_error_detail(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            text = error.get("message") or error.get("code") or error.get("type")
            return _compact_error_text(str(text or ""))
        if isinstance(error, str):
            return _compact_error_text(error)

    response = getattr(exc, "response", None)
    content_type = ""
    text = ""
    if response is not None:
        content_type = response.headers.get("content-type", "").lower()
        try:
            text = response.text
        except Exception:
            text = ""
    if not text and isinstance(body, str):
        text = body
    if not text:
        text = str(exc)
    if _looks_like_html(text, content_type):
        return ""
    return _compact_error_text(text)


def _looks_like_html(text: str, content_type: str) -> bool:
    sample = text.lstrip()[:200].lower()
    return "html" in content_type or sample.startswith("<!doctype html") or sample.startswith("<html")


def _compact_error_text(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


async def _run_codex_cli(
    messages: Sequence[Message],
    *,
    max_tokens: int | None,
    timeout: float,
) -> str:
    prompt = _codex_prompt(messages, max_tokens=max_tokens)
    run_path = Path(tempfile.mkdtemp(prefix="tickflow-codex-run-"))
    try:
        codex_home_path = run_path / "codex-home"
        workspace_path = run_path / "workspace"
        codex_home_path.mkdir()
        workspace_path.mkdir()
        output_path = codex_home_path / "last-message.txt"
        _prepare_codex_home(codex_home_path)

        # 不传 --ephemeral: 老版本 codex(如 0.58)无此参数, 传了直接报
        # unexpected argument; 会话隔离已由一次性临时 CODEX_HOME 保证(跑完即删)。
        args = [
            *_codex_base_command(),
            "exec",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--output-last-message",
            str(output_path),
        ]
        model = current_ai_model().strip()
        if model:
            args.extend(["--model", model])
        args.extend(["--cd", str(workspace_path), "-"])

        env = _codex_process_env(codex_home_path)

        returncode, stdout, stderr = await asyncio.to_thread(
            _run_codex_process,
            args,
            prompt,
            env,
            timeout,
        )

        out = _clean_process_text(stdout)
        err = _clean_process_text(stderr)
        final_message = _read_output_file(output_path)
        if returncode != 0:
            detail = err or out or f"exit code {returncode}"
            raise RuntimeError(f"Codex CLI 调用失败: {detail[-1200:]}")
        result = final_message or out
        if not result:
            raise RuntimeError("Codex CLI 未返回内容")
        return result
    finally:
        await asyncio.to_thread(_remove_tree_best_effort, run_path)


def _run_codex_process(
    args: Sequence[str],
    prompt: str,
    env: dict[str, str],
    timeout: float,
) -> tuple[int, bytes, bytes]:
    try:
        proc = subprocess.run(
            list(args),
            input=prompt.encode("utf-8"),
            capture_output=True,
            env=env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Codex CLI 调用超时, 请稍后重试或检查本机 Codex 登录状态") from exc
    return proc.returncode, proc.stdout, proc.stderr


def _codex_process_env(codex_home_path: Path) -> dict[str, str]:
    """Pass only OS, locale, certificate, and proxy settings to Codex."""
    env: dict[str, str] = {}
    seen: set[str] = set()
    for name in _CODEX_ENV_ALLOWLIST:
        normalized = name.casefold() if os.name == "nt" else name
        if normalized in seen:
            continue
        value = os.environ.get(name)
        if value:
            env[name] = value
            seen.add(normalized)
    env["NO_COLOR"] = "1"
    env["CODEX_HOME"] = str(codex_home_path)
    return env


def _remove_tree_best_effort(path: Path) -> None:
    _remove_auth_files(path)
    for attempt in range(4):
        try:
            shutil.rmtree(path, onerror=_make_writable_and_retry)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 3:
                break
            time.sleep(0.2 * (attempt + 1))
    _remove_auth_files(path)
    shutil.rmtree(path, ignore_errors=True)
    _remove_auth_files(path)


def _remove_auth_files(path: Path) -> None:
    try:
        auth_files = list(path.rglob("auth.json"))
    except OSError:
        return
    for auth_file in auth_files:
        try:
            os.chmod(auth_file, stat.S_IWRITE)
            auth_file.unlink(missing_ok=True)
        except OSError:
            pass


def _make_writable_and_retry(
    func: Callable[[str], object],
    path: str,
    exc_info: tuple[type[BaseException], BaseException, TracebackType],
) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        raise exc_info[1] from None


def _codex_prompt(messages: Sequence[Message], *, max_tokens: int | None) -> str:
    parts = [
        "You are Tick Stock Panel's local AI provider.",
        "This is a text-generation task. The working directory is intentionally empty.",
        "Use only the user-provided prompt content below; do not inspect or modify local files.",
        "Return only the final requested content; do not include execution logs.",
    ]
    if max_tokens:
        parts.append(f"Keep the final answer within about {max_tokens} output tokens.")
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        parts.append(f"\n<{role}>\n{content}\n</{role}>")
    return "\n".join(parts)


def _codex_base_command() -> list[str]:
    command = current_codex_command()
    resolved = _resolve_command(command)
    if not resolved:
        raise RuntimeError(f"未找到 Codex CLI 命令: {command}")

    if sys.platform == "win32" and resolved.lower().endswith(".ps1"):
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", resolved]
    return [resolved]


def _resolve_command(command: str) -> str | None:
    if command.lower() != CODEX_DEFAULT_COMMAND:
        return None

    if sys.platform == "win32":
        desktop_codex = _resolve_windows_desktop_codex()
        if desktop_codex:
            return desktop_codex

    resolved = shutil.which(command)
    if sys.platform == "win32" and resolved:
        resolved_path = Path(resolved)
        if not resolved_path.suffix:
            cmd_path = resolved_path.with_suffix(".cmd")
            if cmd_path.exists():
                return str(cmd_path)
    if not resolved and sys.platform == "win32" and not command.lower().endswith(".cmd"):
        resolved = shutil.which(f"{command}.cmd")
    if not resolved and sys.platform == "win32":
        resolved = _resolve_windows_codex_command(command)
    return resolved


def _resolve_windows_codex_command(command: str) -> str | None:
    """Find npm-installed Codex when the backend process has a minimal PATH."""
    raw = Path(command)
    if raw.parent != Path("."):
        return None

    names = [command]
    if not raw.suffix:
        names = [f"{command}.cmd", f"{command}.exe", f"{command}.bat", f"{command}.ps1", command]

    dirs: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / "npm")
    dirs.append(Path.home() / "AppData" / "Roaming" / "npm")

    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        value = os.environ.get(env_name)
        if value:
            dirs.append(Path(value) / "nodejs")

    for directory in dirs:
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return str(candidate)
    return None


def _resolve_windows_desktop_codex() -> str | None:
    """Prefer the Codex Desktop bundled CLI over an older npm shim."""
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None

    root = Path(local_appdata) / "OpenAI" / "Codex" / "bin"
    if not root.exists():
        return None

    candidates = list(root.glob("*/codex.exe"))
    direct = root / "codex.exe"
    if direct.exists():
        candidates.append(direct)
    if not candidates:
        return None

    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return str(newest)


def _prepare_codex_home(target: Path) -> None:
    """Create an isolated CODEX_HOME that reuses auth but not fragile config."""
    source = _codex_home()
    auth_file = source / "auth.json"
    if auth_file.exists():
        shutil.copy2(auth_file, target / "auth.json")
    _write_compatible_codex_config(target / "config.toml")


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _write_compatible_codex_config(path: Path) -> None:
    config = _read_codex_config()
    lines: list[str] = []
    active_provider = _active_codex_provider(config)

    if active_provider:
        lines.append(_toml_string("model_provider", active_provider[0]))

    openai_base_url = config.get("openai_base_url")
    if isinstance(openai_base_url, str) and openai_base_url:
        lines.append(_toml_string("openai_base_url", openai_base_url))

    model = current_ai_model() or normalize_codex_model(str(config.get("model") or ""))
    if model:
        lines.append(_toml_string("model", model))

    effort = current_codex_reasoning_effort() or normalize_codex_reasoning_effort(
        str(config.get("model_reasoning_effort") or "")
    )
    if effort:
        lines.append(_toml_string("model_reasoning_effort", effort))

    lines.append(_toml_string("approval_policy", "never"))
    lines.append(_toml_string("sandbox_mode", "read-only"))

    if active_provider:
        provider_name, provider = active_provider
        lines.append("")
        lines.append(f"[model_providers.{_toml_key(provider_name)}]")
        for key in ("name", "base_url", "wire_api", "experimental_bearer_token"):
            value = provider.get(key)
            if isinstance(value, str) and value:
                lines.append(_toml_string(key, value))
        for key in ("requires_openai_auth", "supports_websockets"):
            value = provider.get(key)
            if isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _active_codex_provider(config: dict) -> tuple[str, dict] | None:
    """Return the active custom provider, adapting loopback URLs for Docker."""
    provider_name = config.get("model_provider")
    if not isinstance(provider_name, str) or not provider_name:
        return None

    providers = config.get("model_providers")
    if not isinstance(providers, dict):
        return None
    source = providers.get(provider_name)
    if not isinstance(source, dict):
        return None

    provider = dict(source)
    base_url = str(provider.get("base_url") or "").strip()
    parsed = urlsplit(base_url)
    docker_host = os.environ.get("CODEX_DOCKER_HOST", "").strip()
    if docker_host and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        port = f":{parsed.port}" if parsed.port else ""
        provider["base_url"] = urlunsplit(parsed._replace(netloc=f"{docker_host}{port}"))
    return provider_name, provider


def _read_codex_config() -> dict:
    path = _codex_home() / "config.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError:
        return _read_codex_config_lenient(path)
    except OSError:
        return {}


def _read_codex_config_lenient(path: Path) -> dict:
    config: dict[str, str] = {}
    pattern = re.compile(r'^\s*([A-Za-z0-9_-]+)\s*=\s*"([^"]*)"\s*$')
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = pattern.match(line)
            if match:
                config[match.group(1)] = match.group(2)
    except OSError:
        pass
    return config


def _toml_string(key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key} = "{escaped}"'


def _toml_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _clean_process_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    return _ANSI_RE.sub("", text).strip()


def _read_output_file(path: Path) -> str:
    if path.exists():
        return _ANSI_RE.sub("", path.read_text(encoding="utf-8", errors="replace")).strip()
    return ""
