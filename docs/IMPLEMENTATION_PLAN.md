# Neo — Implementation Plan

Step-by-step plan to build Neo from an empty repo to a deployed campus receptionist robot.

Companion to [CLAUDE.md](../CLAUDE.md), which holds the design invariants. This document holds the *order of work*, the concrete steps, and the acceptance criteria.

**Status:** written before any code exists. Effort estimates are rough, and every performance number below is a *target to measure in Phase 0/1*, not a promise. Update this file as phases land.

**Revision 2** — incorporates the owner's decisions: Pi Camera (CSI) + webapp-device camera, USB mic on the Pi + webapp-device mic, Ethernet **and** Wi-Fi paths to the off-board host, the off-board link must be secured, campus data arrives incrementally, the laptop is a daily-driver machine, the joystick is virtual (in the web app), and **the web app is an admin panel that manages the entire robot**.

---

## How to use this document

- Phases are ordered by dependency, not by excitement. The seams (message contracts, source selection, admin panel) come before the fun parts (YOLO, LLM, emotion), because every later phase plugs into them.
- Each phase has **Steps**, **Done when**, and **Do not** (scope fences to stop the phase sprawling).
- "Bench" steps are measurements. Do them; several architectural choices below are only correct if the numbers hold.
- The admin panel is not a phase. It grows **one tab per phase** — see §2.5.

---

## 0. Decisions

### 0.1 Invariants from CLAUDE.md (do not relitigate)

| Decision | Consequence for this plan |
|---|---|
| ROS 2 Jazzy as integration layer | Everything is a node; nothing calls another subsystem in-process |
| Qwen 2.5 3B runs off-board | Pi needs timeouts + degraded mode from day one |
| Vectorless RAG | No embeddings, no vector DB. Deterministic lookup over a structured dataset |
| YOLO on the Pi camera feed | Perception is the Pi's tightest constraint |
| Runtime per-stream source selection | A mux + lifecycle pattern, designed in Phase 2 before any consumer exists |
| Wake word gates everything upstream of ASR | Wake detector owns the mic; ASR never runs un-gated |
| Head-only motion now | Motion interfaces stay general; no base, no nav stack |

### 0.2 Decisions confirmed by the owner (revision 2)

| Question | Answer | What it changes |
|---|---|---|
| Pi OS | Ubuntu Server 24.04 LTS arm64 | Jazzy tier-1; **CSI camera on Ubuntu is now the top technical risk** (Bench A) |
| Camera backends | Pi Camera Module (hardware) / webapp device camera | `camera_ros` + libcamera on the Pi; JPEG-over-WS from the browser |
| Mic backends | USB mic on the Pi / webapp device mic | Both switchable at runtime, per §2 |
| Off-board transport | HTTP **but it must be secured** | Promoted to mutual-TLS; see §0.3.1 — no longer a Phase 6 detail |
| Link | Ethernet **and** Wi-Fi, for safety | Dual-path failover in `llm_client`; see §0.3.2 |
| Campus data | Added incrementally as it is collected | KB is built data-last: engine + validator + admin CRUD first, coverage-aware answers, hot reload; see Phase 7 |
| Off-board host | The owner's daily-driver laptop | **Degraded mode is the normal case, not the exception.** Forces on-Pi ASR; see §0.3.3 |
| Joystick | Virtual, in the web app | No USB gamepad node. Adds a network deadman requirement; see Phase 3 |
| Web app | **Admin panel for the whole robot**, not just a dev I/O stand-in | Split backend, authentication, per-phase tab growth; see §2 |

### 0.3 The four consequences worth reading before you start

#### 0.3.1 Security is now structural, not a later hardening pass

Two things are reachable over a network that is not a private cable: the off-board inference service, and the admin panel that can move the robot and edit its data. Both need to be secured from the first commit that exposes them.

- **Off-board service: mutual TLS.** Generate a private CA once. Issue a server certificate for the laptop (SAN covering both its Ethernet address and its Wi-Fi name) and a client certificate for the Pi. The service accepts only that client certificate. This beats a bearer token: nothing to rotate, nothing to leak in a config file, and it authenticates both directions. Bind the service to the specific interfaces and firewall the rest.
  An unauthenticated llama.cpp server on campus Wi-Fi is an open LLM proxy for anyone on that network. Do not run one, even "just for testing".
- **Admin panel: HTTPS + login.** Single admin account, password hashed with argon2, session cookie, HTTPS only, rate-limited login. The panel can drive servos and rewrite campus data; on a shared network, an unauthenticated panel means anyone can do both.
- **HTTPS is not optional for the admin panel — it is a functional requirement.** Browsers only grant `getUserMedia` (camera and microphone) in a *secure context*. Over plain HTTP from another device, the webapp camera/mic sources **will not work at all**; only `localhost` is exempt. So the moment you want to use your phone or laptop as Neo's mic, you need TLS on the Pi. Plan for it in Phase 2, not Phase 10.
  Use a certificate from the same private CA, install the CA once on the devices you use, and give the Pi a stable name.

#### 0.3.2 Dual-path link (Ethernet + Wi-Fi)

`llm_client` holds an **ordered list of endpoints**, not one address: Ethernet first (deterministic, sub-millisecond), Wi-Fi second, then degraded. It health-probes both continuously and publishes the active path on `/link/health`.

Two practical traps:
- The laptop's Wi-Fi address will move. Use a DHCP reservation or mDNS (`neo-brain.local`), and make sure the TLS certificate's SAN covers whichever you choose.
- **Many campus Wi-Fi networks enable AP isolation**, which blocks client-to-client traffic outright. If yours does, the Wi-Fi path is dead regardless of your code. Test this in Phase 0 with a single `curl` before designing around it.

#### 0.3.3 A daily-driver laptop means Neo must be genuinely useful without it

The laptop will be asleep, gaming, on battery, or elsewhere. So:

- **Degraded mode is the default state to design for.** It is not an error path you visit occasionally; it is a mode Neo will spend real hours in, at the desk, in front of people.
- **On-Pi ASR is now mandatory, not optional.** In revision 1, ASR ran off-board next to the LLM. With an unreliable host, that would leave Neo *deaf* whenever the laptop is away — unable even to answer "where is CS-204", which is its primary job and otherwise fully local. Local ASR moves from "Phase 10, optional" to **Phase 5, required**.
  Recommendation: **Vosk small (Indian English model)** on the Pi — around 50 MB, streaming, real-time on a Pi 4, and it supports grammar-constrained decoding, which is a strong fit for room codes like "CS-204". Off-board `faster-whisper` remains an *accuracy upgrade* used when the link is healthy, particularly for open-domain assistant questions.
- **The info-desk path must be 100% LLM-free.** The template renderer in Phase 7 is the *primary* output path, not a fallback. Budget real effort on making template phrasing sound natural, because most of the time it is what people will hear.
- **The laptop side needs an idle unload** so a 3B model isn't holding VRAM while you work. Keep it resident during desk hours; let it unload after idle and accept a few seconds on the first question.

#### 0.3.4 The web app is the product's operator surface

It is three things at once, and separating them matters for the Pi's CPU budget:

1. **Admin API** (always on, light): auth, health, config, campus data CRUD, logs, state stream.
2. **Media bridge** (on demand, heavy): camera/mic/speaker WebSocket bridges. Active only when a webapp source is selected or the media tab is open.
3. **Operator UI**: the browser app itself.

Splitting 1 from 2 is what lets the panel stay available in the field profile without a media pipeline burning a core. In revision 1 the whole web app was excluded from the `hardware` profile; that is no longer possible, so the split does the same job.

### 0.4 Remaining open items

- **Bench A outcome** decides the camera story. Criteria in Phase 0, step 6.
- **AP isolation** on campus Wi-Fi (Phase 0, step 4) decides whether the second link path exists at all.
- Whether you want a **physical power cutoff** within reach of the desk. For a head-only robot with small servos the risk is low and a power switch is adequate, but decide deliberately rather than by omission.

---

## 1. Target architecture

### 1.1 Node map

```
 ON-ROBOT (Raspberry Pi 4)                            OFF-BOARD (daily-driver laptop)
 -------------------------                            -------------------------------
 camera_hw (PiCam/libcamera) --+                       +------------------------------+
 camera_web (browser camera) --+-> camera_mux           | llama.cpp: Qwen2.5-3B Q4_K_M |
                                    |                   | faster-whisper (upgrade ASR) |
                                    v                   | mTLS, idle model unload      |
                            /camera/image_raw           +---------------+--------------+
                                    |                                   ^
                                    v                                   | HTTPS (mTLS)
                                yolo_node                               | eth0 -> wlan0
                                    |                                   |   failover
                          /perception/detections                        |
                                    |                                   |
 mic_hw (USB) --+                   v                                   |
 mic_web -------+-> mic_mux    attention_tracker                        |
                     |              |                                   |
                /audio/in           v                                   |
                     |         dialog_manager <----- llm_client --------+
                     v            |    |    |         (dual path, timeouts, health)
                 wake_word -------+    |    +--> kb_service (vectorless RAG, LOCAL)
                 /wake/event           |    |
                     |                 |    +--> asr_router --+--> vosk (on-Pi, always)
                     +--> vad ---------+                      +--> whisper (off-board)
                                       v
                                   tts (Piper, LOCAL) -> /audio/out -> speaker_mux
                                                                       |        |
                                                                  speaker_hw  speaker_web

 ADMIN PANEL (neo_webapp)                    emotion_node -> /emotion/state
   api  (always on, light) ---------------+                      |
   media bridge (on demand, heavy) -------+   virtual joystick    v
   UI: sources, head, vision, audio,          /joy ------> head_behavior
       dialog, data, config, system, logs                        |
                                                                 v /head/command @ 50 Hz
                                                          servo_driver -> PCA9685 -> pan/tilt
```

### 1.2 Interface contracts (frozen in Phase 1)

| Interface | Type | Notes |
|---|---|---|
| `/camera/image_raw` | `sensor_msgs/Image` | mux output; consumers only ever see this |
| `/camera/{hw,webapp}/image_raw` | `sensor_msgs/Image` | backend inputs |
| `/perception/detections` | `vision_msgs/Detection2DArray` | use the standard type, don't invent one |
| `/perception/attention` | `neo_msgs/AttentionTarget` | normalized x,y in [-1,1], confidence, track id, `person_present` |
| `/audio/in`, `/audio/out` | `neo_msgs/AudioChunk` | 16 kHz mono s16le in, 22.05 kHz out; seq + stamp |
| `/wake/event` | `neo_msgs/WakeEvent` | score, threshold applied, stamp |
| `/dialog/state` | `neo_msgs/DialogState` | `IDLE, LISTENING, THINKING, SPEAKING, DEGRADED, ESTOP` |
| `/dialog/transcript` | `neo_msgs/Transcript` | text, is_final, confidence, `engine` in {vosk, whisper} |
| `/dialog/reply` | `neo_msgs/Reply` | text, `source` in {kb, llm, fallback}, latency_ms |
| `/emotion/state` | `neo_msgs/EmotionState` | label, intensity, motion params (see §9.1) |
| `/head/command` | `neo_msgs/HeadCommand` | pan/tilt rad, max speed, `priority` |
| `/head/state` | `sensor_msgs/JointState` | position actually commanded after limiting |
| `/joy` | `sensor_msgs/Joy` | published by the web app; a USB pad could publish the same later |
| `/link/health` | `neo_msgs/LinkHealth` | up, `active_path` in {eth, wifi, none}, rtt_ms, consecutive_failures |
| `/kb/coverage` | `neo_msgs/KbCoverage` | blocks/floors currently populated — drives honest "I don't know that yet" |
| `/sources/set` | `neo_msgs/srv/SetSource` | `{stream, backend}`, activates/deactivates lifecycle nodes |
| `/head/center`, `/system/estop` | `std_srvs/Trigger`, `std_srvs/SetBool` | |

**Rule:** no node subscribes to a `*/hw/*` or `*/webapp/*` topic except its own mux.

### 1.3 Repository layout

```
neo1/
├── CLAUDE.md
├── docs/
│   ├── IMPLEMENTATION_PLAN.md   # this file
│   ├── hardware.md              # BOM, wiring, calibration values, bench results
│   ├── security.md              # CA setup, cert issuance, admin account, threat notes
│   └── operations.md            # runbook: start, stop, degraded mode, field debug
├── src/                         # colcon workspace
│   ├── neo_msgs/                # interfaces only, no logic, no deps
│   ├── neo_sources/             # generic mux + lifecycle source selection
│   ├── neo_perception/          # yolo_node, attention_tracker, camera drivers
│   ├── neo_audio/               # wake_word, vad, asr_router, vosk, tts, audio device I/O
│   ├── neo_kb/                  # campus dataset + vectorless retrieval (pure Python)
│   ├── neo_dialog/              # dialog_manager, intent_router, llm_client
│   ├── neo_motion/              # servo_driver, head_behavior
│   ├── neo_emotion/             # emotion state machine
│   ├── neo_webapp/              # admin panel: api/ + media/ + ui/
│   └── neo_bringup/             # launch files, param YAML, profiles, systemd units
├── offboard/                    # runs on the laptop — NOT a ROS package
│   ├── serve.py                 # mTLS FastAPI: chat completions, /asr, /health
│   └── README.md
└── data/campus/                 # rooms.yaml, graph.yaml, aliases.yaml + validator
```

`neo_kb` is deliberately a **plain Python package with no ROS imports**, so campus data logic is testable with `pytest` alone and can be iterated on a laptop with no robot present.

---

## Phase 0 — Hardware, OS, network, and the benchmarks that validate this plan

*Goal: prove the Pi can carry the load, and prove the two link paths exist, before writing code that assumes either.*
*Estimate: 3–4 days.*

### Steps

1. **Fix the BOM and write `docs/hardware.md`.** Pi 4 4GB + active cooling (a throttled Pi silently halves your YOLO rate), 32GB+ A2 SD card or USB SSD, PCA9685, 2x metal-gear servos (MG996R class; note stall current), Pi Camera Module + ribbon of the right length for a moving head, USB mic, powered speaker, separate 5V 3A+ supply for servos, Ethernet cable.
   Cable strain relief on a panning head is a real mechanical concern — plan the ribbon route before mounting.
2. **Flash Ubuntu Server 24.04 LTS arm64.** Enable I2C (`dtparam=i2c_arm=on` in `/boot/firmware/config.txt`), SSH, hostname `neo`, disable Wi-Fi power save.
3. **Install ROS 2 Jazzy base** (`ros-jazzy-ros-base`, not desktop — no GUI on the Pi) plus `python3-colcon-common-extensions` and `python3-rosdep`.
4. **Bring up both link paths and test them.**
   - Ethernet: static addresses, e.g. Pi `192.168.50.2/24`, laptop `192.168.50.1/24`. Verify RTT and that a cable pull/replug recovers with no manual step.
   - Wi-Fi: **`curl` the laptop from the Pi over campus Wi-Fi.** If it fails, check for AP isolation before writing a line of failover code. Record the result — it decides whether §0.3.2's second path is real.
   - Confirm routing prefers Ethernet when both are up (metric/priority), and that losing Ethernet actually falls through to Wi-Fi rather than blackholing.
5. **Pin DDS to local interfaces.** Set `ROS_DOMAIN_ID` and a CycloneDDS config restricting discovery to `lo` and the robot's own interfaces. Without this, campus Wi-Fi full of other ROS machines will find you — and you will find them.
6. **Bench A — the Pi Camera on Ubuntu.** This is the highest-risk item in the project. Capture 640x480 at 30 fps for 60 s via libcamera / `camera_ros`; record achieved fps and CPU.
   **Decision rule:** if this isn't working after roughly one focused day, stop and pick one — (a) USB webcam on Ubuntu (you lose the CSI camera, keep everything else), or (b) Raspberry Pi OS + Jazzy in Docker (you keep the CSI camera, pay in RAM and debugging). Do not let this quietly consume a week.
7. **Bench B — YOLO.** Export YOLOv8n to NCNN (`yolo export model=yolov8n.pt format=ncnn`), run inference at 320x320 and 416x416, record ms/frame and RSS. Expect single-digit fps on 4 cores. Under ~4 fps at 320 means frame-skipping to 2–3 Hz, or a conversation about offloading detection.
8. **Bench C — Piper TTS.** Synthesize a 12-word sentence; record time-to-first-audio and realtime factor.
9. **Bench D — wake word.** Run openWakeWord against 10 minutes of live corridor noise; record CPU and false accepts/hour.
10. **Bench E — on-Pi ASR (new, and now load-bearing).** Run Vosk small (Indian English) on 20 recorded room-code questions; record word accuracy, room-code accuracy, latency, CPU. Repeat with a constrained grammar built from a mock room list. This decides how good Neo is when the laptop is away, which is most of the time.
11. **Bench F — off-board LLM.** Serve Qwen2.5-3B-Instruct Q4_K_M via llama.cpp; record time-to-first-token and tokens/s **measured from the Pi over each link path**, and how long a cold start takes after an idle unload.
12. **Record every result in `docs/hardware.md`** under "Measured", with date and commit, and rewrite the §12 budgets with real numbers.

### Done when
Every bench has a number written down; the camera decision is made; and you know whether the Wi-Fi path exists.

### Do not
Do not start writing nodes while benches are pending. Bench A and Bench E can each change the plan.

---

## Phase 1 — Workspace skeleton, message contracts, security groundwork, CI

*Goal: the seams exist, the CA exists, and CI runs.*
*Estimate: 2–3 days.*

### Steps

1. Create the colcon workspace and the ten package skeletons from §1.3. Python packages use `ament_python`; `neo_msgs` uses `ament_cmake` + `rosidl`.
2. **Define every message and service in §1.2 now, in one pass.** Getting these wrong is the expensive mistake; changing them later touches every package. Review against the node map before writing a single node.
3. **Set up the private CA** and write `docs/security.md`: how to generate the CA, issue the laptop server cert (SAN: Ethernet IP + mDNS name), the Pi client cert, and the Pi's own admin-panel server cert; where keys live; how to install the CA on your phone and laptop so the admin panel is trusted. Keys go in a gitignored path, never in the repo.
4. Add `neo_bringup` with `neo.launch.py` taking a `profile` argument (`dev`, `hardware`, `hybrid`, `bench`) loading `config/<profile>.yaml`.
5. Testing harness: `pytest` for logic, `launch_testing` for node integration, `colcon test` as the entry point, one trivial passing test per package.
6. Linting via the standard `ament_flake8` / `ament_pep257` hooks.
7. CI running `colcon build && colcon test` on Ubuntu 24.04 + Jazzy.
8. **Update CLAUDE.md's Status section** with the real commands now that they exist:
   ```bash
   colcon build --symlink-install && source install/setup.bash
   colcon test --packages-select neo_kb && colcon test-result --verbose
   ```

### Done when
`colcon build` and `colcon test` pass from clean on both the Pi and your dev machine; `ros2 interface show neo_msgs/msg/HeadCommand` prints the frozen contract; and `curl --cacert ...` against a stub mTLS service succeeds from the Pi and is refused without the client cert.

### Do not
Do not put logic in `neo_msgs`. Do not add an `rclpy` dependency to `neo_kb`.

---

## Phase 2 — Source selection and the admin panel core

*Goal: the hardware/webapp seam plus the operator surface, built before any consumer exists.*
*Estimate: 7–9 days. The highest-leverage phase in the plan, and now the largest.*

### 2.1 The mux pattern

Two mechanisms working together:

- **Mux nodes** (`neo_sources`): a generic `StreamMux` subscribing to `<ns>/hw/<topic>` and `<ns>/webapp/<topic>`, republishing exactly one to `<ns>/<topic>`, selected by parameter. One instance per stream. The speaker is the mirror image: a `StreamDemux` routing `/audio/out` to the active sink.
- **Managed lifecycle nodes**: each backend driver is a `LifecycleNode`. Selecting a backend *activates* it and *deactivates* the other. On a 4GB Pi you cannot afford an idle camera pipeline burning a core, so deactivation must genuinely stop capture, not just stop publishing.

`/sources/set` does both transactionally: activate new, wait for `ACTIVE`, switch the mux, deactivate old. On activation failure, roll back and report — never leave both inactive.

### 2.2 Admin panel backend split

- **`neo_webapp/api`** — always on, light. FastAPI over HTTPS: session auth, `/ws/state` fan-out (`/dialog/state`, `/link/health`, `/sources/state`, `/emotion/state`, system health), config read/write, campus data CRUD (Phase 7), log access.
- **`neo_webapp/media`** — on demand, heavy. WebSocket bridges: `/ws/mic` (browser PCM to `/audio/webapp/in`), `/ws/camera` (browser JPEG at 5–10 fps to `/camera/webapp/image_raw`), `/ws/speaker` (`/audio/webapp/out` to browser playback), `/ws/joy` (virtual joystick to `/joy`). Started only when a webapp source is selected or the media tab is open; torn down on disconnect.

Both embed an `rclpy` node running a `MultiThreadedExecutor` on a background thread — be deliberate about the GIL and about which callbacks touch shared state.

### 2.3 Authentication

Single admin account, argon2-hashed password in a gitignored config, session cookie with `Secure` + `HttpOnly` + `SameSite`, rate-limited login, and every mutating endpoint behind the session check. Serve over HTTPS with the Phase 1 certificate — remember from §0.3.1 that without HTTPS the browser will refuse the camera and microphone entirely.

### 2.4 Steps

1. Implement `StreamMux` / `StreamDemux` with parameter and service control, plus `source_manager` owning `/sources/set` and publishing `/sources/state`.
2. Implement hardware backends as lifecycle nodes: `camera_hw` (wrapping `camera_ros`, or `v4l2_camera` if Bench A sent you to USB), `mic_hw` and `speaker_hw` (sounddevice/ALSA).
3. Build `neo_webapp/api` with auth, TLS, and the state WebSocket.
4. Build `neo_webapp/media` with the four bridges and on-demand lifecycle.
5. Build the UI shell: plain HTML/JS is fine — resist adding a build step for a single-operator panel. Tabs are stubs except **Sources** and **System**.
6. **Virtual joystick with a deadman.** It sends over the network, so: it publishes `/joy` at a fixed rate while held, and `head_behavior` stops motion if `/joy` goes stale for >300 ms. A dropped WebSocket mid-drag must stop the head, not leave it driving.
7. `launch_testing` integration tests: publish on both backend topics, flip the source, assert the mux output switches and exactly one backend is `ACTIVE`; assert unauthenticated API calls are refused.

### 2.5 Admin panel tabs — delivered per phase

| Tab | Contents | Phase |
|---|---|---|
| **Sources** | Per-stream backend selector (camera / mic / speaker), live state | 2 |
| **System** | CPU, RAM, temperature, node status, link path + RTT, estop | 2 |
| **Head** | Virtual joystick, center, live pan/tilt readout, limit display | 3 |
| **Vision** | Camera preview with detection overlay, attention target, confidence slider | 4 |
| **Audio** | Live wake-word score meter, threshold tuning, VAD view, TTS test box | 5 |
| **Dialog** | Live transcript, reply + source badge, latency breakdown, conversation history | 6, 8 |
| **Data** | Campus data CRUD, validation results, coverage map, **unanswered-question log** | 7 |
| **Emotion** | Current state, manual override, gesture triggers, motion param tuning | 9 |
| **Logs** | Journal tail, bag recording toggle, download | 10 |

Building each tab in the phase that produces its data keeps the panel useful throughout and avoids a monolithic UI phase at the end.

### Done when
With no camera, mic, or servos attached, you can log in over HTTPS from a second device, see your browser's camera arriving on `/camera/image_raw`, and switch each stream between `hardware` and `webapp` at runtime without restarting anything.

### Do not
Do not use WebRTC yet — WebSockets with raw PCM and JPEG are simpler and sufficient; revisit only if measured latency hurts.
Do not fork nodes per backend. If you write `yolo_node_webapp.py`, the phase has failed.
Do not add multi-user accounts or roles.

---

## Phase 3 — Motion: servos, safety, virtual joystick

*Goal: the head moves, safely, under manual control from the panel. First visible robot.*
*Estimate: 3–4 days.*

### Steps

1. **Wire and calibrate.** PCA9685 on I2C, pan on channel 0, tilt on channel 1, separate 5V supply, common ground. Write a standalone calibration script sweeping pulse widths, recording mechanical min/max, center, and pulse-to-angle mapping per servo. Store in `neo_bringup/config/servos.yaml`; put wiring and numbers in `docs/hardware.md`.
2. **`servo_driver` node.** Subscribes `/head/command`, publishes `/head/state` at 50 Hz. In order:
   - clamp to soft limits from config (never trust an upstream node),
   - slew-rate limit (max deg/s per axis) so a step command becomes a smooth move,
   - deadband to stop buzzing on micro-corrections,
   - watchdog: no command for 500 ms means hold position, not fall limp,
   - `/system/estop` freezes output immediately; `/head/center` returns to neutral,
   - on clean shutdown, center then stop driving.
3. **`head_behavior` node** (skeleton): a priority arbiter producing `/head/command` at 50 Hz. Priority: **estop > manual joystick > gesture overlay > gaze target > idle motion.** Crossfade between sources over ~300 ms rather than jumping. Phases 4 and 9 fill in gaze and idle.
4. **Wire the panel's virtual joystick** through `/joy` with the §2.4 staleness deadman. Add the **Head** tab: joystick, center button, live readout, visual indication when a limit is reached.
5. Tests: unit-test the limiter and arbiter with synthetic streams (step input, conflicting priorities, watchdog expiry, stale `/joy`). Integration test against a fake I2C bus so it runs in CI.

### Done when
You can drive the head from the panel on your phone, the head cannot be commanded past its mechanical limits, killing `head_behavior` leaves the head holding still, and closing the browser mid-drag stops it within 300 ms.

### Do not
Do not add emotion or gaze behavior here. Do not use `RPi.GPIO`. Do not add a USB gamepad node — `/joy` is contract enough if you want one later.

---

## Phase 4 — Perception: YOLO, gestures, and gaze engagement

*Goal: Neo knows a person is there, who is asking for its attention, and when to let go.*
*Estimate: 4 days. **Core landed early** — see 4.0.*

### 4.0 Status: built ahead of Phases 1 and 3

`neo_perception` is implemented and tested (64 tests), including gesture-driven
engagement, which was **not** in the original plan. Design decisions taken there:

* **One pose model, not two networks.** `yolov8n-pose` yields person boxes,
  gesture keypoints, and a face-anchored gaze target from a single pass. A
  dedicated hand model would roughly double per-frame cost on a Pi that only has
  ~4 fps to spend. Object detection stays a separate model, run **on demand**.
* **Engagement is a lock, not a follow.** Presence alone does not move the head;
  a held raised hand does. This is what stops the robot swivelling at everyone
  who walks past a corridor desk.
* **Two grace periods.** "Walked out of frame" (last box on a frame edge, 0.6 s)
  is distinguished from "the detector blinked" (open frame, 2.0 s). One grace
  value cannot serve both without the robot either staring at doorways or
  dropping its lock every missed frame.
* **Static gestures only, at Pi frame rates.** A 2 Hz wave sampled at 4 fps
  aliases; `RAISED_HAND` is the default because it survives.
* **Core is pure Python.** Tracking, gestures, engagement and gaze import no ROS,
  no OpenCV and no numpy, so they are tested with synthetic scenes and an
  explicit clock — and run today inside the admin panel's Vision tab.

Still outstanding for this phase: the ROS node wrapper (blocked on Phase 1
`neo_msgs`), on-demand object detection wiring, and the Bench B measurement on
real Pi hardware.

### 4.1 Original plan

### Steps

1. **`yolo_node`.** Loads the NCNN model from Bench B. Subscribes `/camera/image_raw`, publishes `/perception/detections`.
   - Inference in a worker thread; **drop frames rather than queue them** (depth 1, latest-wins). A backed-up queue makes gaze lag by seconds and look broken.
   - Target rate is a parameter defaulting to whatever Bench B supports (likely 3–5 Hz).
   - Class filter: persons always, plus a small object set for "what is that".
2. **`attention_tracker`.** Turns detections into a stable gaze target on `/perception/attention`:
   - IoU/centroid tracking with track ids and a few frames of hysteresis,
   - salient person = largest box (nearest) with a stickiness bonus for the currently attended track, so Neo doesn't ping-pong between two students,
   - box center to normalized [-1,1] camera coordinates; hold the last target ~1.5 s after loss so brief occlusion doesn't reset the head,
   - publish `person_present`, consumed by the wake word (§0.3, cross-modal gating) and the emotion node.
3. **Camera-to-head mapping.** A proportional controller on normalized error with a deadband is sufficient and stable for centering a face; you do not need camera intrinsics. Feed into `head_behavior` at gaze priority.
4. **Vision tab**: camera preview with boxes and the attention target, plus a confidence slider. This is your debugging tool for the rest of the project.
5. Tests: replay a recorded bag through the pipeline and assert detections and a stable target; unit-test tracker stickiness with synthetic detections.

### Done when
Neo tracks a person walking across the desk area smoothly at the benched rate without oscillation, and CPU stays inside budget with the audio stack also running.

### Do not
Do not add face recognition or identity. Do not chase fps with a bigger model.

---

## Phase 5 — Audio: wake word, endpointing, local ASR, TTS

*Goal: "Neo" wakes the robot; speech becomes text **without the laptop**; text becomes speech.*
*Estimate: 6–7 days (up from revision 1 — local ASR is now in scope).*

### 5.1 The gate

`/audio/in` flows continuously to exactly one consumer: `wake_word`. ASR is **not** subscribed to the mic in `IDLE`. On a wake event, `dialog_manager` opens a listening window and `asr_router` begins consuming; when the window closes, it stops. This is the CLAUDE.md invariant and it is also what keeps the Pi's CPU free.

### Steps

1. **Train a custom "Neo" wake model** with openWakeWord's synthetic-data flow: TTS positives across voices and accents, negatives from your own corridor recordings. A stock model won't have your word, and Indian-English pronunciation coverage matters.
2. **`wake_word` node.** Streams `/audio/in` through the model, publishes `/wake/event` above threshold. Cross-modal gating: threshold relaxes when `person_present`. Refractory period after firing. Publishes a low-rate score so the Audio tab can show a live meter for tuning.
3. **VAD / endpointing.** Silero VAD after wake: capture until ~800 ms trailing silence, hard cap ~10 s, minimum ~500 ms, with ~300 ms pre-roll so the first syllable isn't clipped.
4. **`asr_router` with two engines behind one interface:**
   - **Vosk on the Pi — always available, always the baseline.** Optionally grammar-constrained when the intent looks like a room code, which sharply improves "CS-204" style accuracy.
   - **Off-board Whisper — used when `/link/health` is up**, for open-domain questions where accuracy matters more than locality.
   - Selection is a policy in one place, published as `engine` on `/dialog/transcript` so you can tell which one produced a bad answer.
5. **`tts` node.** Piper, local. Publishes `/audio/out` chunks and a finished signal; streams sentence-by-sentence so Neo starts talking before the full reply is synthesized.
6. **Half-duplex barge-in guard.** Mute or gate the mic while `/dialog/state == SPEAKING`, or Neo will wake itself when a reply contains its own name. Echo cancellation is a later refinement; muting is the right first move.
7. **Audio tab**: live wake score meter with threshold slider, VAD segmentation view, engine indicator, TTS test box.
8. Tests: feed recorded WAVs through `/audio/in` and assert wake fires/doesn't; measure false accepts over an hour of ambient recording; assert ASR never subscribes while `IDLE`; assert the router falls back to Vosk when the link drops mid-utterance.

### Done when
Say "Neo" from 2 m in a noisy corridor and the state badge goes `IDLE → LISTENING`; stop speaking and it goes to `THINKING`; **with the laptop lid closed**, a spoken room code still transcribes correctly; false accepts under roughly one per hour of corridor noise; and it all works identically with the mic source set to `webapp`.

### Do not
Do not add speaker identification, multi-turn barge-in, or continuous listening.

---

## Phase 6 — Off-board service, secured dual-path client, degraded mode

*Goal: the optional brain, and graceful behavior for the many hours it is absent.*
*Estimate: 4–5 days.*

### Steps

1. **`offboard/serve.py` on the laptop.** FastAPI behind mTLS, fronting:
   - llama.cpp server (or Ollama) hosting Qwen2.5-3B-Instruct Q4_K_M, OpenAI-compatible with streaming,
   - faster-whisper at `/asr`,
   - `/health` reporting model-loaded state and GPU memory.
   Bind to the Ethernet and Wi-Fi interfaces only, require the Pi's client certificate, firewall the rest.
   **Daily-driver accommodations:** autostart at login, a tray toggle to disable it, and an idle unload timer so a 3B model isn't holding VRAM while you work. Document the cold-start cost measured in Bench F.
2. **`llm_client` node on the Pi** — the *only* node that talks to the laptop.
   - Ordered endpoints (Ethernet, then Wi-Fi) with continuous health probes every 2 s; publishes `/link/health` including `active_path`,
   - failover on timeout without failing the user-visible request where possible,
   - per-request timeouts (~1 s to first token, ~8 s total), retry once on connection error and no more — a receptionist that pauses 20 s is worse than one that admits failure,
   - streams partial text so TTS can start on the first complete sentence.
3. **Degraded mode as a first-class state.** When the link is down:
   - info-desk questions work **fully** — local ASR (Phase 5) plus local KB (Phase 7), no LLM in the path,
   - general-assistant questions get an honest reply ("I can't reach my language model right now, but I can still help you find rooms and blocks"),
   - the emotion layer reflects it (Phase 9) and the panel shows the link badge,
   - recovery is automatic and silent.
   Test this mode as often as the healthy one. It is where Neo will live.
4. **Prompt scaffolding** in versioned files under `neo_dialog/prompts/`, not string literals: persona, brevity constraints (spoken replies of 1–3 sentences), and the hard rule that campus facts come only from provided context.
5. **Dialog tab**: transcript, reply with `source` badge, latency breakdown, link path indicator.
6. Tests: run the client against a mock server that can be slow, fail mid-stream, present a bad certificate, or vanish; assert failover between paths, degraded transitions, recovery, and that no request exceeds its timeout budget.

### Done when
Close the laptop lid mid-conversation: Neo says something sensible within 2 seconds, still answers "where is CS-204" end to end, and recovers on its own when the laptop returns. Pull the Ethernet cable with the laptop awake: it fails over to Wi-Fi and the panel shows the path change.

### Do not
Do not run the LLM on the Pi. Do not let any other node open a connection to the laptop. Do not run the off-board service without mTLS, even briefly.

---

## Phase 7 — Campus knowledge base, built data-last

*Goal: the info-desk role — deterministic, honest about what it doesn't know yet, and easy to grow.*
*Estimate: 5–6 days of engineering, plus data collection running in the background from Phase 0 onward.*

Since data arrives incrementally, build **engine, validator, admin CRUD, and coverage-awareness first**, against a seed of ten rooms. The system should be correct and useful at 10 rooms and still correct at 500.

### 7.1 Data model (`data/campus/`)

```yaml
# rooms.yaml
- code: CS-204
  name: Computer Science Lab 2
  type: lab                # classroom | lab | office | facility
  block: B
  floor: 2
  wing: East
  department: CSE
  node: b2_j3              # nearest graph node
  aliases: ["cs 204", "cs lab 2", "second cs lab"]
  landmarks: ["opposite the lift", "next to the staff room"]

# graph.yaml — the campus as a walkable graph
nodes:
  - id: reception
    kind: entrance
    block: A
    floor: 0
  - id: b2_j3
    kind: junction
    block: B
    floor: 2
edges:
  - from: reception
    to: a0_corridor
    distance_m: 12
    instruction: "walk straight past the notice board"
  - from: b1_stairs
    to: b2_landing
    kind: stairs
    instruction: "take the stairs up one floor"

# coverage.yaml — what has been surveyed, so Neo can be honest
blocks:
  A: {floors: [0, 1], status: complete}
  B: {floors: [2], status: partial}
  C: {status: not_surveyed}
```

### 7.2 Retrieval pipeline (pure functions, no ROS, no vectors)

1. **Normalize**: lowercase, expand spoken forms ("cee ess two oh four" → "cs 204"), strip filler.
2. **Extract a room code** by regex (`[A-Za-z]{1,4}[-\s]?\d{2,4}`). Exact match, then alias, then fuzzy via `rapidfuzz` above a confidence floor.
3. **Classify the ask**: *location*, *directions*, *listing* ("which labs are in B block"), *attribute* ("which floor is X on").
4. **Directions**: Dijkstra from the origin (default: the reception node where Neo stands) to the destination, rendered through instruction templates — turn-by-turn with landmarks and floor changes.
5. **Render**. Because the LLM is usually unavailable (§0.3.3), the **template renderer is the primary output path** and must sound like a person, not a routing table. Invest here. When the link *is* up, the LLM polishes phrasing under a strict instruction to change no facts.
6. **Coverage-aware misses.** Three distinct outcomes, never blurred:
   - room known → answer,
   - room unknown but its block is surveyed → "I don't have CS-204 in my directory",
   - block not surveyed → "I haven't learned C block yet",
   - multiple candidates → clarifying question ("CS-204 in B block, or CS-240 in C block?").
   Never an invented room.

### 7.3 Steps

1. Implement `neo_kb` as pure Python with a CLI: `python -m neo_kb.query "how do I get to CS-204"`. Iterate here; it is a hundred times faster than testing through the robot.
2. Write `data/campus/validate.py`: schema validation, unique codes, resolvable `node` references, graph connectivity (no unreachable rooms), alias collisions, coverage consistency. Wire into CI — bad campus data should fail the build, not the demo.
3. **Data tab in the panel**: CRUD for rooms, aliases, and graph edges; inline validation on save; a coverage view showing which blocks/floors are done. Writes YAML atomically; `kb_service` watches the files and hot-reloads without a restart. Back up or git-commit after every save — this dataset is the project's most valuable asset and it lives on an SD card.
4. **Unanswered-question log.** Every query that misses is recorded with its transcript and timestamp, surfaced in the Data tab as a work queue. This turns real usage into your data-collection backlog, which matters a great deal when data arrives over months.
5. Wrap as `kb_service` (ROS service returning answer text, confidence, `in_domain`) and publish `/kb/coverage`.
6. **`intent_router`** in `neo_dialog`: rules first (room-code regex, location keywords) to `kb_service`; everything else to the LLM path; only ambiguous cases consult the LLM for classification. Deterministic-first is cheaper and far more predictable — and it is the only option when the laptop is away.
7. **Evaluation set** that grows with the data: start at ~30 questions over the seed rooms, target 100+, including misheard codes, partial names, nonexistent rooms, and unsurveyed blocks. Run as pytest reporting accuracy; it is your regression net when tuning prompts and aliases.

### Done when
With ten rooms of seed data and the laptop closed, Neo answers questions about those ten rooms correctly and says something honest about everything else; adding a room through the panel takes under a minute and needs no restart.

### Do not
Do not add embeddings, a vector store, or chunk-and-similarity search. If retrieval seems weak, fix the structured data, the aliases, or normalization — and raise it before changing the approach.
Do not block this phase on complete campus data. The point of the design is that it works while the data grows.

---

## Phase 8 — Dialog manager

*Goal: one explicit state machine owning the conversation.*
*Estimate: 3–4 days.*

### Steps

1. **`dialog_manager`** implements the state machine over `/dialog/state`: `IDLE → (wake) → LISTENING → (endpoint) → THINKING → SPEAKING → IDLE`, with `DEGRADED` as a modifier and `ESTOP` as an override. Every transition logged with timing.
2. **Timeouts everywhere.** Listening cap, thinking timeout with a graceful fallback line, speaking watchdog. No state may be entered without a defined exit.
3. **Follow-up window.** Stay receptive ~6 s after speaking so "and where's the canteen?" works without repeating the wake word. Show it clearly in the panel; drop to fully-gated `IDLE` on timeout.
4. **Conversation context**: last 2–3 turns for the LLM path only. Keep it short — a 3B model and a 4GB Pi both benefit, and receptionist exchanges are short anyway.
5. **Filler while thinking.** If no reply has started within ~700 ms, emit a short acknowledgement ("let me check"). Cheap, and it transforms perceived responsiveness — especially on the cold-start path after the laptop's idle unload.
6. Tests: `launch_testing` drives fake wake events and transcripts through the full stack with mocked LLM and audio, asserting state sequences and timing bounds, in both healthy and degraded link states.

### Done when
A full exchange works end to end in the `dev` profile with only a browser and no Pi hardware, and the same launch file with `profile:=hardware` runs it on the robot.

---

## Phase 9 — Emotion layer

*Goal: the head reads as alive and its motion reflects state. A modifier on motion, never a second actuator path.*
*Estimate: 4 days.*

### 9.1 Model

`EmotionState = {label, intensity ∈ [0,1], params}` where params modulate motion:

| Param | Effect |
|---|---|
| `idle_amplitude_deg` | size of idle drift |
| `idle_freq_hz` | speed of idle motion |
| `gaze_gain`, `gaze_lag` | how eagerly and how smoothly it snaps to a person |
| `tilt_bias_deg` | resting head tilt (curiosity reads as a tilt) |
| `micro_motion` | small high-frequency movement; its absence reads as "asleep" |
| `settle_time` | how long it holds still after arriving |

Labels: `NEUTRAL, ATTENTIVE, HAPPY, CURIOUS, CONFUSED, SLEEPY`.

### Steps

1. **`emotion_node`** — driven by observable events, not vibes: person appears → `CURIOUS` then `ATTENTIVE`; wake fires → brief `HAPPY`; KB miss or ASR failure → `CONFUSED`; no person for N minutes → `SLEEPY`; link down → subdued `NEUTRAL`. Minimum dwell times so it cannot flicker.
2. **Blending in `head_behavior`** (extending Phase 3): final command = gaze target (filtered by `gaze_gain`/`gaze_lag`) + idle oscillation (two incommensurate sines, or 1-D Perlin noise, scaled by `idle_amplitude_deg`) + `tilt_bias_deg`. Manual joystick still preempts everything below estop.
3. **Gestures as bounded overlays**: `nod`, `shake`, `tilt`, `scan` — short time-parameterized trajectories added on top of the base pose, triggered by dialog events (nod on understanding, shake on "I don't have that room"). Additive and time-limited; never a mode the head can get stuck in.
4. **Emotion tab**: current state, manual override, gesture trigger buttons, live param sliders. Tune by eye with the joystick idle — this phase is judged by watching, not by tests. The failure mode is motion that looks mechanical, or worse, twitchy.
5. Tests: assert transitions from synthetic event sequences; property-test that blended output never exceeds servo limits or rate caps for *any* random emotion params.

### Done when
An idle Neo looks alive but calm, notices someone approaching, and visibly differs between a successful answer and "I don't have that room" — while every Phase 3 safety limit still holds.

### Do not
Do not give `emotion_node` its own path to `servo_driver`. Do not add a face or screen.

---

## Phase 10 — Integration, tuning, field test, deployment

*Goal: it survives contact with actual students.*
*Estimate: 6–8 days.*

### Steps

1. **Launch profiles** in `neo_bringup`: `dev` (all webapp sources), `hardware` (hardware sources + admin API, media bridge on demand), `hybrid` (hardware camera + webapp mic, for audio debugging), `bench` (no servos).
2. **Measure the real latency chain** with timestamped events in one bag: wake → endpoint → ASR → retrieval/LLM → first audio, **for both the degraded and healthy paths** (the degraded path is the one most users will experience). Target under ~3 s for a KB answer. Attack whichever segment dominates; do not optimize blind.
3. **Resource hardening**: RSS and CPU over an hour of continuous operation; watch for leaks in the image pipeline (the usual suspect); confirm no thermal throttling with the case closed and the admin API running.
4. **Failure drills.** Run each, fix what breaks: laptop asleep; Ethernet pulled; Wi-Fi dropped; both dropped; servo power removed; camera unplugged mid-run; SD card full; browser closed mid-joystick; two people talking at once; someone shouting "Neo" repeatedly; expired certificate.
5. **Deployment**: systemd units for the ROS stack and the admin API, `Restart=on-failure`, journald with rotation, recovery from power loss with no keyboard attached. Measure power-on-to-ready.
6. **Logs tab**: journal tail, bag recording toggle, download — so field failures are reproducible offline.
7. **`docs/operations.md`**: start/stop, reading the state badge, what degraded mode looks like, pulling logs and bags, rotating certificates, and **how to add campus data without touching code** — the one procedure a non-programmer colleague may need.
8. **Field test at the desk.** Sit with it for a couple of hours. Record every failure verbatim; the misheard questions become your alias backlog and your wake-tuning data, and the unanswered-question log (§7.3) collects them automatically.

### Done when
Neo runs unattended for a full afternoon at the desk — with the laptop closed for part of it — answers real questions from people who have never used it, and every failure that afternoon is either fixed or written down as a known issue.

---

## Phase 11 — Keeping the door open for a body

*Goal: satisfy CLAUDE.md's "not a rewrite" constraint without building locomotion.*
*Estimate: 1 day, mostly done inside Phase 3 and revisited here.*

- Name motion interfaces for the *head*, not the robot: `/head/command`, not `/cmd`. A future `/base/cmd_vel` sits alongside it.
- Keep the arbiter generic over "motion sources" so a navigation source can be inserted at a priority level later.
- Use proper frames from the start (`base_link` → `head_pan` → `head_tilt` → `camera`) and publish TF even though nothing consumes it yet. Retrofitting TF later is the expensive version.
- Leave PCA9685 channels 2–15 unassigned and documented.
- **Do not** add `nav2`, odometry, or a drive-train URDF.

---

## 12. Cross-cutting budgets

### 12.1 CPU / RAM (4 cores, 4GB) — *estimates; replace with Phase 0 measurements*

| Component | CPU (of 4 cores) | RAM |
|---|---|---|
| YOLOv8n NCNN @ 320, 3–5 Hz | ~1.5 | ~250 MB |
| Camera capture + ROS transport | ~0.4 | ~150 MB |
| Wake word (continuous) | ~0.2 | ~120 MB |
| **Vosk ASR (during listening only)** | ~0.6 burst | ~120 MB |
| Piper TTS (burst while speaking) | ~1.0 peak | ~200 MB |
| Servo + behavior + emotion @ 50 Hz | ~0.2 | ~80 MB |
| ROS 2 middleware | ~0.3 | ~200 MB |
| **Admin API (always on)** | ~0.15 | ~120 MB |
| **Media bridge (only when in use)** | ~0.4 | ~180 MB |

The always-on set must fit comfortably; the media bridge is the piece that must stay on demand. Note that ASR and TTS bursts never overlap (half-duplex), which is what makes the peak survivable.

### 12.2 Latency budget, wake to first audio (targets)

| Segment | Degraded (laptop away) | Healthy |
|---|---|---|
| Wake detection | 300 ms | 300 ms |
| Endpoint (trailing silence) | 800 ms | 800 ms |
| ASR | 400 ms (Vosk, local) | 600 ms (Whisper round trip) |
| KB retrieval | < 50 ms | < 50 ms |
| Reply generation | ~0 (template) | 400 ms (LLM first token) |
| TTS first audio | 500 ms | 500 ms |
| **Total** | **~2.1 s** | **~2.7 s** |

The degraded path being *faster* is not an accident — it is the case to optimize for.

---

## 13. Risk register

| Risk | Impact | Mitigation |
|---|---|---|
| **PiCam on Ubuntu 24.04 fights libcamera** | Blocks Phase 4 | Bench A with a one-day timebox and a written decision rule (USB webcam, or Pi OS + Docker) |
| **Campus Wi-Fi AP isolation** | Second link path doesn't exist | One `curl` in Phase 0 step 4, before any failover code |
| Laptop unavailable more than expected | Most sessions degraded | Local ASR + local KB + strong templates; degraded mode tested as the primary path |
| YOLO too slow on Pi 4 | Gaze feels dead | Frame-skip to 2–3 Hz, 320px; last resort is offloading detection (contradicts CLAUDE.md — raise it first) |
| "Neo" is short → false wakes | Robot talks to nobody | Custom-trained model, person gating, refractory period, tuned threshold; "Hey Neo" as fallback phrasing |
| Self-triggering on its own TTS | Loops | Mic muted while `SPEAKING`; AEC later if needed |
| Admin panel exposed on campus network | Anyone drives the robot / edits data | HTTPS + argon2 login + rate limiting from Phase 2, not later |
| Off-board service exposed | Open LLM proxy on the network | mTLS + interface binding + firewall, from the first commit |
| Servo brownout resetting the Pi | Random reboots | Separate 5V supply, common ground, current headroom |
| Thermal throttling | Everything slows at once | Active cooling; temperature on the System tab |
| Ribbon cable fatigue on a panning head | Camera dies intermittently | Plan the cable route and strain relief in Phase 0 |
| **Campus data lost with the SD card** | The project's most valuable asset | Git-commit or back up `data/campus/` on every panel save |
| Sparse data early on | Neo looks useless in demos | Coverage-aware honesty; demo the surveyed block |
| Scope creep to full body | Nothing ships | Phase 11 fence; CLAUDE.md future-scope rule |

---

## 14. Milestone summary

| Phase | Deliverable | Est. |
|---|---|---|
| 0 | Hardware, OS, both link paths, six benchmarks | 3–4 d |
| 1 | Workspace, frozen contracts, private CA, CI | 2–3 d |
| 2 | Source selection + admin panel core (auth, TLS, sources, system) | 7–9 d |
| 3 | Head moves safely under virtual joystick control | 3–4 d |
| 4 | Person detection and gaze tracking | 4 d |
| 5 | Wake word, endpointing, **local ASR**, TTS | 6–7 d |
| 6 | Secured dual-path off-board client + degraded mode | 4–5 d |
| 7 | KB engine, validator, data CRUD, coverage, eval suite | 5–6 d |
| 8 | Dialog manager, end-to-end conversation | 3–4 d |
| 9 | Emotion-modulated motion | 4 d |
| 10 | Integration, field test, deployment | 6–8 d |
| 11 | Locomotion-ready interfaces (no locomotion) | 1 d |

**Total: roughly 10–12 focused weeks solo** — up from revision 1, mostly from the admin panel and local ASR. Campus data collection runs in parallel throughout and is not on this critical path, by design.

### Demo checkpoints

- **After Phase 3:** the head moves, driven from your phone.
- **After Phase 5:** it wakes to its name and repeats back what it heard — with the laptop closed.
- **After Phase 7:** it answers "where is CS-204" correctly, entirely offline.
- **After Phase 9:** it looks alive and reacts to you approaching.
