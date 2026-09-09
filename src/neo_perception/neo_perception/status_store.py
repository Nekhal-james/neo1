"""Publishes what the robot is seeing to var/perception/status.json.

Third writer in the same family as model_conn's link status and intelligence's
dialog status, and deliberately the same shape: an atomically-replaced JSON file
under var/, readable by any process on the machine with no ROS, no sockets, and
no ordering requirement between the three `neo` commands.

That last property is the point. `neo --webapp up`, `neo --model ... up` and
`neo --prompt` are separate processes started in any order, and any of them may
be absent. A file that is simply stale (or missing) when a reader arrives is a
state every reader already has to handle -- a socket that isn't listening yet is
not.

What it buys: `neo --prompt "what am I holding"` can answer from the camera,
even though the camera belongs to the panel's process.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# .../neo1/src/neo_perception/neo_perception/status_store.py -> up 3
REPO_ROOT = Path(__file__).resolve().parents[3]

SCHEMA_VERSION = 1
STATUS_PATH = REPO_ROOT / "var" / "perception" / "status.json"

STALE_AFTER_S = 10.0
"""Beyond this, a reader should treat the file as "the panel is not running"
rather than as current vision state."""


def write_status(
    *,
    available: bool,
    running: bool = False,
    detector: str = "none",
    state: str = "scanning",
    engaged: bool = False,
    target_id: int | None = None,
    target_facing: str = "unknown",
    person_count: int = 0,
    last_gesture: str = "none",
    release_reason: str = "",
    engage_gesture: str = "open_palm",
    identify_seq: int = 0,
    identify_best: str = "",
    identify_error: str = "",
    identify_guesses: list[dict[str, Any]] | None = None,
    path: Path | None = None,
) -> None:
    """Never raises: losing a status write must not take the writer down.

    Explicit keywords rather than a view object, so this package owns the wire
    format without depending on whatever process happens to be producing it --
    today the admin panel, on the robot a ROS node.
    """
    # Resolved here, not as a default argument: a default would bind at import
    # time, so STATUS_PATH could never be overridden afterwards.
    path = STATUS_PATH if path is None else path
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "written_at_unix": time.time(),
        "available": available,
        "running": running,
        "detector": detector,
        "state": state,
        "engaged": engaged,
        "target_id": target_id,
        "target_facing": target_facing,
        "person_count": person_count,
        "last_gesture": last_gesture,
        "release_reason": release_reason,
        "engage_gesture": engage_gesture,
        "identify": {
            "seq": identify_seq,
            "best": identify_best,
            "error": identify_error,
            "guesses": identify_guesses or [],
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".status-", suffix=".tmp")
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


@dataclass
class VisionSnapshot:
    """What another process can learn about what the robot is looking at."""

    available: bool = False
    fresh: bool = False
    """False when the file is missing or older than STALE_AFTER_S -- i.e. the
    panel is not running. Distinct from `available`, which means the panel is
    running but has no working detector."""

    person_count: int = 0
    engaged: bool = False
    state: str = "scanning"
    target_facing: str = "unknown"
    last_object: str = ""
    objects: list[str] = None  # type: ignore[assignment]
    age_s: float | None = None

    def __post_init__(self) -> None:
        if self.objects is None:
            self.objects = []

    def describe(self) -> str:
        """One line of camera context for a prompt, or "" when there is nothing
        worth telling the model."""
        if not self.fresh or not self.available:
            return ""
        parts = []
        if self.person_count:
            who = "1 person" if self.person_count == 1 else f"{self.person_count} people"
            parts.append(f"{who} in view" + (" (engaged)" if self.engaged else ""))
        else:
            parts.append("nobody in view")
        if self.last_object:
            parts.append(f"most recently identified object: {self.last_object}")
        return "; ".join(parts)


def read_status(path: Path | None = None, now: float | None = None) -> VisionSnapshot:
    """Never raises: a missing or corrupt file just means the panel isn't up."""
    path = STATUS_PATH if path is None else path
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return VisionSnapshot()

    written = raw.get("written_at_unix")
    age = (time.time() if now is None else now) - written if written else None
    identify = raw.get("identify") or {}

    return VisionSnapshot(
        available=bool(raw.get("available")),
        fresh=age is not None and age <= STALE_AFTER_S,
        person_count=int(raw.get("person_count", 0)),
        engaged=bool(raw.get("engaged")),
        state=raw.get("state", "scanning"),
        target_facing=raw.get("target_facing", "unknown"),
        last_object=identify.get("best", ""),
        objects=[g.get("label", "") for g in identify.get("guesses", [])],
        age_s=age,
    )
