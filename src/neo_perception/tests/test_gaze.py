from __future__ import annotations

import pytest

from neo_perception.gaze import GazeConfig, GazeMapper
from neo_perception.types import (
    AttentionTarget,
    EngagementDecision,
    EngagementState,
    Track,
)

from .conftest import FRAME_H, FRAME_W, person


def decision_for(cx, cy=260.0, *, engaged=True, confidence=1.0, with_pose=True):
    det = person(cx=cx, cy=cy, with_pose=with_pose)
    track = Track(track_id=1, bbox=det.bbox, stamp=0.0, keypoints=det.keypoints, confirmed=True)
    return EngagementDecision(
        state=EngagementState.ENGAGED if engaged else EngagementState.SCANNING,
        target=track,
        track_id=1,
        confidence=confidence,
        engaged=engaged,
        person_present=True,
    )


class TestAiming:
    def test_person_left_of_centre_gives_negative_x(self):
        mapper = GazeMapper()
        target = mapper.to_attention(decision_for(cx=100), FRAME_W, FRAME_H)
        assert target.x < 0

    def test_person_right_of_centre_gives_positive_x(self):
        mapper = GazeMapper()
        target = mapper.to_attention(decision_for(cx=560), FRAME_W, FRAME_H)
        assert target.x > 0

    def test_high_in_frame_gives_positive_y(self):
        """y is positive upward, matching the joystick convention."""
        mapper = GazeMapper()
        high = mapper.to_attention(decision_for(cx=320, cy=120), FRAME_W, FRAME_H)
        low = mapper.to_attention(decision_for(cx=320, cy=400), FRAME_W, FRAME_H)
        assert high.y > low.y

    def test_aim_is_the_face_not_the_box_centre(self):
        mapper = GazeMapper()
        det = person(cx=320, cy=260)
        track = Track(track_id=1, bbox=det.bbox, stamp=0.0, keypoints=det.keypoints)
        ax, ay = mapper.aim_point(track)
        assert ay < det.bbox.cy, "should aim at the head, not the torso centre"

    def test_falls_back_to_the_upper_box_without_keypoints(self):
        mapper = GazeMapper()
        det = person(cx=320, with_pose=False)
        track = Track(track_id=1, bbox=det.bbox, stamp=0.0)
        _, ay = mapper.aim_point(track)
        assert det.bbox.y1 < ay < det.bbox.cy

    def test_output_is_clamped_to_the_unit_range(self):
        mapper = GazeMapper()
        target = mapper.to_attention(decision_for(cx=-500), FRAME_W, FRAME_H)
        assert -1.0 <= target.x <= 1.0


class TestControl:
    def test_centred_target_produces_no_motion(self):
        mapper = GazeMapper()
        command = mapper.to_command(AttentionTarget(x=0.02, y=0.01, confidence=1.0, engaged=True))
        assert command.active is False

    def test_offset_target_drives_the_head(self):
        mapper = GazeMapper()
        command = mapper.to_command(AttentionTarget(x=0.7, y=0.0, confidence=1.0, engaged=True))
        assert command.pan > 0 and command.active is True

    def test_deadband_edge_is_continuous(self):
        """No jump in output as the error crosses the deadband."""
        mapper = GazeMapper(GazeConfig(deadband=0.1))
        just_inside = mapper.to_command(AttentionTarget(x=0.099, confidence=1.0, engaged=True))
        just_outside = mapper.to_command(AttentionTarget(x=0.101, confidence=1.0, engaged=True))
        assert abs(just_outside.pan - just_inside.pan) < 0.02

    def test_no_confidence_means_hold(self):
        """SUSPENDED: hold the last position rather than drift."""
        mapper = GazeMapper()
        command = mapper.to_command(AttentionTarget(x=0.9, confidence=0.0))
        assert command.active is False and command.pan == 0.0

    def test_casual_glances_are_gentler_than_engaged_tracking(self):
        mapper = GazeMapper()
        engaged = mapper.to_command(AttentionTarget(x=0.8, confidence=1.0, engaged=True))
        scanning = mapper.to_command(AttentionTarget(x=0.8, confidence=0.35, engaged=False))
        assert abs(scanning.pan) < abs(engaged.pan)

    def test_rate_is_clamped(self):
        mapper = GazeMapper(GazeConfig(kp=50.0))
        command = mapper.to_command(AttentionTarget(x=1.0, confidence=1.0, engaged=True))
        assert command.pan <= 1.0

    def test_control_loop_converges(self):
        """Closed loop: the camera rides the head, so tracking must settle."""
        mapper = GazeMapper(GazeConfig(kp=0.6, deadband=0.05))
        error = 0.9
        for _ in range(40):
            command = mapper.to_command(
                AttentionTarget(x=error, confidence=1.0, engaged=True)
            )
            error -= command.pan * 0.35  # head moves, person recentres in frame
        assert abs(error) < 0.1, f"did not converge, settled at {error:.3f}"
