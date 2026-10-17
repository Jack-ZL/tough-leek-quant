"""IM 机器人 "@机器人 模型名称 标的名称" 按档案路由的契约测试。

核心契约:
① match_profile_in_text 按档案名/模型名 (忽略大小写, 最长别名优先) 命中并移除别名,
   未命中返回 (None, 原文) — 调用方回退当前生效模型;
② profile_override 仅在当前上下文生效: current_ai_model/get_ai_key 等读取点切换到
   指定档案, 退出后恢复, 且 secrets.json 扁平键不被写盘;
③ 机器人消息解析链: 移除模型别名后 "@机器人 模型 标的" 仍能解析出标的名称。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import secrets_store
from app.services import ai_provider
from app.services.dingtalk_bot_service import _parse_stock_analysis_msg


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    yield tmp_path


def _write_secrets(data_dir: Path, data: dict) -> None:
    p = data_dir / "user_data" / "secrets.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    secrets_store._cache = None
    secrets_store._cache_sig = None


def _two_profiles(data_dir: Path) -> None:
    _write_secrets(data_dir, {
        "ai_provider": "openai_compat",
        "ai_base_url": "https://api.deepseek.com",
        "ai_api_key": "sk-ds",
        "ai_model": "deepseek-v4-pro",
        "ai_active_profile": "deepseek",
        "ai_profiles": {
            "deepseek": {
                "name": "DeepSeek", "provider": "openai_compat",
                "base_url": "https://api.deepseek.com", "api_key": "sk-ds",
                "model": "deepseek-v4-pro",
            },
            "qwen": {
                "name": "通义千问", "provider": "openai_compat",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "api_key": "sk-qwen", "model": "qwen3.8-max",
            },
            "glm": {
                "name": "GLM-4.6", "provider": "openai_compat",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "api_key": "sk-glm", "model": "glm-4.6",
            },
            "kimi": {
                "name": "Kimi-K2", "provider": "openai_compat",
                "base_url": "https://api.moonshot.cn/v1",
                "api_key": "sk-kimi", "model": "kimi-k2-turbo",
            },
        },
    })


# ── ① 别名命中 ──────────────────────────────────────────────────────────────

def test_match_by_model_name_and_strip(data_dir: Path) -> None:
    _two_profiles(data_dir)
    prof, cleaned = ai_provider.match_profile_in_text("@机器人 qwen3.8-max 贵州茅台")
    assert prof is not None and prof["id"] == "qwen"
    assert _parse_stock_analysis_msg(cleaned) == "贵州茅台"


def test_match_by_profile_name_case_insensitive(data_dir: Path) -> None:
    _two_profiles(data_dir)
    prof, cleaned = ai_provider.match_profile_in_text("DEEPSEEK-V4-PRO 贵州茅台")
    assert prof is not None and prof["id"] == "deepseek"
    assert cleaned == "贵州茅台"


def test_longest_alias_wins(data_dir: Path) -> None:
    _two_profiles(data_dir)
    # "DeepSeek" 是档案名, "deepseek-v4-pro" 是模型名 — 同时出现时最长别名优先
    prof, _ = ai_provider.match_profile_in_text("deepseek deepseek-v4-pro 贵州茅台")
    assert prof is not None and prof["id"] == "deepseek"


def test_no_match_falls_back_to_none(data_dir: Path) -> None:
    _two_profiles(data_dir)
    prof, cleaned = ai_provider.match_profile_in_text("@机器人 贵州茅台")
    assert prof is None
    assert cleaned == "@机器人 贵州茅台"
    assert _parse_stock_analysis_msg(cleaned) == "贵州茅台"


# ── ①b 厂商关键词 (友好叫法) ────────────────────────────────────────────────

@pytest.mark.parametrize("keyword,expected", [
    ("千问", "qwen"),
    ("QWEN", "qwen"),
    ("通义", "qwen"),
    ("deepseek", "deepseek"),
    ("智谱", "glm"),
    ("glm", "glm"),
    ("kimi", "kimi"),
    ("moonshot", "kimi"),
])
def test_vendor_keyword_routes_to_profile(data_dir: Path, keyword: str, expected: str) -> None:
    _two_profiles(data_dir)
    prof, cleaned = ai_provider.match_profile_in_text(f"@机器人 {keyword} 贵州茅台")
    assert prof is not None and prof["id"] == expected
    assert _parse_stock_analysis_msg(cleaned) == "贵州茅台"


def test_keyword_miss_when_vendor_not_configured(data_dir: Path) -> None:
    _two_profiles(data_dir)
    # 未配置 gemini/gpt 档案 → 关键词不命中, 回退激活模型
    for text in ("@机器人 gemini 贵州茅台", "@机器人 gpt 贵州茅台"):
        prof, cleaned = ai_provider.match_profile_in_text(text)
        assert prof is None
        assert cleaned == text


def test_openai_compat_provider_not_mistaken_as_gpt(data_dir: Path) -> None:
    _two_profiles(data_dir)
    # 所有档案 provider 都是 openai_compat (协议标记), 不应因此把 "openai" 关键词
    # 误路由到某个非 OpenAI 档案
    prof, _ = ai_provider.match_profile_in_text("@机器人 openai 贵州茅台")
    assert prof is None


def test_exact_alias_wins_over_keyword(data_dir: Path) -> None:
    _two_profiles(data_dir)
    # "kimi-k2-turbo" 是精确模型名, 应整段移除而不是只移除关键词 "kimi"
    prof, cleaned = ai_provider.match_profile_in_text("kimi-k2-turbo 贵州茅台")
    assert prof is not None and prof["id"] == "kimi"
    assert cleaned == "贵州茅台"


# ──  上下文覆盖 ────────────────────────────────────────────────────────────

def test_profile_override_scoped_to_context(data_dir: Path) -> None:
    _two_profiles(data_dir)
    assert ai_provider.current_ai_model() == "deepseek-v4-pro"
    assert secrets_store.get_ai_key() == "sk-ds"

    qwen = next(p for p in secrets_store.list_ai_profiles() if p["id"] == "qwen")
    with ai_provider.profile_override(qwen):
        assert ai_provider.current_ai_provider() == "openai_compat"
        assert ai_provider.current_ai_model() == "qwen3.8-max"
        assert secrets_store.get_ai_key() == "sk-qwen"

    assert ai_provider.current_ai_model() == "deepseek-v4-pro"
    assert secrets_store.get_ai_key() == "sk-ds"
    # 覆盖不落盘: 扁平键仍是激活档案 deepseek
    on_disk = json.loads((data_dir / "user_data" / "secrets.json").read_text(encoding="utf-8"))
    assert on_disk["ai_model"] == "deepseek-v4-pro"
    assert on_disk["ai_active_profile"] == "deepseek"


def test_profile_override_none_is_noop(data_dir: Path) -> None:
    _two_profiles(data_dir)
    with ai_provider.profile_override(None):
        assert ai_provider.current_ai_model() == "deepseek-v4-pro"


async def test_override_survives_await(data_dir: Path) -> None:
    """覆盖在 await 之后仍生效 (contextvar 随协程上下文传播)。"""
    import asyncio

    _two_profiles(data_dir)
    qwen = next(p for p in secrets_store.list_ai_profiles() if p["id"] == "qwen")
    with ai_provider.profile_override(qwen):
        await asyncio.sleep(0)
        assert ai_provider.current_ai_model() == "qwen3.8-max"
    assert ai_provider.current_ai_model() == "deepseek-v4-pro"
