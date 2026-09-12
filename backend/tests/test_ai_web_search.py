"""联网搜索开关: ai_profiles[].web_search 持久化 + _stream_openai 按厂商注入参数。

千问(阿里云百炼) → extra_body.enable_search; 智谱 GLM → tools[web_search];
其余厂商(如 DeepSeek)接口不支持, 不注入。
"""
from types import SimpleNamespace

import pytest

from app import secrets_store
from app.api.settings import AiSettingsIn, _upsert_ai_profile
from app.services import ai_provider

_QWEN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_GLM_URL = "https://open.bigmodel.cn/api/paas/v4"
_DEEPSEEK_URL = "https://api.deepseek.com/v1"


@pytest.fixture
def isolated_profiles(monkeypatch):
    store: dict = {"ai_profiles": []}
    monkeypatch.setattr(secrets_store, "load", lambda: store)
    monkeypatch.setattr(secrets_store, "save", lambda updates: store.update(updates) or store)
    return store


def _fake_stream():
    async def gen():
        yield SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop",
            delta=SimpleNamespace(content="ok", reasoning_content=None),
        )])

    return gen()


@pytest.fixture
def captured_kwargs(monkeypatch):
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_stream()

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    monkeypatch.setattr(ai_provider, "_openai_client", lambda *a, **k: fake_client)
    return captured


async def _drain_stream(profile=None, monkeypatch=None):
    async for _ in ai_provider._stream_openai(
        [{"role": "user", "content": "hi"}],
        temperature=0.5,
        max_tokens=None,
        timeout=5,
        prefer_final_answer=False,
        profile=profile,
    ):
        pass


def test_web_search_vendor_detection():
    assert ai_provider._web_search_vendor(_QWEN_URL) == "qwen"
    assert ai_provider._web_search_vendor("https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1") == "qwen"
    assert ai_provider._web_search_vendor(_GLM_URL) == "glm"
    assert ai_provider._web_search_vendor(_DEEPSEEK_URL) == ""


async def test_stream_injects_qwen_enable_search(captured_kwargs):
    await _drain_stream({"base_url": _QWEN_URL, "model": "qwen3.8-max", "api_key": "k", "web_search": True})
    assert captured_kwargs.get("extra_body", {}).get("enable_search") is True


async def test_stream_injects_glm_web_search_tool(captured_kwargs):
    await _drain_stream({"base_url": _GLM_URL, "model": "glm-5.2", "api_key": "k", "web_search": True})
    tools = captured_kwargs.get("tools") or []
    assert any(t.get("type") == "web_search" and t.get("web_search", {}).get("enable") for t in tools)


async def test_stream_no_injection_when_web_search_off(captured_kwargs):
    await _drain_stream({"base_url": _QWEN_URL, "model": "qwen3.8-max", "api_key": "k"})
    assert "tools" not in captured_kwargs
    assert "enable_search" not in (captured_kwargs.get("extra_body") or {})


async def test_stream_unsupported_vendor_not_injected(captured_kwargs):
    await _drain_stream({"base_url": _DEEPSEEK_URL, "model": "deepseek-v4-pro", "api_key": "k", "web_search": True})
    assert "tools" not in captured_kwargs
    assert "enable_search" not in (captured_kwargs.get("extra_body") or {})


async def test_stream_without_profile_uses_global_flag(captured_kwargs, monkeypatch):
    cfg = {"ai_web_search": True, "ai_base_url": _QWEN_URL}
    monkeypatch.setattr(secrets_store, "get_ai_config", lambda key, default=None: cfg.get(key, default))
    monkeypatch.setattr(secrets_store, "get_ai_key", lambda: "k")
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "qwen3.8-max")
    await _drain_stream(None)
    assert captured_kwargs.get("extra_body", {}).get("enable_search") is True


def test_upsert_persists_web_search_flag(isolated_profiles):
    _upsert_ai_profile(AiSettingsIn(model="qwen3.8-max", base_url=_QWEN_URL, profile_id="qwen", web_search=True))
    assert isolated_profiles["ai_profiles"][0]["web_search"] is True


def test_upsert_web_search_defaults_false(isolated_profiles):
    _upsert_ai_profile(AiSettingsIn(model="m1", base_url="https://x/v1"))
    assert isolated_profiles["ai_profiles"][0]["web_search"] is False
