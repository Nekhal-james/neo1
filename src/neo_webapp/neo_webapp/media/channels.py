"""The four media WebSocket bridges.

Panel -> robot: mic, camera, joy.   Robot -> panel: speaker.

Every one of them authenticates before the handshake is accepted, and every one
decrements its channel refcount in a `finally`, so a dropped browser tab always
releases the resource.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..auth import require_ws_session
from ..bridge import Bridge
from .manager import MediaManager

log = logging.getLogger(__name__)
router = APIRouter()

NEUTRAL_AXES = [0.0, 0.0]


def _ctx(ws: WebSocket) -> tuple[Bridge, MediaManager]:
    return ws.app.state.bridge, ws.app.state.media


@router.websocket("/ws/joy")
async def ws_joy(websocket: WebSocket) -> None:
    """Virtual joystick.

    Network-mediated control needs a deadman (plan Phase 3): if the browser stops
    sending -- tab closed, Wi-Fi dropped, laptop asleep mid-drag -- the head must
    stop rather than keep driving. Two guards, because this one moves hardware:

      1. a watchdog that publishes neutral once the stream goes stale, and
      2. an unconditional neutral publish on disconnect.

    `head_behavior` enforces the same rule independently on the robot side; this
    layer just means the stop happens without waiting for a round trip.
    """
    if await require_ws_session(websocket) is None:
        return
    await websocket.accept()
    bridge, media = _ctx(websocket)
    await media.open("joy")

    cfg = websocket.app.state.config.media
    deadman_s = cfg.joy_deadman_ms / 1000.0
    last_rx = time.monotonic()
    neutralized = False

    async def watchdog() -> None:
        nonlocal neutralized
        period = 1.0 / max(cfg.joy_rate_hz, 1)
        while True:
            await asyncio.sleep(period)
            if (time.monotonic() - last_rx) > deadman_s and not neutralized:
                log.warning("joy deadman fired after %.0f ms", deadman_s * 1000)
                await bridge.publish_joy(list(NEUTRAL_AXES), [])
                neutralized = True

    watch = asyncio.create_task(watchdog(), name="joy-deadman")
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            axes = [float(v) for v in msg.get("axes", NEUTRAL_AXES)]
            buttons = [int(v) for v in msg.get("buttons", [])]
            last_rx = time.monotonic()
            neutralized = False
            await bridge.publish_joy(axes, buttons)
    except WebSocketDisconnect:
        pass
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("joy channel: malformed message (%s)", exc)
    finally:
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch
        # Unconditional: never leave the head driving on a dropped connection.
        await bridge.publish_joy(list(NEUTRAL_AXES), [])
        await media.close("joy")


@router.websocket("/ws/camera")
async def ws_camera(websocket: WebSocket) -> None:
    """Browser camera -> /camera/webapp/image_raw, as JPEG frames.

    Server-side rate limiting as well as client-side: a well-meaning browser on a
    fast machine should not be able to push 60 fps at a 4-core Pi.
    """
    if await require_ws_session(websocket) is None:
        return
    await websocket.accept()
    bridge, media = _ctx(websocket)
    await media.open("camera")

    min_interval = 1.0 / max(websocket.app.state.config.media.camera_max_fps, 1)
    last_accepted = 0.0
    try:
        while True:
            frame = await websocket.receive_bytes()
            now = time.monotonic()
            if (now - last_accepted) < min_interval:
                continue  # drop, don't queue
            last_accepted = now
            await bridge.publish_camera_frame(frame)
    except WebSocketDisconnect:
        pass
    finally:
        await media.close("camera")


@router.websocket("/ws/mic")
async def ws_mic(websocket: WebSocket) -> None:
    """Browser microphone -> /audio/webapp/in, as 16 kHz mono s16le PCM.

    The same chunks also go to the recognizer, so the panel transcribes what it
    hears rather than only metering it. That is deliberately not a separate
    "start listening" call: the mic channel being open *is* the listening
    window here. The wake word will own that gate on the real robot (CLAUDE.md);
    until it exists, the operator opening the channel is the gate, which keeps
    the panel honest about never recognizing audio nobody asked it to.

    Recognition failures never close the channel. A missing Vosk model must not
    take the microphone down with it -- the mic is also feeding the meter, the
    bridge, and eventually the wake word.
    """
    if await require_ws_session(websocket) is None:
        return
    await websocket.accept()
    bridge, media = _ctx(websocket)
    speech = websocket.app.state.speech
    await media.open("mic")
    try:
        while True:
            chunk = await websocket.receive_bytes()
            await bridge.publish_mic_chunk(chunk)
            # Guarded here as well as inside SpeechLink. The claim above -- that
            # recognition never takes the microphone down -- has to hold for
            # whatever is plugged in as the recognizer, not only for the one
            # implementation that happens to catch its own errors.
            try:
                await speech.feed(chunk)
            except Exception:  # noqa: BLE001
                log.exception("speech-to-text failed on a mic chunk")
    except WebSocketDisconnect:
        pass
    finally:
        # Flush before closing: without this the trailing audio -- often the
        # last word -- is dropped whenever someone stops the mic instead of
        # pausing long enough for the recognizer to endpoint on its own.
        with contextlib.suppress(Exception):
            await speech.flush()
        await media.close("mic")


@router.websocket("/ws/speaker")
async def ws_speaker(websocket: WebSocket) -> None:
    """/audio/webapp/out -> browser playback."""
    if await require_ws_session(websocket) is None:
        return
    await websocket.accept()
    bridge, media = _ctx(websocket)
    await media.open("speaker")
    try:
        async for chunk in bridge.audio_out_stream():
            await websocket.send_bytes(chunk)
    except WebSocketDisconnect:
        pass
    finally:
        await media.close("speaker")
