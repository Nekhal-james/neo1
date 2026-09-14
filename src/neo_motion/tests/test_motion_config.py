"""neo_motion.config: the servo model, its wiring and calibration, and what uses them."""

from __future__ import annotations

import math
import sys
import types
from pathlib import Path

import pytest

from neo_motion import backend as backend_module
from neo_motion import config as config_module
from neo_motion.backend import Pca9685Backend, make_backend
from neo_motion.config import MG995, MotionConfig, MotionConfigError
from neo_motion.types import HeadLimits

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config" / "motion.yaml"

POSITIONAL = "servo: {model: mg995, driver: rpi-pwm}\n"

CALIBRATED = """\
pan: {min_us: 640, max_us: 2360, min_deg: -80, max_deg: 80}
tilt: {min_us: 1150, max_us: 1850, min_deg: -30, max_deg: 30}
"""


def write(tmp_path, text, name="motion.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def problems(tmp_path, text):
    with pytest.raises(MotionConfigError) as exc:
        MotionConfig.load(write(tmp_path, text))
    return exc.value.problems


# -- the MG995 profile --------------------------------------------------------


def test_the_safe_window_is_clear_of_the_end_stops_under_both_published_ranges():
    """The sheets give 0.5-2.5 ms or 1-2 ms for 180 deg. The window must be inside
    the travel under both, or an uncalibrated servo can be driven into a stop."""
    for low_us, high_us in ((500, 2500), (1000, 2000)):
        assert low_us < MG995.safe_min_us and MG995.safe_max_us < high_us


def test_the_safe_window_label_is_exact_under_the_0_5_to_2_5_ms_reading():
    """No label is right under both readings (this once claimed +/-30 deg was
    "the smaller reading"; the smaller reading is 27). It is labelled for the
    0.5-2.5 ms sheet, and under 1-2 ms the head turns about twice as far --
    still inside the travel, which is what the window protects."""
    half_window_us = (MG995.safe_max_us - MG995.safe_min_us) / 2
    assert MG995.safe_half_range_deg == pytest.approx(half_window_us / ((2500 - 500) / 180))
    assert half_window_us / ((2000 - 1000) / 180) < 90


def test_the_mg995_is_driven_at_50_hz():
    assert MG995.frequency_hz == 50


# -- the committed file -------------------------------------------------------


def test_the_committed_config_is_uncalibrated_on_channels_0_and_1():
    config = MotionConfig.load(REPO_CONFIG)
    assert (config.model, config.driver) == ("mg995-360", "rpi-pwm")
    assert (config.i2c_bus, config.address, config.pwm_chip) == (1, 0x40, None)
    assert (config.pan.channel, config.tilt.channel) == (0, 1)
    assert not config.calibrated


def test_an_uncalibrated_axis_is_held_to_the_safe_window(tmp_path):
    cal = MotionConfig.load(write(tmp_path, POSITIONAL)).calibration("pan")
    assert (cal.min_us, cal.max_us) == (1200, 1800)
    assert cal.to_pulse_us(0.0) == pytest.approx(1500)
    assert cal.to_pulse_us(math.radians(90)) == pytest.approx(1800), "never past the window"
    assert cal.to_pulse_us(math.radians(-90)) == pytest.approx(1200)


def test_the_driver_limits_are_narrowed_to_the_calibration(tmp_path):
    limits = MotionConfig.load(write(tmp_path, POSITIONAL)).head_limits()
    for axis in (limits.pan, limits.tilt):
        assert math.degrees(axis.min_rad) == pytest.approx(-27)
        assert math.degrees(axis.max_rad) == pytest.approx(27)
    base = HeadLimits()
    assert limits.pan.max_speed_rad_s == base.pan.max_speed_rad_s
    assert limits.tilt.deadband_rad == base.tilt.deadband_rad


def test_limits_are_never_widened_past_the_drivers_own(tmp_path):
    config = MotionConfig.load(write(tmp_path, "pan: {min_us: 500, max_us: 2500, min_deg: -120, max_deg: 120}\n"))
    assert math.degrees(config.head_limits().pan.max_rad) == pytest.approx(90)


# -- layering -----------------------------------------------------------------


def test_the_local_file_calibrates_without_repeating_the_rest(tmp_path, monkeypatch):
    committed = write(tmp_path, POSITIONAL)
    local = write(tmp_path, CALIBRATED, "motion.local.yaml")
    monkeypatch.delenv("NEO_MOTION_CONFIG", raising=False)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG", committed)
    monkeypatch.setattr(config_module, "LOCAL_CONFIG", local)

    config = MotionConfig.load()
    assert config.calibrated and config.model == "mg995" and config.pan.channel == 0
    cal = config.calibration("pan")
    assert (cal.min_us, cal.max_us) == (640, 2360)
    assert cal.to_pulse_us(0.0) == pytest.approx(1500)


def test_the_env_var_names_exactly_one_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "LOCAL_CONFIG", write(tmp_path, CALIBRATED, "motion.local.yaml"))
    monkeypatch.setenv("NEO_MOTION_CONFIG", str(REPO_CONFIG))
    assert not MotionConfig.load().calibrated


# -- refusals -----------------------------------------------------------------


def test_half_a_calibration_is_refused(tmp_path):
    assert any("set all four" in p for p in problems(tmp_path, "pan: {min_us: 700, max_us: 2300}\n"))


def test_a_misspelt_field_is_refused_with_a_suggestion(tmp_path):
    assert any("did you mean 'max_us'" in p for p in problems(tmp_path, "pan: {max_usec: 2300}\n"))


def test_pulses_outside_the_mg995_are_refused(tmp_path):
    found = problems(tmp_path, "pan: {min_us: 300, max_us: 2300, min_deg: -80, max_deg: 80}\n")
    assert any("within 500-2500 us" in p for p in found)


def test_reversed_pulses_point_at_inverted(tmp_path):
    found = problems(tmp_path, "pan: {min_us: 2300, max_us: 700, min_deg: -80, max_deg: 80}\n")
    assert any("inverted: true" in p for p in found)


def test_centre_must_lie_inside_the_calibrated_angles(tmp_path):
    found = problems(tmp_path, "pan: {min_us: 1000, max_us: 2000, min_deg: 0, max_deg: 90}\n")
    assert any("0 deg is where the head centres" in p for p in found)


def test_an_unknown_servo_model_is_refused(tmp_path):
    assert any("not a known servo" in p for p in problems(tmp_path, "servo: {model: sg90}\n"))


def test_a_boolean_is_not_a_channel(tmp_path):
    assert any("channel must be" in p for p in problems(tmp_path, "pan: {channel: true}\n"))


def test_every_problem_is_named_at_once(tmp_path):
    found = problems(tmp_path, 'servo: {address: 0x90}\npan: {channel: 1, inverted: "yes"}\nwheels: {}\n')
    assert len(found) == 4, found


def test_broken_yaml_and_a_missing_file_are_config_errors(tmp_path):
    with pytest.raises(MotionConfigError, match="not valid YAML"):
        MotionConfig.load(write(tmp_path, "pan: [unclosed\n"))
    with pytest.raises(MotionConfigError, match="file not found"):
        MotionConfig.load(tmp_path / "nope.yaml")


# -- building the backend ----------------------------------------------------


@pytest.fixture
def robot(monkeypatch, tmp_path):
    """smbus2 importable and /dev/i2c-1 present, as on a Pi with a PCA9685."""
    monkeypatch.setitem(sys.modules, "smbus2", types.ModuleType("smbus2"))
    monkeypatch.setattr(backend_module.Path, "exists", lambda self: True)
    monkeypatch.setenv("NEO_MOTION_CONFIG", str(write(tmp_path, "servo: {driver: pca9685}\n", "pca9685.yaml")))


def test_make_backend_with_no_calibrations_builds_from_the_config(robot, caplog):
    """It used to raise TypeError: the PCA9685 backend needs calibrations, and the
    servo node called make_backend() with none."""
    backend = make_backend("auto")
    assert isinstance(backend, Pca9685Backend)
    assert (backend.pan_cal.min_us, backend.pan_cal.max_us) == (1200, 1800)
    assert (backend.pan_cal.channel, backend.tilt_cal.channel) == (0, 1)
    assert (backend.address, backend.frequency_hz, backend.bus_number) == (0x40, 50, 1)
    assert "uncalibrated" in caplog.text


def test_a_broken_config_stops_the_robot_backend(robot, tmp_path, monkeypatch):
    monkeypatch.setenv("NEO_MOTION_CONFIG", str(write(tmp_path, "pan: {min_us: 700}\n")))
    with pytest.raises(MotionConfigError):
        make_backend("auto")
