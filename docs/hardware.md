# Neo — hardware

What is wired to the Raspberry Pi 4, and how to check each part. Calibration
numbers go here as they are measured (plan Phase 0 and Phase 3, step 1).

## Head servos: two continuous-rotation TowerPro MG995

Two servos, pan and tilt. Their **signal wires go straight to the Pi's two
hardware PWM pins**; their power comes from a buck converter, never from the Pi.
`config/motion.yaml` says so with `model: mg995-360` and `driver: rpi-pwm`.

### What "360°" means for Neo

These are **continuous-rotation** servos: the pulse sets a **speed and
direction**, not an angle. Around 1500 µs they stop; further from it they turn
faster, one way or the other. They report nothing back. That was a deliberate
choice to keep these servos, so the software works around it:

- **The head's angle is estimated, not known.** `RpiPwmContinuousBackend` turns
  each angle the driver asks for into a speed, using measurements of the servo,
  and adds up the speed it sent over time. The estimate **drifts**: each move
  adds a little error, and nothing corrects it.
- **0° is wherever the head points when the program starts.** Point the head
  forward before starting anything that drives it.
- **Limits apply to the estimate.** The head is kept within `min_deg`/`max_deg`
  (±30° unless set) of where it started, and never faster than the servo was
  measured to turn. As the estimate drifts, so does where those limits really
  are: keep an eye on the camera cable.
- **Stopped is the safe state.** Holding still sends the neutral pulse. If the
  program stops sending commands for 150 ms, a watchdog sends neutral. If the
  program exits, it stops the outputs.
- **A killed program leaves the servos turning.** The Pi keeps repeating the last
  pulse. `~/neo-venv/bin/neo-servo-check --stop` ends it.
- **Nothing drives the head until both servos are calibrated.** Without the
  measurements every angle would be a guess, so the backend refuses.

Positional (180°) servos would remove all of the above; the code supports them
too (`model: mg995`, at the end of this section).

### The MG995, electrically

| | MG995 | Consequence here |
|---|---|---|
| Supply | 4.8–6 V ([ProtoSupplies][ps]; other sheets stretch to 6.6 or 7.2 V) | Set the buck converter to **5–6 V**, never more |
| Stall current | 1.3–1.5 A measured ([ProtoSupplies][ps]), ~1.2 A ([Arduino forum][af]) | Buck converter rated **at least 3 A** for the pair |
| Running current | 170–400 mA ([ProtoSupplies][ps]) | |
| PWM | 50 Hz, 20 ms frame ([Components101][c101]) | Driven at 50 Hz only |

[ps]: https://protosupplies.com/product/servo-motor-mg995/
[af]: https://forum.arduino.cc/t/mg995-stall-current/1201149
[c101]: https://components101.com/motors/mg995-servo-motor

### Wiring: straight to the Pi

```
 pan MG995                                  Raspberry Pi 4 header
 ─────────                                  ─────────────────────
 orange  signal ──────────────────────────  pin 12   GPIO18  (hardware PWM0)
 red     V+     ───┐
 brown   GND    ───┼──┐
                   │  │
 tilt MG995        │  │
 ──────────        │  │
 orange  signal ───┼──┼───────────────────  pin 35   GPIO19  (hardware PWM1)
 red     V+     ───┤  │
 brown   GND    ───┼──┤
                   │  │
 buck converter    │  │
 OUT+  5-6 V  ─────┘  │
 OUT−  GND    ────────┴───────────────────  pin 6    GND     (the common ground)
 IN+/IN−  from the main supply
```

- **Only the orange signal wires touch the Pi**, and only physical pins 12 and
  35. Pin 1 is the corner pin nearest the SD card end with the square pad;
  GPIO12 (physical pin 32) is a common mix-up.
- **Set the buck converter's output with a multimeter before connecting the
  servos**, and check it again with both servos connected: 5–6 V, rated for at
  least 3 A.
- **One common ground.** The converter's OUT− must connect to a Pi GND pin (6,
  9, 14, 20, 25, 30, 34 or 39), or the signal has no reference.
- A 470–1000 µF capacitor across the converter's output, near the servos,
  smooths the current spikes when both start at once.
- **The signal is 3.3 V.** If a servo ignores it while it works on an RC
  receiver, put a 3.3 V → 5 V logic level shifter on its signal wire.
- **The Pi's 3.5 mm audio jack is off.** Its analog audio comes from the same
  PWM block, so the setup turns it off; Neo's speaker has to be USB.

### Setting up the Pi, once

```bash
sudo bash ~/neo1/scripts/pi/setup-system.sh --servo-pwm
sudo reboot
```

`--servo-pwm` adds `dtoverlay=pwm-2chan` to `/boot/firmware/config.txt` (its
defaults are exactly GPIO18 and GPIO19), turns off the 3.5 mm audio jack, and
lets the `neo` user drive the PWM without root: a `pwm` group and a udev rule
(`scripts/pi/files/61-neo-pwm.rules`). Verified on this robot: after the reboot
`/sys/class/pwm/pwmchip0` is the Pi's controller and `neo` can export and set
both channels.

### Calibrating each servo

Horns off, both servos connected. Calibrate pan (channel 0) first, then tilt
(channel 1). Every `--pulse` runs for `--hold` seconds (default 2) and then
stops; `--keep` is refused with anything but neutral, because the servo would
keep turning.

1. **Signal check.** `~/neo-venv/bin/neo-servo-check --pulse 0 1600` should make
   pan turn slowly one way for two seconds, then stop. If it does not turn at
   all, fix the wiring before anything else.

2. **Neutral.** Find the pulse where it does not turn at all:

   ```bash
   ~/neo-venv/bin/neo-servo-check --pulse 0 1500 --hold 5
   ```

   If it creeps, try 1490, 1510, then smaller steps, until it stays completely
   still.

3. **Stop band.** From a pulse where it stays still, halve the distance to a
   pulse that turns it, both ways, until each edge is known to about 10 µs.
   `neutral_us` is the middle of the band of still pulses and `deadband_us` half
   its width. Pan: still from 1400 to 1505, turning at 1375 and 1510, so the band
   is about 1387-1507, `neutral_us` 1447 and `deadband_us` 60.

4. **Speed, both directions.** Mark the shaft with a pen, then run it for 20
   seconds at 150 µs above neutral, then 150 µs below, counting full turns (to
   the nearest quarter) each time:

   ```bash
   ~/neo-venv/bin/neo-servo-check --pulse 0 1597 --hold 20    # neutral 1447 + 150
   ~/neo-venv/bin/neo-servo-check --pulse 0 1297 --hold 20    # neutral 1447 - 150
   ```

   `speed_offset_us` is 150; `speed_deg_s` is the turns above neutral × 360 ÷ 20,
   and `speed_below_deg_s` the turns below × 360 ÷ 20. **Measure both**: pan
   turned 3.25 times one way and 4.5 times the other, 38 % apart. With one speed
   for both, the estimate would move about 10° per back-and-forth.

5. **Write it down** in `config/motion.local.yaml` on the Pi (never copied or
   overwritten by the laptop's sync): the four required numbers per servo, plus
   `speed_below_deg_s` when the two directions differ. Pan's are its real
   measurements; tilt's come from the same steps on channel 1:

   ```yaml
   pan:
     neutral_us: 1447
     deadband_us: 60
     speed_offset_us: 150
     speed_deg_s: 58.5
     speed_below_deg_s: 81
     min_deg: -60        # optional: how far the estimated angle may go; +/-30 if left out
     max_deg: 60
   tilt:
     neutral_us: ...     # measured on channel 1, as above
     deadband_us: ...
     speed_offset_us: 150
     speed_deg_s: ...
     speed_below_deg_s: ...
   ```

   Then `~/neo-venv/bin/neo-servo-check` should show both as "calibrated". If a
   servo turns the wrong way for its axis, add `inverted: true` rather than
   changing numbers. Anything missing or misspelt is refused, with every problem
   listed, rather than half-applied.

6. **First real move.** With both calibrated, drive the head through the actual
   motion driver, the same code the robot uses, out to an angle and back:

   ```bash
   ~/neo-venv/bin/neo-servo-check --move 20 0     # pan to +20 deg, back to 0, stop
   ~/neo-venv/bin/neo-servo-check --move 0 15     # tilt to +15 deg, back to 0, stop
   ```

   It prints where the driver and the dead-reckoned estimate each think the head
   is. The shaft mark should come back close to where it started; the gap is the
   drift of one out-and-back. A consistently large gap in one direction points
   at that direction's speed measurement.

7. **Fit the horns and the head** with both servos stopped
   (`neo-servo-check --center --keep` holds them at neutral), head facing
   forward and level. Forward is 0° every time the program starts.

### Software

`neo_motion.backend.RpiPwmContinuousBackend` drives `/sys/class/pwm` through
`SysfsPwmChip`: it finds the Pi's PWM controller by what it is
(`brcm,bcm2835-pwm`), exports the two channels, sets a 20 ms period, and writes
each pulse width in nanoseconds, only when it changes. Each `ContinuousAxis`
models its servo's speed as linear from the edge of the stop band, capped at
400 µs from neutral. `neo_motion.config.MotionConfig` reads `config/motion.yaml`
and `config/motion.local.yaml`, and `make_backend()` and the servo driver node
build from it.

### Alternative: positional (180°) servos

If the servos are ever swapped for positional MG995s, set `model: mg995` in
`config/motion.local.yaml`. Calibration then measures the pulse at each end of
travel (`min_us`, `max_us`, `min_deg`, `max_deg`), and until measured each axis
is held to 1200–1800 µs. The MG995's published pulse ranges disagree, 0.5–2.5 ms
([Components101][c101]) or 1–2 ms ([ProtoSupplies][ps]) for 180°, and that
window is clear of the end stops under either. `RpiPwmBackend` drives them on
the same pins, or `driver: pca9685` drives a PCA9685 board on I2C
(`Pca9685Backend`, registers written with `smbus2`; VCC to pin 1 at 3.3 V, SDA to
pin 3, SCL to pin 5, GND to pin 6).

### Calibration (measured on the robot)

| Servo | Signal | Neutral | Stop band | Speed above / below neutral | Angle limits | Inverted |
|---|---|---|---|---|---|---|
| pan (MG995 360°) | GPIO18, pin 12 | 1447 µs | 1387–1507 µs (±60) | 58.5 °/s counter-clockwise / 81 °/s clockwise, at ±150 µs | ±30° (default) | not yet decided (head not mounted) |
| tilt (MG995 360°) | GPIO19, pin 35 | 1488 µs | 1450–1525 µs (±38) | 108 °/s both ways at ±400 µs (36 °/s at ±150, same both ways) | ±30° (default) | not yet decided (head not mounted) |
