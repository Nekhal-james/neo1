from __future__ import annotations

import json
import sys
import types

import pytest

from intelligence import asr


def test_transcribe_raises_clear_error_when_model_path_not_configured(cfg):
    cfg.asr.vosk_model_path = ""
    with pytest.raises(RuntimeError, match="not configured"):
        asr.transcribe(b"", 16000, cfg)


def test_transcribe_raises_clear_error_when_model_path_missing(cfg, tmp_path):
    cfg.asr.vosk_model_path = str(tmp_path / "nonexistent-model")
    with pytest.raises(RuntimeError, match="not found"):
        asr.transcribe(b"", 16000, cfg)


def test_transcribe_raises_clear_error_when_vosk_not_installed(cfg, tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    cfg.asr.vosk_model_path = str(model_dir)

    monkeypatch.setitem(sys.modules, "vosk", None)
    with pytest.raises(RuntimeError, match="pip install"):
        asr.transcribe(b"", 16000, cfg)


def test_transcribe_returns_scripted_text(cfg, tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    cfg.asr.vosk_model_path = str(model_dir)

    fake_vosk = types.ModuleType("vosk")

    class FakeModel:
        def __init__(self, path):
            self.path = path

    class FakeRecognizer:
        def __init__(self, model, sample_rate):
            self.model = model
            self.sample_rate = sample_rate

        def AcceptWaveform(self, audio_bytes):
            return True

        def FinalResult(self):
            return json.dumps({"text": "where is cs two oh four"})

    fake_vosk.Model = FakeModel
    fake_vosk.KaldiRecognizer = FakeRecognizer
    monkeypatch.setitem(sys.modules, "vosk", fake_vosk)

    result = asr.transcribe(b"\x00\x01", 16000, cfg)
    assert result.text == "where is cs two oh four"
    assert result.engine == "vosk"
