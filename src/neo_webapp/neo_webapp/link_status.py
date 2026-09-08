"""Reads model-conn's local status file (var/model_conn/status.json).

Always available regardless of ROS/rclpy/bridge backend -- this is what lets
the admin panel show real model-conn link/ping stats with zero ROS installed,
the same way MockBridge and RosBridge already coexist (plan section 0.3.4).
See model_conn/status_store.py for the writer and JSON schema.
"""

from __future__ import annotations

import json
from pathlib import Path

from .bridge.types import LinkHealth, PingStats
from .config import REPO_ROOT

STATUS_PATH = REPO_ROOT / "var" / "model_conn" / "status.json"


def read_link_status(path: Path = STATUS_PATH) -> tuple[LinkHealth, PingStats | None]:
    """Never raises: a missing/corrupt file just means model-conn hasn't run here."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return LinkHealth(), None

    link_raw = raw.get("link", {}) or {}
    link = LinkHealth(
        up=bool(link_raw.get("up", False)),
        active_path=link_raw.get("active_path", "none"),
        rtt_ms=link_raw.get("rtt_ms"),
        consecutive_failures=int(link_raw.get("consecutive_failures", 0)),
    )

    ping_raw = raw.get("ping")
    ping = PingStats(**ping_raw) if ping_raw else None
    return link, ping
