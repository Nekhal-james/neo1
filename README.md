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

Three packages under `src/` are implemented and tested; the rest of the plan
(motion, ASR, the campus data engine) has not been started.

| Package | Implements | Status |
|---|---|---|
| [`neo_webapp`](src/neo_webapp/README.md) | Phase 2 — admin panel (API, media bridge, operator UI) | Sources/System/Head tabs live; rest are stubs filled in by later phases |
| [`neo_perception`](src/neo_perception/README.md) | Phase 4 — detection, gestures, gaze engagement | Pure-Python pipeline + ROS node wrapper; runnable standalone or driven from the webapp's Vision tab |
| [`model_conn`](src/model-conn/README.md) | Phase 6 (partial) — the off-board LLM host/receiver link | CLI for serving the model (host) and checking the link (receiver); mTLS is stubbed, not implemented |

All three run with no Pi, no ROS, and no servos attached — `neo_webapp` via a
`MockBridge` that simulates the robot, `neo_perception` against any camera
the panel is using, `model_conn` against whatever endpoints you point it at.

## Repo layout

```
CLAUDE.md                    design invariants (read this first)
docs/IMPLEMENTATION_PLAN.md  build order and acceptance criteria
pyproject.toml               installs all of src/ together for local dev (see below)
config/webapp.yaml           committed defaults for the admin panel
config/model_conn.yaml       committed defaults for the model connection
src/
  neo_webapp/                admin panel: API + media bridge + operator UI
  neo_perception/            YOLO pose pipeline, gestures, gaze/engagement
  model-conn/                neo CLI: off-board LLM host serving + link checks
```

There is exactly one way to `pip install` this repo: from the root, below.
Packages under `src/` keep their own `package.xml` (ROS metadata for a future
colcon build) but have no `setup.py` of their own, so there's no independent
per-package install path to fall out of sync with the root one.

## Getting started

From the repo root, install everything needed for local development in one
shot:

```bash
pip install -e ".[dev]"
```

This installs `neo_webapp`, `neo_perception`, and `model_conn` together (no
ROS toolchain required) and registers their console scripts: `neo`,
`neo-webapp-setup`, `neo-webapp-devcert`, `neo-perception-bench`.

To run the admin panel against a simulated robot:

```bash
neo-webapp-devcert
neo-webapp-setup
neo --webapp up          # https://localhost:8443
```

`neo --webapp up` is a thin forwarder into `neo_webapp`'s own CLI — flags like
`--host`, `--port`, `--backend`, and `--no-tls` all pass straight through
(`neo --webapp up --no-tls --port 8500`).

To connect to the off-board Qwen 2.5 3B host:

```bash
neo --model /path/to/qwen2.5-3b-instruct-q4_k_m.gguf up   # on the laptop (host)
neo --connection:status                                    # on the Pi (receiver)
neo --connection:ping
```

See [src/neo_webapp/README.md](src/neo_webapp/README.md),
[src/neo_perception/README.md](src/neo_perception/README.md), and
[src/model-conn/README.md](src/model-conn/README.md) for details, including
why TLS is required for the panel, the source-backend seam, the joystick
deadman, and why `model_conn`'s mTLS is currently stubbed with a loud warning
rather than silently skipped.

## Not built yet

Motion (servo driver), on-Pi ASR + wake word, real mTLS for the off-board
link, the vectorless campus-data engine, and the emotion layer — see the plan
for ordering. Full-body locomotion is out of scope until asked for.
