"""Changing the admin password from the panel."""

from __future__ import annotations

import yaml
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from neo_webapp.app import create_app
from neo_webapp.config import Config

from .conftest import TEST_PASSWORD

NEW_PASSWORD = "a-brand-new-passphrase"


def _secrets_file(config, tmp_path):
    path = tmp_path / "webapp.local.yaml"
    path.write_text("server:\n  port: 9443\n", encoding="utf-8")
    config.secrets_path = path
    return path


def _change(client, current=TEST_PASSWORD, new=NEW_PASSWORD):
    return client.post(
        "/api/auth/password", json={"current_password": current, "new_password": new}
    )


def test_it_needs_a_session(client, config, tmp_path):
    path = _secrets_file(config, tmp_path)
    assert _change(client).status_code == 401
    assert "password_hash" not in path.read_text(encoding="utf-8")


def test_change_saves_the_hash_and_keeps_other_settings(auth_client, config, tmp_path):
    path = _secrets_file(config, tmp_path)
    res = _change(auth_client)
    assert res.status_code == 200, res.json()

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["server"]["port"] == 9443
    assert PasswordHasher().verify(saved["auth"]["password_hash"], NEW_PASSWORD)
    assert NEW_PASSWORD not in path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".webapp.local.yaml.*.tmp"))


def test_this_browser_stays_signed_in(auth_client, config, tmp_path):
    _secrets_file(config, tmp_path)
    assert _change(auth_client).status_code == 200
    assert auth_client.get("/api/auth/me").status_code == 200


def test_other_browsers_are_signed_out(client, auth_client, config, tmp_path):
    _secrets_file(config, tmp_path)
    old_cookie = auth_client.cookies.get("neo_session")
    assert _change(auth_client).status_code == 200

    other = TestClient(auth_client.app)
    other.cookies.set("neo_session", old_cookie)
    assert other.get("/api/auth/me").status_code == 401


def test_the_new_password_signs_in_and_the_old_one_does_not(client, config, tmp_path):
    _secrets_file(config, tmp_path)
    client.post("/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD})
    assert _change(client).status_code == 200
    client.post("/api/auth/logout")

    old = client.post("/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD})
    new = client.post("/api/auth/login", json={"username": "admin", "password": NEW_PASSWORD})
    assert (old.status_code, new.status_code) == (401, 200)


def test_a_restart_reads_the_new_password(auth_client, config, tmp_path):
    path = tmp_path / "webapp.yaml"
    path.write_text(yaml.safe_dump({
        "auth": {"password_hash": config.auth.password_hash, "session_secret": "x" * 40},
        "server": {"tls": {"enabled": False}},
    }), encoding="utf-8")
    config.secrets_path = path
    assert _change(auth_client).status_code == 200

    restarted = Config.load(path)
    assert restarted.secrets_path == path
    with TestClient(create_app(restarted)) as fresh:
        res = fresh.post("/api/auth/login", json={"username": "admin", "password": NEW_PASSWORD})
        assert res.status_code == 200


def test_a_wrong_current_password_changes_nothing(auth_client, config, tmp_path):
    path = _secrets_file(config, tmp_path)
    before = config.auth.password_hash
    res = _change(auth_client, current="not-the-password")
    assert res.status_code == 403
    assert config.auth.password_hash == before
    assert "password_hash" not in path.read_text(encoding="utf-8")


def test_wrong_current_passwords_hit_the_login_lockout(auth_client, config, tmp_path):
    _secrets_file(config, tmp_path)
    for _ in range(config.auth.max_attempts):
        assert _change(auth_client, current="guess-guess-guess").status_code == 403
    assert _change(auth_client).status_code == 429


def test_a_short_or_unchanged_password_is_refused(auth_client, config, tmp_path):
    _secrets_file(config, tmp_path)
    assert _change(auth_client, new="short").status_code == 400
    res = _change(auth_client, new=TEST_PASSWORD)
    assert res.status_code == 400 and "different" in res.json()["detail"]


def test_without_a_config_file_it_says_so(auth_client, config):
    config.secrets_path = None
    assert _change(auth_client).status_code == 503


def test_load_points_secrets_at_the_local_file_never_the_committed_one(monkeypatch):
    from neo_webapp import config as config_module

    monkeypatch.delenv("NEO_WEBAPP_CONFIG", raising=False)
    assert Config.load().secrets_path == config_module.LOCAL_CONFIG
