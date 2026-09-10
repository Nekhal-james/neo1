"""A speaker whose browser has gone away must release its channel.

`/ws/speaker` only ever sends, so unlike every other channel it never sits in a
receive call that would notice the client leaving. Waiting on an empty audio
queue, it learned of the disconnect only at the next failed send -- which, with
nothing being said, is never. Measured against a real server, two things
followed:

* the channel stayed "connected" to nobody (still reported 5 s after the client
  closed), so the voice loop believed someone was listening, and
* uvicorn's graceful shutdown waited on that handler forever -- "Waiting for
  background tasks to complete", still running 8 s after SIGTERM.

Driven with a fake socket rather than TestClient on purpose: TestClient cancels
the handler when its websocket context exits, which hides exactly this bug. A
version of this test written against TestClient passed on the broken code.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from neo_webapp.bridge.mock import MockBridge
from neo_webapp.media import MediaManager
from neo_webapp.media import channels


class FakeSpeakerSocket:
    """A browser that listens, then leaves without ever sending anything."""

    def __init__(self, state) -> None:
        self.app = SimpleNamespace(state=state)
        self.sent: list[bytes] = []
        self._left = asyncio.Event()

    async def accept(self) -> None:
        pass

    async def receive(self) -> dict:
        await self._left.wait()
        return {"type": "websocket.disconnect", "code": 1001}

    async def send_bytes(self, data: bytes) -> None:
        if self._left.is_set():
            raise RuntimeError("socket is closed")
        self.sent.append(data)

    def leave(self) -> None:
        self._left.set()


async def _signed_in(websocket):
    return "admin"


async def _eventually(predicate, timeout: float = 1.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


@pytest.fixture
def speaker(monkeypatch):
    monkeypatch.setattr(channels, "require_ws_session", _signed_in)
    bridge = MockBridge()
    media = MediaManager(bridge)
    socket = FakeSpeakerSocket(SimpleNamespace(bridge=bridge, media=media))
    return SimpleNamespace(bridge=bridge, media=media, socket=socket)


@pytest.mark.asyncio
async def test_a_speaker_whose_browser_left_releases_its_channel(speaker):
    handler = asyncio.create_task(channels.ws_speaker(speaker.socket))
    assert await _eventually(lambda: speaker.media.active("speaker"))

    speaker.socket.leave()  # nothing is being said, so there is no send to fail
    try:
        await asyncio.wait_for(handler, timeout=1.0)
    except asyncio.TimeoutError:
        handler.cancel()
        pytest.fail("the handler never noticed the browser leave; it would hold "
                    "the channel open and block shutdown forever")
    assert not speaker.media.active("speaker"), "still 'connected' to nobody"


@pytest.mark.asyncio
async def test_audio_still_reaches_a_speaker_that_is_listening(speaker):
    """Watching for the disconnect must not get in the way of delivery."""
    handler = asyncio.create_task(channels.ws_speaker(speaker.socket))
    assert await _eventually(lambda: speaker.media.active("speaker"))

    await speaker.bridge.push_audio_out(b"\x01\x00" * 1024, timeout_s=1.0)
    assert await _eventually(lambda: speaker.socket.sent == [b"\x01\x00" * 1024])

    speaker.socket.leave()
    await asyncio.wait_for(handler, timeout=1.0)


@pytest.mark.asyncio
async def test_what_was_queued_for_a_departed_listener_is_dropped(speaker):
    """Otherwise the next browser to connect hears the tail of an old sentence."""
    handler = asyncio.create_task(channels.ws_speaker(speaker.socket))
    assert await _eventually(lambda: speaker.media.active("speaker"))

    speaker.socket.leave()
    await asyncio.wait_for(handler, timeout=1.0)
    await speaker.bridge.emit_audio_out(b"\x02\x00")  # anything arriving late
    assert speaker.bridge.clear_audio_out() == 1
    assert speaker.bridge._audio_out.qsize() == 0
