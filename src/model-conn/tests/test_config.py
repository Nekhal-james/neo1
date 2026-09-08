from __future__ import annotations

from pathlib import Path

from model_conn.config import Config


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
