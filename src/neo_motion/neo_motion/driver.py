"""The last line before the servos.

Everything here exists because an upstream node can be wrong. The driver never
trusts a command: it clamps, rate-limits, and refuses to keep moving when the
commands stop arriving. Ordering matters and is deliberate:

    clamp to soft limits -> slew-rate limit -> deadband -> watchdog -> estop

Clamping first means a wild target cannot be reached even briefly. Rate limiting
after it turns a step command into a move a gearbox survives. The deadband stops
the hunting that a rate limiter alone leaves behind. The watchdog and e-stop sit
outermost because they must win over everything, including a perfectly valid
command that simply arrived too long ago.

Holding position is the safe state, not releasing: a head that goes limp drops
under its own weight.
"""

from __future__ import annotations

import logging

from .backend import MockServoBackend, ServoBackend
from .types import AxisLimits, HeadCommand, HeadLimits, HeadPose

log = logging.getLogger(__name__)



class ServoDriver:
    def __init__(
        self,
        limits: HeadLimits | None = None,
        backend: ServoBackend | None = None,
        *,
        watchdog_s: float = 0.5,
        start: HeadPose | None = None,
    ) -> None:
        self.limits = limits or HeadLimits()
        self.backend = backend or MockServoBackend()
        self.watchdog_s = watchdog_s

        start = start or HeadPose()
        self.pan_rad = self.limits.pan.clamp(start.pan_rad)
        self.tilt_rad = self.limits.tilt.clamp(start.tilt_rad)

        self._command: HeadCommand | None = None
        self._estop = False
        self.at_limit = False
        self.watchdog_tripped = False

    # -- input -------------------------------------------------------------

    def command(self, cmd: HeadCommand) -> None:
        self._command = cmd

    def set_estop(self, engaged: bool) -> None:
        """Freeze immediately, and stay frozen until explicitly released."""
        if engaged and not self._estop:
            log.warning("e-stop engaged: head frozen at %.1f, %.1f deg", *self.pose.degrees())
        self._estop = engaged

    @property
    def estop(self) -> bool:
        return self._estop

    @property
    def pose(self) -> HeadPose:
        return HeadPose(self.pan_rad, self.tilt_rad)

    # -- the control step --------------------------------------------------

    def step(self, now: float, dt: float) -> HeadPose:
        """Advance one tick and drive the servos. Call at a steady rate."""
        if dt <= 0:
            return self.pose

        if self._estop:
            # Frozen, but still *held*: the position is rewritten so the servos
            # keep their torque rather than drifting under load.
            self.watchdog_tripped = False
            self._write()
            return self.pose

        cmd = self._command
        stale = cmd is None or (now - cmd.stamp) > self.watchdog_s
        if stale:
            if not self.watchdog_tripped and cmd is not None:
                log.warning("no command for %.0f ms: holding", self.watchdog_s * 1000)
            self.watchdog_tripped = True
            self._write()
            return self.pose
        self.watchdog_tripped = False

        self.pan_rad, pan_limited = self._advance(
            self.limits.pan, self.pan_rad, cmd.pan_rad, dt, cmd.max_speed_rad_s
        )
        self.tilt_rad, tilt_limited = self._advance(
            self.limits.tilt, self.tilt_rad, cmd.tilt_rad, dt, cmd.max_speed_rad_s
        )
        self.at_limit = pan_limited or tilt_limited
        self._write()
        return self.pose

    def _advance(
        self,
        limits: AxisLimits,
        current: float,
        target: float,
        dt: float,
        max_speed_override: float,
    ) -> tuple[float, bool]:
        target = limits.clamp(target)
        error = target - current
        # The deadband is the tolerance, not 1e-6. An axis stops as much as a
        # deadband short of its stop -- that last fraction of a degree is below
        # the resolution it will chase -- so an exact comparison reports "not at
        # the limit" precisely when someone is holding the stick hard against it,
        # and the panel's indicator goes dark at the moment it matters.
        at = limits.at_limit(current, tolerance=limits.deadband_rad)
        if abs(error) <= limits.deadband_rad:
            return current, at

        speed = limits.max_speed_rad_s
        if max_speed_override > 0:
            # The driver's own cap always wins: a request may only ask for less.
            speed = min(speed, max_speed_override)

        step = speed * dt
        moved = current + (step if error > 0 else -step) if abs(error) > step else target
        moved = limits.clamp(moved)
        return moved, limits.at_limit(moved, tolerance=limits.deadband_rad)

    # -- lifecycle ---------------------------------------------------------

    def center(self) -> HeadPose:
        """Snap to neutral. Used on a clean shutdown and by the panel's button.

        Deliberately not rate-limited: it is a deliberate operator action at a
        known-safe moment, not part of the control loop.
        """
        self.pan_rad = self.limits.pan.clamp(0.0)
        self.tilt_rad = self.limits.tilt.clamp(0.0)
        self._write()
        return self.pose

    def shutdown(self) -> None:
        """Centre, then stop driving. The only place going limp is correct."""
        self.center()
        self.backend.release()

    def _write(self) -> None:
        self.backend.write(self.pan_rad, self.tilt_rad)
