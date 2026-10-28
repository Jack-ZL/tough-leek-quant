"""P0-4「通知别烦人、别泄密」测试。

覆盖四块:
1. notify_policy 免打扰窗口判定 (跨天/同日/关闭/非法配置 fail-open/起终相同);
2. secrets_store.redact 敏感值脱敏 (密钥/webhook 地址 → 掩码, 短值不误伤);
3. webhook_adapter._post_wecom 瞬时失败重试 (对齐飞书语义);
4. MonitorRuleEngine cooldown 持久化 (落盘/恢复/陈旧丢弃/损坏容忍)。
"""
from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from app.services import notify_policy, preferences


# ===== 隔离: preferences / secrets_store 都指到 tmp, 且清缓存 =====

@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    pref_path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: pref_path)
    preferences._invalidate_cache()

    from app import secrets_store

    secret_path = tmp_path / "secrets.json"
    monkeypatch.setattr(secrets_store, "_path", lambda: secret_path)
    secrets_store._cache = None
    secrets_store._cache_sig = None
    secrets_store.invalidate_redact_cache()

    yield
    preferences._invalidate_cache()
    secrets_store._cache = None
    secrets_store._cache_sig = None
    secrets_store.invalidate_redact_cache()


def _set_quiet(enabled: bool, start: str, end: str) -> None:
    preferences.set_notify_quiet_enabled(enabled)
    preferences.set_notify_quiet_hours(start, end)


# ===== 1. 免打扰窗口 =====

class TestQuietHours:
    def test_cross_day_inside(self):
        """22:00→08:00 跨天窗口: 23:30 与 07:59 都在窗口内。"""
        _set_quiet(True, "22:00", "08:00")
        assert notify_policy.in_quiet_hours(datetime(2026, 1, 5, 23, 30)) is True
        assert notify_policy.in_quiet_hours(datetime(2026, 1, 5, 7, 59)) is True

    def test_cross_day_outside(self):
        """跨天窗口: 08:00 (终点, 不含) 与 21:59 (起点前) 在窗口外。"""
        _set_quiet(True, "22:00", "08:00")
        assert notify_policy.in_quiet_hours(datetime(2026, 1, 5, 8, 0)) is False
        assert notify_policy.in_quiet_hours(datetime(2026, 1, 5, 21, 59)) is False

    def test_cross_day_boundaries(self):
        """边界语义 [start, end): 22:00 整点在窗口内, 08:00 整点不在。"""
        _set_quiet(True, "22:00", "08:00")
        assert notify_policy.in_quiet_hours(datetime(2026, 1, 5, 22, 0)) is True
        assert notify_policy.in_quiet_hours(datetime(2026, 1, 5, 8, 0)) is False

    def test_same_day_window(self):
        """同日窗口 09:30→15:00。"""
        _set_quiet(True, "09:30", "15:00")
        assert notify_policy.in_quiet_hours(datetime(2026, 1, 5, 12, 0)) is True
        assert notify_policy.in_quiet_hours(datetime(2026, 1, 5, 15, 0)) is False
        assert notify_policy.in_quiet_hours(datetime(2026, 1, 5, 9, 29)) is False

    def test_disabled(self):
        """总开关关闭 → 永远不在免打扰期。"""
        _set_quiet(False, "00:00", "23:59")
        assert notify_policy.in_quiet_hours(datetime(2026, 1, 5, 12, 0)) is False

    def test_start_equals_end_means_no_window(self):
        """起终相同 = 手滑全天静音, 视为未配置窗口 (不静音)。"""
        _set_quiet(True, "22:00", "22:00")
        assert notify_policy.in_quiet_hours(datetime(2026, 1, 5, 23, 0)) is False

    def test_invalid_config_fails_open(self):
        """非法 HH:MM → fail-open 放行 (免打扰配置写错不该吞告警)。"""
        _set_quiet(True, "25:00", "abc")
        assert notify_policy.in_quiet_hours(datetime(2026, 1, 5, 23, 0)) is False

    def test_allow_push_is_inverse(self):
        _set_quiet(True, "22:00", "08:00")
        assert notify_policy.allow_push(datetime(2026, 1, 5, 23, 0)) is False
        assert notify_policy.allow_push(datetime(2026, 1, 5, 12, 0)) is True

    def test_parse_hhmm(self):
        assert notify_policy.parse_hhmm("08:05") is not None
        assert notify_policy.parse_hhmm("24:00") is None
        assert notify_policy.parse_hhmm("8:00") is None  # 必须两位小时
        assert notify_policy.parse_hhmm("") is None


# ===== 2. redact 脱敏 =====

class TestRedact:
    def test_redacts_secret_values(self):
        from app import secrets_store

        secrets_store.save({"tickflow_api_key": "tf_key_abcdefgh123456"})
        out = secrets_store.redact("日志里带 key=tf_key_abcdefgh123456 应被掩码")
        assert "tf_key_abcdefgh123456" not in out
        assert "tf_k" in out and "•" in out  # mask 保留首尾

    def test_redacts_webhook_url_from_preferences(self):
        from app import secrets_store

        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=aaaa-bbbb-cccc"
        preferences.save({"wecom_webhook_url": url})
        secrets_store.invalidate_redact_cache()
        out = secrets_store.redact(f"推送失败 url={url}")
        assert url not in out

    def test_short_values_not_redacted(self):
        """长度 <8 的值不参与脱敏 — 防止把正常日志打成筛子。"""
        from app import secrets_store

        secrets_store.save({"tickflow_api_key": "short"})
        assert secrets_store.redact("short 保持原样") == "short 保持原样"

    def test_empty_text_passthrough(self):
        from app import secrets_store

        assert secrets_store.redact("") == ""

    def test_cache_invalidated_on_save(self):
        """save() 后缓存立即失效: 新 Key 下一条日志就要被脱敏。"""
        from app import secrets_store

        secrets_store.save({"tickflow_api_key": "old_key_1234567890"})
        assert "old_key_1234567890" not in secrets_store.redact("x old_key_1234567890")
        secrets_store.save({"tickflow_api_key": "new_key_1234567890"})
        assert "new_key_1234567890" not in secrets_store.redact("x new_key_1234567890")


# ===== 3. 企微重试 =====

class _FakeResp:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TestWecomRetry:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        # 退避 sleep 打桩为 no-op, 测试不真等
        monkeypatch.setattr(time, "sleep", lambda s: None)

    def test_transient_failures_then_success(self, monkeypatch):
        """两次网络异常后成功 → 共 3 次调用, 返回 True。"""
        import httpx

        from app.services import webhook_adapter

        calls = {"n": 0}

        def fake_post(url, json=None, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("boom")
            return _FakeResp(200, {"errcode": 0})

        monkeypatch.setattr(httpx, "post", fake_post)
        assert webhook_adapter._post_wecom("https://x", {}) is True
        assert calls["n"] == 3

    def test_5xx_retried(self, monkeypatch):
        """5xx 属瞬时失败 → 重试满 3 次后返回 False。"""
        import httpx

        from app.services import webhook_adapter

        calls = {"n": 0}

        def fake_post(url, json=None, timeout=None):
            calls["n"] += 1
            return _FakeResp(502, text="bad gateway")

        monkeypatch.setattr(httpx, "post", fake_post)
        assert webhook_adapter._post_wecom("https://x", {}) is False
        assert calls["n"] == 3

    def test_4xx_no_retry(self, monkeypatch):
        """4xx 客户端错误 (key 失效等) → 不重试, 1 次调用。"""
        import httpx

        from app.services import webhook_adapter

        calls = {"n": 0}

        def fake_post(url, json=None, timeout=None):
            calls["n"] += 1
            return _FakeResp(404, text="not found")

        monkeypatch.setattr(httpx, "post", fake_post)
        assert webhook_adapter._post_wecom("https://x", {}) is False
        assert calls["n"] == 1

    def test_errcode_nonzero_no_retry(self, monkeypatch):
        """errcode≠0 业务失败 (限频 45009 等) → 不重试。"""
        import httpx

        from app.services import webhook_adapter

        calls = {"n": 0}

        def fake_post(url, json=None, timeout=None):
            calls["n"] += 1
            return _FakeResp(200, {"errcode": 45009, "errmsg": "rate limit"})

        monkeypatch.setattr(httpx, "post", fake_post)
        assert webhook_adapter._post_wecom("https://x", {}) is False
        assert calls["n"] == 1

    def test_max_attempts_one_for_diagnostics(self, monkeypatch):
        """诊断模式 max_attempts=1: 瞬时失败也不重试。"""
        import httpx

        from app.services import webhook_adapter

        calls = {"n": 0}

        def fake_post(url, json=None, timeout=None):
            calls["n"] += 1
            raise httpx.ConnectError("boom")

        monkeypatch.setattr(httpx, "post", fake_post)
        assert webhook_adapter._post_wecom("https://x", {}, max_attempts=1) is False
        assert calls["n"] == 1


# ===== 4. cooldown 持久化 =====

class TestCooldownPersist:
    def _engine(self, data_dir):
        from app.strategy.monitor import MonitorRuleEngine

        eng = MonitorRuleEngine()
        eng.set_data_dir(data_dir)
        return eng

    def test_round_trip(self, tmp_path):
        """触发 → flush → 新引擎恢复: 冷却期内的键仍在。"""
        eng1 = self._engine(tmp_path)
        now = time.time()
        eng1._mark_fire(("rule1", "SH600000", "cross"), now)
        eng1.flush_cooldown()

        path = tmp_path / "user_data" / "monitor_last_fire.json"
        assert path.exists()

        eng2 = self._engine(tmp_path)
        assert eng2._last_fire.get(("rule1", "SH600000", "cross")) == pytest.approx(now)

    def test_stale_entries_dropped(self, tmp_path):
        """超过 30 天的记录加载时丢弃, 防文件无限膨胀。"""
        path = tmp_path / "user_data" / "monitor_last_fire.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        stale = time.time() - 31 * 86400
        fresh = time.time() - 10
        path.write_text(json.dumps([
            ["old_rule", "SZ000001", "cross", stale],
            ["new_rule", "SZ000002", "cross", fresh],
        ]), encoding="utf-8")

        eng = self._engine(tmp_path)
        assert ("old_rule", "SZ000001", "cross") not in eng._last_fire
        assert ("new_rule", "SZ000002", "cross") in eng._last_fire

    def test_corrupt_file_tolerated(self, tmp_path):
        """损坏 JSON → 容忍, 退回空 dict, 不抛。"""
        path = tmp_path / "user_data" / "monitor_last_fire.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json!!", encoding="utf-8")

        eng = self._engine(tmp_path)
        assert eng._last_fire == {}

    def test_flush_skipped_when_clean(self, tmp_path):
        """无脏数据时 flush 不写盘 (节流语义)。"""
        eng = self._engine(tmp_path)
        eng.flush_cooldown()
        assert not (tmp_path / "user_data" / "monitor_last_fire.json").exists()

    def test_no_data_dir_memory_only(self):
        """未注入 data_dir → 纯内存模式, flush 静默跳过。"""
        from app.strategy.monitor import MonitorRuleEngine

        eng = MonitorRuleEngine()
        eng._mark_fire(("r", "s", "e"), time.time())
        eng.flush_cooldown()  # 不应抛
        assert ("r", "s", "e") in eng._last_fire


# ===== 5. 接线断言 (防回归: 闸门/兜底 flush 被误删) =====

class TestWiring:
    def test_quote_service_gated(self):
        from pathlib import Path

        import app.services.quote_service as qs

        src = Path(qs.__file__).read_text(encoding="utf-8")
        # 三处扇出 (规则 webhook / 模拟盘成交 / 系统通知) 都要过免打扰闸门
        assert src.count("notify_policy.allow_push()") == 3

    def test_main_flushes_on_shutdown(self):
        from pathlib import Path

        import app.main as m

        src = Path(m.__file__).read_text(encoding="utf-8")
        assert "flush_cooldown()" in src
        assert "install_secret_redaction()" in src
