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


def _plausible_head_width(
    width: float, shoulder_span: float | None, person_bbox: BBox | None
) -> bool:
    """Is this a believable head width, given whatever scale we can see?

    Anthropometry, loosely: a head is about 15 cm across and shoulders about
    40 cm, so head/shoulders lands near 0.38. The bounds are wide because
    perspective, rotation and keypoint noise all move it -- they exist to
    reject the degenerate cases (an ear span of two pixels, a head wider than
    the body), not to enforce a ratio.
    """
    if width <= 2.0:
        return False
    if shoulder_span and shoulder_span > 1.0:
        return 0.22 * shoulder_span <= width <= 0.95 * shoulder_span
    if person_bbox is not None and person_bbox.width > 0:
        return 0.12 * person_bbox.width <= width <= 0.85 * person_bbox.width
    return True


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

    def head_box(
        self,
        person_bbox: BBox | None = None,
        min_score: float = 0.3,
    ) -> BBox | None:
        """The head, derived from the face keypoints already in hand.

        At a reception desk people stand close, so the *person* box is usually
        clipped by the frame edge and its centre lands somewhere on a torso that
        fills the view. The head is the part that stays whole, and it is what a
        head-and-camera assembly actually wants to point at.

        Costs no extra inference: nose, eyes and ears come from the same pose
        pass. Measured close up, they stay 4-5 of 5 visible even when the body
        keypoints have fallen to 2 of 8.

        Width comes from the widest reliable span available, in preference
        order, because each degrades differently:

        * **ear to ear** is the head's true width, but the two ears collapse
          together in profile, so it is only trusted once they are far enough
          apart to mean something,
        * **eye to eye** is about 0.31 of head width and survives profile far
          better,
        * **shoulders** are a last resort at roughly 0.45 of their span, used
          when the face is turned away and only an ear or nothing is visible.

        Returns None when nothing usable is visible; callers fall back to the
        person box themselves rather than being handed a fabricated one.
        """
        pts = [p for p in (self.get(n, min_score) for n in FACE_KEYPOINTS) if p]

        shoulders = self._shoulder_span(min_score)
        # A shoulder span can itself be degenerate -- turned away or in poor
        # light the two keypoints collapse to a few pixels apart. Trusting that
        # as the scale reference rejects every other candidate as "too wide" and
        # yields no head at all, so check the reference before believing it.
        if (
            shoulders
            and person_bbox is not None
            and person_bbox.width > 0
            and shoulders < 0.15 * person_bbox.width
        ):
            shoulders = None

        left_ear, right_ear = self.get("left_ear", min_score), self.get("right_ear", min_score)
        left_eye, right_eye = self.get("left_eye", min_score), self.get("right_eye", min_score)

        # Candidates in preference order. Each is checked for plausibility
        # rather than merely for being positive: turned away or edge-on, the two
        # ears land almost on top of each other and their span silently becomes
        # a couple of pixels. Accepting that yields a 1x2 px "head", which then
        # matches nothing and quietly destroys the track.
        candidates: list[float] = []
        if left_ear and right_ear:
            candidates.append(
                math.hypot(left_ear.x - right_ear.x, left_ear.y - right_ear.y) * 1.15
            )
        if left_eye and right_eye:
            candidates.append(
                math.hypot(left_eye.x - right_eye.x, left_eye.y - right_eye.y) * 3.2
            )
        if shoulders:
            candidates.append(shoulders * 0.45)
        if person_bbox is not None and person_bbox.width > 0:
            candidates.append(person_bbox.width * 0.35)

        width = next(
            (w for w in candidates if _plausible_head_width(w, shoulders, person_bbox)),
            None,
        )
        if width is None:
            return None

        if pts:
            cx = sum(p.x for p in pts) / len(pts)
            cy = sum(p.y for p in pts) / len(pts)
        elif person_bbox is not None:
            cx, cy = person_bbox.cx, person_bbox.y1 + person_bbox.height * 0.10
        else:
            return None

        # Eyes and ears sit near the middle of the head, the nose below it, so
        # the keypoint centroid lands low. Lift it to cover the skull.
        height = width * 1.35
        cy -= height * 0.18
        half_w, half_h = width / 2.0, height / 2.0
        return BBox(cx - half_w, cy - half_h, cx + half_w, cy + half_h)

    def _shoulder_span(self, min_score: float = 0.3) -> float | None:
        ls = self.get("left_shoulder", min_score)
        rs = self.get("right_shoulder", min_score)
        if not (ls and rs):
            return None
        span = math.hypot(ls.x - rs.x, ls.y - rs.y)
        return span if span > 1.0 else None

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

    head_bbox: BBox | None = None
    """The head, from `Keypoints.head_box`. None when no usable keypoints."""

    person_bbox: BBox | None = None
    """The full-person box, kept when `bbox` has been swapped for the head so
    the original is still available for overlays and debugging."""


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
    person_bbox: BBox | None = None
    """The full-person box. `bbox` is the head when head tracking is on, so
    gesture overlays and debugging still need this."""

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


@dataclass(frozen=True)
class PalmCheck:
    """Why one person's pose does or does not count as a presented palm.

    The palm test is a chain of conditions, and at a real desk it breaks on
    different links for different people: an elbow below the bottom of the
    frame, a forearm leaning past the limit, a hand drifting enough to read as a
    wave. Knowing which link broke is the difference between tuning the right
    threshold and guessing at all of them. Reported for the arm that got
    furthest through the chain.
    """

    track_id: int
    verdict: GestureKind = GestureKind.NONE
    reason: str = ""
    """The first condition that failed, in words; empty when it is a palm."""
    arm: str = ""
    lift: float | None = None
    """Wrist height above the shoulder, in torso units."""
    forearm_tilt_deg: float | None = None
    forearm_len: float | None = None
    """Wrist-to-elbow distance, in torso units."""
    wrist_score: float = 0.0
    elbow_score: float = 0.0
    shoulder_score: float = 0.0


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
    palm_check: PalmCheck | None = None
    """Why the most relevant person is or is not showing a palm."""
    hold_progress: float = 0.0
    """How far through the palm hold the current candidate is, 0 to 1."""

    @property
    def person_count(self) -> int:
        return sum(1 for t in self.tracks if t.confirmed)
