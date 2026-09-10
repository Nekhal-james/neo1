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

Eight packages under `src/` are implemented and tested; the rest of the plan
(the wake word, the vectorless campus-data engine) has not been started.

| Package | Implements | Status |
|---|---|---|
| [`neo_webapp`](src/neo_webapp/README.md) | Phase 2 — admin panel (API, media bridge, operator UI) | Sources, System, Head, Vision, Dialog, and Audio tabs live; Data/Emotion/Logs are stubs filled in by later phases |
| [`neo_perception`](src/neo_perception/README.md) | Phase 4 — detection, gestures, gaze engagement | Pure-Python pipeline + ROS node wrapper; palm-gesture engagement, facing-based release, on-demand object identification |
| [`neo_motion`](src/neo_motion/README.md) | Phase 3 — head arbitration and the servo driver | Priority arbiter with crossfade and source expiry; driver enforcing clamp/slew/deadband/watchdog/e-stop against a mock or PCA9685 backend |
| [`neo_emotion`](src/neo_emotion/README.md) | Phase 9 — mood and the movement it shapes | Event-driven state machine with dwell times; idle drift, micro-motion, and bounded gesture overlays. Publishes parameters only — no path to the servos |
| [`model_conn`](src/model-conn/README.md) | Phase 6 (partial) — the off-board LLM host/receiver link | CLI for serving the model (host) and checking the link (receiver); real mTLS via a private CA (`neo --tls init`) |
| [`intelligence`](src/intelligence/README.md) | Phases 5/6/7 (partial) — prompts, RAG data, chat/ASR/TTS | Chat works end to end via `model_conn`'s link; streaming Vosk STT and sentence-streamed Piper TTS, joined into a spoken conversation on the panel's Audio tab; RAG data folder and system prompt are placeholders |
| [`neo_msgs`](src/neo_msgs/README.md) | Phase 1 — the frozen message/service contracts | All 15 interfaces defined; a contract test guards them against drifting from the Python mirrors the panel runs on |
| [`neo_bringup`](src/neo_bringup/README.md) | Phase 1 — launch profiles | `dev`, `hardware`, `hybrid`, `bench`; the node registry lists every node the robot will run and which phase makes it real |

All of them run with no Pi, no ROS, and no servos attached — `neo_webapp` via a
`MockBridge` that drives the real `neo_motion` and `neo_emotion` code against a
mock servo backend, `neo_perception` against any camera the panel is using,
`model_conn`/`intelligence` against whatever endpoints you point them at.

641 tests. Run each package from inside its own directory
(`cd src/neo_webapp && python -m pytest -q`) — the per-package `tests/conftest.py`
files collide if you point pytest at `src/` as a whole. `neo_msgs` and
`neo_bringup` are tested the same way with plain pytest: their tests read the
`.msg`/`.srv` and profile YAML as text, so no ROS install is needed for them
either.

Nothing in the suite needs a Pi, a camera, a microphone, ROS, or a downloaded
model — the heavy dependencies are all optional extras, and the tests that would
need them skip with a reason rather than failing. CI
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs that same suite
per package, a bug-level `ruff` pass, and a real `colcon build` on Jazzy, which
is what validates the message contracts as IDL rather than as text.

### What the robot can currently do

- **See you.** Person detection and tracking from a single `yolov8n-pose` pass,
  at the Pi's ~4 fps working point.
- **Wait to be asked.** Presence alone does not move the head. Show a palm and
  hold it for about half a second and it locks on; it follows you, survives the
  detector dropping a frame, and releases itself when you leave the frame or
  turn your back — three causes, three different timings.
- **Name what you hold up.** "What is this?" runs a second model once, on one
  frame, ranked so the answer is the thing being presented rather than the
  furniture behind it.
- **Answer questions**, through the panel or `neo --prompt`, with the model when
  one is reachable and a degraded reply when not — and with one line of camera
  context attached, which is what makes "what am I holding" answerable at all.
- **Hold a spoken conversation.** Press *Start talking* on the Audio tab, ask a
  question, and pause: Vosk transcribes it live, the model answers, and Piper
  speaks the reply a sentence at a time, so Neo starts talking before the whole
  answer is synthesized. While it talks the mic stays open but is not
  transcribed — otherwise it hears its own reply and answers itself. Recognition
  and synthesis both run locally; only the answer needs the laptop, and without
  it Neo speaks the degraded reply instead. Downloading a Vosk model and a Piper
  voice is the only setup — until then the Audio tab says exactly which path is
  missing rather than failing silently.

Not yet: it cannot wake to its own name or answer a campus question from real
data. The motion stack is complete and the panel drives it, but against a mock
servo backend — no PCA9685 has been wired up yet. Speech works, but it is not yet *gated* by the wake
word — on the panel, opening the mic channel is what opens the listening
window. See *Not built yet* below.

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
  neo_msgs/                  frozen message + service contracts (ROS, no logic)
  neo_bringup/               launch profiles and the node registry (ROS)
docs/security.md             the CA, the three certificates, installing them
.github/workflows/ci.yml     pytest + lint + a colcon build on Jazzy
```

There is exactly one way to `pip install` this repo: from the root, below. The
six Python packages keep their own `package.xml` but have no `setup.py` of
their own, so there's no independent per-package install path to fall out of
sync with the root one. They carry a `COLCON_IGNORE` for the same reason —
`colcon build` covers `neo_msgs` and `neo_bringup`, pip covers the rest, and
CLAUDE.md explains what that split does and does not buy.

## Getting started

From the repo root, install everything needed for local development in one
shot:

```bash
pip install -e ".[dev]"
```

The `dev` extras give you testing tools and install `neo_webapp` properly so
the `neo` CLI can find it.

On a machine with ROS 2 Jazzy, build the two ROS packages alongside it:

```bash
colcon build --base-paths src --symlink-install
. install/setup.bash
ros2 interface show neo_msgs/msg/HeadCommand    # the frozen contract
ros2 launch neo_bringup neo.launch.py profile:=dev
```

`--base-paths src` is required: the root `setup.py` makes colcon treat the repo
root as one Python package, so without it colcon never looks inside `src/` and
cheerfully reports "0 packages finished" with exit code 0. See CLAUDE.md.

The launch file skips any node whose package has not been built yet, with a log
line saying which plan phase makes it real — so it is usable now rather than
only once the project is finished.

This works on WSL too (Ubuntu 24.04 is Jazzy's tier-1 platform), building
straight against the Windows checkout at `/mnt/c/...`. Two notes if you set that
up: make the venv with `--system-site-packages` so `rclpy` stays visible, and
install torch from the CPU index (`--index-url https://download.pytorch.org/whl/cpu`)
— the default Linux wheel drags in ~2.5 GB of CUDA packages that nothing here
uses, since perception targets the Pi's CPU.

Do not source ROS and activate the venv in the same shell to run tests; see the
note in CLAUDE.md about `launch_testing` and pytest versions.

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
crashing. For local speech I/O, install the voice extra and download a model:

```bash
pip install -e ".[dev,voice]"
# then set asr.vosk_model_path and tts.piper_model_path in
# config/intelligence.local.yaml -- relative paths resolve against the repo
# root, e.g. models/vosk-model-small-en-in-0.4 -- and press "Start talking"
# on the panel's Audio tab
```

Vosk models come from [alphacephei.com/vosk/models](https://alphacephei.com/vosk/models)
(`vosk-model-small-en-in` for Indian English) and Piper voices from
[the piper-voices repo](https://huggingface.co/rhasspy/piper-voices). Neither is
committed — see [src/intelligence/README.md](src/intelligence/README.md).

The admin panel is a client of that same path, not just an observer of it. With
`neo --model … up` running, the panel's **Dialog** tab shows what is being
served (model name, endpoint, mTLS or plain) and lets you ask it questions
directly — the reply, its source, and the latency all come back in the browser,
and the shared status file updates either way round.

It also catches a mismatch that is otherwise silent: `chat.model_name` in
`config/intelligence.yaml` has to match the name the host registered in Ollama.
Nothing enforces that, and getting it wrong produces a degraded reply with no
visible cause, so the panel says so and tells you the name to set.

Set up mutual TLS for the off-board link once (`neo --tls init` generates a
private CA plus a server cert for the host and a client cert for the Pi), then
set `tls.enabled: true` in `config/model_conn.local.yaml` on both machines. The
admin panel takes its certificate from that same CA (`neo --tls panel`), so
installing the CA once on your phone makes both the panel and the link trusted —
which matters because a browser will not grant the camera or microphone at all
without it. Full procedure in [docs/security.md](docs/security.md).

See [src/neo_webapp/README.md](src/neo_webapp/README.md),
[src/neo_perception/README.md](src/neo_perception/README.md),
[src/model-conn/README.md](src/model-conn/README.md), and
[src/intelligence/README.md](src/intelligence/README.md) for details,
including why TLS is required for the panel, the source-backend seam, the
joystick deadman, and why the RAG data folder and system prompt are still
placeholders.

## Not built yet

- **The wake word** — which is also the gate speech is missing. Recognition
  runs today whenever the panel's mic channel is open; `wake_word` is what will
  own that window on the robot.
- **Off-board Whisper**, the ASR accuracy upgrade for when the link is healthy.
  Vosk is the only engine, which is the right default — it is the one that works
  with the laptop closed.
- **Room-code accuracy.** The small Vosk model hears "CS-204" as "p s two hundred
  for". Plan 5.4's grammar-constrained recognition over the room list is the
  fix, and it needs the Phase 7 data to build the grammar from.
- **The vectorless campus-data engine** — the RAG data folder exists, the
  retrieval logic does not (plan Phase 7).
- **The ROS nodes** wrapping the existing Python cores. Each `nodes/*.py`
  documents its topics and raises `NotImplementedError`; the contracts they were
  waiting on now exist, so they are unblocked.

Full-body locomotion is out of scope until asked for.
