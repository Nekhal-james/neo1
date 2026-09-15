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
from fastapi.responses import JSONResponse, Response

from ..auth import (
    MIN_PASSWORD_CHARS,
    SESSION_COOKIE,
    hash_password,
    hash_recovery_code,
    new_recovery_code,
    require_session,
    require_ws_session,
)
from ..config import new_session_secret, write_local
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


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check_not_throttled(auth, client_ip: str) -> None:
    remaining = auth.throttled(client_ip)
    if remaining > 0:
        raise HTTPException(429, f"too many attempts, retry in {int(remaining)}s")


def _check_new_password(new: str) -> None:
    if len(new) < MIN_PASSWORD_CHARS:
        raise HTTPException(400, f"the new password must be at least {MIN_PASSWORD_CHARS} characters")
    if len(new) > 1024:
        raise HTTPException(400, "the new password is too long")


def _secrets_path(request: Request):
    path = request.app.state.config.secrets_path
    if path is None:
        raise HTTPException(503, "this panel was started without a config file to save to")
    return path


def _with_session(request: Request, body: dict, token: str) -> JSONResponse:
    resp = JSONResponse(body)
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=request.app.state.auth.cfg.session_max_age_s,
        httponly=True,
        samesite="lax",
        secure=request.app.state.config.server.tls.enabled,
        path="/",
    )
    return resp


def _store_credentials(request: Request, new_password: str, *, new_recovery: bool) -> str | None:
    """Save a new password (and optionally a new recovery code), rotating the
    session secret. Returns the new recovery code, if one was made.

    Disk first: if the write fails, nothing has changed anywhere. Then memory,
    which signs out every browser holding a cookie signed with the old secret.
    """
    auth = request.app.state.auth
    updates = {"password_hash": hash_password(new_password), "session_secret": new_session_secret()}
    code = None
    if new_recovery:
        code = new_recovery_code()
        updates["recovery_hash"] = hash_recovery_code(code)
    write_local({"auth": updates}, _secrets_path(request))
    auth.replace_credentials(updates["password_hash"], updates["session_secret"],
                             updates.get("recovery_hash"))
    return code


@router.post("/api/auth/password")
async def change_password(request: Request, user: str = Depends(require_session)) -> JSONResponse:
    """Change the admin password from the panel.

    Needs the current password as well as a session: a session is a cookie on
    whatever browser was left signed in, and it must not be enough to lock the
    owner out. Wrong guesses count toward the login lockout, so this is not a
    second, unthrottled way to brute-force the password.

    Saving rotates the session secret, which signs out every other browser; this
    one is re-issued a cookie and stays signed in.
    """
    payload = await request.json()
    current = str(payload.get("current_password", ""))
    new = str(payload.get("new_password", ""))
    auth = request.app.state.auth
    client_ip = _client_ip(request)

    _check_not_throttled(auth, client_ip)
    _secrets_path(request)
    _check_new_password(new)

    def change() -> str:
        # argon2 is deliberately slow; off the event loop so the head and the
        # state feed do not stall while it runs.
        if not auth.verify(user, current, client_ip):
            log.warning("password change with a wrong current password from %s", client_ip)
            raise HTTPException(403, "the current password is wrong")
        if auth.matches_current(new):
            raise HTTPException(400, "choose a password different from the current one")
        _store_credentials(request, new, new_recovery=False)
        log.info("admin password changed from %s; other sessions signed out", client_ip)
        return auth.issue(user)

    token = await asyncio.to_thread(change)
    return _with_session(request, {"ok": True}, token)


# -- forgot password: one-time recovery codes --------------------------------
#
# The robot has no email or SMS to send a reset link through, so the proof of
# ownership is a code the owner was shown once and kept. A reset form that
# needed less -- just the username, say -- would let anyone who can reach the
# panel on the campus network take over a robot that moves and talks.


@router.get("/api/auth/recovery")
async def recovery_status(request: Request, user: str = Depends(require_session)) -> dict:
    return {"configured": bool(request.app.state.auth.cfg.recovery_hash)}


@router.post("/api/auth/recovery")
async def new_recovery(request: Request, user: str = Depends(require_session)) -> dict:
    """Generate a recovery code, replacing any earlier one, and show it once.

    Needs the current password for the same reason a password change does: a
    code is as good as the password, so a browser left signed in must not be
    able to mint one.
    """
    payload = await request.json()
    current = str(payload.get("current_password", ""))
    auth = request.app.state.auth
    client_ip = _client_ip(request)
    _check_not_throttled(auth, client_ip)
    path = _secrets_path(request)

    def make() -> str:
        if not auth.verify(user, current, client_ip):
            log.warning("recovery code request with a wrong password from %s", client_ip)
            raise HTTPException(403, "the current password is wrong")
        code = new_recovery_code()
        recovery_hash = hash_recovery_code(code)
        write_local({"auth": {"recovery_hash": recovery_hash}}, path)
        auth.cfg.recovery_hash = recovery_hash
        log.info("new recovery code generated from %s; the previous one no longer works", client_ip)
        return code

    return {"ok": True, "recovery_code": await asyncio.to_thread(make)}


@router.post("/api/auth/recover")
async def recover(request: Request) -> JSONResponse:
    """Forgot password: set a new one with the recovery code, and sign in.

    Unauthenticated by necessity, so everything about it is conservative:
    - throttled by the login lockout, and a wrong code counts as a failed login;
    - the code works once -- it is replaced by a fresh one, returned here and
      never again, so the owner is not left without a way back in;
    - the session secret rotates, signing out every other browser, since a
      forgotten password is sometimes a stolen one.
    """
    payload = await request.json()
    code = str(payload.get("recovery_code", ""))
    new = str(payload.get("new_password", ""))
    auth = request.app.state.auth
    client_ip = _client_ip(request)

    _check_not_throttled(auth, client_ip)
    if not auth.cfg.recovery_hash:
        raise HTTPException(
            503, "no recovery code is set for this robot - run neo --webapp setup on the robot"
        )
    _secrets_path(request)
    _check_new_password(new)

    def reset() -> tuple[str, str]:
        if not auth.verify_recovery(code, client_ip):
            log.warning("failed password recovery from %s", client_ip)
            raise HTTPException(401, "that recovery code is not valid")
        fresh = _store_credentials(request, new, new_recovery=True)
        log.warning("admin password reset with the recovery code from %s; "
                    "other sessions signed out, a new recovery code issued", client_ip)
        return auth.issue(auth.cfg.username), fresh

    token, fresh = await asyncio.to_thread(reset)
    return _with_session(request, {"ok": True, "recovery_code": fresh}, token)


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


# -- audio (speech to text, text to speech) --------------------------------

MAX_SAY_CHARS = 600
"""A spoken reply, not a monologue. Also bounds how long one request can hold a
worker thread synthesizing."""

MAX_WAV_BYTES = 10 * 1024 * 1024
"""About ten minutes of 16 kHz mono. Generous for a room question; small enough
that an upload cannot exhaust memory on a 4 GB Pi."""

@router.get("/api/audio/status")
async def audio_status(request: Request, user: str = Depends(require_session)) -> dict:
    """Whether speech works, and if not, exactly why.

    The reason matters more than the flag: "ASR unavailable" is
    indistinguishable from a bug, while "asr.vosk_model_path is not set" is
    something the operator can go and fix.
    """
    speech = request.app.state.speech
    # Availability normally refreshes on the 1 Hz status task; do it here too so
    # a freshly-opened panel does not show "checking..." until the next tick.
    await asyncio.to_thread(speech.refresh_availability)
    view = speech.view(listening=request.app.state.media.active("mic"))
    return asdict(view)


@router.post("/api/audio/say")
async def audio_say(request: Request, user: str = Depends(require_session)) -> dict:
    """Speak `text` through the panel's speaker channel.

    This is the whole text-to-speech path end to end: Piper synthesizes a
    sentence at a time, each is resampled to the speaker channel's rate and
    pushed as soon as it is ready, and the mic stops feeding the recognizer
    until it has finished playing. Nothing here is a mock, and the same
    `intelligence.tts` code runs on the robot.
    """
    payload = await request.json()
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "text must not be empty")
    if len(text) > MAX_SAY_CHARS:
        raise HTTPException(413, f"text must be under {MAX_SAY_CHARS} characters")

    try:
        result = await request.app.state.voice.speak(text)
    except RuntimeError as exc:
        # A missing model or package is a configuration problem, not a crash:
        # 501 with the reason, which is what the panel renders.
        raise HTTPException(501, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 -- report, never 500 the panel
        log.exception("speech synthesis failed")
        raise HTTPException(502, f"speech synthesis failed: {exc}") from exc

    return {
        "ok": True,
        "text": text,
        "sample_rate": result.sample_rate,
        "duration_s": round(result.duration_s, 3),
        "bytes": result.bytes,
        "sentences": result.sentences,
        "first_audio_ms": round(result.first_audio_ms, 1),
        # False when nobody has the speaker channel open -- the audio was
        # synthesized but not queued. Without this the panel would report
        # success for something the operator never heard.
        "speaker_connected": result.speaker_connected,
    }


@router.post("/api/audio/transcribe")
async def audio_transcribe(request: Request, user: str = Depends(require_session)) -> dict:
    """Transcribe an uploaded WAV.

    The path that makes speech-to-text testable with no microphone at all --
    and, later, the one that replays a recorded corridor clip against a tuned
    model. Body is the raw WAV; mono 16-bit only, because anything else is
    silently mis-decoded into plausible nonsense rather than failing.
    """
    wav_bytes = await request.body()
    if not wav_bytes:
        raise HTTPException(400, "request body must be a WAV file")
    if len(wav_bytes) > MAX_WAV_BYTES:
        raise HTTPException(413, f"WAV must be under {MAX_WAV_BYTES // (1024 * 1024)} MB")

    speech = request.app.state.speech
    try:
        view = await speech.transcribe_wav(wav_bytes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(501, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("transcription failed")
        raise HTTPException(502, f"transcription failed: {exc}") from exc

    return asdict(view)


@router.post("/api/audio/reset")
async def audio_reset(request: Request, user: str = Depends(require_session)) -> dict:
    """Drop the current utterance and clear the last transcript.

    Keeps the loaded model: this ends an utterance, it does not reconfigure
    anything.
    """
    request.app.state.speech.reset()
    return {"ok": True}


@router.post("/api/audio/reload")
async def audio_reload(request: Request, user: str = Depends(require_session)) -> dict:
    """Re-read config/intelligence.yaml and drop cached models.

    So downloading a Vosk model or a Piper voice and pointing the config at it
    does not need the panel restarted -- which on the robot means an ssh session
    and a systemctl call, for a change made in a text file.
    """
    speech = request.app.state.speech
    speech.reload_config()
    await asyncio.to_thread(speech.refresh_availability)
    return asdict(speech.view(listening=request.app.state.media.active("mic")))


# -- voice (the spoken conversation loop) -----------------------------------


@router.get("/api/voice")
async def voice_status(request: Request, user: str = Depends(require_session)) -> dict:
    return asdict(request.app.state.voice.view())


@router.post("/api/voice")
async def voice_configure(request: Request, user: str = Depends(require_session)) -> dict:
    """Switch answering out loud on or off.

    Off by default and switched on only here. With it on, whatever the room
    says to an open mic is sent to the model host -- a choice an operator makes,
    not a side effect of opening a microphone.
    """
    payload = await request.json()
    enabled = payload.get("enabled") if isinstance(payload, dict) else None
    if not isinstance(enabled, bool):
        raise HTTPException(400, 'body must be {"enabled": true} or {"enabled": false}')
    voice = request.app.state.voice
    voice.set_enabled(enabled)
    return asdict(voice.view())


@router.post("/api/voice/ask")
async def voice_ask(request: Request, user: str = Depends(require_session)) -> dict:
    """One spoken turn from typed text: ask the model, speak the reply.

    Everything a voice turn does except the microphone -- so the loop can be
    exercised and timed without anybody talking, and a typed question can be
    answered out loud. Does not need answering switched on: typing the question
    is already the explicit act that switch exists to require.

    Returns once the reply has been pushed to the speaker, not once it has
    finished playing.
    """
    payload = await request.json()
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "text must not be empty")
    if len(text) > MAX_PROMPT_CHARS:
        raise HTTPException(413, f"text must be under {MAX_PROMPT_CHARS} characters")

    voice = request.app.state.voice
    if voice.busy:
        raise HTTPException(409, "Neo is already answering; ask again when it has finished")
    try:
        turn = await voice.run_turn(text)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return asdict(turn)


# -- the robot's own conversation, camera and gestures -----------------------
#
# These drive the robot's ROS graph, not the panel's in-process voice loop.
# Each route checks the bridge's declared capabilities first, so on the
# simulated robot the answer is a clear 501 rather than an AttributeError.


def _capable(request: Request, capability: str):
    bridge = request.app.state.bridge
    if capability not in bridge.snapshot().capabilities:
        raise HTTPException(
            501,
            f"the {bridge.name} bridge cannot do that -- it needs the panel "
            f"connected to the robot's ROS graph (scripts/pi/neo-panel.sh)",
        )
    return bridge


async def _text_from(request: Request) -> str:
    payload = await request.json()
    text = str(payload.get("text", "")).strip() if isinstance(payload, dict) else ""
    if not text:
        raise HTTPException(400, "text must not be empty")
    if len(text) > MAX_PROMPT_CHARS:
        raise HTTPException(413, f"text must be under {MAX_PROMPT_CHARS} characters")
    return text


def _confirmed(result) -> dict:
    if not result.ok:
        # 409: the request was understood and refused by the robot's current
        # state -- e-stop engaged, a turn already in flight -- not malformed.
        raise HTTPException(409, result.message)
    return result.to_dict()


@router.post("/api/robot/wake")
async def robot_wake(request: Request, user: str = Depends(require_session)) -> dict:
    """Open the robot's listening window, as if it had heard its name.

    A push-to-talk for a noisy room or a wake word still being tuned. The event
    it publishes carries a threshold of 0, so a turn started this way can never
    be mistaken for the detector having accepted something.
    """
    return _confirmed(await _capable(request, "robot_voice").robot_wake())


@router.post("/api/robot/ask")
async def robot_ask(request: Request, user: str = Depends(require_session)) -> dict:
    """A typed question into the robot's conversation, answered through its speaker.

    Enters exactly where a spoken question does after the wake word, so the
    answer takes the robot's real path: the "what is this" check, the campus
    directory, the model host or its degraded reply, and Piper.
    """
    bridge = _capable(request, "robot_voice")
    return _confirmed(await bridge.robot_ask(await _text_from(request)))


@router.post("/api/robot/say")
async def robot_say(request: Request, user: str = Depends(require_session)) -> dict:
    """Speak `text` through the robot's current speaker. No model involved."""
    bridge = _capable(request, "robot_voice")
    return _confirmed(await bridge.robot_say(await _text_from(request)))


@router.post("/api/robot/cancel")
async def robot_cancel(request: Request, user: str = Depends(require_session)) -> dict:
    """Stop the robot mid-sentence."""
    return _confirmed(await _capable(request, "robot_voice").robot_cancel())


@router.post("/api/emotion/gesture")
async def emotion_gesture(request: Request, user: str = Depends(require_session)) -> dict:
    """Play a head gesture: nod, shake, tilt, or scan.

    At gesture priority -- above following a person, below the joystick -- and
    refused under e-stop. The simulated robot plays it on its simulated head.
    """
    payload = await request.json()
    kind = str(payload.get("kind", "")).strip() if isinstance(payload, dict) else ""
    if not kind:
        raise HTTPException(400, 'body must be {"kind": "nod" | "shake" | "tilt" | "scan"}')
    return _confirmed(await _capable(request, "gestures").trigger_gesture(kind))


@router.get("/api/camera/snapshot")
async def camera_snapshot(request: Request, user: str = Depends(require_session)) -> Response:
    """The robot camera's latest picture, as a JPEG.

    Polled by the Vision tab rather than streamed: the panel subscribes to the
    camera only while pictures are being asked for, and drops the subscription
    a few seconds after they stop. The first request only arms it, hence 503
    with Retry-After until a frame has arrived.
    """
    bridge = _capable(request, "robot_camera")
    jpeg = await bridge.camera_snapshot()
    if jpeg is None:
        return Response(status_code=503, headers={"Retry-After": "1", "Cache-Control": "no-store"})
    return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


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
