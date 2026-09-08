from __future__ import annotations

import pytest

from neo_perception.gestures import GestureConfig, GestureRecognizer
from neo_perception.tracker import MultiTracker, TrackerConfig
from neo_perception.types import GestureKind

from .conftest import make_keypoints, person


def track_for(detection, clock):
    tracker = MultiTracker(TrackerConfig(min_hits=1))
    return tracker.update([detection], clock.tick())


def test_arms_down_is_no_gesture(clock):
    recognizer = GestureRecognizer()
    tracks = track_for(person(cx=320), clock)
    current, _ = recognizer.update(tracks, clock.now)
    assert current == {}


def test_raised_hand_is_detected(clock):
    recognizer = GestureRecognizer()
    tracks = track_for(person(cx=320, hand_raised=True), clock)
    current, events = recognizer.update(tracks, clock.now)
    assert current[tracks[0].track_id] is GestureKind.RAISED_HAND
    assert len(events) == 1


def test_the_event_is_edge_triggered(clock):
    """One event when the gesture starts, not one per frame."""
    recognizer = GestureRecognizer()
    tracker = MultiTracker(TrackerConfig(min_hits=1))
    total = 0
    for _ in range(5):
        tracks = tracker.update([person(cx=320, hand_raised=True)], clock.tick())
        _, events = recognizer.update(tracks, clock.now)
        total += len(events)
    assert total == 1


def test_detection_is_scale_invariant(clock):
    """Someone far from the desk must register the same as someone close."""
    recognizer = GestureRecognizer()
    for scale in (45.0, 100.0, 220.0):
        tracks = track_for(person(cx=320, scale=scale, hand_raised=True), clock)
        current, _ = recognizer.update(tracks, clock.tick())
        assert current, f"raised hand missed at scale {scale}"


def test_no_keypoints_means_no_gesture(clock):
    """A plain detect model yields boxes only; gestures must degrade, not crash."""
    recognizer = GestureRecognizer()
    tracks = track_for(person(cx=320, with_pose=False), clock)
    current, events = recognizer.update(tracks, clock.now)
    assert current == {} and events == []


def test_low_confidence_keypoints_are_ignored(clock):
    recognizer = GestureRecognizer(GestureConfig(keypoint_min_score=0.5))
    detection = person(cx=320, hand_raised=True, score=0.2)
    tracks = track_for(detection, clock)
    current, _ = recognizer.update(tracks, clock.now)
    assert current == {}


def test_unconfirmed_tracks_are_skipped(clock):
    recognizer = GestureRecognizer()
    tracker = MultiTracker(TrackerConfig(min_hits=5))
    tracks = tracker.update([person(cx=320, hand_raised=True)], clock.tick())
    current, _ = recognizer.update(tracks, clock.now)
    assert current == {}


def test_state_is_dropped_when_a_track_disappears(clock):
    recognizer = GestureRecognizer()
    tracker = MultiTracker(TrackerConfig(min_hits=1, max_age_s=0.2))
    tracks = tracker.update([person(cx=320, hand_raised=True)], clock.tick())
    recognizer.update(tracks, clock.now)
    assert recognizer._active

    tracks = tracker.update([], clock.tick(0.5))
    recognizer.update(tracks, clock.now)
    assert not recognizer._active


class TestWave:
    """Wave needs a decent frame rate; these run at 10 fps deliberately."""

    def test_oscillation_is_recognised_as_a_wave(self, clock):
        recognizer = GestureRecognizer()
        tracker = MultiTracker(TrackerConfig(min_hits=1))
        kind = None
        for i in range(14):
            dx = 30.0 if (i // 2) % 2 == 0 else -30.0
            tracks = tracker.update(
                [person(cx=320, hand_raised=True, wrist_dx=dx)], clock.tick(0.1)
            )
            current, _ = recognizer.update(tracks, clock.now)
            kind = current.get(tracks[0].track_id)
        assert kind is GestureKind.WAVE

    def test_a_still_raised_hand_is_not_a_wave(self, clock):
        recognizer = GestureRecognizer()
        tracker = MultiTracker(TrackerConfig(min_hits=1))
        for _ in range(14):
            tracks = tracker.update(
                [person(cx=320, hand_raised=True)], clock.tick(0.1)
            )
            current, _ = recognizer.update(tracks, clock.now)
        assert current[tracks[0].track_id] is GestureKind.RAISED_HAND
