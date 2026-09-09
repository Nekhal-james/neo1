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

        def synthesize_wav(self, text, wav_file):
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


def _fake_piper(loads: list, *, rate: int = 22050, frames: int = 10,
                channels: int = 1, width: int = 2) -> types.ModuleType:
    module = types.ModuleType("piper")

    class FakeVoice:
        @classmethod
        def load(cls, path, config_path=None):
            loads.append(path)
            return cls()

        def synthesize_wav(self, text, wav_file):
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(width)
            wav_file.setframerate(rate)
            wav_file.writeframes(b"\x00\x01" * frames * channels)

    module.PiperVoice = FakeVoice
    return module


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


def test_availability_names_the_unset_config_key(cfg):
    cfg.tts.piper_model_path = ""
    avail = tts.availability(cfg)
    assert not avail.ok
    assert "piper_model_path" in avail.reason


def test_availability_reports_the_missing_package(cfg, tmp_path, monkeypatch):
    model_path = tmp_path / "voice.onnx"
    model_path.write_bytes(b"fake")
    cfg.tts.piper_model_path = str(model_path)
    monkeypatch.setitem(sys.modules, "piper", None)

    avail = tts.availability(cfg)
    assert not avail.ok
    assert "pip install" in avail.reason


def test_availability_does_not_load_the_voice(cfg, tmp_path, monkeypatch):
    model_path = tmp_path / "voice.onnx"
    model_path.write_bytes(b"fake")
    cfg.tts.piper_model_path = str(model_path)

    loads: list = []
    monkeypatch.setitem(sys.modules, "piper", _fake_piper(loads))

    assert tts.availability(cfg).ok
    assert loads == []


# --------------------------------------------------------------------------
# Voice caching
# --------------------------------------------------------------------------


def test_voice_is_loaded_once(cfg, tmp_path, monkeypatch):
    """Loading an ONNX voice per utterance would dominate the reply latency
    budget -- the plan allows 500 ms for first audio, total."""
    model_path = tmp_path / "voice.onnx"
    model_path.write_bytes(b"fake")
    cfg.tts.piper_model_path = str(model_path)

    loads: list = []
    monkeypatch.setitem(sys.modules, "piper", _fake_piper(loads))

    for _ in range(4):
        tts.synthesize("hello", cfg)

    assert loads == [str(model_path)]


# --------------------------------------------------------------------------
# Raw PCM output -- what the panel's speaker channel actually consumes
# --------------------------------------------------------------------------


def test_synthesize_pcm_strips_the_wav_header(cfg, tmp_path, monkeypatch):
    """A WAV header in the middle of a PCM stream is not something the speaker
    channel, or Web Audio, can do anything with."""
    model_path = tmp_path / "voice.onnx"
    model_path.write_bytes(b"fake")
    cfg.tts.piper_model_path = str(model_path)
    monkeypatch.setitem(sys.modules, "piper", _fake_piper([], rate=22050, frames=10))

    audio = tts.synthesize_pcm("hello", cfg)
    assert audio.sample_rate == 22050
    assert len(audio.data) == 20  # 10 frames * 2 bytes, no header
    assert not audio.data.startswith(b"RIFF")


def test_synthesize_pcm_resamples_to_the_speaker_rate(cfg, tmp_path, monkeypatch):
    """The panel's AudioContext is created at one fixed rate. A voice at a
    different rate is not an error -- it just plays at the wrong pitch, which
    is a genuinely confusing bug to chase."""
    model_path = tmp_path / "voice.onnx"
    model_path.write_bytes(b"fake")
    cfg.tts.piper_model_path = str(model_path)
    monkeypatch.setitem(sys.modules, "piper", _fake_piper([], rate=16000, frames=100))

    audio = tts.synthesize_pcm("hello", cfg, target_rate=22050)
    assert audio.sample_rate == 22050
    # 100 frames at 16 kHz is 6.25 ms; the same duration at 22.05 kHz is ~138.
    assert 130 <= len(audio.data) // 2 <= 145


def test_synthesize_pcm_is_a_passthrough_when_rates_match(cfg, tmp_path, monkeypatch):
    model_path = tmp_path / "voice.onnx"
    model_path.write_bytes(b"fake")
    cfg.tts.piper_model_path = str(model_path)
    monkeypatch.setitem(sys.modules, "piper", _fake_piper([], rate=22050, frames=50))

    audio = tts.synthesize_pcm("hello", cfg, target_rate=22050)
    assert len(audio.data) == 100


def test_synthesize_pcm_downmixes_stereo(cfg, tmp_path, monkeypatch):
    model_path = tmp_path / "voice.onnx"
    model_path.write_bytes(b"fake")
    cfg.tts.piper_model_path = str(model_path)
    monkeypatch.setitem(sys.modules, "piper", _fake_piper([], rate=22050, frames=10, channels=2))

    audio = tts.synthesize_pcm("hello", cfg)
    assert len(audio.data) == 20  # 10 mono frames out of 10 stereo frames


def test_synthesize_pcm_rejects_a_width_it_cannot_play(cfg, tmp_path, monkeypatch):
    """Wrong-width samples played as s16le are not quiet -- they are loud. Fail
    with a sentence rather than deafen whoever is at the desk."""
    model_path = tmp_path / "voice.onnx"
    model_path.write_bytes(b"fake")
    cfg.tts.piper_model_path = str(model_path)
    monkeypatch.setitem(sys.modules, "piper", _fake_piper([], width=1))

    with pytest.raises(RuntimeError, match="16-bit"):
        tts.synthesize_pcm("hello", cfg)


def test_duration_is_reported_from_the_actual_rate(cfg):
    assert tts.PcmAudio(b"\x00\x00" * 22050, 22050).duration_s == 1.0
    assert tts.PcmAudio(b"", 22050).duration_s == 0.0


# --------------------------------------------------------------------------
# Resampler, directly
# --------------------------------------------------------------------------


def test_resample_is_identity_at_the_same_rate():
    pcm = b"\x01\x02\x03\x04"
    assert tts.resample(pcm, 16000, 16000) is pcm


def test_resample_handles_empty_and_degenerate_input():
    assert tts.resample(b"", 16000, 22050) == b""
    assert tts.resample(b"\x00\x00", 0, 22050) == b"\x00\x00"


def test_resample_preserves_duration_within_a_sample():
    import array

    src = array.array("h", [int(1000 * (i % 8)) for i in range(160)])
    out = tts.resample(src.tobytes(), 16000, 8000)
    assert abs(len(out) // 2 - 80) <= 1


def test_resample_stays_inside_int16():
    """A rounding surprise here becomes an audible click."""
    import array

    src = array.array("h", [32767, -32768] * 50)
    out = array.array("h")
    out.frombytes(tts.resample(src.tobytes(), 16000, 44100))
    assert all(-32768 <= v <= 32767 for v in out)


# --------------------------------------------------------------------------
# The real Piper API, when it happens to be installed
# --------------------------------------------------------------------------
#
# piper-tts has already renamed this call once -- `synthesize(text, wav_file)`
# became `synthesize_wav()` in 1.2, which is why the comment in tts.py exists.
# A fake voice cannot notice it happening again; this can.


def test_real_piper_exposes_what_tts_calls():
    piper = pytest.importorskip("piper", reason="voice extra not installed")
    import inspect

    voice_cls = piper.PiperVoice
    assert hasattr(voice_cls, "synthesize_wav"), (
        "piper renamed synthesize_wav -- see the comment in tts.synthesize"
    )

    # tts.synthesize calls this positionally as (text, wav_file) and relies on
    # `set_wav_format` defaulting to True so Piper stamps the rate itself.
    params = inspect.signature(voice_cls.synthesize_wav).parameters
    assert list(params)[1:3] == ["text", "wav_file"]

    load_params = inspect.signature(voice_cls.load).parameters
    assert "config_path" in load_params, "tts._load_voice passes config_path by keyword"
