"""Always-on light API: auth, system, sources, control.

Everything here is cheap enough to keep resident in the field profile. Anything
expensive belongs in the on-demand media bridge instead.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status
from fastapi.responses import JSONResponse

from ..auth import SESSION_COOKIE, require_session, require_ws_session
from ..bridge import BACKENDS, STREAMS
from ..bridge.mock import MockBridge

log = logging.getLogger(__name__)

router = APIRouter()
STATE_HZ = 4.0


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
            detail="no admin password set - run neo-webapp-setup",
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


@router.post("/api/media/test-tone")
async def test_tone(request: Request, user: str = Depends(require_session)) -> dict:
    """Prove the speaker path end to end without waiting for Phase 5's TTS."""
    bridge = request.app.state.bridge
    if not isinstance(bridge, MockBridge):
        raise HTTPException(501, "test tone is a mock-bridge affordance")
    result = await bridge.emit_test_tone()
    return result.to_dict()


# -- health (unauthenticated, deliberately says nothing sensitive) ---------


@router.get("/api/health")
async def health(request: Request) -> dict:
    return {
        "ok": True,
        "backend": request.app.state.bridge.name,
        "configured": request.app.state.auth.cfg.configured,
    }
