"""The emotion state machine.

Driven by **observable events, not vibes**: a person appears, a gesture engages,
a lookup misses, the link drops, nobody has been here for a while. Every
transition can be traced to something that actually happened, which is what
makes the behaviour debuggable rather than merely charming.

Minimum dwell times on every state, because the events themselves are noisy --
a detector that flickers for one frame would otherwise flip the robot's
apparent mood several times a second, which reads as a fault.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .types import PROFILES, EmotionLabel, EmotionState

log = logging.getLogger(__name__)


@dataclass
class EmotionConfig:
    min_dwell_s: float = 1.2
    """Shortest time in any state before another may replace it.

    Not applied to states entered by a deliberate, discrete event (engaging,
    a failed lookup): those *should* be immediate, and they are rare enough not
    to flicker.
    """

    happy_s: float = 2.0
    """A greeting is a moment, not a mood."""

    confused_s: float = 2.5
    sleepy_after_s: float = 90.0
    """No person in view for this long and the robot visibly powers down. Also
    what stops an empty lobby being watched attentively all night."""

    curious_s: float = 1.5
    """How long a newly-arrived person reads as interesting before it settles
    into plain attention."""


@dataclass
class EmotionEvents:
    """What the rest of the system observed this tick.

    A snapshot rather than a stream of callbacks: the emotion node runs on a
    timer and needs the current world, not a backlog of everything that ever
    happened.
    """

    person_present: bool = False
    engaged: bool = False
    person_arrived: bool = False
    gesture_seen: bool = False
    lookup_failed: bool = False
    link_down: bool = False
    speaking: bool = False


class EmotionController:
    def __init__(self, config: EmotionConfig | None = None) -> None:
        self.cfg = config or EmotionConfig()
        self.label = EmotionLabel.NEUTRAL
        self.intensity = 0.0
        self._entered_at = 0.0
        self._last_person_at = 0.0
        self._started = False

    def update(self, events: EmotionEvents, now: float) -> EmotionState:
        if not self._started:
            self._entered_at = now
            self._last_person_at = now
            self._started = True
        if events.person_present:
            self._last_person_at = now

        target, intensity, immediate = self._decide(events, now)
        held = now - self._entered_at
        if target is not self.label and (immediate or held >= self.cfg.min_dwell_s):
            log.debug("emotion %s -> %s", self.label.name, target.name)
            self.label = target
            self._entered_at = now

        self.intensity = intensity
        return self.state

    def _decide(
        self, events: EmotionEvents, now: float
    ) -> tuple[EmotionLabel, float, bool]:
        """Returns (label, intensity, bypasses_dwell)."""
        since_entered = now - self._entered_at

        # Discrete, deliberate events first. These are allowed to interrupt.
        if events.lookup_failed:
            return EmotionLabel.CONFUSED, 0.8, True
        if events.gesture_seen and not events.engaged:
            return EmotionLabel.HAPPY, 0.9, True

        # Time-limited moods decay back rather than latching.
        if self.label is EmotionLabel.HAPPY and since_entered < self.cfg.happy_s:
            return EmotionLabel.HAPPY, 0.9, False
        if self.label is EmotionLabel.CONFUSED and since_entered < self.cfg.confused_s:
            return EmotionLabel.CONFUSED, 0.7, False

        if events.person_arrived:
            return EmotionLabel.CURIOUS, 0.7, True
        if self.label is EmotionLabel.CURIOUS and since_entered < self.cfg.curious_s:
            return EmotionLabel.CURIOUS, 0.7, False

        if events.engaged:
            return EmotionLabel.ATTENTIVE, 1.0, False
        if events.person_present:
            return EmotionLabel.ATTENTIVE, 0.5, False

        if (now - self._last_person_at) >= self.cfg.sleepy_after_s:
            return EmotionLabel.SLEEPY, 0.6, False

        # A dead link is a subdued neutral, not a distinct mood: degraded mode
        # is the normal operating state here, so it must not look like distress.
        if events.link_down:
            return EmotionLabel.NEUTRAL, 0.2, False
        return EmotionLabel.NEUTRAL, 0.3, False

    @property
    def state(self) -> EmotionState:
        return EmotionState(
            label=self.label,
            intensity=self.intensity,
            params=PROFILES[self.label],
        )

    def reset(self, now: float = 0.0) -> None:
        self.label = EmotionLabel.NEUTRAL
        self.intensity = 0.0
        self._entered_at = now
        self._last_person_at = now
