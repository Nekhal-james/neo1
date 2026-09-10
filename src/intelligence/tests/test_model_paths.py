"""Model paths resolve against the repo root, not the working directory.

The same config file is read by `neo --webapp up` in WSL and by `neo --prompt`
on Windows, each launched from wherever it happened to be. A CWD-relative
`models/vosk-model-...` worked from the repo root and reported "model not found"
from anywhere else -- including from inside a package directory, which is where
this repo's tests run.
"""

from __future__ import annotations

import sys
import types

from intelligence import asr, tts
from intelligence.config import REPO_ROOT, resolve_path


def test_a_relative_path_is_relative_to_the_repo_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_path("models/x") == REPO_ROOT / "models" / "x"


def test_an_absolute_path_is_left_alone(tmp_path):
    assert resolve_path(tmp_path) == tmp_path


def test_asr_finds_a_relative_model_from_any_directory(cfg, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg.asr.vosk_model_path = "src/intelligence"  # any directory inside the repo
    avail = asr.availability(cfg)
    assert "not found" not in avail.reason
    assert avail.model_path == str(REPO_ROOT / "src" / "intelligence")


def test_tts_finds_a_relative_voice_from_any_directory(cfg, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg.tts.piper_model_path = "config/intelligence.yaml"  # any file inside the repo
    assert "not found" not in tts.availability(cfg).reason


def test_a_missing_relative_model_says_where_it_looked(cfg, tmp_path, monkeypatch):
    """The unresolved path in the message would send someone looking in the
    wrong directory."""
    monkeypatch.chdir(tmp_path)
    cfg.asr.vosk_model_path = "models/does-not-exist"
    reason = asr.availability(cfg).reason
    assert "not found" in reason
    assert str(REPO_ROOT) in reason


def test_the_voice_is_loaded_from_the_resolved_path(cfg, tmp_path, monkeypatch):
    """Availability is only the check; loading is where the old bug actually bit."""
    loaded = {}
    module = types.ModuleType("piper")

    class FakeVoice:
        @classmethod
        def load(cls, path, config_path=None):
            loaded["path"], loaded["config"] = path, config_path
            return cls()

    module.PiperVoice = FakeVoice
    monkeypatch.setitem(sys.modules, "piper", module)
    monkeypatch.chdir(tmp_path)
    cfg.tts.piper_model_path = "config/intelligence.yaml"
    cfg.tts.piper_config_path = "config/intelligence.yaml"

    tts._load_voice(cfg)
    expected = str(REPO_ROOT / "config" / "intelligence.yaml")
    assert loaded == {"path": expected, "config": expected}
