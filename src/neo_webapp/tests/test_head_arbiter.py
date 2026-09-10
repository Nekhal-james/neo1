"""Head arbiter priority: estop > manual > gaze > idle.

A stand-in for Phase 3's `head_behavior`, but the ordering is the real one, and
the rule that matters is that an operator with a hand on the joystick always
outranks the robot's own idea of where to look.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from neo_webapp.bridge.mock import CAMERA_HFOV_DEG, CAMERA_VFOV_DEG, MockBridge


class FakePerception:
    """Minimal stand-in for PerceptionLink.

    `pan` and `tilt` are where the person is in the frame, normalised to
    [-1, 1]. Engaged unless told otherwise, because following an engaged person
    is what most of these tests are about.
    """

    def __init__(self, pan: float = 0.0, tilt: float = 0.0, *, engaged: bool = True) -> None:
        self.target = SimpleNamespace(
            x=pan, y=tilt, engaged=engaged, confidence=1.0 if engaged else 0.35
        )
        self.stale = False
        self.released = False

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def submit_jpeg(self, jpeg: bytes) -> None: ...
    def attention(self, max_age_s: float = 1.0):
        return None if self.stale else self.target

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


class TestRealMotionStack:
    """The panel drives `neo_motion` and `neo_emotion` themselves, not a copy.

    These pin the wiring between them, which is the part that has no test in
    either package: each is correct in isolation and can still be plugged
    together wrongly.
    """

    @pytest.mark.asyncio
    async def test_idle_drift_does_not_pull_the_head_back_to_centre(self):
        """The failure this catches is subtle and infuriating in person.

        Idle motion around an *absolute* zero would quietly return the head to
        centre every time the operator let go of the stick -- the robot undoing
        their aim a few seconds after they set it, looking like drift in the
        servos rather than a bug in the arbiter.
        """
        bridge = MockBridge(deadman_ms=5000)
        await bridge.publish_joy([1.0, 0.0], [])
        drive(bridge, ticks=60)
        aimed = bridge.snapshot().head.pan_deg
        assert aimed > 10.0, "should have driven well off centre"

        await bridge.publish_joy([0.0, 0.0], [])
        drive(bridge, ticks=400)  # eight seconds of idle
        rested = bridge.snapshot().head.pan_deg
        assert abs(rested - aimed) < 5.0, (
            f"idle wandered from {aimed:.1f} to {rested:.1f}: it is drifting "
            f"about centre instead of about where the head was left"
        )

    @pytest.mark.asyncio
    async def test_idle_actually_moves(self):
        """The other half: bounded is not the same as frozen.

        Note what this does *not* assert. The head does not drift smoothly: the
        driver's deadband quantizes slow motion into steps of roughly its own
        size, so a 2 deg idle sine arrives as a few dozen jumps of ~0.4 deg over
        30 s rather than as continuous movement. That is inherent to having a
        deadband at all -- refusing sub-deadband corrections is exactly what
        turns a slow ramp into a staircase -- and its severity is set by the
        deadband, which is still an uncalibrated placeholder. See
        `AxisLimits.deadband_rad`. So: assert that it moves and stays bounded,
        and leave smoothness to Phase 0 rather than encoding today's number.
        """
        bridge = MockBridge()
        positions = []
        for _ in range(1500):  # 30 simulated seconds
            bridge._tick(0.02)
            positions.append(bridge.snapshot().head.pan_deg)

        moves = sum(1 for a, b in zip(positions, positions[1:]) if a != b)
        assert moves > 20, "a still head reads as switched off"
        assert max(positions) - min(positions) > 1.0, "and it should be visible"
        params = bridge._emotion.state.params
        assert max(abs(p) for p in positions) < params.idle_amplitude_deg * 2.0, (
            "idle drift must stay a drift, not wander off"
        )

    @pytest.mark.asyncio
    async def test_emotion_is_never_a_source(self):
        """It modulates the movement; it has no path of its own to the servos."""
        bridge = MockBridge(perception=FakePerception(pan=0.5))
        for _ in range(200):
            bridge._tick(0.02)
            assert bridge.snapshot().head.active_source in {
                "estop", "manual", "gesture", "gaze", "idle", "none",
            }

    @pytest.mark.asyncio
    async def test_the_panel_reports_a_mood(self):
        bridge = MockBridge()
        drive(bridge, ticks=5)
        assert bridge.snapshot().emotion.label == "NEUTRAL"

    @pytest.mark.asyncio
    async def test_an_empty_lobby_eventually_sleeps(self):
        bridge = MockBridge()
        drive(bridge, ticks=6000)  # two simulated minutes
        assert bridge.snapshot().emotion.label == "SLEEPY"

    @pytest.mark.asyncio
    async def test_centring_clears_the_accumulated_targets(self):
        """A stale target left behind drives straight back out of centre, which
        looks exactly like the button not working."""
        bridge = MockBridge(deadman_ms=5000, idle_motion=False)
        await bridge.publish_joy([1.0, 0.0], [])
        drive(bridge, ticks=60)
        assert bridge.snapshot().head.pan_deg > 10.0

        await bridge.center_head()
        await bridge.publish_joy([0.0, 0.0], [])
        drive(bridge, ticks=50)
        assert bridge.snapshot().head.pan_deg == pytest.approx(0.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_the_head_never_leaves_its_envelope_under_any_source(self):
        bridge = MockBridge(perception=FakePerception(pan=1.0, tilt=1.0))
        for i in range(2000):
            if i % 3 == 0:
                await bridge.publish_joy([1.0, -1.0], [])
            bridge._tick(0.02)
            head = bridge.snapshot().head
            assert head.pan_limit_deg[0] <= head.pan_deg <= head.pan_limit_deg[1]
            assert head.tilt_limit_deg[0] <= head.tilt_deg <= head.tilt_limit_deg[1]


class TestGazeInThePanel:
    """The simulated head, against a camera that does not move with it.

    On the robot the camera rides the head, so gaze is rate control on image
    error and the loop closes itself. In the panel the camera is a webcam on a
    desk. Measured on real frames, integrating that rate drove the head from
    1 deg to its 90 deg stop in five seconds and left it there -- and it did so
    for anyone merely in view, which CLAUDE.md rules out on its own.
    """

    @pytest.mark.asyncio
    async def test_presence_alone_does_not_move_the_head(self):
        bridge = MockBridge(perception=FakePerception(pan=0.9, engaged=False), idle_motion=False)
        drive(bridge, ticks=250)
        head = bridge.snapshot().head
        assert head.active_source != "gaze"
        assert head.pan_deg == pytest.approx(0.0, abs=0.5)

    @pytest.mark.asyncio
    async def test_an_engaged_person_is_looked_at_not_chased_to_the_stop(self):
        bridge = MockBridge(perception=FakePerception(pan=0.5), idle_motion=False)
        drive(bridge, ticks=500)  # ten simulated seconds
        head = bridge.snapshot().head
        assert head.pan_deg == pytest.approx(0.5 * CAMERA_HFOV_DEG / 2.0, abs=1.0)
        assert not head.at_limit, "it used to end up pinned against the stop"

    @pytest.mark.asyncio
    async def test_it_settles_on_the_person_on_both_axes(self):
        bridge = MockBridge(perception=FakePerception(pan=-0.4, tilt=0.6), idle_motion=False)
        drive(bridge, ticks=500)
        head = bridge.snapshot().head
        assert head.pan_deg == pytest.approx(-0.4 * CAMERA_HFOV_DEG / 2.0, abs=1.0)
        assert head.tilt_deg == pytest.approx(0.6 * CAMERA_VFOV_DEG / 2.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_a_lost_lock_holds_rather_than_swinging_to_centre(self):
        perception = FakePerception(pan=0.6)
        bridge = MockBridge(perception=perception, idle_motion=False)
        drive(bridge, ticks=300)
        held = bridge.snapshot().head.pan_deg
        assert held > 10.0
        # SUSPENDED: still engaged, nothing to aim at, and the aim reads 0.
        perception.target.confidence = 0.0
        perception.target.x = 0.0
        drive(bridge, ticks=200)
        assert bridge.snapshot().head.pan_deg == pytest.approx(held, abs=0.5)

    @pytest.mark.asyncio
    async def test_a_stopped_camera_does_not_keep_the_head_locked(self):
        perception = FakePerception(pan=0.6)
        bridge = MockBridge(perception=perception, idle_motion=False)
        drive(bridge, ticks=50)
        assert bridge.snapshot().head.active_source == "gaze"
        perception.stale = True
        drive(bridge, ticks=50)
        assert bridge.snapshot().head.active_source != "gaze"


def test_perception_link_ignores_a_frozen_result():
    """Once frames stop, the pipeline keeps its last result for ever."""
    from neo_perception.types import AttentionTarget, PerceptionResult
    from neo_webapp.perception_link import PerceptionLink

    link = PerceptionLink.__new__(PerceptionLink)
    target = AttentionTarget(x=0.5, confidence=1.0, engaged=True)
    link.runner = SimpleNamespace(
        result=PerceptionResult(stamp=time.monotonic(), width=640, height=480, attention=target)
    )
    assert link.attention() is target
    link.runner.result.stamp = time.monotonic() - 5.0
    assert link.attention() is None
    link.runner = SimpleNamespace(result=PerceptionResult(stamp=0.0, width=0, height=0))
    assert link.attention() is None, "no frame processed yet"


def test_perception_link_reset_forgets_the_last_gesture():
    """It went on showing a gesture from before the reset."""
    from neo_webapp.perception_link import PerceptionLink

    link = PerceptionLink.__new__(PerceptionLink)
    link.runner = SimpleNamespace(pipeline=SimpleNamespace(reset=lambda: None))
    link._last_gesture = "open_palm"
    link.reset()
    assert link._last_gesture == "none"
