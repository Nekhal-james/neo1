# neo_msgs

The frozen interface contracts. Interfaces only — no logic, no runtime
dependencies beyond `std_msgs` for `Header`.

This package is the leaf every other package depends on, which is why it is kept
this bare: a dependency added here is a dependency added to the whole robot, and
a field changed here has to be changed everywhere at once. The plan calls getting
these wrong "the expensive mistake", so they were defined in one pass against the
node map rather than grown one node at a time.

## What is here

| Interface | Topic / service | Notes |
|---|---|---|
| `AttentionTarget` | `/perception/attention` | Normalised [-1, 1], **y positive up** — the joystick's convention, so both feed the head the same way |
| `GestureEvent` | `/perception/gestures` | Static poses only; a wave aliases at the Pi's ~4 fps |
| `AudioChunk` | `/audio/in`, `/audio/out` | Carries its own rate: 16 kHz in, 22.05 kHz out |
| `WakeEvent` | `/wake/event` | Records the threshold *applied*, which differs from the configured one when a person is in frame |
| `DialogState` | `/dialog/state` | Four states; `degraded` and `estop` are flags — see below |
| `Transcript` | `/dialog/transcript` | Which engine produced it, and whether a grammar was in force |
| `Reply` | `/dialog/reply` | `SOURCE_KB` is the primary path, not a fallback |
| `EmotionState` | `/emotion/state` | Motion parameters, no pan/tilt — a modifier, never an actuator path |
| `HeadCommand` | `/head/command` | Radians. Priorities numbered with gaps so a source can be inserted later |
| `LinkHealth` | `/link/health` | `active_path` ∈ eth/wifi/none |
| `KbCoverage`, `BlockCoverage` | `/kb/coverage` | What has been surveyed, so misses can be honest |
| `SourceState` | `/sources/state` | The whole vocabulary of the hardware/webapp seam |
| `SetSource` | `/sources/set` | Transactional: activates, waits, switches, deactivates — and rolls back |
| `QueryKb` | `/kb/query` | Four outcomes, kept distinct so a miss is never an invented room |

`/head/state` is a `sensor_msgs/JointState`, `/joy` a `sensor_msgs/Joy`, and
`/perception/detections` a `vision_msgs/Detection2DArray`. Standard types are
used directly rather than wrapped.

## The one deliberate deviation

The plan's §1.2 lists `DialogState` as a flat enum including `DEGRADED` and
`ESTOP`. Its §8.1 calls them a modifier and an override, and that reading is the
correct one: the off-board laptop is usually away, so Neo runs the whole
`IDLE → LISTENING → THINKING → SPEAKING` cycle *while degraded* — answering room
questions entirely locally. A flat enum cannot express "listening, with the link
down", which is the state this robot spends most of its life in. So there are
four states and two orthogonal flags; the panel flattens them back to one badge
for display.

## Testing

`test/test_contract.py` runs as plain pytest with no ROS install:

```bash
cd src/neo_msgs && pytest -q
```

It checks the interface set is complete and listed in `CMakeLists.txt`, that no
logic or extra dependency has crept in, and — the part that matters — that these
definitions have not drifted from the pure-Python mirror dataclasses in
`neo_webapp.bridge.types` and `neo_perception.types`. Those mirrors are what the
admin panel runs on today with no ROS installed. Nothing else in the repo would
notice if the two diverged.

`colcon test` runs the same file, plus the real rosidl validation that only a
ROS build can do.
