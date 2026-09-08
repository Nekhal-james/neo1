"""Detector backends.

Same seam as the admin panel's robot bridge: a real backend and a scriptable one
behind one interface, so the tracking, gesture and engagement logic can be tested
and demonstrated without a model, a camera, or a Pi.

Why a *pose* model for the continuous pass
------------------------------------------
The obvious reading of "person detection plus hand gestures" is two models. On a
Pi 4 that is the wrong shape: a second hand/gesture network roughly doubles the
per-frame cost of a pipeline that only has ~4 fps to begin with.

A single `yolov8n-pose` pass instead yields all three things we need:

* person boxes -- presence, and "someone approached the desk",
* COCO-17 keypoints -- the gesture, derived arithmetically for free,
* face keypoints -- a gaze target at the face rather than the sternum.

Object detection is genuinely a different model, so it runs **on demand** rather
than continuously, and its cost is paid only when something actually asks.
"""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field

from .types import BBox, Detection, Keypoint, Keypoints

log = logging.getLogger(__name__)

PERSON_CLASS_ID = 0


class Detector(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    def infer(self, frame) -> list[Detection]:
        """Run one frame. `frame` is an HxWx3 BGR array for real backends."""

    def warmup(self) -> None:
        """Optional: pay first-inference cost up front, not on the first person."""


@dataclass
class UltralyticsConfig:
    model: str = "yolov8n-pose.pt"
    """Pose model for the continuous pass.

    On the Pi this should point at an NCNN export (`yolo export model=yolov8n-pose.pt
    format=ncnn`), which is several times faster than the PyTorch weights on ARM.
    """

    imgsz: int = 320
    """320 is the Pi 4 working point. 416 roughly doubles the cost."""

    conf: float = 0.35
    iou: float = 0.45
    max_det: int = 8
    """A reception desk is not a crowd scene; capping detections caps the cost of
    the tracking and gesture passes too."""

    device: str = "cpu"


class UltralyticsDetector(Detector):
    name = "ultralytics"

    def __init__(self, config: UltralyticsConfig | None = None) -> None:
        self.cfg = config or UltralyticsConfig()
        self._model = None
        self._pose = "pose" in self.cfg.model.lower()

    def _load(self):
        if self._model is None:
            from ultralytics import YOLO  # imported lazily: heavy, and absent in CI

            log.info("loading detector %s", self.cfg.model)
            self._model = YOLO(self.cfg.model)
        return self._model

    def warmup(self) -> None:
        import numpy as np

        blank = np.zeros((self.cfg.imgsz, self.cfg.imgsz, 3), dtype="uint8")
        self.infer(blank)

    def infer(self, frame) -> list[Detection]:
        model = self._load()
        results = model.predict(
            frame,
            imgsz=self.cfg.imgsz,
            conf=self.cfg.conf,
            iou=self.cfg.iou,
            max_det=self.cfg.max_det,
            classes=[PERSON_CLASS_ID] if self._pose else None,
            device=self.cfg.device,
            verbose=False,
        )
        if not results:
            return []
        return self._convert(results[0])

    def _convert(self, result) -> list[Detection]:
        boxes = getattr(result, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return []

        names = getattr(result, "names", {}) or {}
        xyxy = boxes.xyxy.tolist()
        confs = boxes.conf.tolist()
        classes = [int(c) for c in boxes.cls.tolist()]

        keypoint_rows = None
        kp = getattr(result, "keypoints", None)
        if kp is not None and getattr(kp, "data", None) is not None:
            keypoint_rows = kp.data.tolist()

        detections: list[Detection] = []
        for i, (box, score, cls) in enumerate(zip(xyxy, confs, classes)):
            keypoints = None
            if keypoint_rows is not None and i < len(keypoint_rows):
                keypoints = Keypoints(
                    points=tuple(
                        # Rows are [x, y] when the model carries no visibility
                        # channel, and [x, y, score] when it does.
                        Keypoint(float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 1.0)
                        for p in keypoint_rows[i]
                    )
                )
            detections.append(
                Detection(
                    bbox=BBox(*[float(v) for v in box]),
                    score=float(score),
                    label=str(names.get(cls, cls)),
                    class_id=cls,
                    keypoints=keypoints,
                )
            )
        return detections


@dataclass
class ScriptedFrame:
    """One frame of a synthetic scene."""

    detections: list[Detection] = field(default_factory=list)


class MockDetector(Detector):
    """Replays a scripted scene. Used by the tests and by the panel demo."""

    name = "mock"

    def __init__(self, frames: list[ScriptedFrame] | None = None) -> None:
        self.frames = frames or []
        self.index = 0
        self.calls = 0

    def infer(self, frame) -> list[Detection]:
        self.calls += 1
        if not self.frames:
            return []
        scripted = self.frames[min(self.index, len(self.frames) - 1)]
        self.index += 1
        return list(scripted.detections)


def make_detector(backend: str = "auto", config: UltralyticsConfig | None = None) -> Detector:
    if backend == "mock":
        return MockDetector()
    if backend in ("auto", "ultralytics"):
        try:
            import ultralytics  # noqa: F401
        except ImportError:
            if backend == "ultralytics":
                raise
            log.warning("ultralytics unavailable; using the mock detector")
            return MockDetector()
        return UltralyticsDetector(config)
    raise ValueError(f"unknown detector backend: {backend!r}")


def timed(detector: Detector, frame) -> tuple[list[Detection], float]:
    start = time.perf_counter()
    dets = detector.infer(frame)
    return dets, (time.perf_counter() - start) * 1000.0
