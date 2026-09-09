# neo_bringup

Launch files and profiles. No importable module — this package installs data.

```bash
ros2 launch neo_bringup neo.launch.py profile:=dev
```

## Profiles

| Profile | Camera | Mic / speaker | Servos | For |
|---|---|---|---|---|
| `dev` | webapp | webapp | no | A laptop with no robot attached |
| `hardware` | Pi camera | Pi USB mic / speaker | yes | The robot at the desk |
| `hybrid` | Pi camera | webapp | yes | Debugging audio without shouting at the desk |
| `bench` | Pi camera | — | no | Measuring perception with nothing else competing for the cores |

## How it decides what to start

Which nodes run is **data, not code**: `config/<profile>.yaml` switches nodes on
and off, and `config/nodes.yaml` is the registry saying where each one lives and
which plan phase makes it real. A phase that adds a node edits YAML; it does not
edit the launch file.

A node whose package is not built yet is skipped with a log line naming its
phase, rather than failing the whole bringup. That is what lets one launch file
serve the project from Phase 1 through Phase 10 instead of being unusable until
the last node is written.

Two things it deliberately does not start:

- **The admin panel.** `neo --webapp up` is a separate process, often on a
  different machine, sharing state through the JSON files under `var/`. Starting
  it from here would make the panel die with the robot — precisely when you want
  it alive.
- **Anything above the current phase**, as above.

## Testing

```bash
cd src/neo_bringup && pytest -q
```

Plain pytest over the YAML, no ROS needed. Beyond schema checks it enforces the
consistency rules a typo would otherwise turn into a silently dead stream:

- every node in the registry is switched on or off *explicitly* in every profile
  — no implicit default, because that is how a node ends up running on the robot
  because nobody said otherwise;
- a stream set to `hardware` must start that backend's node, and a stream set to
  `webapp` must not (an idle camera pipeline still costs a core);
- `servo_driver` never runs without `head_behavior`, or nothing publishes
  `/head/command` and the head holds on its watchdog looking dead.

The first version of `bench.yaml` failed that second rule, which is roughly the
point of having it.
