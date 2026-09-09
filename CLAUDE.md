# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Six packages under `src/` (440 tests). Four are pure Python: `neo_webapp`
(admin panel), `neo_perception` (detection/gestures/engagement), `model_conn`
(the `neo` CLI and off-board link), and `intelligence` (prompts, chat, ASR/TTS).
Two are ROS: `neo_msgs` (the frozen interface contracts) and `neo_bringup`
(launch profiles). [README.md](README.md) tracks what each one currently does.

Not built: motion (the servo driver), the wake word, the vectorless campus-data
engine, and the emotion layer. Speech-to-text and text-to-speech *are* built and
wired to the panel's Audio tab, but nothing gates them yet — the wake word is
what will, and until then an open mic channel is the listening window. The ROS *nodes* wrapping the Python cores are
also not built — `nodes/*.py` in each package documents its wiring and raises
`NotImplementedError`. Everything below describes the design those must follow,
including the parts not yet written.

### Two install paths, both needed

They are split on purpose, and neither is going away:

```bash
pip install -e ".[dev,detector]"    # the four Python packages; one root setup.py
colcon build --symlink-install      # neo_msgs + neo_bringup only
```

The four Python packages carry a `COLCON_IGNORE`: they declare
`build_type: ament_python` but deliberately have no per-package `setup.py`, so
colcon would fail on them and take `neo_msgs` down with it. That is enough to
run nodes — rclpy finds `neo_msgs` from the sourced overlay and the packages
from the Python path — but *not* enough for `ros2 run` to discover their
executables. The phase that first needs `ros2 run` for a package removes its
`COLCON_IGNORE` and adds a `setup.py`; not before.

### Testing

Tests run **per package, from inside its own directory** — the four
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
- **Gaze engagement is a lock, not a follow.** Presence alone does not move the head; a person who shows a palm (hand up, forearm vertical) and holds it does. The lock survives dropped frames and occlusion, and releases itself on three distinct signals with three different timings — walked out of frame (short grace), the detector blinked (long grace), and turned their back (medium). Collapsing those into one timeout makes the robot either stare at doorways or drop its lock every missed frame. Facing is derived from keypoints (COCO labels sides from the person's own frame, so shoulder parity inverts when they turn around) — no extra model. Only static gestures are reliable at Pi frame rates; a wave aliases at 4 fps. COCO-17 has no finger keypoints, so "palm" is a pose, not a hand shape.
- **Object identification is request-driven.** "What is this?" runs a second model once, on one frame, and ranks by `centrality² × √area` so the answer is the thing being held up rather than the largest thing in shot. Never run it continuously — it roughly doubles per-frame cost.
- **Motion:** two servos, X (pan) and Y (tilt), moving the head/camera, driven over I2C via a PCA9685 (not Pi GPIO PWM) from a separate 5V supply. Two control paths feed the same servo node — the emotion system and a manual joystick. The joystick is **virtual, in the web app**, so it is network-mediated: it needs a staleness deadman, and a dropped connection must stop the head rather than leave it driving.
- **Emotion layer:** an emotional state modulates head movement (idle motion, gaze, gesture style). It is a movement modifier, not a separate actuator path.
- **Wake word "NEO":** Neo is silent and non-responsive until the wake phrase fires. Any audio/dialogue work must respect this gate — the wake-word detector is upstream of ASR and the LLM, not a filter applied afterwards.

## Source selection (important cross-cutting concern)

Camera, microphone, and speaker each have two possible backends:

- **hardware** — the Pi Camera Module (CSI), a USB mic, and the attached speaker
- **webapp** — the browser device's camera/mic/speaker, via the admin panel

This choice must be runtime-configurable per stream, not a build-time flag or a fork of the node. Node logic downstream of a source must not care which backend is active.

Note that browsers only grant camera and microphone access in a secure context, so the admin panel **must** be served over HTTPS for the webapp backends to work from any device other than the Pi itself. This makes TLS a functional requirement, not just a hardening step.

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
