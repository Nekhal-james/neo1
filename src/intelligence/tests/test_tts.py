from __future__ import annotations

import sys
import types
import wave

import pytest

from intelligence import tts


def test_synthesize_raises_clear_error_when_model_path_not_configured(cfg):
    cfg.tts.piper_model_path = ""
    with pytest.raises(RuntimeError, match="not configured"):
        tts.synthesize("hello", cfg)


def test_synthesize_raises_clear_error_when_model_path_missing(cfg, tmp_path):
    cfg.tts.piper_model_path = str(tmp_path / "nonexistent.onnx")
    with pytest.raises(RuntimeError, match="not found"):
        tts.synthesize("hello", cfg)


def test_synthesize_raises_clear_error_when_piper_not_installed(cfg, tmp_path, monkeypatch):
    model_path = tmp_path / "voice.onnx"
    model_path.write_bytes(b"fake")
    cfg.tts.piper_model_path = str(model_path)

    monkeypatch.setitem(sys.modules, "piper", None)
    with pytest.raises(RuntimeError, match="pip install"):
        tts.synthesize("hello", cfg)


def test_synthesize_returns_wav_bytes(cfg, tmp_path, monkeypatch):
    model_path = tmp_path / "voice.onnx"
    model_path.write_bytes(b"fake")
    cfg.tts.piper_model_path = str(model_path)

    fake_piper = types.ModuleType("piper")

    class FakeVoice:
        @classmethod
        def load(cls, path, config_path=None):
            return cls()

        def synthesize(self, text, wav_file):
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00\x00" * 10)

    fake_piper.PiperVoice = FakeVoice
    monkeypatch.setitem(sys.modules, "piper", fake_piper)

    result = tts.synthesize("hello", cfg)
    assert isinstance(result, bytes)
    assert len(result) > 0

    import io

    with wave.open(io.BytesIO(result), "rb") as wav_file:
        assert wav_file.getnframes() == 10
