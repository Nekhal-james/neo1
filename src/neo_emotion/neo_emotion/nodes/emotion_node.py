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

        super().__init__("emotion_node")

        self.controller = EmotionController(config or EmotionConfig())
        self.events = EmotionEvents()
        self._person_seen = False
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

        state = self.controller.update(self.events, self._now())
        # person_arrived is also an edge; whatever caused it has been seen now.
        self.events.person_arrived = False

        self._publish(state)

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
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
