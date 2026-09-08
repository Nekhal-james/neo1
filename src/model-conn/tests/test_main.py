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
