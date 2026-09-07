"""On-demand media channel lifecycle.

The camera/mic/speaker bridges are the expensive half of the panel (plan 0.3.4).
They must exist only while someone is using them, so the always-on API can stay
resident in the field profile without a media pipeline eating a core.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from ..bridge import Bridge

log = logging.getLogger(__name__)

CHANNELS = ("camera", "mic", "speaker", "joy")


class MediaManager:
    """Reference-counts open channels and mirrors them onto the robot state."""

    def __init__(self, bridge: Bridge) -> None:
        self._bridge = bridge
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def open(self, channel: str) -> None:
        async with self._lock:
            self._counts[channel] += 1
            if self._counts[channel] == 1:
                log.info("media channel opened: %s", channel)
                self._mirror(channel, True)

    async def close(self, channel: str) -> None:
        async with self._lock:
            self._counts[channel] = max(0, self._counts[channel] - 1)
            if self._counts[channel] == 0:
                log.info("media channel closed: %s", channel)
                self._mirror(channel, False)

    def active(self, channel: str) -> bool:
        return self._counts[channel] > 0

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def _mirror(self, channel: str, on: bool) -> None:
        media = self._bridge.snapshot().media
        if hasattr(media, channel):
            setattr(media, channel, on)
