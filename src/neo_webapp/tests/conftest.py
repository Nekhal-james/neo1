from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from neo_webapp.app import create_app
from neo_webapp.auth import hash_password
from neo_webapp.bridge.mock import MockBridge
from neo_webapp.config import AuthConfig, Config, ServerConfig, TlsConfig

TEST_PASSWORD = "correct-horse-battery"


@pytest.fixture
def config() -> Config:
    cfg = Config(
        server=ServerConfig(tls=TlsConfig(enabled=False)),
        auth=AuthConfig(
            username="admin",
            password_hash=hash_password(TEST_PASSWORD),
            session_secret="test-secret-not-for-use",
        ),
        bridge_backend="mock",
    )
    return cfg


@pytest.fixture
def bridge() -> MockBridge:
    # Short deadman keeps the timing tests quick.
    return MockBridge(deadman_ms=120, source_switch_ms=10)


@pytest.fixture
def client(config: Config, bridge: MockBridge):
    app = create_app(config, bridge)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_client(client):
    res = client.post(
        "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
    )
    assert res.status_code == 200
    return client
