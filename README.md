# Neo

A receptionist robot for a college campus, built on ROS 2 Jazzy. Answers
campus-location questions from a structured dataset (no LLM required for
that path) and, when an off-board host is reachable, holds general
conversation via Qwen 2.5 3B. Head-only (pan/tilt) for now, wake-word gated,
running on a Raspberry Pi 4.

Design invariants and architecture live in [CLAUDE.md](CLAUDE.md); the
phase-by-phase build order and acceptance criteria live in
[docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md). This file just
tracks what currently exists.

## Status

Two ROS 2 packages under `src/` are implemented and tested; the rest of the
plan (motion, ASR/LLM client, campus data engine) has not been started.

| Package | Implements | Status |
|---|---|---|
| [`neo_webapp`](src/neo_webapp/README.md) | Phase 2 — admin panel (API, media bridge, operator UI) | Sources/System/Head tabs live; rest are stubs filled in by later phases |
| [`neo_perception`](src/neo_perception/README.md) | Phase 4 — detection, gestures, gaze engagement | Pure-Python pipeline + ROS node wrapper; runnable standalone or driven from the webapp's Vision tab |

Both packages run with no Pi, no ROS, and no servos attached — `neo_webapp`
via a `MockBridge` that simulates the robot, `neo_perception` against any
camera the panel is using.

## Repo layout

```
CLAUDE.md                    design invariants (read this first)
docs/IMPLEMENTATION_PLAN.md  build order and acceptance criteria
config/webapp.yaml           committed defaults for the admin panel
src/
  neo_webapp/                admin panel: API + media bridge + operator UI
  neo_perception/            YOLO pose pipeline, gestures, gaze/engagement
```

## Getting started

Each package has its own quick start. To run the admin panel against a
simulated robot:

```bash
cd src/neo_webapp
pip install -e ".[dev]"
neo-webapp-devcert
neo-webapp-setup
neo-webapp          # https://localhost:8443
```

See [src/neo_webapp/README.md](src/neo_webapp/README.md) and
[src/neo_perception/README.md](src/neo_perception/README.md) for details,
including why TLS is required, the source-backend seam, and the joystick
deadman.

## Not built yet

Motion (servo driver), on-Pi ASR + wake word, the off-board LLM client
(mTLS to the Qwen 2.5 3B host), the vectorless campus-data engine, and the
emotion layer — see the plan for ordering. Full-body locomotion is out of
scope until asked for.
