"""mTLS-terminating reverse proxy in front of Ollama.

Ollama has no native TLS or client-certificate support, so this fronts it:
`up()` binds Ollama itself to 127.0.0.1:{host.internal_ollama_port} (plaintext,
loopback-only -- never reachable from the network), and this proxy listens on
the *public* bind_host:ollama_port with mutual TLS (uvicorn's ssl_* kwargs,
requiring and verifying a client certificate signed by our private CA -- see
tls.server_ssl_kwargs), forwarding every authenticated request through.

fastapi/uvicorn are already hard dependencies via neo_webapp (see root
setup.py), so this adds no new dependency -- imported at module level
(not lazily) because FastAPI resolves this module's `from __future__ import
annotations`-stringified type hints against *module* globals; a `Request`
imported only inside a function is invisible to that resolution and FastAPI
silently treats the parameter as a query field instead of the injected
request object.
"""

from __future__ import annotations

import logging
import threading

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import tls
from .config import Config

log = logging.getLogger("model_conn.tls_proxy")

_DROP_HEADERS = {"host", "content-length"}


def build_app(cfg: Config) -> FastAPI:
    app = FastAPI()
    target = f"http://127.0.0.1:{cfg.host.internal_ollama_port}"

    @app.api_route(
        "/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]
    )
    async def proxy(path: str, request: Request) -> StreamingResponse:
        body = await request.body()
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in _DROP_HEADERS
        }

        def _forward():
            return requests.request(
                request.method,
                f"{target}/{path}",
                params=request.query_params,
                headers=headers,
                data=body,
                stream=True,
                timeout=None,  # a chat completion can legitimately run long
            )

        upstream = await run_in_threadpool(_forward)
        return StreamingResponse(
            upstream.iter_content(chunk_size=8192),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    return app


def start_proxy_thread(cfg: Config) -> threading.Thread:
    """Starts the proxy in a daemon thread and returns immediately -- `up()`
    keeps running in the foreground (tailing Ollama's stdout / idling) while
    this serves requests until the process exits."""
    ssl_kwargs = tls.server_ssl_kwargs(cfg)
    app = build_app(cfg)
    server_config = uvicorn.Config(
        app,
        host=cfg.host.bind_host,
        port=cfg.host.ollama_port,
        log_level="warning",
        **ssl_kwargs,
    )
    server = uvicorn.Server(server_config)

    thread = threading.Thread(target=server.run, daemon=True, name="model-conn-tls-proxy")
    thread.start()
    log.info(
        "mTLS proxy listening on %s:%d -> 127.0.0.1:%d",
        cfg.host.bind_host,
        cfg.host.ollama_port,
        cfg.host.internal_ollama_port,
    )
    return thread
