"""AI 调用审计日志: 每次出站 LLM 调用留痕, 且绝不泄露 prompt 正文与密钥。

背景: 排查「token 到底被谁消耗」时, 日志里只有 httpx 的 URL 行, 分不清是哪个
档案 (profile)、哪种调用 (报告/助手/流式/连通性探测), 也看不到 prompt 规模。
ai_provider 因此在每次真实调用前打一行 [AI-AUDIT] (归属 + 网关域名 + 字符数),
非流式调用返回后再打一行上游回报的 token 用量。

隐私铁律: 只记长度不记内容 —— 与 tests/test_assistant_codex.py 里
"private prompt" 不得入日志的断言是同一条约束, 此处对 prompt 正文与 API Key
双向锁定。
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.services import ai_provider

LOGGER_NAME = "app.services.ai_provider"
SECRET_PROMPT = "PRIVATE-PROMPT-DO-NOT-LOG"
FAKE_KEY = "sk-fake-audit-key"


def _async_return(value):
    async def _fn(**kwargs):
        return value

    return _fn


def _audit_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "[AI-AUDIT]" in r.getMessage()]


@pytest.fixture
def fake_openai(monkeypatch):
    """把出站客户端换成假实现: 不联网, 返回带 usage 的响应。"""
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_async_return(resp)))
    )
    monkeypatch.setattr(ai_provider, "_openai_client", lambda api_key, timeout: client)
    monkeypatch.setattr(ai_provider.secrets_store, "get_ai_key", lambda: FAKE_KEY)
    monkeypatch.setattr(
        ai_provider.secrets_store,
        "get_ai_config",
        lambda key, default="": (
            "https://api.deepseek.com/v1" if key == "ai_base_url" else default
        ),
    )
    monkeypatch.setattr(
        ai_provider.secrets_store, "get_active_ai_profile_id", lambda: "deepseek"
    )
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "deepseek-v4-pro")
    monkeypatch.setattr(ai_provider, "web_search_supported", lambda: False)
    return client


def test_chat_call_logs_attribution_and_usage(fake_openai, caplog):
    """非流式调用: 请求行含档案/模型/域名/字符数, 返回行含上游 token 用量。"""
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        message = asyncio.run(
            ai_provider._run_openai_message_once(
                [{"role": "user", "content": SECRET_PROMPT}],
                temperature=None,
                max_tokens=None,
                timeout=5.0,
            )
        )

    assert message is not None and message.content == "ok"
    lines = _audit_lines(caplog)
    req = next(line for line in lines if line.startswith("[AI-AUDIT] chat "))
    assert "model=deepseek-v4-pro" in req
    assert "profile=deepseek" in req
    # 只记域名, 不落完整端点 (避免把带参数的 URL 写进日志)
    assert "host=api.deepseek.com" in req
    assert f"prompt_chars={len(SECRET_PROMPT)}" in req
    assert "msgs=1" in req
    assert any("total_tokens=18" in line for line in lines)


def test_audit_log_never_contains_prompt_or_key(fake_openai, caplog):
    """隐私与密钥: prompt 正文和 API Key 都不得出现在任何日志行里。"""
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        asyncio.run(
            ai_provider._run_openai_message_once(
                [{"role": "user", "content": SECRET_PROMPT}],
                temperature=None,
                max_tokens=None,
                timeout=5.0,
            )
        )

    assert SECRET_PROMPT not in caplog.text
    assert FAKE_KEY not in caplog.text


def test_stream_call_logs_audit_before_sending(fake_openai, caplog):
    """流式调用: 审计行在真正发包之前落地 (即使随后失败也已留痕)。"""

    async def _boom(**kwargs):
        raise ValueError("stop-here")

    fake_openai.chat.completions.create = _boom

    async def _drain():
        async for _ in ai_provider._stream_openai(
            [{"role": "user", "content": SECRET_PROMPT}],
            temperature=None,
            max_tokens=None,
            timeout=5.0,
            prefer_final_answer=False,
        ):
            pass

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME), pytest.raises(
        ValueError, match="stop-here"
    ):
        asyncio.run(_drain())

    req = next(line for line in _audit_lines(caplog) if " stream " in line)
    assert "host=api.deepseek.com" in req
    assert f"prompt_chars={len(SECRET_PROMPT)}" in req
    assert SECRET_PROMPT not in caplog.text


def test_probe_logs_audit_with_draft_config(monkeypatch, caplog):
    """连通性探测: 用表单草稿值留痕 — 草稿填错网关时日志能直接看出来。"""
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))]
    )

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=_async_return(resp))
            )

    monkeypatch.setattr("openai.AsyncOpenAI", _FakeAsyncOpenAI)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        out = asyncio.run(
            ai_provider.probe_ai_config(
                provider="openai_compat",
                api_key="sk-probe-key",
                base_url="https://api.deepseek.com/v1",
                model="deepseek-chat",
            )
        )

    assert out == "OK"
    req = next(line for line in _audit_lines(caplog) if " probe " in line)
    assert "model=deepseek-chat" in req
    assert "host=api.deepseek.com" in req
    assert "sk-probe-key" not in caplog.text
