"""ROS 2 node wrapping the head arbiter.

Everything that wants to move the head publishes here, and exactly one of them
wins. The rule that matters is that an operator always outranks the robot's own
idea of where to look: gaze is an *attention target*, not a servo command, and
the arbiter is what decides whether to act on it.

Activates in Phase 3 (docs/IMPLEMENTATION_PLAN.md section 3.3); Phase 4 fills in
gaze and Phase 9 the emotion blend.
"""

from __future__ import annotations

import logging

from ..arbiter import ArbiterConfig, HeadArbiter
from ..types import Priority

log = logging.getLogger(__name__)

RATE_HZ = 50.0

JOY_TIMEOUT_S = 0.3
"""The virtual joystick's deadman.

It is in a browser on the far side of a WebSocket, so "the user let go" and "the
connection died mid-drag" arrive as the same thing: nothing. The arbiter's
freshness expiry covers both -- a source that stops publishing stops winning --
which is why there is no separate deadman timer here. Losing the head is
enough; the driver then holds position rather than continuing the last drag.
"""


def probe() -> tuple[bool, str]:
    try:
        import rclpy  # noqa: F401
    except ImportError:
        return False, "rclpy not installed"
    try:
        import neo_msgs.msg  # noqa: F401
    except ImportError:
        return False, "neo_msgs not built (Phase 1)"
    return True, "ok"


class HeadBehaviorNode:
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 3.3).

    subscribe  /joy                    sensor_msgs/Joy          -> MANUAL (70)
               /perception/attention   neo_msgs/AttentionTarget -> GAZE (30)
               /emotion/state          neo_msgs/EmotionState    -> blend params
    publish    /head/command           neo_msgs/HeadCommand     @ 50 Hz

    Each input becomes a named source on the arbiter, submitted with the stamp
    it arrived with. `resolve()` runs on the timer and its winner is published;
    the crossfade means a handover ramps over ~300 ms instead of snapping.

    Three things that are easy to get wrong here:

    * **Emotion is a modifier, not a source.** `/emotion/state` never becomes an
      arbiter entry -- it scales the gaze gain and supplies the idle drift and
      tilt bias that the IDLE-priority submission is built from. Giving emotion
      its own path to the servos is the failure this design exists to prevent
      (CLAUDE.md, and the EmotionState contract says the same).
    * **Feed the real pose back.** `/head/state` goes into `sync_to()` so a fade
      starts from where the head actually is, not from the last target it was
      asked for. The two differ whenever a fade was cut short.
    * **Idle is always submitted.** It is the floor that keeps the head from
      looking switched off, and being lowest priority it costs nothing when
      anything else is live.

    Killing this node must leave the head holding still, not driving: it stops
    publishing, the driver's watchdog expires, and the driver holds.
    """

    SOURCES = {
        "manual": Priority.MANUAL,
        "gesture": Priority.GESTURE,
        "gaze": Priority.GAZE,
        "idle": Priority.IDLE,
    }

    def __init__(self, config: ArbiterConfig | None = None) -> None:
        ok, why = probe()
        if not ok:
            raise RuntimeError(f"ROS head behavior unavailable: {why}")
        self.arbiter = HeadArbiter(
            config or ArbiterConfig(source_timeout_s=JOY_TIMEOUT_S)
        )
        raise NotImplementedError("phase-3: bind /joy, /perception/attention, /head/command")


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start head behavior: {why}")
        print("The arbiter runs without ROS; the admin panel's Head tab drives it.")
        return 1
    raise NotImplementedError("phase-3: bind /joy, /perception/attention, /head/command")
