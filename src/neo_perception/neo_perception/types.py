"""Perception data types.

Deliberately plain Python: no ROS, no OpenCV, no numpy in the signatures. The whole
tracking / gesture / engagement stack is therefore unit-testable on a laptop with
no camera and no robot, which is the same discipline `neo_kb` uses for campus data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

# COCO-17 keypoint order, as produced by the YOLO pose models.
KEYPOINT_NAMES = (
    "nose",
    "left_eye",
    "right_eye",
    "left_ear",
    "right_ear",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
)
KP = {name: i for i, name in enumerate(KEYPOINT_NAMES)}

FACE_KEYPOINTS = ("nose", "left_eye", "right_eye", "left_ear", "right_ear")


@dataclass(frozen=True)
class BBox:
    """Axis-aligned box in pixel coordinates, origin top-left."""

    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2.0

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2.0

    def iou(self, other: BBox) -> float:
        ix1, iy1 = max(self.x1, other.x1), max(self.y1, other.y1)
        ix2, iy2 = min(self.x2, other.x2), min(self.y2, other.y2)
        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0

    def translated(self, dx: float, dy: float) -> BBox:
        return BBox(self.x1 + dx, self.y1 + dy, self.x2 + dx, self.y2 + dy)

    def touches_border(self, width: int, height: int, margin_px: float = 8.0) -> bool:
        """True when the box is against a frame edge.

        This is how we tell "walked out of shot" from "the detector blinked", which
        get very different grace periods in the engagement state machine.
        """
        return (
            self.x1 <= margin_px
            or self.y1 <= margin_px
            or self.x2 >= width - margin_px
            or self.y2 >= height - margin_px
        )


@dataclass(frozen=True)
class Keypoint:
    x: float
    y: float
    score: float


@dataclass(frozen=True)
class Keypoints:
    """COCO-17 pose keypoints for one person."""

    points: tuple[Keypoint, ...]

    def get(self, name: str, min_score: float = 0.3) -> Keypoint | None:
        idx = KP.get(name)
        if idx is None or idx >= len(self.points):
            return None
        kp = self.points[idx]
        return kp if kp.score >= min_score else None

    def torso_scale(self, min_score: float = 0.3) -> float | None:
        """A scale reference in pixels, so thresholds are distance-invariant.

        Shoulder-to-hip if both are visible, else shoulder width, else nothing --
        never a box dimension, which changes with raised arms.
        """
        ls = self.get("left_shoulder", min_score)
        rs = self.get("right_shoulder", min_score)
        lh = self.get("left_hip", min_score)
        rh = self.get("right_hip", min_score)

        if ls and rs and (lh or rh):
            shoulder_y = (ls.y + rs.y) / 2.0
            hip_y = ((lh.y if lh else rh.y) + (rh.y if rh else lh.y)) / 2.0
            span = abs(hip_y - shoulder_y)
            if span > 1.0:
                return span
        if ls and rs:
            span = math.hypot(ls.x - rs.x, ls.y - rs.y)
            if span > 1.0:
                return span
        return None

    def face_anchor(self, min_score: float = 0.3) -> tuple[float, float] | None:
        """Mean of the visible face keypoints -- where to actually look.

        A receptionist should meet your eyes, not stare at your sternum, so this
        beats the bounding-box centre as a gaze target.
        """
        pts = [self.get(n, min_score) for n in FACE_KEYPOINTS]
        vis = [p for p in pts if p is not None]
        if not vis:
            return None
        return (
            sum(p.x for p in vis) / len(vis),
            sum(p.y for p in vis) / len(vis),
        )

    def has_face(self, min_score: float = 0.3) -> bool:
        """Whether the front of the head is visible.

        The nose or a pair of eyes. Ears deliberately do not count: they stay
        visible from behind, which is exactly the case this must not confuse
        with facing the camera.
        """
        if self.get("nose", min_score) is not None:
            return True
        return (
            self.get("left_eye", min_score) is not None
            and self.get("right_eye", min_score) is not None
        )

    def facing(
        self,
        min_score: float = 0.3,
        profile_ratio: float = 0.25,
    ) -> FacingState:
        """Which way the person is turned, from keypoints alone.

        Two independent signals, because either one alone is fooled:

        * **Shoulder parity.** COCO labels shoulders from the *person's* frame,
          so someone facing the camera has their left shoulder on the image's
          right (`left.x > right.x`). Turn around and that inverts. This is the
          strong signal, and it keeps working in poor light where the face does
          not resolve.
        * **Face visibility.** Nose or both eyes. Necessary because at very
          shallow angles the shoulder parity is within noise.

        Parity is only trusted once the shoulders are far enough apart to mean
        anything: edge-on, their x separation collapses and the sign is noise,
        which is what `profile_ratio` gates.
        """
        ls = self.get("left_shoulder", min_score)
        rs = self.get("right_shoulder", min_score)
        if ls is None or rs is None:
            return FacingState.UNKNOWN

        scale = self.torso_scale(min_score)
        if scale is None or scale <= 0:
            return FacingState.UNKNOWN

        parity = ls.x - rs.x
        separation = abs(parity) / scale
        face = self.has_face(min_score)

        if separation < profile_ratio:
            # Edge-on. Still engaged with the room, just not square to us.
            return FacingState.PROFILE if face else FacingState.AWAY
        if parity < 0:
            return FacingState.AWAY
        if not face:
            return FacingState.AWAY
        return FacingState.FACING


class FacingState(str, Enum):
    """Which way a tracked person is turned."""

    FACING = "facing"      # square to the camera
    PROFILE = "profile"    # edge-on; still present, just looking elsewhere
    AWAY = "away"          # turned their back
    UNKNOWN = "unknown"    # not enough keypoints to say


@dataclass(frozen=True)
class Detection:
    bbox: BBox
    score: float
    label: str = "person"
    class_id: int = 0
    keypoints: Keypoints | None = None


class GestureKind(str, Enum):
    NONE = "none"
    RAISED_HAND = "raised_hand"
    """Hand above the shoulder, at any arm angle. Loose: it also fires on
    stretching, scratching your head, or reaching for a shelf."""

    OPEN_PALM = "open_palm"
    """Hand held up with a roughly vertical forearm -- a palm presented to the
    camera. The default engage gesture, because the verticality requirement is
    what separates "I want your attention" from "I am scratching my head".

    Caveat worth knowing: COCO-17 has no finger keypoints, so this is a pose,
    not a hand shape. A raised fist looks identical. Telling those apart needs a
    hand-landmark model on a wrist crop, which is a cost the Pi has not got.
    """

    WAVE = "wave"


@dataclass(frozen=True)
class GestureEvent:
    track_id: int
    kind: GestureKind
    confidence: float
    stamp: float


class EngagementState(str, Enum):
    """Who, if anyone, the head is locked onto."""

    SCANNING = "scanning"      # nobody engaged; watching for a gesture
    ENGAGING = "engaging"      # gesture seen, confirming it is deliberate
    ENGAGED = "engaged"        # locked and tracking
    SUSPENDED = "suspended"    # locked target not visible; grace timer running


@dataclass(frozen=True)
class ObjectGuess:
    """One candidate answer to "what is this?"."""

    label: str
    confidence: float
    bbox: BBox
    prominence: float = 0.0
    """How much this looks like the thing being *presented* rather than
    something incidental in shot -- see `pipeline.pick_presented_object`."""


@dataclass
class Track:
    """One tracked person across frames."""

    track_id: int
    bbox: BBox
    stamp: float
    score: float = 0.0
    keypoints: Keypoints | None = None
    facing: FacingState = FacingState.UNKNOWN
    vx: float = 0.0
    vy: float = 0.0
    hits: int = 1
    misses: int = 0
    confirmed: bool = False
    first_seen: float = 0.0
    last_seen: float = 0.0
    # Recent wrist x positions, for detecting a wave.
    wrist_history: list[tuple[float, float]] = field(default_factory=list)

    def predict(self, dt: float) -> BBox:
        return self.bbox.translated(self.vx * dt, self.vy * dt)


@dataclass(frozen=True)
class AttentionTarget:
    """Mirrors /perception/attention.

    x and y are normalised to [-1, 1] with **y positive upward**, matching the
    joystick convention in the admin panel so both feed the head the same way.
    """

    x: float = 0.0
    y: float = 0.0
    confidence: float = 0.0
    track_id: int | None = None
    person_present: bool = False
    engaged: bool = False
    state: EngagementState = EngagementState.SCANNING


@dataclass(frozen=True)
class EngagementDecision:
    """What the engagement machine concluded, before any gaze geometry.

    Kept separate from `AttentionTarget` so the state machine can be tested with
    no notion of pixels, frames, or servo angles.
    """

    state: EngagementState
    target: Track | None = None
    track_id: int | None = None
    confidence: float = 0.0
    engaged: bool = False
    person_present: bool = False


@dataclass
class PerceptionResult:
    """Everything produced for one frame."""

    stamp: float
    width: int
    height: int
    tracks: list[Track] = field(default_factory=list)
    objects: list[ObjectGuess] = field(default_factory=list)
    gestures: list[GestureEvent] = field(default_factory=list)
    attention: AttentionTarget = field(default_factory=AttentionTarget)
    inference_ms: float = 0.0
    dropped_frames: int = 0

    @property
    def person_count(self) -> int:
        return sum(1 for t in self.tracks if t.confirmed)
