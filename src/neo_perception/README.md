# neo_perception — detection, gestures, and gaze engagement

Person and object detection, hand-gesture recognition, and the engagement state
machine that decides who the head follows and when it lets go.

Implements Phase 4 of [the implementation plan](../../docs/IMPLEMENTATION_PLAN.md),
plus gesture-driven engagement, which was added after the plan was written.

## The behaviour

1. Someone walks up. They are detected and tracked, but the head does not follow —
   presence alone is not a request for attention.
2. They **raise a hand** and hold it for about half a second. The head locks on.
3. The lock **follows them** around the frame, and survives the detector dropping
   a frame or somebody walking in front of them.
4. They **leave** — the lock releases on its own, and the next person can engage.

## One model, not two

The obvious reading of "person detection plus hand gestures" is two models. On a
Pi 4 that is the wrong shape: a dedicated hand network roughly doubles the
per-frame cost of a pipeline that only has ~4 fps to begin with.

A single `yolov8n-pose` pass gives all three things instead:

| Need | Comes from |
|---|---|
| Presence, "someone approached the desk" | person boxes |
| The gesture | COCO-17 keypoints, derived arithmetically — free |
| Where to actually look | face keypoints — the face, not the sternum |

Object detection is genuinely a different model, so it is **on demand** rather
than continuous, and its cost is paid only when something asks.

> **Frame-rate reality.** A *static* pose held over several frames is reliable at
> 3–5 fps. A *dynamic* one is not: a 2 Hz wave sampled at 4 fps aliases badly.
> `WAVE` is implemented and works when the pipeline is fast enough, but
> `RAISED_HAND` is the default engage gesture because it survives the Pi.

## Two grace periods, not one

"The target vanished" has two very different causes, and treating them alike makes
the robot feel broken either way:

| Cause | Evidence | Grace | Why |
|---|---|---|---|
| Walked out of shot | last box touched a frame edge | 0.6 s | A long grace here means the head stares at a doorway |
| Detector blinked, or someone walked in front | last box in open frame | 2.0 s | A short grace here drops the lock every time the detector misses a frame — which at 4 fps is often |

Re-acquisition is by track id, so a grace period can never usefully outlive the
tracker's own memory. `EngagementController.validate_against_tracker` checks that
relationship and warns rather than failing silently.

## Layout

```
types.py       BBox, Keypoints, Track, GestureEvent, AttentionTarget
detector.py    Detector ABC + UltralyticsDetector + MockDetector
tracker.py     greedy IoU/centroid tracking, time-based ageing
gestures.py    keypoint -> gesture, scale-invariant
engagement.py  the lock/unlock state machine
gaze.py        image error -> normalised pan/tilt rate
pipeline.py    glue, plus the latest-wins worker thread
nodes/         thin ROS wrapper (Phase 1)
```

Everything except `detector.py` and `nodes/` is **pure Python** — no ROS, no
OpenCV, no numpy — so a scene is a few lines of test code and an explicit clock.

## Design notes

**Latest-wins, never queue.** `AsyncPerception` holds a single input slot and
drops frames when inference falls behind. A queued backlog makes the head chase
where somebody used to be, which looks far worse than a lower frame rate.

**Gaze is visual servoing.** The camera rides the head, so proportional control on
*image* error closes the loop with no intrinsics, no distance estimate, and no
kinematics. Centring a face does not need any of them.

**Output is an attention target, not a servo command.** The head arbiter decides
whether to act on it, so manual control always outranks the robot's own gaze.

## Running it

Tests need nothing but pytest:

```bash
python -m pytest -q
```

The real detector needs the extra, installed from the repo root (this package
has no `setup.py` of its own — see the [top-level README](../../README.md)):

```bash
pip install -e ".[detector,dev]"
```

Benchmark a backend — this is the plan's Bench B, and it also verifies the model
really produces the COCO-17 layout the gesture code assumes:

```bash
python -m neo_perception.scripts.bench --source bus --imgsz 320
```

### On the Pi

Export to NCNN first; it is several times faster than PyTorch weights on ARM:

```bash
yolo export model=yolov8n-pose.pt format=ncnn
```

Then point `UltralyticsConfig.model` at the exported directory and re-run the
bench to get the real figure for `docs/hardware.md`.

## Trying it without a robot

The admin panel runs this exact pipeline against whatever camera it is using, so
the whole behaviour can be exercised with no Pi, no ROS, and no servos — see the
Vision tab. The head it drives is simulated; the perception is not.
