from __future__ import annotations

import pytest

from neo_perception.tracker import MultiTracker, TrackerConfig

from .conftest import person


def test_identity_is_stable_across_frames(clock):
    tracker = MultiTracker(TrackerConfig(min_hits=1))
    ids = set()
    for cx in range(200, 400, 20):
        tracks = tracker.update([person(cx=cx)], clock.tick())
        ids.update(t.track_id for t in tracks)
    assert ids == {1}, "a person walking smoothly must keep one id"


def test_two_people_keep_separate_ids(clock):
    tracker = MultiTracker(TrackerConfig(min_hits=1))
    for i in range(6):
        tracks = tracker.update(
            [person(cx=150 + i * 5), person(cx=480 - i * 5)], clock.tick()
        )
    assert len({t.track_id for t in tracks}) == 2


def test_crossing_paths_does_not_merge_tracks(clock):
    """People passing each other must not collapse into one track."""
    tracker = MultiTracker(TrackerConfig(min_hits=1))
    for i in range(10):
        tracks = tracker.update(
            [person(cx=200 + i * 12), person(cx=440 - i * 12)], clock.tick()
        )
    assert len(tracks) == 2


def test_confirmation_requires_multiple_hits(clock):
    tracker = MultiTracker(TrackerConfig(min_hits=3))
    tracks = tracker.update([person(cx=300)], clock.tick())
    assert tracks[0].confirmed is False

    tracker.update([person(cx=302)], clock.tick())
    tracks = tracker.update([person(cx=304)], clock.tick())
    assert tracks[0].confirmed is True


def test_a_one_frame_flicker_never_confirms(clock):
    """Detector noise must not become a lock candidate."""
    tracker = MultiTracker(TrackerConfig(min_hits=2, max_age_s=0.5))
    tracker.update([person(cx=100)], clock.tick())
    for _ in range(4):
        tracks = tracker.update([], clock.tick())
    assert tracks == []


def test_tracks_age_out_in_seconds_not_frames(clock):
    tracker = MultiTracker(TrackerConfig(min_hits=1, max_age_s=1.0))
    tracker.update([person(cx=300)], clock.tick())

    tracker.update([], clock.tick(0.5))
    assert len(tracker.tracks) == 1, "still inside the age window"

    tracker.update([], clock.tick(0.8))
    assert tracker.tracks == []


def test_fast_motion_is_matched_by_the_centre_gate(clock):
    """At 4 fps a walking person can move further than their own box width."""
    tracker = MultiTracker(TrackerConfig(min_hits=1))
    tracker.update([person(cx=200, scale=80)], clock.tick())
    tracks = tracker.update([person(cx=300, scale=80)], clock.tick())
    assert len(tracks) == 1, "should follow the person, not spawn a second track"
    assert tracks[0].track_id == 1


def test_visible_tracks_excludes_stale_ones(clock):
    tracker = MultiTracker(TrackerConfig(min_hits=1, max_age_s=2.0))
    tracker.update([person(cx=300)], clock.tick())
    stamp = clock.tick()
    tracker.update([], stamp)
    assert tracker.visible_tracks(stamp) == []
    assert len(tracker.tracks) == 1, "kept for re-acquisition, but not visible"


def test_velocity_is_estimated(clock):
    tracker = MultiTracker(TrackerConfig(min_hits=1))
    tracker.update([person(cx=200)], clock.tick(0.25))
    tracker.update([person(cx=250)], clock.tick(0.25))
    tracks = tracker.update([person(cx=300)], clock.tick(0.25))
    assert tracks[0].vx > 0


def test_reset_clears_ids(clock):
    tracker = MultiTracker(TrackerConfig(min_hits=1))
    tracker.update([person(cx=300)], clock.tick())
    tracker.reset()
    tracks = tracker.update([person(cx=300)], clock.tick())
    assert tracks[0].track_id == 1
