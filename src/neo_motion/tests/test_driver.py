"""The servo driver's safety rules.

Every one of these exists because an upstream node can be wrong, and this is the
last code that runs before something physical moves.
"""

from __future__ import annotations

import math

import pytest

from neo_motion.backend import MockServoBackend, ServoCalibration
from neo_motion.driver import ServoDriver
from neo_motion.types import AxisLimits, HeadCommand, HeadLimits, HeadPose

DT = 0.02  # 50 Hz


def driver(**kwargs) -> ServoDriver:
    return ServoDriver(backend=MockServoBackend(), **kwargs)


def run(d: ServoDriver, cmd: HeadCommand | None, ticks: int, start: float = 0.0):
    t = start
    for _ in range(ticks):
        if cmd is not None:
            d.command(HeadCommand(cmd.pan_rad, cmd.tilt_rad, cmd.priority,
                                  cmd.max_speed_rad_s, stamp=t))
        t += DT
        d.step(t, DT)
    return t


class TestLimits:
    def test_a_command_past_the_limit_is_clamped_not_rejected(self):
        d = driver()
        run(d, HeadCommand(pan_rad=math.radians(500)), 400)
        assert d.pan_rad <= d.limits.pan.max_rad, "never past the stop"
        assert d.pan_rad == pytest.approx(
            d.limits.pan.max_rad, abs=d.limits.pan.deadband_rad
        ), "and up against it, to within the deadband"
        assert d.at_limit

    def test_both_directions(self):
        d = driver()
        run(d, HeadCommand(pan_rad=math.radians(-500)), 400)
        assert d.pan_rad >= d.limits.pan.min_rad
        assert d.pan_rad == pytest.approx(
            d.limits.pan.min_rad, abs=d.limits.pan.deadband_rad
        )

    def test_tilt_has_its_own_tighter_envelope(self):
        d = driver()
        assert d.limits.tilt.max_rad < d.limits.pan.max_rad
        run(d, HeadCommand(tilt_rad=math.radians(90)), 400)
        assert d.tilt_rad <= d.limits.tilt.max_rad
        assert d.tilt_rad == pytest.approx(
            d.limits.tilt.max_rad, abs=d.limits.tilt.deadband_rad
        )

    def test_at_limit_survives_the_deadband(self):
        """Held hard against the stop, the indicator must not go dark.

        The head settles up to a deadband short of the stop, because that last
        fraction of a degree is below what it will chase. An exact comparison
        reads that as "not at the limit" -- while the operator is pushing into
        it as hard as they can.
        """
        # Whether it settles exactly on the stop or just short of it depends on
        # the phase of the approach -- a step lands where it lands. Starting a
        # degree off centre makes the remainder fall inside the deadband, which
        # is the case an exact comparison gets wrong.
        d = driver(start=HeadPose(pan_rad=math.radians(1.0)))
        run(d, HeadCommand(pan_rad=math.radians(500)), 400)
        assert d.pan_rad < d.limits.pan.max_rad, "settles inside the stop, as designed"
        assert d.limits.pan.max_rad - d.pan_rad <= d.limits.pan.deadband_rad
        assert d.at_limit, "and must still say so"

    def test_a_starting_pose_outside_the_limits_is_clamped(self):
        d = driver(start=HeadPose(pan_rad=math.radians(200)))
        assert d.pan_rad == pytest.approx(d.limits.pan.max_rad)


class TestSlewRate:
    def test_a_step_command_becomes_a_ramp(self):
        d = driver()
        target = math.radians(60)
        d.command(HeadCommand(pan_rad=target, stamp=0.0))
        d.step(DT, DT)
        # One tick at 60 deg/s is about 1.2 deg, nowhere near the target.
        assert d.pan_rad < math.radians(3)

    def test_it_gets_there_eventually(self):
        d = driver()
        run(d, HeadCommand(pan_rad=math.radians(60)), 200)
        assert d.pan_rad == pytest.approx(math.radians(60), abs=math.radians(1))

    def test_speed_never_exceeds_the_axis_cap(self):
        d = driver()
        previous = d.pan_rad
        t = 0.0
        for _ in range(50):
            d.command(HeadCommand(pan_rad=math.radians(90), stamp=t))
            t += DT
            d.step(t, DT)
            assert abs(d.pan_rad - previous) <= d.limits.pan.max_speed_rad_s * DT + 1e-9
            previous = d.pan_rad

    def test_a_request_may_ask_for_less_but_never_for_more(self):
        slow = driver()
        fast = driver()
        t = 0.0
        for _ in range(20):
            slow.command(HeadCommand(pan_rad=math.radians(90),
                                     max_speed_rad_s=math.radians(5), stamp=t))
            # Asking for 10x the cap must not raise it.
            fast.command(HeadCommand(pan_rad=math.radians(90),
                                     max_speed_rad_s=math.radians(600), stamp=t))
            t += DT
            slow.step(t, DT)
            fast.step(t, DT)
        assert slow.pan_rad < fast.pan_rad
        assert fast.pan_rad <= math.radians(60) * (20 * DT) + 1e-6


class TestDeadband:
    def test_a_tiny_error_does_not_move_the_head(self):
        d = driver()
        tiny = d.limits.pan.deadband_rad * 0.5
        run(d, HeadCommand(pan_rad=tiny), 20)
        assert d.pan_rad == 0.0, "hunting inside the deadband grinds the gears"

    def test_an_error_past_the_deadband_does_move_it(self):
        d = driver()
        run(d, HeadCommand(pan_rad=d.limits.pan.deadband_rad * 4), 40)
        assert d.pan_rad > 0


class TestWatchdog:
    def test_stale_commands_stop_the_head(self):
        """The last command stays valid for the watchdog window, then stops.

        Not instant: honouring a command for `watchdog_s` after it arrives is
        the point of the window. What must not happen is driving forever.
        """
        d = driver(watchdog_s=0.2)
        t = run(d, HeadCommand(pan_rad=math.radians(90)), 5)

        # Tick past the window without commanding.
        for _ in range(20):
            t += DT
            d.step(t, DT)
        assert d.watchdog_tripped, "the window should have expired by now"
        settled = d.pan_rad

        for _ in range(40):
            t += DT
            d.step(t, DT)
        assert d.pan_rad == pytest.approx(settled), "a dead source must not keep driving"
        assert d.pan_rad < d.limits.pan.max_rad, "it must not have reached the target"

    def test_it_holds_rather_than_going_limp(self):
        """A head that releases drops under its own weight."""
        backend = MockServoBackend()
        d = ServoDriver(backend=backend, watchdog_s=0.1)
        t = run(d, HeadCommand(pan_rad=math.radians(45)), 5)

        for _ in range(20):
            t += DT
            d.step(t, DT)
        held = d.pose

        for _ in range(10):
            t += DT
            d.step(t, DT)
        assert backend.released is False
        assert backend.last == pytest.approx((held.pan_rad, held.tilt_rad))

    def test_a_fresh_command_clears_it(self):
        d = driver(watchdog_s=0.1)
        t = run(d, HeadCommand(pan_rad=math.radians(45)), 3)
        for _ in range(20):
            t += DT
            d.step(t, DT)
        assert d.watchdog_tripped

        d.command(HeadCommand(pan_rad=math.radians(45), stamp=t))
        t += DT
        d.step(t, DT)
        assert d.watchdog_tripped is False


class TestEstop:
    def test_it_freezes_the_head(self):
        d = driver()
        run(d, HeadCommand(pan_rad=math.radians(90)), 10)
        frozen = d.pose
        d.set_estop(True)

        t = 10 * DT
        for _ in range(50):
            d.command(HeadCommand(pan_rad=math.radians(-90), stamp=t))
            t += DT
            d.step(t, DT)
        assert d.pose.pan_rad == pytest.approx(frozen.pan_rad)

    def test_it_outranks_a_perfectly_valid_command(self):
        d = driver()
        d.set_estop(True)
        run(d, HeadCommand(pan_rad=math.radians(45)), 30)
        assert d.pan_rad == 0.0

    def test_releasing_resumes_control(self):
        d = driver()
        d.set_estop(True)
        run(d, HeadCommand(pan_rad=math.radians(45)), 10)
        assert d.pan_rad == 0.0

        d.set_estop(False)
        run(d, HeadCommand(pan_rad=math.radians(45)), 100, start=10 * DT)
        assert d.pan_rad > 0

    def test_a_frozen_head_is_still_actively_held(self):
        backend = MockServoBackend()
        d = ServoDriver(backend=backend)
        d.set_estop(True)
        d.step(DT, DT)
        assert backend.released is False, "freezing must not mean going limp"


class TestLifecycle:
    def test_center_snaps_to_neutral(self):
        d = driver()
        run(d, HeadCommand(pan_rad=math.radians(80)), 200)
        assert d.center() == HeadPose(0.0, 0.0)

    def test_shutdown_centres_then_releases(self):
        backend = MockServoBackend()
        d = ServoDriver(backend=backend)
        run(d, HeadCommand(pan_rad=math.radians(60)), 100)
        d.shutdown()

        assert backend.released is True
        assert backend.writes[-1] == pytest.approx((0.0, 0.0)), "centre before letting go"

    def test_a_zero_dt_tick_is_a_no_op(self):
        d = driver()
        d.command(HeadCommand(pan_rad=math.radians(45), stamp=0.0))
        assert d.step(0.0, 0.0) == HeadPose(0.0, 0.0)


class TestCalibration:
    def test_angles_map_across_the_pulse_range(self):
        cal = ServoCalibration(channel=0, min_us=500, max_us=2500,
                               min_rad=math.radians(-90), max_rad=math.radians(90))
        assert cal.to_pulse_us(math.radians(-90)) == pytest.approx(500)
        assert cal.to_pulse_us(0.0) == pytest.approx(1500)
        assert cal.to_pulse_us(math.radians(90)) == pytest.approx(2500)

    def test_out_of_range_angles_cannot_drive_past_the_stops(self):
        cal = ServoCalibration(channel=0)
        assert cal.to_pulse_us(math.radians(400)) == pytest.approx(cal.max_us)
        assert cal.to_pulse_us(math.radians(-400)) == pytest.approx(cal.min_us)

    def test_inversion_mirrors_the_travel(self):
        normal = ServoCalibration(channel=0)
        flipped = ServoCalibration(channel=0, inverted=True)
        assert flipped.to_pulse_us(math.radians(90)) == pytest.approx(
            normal.to_pulse_us(math.radians(-90))
        )


def test_custom_limits_are_honoured():
    limits = HeadLimits(pan=AxisLimits(min_rad=-0.2, max_rad=0.2,
                                       max_speed_rad_s=10.0, deadband_rad=0.0))
    d = ServoDriver(limits=limits, backend=MockServoBackend())
    run(d, HeadCommand(pan_rad=5.0), 100)
    assert d.pan_rad == pytest.approx(0.2)
