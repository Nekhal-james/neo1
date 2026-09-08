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

Four packages under `src/` are implemented and tested; the rest of the plan
(motion, wake word, the vectorless campus-data engine) has not been started.

| Package | Implements | Status |
|---|---|---|
| [`neo_webapp`](src/neo_webapp/README.md) | Phase 2 — admin panel (API, media bridge, operator UI) | Sources/System/Head, Link, and Dialog state live; rest are stubs filled in by later phases |
| [`neo_perception`](src/neo_perception/README.md) | Phase 4 — detection, gestures, gaze engagement | Pure-Python pipeline + ROS node wrapper; runnable standalone or driven from the webapp's Vision tab |
| [`model_conn`](src/model-conn/README.md) | Phase 6 (partial) — the off-board LLM host/receiver link | CLI for serving the model (host) and checking the link (receiver); real mTLS via a private CA (`neo --tls init`) |
| [`intelligence`](src/intelligence/README.md) | Phases 5/6/7 (partial) — prompts, RAG data, chat/ASR/TTS | Chat works end to end via `model_conn`'s link; local Vosk STT and Piper TTS wired up; RAG data folder and system prompt are placeholders |

All four run with no Pi, no ROS, and no servos attached — `neo_webapp` via a
`MockBridge` that simulates the robot, `neo_perception` against any camera
the panel is using, `model_conn`/`intelligence` against whatever endpoints
you point them at.

## Repo layout

```
CLAUDE.md                    design invariants (read this first)
docs/IMPLEMENTATION_PLAN.md  build order and acceptance criteria
setup.py                     installs all of src/ together for local dev (see below)
config/webapp.yaml           committed defaults for the admin panel
config/model_conn.yaml       committed defaults for the model connection
config/intelligence.yaml     committed defaults for prompts/chat/asr/tts
src/
  neo_webapp/                admin panel: API + media bridge + operator UI
  neo_perception/            YOLO pose pipeline, gestures, gaze/engagement
  model-conn/                neo CLI: off-board LLM host serving + link checks
  intelligence/              prompts, RAG data, chat/ASR/TTS
```

Gitignored, machine-local, never committed: `config/*.local.yaml` (secrets
and per-machine overrides — password hashes, real endpoint IPs, model
paths), `certs/` (TLS certs, including the private CA from `neo --tls init`),
`models/` (`.gguf`/`.onnx` weights), and `var/` (the JSON status files
`neo_webapp` polls).

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

The `dev` extras give you testing tools and install `neo_webapp` properly so
the `neo` CLI can find it.

To run the admin panel against a simulated robot:

```bash
neo --webapp devcert
neo --webapp setup
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

To test the assistant directly, e.g. on the Pi:

```bash
neo --prompt "where is CS-204"
```

This routes through `model_conn`'s link the same way `--connection:status`
does; with no reachable host it prints a degraded-mode message instead of
crashing. For local speech I/O (`pip install -e ".[dev,voice]"` to get Vosk
and Piper), see [src/intelligence/README.md](src/intelligence/README.md).

Set up mutual TLS for the off-board link once (`neo --tls init` generates a
private CA plus a server cert for the host and a client cert for the Pi —
see [src/model-conn/README.md](src/model-conn/README.md)), then set
`tls.enabled: true` in `config/model_conn.local.yaml` on both machines.

See [src/neo_webapp/README.md](src/neo_webapp/README.md),
[src/neo_perception/README.md](src/neo_perception/README.md),
[src/model-conn/README.md](src/model-conn/README.md), and
[src/intelligence/README.md](src/intelligence/README.md) for details,
including why TLS is required for the panel, the source-backend seam, the
joystick deadman, and why the RAG data folder and system prompt are still
placeholders.

## Not built yet

Motion (servo driver), wake word, off-board Whisper (ASR upgrade when the
link is healthy), the vectorless campus-data engine (RAG data folder exists,
retrieval logic doesn't), and the emotion layer — see the plan for ordering.
Full-body locomotion is out of
scope until asked for.
