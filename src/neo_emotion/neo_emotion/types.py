"""Emotion types, mirroring the frozen `neo_msgs/EmotionState` contract.

Emotion here is a **movement modifier, not an actuator path** (CLAUDE.md). It
never talks to a servo; it publishes parameters that shape how the head moves,
and `head_behavior` blends them into whatever the arbiter already decided. There
is exactly one path to the servos and this is not it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class EmotionLabel(IntEnum):
    """Mirrors the constants in neo_msgs/EmotionState.msg."""

    NEUTRAL = 0
    ATTENTIVE = 1
    HAPPY = 2
    CURIOUS = 3
    CONFUSED = 4
    SLEEPY = 5


@dataclass(frozen=True)
class MotionParams:
    """How this emotion changes the way the head moves.

    Field-for-field with EmotionState.msg. Degrees and hertz here because that
    is what the contract carries and what is legible when tuning by eye -- the
    conversion to radians happens where these meet the motion path.
    """

    idle_amplitude_deg: float = 2.0
    idle_freq_hz: float = 0.12
    gaze_gain: float = 1.0
    gaze_lag: float = 0.25
    tilt_bias_deg: float = 0.0
    micro_motion: float = 0.35
    settle_time: float = 0.6


@dataclass(frozen=True)
class EmotionState:
    label: EmotionLabel = EmotionLabel.NEUTRAL
    intensity: float = 0.0
    params: MotionParams = MotionParams()


# Tuned by eye rather than derived: this table is the whole personality, and the
# only honest way to set it is to watch the head for a few minutes at a time.
# The failure mode is motion that reads as mechanical, or worse, twitchy.
PROFILES: dict[EmotionLabel, MotionParams] = {
    EmotionLabel.NEUTRAL: MotionParams(),
    # Someone is here and being watched: steadier, quicker to follow, almost no
    # idle drift because drift reads as inattention.
    EmotionLabel.ATTENTIVE: MotionParams(
        idle_amplitude_deg=0.8, idle_freq_hz=0.18, gaze_gain=1.25, gaze_lag=0.15,
        micro_motion=0.5, settle_time=0.35,
    ),
    EmotionLabel.HAPPY: MotionParams(
        idle_amplitude_deg=2.5, idle_freq_hz=0.35, gaze_gain=1.2, gaze_lag=0.15,
        tilt_bias_deg=3.0, micro_motion=0.7, settle_time=0.3,
    ),
    # Curiosity reads as a tilt more than as anything else.
    EmotionLabel.CURIOUS: MotionParams(
        idle_amplitude_deg=1.8, idle_freq_hz=0.22, gaze_gain=1.1, gaze_lag=0.2,
        tilt_bias_deg=7.0, micro_motion=0.6, settle_time=0.5,
    ),
    EmotionLabel.CONFUSED: MotionParams(
        idle_amplitude_deg=2.2, idle_freq_hz=0.28, gaze_gain=0.8, gaze_lag=0.4,
        tilt_bias_deg=-5.0, micro_motion=0.55, settle_time=0.8,
    ),
    # Absence of micro-motion is what actually reads as "asleep"; amplitude
    # alone just looks like a slow version of awake.
    EmotionLabel.SLEEPY: MotionParams(
        idle_amplitude_deg=1.2, idle_freq_hz=0.05, gaze_gain=0.5, gaze_lag=0.6,
        tilt_bias_deg=-8.0, micro_motion=0.02, settle_time=1.5,
    ),
}
