"""ROS 2 node wrapping the emotion controller.

Publishes what Neo currently feels and the motion parameters that go with it.
It has **no path to servo_driver**, by design: emotion is a movement modifier
that `head_behavior` blends into the command it was going to send anyway. If
this node ever grows a `/head/command` publisher, that design has been lost.

Activates in Phase 9 (docs/IMPLEMENTATION_PLAN.md section 9.1).
"""

from __future__ import annotations

import logging

from ..motion import GestureKind
from ..state import EmotionConfig, EmotionController, EmotionEvents
from ..types import EmotionLabel

log = logging.getLogger(__name__)

try:
    from rclpy.node import Node
except ImportError:  # pragma: no cover - exercised only where ROS is installed
    Node = object

RATE_HZ = 10.0
"""Fast enough that a mood change is not visibly late, slow enough to be free.

The motion it shapes is generated at 50 Hz in head_behavior from the parameters
this publishes; the parameters themselves change on human timescales.
"""

_DIALOG_SPEAKING = 3
"""neo_msgs/DialogState.SPEAKING, mirrored as a plain int so this module needs
no import of neo_msgs at parse time (see probe())."""

GESTURE_COOLDOWN_S = 2.5
"""Least time between two expressed gestures.

Without it a mood that flickers across a boundary -- which the controller's
dwell timer already limits but does not forbid -- reads as a head twitching
rather than as a robot reacting.
"""

_LABEL_GESTURE = {
    EmotionLabel.CURIOUS: GestureKind.TILT,
    EmotionLabel.CONFUSED: GestureKind.SHAKE,
}
"""What *entering* a mood looks like. Curiosity reads as a head tilt and not
being able to help reads as a small shake -- both are things people do, which
is the whole reason this layer exists.

Only transitions appear here. A mood is a state and lasts; a gesture is an
event and must not repeat for as long as the mood holds.
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


class EmotionNode(Node):
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 9.1).

    subscribe  /perception/attention   neo_msgs/AttentionTarget
               /perception/gestures    neo_msgs/GestureEvent
               /dialog/state           neo_msgs/DialogState
               /link/health            neo_msgs/LinkHealth
    publish    /emotion/state          neo_msgs/EmotionState   @ 10 Hz
               /emotion/gesture        std_msgs/String         on a transition

    /emotion/gesture is still not a path to the servos. It names a shape --
    "nod", "shake", "tilt" -- and `head_behavior` decides whether anything
    happens, at GESTURE priority, below the operator's joystick. A std_msgs
    String rather than neo_msgs/GestureEvent because that contract describes a
    gesture Neo *saw a person make*; this is one Neo makes, and overloading the
    two would put "open palm" and "nod" in the same enum.

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

        from neo_msgs.msg import AttentionTarget, DialogState, EmotionState, GestureEvent, LinkHealth
        from std_msgs.msg import String

        super().__init__("emotion_node")

        self.controller = EmotionController(config or EmotionConfig())
        self.events = EmotionEvents()
        self._person_seen = False
        self._last_label = None
        self._last_gesture_at = 0.0
        # gesture_seen is a discrete edge, not a level: latch it in the
        # callback and clear it once the timer has consumed it, or a single
        # gesture would bypass the dwell timer on every tick forever.
        self._gesture_seen_pending = False

        self.create_subscription(
            AttentionTarget, "/perception/attention", self._on_attention, 10
        )
        self.create_subscription(GestureEvent, "/perception/gestures", self._on_gesture, 10)
        self.create_subscription(DialogState, "/dialog/state", self._on_dialog_state, 10)
        self.create_subscription(LinkHealth, "/link/health", self._on_link_health, 10)

        self._pub = self.create_publisher(EmotionState, "/emotion/state", 10)
        self._gesture_pub = self.create_publisher(String, "/emotion/gesture", 10)
        self._timer = self.create_timer(1.0 / RATE_HZ, self._on_timer)
        self.get_logger().info("emotion_node: publishing /emotion/state")

    # -- clock -----------------------------------------------------------

    def _now(self) -> float:
        return self.get_clock().now().nanoseconds / 1e9

    # -- ROS callbacks -----------------------------------------------------

    def _on_attention(self, msg) -> None:
        present = bool(msg.person_present)
        self.events.person_arrived = present and not self._person_seen
        self.events.person_present = present
        self.events.engaged = bool(msg.engaged)
        self._person_seen = present

    def _on_gesture(self, msg) -> None:
        self._gesture_seen_pending = True

    def _on_dialog_state(self, msg) -> None:
        self.events.speaking = msg.state == _DIALOG_SPEAKING

    def _on_link_health(self, msg) -> None:
        self.events.link_down = not bool(msg.up)

    def _on_timer(self) -> None:
        self.events.gesture_seen = self._gesture_seen_pending
        self._gesture_seen_pending = False
        arrived = self.events.person_arrived

        now = self._now()
        state = self.controller.update(self.events, now)
        # person_arrived is also an edge; whatever caused it has been seen now.
        self.events.person_arrived = False

        self._maybe_gesture(state.label, arrived, now)
        self._publish(state)

    def _maybe_gesture(self, label, person_arrived: bool, now: float) -> None:
        """Express a gesture on a transition worth reacting to, at most one.

        Someone walking up is greeted with a nod before any mood change has
        had time to happen, so arrival is checked first and wins: a nod that
        arrives a second late has stopped being a greeting.
        """
        entering = label if label != self._last_label else None
        self._last_label = label

        if person_arrived:
            kind = GestureKind.NOD
        elif entering is not None:
            kind = _LABEL_GESTURE.get(entering)
        else:
            kind = None
        if kind is None:
            return
        if (now - self._last_gesture_at) < GESTURE_COOLDOWN_S:
            return

        self._last_gesture_at = now
        self._publish_gesture(kind)

    def _publish_gesture(self, kind: GestureKind) -> None:
        from std_msgs.msg import String

        self._gesture_pub.publish(String(data=kind.value))
        self.get_logger().info(f"gesture: {kind.value}")

    def _publish(self, state) -> None:
        from neo_msgs.msg import EmotionState as EmotionStateMsg

        msg = EmotionStateMsg()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.label = int(state.label)
        msg.intensity = state.intensity
        params = state.params
        msg.idle_amplitude_deg = params.idle_amplitude_deg
        msg.idle_freq_hz = params.idle_freq_hz
        msg.gaze_gain = params.gaze_gain
        msg.gaze_lag = params.gaze_lag
        msg.tilt_bias_deg = params.tilt_bias_deg
        msg.micro_motion = params.micro_motion
        msg.settle_time = params.settle_time
        self._pub.publish(msg)


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start emotion node: {why}")
        print("The controller runs without ROS; the admin panel drives the same core.")
        return 1

    import rclpy

    rclpy.init(args=argv)
    node = EmotionNode()
    import signal

    from rclpy.executors import ExternalShutdownException

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        # A second SIGINT (the process group's, then launch's) must not cut the
        # shutdown short; see neo_motion/nodes/head_behavior.py.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
