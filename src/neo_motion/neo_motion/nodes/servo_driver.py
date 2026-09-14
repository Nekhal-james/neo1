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
from ..types import HeadLimits

log = logging.getLogger(__name__)

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


class ServoDriverNode:
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
        config = config or MotionConfig.load()
        self.driver = ServoDriver(
            # Never wider than the calibration, or /head/state reports a pose the
            # servo was clamped short of.
            limits=config.head_limits(limits),
            # The configured driver by name, never `auto`: on the robot, missing
            # PWM hardware must be an error, not a silent mock.
            backend=backend or make_backend(config.driver, **config.backend_kwargs()),
        )
        raise NotImplementedError("phase-3: bind /head/command, /head/state, /head/estop")


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start servo driver: {why}")
        print("The driver runs headless against a mock backend:")
        print("  python -c 'from neo_motion.driver import ServoDriver; ...'")
        print("or drive it from the admin panel's Head tab, which uses the same core.")
        return 1
    raise NotImplementedError("phase-3: bind /head/command, /head/state, /head/estop")
