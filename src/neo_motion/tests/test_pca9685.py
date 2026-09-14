"""The PCA9685 backend, against a fake I2C bus that records register writes.

Nothing here needs a Pi: the bus is injected. What it pins down is what the
chip is actually told, because a wrong byte here is a servo slammed into its end
stop, not a failed assertion.
"""

from __future__ import annotations

import logging
import math
import sys
import types

import pytest

from neo_motion import backend as backend_module
from neo_motion.backend import (
    MockServoBackend,
    Pca9685,
    Pca9685Backend,
    ServoCalibration,
    make_backend,
)


class FakeBus:
    def __init__(self, present: bool = True) -> None:
        self.present = present
        self.bytes: list[tuple[int, int, int]] = []
        self.blocks: list[tuple[int, int, list[int]]] = []
        self.closed = False

    def read_byte_data(self, address, register):
        if not self.present:
            raise OSError(121, "Remote I/O error")
        return 0x11

    def write_byte_data(self, address, register, value):
        self.bytes.append((address, register, value))

    def write_i2c_block_data(self, address, register, data):
        self.blocks.append((address, register, list(data)))

    def close(self):
        self.closed = True


def make(fake=None, **kwargs):
    bus = fake or FakeBus()
    opened = []

    def factory(number):
        opened.append(number)
        return bus

    backend = Pca9685Backend(ServoCalibration(channel=0), ServoCalibration(channel=1),
                             bus_factory=factory, **kwargs)
    return backend, bus, opened


def pulse_of(block, frequency_hz=Pca9685.OSCILLATOR_HZ / (4096 * 122)):
    _addr, _reg, (on_l, on_h, off_l, off_h) = block
    assert (on_l, on_h) == (0, 0)
    return ((off_h << 8) | off_l) * (1_000_000 / frequency_hz) / 4096


def test_the_bus_is_not_opened_until_the_first_write():
    backend, bus, opened = make()
    assert opened == [] and bus.bytes == []
    backend.write(0.0, 0.0)
    assert opened == [1]


def test_startup_sets_50_hz_while_asleep_then_wakes():
    backend, bus, _ = make()
    backend.write(0.0, 0.0)
    assert bus.bytes == [
        (0x40, Pca9685.MODE1, Pca9685.SLEEP),
        (0x40, Pca9685.PRESCALE, 121),                 # 25 MHz / (4096 * 50) - 1
        (0x40, Pca9685.MODE2, Pca9685.OUTDRV),
        (0x40, Pca9685.MODE1, Pca9685.AUTO_INCREMENT),
        (0x40, Pca9685.MODE1, Pca9685.AUTO_INCREMENT | Pca9685.RESTART),
    ]
    backend.write(0.1, 0.1)
    assert len(bus.bytes) == 5, "the chip is configured once, not on every write"


def test_centre_is_1500_us_on_each_channels_own_register():
    backend, bus, _ = make()
    backend.write(0.0, 0.0)
    pan, tilt = bus.blocks
    assert (pan[1], tilt[1]) == (0x06, 0x0A)          # LED0_ON_L, LED1_ON_L
    assert pulse_of(pan) == pytest.approx(1500, abs=5)  # one tick is 4.9 us
    assert pulse_of(tilt) == pytest.approx(1500, abs=5)


def test_the_ends_of_travel_are_the_calibrated_pulses():
    backend, bus, _ = make()
    backend.write(math.radians(-90), math.radians(90))
    pan, tilt = bus.blocks
    assert pulse_of(pan) == pytest.approx(500, abs=5)
    assert pulse_of(tilt) == pytest.approx(2500, abs=5)


def test_pulses_use_the_frequency_the_chip_really_runs_at():
    chip = Pca9685(FakeBus())
    chip.start(50)
    assert chip.frequency_hz == pytest.approx(50.03, abs=0.01)
    assert chip.ticks_for(1500) == 307                  # 306.9 at 50.03 Hz, not 307.2 at 50


def test_ticks_are_clamped_to_the_12_bit_counter():
    chip = Pca9685(FakeBus())
    chip.start(50)
    assert chip.ticks_for(-10) == 0
    assert chip.ticks_for(1e9) == 4095


def test_release_holds_the_outputs_low_and_close_closes_the_bus():
    backend, bus, _ = make()
    backend.write(0.0, 0.0)
    bus.blocks.clear()
    backend.close()
    assert bus.blocks == [(0x40, 0x06, [0, 0, 0, 0x10]), (0x40, 0x0A, [0, 0, 0, 0x10])]
    assert bus.closed


def test_releasing_before_any_write_touches_nothing():
    backend, bus, opened = make()
    backend.close()
    assert opened == [] and bus.blocks == []


def test_a_missing_board_is_a_clear_error_and_the_bus_is_closed():
    backend, bus, _ = make(fake=FakeBus(present=False))
    with pytest.raises(RuntimeError, match=r"no PCA9685 answered at 0x40 on /dev/i2c-1.*i2cdetect"):
        backend.write(0.0, 0.0)
    assert bus.closed and bus.bytes == []


def test_address_and_bus_are_honoured():
    backend, bus, opened = make(address=0x41, bus=3)
    backend.write(0.0, 0.0)
    assert opened == [3]
    assert {addr for addr, _r, _v in bus.bytes} == {0x41}


@pytest.mark.parametrize("pan, tilt", [(0, 0), (16, 1), (-1, 1)])
def test_bad_channels_are_refused_at_construction(pan, tilt):
    with pytest.raises(ValueError):
        Pca9685Backend(ServoCalibration(channel=pan), ServoCalibration(channel=tilt),
                       bus_factory=lambda n: FakeBus())


def test_an_impossible_frequency_is_refused_at_construction():
    with pytest.raises(ValueError, match="cannot run at"):
        Pca9685Backend(ServoCalibration(channel=0), ServoCalibration(channel=1),
                       frequency_hz=5000, bus_factory=lambda n: FakeBus())


# -- choosing a backend ------------------------------------------------------


def _cals():
    return {"pan": ServoCalibration(channel=0), "tilt": ServoCalibration(channel=1)}


@pytest.fixture
def smbus2_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "smbus2", types.ModuleType("smbus2"))


def test_auto_without_smbus2_falls_back_and_says_how_to_install(monkeypatch, caplog):
    # See the identical comment in test_motion_config.py's caplog test.
    caplog.set_level(logging.WARNING)
    monkeypatch.setitem(sys.modules, "smbus2", None)  # import raises ImportError
    assert isinstance(make_backend("auto", **_cals()), MockServoBackend)
    assert "pip install -e '.[servo]'" in caplog.text


def test_auto_without_an_i2c_bus_falls_back_to_the_mock(monkeypatch, smbus2_installed):
    monkeypatch.setattr(backend_module.Path, "exists", lambda self: False)
    assert isinstance(make_backend("auto", **_cals()), MockServoBackend)


def test_asking_for_the_pca9685_without_a_bus_is_an_error(monkeypatch, smbus2_installed):
    monkeypatch.setattr(backend_module.Path, "exists", lambda self: False)
    with pytest.raises(RuntimeError, match=r"/dev/i2c-1 does not exist"):
        make_backend("pca9685", **_cals())


def test_auto_with_a_bus_picks_the_pca9685(monkeypatch, smbus2_installed):
    monkeypatch.setattr(backend_module.Path, "exists", lambda self: True)
    assert isinstance(make_backend("auto", **_cals()), Pca9685Backend)


# -- neo-servo-check ---------------------------------------------------------


@pytest.fixture
def check(monkeypatch, tmp_path):
    from neo_motion.scripts import servo_check

    # One config file, naming the PCA9685 driver: the committed config drives
    # the Pi's own PWM, and a developer's motion.local.yaml must not change
    # what these tests see either.
    pca9685_config = tmp_path / "motion-pca9685.yaml"
    pca9685_config.write_text("servo: {driver: pca9685}\n", encoding="utf-8")
    monkeypatch.setenv("NEO_MOTION_CONFIG", str(pca9685_config))
    fake = FakeBus()
    monkeypatch.setattr(servo_check.os.path, "exists", lambda p: True)
    monkeypatch.setattr(servo_check.os, "access", lambda p, mode: True)
    monkeypatch.setattr(servo_check, "open_smbus", lambda n: fake)
    monkeypatch.setattr(servo_check.time, "sleep", lambda s: None)
    return servo_check, fake


def test_the_check_is_read_only_unless_asked_to_move(check, capsys):
    servo_check, fake = check
    assert servo_check.main([]) == 0
    assert fake.bytes == [] and fake.blocks == []
    assert "nothing was moved" in capsys.readouterr().out
    assert fake.closed


def test_center_moves_both_servos_then_releases(check):
    servo_check, fake = check
    assert servo_check.main(["--center"]) == 0
    moved = [b for b in fake.blocks if b[2][3] != Pca9685.FULL_OFF]
    released = [b for b in fake.blocks if b[2][3] == Pca9685.FULL_OFF]
    assert [b[1] for b in moved] == [0x06, 0x0A]
    assert all(pulse_of(b) == pytest.approx(1500, abs=5) for b in moved)
    assert [b[1] for b in released] == [0x06, 0x0A]


def test_center_keep_leaves_them_driven(check):
    servo_check, fake = check
    assert servo_check.main(["--center", "--keep"]) == 0
    assert all(b[2][3] != Pca9685.FULL_OFF for b in fake.blocks)


def test_no_board_says_how_to_wire_it(check, monkeypatch, capsys):
    servo_check, fake = check
    fake.present = False
    assert servo_check.main([]) == 1
    out = capsys.readouterr().out
    assert "nothing answered at 0x40" in out and "SDA -> pin 3" in out


def test_no_i2c_device_says_how_to_enable_it(check, monkeypatch, capsys):
    servo_check, _fake = check
    monkeypatch.setattr(servo_check.os.path, "exists", lambda p: False)
    assert servo_check.main([]) == 1
    assert "dtparam=i2c_arm=on" in capsys.readouterr().out


def test_the_read_only_check_shows_the_config_it_would_drive_with(check, capsys):
    servo_check, _fake = check
    assert servo_check.main([]) == 0
    out = capsys.readouterr().out
    assert "MG995" in out and "UNCALIBRATED" in out and "1200-1800 us" in out
    assert "at least 3 A" in out


def test_pulse_moves_one_channel_then_releases_it(check):
    servo_check, fake = check
    assert servo_check.main(["--pulse", "1", "1350"]) == 0
    moved = [b for b in fake.blocks if b[2][3] != Pca9685.FULL_OFF]
    released = [b for b in fake.blocks if b[2][3] == Pca9685.FULL_OFF]
    assert [b[1] for b in moved] == [0x0A]
    assert pulse_of(moved[0]) == pytest.approx(1350, abs=5)
    assert [b[1] for b in released] == [0x0A]


@pytest.mark.parametrize("args", [
    ["--pulse", "0", "2600"],      # past the MG995's hard limit
    ["--pulse", "0", "400"],
    ["--pulse", "16", "1500"],     # not a PCA9685 channel
    ["--pulse", "0.5", "1500"],
])
def test_a_bad_pulse_is_refused_before_the_bus_is_opened(check, monkeypatch, args):
    servo_check, _fake = check
    opened = []
    monkeypatch.setattr(servo_check, "open_smbus", lambda n: opened.append(n))
    assert servo_check.main(args) == 2
    assert opened == []


def test_center_goes_to_the_calibrated_zero_not_1500(check, tmp_path):
    servo_check, fake = check
    config = tmp_path / "motion.yaml"
    config.write_text("pan: {min_us: 1000, max_us: 2400, min_deg: -60, max_deg: 80}\n", encoding="utf-8")
    assert servo_check.main(["--center", "--config", str(config)]) == 0
    pan = next(b for b in fake.blocks if b[1] == 0x06 and b[2][3] != Pca9685.FULL_OFF)
    assert pulse_of(pan) == pytest.approx(1600, abs=5)      # 0 deg is 60/140 of the way


def test_a_broken_config_drives_nothing(check, capsys, tmp_path):
    servo_check, fake = check
    config = tmp_path / "motion.yaml"
    config.write_text("pan: {min_us: 700}\n", encoding="utf-8")
    assert servo_check.main(["--center", "--config", str(config)]) == 1
    assert fake.bytes == [] and fake.blocks == []
    assert "set all four" in capsys.readouterr().out
