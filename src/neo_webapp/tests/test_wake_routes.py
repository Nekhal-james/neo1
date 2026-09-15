"""The wake word through the webapp: detector on the browser mic, and the routes.

CI has no wake word model downloaded, and neither does most development -- that
is the normal case these tests cover, not an edge case. A fake WakeLink stands
in for the real numpy detector the same way FakeSpeech stands in for Vosk; the
wiring under test is the panel's, not the network's.

The one arena the fake cannot cover is whether the *detector* produces sane
scores from a mic, which is exactly what the live wake meter on the Audio tab
is for. Everything else -- that audio reaches the detector, that a fire is
recorded and displayed, that the half-duplex gate and the robot path keep it
out -- belongs here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from neo_webapp.app import create_app
from neo_webapp.bridge.mock import MockBridge
from neo_webapp.bridge.types import WakeView

from .conftest import TEST_PASSWORD


class FakeWake:
    """Stands in for WakeLink without a model or a mic."""

    def __init__(
        self, *, avail_ok=True, reason="", fire_on_feed=False, score=0.31, threshold=0.0
    ) -> None:
        self.avail_ok = avail_ok
        self._reason = reason
        self.fire_on_feed = fire_on_feed
        self.score = score
        self.threshold = threshold
        self.fed: list[bytes] = []
        self.person_presents: list[bool] = []
        self.fires = 0
        self.refreshes = 0
        self.resets = 0

    def refresh_availability(self):
        self.refreshes += 1

    def view(self) -> WakeView:
        return WakeView(
            available=self.avail_ok,
            reason="" if self.avail_ok else self._reason,
            score=self.score,
            fired=self.fires,
            last_score=self.score,
            last_threshold=self.threshold,
            last_person_present=bool(self.person_presents),
            last_fired_age_s=1.5 if self.fires else None,
        )

    def feed(self, pcm, *, person_present=False):
        self.fed.append(pcm)
        self.person_presents.append(person_present)
        if self.fire_on_feed:
            self.manual_wake()

    def manual_wake(self):
        self.fires += 1
        self.threshold = 0.0

    def reset(self):
        self.resets += 1
        self.fires = 0
        self.threshold = 0.0


class FakeSpeech:
    """Minimal speech stand-in so the mic handler needs no Vosk to drain
    chunks. It must quack enough for ws_mic: an async feed that returns None."""

    def __init__(self) -> None:
        self.fed: list[bytes] = []

    def refresh_availability(self):
        pass

    def view(self, *, listening=False):
        from neo_webapp.bridge.types import AudioView, TranscriptView

        return AudioView(
            asr_available=False,
            asr_reason="fake for mic-channel tests",
            asr_engine="vosk",
            tts_available=False,
            tts_reason="fake for mic-channel tests",
            tts_engine="piper",
            listening=listening,
            last=TranscriptView(
                text="", is_final=True, confidence=0.0, engine="vosk"
            ),
        )

    async def feed(self, pcm):
        self.fed.append(pcm)
        return None

    async def flush(self):
        return None

    def discard_utterance(self):
        pass


@pytest.fixture
def wake_client(config):
    def build(**kwargs):
        app = create_app(config, MockBridge(deadman_ms=120, source_switch_ms=10))
        app.state.wake = FakeWake(**kwargs)
        app.state.speech = FakeSpeech()
        client = TestClient(app)
        client.__enter__()
        res = client.post(
            "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
        )
        assert res.status_code == 200
        return client, app.state.wake

    return build


# -- auth ------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/wake/status"),
        ("post", "/api/wake/listen"),
        ("post", "/api/wake/reset"),
    ],
)
def test_wake_routes_require_a_session(client: TestClient, method, path):
    """The wake word opens the listening window, which on the robot sends room
    audio to the model host -- not an anonymous caller's business."""
    res = getattr(client, method)(path)
    assert res.status_code == 401


# -- status ---------------------------------------------------------------


def test_wake_status_reports_the_detector_and_reason(wake_client):
    client, wake = wake_client()
    body = client.get("/api/wake/status").json()
    assert body["available"] is True
    assert body["score"] == 0.31
    assert body["reason"] == ""


def test_wake_status_names_the_reason_it_is_off(wake_client):
    """The reason is the whole point: "unavailable" with no detail is
    indistinguishable from a bug, and the fix differs by cause."""
    client, _ = wake_client(avail_ok=False, reason="audio.wakeword.model_path is not set")
    body = client.get("/api/wake/status").json()
    assert body["available"] is False
    assert "model_path" in body["reason"]


def test_wake_status_refreshes_availability_for_a_freshly_opened_panel(wake_client):
    client, wake = wake_client()
    before = wake.refreshes
    client.get("/api/wake/status")
    assert wake.refreshes > before


# -- listen / reset -------------------------------------------------------


def test_manual_wake_records_a_fire(wake_client):
    """Push-to-talk: the operator opens the window as if the word had been
    heard, and the fire must never look like the detector accepted something."""
    client, _ = wake_client()
    body = client.post("/api/wake/listen").json()
    assert body["fired"] == 1
    assert body["last_threshold"] == 0.0


def test_manual_wake_counts_consecutive_fires(wake_client):
    client, _ = wake_client()
    assert client.post("/api/wake/listen").json()["fired"] == 1
    assert client.post("/api/wake/listen").json()["fired"] == 2


def test_reset_clears_fires_but_keeps_the_model(wake_client):
    client, wake = wake_client()
    client.post("/api/wake/listen")
    assert client.post("/api/wake/reset").json()["ok"] is True
    assert wake.resets == 1
    body = client.get("/api/wake/status").json()
    assert body["fired"] == 0


# -- the mic channel feeding the detector ---------------------------------


def test_mic_audio_reaches_the_detector(wake_client):
    """The wiring this exists for: the wake word hears the same browser mic
    that feeds the recognizer."""
    client, wake = wake_client()
    with client.websocket_connect("/ws/mic") as ws:
        ws.send_bytes(b"\x00\x01" * 160)
        ws.send_bytes(b"\x00\x02" * 160)
        client.get("/api/state")  # round trip so the server drains both
    assert wake.fed == [b"\x00\x01" * 160, b"\x00\x02" * 160]


def test_mic_feeds_person_presence_to_the_detector(wake_client):
    """The relaxed threshold (threshold_with_person) is only ever applied when
    the detector is told someone is in frame."""
    client, wake = wake_client()
    with client.websocket_connect("/ws/mic") as ws:
        ws.send_bytes(b"\x00\x01" * 160)
    assert wake.person_presents == [False]
    assert not any(wake.person_presents)


def test_a_fire_from_the_mic_shows_in_the_snapshot(wake_client):
    """A real score crossing the threshold on the browser mic has to surface
    in the 4 Hz state broadcast, or the meter would never move."""
    client, wake = wake_client(fire_on_feed=True)
    with client.websocket_connect("/ws/mic") as ws:
        ws.send_bytes(b"\x00\x01" * 160)
        snap = client.get("/api/state").json()
    assert snap["wake"]["fired"] == 1
    assert wake.fed == [b"\x00\x01" * 160]


def test_mic_audio_is_not_fed_twice_on_the_robot(config):
    """Under the ROS bridge the browser mic goes into the graph, where the
    robot's own wake word already listens. A second detector here would run
    the same audio twice, on the same Pi."""

    class RobotBridge(MockBridge):
        owns_recognition = True

    app = create_app(config, RobotBridge())
    app.state.wake = FakeWake()
    client = TestClient(app)
    client.__enter__()
    client.post("/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD})
    try:
        with client.websocket_connect("/ws/mic") as ws:
            ws.send_bytes(b"\x00\x01" * 160)
            client.get("/api/state")
        assert app.state.wake.fed == []
    finally:
        client.__exit__(None, None, None)


def test_detector_is_not_fed_while_neo_is_speaking(
    wake_client, monkeypatch
):
    """Half-duplex, one layer down: Neo talking must never wake itself. The
    gate mirrors the dialog node muting the wake word while a reply plays."""
    client, wake = wake_client()

    gated = {"now": False}

    class GatedVoice:
        def publish(self):
            pass

        def gated(self):
            return gated["now"]

        def on_transcript(self, transcript):
            pass

    client.app.state.voice = GatedVoice()
    with client.websocket_connect("/ws/mic") as ws:
        ws.send_bytes(b"\x00\x01" * 160)
        gated["now"] = True
        ws.send_bytes(b"\x00\x02" * 160)
        gated["now"] = False
        ws.send_bytes(b"\x00\x03" * 160)
        client.get("/api/state")
    assert wake.fed == [b"\x00\x01" * 160, b"\x00\x03" * 160]


# -- WakeLink itself, with nothing installed ------------------------------


def test_wake_link_reports_a_reason_when_neo_audio_is_missing(monkeypatch):
    monkeypatch.setattr("neo_webapp.wake.WAKE_AVAILABLE", False)
    from neo_webapp.wake import WakeLink

    view = WakeLink().view()
    assert not view.available
    assert "neo_audio" in view.reason


def test_wake_link_feed_is_inert_without_a_detector(monkeypatch):
    monkeypatch.setattr("neo_webapp.wake.WAKE_AVAILABLE", False)
    from neo_webapp.wake import WakeLink

    link = WakeLink()
    link.feed(b"\x00" * 320)  # must not raise, and must not fire
    assert link.view().fired == 0