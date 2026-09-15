"""Ask the off-board LLM a question, routed through model_conn's link.

Resolution order:
1. model_conn.link.probe_ordered over mc_cfg.receiver.endpoints (eth then
   wifi) -- the Pi-to-laptop case.
2. Fall back to 127.0.0.1:{mc_cfg.host.ollama_port} -- the same-machine
   testing case `neo --prompt` is meant to cover (dev laptop running both
   `neo --model ... up` and `neo --prompt ...`).

No reachable endpoint is not an error: it answers from the campus directory when
the question is one it can answer, and otherwise returns the degraded-mode
reply, per CLAUDE.md's "degraded mode is the normal operating state, not an
error path". Either way the model, when present, sees only the directory entries
the question matched (`retrieval.py`), never the whole dataset.
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
"""(host, port, model, message, timeout_s, system) -> reply text. Raises on failure.

`timeout_s` is the *read* timeout only. How long to wait for the connection is a
separate question with a different answer, and it is bound into the transport by
`chat_transport_for` rather than passed here -- see below.
"""

DEFAULT_CONNECT_TIMEOUT_S = 2.0
"""Fallback when no receiver probe timeout is configured."""


def _chat_request(scheme: str, host: str, port: int, model: str, message: str,
                   timeout_s: float, system: str, *,
                   connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
                   keep_alive: str | None = None,
                   **request_kwargs) -> str:
    import requests

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ],
        "stream": False,
    }
    if keep_alive is not None:
        # Ollama drops a model from memory after five minutes by default, and
        # reloading a 3B model costs ~45 s. Without this, the first question
        # anyone asks after a quiet spell is guaranteed to time out and get the
        # degraded reply -- on a reception desk, quiet spells are the norm, so
        # that is close to *every* first question.
        payload["keep_alive"] = keep_alive

    resp = requests.post(
        f"{scheme}://{host}:{port}/api/chat",
        json=payload,
        # Two timeouts, because one number cannot do both jobs. Connecting is
        # how we detect a host that is gone, and must fail fast so the degraded
        # reply is prompt. Reading is how long generation may take, and must be
        # patient enough to survive a cold model load. A single value forces a
        # choice between a laggy degrade and a spurious one.
        timeout=(connect_timeout_s, timeout_s),
        **request_kwargs,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["message"]["content"]


def _http_chat(host: str, port: int, model: str, message: str, timeout_s: float, system: str) -> str:
    return _chat_request("http", host, port, model, message, timeout_s, system)


def _keep_alive_for(mc_cfg: ModelConnConfig) -> str | None:
    """`host.idle_unload_minutes` as an Ollama keep_alive string.

    The knob existed in the config and in the plan but was never wired to
    anything, so Ollama silently used its own five-minute default instead of the
    thirty configured here.
    """
    minutes = getattr(mc_cfg.host, "idle_unload_minutes", 0)
    return f"{int(minutes)}m" if minutes and minutes > 0 else None


def chat_transport_for(mc_cfg: ModelConnConfig) -> ChatTransport:
    """Mirrors model_conn.link.transport_for: plain HTTP when tls.enabled is
    False, HTTPS presenting the client cert when it's True.

    The connect timeout and keep-alive are bound here rather than threaded
    through every call: both come from `mc_cfg`, and both already mean exactly
    what is needed. `receiver.probe_timeout_s` is by definition "how long to
    wait before calling a host unreachable", which is the connect timeout.
    """
    connect_timeout_s = (
        getattr(mc_cfg.receiver, "probe_timeout_s", 0) or DEFAULT_CONNECT_TIMEOUT_S
    )
    keep_alive = _keep_alive_for(mc_cfg)

    if not mc_cfg.tls.enabled:
        def _plain(host: str, port: int, model: str, message: str,
                   timeout_s: float, system: str) -> str:
            return _chat_request("http", host, port, model, message, timeout_s, system,
                                 connect_timeout_s=connect_timeout_s,
                                 keep_alive=keep_alive)

        return _plain

    from model_conn import tls

    kwargs = tls.client_request_kwargs(mc_cfg)

    def _https_chat(host: str, port: int, model: str, message: str, timeout_s: float, system: str) -> str:
        return _chat_request("https", host, port, model, message, timeout_s, system,
                             connect_timeout_s=connect_timeout_s,
                             keep_alive=keep_alive, **kwargs)

    return _https_chat


@dataclass
class ChatResult:
    reply: str
    source: str  # "ollama" | "kb" | "degraded"
    host: str = ""
    latency_ms: float = 0.0


@dataclass
class CampusLookup:
    context: str
    """The "Campus directory" section for the system prompt."""

    offline_reply: str | None
    """What to say from the directory alone when no model host answers."""


def campus_lookup(text: str, cfg: Config) -> CampusLookup:
    """Look the question up in the campus dataset. Never raises.

    Read from disk on every question (cached until a file changes), so a room
    saved in the admin panel is answerable by the next question.
    """
    try:
        from . import campus, retrieval

        ds = campus.load_dataset(cfg.rag_data_path)
        found = retrieval.retrieve(text, ds)
        return CampusLookup(retrieval.context(ds, found), retrieval.offline_reply(ds, found))
    except Exception:  # noqa: BLE001 -- a broken data file must not cost the answer
        log.warning("campus lookup failed", exc_info=True)
        return CampusLookup("", None)


def vision_context() -> str:
    """One line about what the camera currently sees, or "" if nothing is.

    Read from neo_perception's status file rather than from the camera: the
    camera belongs to whichever process is running perception (today the admin
    panel), and this one may be a short-lived `neo --prompt`. A stale or absent
    file simply means no context, which is the same as having no camera.
    """
    try:
        from neo_perception.status_store import read_status
    except ImportError:
        return ""
    try:
        return read_status().describe()
    except Exception:  # noqa: BLE001 -- context is a nicety, never a failure
        log.debug("vision context unavailable", exc_info=True)
        return ""


def ask(
    text: str,
    *,
    cfg: Config,
    mc_cfg: ModelConnConfig,
    transport: ChatTransport | None = None,
    probe_transport=None,
    vision: str | None = None,
    campus: CampusLookup | None = None,
) -> ChatResult:
    """`probe_transport` overrides model_conn.link's default HTTP probe --
    exposed only so tests can fake endpoint reachability without touching a
    socket; production code never passes it.

    `vision` overrides the camera context line for the same reason, and
    `campus` the directory lookup.
    """
    model_name = cfg.chat.model_name
    if not model_name:
        model_name = _discover_model_name(mc_cfg)
        if model_name:
            log.info("auto-discovered model name from host status: %s", model_name)
        else:
            log.warning("chat.model_name is not configured and no host status found")

    system = load_system_prompt(cfg)
    lookup = campus_lookup(text, cfg) if campus is None else campus
    if lookup.context:
        # System prompt, not the user's message, for the same reason as the
        # camera line below: it is looked-up fact, not something the person said.
        system = f"{system}\n\n{lookup.context}"
    seen = vision_context() if vision is None else vision
    if seen:
        # Appended to the system prompt rather than the user's message so the
        # model treats it as situational context, not as something the person
        # said. Questions like "what am I holding" are unanswerable without it.
        system = f"{system}\n\nWhat your camera sees right now: {seen}."
    host, port = _resolve_endpoint(mc_cfg, probe_transport)

    if host is not None:
        start = time.monotonic()
        try:
            call = transport if transport is not None else chat_transport_for(mc_cfg)
            reply = call(host, port, model_name, text, cfg.chat.timeout_s, system)
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

    if lookup.offline_reply:
        # The info-desk role works with no model at all (CLAUDE.md): a question
        # the directory can answer gets that answer, not an apology.
        result = ChatResult(reply=lookup.offline_reply, source="kb")
    else:
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


def _discover_model_name(mc_cfg: ModelConnConfig) -> str:
    """Read the model name from var/model_conn/host.json when chat.model_name
    is not configured.

    `neo --model ... up` heartbeats this file with what it is serving, so the
    panel can show it -- and the same file tells us what Ollama will accept
    when the config stays silent. A stale or missing file means no host is up,
    which ask() already handles as degraded mode.
    """
    try:
        from model_conn.status_store import host_is_fresh, read_status
    except ImportError:
        return ""
    payload = read_status(mc_cfg.host_status_path)
    if not host_is_fresh(payload):
        log.info("host status is stale or absent at %s", mc_cfg.host_status_path)
        return ""
    name = payload.get("model_name", "")
    log.info("host status names model %r", name)
    return name


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
