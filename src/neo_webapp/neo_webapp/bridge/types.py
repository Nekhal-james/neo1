"""State shapes exchanged between a bridge and the API.

These mirror the ROS contracts in docs/IMPLEMENTATION_PLAN.md section 1.2. Keeping
them as plain dataclasses (not ROS messages) is what lets the panel run with no ROS
installed, and keeps the UI from depending on message definitions that will move.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Stream = Literal["camera", "mic", "speaker"]
Backend = Literal["hardware", "webapp"]

STREAMS: tuple[Stream, ...] = ("camera", "mic", "speaker")
BACKENDS: tuple[Backend, ...] = ("hardware", "webapp")

# /dialog/state
DIALOG_STATES = ("IDLE", "LISTENING", "THINKING", "SPEAKING", "DEGRADED", "ESTOP")


@dataclass
class SourcesState:
    camera: Backend = "hardware"
    mic: Backend = "hardware"
    speaker: Backend = "hardware"
    # Set while a transition is in flight, so the UI can disable the control
    # rather than let an operator queue up conflicting switches.
    transitioning: Stream | None = None


@dataclass
class LinkHealth:
    """Mirrors /link/health. `active_path` reflects the dual-path failover."""

    up: bool = False
    active_path: Literal["eth", "wifi", "none"] = "none"
    rtt_ms: float | None = None
    consecutive_failures: int = 0


@dataclass
class HeadState:
    """Mirrors /head/state, plus the limits the UI needs to render them."""

    pan_deg: float = 0.0
    tilt_deg: float = 0.0
    pan_limit_deg: tuple[float, float] = (-90.0, 90.0)
    tilt_limit_deg: tuple[float, float] = (-35.0, 35.0)
    at_limit: bool = False
    estop: bool = False
    # Which arbiter source is currently winning; see plan Phase 3.
    active_source: str = "idle"


@dataclass
class SystemHealth:
    cpu_percent: float = 0.0
    mem_used_mb: float = 0.0
    mem_total_mb: float = 0.0
    temp_c: float | None = None
    uptime_s: float = 0.0


@dataclass
class EmotionState:
    label: str = "NEUTRAL"
    intensity: float = 0.0


@dataclass
class MediaChannels:
    """Which on-demand media bridges are currently running (plan section 0.3.4)."""

    camera: bool = False
    mic: bool = False
    speaker: bool = False
    joy: bool = False


@dataclass
class NodeStatus:
    name: str
    state: Literal["active", "inactive", "missing", "error"]


@dataclass
class RobotState:
    backend: str = "mock"
    dialog_state: str = "IDLE"
    sources: SourcesState = field(default_factory=SourcesState)
    link: LinkHealth = field(default_factory=LinkHealth)
    head: HeadState = field(default_factory=HeadState)
    system: SystemHealth = field(default_factory=SystemHealth)
    emotion: EmotionState = field(default_factory=EmotionState)
    media: MediaChannels = field(default_factory=MediaChannels)
    nodes: list[NodeStatus] = field(default_factory=list)
    # Rolling counters, useful for confirming a stream is actually flowing.
    camera_fps_in: float = 0.0
    mic_kbps_in: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Result:
    ok: bool
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
