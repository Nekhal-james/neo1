"""The emotion state machine and the motion it shapes.

Emotion is a movement *modifier*: it never reaches a servo on its own. These
tests hold that line as much as they check the transitions.
"""

from __future__ import annotations

import math

import pytest

from neo_emotion.motion import EmotionMotion, GestureKind, IdleMotion
from neo_emotion.state import EmotionConfig, EmotionController, EmotionEvents
from neo_emotion.types import PROFILES, EmotionLabel


def controller(**kwargs) -> EmotionController:
    return EmotionController(EmotionConfig(**kwargs))


def drive(c: EmotionController, events: EmotionEvents, start: float, seconds: float,
          step: float = 0.2):
    t = start
    end = start + seconds
    state = c.update(events, t)
    while t < end:
        t += step
        state = c.update(events, t)
    return state, t


class TestTransitions:
    def test_it_starts_neutral(self):
        c = controller()
        assert c.update(EmotionEvents(), 0.0).label is EmotionLabel.NEUTRAL

    def test_a_person_arriving_reads_as_curious(self):
        c = controller()
        c.update(EmotionEvents(), 0.0)
        s = c.update(EmotionEvents(person_present=True, person_arrived=True), 1.0)
        assert s.label is EmotionLabel.CURIOUS

    def test_curiosity_settles_into_attention(self):
        c = controller(curious_s=1.0)
        c.update(EmotionEvents(), 0.0)
        c.update(EmotionEvents(person_present=True, person_arrived=True), 1.0)
        s, _ = drive(c, EmotionEvents(person_present=True), 1.2, 3.0)
        assert s.label is EmotionLabel.ATTENTIVE

    def test_engagement_is_full_intensity_attention(self):
        c = controller()
        c.update(EmotionEvents(), 0.0)
        s, _ = drive(c, EmotionEvents(person_present=True, engaged=True), 1.0, 3.0)
        assert s.label is EmotionLabel.ATTENTIVE
        assert s.intensity == pytest.approx(1.0)

    def test_a_gesture_is_greeted(self):
        c = controller()
        c.update(EmotionEvents(), 0.0)
        s = c.update(EmotionEvents(person_present=True, gesture_seen=True), 1.0)
        assert s.label is EmotionLabel.HAPPY

    def test_a_greeting_is_a_moment_not_a_mood(self):
        c = controller(happy_s=1.0)
        c.update(EmotionEvents(), 0.0)
        c.update(EmotionEvents(person_present=True, gesture_seen=True), 1.0)
        s, _ = drive(c, EmotionEvents(person_present=True, engaged=True), 1.1, 4.0)
        assert s.label is EmotionLabel.ATTENTIVE

    def test_a_failed_lookup_reads_as_confusion(self):
        c = controller()
        c.update(EmotionEvents(person_present=True), 0.0)
        s = c.update(EmotionEvents(person_present=True, lookup_failed=True), 1.0)
        assert s.label is EmotionLabel.CONFUSED

    def test_an_empty_lobby_eventually_sleeps(self):
        c = controller(sleepy_after_s=5.0)
        c.update(EmotionEvents(), 0.0)
        s, _ = drive(c, EmotionEvents(), 0.0, 12.0, step=0.5)
        assert s.label is EmotionLabel.SLEEPY

    def test_someone_arriving_wakes_it(self):
        c = controller(sleepy_after_s=3.0)
        c.update(EmotionEvents(), 0.0)
        drive(c, EmotionEvents(), 0.0, 8.0, step=0.5)
        assert c.label is EmotionLabel.SLEEPY

        s, _ = drive(c, EmotionEvents(person_present=True, person_arrived=True), 8.5, 2.0)
        assert s.label is not EmotionLabel.SLEEPY

    def test_a_dead_link_is_subdued_not_distressed(self):
        """Degraded mode is the normal operating state; it must not look wrong."""
        c = controller()
        c.update(EmotionEvents(), 0.0)
        s, _ = drive(c, EmotionEvents(link_down=True), 1.0, 3.0)
        assert s.label is EmotionLabel.NEUTRAL
        assert s.intensity < 0.3


class TestDwell:
    def test_a_one_frame_flicker_does_not_change_the_mood(self):
        """Detector noise must not make the robot's apparent mood strobe."""
        c = controller(min_dwell_s=1.0)
        c.update(EmotionEvents(), 0.0)
        c.update(EmotionEvents(person_present=True), 1.5)
        assert c.label is EmotionLabel.ATTENTIVE

        c.update(EmotionEvents(person_present=False), 1.6)
        assert c.label is EmotionLabel.ATTENTIVE, "too soon to change again"

    def test_deliberate_events_bypass_the_dwell(self):
        """A failed lookup should show immediately; it is not noise."""
        c = controller(min_dwell_s=10.0)
        c.update(EmotionEvents(), 0.0)
        c.update(EmotionEvents(person_present=True), 0.1)
        s = c.update(EmotionEvents(person_present=True, lookup_failed=True), 0.2)
        assert s.label is EmotionLabel.CONFUSED


class TestProfiles:
    def test_every_label_has_motion_params(self):
        assert set(PROFILES) == set(EmotionLabel)

    def test_sleepy_is_defined_by_absent_micro_motion(self):
        assert PROFILES[EmotionLabel.SLEEPY].micro_motion < 0.1
        assert PROFILES[EmotionLabel.ATTENTIVE].micro_motion > 0.3

    def test_attention_drifts_less_than_neutral(self):
        """Drift reads as inattention, so being watched means holding steadier."""
        assert (PROFILES[EmotionLabel.ATTENTIVE].idle_amplitude_deg
                < PROFILES[EmotionLabel.NEUTRAL].idle_amplitude_deg)

    def test_curiosity_reads_as_a_tilt(self):
        assert PROFILES[EmotionLabel.CURIOUS].tilt_bias_deg > 5.0

    def test_attention_follows_more_eagerly(self):
        assert (PROFILES[EmotionLabel.ATTENTIVE].gaze_gain
                > PROFILES[EmotionLabel.SLEEPY].gaze_gain)


class TestIdleMotion:
    def test_it_stays_within_amplitude(self):
        idle = IdleMotion()
        params = PROFILES[EmotionLabel.NEUTRAL]
        for i in range(2000):
            pan, tilt = idle.offset(params, i * 0.05)
            assert abs(pan) <= params.idle_amplitude_deg * 1.5
            assert abs(tilt - params.tilt_bias_deg) <= params.idle_amplitude_deg * 1.5

    def test_it_does_not_visibly_repeat(self):
        """A single sine reads as a metronome within about ten seconds."""
        idle = IdleMotion()
        params = PROFILES[EmotionLabel.NEUTRAL]
        period = 1.0 / params.idle_freq_hz
        a = idle.offset(params, 0.0)[0]
        b = idle.offset(params, period)[0]
        assert abs(a - b) > 1e-3, "the two components should not re-align"

    def test_the_tilt_bias_shifts_the_resting_pose(self):
        idle = IdleMotion()
        biased = PROFILES[EmotionLabel.CURIOUS]
        samples = [idle.offset(biased, i * 0.37)[1] for i in range(400)]
        assert sum(samples) / len(samples) == pytest.approx(
            biased.tilt_bias_deg, abs=1.0
        )


class TestGestures:
    def test_a_gesture_returns_to_zero(self):
        """Overlays, never modes: the head cannot get stuck in one."""
        m = EmotionMotion()
        m.trigger(GestureKind.NOD, 0.0)
        assert m.gesture is GestureKind.NOD

        params = PROFILES[EmotionLabel.NEUTRAL]
        m.offset_deg(params, 5.0)
        assert m.gesture is None

    def test_it_starts_and_ends_smoothly(self):
        """A step at either end is a shock load on the gears."""
        from neo_emotion.motion import ActiveGesture

        g = ActiveGesture(GestureKind.NOD, 0.0)
        assert g.offset(0.0) == (0.0, 0.0)
        assert g.offset(0.9) == (0.0, 0.0)
        # Sample the whole gesture: a nod crosses zero four times on its way
        # through, so any single instant can legitimately read as nearly still.
        peak = max(abs(g.offset(t / 200.0)[1]) for t in range(1, 180))
        assert peak > 3.0, "should actually move in between"

    def test_a_nod_moves_tilt_and_a_shake_moves_pan(self):
        from neo_emotion.motion import ActiveGesture

        nod = [abs(ActiveGesture(GestureKind.NOD, 0.0).offset(t / 100)[1])
               for t in range(90)]
        shake = [abs(ActiveGesture(GestureKind.SHAKE, 0.0).offset(t / 100)[0])
                 for t in range(90)]
        assert max(nod) > 1.0 and max(shake) > 1.0

        assert max(abs(ActiveGesture(GestureKind.NOD, 0.0).offset(t / 100)[0])
                   for t in range(90)) < 1e-9, "a nod must not swing pan"

    def test_triggering_replaces_rather_than_queues(self):
        """A queue would buy seconds of movement outliving the event."""
        m = EmotionMotion()
        m.trigger(GestureKind.SCAN, 0.0)
        m.trigger(GestureKind.NOD, 0.1)
        assert m.gesture is GestureKind.NOD

    def test_offsets_are_available_in_radians(self):
        m = EmotionMotion()
        params = PROFILES[EmotionLabel.NEUTRAL]
        deg = m.offset_deg(params, 1.0)
        rad = m.offset_rad(params, 1.0)
        assert rad[0] == pytest.approx(math.radians(deg[0]))
