"""Routes that steer the robot's own graph: conversation, camera, gestures."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from neo_webapp.app import create_app
from neo_webapp.bridge.mock import MockBridge
from neo_webapp.bridge.types import Result

from .conftest import TEST_PASSWORD

ROBOT_POSTS = [
    ("/api/robot/wake", {}),
    ("/api/robot/ask", {"text": "where is the library"}),
    ("/api/robot/say", {"text": "hello"}),
    ("/api/robot/cancel", {}),
]


def test_robot_routes_need_a_session(client):
    for path, body in ROBOT_POSTS:
        assert client.post(path, json=body).status_code == 401
    assert client.get("/api/camera/snapshot").status_code == 401
    assert client.post("/api/emotion/gesture", json={"kind": "nod"}).status_code == 401


def test_the_simulated_robot_has_no_robot_voice_or_camera_and_says_so(auth_client):
    for path, body in ROBOT_POSTS:
        res = auth_client.post(path, json=body)
        assert res.status_code == 501, path
        assert "ROS graph" in res.json()["detail"]
    assert auth_client.get("/api/camera/snapshot").status_code == 501


def test_capabilities_ride_along_in_the_state(auth_client):
    caps = auth_client.get("/api/state").json()["capabilities"]
    assert "gestures" in caps and "robot_voice" not in caps


def test_a_gesture_plays_on_the_simulated_head(auth_client):
    res = auth_client.post("/api/emotion/gesture", json={"kind": "nod"})
    assert res.status_code == 200 and res.json()["message"] == "nod"
    assert auth_client.get("/api/state").json()["conversation"]["last_gesture"] == "nod"


def test_an_unknown_gesture_is_refused_with_the_valid_ones(auth_client):
    res = auth_client.post("/api/emotion/gesture", json={"kind": "wave"})
    assert res.status_code == 409 and "nod" in res.json()["detail"]
    assert auth_client.post("/api/emotion/gesture", json={}).status_code == 400


def test_no_gesture_under_estop(auth_client):
    auth_client.post("/api/system/estop", json={"engaged": True})
    assert auth_client.post("/api/emotion/gesture", json={"kind": "shake"}).status_code == 409


class FakeRobot(MockBridge):
    """A bridge that claims the robot's capabilities and records the calls."""

    def __init__(self):
        super().__init__(deadman_ms=120, source_switch_ms=10, idle_motion=False)
        self._state.capabilities += ["robot_voice", "robot_camera"]
        self.calls: list[tuple] = []
        self.busy = False
        self.frames: list[bytes | None] = [None, b"\xff\xd8jpeg"]

    async def robot_wake(self):
        self.calls.append(("wake",))
        return Result(ok=True, message="listening")

    async def robot_ask(self, text):
        self.calls.append(("ask", text))
        return Result(ok=not self.busy, message="Neo is already answering" if self.busy else "asked")

    async def robot_say(self, text):
        self.calls.append(("say", text))
        return Result(ok=True, message="speaking")

    async def robot_cancel(self):
        self.calls.append(("cancel",))
        return Result(ok=True, message="stopped")

    async def camera_snapshot(self):
        return self.frames.pop(0) if self.frames else None


@pytest.fixture
def robot(config):
    bridge = FakeRobot()
    with TestClient(create_app(config, bridge)) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}).status_code == 200
        yield c, bridge


def test_robot_voice_routes_reach_a_bridge_that_can(robot):
    client, bridge = robot
    for path, body in ROBOT_POSTS:
        assert client.post(path, json=body).status_code == 200, path
    assert bridge.calls == [("wake",), ("ask", "where is the library"), ("say", "hello"), ("cancel",)]


def test_empty_or_oversized_text_never_reaches_the_robot(robot):
    client, bridge = robot
    assert client.post("/api/robot/ask", json={"text": "   "}).status_code == 400
    assert client.post("/api/robot/say", json={"text": "x" * 5000}).status_code == 413
    assert bridge.calls == []


def test_a_refusal_from_the_robot_is_a_409_with_its_reason(robot):
    client, bridge = robot
    bridge.busy = True
    res = client.post("/api/robot/ask", json={"text": "hi"})
    assert res.status_code == 409 and "already answering" in res.json()["detail"]


def test_the_camera_answers_503_until_a_frame_exists_then_a_jpeg(robot):
    client, _bridge = robot
    first = client.get("/api/camera/snapshot")
    assert first.status_code == 503 and first.headers["retry-after"] == "1"
    second = client.get("/api/camera/snapshot")
    assert second.status_code == 200
    assert second.headers["content-type"] == "image/jpeg"
    assert second.headers["cache-control"] == "no-store"
    assert second.content.startswith(b"\xff\xd8")
