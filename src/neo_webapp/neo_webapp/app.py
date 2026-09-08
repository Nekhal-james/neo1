"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import api_router
from .auth import SESSION_COOKIE, Authenticator
from .bridge import Bridge, make_bridge
from .config import Config
from .media import MediaManager, media_router
from .perception_link import PerceptionLink

log = logging.getLogger(__name__)
UI_DIR = Path(__file__).parent / "ui"


def create_app(config: Config | None = None, bridge: Bridge | None = None) -> FastAPI:
    cfg = config or Config.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.bridge.start()
        log.info("admin panel up: bridge=%s", app.state.bridge.name)
        try:
            yield
        finally:
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
