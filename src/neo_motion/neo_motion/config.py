"""Which servos drive the head, how they are wired, and their calibration.

Resolution order: $NEO_MOTION_CONFIG names exactly one file; otherwise
config/motion.local.yaml is merged over config/motion.yaml, key by key -- the
same two layers as every other package (CLAUDE.md). Measured calibration belongs
in the local file on the robot, which the Pi sync never overwrites.

Two kinds of servo, calibrated differently:

* **Positional** (`mg995`): the pulse sets an angle. Calibration is the pulse at
  each end of travel; until measured, an axis is held to a narrow window.
* **Continuous rotation** (`mg995-360`, the robot's): the pulse sets a speed.
  Calibration is either *fixed speed* -- a stop pulse and one measured pulse
  each way, what the robot uses -- or *proportional* -- a stop band and a
  speed model. Until measured, the head is not driven at all, because its
  angle would be a guess.

Unknown or partial settings are errors either way. Half a calibration, or a
field spelled wrong, would put a servo somewhere nobody measured, so loading
refuses and names every problem at once rather than one per ssh round trip.
"""

from __future__ import annotations

import contextlib
import math
import os
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .backend import DRIVERS, RPI_PWM_PINS, ContinuousCalibration, ServoCalibration, rpi_pwm_pin
from .types import AxisLimits, HeadLimits

# repo root: .../neo1/src/neo_motion/neo_motion/config.py -> up 3
REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_CONFIG = CONFIG_DIR / "motion.yaml"
LOCAL_CONFIG = CONFIG_DIR / "motion.local.yaml"


class MotionConfigError(ValueError):
    def __init__(self, source: str, problems: list[str]) -> None:
        self.source = source
        self.problems = problems
        super().__init__(f"{source}: " + "; ".join(problems))


@dataclass(frozen=True)
class ServoModel:
    name: str

    frequency_hz: int
    """The only PWM frequency this servo is driven at."""

    min_us: float
    max_us: float
    """Hard bounds. Nothing -- calibration, --pulse, anything -- sends outside them."""

    safe_min_us: float
    safe_max_us: float
    safe_half_range_deg: float
    """Positional: the window an uncalibrated axis is held to, and the angle it
    is taken to span either side of centre. Continuous: nominal neutral, and the
    default limit on the estimated angle either side of where the head started."""

    max_voltage: float
    stall_current_a: float

    continuous: bool = False
    """The pulse sets speed, not angle: a continuous-rotation (360 deg) servo."""

    max_offset_us: float = 0.0
    """Continuous only: never command further from neutral than this."""


MG995 = ServoModel(
    name="mg995",
    # Specified for a 50 Hz (20 ms) frame. Not a setting to raise for smoother
    # motion: that is a lever for servos rated for it, and this one is not.
    frequency_hz=50,
    min_us=500.0,
    max_us=2500.0,
    # The published pulse ranges disagree. Components101 gives 0.5-2.5 ms for
    # 0-180 deg; ProtoSupplies gives 1-2 ms for the same travel. 1000-2000 us
    # would be +/-45 deg under the first and hard against the end stops under
    # the second. 1200-1800 us is clear of the stops under either (+/-27 deg,
    # or +/-54 deg), so it is the window safe to drive before a unit is
    # measured. No single angle label is right under both readings: it is
    # labelled +/-27 deg, exact under the 0.5-2.5 ms sheet, and under the other
    # the head turns about twice what the driver believes. Both stay off the
    # stops, which is the window's only job; calibration replaces the guess.
    safe_min_us=1200.0,
    safe_max_us=1800.0,
    safe_half_range_deg=27.0,
    # 4.8-6 V (ProtoSupplies; other sheets stretch to 6.6 or 7.2 V -- stay at 6).
    max_voltage=6.0,
    # Measured 1.3-1.5 A (ProtoSupplies) and about 1.2 A (Arduino forum). The
    # higher figure, so a supply sized from it survives both servos stalling.
    stall_current_a=1.5,
)

MG995_360 = ServoModel(
    name="mg995-360",
    # The robot's servos: MG995s that rotate continuously, so there is no angle
    # to send them to -- nominally 1500 us stops them, and further from it turns
    # faster, one way or the other. Electrically they are MG995s.
    frequency_hz=50,
    min_us=500.0,
    max_us=2500.0,
    safe_min_us=1500.0,
    safe_max_us=1500.0,
    # Chosen, not measured: how far the estimated angle may go either side of
    # where the head started, until min_deg/max_deg say otherwise.
    safe_half_range_deg=30.0,
    max_voltage=6.0,
    stall_current_a=1.5,
    continuous=True,
    # A cap, not a measured figure: calibration measures speed inside it, and
    # neutral +/- this stays within the hard bounds (checked on load).
    max_offset_us=400.0,
)

MODELS = {model.name: model for model in (MG995, MG995_360)}

_AXES = ("pan", "tilt")
_SERVO_KEYS = ("model", "driver", "i2c_bus", "address", "pwm_chip")

_SPIN_KEYS = ("neutral_us", "deadband_us", "speed_offset_us", "speed_deg_s")
"""Required together for a proportional continuous-rotation calibration."""
_FIXED_KEYS = ("neutral_us", "above_us", "above_deg_s", "below_us", "below_deg_s")
"""Required together for a fixed-speed continuous-rotation calibration."""
_PROPORTIONAL_ONLY = ("deadband_us", "speed_offset_us", "speed_deg_s", "speed_below_deg_s")
_FIXED_ONLY = ("above_us", "above_deg_s", "below_us", "below_deg_s")
_CONTINUOUS_KEYS = ("neutral_us", *_PROPORTIONAL_ONLY, *_FIXED_ONLY)
_AXIS_KEYS = ("channel", "min_us", "max_us", "min_deg", "max_deg", "inverted", *_CONTINUOUS_KEYS)


@dataclass
class AxisConfig:
    channel: int
    min_us: float | None = None
    max_us: float | None = None
    min_deg: float | None = None
    max_deg: float | None = None
    inverted: bool = False
    neutral_us: float | None = None
    deadband_us: float | None = None
    speed_offset_us: float | None = None
    speed_deg_s: float | None = None
    speed_below_deg_s: float | None = None
    """Proportional, optional: the speed below neutral, when it differs from speed_deg_s."""
    above_us: float | None = None
    above_deg_s: float | None = None
    below_us: float | None = None
    below_deg_s: float | None = None

    @property
    def measured(self) -> tuple[Any, Any, Any, Any]:
        """A positional servo's calibration."""
        return (self.min_us, self.max_us, self.min_deg, self.max_deg)

    @property
    def calibrated(self) -> bool:
        return all(value is not None for value in self.measured)

    def complete(self, keys: tuple[str, ...]) -> bool:
        return all(getattr(self, key) is not None for key in keys)


@dataclass
class MotionConfig:
    model: str = "mg995"

    driver: str = "pca9685"
    """`rpi-pwm`: signal wires on the Pi's hardware PWM pins (GPIO18, GPIO19).
    `pca9685`: signal wires on a PCA9685 board over I2C."""

    i2c_bus: int = 1
    address: int = 0x40
    """PCA9685 only."""

    pwm_chip: int | None = None
    """rpi-pwm only: which /sys/class/pwm/pwmchipN. None finds the Pi's own."""

    pan: AxisConfig = field(default_factory=lambda: AxisConfig(channel=0))
    tilt: AxisConfig = field(default_factory=lambda: AxisConfig(channel=1))
    source: str = "built-in defaults"

    @property
    def servo(self) -> ServoModel:
        return MODELS[self.model]

    def axis(self, name: str) -> AxisConfig:
        if name not in _AXES:
            raise ValueError(f"axis must be one of {_AXES}, got {name!r}")
        return getattr(self, name)

    def axis_calibrated(self, name: str) -> bool:
        axis = self.axis(name)
        if self.servo.continuous:
            return axis.complete(_FIXED_KEYS) or axis.complete(_SPIN_KEYS)
        return axis.calibrated

    @property
    def calibrated(self) -> bool:
        return all(self.axis_calibrated(name) for name in _AXES)

    def calibration(self, name: str) -> ServoCalibration | ContinuousCalibration:
        """One axis's pulse mapping: measured, or the model's safe default."""
        axis = self.axis(name)
        servo = self.servo
        if servo.continuous:
            half = servo.safe_half_range_deg
            measured = {k: float(getattr(axis, k)) for k in _CONTINUOUS_KEYS if getattr(axis, k) is not None}
            return ContinuousCalibration(
                channel=axis.channel,
                max_offset_us=servo.max_offset_us,
                min_deg=float(axis.min_deg) if axis.min_deg is not None else -half,
                max_deg=float(axis.max_deg) if axis.max_deg is not None else half,
                inverted=axis.inverted,
                **measured,
            )
        if axis.calibrated:
            min_us, max_us = float(axis.min_us), float(axis.max_us)
            min_deg, max_deg = float(axis.min_deg), float(axis.max_deg)
        else:
            min_us, max_us = servo.safe_min_us, servo.safe_max_us
            min_deg, max_deg = -servo.safe_half_range_deg, servo.safe_half_range_deg
        return ServoCalibration(
            channel=axis.channel,
            min_us=min_us,
            max_us=max_us,
            min_rad=math.radians(min_deg),
            max_rad=math.radians(max_deg),
            inverted=axis.inverted,
        )

    def head_limits(self, base: HeadLimits | None = None) -> HeadLimits:
        """The driver's soft limits, never wider than the servos can honour.

        Positional: the angles are narrowed to the calibration, or the driver
        would report 90 deg while the pulse is clamped at the calibration's edge.
        Continuous: the angles are narrowed to the estimate's limits, and the
        speed to what the servo was measured to do in its slower direction, or
        the driver's pose would run ahead of the estimate it is meant to match.
        """
        base = base or HeadLimits()
        return HeadLimits(pan=self._narrowed(base.pan, "pan"), tilt=self._narrowed(base.tilt, "tilt"))

    def _narrowed(self, limits: AxisLimits, name: str) -> AxisLimits:
        cal = self.calibration(name)
        if isinstance(cal, ContinuousCalibration):
            narrowed = replace(
                limits,
                min_rad=max(limits.min_rad, math.radians(cal.min_deg)),
                max_rad=min(limits.max_rad, math.radians(cal.max_deg)),
            )
            if cal.calibrated:
                speed = min(narrowed.max_speed_rad_s, math.radians(cal.max_speed_deg_s))
                narrowed = replace(narrowed, max_speed_rad_s=speed)
            return narrowed
        return replace(limits, min_rad=max(limits.min_rad, cal.min_rad), max_rad=min(limits.max_rad, cal.max_rad))

    def backend_kwargs(self) -> dict[str, Any]:
        """What the configured driver's backend class needs, from this config."""
        common = {
            "pan": self.calibration("pan"),
            "tilt": self.calibration("tilt"),
            "frequency_hz": self.servo.frequency_hz,
        }
        if self.driver == "rpi-pwm":
            return {**common, "chip": self.pwm_chip}
        return {**common, "address": self.address, "bus": self.i2c_bus}

    def channel_label(self, channel: int) -> str:
        if self.driver == "rpi-pwm" and channel in RPI_PWM_PINS:
            return f"{rpi_pwm_pin(channel)} (PWM channel {channel})"
        return f"channel {channel}"

    def describe(self) -> list[str]:
        servo = self.servo
        if self.driver == "rpi-pwm":
            chip = f"pwmchip{self.pwm_chip}" if self.pwm_chip is not None else "controller found automatically"
            where = f"the Pi's hardware PWM ({chip})"
        else:
            where = f"PCA9685 0x{self.address:02x}, /dev/i2c-{self.i2c_bus}"
        kind = ", continuous rotation" if servo.continuous else ""
        lines = [f"{servo.name.upper()}{kind} on {where}, {servo.frequency_hz} Hz  (from {self.source})"]
        for name in _AXES:
            cal = self.calibration(name)
            inverted = ", inverted" if cal.inverted else ""
            if isinstance(cal, ContinuousCalibration):
                if self.axis_calibrated(name) and cal.fixed:
                    spin = (f"fixed speed: stops at {cal.neutral_us:.0f} us, {cal.above_deg_s:g} deg/s at "
                            f"{cal.above_us:.0f} us, {cal.below_deg_s:g} deg/s at {cal.below_us:.0f} us")
                    state = "calibrated"
                elif self.axis_calibrated(name):
                    if cal.speed_below_deg_s:
                        speeds = (f"{cal.speed_deg_s:g} deg/s above and {cal.speed_below_deg_s:g} deg/s "
                                  f"below neutral at {cal.speed_offset_us:.0f} us")
                    else:
                        speeds = f"{cal.speed_deg_s:g} deg/s at +/-{cal.speed_offset_us:.0f} us"
                    spin = f"proportional: stops at {cal.neutral_us:.0f} us (+/-{cal.deadband_us:.0f}), {speeds}"
                    state = "calibrated"
                else:
                    spin = "stop point and speed not measured"
                    state = "UNCALIBRATED: will not drive the head until measured"
                lines.append(
                    f"{name}: {self.channel_label(cal.channel)}, {spin}, estimated angle held to "
                    f"{cal.min_deg:+.0f} to {cal.max_deg:+.0f} deg{inverted}  [{state}]"
                )
                continue
            state = "calibrated" if self.axis_calibrated(name) else "UNCALIBRATED: held to the safe window"
            lines.append(
                f"{name}: {self.channel_label(cal.channel)}, {cal.min_us:.0f}-{cal.max_us:.0f} us = "
                f"{math.degrees(cal.min_rad):+.0f} to {math.degrees(cal.max_rad):+.0f} deg{inverted}  [{state}]"
            )
        return lines

    def problems(self) -> list[str]:
        if self.model not in MODELS:
            return [f"servo.model '{self.model}' is not a known servo (known: {', '.join(MODELS)})"]
        if self.driver not in DRIVERS:
            return [f"servo.driver '{self.driver}' is not known (known: {', '.join(DRIVERS)})"]
        servo = self.servo
        found = []
        if not _is_int(self.i2c_bus) or self.i2c_bus < 0:
            found.append("servo.i2c_bus must be a whole number, like 1")
        if not _is_int(self.address) or not 0x03 <= self.address <= 0x77:
            found.append("servo.address must be a 7-bit I2C address (0x03-0x77), like 0x40")
        if self.pwm_chip is not None and (not _is_int(self.pwm_chip) or self.pwm_chip < 0):
            found.append("servo.pwm_chip must be a whole number, like 0, or left out to find it")
        if servo.continuous and self.driver != "rpi-pwm":
            found.append(f"servo.model {servo.name} (continuous rotation) is only supported with driver rpi-pwm")
        for name in _AXES:
            axis = self.axis(name)
            if self.driver == "rpi-pwm":
                if not _is_int(axis.channel) or axis.channel not in RPI_PWM_PINS:
                    found.append(f"{name}.channel must be 0 ({rpi_pwm_pin(0)}) or 1 ({rpi_pwm_pin(1)}) "
                                 f"with driver rpi-pwm")
            elif not _is_int(axis.channel) or not 0 <= axis.channel <= 15:
                found.append(f"{name}.channel must be a PCA9685 channel, 0-15")
            if not isinstance(axis.inverted, bool):
                found.append(f"{name}.inverted must be true or false")
            if servo.continuous:
                found.extend(_continuous_problems(name, axis, servo))
            else:
                found.extend(_positional_problems(name, axis, servo))
        if _is_int(self.pan.channel) and self.pan.channel == self.tilt.channel:
            found.append(f"pan and tilt are both on channel {self.pan.channel}")
        return found

    @classmethod
    def load(cls, path: str | Path | None = None) -> MotionConfig:
        layers = _config_layers(path)
        raw: dict[str, Any] = {}
        for layer in layers:
            try:
                data = yaml.safe_load(layer.read_text(encoding="utf-8")) or {}
            except FileNotFoundError:
                raise MotionConfigError(str(layer), ["file not found"]) from None
            except yaml.YAMLError as exc:
                raise MotionConfigError(str(layer), [f"not valid YAML: {exc}"]) from None
            if not isinstance(data, dict):
                raise MotionConfigError(str(layer), ["must be a mapping with servo:, pan: and tilt:"])
            raw = _deep_merge(raw, data)
        source = " + ".join(str(p) for p in layers) if layers else "built-in defaults"
        return cls.from_raw(raw, source=source)

    @classmethod
    def from_raw(cls, raw: dict[str, Any], source: str = "(inline)") -> MotionConfig:
        found: list[str] = []
        for key in raw:
            if key not in ("servo", *_AXES):
                found.append(f"unknown section '{key}' (allowed: servo, pan, tilt)")

        # Only keys actually present are passed, so the dataclass defaults above
        # are the one statement of what "not set" means (CLAUDE.md: a loader
        # must not restate a default).
        defaults = cls()
        servo_raw = _section(raw, "servo", found)
        _reject_unknown(servo_raw, _SERVO_KEYS, "servo", found)
        servo_values = {k: servo_raw[k] for k in _SERVO_KEYS if k in servo_raw}
        for key in ("model", "driver"):
            if key in servo_values:
                servo_values[key] = str(servo_values[key]).strip().lower()

        axes = {}
        for name in _AXES:
            section = _section(raw, name, found)
            _reject_unknown(section, _AXIS_KEYS, name, found)
            axes[name] = replace(defaults.axis(name), **{k: section[k] for k in _AXIS_KEYS if k in section})

        config = cls(**servo_values, **axes, source=source)
        found.extend(config.problems())
        if found:
            raise MotionConfigError(source, found)
        return config


def _positional_problems(name: str, axis: AxisConfig, servo: ServoModel) -> list[str]:
    found = []
    spin_given = [k for k in _CONTINUOUS_KEYS if getattr(axis, k) is not None]
    if spin_given:
        found.append(f"{name}: {', '.join(spin_given)} only apply to a continuous-rotation servo, "
                     f"and servo.model {servo.name} is positional (mg995-360 is the continuous one)")
        return found
    given = [value is not None for value in axis.measured]
    if any(given) and not all(given):
        found.append(f"{name}: set all four of min_us, max_us, min_deg and max_deg "
                     f"from one calibration, or none of them")
        return found
    if not all(given):
        return found
    if not all(_is_number(value) for value in axis.measured):
        found.append(f"{name}: min_us, max_us, min_deg and max_deg must be numbers")
        return found
    if not axis.min_us < axis.max_us:
        found.append(f"{name}: min_us must be below max_us (swap the angles and set "
                     f"inverted: true if the servo turns the other way)")
    if axis.min_us < servo.min_us or axis.max_us > servo.max_us:
        found.append(f"{name}: pulses must stay within {servo.min_us:.0f}-{servo.max_us:.0f} us "
                     f"for the {servo.name.upper()}")
    if not axis.min_deg < 0 < axis.max_deg:
        found.append(f"{name}: min_deg must be below 0 and max_deg above it -- "
                     f"0 deg is where the head centres")
    return found


def _continuous_problems(name: str, axis: AxisConfig, servo: ServoModel) -> list[str]:
    found = []
    if axis.min_us is not None or axis.max_us is not None:
        found.append(f"{name}: min_us and max_us do not apply to a continuous-rotation servo, whose "
                     f"pulse sets speed, not angle (measure a fixed-speed or proportional "
                     f"calibration instead; docs/hardware.md)")

    low, high = servo.min_us + servo.max_offset_us, servo.max_us - servo.max_offset_us
    fixed_given = [k for k in _FIXED_ONLY if getattr(axis, k) is not None]
    proportional_given = [k for k in _PROPORTIONAL_ONLY if getattr(axis, k) is not None]

    if fixed_given and proportional_given:
        found.append(f"{name}: {', '.join(fixed_given)} (fixed speed) and {', '.join(proportional_given)} "
                     f"(proportional) are two different calibrations; use one")
    elif fixed_given:
        missing = [k for k in _FIXED_KEYS if getattr(axis, k) is None]
        if missing:
            found.append(f"{name}: a fixed-speed calibration needs all of {', '.join(_FIXED_KEYS)}; "
                         f"missing {', '.join(missing)}")
        elif not all(_is_number(getattr(axis, k)) for k in _FIXED_KEYS):
            found.append(f"{name}: {', '.join(_FIXED_KEYS)} must be numbers")
        else:
            if not low <= axis.neutral_us <= high:
                found.append(f"{name}: neutral_us must be within {low:.0f}-{high:.0f} us")
            if not axis.below_us < axis.neutral_us < axis.above_us:
                found.append(f"{name}: need below_us < neutral_us < above_us")
            for key in ("above_us", "below_us"):
                if abs(getattr(axis, key) - axis.neutral_us) > servo.max_offset_us:
                    found.append(f"{name}: {key} must be within {servo.max_offset_us:.0f} us of neutral_us")
            for key in ("above_deg_s", "below_deg_s"):
                if not getattr(axis, key) > 0:
                    found.append(f"{name}: {key} must be above 0")
    else:
        given = [getattr(axis, k) is not None for k in _SPIN_KEYS]
        if any(given) and not all(given):
            found.append(f"{name}: set all four of neutral_us, deadband_us, speed_offset_us and "
                         f"speed_deg_s from one calibration, or none of them")
        elif all(given):
            values = [getattr(axis, k) for k in _SPIN_KEYS]
            if not all(_is_number(v) for v in values):
                found.append(f"{name}: neutral_us, deadband_us, speed_offset_us and speed_deg_s must be numbers")
            else:
                if not low <= axis.neutral_us <= high:
                    found.append(f"{name}: neutral_us must be within {low:.0f}-{high:.0f} us")
                if not 0 <= axis.deadband_us < axis.speed_offset_us <= servo.max_offset_us:
                    found.append(f"{name}: need 0 <= deadband_us < speed_offset_us <= "
                                 f"{servo.max_offset_us:.0f} us")
                if not axis.speed_deg_s > 0:
                    found.append(f"{name}: speed_deg_s must be above 0")
        if axis.speed_below_deg_s is not None:
            if not all(given):
                found.append(f"{name}: speed_below_deg_s only makes sense with the other four measured")
            elif not _is_number(axis.speed_below_deg_s) or not axis.speed_below_deg_s > 0:
                found.append(f"{name}: speed_below_deg_s must be above 0 (leave it out if both "
                             f"directions turn at speed_deg_s)")

    angles = [axis.min_deg is not None, axis.max_deg is not None]
    if any(angles) and not all(angles):
        found.append(f"{name}: set both min_deg and max_deg, or neither")
    elif all(angles):
        if not (_is_number(axis.min_deg) and _is_number(axis.max_deg)):
            found.append(f"{name}: min_deg and max_deg must be numbers")
        elif not axis.min_deg < 0 < axis.max_deg:
            found.append(f"{name}: min_deg must be below 0 and max_deg above it -- "
                         f"0 deg is where the head points when the servos start")
    return found


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _section(raw: dict[str, Any], name: str, found: list[str]) -> dict[str, Any]:
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        found.append(f"'{name}' must be a section of settings")
        return {}
    return value


def _reject_unknown(section: dict[str, Any], allowed: tuple[str, ...], name: str, found: list[str]) -> None:
    import difflib

    for key in section:
        if key not in allowed:
            close = difflib.get_close_matches(str(key), allowed, n=1)
            hint = f" -- did you mean '{close[0]}'?" if close else ""
            found.append(f"{name}: unknown field '{key}'{hint} (allowed: {', '.join(allowed)})")


def _config_layers(explicit: str | Path | None) -> list[Path]:
    """An explicit path or $NEO_MOTION_CONFIG is exactly one file; otherwise the
    local file is merged over the committed one. Same rules as the other packages."""
    if explicit:
        return [Path(explicit)]
    env = os.environ.get("NEO_MOTION_CONFIG")
    if env:
        return [Path(env)]
    return [p for p in (DEFAULT_CONFIG, LOCAL_CONFIG) if p.exists()]


def load_raw(path: str | Path | None = None) -> dict[str, Any]:
    """The merged config layers as a plain dict, with no validation at all.

    `MotionConfig.load()` correctly refuses a half-measured calibration (a
    fixed-speed axis needs `neutral_us`, `above_us`, `above_deg_s`, `below_us`
    and `below_deg_s` all at once, or it is rejected as incomplete). That is
    right for anything that drives a servo, and wrong for tooling that is in
    the middle of *building up* that calibration one measurement at a time --
    `neo-servo-check --record-speed` needs to read whatever `neutral_us` was
    already recorded before `above_us`/`below_us` exist yet. Prefer
    `MotionConfig.load()` wherever a complete, driveable config is actually
    needed; this is only for reading (or, via `record_calibration`, writing)
    one field of a calibration still in progress.
    """
    raw: dict[str, Any] = {}
    for layer in _config_layers(path):
        # Unlike MotionConfig.load(), a missing explicit file is not an error
        # here: "nothing recorded yet" is exactly the state a calibration in
        # progress starts from, on the very first --record-neutral.
        try:
            text = layer.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        data = yaml.safe_load(text) or {}
        if isinstance(data, dict):
            raw = _deep_merge(raw, data)
    return raw


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def record_calibration(
    axis: str, fields: dict[str, float], *, path: Path = LOCAL_CONFIG
) -> None:
    """Merge measured calibration fields into one axis's section of the local
    config file -- the write side of `neo-servo-check`'s `--record-neutral` and
    `--record-speed`.

    Reads whatever is already at `path` and merges `fields` into `data[axis]`
    only: the other axis, the servo/driver section, and anything already
    measured on this axis are left standing, per the two-layer merge rule this
    file already loads by (a local file naming one key must not blank the
    rest). Written atomically (temp file + rename) so a write torn by a lost
    SSH session or a pulled plug cannot leave the file half-written -- the same
    failure mode neo_webapp.config's password-hash writer guards against.
    """
    if axis not in _AXES:
        raise ValueError(f"axis must be one of {_AXES}, got {axis!r}")

    data: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded

    section = dict(data.get(axis) or {})
    section.update(fields)
    data[axis] = section

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
