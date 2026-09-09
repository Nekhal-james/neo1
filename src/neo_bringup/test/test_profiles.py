"""Profile validation, with no ROS installed.

The launch file resolves nodes from data, so a typo in a profile YAML does not
fail at launch time -- it fails as a node that quietly never starts, which on a
robot reads as "the microphone is broken". These checks turn that into a test
failure instead.

Deliberately parses launch/neo.launch.py as text rather than importing it: the
`launch` and `launch_ros` packages only exist inside a ROS install, and this
test has to run in the pip-only CI job too.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

PKG = Path(__file__).resolve().parents[1]
CONFIG = PKG / "config"
LAUNCH = PKG / "launch" / "neo.launch.py"

PROFILE_NAMES = ("dev", "hardware", "hybrid", "bench")
STREAMS = ("camera", "mic", "speaker")
BACKENDS = ("hardware", "webapp")

# stream -> the lifecycle node that provides its `hardware` backend
HW_NODE = {"camera": "camera_hw", "mic": "mic_hw", "speaker": "speaker_hw"}


def load(name: str) -> dict:
    return yaml.safe_load((CONFIG / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def registry() -> dict:
    return load("nodes.yaml")["nodes"]


@pytest.fixture(scope="module", params=PROFILE_NAMES)
def profile(request) -> tuple[str, dict]:
    return request.param, load(f"{request.param}.yaml")


def test_launch_file_knows_exactly_these_profiles():
    """The launch argument's `choices` is what rejects a typo at the command
    line, so it has to list every profile and no others."""
    tree = ast.parse(LAUNCH.read_text(encoding="utf-8"))
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PROFILES" for t in node.targets
        ):
            found = ast.literal_eval(node.value)
    assert found is not None, "neo.launch.py no longer defines PROFILES"
    assert set(found) == set(PROFILE_NAMES)


def test_every_profile_file_exists():
    on_disk = {p.stem for p in CONFIG.glob("*.yaml")} - {"nodes"}
    assert on_disk == set(PROFILE_NAMES)


def test_registry_entries_are_complete(registry):
    for name, spec in registry.items():
        assert set(spec) == {"package", "executable", "phase", "description"}, name
        assert isinstance(spec["phase"], int) and 1 <= spec["phase"] <= 11, name
        assert spec["description"].endswith("."), f"{name}: description reads as prose"


def test_profile_names_match_their_filenames(profile):
    name, data = profile
    assert data["profile"] == name
    assert data["description"]


def test_sources_are_complete_and_valid(profile):
    name, data = profile
    assert set(data["sources"]) == set(STREAMS), name
    for stream, backend in data["sources"].items():
        assert backend in BACKENDS, f"{name}: {stream}={backend}"


def test_every_node_is_decided_in_every_profile(profile, registry):
    """Exact equality, not a subset: a node added to the registry must be
    switched on or off deliberately in each profile. An implicit default is how
    a node ends up running on the robot because nobody said otherwise."""
    name, data = profile
    assert set(data["nodes"]) == set(registry), (
        f"{name}.yaml is out of sync with nodes.yaml: "
        f"missing={sorted(set(registry) - set(data['nodes']))} "
        f"unknown={sorted(set(data['nodes']) - set(registry))}"
    )
    for node_name, enabled in data["nodes"].items():
        assert isinstance(enabled, bool), f"{name}: {node_name} is not a bool"


def test_hardware_sources_start_their_backend_node(profile):
    """A profile selecting the `hardware` backend without starting that
    backend's node is a stream that silently never produces a frame."""
    name, data = profile
    for stream, backend in data["sources"].items():
        node = HW_NODE[stream]
        if backend == "hardware":
            assert data["nodes"][node], (
                f"{name}: sources.{stream}=hardware but nodes.{node} is off"
            )


def test_webapp_sources_do_not_burn_a_core_on_an_unused_backend(profile):
    """The mirror image, and the reason it matters: on a 4-core Pi an idle
    camera pipeline still costs a core, so deactivation has to be real
    (plan 2.1)."""
    name, data = profile
    for stream, backend in data["sources"].items():
        node = HW_NODE[stream]
        if backend == "webapp":
            assert not data["nodes"][node], (
                f"{name}: sources.{stream}=webapp but nodes.{node} is still on"
            )


def test_servo_driver_never_runs_without_an_arbiter(profile):
    """servo_driver subscribes /head/command; head_behavior is the only thing
    that publishes it. Enabling the driver alone gives a head that holds
    position on its watchdog forever and looks dead."""
    name, data = profile
    if data["nodes"]["servo_driver"]:
        assert data["nodes"]["head_behavior"], (
            f"{name}: servo_driver without head_behavior -- nothing would command it"
        )


def test_bench_profile_measures_perception_alone():
    """The bench profile exists to produce a number for one subsystem. Anything
    else running contaminates it (plan Phase 0, Bench B)."""
    data = load("bench.yaml")
    assert data["nodes"]["perception"] and data["nodes"]["camera_hw"]
    for noisy in ("wake_word", "asr_router", "tts", "servo_driver", "dialog", "emotion"):
        assert not data["nodes"][noisy], f"bench profile also starts {noisy}"


def test_dev_profile_needs_no_hardware_at_all():
    """dev is the profile that runs on a laptop with no Pi attached."""
    data = load("dev.yaml")
    assert all(b == "webapp" for b in data["sources"].values())
    for hw in (*HW_NODE.values(), "servo_driver"):
        assert not data["nodes"][hw], f"dev profile starts {hw}"
