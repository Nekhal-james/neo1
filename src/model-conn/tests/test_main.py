from __future__ import annotations

from pathlib import Path

import pytest

from model_conn.__main__ import main


def _config_argv(tmp_path: Path, extra: list[str]) -> list[str]:
    cfg_path = tmp_path / "model_conn.yaml"
    cfg_path.write_text(
        "receiver:\n  endpoints: []\n  probe_timeout_s: 0.01\n", encoding="utf-8"
    )
    return ["--config", str(cfg_path), *extra]


def test_connection_status_dispatch_with_no_endpoints(tmp_path, capsys):
    rc = main(_config_argv(tmp_path, ["--connection:status"]))
    assert rc == 1
    assert "DOWN" in capsys.readouterr().out


def test_connection_ping_dispatch_with_no_endpoints(tmp_path, capsys):
    rc = main(_config_argv(tmp_path, ["--connection:ping"]))
    assert rc == 1
    assert "% loss" in capsys.readouterr().out


def test_conflicting_modes_error(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(_config_argv(tmp_path, ["--connection:status", "--connection:ping"]))
    assert exc.value.code == 2


def test_empty_prompt_errors(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(_config_argv(tmp_path, ["--prompt", "   "]))
    assert exc.value.code == 2


def test_no_mode_prints_help(capsys):
    rc = main([])
    assert rc == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_webapp_config_flag_is_not_swallowed_by_model_conn(monkeypatch):
    """The bug this guards: --config used to be defined on model_conn's own
    parser, so it was consumed before `--webapp` forwarding ever saw it --
    neo_webapp's own --config flag could never be reached this way."""
    calls = []

    def fake_webapp_main(argv):
        calls.append(argv)
        return 0

    import model_conn.__main__ as mm

    monkeypatch.setattr(mm, "_cmd_webapp", lambda subcommand, extra: fake_webapp_main(extra))

    rc = main(["--webapp", "up", "--config", "/some/webapp/config.yaml", "--no-tls"])
    assert rc == 0
    assert "--config" in calls[0]
    assert "/some/webapp/config.yaml" in calls[0]
    assert "--no-tls" in calls[0]


def test_webapp_bad_subcommand(capsys):
    rc = main(["--webapp", "bogus"])
    assert rc == 1
    assert "usage: neo --webapp" in capsys.readouterr().out


def test_webapp_missing_subcommand(capsys):
    rc = main(["--webapp"])
    assert rc == 1
    assert "usage: neo --webapp" in capsys.readouterr().out


def test_webapp_dispatches_setup(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "neo_webapp.scripts.setup_admin.main", lambda argv: calls.append(argv) or 0
    )
    rc = main(["--webapp", "setup", "--foo"])
    assert rc == 0
    assert calls == [["--foo"]]


def test_webapp_dispatches_devcert(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "neo_webapp.scripts.make_dev_cert.main", lambda argv: calls.append(argv) or 0
    )
    rc = main(["--webapp", "devcert"])
    assert rc == 0
    assert calls == [[]]


def test_tls_bad_subcommand(capsys):
    rc = main(["--tls", "bogus"])
    assert rc == 1
    assert "usage: neo --tls" in capsys.readouterr().out


def test_tls_missing_subcommand(capsys):
    rc = main(["--tls"])
    assert rc == 1
    assert "usage: neo --tls" in capsys.readouterr().out


def test_tls_init_dispatch(monkeypatch, tmp_path):
    calls = []
    import model_conn.__main__ as mm

    monkeypatch.setattr(mm, "_cmd_tls_init", lambda cfg: calls.append(cfg) or 0)

    cfg_path = tmp_path / "model_conn.yaml"
    cfg_path.write_text("", encoding="utf-8")
    rc = main(["--tls", "init", "--config", str(cfg_path)])
    assert rc == 0
    assert len(calls) == 1


def test_tls_config_flag_is_recognized(monkeypatch, tmp_path):
    """--tls doesn't forward to another program, so it's allowed to own
    --config directly -- unlike --webapp, there's no ambiguity to guard."""
    seen_cfg_paths = []
    import model_conn.__main__ as mm

    def fake_init(cfg):
        seen_cfg_paths.append(cfg.source_path)
        return 0

    monkeypatch.setattr(mm, "_cmd_tls_init", fake_init)

    cfg_path = tmp_path / "custom.yaml"
    cfg_path.write_text("host:\n  ollama_port: 9999\n", encoding="utf-8")
    rc = main(["--tls", "init", "--config", str(cfg_path)])
    assert rc == 0
    assert seen_cfg_paths == [cfg_path]


def _tls_config(tmp_path: Path) -> Path:
    """A config whose cert paths all land in tmp_path, so nothing touches the
    repo's real certs/ directory."""
    cfg_path = tmp_path / "model_conn.yaml"
    cfg_path.write_text(
        "receiver:\n"
        "  endpoints: []\n"
        "tls:\n"
        f"  ca_cert: {tmp_path}/ca-cert.pem\n"
        f"  ca_key: {tmp_path}/ca-key.pem\n"
        f"  server_cert: {tmp_path}/server-cert.pem\n"
        f"  server_key: {tmp_path}/server-key.pem\n"
        f"  client_cert: {tmp_path}/client-cert.pem\n"
        f"  client_key: {tmp_path}/client-key.pem\n"
        f"  panel_cert: {tmp_path}/panel-cert.pem\n"
        f"  panel_key: {tmp_path}/panel-key.pem\n",
        encoding="utf-8",
    )
    return cfg_path


def test_tls_panel_is_a_known_subcommand(monkeypatch, tmp_path):
    import model_conn.__main__ as mm

    calls = []
    monkeypatch.setattr(mm, "_cmd_tls_panel", lambda cfg: calls.append(cfg) or 0)

    rc = main(["--tls", "panel", "--config", str(_tls_config(tmp_path))])
    assert rc == 0
    assert len(calls) == 1


def test_tls_panel_without_a_ca_says_it_needs_the_key(tmp_path, capsys):
    """The failure to be helpful about: copying only ca-cert.pem to the Pi and
    not ca-key.pem looks like everything is in place, but signing needs the
    key."""
    pytest.importorskip("cryptography")

    rc = main(["--tls", "panel", "--config", str(_tls_config(tmp_path))])
    assert rc == 1
    err = capsys.readouterr().err
    assert "neo --tls init" in err
    assert "key" in err


def test_tls_panel_issues_a_cert_after_init(tmp_path, capsys):
    pytest.importorskip("cryptography")
    cfg_path = _tls_config(tmp_path)

    assert main(["--tls", "init", "--config", str(cfg_path)]) == 0
    capsys.readouterr()

    assert main(["--tls", "panel", "--config", str(cfg_path)]) == 0
    assert (tmp_path / "panel-cert.pem").exists()
    assert (tmp_path / "panel-key.pem").exists()

    out = capsys.readouterr().out
    # It has to tell you the two things that are not discoverable from the
    # filesystem: where to point the panel, and that the CA still needs
    # installing on the browsing device.
    assert "webapp.local.yaml" in out
    assert "docs/security.md" in out
