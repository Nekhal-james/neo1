from __future__ import annotations

from pathlib import Path

from model_conn.config import Config, discover_model, MODELS_DIR


def test_defaults_with_no_files(tmp_path: Path):
    cfg = Config.load(tmp_path / "nonexistent.yaml")
    assert cfg.host.ollama_port == 11434
    assert cfg.host.default_model_path == ""
    assert cfg.receiver.endpoints == []
    assert cfg.receiver.ping_count == 5
    assert cfg.tls.enabled is False
    assert cfg.status_file == "var/model_conn/status.json"


def test_load_from_explicit_path(tmp_path: Path):
    path = tmp_path / "model_conn.yaml"
    path.write_text(
        """
host:
  default_model_path: /models/qwen.gguf
  ollama_port: 9999
receiver:
  endpoints:
    - {name: eth, host: 10.0.0.5, port: 1111}
    - {name: wifi, host: neo-brain.local, port: 2222}
  ping_count: 10
tls:
  enabled: true
status_file: custom/status.json
""",
        encoding="utf-8",
    )
    cfg = Config.load(path)
    assert cfg.host.default_model_path == "/models/qwen.gguf"
    assert cfg.host.ollama_port == 9999
    assert len(cfg.receiver.endpoints) == 2
    assert cfg.receiver.endpoints[0].name == "eth"
    assert cfg.receiver.endpoints[0].host == "10.0.0.5"
    assert cfg.receiver.ping_count == 10
    assert cfg.tls.enabled is True
    assert cfg.status_file == "custom/status.json"


def test_env_override_wins(tmp_path: Path, monkeypatch):
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("host: {ollama_port: 1}", encoding="utf-8")
    env_path = tmp_path / "env.yaml"
    env_path.write_text("host: {ollama_port: 2}", encoding="utf-8")

    monkeypatch.setenv("NEO_MODEL_CONN_CONFIG", str(env_path))
    # No explicit path given -> env var wins over any default resolution.
    cfg = Config.load(None)
    assert cfg.host.ollama_port == 2


def test_status_path_resolves_relative_to_repo_root(tmp_path: Path):
    cfg = Config.load(tmp_path / "nonexistent.yaml")
    assert cfg.status_path.is_absolute()
    assert cfg.status_path.name == "status.json"


def test_discover_model_single_gguf(monkeypatch, tmp_path):
    gguf = tmp_path / "some-model.gguf"
    gguf.write_bytes(b"fake")
    monkeypatch.setattr("model_conn.config.MODELS_DIR", tmp_path)
    assert discover_model() == str(gguf)


def test_discover_model_no_gguf(monkeypatch, tmp_path):
    monkeypatch.setattr("model_conn.config.MODELS_DIR", tmp_path)
    assert discover_model() == ""


def test_discover_model_nonexistent_dir(monkeypatch):
    monkeypatch.setattr(
        "model_conn.config.MODELS_DIR", Path("/nonexistent/models")
    )
    assert discover_model() == ""


def test_discover_model_multiple_ggufs(monkeypatch, tmp_path):
    (tmp_path / "a.gguf").write_bytes(b"a")
    (tmp_path / "b.gguf").write_bytes(b"b")
    monkeypatch.setattr("model_conn.config.MODELS_DIR", tmp_path)
    try:
        discover_model()
    except SystemExit as exc:
        assert "multiple .gguf models" in str(exc)
    else:
        raise AssertionError("expected SystemExit for ambiguous models")
