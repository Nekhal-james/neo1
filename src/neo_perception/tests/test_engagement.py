"""The engagement state machine: lock on a gesture, auto-unlink when they leave.

This is the behaviour the whole feature exists for, so it is tested through the
full pipeline (detector -> tracker -> gestures -> engagement) rather than against
the controller in isolation -- a lock that works on synthetic tracks but not on
tracker output would be worthless.
"""

from __future__ import annotations

import pytest

from neo_perception.detector import MockDetector, ScriptedFrame
from neo_perception.engagement import EngagementConfig
from neo_perception.gestures import GestureConfig
from neo_perception.pipeline import PerceptionPipeline, PipelineConfig
from neo_perception.tracker import TrackerConfig
from neo_perception.types import EngagementState, GestureKind

from .conftest import FRAME_H, FRAME_W, person

DT = 0.25  # 4 fps, the Pi working point


def build(**engagement_overrides) -> tuple[PerceptionPipeline, MockDetector]:
    detector = MockDetector()
    cfg = PipelineConfig(
        tracker=TrackerConfig(min_hits=2, max_age_s=3.0),
        gestures=GestureConfig(),
        engagement=EngagementConfig(
            confirm_s=0.5, exit_grace_s=0.6, occlusion_grace_s=2.0, **engagement_overrides
        ),
    )
    return PerceptionPipeline(cfg, detector=detector), detector


def feed(pipeline, detector, detections, clock, frame, dt=DT):
    detector.frames = [ScriptedFrame(detections)]
    detector.index = 0
    return pipeline.process(frame, clock.tick(dt))


class TestLockOn:
    def test_presence_alone_does_not_engage(self, clock, frame):
        pipeline, detector = build()
        for _ in range(8):
            result = feed(pipeline, detector, [person(cx=320)], clock, frame)
        assert result.attention.person_present is True
        assert result.attention.engaged is False
        assert result.attention.state is EngagementState.SCANNING

    def test_raised_hand_engages_after_confirmation(self, clock, frame):
        pipeline, detector = build()
        for _ in range(3):  # establish and confirm the track
            feed(pipeline, detector, [person(cx=320)], clock, frame)

        result = feed(pipeline, detector, [person(cx=320, hand_raised=True)], clock, frame)
        assert result.attention.state is EngagementState.ENGAGING, "should confirm first"
        assert result.attention.engaged is False

        for _ in range(3):
            result = feed(pipeline, detector, [person(cx=320, hand_raised=True)], clock, frame)
        assert result.attention.state is EngagementState.ENGAGED
        assert result.attention.engaged is True
        assert result.attention.track_id is not None

    def test_a_brief_arm_raise_does_not_engage(self, clock, frame):
        """Someone scratching their head must not capture the robot."""
        pipeline, detector = build()
        for _ in range(3):
            feed(pipeline, detector, [person(cx=320)], clock, frame)

        feed(pipeline, detector, [person(cx=320, hand_raised=True)], clock, frame)
        result = feed(pipeline, detector, [person(cx=320)], clock, frame)
        assert result.attention.state is EngagementState.SCANNING
        assert result.attention.engaged is False

    def test_engagement_survives_the_hand_coming_down(self, clock, frame):
        """The gesture asks for attention; it is not a dead-man switch."""
        pipeline, detector = build()
        engage(pipeline, detector, clock, frame, cx=320)

        for _ in range(6):
            result = feed(pipeline, detector, [person(cx=320)], clock, frame)
        assert result.attention.engaged is True

    def test_nearest_gesturing_person_wins(self, clock, frame):
        pipeline, detector = build()
        far = dict(cx=180, scale=60)
        near = dict(cx=460, scale=140)
        for _ in range(3):
            feed(pipeline, detector, [person(**far), person(**near)], clock, frame)
        for _ in range(4):
            result = feed(
                pipeline,
                detector,
                [person(**far, hand_raised=True), person(**near, hand_raised=True)],
                clock,
                frame,
            )

        assert result.attention.engaged is True
        engaged_track = next(
            t for t in result.tracks if t.track_id == result.attention.track_id
        )
        assert engaged_track.bbox.cx == pytest.approx(460, abs=1)

    def test_a_second_person_cannot_steal_the_lock(self, clock, frame):
        pipeline, detector = build()
        engage(pipeline, detector, clock, frame, cx=200)
        locked_id = pipeline.engagement.target_id

        for _ in range(6):
            result = feed(
                pipeline,
                detector,
                [person(cx=200), person(cx=480, scale=160, hand_raised=True)],
                clock,
                frame,
            )
        assert result.attention.track_id == locked_id


class TestAutoUnlink:
    def test_walking_out_of_frame_releases_quickly(self, clock, frame):
        """The ask: auto-unlink once the person is out of the frame."""
        pipeline, detector = build()
        engage(pipeline, detector, clock, frame, cx=320)

        # Walk to the right edge, so the last box touches the border.
        for cx in (420, 520, 600, 636):
            result = feed(pipeline, detector, [person(cx=cx)], clock, frame)
        assert result.attention.engaged is True, "still visible at the edge"

        # Now gone. exit_grace_s is 0.6, so ~3 empty frames at 4 fps.
        result = feed(pipeline, detector, [], clock, frame)
        assert result.attention.state is EngagementState.SUSPENDED

        for _ in range(3):
            result = feed(pipeline, detector, [], clock, frame)
        assert result.attention.state is EngagementState.SCANNING
        assert result.attention.engaged is False
        assert pipeline.engagement.last_release_reason == "left the frame"

    def test_a_detector_blink_mid_frame_does_not_release(self, clock, frame):
        """A dropped frame in open view must not break the lock.

        At 3-5 fps the detector misses people regularly; releasing on the first
        miss would make engagement useless.
        """
        pipeline, detector = build()
        engage(pipeline, detector, clock, frame, cx=320)

        feed(pipeline, detector, [], clock, frame)  # one missed frame
        result = feed(pipeline, detector, [person(cx=320)], clock, frame)
        assert result.attention.state is EngagementState.ENGAGED
        assert result.attention.engaged is True

    def test_occlusion_gets_a_longer_grace_than_an_exit(self, clock, frame):
        """Same disappearance, different cause, deliberately different timing."""
        pipeline, detector = build()
        engage(pipeline, detector, clock, frame, cx=320)  # mid-frame, not at an edge

        # Past the exit grace (0.6 s) but inside the occlusion grace (2.0 s).
        for _ in range(3):
            result = feed(pipeline, detector, [], clock, frame)
        assert result.attention.state is EngagementState.SUSPENDED, (
            "a person occluded mid-frame should still be held"
        )

        result = feed(pipeline, detector, [person(cx=320)], clock, frame)
        assert result.attention.state is EngagementState.ENGAGED

    def test_occlusion_grace_eventually_expires(self, clock, frame):
        pipeline, detector = build()
        engage(pipeline, detector, clock, frame, cx=320)

        for _ in range(12):
            result = feed(pipeline, detector, [], clock, frame)
        assert result.attention.state is EngagementState.SCANNING
        assert pipeline.engagement.last_release_reason == "lost from view"

    def test_after_release_a_new_person_can_engage(self, clock, frame):
        pipeline, detector = build()
        engage(pipeline, detector, clock, frame, cx=320)
        for _ in range(12):
            feed(pipeline, detector, [], clock, frame)
        assert pipeline.engagement.state is EngagementState.SCANNING

        engage(pipeline, detector, clock, frame, cx=200)
        assert pipeline.engagement.state is EngagementState.ENGAGED

    def test_suspended_reports_no_aim_point(self, clock, frame):
        """Nothing to aim at means hold, not drift toward centre or someone else."""
        pipeline, detector = build()
        engage(pipeline, detector, clock, frame, cx=320)
        result = feed(pipeline, detector, [], clock, frame)

        assert result.attention.state is EngagementState.SUSPENDED
        assert result.attention.confidence == 0.0
        assert pipeline.last_command.active is False

    def test_manual_release(self, clock, frame):
        pipeline, detector = build()
        engage(pipeline, detector, clock, frame, cx=320)
        pipeline.release("panel override")

        result = feed(pipeline, detector, [person(cx=320)], clock, frame)
        assert result.attention.engaged is False
        assert pipeline.engagement.last_release_reason == "panel override"

    def test_max_engage_timeout(self, clock, frame):
        pipeline, detector = build(max_engage_s=1.0)
        engage(pipeline, detector, clock, frame, cx=320)

        for _ in range(8):
            result = feed(pipeline, detector, [person(cx=320)], clock, frame)
        assert result.attention.engaged is False
        assert "max engagement" in pipeline.engagement.last_release_reason


class TestConfigSanity:
    def test_grace_longer_than_tracker_memory_is_reported(self):
        from neo_perception.engagement import EngagementController

        controller = EngagementController(EngagementConfig(occlusion_grace_s=5.0))
        problems = controller.validate_against_tracker(TrackerConfig(max_age_s=2.5))
        assert problems, "a grace the tracker cannot honour should be flagged"
        assert "max_age_s" in problems[0]

    def test_default_config_is_self_consistent(self):
        from neo_perception.engagement import EngagementController

        controller = EngagementController()
        assert controller.validate_against_tracker(TrackerConfig()) == []


def engage(pipeline, detector, clock, frame, *, cx: float) -> None:
    """Drive a person to a confirmed lock."""
    for _ in range(3):
        feed(pipeline, detector, [person(cx=cx)], clock, frame)
    for _ in range(4):
        feed(pipeline, detector, [person(cx=cx, hand_raised=True)], clock, frame)
    assert pipeline.engagement.state is EngagementState.ENGAGED, "setup failed to engage"
