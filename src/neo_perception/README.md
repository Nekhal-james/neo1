# neo_perception — detection, gestures, and gaze engagement

Person and object detection, hand-gesture recognition, and the engagement state
machine that decides who the head follows and when it lets go.

Implements Phase 4 of [the implementation plan](../../docs/IMPLEMENTATION_PLAN.md),
plus gesture-driven engagement, which was added after the plan was written.

## The behaviour

1. Someone walks up. They are detected and tracked, but the head does not follow —
   presence alone is not a request for attention.
2. They **show a palm** — hand up, forearm vertical — and hold it for about half a
   second. The head locks on.
3. The lock **follows them** around the frame, and survives the detector dropping
   a frame or somebody walking in front of them.
4. They **leave, or turn their back** — the lock releases on its own, and the next
   person can engage.
5. They hold something up and **ask what it is** — a second model runs, once, on
   that frame.

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
> `WAVE` is implemented and works when the pipeline is fast enough, but the
> default engage gesture is a static pose because it survives the Pi:
> `OPEN_PALM`, a raised hand with a vertical forearm.

## When a palm will not register

The palm test is a chain — both shoulders visible for scale, a wrist above its
shoulder, that arm's elbow visible, the forearm pointing up, long enough to judge,
and within 40° of vertical — and the lock needs it unbroken for 0.6 s. At a real
desk it breaks on different links for different people, and from outside they
all look the same: nothing happens.

`GestureRecognizer.explain(track, stamp)` names the link that broke and by how
much, for whoever matters most on the frame: the person part-way through a hold,
else the locked target, else the nearest person. The pipeline puts it on every
result as `palm_check`, with `hold_progress` beside it; the panel shows both on
the Vision tab and writes them to `var/perception/status.json`. It mirrors
`_classify` step for step, and a 500-case fuzz test fails if the two ever
disagree, so change them together.

A single frame that is not a palm restarts the hold. That is deliberate — it is
what rejects keypoint noise, such as a person clipped at the frame edge whose
wrist, elbow and shoulder pile up within a few pixels and read as a palm for one
frame (measured: 1 frame in 47 on bus.jpg) — but it is also the first thing to
check if a real palm keeps failing: the hold bar will keep falling back to zero.

Measured on a laptop webcam, the link people actually break is the elbow. Close
to the screen, a raised hand read as `raised_hand` with the elbow at 0.21–0.33
confidence against the 0.35 cutoff: the model does not extrapolate an elbow that
is below the frame (checked on real people too — 0.01–0.32 whenever the crop
ended above mid-upper-arm). Sitting back until it was in shot, the same palm
scored 0.35 at a 21° tilt and locked. That elbow sat exactly on the threshold,
so if palms lock only intermittently at a real desk, the elbow's confidence
cutoff is the first thing to measure — not the tilt limit.

## The head is what gets tracked

People stand close at a reception desk. The *person* box is then clipped by the
frame and its centre lands on a torso filling the view — useless as an aim point
for a pan/tilt head. So the tracked box is the **head**, derived from the
nose/eyes/ears the pose pass already produced. No second model, no extra
inference.

Measured on real frames at four framings, from full-scene down to head-only:

| Framing | Detection score | Face keypoints | Body keypoints |
|---|---|---|---|
| full frame | 0.85 | 5/5 | 8/8 |
| person fills frame | 0.92 | 5/5 | 8/8 |
| head + shoulders | 0.92 | 4/5 | 5/8 |
| head only | 0.93 | 4/5 | **2/8** |

Detection gets *better* close up. What degrades is the body, which is exactly
what we stopped depending on.

Head width comes from the widest reliable span available — ear-to-ear, else
eye-to-eye (~0.31 of head width), else shoulders (~0.45 of their span), else the
person box. Each is checked for plausibility against a scale reference rather
than merely for being positive, because the degenerate cases are the common
ones: turned away, the two ear keypoints converge and their span becomes a
couple of pixels, which yielded a 1×2 px "head" that matched nothing and
silently destroyed the track. The *reference* is validated too — a collapsed
3 px shoulder span once rejected every other candidate as "too wide" and
returned no head at all.

> **Limitation worth knowing.** The engage gesture needs shoulder, elbow and
> wrist. At head-only framing those are gone (2/8 keypoints), so a palm cannot
> be seen and nobody can engage. If the camera is framed that tightly, either
> widen it to include the upper body or engagement needs a different trigger at
> close range.

## Palm, not just "hand up"

`RAISED_HAND` (wrist above shoulder) is a weak signal: stretching, reaching for a
shelf and scratching your head all produce it. `OPEN_PALM` additionally requires
the **forearm to be roughly vertical**, which is what separates "I want your
attention" from those. It is the default engage gesture.

The honest limit: COCO-17 has no finger keypoints, so this is a *pose*, not a hand
shape — a raised fist looks identical to a raised palm. Telling those apart needs
a hand-landmark model on a wrist crop, which is a per-frame cost the Pi has not
got. In practice the vertical-forearm test is what does the useful work.

## Three ways to lose someone, three timings

"The target is gone" has causes that look identical to the code and want opposite
handling. One timeout cannot serve all three:

| Cause | Evidence | Grace | Why |
|---|---|---|---|
| Walked out of shot | last box touched a frame edge | 0.6 s | A long grace here means the head stares at a doorway |
| Detector blinked, or someone crossed in front | last box in open frame | 2.0 s | A short grace here drops the lock every time the detector misses a frame — which at 4 fps is often |
| **Turned their back** | shoulder parity + no face keypoints | 1.5 s | Nothing above catches this: the person is perfectly visible and perfectly tracked, so the lock would hold until they happened to walk out of shot |

Facing comes from keypoints alone — no extra model. COCO labels sides from the
*person's* frame, so someone facing the camera has their left shoulder on the
image's **right**; turn around and that inverts. Face-keypoint visibility is the
second signal, because at shallow angles the shoulder parity is within noise.
PROFILE (edge-on) deliberately does not start the release clock: glancing down a
corridor is still being in the conversation.

Re-acquisition is by track id, so a grace period can never usefully outlive the
tracker's own memory. `EngagementController.validate_against_tracker` checks that
relationship and warns rather than failing silently.

## "What is this?"

Object identification is **request-driven**, not continuous. It is a second model,
so running it every frame would roughly double the cost of a pipeline that has
~4 fps to spend — and nobody needs the desk identified sixty times a minute. The
model is loaded lazily on the first question.

Ranking picks the thing being *presented* rather than the largest thing in shot:

    prominence = centrality² × √(area fraction)

Centrality is squared because it is the stronger signal — people hold things up in
the middle of the frame, and the failure to avoid is confidently naming a chair at
the edge. People are excluded; the questioner is not the answer.

## Layout

```
types.py         BBox, Keypoints (incl. facing), Track, GestureEvent, ObjectGuess
detector.py      Detector ABC + Ultralytics pose/object backends + MockDetector
tracker.py       greedy IoU/centroid tracking, time-based ageing
gestures.py      keypoints -> gesture, scale-invariant
engagement.py    the lock/unlock state machine
gaze.py          image error -> normalised pan/tilt rate
pipeline.py      glue, latest-wins worker thread, object ranking
status_store.py  var/perception/status.json -- how other processes see the camera
nodes/           thin ROS wrapper (Phase 1)
```

Everything except `detector.py` and `nodes/` is **pure Python** — no ROS, no
OpenCV, no numpy — so a scene is a few lines of test code and an explicit clock.

## Sharing what the camera sees

`status_store.py` publishes vision state to `var/perception/status.json`, the same
atomically-replaced-JSON pattern `model_conn` and `intelligence` already use for
their own state.

The point is process independence: `neo --webapp up`, `neo --model ... up` and
`neo --prompt` are separate processes started in any order, and any may be absent.
A file that is missing or stale is a state every reader already handles; a socket
that is not listening yet is not.

What it buys — the camera belongs to the panel's process, but:

```bash
neo --vision:status            # what the robot can see right now
neo --prompt "what is this"    # answerable, because chat reads the same file
```

`intelligence.chat` appends one line of camera context to the *system* prompt
(never the user's message), and only when the file is fresh.

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
