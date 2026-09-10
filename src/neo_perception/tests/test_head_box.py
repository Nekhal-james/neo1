"""Head finding and head tracking.

The reception-desk case: people stand close, so the *person* box is clipped by
the frame and its centre lands on a torso filling the view. The head stays whole
and is what the pan/tilt assembly aims at. It costs no extra inference -- nose,
eyes and ears come from the pose pass already running.
"""

from __future__ import annotations

import pytest

from neo_perception.detector import MockDetector, ScriptedFrame
from neo_perception.pipeline import PerceptionPipeline, PipelineConfig
from neo_perception.tracker import TrackerConfig
from neo_perception.types import BBox, Detection, Keypoint, Keypoints, KEYPOINT_NAMES

from .conftest import make_keypoints, person


def kps_with(**overrides) -> Keypoints:
    """Keypoints with named points forced to a position/score."""
    base = make_keypoints(cx=320, cy=260, scale=100)
    pts = list(base.points)
    for name, value in overrides.items():
        pts[KEYPOINT_NAMES.index(name)] = value
    return Keypoints(points=tuple(pts))


class TestHeadGeometry:
    def test_a_head_is_found_on_a_normal_pose(self):
        det = person(cx=320)
        head = det.keypoints.head_box(det.bbox)
        assert head is not None
        assert head.width > 0 and head.height > 0

    def test_the_head_sits_inside_the_upper_person_box(self):
        det = person(cx=320)
        head = det.keypoints.head_box(det.bbox)
        assert head.cy < det.bbox.cy, "head must be above the person box centre"
        assert head.area < det.bbox.area * 0.5, "a head is not half a person"

    def test_the_head_is_centred_on_the_face(self):
        det = person(cx=320)
        head = det.keypoints.head_box(det.bbox)
        face = det.keypoints.face_anchor()
        assert head.x1 <= face[0] <= head.x2
        assert head.y1 <= face[1] <= head.y2

    def test_head_scales_with_the_person(self):
        small = person(cx=320, scale=50).keypoints.head_box(person(cx=320, scale=50).bbox)
        big = person(cx=320, scale=200).keypoints.head_box(person(cx=320, scale=200).bbox)
        assert big.width > small.width * 2

    def test_a_turned_away_person_still_gets_a_head(self):
        """No face keypoints at all -- the shoulders still give a scale."""
        det = person(cx=320, facing="away")
        assert det.keypoints.has_face() is False
        assert det.keypoints.head_box(det.bbox) is not None

    def test_no_keypoints_but_a_person_box_gives_a_head_shaped_guess(self):
        """Better than nothing: a comparable box keeps salience honest.

        Leaving this as None would put a full person box into a tracker
        otherwise holding heads, and it would win "nearest" every time.
        """
        head = Keypoints(points=()).head_box(BBox(0, 0, 100, 300))
        assert head is not None
        assert head.width < 100, "narrower than the person"
        assert head.cy < 150, "in the upper half"

    def test_nothing_at_all_yields_nothing(self):
        assert Keypoints(points=()).head_box(None) is None


class TestDegenerateInputs:
    """Every one of these produced a broken box before it was guarded."""

    def test_collapsed_ears_do_not_produce_a_pixel_sized_head(self):
        """Turned away, the two ear keypoints land almost on top of each other.

        Taking that span as head width gave a 1x2 px box, which then matched no
        detection and silently destroyed the track.
        """
        kps = kps_with(
            left_ear=Keypoint(320.0, 200.0, 0.9),
            right_ear=Keypoint(321.5, 200.0, 0.9),
            nose=Keypoint(320.0, 205.0, 0.1),
            left_eye=Keypoint(0.0, 0.0, 0.0),
            right_eye=Keypoint(0.0, 0.0, 0.0),
        )
        head = kps.head_box(BBox(270, 170, 370, 470))
        assert head is not None
        assert head.width > 10, f"degenerate ear span leaked through: {head.width}"

    def test_a_collapsed_shoulder_span_does_not_veto_every_candidate(self):
        """A bad scale *reference* is worse than a bad candidate.

        With shoulders 3 px apart, the plausibility check rejected the perfectly
        good person-box estimate as "far too wide" and returned no head at all.
        """
        kps = kps_with(
            left_shoulder=Keypoint(320.0, 240.0, 0.9),
            right_shoulder=Keypoint(323.0, 240.0, 0.9),
            nose=Keypoint(0.0, 0.0, 0.0),
            left_eye=Keypoint(0.0, 0.0, 0.0),
            right_eye=Keypoint(0.0, 0.0, 0.0),
            left_ear=Keypoint(0.0, 0.0, 0.0),
            right_ear=Keypoint(0.0, 0.0, 0.0),
        )
        person_box = BBox(250, 170, 390, 640)
        head = kps.head_box(person_box)
        assert head is not None, "a degenerate shoulder span must not veto the fallback"
        assert head.width > 0.1 * person_box.width

    def test_a_head_is_never_wider_than_the_person(self):
        det = person(cx=320)
        head = det.keypoints.head_box(det.bbox)
        assert head.width < det.bbox.width


class TestHeadTracking:
    def build(self, track_head=True, **tracker_kwargs):
        detector = MockDetector()
        cfg = PipelineConfig(
            tracker=TrackerConfig(min_hits=1, **tracker_kwargs),
            track_head=track_head,
        )
        return PerceptionPipeline(cfg, detector), detector

    def feed(self, pipeline, detector, dets, clock, frame):
        detector.frames = [ScriptedFrame(dets)]
        detector.index = 0
        return pipeline.process(frame, clock.tick())

    def test_tracks_carry_the_head_not_the_person(self, clock, frame):
        pipeline, detector = self.build()
        result = self.feed(pipeline, detector, [person(cx=320)], clock, frame)
        t = result.tracks[0]

        assert t.person_bbox is not None, "the person box should be kept alongside"
        assert t.bbox.area < t.person_bbox.area, "the tracked box must be the head"

    def test_person_tracking_can_still_be_selected(self, clock, frame):
        pipeline, detector = self.build(track_head=False)
        result = self.feed(pipeline, detector, [person(cx=320)], clock, frame)
        t = result.tracks[0]

        assert t.person_bbox is None
        assert t.bbox.area == pytest.approx(person(cx=320).bbox.area)

    def test_identity_survives_walking_across_the_frame(self, clock, frame):
        """Head boxes are ~4x smaller, so the motion gate has to keep up."""
        pipeline, detector = self.build()
        ids = set()
        for cx in range(200, 420, 20):
            result = self.feed(pipeline, detector, [person(cx=cx)], clock, frame)
            ids.update(t.track_id for t in result.tracks)
        assert ids == {1}, f"head track fragmented into {ids}"

    def test_two_people_keep_separate_head_tracks(self, clock, frame):
        pipeline, detector = self.build()
        for i in range(5):
            result = self.feed(
                pipeline, detector,
                [person(cx=180 + i * 6), person(cx=470 - i * 6)],
                clock, frame,
            )
        assert len({t.track_id for t in result.tracks}) == 2

    def test_every_tracked_box_is_a_head_even_without_keypoints(self, clock, frame):
        """Mixed semantics break salience: one person box among head boxes is
        several times the area of any of them and would always win "nearest"."""
        pipeline, detector = self.build()
        result = self.feed(
            pipeline, detector,
            [person(cx=200), person(cx=430, with_pose=False)],
            clock, frame,
        )
        areas = [t.bbox.area for t in result.tracks]
        assert max(areas) < 4 * min(areas), f"box areas not comparable: {areas}"

    def test_the_nearest_head_wins_salience(self, clock, frame):
        pipeline, detector = self.build()
        for _ in range(3):
            result = self.feed(
                pipeline, detector,
                [person(cx=180, scale=60), person(cx=460, scale=160)],
                clock, frame,
            )
        near = max(result.tracks, key=lambda t: t.bbox.area)
        assert near.bbox.cx > 400, "the closer (bigger-headed) person should win"


class TestGazeOnHeads:
    def test_aim_is_the_head_centre_without_face_keypoints(self):
        from neo_perception.gaze import GazeMapper
        from neo_perception.types import Track

        head = BBox(300, 180, 360, 260)
        track = Track(track_id=1, bbox=head, stamp=0.0,
                      person_bbox=BBox(270, 170, 390, 640))
        ax, ay = GazeMapper().aim_point(track)

        assert (ax, ay) == pytest.approx((head.cx, head.cy)), (
            "with the head already isolated, the upper-third rule would aim at "
            "the forehead"
        )

    def test_face_keypoints_still_win_when_present(self):
        from neo_perception.gaze import GazeMapper
        from neo_perception.types import Track

        det = person(cx=320)
        track = Track(track_id=1, bbox=det.keypoints.head_box(det.bbox), stamp=0.0,
                      keypoints=det.keypoints, person_bbox=det.bbox)
        assert GazeMapper().aim_point(track) == pytest.approx(
            det.keypoints.face_anchor()
        )
