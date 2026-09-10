"""Frame in, attention out.

    frame -> detector -> tracker -> gestures -> engagement -> gaze -> command

`AsyncPerception` adds the threading policy the Pi needs: inference on a worker
thread with a **single-slot, latest-wins** input. Frames are dropped, never
queued. A queued backlog would make the head chase where somebody used to be,
which looks far worse than a lower frame rate.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field

from .detector import (
    Detector,
    MockDetector,
    ObjectDetector,
    ObjectDetectorConfig,
    UltralyticsConfig,
    make_detector,
)
from .engagement import EngagementConfig, EngagementController
from .gaze import GazeCommand, GazeConfig, GazeMapper
from .gestures import GestureConfig, GestureRecognizer
from .tracker import MultiTracker, TrackerConfig
from .types import BBox, Detection, FacingState, ObjectGuess, PerceptionResult

log = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    detector_backend: str = "auto"
    detector: UltralyticsConfig = field(default_factory=UltralyticsConfig)
    objects: ObjectDetectorConfig = field(default_factory=ObjectDetectorConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    gestures: GestureConfig = field(default_factory=GestureConfig)
    engagement: EngagementConfig = field(default_factory=EngagementConfig)
    gaze: GazeConfig = field(default_factory=GazeConfig)

    target_fps: float = 4.0
    """Inference rate ceiling. Defaults to the Pi 4 working point; raise it on a
    machine that can afford more."""

    track_head: bool = True
    """Track the head rather than the whole person.

    The reception-desk default. People stand close enough that the person box is
    clipped by the frame and its centre sits on a torso filling the view, while
    the head stays whole and is what the pan/tilt assembly actually aims at.
    Detection measurably *improves* at that range -- what degrades is the body
    keypoints, not the face ones.

    Set False for a wide shot where whole people are visible and the extra
    stability of a large box is worth more.
    """


class PerceptionPipeline:
    """Synchronous pipeline. One call, one frame, deterministic -- so the whole
    behaviour can be driven from tests with no camera and no clock."""

    def __init__(
        self,
        config: PipelineConfig | None = None,
        detector: Detector | None = None,
        object_detector: Detector | None = None,
    ) -> None:
        self.cfg = config or PipelineConfig()
        self.detector = detector or make_detector(self.cfg.detector_backend, self.cfg.detector)
        self.tracker = MultiTracker(self.cfg.tracker)
        self.gestures = GestureRecognizer(self.cfg.gestures)
        self.engagement = EngagementController(self.cfg.engagement)
        self.gaze = GazeMapper(self.cfg.gaze)

        for problem in self.engagement.validate_against_tracker(self.cfg.tracker):
            log.warning("engagement config: %s", problem)

        self.last_result = PerceptionResult(stamp=0.0, width=0, height=0)
        self.last_command = GazeCommand()

        # Object identification is request-driven: no model is loaded, and no
        # cost paid, until somebody actually asks "what is this?".
        self._object_detector: Detector | None = object_detector
        self._identify_requested = False
        self.last_identification: list[ObjectGuess] = []
        self.last_identify_error: str = ""
        self.identify_seq = 0

    def process(self, frame, stamp: float | None = None) -> PerceptionResult:
        stamp = time.monotonic() if stamp is None else stamp
        height, width = _frame_size(frame)

        detections, inference_ms = _timed(self.detector, frame)
        persons = [d for d in detections if d.label == "person" or d.class_id == 0]
        if self.cfg.track_head:
            persons = [_as_head_detection(d) for d in persons]

        tracks = self.tracker.update(persons, stamp)
        visible = self.tracker.visible_tracks(stamp)
        for track in visible:
            track.facing = (
                track.keypoints.facing()
                if track.keypoints is not None
                else FacingState.UNKNOWN
            )

        gesture_map, gesture_events = self.gestures.update(visible, stamp)
        decision = self.engagement.update(visible, gesture_map, stamp, width, height)
        attention = self.gaze.to_attention(decision, width, height)
        self.last_command = self.gaze.to_command(attention)

        if self._identify_requested:
            self._identify_requested = False
            self._run_identify(frame, width, height)

        self.last_result = PerceptionResult(
            stamp=stamp,
            width=width,
            height=height,
            tracks=list(tracks),
            objects=list(self.last_identification),
            gestures=gesture_events,
            attention=attention,
            inference_ms=inference_ms,
            palm_check=self._explain_palm(visible, stamp),
            hold_progress=self.engagement.hold_progress(stamp),
        )
        return self.last_result

    # -- object identification --------------------------------------------

    def request_identify(self) -> None:
        """Ask for the next frame to be run through the object model.

        Deferred to the worker rather than run here so a question never blocks
        the caller, and so identification uses a live frame rather than whatever
        the pipeline last happened to keep.
        """
        self._identify_requested = True

    @property
    def identify_pending(self) -> bool:
        return self._identify_requested

    def _run_identify(self, frame, width: int, height: int) -> None:
        try:
            if self._object_detector is None:
                self._object_detector = self._make_object_detector()
            detections = self._object_detector.infer(frame)
        except Exception as exc:
            log.exception("object identification failed")
            self.last_identify_error = str(exc)
            self.last_identification = []
            self.identify_seq += 1
            return

        self.last_identify_error = ""
        self.last_identification = rank_presented_objects(detections, width, height)
        self.identify_seq += 1

    def _make_object_detector(self) -> Detector:
        """A mock person detector implies a test or a machine with no models, so
        identification stays mocked too rather than trying to download weights."""
        if isinstance(self.detector, MockDetector):
            return MockDetector()
        return ObjectDetector(self.cfg.objects)

    def release(self, reason: str = "released") -> None:
        self.engagement.release(reason)

    def _explain_palm(self, visible, stamp: float):
        """The palm check for whoever matters most on this frame.

        The candidate part-way through a hold if there is one, else the locked
        target, else the nearest person -- the one who would win if they raised
        a hand now.
        """
        by_id = {t.track_id: t for t in visible}
        track = by_id.get(self.engagement.candidate_id) or by_id.get(self.engagement.target_id)
        if track is None and visible:
            track = max(visible, key=lambda t: t.bbox.area)
        return self.gestures.explain(track, stamp) if track is not None else None

    def reset(self) -> None:
        self.tracker.reset()
        self.gestures.reset()
        self.engagement.reset()


class AsyncPerception:
    """Runs a pipeline on a worker thread with a latest-wins input slot."""

    def __init__(self, pipeline: PerceptionPipeline | None = None) -> None:
        self.pipeline = pipeline or PerceptionPipeline()
        self._pending: tuple[object, float] | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self.dropped = 0
        self.processed = 0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="perception", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._running = False
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def submit(self, frame, stamp: float | None = None) -> None:
        """Never blocks. Replacing an unprocessed frame is the intended behaviour."""
        with self._lock:
            if self._pending is not None:
                self.dropped += 1
            self._pending = (frame, time.monotonic() if stamp is None else stamp)
        self._wake.set()

    def _run(self) -> None:
        min_period = 1.0 / max(self.pipeline.cfg.target_fps, 0.1)
        last_run = 0.0

        # Pay the model's first-inference cost here rather than on the first
        # person to walk up: lazy loading otherwise swallows the opening seconds
        # of an interaction, which is exactly when someone is waving.
        try:
            self.pipeline.detector.warmup()
        except Exception:
            log.exception("detector warmup failed")
        while self._running:
            self._wake.wait(timeout=0.1)
            self._wake.clear()

            now = time.monotonic()
            if (now - last_run) < min_period:
                continue

            with self._lock:
                item, self._pending = self._pending, None
            if item is None:
                continue

            frame, stamp = item
            try:
                self.pipeline.process(frame, stamp)
                self.processed += 1
            except Exception:
                log.exception("perception frame failed")
            last_run = time.monotonic()

    @property
    def result(self) -> PerceptionResult:
        result = self.pipeline.last_result
        result.dropped_frames = self.dropped
        return result

    @property
    def command(self) -> GazeCommand:
        return self.pipeline.last_command

    def request_identify(self) -> None:
        self.pipeline.request_identify()
        # Nudge the worker: a question should not wait out an idle timeout.
        self._wake.set()


def _as_head_detection(det: Detection) -> Detection:
    """Swap `bbox` for the head, keeping the person box alongside it.

    Done by rewriting the detection rather than teaching the tracker about two
    boxes: everything downstream -- association, border tests, "nearest person
    is the biggest box" -- then operates on the head with no special cases, and
    a bigger head still correctly means a closer person.

    Every tracked box must be a head, including when the keypoints gave us
    nothing: salience is "largest box wins", so one person box left among head
    boxes is several times the area of any of them and would always win. Rather
    than let the semantics mix, fall back to the top of the person box -- a
    guess, but a head-shaped one that compares fairly against the rest.
    """
    head = det.head_bbox or _head_from_person_box(det.bbox)
    if head is None:
        return det
    return Detection(
        bbox=head,
        score=det.score,
        label=det.label,
        class_id=det.class_id,
        keypoints=det.keypoints,
        head_bbox=head,
        person_bbox=det.bbox,
    )


def _head_from_person_box(box: BBox) -> BBox | None:
    """Last-resort head region: the top of the person box.

    Only reached when the pose model gave no usable keypoints at all -- a
    plain detect model, or a person too small or too dark to resolve a face.
    Proportions are rough anthropometry (a head is roughly an eighth of a
    standing figure), and the point is a comparable box, not an accurate one.
    """
    if box.width <= 0 or box.height <= 0:
        return None
    width = box.width * 0.35
    height = min(width * 1.35, box.height * 0.3)
    return BBox(box.cx - width / 2, box.y1, box.cx + width / 2, box.y1 + height)


def rank_presented_objects(
    detections: list[Detection], width: int, height: int
) -> list[ObjectGuess]:
    """Rank detections by how much each looks like the thing being *shown*.

    "What is this?" is asked while holding something up, so the answer is
    usually central and reasonably large -- while the desk, the chairs and the
    notice board are none of those things but are permanently in shot.

        prominence = centrality^2 * sqrt(area fraction)

    Centrality is squared because it is the stronger signal: people hold things
    up in the middle of the frame, and the whole failure mode to avoid is
    confidently naming a chair at the edge. Area is square-rooted so a phone
    held up still competes with a large background object.

    People are excluded -- the questioner is not the answer.
    """
    if width <= 0 or height <= 0:
        return []

    cx, cy = width / 2.0, height / 2.0
    half_diagonal = math.hypot(cx, cy)
    frame_area = float(width * height)

    guesses: list[ObjectGuess] = []
    for det in detections:
        if det.label == "person" or det.class_id == 0:
            continue
        distance = math.hypot(det.bbox.cx - cx, det.bbox.cy - cy)
        centrality = max(0.0, 1.0 - distance / half_diagonal)
        area_fraction = min(1.0, det.bbox.area / frame_area)
        prominence = (centrality**2) * (area_fraction**0.5)
        guesses.append(
            ObjectGuess(
                label=det.label,
                confidence=det.score,
                bbox=det.bbox,
                prominence=prominence,
            )
        )

    guesses.sort(key=lambda g: g.prominence, reverse=True)
    return guesses


def _timed(detector: Detector, frame) -> tuple[list, float]:
    start = time.perf_counter()
    detections = detector.infer(frame)
    return detections, (time.perf_counter() - start) * 1000.0


def _frame_size(frame) -> tuple[int, int]:
    """(height, width), tolerating the plain tuples the tests hand in."""
    shape = getattr(frame, "shape", None)
    if shape is not None and len(shape) >= 2:
        return int(shape[0]), int(shape[1])
    if isinstance(frame, (tuple, list)) and len(frame) >= 2:
        return int(frame[0]), int(frame[1])
    if isinstance(frame, MockFrame):
        return frame.height, frame.width
    return 0, 0


@dataclass(frozen=True)
class MockFrame:
    """Stand-in for an image when only its dimensions matter."""

    width: int = 640
    height: int = 480

    @property
    def shape(self) -> tuple[int, int, int]:
        return (self.height, self.width, 3)
