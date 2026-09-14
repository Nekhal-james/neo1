"""Are the servos wired up and drivable?  `neo-servo-check`

    neo-servo-check                       read-only: the PWM hardware, permissions,
                                          and the servo config it would drive with
    neo-servo-check --center              both servos to centre (continuous
                                          rotation: to neutral, i.e. stopped)
    neo-servo-check --center --keep       ...and keep sending that when it exits
    neo-servo-check --pulse 0 1600        one channel to one pulse width, for
                                          calibrating (docs/hardware.md)
    neo-servo-check --stop                stop sending anything to either servo
    neo-servo-check --move 20 10          drive the head through the real motion
                                          driver to pan 20, tilt 10 deg, back to 0,
                                          then stop
    neo-servo-check --record-neutral pan 1500
                                          write a measured neutral pulse for one
                                          axis to config/motion.local.yaml
    neo-servo-check --record-speed pan 1550 --turns 5.5 --seconds 40
                                          compute deg/s from a counted measurement
                                          and write it (needs --record-neutral first)

Nothing moves without --center, --pulse or --move. Which driver, which kind of
servo, the channels, the PWM frequency and each axis's calibration all come
from config/motion.yaml, merged with config/motion.local.yaml on the robot.

--record-neutral and --record-speed touch no hardware at all: they are the
arithmetic-and-YAML half of calibrating a continuous-rotation servo (turns x
360 / seconds, written to the right side of neutral), not the physical half --
finding the pulse, marking the shaft and counting turns is still done by
watching the servo (drive it with --pulse and time it yourself). Measuring
speed needs a neutral already recorded, because which measured speed applies
depends on which side of neutral the pulse is on (CLAUDE.md).

A continuous-rotation servo told to turn keeps turning for as long as the pulse
runs, so for those --keep is refused with any pulse but neutral, and --stop is
the way to halt one a crashed program left running.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path

from ..backend import PWM_ROOT, RPI_PWM_PINS, ContinuousCalibration, Pca9685, SysfsPwmChip, open_smbus
from ..config import LOCAL_CONFIG, MotionConfig, MotionConfigError, ServoModel, load_raw, record_calibration


def _check_bus(bus: int) -> bool:
    path = f"/dev/i2c-{bus}"
    if not os.path.exists(path):
        print(f"FAIL  {path} does not exist.")
        print("      Enable I2C: dtparam=i2c_arm=on in /boot/firmware/config.txt, then reboot.")
        print("      scripts/pi/setup-system.sh does this.")
        return False
    if not os.access(path, os.R_OK | os.W_OK):
        import grp  # POSIX only; the robot is Linux

        try:
            groups = [grp.getgrgid(g).gr_name for g in os.getgroups()]
        except KeyError:
            groups = []
        print(f"FAIL  {path} exists but this user cannot open it.")
        if "i2c" not in groups:
            print("      Add yourself to the i2c group (setup-system.sh does), then log out and back in.")
        return False
    print(f"ok    {path} is readable and writable by this user")
    return True


def _axis_name(config: MotionConfig, channel: int) -> str:
    for name in ("pan", "tilt"):
        if config.axis(name).channel == channel:
            return name
    return "unassigned"


def _power_lines(servo: ServoModel) -> list[str]:
    return [
        f"      Servo power: a separate 5-{servo.max_voltage:g} V supply (a buck converter is fine), "
        f"at least {2 * servo.stall_current_a:g} A for two {servo.name.upper()}s at stall,",
        "      with its ground joined to the Pi's. Never from the Pi's own 5 V pin.",
    ]


def _centre_pulse(cal) -> float:
    return cal.neutral_us if isinstance(cal, ContinuousCalibration) else cal.to_pulse_us(0.0)


def _moves(config: MotionConfig, args) -> list[tuple[str, int, float]]:
    if args.center:
        cals = [(name, config.calibration(name)) for name in ("pan", "tilt")]
        return [(name, cal.channel, _centre_pulse(cal)) for name, cal in cals]
    channel = int(args.pulse[0])
    return [(_axis_name(config, channel), channel, args.pulse[1])]


def _calibration_hint(continuous: bool) -> None:
    if continuous:
        print("      Calibrating a continuous-rotation servo (fixed speed):")
        print("      - neutral: the pulse where it does not turn at all, the middle of the band")
        print("        of pulses where it stays still.")
        print("      - one pulse each way, well clear of that band: mark the shaft, --hold 40,")
        print("        count turns; speed = turns x 360 / 40. Adjust each pulse until both ways")
        print("        turn at the speed you want, then record above_us/above_deg_s and")
        print("        below_us/below_deg_s -- the speed measured at exactly those pulses.")
    else:
        print("      Calibrating: step 25-50 us at a time towards each end. The moment the")
        print("      servo buzzes, strains or stops following, that pulse is past its limit --")
        print("      back off 50 us and record that as the end.")


def _config_path(args) -> Path:
    return Path(args.config) if args.config else LOCAL_CONFIG


def _record_neutral(args) -> int:
    """The arithmetic-free half: just remembers a pulse. Finding it -- driving
    --pulse and watching for the pulse where the shaft does not turn at all --
    is still done by a person, the same as every other calibration step here.
    """
    axis, pulse_str = args.record_neutral
    if axis not in ("pan", "tilt"):
        print(f"FAIL  axis must be 'pan' or 'tilt', got {axis!r}")
        return 2
    try:
        pulse_us = float(pulse_str)
    except ValueError:
        print(f"FAIL  US must be a number, got {pulse_str!r}")
        return 2

    path = _config_path(args)
    record_calibration(axis, {"neutral_us": pulse_us}, path=path)
    print(f"ok    {axis}: neutral_us = {pulse_us:.0f} written to {path}")
    print("      Next: --record-speed for a pulse well clear of it, above and below.")
    return 0


def _record_speed(args) -> int:
    """turns x 360 / seconds, written to whichever side of neutral the pulse
    is on -- CLAUDE.md is explicit that speed belongs to the side the *pulse*
    is on, not to a direction, so this needs neutral_us already recorded to
    know which.
    """
    axis, pulse_str = args.record_speed
    if axis not in ("pan", "tilt"):
        print(f"FAIL  axis must be 'pan' or 'tilt', got {axis!r}")
        return 2
    if args.turns is None or args.seconds is None:
        print("FAIL  --record-speed needs --turns and --seconds too")
        return 2
    if args.seconds <= 0:
        print("FAIL  --seconds must be positive")
        return 2
    try:
        pulse_us = float(pulse_str)
    except ValueError:
        print(f"FAIL  US must be a number, got {pulse_str!r}")
        return 2

    path = _config_path(args)
    neutral_us = load_raw(args.config).get(axis, {}).get("neutral_us")
    if neutral_us is None:
        print(f"FAIL  no neutral_us recorded for {axis} yet -- run --record-neutral {axis} <US> first")
        return 2
    neutral_us = float(neutral_us)
    if pulse_us == neutral_us:
        print("FAIL  this pulse IS neutral_us; a speed measurement needs a pulse clear of it")
        return 2

    deg_s = abs(args.turns) * 360.0 / args.seconds
    side = "above" if pulse_us > neutral_us else "below"
    record_calibration(axis, {f"{side}_us": pulse_us, f"{side}_deg_s": deg_s}, path=path)
    print(f"ok    {axis}: {side}_us = {pulse_us:.0f}, {side}_deg_s = {deg_s:.1f} written to {path}")
    other = "below" if side == "above" else "above"
    print(f"      Still needed: {other}_us and {other}_deg_s, measured the same way "
          f"on the other side of neutral, before {axis} is calibrated.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neo-servo-check", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="use exactly this motion config file")
    move = parser.add_mutually_exclusive_group()
    move.add_argument("--center", action="store_true",
                      help="drive pan and tilt to centre (continuous rotation: neutral)")
    move.add_argument("--pulse", nargs=2, type=float, metavar=("CHANNEL", "US"),
                      help="drive one channel to a pulse width, within the servo's hard limits")
    move.add_argument("--stop", action="store_true", help="stop sending pulses to both servos")
    move.add_argument("--move", nargs=2, type=float, metavar=("PAN_DEG", "TILT_DEG"),
                      help="drive the head through the motion driver to this angle and back to 0")
    parser.add_argument("--hold", type=float, default=2.0, help="seconds to hold before releasing")
    parser.add_argument("--keep", action="store_true", help="leave the servos driven on exit")
    parser.add_argument("--record-neutral", nargs=2, metavar=("AXIS", "US"),
                        help="write AXIS's (pan or tilt) measured neutral pulse to motion.local.yaml")
    parser.add_argument("--record-speed", nargs=2, metavar=("AXIS", "US"),
                        help="write a measured speed at this pulse; needs --turns, --seconds, and "
                             "--record-neutral for this axis already done")
    parser.add_argument("--turns", type=float,
                        help="full rotations counted while --record-speed's pulse ran (magnitude only)")
    parser.add_argument("--seconds", type=float, help="how long that pulse ran for, in seconds")
    args = parser.parse_args(argv)

    if args.record_neutral:
        return _record_neutral(args)
    if args.record_speed:
        return _record_speed(args)

    try:
        config = MotionConfig.load(args.config)
    except MotionConfigError as exc:
        print("FAIL  motion config is not usable, so nothing will be driven:")
        print(f"      {exc.source}")
        for problem in exc.problems:
            print(f"      - {problem}")
        return 1
    servo = config.servo
    for line in config.describe():
        print(f"      {line}")

    # Refused before any hardware is touched: a bad request must not half-happen.
    if args.pulse:
        channel_f, pulse_us = args.pulse
        if config.driver == "rpi-pwm":
            valid, meaning = RPI_PWM_PINS, "0 (GPIO18, pin 12) or 1 (GPIO19, pin 35)"
        else:
            valid, meaning = range(16), "a PCA9685 channel, 0-15"
        if channel_f != int(channel_f) or int(channel_f) not in valid:
            print(f"FAIL  --pulse CHANNEL must be {meaning}")
            return 2
        if not servo.min_us <= pulse_us <= servo.max_us:
            print(f"FAIL  {pulse_us:.0f} us is outside the {servo.name.upper()}'s "
                  f"{servo.min_us:.0f}-{servo.max_us:.0f} us; not sent")
            return 2
        if servo.continuous and args.keep:
            name = _axis_name(config, int(channel_f))
            neutral = config.calibration(name).neutral_us if name in ("pan", "tilt") else None
            if pulse_us != neutral:
                print("FAIL  --keep with a pulse away from neutral would leave a continuous-rotation")
                print("      servo turning until something stops it; not sent. Use --hold SECONDS.")
                return 2

    if args.move:
        return _move(config, args)
    if config.driver == "rpi-pwm":
        return _rpi_pwm(config, args)
    return _pca9685(config, args)


MOVE_RATE_HZ = 50.0
"""The servo driver node's own rate: slew limits are per tick, so it must match."""


def _move(config: MotionConfig, args) -> int:
    """Out to an angle and back through the real stack: `ServoDriver`, with its
    clamp, slew limit, deadband and watchdog, over the configured backend.

    For continuous-rotation servos this is the first test of the dead reckoning
    itself: the head should come back to where it started, and any gap is the
    drift of one out-and-back.
    """
    from ..backend import make_backend
    from ..driver import ServoDriver
    from ..types import HeadCommand

    if config.servo.continuous and not config.calibrated:
        print("FAIL  --move needs both servos calibrated: a continuous-rotation head's angle")
        print("      would otherwise be a guess. See docs/hardware.md, 'Calibrating each servo'.")
        return 1

    limits = config.head_limits()
    targets = []
    for value, axis, name in ((args.move[0], limits.pan, "pan"), (args.move[1], limits.tilt, "tilt")):
        low, high = math.degrees(axis.min_rad), math.degrees(axis.max_rad)
        clamped = max(low, min(high, value))
        if clamped != value:
            print(f"      {name} {value:+.0f} deg is outside {low:+.0f} to {high:+.0f} deg; using {clamped:+.0f}")
        targets.append(clamped)

    extra = {"clock": time.monotonic} if config.servo.continuous else {}
    try:
        backend = make_backend(config.driver, **config.backend_kwargs(), **extra)
    except (RuntimeError, ValueError) as exc:
        print(f"FAIL  {exc}")
        return 1
    driver = ServoDriver(limits=limits, backend=backend)
    dt = 1.0 / MOVE_RATE_HZ

    def go(pan_deg: float, tilt_deg: float, label: str) -> None:
        start_pan, start_tilt = driver.pose.degrees()
        seconds = max(abs(pan_deg - start_pan) / math.degrees(limits.pan.max_speed_rad_s),
                      abs(tilt_deg - start_tilt) / math.degrees(limits.tilt.max_speed_rad_s)) + 1.0
        end = time.monotonic() + seconds
        while (now := time.monotonic()) < end:
            driver.command(HeadCommand(pan_rad=math.radians(pan_deg), tilt_rad=math.radians(tilt_deg), stamp=now))
            driver.step(now, dt)
            time.sleep(dt)
        pan_now, tilt_now = driver.pose.degrees()
        line = f"ok    {label}: driver at pan {pan_now:+.1f}, tilt {tilt_now:+.1f} deg"
        estimate = getattr(backend, "estimate_deg", None)
        if estimate is not None:
            line += f"; estimate pan {estimate[0]:+.1f}, tilt {estimate[1]:+.1f} deg"
        print(line)

    try:
        go(targets[0], targets[1], f"out to pan {targets[0]:+.0f}, tilt {targets[1]:+.0f}")
        go(0.0, 0.0, "back at 0")
    except RuntimeError as exc:
        print(f"FAIL  {exc}")
        return 1
    finally:
        backend.close()
    print("ok    stopped and released.")
    if config.servo.continuous:
        print("      If the shaft mark is not back where it started, that gap is the drift of one")
        print("      out-and-back: the angle is estimated from the calibration, never measured.")
    return 0


def _rpi_pwm(config: MotionConfig, args) -> int:
    servo = config.servo
    try:
        chip = SysfsPwmChip.find(PWM_ROOT, config.pwm_chip)
    except RuntimeError as exc:
        print(f"FAIL  {exc}")
        print("      With it on, GPIO18 (pin 12) and GPIO19 (pin 35) carry the servo signals.")
        return 1
    print(f"ok    hardware PWM controller {chip.path.name}, {chip.npwm} channels")
    if not os.access(chip.path / "export", os.W_OK):
        print(f"FAIL  {chip.path / 'export'} is not writable by this user.")
        print("      sudo bash ~/neo1/scripts/pi/setup-system.sh --servo-pwm adds the pwm group")
        print("      and its udev rule; then log out and back in (or reboot).")
        return 1
    print("ok    this user can drive it")

    if args.stop:
        stopped = []
        for channel in sorted(RPI_PWM_PINS):
            if chip.channel_path(channel).exists():
                chip.disable(channel)
                chip.unexport(channel)
                stopped.append(config.channel_label(channel))
        print(f"ok    stopped: {'; '.join(stopped) if stopped else 'nothing was being driven'}")
        return 0

    if not (args.center or args.pulse):
        print("      Read-only check done; nothing was moved. --center or --pulse drives the servos.")
        for line in _power_lines(servo):
            print(line)
        print("      Signal wires: pan -> " + config.channel_label(config.pan.channel)
              + ", tilt -> " + config.channel_label(config.tilt.channel) + ".")
        return 0

    moves = _moves(config, args)
    try:
        for _name, channel, _pulse in moves:
            chip.open_channel(channel, servo.frequency_hz)
        for name, channel, pulse in moves:
            chip.set_pulse_us(channel, pulse)
            print(f"ok    {name} ({config.channel_label(channel)}) -> {pulse:.0f} us at {servo.frequency_hz} Hz")
    except RuntimeError as exc:
        print(f"FAIL  {exc}")
        return 1
    if args.pulse:
        _calibration_hint(servo.continuous)

    if args.keep:
        print("      Holding; the pulses keep running. neo-servo-check --stop ends them.")
        return 0
    time.sleep(max(0.0, args.hold))
    for _name, channel, _pulse in moves:
        chip.disable(channel)
        chip.unexport(channel)
    print(f"ok    released after {args.hold:.1f} s")
    return 0


def _pca9685(config: MotionConfig, args) -> int:
    servo = config.servo
    if not _check_bus(config.i2c_bus):
        return 1

    try:
        bus = open_smbus(config.i2c_bus)
    except ImportError:
        print("FAIL  smbus2 is not installed: pip install -e '.[servo]' from the repo root")
        return 1

    chip = Pca9685(bus, config.address)
    try:
        try:
            mode1 = chip.probe()
        except OSError:
            print(f"FAIL  nothing answered at 0x{config.address:02x} on bus {config.i2c_bus}.")
            print("      Wiring (PCA9685 -> Pi header): VCC -> pin 1 (3.3V), GND -> pin 6,")
            print(f"      SDA -> pin 3 (GPIO2), SCL -> pin 5 (GPIO3). Then: i2cdetect -y {config.i2c_bus}")
            return 1
        prescale = bus.read_byte_data(config.address, Pca9685.PRESCALE)
        hz = Pca9685.OSCILLATOR_HZ / (Pca9685.TICKS * (prescale + 1))
        state = "asleep" if mode1 & Pca9685.SLEEP else "awake"
        print(f"ok    PCA9685 at 0x{config.address:02x}: MODE1=0x{mode1:02x} ({state}), "
              f"prescale {prescale} = {hz:.1f} Hz")

        if args.stop:
            for name in ("pan", "tilt"):
                chip.set_off(config.axis(name).channel)
            print("ok    stopped: both channels held low")
            return 0

        if not (args.center or args.pulse):
            print("      Read-only check done; nothing was moved. --center or --pulse drives the servos.")
            for line in _power_lines(servo):
                print(line)
            return 0

        chip.start(servo.frequency_hz)
        moves = _moves(config, args)
        for name, channel, pulse in moves:
            chip.set_pulse(channel, pulse)
            print(f"ok    {name} (channel {channel}) -> {pulse:.0f} us "
                  f"({chip.ticks_for(pulse)} ticks at {chip.frequency_hz:.2f} Hz)")
        if args.pulse:
            _calibration_hint(servo.continuous)

        if args.keep:
            print("      Holding position; the servos stay driven.")
            return 0
        time.sleep(max(0.0, args.hold))
        for _name, channel, _pulse in moves:
            chip.set_off(channel)
        print(f"ok    released after {args.hold:.1f} s")
        return 0
    finally:
        bus.close()


if __name__ == "__main__":
    sys.exit(main())
