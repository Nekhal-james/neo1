"""The panel's local config is merged over the committed one, not swapped in.

config/webapp.yaml has always documented the merge ("The local file is merged
over this one, so anything here can be overridden per machine"). Until this was
fixed it did not happen: a webapp.local.yaml holding only the password hash and
session secret silently switched off every other value in webapp.yaml -- certs,
port, bridge backend -- which then fell back to dataclass defaults that happened
to agree, hiding the bug.
"""

from __future__ import annotations

import pytest

from neo_webapp import config as wcfg


@pytest.fixture
def layered(tmp_path, monkeypatch):
    base = tmp_path / "webapp.yaml"
    local = tmp_path / "webapp.local.yaml"
    monkeypatch.setattr(wcfg, "DEFAULT_CONFIG", base)
    monkeypatch.setattr(wcfg, "LOCAL_CONFIG", local)
    monkeypatch.delenv("NEO_WEBAPP_CONFIG", raising=False)
    return base, local


def test_secrets_in_the_local_file_do_not_switch_off_the_committed_one(layered):
    """The exact shape of the real bug: local holds only secrets."""
    base, local = layered
    base.write_text(
        "server:\n"
        "  port: 9443\n"
        "  tls:\n"
        "    enabled: true\n"
        "    certfile: certs/panel/panel-cert.pem\n"
        "bridge:\n"
        "  backend: mock\n"
    )
    local.write_text('auth:\n  password_hash: "$argon2id$fake"\n')

    cfg = wcfg.Config.load()
    assert cfg.auth.password_hash == "$argon2id$fake"
    assert cfg.server.port == 9443, "the committed port must survive"
    assert cfg.server.tls.certfile == "certs/panel/panel-cert.pem"
    assert cfg.bridge_backend == "mock"


def test_local_wins_where_the_two_disagree(layered):
    base, local = layered
    base.write_text("server:\n  port: 8443\n")
    local.write_text("server:\n  port: 9999\n")
    assert wcfg.Config.load().server.port == 9999


def test_an_explicit_path_is_used_alone(layered, tmp_path):
    base, local = layered
    base.write_text("server:\n  port: 1111\n")
    local.write_text("server:\n  port: 2222\n")
    named = tmp_path / "named.yaml"
    named.write_text("server:\n  port: 3333\n")
    assert wcfg.Config.load(named).server.port == 3333


def test_no_config_at_all_is_survivable(layered):
    assert wcfg.Config.load().server.port == 8443
