"""Frame in, attention out.

    frame -> detector -> tracker -> gestures -> engagement -> gaze -> command

`AsyncPerception` adds the threading policy the Pi needs: inference on a worker
thread with a **single-slot, latest-wins** input. Frames are dropped, never
queued. A queued backlog would make the head chase where somebody used to be,
which looks far worse than a lower frame rate.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from .detector import Detector, MockDetector, UltralyticsConfig, make_detector
from .engagement import EngagementConfig, EngagementController
from .gaze import GazeCommand, GazeConfig, GazeMapper
from .gestures import GestureConfig, GestureRecognizer
from .tracker import MultiTracker, TrackerConfig
from .types import PerceptionResult

log = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    detector_backend: str = "auto"
    detector: UltralyticsConfig = field(default_factory=UltralyticsConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    gestures: GestureConfig = field(default_factory=GestureConfig)
    engagement: EngagementConfig = field(default_factory=EngagementConfig)
    gaze: GazeConfig = field(default_factory=GazeConfig)

    target_fps: float = 4.0
    """Inference rate ceiling. Defaults to the Pi 4 working point; raise it on a
    machine that can afford more."""


class PerceptionPipeline:
    """Synchronous pipeline. One call, one frame, deterministic -- so the whole
    behaviour can be driven from tests with no camera and no clock."""

    def __init__(self, config: PipelineConfig | None = None, detector: Detector | None = None) -> None:
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

    def process(self, frame, stamp: float | None = None) -> PerceptionResult:
        stamp = time.monotonic() if stamp is None else stamp
        height, width = _frame_size(frame)

        detections, inference_ms = _timed(self.detector, frame)
        persons = [d for d in detections if d.label == "person" or d.class_id == 0]

        tracks = self.tracker.update(persons, stamp)
        visible = self.tracker.visible_tracks(stamp)
        gesture_map, gesture_events = self.gestures.update(visible, stamp)
        decision = self.engagement.update(visible, gesture_map, stamp, width, height)
        attention = self.gaze.to_attention(decision, width, height)
        self.last_command = self.gaze.to_command(attention)

        self.last_result = PerceptionResult(
            stamp=stamp,
            width=width,
            height=height,
            tracks=list(tracks),
            gestures=gesture_events,
            attention=attention,
            inference_ms=inference_ms,
        )
        return self.last_result

    def release(self, reason: str = "released") -> None:
        self.engagement.release(reason)

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
