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
import math

from ..arbiter import ArbiterConfig, HeadArbiter
from ..config import MotionConfig
from ..types import HeadCommand, HeadLimits, Priority

log = logging.getLogger(__name__)

try:
    from rclpy.node import Node
except ImportError:  # pragma: no cover - exercised only where ROS is installed
    Node = object

RATE_HZ = 50.0

JOY_TIMEOUT_S = 0.3
"""The virtual joystick's deadman.

It is in a browser on the far side of a WebSocket, so "the user let go" and "the
connection died mid-drag" arrive as the same thing: nothing. The arbiter's
freshness expiry covers both -- a source that stops publishing stops winning --
which is why there is no separate deadman timer here. Losing the head is
enough; the driver then holds position rather than continuing the last drag.
"""

CAMERA_HFOV_DEG = 60.0
CAMERA_VFOV_DEG = 45.0
"""Degrees a normalised gaze bearing of +-1 maps to, each axis. A guess until a
real camera's field of view is measured; see neo_webapp's MockBridge, which
carries the same constants for the identical reason (a bearing, not a rate --
CLAUDE.md)."""


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


GESTURE_SHAPES: dict[str, tuple[float, float, float, float]] = {
    # name: (duration_s, pan_amplitude_deg, tilt_amplitude_deg, cycles)
    "nod": (0.9, 0.0, 7.0, 2.0),
    "shake": (0.9, 9.0, 0.0, 2.0),
    "tilt": (1.2, 0.0, 5.0, 0.5),
    "scan": (2.4, 22.0, 0.0, 1.0),
}
"""Mirrors `neo_emotion.motion.GESTURE_SHAPES`, and deliberately does not
import it -- exactly as `_idle_offset_rad` mirrors `IdleMotion.offset`.

`neo_motion` has no dependency on `neo_emotion` on purpose: emotion publishes
parameters and gesture *names*, and must never acquire a path to the servos.
Importing its motion module to avoid duplicating four tuples would be the first
step of growing one. Kept in sync by eye -- the same trade-off
`GestureRecognizer.explain` makes against `_classify` in neo_perception.
"""


def _gesture_offset_rad(kind: str, elapsed: float) -> tuple[float, float]:
    """The overlay for a running gesture; (0, 0) once it has finished.

    Tapered at both ends by a half-sine envelope so the head eases in and out.
    Starting and stopping a 7-degree nod with a step is a servo noise, not a
    gesture -- and on continuous-rotation servos it is also a step the dead
    reckoning has to account for.
    """
    shape = GESTURE_SHAPES.get(kind)
    if shape is None:
        return (0.0, 0.0)
    duration, pan_amp, tilt_amp, cycles = shape
    t = elapsed / duration
    if t < 0.0 or t >= 1.0:
        return (0.0, 0.0)
    envelope = math.sin(math.pi * t)
    wave = math.sin(2.0 * math.pi * cycles * t)
    return (math.radians(pan_amp * wave * envelope), math.radians(tilt_amp * wave * envelope))


def _idle_offset_rad(
    idle_amplitude_deg: float,
    idle_freq_hz: float,
    tilt_bias_deg: float,
    micro_motion: float,
    phase: float,
    now: float,
) -> tuple[float, float]:
    """The idle drift, computed straight from `/emotion/state`'s raw fields.

    Deliberately **not** a call into `neo_emotion.motion.EmotionMotion`:
    `neo_motion`'s package.xml has no build dependency on `neo_emotion` by
    design (emotion publishes parameters; it must never grow a path to the
    servos, and importing its motion module would be a step in that
    direction). This mirrors `IdleMotion.offset` in neo_emotion/motion.py --
    same two incommensurate sine components, same micro-motion term -- kept in
    sync by eye rather than by a shared import, the same trade-off
    `GestureRecognizer.explain` makes against `_classify` in neo_perception.
    """
    second_component = 0.61803  # irrational-ish ratio: the pattern never re-aligns
    t = now + phase
    a = idle_amplitude_deg
    f = idle_freq_hz

    pan = a * (
        0.7 * math.sin(2 * math.pi * f * t)
        + 0.3 * math.sin(2 * math.pi * f * second_component * t + 1.1)
    )
    tilt = a * 0.45 * math.sin(2 * math.pi * f * 0.83 * t + 0.6)

    if micro_motion > 0:
        micro = micro_motion * 0.25
        pan += micro * math.sin(2 * math.pi * 1.7 * t)
        tilt += micro * math.sin(2 * math.pi * 2.3 * t + 0.4)

    return (math.radians(pan), math.radians(tilt + tilt_bias_deg))


class HeadBehaviorNode(Node):
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 3.3).

    subscribe  /joy                    sensor_msgs/Joy          -> MANUAL (70)
               /perception/attention   neo_msgs/AttentionTarget -> GAZE (30)
               /emotion/state          neo_msgs/EmotionState    -> blend params
               /head/state             sensor_msgs/JointState   -> sync_to()
               /emotion/gesture        std_msgs/String          -> GESTURE (50)
    publish    /head/command           neo_msgs/HeadCommand     @ 50 Hz
    service    /head/center            std_srvs/Trigger

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

    def __init__(
        self,
        config: ArbiterConfig | None = None,
        limits: HeadLimits | None = None,
    ) -> None:
        ok, why = probe()
        if not ok:
            raise RuntimeError(f"ROS head behavior unavailable: {why}")

        from neo_msgs.msg import AttentionTarget, EmotionState, HeadCommand as HeadCommandMsg
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import Joy, JointState
        from std_msgs.msg import String
        from std_srvs.srv import Trigger

        super().__init__("head_behavior")

        self.arbiter = HeadArbiter(config or ArbiterConfig(source_timeout_s=JOY_TIMEOUT_S))
        self.limits = limits or MotionConfig.load().head_limits()

        # Last known emotion params (defaults match neo_emotion's NEUTRAL profile
        # closely enough to drift gently before the first /emotion/state arrives).
        self._idle_amplitude_deg = 2.0
        self._idle_freq_hz = 0.12
        self._gaze_gain = 1.0
        self._tilt_bias_deg = 0.0
        self._micro_motion = 0.35
        self._idle_phase = 0.0
        self._idle_base = (0.0, 0.0)

        self._manual_target: tuple[float, float] | None = None
        self._last_joy_time: float | None = None
        self._attention: AttentionTarget | None = None
        self._last_source = "idle"
        self._gesture: tuple[str, float] | None = None

        qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
        )
        self.create_subscription(Joy, "/joy", self._on_joy, qos)
        self.create_subscription(
            AttentionTarget, "/perception/attention", self._on_attention, qos
        )
        self.create_subscription(EmotionState, "/emotion/state", self._on_emotion, 10)
        self.create_subscription(String, "/emotion/gesture", self._on_gesture, 10)
        self.create_subscription(JointState, "/head/state", self._on_head_state, qos)

        self._pub = self.create_publisher(HeadCommandMsg, "/head/command", qos)
        self.create_service(Trigger, "/head/center", self._on_center)
        self._timer = self.create_timer(1.0 / RATE_HZ, self._on_timer)
        self.get_logger().info("head_behavior: arbitrating manual/gesture/gaze/idle")

    # -- clock -----------------------------------------------------------

    def _now(self) -> float:
        return self.get_clock().now().nanoseconds / 1e9

    # -- ROS callbacks -----------------------------------------------------

    def _on_joy(self, msg) -> None:
        # Axes are a *rate* (a spring-centred stick), so this edge integrates
        # them into the absolute target the wire carries -- mirroring
        # neo_webapp.bridge.mock.MockBridge's virtual joystick, the reference
        # implementation this design was validated against.
        now = self._now()
        dt = 0.0 if self._last_joy_time is None else max(0.0, now - self._last_joy_time)
        self._last_joy_time = now

        ax = msg.axes[0] if len(msg.axes) > 0 else 0.0
        ay = msg.axes[1] if len(msg.axes) > 1 else 0.0

        if ax or ay:
            if self._manual_target is None:
                self._manual_target = self._idle_base
            pan, tilt = self._manual_target
            pan = self.limits.pan.clamp(pan + ax * self.limits.pan.max_speed_rad_s * dt)
            tilt = self.limits.tilt.clamp(tilt + ay * self.limits.tilt.max_speed_rad_s * dt)
            self._manual_target = (pan, tilt)
            self.arbiter.submit(
                "manual",
                HeadCommand(pan_rad=pan, tilt_rad=tilt, priority=Priority.MANUAL, stamp=now),
            )
        else:
            self._manual_target = None
            self.arbiter.withdraw("manual")

    def _on_attention(self, msg) -> None:
        self._attention = msg

    def _on_emotion(self, msg) -> None:
        self._idle_amplitude_deg = msg.idle_amplitude_deg
        self._idle_freq_hz = msg.idle_freq_hz
        self._gaze_gain = msg.gaze_gain
        self._tilt_bias_deg = msg.tilt_bias_deg
        self._micro_motion = msg.micro_motion

    def _on_gesture(self, msg) -> None:
        kind = (msg.data or "").strip().lower()
        if kind not in GESTURE_SHAPES:
            self.get_logger().warning(f"ignoring unknown gesture {kind!r}")
            return
        # Replace whatever is running rather than queueing: a queue lets a burst
        # of dialogue events buy several seconds of head movement that outlives
        # the moment that caused it (the same rule EmotionMotion.trigger keeps).
        self._gesture = (kind, self._now())

    def _on_center(self, request, response):
        """Send the head back to centre from wherever it was last aimed.

        Every accumulator is reset with it, the same as the panel's simulated
        robot does: a stale joystick target or a resting point left over from
        the last gaze lock would drive the head straight back out of centre on
        the next tick, which looks exactly like the button not working. The
        arbiter's crossfade then carries the head there rather than snapping.
        """
        self._manual_target = None
        self._gesture = None
        self._idle_base = (0.0, 0.0)
        self.arbiter.withdraw("manual")
        self.arbiter.withdraw("gesture")
        response.success = True
        response.message = "centering"
        return response

    def _on_head_state(self, msg) -> None:
        if len(msg.position) >= 2:
            self.arbiter.sync_to(msg.position[0], msg.position[1])

    def _on_timer(self) -> None:
        now = self._now()

        target = self._attention
        if target is not None and target.engaged and target.confidence > 0.0:
            # A bearing, not a rate -- see CLAUDE.md on why gaze cannot be
            # integrated as one here the way the joystick is.
            eagerness = max(0.1, min(1.0, self._gaze_gain))
            self.arbiter.submit(
                "gaze",
                HeadCommand(
                    pan_rad=math.radians(target.x * CAMERA_HFOV_DEG / 2.0),
                    tilt_rad=math.radians(target.y * CAMERA_VFOV_DEG / 2.0),
                    max_speed_rad_s=self.limits.pan.max_speed_rad_s * eagerness,
                    priority=Priority.GAZE,
                    stamp=now,
                ),
            )
        else:
            # Presence alone never moves the head (CLAUDE.md): noticing is not
            # following, and a momentarily-lost lock (aim reads as dead centre)
            # should hold rather than swing to the middle.
            self.arbiter.withdraw("gaze")

        self._submit_gesture(now)

        dpan, dtilt = _idle_offset_rad(
            self._idle_amplitude_deg,
            self._idle_freq_hz,
            self._tilt_bias_deg,
            self._micro_motion,
            self._idle_phase,
            now,
        )
        self.arbiter.submit(
            "idle",
            HeadCommand(
                pan_rad=self._idle_base[0] + dpan,
                tilt_rad=self._idle_base[1] + dtilt,
                priority=Priority.IDLE,
                stamp=now,
            ),
        )

        resolved = self.arbiter.resolve(now)
        if resolved.source not in ("idle", "gesture"):
            # Relative to where the head is resting, not to centre -- an
            # absolute idle target would quietly undo wherever the operator or
            # gaze last aimed it.
            #
            # "gesture" is excluded for a different reason: the gesture overlay
            # is *computed from* `_idle_base`, so adopting its output as the new
            # base feeds it back into itself and the head walks away by one
            # overlay per tick instead of nodding and returning.
            self._idle_base = (resolved.command.pan_rad, resolved.command.tilt_rad)
        self._last_source = resolved.source
        self._publish(resolved.command, now)

    def _submit_gesture(self, now: float) -> None:
        """A nod or a shake, as an overlay on where the head already is.

        Relative to `_idle_base` -- the resting point gaze or the operator last
        established -- so a nod is a nod *at the person*, not a nod that first
        swings the head back to centre. Above GAZE so it is visible while
        tracking someone, and below MANUAL so it never fights an operator.
        """
        if self._gesture is None:
            self.arbiter.withdraw("gesture")
            return
        kind, started = self._gesture
        dpan, dtilt = _gesture_offset_rad(kind, now - started)
        if dpan == 0.0 and dtilt == 0.0:
            self._gesture = None
            self.arbiter.withdraw("gesture")
            return
        self.arbiter.submit(
            "gesture",
            HeadCommand(
                pan_rad=self._idle_base[0] + dpan,
                tilt_rad=self._idle_base[1] + dtilt,
                priority=Priority.GESTURE,
                stamp=now,
            ),
        )

    def _publish(self, command: HeadCommand, now: float) -> None:
        from neo_msgs.msg import HeadCommand as HeadCommandMsg

        msg = HeadCommandMsg()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.pan_rad = command.pan_rad
        msg.tilt_rad = command.tilt_rad
        msg.max_speed_rad_s = command.max_speed_rad_s
        msg.priority = int(command.priority)
        self._pub.publish(msg)


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start head behavior: {why}")
        print("The arbiter runs without ROS; the admin panel's Head tab drives it.")
        return 1

    import rclpy

    rclpy.init(args=argv)
    node = HeadBehaviorNode()
    import signal

    from rclpy.executors import ExternalShutdownException

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        # ctrl-C and `timeout` signal the whole process group, and ros2 launch
        # forwards its own SIGINT on top. The second one landed inside
        # destroy_node() and cut the shutdown short -- measured on the Pi. There
        # is nothing left to interrupt by now, so finish cleaning up instead.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
