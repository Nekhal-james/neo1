"""When audio sent to a speaker this process cannot hear stops being audible.

No ROS here, so it is testable with a fake clock.
"""

from __future__ import annotations

import time
from collections.abc import Callable

BYTES_PER_SAMPLE = 2  # S16_LE, AudioChunk's only encoding


class PlaybackClock:
    """A play cursor, advanced the way the browser advances its own.

    The panel's speaker schedules each chunk at the later of "now" and the end
    of the previous chunk (neo_webapp/voice.py keeps the same cursor), so the
    moment Neo's reply stops sounding is that cursor -- not the moment the last
    chunk was forwarded. Synthesis runs ~20x real time (CLAUDE.md), so the two
    are seconds apart, and a half-duplex gate built on forwarding would unmute
    the wake word while the reply was still coming out of the browser.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._until = 0.0

    @property
    def until(self) -> float:
        return self._until

    def add(self, nbytes: int, sample_rate: int, channels: int = 1) -> float:
        """Account for one chunk; returns when everything so far stops playing."""
        if nbytes <= 0 or sample_rate <= 0 or channels <= 0:
            return self._until
        duration = nbytes / float(sample_rate * channels * BYTES_PER_SAMPLE)
        self._until = max(self._until, self._clock()) + duration
        return self._until

    def playing(self) -> bool:
        return self._clock() < self._until

    def reset(self) -> None:
        self._until = 0.0
