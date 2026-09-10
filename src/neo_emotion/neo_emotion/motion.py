"""Turning an emotion into movement.

Two things live here, and both are **additive modifiers** on whatever the
arbiter already decided. Neither ever reaches a servo on its own.

**Idle motion.** Two sine components at incommensurate frequencies, so the
pattern never visibly repeats. A single sine reads as a metronome within about
ten seconds, which is worse than no motion at all. Micro-motion rides on top at
a higher rate; its *absence* is what actually makes SLEEPY read as asleep.

**Gestures.** Short, time-parameterised trajectories added on top of the base
pose: a nod, a shake, a tilt, a scan. Every one is bounded in time and returns
to zero -- they are overlays, never modes, so the head cannot get stuck in one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from .types import MotionParams


class GestureKind(str, Enum):
    NOD = "nod"
    SHAKE = "shake"
    TILT = "tilt"
    SCAN = "scan"


# (duration_s, pan_amplitude_deg, tilt_amplitude_deg, cycles)
GESTURE_SHAPES: dict[GestureKind, tuple[float, float, float, float]] = {
    GestureKind.NOD: (0.9, 0.0, 7.0, 2.0),
    GestureKind.SHAKE: (0.9, 9.0, 0.0, 2.0),
    GestureKind.TILT: (1.2, 0.0, 5.0, 0.5),
    GestureKind.SCAN: (2.4, 22.0, 0.0, 1.0),
}


@dataclass
class ActiveGesture:
    kind: GestureKind
    started_at: float

    def offset(self, now: float) -> tuple[float, float]:
        """Pan/tilt offset in degrees, and (0, 0) once finished."""
        duration, pan_amp, tilt_amp, cycles = GESTURE_SHAPES[self.kind]
        t = (now - self.started_at) / duration
        if t < 0.0 or t >= 1.0:
            return (0.0, 0.0)
        # Tapered at both ends so the overlay grows in and dies away instead of
        # starting and stopping with a step.
        envelope = math.sin(math.pi * t)
        wave = math.sin(2.0 * math.pi * cycles * t)
        return (pan_amp * wave * envelope, tilt_amp * wave * envelope)

    def finished(self, now: float) -> bool:
        return (now - self.started_at) >= GESTURE_SHAPES[self.kind][0]


class IdleMotion:
    """Slow drift that keeps a still head from looking switched off."""

    # Irrational-ish ratio: the two components never re-align, so the pattern
    # does not visibly loop the way a single sine does.
    SECOND_COMPONENT = 0.61803

    def __init__(self, phase: float = 0.0) -> None:
        self.phase = phase

    def offset(self, params: MotionParams, now: float) -> tuple[float, float]:
        f = params.idle_freq_hz
        a = params.idle_amplitude_deg
        t = now + self.phase

        pan = a * (
            0.7 * math.sin(2 * math.pi * f * t)
            + 0.3 * math.sin(2 * math.pi * f * self.SECOND_COMPONENT * t + 1.1)
        )
        tilt = a * 0.45 * math.sin(2 * math.pi * f * 0.83 * t + 0.6)

        if params.micro_motion > 0:
            micro = params.micro_motion * 0.25
            pan += micro * math.sin(2 * math.pi * 1.7 * t)
            tilt += micro * math.sin(2 * math.pi * 2.3 * t + 0.4)

        return (pan, tilt + params.tilt_bias_deg)


class EmotionMotion:
    """Idle drift plus at most one running gesture."""

    def __init__(self) -> None:
        self.idle = IdleMotion()
        self._gesture: ActiveGesture | None = None

    def trigger(self, kind: GestureKind, now: float) -> None:
        """Start a gesture, replacing any already running.

        Replacing rather than queueing: a queue would let a burst of dialogue
        events buy several seconds of head movement that outlives the moment
        that caused it.
        """
        self._gesture = ActiveGesture(kind, now)

    @property
    def gesture(self) -> GestureKind | None:
        return self._gesture.kind if self._gesture else None

    def offset_deg(self, params: MotionParams, now: float) -> tuple[float, float]:
        pan, tilt = self.idle.offset(params, now)
        if self._gesture is not None:
            if self._gesture.finished(now):
                self._gesture = None
            else:
                gp, gt = self._gesture.offset(now)
                pan += gp
                tilt += gt
        return (pan, tilt)

    def offset_rad(self, params: MotionParams, now: float) -> tuple[float, float]:
        pan, tilt = self.offset_deg(params, now)
        return (math.radians(pan), math.radians(tilt))
