"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import api_router
from .auth import SESSION_COOKIE, Authenticator
from .bridge import Bridge, make_bridge
from .config import Config
from .link_status import read_link_status
from .media import MediaManager, media_router
from .perception_link import PerceptionLink
from .speech import SpeechLink
from .voice import VoiceLoop

try:
    from neo_perception.status_store import write_status as write_vision_status
except ImportError:  # pragma: no cover - perception is an optional extra
    write_vision_status = None

log = logging.getLogger(__name__)
UI_DIR = Path(__file__).parent / "ui"


def create_app(config: Config | None = None, bridge: Bridge | None = None) -> FastAPI:
    cfg = config or Config.load()

    def refresh_link(app: FastAPI) -> None:
        """Keep RobotState.link in step with what /api/link/status reports.

        Without this the field ships in every 4 Hz state snapshot permanently
        reading "down, 0 failures" while the route beside it has the real
        numbers -- two sources of truth for one fact, one of them always wrong.

        Refreshed here, at 1 Hz, rather than in `snapshot()`: reading it is file
        I/O, which is exactly what the state broadcast loop must not do. The ROS
        backend will fill the same field from `/link/health` instead.
        """
        link, _ping = read_link_status()
        app.state.bridge.snapshot().link = link

    def refresh_audio(app: FastAPI) -> None:
        """Keep RobotState.audio current.

        Availability stats the Vosk/Piper model paths, so it lives here on the
        1 Hz task rather than in `snapshot()` -- same rule as the link status
        above. The live parts (partial text, last transcript) are in memory and
        are read straight out of SpeechLink at 4 Hz.
        """
        speech: SpeechLink = app.state.speech
        speech.refresh_availability()
        app.state.bridge.snapshot().audio = speech.view(
            listening=app.state.media.active("mic")
        )
        # The loop publishes on every transition, but an e-stop release resets
        # the badge underneath it; re-publishing here puts it back within a
        # second rather than at the next turn.
        app.state.voice.publish()

    async def publish_vision_status(app: FastAPI) -> None:
        """Mirror what the camera sees into var/perception/status.json, and keep
        the file-backed parts of RobotState current.

        Its own slow task rather than part of the 4 Hz state broadcast: this is
        file I/O, and the readers (`neo --prompt`, `neo --vision:status`) are
        answering human-paced questions, not driving a control loop.
        """
        while True:
            try:
                refresh_link(app)
            except Exception:
                log.exception("link status refresh failed")
            try:
                refresh_audio(app)
            except Exception:
                log.exception("audio status refresh failed")
            if write_vision_status is None:
                await asyncio.sleep(cfg.perception.status_interval_s)
                continue
            try:
                view = app.state.bridge.snapshot().perception
                write_vision_status(
                    available=view.available,
                    running=view.running,
                    detector=view.detector,
                    state=view.state,
                    engaged=view.engaged,
                    target_id=view.target_id,
                    target_facing=view.target_facing,
                    person_count=view.person_count,
                    last_gesture=view.last_gesture,
                    release_reason=view.release_reason,
                    engage_gesture=view.engage_gesture,
                    identify_seq=view.identify.seq,
                    identify_best=view.identify.best,
                    identify_error=view.identify.error,
                    identify_guesses=[
                        {
                            "label": g.label,
                            "confidence": g.confidence,
                            "prominence": g.prominence,
                        }
                        for g in view.identify.guesses
                    ],
                    palm=asdict(view.palm) if view.palm is not None else None,
                )
            except Exception:
                log.exception("vision status write failed")
            await asyncio.sleep(cfg.perception.status_interval_s)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.bridge.start()
        log.info("admin panel up: bridge=%s", app.state.bridge.name)
        publisher = asyncio.create_task(publish_vision_status(app), name="vision-status")
        try:
            yield
        finally:
            publisher.cancel()
            with suppress(asyncio.CancelledError):
                await publisher
            await app.state.voice.close()
            await app.state.bridge.stop()

    app = FastAPI(title="Neo admin panel", version="0.1.0", lifespan=lifespan)
    app.state.config = cfg
    app.state.auth = Authenticator(cfg.auth)
    if bridge is None:
        # Perception is attached to the simulated robot so the gesture-engagement
        # stack can be exercised against a real camera with no hardware. On the
        # real robot it runs as its own ROS node, not inside the panel.
        link = PerceptionLink(target_fps=cfg.perception.target_fps) if cfg.perception.enabled else None
        bridge = make_bridge(
            cfg.bridge_backend,
            deadman_ms=cfg.media.joy_deadman_ms,
            perception=link,
        )
    app.state.bridge = bridge
    app.state.media = MediaManager(app.state.bridge)
    app.state.speech = SpeechLink(
        mic_rate=cfg.media.mic_sample_rate,
        speaker_rate=cfg.media.speaker_sample_rate,
    )
    app.state.voice = VoiceLoop(app.state)

    app.include_router(api_router)
    app.include_router(media_router)

    @app.get("/")
    async def index(request: Request):
        # The panel itself is behind the session; the login page is not.
        user = request.app.state.auth.validate(request.cookies.get(SESSION_COOKIE))
        if user is None:
            return RedirectResponse("/login", status_code=302)
        return FileResponse(UI_DIR / "index.html")

    @app.get("/login")
    async def login_page():
        return FileResponse(UI_DIR / "login.html")

    app.mount("/static", StaticFiles(directory=UI_DIR), name="static")
    return app
