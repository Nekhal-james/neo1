"""Where the angles actually go.

Same seam as the detector and the robot bridge: a real backend and a recording
one behind a single interface, so every safety rule above this line is testable
with no I2C bus and no servos to break while testing it.

Two real backends, and neither is software PWM: software PWM on a busy Pi
produces jitter you can see in a face (CLAUDE.md). `RpiPwmBackend` drives the
Pi's own two *hardware* PWM channels (GPIO18, GPIO19), clocked by the SoC's PWM
block whatever the CPU is doing; `Pca9685Backend` drives a PCA9685 board over
I2C, whose own oscillator does the same job. `config/motion.yaml` picks one.
"""

from __future__ import annotations

import abc
import atexit
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ServoCalibration:
    """Pulse width at each end of one servo's travel.

    Measured per servo by the calibration script, never assumed: two servos of
    the same part number differ by several degrees, and the difference between
    "centred" and "straining against an end stop" is a few dozen microseconds.
    """

    channel: int
    min_us: float = 500.0
    max_us: float = 2500.0
    min_rad: float = math.radians(-90.0)
    max_rad: float = math.radians(90.0)
    inverted: bool = False
    """Set when the servo is mounted so positive angle drives it the wrong way.
    Cheaper and far clearer than negating angles somewhere upstream."""

    def to_pulse_us(self, angle_rad: float) -> float:
        span_rad = self.max_rad - self.min_rad
        if span_rad == 0:
            return (self.min_us + self.max_us) / 2.0
        t = (angle_rad - self.min_rad) / span_rad
        t = max(0.0, min(1.0, t))
        if self.inverted:
            t = 1.0 - t
        return self.min_us + t * (self.max_us - self.min_us)


@dataclass
class ContinuousCalibration:
    """A continuous-rotation (360 deg) servo, where the pulse sets speed, not angle.

    Two ways to calibrate one, both measured per servo, because no two stop at
    the same pulse:

    * **Fixed speed** (`above_us` and `below_us` set -- what the robot uses):
      the servo is only ever sent three pulses. `neutral_us` stops it,
      `above_us` turns it one way at `above_deg_s`, `below_us` the other way at
      `below_deg_s`. Every pulse it is sent was measured directly, so nothing
      depends on how speed varies between them -- which matters, because it
      does not vary in a straight line. Measured on the robot's pan servo: 79
      deg/s at 150 us below neutral and 83 deg/s at 128 us. A proportional
      model that assumed a straight line sent the 128 us pulse expecting 60
      deg/s, and the head overshot its return by 5-7 deg.
    * **Proportional** (otherwise): speed modelled as linear from the edge of
      the stop band (`deadband_us`), measured at `speed_offset_us` above
      neutral (`speed_deg_s`) and below it (`speed_below_deg_s`, which defaults
      to the same). Smoother, and right only where the servo really is linear.

    Either way the directions are measured separately -- the pan servo is 38 %
    faster one way -- and either way the servo reports nothing back, so the
    head's angle is an estimate that drifts, not a measurement.
    """

    channel: int
    neutral_us: float = 1500.0
    deadband_us: float = 0.0
    speed_offset_us: float = 0.0
    speed_deg_s: float = 0.0
    """Proportional: measured at neutral + speed_offset_us (pulses above neutral)."""
    speed_below_deg_s: float = 0.0
    """Proportional: measured at neutral - speed_offset_us. 0 means the same as speed_deg_s."""
    above_us: float = 0.0
    above_deg_s: float = 0.0
    below_us: float = 0.0
    below_deg_s: float = 0.0
    """Fixed speed: the one pulse used each side of neutral, and the speed measured at it."""
    max_offset_us: float = 400.0
    min_deg: float = -30.0
    max_deg: float = 30.0
    """Limits on the *estimated* angle, from wherever the head started."""
    inverted: bool = False

    @property
    def fixed(self) -> bool:
        return self.above_us > 0 and self.below_us > 0

    @property
    def calibrated(self) -> bool:
        if self.fixed:
            return (self.above_deg_s > 0 and self.below_deg_s > 0
                    and self.below_us < self.neutral_us < self.above_us)
        return (self.speed_deg_s > 0 and self.speed_below_deg_s >= 0
                and self.speed_offset_us > self.deadband_us >= 0)

    def _physical_sign(self, logical: float) -> float:
        """Which side of neutral the *pulse* is on. Speed belongs to the pulse,
        so `inverted` must not swap which measured speed applies."""
        return -logical if self.inverted else logical

    def _gain(self, physical_sign: float) -> float:
        above = physical_sign > 0 or not self.speed_below_deg_s
        speed = self.speed_deg_s if above else self.speed_below_deg_s
        return speed / (self.speed_offset_us - self.deadband_us)

    @property
    def gain(self) -> float:
        """Proportional: degrees per second per microsecond beyond the stop band, above neutral."""
        return self._gain(1.0)

    def max_speed_for(self, logical_sign: float) -> float:
        physical = self._physical_sign(logical_sign)
        if self.fixed:
            return self.above_deg_s if physical > 0 else self.below_deg_s
        return self._gain(physical) * (self.max_offset_us - self.deadband_us)

    @property
    def max_speed_deg_s(self) -> float:
        """The slower direction's top speed: the most that can be promised both ways."""
        return min(self.max_speed_for(1.0), self.max_speed_for(-1.0))

    def offset_for(self, speed_deg_s: float) -> float:
        """The offset from neutral to send for this speed (logical sign)."""
        if speed_deg_s == 0:
            return 0.0
        physical = self._physical_sign(speed_deg_s)
        if self.fixed:
            physical_offset = (self.above_us if physical > 0 else self.below_us) - self.neutral_us
            return -physical_offset if self.inverted else physical_offset
        gain = self._gain(physical)
        magnitude = min(self.deadband_us + abs(speed_deg_s) / gain, self.max_offset_us)
        return math.copysign(magnitude, speed_deg_s)

    def speed_for(self, offset_us: float) -> float:
        if offset_us == 0:
            return 0.0
        physical = self._physical_sign(offset_us)
        if self.fixed:
            return math.copysign(self.above_deg_s if physical > 0 else self.below_deg_s, offset_us)
        beyond = min(abs(offset_us), self.max_offset_us) - self.deadband_us
        if beyond <= 0:
            return 0.0
        return math.copysign(self._gain(physical) * beyond, offset_us)

    def pulse_for(self, offset_us: float) -> float:
        return self.neutral_us + (-offset_us if self.inverted else offset_us)


class ServoBackend(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    def write(self, pan_rad: float, tilt_rad: float) -> None: ...

    def release(self) -> None:
        """Stop driving the servos, letting them go limp.

        Only for a deliberate shutdown. A head that goes limp mid-operation
        drops under its own weight, so nothing in the normal control path calls
        this -- holding position is the safe state, not releasing.
        """

    def close(self) -> None:
        self.release()


class MockServoBackend(ServoBackend):
    """Records what would have been written. Used by the tests and by the panel."""

    name = "mock"

    def __init__(self) -> None:
        self.writes: list[tuple[float, float]] = []
        self.released = False

    def write(self, pan_rad: float, tilt_rad: float) -> None:
        self.writes.append((pan_rad, tilt_rad))
        self.released = False

    def release(self) -> None:
        self.released = True

    @property
    def last(self) -> tuple[float, float] | None:
        return self.writes[-1] if self.writes else None


class Pca9685:
    """The five PCA9685 registers this robot uses, written with smbus2.

    Not Adafruit's CircuitPython driver. On Ubuntu for the Pi that stack cannot
    even be imported without a GPIO library: `import board` wants RPi.GPIO for
    the pin table, and `import adafruit_pca9685` pulls in `digitalio`, which
    wants it too -- measured on a Pi 4 running Ubuntu 24.04, both fail with
    "The platform library 'RPi' was not found". A GPIO shim, a dozen packages
    and a board-detection layer, for a chip this code only ever speaks I2C to.
    smbus2 is pure Python and opens /dev/i2c-1 as a member of the i2c group.
    """

    MODE1 = 0x00
    MODE2 = 0x01
    PRESCALE = 0xFE
    LED0_ON_L = 0x06

    RESTART = 0x80
    SLEEP = 0x10
    AUTO_INCREMENT = 0x20
    OUTDRV = 0x04          # totem-pole outputs, which is what a servo input wants
    FULL_OFF = 0x10        # bit 4 of LEDn_OFF_H: output held low

    OSCILLATOR_HZ = 25_000_000.0
    """Nominal. Real boards run a few percent off, which moves every pulse by the
    same factor -- the per-servo calibration absorbs it, so it is not tuned here."""

    TICKS = 4096

    def __init__(self, bus, address: int = 0x40) -> None:
        self.bus = bus
        self.address = address
        self.frequency_hz = 0.0

    @classmethod
    def prescale_for(cls, frequency_hz: float) -> int:
        prescale = round(cls.OSCILLATOR_HZ / (cls.TICKS * frequency_hz)) - 1
        if not 3 <= prescale <= 255:
            raise ValueError(f"PCA9685 cannot run at {frequency_hz} Hz (about 24-1526 Hz)")
        return prescale

    def probe(self) -> int:
        """Read MODE1. Raises OSError when nothing answers at this address."""
        return self.bus.read_byte_data(self.address, self.MODE1)

    def start(self, frequency_hz: float) -> None:
        """Set the PWM frequency and wake the oscillator.

        The prescaler can only be written while asleep. After waking, the
        oscillator needs 500 us to settle before outputs are meaningful.
        """
        prescale = self.prescale_for(frequency_hz)
        self.bus.write_byte_data(self.address, self.MODE1, self.SLEEP)
        self.bus.write_byte_data(self.address, self.PRESCALE, prescale)
        self.bus.write_byte_data(self.address, self.MODE2, self.OUTDRV)
        self.bus.write_byte_data(self.address, self.MODE1, self.AUTO_INCREMENT)
        time.sleep(0.005)
        self.bus.write_byte_data(self.address, self.MODE1, self.AUTO_INCREMENT | self.RESTART)
        # What the chip actually runs at, which is what pulse widths must be
        # converted against -- 50 Hz requested is 50.03 Hz delivered.
        self.frequency_hz = self.OSCILLATOR_HZ / (self.TICKS * (prescale + 1))

    def ticks_for(self, pulse_us: float) -> int:
        period_us = 1_000_000.0 / self.frequency_hz
        return max(0, min(self.TICKS - 1, round(pulse_us * self.TICKS / period_us)))

    def set_pulse(self, channel: int, pulse_us: float) -> None:
        off = self.ticks_for(pulse_us)
        self.bus.write_i2c_block_data(
            self.address, self.LED0_ON_L + 4 * channel, [0, 0, off & 0xFF, (off >> 8) & 0x0F]
        )

    def set_off(self, channel: int) -> None:
        self.bus.write_i2c_block_data(
            self.address, self.LED0_ON_L + 4 * channel, [0, 0, 0, self.FULL_OFF]
        )


def open_smbus(bus_number: int):
    from smbus2 import SMBus

    return SMBus(bus_number)


class Pca9685Backend(ServoBackend):
    """Real hardware: two channels on a PCA9685 over I2C.

    Opens the bus lazily, on the first write, so this module stays importable
    and constructible on a laptop, the same way the ROS and YOLO backends do.
    """

    name = "pca9685"

    def __init__(
        self,
        pan: ServoCalibration,
        tilt: ServoCalibration,
        *,
        address: int = 0x40,
        frequency_hz: int = 50,
        bus: int = 1,
        bus_factory=open_smbus,
    ) -> None:
        for cal in (pan, tilt):
            if not 0 <= cal.channel <= 15:
                raise ValueError(f"PCA9685 channel must be 0-15, got {cal.channel}")
        if pan.channel == tilt.channel:
            raise ValueError(f"pan and tilt are both on channel {pan.channel}")
        Pca9685.prescale_for(frequency_hz)  # fail at construction, not first motion
        self.pan_cal = pan
        self.tilt_cal = tilt
        self.address = address
        self.frequency_hz = frequency_hz
        self.bus_number = bus
        self._bus_factory = bus_factory
        self._bus = None
        self._chip: Pca9685 | None = None

    def _device(self) -> Pca9685:
        if self._chip is None:
            bus = self._bus_factory(self.bus_number)
            chip = Pca9685(bus, self.address)
            try:
                chip.probe()
            except OSError as exc:
                bus.close()
                raise RuntimeError(
                    f"no PCA9685 answered at 0x{self.address:02x} on /dev/i2c-{self.bus_number}: "
                    f"check SDA, SCL, VCC and GND, then `i2cdetect -y {self.bus_number}` "
                    f"should show {self.address:02x}"
                ) from exc
            chip.start(self.frequency_hz)
            self._bus, self._chip = bus, chip
            log.info("PCA9685 ready at 0x%02x on /dev/i2c-%d, %.2f Hz",
                     self.address, self.bus_number, chip.frequency_hz)
        return self._chip

    def write(self, pan_rad: float, tilt_rad: float) -> None:
        chip = self._device()
        chip.set_pulse(self.pan_cal.channel, self.pan_cal.to_pulse_us(pan_rad))
        chip.set_pulse(self.tilt_cal.channel, self.tilt_cal.to_pulse_us(tilt_rad))

    def release(self) -> None:
        if self._chip is None:
            return
        for cal in (self.pan_cal, self.tilt_cal):
            self._chip.set_off(cal.channel)

    def close(self) -> None:
        self.release()
        if self._bus is not None:
            self._bus.close()
        self._bus = self._chip = None


# -- the Pi's own hardware PWM ------------------------------------------------

PWM_ROOT = Path("/sys/class/pwm")

RPI_PWM_PINS = {0: ("GPIO18", 12), 1: ("GPIO19", 35)}
"""PWM channel -> (GPIO, physical header pin), as `dtoverlay=pwm-2chan` maps them."""

ENABLE_PWM_HINT = (
    "add dtoverlay=pwm-2chan to /boot/firmware/config.txt and reboot "
    "(sudo bash ~/neo1/scripts/pi/setup-system.sh --servo-pwm does it)"
)

PWM_GROUP_HINT = (
    "this user needs the pwm group and its udev rule "
    "(sudo bash ~/neo1/scripts/pi/setup-system.sh --servo-pwm), then a fresh login"
)


def rpi_pwm_pin(channel: int) -> str:
    gpio, pin = RPI_PWM_PINS[channel]
    return f"{gpio}, pin {pin}"


def _compatible(chip: Path) -> str:
    try:
        raw = (chip / "device" / "of_node" / "compatible").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("ascii", "replace")


class SysfsPwmChip:
    """One Linux PWM controller, driven through /sys/class/pwm/pwmchipN.

    Every value is nanoseconds, written as text. The kernel is fussy about
    order -- a period may not be set shorter than the duty cycle already there,
    and an enabled channel with no duty cycle emits nothing useful -- so the
    order below is the point, not incidental.
    """

    COMPATIBLE = "brcm,bcm2835-pwm"
    EXPORT_TIMEOUT_S = 1.0

    def __init__(self, path: Path) -> None:
        self.path = path
        self.npwm = int(self._read(path / "npwm") or 0)
        self._period_ns: dict[int, int] = {}
        self._duty_ns: dict[int, int] = {}
        self._enabled: dict[int, bool] = {}

    @classmethod
    def find(cls, root: Path | None = None, index: int | None = None) -> SysfsPwmChip:
        root = root or PWM_ROOT
        if index is not None:
            path = root / f"pwmchip{index}"
            if not path.exists():
                raise RuntimeError(f"{path} does not exist: {ENABLE_PWM_HINT}")
            return cls(path)
        chips = sorted(root.glob("pwmchip*")) if root.exists() else []
        for path in chips:
            if cls.COMPATIBLE in _compatible(path):
                return cls(path)
        if chips:
            names = ", ".join(p.name for p in chips)
            raise RuntimeError(
                f"none of {names} is the Raspberry Pi's PWM controller; "
                f"set servo.pwm_chip in config/motion.yaml to the right one"
            )
        raise RuntimeError(f"no hardware PWM controller under {root}: {ENABLE_PWM_HINT}")

    def channel_path(self, channel: int) -> Path:
        return self.path / f"pwm{channel}"

    def open_channel(self, channel: int, frequency_hz: float) -> None:
        """Export a channel if needed and set its period. Leaves it disabled."""
        if not 0 <= channel < self.npwm:
            raise RuntimeError(f"{self.path.name} has {self.npwm} PWM channels; {channel} is not one of them")
        ch = self.channel_path(channel)
        if not ch.exists():
            self._write(self.path / "export", channel)
            self._wait_for(ch / "duty_cycle")
        period_ns = round(1e9 / frequency_hz)
        # A previous run with a longer period can leave a duty cycle this period
        # cannot hold, and the kernel then refuses the period. Clear it first.
        if int(self._read(ch / "duty_cycle") or 0) > period_ns:
            self._write(ch / "duty_cycle", 0)
        self._write(ch / "period", period_ns)
        self._period_ns[channel] = period_ns
        self._duty_ns.pop(channel, None)

    def set_pulse_us(self, channel: int, pulse_us: float) -> None:
        period_ns = self._period_ns[channel]
        duty_ns = max(0, min(period_ns, round(pulse_us * 1000)))
        # Only when it changed: the driver rewrites its position every tick to
        # hold torque, and the PWM block keeps pulsing without being told again.
        if self._duty_ns.get(channel) != duty_ns:
            self._write(self.channel_path(channel) / "duty_cycle", duty_ns)
            self._duty_ns[channel] = duty_ns
        if not self._enabled.get(channel):
            # After the duty cycle, so the first pulse out is already the right one.
            self._write(self.channel_path(channel) / "enable", 1)
            self._enabled[channel] = True

    def disable(self, channel: int) -> None:
        if self.channel_path(channel).exists():
            self._write(self.channel_path(channel) / "enable", 0)
        self._enabled[channel] = False

    def unexport(self, channel: int) -> None:
        if self.channel_path(channel).exists():
            self._write(self.path / "unexport", channel)
        self._period_ns.pop(channel, None)
        self._duty_ns.pop(channel, None)
        self._enabled.pop(channel, None)

    def _wait_for(self, path: Path) -> None:
        """Exporting creates the channel's files asynchronously, and udev then
        hands them to the pwm group: for a moment they exist and are root-only."""
        deadline = time.monotonic() + self.EXPORT_TIMEOUT_S
        while time.monotonic() < deadline:
            if path.exists() and os.access(path, os.W_OK):
                return
            time.sleep(0.01)
        if path.exists():
            raise RuntimeError(f"{path} is not writable: {PWM_GROUP_HINT}")
        raise RuntimeError(f"exporting did not create {path.parent}")

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="ascii").strip()
        except OSError:
            return ""

    def _write(self, path: Path, value: int) -> None:
        try:
            with open(path, "w", encoding="ascii") as fh:
                fh.write(f"{value}\n")
        except PermissionError as exc:
            raise RuntimeError(f"cannot write {path}: {PWM_GROUP_HINT}") from exc


class RpiPwmBackend(ServoBackend):
    """Real hardware, no board: the servos' signal wires on the Pi's hardware PWM
    pins, GPIO18 (pin 12) and GPIO19 (pin 35), through Linux's PWM sysfs.

    Needs `dtoverlay=pwm-2chan`, and for a non-root user the pwm group's udev
    rule -- `setup-system.sh --servo-pwm` does both. The 3.5 mm audio jack uses
    the same PWM block, so it is off while this drives the servos.

    Opens nothing until the first write, like the PCA9685 backend, so it is
    constructible on a laptop.
    """

    name = "rpi-pwm"

    def __init__(
        self,
        pan: ServoCalibration,
        tilt: ServoCalibration,
        *,
        frequency_hz: int = 50,
        chip: int | None = None,
        root: Path | None = None,
    ) -> None:
        for cal in (pan, tilt):
            if cal.channel not in RPI_PWM_PINS:
                raise ValueError(
                    f"the Pi's hardware PWM has channel 0 ({rpi_pwm_pin(0)}) and "
                    f"channel 1 ({rpi_pwm_pin(1)}), not {cal.channel}"
                )
        if pan.channel == tilt.channel:
            raise ValueError(f"pan and tilt are both on PWM channel {pan.channel}")
        if frequency_hz <= 0:
            raise ValueError(f"PWM frequency must be positive, got {frequency_hz}")
        self.pan_cal = pan
        self.tilt_cal = tilt
        self.frequency_hz = frequency_hz
        self.chip_index = chip
        self.root = root or PWM_ROOT
        self._chip: SysfsPwmChip | None = None

    def _device(self) -> SysfsPwmChip:
        if self._chip is None:
            chip = SysfsPwmChip.find(self.root, self.chip_index)
            for cal in (self.pan_cal, self.tilt_cal):
                chip.open_channel(cal.channel, self.frequency_hz)
            self._chip = chip
            log.info("hardware PWM ready on %s: pan on %s, tilt on %s, %d Hz",
                     chip.path, rpi_pwm_pin(self.pan_cal.channel),
                     rpi_pwm_pin(self.tilt_cal.channel), self.frequency_hz)
        return self._chip

    def write(self, pan_rad: float, tilt_rad: float) -> None:
        chip = self._device()
        chip.set_pulse_us(self.pan_cal.channel, self.pan_cal.to_pulse_us(pan_rad))
        chip.set_pulse_us(self.tilt_cal.channel, self.tilt_cal.to_pulse_us(tilt_rad))

    def release(self) -> None:
        if self._chip is None:
            return
        for cal in (self.pan_cal, self.tilt_cal):
            self._chip.disable(cal.channel)

    def close(self) -> None:
        self.release()
        if self._chip is not None:
            for cal in (self.pan_cal, self.tilt_cal):
                self._chip.unexport(cal.channel)
        self._chip = None


# -- continuous-rotation servos ----------------------------------------------


class ContinuousAxis:
    """Turns "be at this angle" into "turn at this speed" for one 360-deg servo.

    There is no feedback, so the angle is dead-reckoned: the estimate advances by
    the speed the model predicts for each pulse, over the time that pulse was
    actually out. It starts at 0 -- wherever the head pointed when this was
    created -- and drifts as the model and the servo disagree.
    """

    TOLERANCE_DEG = 0.5
    """Within this of the target, send neutral. A command smaller than the
    slowest speed the servo really turns at would move only the estimate."""

    DEFAULT_TICK_S = 0.02

    def __init__(self, cal: ContinuousCalibration) -> None:
        if not cal.calibrated:
            raise RuntimeError(
                f"the continuous-rotation servo on channel {cal.channel} is not calibrated: without "
                f"its neutral, stop band and speed the head's angle would be a guess, so it is not "
                f"driven. Measure them with neo-servo-check --pulse (docs/hardware.md)"
            )
        self.cal = cal
        self.estimate_deg = 0.0
        self.speed_deg_s = 0.0
        self._last: float | None = None
        self._tick_s = self.DEFAULT_TICK_S

    def _advance(self, now: float) -> None:
        if self._last is not None:
            elapsed = max(0.0, now - self._last)
            self.estimate_deg += self.speed_deg_s * elapsed
            if elapsed > 0:
                self._tick_s = min(max(elapsed, 0.005), 0.1)
        self._last = now

    def update(self, desired_rad: float, now: float) -> float:
        """Bring the estimate up to `now`, then return the pulse for the next tick."""
        self._advance(now)
        cal = self.cal
        target = max(cal.min_deg, min(cal.max_deg, math.degrees(desired_rad)))
        error = target - self.estimate_deg
        if abs(error) <= self.TOLERANCE_DEG:
            wanted = 0.0
        else:
            # Enough to arrive by the next tick, and never more than the servo
            # can do in this direction -- the two directions have different tops.
            limit = cal.max_speed_for(math.copysign(1.0, error))
            wanted = max(-limit, min(limit, error / self._tick_s))
            if cal.fixed and abs(error) < limit * self._tick_s / 2:
                # A fixed-speed servo moves a whole tick's worth or nothing.
                # Stop rather than overshoot by more than half a tick.
                wanted = 0.0
        offset = cal.offset_for(wanted)
        self.speed_deg_s = cal.speed_for(offset)
        return cal.pulse_for(offset)

    def stop(self, now: float) -> float:
        self._advance(now)
        self.speed_deg_s = 0.0
        return self.cal.pulse_for(0.0)

    def rehome(self) -> None:
        """Declare wherever the head points now to be 0 deg."""
        self.estimate_deg = 0.0


class RpiPwmContinuousBackend(ServoBackend):
    """Continuous-rotation servos on the Pi's hardware PWM, made to act like
    position servos by dead reckoning.

    The driver above still speaks angles: every tick it says where the head
    should be, and each `ContinuousAxis` turns that into a speed. That keeps its
    safety rules meaning what they say. Its "hold position" -- rewrite the same
    pose -- arrives here as zero error, so neutral: it *stops* a continuous
    servo, where repeating a spinning pulse would not.

    Two failures a position servo does not have are handled here:

    * **Nothing writes for a while** (a stalled process): the kernel keeps the
      last pulse going, so a servo told to turn keeps turning. A watchdog thread
      sends neutral if no write arrives for `STALL_STOP_S`.
    * **The process ends**: an exit handler stops the outputs. A kill cannot be
      caught, and the last pulse then runs until `neo-servo-check --stop`.
    """

    name = "rpi-pwm-continuous"
    STALL_STOP_S = 0.15

    def __init__(
        self,
        pan: ContinuousCalibration,
        tilt: ContinuousCalibration,
        *,
        frequency_hz: int = 50,
        chip: int | None = None,
        root: Path | None = None,
        clock=time.monotonic,
        watchdog: bool = True,
    ) -> None:
        for cal in (pan, tilt):
            if cal.channel not in RPI_PWM_PINS:
                raise ValueError(
                    f"the Pi's hardware PWM has channel 0 ({rpi_pwm_pin(0)}) and "
                    f"channel 1 ({rpi_pwm_pin(1)}), not {cal.channel}"
                )
        if pan.channel == tilt.channel:
            raise ValueError(f"pan and tilt are both on PWM channel {pan.channel}")
        if frequency_hz <= 0:
            raise ValueError(f"PWM frequency must be positive, got {frequency_hz}")
        self.pan = ContinuousAxis(pan)
        self.tilt = ContinuousAxis(tilt)
        self.frequency_hz = frequency_hz
        self.chip_index = chip
        self.root = root or PWM_ROOT
        self._clock = clock
        self._watchdog = watchdog
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None
        self._chip: SysfsPwmChip | None = None
        self._last_write: float | None = None
        self._stalled = False

    @property
    def estimate_deg(self) -> tuple[float, float]:
        """Where the head is believed to be. Dead-reckoned: it drifts."""
        return (self.pan.estimate_deg, self.tilt.estimate_deg)

    def _axes(self) -> tuple[ContinuousAxis, ContinuousAxis]:
        return (self.pan, self.tilt)

    def _device(self) -> SysfsPwmChip:
        if self._chip is None:
            chip = SysfsPwmChip.find(self.root, self.chip_index)
            for axis in self._axes():
                chip.open_channel(axis.cal.channel, self.frequency_hz)
            self._chip = chip
            atexit.register(self._close_at_exit)
            log.info("continuous servos on %s: pan on %s, tilt on %s, %d Hz; angles are estimates from here",
                     chip.path, rpi_pwm_pin(self.pan.cal.channel),
                     rpi_pwm_pin(self.tilt.cal.channel), self.frequency_hz)
        return self._chip

    def write(self, pan_rad: float, tilt_rad: float) -> None:
        with self._lock:
            chip = self._device()
            now = self._clock()
            chip.set_pulse_us(self.pan.cal.channel, self.pan.update(pan_rad, now))
            chip.set_pulse_us(self.tilt.cal.channel, self.tilt.update(tilt_rad, now))
            self._last_write = now
            self._stalled = False
            if self._watchdog and self._thread is None:
                self._thread = threading.Thread(target=self._watch, name="servo-stall-watchdog", daemon=True)
                self._thread.start()

    def _watch(self) -> None:
        while not self._closing.wait(self.STALL_STOP_S / 3):
            self.check_stall()

    def check_stall(self, now: float | None = None) -> bool:
        """Send neutral if no write has arrived for STALL_STOP_S. True if it did."""
        with self._lock:
            if self._chip is None or self._last_write is None or self._stalled:
                return False
            now = self._clock() if now is None else now
            if now - self._last_write <= self.STALL_STOP_S:
                return False
            for axis in self._axes():
                self._chip.set_pulse_us(axis.cal.channel, axis.stop(now))
            self._stalled = True
        log.warning("no servo command for %.0f ms: continuous servos stopped", self.STALL_STOP_S * 1000)
        return True

    def rehome(self) -> None:
        with self._lock:
            for axis in self._axes():
                axis.rehome()

    def release(self) -> None:
        with self._lock:
            if self._chip is None:
                return
            now = self._clock()
            for axis in self._axes():
                axis.stop(now)
                self._chip.disable(axis.cal.channel)

    def close(self) -> None:
        self._closing.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        with self._lock:
            self.release()
            if self._chip is not None:
                for axis in self._axes():
                    self._chip.unexport(axis.cal.channel)
            self._chip = None

    def _close_at_exit(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 -- interpreter shutdown: best effort, never a traceback
            pass


# -- choosing -----------------------------------------------------------------

DRIVERS = ("rpi-pwm", "pca9685")


def make_backend(kind: str = "auto", **kwargs) -> ServoBackend:
    """`auto` picks the configured driver when this machine can reach it at all.

    Falling back to the mock is right on a laptop and wrong on the robot, where
    it means a head that never moves while everything reports success -- so the
    fallback is logged as a warning, and the robot should name its driver
    (`make_backend(config.driver, ...)`, as the servo node does) so missing
    hardware is an error instead.

    Without `pan`/`tilt` calibrations, everything comes from config/motion.yaml
    (merged with the robot's motion.local.yaml): the driver, the servo model's
    PWM frequency, the wiring, and the calibration -- or, until one is measured,
    a window that keeps the servos off their end stops. With calibrations
    passed in and no driver named, `auto` means the PCA9685, as it always has.
    """
    if kind == "mock":
        return MockServoBackend()
    if kind not in ("auto", *DRIVERS):
        raise ValueError(f"unknown servo backend: {kind!r} (known: mock, auto, {', '.join(DRIVERS)})")

    driver = "pca9685" if kind == "auto" else kind
    if "pan" not in kwargs or "tilt" not in kwargs:
        from .config import MotionConfig

        config = MotionConfig.load()
        if kind != "auto" and kind != config.driver:
            raise ValueError(f"asked for the {kind} backend, but {config.source} configures {config.driver}")
        driver = config.driver
        if not config.calibrated and not config.servo.continuous:
            servo = config.servo
            log.warning(
                "%s servos are uncalibrated: held to %.0f-%.0f us (about +/-%.0f deg) until "
                "config/motion.local.yaml has measured limits -- see docs/hardware.md",
                servo.name.upper(), servo.safe_min_us, servo.safe_max_us, servo.safe_half_range_deg,
            )
        kwargs = {**config.backend_kwargs(), **kwargs}

    missing = _unavailable(driver, kwargs)
    if missing:
        if kind != "auto":
            raise RuntimeError(f"{driver} backend unavailable: {missing}")
        log.warning("%s backend unavailable (%s); using the mock servo backend", driver, missing)
        return MockServoBackend()
    if driver == "rpi-pwm":
        if isinstance(kwargs.get("pan"), ContinuousCalibration):
            return RpiPwmContinuousBackend(**kwargs)
        return RpiPwmBackend(**kwargs)
    return Pca9685Backend(**kwargs)


def _unavailable(driver: str, kwargs: dict) -> str | None:
    """Why this driver cannot reach its hardware here, or None if it can."""
    if driver == "rpi-pwm":
        try:
            SysfsPwmChip.find(kwargs.get("root"), kwargs.get("chip"))
        except RuntimeError as exc:
            return str(exc)
        return None
    try:
        import smbus2  # noqa: F401
    except ImportError:
        return "smbus2 is not installed (pip install -e '.[servo]')"
    bus = kwargs.get("bus", 1)
    if not Path(f"/dev/i2c-{bus}").exists():
        return f"/dev/i2c-{bus} does not exist (is I2C enabled?)"
    return None
