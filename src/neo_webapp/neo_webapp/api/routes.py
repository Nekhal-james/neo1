"""Always-on light API: auth, system, sources, control.

Everything here is cheap enough to keep resident in the field profile. Anything
expensive belongs in the on-demand media bridge instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status
from fastapi.responses import JSONResponse

from ..auth import SESSION_COOKIE, require_session, require_ws_session
from ..bridge import BACKENDS, STREAMS
from ..bridge.mock import MockBridge
from ..dialog_status import read_dialog_status
from ..link_status import read_link_status
from ..model_status import read_model_status

log = logging.getLogger(__name__)

router = APIRouter()
STATE_HZ = 4.0

MAX_PROMPT_CHARS = 2000
"""A receptionist question, not an essay. Bounds how long a worker thread can
be tied up by one request."""

IDENTIFY_TIMEOUT_S = 6.0
"""Generous on purpose: the object model is loaded lazily, so the very first
question also pays for reading weights off an SD card."""


# -- auth ------------------------------------------------------------------


@router.post("/api/auth/login")
async def login(request: Request) -> JSONResponse:
    payload = await request.json()
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    auth = request.app.state.auth
    client_ip = request.client.host if request.client else "unknown"

    if not auth.cfg.configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no admin password set - run neo --webapp setup",
        )

    remaining = auth.throttled(client_ip)
    if remaining > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"too many attempts, retry in {int(remaining)}s",
        )

    if not auth.verify(username, password, client_ip):
        log.warning("failed login from %s", client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        )

    token = auth.issue(username)
    resp = JSONResponse({"ok": True, "user": username})
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=auth.cfg.session_max_age_s,
        httponly=True,
        samesite="lax",
        secure=request.app.state.config.server.tls.enabled,
        path="/",
    )
    return resp


@router.post("/api/auth/logout")
async def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/api/auth/me")
async def me(user: str = Depends(require_session)) -> dict:
    return {"user": user}


# -- config (the subset the operator UI needs to render itself correctly) --


@router.get("/api/config")
async def get_config(request: Request, user: str = Depends(require_session)) -> dict:
    """Serves config.media so the UI's joystick rate, camera fps, deadman
    display, and JPEG quality track config/webapp.yaml instead of a hardcoded
    copy that silently drifts from what the server actually enforces."""
    media = request.app.state.config.media
    return {
        "joy_deadman_ms": media.joy_deadman_ms,
        "joy_rate_hz": media.joy_rate_hz,
        "mic_sample_rate": media.mic_sample_rate,
        "speaker_sample_rate": media.speaker_sample_rate,
        "camera_max_fps": media.camera_max_fps,
        "camera_jpeg_quality": media.camera_jpeg_quality,
    }


# -- state -----------------------------------------------------------------


@router.get("/api/state")
async def get_state(request: Request, user: str = Depends(require_session)) -> dict:
    return request.app.state.bridge.snapshot().to_dict()


@router.websocket("/ws/state")
async def ws_state(websocket: WebSocket) -> None:
    """Full snapshot at a low fixed rate.

    A single operator at 4 Hz costs a few KB/s; the simplicity of resending the
    whole state beats maintaining a patch protocol for a payload this small.
    """
    if await require_ws_session(websocket) is None:
        return
    await websocket.accept()
    bridge = websocket.app.state.bridge
    try:
        while True:
            await websocket.send_text(json.dumps(bridge.snapshot().to_dict()))
            await asyncio.sleep(1.0 / STATE_HZ)
    except Exception:  # client vanished; nothing to clean up
        return


# -- sources ---------------------------------------------------------------


@router.get("/api/sources")
async def get_sources(request: Request, user: str = Depends(require_session)) -> dict:
    snap = request.app.state.bridge.snapshot()
    return {
        "sources": snap.to_dict()["sources"],
        "streams": list(STREAMS),
        "backends": list(BACKENDS),
    }


@router.post("/api/sources/set")
async def set_source(request: Request, user: str = Depends(require_session)) -> dict:
    payload = await request.json()
    stream = payload.get("stream")
    backend = payload.get("backend")
    if stream not in STREAMS:
        raise HTTPException(400, f"stream must be one of {list(STREAMS)}")
    if backend not in BACKENDS:
        raise HTTPException(400, f"backend must be one of {list(BACKENDS)}")

    snap = request.app.state.bridge.snapshot()
    if snap.sources.transitioning is not None:
        raise HTTPException(409, f"'{snap.sources.transitioning}' is mid-transition")

    result = await request.app.state.bridge.set_source(stream, backend)
    if not result.ok:
        raise HTTPException(500, result.message)
    return result.to_dict()


# -- control ---------------------------------------------------------------


@router.post("/api/head/center")
async def center_head(request: Request, user: str = Depends(require_session)) -> dict:
    result = await request.app.state.bridge.center_head()
    if not result.ok:
        raise HTTPException(409, result.message)
    return result.to_dict()


@router.post("/api/system/estop")
async def estop(request: Request, user: str = Depends(require_session)) -> dict:
    payload = await request.json()
    engaged = bool(payload.get("engaged", True))
    result = await request.app.state.bridge.set_estop(engaged)
    return result.to_dict()


@router.post("/api/perception/release")
async def release_engagement(
    request: Request, user: str = Depends(require_session)
) -> dict:
    """Operator override: drop the engagement lock and go back to scanning."""
    perception = getattr(request.app.state.bridge, "perception", None)
    if perception is None:
        raise HTTPException(501, "no perception attached to this bridge")
    perception.release("panel override")
    return {"ok": True}


@router.post("/api/perception/reset")
async def reset_perception(request: Request, user: str = Depends(require_session)) -> dict:
    """Clear all tracks and identities -- useful after moving the camera."""
    perception = getattr(request.app.state.bridge, "perception", None)
    if perception is None:
        raise HTTPException(501, "no perception attached to this bridge")
    perception.reset()
    return {"ok": True}


@router.post("/api/perception/identify")
async def identify_object(request: Request, user: str = Depends(require_session)) -> dict:
    """"What is this?" -- run the object model against the next live frame.

    Waits for the answer rather than returning immediately: the caller asked a
    question, and a 202 plus a polling loop would push that wait into every
    client for no benefit. The wait is bounded, and a timeout is reported as a
    timeout rather than as an empty (and therefore wrong) answer.
    """
    perception = getattr(request.app.state.bridge, "perception", None)
    if perception is None:
        raise HTTPException(501, "no perception attached to this bridge")

    view = request.app.state.bridge.snapshot().perception
    if not view.available:
        raise HTTPException(503, "perception unavailable")

    target_seq = perception.request_identify()
    deadline = time.monotonic() + IDENTIFY_TIMEOUT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        current = perception.view(running=True).identify
        if current.seq >= target_seq:
            if current.error:
                raise HTTPException(500, f"identification failed: {current.error}")
            return {
                "ok": True,
                "best": current.best,
                "guesses": [asdict(g) for g in current.guesses],
            }

    # Almost always means no frames are arriving -- the camera is off, or the
    # source is set to hardware with nothing behind it. Say that, rather than
    # reporting "nothing recognised", which would be a different problem.
    raise HTTPException(
        504,
        "no frame was identified in time -- is the camera running and is the "
        "camera source set to webapp?",
    )


@router.post("/api/media/test-tone")
async def test_tone(request: Request, user: str = Depends(require_session)) -> dict:
    """Prove the speaker path end to end without waiting for Phase 5's TTS."""
    bridge = request.app.state.bridge
    if not isinstance(bridge, MockBridge):
        raise HTTPException(501, "test tone is a mock-bridge affordance")
    result = await bridge.emit_test_tone()
    return result.to_dict()


# -- link (model-conn's host/receiver connection) --------------------------


@router.get("/api/link/status")
async def link_status(user: str = Depends(require_session)) -> dict:
    """Reads model-conn's local status file -- see ../link_status.py.

    Deliberately not folded into /api/state or /ws/state: this does file I/O,
    and that doesn't belong in the 4 Hz websocket snapshot loop.
    """
    link, ping = read_link_status()
    return {"link": asdict(link), "ping": asdict(ping) if ping else None}


# -- dialog (intelligence's chat/asr/tts state) -----------------------------


@router.get("/api/dialog/status")
async def dialog_status(user: str = Depends(require_session)) -> dict:
    """Reads intelligence's local status file -- see ../dialog_status.py.

    Same reasoning as /api/link/status: file I/O, so its own route rather
    than folded into /api/state or /ws/state.
    """
    return asdict(read_dialog_status())


@router.get("/api/model/status")
async def model_status(user: str = Depends(require_session)) -> dict:
    """What `neo --model ... up` is serving right now -- see ../model_status.py."""
    return asdict(read_model_status())


@router.post("/api/dialog/ask")
async def dialog_ask(request: Request, user: str = Depends(require_session)) -> dict:
    """Ask the model from the panel, over the same path `neo --prompt` uses.

    This is what makes the panel a client of `neo --model ... up` rather than a
    read-only observer of whatever the CLI last did. Same code path deliberately:
    `intelligence.chat.ask` owns endpoint resolution, mTLS, the degraded reply,
    and writing the dialog status file, so the panel gets all of that for free
    and cannot drift from the CLI's behaviour.
    """
    payload = await request.json()
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "text must not be empty")
    if len(text) > MAX_PROMPT_CHARS:
        raise HTTPException(413, f"text must be under {MAX_PROMPT_CHARS} characters")

    try:
        from intelligence.chat import ask
        from intelligence.config import Config as IntelligenceConfig
        from model_conn.config import Config as ModelConnConfig
    except ImportError as exc:
        raise HTTPException(501, f"intelligence is not installed: {exc}") from exc

    def run() -> dict:
        # ask() does blocking HTTP with `requests`, so it goes to a worker
        # thread -- on the event loop it would stall every other client for the
        # length of the model's reply.
        result = ask(
            text, cfg=IntelligenceConfig.load(), mc_cfg=ModelConnConfig.load()
        )
        return {
            "reply": result.reply,
            "source": result.source,
            "host": result.host,
            "latency_ms": result.latency_ms,
        }

    try:
        return await asyncio.to_thread(run)
    except Exception as exc:  # noqa: BLE001 -- report, never 500 the panel
        log.exception("chat request failed")
        raise HTTPException(502, f"chat failed: {exc}") from exc


# -- health (unauthenticated, deliberately says nothing sensitive) ---------


@router.get("/api/health")
async def health(request: Request) -> dict:
    return {
        "ok": True,
        "backend": request.app.state.bridge.name,
        "configured": request.app.state.auth.cfg.configured,
    }
