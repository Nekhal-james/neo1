"""Motion types, mirroring the frozen `neo_msgs/HeadCommand` contract.

Plain Python, no ROS: the limiting, arbitration and watchdog logic is what
actually keeps a servo from tearing itself apart, so it has to be testable
without a robot, a ROS install, or an I2C bus.

Radians throughout, matching the wire contract. The panel shows degrees and
converts at its own edge; that boundary is the only place a factor of 180/pi
belongs (see `neo_msgs/msg/HeadCommand.msg`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum


class Priority(IntEnum):
    """Mirrors the PRIORITY_* constants in neo_msgs/HeadCommand.msg.

    Gaps are deliberate: a navigation source can be inserted when there is a
    body to navigate, without renumbering anything already deployed.
    """

    IDLE = 10
    GAZE = 30
    GESTURE = 50
    MANUAL = 70
    ESTOP = 90


@dataclass(frozen=True)
class HeadCommand:
    """A *request* to point the head, not an order.

    `servo_driver` clamps to its own soft limits and slew caps before driving
    anything and never trusts an upstream node -- a command outside the limits
    is silently clamped, not rejected.
    """

    pan_rad: float = 0.0
    tilt_rad: float = 0.0
    priority: int = Priority.IDLE
    max_speed_rad_s: float = 0.0
    """Ceiling on how fast to get there. <= 0 means "use the driver's default".
    The driver's own cap still applies and is always the lower of the two."""

    stamp: float = 0.0


@dataclass(frozen=True)
class HeadPose:
    pan_rad: float = 0.0
    tilt_rad: float = 0.0

    def degrees(self) -> tuple[float, float]:
        return (math.degrees(self.pan_rad), math.degrees(self.tilt_rad))


@dataclass
class AxisLimits:
    """One axis's mechanical and safety envelope.

    Defaults are a placeholder until Phase 0's calibration measures the real
    ones per servo into `neo_bringup/config/servos.yaml`; nothing should ship to
    hardware on these numbers.
    """

    min_rad: float = math.radians(-90.0)
    max_rad: float = math.radians(90.0)
    max_speed_rad_s: float = math.radians(60.0)

    deadband_rad: float = math.radians(0.4)
    """Below this, hold still rather than chase.

    Without it the head hunts forever around its target, which reads as a
    nervous twitch and grinds the gears for no benefit.

    **This number also sets how smooth slow motion can be, so calibration is not
    free to make it large.** A deadband cannot distinguish a stationary target
    with jitter on it from a target moving slowly, and treats both the same way:
    the error creeps up to the threshold, the axis covers the whole gap in one
    tick, and the error resets. Slow motion therefore comes out quantized into
    steps of roughly one deadband -- measured, a 2 deg idle sine renders as 58
    jumps of 0.43 deg over 30 s at this value, against 256 jumps of 0.12 deg at
    0.1 deg. The first reads as a twitch, the second as drift.

    There is no way to have both: refusing sub-deadband corrections *is* what
    quantizes a slow ramp. The only lever is the size. For scale, a PCA9685
    driving 500-2500 us across 180 deg resolves about 0.044 deg per step, so
    this placeholder is roughly nine times coarser than the hardware can render
    -- there is real room, and Phase 0 should spend it. Idle drift and tracking
    a person at a distance are the two things that get worse if it does not.
    """

    def clamp(self, value: float) -> float:
        return max(self.min_rad, min(self.max_rad, value))

    def at_limit(self, value: float, tolerance: float = 1e-6) -> bool:
        return value <= self.min_rad + tolerance or value >= self.max_rad - tolerance


@dataclass
class HeadLimits:
    pan: AxisLimits = field(default_factory=AxisLimits)
    tilt: AxisLimits = field(
        default_factory=lambda: AxisLimits(
            min_rad=math.radians(-35.0),
            max_rad=math.radians(35.0),
            max_speed_rad_s=math.radians(45.0),
        )
    )
