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
class PingStats:
    """Mirrors model_conn.link.PingStats -- populated from its status file.

    Not part of RobotState/the /ws/state snapshot: it comes from polling
    model-conn's local JSON file (see ../link_status.py), and that file I/O
    doesn't belong in the 4 Hz websocket loop. Served from its own route
    instead.
    """

    sent: int = 0
    received: int = 0
    loss_pct: float = 0.0
    rtt_min_ms: float | None = None
    rtt_avg_ms: float | None = None
    rtt_max_ms: float | None = None
    active_path: Literal["eth", "wifi", "none"] = "none"


@dataclass
class DialogView:
    """Mirrors intelligence's status file -- see ../dialog_status.py.

    Same reasoning as PingStats: not part of RobotState/the /ws/state
    snapshot, since reading it is file I/O and that doesn't belong in the
    4 Hz websocket loop. Served from its own route instead.
    """

    state: str = "IDLE"
    last_prompt: str = ""
    last_reply: str = ""
    chat_source: Literal["ollama", "degraded", "none"] = "none"
    asr_engine: str = ""
    tts_engine: str = ""
    updated_at: float | None = None


@dataclass
class TranscriptView:
    """Mirrors /dialog/transcript (neo_msgs/Transcript).

    Field-for-field, and asserted so by neo_msgs/test/test_contract.py -- this
    is what the ROS node will publish once Phase 5's asr_router exists, and the
    panel already renders it today from the in-process recognizer.
    """

    text: str = ""
    is_final: bool = False
    confidence: float = 0.0
    # "vosk" or "whisper". A string here, a uint8 constant on the wire; the
    # panel shows it verbatim because a bad transcript is not diagnosable
    # without knowing which engine produced it.
    engine: str = ""
    # Vosk was run against a grammar built from the room list. Phase 7 wires
    # that up; the field exists now because the contract has it and a
    # transcript suspiciously close to a real room code means something
    # different when a grammar was in force.
    grammar_constrained: bool = False


@dataclass
class AudioView:
    """Speech-to-text and text-to-speech state for the Audio tab.

    Availability is refreshed on the slow status task, not computed here: it
    stats the model paths, and file I/O has no business in the 4 Hz state
    broadcast (same rule as LinkHealth and the vision status file).
    """

    asr_available: bool = False
    asr_reason: str = ""
    asr_engine: str = ""
    tts_available: bool = False
    tts_reason: str = ""
    tts_engine: str = ""

    # A mic channel is open and audio is reaching the recognizer.
    listening: bool = False
    # The in-progress hypothesis. Changes as you speak, and is not a
    # transcript -- never stored as one.
    partial: str = ""
    last: TranscriptView = field(default_factory=TranscriptView)
    # Completed utterances this session, so the panel can tell "heard nothing"
    # from "not listening at all".
    utterances: int = 0
    # Bytes of PCM the recognizer has been fed. Distinguishes a muted mic from
    # a recognizer that heard audio but found no words in it.
    audio_bytes: int = 0
    # Set while synthesized audio is being pushed to the speaker channel.
    speaking: bool = False
    last_error: str = ""


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
class TrackView:
    """One tracked person, in normalised frame coordinates.

    Normalised (0..1) rather than pixels so the overlay lines up regardless of the
    resolution the browser happens to be sending or displaying.
    """

    track_id: int
    x1: float
    y1: float
    x2: float
    y2: float
    confirmed: bool = False
    engaged: bool = False
    gesture: str = "none"
    facing: str = "unknown"


@dataclass
class ObjectGuessView:
    """One candidate answer to "what is this?"."""

    label: str
    confidence: float
    prominence: float
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class IdentifyView:
    """Result of the most recent identify request."""

    pending: bool = False
    seq: int = 0
    """Increments per completed request, so the UI can tell a fresh answer from
    a stale one without comparing labels."""

    error: str = ""
    guesses: list[ObjectGuessView] = field(default_factory=list)

    best: str = field(init=False, default="")
    """The top-ranked label, or "".

    A real field rather than a property: `RobotState.to_dict()` is `asdict()`,
    which drops properties silently, so a property here would be missing from
    /api/state and /ws/state with no error -- and `best` is the one field of
    this view a consumer actually wants.
    """

    def __post_init__(self) -> None:
        self.best = self.guesses[0].label if self.guesses else ""


@dataclass
class PerceptionView:
    """Perception state for the Vision tab."""

    available: bool = False
    running: bool = False
    detector: str = "none"
    state: str = "scanning"
    engaged: bool = False
    target_id: int | None = None
    person_count: int = 0
    inference_ms: float = 0.0
    fps: float = 0.0
    dropped_frames: int = 0
    last_gesture: str = "none"
    release_reason: str = ""
    target_facing: str = "unknown"
    engage_gesture: str = "open_palm"
    """What the robot is actually waiting to see, so the panel's instruction to
    the operator can never drift from the configured gesture."""

    identify: IdentifyView = field(default_factory=IdentifyView)
    tracks: list[TrackView] = field(default_factory=list)
    # Normalised aim point, y positive up, matching the joystick convention.
    aim_x: float = 0.0
    aim_y: float = 0.0


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
    perception: PerceptionView = field(default_factory=PerceptionView)
    audio: AudioView = field(default_factory=AudioView)
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
