"""Scene-building helpers.

The whole point of keeping the perception core free of ROS, OpenCV and numpy is
that a scene can be written as a few lines of Python and stepped frame by frame
with an explicit clock -- no camera, no model, no wall-clock flakiness.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from neo_perception.types import BBox, Detection, Keypoint, Keypoints, KEYPOINT_NAMES

FRAME_W, FRAME_H = 640, 480


def make_keypoints(
    *,
    cx: float,
    cy: float,
    scale: float = 100.0,
    hand_raised: bool = False,
    wrist_dx: float = 0.0,
    score: float = 0.9,
) -> Keypoints:
    """A plausible standing person centred on (cx, cy).

    `scale` is the shoulder-to-hip span in pixels, which is what the gesture
    thresholds are measured against.
    """
    shoulder_y = cy - scale * 0.2
    hip_y = shoulder_y + scale
    half_shoulders = scale * 0.35

    points = {
        "nose": (cx, shoulder_y - scale * 0.35),
        "left_eye": (cx - scale * 0.08, shoulder_y - scale * 0.4),
        "right_eye": (cx + scale * 0.08, shoulder_y - scale * 0.4),
        "left_ear": (cx - scale * 0.15, shoulder_y - scale * 0.36),
        "right_ear": (cx + scale * 0.15, shoulder_y - scale * 0.36),
        "left_shoulder": (cx - half_shoulders, shoulder_y),
        "right_shoulder": (cx + half_shoulders, shoulder_y),
        "left_elbow": (cx - half_shoulders * 1.2, shoulder_y + scale * 0.4),
        "right_elbow": (cx + half_shoulders * 1.2, shoulder_y + scale * 0.4),
        # Arms down by default: wrists well below the shoulders.
        "left_wrist": (cx - half_shoulders * 1.3, shoulder_y + scale * 0.8),
        "right_wrist": (cx + half_shoulders * 1.3, shoulder_y + scale * 0.8),
        "left_hip": (cx - half_shoulders * 0.7, hip_y),
        "right_hip": (cx + half_shoulders * 0.7, hip_y),
        "left_knee": (cx - half_shoulders * 0.7, hip_y + scale * 0.8),
        "right_knee": (cx + half_shoulders * 0.7, hip_y + scale * 0.8),
        "left_ankle": (cx - half_shoulders * 0.7, hip_y + scale * 1.6),
        "right_ankle": (cx + half_shoulders * 0.7, hip_y + scale * 1.6),
    }

    if hand_raised:
        # Right wrist clearly above the shoulder line.
        points["right_wrist"] = (
            cx + half_shoulders * 1.1 + wrist_dx,
            shoulder_y - scale * 0.45,
        )
        points["right_elbow"] = (cx + half_shoulders * 1.2, shoulder_y - scale * 0.02)

    return Keypoints(
        points=tuple(
            Keypoint(points[name][0], points[name][1], score) for name in KEYPOINT_NAMES
        )
    )


def person(
    *,
    cx: float,
    cy: float = 260.0,
    scale: float = 100.0,
    hand_raised: bool = False,
    wrist_dx: float = 0.0,
    score: float = 0.9,
    with_pose: bool = True,
) -> Detection:
    half_w = scale * 0.5
    bbox = BBox(cx - half_w, cy - scale * 0.9, cx + half_w, cy + scale * 1.9)
    return Detection(
        bbox=bbox,
        score=score,
        label="person",
        class_id=0,
        keypoints=make_keypoints(
            cx=cx, cy=cy, scale=scale, hand_raised=hand_raised, wrist_dx=wrist_dx, score=score
        )
        if with_pose
        else None,
    )


@dataclass
class Clock:
    """Explicit time, so nothing in the suite depends on wall-clock timing."""

    now: float = 0.0

    def tick(self, dt: float = 0.25) -> float:
        self.now += dt
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def frame():
    from neo_perception.pipeline import MockFrame

    return MockFrame(FRAME_W, FRAME_H)
