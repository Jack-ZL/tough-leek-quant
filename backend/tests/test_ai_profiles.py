"""AI 模型档案 (多模型切换) 的存储层与 API 契约测试。

核心契约:
① 旧格式自动迁移 — 遗留数组 profiles → dict; 扁平键无对应档案且指针失效时捕获为 default;
② 扁平 ai_* 键 = 当前生效配置 — 激活档案把字段镜像写入扁平键, 所有既有读取点
   (get_ai_key/get_ai_config*/ai_provider) 零改动即生效;
③ 档案中缺失的字段激活时从扁平键清除, 不残留上一个模型的值;
④ POST /ai (保存生效配置) 同步写回当前档案, 库与实际配置不脱节;
⑤ 激活中的档案不可删除; DELETE /ai 一键清空连档案库一起清。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import secrets_store
from app.api import settings as settings_api


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    # _sync_ai_runtime 会写全局 settings.* — 快照恢复, 防污染其他测试
    originals = {k: getattr(settings, k, None) for k in settings_api._AI_RUNTIME_DEFAULTS}
    yield tmp_path
    for k, v in originals.items():
        setattr(settings, k, v)


def _write_secrets(data_dir: Path, data: dict) -> None:
    p = data_dir / "user_data" / "secrets.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    secrets_store._cache = None
    secrets_store._cache_sig = None


# ── ① 迁移 ──────────────────────────────────────────────────────────────────

def test_legacy_array_and_flat_keys_migrate(data_dir: Path) -> None:
    _write_secrets(data_dir, {
        "ai_provider": "openai_compat",
        "ai_base_url": "https://relay.example/v1",
        "ai_api_key": "sk-qwen",
        "ai_model": "qwen-max",
        "ai_web_search": True,
        "ai_profiles": [{
            "name": "DeepSeek", "id": "deepseek", "provider": "openai_compat",
            "base_url": "https://api.deepseek.com", "api_key": "sk-ds", "model": "deepseek-v4-pro",
        }],
        "tickflow_api_key": "tf",
    })
    data = secrets_store.load()
    assert set(data["ai_profiles"]) == {"deepseek", "default"}
    # 扁平键 (qwen) 才是生效配置 → 指针必须指向捕获出的 default, 而非数组首项
    assert data["ai_active_profile"] == "default"
    assert data["ai_profiles"]["default"]["model"] == "qwen-max"
    assert data["tickflow_api_key"] == "tf"  # 非 AI 键不受影响


def test_new_format_dict_passes_through(data_dir: Path) -> None:
    _write_secrets(data_dir, {
        "ai_model": "m1", "ai_base_url": "https://a/v1", "ai_provider": "openai_compat",
        "ai_profiles": {"p1": {"name": "P1", "model": "m1", "base_url": "https://a/v1"}},
        "ai_active_profile": "p1",
    })
    data = secrets_store.load()
    assert data["ai_active_profile"] == "p1"
    assert data["ai_profiles"]["p1"]["name"] == "P1"


def test_unconfigured_file_yields_empty_library(data_dir: Path) -> None:
    _write_secrets(data_dir, {"tickflow_api_key": "tf"})
    data = secrets_store.load()
    assert data["ai_profiles"] == {} and data["ai_active_profile"] == ""


# ── ②③ 激活镜像 ─────────────────────────────────────────────────────────────

def test_activate_mirrors_profile_to_flat_keys(data_dir: Path) -> None:
    _write_secrets(data_dir, {
        "ai_model": "qwen-max", "ai_base_url": "https://relay/v1",
        "ai_api_key": "sk-qwen", "ai_web_search": True, "ai_max_output_tokens": 8192,
        "ai_profiles": {
            "default": {"name": "qwen", "provider": "openai_compat", "model": "qwen-max",
                        "base_url": "https://relay/v1", "api_key": "sk-qwen", "web_search": True,
                        "max_output_tokens": 8192},
            "deepseek": {"name": "DeepSeek", "provider": "openai_compat", "model": "deepseek-v4-pro",
                         "base_url": "https://api.deepseek.com", "api_key": "sk-ds"},
        },
        "ai_active_profile": "default",
    })
    profile = secrets_store.activate_ai_profile("deepseek")
    assert profile is not None and profile["model"] == "deepseek-v4-pro"

    # 既有读取点零改动即生效
    assert secrets_store.get_ai_config("ai_model") == "deepseek-v4-pro"
    assert secrets_store.get_ai_key() == "sk-ds"
    # deepseek 档案没有 web_search/max_output_tokens → 扁平键被清除, 不残留 qwen 的值
    data = secrets_store.load()
    assert "ai_web_search" not in data and "ai_max_output_tokens" not in data
    assert secrets_store.get_ai_config_bool("ai_web_search", False) is False
    assert data["ai_active_profile"] == "deepseek"

    # 切回 default → 字段恢复
    secrets_store.activate_ai_profile("default")
    data = secrets_store.load()
    assert data["ai_web_search"] is True and data["ai_max_output_tokens"] == 8192
    assert secrets_store.get_ai_key() == "sk-qwen"


def test_activate_missing_profile_returns_none(data_dir: Path) -> None:
    _write_secrets(data_dir, {})
    assert secrets_store.activate_ai_profile("nope") is None


# ── upsert / delete 存储层 ──────────────────────────────────────────────────

def test_upsert_generates_id_and_preserves_key(data_dir: Path) -> None:
    _write_secrets(data_dir, {})
    profiles, pid = secrets_store.upsert_ai_profile("", {
        "name": "千问 Max", "provider": "openai_compat",
        "base_url": "https://relay/v1", "model": "qwen-max", "api_key": "sk-1",
    })
    assert pid == "千问-max" and len(profiles) == 1

    # api_key=None → 保留原值 (编辑时不必重填)
    profiles, pid2 = secrets_store.upsert_ai_profile(pid, {"model": "qwen-plus", "api_key": None})
    assert pid2 == pid
    entry = next(p for p in profiles if p["id"] == pid)
    assert entry["model"] == "qwen-plus" and entry["api_key"] == "sk-1"

    # 同名新建 → id 自动加后缀, 不覆盖
    profiles, pid3 = secrets_store.upsert_ai_profile("", {"name": "千问 Max", "model": "other"})
    assert pid3 != pid and len(profiles) == 2


def test_delete_non_active_keeps_pointer(data_dir: Path) -> None:
    _write_secrets(data_dir, {
        "ai_model": "m1", "ai_base_url": "https://a/v1", "ai_provider": "openai_compat",
        "ai_profiles": {
            "p1": {"name": "P1", "model": "m1", "base_url": "https://a/v1", "provider": "openai_compat"},
            "p2": {"name": "P2", "model": "m2"},
        },
        "ai_active_profile": "p1",
    })
    rest = secrets_store.delete_ai_profile("p2")
    assert [p["id"] for p in rest] == ["p1"]
    assert secrets_store.get_active_ai_profile_id() == "p1"


# ── API 层 ──────────────────────────────────────────────────────────────────

def test_api_profile_crud_and_activate_flow(data_dir: Path) -> None:
    _write_secrets(data_dir, {
        "ai_provider": "openai_compat", "ai_base_url": "https://relay/v1",
        "ai_api_key": "sk-qwen", "ai_model": "qwen-max",
    })

    # GET /ai/profiles: 迁移出的 default 档案
    resp = settings_api.get_ai_profiles()
    assert resp["active"] == "default"
    assert resp["profiles"][0]["model"] == "qwen-max"

    # POST /ai/profiles (apply=true): 新建并激活 deepseek
    resp = settings_api.save_ai_profile(settings_api.AiProfileIn(
        name="DeepSeek", provider="openai_compat", base_url="https://api.deepseek.com",
        api_key="sk-ds", model="deepseek-v4-pro", apply=True,
    ))
    assert resp["ok"] and resp["saved_id"] == "deepseek"
    assert resp["ai_model"] == "deepseek-v4-pro" and resp["ai_configured"] is True
    assert resp["ai_active_profile"] == "deepseek"
    assert secrets_store.get_ai_key() == "sk-ds"

    # 运行时内存同步 (settings.* 镜像)
    from app.config import settings as _s
    assert _s.ai_model == "deepseek-v4-pro" and _s.ai_api_key == "sk-ds"

    # 激活回 default
    resp = settings_api.activate_ai_profile(settings_api.AiProfileActivateIn(id="default"))
    assert resp["ai_model"] == "qwen-max" and resp["ai_active_profile"] == "default"
    assert _s.ai_model == "qwen-max"

    # 激活不存在的档案 → 404
    with pytest.raises(HTTPException) as exc:
        settings_api.activate_ai_profile(settings_api.AiProfileActivateIn(id="ghost"))
    assert exc.value.status_code == 404

    # 删除激活中的档案 → 400; 删除非激活 → ok
    with pytest.raises(HTTPException) as exc:
        settings_api.delete_ai_profile("default")
    assert exc.value.status_code == 400
    resp = settings_api.delete_ai_profile("deepseek")
    assert resp["ok"] and [p["id"] for p in resp["profiles"]] == ["default"]

    # 非法入参 → 400
    with pytest.raises(HTTPException):
        settings_api.save_ai_profile(settings_api.AiProfileIn(provider="gemini"))
    with pytest.raises(HTTPException):
        settings_api.save_ai_profile(settings_api.AiProfileIn(
            provider="openai_compat", protocol="grpc", base_url="https://a/v1", model="m"))
    with pytest.raises(HTTPException):
        settings_api.save_ai_profile(settings_api.AiProfileIn(
            provider="openai_compat", base_url="https://a/v1", model="m", max_output_tokens=0))


def test_save_ai_settings_syncs_active_profile(data_dir: Path) -> None:
    _write_secrets(data_dir, {
        "ai_provider": "openai_compat", "ai_base_url": "https://relay/v1",
        "ai_api_key": "sk-qwen", "ai_model": "qwen-max",
    })
    resp = settings_api.save_ai_settings(settings_api.AiSettingsIn(
        provider="openai_compat", base_url="https://relay/v1", api_key="sk-qwen",
        model="qwen3-max", user_agent="", profile_name="千问旗舰",
    ))
    assert resp["ok"] and resp["ai_model"] == "qwen3-max"
    # 档案库同步: default 档案跟随扁平键更新, 名称生效
    profiles = {p["id"]: p for p in resp["ai_profiles"]}
    assert profiles["default"]["model"] == "qwen3-max"
    assert profiles["default"]["name"] == "千问旗舰"
    assert resp["ai_active_profile"] == "default"


def test_clear_ai_settings_wipes_profiles(data_dir: Path) -> None:
    _write_secrets(data_dir, {
        "ai_provider": "openai_compat", "ai_base_url": "https://relay/v1",
        "ai_api_key": "sk-qwen", "ai_model": "qwen-max",
    })
    assert secrets_store.get_active_ai_profile_id() == "default"
    resp = settings_api.clear_ai_settings()
    assert resp["ok"]
    data = secrets_store.load()
    assert data["ai_profiles"] == {} and data["ai_active_profile"] == ""
    assert "ai_model" not in data and "ai_api_key" not in data


def test_get_settings_overview_includes_slim_profiles(data_dir: Path) -> None:
    _write_secrets(data_dir, {
        "ai_provider": "openai_compat", "ai_base_url": "https://relay/v1",
        "ai_api_key": "sk-qwen", "ai_model": "qwen-max",
    })
    overview = settings_api.get_settings()
    assert overview["ai_active_profile"] == "default"
    slim = overview["ai_profiles"][0]
    assert slim["id"] == "default" and slim["model"] == "qwen-max"
    # 精简字段不下发密钥
    assert "api_key" not in slim and "base_url" not in slim
