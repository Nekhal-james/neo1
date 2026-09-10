"""Where the angles actually go.

Same seam as the detector and the robot bridge: a real backend and a recording
one behind a single interface, so every safety rule above this line is testable
with no I2C bus and no servos to break while testing it.

PCA9685 over I2C rather than Pi GPIO PWM, per CLAUDE.md: software PWM on a busy
Pi produces jitter you can see in a face, and the PCA9685's own oscillator does
not care what the CPU is doing.
"""

from __future__ import annotations

import abc
import logging
import math
from dataclasses import dataclass

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


class Pca9685Backend(ServoBackend):
    """Real hardware: two channels on a PCA9685 over I2C.

    Imports its driver lazily so this module stays importable on a laptop, the
    same way the ROS and YOLO backends do.
    """

    name = "pca9685"

    def __init__(
        self,
        pan: ServoCalibration,
        tilt: ServoCalibration,
        *,
        address: int = 0x40,
        frequency_hz: int = 50,
    ) -> None:
        self.pan_cal = pan
        self.tilt_cal = tilt
        self.address = address
        self.frequency_hz = frequency_hz
        self._pca = None

    def _device(self):
        if self._pca is None:
            import board  # noqa: F401  (adafruit-blinka)
            import busio
            from adafruit_pca9685 import PCA9685

            i2c = busio.I2C(board.SCL, board.SDA)
            pca = PCA9685(i2c, address=self.address)
            pca.frequency = self.frequency_hz
            self._pca = pca
            log.info("PCA9685 ready at 0x%02x, %d Hz", self.address, self.frequency_hz)
        return self._pca

    def _set(self, cal: ServoCalibration, angle_rad: float) -> None:
        pca = self._device()
        pulse_us = cal.to_pulse_us(angle_rad)
        period_us = 1_000_000.0 / self.frequency_hz
        duty = int(max(0, min(0xFFFF, round(pulse_us / period_us * 0xFFFF))))
        pca.channels[cal.channel].duty_cycle = duty

    def write(self, pan_rad: float, tilt_rad: float) -> None:
        self._set(self.pan_cal, pan_rad)
        self._set(self.tilt_cal, tilt_rad)

    def release(self) -> None:
        if self._pca is None:
            return
        for cal in (self.pan_cal, self.tilt_cal):
            self._pca.channels[cal.channel].duty_cycle = 0

    def close(self) -> None:
        self.release()
        if self._pca is not None:
            self._pca.deinit()
            self._pca = None


def make_backend(kind: str = "auto", **kwargs) -> ServoBackend:
    if kind == "mock":
        return MockServoBackend()
    if kind in ("auto", "pca9685"):
        try:
            import adafruit_pca9685  # noqa: F401
        except ImportError:
            if kind == "pca9685":
                raise
            log.warning("PCA9685 driver unavailable; using the mock servo backend")
            return MockServoBackend()
        return Pca9685Backend(**kwargs)
    raise ValueError(f"unknown servo backend: {kind!r}")
