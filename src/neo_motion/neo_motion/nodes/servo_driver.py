"""ROS 2 node wrapping the servo driver.

The last code that runs before something physical moves, so it is the node that
trusts nothing. Every safety rule -- clamp, slew-limit, deadband, watchdog,
estop -- lives in `ServoDriver`, tested at 50 Hz against a mock backend with no
I2C bus, no PCA9685 and no servos attached. This file only converts messages and
turns the crank.

Activates in Phase 3 (docs/IMPLEMENTATION_PLAN.md section 3.2).
"""

from __future__ import annotations

import logging

from ..backend import ServoBackend, make_backend
from ..config import MotionConfig
from ..driver import ServoDriver
from ..types import HeadCommand, HeadLimits

log = logging.getLogger(__name__)

try:
    from rclpy.node import Node
except ImportError:  # pragma: no cover - exercised only where ROS is installed
    Node = object

RATE_HZ = 50.0
"""Fixed, and not a parameter.

The driver's slew limiting is per-tick, so the tick rate is part of how fast the
head is allowed to move. A configurable rate would silently change the enforced
speed cap, which is exactly the kind of safety property that must not be tunable
from a YAML file.
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


class ServoDriverNode(Node):
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 3.2).

    subscribe  /head/command      neo_msgs/HeadCommand
    publish    /head/state        sensor_msgs/JointState   @ 50 Hz
    service    /head/estop        std_srvs/SetBool

    Four rules this node must not undo, all already enforced by `ServoDriver`:

    * **The timer drives, not the subscription.** `step()` runs on a 50 Hz timer
      regardless of whether commands are arriving. A subscription-driven driver
      stops ticking exactly when its input dies, which is the moment the
      watchdog most needs to run.
    * **Stamp from the message, not from the clock.** The watchdog measures the
      age of the *command*, so a stale message that arrives late must read as
      stale. Re-stamping it on arrival would defeat the deadman that the virtual
      joystick depends on -- see CLAUDE.md on the network-mediated joystick.
    * **Radians end to end.** HeadCommand is rad and JointState is rad by ROS
      convention, so nothing here converts. The panel owns the only 180/pi.
    * **Holding is the safe state.** Neither watchdog expiry nor estop releases
      the servos; a released head drops under its own weight. Only `shutdown()`
      releases, and it centres first.

    Subscribe with depth 1 and BEST_EFFORT: a queue of head commands is a queue
    of stale intentions, and latest-wins is the only sensible reading.
    """

    def __init__(
        self,
        limits: HeadLimits | None = None,
        backend: ServoBackend | None = None,
        config: MotionConfig | None = None,
    ) -> None:
        ok, why = probe()
        if not ok:
            raise RuntimeError(f"ROS servo driver unavailable: {why}")

        from neo_msgs.msg import HeadCommand as HeadCommandMsg
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import JointState
        from std_srvs.srv import SetBool

        super().__init__("servo_driver")

        config = config or MotionConfig.load()
        self.driver = ServoDriver(
            # Never wider than the calibration, or /head/state reports a pose the
            # servo was clamped short of.
            limits=config.head_limits(limits),
            # The configured driver by name, never `auto`: on the robot, missing
            # PWM hardware must be an error, not a silent mock.
            backend=backend or make_backend(config.driver, **config.backend_kwargs()),
        )

        qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
        )
        self._sub = self.create_subscription(
            HeadCommandMsg, "/head/command", self._on_command, qos
        )
        self._state_pub = self.create_publisher(JointState, "/head/state", 10)
        self._estop_srv = self.create_service(SetBool, "/head/estop", self._on_estop)

        self._last_tick = self._now()
        self._timer = self.create_timer(1.0 / RATE_HZ, self._on_timer)
        self.get_logger().info(f"servo_driver: driving '{config.driver}' backend")

    # -- clock ---------------------------------------------------------

    def _now(self) -> float:
        """Seconds on the node's own clock -- the same basis a command's stamp
        must be measured against, whether that is wall time or, under a future
        simulation, sim time."""
        return self.get_clock().now().nanoseconds / 1e9

    # -- ROS callbacks ---------------------------------------------------

    def _on_command(self, msg) -> None:
        from rclpy.time import Time

        # The message's own header stamp, not time.time() on arrival: a command
        # delayed in transit must still read as however old it actually is.
        stamp = Time.from_msg(msg.header.stamp).nanoseconds / 1e9
        self.driver.command(
            HeadCommand(
                pan_rad=msg.pan_rad,
                tilt_rad=msg.tilt_rad,
                priority=msg.priority,
                max_speed_rad_s=msg.max_speed_rad_s,
                stamp=stamp,
            )
        )

    def _on_estop(self, request, response):
        self.driver.set_estop(bool(request.data))
        response.success = True
        response.message = "estop engaged" if request.data else "estop released"
        return response

    def _on_timer(self) -> None:
        now = self._now()
        dt = now - self._last_tick
        self._last_tick = now
        pose = self.driver.step(now, dt)
        self._publish_state(pose)

    def _publish_state(self, pose) -> None:
        from sensor_msgs.msg import JointState

        msg = JointState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.name = ["pan", "tilt"]
        msg.position = [pose.pan_rad, pose.tilt_rad]
        self._state_pub.publish(msg)

    def destroy_node(self) -> None:
        # Centre and release, not just stop publishing -- a killed process must
        # not leave the last pulse running (CLAUDE.md, continuous-rotation
        # servos especially).
        self.driver.shutdown()
        super().destroy_node()


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start servo driver: {why}")
        print("The driver runs headless against a mock backend:")
        print("  python -c 'from neo_motion.driver import ServoDriver; ...'")
        print("or drive it from the admin panel's Head tab, which uses the same core.")
        return 1

    import rclpy

    rclpy.init(args=argv)
    node = ServoDriverNode()
    import signal

    from rclpy.executors import ExternalShutdownException

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        # This node's cleanup is the one that stops the servos, so it must run
        # to the end. ctrl-C and `timeout` signal the whole process group and
        # ros2 launch forwards its own SIGINT on top; on the Pi the second one
        # landed inside destroy_node() and aborted it. A continuous-rotation
        # servo whose shutdown is aborted keeps turning on its last pulse.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
