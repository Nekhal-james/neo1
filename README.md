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

Ten packages under `src/` are implemented and tested, and the robot runs as a
ROS graph from one command (`bash scripts/pi/neo-up.sh`). The campus knowledge
base is partly built (see below).

| Package | Implements | Status |
|---|---|---|
| [`neo_webapp`](src/neo_webapp/README.md) | Phase 2 — admin panel (API, media bridge, operator UI) | Sources, System, Head, Vision, Audio, Dialog and Data tabs live (Data edits the campus files, checks them, and tries questions against them); password change and recovery-code reset; the Emotion tab (mood, gesture buttons). Against the robot the panel drives its ROS graph: open the listening window, ask aloud, say something, stop talking, the robot camera, centre and e-stop. Logs is a stub |
| [`neo_perception`](src/neo_perception/README.md) | Phase 4 — detection, gestures, gaze engagement | Pure-Python pipeline + ROS node wrapper; palm-gesture engagement, facing-based release, on-demand object identification (served as `/perception/identify` for a spoken "what is this"); `camera_hw` publishes the USB webcam, choosing the V4L2 node that actually yields a frame |
| [`neo_motion`](src/neo_motion/README.md) | Phase 3 — head arbitration and the servo driver | Priority arbiter with crossfade and source expiry; driver enforcing clamp/slew/deadband/watchdog/e-stop against a mock backend, the Pi's hardware PWM (the robot's wiring: continuous-rotation MG995s on GPIO18/19, head angle dead-reckoned), or a PCA9685; per-axis calibration in `config/motion.yaml`, and continuous servos are not driven until it is measured |
| [`neo_emotion`](src/neo_emotion/README.md) | Phase 9 — mood and the movement it shapes | Event-driven state machine with dwell times; idle drift, micro-motion, and bounded gesture overlays. The node nods when someone walks up and tilts or shakes on entering a curious or confused mood, which `head_behavior` plays at gesture priority. Publishes parameters and gesture names only — no path to the servos |
| [`neo_audio`](src/neo_audio/README.md) | Phases 2/5 — mic, speaker, wake word, speech to text | The wake word from a Teachable Machine export run in numpy; an adaptive-floor endpointer; `mic_hw`/`speaker_hw` over `arecord`/`aplay`, surviving an unplugged USB device; `asr_router` (Vosk over the wake window, with pre-roll) and `tts`; `neo-audio-check` for levels, live wake scores and a spoken round trip |
| [`neo_sources`](src/neo_sources/README.md) | Phase 2 — source selection | `source_manager`: `/sources/set`, a latched `/sources/state` the hardware backends gate themselves on, and the mux that lets a browser's camera, mic and speaker stand in for the robot's |
| [`model_conn`](src/model-conn/README.md) | Phase 6 (partial) — the off-board LLM host/receiver link | CLI for serving the model (host) and checking the link (receiver); real mTLS via a private CA (`neo --tls init`) |
| [`intelligence`](src/intelligence/README.md) | Phases 5/6/7 (partial) — prompts, RAG data, chat/ASR/TTS | Chat works end to end via `model_conn`'s link; streaming Vosk STT and sentence-streamed Piper TTS, joined into a spoken conversation on the panel's Audio tab; Neo's system prompt; vectorless retrieval over `rooms`/`graph`/`coverage` YAML (whole-phrase matching on codes, names and aliases, spoken numbers folded in), sent to the model as only the matched entries and answered from templates when the model host is away; the `dialog` node runs the robot's turn (wake → listen → answer → speak) off its executor thread and sends "what is this" to the camera, not the model |
| [`neo_msgs`](src/neo_msgs/README.md) | Phase 1 — the frozen message/service contracts | All 15 interfaces defined; a contract test guards them against drifting from the Python mirrors the panel runs on |
| [`neo_bringup`](src/neo_bringup/README.md) | Phase 1 — launch profiles | `dev`, `hardware`, `hybrid`, `bench`; the node registry lists every node the robot will run and which phase makes it real |

All of them run with no Pi, no ROS, and no servos attached — `neo_webapp` via a
`MockBridge` that drives the real `neo_motion` and `neo_emotion` code against a
mock servo backend, `neo_perception` against any camera the panel is using,
`model_conn`/`intelligence` against whatever endpoints you point them at.

1,033 tests. Run each package from inside its own directory
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
- **Wake to its name.** Say "Neo" and the robot's own wake word — a Teachable
  Machine model, run without TensorFlow — opens a listening window; it
  transcribes the question, answers from the campus directory or the model host,
  and speaks through its own speaker. Nothing it hears before its name is
  transcribed or sent anywhere.
- **React with its head.** A nod when someone walks up; a tilt when curious, a
  shake when it cannot help — played over whatever the head is already doing,
  below the joystick, never under e-stop.
- **Be run from a browser.** The admin panel, connected to the running robot,
  shows the live wake score, what was heard and said, and the robot camera; it
  can open the listening window, ask a question aloud, make Neo say something,
  stop it, play a gesture, and swap any of camera, mic or speaker for the
  browser's own.

Not yet: it has no campus data to answer from — rooms are entered on the Data
tab. The whole graph has been driven end to end under ROS in WSL, but not yet on
the Pi with its USB webcam and microphone attached. See *Not built yet* below.

## Repo layout

```
CLAUDE.md                    design invariants (read this first)
docs/IMPLEMENTATION_PLAN.md  build order and acceptance criteria
setup.py                     installs all of src/ together for local dev (see below)
config/webapp.yaml           committed defaults for the admin panel
config/model_conn.yaml       committed defaults for the model connection
config/intelligence.yaml     committed defaults for prompts/chat/asr/tts
config/audio.yaml            committed defaults for the mic, speaker, wake word
config/motion.yaml           the servo model, wiring and calibration
scripts/pi/                  Pi provisioning, neo-up.sh / neo-panel.sh, systemd units
models/                      downloaded and trained models (gitignored)
src/
  neo_webapp/                admin panel: API + media bridge + operator UI
  neo_perception/            YOLO pose pipeline, gestures, gaze/engagement, camera_hw
  neo_motion/                head arbiter, servo driver, neo-servo-check
  neo_emotion/               mood, idle motion, gestures
  neo_audio/                 mic, speaker, wake word, asr_router, tts, neo-audio-check
  neo_sources/               source selection and the browser mux
  model-conn/                neo CLI: off-board LLM host serving + link checks
  intelligence/              prompts, RAG data, chat/ASR/TTS, the dialog node
  neo_msgs/                  frozen message + service contracts (ROS, no logic)
  neo_bringup/               launch profiles and the node registry (ROS)
docs/security.md             the CA, the three certificates, installing them
.github/workflows/ci.yml     pytest + lint + a colcon build on Jazzy
```

There is exactly one way to `pip install` this repo: from the root, below.
The ROS packages also carry a `setup.py` of their own, used only by
`colcon build` so `ros2 launch` can find their nodes; `neo_webapp` is the one
that does not, because the panel is not a launched node. CLAUDE.md explains the
split, and why running the robot needs the venv's packages without the venv
activated.

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

**Setting up the Raspberry Pi** is scripted: flash Ubuntu Server 24.04 with your
SSH key and Wi-Fi, run `scripts/pi/sync-to-pi.sh` from the laptop, then
`setup-system.sh` (with sudo) and `setup-user.sh` on the Pi. The full
walk-through, including the steps that stay manual because they need a password
or a certificate, is [docs/pi-setup.md](docs/pi-setup.md).

**Starting the robot**, on the Pi:

```bash
bash scripts/pi/neo-up.sh --check     # what would start, and whether each module imports
bash scripts/pi/neo-up.sh --panel     # the ROS graph ('hardware' profile) plus the admin panel
neo-audio-check --listen              # tune the wake word: live scores while you say "Neo"
```

To start both at every boot, install the services once:
`sudo bash scripts/pi/setup-system.sh --with-robot-service --with-panel-service`.
Use the scripts rather than `ros2 launch` and `neo --webapp up` directly: the
first makes the venv's speech packages visible to ROS's interpreter, the second
keeps the panel on the robot's DDS domain — without them the voice nodes fail to
import and the panel quietly runs its simulated robot.

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

## Command-line reference

Every argument every command-line entry point in this repo accepts, in one
place. Mutually exclusive flags are marked; everything else can be combined
freely with `--config`/`--host`/etc. where noted.

### Config-resolution environment variables

Each package that reads a two-layer `config/<name>.yaml` + `config/<name>.local.yaml`
also honors one environment variable that, like that package's own `--config`
flag, names exactly one file and skips the local-file merge entirely. Useful
for pointing a one-off command at a test fixture without a `--config` flag
existing on every code path that loads that config.

| Variable | Package | Equivalent to |
|---|---|---|
| `NEO_WEBAPP_CONFIG` | `neo_webapp` | `neo-webapp`'s `--config` |
| `NEO_MOTION_CONFIG` | `neo_motion` | `neo-servo-check`'s `--config` |
| `NEO_MODEL_CONN_CONFIG` | `model_conn` | `neo`'s top-level `--config` |
| `NEO_INTELLIGENCE_CONFIG` | `intelligence` | (no CLI flag of its own; set this to override `config/intelligence.yaml` for `neo --prompt`, the panel's Dialog/Audio tabs, etc.) |
| `NEO_AUDIO_CONFIG` | `neo_audio` | `neo-audio-check`'s `--config` |

### `neo` -- the model-conn CLI (installed by `pip install -e ".[dev]"`)

Top-level flags on `neo` itself. **Exactly one** of `--prompt`, `command up`,
`--connection:status`, `--connection:ping`, `--vision:status`, `--webapp ...`
or `--tls ...` may be given per invocation -- combining two of them is an
error.

| Flag | Takes | Meaning |
|---|---|---|
| `--model` | `PATH` | Path to a `.gguf` model file. Only meaningful with the positional `up` command (host role): starts Ollama serving that model. |
| `up` (positional) | -- | Host role: start serving the model named by `--model`. The only accepted value; anything else is rejected by argparse's `choices`. |
| `--connection:status` | -- | Receiver role: probe the off-board host once (Ethernet then Wi-Fi) and print `UP`/`DOWN`, latency and failure count. Also writes `var/model_conn/status.json`. |
| `--connection:ping` | -- | Receiver role: run several probes (`receiver.ping_count` in config) and print aggregate latency/loss stats, rather than a single check. |
| `--vision:status` | -- | Print what `neo_perception` currently sees, read from its status file -- needs `neo --webapp up` (or another perception-owning process) already running; this command does not open a camera itself. |
| `--prompt` | `TEXT` | Ask the assistant `TEXT` directly through the same `chat.ask()` path the panel's Dialog tab and voice loop use, print the reply, and exit. Refused if `TEXT` is empty/whitespace-only. |
| `--webapp` | -- | Switch to the admin-panel subcommand surface -- see the `neo --webapp <subcommand>` table below. Every flag after the subcommand is forwarded verbatim to that subcommand's own parser. |
| `--tls` | -- | Switch to the mTLS subcommand surface -- see the `neo --tls <subcommand>` table below. |
| `--config` | `PATH` | Override `config/model_conn.yaml` with exactly this file (no local-file merge). Applies only to `up`, `--connection:status`, `--connection:ping` and `--prompt` -- **not** to `--webapp ...`, which has its own, separate `--config` forwarded to `neo_webapp`. |

#### `neo --webapp <subcommand>`

| Subcommand | Forwards to | Extra flags accepted (forwarded, not parsed by `neo` itself) |
|---|---|---|
| `up` | `neo_webapp.__main__` | See the `neo-webapp` table below. |
| `setup` | `neo_webapp.scripts.setup_admin` | See the `neo --webapp setup` table below. |
| `devcert` | `neo_webapp.scripts.make_dev_cert` | None. Mints a self-signed dev certificate at the paths `config/webapp.yaml` names; takes no arguments and reads no environment variables. |

Anything typed after the subcommand that `neo`'s own top-level parser (the
table above) does not recognize is passed straight through, unparsed, to that
subcommand's own `main()` -- which is what lets `neo --webapp up --config
other.yaml` mean *neo_webapp's* `--config`, distinct from the top-level `neo
--config` in the first table.

**`neo-webapp` (i.e. `neo --webapp up`)**

| Flag | Takes | Meaning |
|---|---|---|
| `--config` | `PATH` | Use exactly this webapp YAML config file instead of the merged `config/webapp.yaml` + `config/webapp.local.yaml`. |
| `--host` | `HOST` | Override `server.host` from config (e.g. bind to `0.0.0.0` to accept connections from other devices, or `127.0.0.1` to restrict to this machine). |
| `--port` | `INT` | Override `server.port` from config. |
| `--backend` | `auto`\|`mock`\|`ros` | Which robot bridge to run against. `auto` picks `ros` if `rclpy`+`neo_msgs` are importable, else falls back to `mock`. `mock` forces the simulated robot (`MockBridge`) even when ROS is available -- useful for developing the panel without disturbing a running robot. `ros` forces the real bridge and fails loudly if ROS isn't actually available, rather than silently falling back. |
| `--no-tls` | -- | Serve plain HTTP instead of HTTPS. Browsers then refuse camera/microphone access from any device but the panel's own host (`getUserMedia` requires a secure context), so this is for command-line/API testing, not for using the webapp sources from a phone or another machine. |
| `--log-level` | e.g. `debug`, `info`, `warning` | Uvicorn and the panel's own logger level. Default `info`. |

**`neo --webapp setup`**

| Argument | Takes | Meaning |
|---|---|---|
| `username` (positional, optional) | string | Which account to create/reset. Default `admin` -- this is a single-operator console, so there is normally only ever one. |
| `NEO_ADMIN_PASSWORD` (environment variable, not a flag) | string | Set this to skip the interactive password prompt entirely -- the non-interactive path used when provisioning the Pi over SSH (`scripts/pi/setup-user.sh` does not set it; it's for your own scripting). Unset, the command prompts twice with `getpass` and refuses if the two entries don't match. Either way the password itself is never echoed or stored -- only its argon2 hash is written to `config/webapp.local.yaml`, alongside a freshly generated session secret and a one-time recovery code that is printed once and never stored in plaintext. |

#### `neo --tls <subcommand>`

| Subcommand | Meaning |
|---|---|
| `init` | Run once, on the off-board host (the laptop). Generates the private CA (only if it doesn't already exist -- safe to re-run) plus a fresh server certificate for this machine and a fresh client certificate for the Pi. Prints the three paths to copy to the Pi. |
| `panel` | Run on the Pi, after the CA cert+key are present there (either from `init` run locally, which they should not be, or copied over -- signing needs the *key*, not just the cert). Issues the admin panel's own server certificate from that same CA, so installing one CA on your phone trusts both the panel and the model-host link. Safe to re-run, e.g. after the Pi's hostname or address changes. |

| Flag (either subcommand) | Takes | Meaning |
|---|---|---|
| `--config` | `PATH` | Override `config/model_conn.yaml` for this `--tls` invocation only -- a separate, minimal parser from the main one, but the same semantics. |

### `neo-servo-check` -- is the servo hardware wired up and drivable?

No arguments at all performs a **read-only** check (PWM/I2C permissions, the
config that would be used) and moves nothing. Everything below is optional;
most are mutually exclusive move modes.

| Flag | Takes | Meaning |
|---|---|---|
| `--config` | `PATH` | Use exactly this motion config file instead of the merged `config/motion.yaml` + `config/motion.local.yaml`. |
| `--center` | -- | *(move mode)* Drive pan and tilt to centre (a continuous-rotation servo: to neutral, i.e. stopped, not to an angle). |
| `--pulse` | `CHANNEL US` | *(move mode)* Drive one PWM channel to one raw pulse width in microseconds -- the low-level primitive calibration is built from. `CHANNEL` is 0 or 1 for `rpi-pwm` (GPIO18/GPIO19), or 0-15 for `pca9685`. Refused outside the servo model's `min_us`-`max_us` hard limits before anything is touched. |
| `--stop` | -- | *(move mode)* Stop sending pulses to both servos (releases/unexports the channels). The way to halt a continuous-rotation servo a crashed process left spinning. |
| `--move` | `PAN_DEG TILT_DEG` | *(move mode)* Drive the head through the real `ServoDriver` (clamp, slew limit, deadband, watchdog) to this angle and back to `(0, 0)`. For a continuous-rotation servo this is also the first real test of the dead-reckoning estimate: refused unless both axes are already calibrated, since the angle would otherwise be a pure guess. |
| `--hold` | `SECONDS` (default `2.0`) | How long to hold a `--center` or `--pulse` move before releasing. Ignored by `--move` (which times itself from the configured max speed) and by `--stop`. |
| `--keep` | -- | Leave the servos actively driven when the command exits, instead of releasing after `--hold` seconds. Refused for `--pulse` on a continuous-rotation servo unless the pulse *is* neutral -- any other pulse would leave it turning indefinitely with nothing watching it. |
| `--record-neutral` | `AXIS US` | Write a measured neutral pulse (the point in the dead-band where the servo does not turn) for `AXIS` (`pan` or `tilt`) straight into `config/motion.local.yaml`. Touches no hardware -- purely a YAML write, merged so every other key already there is left standing. Finding the actual pulse is still a physical step: drive candidates with `--pulse` and watch the shaft. |
| `--record-speed` | `AXIS US` | Compute a measured speed at this pulse (`abs(--turns) * 360 / --seconds`) and write it to whichever side of neutral the pulse falls on (`above_us`/`above_deg_s` or `below_us`/`below_deg_s`) in `config/motion.local.yaml`. Requires `--record-neutral` for this axis to have been run already, since which measured speed applies depends on which side of neutral the *pulse* is on. Needs `--turns` and `--seconds` too. Refused if `US` equals the recorded neutral exactly (a speed measurement needs a pulse clear of it). |
| `--turns` | `FLOAT` | Full rotations counted by hand while `--record-speed`'s pulse was running (magnitude only; direction comes from which side of neutral the pulse is on, not the sign here). Required with `--record-speed`. |
| `--seconds` | `FLOAT` | How long that pulse ran for, in seconds -- the denominator of the deg/s calculation. Required with `--record-speed`; must be positive. |

### `neo-audio-check` -- the microphone, speaker, wake word and speech path

With no arguments it lists the ALSA capture and playback devices, loads the wake
word model, and reports whether speech to text and synthesis are available.
Nothing is recorded or played. The rest are mutually exclusive.

| Flag | Takes | Meaning |
|---|---|---|
| `--config` | `PATH` | Use exactly this audio config instead of the merged `config/audio.yaml` + `config/audio.local.yaml`. |
| `--listen` | -- | Live wake word scores from the mic, one bar per window, marking each accepted firing. How `wakeword.threshold` is chosen. |
| `--record` | -- | Record and print RMS levels, to tell a muted or wrong device from a model problem. |
| `--say` | `TEXT` | Synthesise `TEXT` with Piper and play it through the speaker, waiting until it has finished sounding. |
| `--transcribe` | -- | Transcribe what you say, closing on the same endpointer the robot uses. |
| `--seconds` | `FLOAT` (default `10`) | How long `--listen`, `--record` or `--transcribe` runs. |

### `neo-perception-bench` -- benchmark and validate a detector backend

| Flag | Takes | Meaning |
|---|---|---|
| `--model` | `PATH` (default `yolov8n-pose.pt`) | Which Ultralytics pose-model weights to load. |
| `--imgsz` | `INT` (default `320`) | Inference resolution, square. `320` is the Pi 4 working point this repo's frame-rate targets assume. |
| `--conf` | `FLOAT` (default `0.35`) | Detection confidence threshold passed straight to the Ultralytics detector. |
| `--runs` | `INT` (default `20`) | How many inference passes to time and average for the ms/frame and fps figures. |
| `--source` | `bus`\|`webcam`\|`PATH` (default `bus`) | Where the benchmark frame comes from: `bus` downloads Ultralytics' own documented sample photo (has people in it), `webcam` grabs one frame from the default camera, or any other value is treated as an image file path. |

### `scripts/pi/setup-system.sh` -- provision a fresh Pi (run as root, once)

| Flag | Takes | Meaning |
|---|---|---|
| `--eth-address` | `CIDR` (default `192.168.50.2/24`) | Static address to assign `eth0` -- the Ethernet path of the dual-path link to the off-board host. |
| `--with-panel-service` | -- | Install and enable a `neo-panel.service` systemd unit, so the admin panel starts on boot rather than needing a manual `neo --webapp up`. |
| `--with-robot-service` | -- | Install and enable `neo-robot.service`, which runs `scripts/pi/neo-up.sh` (the ROS graph, profile `hardware`) at boot. Change the profile with `sudo systemctl edit neo-robot` and `Environment=NEO_PROFILE=dev`. |
| `--servo-pwm` | -- | Enable the Pi's hardware PWM on GPIO18/GPIO19 (`dtoverlay=pwm-2chan`) for servos wired straight to the Pi, add the `pwm` group and udev rule so `neo-servo-check` needs no root, and disable the 3.5 mm audio jack (it shares that same hardware block) -- the speaker must then be USB. |
| `-h`, `--help` | -- | Print the usage block from the top of the script and exit. |

Any other flag is rejected with `unknown option: ... (try --help)`.

### `scripts/pi/setup-user.sh` -- the per-user half (run as the robot's own user, NOT root)

| Argument | Takes | Meaning |
|---|---|---|
| `--skip-tests` (positional, i.e. must be the first argument) | -- | Skip running every package's test suite at the end. Everything else (venv creation, `pip install`, writing missing per-machine config files, the `colcon build`) still happens. |

| Environment variable | Default | Meaning |
|---|---|---|
| `NEO_VENV` | `~/neo-venv` | Where to create the Python virtual environment. |
| `NEO_EXTRAS` | `dev,detector,voice,servo` | Which `pip install -e ".[...]"` extras to install into it. |
| `NEO_LAPTOP_ETH` | `192.168.50.1` | The off-board host's static Ethernet address, written into the per-machine config this script generates. |

### `scripts/pi/neo-up.sh` -- start the robot (on the Pi, as its user)

| Flag | Takes | Meaning |
|---|---|---|
| `--profile` | `NAME` (default `hardware`, or `$NEO_PROFILE`) | Which `neo_bringup` profile to launch: `dev`, `hardware`, `hybrid`, `bench`. |
| `--panel` | -- | Also start the admin panel in the background, through `neo-panel.sh` (logs `var/panel.log`, pid `var/panel.pid`). |
| `--check` | -- | Print the profile, the interpreter, whether `rclpy`, `neo_msgs`, `cv_bridge`, `numpy`, `vosk` and `piper` import, and the numpy version ROS will get (it must be the system's 1.x, or `cv_bridge` misbehaves); start nothing. |

| Environment variable | Default | Meaning |
|---|---|---|
| `NEO_VENV` | `~/neo-venv` | The venv whose site-packages go on `PYTHONPATH`, after the system's dist-packages so ROS keeps the numpy `cv_bridge` was built against. |
| `NEO_PROFILE` | `hardware` | The profile when `--profile` is not given; what `neo-robot.service` sets. |
| `NEO_ROS_SETUP` | `/opt/ros/jazzy/setup.bash` | Which ROS install to source. |

### `scripts/pi/neo-panel.sh` -- start the admin panel against the robot

Sources `/etc/profile.d/neo-ros-env.sh`, ROS and the workspace, then runs
`$NEO_VENV/bin/neo --webapp up`, passing any arguments through. What
`neo-panel.service` runs. With no ROS workspace present it says so and the
panel runs its simulated robot.

### `scripts/pi/sync-to-pi.sh` -- copy this checkout to the robot (run from the laptop)

| Argument | Takes | Meaning |
|---|---|---|
| `--dry-run` (must come first, if given) | -- | List what would be sent, and confirm no secret is among it (certs, `*.local.yaml`), without touching the network. |
| `[user@host]` (positional, optional) | string (default `neo@neo-pi.local`) | The Pi's SSH target. |

| Environment variable | Default | Meaning |
|---|---|---|
| `NEO_SSH_KEY` | `~/.ssh/id_ed25519_neo` if present | Which SSH private key to authenticate with. |

## Not built yet

- **A run on the Pi with its USB webcam and microphone.** The graph has been
  driven end to end under ROS in WSL, and every hardware backend is written to
  find its device and recover from an unplug, but the webcam-with-mic did not
  enumerate on the Pi (`lsusb` showed no device) when they were written. That
  run, and the wake word's false-accept rate in the real foyer, are still owed.
- **Dialog refinements** (plan Phase 8): the ~6 s follow-up window, a filler
  line while thinking, short conversation context, and a `launch_testing` suite.
- **Off-board Whisper**, the ASR accuracy upgrade for when the link is healthy.
  Vosk is the only engine, which is the right default — it is the one that works
  with the laptop closed.
- **Room-code accuracy.** The small Vosk model hears "CS-204" as "p s two hundred
  for". Plan 5.4's grammar-constrained recognition over the room list is the
  fix, and it needs the Phase 7 data to build the grammar from.
- **The rest of the campus knowledge base** (plan Phase 7). Built: the data
  files, their validation, retrieval, template answers, and the Data tab.
  Not built: fuzzy matching of misheard codes, the unanswered-question log,
  the evaluation set, and the `kb_service` ROS wrapper. There is no campus data
  yet; it is entered on the Data tab.
- **The Logs tab** (plan Phase 10): journal tail and bag recording.

Full-body locomotion is out of scope until asked for.
