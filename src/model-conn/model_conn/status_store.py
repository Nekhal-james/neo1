"""Atomic JSON status file, shared with neo_webapp across process boundaries.

neo_webapp reads this file to show link/ping stats with zero ROS installed
(see neo_webapp/link_status.py). Writes use a tempfile-then-rename so a reader
never sees a torn/partial file; reads never raise -- a missing or corrupt file
just means "model-conn hasn't run here yet", which is the project's normal
degraded-mode philosophy applied to this file too.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .link import LinkStatus, PingStats

SCHEMA_VERSION = 1


def write_status(
    link: LinkStatus, ping: PingStats | None, *, path: Path
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "written_at_unix": time.time(),
        "link": {
            "up": link.up,
            "active_path": link.active_path,
            "rtt_ms": link.rtt_ms,
            "consecutive_failures": link.consecutive_failures,
        },
        "ping": None
        if ping is None
        else {
            "sent": ping.sent,
            "received": ping.received,
            "loss_pct": ping.loss_pct,
            "rtt_min_ms": ping.rtt_min_ms,
            "rtt_avg_ms": ping.rtt_avg_ms,
            "rtt_max_ms": ping.rtt_max_ms,
            "active_path": ping.active_path,
        },
    }

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".status-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def read_status(path: Path) -> dict[str, Any] | None:
    """Returns None on any missing/unreadable/corrupt file. Never raises."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# -- host status ------------------------------------------------------------
#
# The file above is written by the *receiver* checking its link. This one is
# written by the *host* -- `neo --model ... up` -- saying what it is actually
# serving. The admin panel needs the second: "is a model up, and which one",
# which no amount of probing from the receiver side can answer.

HOST_SCHEMA_VERSION = 1

HOST_HEARTBEAT_S = 5.0
"""How often `up` refreshes the file while it runs. Readers treat anything
older than a few multiples of this as "not running" -- see `host_is_fresh`."""

HOST_STALE_AFTER_S = 20.0


def write_host_status(
    *,
    serving: bool,
    model_name: str = "",
    model_path: str = "",
    endpoint: str = "",
    tls_enabled: bool = False,
    path: Path,
) -> None:
    """Never raises: losing a status write must not take the model host down."""
    payload: dict[str, Any] = {
        "schema_version": HOST_SCHEMA_VERSION,
        "written_at_unix": time.time(),
        "serving": serving,
        "model_name": model_name,
        "model_path": model_path,
        "endpoint": endpoint,
        "tls_enabled": tls_enabled,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".host-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        finally:
            Path(tmp_name).unlink(missing_ok=True)
    except OSError:
        pass


def host_is_fresh(payload: dict[str, Any] | None, now: float | None = None) -> bool:
    """Whether the host process is still alive and writing.

    `serving: false` is written on a clean exit, but a killed process leaves the
    last heartbeat behind -- so age is what actually decides, and the flag only
    makes a clean shutdown visible immediately rather than after the timeout.
    """
    if not payload:
        return False
    written = payload.get("written_at_unix")
    if not written:
        return False
    age = (time.time() if now is None else now) - written
    return age <= HOST_STALE_AFTER_S
