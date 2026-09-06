"""preferences mtime 缓存测试 — 读盘去重, 且外部修改/自身写入后立即可见。"""
from __future__ import annotations

import json
import os

import pytest

from app.services import preferences


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    yield path
    preferences._invalidate_cache()


def _patched_loads(monkeypatch, counter: dict):
    real_loads = json.loads

    def _counting(text):
        counter["loads"] += 1
        return real_loads(text)

    monkeypatch.setattr(preferences.json, "loads", _counting)


def test_second_load_hits_cache_without_disk_parse(_isolated, monkeypatch):
    _isolated.write_text(json.dumps({"realtime_quotes_enabled": True}), encoding="utf-8")
    counter = {"loads": 0}
    _patched_loads(monkeypatch, counter)

    assert preferences.load()["realtime_quotes_enabled"] is True
    assert preferences.load()["realtime_quotes_enabled"] is True
    assert counter["loads"] == 1, "签名未变时第二次 load 不得重复读盘+parse"


def test_external_file_change_invalidates_cache(_isolated, monkeypatch):
    _isolated.write_text(json.dumps({"realtime_quote_interval": 6.0}), encoding="utf-8")
    assert preferences.load()["realtime_quote_interval"] == 6.0

    _isolated.write_text(json.dumps({"realtime_quote_interval": 3.0}), encoding="utf-8")
    # 同尺寸修改且 mtime 粒度可能不变时, 显式推进 mtime 模拟真实场景
    st = _isolated.stat()
    os.utime(_isolated, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    assert preferences.load()["realtime_quote_interval"] == 3.0


def test_save_then_load_sees_merged_values(_isolated):
    _isolated.write_text(json.dumps({"a": 1}), encoding="utf-8")
    out = preferences.save({"b": 2})
    assert out == {"a": 1, "b": 2}
    assert preferences.load() == {"a": 1, "b": 2}


def test_interval_setter_invalidates_cache(_isolated):
    preferences.set_realtime_quote_interval(2.0)
    assert preferences.load()["realtime_quote_interval"] == 2.0


def test_load_returns_copy_not_cached_object(_isolated):
    _isolated.write_text(json.dumps({"k": [1, 2]}), encoding="utf-8")
    first = preferences.load()
    first["k"].append(3)
    first["extra"] = True
    again = preferences.load()
    assert again == {"k": [1, 2]}


def test_mining_schedule_defaults_are_disabled(_isolated):
    assert preferences.get_mining_schedule() == {
        "mining_schedule_enabled": False,
        "mining_schedule_weekday": 4,
        "mining_budget_profile": "balanced",
    }


def test_mining_schedule_invalid_stored_values_fail_closed(_isolated):
    _isolated.write_text(
        json.dumps(
            {
                "mining_schedule_enabled": "false",
                "mining_schedule_weekday": True,
                "mining_budget_profile": None,
            }
        ),
        encoding="utf-8",
    )

    assert preferences.get_mining_schedule() == {
        "mining_schedule_enabled": False,
        "mining_schedule_weekday": 4,
        "mining_budget_profile": "balanced",
    }


def test_mining_schedule_setter_saves_group_once(monkeypatch):
    calls = []
    monkeypatch.setattr(preferences, "save", lambda updates: calls.append(updates) or updates)

    result = preferences.set_mining_schedule(True, 2, "strict")

    assert result == {
        "mining_schedule_enabled": True,
        "mining_schedule_weekday": 2,
        "mining_budget_profile": "strict",
    }
    assert calls == [result]


@pytest.mark.parametrize("weekday", [-1, 5, True])
def test_mining_schedule_setter_rejects_invalid_weekday(weekday):
    with pytest.raises(ValueError, match="weekday"):
        preferences.set_mining_schedule(True, weekday, "balanced")


def test_mining_schedule_setter_rejects_invalid_profile():
    with pytest.raises(ValueError, match="profile"):
        preferences.set_mining_schedule(True, 4, "exploratory")


def test_external_push_channel_whitelists_include_dingtalk_custom_and_email(_isolated):
    assert preferences.set_webhook_default_channels([
        "custom", "dingtalk", "email", "custom", "unsupported",
    ]) == ["custom", "dingtalk", "email"]
    assert preferences.set_review_push_channels([
        "email", "wecom", "dingtalk", "unsupported",
    ]) == ["email", "wecom", "dingtalk"]


def test_email_smtp_config_falls_back_from_malformed_stored_values(_isolated):
    preferences.save({
        "email_smtp_config": {
            "host": " smtp.example.com ",
            "port": "invalid",
            "security": "invalid",
            "to_addresses": "alerts@example.com",
        },
    })

    assert preferences.get_email_smtp_config() == {
        "host": "smtp.example.com",
        "port": 465,
        "security": "ssl",
        "username": "",
        "from_address": "",
        "to_addresses": [],
    }


def test_notification_getters_support_tough_nested_structure(_isolated):
    preferences.save({
        "push_notification": {
            "feishu": {
                "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/x",
                "feishu_webhook_secret": "fs-secret",
            },
            "wecom": {
                "wecom_webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=w-key",
            },
            "dingtalk": {
                "dingtalk_webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=d-token",
                "dingtalk_webhook_secret": "dt-secret",
            },
            "email": {
                "email_smtp_host": "smtp.example.com",
                "email_smtp_port": 465,
                "email_use_ssl": True,
                "email_sender": "bot@example.com",
                "email_auth_code": "legacy-auth",
                "email_recipients": ["alerts@example.com"],
            },
        },
        "smart_bots": {
            "wecom": {
                "wecom_bot_id": "wecom-bot",
                "wecom_bot_secret": "wecom-secret",
                "wecom_bot_enabled": True,
            },
            "dingtalk": {
                "dingtalk_bot_id": "ding-bot",
                "dingtalk_bot_secret": "ding-secret",
                "dingtalk_bot_enabled": True,
            },
        },
        "alert_default_channels": ["wecom", "dingtalk", "email"],
    })

    assert preferences.get_feishu_webhook_url().endswith("/x")
    assert preferences.get_feishu_webhook_secret() == "fs-secret"
    assert "key=w-key" in preferences.get_wecom_webhook_url()
    assert "access_token=d-token" in preferences.get_dingtalk_webhook_url()
    assert preferences.get_dingtalk_webhook_secret() == "dt-secret"
    assert preferences.get_wecom_bot_id() == "wecom-bot"
    assert preferences.get_wecom_bot_secret() == "wecom-secret"
    assert preferences.get_wecom_bot_enabled() is True
    assert preferences.get_dingtalk_bot_id() == "ding-bot"
    assert preferences.get_dingtalk_bot_secret() == "ding-secret"
    assert preferences.get_dingtalk_bot_enabled() is True
    assert preferences.get_webhook_default_channels() == ["wecom", "dingtalk", "email"]
    assert preferences.get_alert_default_channels() == ["wecom", "dingtalk", "email"]
    assert preferences.get_email_smtp_config() == {
        "host": "smtp.example.com",
        "port": 465,
        "security": "ssl",
        "username": "bot@example.com",
        "from_address": "bot@example.com",
        "to_addresses": ["alerts@example.com"],
    }


def test_webhook_default_channels_mirrors_alert_key(_isolated):
    saved = preferences.set_webhook_default_channels(["wecom", "dingtalk", "unsupported"])
    assert saved == ["wecom", "dingtalk"]
    payload = preferences.load()
    assert payload["webhook_default_channels"] == ["wecom", "dingtalk"]
    assert payload["alert_default_channels"] == ["wecom", "dingtalk"]


def test_email_password_prefers_secrets_then_legacy_auth_code(_isolated, monkeypatch):
    preferences.save({
        "push_notification": {
            "email": {
                "email_auth_code": "legacy-auth-code",
            },
        },
    })
    monkeypatch.setattr("app.secrets_store.get_email_smtp_password", lambda: "")
    assert preferences.get_email_smtp_password() == "legacy-auth-code"

    monkeypatch.setattr("app.secrets_store.get_email_smtp_password", lambda: "new-secret-password")
    assert preferences.get_email_smtp_password() == "new-secret-password"
