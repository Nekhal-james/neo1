"""Forgot password: one-time recovery codes."""

from __future__ import annotations

import yaml
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from neo_webapp.auth import hash_recovery_code, new_recovery_code, normalize_recovery_code
from neo_webapp.scripts import setup_admin

from .conftest import TEST_PASSWORD

NEW_PASSWORD = "remembered-this-time"


def _with_code(config, tmp_path) -> tuple[str, object]:
    code = new_recovery_code()
    config.auth.recovery_hash = hash_recovery_code(code)
    path = tmp_path / "webapp.local.yaml"
    path.write_text("server:\n  port: 9443\n", encoding="utf-8")
    config.secrets_path = path
    return code, path


def _recover(client, code, new=NEW_PASSWORD):
    return client.post("/api/auth/recover", json={"recovery_code": code, "new_password": new})


def _login(client, password):
    return client.post("/api/auth/login", json={"username": "admin", "password": password})


# -- the code itself --------------------------------------------------------


def test_codes_are_long_unambiguous_and_different():
    codes = {new_recovery_code() for _ in range(50)}
    assert len(codes) == 50
    for code in codes:
        assert len(code) == 23 and code.count("-") == 3
        assert not set(code) & set("0O1IL")


def test_case_spaces_and_dashes_do_not_matter():
    code = new_recovery_code()
    typed = " " + code.lower().replace("-", " ") + " "
    assert normalize_recovery_code(typed) == normalize_recovery_code(code)


# -- resetting --------------------------------------------------------------


def test_forgot_password_resets_signs_in_and_issues_a_new_code(client, config, tmp_path):
    code, path = _with_code(config, tmp_path)
    res = _recover(client, code.lower().replace("-", " "))
    assert res.status_code == 200, res.json()
    fresh = res.json()["recovery_code"]
    assert fresh and fresh != code

    # Signed in by the reset itself.
    assert client.get("/api/auth/me").status_code == 200

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["server"]["port"] == 9443
    assert PasswordHasher().verify(saved["auth"]["password_hash"], NEW_PASSWORD)
    assert PasswordHasher().verify(saved["auth"]["recovery_hash"], normalize_recovery_code(fresh))
    text = path.read_text(encoding="utf-8")
    assert NEW_PASSWORD not in text and fresh not in text


def test_the_new_password_works_and_the_old_one_does_not(client, config, tmp_path):
    code, _ = _with_code(config, tmp_path)
    assert _recover(client, code).status_code == 200
    client.post("/api/auth/logout")
    assert _login(client, TEST_PASSWORD).status_code == 401
    assert _login(client, NEW_PASSWORD).status_code == 200


def test_a_code_works_once(client, config, tmp_path):
    code, _ = _with_code(config, tmp_path)
    assert _recover(client, code).status_code == 200
    assert _recover(client, code, new="and-again-a-new-one").status_code == 401


def test_the_replacement_code_works(client, config, tmp_path):
    code, _ = _with_code(config, tmp_path)
    fresh = _recover(client, code).json()["recovery_code"]
    assert _recover(client, fresh, new="a-third-password-here").status_code == 200


def test_a_reset_signs_out_every_other_browser(auth_client, config, tmp_path):
    code, _ = _with_code(config, tmp_path)
    old_cookie = auth_client.cookies.get("neo_session")
    with TestClient(auth_client.app) as stranger:
        assert _recover(stranger, code).status_code == 200
    other = TestClient(auth_client.app)
    other.cookies.set("neo_session", old_cookie)
    assert other.get("/api/auth/me").status_code == 401


def test_a_wrong_code_changes_nothing(client, config, tmp_path):
    _code, path = _with_code(config, tmp_path)
    before = (config.auth.password_hash, config.auth.recovery_hash)
    res = _recover(client, new_recovery_code())
    assert res.status_code == 401
    assert (config.auth.password_hash, config.auth.recovery_hash) == before
    assert "auth" not in path.read_text(encoding="utf-8")
    assert _login(client, TEST_PASSWORD).status_code == 200


def test_wrong_codes_hit_the_login_lockout(client, config, tmp_path):
    code, _ = _with_code(config, tmp_path)
    for _ in range(config.auth.max_attempts):
        assert _recover(client, new_recovery_code()).status_code == 401
    # Locked out: even the right code, and even the right password, must wait.
    assert _recover(client, code).status_code == 429
    assert _login(client, TEST_PASSWORD).status_code == 429


def test_failed_logins_also_lock_the_reset_form(client, config, tmp_path):
    code, _ = _with_code(config, tmp_path)
    for _ in range(config.auth.max_attempts):
        _login(client, "not-it-at-all")
    assert _recover(client, code).status_code == 429


def test_no_code_set_points_at_the_robot(client, config, tmp_path):
    _with_code(config, tmp_path)
    config.auth.recovery_hash = ""
    res = _recover(client, new_recovery_code())
    assert res.status_code == 503
    assert "neo --webapp setup" in res.json()["detail"]


def test_a_short_new_password_is_refused_before_the_code_is_spent(client, config, tmp_path):
    code, _ = _with_code(config, tmp_path)
    assert _recover(client, code, new="short").status_code == 400
    assert _recover(client, code).status_code == 200


# -- making a code from the panel -------------------------------------------


def test_generating_a_code_needs_a_session_and_the_password(client, auth_client, config, tmp_path):
    _with_code(config, tmp_path)
    with TestClient(auth_client.app) as anonymous:
        assert anonymous.post("/api/auth/recovery", json={"current_password": TEST_PASSWORD}).status_code == 401
    res = auth_client.post("/api/auth/recovery", json={"current_password": "wrong-wrong-wrong"})
    assert res.status_code == 403


def test_a_generated_code_replaces_the_old_one(auth_client, config, tmp_path):
    old, path = _with_code(config, tmp_path)
    res = auth_client.post("/api/auth/recovery", json={"current_password": TEST_PASSWORD})
    assert res.status_code == 200
    new = res.json()["recovery_code"]
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert PasswordHasher().verify(saved["auth"]["recovery_hash"], normalize_recovery_code(new))

    auth_client.post("/api/auth/logout")
    assert _recover(auth_client, old).status_code == 401
    assert _recover(auth_client, new).status_code == 200


def test_recovery_status(auth_client, config, tmp_path):
    _with_code(config, tmp_path)
    assert auth_client.get("/api/auth/recovery").json() == {"configured": True}
    config.auth.recovery_hash = ""
    assert auth_client.get("/api/auth/recovery").json() == {"configured": False}


def test_health_still_says_nothing_about_recovery(client):
    assert set(client.get("/api/health").json()) == {"ok", "backend", "configured"}


# -- neo --webapp setup -----------------------------------------------------


def test_setup_prints_a_working_recovery_code(tmp_path, monkeypatch, capsys):
    path = tmp_path / "webapp.yaml"
    path.write_text("server:\n  port: 9443\n", encoding="utf-8")
    monkeypatch.setenv("NEO_WEBAPP_CONFIG", str(path))
    monkeypatch.setenv("NEO_ADMIN_PASSWORD", "provisioned-password")

    assert setup_admin.main([]) == 0
    code = next(line.split(": ", 1)[1] for line in capsys.readouterr().out.splitlines()
                if line.startswith("Recovery code: "))

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["server"]["port"] == 9443
    assert PasswordHasher().verify(saved["auth"]["recovery_hash"], normalize_recovery_code(code))
    assert PasswordHasher().verify(saved["auth"]["password_hash"], "provisioned-password")
    assert code not in path.read_text(encoding="utf-8")
