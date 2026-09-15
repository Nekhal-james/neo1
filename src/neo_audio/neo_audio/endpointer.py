"""When the speaker has finished, so the listening window can close.

The wake word opens a window; something has to close it. Vosk emits a final
result on its own internal endpointing, but that is tuned for dictation and
waits a long time -- long enough that Neo feels like it did not hear you. This
runs alongside it and closes the window on the shape of the audio instead.

Energy against an adaptive noise floor, not a fixed threshold: a reception
desk's noise floor changes through the day, and any absolute level is either
deaf in a busy foyer or permanently triggered in a quiet one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import numpy as np

BYTES_PER_SAMPLE = 2


class Endpoint(str, Enum):
    LISTENING = "listening"
    """Still open: either waiting for speech to start, or it is still going."""

    ENDED = "ended"
    """Speech happened and then stopped. The normal close."""

    TIMED_OUT = "timed_out"
    """The window opened and nobody said anything. A false wake, usually."""

    TOO_LONG = "too_long"
    """Someone is still talking well past any plausible question. Closed so a
    stuck-open mic cannot stream a room to the model host indefinitely."""


@dataclass
class EndpointerConfig:
    silence_to_end_s: float = 0.9
    """Quiet after speech before the window closes. Below ~0.7 s it clips
    people who pause mid-sentence; above ~1.2 s the robot feels deaf."""

    max_wait_for_speech_s: float = 4.0
    """How long a window stays open with nothing said at all."""

    max_utterance_s: float = 15.0
    speech_start_ratio: float = 3.0
    """Times the noise floor that counts as speech. A ratio, not a level, so it
    survives a change of room, mic gain or distance."""

    min_speech_s: float = 0.25
    """Speech shorter than this is a cough or a door, not an utterance."""

    noise_floor_alpha: float = 0.05
    """How fast the floor tracks. Slow: it must follow the room over seconds
    without being dragged up by the speech it is supposed to detect."""


class Endpointer:
    """Feed it the same PCM the recogniser gets; ask it when to stop."""

    def __init__(self, config: EndpointerConfig | None = None, sample_rate: int = 16000) -> None:
        self.config = config or EndpointerConfig()
        self.sample_rate = sample_rate
        self._noise_floor: float | None = None
        self.reset()

    def reset(self) -> None:
        self._elapsed = 0.0
        self._speech_s = 0.0
        self._silence_s = 0.0
        self._heard_speech = False
        # The noise floor is deliberately *not* reset: it is a property of the
        # room, learned across windows, and re-learning it every time would
        # make the first moment of each window the least reliable.

    @property
    def heard_speech(self) -> bool:
        return self._heard_speech

    @property
    def noise_floor(self) -> float:
        return self._noise_floor or 0.0

    def accept(self, pcm: bytes | np.ndarray) -> Endpoint:
        if isinstance(pcm, bytes):
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        else:
            samples = np.asarray(pcm, dtype=np.float32)
        if samples.size == 0:
            return Endpoint.LISTENING

        duration = samples.size / float(self.sample_rate)
        self._elapsed += duration
        rms = float(math.sqrt(float(np.mean(samples * samples)) + 1e-12))

        if self._noise_floor is None:
            self._noise_floor = rms
        is_speech = rms > self._noise_floor * self.config.speech_start_ratio

        if is_speech:
            self._speech_s += duration
            self._silence_s = 0.0
            if self._speech_s >= self.config.min_speech_s:
                self._heard_speech = True
        else:
            self._silence_s += duration
            # Only track the floor while nothing is being said, or the floor
            # climbs to meet the speech and the window never closes.
            a = self.config.noise_floor_alpha
            self._noise_floor = (1 - a) * self._noise_floor + a * rms

        if self._heard_speech:
            if self._silence_s >= self.config.silence_to_end_s:
                return Endpoint.ENDED
            if self._elapsed >= self.config.max_utterance_s:
                return Endpoint.TOO_LONG
        elif self._elapsed >= self.config.max_wait_for_speech_s:
            return Endpoint.TIMED_OUT
        return Endpoint.LISTENING
