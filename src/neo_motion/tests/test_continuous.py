"""Continuous-rotation (360 deg) servos: speed control, dead reckoning, and stopping.

These are the robot's servos, kept without a position sensor, so the head's
angle is an estimate. What these tests pin down is that the estimate is honest
to the model, that nothing asks the servo for more than it was measured to do,
and above all that every way of "holding still" actually sends neutral --
because on these servos, repeating the last pulse keeps them turning.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from neo_motion.backend import (
    ContinuousAxis,
    ContinuousCalibration,
    RpiPwmBackend,
    RpiPwmContinuousBackend,
    make_backend,
)
from neo_motion.config import MG995_360, MotionConfig, MotionConfigError
from neo_motion.driver import ServoDriver
from neo_motion.types import HeadCommand, HeadLimits

from .test_rpi_pwm import FakeSysfs, sysfs  # noqa: F401 -- sysfs is a fixture

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config" / "motion.yaml"

# Stops at 1490 us, still stopped within +/-20 us, 150 deg/s at +/-100 us.
CAL = dict(neutral_us=1490.0, deadband_us=20.0, speed_offset_us=100.0, speed_deg_s=150.0)
CALIBRATED_YAML = """\
servo: {model: mg995-360, driver: rpi-pwm}
pan: {neutral_us: 1490, deadband_us: 20, speed_offset_us: 100, speed_deg_s: 150, min_deg: -60, max_deg: 60}
tilt: {neutral_us: 1510, deadband_us: 15, speed_offset_us: 80, speed_deg_s: 120}
"""


def cal(channel=0, **overrides):
    return ContinuousCalibration(channel=channel, **{**CAL, **overrides})


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


# -- the speed model ------------------------------------------------------------


def test_zero_speed_is_exactly_neutral():
    c = cal()
    assert c.offset_for(0.0) == 0.0 and c.pulse_for(c.offset_for(0.0)) == 1490.0


def test_speed_is_linear_beyond_the_stop_band():
    c = cal()
    assert c.gain == pytest.approx(150 / 80)                 # 150 deg/s over the 80 us past the band
    assert c.speed_for(100) == pytest.approx(150)
    assert c.speed_for(-60) == pytest.approx(-(150 / 80) * 40)
    assert c.speed_for(15) == 0.0, "inside the stop band it does not turn"
    for speed in (-300.0, -12.5, 7.0, 150.0):
        assert c.speed_for(c.offset_for(speed)) == pytest.approx(speed)


def test_offsets_never_pass_the_cap():
    c = cal(max_offset_us=200.0)
    assert c.offset_for(10_000) == 200.0
    assert c.max_speed_deg_s == pytest.approx((150 / 80) * (200 - 20))


def test_inverted_turns_the_pulse_the_other_way():
    assert cal(inverted=True).pulse_for(50) == 1440.0


# -- one axis -------------------------------------------------------------------


def run_axis(axis, target_deg, seconds, tick=0.02, start=0.0):
    t, pulses = start, []
    for _ in range(round(seconds / tick)):
        pulses.append(axis.update(math.radians(target_deg), t))
        t += tick
    return t, pulses


def test_it_turns_towards_the_target_and_stops_there():
    axis = ContinuousAxis(cal())
    _, pulses = run_axis(axis, 20.0, 1.0)
    assert pulses[0] > 1490 + 20, "starts turning, beyond the stop band"
    assert axis.estimate_deg == pytest.approx(20.0, abs=ContinuousAxis.TOLERANCE_DEG)
    assert pulses[-1] == 1490.0, "arrived: neutral"


def test_the_target_is_held_to_the_limits():
    axis = ContinuousAxis(cal(min_deg=-10, max_deg=10))
    run_axis(axis, 90.0, 2.0)
    assert axis.estimate_deg <= 10 + ContinuousAxis.TOLERANCE_DEG


def test_it_never_asks_for_more_than_the_servo_can_do():
    c = cal(max_offset_us=120.0)                             # (150/80) x 100 = 187.5 deg/s at most
    axis = ContinuousAxis(c)
    axis.update(math.radians(1000), 0.0)
    assert axis.speed_deg_s == pytest.approx(c.max_speed_deg_s)


def test_stop_sends_neutral_and_keeps_the_distance_already_covered():
    axis = ContinuousAxis(cal())
    axis.update(math.radians(20), 0.0)
    speed = axis.speed_deg_s
    assert axis.stop(0.05) == 1490.0
    assert axis.estimate_deg == pytest.approx(speed * 0.05)


def test_rehome_makes_here_zero():
    axis = ContinuousAxis(cal())
    run_axis(axis, 15.0, 1.0)
    axis.rehome()
    assert axis.estimate_deg == 0.0


def test_an_uncalibrated_servo_is_never_driven():
    with pytest.raises(RuntimeError, match="not calibrated"):
        ContinuousAxis(ContinuousCalibration(channel=0))


# -- the backend, on a simulated /sys/class/pwm ---------------------------------


def backend(clock, **kwargs):
    return RpiPwmContinuousBackend(cal(0), cal(1, neutral_us=1510.0), clock=clock, watchdog=False, **kwargs)


def test_holding_the_same_pose_sends_neutral(sysfs):  # noqa: F811
    """The driver's hold, watchdog and e-stop all rewrite the current pose.
    On these servos that must mean stopped, not "keep doing the last thing"."""
    clock = Clock()
    b = backend(clock)
    b.write(0.0, 0.0)
    assert sysfs.value(0, "duty_cycle") == "1490000" and sysfs.value(1, "duty_cycle") == "1510000"
    assert sysfs.value(0, "enable") == "1"


def test_a_move_turns_then_settles_back_to_neutral(sysfs):  # noqa: F811
    clock = Clock()
    b = backend(clock)
    b.write(math.radians(10), 0.0)
    assert int(sysfs.value(0, "duty_cycle")) > 1510000, "turning"
    for _ in range(50):
        clock.now += 0.02
        b.write(math.radians(10), 0.0)
    assert b.estimate_deg[0] == pytest.approx(10.0, abs=ContinuousAxis.TOLERANCE_DEG)
    assert sysfs.value(0, "duty_cycle") == "1490000"


def test_a_stalled_writer_is_stopped_by_the_watchdog(sysfs):  # noqa: F811
    clock = Clock()
    b = backend(clock)
    b.write(math.radians(30), 0.0)
    assert sysfs.value(0, "duty_cycle") != "1490000"
    clock.now += RpiPwmContinuousBackend.STALL_STOP_S / 2
    assert not b.check_stall(), "not stalled yet"
    clock.now += RpiPwmContinuousBackend.STALL_STOP_S
    assert b.check_stall()
    assert sysfs.value(0, "duty_cycle") == "1490000"
    assert not b.check_stall(), "only once per stall"


def test_the_watchdog_thread_stops_a_real_stall(sysfs):  # noqa: F811
    import time

    b = RpiPwmContinuousBackend(cal(0), cal(1), watchdog=True)
    try:
        b.write(math.radians(30), 0.0)
        deadline = time.monotonic() + 2.0
        while sysfs.value(0, "duty_cycle") != "1490000" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert sysfs.value(0, "duty_cycle") == "1490000"
    finally:
        b.close()


def test_release_stops_the_pulses_and_close_unexports(sysfs):  # noqa: F811
    clock = Clock()
    b = backend(clock)
    b.write(math.radians(10), 0.0)
    b.release()
    assert sysfs.value(0, "enable") == "0" and sysfs.value(1, "enable") == "0"
    b.close()
    assert not (sysfs.chip / "pwm0").exists()


def test_the_driver_and_the_estimate_agree(sysfs):  # noqa: F811
    """The whole stack: the driver slews to a target at its speed cap, and the
    estimate follows it tick for tick, then both hold with the servo stopped."""
    config = MotionConfig.from_raw({"servo": {"model": "mg995-360", "driver": "rpi-pwm"},
                                    "pan": dict(CAL), "tilt": dict(CAL)})
    clock = Clock(0.0)
    b = RpiPwmContinuousBackend(**{**config.backend_kwargs(), "clock": clock, "watchdog": False})
    driver = ServoDriver(limits=config.head_limits(), backend=b)
    for _ in range(100):                                     # 2 s at 50 Hz
        driver.command(HeadCommand(pan_rad=math.radians(25), stamp=clock.now))
        driver.step(clock.now, 0.02)
        clock.now += 0.02
    assert math.degrees(driver.pan_rad) == pytest.approx(25, abs=0.5)
    assert b.estimate_deg[0] == pytest.approx(math.degrees(driver.pan_rad), abs=1.0)
    for _ in range(50):                                      # commands stop: the watchdog holds
        driver.step(clock.now, 0.02)
        clock.now += 0.02
    assert sysfs.value(0, "duty_cycle") == "1490000"


# -- config -------------------------------------------------------------------


def write(tmp_path, text, name="motion.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def problems(tmp_path, text):
    with pytest.raises(MotionConfigError) as exc:
        MotionConfig.load(write(tmp_path, text))
    return exc.value.problems


def test_the_committed_config_is_uncalibrated_continuous_rotation_on_the_pis_pwm():
    config = MotionConfig.load(REPO_CONFIG)
    assert (config.model, config.driver) == ("mg995-360", "rpi-pwm")
    assert config.servo is MG995_360 and not config.calibrated
    assert "UNCALIBRATED: will not drive the head" in config.describe()[1]


def test_calibration_and_limits_come_from_the_local_numbers(tmp_path):
    config = MotionConfig.load(write(tmp_path, CALIBRATED_YAML))
    assert config.calibrated
    pan, tilt = config.calibration("pan"), config.calibration("tilt")
    assert isinstance(pan, ContinuousCalibration)
    assert (pan.neutral_us, pan.min_deg, pan.max_deg) == (1490, -60, 60)
    assert (tilt.neutral_us, tilt.min_deg, tilt.max_deg) == (1510, -30, 30), "angle limit defaults to +/-30"
    assert "stops at 1490 us (+/-20), 150 deg/s at +/-100 us" in config.describe()[1]


def test_the_driver_is_narrowed_to_the_estimates_limits_and_the_servos_speed(tmp_path):
    config = MotionConfig.load(write(tmp_path, CALIBRATED_YAML))
    limits = config.head_limits()
    assert math.degrees(limits.pan.max_rad) == pytest.approx(60)
    assert math.degrees(limits.tilt.max_rad) == pytest.approx(30)
    assert limits.pan.max_speed_rad_s <= math.radians(config.calibration("pan").max_speed_deg_s)
    assert limits.pan.max_speed_rad_s == HeadLimits().pan.max_speed_rad_s, "60 deg/s is below the servo's"


def test_positional_calibration_is_refused_for_a_continuous_servo(tmp_path):
    found = problems(tmp_path, "servo: {model: mg995-360, driver: rpi-pwm}\npan: {min_us: 1000, max_us: 2000}\n")
    assert any("sets speed, not angle" in p for p in found)


def test_half_a_spin_calibration_is_refused(tmp_path):
    found = problems(tmp_path, "servo: {model: mg995-360, driver: rpi-pwm}\npan: {neutral_us: 1490}\n")
    assert any("set all four of neutral_us" in p for p in found)


@pytest.mark.parametrize("fields, message", [
    ("neutral_us: 2300, deadband_us: 20, speed_offset_us: 100, speed_deg_s: 150", "neutral_us must be within 900-2100"),
    ("neutral_us: 1500, deadband_us: 120, speed_offset_us: 100, speed_deg_s: 150", "deadband_us < speed_offset_us"),
    ("neutral_us: 1500, deadband_us: 20, speed_offset_us: 100, speed_deg_s: 0", "speed_deg_s must be above 0"),
])
def test_impossible_spin_numbers_are_refused(tmp_path, fields, message):
    found = problems(tmp_path, f"servo: {{model: mg995-360, driver: rpi-pwm}}\npan: {{{fields}}}\n")
    assert any(message in p for p in found), found


def test_continuous_servos_need_the_pis_pwm(tmp_path):
    found = problems(tmp_path, "servo: {model: mg995-360, driver: pca9685}\n")
    assert any("only supported with driver rpi-pwm" in p for p in found)


def test_spin_fields_on_a_positional_servo_are_refused(tmp_path):
    found = problems(tmp_path, "servo: {model: mg995}\npan: {neutral_us: 1490}\n")
    assert any("only apply to a continuous-rotation servo" in p for p in found)


def test_make_backend_refuses_an_uncalibrated_continuous_servo(sysfs, monkeypatch):  # noqa: F811
    monkeypatch.setenv("NEO_MOTION_CONFIG", str(REPO_CONFIG))
    with pytest.raises(RuntimeError, match="not calibrated"):
        make_backend("rpi-pwm")


def test_make_backend_builds_the_continuous_backend_once_calibrated(sysfs, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("NEO_MOTION_CONFIG", str(write(tmp_path, CALIBRATED_YAML)))
    b = make_backend("rpi-pwm")
    try:
        assert isinstance(b, RpiPwmContinuousBackend) and not isinstance(b, RpiPwmBackend)
        assert b.pan.cal.neutral_us == 1490 and b.tilt.cal.neutral_us == 1510
    finally:
        b.close()


# -- neo-servo-check ------------------------------------------------------------


@pytest.fixture
def check(sysfs, monkeypatch):  # noqa: F811
    from neo_motion.scripts import servo_check

    monkeypatch.setenv("NEO_MOTION_CONFIG", str(REPO_CONFIG))
    monkeypatch.setattr(servo_check, "PWM_ROOT", sysfs.root)
    monkeypatch.setattr(servo_check.time, "sleep", lambda s: None)
    return servo_check


def test_keep_with_a_spinning_pulse_is_refused_and_nothing_is_sent(check, sysfs, capsys):  # noqa: F811
    assert check.main(["--pulse", "0", "1600", "--keep"]) == 2
    assert sysfs.writes == []
    assert "would leave a continuous-rotation" in capsys.readouterr().out


def test_keep_at_neutral_is_allowed(check, sysfs, tmp_path):  # noqa: F811
    config = write(tmp_path, CALIBRATED_YAML)
    assert check.main(["--center", "--keep", "--config", str(config)]) == 0
    assert sysfs.value(0, "duty_cycle") == "1490000" and sysfs.value(1, "duty_cycle") == "1510000"
    assert sysfs.value(0, "enable") == "1"


def test_a_timed_spin_releases_and_explains_calibration(check, sysfs, capsys):  # noqa: F811
    assert check.main(["--pulse", "0", "1600", "--hold", "3"]) == 0
    assert [v for n, v in sysfs.writes if n == "pwm0/duty_cycle"] == ["1600000"]
    assert not (sysfs.chip / "pwm0").exists()
    assert "speed = turns x 360 / 40" in capsys.readouterr().out


def test_stop_ends_pulses_a_crashed_program_left_running(check, sysfs, capsys):  # noqa: F811
    for channel in (0, 1):
        sysfs.export(sysfs.chip, channel)
        (sysfs.chip / f"pwm{channel}" / "enable").write_text("1\n")
    assert check.main(["--stop"]) == 0
    assert not (sysfs.chip / "pwm0").exists() and not (sysfs.chip / "pwm1").exists()
    assert "stopped: GPIO18" in capsys.readouterr().out


def test_stop_with_nothing_running_says_so(check, capsys):
    assert check.main(["--stop"]) == 0
    assert "nothing was being driven" in capsys.readouterr().out


# -- a different speed each way ------------------------------------------------
#
# The robot's pan servo, measured: stop band 1387-1507 us, 3.25 turns in 20 s
# at 1597 us (58.5 deg/s) and 4.5 turns in 20 s at 1297 us (81 deg/s).

PAN = dict(neutral_us=1447.0, deadband_us=60.0, speed_offset_us=150.0,
           speed_deg_s=58.5, speed_below_deg_s=81.0)


def test_each_side_of_neutral_uses_its_own_speed():
    c = ContinuousCalibration(channel=0, **PAN)
    assert c.speed_for(150) == pytest.approx(58.5)
    assert c.speed_for(-150) == pytest.approx(-81.0)
    for speed in (-70.0, -5.0, 5.0, 50.0):
        assert c.speed_for(c.offset_for(speed)) == pytest.approx(speed)


def test_the_same_speed_needs_a_smaller_offset_on_the_faster_side():
    c = ContinuousCalibration(channel=0, **PAN)
    assert abs(c.offset_for(-40.0)) < abs(c.offset_for(40.0))


def test_inverted_keeps_each_speed_with_its_side_of_the_pulse():
    """Speed belongs to the pulse, not to the axis's sign convention."""
    c = ContinuousCalibration(channel=0, inverted=True, **PAN)
    offset = c.offset_for(40.0)                    # logical +, so the pulse goes below neutral
    assert c.pulse_for(offset) < 1447.0
    assert c.speed_for(offset) == pytest.approx(40.0)
    assert abs(offset) - 60.0 == pytest.approx(40.0 / (81.0 / 90.0))


def test_each_direction_is_capped_at_its_own_top_speed():
    c = ContinuousCalibration(channel=0, **PAN)
    assert c.max_speed_for(1.0) == pytest.approx((58.5 / 90.0) * 340.0)
    assert c.max_speed_for(-1.0) == pytest.approx((81.0 / 90.0) * 340.0)
    assert c.max_speed_deg_s == pytest.approx(c.max_speed_for(1.0)), "the slower way is what both promise"
    axis = ContinuousAxis(c)
    axis.update(math.radians(-1000), 0.0)
    assert axis.speed_deg_s == pytest.approx(-c.max_speed_for(-1.0))


def test_an_asymmetric_back_and_forth_comes_home():
    """The drift a single averaged speed would cause, avoided: out and back
    again lands where it started, as far as the model is concerned."""
    axis = ContinuousAxis(ContinuousCalibration(channel=0, **PAN))
    t, _ = run_axis(axis, 25.0, 3.0)
    run_axis(axis, 0.0, 3.0, start=t)
    assert axis.estimate_deg == pytest.approx(0.0, abs=ContinuousAxis.TOLERANCE_DEG)


def test_the_below_speed_is_read_validated_and_described(tmp_path):
    config = MotionConfig.load(write(tmp_path, (
        "servo: {model: mg995-360, driver: rpi-pwm}\n"
        "pan: {neutral_us: 1447, deadband_us: 60, speed_offset_us: 150, speed_deg_s: 58.5, speed_below_deg_s: 81}\n"
    )))
    assert config.calibration("pan").speed_below_deg_s == 81
    assert "58.5 deg/s above and 81 deg/s below neutral at 150 us" in config.describe()[1]


@pytest.mark.parametrize("text, message", [
    ("pan: {neutral_us: 1447, deadband_us: 60, speed_offset_us: 150, speed_deg_s: 58.5, speed_below_deg_s: 0}",
     "speed_below_deg_s must be above 0"),
    ("pan: {speed_below_deg_s: 81}", "only makes sense with the other four"),
])
def test_bad_below_speeds_are_refused(tmp_path, text, message):
    found = problems(tmp_path, "servo: {model: mg995-360, driver: rpi-pwm}\n" + text + "\n")
    assert any(message in p for p in found), found


def test_a_below_speed_on_a_positional_servo_is_refused(tmp_path):
    found = problems(tmp_path, "servo: {model: mg995}\npan: {speed_below_deg_s: 81}\n")
    assert any("only apply to a continuous-rotation servo" in p for p in found)


# -- neo-servo-check --move ------------------------------------------------------


@pytest.fixture
def fake_time(check, monkeypatch):
    """Time that passes only when the move loop sleeps, so a 3 s move takes none."""
    clock = Clock(1000.0)
    monkeypatch.setattr(check.time, "monotonic", clock)
    monkeypatch.setattr(check.time, "sleep", lambda s: setattr(clock, "now", clock.now + s))
    return clock


def test_move_goes_out_and_back_through_the_driver_then_stops(check, fake_time, sysfs, tmp_path, capsys):  # noqa: F811
    import re

    config = write(tmp_path, CALIBRATED_YAML)
    assert check.main(["--move", "20", "10", "--config", str(config)]) == 0
    pan = [int(v) for n, v in sysfs.writes if n == "pwm0/duty_cycle"]
    assert max(pan) > 1490000 + 20000, "pan turned out past its stop band"
    assert min(pan) < 1490000 - 20000, "and back the other way"
    assert pan[-1] == 1490000, "and ended stopped"
    assert not (sysfs.chip / "pwm0").exists() and not (sysfs.chip / "pwm1").exists(), "released"
    out = capsys.readouterr().out
    assert "out to pan +20, tilt +10" in out
    home = re.search(r"back at 0: .*estimate pan ([-+]\d+\.\d), tilt ([-+]\d+\.\d)", out)
    assert home and abs(float(home.group(1))) <= 1.0 and abs(float(home.group(2))) <= 1.0


def test_move_is_clamped_to_the_estimates_limits(check, fake_time, sysfs, tmp_path, capsys):  # noqa: F811
    config = write(tmp_path, CALIBRATED_YAML)
    assert check.main(["--move", "0", "90", "--config", str(config)]) == 0
    assert "tilt +90 deg is outside -30 to +30 deg; using +30" in capsys.readouterr().out


def test_move_refuses_uncalibrated_servos_and_sends_nothing(check, fake_time, sysfs, capsys):  # noqa: F811
    assert check.main(["--move", "10", "0"]) == 1
    assert sysfs.writes == []
    assert "needs both servos calibrated" in capsys.readouterr().out


# -- fixed speed: only three pulses, each measured ---------------------------------
#
# What the robot uses. Pan's speed is not a straight line of pulse width (79
# deg/s at 150 us below neutral, 83 deg/s at 128 us), so the proportional model
# overshot; a fixed-speed servo is only sent pulses that were measured directly.

FIXED = dict(neutral_us=1447.0, above_us=1597.0, above_deg_s=58.5, below_us=1352.0, below_deg_s=58.5)
FIXED_YAML = """\
servo: {model: mg995-360, driver: rpi-pwm}
pan: {neutral_us: 1447, above_us: 1597, above_deg_s: 58.5, below_us: 1352, below_deg_s: 58.5}
tilt: {neutral_us: 1488, above_us: 1677, above_deg_s: 45, below_us: 1299, below_deg_s: 45}
"""


def test_fixed_speed_sends_only_its_three_pulses():
    c = ContinuousCalibration(channel=0, **FIXED)
    assert c.fixed and c.calibrated
    assert c.pulse_for(c.offset_for(0.0)) == 1447.0
    assert c.pulse_for(c.offset_for(5.0)) == 1597.0, "any speed one way is the one measured pulse"
    assert c.pulse_for(c.offset_for(-500.0)) == 1352.0
    assert c.speed_for(c.offset_for(3.0)) == pytest.approx(58.5)
    assert c.max_speed_for(1.0) == pytest.approx(58.5)


def test_fixed_speed_inverted_swaps_the_direction_not_the_measurements():
    c = ContinuousCalibration(channel=0, inverted=True, **{**FIXED, "below_deg_s": 70.0})
    offset = c.offset_for(10.0)
    assert c.pulse_for(offset) == 1352.0
    assert c.speed_for(offset) == pytest.approx(70.0)


def test_a_fixed_speed_axis_lands_within_half_a_tick_using_nothing_but_its_pulses():
    axis = ContinuousAxis(ContinuousCalibration(channel=0, **FIXED))
    t, out = run_axis(axis, 20.0, 1.5)
    assert set(out) <= {1447.0, 1597.0, 1352.0}
    assert abs(axis.estimate_deg - 20.0) <= 58.5 * 0.02 / 2 + 1e-9
    assert out[-1] == 1447.0
    _, back = run_axis(axis, 0.0, 1.5, start=t)
    assert set(back) <= {1447.0, 1597.0, 1352.0}
    assert abs(axis.estimate_deg) <= 58.5 * 0.02 / 2 + 1e-9, "same speed both ways: home again"


def test_fixed_speed_config_loads_describes_and_caps_the_driver(tmp_path):
    config = MotionConfig.load(write(tmp_path, FIXED_YAML))
    assert config.calibrated and config.calibration("pan").fixed
    assert "fixed speed: stops at 1447 us, 58.5 deg/s at 1597 us, 58.5 deg/s at 1352 us" in config.describe()[1]
    limits = config.head_limits()
    assert math.degrees(limits.pan.max_speed_rad_s) == pytest.approx(58.5)
    assert math.degrees(limits.tilt.max_speed_rad_s) == pytest.approx(45)


@pytest.mark.parametrize("pan, message", [
    ("{neutral_us: 1447, above_us: 1597, above_deg_s: 58.5, below_us: 1352, below_deg_s: 58.5, deadband_us: 60}",
     "two different calibrations"),
    ("{neutral_us: 1447, above_us: 1597}", "a fixed-speed calibration needs"),
    ("{neutral_us: 1447, above_us: 1400, above_deg_s: 58.5, below_us: 1352, below_deg_s: 58.5}",
     "below_us < neutral_us < above_us"),
    ("{neutral_us: 1447, above_us: 1900, above_deg_s: 58.5, below_us: 1352, below_deg_s: 58.5}",
     "within 400 us of neutral_us"),
    ("{neutral_us: 1447, above_us: 1597, above_deg_s: 0, below_us: 1352, below_deg_s: 58.5}",
     "above_deg_s must be above 0"),
])
def test_bad_fixed_speed_calibrations_are_refused(tmp_path, pan, message):
    found = problems(tmp_path, f"servo: {{model: mg995-360, driver: rpi-pwm}}\npan: {pan}\n")
    assert any(message in p for p in found), found


def test_the_driver_and_a_fixed_speed_estimate_agree(sysfs, tmp_path):  # noqa: F811
    """The whole stack in fixed-speed mode: out to 20 deg and back through the
    driver, sending only the three measured pulses, ending home and stopped."""
    config = MotionConfig.load(write(tmp_path, FIXED_YAML))
    clock = Clock(0.0)
    b = RpiPwmContinuousBackend(**{**config.backend_kwargs(), "clock": clock, "watchdog": False})
    driver = ServoDriver(limits=config.head_limits(), backend=b)
    for target, ticks in ((20.0, 75), (0.0, 75)):
        for _ in range(ticks):
            driver.command(HeadCommand(pan_rad=math.radians(target), stamp=clock.now))
            driver.step(clock.now, 0.02)
            clock.now += 0.02
        assert b.estimate_deg[0] == pytest.approx(math.degrees(driver.pan_rad), abs=1.0)
    duties = {int(v) for n, v in sysfs.writes if n == "pwm0/duty_cycle"}
    assert duties <= {1447000, 1597000, 1352000}
    assert sysfs.value(0, "duty_cycle") == "1447000"
    assert abs(b.estimate_deg[0]) <= 1.0
