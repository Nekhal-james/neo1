"""config/audio.yaml, with audio.local.yaml merged over it."""

from __future__ import annotations

from dataclasses import fields

import pytest

from neo_audio import config as config_module
from neo_audio.config import Config, MicConfig, SpeakerConfig
from neo_audio.endpointer import EndpointerConfig
from neo_audio.wakeword import WakeWordConfig

REPO_AUDIO_YAML = config_module.REPO_ROOT / "config" / "audio.yaml"


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def layers(tmp_path, monkeypatch):
    monkeypatch.delenv("NEO_AUDIO_CONFIG", raising=False)

    def use(committed: str, local: str | None = None):
        monkeypatch.setattr(config_module, "DEFAULT_CONFIG", _write(tmp_path, "audio.yaml", committed))
        monkeypatch.setattr(
            config_module,
            "LOCAL_CONFIG",
            _write(tmp_path, "audio.local.yaml", local) if local is not None else tmp_path / "absent.yaml",
        )

    return use


def test_the_local_file_names_one_key_and_leaves_the_rest(layers):
    layers(
        "wakeword: {threshold: 0.9, refractory_s: 3.0}\nmic: {sample_rate: 16000, chunk_ms: 50}\n",
        "wakeword: {model_path: models/wakeword/neo.zip}\n",
    )
    cfg = Config.load()
    assert cfg.wakeword.model_path == "models/wakeword/neo.zip"
    assert cfg.wakeword.threshold == 0.9 and cfg.wakeword.refractory_s == 3.0
    assert cfg.mic.chunk_ms == 50


def test_a_relative_model_path_resolves_against_the_repo_not_the_cwd(layers, tmp_path, monkeypatch):
    layers("wakeword: {model_path: models/wakeword/neo.zip}\n")
    monkeypatch.chdir(tmp_path)
    assert Config.load().wakeword_model_path == config_module.REPO_ROOT / "models/wakeword/neo.zip"


def test_no_model_configured_is_none_not_the_repo_root(layers):
    layers("mic: {}\n")
    assert Config.load().wakeword_model_path is None


def test_the_env_var_names_exactly_one_file(layers, tmp_path, monkeypatch):
    layers("wakeword: {threshold: 0.9}\n", "wakeword: {threshold: 0.5}\n")
    monkeypatch.setenv("NEO_AUDIO_CONFIG", str(_write(tmp_path, "only.yaml", "mic: {chunk_ms: 20}\n")))
    cfg = Config.load()
    assert cfg.mic.chunk_ms == 20
    assert cfg.wakeword.threshold == WakeWordConfig.threshold


def test_the_committed_file_restates_the_dataclass_defaults_exactly():
    """audio.yaml documents every default. If it ever disagrees with the
    dataclass, one of them is lying -- this is the check that catches it."""
    cfg = Config.load(REPO_AUDIO_YAML)
    for section, cls in (
        (cfg.mic, MicConfig),
        (cfg.speaker, SpeakerConfig),
        (cfg.wakeword, WakeWordConfig),
        (cfg.endpointer, EndpointerConfig),
    ):
        for f in fields(cls):
            assert getattr(section, f.name) == getattr(cls(), f.name), f"{cls.__name__}.{f.name}"
