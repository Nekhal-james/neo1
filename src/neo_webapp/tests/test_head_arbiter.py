"""Head arbiter priority: estop > manual > gaze > idle.

A stand-in for Phase 3's `head_behavior`, but the ordering is the real one, and
the rule that matters is that an operator with a hand on the joystick always
outranks the robot's own idea of where to look.
"""

from __future__ import annotations

import time

import pytest

from neo_webapp.bridge.mock import MockBridge


class FakePerception:
    """Minimal stand-in for PerceptionLink."""

    def __init__(self, pan: float = 0.0, tilt: float = 0.0) -> None:
        self.axes = (pan, tilt)
        self.released = False

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def submit_jpeg(self, jpeg: bytes) -> None: ...
    def gaze_axes(self) -> tuple[float, float]:
        return self.axes

    def release(self, reason: str = "") -> None:
        self.released = True

    def reset(self) -> None: ...
    def view(self, running: bool):
        from neo_webapp.bridge.types import PerceptionView

        return PerceptionView(available=True, running=running)


def drive(bridge: MockBridge, ticks: int = 10, dt: float = 0.02) -> None:
    for _ in range(ticks):
        bridge._tick(dt)


@pytest.mark.asyncio
async def test_gaze_moves_the_head_when_nobody_is_driving():
    bridge = MockBridge(perception=FakePerception(pan=0.8))
    drive(bridge)
    head = bridge.snapshot().head
    assert head.pan_deg > 0
    assert head.active_source == "gaze"


@pytest.mark.asyncio
async def test_manual_input_outranks_gaze():
    """Gaze wants to go right; the operator says left. The operator wins."""
    bridge = MockBridge(perception=FakePerception(pan=1.0))
    await bridge.publish_joy([-1.0, 0.0], [])
    drive(bridge)

    head = bridge.snapshot().head
    assert head.active_source == "manual"
    assert head.pan_deg < 0, "manual command should have moved the head left"


@pytest.mark.asyncio
async def test_gaze_resumes_once_the_operator_lets_go():
    bridge = MockBridge(deadman_ms=50, perception=FakePerception(pan=0.6))
    await bridge.publish_joy([-1.0, 0.0], [])
    drive(bridge, ticks=3)
    assert bridge.snapshot().head.active_source == "manual"

    time.sleep(0.08)  # let the joystick stream go stale
    drive(bridge, ticks=3)
    assert bridge.snapshot().head.active_source == "gaze"


@pytest.mark.asyncio
async def test_neutral_joystick_does_not_block_gaze():
    """Holding the stick centred should not veto the robot looking around."""
    bridge = MockBridge(perception=FakePerception(pan=0.6))
    await bridge.publish_joy([0.0, 0.0], [])
    drive(bridge)
    assert bridge.snapshot().head.active_source == "gaze"


@pytest.mark.asyncio
async def test_estop_beats_everything():
    bridge = MockBridge(perception=FakePerception(pan=1.0, tilt=1.0))
    await bridge.set_estop(True)
    await bridge.publish_joy([1.0, 1.0], [])
    drive(bridge, ticks=20)

    head = bridge.snapshot().head
    assert head.active_source == "estop"
    assert head.pan_deg == 0.0 and head.tilt_deg == 0.0


@pytest.mark.asyncio
async def test_gaze_still_respects_soft_limits():
    bridge = MockBridge(perception=FakePerception(pan=1.0, tilt=1.0))
    drive(bridge, ticks=500)

    head = bridge.snapshot().head
    assert head.pan_deg <= head.pan_limit_deg[1]
    assert head.tilt_deg <= head.tilt_limit_deg[1]


@pytest.mark.asyncio
async def test_no_perception_falls_back_to_idle():
    bridge = MockBridge(perception=None)
    drive(bridge)
    assert bridge.snapshot().head.active_source == "idle"


def test_release_endpoint_reaches_the_pipeline(config, client):
    from neo_webapp.app import create_app
    from fastapi.testclient import TestClient
    from .conftest import TEST_PASSWORD

    perception = FakePerception()
    app = create_app(config, MockBridge(perception=perception))
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD})
        assert c.post("/api/perception/release").status_code == 200
    assert perception.released is True


def test_release_is_refused_without_perception(auth_client):
    """The default test bridge has no perception attached."""
    assert auth_client.post("/api/perception/release").status_code == 501
