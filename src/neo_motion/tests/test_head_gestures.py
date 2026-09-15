"""The gesture overlay head_behavior plays at GESTURE priority."""

from __future__ import annotations

import math

import pytest

from neo_motion.nodes.head_behavior import GESTURE_SHAPES, _gesture_offset_rad


def _samples(kind, n=400):
    duration = GESTURE_SHAPES[kind][0]
    return [_gesture_offset_rad(kind, duration * i / n) for i in range(n)]


@pytest.mark.parametrize("kind", sorted(GESTURE_SHAPES))
def test_every_gesture_starts_and_ends_at_rest(kind):
    duration = GESTURE_SHAPES[kind][0]
    assert _gesture_offset_rad(kind, 0.0) == pytest.approx((0.0, 0.0), abs=1e-12)
    assert _gesture_offset_rad(kind, duration) == (0.0, 0.0)
    assert _gesture_offset_rad(kind, -0.5) == (0.0, 0.0)


@pytest.mark.parametrize("kind", sorted(GESTURE_SHAPES))
def test_no_gesture_exceeds_its_own_amplitude(kind):
    _duration, pan_amp, tilt_amp, _cycles = GESTURE_SHAPES[kind]
    for pan, tilt in _samples(kind):
        assert abs(math.degrees(pan)) <= pan_amp + 1e-9
        assert abs(math.degrees(tilt)) <= tilt_amp + 1e-9


def test_a_nod_moves_only_tilt_and_a_shake_only_pan():
    nod, shake = _samples("nod"), _samples("shake")
    assert max(abs(p) for p, _ in nod) == 0.0 and max(abs(t) for _, t in nod) > math.radians(5)
    assert max(abs(t) for _, t in shake) == 0.0 and max(abs(p) for p, _ in shake) > math.radians(6)


def test_an_unknown_gesture_does_nothing():
    assert _gesture_offset_rad("wave", 0.3) == (0.0, 0.0)


def test_the_shapes_mirror_neo_emotions():
    """Duplicated on purpose (neo_motion must not depend on neo_emotion), so a
    test is what keeps the copies honest when both are importable."""
    motion = pytest.importorskip("neo_emotion.motion")
    assert {k.value: v for k, v in motion.GESTURE_SHAPES.items()} == GESTURE_SHAPES
