"""Priority arbitration: who gets the head, and what happens when that changes."""

from __future__ import annotations

import pytest

from neo_motion.arbiter import ArbiterConfig, HeadArbiter
from neo_motion.types import HeadCommand, Priority


def cmd(pan=0.0, tilt=0.0, priority=Priority.IDLE, stamp=0.0):
    return HeadCommand(pan_rad=pan, tilt_rad=tilt, priority=priority, stamp=stamp)


def arbiter(**kwargs):
    return HeadArbiter(ArbiterConfig(**kwargs))


class TestPriority:
    def test_the_highest_priority_source_wins(self):
        a = arbiter(crossfade_s=0)
        a.submit("idle", cmd(pan=0.1, priority=Priority.IDLE))
        a.submit("gaze", cmd(pan=0.2, priority=Priority.GAZE))
        a.submit("manual", cmd(pan=0.3, priority=Priority.MANUAL))

        r = a.resolve(0.0)
        assert r.source == "manual"
        assert r.command.pan_rad == pytest.approx(0.3)

    def test_an_operator_outranks_the_robots_own_gaze(self):
        """The rule that matters: a hand on the joystick always wins."""
        a = arbiter(crossfade_s=0)
        a.submit("gaze", cmd(pan=1.0, priority=Priority.GAZE))
        a.submit("manual", cmd(pan=-1.0, priority=Priority.MANUAL))
        assert a.resolve(0.0).command.pan_rad == pytest.approx(-1.0)

    def test_estop_outranks_everything(self):
        a = arbiter(crossfade_s=0)
        for name, p in [("idle", Priority.IDLE), ("gaze", Priority.GAZE),
                        ("gesture", Priority.GESTURE), ("manual", Priority.MANUAL)]:
            a.submit(name, cmd(pan=1.0, priority=p))
        a.submit("estop", cmd(pan=0.0, priority=Priority.ESTOP))
        assert a.resolve(0.0).source == "estop"

    def test_the_documented_order_holds(self):
        assert (Priority.IDLE < Priority.GAZE < Priority.GESTURE
                < Priority.MANUAL < Priority.ESTOP)


class TestFreshness:
    def test_a_stale_source_loses_the_head(self):
        """A joystick whose tab was closed must not hold the head at priority 70."""
        a = arbiter(source_timeout_s=0.5, crossfade_s=0)
        a.submit("gaze", cmd(pan=0.2, priority=Priority.GAZE, stamp=10.0))
        a.submit("manual", cmd(pan=0.9, priority=Priority.MANUAL, stamp=10.0))
        assert a.resolve(10.1).source == "manual"

        # Only gaze keeps publishing.
        a.submit("gaze", cmd(pan=0.2, priority=Priority.GAZE, stamp=11.0))
        assert a.resolve(11.0).source == "gaze"

    def test_everything_stale_holds_position(self):
        a = arbiter(source_timeout_s=0.2, crossfade_s=0)
        a.submit("gaze", cmd(pan=0.5, priority=Priority.GAZE, stamp=0.0))
        a.resolve(0.0)

        r = a.resolve(5.0)
        assert r.source == "none"
        assert r.command.pan_rad == pytest.approx(0.5), (
            "falling to zero would swing the head to centre on any hiccup"
        )

    def test_withdrawing_releases_immediately(self):
        a = arbiter(crossfade_s=0)
        a.submit("gaze", cmd(pan=0.2, priority=Priority.GAZE))
        a.submit("manual", cmd(pan=0.9, priority=Priority.MANUAL))
        a.withdraw("manual")
        assert a.resolve(0.0).source == "gaze"


class TestCrossfade:
    def test_a_handover_ramps_rather_than_jumps(self):
        a = arbiter(crossfade_s=0.3)
        a.submit("gaze", cmd(pan=0.0, priority=Priority.GAZE, stamp=0.0))
        a.resolve(0.0)

        # The fade starts when the new source first wins, so that resolve is
        # the t=0 of the ramp and necessarily still reads the old position.
        a.submit("manual", cmd(pan=1.0, priority=Priority.MANUAL, stamp=0.0))
        start = a.resolve(0.0)
        assert start.command.pan_rad == pytest.approx(0.0), "ramp begins where it was"

        a.submit("manual", cmd(pan=1.0, priority=Priority.MANUAL, stamp=0.15))
        mid = a.resolve(0.15)
        assert mid.blending is True
        assert 0.2 < mid.command.pan_rad < 0.8, "should be part-way, not snapped"

    def test_the_fade_completes(self):
        a = arbiter(crossfade_s=0.3)
        a.submit("gaze", cmd(pan=0.0, priority=Priority.GAZE, stamp=0.0))
        a.resolve(0.0)
        a.submit("manual", cmd(pan=1.0, priority=Priority.MANUAL, stamp=0.0))

        for t in (0.1, 0.2, 0.31, 0.4):
            a.submit("manual", cmd(pan=1.0, priority=Priority.MANUAL, stamp=t))
            r = a.resolve(t)
        assert r.blending is False
        assert r.command.pan_rad == pytest.approx(1.0)

    def test_the_ramp_is_monotonic(self):
        a = arbiter(crossfade_s=0.3)
        a.submit("gaze", cmd(pan=0.0, priority=Priority.GAZE, stamp=0.0))
        a.resolve(0.0)

        seen = []
        for t in [i * 0.03 for i in range(11)]:
            a.submit("manual", cmd(pan=1.0, priority=Priority.MANUAL, stamp=t))
            seen.append(a.resolve(t).command.pan_rad)
        assert seen == sorted(seen)

    def test_it_ramps_from_the_real_pose_not_the_old_target(self):
        """They differ whenever a fade was cut short by a third source."""
        a = arbiter(crossfade_s=0.3)
        a.submit("gaze", cmd(pan=1.0, priority=Priority.GAZE, stamp=0.0))
        a.resolve(0.0)
        a.sync_to(0.2, 0.0)  # the driver only got this far

        a.submit("manual", cmd(pan=1.0, priority=Priority.MANUAL, stamp=0.0))
        first = a.resolve(0.001).command.pan_rad
        assert first == pytest.approx(0.2, abs=0.05), (
            f"fade started from {first}, not from where the head actually is"
        )

    def test_zero_crossfade_switches_instantly(self):
        a = arbiter(crossfade_s=0.0)
        a.submit("gaze", cmd(pan=0.0, priority=Priority.GAZE, stamp=0.0))
        a.resolve(0.0)
        a.submit("manual", cmd(pan=1.0, priority=Priority.MANUAL, stamp=0.0))
        assert a.resolve(0.0).command.pan_rad == pytest.approx(1.0)


def test_no_sources_at_all_is_survivable():
    r = HeadArbiter().resolve(0.0)
    assert r.source == "none"
    assert r.command.pan_rad == 0.0


def test_live_sources_reports_only_the_fresh_ones():
    a = arbiter(source_timeout_s=0.5)
    a.submit("gaze", cmd(priority=Priority.GAZE, stamp=0.0))
    a.submit("manual", cmd(priority=Priority.MANUAL, stamp=1.0))
    assert a.live_sources(1.0) == ["manual"]
