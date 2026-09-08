"""mTLS for the off-board link.

CLAUDE.md and docs/IMPLEMENTATION_PLAN.md section 0.3.1 are explicit: an
unauthenticated off-board inference endpoint on a campus network is an open
proxy, and must never run -- not even briefly for testing. This module is
the real implementation: a private CA (see certs.py) signs a server cert for
the host and a client cert for the receiver; the host's mTLS proxy
(tls_proxy.py) requires and verifies the client cert, and every receiver-side
request presents it.

`tls.enabled: false` remains a supported, explicit opt-out for local/dev
testing (e.g. everything on one laptop loopback) -- `warn_insecure()` fires
loudly every time that path is taken, so it can never be silently insecure.
"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path

from .config import Config

log = logging.getLogger("model_conn.tls")

_BANNER = "!" * 70


class TlsError(RuntimeError):
    """tls.enabled is true but the required cert material doesn't exist yet."""


def warn_insecure(context: str) -> None:
    """Log an impossible-to-miss warning. Call once per process start, not per-request."""
    log.warning(_BANNER)
    log.warning("INSECURE: %s is running WITHOUT mTLS (tls.enabled is false)", context)
    log.warning("Set tls.enabled: true in config/model_conn.yaml for real use.")
    log.warning(_BANNER)
    print(f"\n[model-conn] WARNING: {context} has NO mTLS (tls.enabled is false).\n")


def _missing(paths: list[Path]) -> list[Path]:
    return [p for p in paths if not p.exists()]


def require_client_certs(cfg: Config) -> None:
    missing = _missing([cfg.ca_cert_path, cfg.client_cert_path, cfg.client_key_path])
    if missing:
        raise TlsError(
            "tls.enabled is true but these certs are missing: "
            + ", ".join(str(p) for p in missing)
            + " -- run `neo --tls init` first"
        )


def require_server_certs(cfg: Config) -> None:
    missing = _missing([cfg.ca_cert_path, cfg.server_cert_path, cfg.server_key_path])
    if missing:
        raise TlsError(
            "tls.enabled is true but these certs are missing: "
            + ", ".join(str(p) for p in missing)
            + " -- run `neo --tls init` first"
        )


def scheme(cfg: Config) -> str:
    return "https" if cfg.tls.enabled else "http"


def client_request_kwargs(cfg: Config) -> dict:
    """kwargs to splat into requests.get/post for mTLS client auth.

    Empty dict when tls.enabled is False -- callers just get plain http.
    """
    if not cfg.tls.enabled:
        return {}
    require_client_certs(cfg)
    return {
        "cert": (str(cfg.client_cert_path), str(cfg.client_key_path)),
        "verify": str(cfg.ca_cert_path),
    }


def server_ssl_kwargs(cfg: Config) -> dict:
    """kwargs to splat into uvicorn.Config so the mTLS proxy requires and
    verifies a client certificate signed by our CA."""
    require_server_certs(cfg)
    return {
        "ssl_certfile": str(cfg.server_cert_path),
        "ssl_keyfile": str(cfg.server_key_path),
        "ssl_ca_certs": str(cfg.ca_cert_path),
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }
