"""P1-15 第二批: 模型归属 + 不静默切换 — 契约测试。

锁死的行为:
① current_attribution() 返回 model/provider/profile_id 归属快照;
② profile_override 上下文内归属跟着覆盖档案走 (机器人临时切档案不失真),
   退出后恢复 — 覆盖是临时的, 激活档案指针不动;
③ _openai_retry_kwargs 只删被拒参数、绝不改 model (失败不换模型),
   且每次删参数都留 warning 日志 (不再静默降级);
④ 机器人 _profile_switch_note: 命中档案必回显"本次用档案 X (模型 Y)",
   未命中返回空串不打扰。
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import openai
import pytest

from app import secrets_store
from app.services import ai_provider

# ── 打桩: 真 secrets.json 落 tmp 数据目录 (与 test_bot_profile_routing 同款,
#    不 monkeypatch load — 那会绕过运行时覆盖合并, 测不到 profile_override) ──

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


def _base_secrets(data_dir: Path) -> None:
    # 必须带真实 ai_profiles 结构 — load() 归一化会把指向不存在档案的指针重置为 default
    _write_secrets(data_dir, {
        "ai_provider": "openai_compat",
        "ai_model": "active-model",
        "ai_active_profile": "p-active",
        "ai_profiles": {
            "p-active": {"name": "激活档", "provider": "openai_compat",
                         "base_url": "https://a/v1", "api_key": "sk-a",
                         "model": "active-model"},
        },
    })


def _bad_request(param: str) -> openai.BadRequestError:
    """构造"某参数不被支持"的 400 (与 test_ai_provider 同款模式)。"""
    body = {"error": {"message": "unrecognized request argument", "param": param}}
    response = httpx.Response(
        400, json=body,
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    return openai.BadRequestError("bad request", response=response, body=body)


class TestAttribution:
    def test_shape(self, data_dir):
        _base_secrets(data_dir)
        assert ai_provider.current_attribution() == {
            "model": "active-model",
            "provider": "openai_compat",
            "profile_id": "p-active",
        }

    def test_under_profile_override(self, data_dir):
        # 机器人临时切档案: 归属快照必须跟着走, 否则台账记的是没用的档案
        _base_secrets(data_dir)
        profile = {"id": "p-qw", "name": "QW", "model": "qwen-max",
                   "provider": "openai_compat", "base_url": "https://x/v1"}
        with ai_provider.profile_override(profile):
            attr = ai_provider.current_attribution()
            assert attr["model"] == "qwen-max"
            assert attr["profile_id"] == "p-qw"
        # 退出后恢复激活档案 — 覆盖是临时的, 指针不动
        assert ai_provider.current_attribution()["profile_id"] == "p-active"
        assert ai_provider.current_attribution()["model"] == "active-model"
        # 覆盖不落盘
        on_disk = json.loads(
            (data_dir / "user_data" / "secrets.json").read_text(encoding="utf-8"))
        assert on_disk["ai_active_profile"] == "p-active"

    def test_override_without_id_falls_back_to_empty(self, data_dir):
        _base_secrets(data_dir)
        with ai_provider.profile_override({"model": "m2"}):
            assert ai_provider.current_attribution()["profile_id"] == ""


class TestRetryNeverSwitchesModel:
    """审计锁: 失败重试只删被拒参数, model 键原封不动 — 绝不静默换模型。"""

    def test_model_untouched_when_dropping_temperature(self):
        kwargs = {"model": "m1", "temperature": 0.3, "max_tokens": 100}
        result = ai_provider._openai_retry_kwargs(_bad_request("temperature"), kwargs)
        assert result is not None
        assert result["model"] == "m1"          # 换的只能是参数, 不能是模型
        assert "temperature" not in result
        assert kwargs["temperature"] == 0.3     # 原 kwargs 不被就地修改

    def test_unrecognized_error_no_retry(self):
        # 认不出的错误 → None (直接报错上抛, 不猜着换任何东西)
        assert ai_provider._openai_retry_kwargs(RuntimeError("boom"), {"model": "m1"}) is None

    def test_drop_is_loud(self, caplog):
        # P1-15: 删参数必须留 warning 日志 (以前是静默降级, 排查无从下手)
        with caplog.at_level("WARNING", logger="app.services.ai_provider"):
            ai_provider._openai_retry_kwargs(
                _bad_request("temperature"), {"model": "m1", "temperature": 0.3})
        text = caplog.text
        assert "P1-15" in text and "temperature" in text and "m1" in text

    def test_no_warning_when_nothing_dropped(self, caplog):
        with caplog.at_level("WARNING", logger="app.services.ai_provider"):
            ai_provider._openai_retry_kwargs(RuntimeError("boom"), {"model": "m1"})
        assert "P1-15" not in caplog.text


class TestBotProfileSwitchNote:
    """机器人切档案回显: 命中必说, 未命中不打扰。"""

    @pytest.mark.parametrize("mod_name", [
        "app.services.wecom_bot_service",
        "app.services.dingtalk_bot_service",
    ])
    def test_none_profile_silent(self, mod_name):
        from importlib import import_module
        mod = import_module(mod_name)
        assert mod._profile_switch_note(None) == ""

    @pytest.mark.parametrize("mod_name", [
        "app.services.wecom_bot_service",
        "app.services.dingtalk_bot_service",
    ])
    def test_matched_profile_echoes(self, mod_name):
        from importlib import import_module
        mod = import_module(mod_name)
        note = mod._profile_switch_note({"id": "p-qw", "name": "千问", "model": "qwen-max"})
        assert "🔀" in note and "千问" in note and "qwen-max" in note

    @pytest.mark.parametrize("mod_name", [
        "app.services.wecom_bot_service",
        "app.services.dingtalk_bot_service",
    ])
    def test_codex_profile_uses_codex_model(self, mod_name):
        from importlib import import_module
        mod = import_module(mod_name)
        note = mod._profile_switch_note({"id": "p-cx", "name": "", "codex_model": "gpt-5.1-codex"})
        assert "gpt-5.1-codex" in note
        assert "p-cx" in note  # name 缺失回退 id
