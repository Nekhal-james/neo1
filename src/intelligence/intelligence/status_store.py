"""Atomic JSON status file, read by neo_webapp -- same pattern as
model_conn/status_store.py (see that module's docstring for the reasoning).
Written on every chat turn so the admin panel can show the last
prompt/reply/engine with no ROS installed.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def write_status(
    *,
    dialog_state: str,
    last_prompt: str,
    last_reply: str,
    chat_source: str,
    asr_engine: str,
    tts_engine: str,
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "written_at_unix": time.time(),
        "dialog_state": dialog_state,
        "last_prompt": last_prompt,
        "last_reply": last_reply,
        "chat_source": chat_source,
        "asr_engine": asr_engine,
        "tts_engine": tts_engine,
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
