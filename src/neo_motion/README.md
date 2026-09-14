# neo_motion — head arbitration and the servo driver

Two servos move Neo's head, and several things want to move them. This package
decides **who wins** and then decides **what the servos are actually allowed to
do about it**. It is the only path to the servos; nothing else in the repo talks
to a PCA9685.

Implements Phase 3 of [the implementation plan](../../docs/IMPLEMENTATION_PLAN.md).

## Two layers, because they answer different questions

```
  joystick ─┐
  gesture  ─┤
  gaze     ─┼──►  HeadArbiter  ──►  ServoDriver  ──►  PCA9685  ──►  pan/tilt
  idle     ─┤     "who drives"      "what moves"
  estop    ─┘
```

`HeadArbiter` is policy and can be argued about. `ServoDriver` is safety and
cannot: it is the last code that runs before something physical moves, so it
trusts nothing upstream. Keeping them apart means a bug in the first can never
talk the second into exceeding a limit.

## Priority, and the rule that matters

| Priority | Source | |
|---|---|---|
| 90 | `estop` | freeze |
| 70 | `manual` | the panel's virtual joystick |
| 50 | `gesture` | a bounded nod/shake/tilt overlay |
| 30 | `gaze` | tracking an engaged person |
| 10 | `idle` | drift and micro-motion |

**A hand on the joystick always outranks the robot's own idea of where to look.**
Gaps between the levels are deliberate — a navigation source can be inserted
later without renumbering anything already deployed.

Handovers **crossfade** over ~300 ms rather than jumping, and the fade starts
from where the head *actually is* (`sync_to`), not from the last target it was
asked for. Those differ whenever a previous fade was cut short, and jumping the
difference is a shock load.

## The deadman is source expiry, not a special case

The joystick is virtual — it lives in a browser, on the far side of a WebSocket.
So "the operator let go" and "the connection died mid-drag" arrive as the same
thing: nothing. A source that stops publishing stops winning, and the head is
handed to whatever is below it. There is no separate deadman timer, because a
timer that only guards the joystick is a timer that does not guard the next
network-mediated source somebody adds.

## What the driver enforces, in this order

```
clamp to soft limits → slew-rate limit → deadband → watchdog → estop
```

Clamping first means a wild target cannot be reached even briefly. Rate limiting
after it turns a step command into a move a gearbox survives. The deadband stops
the hunting a rate limiter alone leaves behind. Watchdog and e-stop sit outermost
because they must beat everything, including a perfectly valid command that
simply arrived too long ago.

A request may ask the driver to move **slower** than the axis cap. It can never
ask it to move faster.

**Holding is the safe state.** Neither a tripped watchdog nor an engaged e-stop
releases the servos — a head that goes limp drops under its own weight. The only
place releasing is correct is `shutdown()`, which centres first.

### The deadband is also a smoothness budget

It cannot tell a jittering stationary target from a slowly moving one, and
handles both the same way: the error creeps to the threshold, the axis covers the
gap in one tick, the error resets. Slow motion therefore comes out in steps of
roughly one deadband — a 2° idle sine renders as 58 jumps of 0.43° over 30 s at
the current 0.4° placeholder, against 256 jumps of 0.12° at 0.1°.

There is no way to have both; refusing sub-deadband corrections *is* what
quantizes a slow ramp. The only lever is the size, and the hardware puts a floor
under it: a PCA9685 at 50 Hz resolves about **0.44°** per step (4.9 µs of a
2000 µs range over 180°), the same as the placeholder deadband, so a smaller
deadband smooths the commanded motion but not the servo's. The usual way under
that floor is a higher PWM frequency, and it is not available here: the robot's
TowerPro MG995s are specified for 50 Hz. See
[docs/hardware.md](../../docs/hardware.md).

## Servo config and calibration

`config/motion.yaml` says which servo drives the head (`mg995`), how its signal
reaches it, which channel each axis is on, and each axis's calibration. The
robot uses `driver: rpi-pwm` — signal wires straight to the Pi's hardware PWM
pins, GPIO18 (pan) and GPIO19 (tilt), driven through `/sys/class/pwm` by
`RpiPwmBackend`; `driver: pca9685` drives a PCA9685 board over I2C instead.
Neither is software PWM, which twitches whenever YOLO has the CPU. The measured
calibration goes in `config/motion.local.yaml`,
the robot's measured numbers go in `config/motion.local.yaml`, merged over it.
`neo_motion.config.MotionConfig` loads it and refuses anything incomplete or
unknown, naming every problem at once.

**The robot's servos are continuous-rotation (360°) MG995s** (`model: mg995-360`),
with no position sensor. Their pulse sets speed, not angle, so
`RpiPwmContinuousBackend` dead-reckons: each `ContinuousAxis` turns the angle
the driver wants into a speed through the servo's measured neutral pulse, stop
band and speed, and adds up what it commanded. The angle is an estimate that
drifts from wherever the head pointed at start-up. Every way the driver holds
still — its hold, watchdog and e-stop all rewrite the current pose — arrives as
neutral, which stops these servos; a 150 ms stall watchdog covers a process that
stops writing, and `neo-servo-check --stop` covers one that was killed. Until
calibrated, the backend refuses to drive them.

For positional servos (`model: mg995`), until an axis is calibrated it is held to
**1200–1800 µs, labelled ±27°**. The
MG995's published pulse ranges disagree (0.5–2.5 ms or 1–2 ms for 180°), and
that window is clear of the end stops under either; driving a servo into its
stop stalls it at over an amp. The label is exact under the first reading only
(under the second the head turns about ±54°), which calibration settles. The driver's soft limits are narrowed to the
calibration too (`MotionConfig.head_limits`), so it never reports a pose the
servo was clamped short of. `neo-servo-check --pulse CHANNEL US` is how the
real limits get measured.

## Running it without a robot

`MockServoBackend` records what would have been written, so the whole package —
limits, slew, deadband, watchdog, e-stop, arbitration, crossfade — is tested with
no Pi, no I2C bus and no servos:

```bash
(cd src/neo_motion && pytest -q)
```

The admin panel runs this same code against the same mock backend, so the Head
tab is not a simulation of the motion stack. It *is* the motion stack.

## Status

The pure-Python core is complete and tested. The ROS nodes in `neo_motion/nodes/`
document their wiring and raise `NotImplementedError` until Phase 3 binds them to
`neo_msgs` — see the repo [CLAUDE.md](../../CLAUDE.md) for why the nodes land last.
