"""The spoken turn: a transcript in, a reply out loud, and the mic kept from hearing it.

Faked at the edges only -- the recognizer, the model host and the voice -- so the
loop's own rules are what is under test: when a turn starts, that there is never
more than one, that the reply reaches the speaker, and that Neo's own voice never
reaches the recognizer.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from neo_webapp.app import create_app
from neo_webapp.bridge.mock import MockBridge
from neo_webapp.bridge.types import TranscriptView
from neo_webapp.media import MediaManager
from neo_webapp.voice import ChatReply, VoiceLoop

from .conftest import TEST_PASSWORD
from .test_audio_routes import FakeAudio, FakeSpeech

CHUNK = b"\x00\x01" * 160


def final(text: str) -> TranscriptView:
    return TranscriptView(text=text, is_final=True, confidence=0.9, engine="vosk")


class ScriptedSpeech(FakeSpeech):
    """Hands back a final transcript on chosen mic chunks, and speaks by sentence."""

    def __init__(self, *, finals=None, flush_text=None, sentence_bytes=4096,
                 sample_rate=22050, **kwargs):
        super().__init__(**kwargs)
        self.finals = dict(finals or {})  # index of the fed chunk -> transcript
        self.flush_text = flush_text
        self.sentence_bytes = sentence_bytes
        self.sample_rate = sample_rate

    async def feed(self, pcm):
        index = len(self.fed)
        self.fed.append(pcm)
        text = self.finals.get(index)
        return final(text) if text is not None else None

    async def flush(self):
        self.flushed += 1
        return final(self.flush_text) if self.flush_text else None

    async def say_sentences(self, text):
        if self.say_error:
            raise self.say_error
        self.said.append(text)
        for _sentence in [s for s in text.split(". ") if s]:
            yield FakeAudio(b"\x10\x00" * (self.sentence_bytes // 2), self.sample_rate)


def wait_for(predicate, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@pytest.fixture
def voice_client(config):
    opened = []

    def build(*, reply="CS-204 is in B block.", source="ollama", ask=None,
              tail_s=0.05, **speech_kwargs):
        app = create_app(config, MockBridge(deadman_ms=120, source_switch_ms=10))
        speech = ScriptedSpeech(**speech_kwargs)
        app.state.speech = speech
        asked: list[str] = []

        def fake_ask(text):
            asked.append(text)
            return ChatReply(reply=reply, source=source)

        voice = app.state.voice
        voice._ask = ask or fake_ask
        voice.tail_s = tail_s

        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        res = client.post(
            "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
        )
        assert res.status_code == 200
        return SimpleNamespace(client=client, app=app, speech=speech, voice=voice, asked=asked)

    yield build
    for client in opened:
        client.__exit__(None, None, None)


# -- the switch ------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [("get", "/api/voice"), ("post", "/api/voice"), ("post", "/api/voice/ask")],
)
def test_voice_routes_require_a_session(client: TestClient, method, path):
    """It sends what the room says to the model host and talks back through the
    speaker. Neither belongs to an anonymous caller."""
    assert getattr(client, method)(path).status_code == 401


def test_answering_out_loud_is_off_until_switched_on(voice_client):
    v = voice_client()
    assert v.client.get("/api/voice").json()["enabled"] is False
    assert v.client.post("/api/voice", json={"enabled": True}).json()["enabled"] is True
    assert v.client.get("/api/state").json()["voice"]["enabled"] is True


@pytest.mark.parametrize("payload", [{}, {"enabled": "yes"}, {"enabled": 1}])
def test_the_switch_wants_a_real_boolean(voice_client, payload):
    """`1` and `"yes"` are not an answer to "send the room to the model host?"."""
    v = voice_client()
    assert v.client.post("/api/voice", json=payload).status_code == 400


# -- a turn ----------------------------------------------------------------


def test_a_typed_turn_is_answered_out_loud(voice_client):
    v = voice_client(reply="CS-204 is in B block. Take the stairs.")
    with v.client.websocket_connect("/ws/speaker") as ws:
        turn = v.client.post("/api/voice/ask", json={"text": "where is cs 204"}).json()
        received = b"".join(ws.receive_bytes() for _ in range(4))

    assert v.asked == ["where is cs 204"]
    assert turn["heard"] == "where is cs 204"
    assert turn["reply"] == "CS-204 is in B block. Take the stairs."
    assert turn["source"] == "ollama"
    assert turn["sentences"] == 2
    assert turn["error"] == ""
    assert len(received) == 2 * 4096, "both sentences reached the browser"


def test_without_a_speaker_the_turn_says_nobody_heard_it(voice_client):
    v = voice_client()
    turn = v.client.post("/api/voice/ask", json={"text": "hi"}).json()
    assert turn["reply"]
    assert "no speaker" in turn["error"]


def test_a_final_transcript_from_the_mic_starts_a_turn(voice_client):
    v = voice_client(finals={1: "where is the library"})
    v.client.post("/api/voice", json={"enabled": True})
    with v.client.websocket_connect("/ws/mic") as ws:
        ws.send_bytes(CHUNK)
        ws.send_bytes(CHUNK)
        assert wait_for(lambda: v.asked == ["where is the library"])
    assert wait_for(lambda: not v.voice.busy)
    assert v.voice.view().last.heard == "where is the library"


def test_nothing_goes_to_the_model_while_answering_is_off(voice_client):
    v = voice_client(finals={0: "where is the library"})
    with v.client.websocket_connect("/ws/mic") as ws:
        ws.send_bytes(CHUNK)
        v.client.get("/api/state")
    time.sleep(0.1)
    assert v.asked == []


def test_an_empty_transcript_is_not_a_question(voice_client):
    v = voice_client(finals={0: "   "})
    v.client.post("/api/voice", json={"enabled": True})
    with v.client.websocket_connect("/ws/mic") as ws:
        ws.send_bytes(CHUNK)
        v.client.get("/api/state")
    time.sleep(0.1)
    assert v.asked == []


def test_the_last_words_before_the_mic_closes_are_still_answered(voice_client):
    """Stopping the mic straight after asking is the common case, not an edge."""
    v = voice_client(flush_text="where is the canteen")
    v.client.post("/api/voice", json={"enabled": True})
    with v.client.websocket_connect("/ws/mic") as ws:
        ws.send_bytes(CHUNK)
    assert wait_for(lambda: v.asked == ["where is the canteen"])


def test_a_second_question_during_a_turn_is_dropped_not_queued(voice_client):
    """A queue would answer a question the person has already moved on from."""
    release = threading.Event()
    asked = []

    def slow_ask(text):
        asked.append(text)
        release.wait(3)
        return ChatReply("Sure.", "ollama")

    v = voice_client(ask=slow_ask, finals={0: "first question", 1: "second question"})
    v.client.post("/api/voice", json={"enabled": True})
    with v.client.websocket_connect("/ws/mic") as ws:
        ws.send_bytes(CHUNK)
        assert wait_for(lambda: asked == ["first question"])
        ws.send_bytes(CHUNK)
        v.client.get("/api/state")
        release.set()
        assert wait_for(lambda: not v.voice.busy)
    assert asked == ["first question"]


def test_typing_a_question_while_neo_is_answering_is_refused(voice_client):
    release = threading.Event()

    def slow_ask(text):
        release.wait(3)
        return ChatReply("Sure.", "ollama")

    v = voice_client(ask=slow_ask, finals={0: "first question"})
    v.client.post("/api/voice", json={"enabled": True})
    with v.client.websocket_connect("/ws/mic") as ws:
        ws.send_bytes(CHUNK)
        assert wait_for(lambda: v.voice.busy)
        res = v.client.post("/api/voice/ask", json={"text": "another"})
        release.set()
    assert res.status_code == 409


def test_a_model_failure_is_reported_on_the_turn_not_raised(voice_client):
    def vanished(text):
        raise ConnectionError("host vanished")

    v = voice_client(ask=vanished)
    res = v.client.post("/api/voice/ask", json={"text": "hello"})
    assert res.status_code == 200
    assert "host vanished" in res.json()["error"]
    assert v.speech.said == []


def test_a_voice_failure_is_reported_on_the_turn(voice_client):
    v = voice_client(say_error=RuntimeError("tts.piper_model_path is not configured"))
    body = v.client.post("/api/voice/ask", json={"text": "hello"}).json()
    assert body["reply"], "the answer is still shown when it cannot be spoken"
    assert "piper_model_path" in body["error"]


# -- half-duplex -----------------------------------------------------------


def test_neos_own_voice_never_reaches_the_recognizer(voice_client):
    """Plan 5.6. Without the gate the mic hears the reply, transcribes it, and
    Neo answers itself for as long as anybody lets it."""
    # Two seconds of speech: pushing it takes microseconds, playing it does not.
    v = voice_client(sentence_bytes=22050 * 2 * 2, tail_s=0.1)
    with v.client.websocket_connect("/ws/speaker"), v.client.websocket_connect("/ws/mic") as mic:
        v.client.post("/api/audio/say", json={"text": "This is Neo talking"})
        state = v.client.get("/api/state").json()
        assert state["voice"]["phase"] == "speaking"
        assert state["voice"]["gated"] is True
        assert state["dialog_state"] == "SPEAKING"

        mic.send_bytes(CHUNK)  # the room, hearing Neo
        v.client.get("/api/state")
        assert v.speech.fed == [], "mic audio reached the recognizer while Neo spoke"


def test_the_mic_is_handed_back_once_playback_has_finished(voice_client):
    v = voice_client(sentence_bytes=int(22050 * 0.3) * 2, tail_s=0.05)
    with v.client.websocket_connect("/ws/speaker"), v.client.websocket_connect("/ws/mic") as mic:
        v.client.post("/api/audio/say", json={"text": "Short"})
        assert wait_for(lambda: not v.voice.gated())
        mic.send_bytes(CHUNK)
        v.client.get("/api/state")
        assert v.speech.fed == [CHUNK]
        assert wait_for(lambda: v.speech.discards >= 1), (
            "whatever was half-heard around Neo's voice is dropped"
        )


# -- the badge -------------------------------------------------------------


def test_the_badge_follows_the_turn(voice_client):
    release = threading.Event()

    def slow_ask(text):
        release.wait(3)
        return ChatReply("Sure.", "ollama")

    v = voice_client(ask=slow_ask, finals={0: "hello"})
    v.client.post("/api/voice", json={"enabled": True})

    def badge():
        return v.client.get("/api/state").json()["dialog_state"]

    assert badge() == "IDLE"
    with v.client.websocket_connect("/ws/mic") as ws:
        assert wait_for(lambda: badge() == "LISTENING")
        ws.send_bytes(CHUNK)
        assert wait_for(lambda: badge() == "THINKING")
        release.set()
        assert wait_for(lambda: badge() == "LISTENING")
    assert wait_for(lambda: badge() == "IDLE")


def test_a_turn_never_overwrites_estop_on_the_badge(voice_client):
    v = voice_client()
    v.client.portal.call(v.app.state.bridge.set_estop, True)
    turn = v.client.post("/api/voice/ask", json={"text": "hello"}).json()
    assert turn["reply"]
    assert v.client.get("/api/state").json()["dialog_state"] == "ESTOP"


# -- the loop and the bridge on their own ------------------------------------


async def _drain(bridge):
    async for _ in bridge.audio_out_stream():
        pass


@pytest.mark.asyncio
async def test_the_gate_covers_playback_and_the_tail_not_just_the_push():
    """Synthesis runs faster than real time, so the push is finished long before
    the listener is. A gate that only covered the push would reopen with nearly
    the whole reply still coming out of the speaker."""
    now = [100.0]
    bridge = MockBridge()
    media = MediaManager(bridge)
    await media.open("speaker")
    speech = ScriptedSpeech(sentence_bytes=22050 * 2)  # one second at 22.05 kHz
    voice = VoiceLoop(
        SimpleNamespace(bridge=bridge, media=media, speech=speech),
        tail_s=0.3,
        clock=lambda: now[0],
    )
    consumer = asyncio.create_task(_drain(bridge))
    try:
        await voice.speak("Hello")
        assert voice.speaking() and voice.gated(), "pushed, but still playing"
        now[0] += 1.1
        assert not voice.speaking() and voice.gated(), "playback over, the tail is not"
        now[0] += 0.3
        assert not voice.gated()
    finally:
        consumer.cancel()
        await voice.close()


@pytest.mark.asyncio
async def test_a_speaker_that_stops_draining_abandons_the_reply_instead_of_hanging():
    bridge = MockBridge()
    media = MediaManager(bridge)
    await media.open("speaker")  # open, but nothing is consuming the queue
    speech = ScriptedSpeech(sentence_bytes=2048 * 40)  # more than the queue holds
    voice = VoiceLoop(
        SimpleNamespace(bridge=bridge, media=media, speech=speech),
        push_timeout_s=0.1,
        tail_s=0.0,
    )
    started = time.monotonic()
    try:
        result = await voice.speak("One. Two.")
    finally:
        await voice.close()
    assert time.monotonic() - started < 2.0
    assert result.speaker_connected is True


@pytest.mark.asyncio
async def test_pushing_speech_waits_for_room_instead_of_dropping():
    bridge = MockBridge()
    for _ in range(32):
        assert await bridge.push_audio_out(b"\x00\x00", timeout_s=0.5)

    async def take_one():
        await asyncio.sleep(0.05)
        bridge._audio_out.get_nowait()

    taker = asyncio.create_task(take_one())
    assert await bridge.push_audio_out(b"\x01\x01", timeout_s=1.0) is True
    await taker
    assert await bridge.push_audio_out(b"\x02\x02", timeout_s=0.05) is False


@pytest.mark.asyncio
async def test_clearing_the_speaker_queue_drops_what_was_meant_for_the_last_listener():
    bridge = MockBridge()
    for _ in range(3):
        await bridge.emit_audio_out(b"\x00\x00")
    assert bridge.clear_audio_out() == 3
    assert bridge._audio_out.qsize() == 0
