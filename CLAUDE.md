# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Eight packages under `src/` (872 tests). Six are pure Python: `neo_webapp`
(admin panel), `neo_perception` (detection/gestures/engagement), `neo_motion`
(head arbiter and servo driver), `neo_emotion` (mood and the motion it shapes),
`model_conn` (the `neo` CLI and off-board link), and `intelligence` (prompts,
chat, ASR/TTS). Two are ROS: `neo_msgs` (the frozen interface contracts) and
`neo_bringup` (launch profiles). [README.md](README.md) tracks what each one
currently does.

Not built: the wake word. The campus knowledge base is partly built:
`intelligence/campus.py` (the three YAML files, validation, atomic saves) and
`intelligence/retrieval.py` (whole-phrase lookup, the "Campus directory" section
the system prompt refers to by name, and template answers used when no model
host answers), edited on the panel's Data tab. Its samples are validated by the
tests, and a test holds the prompt and the section header together. Speech-to-text
and text-to-speech *are* built, and joined on the panel's Audio tab into a spoken
turn (transcript → `chat.ask` → sentence-streamed reply, in `neo_webapp/voice.py`),
but nothing gates recognition yet — the wake word is what will, and until then an
open mic channel is the listening window. Answering out loud is a separate switch,
off by default, because with it on the room's speech goes to the model host. The ROS *nodes* wrapping the Python
cores now bind real rclpy pub/sub/service I/O against the contracts in
`neo_msgs` — `nodes/*.py` in each package converts messages and calls into the
already-tested core, per its own wiring-plan docstring. They are written
against the frozen message definitions and pass the existing pure-Python
suite, but nothing in this repo has run them under a live ROS graph yet: there
is no rclpy on the machine this was written on, so `colcon build`/`colcon
test` on an actual Jazzy install is the verification still owed before they
are trusted the way the pure-Python cores are. Everything below describes the
design those must follow, including any wiring detail a real ROS run turns up
as wrong.

**The admin panel runs the real motion and emotion code**, not a stand-in: its
`MockBridge` drives `neo_motion`'s arbiter and driver and `neo_emotion`'s
controller, against a mock servo backend instead of a PCA9685. The simulated
part is exactly one thing — the backend that would write pulse widths to I2C —
so a bug found on the Head tab is a bug in the code that will drive the servos.
That also makes `neo_webapp` depend on both packages at import time; the repo
must be pip-installed (or they must be on `PYTHONPATH`) or the panel will not
start.

One deliberate difference from the robot: **in the panel, gaze is a bearing, not
a rate.** On the robot the camera rides the head, so rate control on image error
closes its own loop. The panel's camera is a webcam that does not move with the
simulated head, so that error never shrinks — integrated as a rate, it ran the
head to its 90° stop in five seconds on real frames and left it there. The panel
turns the head to face the person's bearing instead, follows only an *engaged*
person, and ignores perception results older than a second.

### Two install paths, both needed

They are split on purpose, and neither is going away:

```bash
pip install -e ".[dev,detector]"              # the six Python packages; one root setup.py
colcon build --base-paths src --symlink-install   # neo_msgs + neo_bringup only
```

**`--base-paths src` is required, not optional.** The root `setup.py` makes
colcon identify the repo root as a single Python package, so a bare
`colcon build` never descends into `src/`: it finds neither ROS package,
prints "0 packages finished", and exits 0. A green build that built nothing is
the failure mode to watch for here, because the colcon job is the only thing
that validates the message contracts as real IDL rather than as text.

Five of the six Python packages (`neo_motion`, `neo_perception`, `neo_emotion`,
`model_conn`, `intelligence`) now carry a per-package `setup.py` and
`console_scripts` entry points matching `neo_bringup/config/nodes.yaml`, and
no longer carry `COLCON_IGNORE` — that landed alongside the real node bindings
in each package's `nodes/*.py`, on the theory that a package with no way to
run its executable had nothing for `ros2 run` to find. `neo_webapp` still
carries `COLCON_IGNORE` and has no per-package `setup.py`: the admin panel is
launched as `neo --webapp up`, not via `ros2 run` (`neo_bringup`'s launch file
deliberately never starts it), so only its `bridge/ros.py` needs rclpy, as a
client of the topics/services the other five nodes now expose. **This has not
been proven against a real `colcon build`** — do that on a Jazzy machine
before trusting it; a `pip install`-only environment cannot rule out a wrong
`data_files` path or a typo'd entry point.

**Do not activate a pip venv and source ROS in the same shell to run tests.**
ROS Jazzy ships `launch_testing`, a pytest plugin built against pytest 7's hook
signatures, while the `[dev]` extra installs pytest 9. With both on the path,
*every* pytest invocation dies in plugin validation — including the ones colcon
drives, which turns `colcon test` into errors that look like test failures and
say nothing about the cause. Disabling the plugin does not help; a second ROS
plugin then fails on its missing hook.

Keep them apart, which costs nothing because neither side needs the other:

```bash
# pytest: venv only. No suite in this repo needs ROS, by design.
source ~/neo-venv/bin/activate && (cd src/neo_webapp && pytest -q)

# colcon: ROS only. Its system pytest 7 matches launch_testing.
source /opt/ros/jazzy/setup.bash && colcon build --base-paths src
```

CI does not hit this: the colcon job runs in a bare `ros:jazzy-ros-base`
container with no pip install, so only the system pytest is present.

### Testing

Tests run **per package, from inside its own directory** — the per-package
`tests/conftest.py` files collide otherwise:

```bash
(cd src/neo_perception && pytest -q)
(cd src/model-conn && pytest -q tests/test_link.py::test_probe_ordered_falls_through_to_wifi)
colcon test --packages-select neo_msgs && colcon test-result --verbose
```

`neo_msgs` and `neo_bringup` are testable both ways: their tests parse the
`.msg`/`.srv` and profile YAML as text, so they need no ROS install and run in
the pip-only CI job too. `neo_msgs/test/test_contract.py` is the guard against
the frozen contracts drifting from the Python mirror dataclasses in
`neo_webapp.bridge.types` and `neo_perception.types` — nothing else in the repo
would notice if they diverged.

Security setup (the private CA, the three certificates, installing the CA on a
phone) is in [docs/security.md](docs/security.md).

Pi provisioning is `scripts/pi/` with [docs/pi-setup.md](docs/pi-setup.md).
Ubuntu Server **24.04** only — the system script refuses anything else, because
Jazzy is not packaged for other releases. The root script (`setup-system.sh`)
and the user script (`setup-user.sh`) are split along the same line as the
venv/ROS rule above, and no script handles a password, Wi-Fi key or
certificate: those stay manual steps in the doc. The robot's hostname is
`neo-pi`, reached as `neo-pi.local` once avahi is installed. Files under
`scripts/pi/` must keep LF line endings (`.gitattributes` enforces it): bash,
systemd and netplan on the Pi all fail on CRLF.

The build order, per-phase acceptance criteria, and the reasoning behind the
phase ordering live in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).
Follow that ordering unless told otherwise, and keep it updated as phases land.

## Project: Neo

Neo is a receptionist robot for a college campus, running on a Raspberry Pi 4 (4GB). Two roles:

1. **Info desk** — answers questions about the campus (e.g. "where is classroom CS-204?", "how do I get to B block?") from college classroom/location data.
2. **General AI assistant** — conversational answers outside the info-desk domain.

It is head-only for now (pan/tilt), wake-word gated, and has a web app that is both the robot's admin panel and a stand-in for its physical I/O during development.

## Architecture (intended)

- **ROS 2 Jazzy** is the integration layer. Perception, LLM, motion, and audio are separate nodes communicating over topics/services, so any of them can be swapped for a simulated/web-app source without the others knowing.
- **LLM: Qwen 2.5 3B, off-board.** The model does not run on the Pi. The Pi talks to an RTX laptop over a link with two paths (Ethernet preferred, Wi-Fi failover); that host serves inference. The transport is HTTP secured with mutual TLS — an unauthenticated inference endpoint on a campus network is an open proxy, so never run one, even briefly for testing.
- **The off-board host is the owner's daily-driver laptop, so it is usually unavailable.** Degraded mode is the normal operating state, not an error path. Two consequences that are invariants, not optimizations: ASR runs on the Pi (off-board Whisper is only an accuracy upgrade when the link is up), and the info-desk role works end to end with no LLM at all — templates are its primary output path, not a fallback.
- **Vectorless RAG** for the college classroom data. No embedding store/vector DB: retrieval is over the structured classroom/location dataset directly (lookup/filter/prompt-stuffing). Do not introduce a vector database to "fix" retrieval without discussing it.
- **Campus data arrives incrementally** and is edited through the admin panel, not in code. The system must be correct at ten rooms and at five hundred: retrieval is coverage-aware, distinguishing "that room isn't in my directory" from "I haven't learned that block yet", and never inventing a room.
- **YOLO detection** for person and object, running on the Pi camera feed. This drives both presence ("someone approached the desk") and the head's gaze target. The continuous pass is a **pose** model (`yolov8n-pose`), not a plain detector: one inference gives person boxes, the COCO-17 keypoints that gestures are derived from, and a face-anchored gaze target. A second network for hands would roughly double per-frame cost on a Pi that only has ~4 fps to spend. Object detection is a separate model and runs **on demand**, not continuously.
- **Track the head, not the whole person.** People stand close at a reception desk, so the person box is usually clipped by the frame and its centre sits on a torso filling the view; the head stays whole and is what a pan/tilt assembly aims at. The head box is derived from the COCO-17 nose/eyes/ears that the pose pass already produced, so it costs **no extra inference** — measured close up, face keypoints stay 4-5 of 5 visible while body keypoints fall to 2 of 8, and detection confidence actually *rises* (0.85 → 0.93). Every tracked box must be a head, including the fallbacks: salience is "largest box wins", so a single person box left among head boxes is several times their area and would always win. Head boxes are ~4× smaller, so the tracker's motion gate is scaled for them (`center_gate_scale`), not for bodies. `track_head=False` restores person tracking for a wide shot.
- **Gaze engagement is a lock, not a follow.** Presence alone does not move the head; a person who shows a palm (hand up, forearm vertical) and holds it does. The lock survives dropped frames and occlusion, and releases itself on three distinct signals with three different timings — walked out of frame (short grace), the detector blinked (long grace), and turned their back (medium). Collapsing those into one timeout makes the robot either stare at doorways or drop its lock every missed frame. Facing is derived from keypoints (COCO labels sides from the person's own frame, so shoulder parity inverts when they turn around) — no extra model. Only static gestures are reliable at Pi frame rates; a wave aliases at 4 fps. COCO-17 has no finger keypoints, so "palm" is a pose, not a hand shape. When a palm will not register, `GestureRecognizer.explain` names which link of the test broke (shown on the Vision tab, written to the vision status file). It mirrors `_classify` step for step and a fuzz test fails if they ever disagree, so change the two together.
- **Object identification is request-driven.** "What is this?" runs a second model once, on one frame, and ranks by `centrality² × √area` so the answer is the thing being held up rather than the largest thing in shot. Never run it continuously — it roughly doubles per-frame cost.
- **Motion:** two servos, X (pan) and Y (tilt), moving the head/camera, powered from a separate 5–6 V supply (a buck converter) with a common ground. **Their signal wires go straight to the Pi's two *hardware* PWM pins, GPIO18 (pin 12) and GPIO19 (pin 35)**, enabled by `dtoverlay=pwm-2chan` and driven through `/sys/class/pwm` (`RpiPwmBackend`, `driver: rpi-pwm`). **Never software PWM on an ordinary GPIO pin**: it is timed by a CPU that YOLO keeps busy, and every late edge is a visible twitch — the reason the original design used a PCA9685, which stays supported as `driver: pca9685` (registers written with `smbus2`, not Adafruit's CircuitPython stack: on Ubuntu for the Pi, `import board` and `import adafruit_pca9685` both demand a GPIO library, measured). The hardware PWM block also makes the 3.5 mm jack's analog audio, so that jack is off and the speaker must be USB; `setup-system.sh --servo-pwm` sets all of this up, including a `pwm` group and udev rule so the driver needs no root. A PCA9685 at 50 Hz resolves ~0.44° per step; with hardware PWM the pulse is set in nanoseconds and the servo's own deadband is the floor. **The robot's servos are continuous-rotation (360°) TowerPro MG995s** (`model: mg995-360`), kept by the owner's decision with no position sensor: the pulse sets speed, not angle. `RpiPwmContinuousBackend` makes them act like position servos by dead reckoning — each `ContinuousAxis` turns the driver's target angle into a speed through a measured model (neutral pulse, stop band, and the speed at a known offset **measured separately above and below neutral** — the pan servo turns 58.5 °/s one way and 81 °/s the other at the same offset, and one averaged speed would walk the estimate ~10° per back-and-forth) and integrates the speed it commanded. Speed belongs to the side of neutral the *pulse* is on, so `inverted` never swaps which measured speed applies. The head angle is therefore an **estimate that drifts**, starts at 0 wherever the head points when the backend is created, and the backend **refuses to drive an uncalibrated servo** because every angle would be a guess. Invariants for these servos: **the safe state is neutral (stop), not "hold the last pulse"** — the driver's hold-by-rewriting-the-pose arrives as zero error and so as neutral, which covers its watchdog and e-stop; **a stall watchdog sends neutral after 150 ms without a write**, because the kernel keeps the last pulse running; an exit handler stops the outputs, but a killed process leaves the last pulse running until `neo-servo-check --stop`; and `neo-servo-check --keep` with any pulse but neutral is refused. The driver's soft limits are narrowed to the estimate's angle limits and to the servo's measured top speed, so its pose cannot run ahead of the estimate. `config/motion.yaml` (+ `motion.local.yaml` on the robot) holds the servo model, wiring and per-axis calibration for either kind of servo, loaded by `neo_motion.config.MotionConfig`, which rejects partial or unknown settings. For positional (180°) MG995s (`model: mg995`), specified for 50 Hz only, **an uncalibrated axis is held to 1200–1800 µs, labelled ±27°**: the MG995's published pulse ranges disagree (0.5–2.5 ms vs 1–2 ms for 180°), and that window is clear of the end stops under both. The label is exact only under the first reading (under the second the head turns ~±54°); no single label is right under both, and calibration replaces it. The driver's soft limits are narrowed to the calibration, so it never reports a pose the servo was clamped short of. Wiring and calibration live in [docs/hardware.md](docs/hardware.md). Two control paths feed the same servo node — the emotion system and a manual joystick. The joystick is **virtual, in the web app**, so it is network-mediated: it needs a staleness deadman, and a dropped connection must stop the head rather than leave it driving.
- **Emotion layer:** an emotional state modulates head movement (idle motion, gaze, gesture style). It is a movement modifier, not a separate actuator path.
- **Wake word "NEO":** Neo is silent and non-responsive until the wake phrase fires. Any audio/dialogue work must respect this gate — the wake-word detector is upstream of ASR and the LLM, not a filter applied afterwards.
- **Spoken turns are half-duplex.** While Neo's voice is playing, mic audio still reaches the meter but not the recognizer, or it transcribes its own reply and answers itself — measured live, Vosk hears the reply near-perfectly if ungated. The gate must model *browser playback*, a cursor advanced the way `app.js` advances its own, not the server's push: synthesis runs ~20× real time, so pushing ends seconds before listening does. One turn at a time and no queue — a transcript arriving mid-turn is dropped.
- **Speech reaches the speaker with backpressure.** Use `Bridge.push_audio_out`, never `emit_audio_out`, for anything longer than a tone: the latter drops on a full 32-chunk queue and, called in a loop that never yields, cut every reply off at exactly 1.49 s with a 200 response. Nothing is queued when no speaker is connected, and the queue is cleared when the last one leaves — otherwise the next listener hears a stale fragment of an old sentence. And the speaker handler must watch for its browser leaving: it only sends, so blocked on an empty queue it never sees a disconnect — it held a phantom "connected" channel the voice loop kept talking to, and uvicorn's graceful shutdown waited on it forever. TestClient hides this, because it cancels the handler when the socket context exits; `test_speaker_disconnect.py` drives the handler with a fake socket for that reason.

## Source selection (important cross-cutting concern)

Camera, microphone, and speaker each have two possible backends:

- **hardware** — the Pi Camera Module (CSI), a USB mic, and the attached speaker
- **webapp** — the browser device's camera/mic/speaker, via the admin panel

This choice must be runtime-configurable per stream, not a build-time flag or a fork of the node. Node logic downstream of a source must not care which backend is active.

Note that browsers only grant camera and microphone access in a secure context, so the admin panel **must** be served over HTTPS for the webapp backends to work from any device other than the Pi itself. This makes TLS a functional requirement, not just a hardening step.

## Config: two layers, merged

Every package reads `config/<name>.yaml` (committed defaults, no secrets) with
`config/<name>.local.yaml` (gitignored, per-machine) **merged over it**, key by
key and into nested sections. A local file naming one key must leave every other
committed value standing.

This is worth stating because it was wrong for a long time and the breakage was
invisible: the local file *replaced* the committed one, so a `webapp.local.yaml`
holding just the password hash switched off all of `webapp.yaml`, which then
fell back to dataclass defaults. Those defaults mostly agree with the shipped
YAML, so nothing looked wrong until one of them did not — and then the committed
file that plainly said otherwise turned out to be inert.

Two rules follow. **An explicit `--config` path, or the `$NEO_*_CONFIG` env var,
names exactly one file** and nothing is layered over it. And **the loader must
not restate a dataclass default** — `chat.timeout_s` drifted to 8.0 in the
loader while the dataclass said 60.0, and the loader won.

**Relative paths in config resolve against the repo root, never the working
directory** (`intelligence.config.resolve_path`). The same file is read by the
panel in WSL and `neo --prompt` on Windows, each launched from anywhere; model
paths used to be CWD-relative and reported "not found" from everywhere but the
repo root.

## Cross-process state: the `var/` status files

`neo --webapp up`, `neo --model ... up` and `neo --prompt` are separate processes,
started in any order, any of which may be absent. They share state through
atomically-replaced JSON files under `var/` — one per producing package
(`model_conn` link, `intelligence` dialog, `neo_perception` vision), each owning
its own schema.

This is deliberate, not a stopgap. A missing or stale file is a state every reader
must handle anyway; a socket that is not listening yet is not. Readers must treat
a stale file as "that process is not running" rather than as current state — hence
the freshness check, distinct from availability.

It is what lets `neo --prompt "what is this"` answer from a camera owned by a
different process, and what lets the panel show whether `neo --model … up` is
serving without being on that machine's terminal.

Two rules for writers: **heartbeat, don't write once** (a one-shot file keeps
claiming a process is alive after it is killed, and killed is how foreground
commands normally end), and **mark a clean shutdown explicitly** so it is visible
immediately rather than after the staleness timeout.

The panel is a *client* of these processes, not only an observer: `POST
/api/dialog/ask` goes through `intelligence.chat.ask`, the same path as `neo
--prompt`, so endpoint resolution, mTLS, the degraded reply and status-file
writing cannot drift between the two surfaces.

## Web app = admin panel

The web app manages the whole robot: source selection, head control, vision and audio tuning, dialog monitoring, campus data editing, config, health, and logs. Two things follow:

- It is **split into an always-on light API and an on-demand media bridge**. The camera/mic/speaker WebSocket bridges are the expensive part and must not run when nobody is using them — the Pi has 4 cores and YOLO wants most of them.
- It can move servos and rewrite campus data, so it is behind authentication over HTTPS from the first commit that exposes it.

## Future scope (do not build unless asked)

Full-body locomotion. Currently only the head moves; keep the motion interface general enough that adding a base is not a rewrite, but do not implement drive/navigation now.
