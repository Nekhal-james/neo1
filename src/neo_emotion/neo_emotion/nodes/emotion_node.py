"""ROS 2 node wrapping the emotion controller.

Publishes what Neo currently feels and the motion parameters that go with it.
It has **no path to servo_driver**, by design: emotion is a movement modifier
that `head_behavior` blends into the command it was going to send anyway. If
this node ever grows a `/head/command` publisher, that design has been lost.

Activates in Phase 9 (docs/IMPLEMENTATION_PLAN.md section 9.1).
"""

from __future__ import annotations

import logging

from ..state import EmotionConfig, EmotionController, EmotionEvents

log = logging.getLogger(__name__)

RATE_HZ = 10.0
"""Fast enough that a mood change is not visibly late, slow enough to be free.

The motion it shapes is generated at 50 Hz in head_behavior from the parameters
this publishes; the parameters themselves change on human timescales.
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


class EmotionNode:
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 9.1).

    subscribe  /perception/attention   neo_msgs/AttentionTarget
               /perception/gestures    neo_msgs/GestureEvent
               /dialog/state           neo_msgs/DialogState
               /link/health            neo_msgs/LinkHealth
    publish    /emotion/state          neo_msgs/EmotionState   @ 10 Hz

    `EmotionEvents` is a **snapshot, not a queue**: each subscription updates a
    field on the latest-known world, and the timer hands the whole picture to
    `update()`. Draining a backlog of callbacks would let a burst of detector
    noise walk the state machine through several moods after the fact.

    The subscriptions are all optional. A missing dialog node or a dead link is
    the normal operating state on this robot, and the controller's answer to an
    empty `EmotionEvents` is a valid one -- NEUTRAL, then SLEEPY. Nothing here
    may block on a topic that has no publisher.

    Publish the parameters alongside the label, as the contract requires, rather
    than letting each consumer look them up: the Emotion tab's live sliders and
    the running behaviour must not be able to disagree about what CURIOUS
    currently means.
    """

    def __init__(self, config: EmotionConfig | None = None) -> None:
        ok, why = probe()
        if not ok:
            raise RuntimeError(f"ROS emotion node unavailable: {why}")
        self.controller = EmotionController(config or EmotionConfig())
        self.events = EmotionEvents()
        raise NotImplementedError("phase-9: bind /emotion/state and its inputs")


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start emotion node: {why}")
        print("The controller runs without ROS; the admin panel drives the same core.")
        return 1
    raise NotImplementedError("phase-9: bind /emotion/state and its inputs")
