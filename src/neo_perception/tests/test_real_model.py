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


class TestRealObjectIdentification:
    """The identify path against the real object model, not the pose one."""

    @pytest.fixture(scope="class")
    @classmethod
    def object_detections(cls):
        from neo_perception.detector import ObjectDetector, ObjectDetectorConfig

        try:
            return ObjectDetector(ObjectDetectorConfig(imgsz=416)).infer(SAMPLE)
        except Exception as exc:
            pytest.skip(f"object model unavailable: {exc}")

    def test_the_object_model_labels_real_things(self, object_detections):
        assert object_detections
        assert any(d.label == "bus" for d in object_detections)

    def test_people_are_stripped_from_the_answer(self, object_detections):
        from neo_perception.pipeline import rank_presented_objects

        assert any(d.label == "person" for d in object_detections), "sample has people"
        ranked = rank_presented_objects(object_detections, 810, 1080)
        assert ranked, "something should survive ranking"
        assert all(g.label != "person" for g in ranked)

    def test_the_dominant_object_ranks_first(self, object_detections):
        from neo_perception.pipeline import rank_presented_objects

        ranked = rank_presented_objects(object_detections, 810, 1080)
        assert ranked[0].label == "bus"


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


class TestRealHeadBoxes:
    """Head boxes on real poses, where the degenerate cases actually appeared.

    Synthetic skeletons are always facing forward with well-separated ears, so
    they never produce the failure this guards: edge-on, the two ears land
    almost on top of each other and their span silently becomes a couple of
    pixels, yielding a 1x2 px "head" that matches nothing and destroys the
    track.
    """

    def test_every_real_detection_yields_a_usable_head(self, real_detections):
        for det in real_detections:
            head = det.keypoints.head_box(det.bbox)
            if head is None:
                continue  # a legitimate answer; callers fall back themselves
            assert head.width > 8.0 and head.height > 8.0, (
                f"degenerate {head.width:.0f}x{head.height:.0f} head box"
            )

    def test_head_proportions_are_anatomical(self, real_detections):
        """Not merely non-degenerate: the right fraction of the person."""
        checked = 0
        for det in real_detections:
            head = det.keypoints.head_box(det.bbox)
            if head is None or det.bbox.width <= 0:
                continue
            ratio = head.width / det.bbox.width
            assert 0.10 < ratio < 0.90, f"head is {ratio:.2f} of the person box"
            checked += 1
        assert checked > 0, "expected at least one head on real data"

    def test_the_head_sits_inside_the_upper_person_box(self, real_detections):
        for det in real_detections:
            head = det.keypoints.head_box(det.bbox)
            if head is None:
                continue
            assert det.bbox.x1 - head.width <= head.cx <= det.bbox.x2 + head.width
            assert head.cy < det.bbox.y1 + det.bbox.height * 0.6, (
                "a head below the midline of the person means it latched onto a torso"
            )

    def test_the_pipeline_tracks_heads_not_bodies(self, real_detections):
        """The invariant that matters: no person box may survive among heads.

        Salience is largest-box-wins, so a single un-narrowed person box left in
        the mix is several times the area of every head and would always win.
        """
        from neo_perception.detector import MockDetector, ScriptedFrame
        from neo_perception.pipeline import MockFrame

        pipeline = PerceptionPipeline(
            PipelineConfig(track_head=True, tracker=TrackerConfig(min_hits=1)),
            MockDetector([ScriptedFrame(list(real_detections))]),
        )
        frame = MockFrame(810, 1080)
        result = pipeline.process(frame, 0.0)

        assert result.tracks, "expected tracked heads"
        for track in result.tracks:
            if track.person_bbox is None:
                continue
            assert track.bbox.area < track.person_bbox.area * 0.5, (
                "a full person box survived into the tracker"
            )
