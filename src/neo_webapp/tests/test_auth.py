"""The panel drives servos and rewrites campus data, so the gate matters."""

from __future__ import annotations

import pytest

from .conftest import TEST_PASSWORD

PROTECTED = [
    ("get", "/api/state"),
    ("get", "/api/sources"),
    ("get", "/api/auth/me"),
    ("post", "/api/sources/set"),
    ("post", "/api/head/center"),
    ("post", "/api/system/estop"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_endpoints_require_a_session(client, method, path):
    kwargs = {"json": {}} if method == "post" else {}
    res = getattr(client, method)(path, **kwargs)
    assert res.status_code == 401


def test_health_is_public_and_leaks_nothing(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"ok", "backend", "configured"}


def test_login_rejects_a_wrong_password(client):
    res = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert res.status_code == 401


def test_login_rejects_a_wrong_username(client):
    res = client.post(
        "/api/auth/login", json={"username": "root", "password": TEST_PASSWORD}
    )
    assert res.status_code == 401


def test_login_then_access(auth_client):
    assert auth_client.get("/api/auth/me").json() == {"user": "admin"}


def test_logout_clears_the_session(auth_client):
    auth_client.post("/api/auth/logout")
    assert auth_client.get("/api/auth/me").status_code == 401


def test_lockout_after_repeated_failures(client, config):
    for _ in range(config.auth.max_attempts):
        client.post("/api/auth/login", json={"username": "admin", "password": "x"})
    # Correct credentials are refused too: the lockout is on the client, not the
    # guess, so an attacker cannot probe past it by getting one right.
    res = client.post(
        "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
    )
    assert res.status_code == 429


def test_websocket_is_closed_without_a_session(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/state") as ws:
            ws.receive_text()
    assert exc.value.code == 1008


def test_unconfigured_panel_refuses_login(client, config):
    config.auth.password_hash = ""
    res = client.post(
        "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
    )
    assert res.status_code == 503
