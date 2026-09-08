"""Reads intelligence's local status file (var/intelligence/status.json).

Same pattern as link_status.py: always available regardless of ROS/rclpy, so
the admin panel can show the last chat turn with zero ROS installed. See
intelligence/status_store.py for the writer and JSON schema.
"""

from __future__ import annotations

import json
from pathlib import Path

from .bridge.types import DialogView
from .config import REPO_ROOT

STATUS_PATH = REPO_ROOT / "var" / "intelligence" / "status.json"


def read_dialog_status(path: Path = STATUS_PATH) -> DialogView:
    """Never raises: a missing/corrupt file just means intelligence hasn't run here."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return DialogView()

    return DialogView(
        state=raw.get("dialog_state", "IDLE"),
        last_prompt=raw.get("last_prompt", ""),
        last_reply=raw.get("last_reply", ""),
        chat_source=raw.get("chat_source", "none"),
        asr_engine=raw.get("asr_engine", ""),
        tts_engine=raw.get("tts_engine", ""),
        updated_at=raw.get("written_at_unix"),
    )
