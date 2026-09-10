# neo_emotion — mood, and the movement it shapes

Neo has no face and no screen. Everything it expresses, it expresses by *how it
moves*. This package decides what it currently feels and publishes the parameters
that shape the movement — and it publishes nothing else.

Implements Phase 9 of [the implementation plan](../../docs/IMPLEMENTATION_PLAN.md).

## A modifier, never an actuator path

This is the design constraint the package exists under, and the one worth
defending in review:

```
  emotion_node ──► /emotion/state ──► head_behavior ──► servo_driver
                                          ▲
                    (blended into the command it was already sending)
```

`neo_emotion` has **no route to the servos**. It does not depend on `neo_motion`,
not even in `package.xml`, because a build dependency is the first step towards
growing one. If this package ever gains a `/head/command` publisher, the design
has been lost: there would then be two things driving the head, and the arbiter's
priority order — the thing that guarantees an operator can always take over —
would no longer be the whole story.

## Driven by events, not vibes

Every transition traces to something that actually happened, which is what makes
the behaviour debuggable rather than merely charming.

| What happened | What it reads as |
|---|---|
| Someone walks up | `CURIOUS`, settling into `ATTENTIVE` |
| They engage | `ATTENTIVE` at full intensity |
| They gesture a greeting | `HAPPY`, briefly |
| A room lookup misses | `CONFUSED` |
| Nobody for 90 s | `SLEEPY` |
| The off-board link drops | a subdued `NEUTRAL` |

That last row matters. The laptop serving the LLM is the owner's daily driver, so
**degraded mode is the normal operating state** — it must not look like distress.
A robot that appears alarmed whenever its optional half is missing is a robot
that appears alarmed most of the time.

Every state has a **minimum dwell time**, because the events are noisy: a
detector that flickers for one frame would otherwise flip the apparent mood
several times a second, which reads as a fault rather than as a feeling.
Deliberate, discrete events — a failed lookup, a greeting — bypass the dwell,
because those *should* be immediate and are rare enough not to flicker.

## Idle motion is two sines, on purpose

A single sine reads as a metronome within about ten seconds, which is worse than
no motion at all. Two components at an incommensurate ratio (0.618) never
re-align, so the pattern does not visibly loop.

Micro-motion rides on top at a higher rate, and its **absence** is what actually
makes `SLEEPY` read as asleep — amplitude alone just looks like a slow version of
awake.

## Gestures are overlays, never modes

A nod, a shake, a tilt, a scan: each is a bounded trajectory added on top of the
base pose, tapered at both ends with a `sin(πt)` envelope so it grows in and dies
away instead of starting and stopping with a step. Every one returns to zero, so
the head cannot get stuck in one.

Triggering a gesture **replaces** any already running rather than queueing it. A
queue would let a burst of dialogue events buy several seconds of head movement
that outlives the moment which caused it.

## The profile table is the personality

`PROFILES` in [`types.py`](neo_emotion/types.py) is tuned by eye rather than
derived, and the only honest way to set it is to watch the head for a few minutes
at a time. The failure mode is motion that reads as mechanical, or worse, twitchy.

Note that the amplitudes here interact with the servo driver's deadband: idle
drift smaller than a few deadbands arrives as visible steps rather than as drift.
See [`neo_motion`](../neo_motion/README.md#the-deadband-is-also-a-smoothness-budget).

```bash
(cd src/neo_emotion && pytest -q)
```

## Status

The pure-Python core is complete and tested, and the admin panel drives it live —
the mood shown on the panel comes from this controller, and the idle drift the
head shows there is this package's. The ROS node in `neo_emotion/nodes/` documents
its wiring and raises `NotImplementedError` until Phase 9 binds it to `neo_msgs`.
