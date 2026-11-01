"""P1-15 第三批: 密钥大声报错 + 实测记录 — 契约测试。

锁死的行为:
① 401/403 鉴权失败 → ERROR 日志 + 密钥健康记录 (设置页红横幅数据源);
   其他状态码 (429/500) 不记 — 横幅只对"密钥/权限问题"负责;
② Codex 审计承诺: OPENAI_API_KEY 绝不进子进程环境 (登录态过期不静默切按量付费);
③ 连通性测试结果记进档案 (last_verified_*): 成功盖"通过"章、失败盖"失败"章,
   档案不存在/未传 id 静默跳过; 成功同时清掉该模型的鉴权失败记录。
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import openai
import pytest

from app import secrets_store
from app.api import settings as settings_api
from app.services import ai_provider


@pytest.fixture(autouse=True)
def _clean_health():
    """健康记录是模块级内存态 — 每条测试前后清空, 避免串场。"""
    ai_provider.clear_key_health()
    yield
    ai_provider.clear_key_health()


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


def _secrets_with_profile(data_dir: Path) -> None:
    _write_secrets(data_dir, {
        "ai_provider": "openai_compat",
        "ai_model": "m-active",
        "ai_active_profile": "p1",
        "ai_profiles": {
            "p1": {"name": "档案一", "provider": "openai_compat",
                   "base_url": "https://a/v1", "api_key": "sk-a", "model": "m-active"},
        },
    })


def _status_error(status: int) -> openai.APIStatusError:
    body = {"error": {"message": f"upstream said {status}"}}
    response = httpx.Response(
        status, json=body,
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    return openai.APIStatusError("boom", response=response, body=body)


# ================================================================
# ① 密钥健康记录器
# ================================================================

class TestKeyHealthRecorder:
    def test_401_recorded_with_attribution(self, data_dir, caplog):
        _secrets_with_profile(data_dir)
        with caplog.at_level("ERROR", logger="app.services.ai_provider"):
            msg = ai_provider._format_openai_error(_status_error(401))
        assert "401" in msg
        events = ai_provider.key_health()
        assert len(events) == 1
        event = events[0]
        assert event["status"] == "401"
        assert event["model"] == "m-active"       # 归属快照: 哪个模型/档案出的事
        assert event["profile_id"] == "p1"
        assert event["ts"] and event["detail"]
        # 大声: ERROR 日志带 P1-15 标记
        assert "P1-15" in caplog.text and "鉴权失败" in caplog.text

    def test_403_recorded(self, data_dir):
        _secrets_with_profile(data_dir)
        ai_provider._format_openai_error(_status_error(403))
        assert ai_provider.key_health()[0]["status"] == "403"

    @pytest.mark.parametrize("status", [429, 500, 404])
    def test_non_auth_status_not_recorded(self, data_dir, status):
        # 限流/内错/模型不存在不是密钥问题 — 不进红横幅, 避免狼来了
        _secrets_with_profile(data_dir)
        ai_provider._format_openai_error(_status_error(status))
        assert ai_provider.key_health() == []

    def test_clear_by_model_and_all(self, data_dir):
        _secrets_with_profile(data_dir)
        ai_provider.record_auth_failure(401, "模型A的事")
        ai_provider.record_auth_failure(401, "模型B的事")
        # 手工记录没有 model 归属时按当前激活模型记 — 两条都是 m-active
        ai_provider.clear_key_health("m-active")
        assert ai_provider.key_health() == []
        ai_provider.record_auth_failure(403, "又来一次")
        ai_provider.clear_key_health()  # 空参 = 全清
        assert ai_provider.key_health() == []

    def test_newest_first_and_capped(self, data_dir):
        _secrets_with_profile(data_dir)
        for i in range(25):
            ai_provider.record_auth_failure(401, f"第{i}次")
        events = ai_provider.key_health()
        assert len(events) == ai_provider._KEY_HEALTH_MAX
        assert events[0]["detail"] == "第24次"  # 新→旧

    def test_endpoint_shape(self, data_dir):
        _secrets_with_profile(data_dir)
        ai_provider.record_auth_failure(401, "详情")
        payload = settings_api.get_ai_key_health()
        assert payload["ok"] is True
        assert payload["events"][0]["detail"] == "详情"


# ================================================================
# ② Codex 审计: 绝不把按量 Key 递给子进程
# ================================================================

class TestCodexNoPaygFallback:
    def test_allowlist_excludes_api_keys(self):
        # 审计锁: 允许列表里不许出现任何按量密钥变量 —
        # 登录态过期时 codex 子进程拿不到 OPENAI_API_KEY, 无法静默切计费通道
        banned = {"OPENAI_API_KEY", "AZURE_API_KEY", "ANTHROPIC_API_KEY"}
        assert banned.isdisjoint(set(ai_provider._CODEX_ENV_ALLOWLIST))

    def test_process_env_never_leaks_openai_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-payg-secret")
        monkeypatch.setattr(ai_provider, "_read_codex_config", lambda: {})
        env = ai_provider._codex_process_env(tmp_path)
        assert "OPENAI_API_KEY" not in env
        assert "sk-payg-secret" not in json.dumps(env)

    def test_expired_hint_promises_no_payg(self):
        hint = ai_provider._codex_auth_expired_hint()
        assert "过期" in hint and "codex login" in hint
        assert "按量" in hint and "不会换模型" in hint  # 审计承诺写进用户可见文案


# ================================================================
# ③ 连通性测试结果记进档案 (last_verified_*)
# ================================================================

class TestProfileVerificationStamp:
    @pytest.mark.asyncio
    async def test_success_stamps_profile(self, data_dir, monkeypatch):
        _secrets_with_profile(data_dir)

        async def fake_probe(**kwargs):
            return "OK"

        monkeypatch.setattr(ai_provider, "probe_ai_config", fake_probe)
        result = await settings_api.test_ai_settings(settings_api.AiTestIn(
            provider="openai_compat", base_url="https://a/v1",
            api_key="sk-a", model="m-active", profile_id="p1",
        ))
        assert result["ok"] is True
        prof = next(p for p in secrets_store.list_ai_profiles() if p["id"] == "p1")
        assert prof["last_verified_ok"] is True
        assert prof["last_verified_model"] == "m-active"
        assert prof["last_verified_via"] == "真实调用 200"
        assert prof["last_verified_at"]
        # 档案原有字段不被盖章冲掉
        assert prof["api_key"] == "sk-a" and prof["name"] == "档案一"

    @pytest.mark.asyncio
    async def test_success_clears_key_health_for_model(self, data_dir, monkeypatch):
        _secrets_with_profile(data_dir)
        ai_provider.record_auth_failure(401, "之前的失败")
        # record_auth_failure 用当前归属 → model=m-active, 与测试模型一致应被清除
        async def fake_probe(**kwargs):
            return "OK"

        monkeypatch.setattr(ai_provider, "probe_ai_config", fake_probe)
        await settings_api.test_ai_settings(settings_api.AiTestIn(
            provider="openai_compat", base_url="https://a/v1",
            api_key="sk-a", model="m-active", profile_id="p1",
        ))
        assert ai_provider.key_health() == []

    @pytest.mark.asyncio
    async def test_failure_stamps_error(self, data_dir, monkeypatch):
        _secrets_with_profile(data_dir)

        async def boom(**kwargs):
            raise RuntimeError("API Key 无效")

        monkeypatch.setattr(ai_provider, "probe_ai_config", boom)
        monkeypatch.setattr(ai_provider, "_is_openai_transport_error", lambda exc: False)
        result = await settings_api.test_ai_settings(settings_api.AiTestIn(
            provider="openai_compat", base_url="https://a/v1",
            api_key="sk-a", model="m-active", profile_id="p1",
        ))
        assert result["ok"] is False
        prof = next(p for p in secrets_store.list_ai_profiles() if p["id"] == "p1")
        assert prof["last_verified_ok"] is False
        assert "API Key 无效" in prof["last_verified_error"]

    @pytest.mark.asyncio
    async def test_codex_success_via_label(self, data_dir, monkeypatch):
        _secrets_with_profile(data_dir)

        async def fake_probe(**kwargs):
            return "OK"

        monkeypatch.setattr(ai_provider, "probe_ai_config", fake_probe)
        await settings_api.test_ai_settings(settings_api.AiTestIn(
            provider="codex_cli", model="gpt-5.1-codex", profile_id="p1",
        ))
        prof = next(p for p in secrets_store.list_ai_profiles() if p["id"] == "p1")
        assert prof["last_verified_via"] == "Codex 真实生成"

    @pytest.mark.asyncio
    async def test_unknown_or_missing_profile_id_is_noop(self, data_dir, monkeypatch):
        _secrets_with_profile(data_dir)

        async def fake_probe(**kwargs):
            return "OK"

        monkeypatch.setattr(ai_provider, "probe_ai_config", fake_probe)
        # 不存在的档案 id → 只测不记, 不报错不新建
        r1 = await settings_api.test_ai_settings(settings_api.AiTestIn(
            provider="openai_compat", base_url="https://a/v1",
            api_key="sk-a", model="m-active", profile_id="no-such",
        ))
        assert r1["ok"] is True
        # 未传 id (草稿还没存成档案) → 同样只测不记
        r2 = await settings_api.test_ai_settings(settings_api.AiTestIn(
            provider="openai_compat", base_url="https://a/v1",
            api_key="sk-a", model="m-active",
        ))
        assert r2["ok"] is True
        ids = [p["id"] for p in secrets_store.list_ai_profiles()]
        assert ids == ["p1"]  # 没有多出新档案
