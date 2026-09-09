# neo_webapp — Neo's admin panel

The operator surface for the whole robot, and the stand-in for its physical I/O
during development. Implements Phase 2 of [the implementation plan](../../docs/IMPLEMENTATION_PLAN.md).

It is three things, deliberately separated:

| Piece | Cost | Runs when |
|---|---|---|
| **Admin API** (`api/`) | light | always, including the field profile |
| **Media bridge** (`media/`) | heavy | only while a channel is actually open |
| **Operator UI** (`ui/`) | — | in the browser; no build step |

## Quick start

Install from the repo root (`pip install -e ".[dev]"`, see the
[top-level README](../../README.md)), then:

```bash
neo --webapp devcert        # TLS, needed for the browser camera/mic
neo --webapp setup          # sets the admin password
neo --webapp up             # https://localhost:8443
```

`neo --webapp {up,setup,devcert}` (from the [`model_conn`](../model-conn/README.md)
package) forwards straight into this package's own CLI/scripts, so every flag
below still applies, e.g. `neo --webapp up --no-tls --port 8500`. There is no
separate `neo-webapp`/`neo-webapp-setup`/`neo-webapp-devcert` script anymore —
`neo --webapp ...` is the only entry point.

On Windows without installing the package:

```bash
set PYTHONPATH=src\neo_webapp && python -m neo_webapp
```

### Why TLS is not optional

Browsers only grant `getUserMedia` in a *secure context*. Over plain HTTP from any
device other than the host, the webapp camera and microphone sources **will not
work at all** — `localhost` is the sole exemption. `--no-tls` is therefore a
localhost-only development convenience, and it logs a warning saying so.

The generated certificate is self-signed, so the browser warns once. Phase 1
replaces it with one issued by the project's private CA.

## The bridge seam

Nothing above `bridge/` knows whether it is talking to a robot or a simulation —
the same discipline the source mux applies to camera, mic, and speaker.

| Backend | When | What it does |
|---|---|---|
| `MockBridge` | no ROS present | Simulates the robot. Integrates joystick input into a head pose under the *same* limits, slew rate, and deadman rules the real servo driver will enforce. |
| `RosBridge` | `rclpy` + `neo_msgs` importable | Talks to the real nodes. Skeleton until Phase 1 freezes the message contracts. |

`bridge.backend: auto` prefers ROS and falls back to the mock **with a warning**,
so a Pi that has lost its ROS environment never silently looks healthy.

This is what makes the panel developable on a Windows laptop with no Pi, no ROS,
and no servos attached.

## Safety: the joystick deadman

The joystick is virtual, so head control is network-mediated. A stale command
stream must stop the head, not let it coast. Three independent guards:

1. **Client** sends neutral axes on pointer release.
2. **Server** watchdog publishes neutral after `joy_deadman_ms` (default 300 ms)
   of silence — this is the one that covers a frozen tab, a closed lid, or a
   dropped Wi-Fi link, where the browser never gets to send anything.
3. **Server** publishes neutral unconditionally on disconnect.

`head_behavior` will enforce the same rule independently on the robot side
(Phase 3); this layer only means the stop happens without a round trip.

> **Tuning note for Phase 3.** The deadman is a *travel* budget, not just a time
> budget: 300 ms at 60°/s is ~18° of unwanted head movement after an operator's
> connection dies. Measured, not theoretical. Pick the deadman and the max slew
> rate together.

## Configuration

`config/webapp.yaml` holds committed defaults and no secrets.
`config/webapp.local.yaml` holds the argon2 password hash and session secret; it is
gitignored and written by `neo --webapp setup`. The local file merges over the
main `webapp.yaml` file to apply those overrides without making the working tree
dirty.
For unattended provisioning: `NEO_ADMIN_PASSWORD=... neo --webapp setup`.

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/health` | no | liveness; deliberately says nothing sensitive |
| POST | `/api/auth/login` \| `logout` | no \| — | session cookie |
| GET | `/api/auth/me` | yes | current user |
| GET | `/api/state` | yes | full robot state snapshot |
| GET | `/api/config` | yes | media config the UI needs (deadman ms, rates, fps, jpeg quality) |
| GET | `/api/sources` | yes | current backends |
| POST | `/api/sources/set` | yes | `{stream, backend}` |
| POST | `/api/head/center` | yes | recenter |
| POST | `/api/system/estop` | yes | `{engaged}` |
| POST | `/api/media/test-tone` | yes | prove the speaker path (mock only) |
| GET | `/api/link/status` | yes | model_conn's link/ping stats — polled by the System tab |
| GET | `/api/dialog/status` | yes | intelligence's last chat turn — polled by the Dialog tab |
| GET | `/api/model/status` | yes | what `neo --model … up` is serving, plus the config-mismatch check |
| POST | `/api/dialog/ask` | yes | `{text}` → ask the model, same path as `neo --prompt` |
| POST | `/api/perception/identify` | yes | "what is this?" — one object-model pass on a live frame |
| POST | `/api/perception/release` | yes | drop the engagement lock (operator override) |
| POST | `/api/perception/reset` | yes | clear all tracks and ids — after moving the camera |

WebSockets — all authenticated **before** the handshake is accepted, so an
unauthenticated client never holds a media channel open:

| Path | Direction | Payload |
|---|---|---|
| `/ws/state` | robot → panel | JSON snapshot at 4 Hz |
| `/ws/joy` | panel → robot | JSON `{axes, buttons}` |
| `/ws/camera` | panel → robot | binary JPEG, server-side rate capped |
| `/ws/mic` | panel → robot | binary 16 kHz mono s16le PCM |
| `/ws/speaker` | robot → panel | binary 22.05 kHz s16le PCM |

## Tests

```bash
python -m pytest -q
```

Covers the auth gate (including lockout and pre-handshake WebSocket rejection),
per-stream source switching, media channel lifecycle and leak-on-disconnect,
server-side frame rate capping, the deadman — including that soft limits hold
under sustained input and that e-stop overrides live commands — the head arbiter's
priority order, the identify route's timeout and failure paths, the model-host
status transitions, and the state-consistency regressions in §*Two ways state used
to lie* below.

## What the panel is a client of, not just an observer of

Two things it now *drives* rather than merely displaying:

**Perception.** The Vision tab runs the real
[`neo_perception`](../neo_perception/README.md) pipeline against whatever camera
the panel is using — palm-gesture engagement, facing-based release, and on-demand
object identification, all with no Pi and no servos. The head it drives is
simulated; the perception is not.

**The model.** With `neo --model … up` running, the Dialog tab shows what is being
served (name, endpoint, mTLS or plain) and lets you ask it questions.
`POST /api/dialog/ask` goes through `intelligence.chat.ask` — deliberately the same
function `neo --prompt` calls — so endpoint resolution, mTLS, the degraded reply
and status-file writing cannot drift between the CLI and the browser.

It also surfaces a mismatch that is otherwise silent: `chat.model_name` in
`config/intelligence.yaml` has to match the name the host registered in Ollama.
Nothing enforces that, and getting it wrong produces a degraded reply
indistinguishable from the link being down — so the panel names the value to set.

## Two ways state used to lie

Both were invisible without a test, and both now have regressions in
`tests/test_state_consistency.py`:

- **A `@property` on a dataclass served through `asdict()`** is dropped silently —
  no error, the key is simply absent from the JSON. `IdentifyView.best` and
  `ModelView.summary` were both lost this way. They are plain fields now, and a
  test fails if any served dataclass grows a property.
- **A snapshot field nothing keeps current.** `RobotState.link` shipped in every
  4 Hz snapshot reading "down, 0 failures" while `/api/link/status` beside it had
  the real numbers. It is refreshed from the same reader at 1 Hz — *not* inside
  `snapshot()`, because reading it is file I/O and that is exactly what the
  broadcast loop must not do.

## Tabs, and the phase that fills each one

Sources, System, Head, Vision, and Dialog are live. Audio, Data, Emotion, and Logs
are stubs labelled with the phase that fills them — see plan §2.5. Building them
this way keeps the panel useful throughout rather than deferring a monolithic UI
phase to the end.
