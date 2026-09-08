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
