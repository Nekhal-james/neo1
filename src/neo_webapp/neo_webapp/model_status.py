"""What `neo --model ... up` is serving, as seen by the admin panel.

Sibling of link_status.py and dialog_status.py, and the third leg of the same
idea: those two report the *receiver's* view of the link and the last chat turn;
this one reports the **host's** view -- whether a model is up right now, which
one, and where. No amount of probing from the receiver side answers "which model
name is registered", and that is exactly the value the panel needs, because
asking for the wrong name fails as a 404 that looks like the link being down.

Also checks a mismatch that is otherwise silent: `intelligence` asks for
`chat.model_name` from config/intelligence.yaml, and that has to match what the
host actually registered. Nothing enforces it, and getting it wrong produces a
degraded reply with no clue why -- so the panel says so out loud.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class ModelView:
    reachable: bool = False
    """A live `neo --model ... up` is publishing heartbeats."""

    serving: bool = False
    model_name: str = ""
    model_path: str = ""
    endpoint: str = ""
    tls_enabled: bool = False
    age_s: float | None = None

    configured_model: str = ""
    """What intelligence will actually ask for."""

    mismatch: bool = False
    warnings: list[str] = field(default_factory=list)

    summary: str = ""
    """One-line status for the UI. A real field, not a property: this view is
    served through `asdict()`, which drops properties silently. Filled by
    `refresh_summary()` once the fields above are settled."""

    def refresh_summary(self) -> None:
        if not self.reachable:
            self.summary = "no model host running"
        elif not self.serving:
            self.summary = "model host stopped"
        else:
            self.summary = f"serving {self.model_name}"


def read_model_status() -> ModelView:
    """Never raises: every failure here just means "no host visible"."""
    try:
        from model_conn.config import Config as ModelConnConfig
        from model_conn.status_store import host_is_fresh, read_status
    except ImportError as exc:  # pragma: no cover - depends on install
        log.debug("model_conn unavailable: %s", exc)
        return _finish(ModelView(warnings=["model_conn is not installed"]))

    try:
        mc_cfg = ModelConnConfig.load()
        payload = read_status(mc_cfg.host_status_path)
        fresh = host_is_fresh(payload)
    except Exception:  # noqa: BLE001 -- status must never break the panel
        log.exception("reading host status failed")
        return _finish(
            ModelView(warnings=["could not read the model host status file"])
        )

    view = ModelView(reachable=fresh)
    if payload:
        written = payload.get("written_at_unix")
        if written:
            import time

            view.age_s = time.time() - written
        # Only trust the payload's claims while it is fresh: a stale file is a
        # record of a process that has since exited.
        if fresh:
            view.serving = bool(payload.get("serving"))
            view.model_name = payload.get("model_name", "")
            view.model_path = payload.get("model_path", "")
            view.endpoint = payload.get("endpoint", "")
            view.tls_enabled = bool(payload.get("tls_enabled"))

    view.configured_model = _configured_model()
    if view.serving and view.configured_model and view.model_name:
        if view.configured_model != view.model_name:
            view.mismatch = True
            view.warnings.append(
                f"the host is serving '{view.model_name}' but intelligence is "
                f"configured to ask for '{view.configured_model}' -- chat will "
                f"fail and fall back to degraded mode until they match "
                f"(chat.model_name in config/intelligence.yaml)"
            )
    elif view.serving and not view.configured_model:
        view.warnings.append(
            "chat.model_name is not set in config/intelligence.yaml, so chat "
            f"cannot reach the model -- set it to '{view.model_name}'"
        )
    return _finish(view)


def _finish(view: ModelView) -> ModelView:
    """Single exit point, so no return path can ship an empty summary."""
    view.refresh_summary()
    return view


def _configured_model() -> str:
    try:
        from intelligence.config import Config as IntelligenceConfig

        return IntelligenceConfig.load().chat.model_name
    except Exception:  # noqa: BLE001 -- optional package, optional config
        log.debug("intelligence config unavailable", exc_info=True)
        return ""
