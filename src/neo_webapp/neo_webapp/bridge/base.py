"""The seam between the admin panel and the robot.

Nothing above this interface knows whether it is talking to ROS or to a simulation,
in the same way that no node downstream of a source mux knows which backend is live.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
from collections.abc import AsyncIterator

from .types import Backend, Result, RobotState, Stream


class Bridge(abc.ABC):
    """Abstract robot bridge.

    Implementations must be safe to call from the asyncio event loop; anything
    blocking (ROS executors, device I/O) belongs on its own thread.
    """

    name: str = "abstract"

    def __init__(self) -> None:
        self._audio_out: asyncio.Queue[bytes] = asyncio.Queue(maxsize=32)

    # -- lifecycle ---------------------------------------------------------

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    # -- state -------------------------------------------------------------

    @abc.abstractmethod
    def snapshot(self) -> RobotState:
        """Current full state. Cheap: called at the state broadcast rate."""

    # -- commands ----------------------------------------------------------

    @abc.abstractmethod
    async def set_source(self, stream: Stream, backend: Backend) -> Result:
        """Switch one stream's backend.

        Must be transactional (plan section 2.1): activate the new backend, switch
        the mux, then deactivate the old one. On failure, roll back and report --
        never leave a stream with no active backend.
        """

    @abc.abstractmethod
    async def set_estop(self, engaged: bool) -> Result: ...

    @abc.abstractmethod
    async def center_head(self) -> Result: ...

    # -- media in (panel -> robot) ----------------------------------------

    @abc.abstractmethod
    async def publish_joy(self, axes: list[float], buttons: list[int]) -> None:
        """Publish /joy. Neutral axes are published on deadman expiry."""

    @abc.abstractmethod
    async def publish_camera_frame(self, jpeg: bytes) -> None: ...

    @abc.abstractmethod
    async def publish_mic_chunk(self, pcm_s16le: bytes) -> None: ...

    # -- media out (robot -> panel) ---------------------------------------

    async def emit_audio_out(self, pcm_s16le: bytes) -> None:
        """Queue audio for the panel's speaker channel, dropping if it backs up.

        Dropping is deliberate: a stalled browser must not grow an unbounded queue
        on a 4GB Pi.
        """
        with contextlib.suppress(asyncio.QueueFull):
            self._audio_out.put_nowait(pcm_s16le)

    async def push_audio_out(self, pcm_s16le: bytes, *, timeout_s: float = 2.0) -> bool:
        """Queue speech for the speaker channel, waiting for room instead of dropping.

        `emit_audio_out` drops when the queue is full, which is right for a
        test tone and wrong for speech: called in a loop it never yields, so
        the 32-chunk queue filled in one pass and the rest was discarded. Every
        reply longer than 1.49 s -- exactly 32 x 2048 bytes -- was cut off there,
        and the say route still answered 200 with the full duration.

        Awaiting yields to the speaker socket, which drains as fast as the
        network allows. The timeout stops a browser that vanished mid-sentence
        from wedging the caller forever: False means stop pushing.
        """
        try:
            await asyncio.wait_for(self._audio_out.put(pcm_s16le), timeout_s)
        except asyncio.TimeoutError:
            return False
        return True

    def clear_audio_out(self) -> int:
        """Discard queued speaker audio; returns how many chunks were dropped.

        For when the last speaker disconnects. Anything still queued was meant
        for that listener, and would otherwise play as a fragment of an old
        sentence to whoever connects next.
        """
        dropped = 0
        while True:
            try:
                self._audio_out.get_nowait()
            except asyncio.QueueEmpty:
                return dropped
            dropped += 1

    async def audio_out_stream(self) -> AsyncIterator[bytes]:
        while True:
            yield await self._audio_out.get()
