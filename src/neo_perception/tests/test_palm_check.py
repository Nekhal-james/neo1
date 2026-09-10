"""The palm check: which part of the pose decides whether it counts as a palm.

A palm that will not register was a guessing game -- the elbow below the frame,
a forearm leaning too far, a hand drifting into a "wave" and a hold that keeps
restarting all look identical from outside. `GestureRecognizer.explain` names
the link that broke. Its one hard requirement is that it can never disagree with
the classifier it explains, which the fuzz test below holds it to.
"""

from __future__ import annotations

import dataclasses
import json
import random

import pytest

from neo_perception.detector import MockDetector, ScriptedFrame
from neo_perception.engagement import EngagementConfig
from neo_perception.gestures import GestureRecognizer
from neo_perception.pipeline import MockFrame, PerceptionPipeline, PipelineConfig
from neo_perception.status_store import write_status
from neo_perception.tracker import MultiTracker, TrackerConfig
from neo_perception.types import KEYPOINT_NAMES, GestureKind, Keypoint, Keypoints

from .conftest import person


def confirmed(detection, stamp: float = 1.0):
    return MultiTracker(TrackerConfig(min_hits=1)).update([detection], stamp)[0]


def with_points(detection, **changes):
    """A copy of `detection` with named keypoints replaced by (x, y, score)."""
    points = list(detection.keypoints.points)
    for name, (x, y, score) in changes.items():
        points[KEYPOINT_NAMES.index(name)] = Keypoint(x, y, score)
    return dataclasses.replace(detection, keypoints=Keypoints(points=tuple(points)))


def rescored(detection, **scores):
    changes = {}
    for name, score in scores.items():
        point = detection.keypoints.get(name, 0.0)
        changes[name] = (point.x, point.y, score)
    return with_points(detection, **changes)


def explain(detection, stamp: float = 1.0):
    track = confirmed(detection, stamp)
    recognizer = GestureRecognizer()
    current, _ = recognizer.update([track], stamp)
    return recognizer.explain(track, stamp), current.get(track.track_id, GestureKind.NONE)


# -- each link of the chain, named -----------------------------------------


def test_a_presented_palm_explains_itself_as_a_palm():
    check, _ = explain(person(cx=320, hand_raised=True))
    assert check.verdict is GestureKind.OPEN_PALM
    assert check.reason == ""
    assert check.arm == "right"
    assert check.forearm_tilt_deg is not None and check.forearm_tilt_deg <= 40.0


def test_arms_down_says_the_hand_is_not_raised():
    check, _ = explain(person(cx=320))
    assert check.verdict is GestureKind.NONE
    assert "not raised" in check.reason


def test_a_sideways_arm_names_the_tilt():
    check, _ = explain(person(cx=320, arm_out=True))
    assert check.verdict is GestureKind.RAISED_HAND
    assert "from vertical" in check.reason
    assert check.forearm_tilt_deg > 40.0


def test_an_elbow_below_the_frame_is_named():
    """The suspected desk-range failure: wrist and shoulder in shot, elbow not."""
    check, _ = explain(rescored(person(cx=320, hand_raised=True), right_elbow=0.1))
    assert check.verdict is GestureKind.RAISED_HAND
    assert "elbow not visible" in check.reason
    assert check.elbow_score == pytest.approx(0.1)


def test_missing_shoulders_are_named():
    check, _ = explain(
        rescored(person(cx=320, hand_raised=True), left_shoulder=0.0, right_shoulder=0.0)
    )
    assert check.verdict is GestureKind.NONE
    assert "shoulders" in check.reason


def test_no_keypoints_are_named():
    check, _ = explain(person(cx=320, with_pose=False))
    assert check.verdict is GestureKind.NONE
    assert "no pose" in check.reason


def test_a_moving_hand_is_named_as_a_wave():
    tracker = MultiTracker(TrackerConfig(min_hits=1))
    recognizer = GestureRecognizer()
    stamp = 0.0
    for i in range(10):
        stamp += 0.1
        dx = 40.0 if i % 2 else -40.0
        tracks = tracker.update([person(cx=320, hand_raised=True, wrist_dx=dx)], stamp)
        recognizer.update(tracks, stamp)
    check = recognizer.explain(tracks[0], stamp)
    assert check.verdict is GestureKind.WAVE
    assert "wave" in check.reason


# -- it can never disagree with the classifier ------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"hand_raised": True}, {"arm_out": True}, {"hand_raised": True, "score": 0.2}],
)
def test_the_explanation_agrees_with_the_classifier_on_the_standard_poses(kwargs):
    check, verdict = explain(person(cx=320, **kwargs))
    assert check.verdict is verdict


def test_the_explanation_never_disagrees_with_the_classifier():
    """Five hundred jittered arms and scores. An explanation that could say
    "palm" while the classifier said otherwise would be worse than none."""
    rng = random.Random(7)
    base = person(cx=320, hand_raised=True)
    disagreements = []
    for case in range(500):
        changes = {}
        for name in ("left_wrist", "right_wrist", "left_elbow", "right_elbow",
                     "left_shoulder", "right_shoulder"):
            point = base.keypoints.get(name, 0.0)
            changes[name] = (
                point.x + rng.uniform(-70, 70),
                point.y + rng.uniform(-70, 70),
                rng.choice([rng.uniform(0.0, 1.0), 0.9, 0.34, 0.36]),
            )
        check, verdict = explain(with_points(base, **changes))
        if check.verdict is not verdict:
            disagreements.append((case, verdict, check))
    assert not disagreements, disagreements[:3]


# -- in the pipeline --------------------------------------------------------


def _pipeline():
    return PerceptionPipeline(
        PipelineConfig(
            tracker=TrackerConfig(min_hits=1),
            engagement=EngagementConfig(confirm_s=0.6),
        ),
        MockDetector(),
    )


def _step(pipeline, detection, stamp):
    pipeline.detector.frames = [ScriptedFrame([detection])]
    pipeline.detector.index = 0
    return pipeline.process(MockFrame(640, 480), stamp)


def test_the_pipeline_reports_the_check_and_the_hold_building():
    pipeline = _pipeline()
    holds = []
    for i in range(6):
        result = _step(pipeline, person(cx=320, hand_raised=True), i * 0.25)
        assert result.palm_check is not None
        assert result.palm_check.verdict is GestureKind.OPEN_PALM
        holds.append(result.hold_progress)
    assert 0.0 < holds[1] < 1.0, "part-way through the hold"
    assert holds[-1] == 1.0, "locked"
    assert result.attention.engaged


def test_one_dropped_frame_restarts_the_hold():
    """Current behaviour, made visible: the hold must be unbroken. If a real
    palm keeps flickering out for a frame, this is the bar that keeps falling
    back to zero -- which is exactly what the panel now shows."""
    pipeline = _pipeline()
    raised, down = person(cx=320, hand_raised=True), person(cx=320)
    _step(pipeline, raised, 0.0)
    building = _step(pipeline, raised, 0.25).hold_progress
    assert building > 0.0
    dropped = _step(pipeline, down, 0.5)
    assert dropped.hold_progress == 0.0
    assert dropped.palm_check.verdict is GestureKind.NONE


def test_nobody_in_view_has_no_check():
    pipeline = PerceptionPipeline(PipelineConfig(), MockDetector())
    pipeline.detector.frames = [ScriptedFrame([])]
    pipeline.detector.index = 0
    assert pipeline.process(MockFrame(640, 480), 0.0).palm_check is None


# -- in the status file -------------------------------------------------------


def test_the_check_is_written_for_other_processes(tmp_path):
    path = tmp_path / "status.json"
    write_status(available=True, palm={"verdict": "raised_hand", "reason": "right elbow not visible"},
                 path=path)
    assert json.loads(path.read_text())["palm"]["reason"] == "right elbow not visible"

    write_status(available=True, path=path)
    assert json.loads(path.read_text())["palm"] is None
