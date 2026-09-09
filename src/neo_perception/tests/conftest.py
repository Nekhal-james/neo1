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
    arm_out: bool = False,
    wrist_dx: float = 0.0,
    score: float = 0.9,
    facing: str = "facing",
) -> Keypoints:
    """A plausible standing person centred on (cx, cy).

    `scale` is the shoulder-to-hip span in pixels, which is what the gesture
    thresholds are measured against.

    `facing` is one of "facing", "away", "profile". COCO labels sides from the
    *person's* own frame, so someone facing the camera has their left shoulder
    on the image's **right** -- getting this backwards is exactly the mistake
    `Keypoints.facing` exists to detect, so the fixture must model it properly.

    Poses:
      hand_raised  hand up with a vertical forearm -> OPEN_PALM
      arm_out      hand up but the arm reaching sideways -> RAISED_HAND only
    """
    shoulder_y = cy - scale * 0.2
    hip_y = shoulder_y + scale

    if facing == "profile":
        # Edge-on: the shoulders nearly overlap in x.
        half_shoulders, side = scale * 0.08, 1.0
    else:
        # +1 puts the person's left on the image right, i.e. facing us.
        half_shoulders, side = scale * 0.35, (1.0 if facing == "facing" else -1.0)

    def left(offset: float) -> float:
        return cx + side * offset

    def right(offset: float) -> float:
        return cx - side * offset

    points = {
        "nose": (cx, shoulder_y - scale * 0.35),
        "left_eye": (left(scale * 0.08), shoulder_y - scale * 0.4),
        "right_eye": (right(scale * 0.08), shoulder_y - scale * 0.4),
        "left_ear": (left(scale * 0.15), shoulder_y - scale * 0.36),
        "right_ear": (right(scale * 0.15), shoulder_y - scale * 0.36),
        "left_shoulder": (left(half_shoulders), shoulder_y),
        "right_shoulder": (right(half_shoulders), shoulder_y),
        "left_elbow": (left(half_shoulders * 1.2), shoulder_y + scale * 0.4),
        "right_elbow": (right(half_shoulders * 1.2), shoulder_y + scale * 0.4),
        # Arms down by default: wrists well below the shoulders.
        "left_wrist": (left(half_shoulders * 1.3), shoulder_y + scale * 0.8),
        "right_wrist": (right(half_shoulders * 1.3), shoulder_y + scale * 0.8),
        "left_hip": (left(half_shoulders * 0.7), hip_y),
        "right_hip": (right(half_shoulders * 0.7), hip_y),
        "left_knee": (left(half_shoulders * 0.7), hip_y + scale * 0.8),
        "right_knee": (right(half_shoulders * 0.7), hip_y + scale * 0.8),
        "left_ankle": (left(half_shoulders * 0.7), hip_y + scale * 1.6),
        "right_ankle": (right(half_shoulders * 0.7), hip_y + scale * 1.6),
    }

    if hand_raised:
        # Forearm near vertical: wrist almost directly above the elbow.
        points["right_elbow"] = (right(half_shoulders * 1.2), shoulder_y - scale * 0.02)
        points["right_wrist"] = (
            right(half_shoulders * 1.1) + wrist_dx,
            shoulder_y - scale * 0.45,
        )
    elif arm_out:
        # Hand above the shoulder but the arm thrown out sideways -- a stretch,
        # not a presented palm.
        points["right_elbow"] = (right(half_shoulders * 1.6), shoulder_y - scale * 0.15)
        points["right_wrist"] = (right(half_shoulders * 3.2), shoulder_y - scale * 0.30)

    scores = dict.fromkeys(KEYPOINT_NAMES, score)
    if facing == "away":
        # From behind, the front of the face simply is not there. Ears still are.
        for hidden in ("nose", "left_eye", "right_eye"):
            scores[hidden] = 0.0

    return Keypoints(
        points=tuple(
            Keypoint(points[name][0], points[name][1], scores[name])
            for name in KEYPOINT_NAMES
        )
    )


def person(
    *,
    cx: float,
    cy: float = 260.0,
    scale: float = 100.0,
    hand_raised: bool = False,
    arm_out: bool = False,
    wrist_dx: float = 0.0,
    score: float = 0.9,
    with_pose: bool = True,
    facing: str = "facing",
) -> Detection:
    half_w = scale * 0.5
    bbox = BBox(cx - half_w, cy - scale * 0.9, cx + half_w, cy + scale * 1.9)
    return Detection(
        bbox=bbox,
        score=score,
        label="person",
        class_id=0,
        keypoints=make_keypoints(
            cx=cx,
            cy=cy,
            scale=scale,
            hand_raised=hand_raised,
            arm_out=arm_out,
            wrist_dx=wrist_dx,
            score=score,
            facing=facing,
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
