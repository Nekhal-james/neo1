"""When the emotion node asks the head for a gesture.

Built without the node's __init__ (which needs rclpy): the decision is
`_maybe_gesture`, and publishing is replaced with a list.
"""

from __future__ import annotations

from neo_emotion.motion import GestureKind
from neo_emotion.nodes import emotion_node as en
from neo_emotion.types import EmotionLabel


def _node():
    node = en.EmotionNode.__new__(en.EmotionNode)
    node._last_label = None
    node._last_gesture_at = -1e9
    node.published = []
    node._publish_gesture = node.published.append
    return node


def test_someone_walking_up_is_greeted_with_a_nod():
    node = _node()
    node._maybe_gesture(EmotionLabel.NEUTRAL, person_arrived=True, now=10.0)
    assert node.published == [GestureKind.NOD]


def test_entering_a_mood_gestures_once_not_for_as_long_as_it_lasts():
    node = _node()
    node._maybe_gesture(EmotionLabel.NEUTRAL, False, now=0.0)
    for t in (10.0, 20.0, 30.0):
        node._maybe_gesture(EmotionLabel.CURIOUS, False, now=t)
    assert node.published == [GestureKind.TILT]


def test_confusion_reads_as_a_shake():
    node = _node()
    node._maybe_gesture(EmotionLabel.CONFUSED, False, now=5.0)
    assert node.published == [GestureKind.SHAKE]


def test_moods_with_no_gesture_stay_still():
    node = _node()
    for label in (EmotionLabel.NEUTRAL, EmotionLabel.HAPPY, EmotionLabel.SLEEPY, EmotionLabel.ATTENTIVE):
        node._maybe_gesture(label, False, now=100.0 * (int(label) + 1))
    assert node.published == []


def test_gestures_are_spaced_out_so_a_flicker_is_not_a_twitch():
    node = _node()
    node._maybe_gesture(EmotionLabel.CURIOUS, False, now=10.0)
    node._maybe_gesture(EmotionLabel.CONFUSED, False, now=10.0 + en.GESTURE_COOLDOWN_S / 2)
    node._maybe_gesture(EmotionLabel.CURIOUS, False, now=10.0 + en.GESTURE_COOLDOWN_S * 2)
    assert node.published == [GestureKind.TILT, GestureKind.TILT]
