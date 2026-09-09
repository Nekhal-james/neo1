"""Bring Neo up under one of the profiles in config/.

    ros2 launch neo_bringup neo.launch.py profile:=dev

One launch file serves the whole project. Which nodes it starts is data
(config/<profile>.yaml against config/nodes.yaml), not code, so a phase that
adds a node edits YAML rather than this file.

Two things it deliberately does NOT start:

* **The admin panel.** `neo --webapp up` is a separate process, started in any
  order and often on a different machine, sharing state through the JSON files
  under var/ (CLAUDE.md). Launching it from here would contradict that -- and
  would make the panel die with the robot, which is precisely when you want it.
* **Nodes from a phase that has not landed.** They are listed in nodes.yaml
  from the start so profiles can reference them, and skipped with a log line
  until their package exists. Failing the whole bringup because Phase 9 is not
  written yet would make this file unusable until the project was finished.
"""

from pathlib import Path

import yaml
from ament_index_python.packages import (
    PackageNotFoundError,
    get_package_share_directory,
)
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, LogInfo, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

PROFILES = ("dev", "hardware", "hybrid", "bench")


def _config_dir() -> Path:
    return Path(get_package_share_directory("neo_bringup")) / "config"


def _load(name: str) -> dict:
    path = _config_dir() / name
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _setup(context, *args, **kwargs):
    profile_name = LaunchConfiguration("profile").perform(context)
    profile = _load(f"{profile_name}.yaml")
    registry = _load("nodes.yaml")["nodes"]

    sources = profile["sources"]
    actions = [
        LogInfo(msg=f"[neo_bringup] profile '{profile_name}': {profile['description']}"),
        LogInfo(
            msg="[neo_bringup] sources: "
            + ", ".join(f"{k}={v}" for k, v in sorted(sources.items()))
        ),
    ]

    for name, enabled in sorted(profile["nodes"].items()):
        if not enabled:
            continue
        spec = registry[name]

        try:
            get_package_share_directory(spec["package"])
        except PackageNotFoundError:
            # Expected for any node above the current phase. A log line rather
            # than a failure, so an unfinished project still launches.
            actions.append(
                LogInfo(
                    msg=f"[neo_bringup] skipping '{name}': package "
                    f"{spec['package']} not built (plan phase {spec['phase']})"
                )
            )
            continue

        parameters = [{"profile": profile_name}]
        if name == "source_manager":
            # Only the manager needs these: everything downstream of a mux is
            # forbidden to know which backend is live (plan 1.2).
            parameters.append({f"initial_{k}": v for k, v in sources.items()})

        actions.append(
            Node(
                package=spec["package"],
                executable=spec["executable"],
                name=name,
                output="screen",
                parameters=parameters,
            )
        )

    return actions


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "profile",
                default_value="dev",
                choices=list(PROFILES),
                description="Which config/<profile>.yaml to bring up.",
            ),
            OpaqueFunction(function=_setup),
        ]
    )
