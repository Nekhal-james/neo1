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


# --------------------------------------------------------------------------
# Availability -- what the admin panel shows when ASR cannot run
# --------------------------------------------------------------------------


def test_availability_names_the_unset_config_key(cfg):
    """"ASR unavailable" with no detail is indistinguishable from a bug. The
    reason has to be something the operator can act on."""
    cfg.asr.vosk_model_path = ""
    avail = asr.availability(cfg)
    assert not avail.ok
    assert "vosk_model_path" in avail.reason


def test_availability_names_a_missing_model_directory(cfg, tmp_path):
    cfg.asr.vosk_model_path = str(tmp_path / "not-there")
    avail = asr.availability(cfg)
    assert not avail.ok
    assert "not found" in avail.reason


def test_availability_reports_the_missing_package(cfg, tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    cfg.asr.vosk_model_path = str(model_dir)
    monkeypatch.setitem(sys.modules, "vosk", None)

    avail = asr.availability(cfg)
    assert not avail.ok
    assert "pip install" in avail.reason


def test_availability_does_not_load_the_model(cfg, tmp_path, monkeypatch):
    """It is called on every status poll. Loading a Vosk model there would
    stall the panel for seconds on the first request."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    cfg.asr.vosk_model_path = str(model_dir)

    loads = []
    fake_vosk = types.ModuleType("vosk")
    fake_vosk.Model = lambda path: loads.append(path)
    fake_vosk.KaldiRecognizer = lambda model, rate: None
    monkeypatch.setitem(sys.modules, "vosk", fake_vosk)

    assert asr.availability(cfg).ok
    assert loads == []


# --------------------------------------------------------------------------
# Model caching -- the fix that makes streaming possible at all
# --------------------------------------------------------------------------


def _fake_vosk(loads: list, *, script: dict | None = None) -> types.ModuleType:
    module = types.ModuleType("vosk")
    script = script or {}

    class FakeModel:
        def __init__(self, path):
            loads.append(path)

    class FakeRecognizer:
        def __init__(self, model, sample_rate):
            self.sample_rate = sample_rate
            self._fed = b""

        def AcceptWaveform(self, audio_bytes):
            self._fed += audio_bytes
            return len(self._fed) >= script.get("endpoint_after", 1 << 30)

        def Result(self):
            return json.dumps({"text": script.get("final", ""), "conf": 0.9})

        def PartialResult(self):
            return json.dumps({"partial": script.get("partial", "")})

        def FinalResult(self):
            return json.dumps({"text": script.get("final", "")})

    module.Model = FakeModel
    module.KaldiRecognizer = FakeRecognizer
    return module


def test_model_is_loaded_once_across_many_calls(cfg, tmp_path, monkeypatch):
    """The bug this fixes: a Vosk model load takes seconds, and it used to
    happen per call. Streaming audio arrives every ~20 ms."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    cfg.asr.vosk_model_path = str(model_dir)

    loads: list = []
    monkeypatch.setitem(sys.modules, "vosk", _fake_vosk(loads))

    for _ in range(5):
        asr.transcribe(b"\x00\x01", 16000, cfg)
    asr.Session(cfg)

    assert loads == [str(model_dir)]


def test_reset_cache_forces_a_reload(cfg, tmp_path, monkeypatch):
    """A config change at runtime must not keep serving the old model."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    cfg.asr.vosk_model_path = str(model_dir)

    loads: list = []
    monkeypatch.setitem(sys.modules, "vosk", _fake_vosk(loads))

    asr.Session(cfg)
    asr.reset_cache()
    asr.Session(cfg)

    assert len(loads) == 2


# --------------------------------------------------------------------------
# Streaming session
# --------------------------------------------------------------------------


def test_session_returns_none_until_the_recognizer_endpoints(cfg, tmp_path, monkeypatch):
    """Vosk decides when an utterance ended. A fixed silence timer in the
    caller would clip anyone who pauses mid-sentence."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    cfg.asr.vosk_model_path = str(model_dir)
    monkeypatch.setitem(
        sys.modules, "vosk",
        _fake_vosk([], script={"endpoint_after": 8, "final": "where is cs two oh four"}),
    )

    session = asr.Session(cfg)
    assert session.accept(b"\x00" * 4) is None
    result = session.accept(b"\x00" * 4)

    assert result is not None
    assert result.text == "where is cs two oh four"
    assert result.is_final
    assert result.engine == "vosk"


def test_session_exposes_the_in_progress_partial(cfg, tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    cfg.asr.vosk_model_path = str(model_dir)
    monkeypatch.setitem(sys.modules, "vosk", _fake_vosk([], script={"partial": "where is"}))

    session = asr.Session(cfg)
    session.accept(b"\x00" * 4)
    assert session.partial() == "where is"


def test_session_final_flushes_trailing_audio(cfg, tmp_path, monkeypatch):
    """When the window closes without an endpoint -- browser stopped sending,
    operator pressed stop -- the buffered audio must not be dropped."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    cfg.asr.vosk_model_path = str(model_dir)
    monkeypatch.setitem(sys.modules, "vosk", _fake_vosk([], script={"final": "b block"}))

    session = asr.Session(cfg)
    assert session.accept(b"\x00" * 4) is None
    assert session.final().text == "b block"


def test_session_tracks_bytes_seen(cfg, tmp_path, monkeypatch):
    """So the panel can tell "the mic is muted" from "the recognizer heard
    nothing in it" -- silence and no audio at all look identical otherwise."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    cfg.asr.vosk_model_path = str(model_dir)
    monkeypatch.setitem(sys.modules, "vosk", _fake_vosk([]))

    session = asr.Session(cfg)
    session.accept(b"\x00" * 320)
    session.accept(b"\x00" * 320)
    assert session.bytes_seen == 640

    session.reset()
    assert session.bytes_seen == 0


def test_session_uses_the_configured_sample_rate(cfg, tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    cfg.asr.vosk_model_path = str(model_dir)
    cfg.asr.sample_rate = 16000
    monkeypatch.setitem(sys.modules, "vosk", _fake_vosk([]))

    assert asr.Session(cfg).sample_rate == 16000
    assert asr.Session(cfg, sample_rate=8000).sample_rate == 8000


# --------------------------------------------------------------------------
# The real Vosk API, when it happens to be installed
# --------------------------------------------------------------------------
#
# Everything above fakes `vosk`, which cannot catch the one failure that has
# actually bitten this project's dependencies: an upstream rename. These tests
# assert the API surface asr.py calls really exists, and skip on the machines
# (CI, most laptops) without the `voice` extra.


def test_real_vosk_exposes_what_asr_calls():
    vosk = pytest.importorskip("vosk", reason="voice extra not installed")

    assert hasattr(vosk, "Model")
    assert hasattr(vosk, "KaldiRecognizer")
    for method in ("AcceptWaveform", "Result", "PartialResult", "FinalResult"):
        assert hasattr(vosk.KaldiRecognizer, method), f"vosk dropped {method}"
