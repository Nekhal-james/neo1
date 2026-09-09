"""The "what is this?" endpoint.

Deliberately synchronous from the caller's point of view: a question deserves an
answer, not a 202 and a polling loop in every client. These tests are mostly
about the ways that wait can go wrong.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from neo_webapp.app import create_app
from neo_webapp.bridge.mock import MockBridge
from neo_webapp.bridge.types import IdentifyView, ObjectGuessView, PerceptionView

from .conftest import TEST_PASSWORD


class FakePerception:
    """Stands in for PerceptionLink without a model or a camera."""

    def __init__(self, *, available=True, guesses=None, error="", answer_after=1):
        self._available = available
        self._guesses = guesses if guesses is not None else [
            ObjectGuessView("cell phone", 0.82, 0.41, 0.3, 0.3, 0.6, 0.7)
        ]
        self._error = error
        self._answer_after = answer_after
        self.seq = 0
        self._requests = 0
        self._polls = 0

    def start(self): ...
    def stop(self): ...
    def submit_jpeg(self, jpeg): ...
    def gaze_axes(self): return (0.0, 0.0)
    def release(self, reason=""): ...
    def reset(self): ...

    def request_identify(self) -> int:
        self._requests += 1
        self._polls = 0
        return self.seq + 1

    def view(self, running: bool) -> PerceptionView:
        # Complete the request after N polls, so the route's wait is exercised
        # rather than short-circuited.
        self._polls += 1
        if self._polls >= self._answer_after and self._requests:
            self.seq = max(self.seq, self._requests)
        return PerceptionView(
            available=self._available,
            running=running,
            identify=IdentifyView(seq=self.seq, error=self._error, guesses=self._guesses),
        )


def client_for(config, perception):
    app = create_app(config, MockBridge(perception=perception))
    c = TestClient(app)
    c.__enter__()
    c.post("/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD})
    return c


def test_requires_a_session(config):
    app = create_app(config, MockBridge(perception=FakePerception()))
    with TestClient(app) as c:
        assert c.post("/api/perception/identify").status_code == 401


def test_returns_the_best_guess(config):
    c = client_for(config, FakePerception())
    try:
        body = c.post("/api/perception/identify").json()
        assert body["best"] == "cell phone"
        assert body["guesses"][0]["confidence"] == pytest.approx(0.82)
    finally:
        c.__exit__(None, None, None)


def test_nothing_recognised_is_a_normal_answer(config):
    """An empty answer is not an error -- there may just be nothing to name."""
    c = client_for(config, FakePerception(guesses=[]))
    try:
        res = c.post("/api/perception/identify")
        assert res.status_code == 200
        assert res.json()["best"] == ""
    finally:
        c.__exit__(None, None, None)


def test_a_model_failure_is_reported(config):
    c = client_for(config, FakePerception(error="no weights"))
    try:
        res = c.post("/api/perception/identify")
        assert res.status_code == 500
        assert "no weights" in res.json()["detail"]
    finally:
        c.__exit__(None, None, None)


def test_unavailable_perception_is_refused_early(config):
    """No point starting a six-second wait when there is no detector at all."""
    c = client_for(config, FakePerception(available=False))
    try:
        res = c.post("/api/perception/identify")
        assert res.status_code == 503
    finally:
        c.__exit__(None, None, None)


def test_no_frames_times_out_with_a_useful_message(config, monkeypatch):
    """The common real cause is a camera that was never started."""
    from neo_webapp.api import routes

    monkeypatch.setattr(routes, "IDENTIFY_TIMEOUT_S", 0.3)
    # answer_after is unreachable, so the request never completes.
    c = client_for(config, FakePerception(answer_after=10_000))
    try:
        res = c.post("/api/perception/identify")
        assert res.status_code == 504
        assert "camera" in res.json()["detail"]
    finally:
        c.__exit__(None, None, None)


def test_refused_when_the_bridge_has_no_perception(auth_client):
    assert auth_client.post("/api/perception/identify").status_code == 501
