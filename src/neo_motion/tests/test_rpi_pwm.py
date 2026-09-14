"""The Pi's hardware PWM backend, against a simulated /sys/class/pwm.

The simulation does what the kernel does for everything the backend touches:
writing a channel number to `export` creates `pwmN/` with its files, and
`unexport` removes it. What it cannot do is prove the real controller, overlay
and udev rule behave the same -- that is checked on the Pi.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from neo_motion import backend as backend_module
from neo_motion.backend import (
    MockServoBackend,
    RpiPwmBackend,
    ServoCalibration,
    SysfsPwmChip,
    make_backend,
)
from neo_motion.config import MotionConfig, MotionConfigError

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config" / "motion.yaml"


class FakeSysfs:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.writes: list[tuple[str, str]] = []
        self.chip = self.add_chip(0)

    def add_chip(self, index: int, compatible: bytes = b"brcm,bcm2835-pwm\x00", npwm: int = 2) -> Path:
        chip = self.root / f"pwmchip{index}"
        (chip / "device" / "of_node").mkdir(parents=True)
        (chip / "device" / "of_node" / "compatible").write_bytes(compatible)
        (chip / "npwm").write_text(f"{npwm}\n")
        (chip / "export").write_text("")
        (chip / "unexport").write_text("")
        return chip

    def export(self, chip: Path, channel: int) -> None:
        ch = chip / f"pwm{channel}"
        ch.mkdir()
        for name in ("period", "duty_cycle", "enable"):
            (ch / name).write_text("0\n")

    def value(self, channel: int, name: str) -> str:
        return (self.chip / f"pwm{channel}" / name).read_text().strip()

    def names(self) -> list[str]:
        return [name for name, _value in self.writes]


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    fake = FakeSysfs(tmp_path / "pwm")
    real_write = SysfsPwmChip._write

    def write(self, path, value):
        fake.writes.append((f"{path.parent.name}/{path.name}", str(value)))
        if path.name == "export":
            fake.export(path.parent, int(value))
        elif path.name == "unexport":
            shutil.rmtree(path.parent / f"pwm{value}")
        else:
            real_write(self, path, value)

    monkeypatch.setattr(SysfsPwmChip, "_write", write)
    monkeypatch.setattr(backend_module, "PWM_ROOT", fake.root)
    return fake


def cals(pan=0, tilt=1, **kwargs):
    return ServoCalibration(channel=pan, **kwargs), ServoCalibration(channel=tilt, **kwargs)


# -- finding the controller -------------------------------------------------


def test_the_pis_controller_is_found_by_what_it_is_not_its_number(sysfs):
    other = sysfs.root / "pwmchip0"
    shutil.rmtree(other)
    sysfs.add_chip(0, compatible=b"some,other-pwm\x00")
    sysfs.chip = sysfs.add_chip(3)
    assert SysfsPwmChip.find(sysfs.root).path.name == "pwmchip3"


def test_no_controller_names_the_overlay(tmp_path):
    with pytest.raises(RuntimeError, match="dtoverlay=pwm-2chan"):
        SysfsPwmChip.find(tmp_path / "nothing-here")


def test_only_foreign_controllers_points_at_pwm_chip(sysfs):
    shutil.rmtree(sysfs.chip)
    sysfs.add_chip(0, compatible=b"some,other-pwm\x00")
    with pytest.raises(RuntimeError, match="servo.pwm_chip"):
        SysfsPwmChip.find(sysfs.root)


def test_a_named_chip_that_is_missing_is_an_error(sysfs):
    with pytest.raises(RuntimeError, match="pwmchip5 does not exist"):
        SysfsPwmChip.find(sysfs.root, index=5)


# -- driving it ---------------------------------------------------------------


def test_nothing_is_touched_until_the_first_write(sysfs):
    RpiPwmBackend(*cals())
    assert sysfs.writes == []


def test_first_write_exports_sets_50_hz_then_duty_before_enable(sysfs):
    backend = RpiPwmBackend(*cals())
    backend.write(0.0, 0.0)
    assert sysfs.names() == [
        "pwmchip0/export", "pwm0/period",
        "pwmchip0/export", "pwm1/period",
        "pwm0/duty_cycle", "pwm0/enable",
        "pwm1/duty_cycle", "pwm1/enable",
    ]
    for channel in (0, 1):
        assert sysfs.value(channel, "period") == "20000000"      # 50 Hz, in ns
        assert sysfs.value(channel, "duty_cycle") == "1500000"   # 1500 us
        assert sysfs.value(channel, "enable") == "1"


def test_the_ends_of_travel_are_the_calibrated_pulses(sysfs):
    backend = RpiPwmBackend(*cals())                              # 500-2500 us across +/-90 deg
    import math

    backend.write(math.radians(-90), math.radians(90))
    assert sysfs.value(0, "duty_cycle") == "500000"
    assert sysfs.value(1, "duty_cycle") == "2500000"


def test_an_unchanged_position_is_not_rewritten(sysfs):
    backend = RpiPwmBackend(*cals())
    for _ in range(5):
        backend.write(0.0, 0.0)
    assert sysfs.names().count("pwm0/duty_cycle") == 1
    assert sysfs.names().count("pwm0/enable") == 1


def test_release_stops_the_pulses_and_close_unexports(sysfs):
    backend = RpiPwmBackend(*cals())
    backend.write(0.0, 0.0)
    backend.release()
    assert sysfs.value(0, "enable") == "0" and sysfs.value(1, "enable") == "0"
    backend.close()
    assert not (sysfs.chip / "pwm0").exists() and not (sysfs.chip / "pwm1").exists()


def test_a_leftover_duty_cycle_longer_than_the_period_is_cleared_first(sysfs):
    sysfs.export(sysfs.chip, 0)
    (sysfs.chip / "pwm0" / "duty_cycle").write_text("30000000\n")   # from a slower frame rate
    SysfsPwmChip(sysfs.chip).open_channel(0, 50)
    assert sysfs.names() == ["pwm0/duty_cycle", "pwm0/period"]
    assert sysfs.value(0, "duty_cycle") == "0" and sysfs.value(0, "period") == "20000000"


@pytest.mark.parametrize("pan, tilt", [(0, 2), (0, 0), (-1, 1)])
def test_only_the_two_hardware_channels_are_accepted(pan, tilt):
    with pytest.raises(ValueError):
        RpiPwmBackend(*cals(pan, tilt))


def test_files_that_stay_root_only_after_export_name_the_pwm_group(sysfs, monkeypatch):
    monkeypatch.setattr(SysfsPwmChip, "EXPORT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(backend_module.os, "access", lambda path, mode: False)
    with pytest.raises(RuntimeError, match="pwm group"):
        RpiPwmBackend(*cals()).write(0.0, 0.0)


def test_a_permission_error_names_the_pwm_group(tmp_path, monkeypatch):
    fake = FakeSysfs(tmp_path / "pwm")

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(backend_module, "open", denied, raising=False)
    with pytest.raises(RuntimeError, match="pwm group"):
        SysfsPwmChip(fake.chip).open_channel(0, 50)


# -- choosing it --------------------------------------------------------------


def test_a_positional_config_drives_the_pis_hardware_pwm(sysfs, monkeypatch, tmp_path):
    positional = tmp_path / "positional.yaml"
    positional.write_text("servo: {model: mg995, driver: rpi-pwm}\n", encoding="utf-8")
    monkeypatch.setenv("NEO_MOTION_CONFIG", str(positional))
    backend = make_backend("auto")
    assert isinstance(backend, RpiPwmBackend)
    assert (backend.pan_cal.channel, backend.tilt_cal.channel, backend.frequency_hz) == (0, 1, 50)
    assert (backend.pan_cal.min_us, backend.pan_cal.max_us) == (1200, 1800)


def test_without_pwm_hardware_auto_falls_back_and_says_how_to_enable_it(tmp_path, monkeypatch, caplog):
    # See the identical comment in test_motion_config.py's caplog test.
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(backend_module, "PWM_ROOT", tmp_path / "no-pwm")
    monkeypatch.setenv("NEO_MOTION_CONFIG", str(REPO_CONFIG))
    assert isinstance(make_backend("auto"), MockServoBackend)
    assert "pwm-2chan" in caplog.text


def test_naming_the_driver_makes_missing_hardware_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_module, "PWM_ROOT", tmp_path / "no-pwm")
    monkeypatch.setenv("NEO_MOTION_CONFIG", str(REPO_CONFIG))
    with pytest.raises(RuntimeError, match="rpi-pwm backend unavailable"):
        make_backend("rpi-pwm")


def test_asking_for_a_driver_the_config_does_not_use_is_refused(monkeypatch):
    monkeypatch.setenv("NEO_MOTION_CONFIG", str(REPO_CONFIG))
    with pytest.raises(ValueError, match="configures rpi-pwm"):
        make_backend("pca9685")


# -- config -------------------------------------------------------------------


def _problems(tmp_path, text):
    path = tmp_path / "motion.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(MotionConfigError) as exc:
        MotionConfig.load(path)
    return exc.value.problems


def test_rpi_pwm_has_only_channels_0_and_1(tmp_path):
    found = _problems(tmp_path, "servo: {driver: rpi-pwm}\ntilt: {channel: 4}\n")
    assert any("GPIO19, pin 35" in p for p in found)


def test_an_unknown_driver_is_refused(tmp_path):
    assert any("servo.driver 'gpio' is not known" in p for p in _problems(tmp_path, "servo: {driver: gpio}\n"))


def test_a_negative_pwm_chip_is_refused(tmp_path):
    assert any("pwm_chip" in p for p in _problems(tmp_path, "servo: {driver: rpi-pwm, pwm_chip: -1}\n"))


def test_the_description_names_the_header_pins():
    lines = MotionConfig.load(REPO_CONFIG).describe()
    assert "the Pi's hardware PWM" in lines[0]
    assert "GPIO18, pin 12" in lines[1] and "GPIO19, pin 35" in lines[2]


# -- neo-servo-check ------------------------------------------------------------


@pytest.fixture
def check(sysfs, monkeypatch):
    from neo_motion.scripts import servo_check

    monkeypatch.setenv("NEO_MOTION_CONFIG", str(REPO_CONFIG))
    monkeypatch.setattr(servo_check, "PWM_ROOT", sysfs.root)
    monkeypatch.setattr(servo_check.time, "sleep", lambda s: None)
    return servo_check


def test_the_read_only_check_writes_nothing(check, sysfs, capsys):
    assert check.main([]) == 0
    out = capsys.readouterr().out
    assert sysfs.writes == []
    assert "nothing was moved" in out and "GPIO18, pin 12" in out and "at least 3 A" in out


def test_center_drives_both_then_lets_go(check, sysfs):
    assert check.main(["--center"]) == 0
    assert [v for n, v in sysfs.writes if n.endswith("/duty_cycle")] == ["1500000", "1500000"]
    assert not (sysfs.chip / "pwm0").exists() and not (sysfs.chip / "pwm1").exists()


def test_center_keep_leaves_them_driven(check, sysfs):
    assert check.main(["--center", "--keep"]) == 0
    assert sysfs.value(0, "enable") == "1" and sysfs.value(1, "enable") == "1"


def test_pulse_drives_only_that_pin(check, sysfs, tmp_path):
    # Positional: --keep off centre is refused for the committed continuous servos.
    positional = tmp_path / "positional.yaml"
    positional.write_text("servo: {model: mg995, driver: rpi-pwm}\n", encoding="utf-8")
    assert check.main(["--pulse", "1", "1350", "--keep", "--config", str(positional)]) == 0
    assert not (sysfs.chip / "pwm0").exists()
    assert sysfs.value(1, "duty_cycle") == "1350000"


@pytest.mark.parametrize("args", [["--pulse", "2", "1500"], ["--pulse", "0", "2600"]])
def test_a_bad_pulse_touches_nothing(check, sysfs, args):
    assert check.main(args) == 2
    assert sysfs.writes == []


def test_no_pwm_overlay_says_how_to_enable_it(check, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check, "PWM_ROOT", tmp_path / "no-pwm")
    assert check.main(["--center"]) == 1
    assert "--servo-pwm" in capsys.readouterr().out


def test_no_permission_says_how_to_get_it(check, sysfs, monkeypatch, capsys):
    monkeypatch.setattr(check.os, "access", lambda path, mode: False)
    assert check.main(["--center"]) == 1
    assert sysfs.writes == []
    assert "pwm group" in capsys.readouterr().out
