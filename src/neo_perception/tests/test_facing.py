"""Which way a person is turned, and the lock release that depends on it.

COCO labels sides from the *person's* own frame, so facing is recoverable from
keypoints alone -- no extra model, no face-recognition, nothing that costs the Pi
another inference pass.
"""

from __future__ import annotations

import pytest

from neo_perception.detector import MockDetector, ScriptedFrame
from neo_perception.engagement import EngagementConfig
from neo_perception.pipeline import PerceptionPipeline, PipelineConfig
from neo_perception.tracker import TrackerConfig
from neo_perception.types import EngagementState, FacingState

from .conftest import make_keypoints, person

DT = 0.25


class TestFacingFromKeypoints:
    def test_facing_the_camera(self):
        kps = make_keypoints(cx=320, cy=260, facing="facing")
        assert kps.facing() is FacingState.FACING

    def test_turned_away(self):
        kps = make_keypoints(cx=320, cy=260, facing="away")
        assert kps.facing() is FacingState.AWAY

    def test_edge_on_is_profile_not_away(self):
        """Looking sideways is not the same as turning your back."""
        kps = make_keypoints(cx=320, cy=260, facing="profile")
        assert kps.facing() is FacingState.PROFILE

    def test_no_shoulders_is_unknown(self):
        kps = make_keypoints(cx=320, cy=260, score=0.05)
        assert kps.facing() is FacingState.UNKNOWN

    def test_ears_alone_do_not_count_as_a_face(self):
        """Ears stay visible from behind -- the exact confusion to avoid."""
        kps = make_keypoints(cx=320, cy=260, facing="away")
        assert kps.get("left_ear") is not None
        assert kps.has_face() is False

    def test_face_visible_when_facing(self):
        assert make_keypoints(cx=320, cy=260, facing="facing").has_face() is True

    def test_facing_is_scale_invariant(self):
        for scale in (40.0, 100.0, 260.0):
            assert (
                make_keypoints(cx=320, cy=260, scale=scale, facing="facing").facing()
                is FacingState.FACING
            )
            assert (
                make_keypoints(cx=320, cy=260, scale=scale, facing="away").facing()
                is FacingState.AWAY
            )


class TestReleaseOnTurningAway:
    """The ask: untrack when the person turns their back on the robot.

    Nothing in the frame-exit or occlusion logic catches this -- the person is
    perfectly visible and perfectly tracked the whole time.
    """

    def build(self, **overrides):
        detector = MockDetector()
        cfg = PipelineConfig(
            tracker=TrackerConfig(min_hits=2, max_age_s=3.0),
            engagement=EngagementConfig(
                confirm_s=0.5, turned_away_grace_s=1.0, **overrides
            ),
        )
        return PerceptionPipeline(cfg, detector), detector

    def feed(self, pipeline, detector, detections, clock, frame):
        detector.frames = [ScriptedFrame(detections)]
        detector.index = 0
        return pipeline.process(frame, clock.tick(DT))

    def engage(self, pipeline, detector, clock, frame, cx=320):
        for _ in range(3):
            self.feed(pipeline, detector, [person(cx=cx)], clock, frame)
        for _ in range(4):
            self.feed(pipeline, detector, [person(cx=cx, hand_raised=True)], clock, frame)
        assert pipeline.engagement.state is EngagementState.ENGAGED, "setup failed"

    def test_turning_away_releases_the_lock(self, clock, frame):
        pipeline, detector = self.build()
        self.engage(pipeline, detector, clock, frame)

        for _ in range(8):
            result = self.feed(
                pipeline, detector, [person(cx=320, facing="away")], clock, frame
            )

        assert result.attention.engaged is False
        assert result.attention.state is EngagementState.SCANNING
        assert pipeline.engagement.last_release_reason == "turned away"
        assert result.attention.person_present is True, "still visible, just released"

    def test_a_brief_glance_away_does_not_release(self, clock, frame):
        """People turn away constantly mid-conversation. One frame is not leaving."""
        pipeline, detector = self.build()
        self.engage(pipeline, detector, clock, frame)

        self.feed(pipeline, detector, [person(cx=320, facing="away")], clock, frame)
        result = self.feed(pipeline, detector, [person(cx=320)], clock, frame)
        assert result.attention.engaged is True

    def test_profile_never_releases(self, clock, frame):
        """Looking down a corridor -- including at what the robot is looking at."""
        pipeline, detector = self.build()
        self.engage(pipeline, detector, clock, frame)

        for _ in range(12):
            result = self.feed(
                pipeline, detector, [person(cx=320, facing="profile")], clock, frame
            )
        assert result.attention.engaged is True

    def test_the_away_timer_resets_on_turning_back(self, clock, frame):
        pipeline, detector = self.build()
        self.engage(pipeline, detector, clock, frame)

        for _ in range(6):
            self.feed(pipeline, detector, [person(cx=320, facing="away")], clock, frame)
            result = self.feed(pipeline, detector, [person(cx=320)], clock, frame)
        assert result.attention.engaged is True, "alternating should never accumulate"

    def test_after_turning_away_someone_else_can_engage(self, clock, frame):
        pipeline, detector = self.build()
        self.engage(pipeline, detector, clock, frame)
        for _ in range(8):
            self.feed(pipeline, detector, [person(cx=320, facing="away")], clock, frame)
        assert pipeline.engagement.state is EngagementState.SCANNING

        for _ in range(6):
            result = self.feed(
                pipeline, detector, [person(cx=180, hand_raised=True)], clock, frame
            )
        assert result.attention.engaged is True

    def test_facing_is_published_on_tracks(self, clock, frame):
        pipeline, detector = self.build()
        for _ in range(3):
            result = self.feed(pipeline, detector, [person(cx=320)], clock, frame)
        assert result.tracks[0].facing is FacingState.FACING

    def test_a_person_with_no_pose_does_not_release(self, clock, frame):
        """A plain detect model gives boxes only: UNKNOWN must not read as AWAY."""
        pipeline, detector = self.build()
        self.engage(pipeline, detector, clock, frame)

        for _ in range(10):
            result = self.feed(
                pipeline, detector, [person(cx=320, with_pose=False)], clock, frame
            )
        assert result.attention.engaged is True
