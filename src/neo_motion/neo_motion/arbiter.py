"""Who gets to point the head.

Several sources want the head at once -- idle drift, a gaze target, a gesture
overlay, an operator on the joystick, an e-stop. They are ranked, and the
highest-ranked *fresh* one wins:

    estop > manual > gesture > gaze > idle

Two rules that are not obvious:

**Freshness, not just priority.** A source that has stopped publishing must lose,
or a joystick whose browser tab was closed would hold the head forever at
priority 70. Every command carries a stamp and expires; this is the same deadman
the panel enforces at its own edge, generalised so it protects against *any*
source dying, not only that one.

**Crossfade, don't jump.** When the winner changes, the output ramps from where
the head was to where the new winner wants it, over `crossfade_s`. Snapping
between two sources looks like a fault, and on real gears it is a shock load.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import HeadCommand, Priority


@dataclass
class ArbiterConfig:
    source_timeout_s: float = 0.5
    """How long a source's command stays valid without being resubmitted.

    The generalised deadman. Long enough to survive one missed cycle at the
    50 Hz the driver runs at, short enough that a dead source releases the head
    while somebody is still watching it.
    """

    crossfade_s: float = 0.3
    """Ramp time when the winning source changes."""


@dataclass
class Resolution:
    """What the arbiter decided this tick."""

    command: HeadCommand
    source: str = "idle"
    blending: bool = False
    """True while a crossfade is in progress, so the UI can show it and tests
    can assert the ramp rather than only its endpoints."""


class HeadArbiter:
    def __init__(self, config: ArbiterConfig | None = None) -> None:
        self.cfg = config or ArbiterConfig()
        self._commands: dict[str, HeadCommand] = {}
        self._winner: str | None = None
        self._fade_from: tuple[float, float] | None = None
        self._fade_start: float = 0.0
        self._last_output = (0.0, 0.0)

    # -- input -------------------------------------------------------------

    def submit(self, source: str, command: HeadCommand) -> None:
        """Offer a command from a named source. Cheap; call it every tick."""
        self._commands[source] = command

    def withdraw(self, source: str) -> None:
        """Explicitly give up the head, rather than waiting out the timeout."""
        self._commands.pop(source, None)

    def clear(self) -> None:
        self._commands.clear()
        self._winner = None
        self._fade_from = None

    # -- resolution --------------------------------------------------------

    def resolve(self, now: float) -> Resolution:
        live = {
            name: cmd
            for name, cmd in self._commands.items()
            if (now - cmd.stamp) <= self.cfg.source_timeout_s
        }

        if not live:
            # Nothing fresh: hold position rather than falling to zero, which
            # would swing the head to centre every time a source hiccups.
            return Resolution(
                command=HeadCommand(*self._last_output, priority=Priority.IDLE, stamp=now),
                source="none",
            )

        name = max(live, key=lambda n: (live[n].priority, n))
        winning = live[name]
        target = (winning.pan_rad, winning.tilt_rad)

        if name != self._winner:
            # Ramp from wherever the head actually is, not from the previous
            # source's target -- those differ whenever the last fade was cut
            # short by a third source.
            self._fade_from = self._last_output
            self._fade_start = now
            self._winner = name

        blending = False
        if self._fade_from is not None and self.cfg.crossfade_s > 0:
            alpha = (now - self._fade_start) / self.cfg.crossfade_s
            if alpha < 1.0:
                blending = True
                a = max(0.0, alpha)
                target = (
                    self._fade_from[0] + (target[0] - self._fade_from[0]) * a,
                    self._fade_from[1] + (target[1] - self._fade_from[1]) * a,
                )
            else:
                self._fade_from = None

        self._last_output = target
        return Resolution(
            command=HeadCommand(
                pan_rad=target[0],
                tilt_rad=target[1],
                priority=winning.priority,
                max_speed_rad_s=winning.max_speed_rad_s,
                stamp=now,
            ),
            source=name,
            blending=blending,
        )

    # -- introspection -----------------------------------------------------

    def sync_to(self, pan_rad: float, tilt_rad: float) -> None:
        """Tell the arbiter where the head actually is.

        Called by the driver after it has clamped and slew-limited, so a
        crossfade starts from the real pose rather than from a target the head
        never reached.
        """
        self._last_output = (pan_rad, tilt_rad)

    @property
    def winner(self) -> str | None:
        return self._winner

    def live_sources(self, now: float) -> list[str]:
        return sorted(
            n for n, c in self._commands.items()
            if (now - c.stamp) <= self.cfg.source_timeout_s
        )
