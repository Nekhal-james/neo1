"""Run the admin panel: `python -m neo_webapp`."""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .app import create_app
from .config import Config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neo-webapp", description="Neo admin panel")
    parser.add_argument("--config", help="path to a webapp YAML config")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--backend",
        choices=("auto", "mock", "ros"),
        help="robot bridge backend (default: from config, else auto)",
    )
    parser.add_argument(
        "--no-tls", action="store_true", help="serve plain HTTP (localhost only)"
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("neo_webapp")

    cfg = Config.load(args.config)
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port
    if args.backend:
        cfg.bridge_backend = args.backend
    if args.no_tls:
        cfg.server.tls.enabled = False

    if not cfg.auth.configured:
        log.error("no admin password configured. Run: neo-webapp-setup")
        return 2

    ssl_kwargs: dict = {}
    if cfg.server.tls.enabled:
        certfile, keyfile = cfg.server.tls.resolved()
        if not certfile.exists() or not keyfile.exists():
            log.error(
                "TLS enabled but certificate missing (%s). Run: neo-webapp-devcert",
                certfile,
            )
            return 2
        ssl_kwargs = {"ssl_certfile": str(certfile), "ssl_keyfile": str(keyfile)}
        scheme = "https"
    else:
        scheme = "http"
        log.warning(
            "running without TLS: browsers will refuse camera and microphone "
            "access from anywhere except localhost, so the webapp sources will "
            "not work from another device"
        )

    shown_host = "localhost" if cfg.server.host in ("0.0.0.0", "::") else cfg.server.host
    log.info("panel at %s://%s:%d", scheme, shown_host, cfg.server.port)

    uvicorn.run(
        create_app(cfg),
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=args.log_level,
        **ssl_kwargs,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
