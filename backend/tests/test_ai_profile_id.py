"""模型 ID (profile_id) 读写: 前端表单字段 <-> ai_profiles[].id (智能机器人触发词匹配用)。"""
import pytest

from app import secrets_store
from app.api.settings import AiSettingsIn, _upsert_ai_profile


@pytest.fixture
def isolated_profiles(monkeypatch):
    store: dict = {"ai_profiles": []}
    monkeypatch.setattr(secrets_store, "load", lambda: store)
    monkeypatch.setattr(secrets_store, "save", lambda updates: store.update(updates) or store)
    return store


def _profile(store: dict) -> dict:
    return store["ai_profiles"][0]


def test_upsert_stores_profile_id_as_id(isolated_profiles):
    _upsert_ai_profile(AiSettingsIn(model="qwen3.8-max", base_url="https://x/v1", profile_id="qwen"))
    assert _profile(isolated_profiles)["id"] == "qwen"


def test_upsert_strips_profile_id_whitespace(isolated_profiles):
    _upsert_ai_profile(AiSettingsIn(model="m1", base_url="https://x/v1", profile_id="  glm  "))
    assert _profile(isolated_profiles)["id"] == "glm"


def test_upsert_empty_profile_id_keeps_existing(isolated_profiles):
    _upsert_ai_profile(AiSettingsIn(model="m1", base_url="https://x/v1", profile_id="deepseek"))
    _upsert_ai_profile(AiSettingsIn(model="m1", base_url="https://x/v1"))
    assert _profile(isolated_profiles)["id"] == "deepseek"


def test_upsert_profile_id_overwrites_existing(isolated_profiles):
    _upsert_ai_profile(AiSettingsIn(model="m1", base_url="https://x/v1", profile_id="deepseek"))
    _upsert_ai_profile(AiSettingsIn(model="m1", base_url="https://x/v1", profile_id="ds2"))
    assert _profile(isolated_profiles)["id"] == "ds2"


def test_upsert_without_profile_id_has_no_id_key(isolated_profiles):
    _upsert_ai_profile(AiSettingsIn(model="m1", base_url="https://x/v1"))
    assert "id" not in _profile(isolated_profiles)
