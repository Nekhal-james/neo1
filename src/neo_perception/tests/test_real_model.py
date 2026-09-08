"""Integration against a real YOLO pose model.

The unit tests use idealised synthetic skeletons. These run the actual model, so
they catch the assumptions that synthetic data cannot: keypoint ordering, the
shape of the tensor ultralytics returns, real confidence scores, and real
proportions.

Skipped automatically when ultralytics or the weights are unavailable, so CI and
the Pi can run the rest of the suite without them.
"""

from __future__ import annotations

import pytest

from neo_perception.engagement import EngagementConfig
from neo_perception.pipeline import PerceptionPipeline, PipelineConfig
from neo_perception.tracker import TrackerConfig
from neo_perception.types import (
    KEYPOINT_NAMES,
    Detection,
    EngagementState,
    GestureKind,
    Keypoint,
    Keypoints,
)

ultralytics = pytest.importorskip("ultralytics", reason="needs the real detector")

SAMPLE = "https://ultralytics.com/images/bus.jpg"


@pytest.fixture(scope="module")
def real_detections():
    from neo_perception.detector import UltralyticsConfig, UltralyticsDetector

    detector = UltralyticsDetector(UltralyticsConfig(model="yolov8n-pose.pt", imgsz=320))
    try:
        return detector.infer(SAMPLE)
    except Exception as exc:  # no weights, no network
        pytest.skip(f"real model unavailable: {exc}")


def test_model_produces_people_with_poses(real_detections):
    assert real_detections, "expected people in the sample image"
    assert all(d.label == "person" for d in real_detections)
    assert all(d.keypoints is not None for d in real_detections)


def test_keypoint_layout_is_coco17(real_detections):
    """The gesture code indexes by this layout; if it changes, gestures break."""
    for det in real_detections:
        assert len(det.keypoints.points) == len(KEYPOINT_NAMES)


def test_derived_measures_work_on_real_poses(real_detections):
    """torso_scale and face_anchor must resolve on real data, not just synthetic."""
    usable = 0
    for det in real_detections:
        if det.keypoints.torso_scale() is not None and det.keypoints.face_anchor():
            usable += 1
    assert usable > 0


def test_arms_down_is_not_read_as_a_gesture(real_detections):
    """Negative case on real geometry: standing people must not engage the robot."""
    from neo_perception.detector import MockDetector, ScriptedFrame
    from neo_perception.pipeline import MockFrame

    pipeline = PerceptionPipeline(
        PipelineConfig(tracker=TrackerConfig(min_hits=1)),
        MockDetector([ScriptedFrame(list(real_detections))]),
    )
    frame = MockFrame(810, 1080)
    for i in range(8):
        result = pipeline.process(frame, i * 0.25)

    assert result.person_count > 0, "people should be tracked"
    assert result.attention.engaged is False
    assert result.attention.state is EngagementState.SCANNING


def test_raising_a_real_persons_wrist_engages(real_detections):
    """Positive case built from real keypoints rather than an idealised skeleton."""
    from neo_perception.detector import MockDetector, ScriptedFrame
    from neo_perception.pipeline import MockFrame

    subject = max(real_detections, key=lambda d: d.bbox.area)
    raised = _raise_wrist(subject)

    pipeline = PerceptionPipeline(
        PipelineConfig(
            tracker=TrackerConfig(min_hits=1),
            engagement=EngagementConfig(confirm_s=0.5),
        ),
        MockDetector(),
    )
    frame = MockFrame(810, 1080)
    detector = pipeline.detector

    for i in range(10):
        detector.frames = [ScriptedFrame([raised])]
        detector.index = 0
        result = pipeline.process(frame, i * 0.25)

    assert result.attention.engaged is True
    assert result.attention.state is EngagementState.ENGAGED


def _raise_wrist(detection: Detection) -> Detection:
    """Lift the right wrist above the shoulder, leaving everything else real."""
    kps = detection.keypoints
    shoulder = kps.get("right_shoulder", 0.0)
    scale = kps.torso_scale() or 100.0
    points = list(kps.points)
    idx = KEYPOINT_NAMES.index("right_wrist")
    points[idx] = Keypoint(shoulder.x, shoulder.y - scale * 0.5, 0.9)
    return Detection(
        bbox=detection.bbox,
        score=detection.score,
        label=detection.label,
        class_id=detection.class_id,
        keypoints=Keypoints(points=tuple(points)),
    )
