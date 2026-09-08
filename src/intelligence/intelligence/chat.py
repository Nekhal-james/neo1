"""Ask the off-board LLM a question, routed through model_conn's link.

Resolution order:
1. model_conn.link.probe_ordered over mc_cfg.receiver.endpoints (eth then
   wifi) -- the Pi-to-laptop case.
2. Fall back to 127.0.0.1:{mc_cfg.host.ollama_port} -- the same-machine
   testing case `neo --prompt` is meant to cover (dev laptop running both
   `neo --model ... up` and `neo --prompt ...`).

No reachable endpoint is not an error: it returns a degraded-mode reply, per
CLAUDE.md's "degraded mode is the normal operating state, not an error path".
Every call writes the result to status_store so the admin panel can show it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from model_conn.config import Config as ModelConnConfig
from model_conn.config import Endpoint
from model_conn.link import probe_ordered
from model_conn.link import transport_for as probe_transport_for

from .config import Config
from .prompts import load_system_prompt
from .status_store import write_status

log = logging.getLogger("intelligence.chat")

DEGRADED_REPLY = (
    "I can't reach my language model right now, but I can still help you "
    "find rooms and blocks."
)

ChatTransport = Callable[[str, int, str, str, float, str], str]
"""(host, port, model, message, timeout_s, system) -> reply text. Raises on failure."""


def _chat_request(scheme: str, host: str, port: int, model: str, message: str,
                   timeout_s: float, system: str, **request_kwargs) -> str:
    import requests

    resp = requests.post(
        f"{scheme}://{host}:{port}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": message},
            ],
            "stream": False,
        },
        timeout=timeout_s,
        **request_kwargs,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["message"]["content"]


def _http_chat(host: str, port: int, model: str, message: str, timeout_s: float, system: str) -> str:
    return _chat_request("http", host, port, model, message, timeout_s, system)


def chat_transport_for(mc_cfg: ModelConnConfig) -> ChatTransport:
    """Mirrors model_conn.link.transport_for: plain HTTP when tls.enabled is
    False, HTTPS presenting the client cert when it's True."""
    if not mc_cfg.tls.enabled:
        return _http_chat

    from model_conn import tls

    kwargs = tls.client_request_kwargs(mc_cfg)

    def _https_chat(host: str, port: int, model: str, message: str, timeout_s: float, system: str) -> str:
        return _chat_request("https", host, port, model, message, timeout_s, system, **kwargs)

    return _https_chat


@dataclass
class ChatResult:
    reply: str
    source: str  # "ollama" | "degraded"
    host: str = ""
    latency_ms: float = 0.0


def ask(
    text: str,
    *,
    cfg: Config,
    mc_cfg: ModelConnConfig,
    transport: ChatTransport | None = None,
    probe_transport=None,
) -> ChatResult:
    """`probe_transport` overrides model_conn.link's default HTTP probe --
    exposed only so tests can fake endpoint reachability without touching a
    socket; production code never passes it."""
    if not cfg.chat.model_name:
        log.warning("chat.model_name is not configured; the request will likely 404")

    system = load_system_prompt(cfg)
    host, port = _resolve_endpoint(mc_cfg, probe_transport)

    if host is not None:
        start = time.monotonic()
        try:
            call = transport if transport is not None else chat_transport_for(mc_cfg)
            reply = call(host, port, cfg.chat.model_name, text, cfg.chat.timeout_s, system)
            result = ChatResult(
                reply=reply,
                source="ollama",
                host=f"{host}:{port}",
                latency_ms=(time.monotonic() - start) * 1000.0,
            )
            _write(cfg, text, result)
            return result
        except Exception as exc:  # noqa: BLE001 -- any failure here means degraded
            log.warning("chat request to %s:%d failed: %s", host, port, exc)

    result = ChatResult(reply=DEGRADED_REPLY, source="degraded")
    _write(cfg, text, result)
    return result


def _resolve_endpoint(mc_cfg: ModelConnConfig, probe_transport=None) -> tuple[str | None, int]:
    if probe_transport is None:
        from model_conn.tls import TlsError

        try:
            probe_transport = probe_transport_for(mc_cfg)
        except TlsError as exc:
            # tls.enabled but certs missing -- no reachable endpoint is not
            # a crash, it's exactly the degraded-mode case ask() already
            # handles when nothing answers.
            log.warning("mTLS not ready: %s", exc)
            return None, 0

    kwargs = {"timeout_s": mc_cfg.receiver.probe_timeout_s, "transport": probe_transport}

    probe = probe_ordered(mc_cfg.receiver.endpoints, **kwargs)
    if probe is not None and probe.ok:
        return probe.endpoint.host, probe.endpoint.port

    localhost = Endpoint(name="eth", host="127.0.0.1", port=mc_cfg.host.ollama_port)
    local_probe = probe_ordered([localhost], **kwargs)
    if local_probe is not None and local_probe.ok:
        return "127.0.0.1", mc_cfg.host.ollama_port

    return None, 0


def _write(cfg: Config, prompt: str, result: ChatResult) -> None:
    write_status(
        dialog_state="IDLE",
        last_prompt=prompt,
        last_reply=result.reply,
        chat_source=result.source,
        asr_engine=cfg.asr.engine,
        tts_engine=cfg.tts.engine,
        path=cfg.status_path,
    )
