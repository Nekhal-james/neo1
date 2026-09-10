"""The speech path: mic -> recognizer -> transcript, and text -> voice -> speaker.

CI has neither Vosk nor Piper installed, and neither does most development. That
is the normal case these tests cover, not an edge case: the panel has to keep
working and say *why* speech is off, and every route has to fail as a
configuration problem rather than a traceback.

A fake SpeechLink stands in for the real engines the same way FakePerception
does in test_identify_route.py -- the wiring under test is the panel's, not
Vosk's.
"""

from __future__ import annotations

import io
import struct
import wave

import pytest
from fastapi.testclient import TestClient

from neo_webapp.app import create_app
from neo_webapp.bridge.mock import MockBridge
from neo_webapp.bridge.types import AudioView, TranscriptView
from neo_webapp.speech import SpeechLink, _wav_to_pcm

from .conftest import TEST_PASSWORD


class FakeAudio:
    def __init__(self, data: bytes, sample_rate: int) -> None:
        self.data = data
        self.sample_rate = sample_rate

    @property
    def duration_s(self) -> float:
        return len(self.data) / 2.0 / self.sample_rate


class FakeSpeech:
    """Stands in for SpeechLink without a model, a mic, or a voice."""

    def __init__(self, *, asr_ok=True, tts_ok=True, say_error=None, pcm_bytes=4096):
        self.asr_ok = asr_ok
        self.tts_ok = tts_ok
        self.say_error = say_error
        self.pcm_bytes = pcm_bytes
        self.said: list[str] = []
        self.fed: list[bytes] = []
        self.flushed = 0
        self.discards = 0
        self.resets = 0
        self.reloads = 0
        self.refreshes = 0
        self.transcript = TranscriptView(text="where is cs two oh four", is_final=True,
                                         confidence=0.9, engine="vosk")

    def refresh_availability(self):
        self.refreshes += 1

    def view(self, *, listening=False):
        return AudioView(
            asr_available=self.asr_ok,
            asr_reason="" if self.asr_ok else "asr.vosk_model_path is not set",
            asr_engine="vosk",
            tts_available=self.tts_ok,
            tts_reason="" if self.tts_ok else "tts.piper_model_path is not set",
            tts_engine="piper",
            listening=listening,
            last=self.transcript,
        )

    async def feed(self, pcm):
        self.fed.append(pcm)
        return None

    async def flush(self):
        self.flushed += 1
        return None

    async def transcribe_wav(self, wav_bytes):
        if not self.asr_ok:
            raise RuntimeError("asr.vosk_model_path is not configured")
        _wav_to_pcm(wav_bytes)  # exercise the real validation
        return self.transcript

    async def say(self, text):
        if self.say_error:
            raise self.say_error
        self.said.append(text)
        return FakeAudio(b"\x00\x01" * (self.pcm_bytes // 2), 22050)

    async def say_sentences(self, text):
        if self.say_error:
            raise self.say_error
        self.said.append(text)
        yield FakeAudio(b"\x00\x01" * (self.pcm_bytes // 2), 22050)

    def discard_utterance(self):
        self.discards += 1

    def reset(self):
        self.resets += 1

    def reload_config(self):
        self.reloads += 1


@pytest.fixture
def speech_client(config):
    def build(**kwargs):
        app = create_app(config, MockBridge(deadman_ms=120, source_switch_ms=10))
        app.state.speech = FakeSpeech(**kwargs)
        client = TestClient(app)
        client.__enter__()
        res = client.post(
            "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
        )
        assert res.status_code == 200
        return client, app.state.speech

    return build


def _wav(frames: int = 160, rate: int = 16000, channels: int = 1, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 1000) * frames * channels)
    return buf.getvalue()


# -- auth ------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/audio/status"),
        ("post", "/api/audio/say"),
        ("post", "/api/audio/transcribe"),
        ("post", "/api/audio/reset"),
        ("post", "/api/audio/reload"),
    ],
)
def test_audio_routes_require_a_session(client: TestClient, method, path):
    """The panel can drive the speaker and read what the room said out loud.
    Neither belongs to an anonymous caller."""
    res = getattr(client, method)(path)
    assert res.status_code == 401


# -- status ----------------------------------------------------------------


def test_status_reports_engines_and_availability(speech_client):
    client, _ = speech_client()
    body = client.get("/api/audio/status").json()
    assert body["asr_available"] and body["tts_available"]
    assert body["asr_engine"] == "vosk"
    assert body["tts_engine"] == "piper"


def test_status_names_the_reason_speech_is_off(speech_client):
    """The reason is the whole point: "unavailable" with no detail is
    indistinguishable from a bug."""
    client, _ = speech_client(asr_ok=False, tts_ok=False)
    body = client.get("/api/audio/status").json()
    assert not body["asr_available"]
    assert "vosk_model_path" in body["asr_reason"]
    assert "piper_model_path" in body["tts_reason"]


def test_status_refreshes_availability_for_a_freshly_opened_panel(speech_client):
    """Otherwise the first paint shows "checking..." until the 1 Hz task ticks."""
    client, speech = speech_client()
    before = speech.refreshes
    client.get("/api/audio/status")
    assert speech.refreshes > before


# -- say -------------------------------------------------------------------


def test_say_synthesizes_and_reports_the_audio(speech_client):
    client, speech = speech_client()
    res = client.post("/api/audio/say", json={"text": "hello"})
    assert res.status_code == 200

    body = res.json()
    assert speech.said == ["hello"]
    assert body["sample_rate"] == 22050
    assert body["bytes"] == 4096
    assert body["duration_s"] > 0


def test_say_pushes_pcm_to_the_speaker_channel(speech_client):
    """The end of the path that matters: synthesized audio has to reach the
    browser playing the speaker channel, not just be returned to the caller."""
    client, _ = speech_client(pcm_bytes=4096)
    with client.websocket_connect("/ws/speaker") as ws:
        client.post("/api/audio/say", json={"text": "hello"})
        # 4096 bytes at 2048 per chunk.
        assert ws.receive_bytes() == b"\x00\x01" * 1024
        assert ws.receive_bytes() == b"\x00\x01" * 1024


def test_say_queues_nothing_when_no_speaker_is_connected(speech_client):
    """This used to queue the audio regardless, and whoever connected the
    speaker next -- a second or an hour later -- heard a fragment of the old
    sentence. Measured: 0.19 s of it, one second after it was spoken."""
    client, _ = speech_client(pcm_bytes=4096)
    body = client.post("/api/audio/say", json={"text": "hello"}).json()
    assert body["speaker_connected"] is False
    assert body["bytes"] == 4096, "still synthesized, so a broken voice still shows"
    assert client.app.state.bridge._audio_out.qsize() == 0


def test_say_tells_you_when_nobody_is_listening(speech_client):
    """Audio produced with no speaker connected is dropped. Reporting success
    for something the operator never heard is the confusing outcome."""
    client, _ = speech_client()
    body = client.post("/api/audio/say", json={"text": "hello"}).json()
    assert body["speaker_connected"] is False


def test_say_reports_speaker_connected_when_a_browser_is_attached(speech_client):
    client, _ = speech_client()
    with client.websocket_connect("/ws/speaker"):
        body = client.post("/api/audio/say", json={"text": "hello"}).json()
    assert body["speaker_connected"] is True


def test_say_rejects_empty_text(speech_client):
    client, _ = speech_client()
    assert client.post("/api/audio/say", json={"text": "   "}).status_code == 400


def test_say_bounds_the_text_length(speech_client):
    """Bounds how long one request can hold a worker thread synthesizing."""
    client, _ = speech_client()
    res = client.post("/api/audio/say", json={"text": "a" * 5000})
    assert res.status_code == 413


def test_say_without_a_voice_is_a_configuration_error_not_a_crash(speech_client):
    client, _ = speech_client(say_error=RuntimeError("tts.piper_model_path is not configured"))
    res = client.post("/api/audio/say", json={"text": "hello"})
    assert res.status_code == 501
    assert "piper_model_path" in res.json()["detail"]


def test_say_surfaces_an_unexpected_engine_failure_as_502(speech_client):
    client, _ = speech_client(say_error=ValueError("onnx exploded"))
    res = client.post("/api/audio/say", json={"text": "hello"})
    assert res.status_code == 502


# -- transcribe ------------------------------------------------------------


def test_transcribe_returns_a_transcript_for_an_uploaded_wav(speech_client):
    """The path that makes speech-to-text testable with no microphone."""
    client, _ = speech_client()
    res = client.post("/api/audio/transcribe", content=_wav())
    assert res.status_code == 200

    body = res.json()
    assert body["text"] == "where is cs two oh four"
    assert body["engine"] == "vosk"
    assert body["is_final"] is True


def test_transcribe_rejects_an_empty_body(speech_client):
    client, _ = speech_client()
    assert client.post("/api/audio/transcribe", content=b"").status_code == 400


def test_transcribe_rejects_something_that_is_not_a_wav(speech_client):
    client, _ = speech_client()
    res = client.post("/api/audio/transcribe", content=b"this is not audio")
    assert res.status_code == 400
    assert "WAV" in res.json()["detail"]


def test_transcribe_rejects_stereo_rather_than_mis_decoding_it(speech_client):
    """Feeding the recognizer interleaved stereo produces plausible-looking
    nonsense, which is far harder to debug than an error."""
    client, _ = speech_client()
    res = client.post("/api/audio/transcribe", content=_wav(channels=2))
    assert res.status_code == 400
    assert "mono" in res.json()["detail"]


def test_transcribe_rejects_a_width_it_cannot_read(speech_client):
    client, _ = speech_client()
    res = client.post("/api/audio/transcribe", content=_wav(width=1))
    assert res.status_code == 400
    assert "16-bit" in res.json()["detail"]


def test_transcribe_bounds_the_upload_size(speech_client):
    """An upload big enough to exhaust memory on a 4 GB Pi."""
    client, _ = speech_client()
    res = client.post("/api/audio/transcribe", content=b"\x00" * (11 * 1024 * 1024))
    assert res.status_code == 413


def test_transcribe_without_a_model_is_a_configuration_error(speech_client):
    client, _ = speech_client(asr_ok=False)
    res = client.post("/api/audio/transcribe", content=_wav())
    assert res.status_code == 501


# -- reset / reload --------------------------------------------------------


def test_reset_clears_the_current_utterance(speech_client):
    client, speech = speech_client()
    assert client.post("/api/audio/reset").json()["ok"] is True
    assert speech.resets == 1


def test_reload_rereads_config_and_returns_fresh_status(speech_client):
    """So downloading a voice does not need the panel restarted -- which on the
    robot means an ssh session and a systemctl call, for a text-file change."""
    client, speech = speech_client()
    body = client.post("/api/audio/reload").json()
    assert speech.reloads == 1
    assert speech.refreshes >= 1
    assert body["tts_engine"] == "piper"


# -- the mic channel feeding the recognizer --------------------------------


def test_mic_audio_reaches_the_recognizer(speech_client):
    """The wiring this whole change exists for: mic chunks are transcribed, not
    only metered."""
    client, speech = speech_client()
    with client.websocket_connect("/ws/mic") as ws:
        ws.send_bytes(b"\x00\x01" * 160)
        ws.send_bytes(b"\x00\x02" * 160)
        client.get("/api/state")  # round trip so the server drains both

    assert speech.fed == [b"\x00\x01" * 160, b"\x00\x02" * 160]


def test_mic_close_flushes_trailing_audio(speech_client):
    """Without the flush, the last word is dropped whenever someone stops the
    mic instead of pausing long enough for the recognizer to endpoint."""
    client, speech = speech_client()
    with client.websocket_connect("/ws/mic") as ws:
        ws.send_bytes(b"\x00\x01" * 160)
    assert speech.flushed == 1


def test_mic_channel_survives_a_recognizer_failure(speech_client):
    """A missing model must not take the microphone down with it -- the mic
    also feeds the meter, the bridge, and eventually the wake word."""
    client, speech = speech_client()

    async def boom(pcm):
        raise RuntimeError("no model")

    speech.feed = boom
    with client.websocket_connect("/ws/mic") as ws:
        ws.send_bytes(b"\x00\x01" * 160)
        ws.send_bytes(b"\x00\x01" * 160)
        # Still open, still accepting audio.
        assert client.get("/api/state").status_code == 200


def test_listening_reflects_the_mic_channel(speech_client):
    client, _ = speech_client()
    assert client.get("/api/audio/status").json()["listening"] is False
    with client.websocket_connect("/ws/mic"):
        assert client.get("/api/audio/status").json()["listening"] is True


# -- SpeechLink itself, with nothing installed -----------------------------


def test_speech_link_reports_a_reason_when_intelligence_is_missing(monkeypatch):
    """The panel must stay up and explain itself, not 500."""
    monkeypatch.setattr("neo_webapp.speech.SPEECH_AVAILABLE", False)
    view = SpeechLink().view()
    assert not view.asr_available and not view.tts_available
    assert "intelligence" in view.asr_reason


@pytest.mark.asyncio
async def test_speech_link_feed_is_inert_without_intelligence(monkeypatch):
    monkeypatch.setattr("neo_webapp.speech.SPEECH_AVAILABLE", False)
    assert await SpeechLink().feed(b"\x00" * 320) is None
    assert await SpeechLink().flush() is None


def test_wav_to_pcm_unwraps_a_mono_16bit_file():
    pcm, rate = _wav_to_pcm(_wav(frames=160, rate=16000))
    assert rate == 16000
    assert len(pcm) == 320


# -- the real pipeline, faking only the voice model itself ------------------
#
# Everything above stubs SpeechLink. These two use the real one -- real
# intelligence.tts, real WAV parsing, real resampling, real chunking, real
# websocket delivery -- and fake only the ONNX voice, which is the one piece
# that cannot exist without downloaded weights. This is what proves the path
# actually carries audio rather than that the mocks agree with each other.


@pytest.fixture
def piper_voice(monkeypatch):
    """Install a fake `piper` module whose voice emits a real WAV."""
    import sys
    import types

    def install(rate: int, frames: int):
        module = types.ModuleType("piper")

        class FakeVoice:
            @classmethod
            def load(cls, path, config_path=None):
                return cls()

            def synthesize_wav(self, text, wav_file):
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(rate)
                # A ramp rather than silence: silence survives a resampler that
                # silently drops everything.
                wav_file.writeframes(
                    b"".join(struct.pack("<h", (i * 37) % 20000 - 10000) for i in range(frames))
                )

        module.PiperVoice = FakeVoice
        monkeypatch.setitem(sys.modules, "piper", module)

    return install


def _real_speech_client(config, tmp_path):
    from intelligence import tts

    tts.reset_cache()
    app = create_app(config, MockBridge(deadman_ms=120, source_switch_ms=10))
    speech = app.state.speech

    voice = tmp_path / "voice.onnx"
    voice.write_bytes(b"fake-onnx")
    cfg = speech._config()
    cfg.tts.piper_model_path = str(voice)

    client = TestClient(app)
    client.__enter__()
    res = client.post(
        "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
    )
    assert res.status_code == 200
    return client


def test_spoken_audio_reaches_a_connected_browser(config, tmp_path, piper_voice):
    """The whole text-to-speech path, for real: synthesize, unwrap the WAV,
    resample to the speaker channel's rate, chunk it, and deliver it to a
    browser over the websocket."""
    piper_voice(rate=22050, frames=4096)
    client = _real_speech_client(config, tmp_path)

    try:
        with client.websocket_connect("/ws/speaker") as ws:
            body = client.post("/api/audio/say", json={"text": "hello"}).json()
            assert body["speaker_connected"] is True
            assert body["sample_rate"] == 22050

            received = b""
            while len(received) < body["bytes"]:
                received += ws.receive_bytes()

        assert len(received) == 4096 * 2          # every sample delivered
        assert received != b"\x00" * len(received)  # and it is not silence
    finally:
        client.__exit__(None, None, None)


def test_a_voice_at_the_wrong_rate_is_resampled_not_pitch_shifted(
    config, tmp_path, piper_voice
):
    """A 16 kHz voice played into the panel's 22.05 kHz AudioContext would come
    out a fifth too high. The fix is server-side, so the wire format stays one
    thing -- assert the duration survives, which is what pitch-shifting breaks.
    """
    piper_voice(rate=16000, frames=16000)  # exactly one second of speech
    client = _real_speech_client(config, tmp_path)

    try:
        body = client.post("/api/audio/say", json={"text": "hello"}).json()
    finally:
        client.__exit__(None, None, None)

    assert body["sample_rate"] == 22050
    # One second in, one second out -- at the speaker's rate, not the voice's.
    assert body["bytes"] == pytest.approx(22050 * 2, rel=0.01)
    assert body["duration_s"] == pytest.approx(1.0, rel=0.01)


def test_a_long_reply_reaches_the_browser_in_full(config, tmp_path, piper_voice):
    """The regression this exists for: spoken replies were cut off at 1.49 s.

    The say route pushed chunks with put_nowait into a 32-slot queue without
    ever yielding, so the queue filled in one pass and everything past the first
    32 x 2048 bytes was dropped -- silently, with a 200 and the full duration in
    the response. Three sentences of two seconds each is six seconds of speech,
    four times what used to survive.
    """
    piper_voice(rate=22050, frames=22050 * 2)
    client = _real_speech_client(config, tmp_path)
    text = "The library is in the east wing. Take the stairs. It is on your left."

    try:
        with client.websocket_connect("/ws/speaker") as ws:
            body = client.post("/api/audio/say", json={"text": text}).json()
            received = b""
            while len(received) < body["bytes"]:
                received += ws.receive_bytes()
    finally:
        client.__exit__(None, None, None)

    assert body["sentences"] == 3, "synthesized a sentence at a time"
    assert body["bytes"] == 3 * 22050 * 2 * 2
    assert len(received) == body["bytes"], "every sample of every sentence delivered"
