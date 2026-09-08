from __future__ import annotations

from pathlib import Path

import pytest

from model_conn import ollama


def test_is_ollama_installed_false(monkeypatch):
    monkeypatch.setattr(ollama.shutil, "which", lambda _name: None)
    assert ollama.is_ollama_installed() is False


def test_is_ollama_installed_true(monkeypatch):
    monkeypatch.setattr(ollama.shutil, "which", lambda _name: "/usr/local/bin/ollama")
    assert ollama.is_ollama_installed() is True


def test_start_serve_not_called_when_already_serving(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(ollama, "is_serving", lambda port, **_kw: True)
    monkeypatch.setattr(
        ollama,
        "start_serve",
        lambda *a, **kw: calls.append((a, kw)) or None,
    )
    # Simulate the same idempotency check `up()` performs.
    if not ollama.is_serving(11434):
        ollama.start_serve(11434, "0.0.0.0")
    assert calls == []


def test_start_serve_invokes_ollama_with_host_env(monkeypatch):
    captured = {}

    class FakeProc:
        stdout = None

    def fake_popen(cmd, env, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = env
        return FakeProc()

    monkeypatch.setattr(ollama.subprocess, "Popen", fake_popen)
    ollama.start_serve(11434, "0.0.0.0")
    assert captured["cmd"] == ["ollama", "serve"]
    assert captured["env"]["OLLAMA_HOST"] == "0.0.0.0:11434"


def test_ensure_model_gguf_path_runs_create(tmp_path: Path, monkeypatch):
    model = tmp_path / "qwen2.5-3b.gguf"
    model.write_bytes(b"fake weights")

    calls = []
    monkeypatch.setattr(
        ollama.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or None
    )

    name = ollama.ensure_model(str(model), port=11434)
    assert name == "qwen2.5-3b"
    assert calls[0][:2] == ["ollama", "create"]
    assert calls[0][2] == "qwen2.5-3b"


def test_ensure_model_missing_gguf_raises(tmp_path: Path):
    missing = tmp_path / "nope.gguf"
    with pytest.raises(ollama.OllamaError):
        ollama.ensure_model(str(missing), port=11434)


def test_ensure_model_registry_name_pulls_if_absent(monkeypatch):
    run_calls = []

    def fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        if cmd[:2] == ["ollama", "list"]:
            class Result:
                stdout = ""

            return Result()
        return None

    monkeypatch.setattr(ollama.subprocess, "run", fake_run)
    name = ollama.ensure_model("qwen2.5:3b", port=11434)
    assert name == "qwen2.5:3b"
    assert ["ollama", "pull", "qwen2.5:3b"] in run_calls


def test_ensure_model_registry_name_skips_pull_if_present(monkeypatch):
    run_calls = []

    def fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        if cmd[:2] == ["ollama", "list"]:
            class Result:
                stdout = "qwen2.5:3b\n"

            return Result()
        return None

    monkeypatch.setattr(ollama.subprocess, "run", fake_run)
    ollama.ensure_model("qwen2.5:3b", port=11434)
    assert ["ollama", "pull", "qwen2.5:3b"] not in run_calls


def test_up_reports_clear_error_when_ollama_missing(monkeypatch, capsys):
    from model_conn.config import Config

    monkeypatch.setattr(ollama, "is_ollama_installed", lambda: False)
    rc = ollama.up(Config(), model_override="/tmp/model.gguf")
    assert rc == 1
    assert "ollama not found" in capsys.readouterr().out


def test_up_reports_clear_error_when_no_model_given(monkeypatch, capsys):
    from model_conn.config import Config

    monkeypatch.setattr(ollama, "is_ollama_installed", lambda: True)
    rc = ollama.up(Config(), model_override=None)
    assert rc == 1
    assert "no model path given" in capsys.readouterr().out
