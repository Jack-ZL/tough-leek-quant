"""「未设密码」闸门与 /setup 的回环限定 — 内网不再等同于可信。

背景: 旧实现用 is_local_network(整个 RFC1918) 放行未设密码的请求, 而默认
HOST=0.0.0.0 出厂即无密码, 导致同一局域网任意设备可无认证调用全部 API
(含明文密钥回显 / SSRF / 策略代码执行)。且 _client_ip 对私网直连 peer 采信
X-Forwarded-For, 内网攻击者伪造 `XFF: 127.0.0.1` 即可冒充本机。

本测试锁定新行为: 直连 peer 与 XFF 解析结果都必须是回环地址才放行。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import auth as auth_api
from app.services import api_gateway


# ── is_loopback 边界 ──────────────────────────────────────────────

def test_is_loopback_boundaries():
    f = api_gateway.is_loopback
    for loopback in ("127.0.0.1", "127.1.2.3", "127.255.255.254", "::1", "localhost"):
        assert f(loopback) is True, loopback
    # 内网段 / 公网 / 畸形值 / 空值均非回环
    for other in ("10.0.0.1", "192.168.1.1", "172.16.0.1", "8.8.8.8",
                  "127.0.0.999", "127.0.0", "127.0.0.1.2", "", None, "testclient"):
        assert f(other) is False, repr(other)


# ── _is_loopback_request: XFF 伪造与同机反代 ──────────────────────

def _fake_request(peer: str | None, xff: str | None = None):
    headers = {"x-forwarded-for": xff} if xff else {}
    client = SimpleNamespace(host=peer) if peer else None
    return SimpleNamespace(client=client, headers=headers)


@pytest.mark.parametrize(
    ("peer", "xff", "expected"),
    [
        ("127.0.0.1", None, True),            # 本机直连
        ("::1", None, True),
        ("192.168.1.50", None, False),        # 内网设备
        ("192.168.1.50", "127.0.0.1", False), # 内网伪造 XFF 冒充本机
        ("10.0.0.9", "127.0.0.1", False),
        ("127.0.0.1", "203.0.113.7", False),  # 同机反代转发的公网客户端
        ("127.0.0.1", "192.168.1.50", False), # 同机反代转发的内网客户端
        (None, None, False),                  # 缺 client → fail-closed
    ],
)
def test_is_loopback_request(peer, xff, expected):
    assert auth_api._is_loopback_request(_fake_request(peer, xff)) is expected


# ── 中间件端到端: 未设密码时仅回环放行 ────────────────────────────

@pytest.fixture
def gate_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """挂载真实 auth_middleware 的裸 app, 密码置为未配置。"""
    import app.main as main_module
    from app.services import auth as auth_service

    monkeypatch.setattr(auth_service, "is_configured", lambda: False)

    app = FastAPI()
    app.add_middleware(BaseHTTPMiddleware, dispatch=main_module.auth_middleware)

    @app.get("/api/settings/ai/profiles")
    def profiles():
        return {"leaked": True}

    return TestClient(app)


def _get(client: TestClient, peer: str, xff: str | None = None):
    headers = {"X-Forwarded-For": xff} if xff else {}
    return client.get("/api/settings/ai/profiles", headers=headers)


def test_no_password_loopback_allowed(gate_client: TestClient):
    client = TestClient(gate_client.app, client=("127.0.0.1", 23456))
    assert client.get("/api/settings/ai/profiles").status_code == 200


def test_no_password_lan_peer_rejected(gate_client: TestClient):
    client = TestClient(gate_client.app, client=("192.168.1.50", 23456))
    resp = client.get("/api/settings/ai/profiles")
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "NOT_INITIALIZED"
    # 前端 main.tsx 依赖该文案跳转 /login, 不得改动措辞
    assert "尚未初始化访问密码" in body["detail"]


def test_no_password_lan_spoofed_xff_rejected(gate_client: TestClient):
    """内网直连 + 伪造 XFF: 127.0.0.1 — 旧实现会放行, 必须拒绝。"""
    client = TestClient(gate_client.app, client=("192.168.1.50", 23456))
    resp = client.get(
        "/api/settings/ai/profiles", headers={"X-Forwarded-For": "127.0.0.1"}
    )
    assert resp.status_code == 403


def test_no_password_public_via_same_host_proxy_rejected(gate_client: TestClient):
    """同机反代(直连 127.0.0.1)转发公网客户端 — XFF 解析后必须拒绝。"""
    client = TestClient(gate_client.app, client=("127.0.0.1", 23456))
    resp = client.get(
        "/api/settings/ai/profiles", headers={"X-Forwarded-For": "203.0.113.7"}
    )
    assert resp.status_code == 403


# ── /setup 端点: 仅回环可设密码 ───────────────────────────────────

def _call_setup(monkeypatch: pytest.MonkeyPatch, peer: str | None, xff: str | None):
    from app.services import auth as auth_service

    monkeypatch.setattr(auth_service, "is_configured", lambda: False)
    set_calls: list[str] = []
    monkeypatch.setattr(auth_service, "set_password", lambda p: set_calls.append(p))

    request = _fake_request(peer, xff)
    try:
        auth_api.setup_password(auth_api.PasswordIn(password="secret123"), request)
    except HTTPException as exc:
        return exc.status_code, set_calls
    return 200, set_calls


def test_setup_allows_loopback(monkeypatch: pytest.MonkeyPatch):
    status, calls = _call_setup(monkeypatch, "127.0.0.1", None)
    assert status == 200 and calls == ["secret123"]


def test_setup_rejects_lan_and_spoof(monkeypatch: pytest.MonkeyPatch):
    status, calls = _call_setup(monkeypatch, "192.168.1.50", None)
    assert status == 403 and calls == []
    # 内网伪造 XFF 抢占密码
    status, calls = _call_setup(monkeypatch, "192.168.1.50", "127.0.0.1")
    assert status == 403 and calls == []


def test_setup_rejects_public(monkeypatch: pytest.MonkeyPatch):
    status, calls = _call_setup(monkeypatch, "203.0.113.7", None)
    assert status == 403 and calls == []
