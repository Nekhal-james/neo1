"""The frozen-contract regression net.

Plain pytest over the `.msg`/`.srv` *text*: no rosidl, no rclpy, no built
workspace. That is deliberate. These contracts are the one thing in the project
every package depends on, and the plan calls getting them wrong "the expensive
mistake" -- so the check for them has to run everywhere, including on a laptop
with no Jazzy install and in the pip-only CI job.

Two jobs:

1. **Completeness.** Every interface named in docs/IMPLEMENTATION_PLAN.md
   section 1.2 exists, is listed in CMakeLists.txt, and parses.
2. **Drift.** Four packages already ship pure-Python dataclasses that mirror
   these contracts (`neo_webapp.bridge.types`, `neo_perception.types`) and are
   what the admin panel runs on today with no ROS installed. Those mirrors and
   these messages must not diverge. Nothing else in the repo would notice if
   they did -- they are only compared here.
"""

from __future__ import annotations

import re
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

PKG = Path(__file__).resolve().parents[1]
MSG_DIR = PKG / "msg"
SRV_DIR = PKG / "srv"

PRIMITIVES = {
    "bool", "byte", "char",
    "float32", "float64",
    "int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64",
    "string", "wstring",
}

# Interfaces from plan section 1.2, plus the three the plan implies elsewhere:
# GestureEvent (named by neo_perception/nodes/perception_node.py's wiring plan),
# SourceState (published on /sources/state, section 2.4), and QueryKb (the
# kb_service contract, section 7.3).
EXPECTED_MSGS = {
    "AttentionTarget", "AudioChunk", "BlockCoverage", "DialogState",
    "EmotionState", "GestureEvent", "HeadCommand", "KbCoverage",
    "LinkHealth", "Reply", "SourceState", "Transcript", "WakeEvent",
}
EXPECTED_SRVS = {"QueryKb", "SetSource"}


# --------------------------------------------------------------------------
# A minimal .msg/.srv parser. Enough of the IDL to check names and types --
# not a substitute for rosidl, which does the real validation at build time.
# --------------------------------------------------------------------------

_CONST_RE = re.compile(r"^(?P<type>\S+)\s+(?P<name>[A-Z][A-Z0-9_]*)\s*=\s*(?P<value>.+)$")
_FIELD_RE = re.compile(r"^(?P<type>\S+)\s+(?P<name>[a-z][a-z0-9_]*)\s*(?P<default>.*)$")


def parse(text: str) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Return ([(type, field_name), ...], {CONSTANT_NAME: value}).

    Constants are matched before fields because the two productions are
    ambiguous on shape alone; ROS distinguishes them by the `=`, and by
    convention constants are upper-case and fields lower-case.
    """
    field_list: list[tuple[str, str]] = []
    constants: dict[str, str] = {}

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if (m := _CONST_RE.match(line)) and "=" in line:
            constants[m.group("name")] = m.group("value").strip()
            continue
        if m := _FIELD_RE.match(line):
            field_list.append((m.group("type"), m.group("name")))
            continue
        pytest.fail(f"unparseable interface line: {raw!r}")

    return field_list, constants


def base_type(t: str) -> str:
    """Strip any array suffix: `int32[]`, `uint8[4]`, `string[<=10]`."""
    return re.sub(r"\[[^\]]*\]$", "", t)


def load(path: Path) -> tuple[list[tuple[str, str]], dict[str, str]]:
    return parse(path.read_text(encoding="utf-8"))


def load_srv(path: Path) -> tuple[tuple, tuple]:
    """Services split on a line that is exactly `---`."""
    text = path.read_text(encoding="utf-8")
    parts = text.split("\n---\n")
    assert len(parts) == 2, f"{path.name}: expected exactly one '---' separator"
    return parse(parts[0]), parse(parts[1])


def msg(name: str):
    return load(MSG_DIR / f"{name}.msg")


def msg_fields(name: str) -> list[str]:
    return [f for _, f in msg(name)[0]]


def msg_consts(name: str) -> dict[str, str]:
    return msg(name)[1]


# --------------------------------------------------------------------------
# 1. Completeness and structure
# --------------------------------------------------------------------------

def test_expected_interfaces_exist_and_nothing_extra():
    on_disk_msgs = {p.stem for p in MSG_DIR.glob("*.msg")}
    on_disk_srvs = {p.stem for p in SRV_DIR.glob("*.srv")}

    assert on_disk_msgs == EXPECTED_MSGS
    assert on_disk_srvs == EXPECTED_SRVS


def test_cmakelists_lists_every_interface():
    """A file on disk that CMakeLists forgets is not generated, and the failure
    surfaces as a confusing ImportError in some node months later."""
    cmake = (PKG / "CMakeLists.txt").read_text(encoding="utf-8")
    for name in EXPECTED_MSGS:
        assert f'"msg/{name}.msg"' in cmake, f"msg/{name}.msg missing from CMakeLists.txt"
    for name in EXPECTED_SRVS:
        assert f'"srv/{name}.srv"' in cmake, f"srv/{name}.srv missing from CMakeLists.txt"

    listed = set(re.findall(r'"(?:msg|srv)/(\w+)\.(?:msg|srv)"', cmake))
    assert listed == EXPECTED_MSGS | EXPECTED_SRVS


@pytest.mark.parametrize("name", sorted(EXPECTED_MSGS))
def test_msg_field_types_resolve(name):
    field_list, _ = msg(name)
    assert field_list, f"{name}.msg declares no fields"
    for ftype, _fname in field_list:
        base = base_type(ftype)
        if base in PRIMITIVES:
            continue
        assert "/" in base, f"{name}.msg: unqualified non-primitive type {ftype!r}"
        pkg, typename = base.split("/", 1)
        assert pkg in {"std_msgs", "builtin_interfaces", "neo_msgs"}, (
            f"{name}.msg depends on {pkg}, which is not a declared dependency "
            "of neo_msgs -- see package.xml"
        )
        if pkg == "neo_msgs":
            assert typename in EXPECTED_MSGS, f"{name}.msg references unknown {ftype}"


@pytest.mark.parametrize("name", sorted(EXPECTED_SRVS))
def test_srv_halves_parse(name):
    (req_fields, _), (resp_fields, _) = load_srv(SRV_DIR / f"{name}.srv")
    assert req_fields, f"{name}.srv has an empty request"
    assert resp_fields, f"{name}.srv has an empty response"


def test_package_ships_no_logic():
    """Plan Phase 1: "Do not put logic in neo_msgs."

    Nothing importable may live here -- if it did, every package in the robot
    would inherit it, which is exactly what an interfaces-only package exists to
    prevent. The test tree is not shipped and is exempt.
    """
    stray = [
        p for p in PKG.rglob("*.py")
        if p.parent != PKG / "test"
    ]
    assert stray == [], f"neo_msgs must contain no Python modules, found: {stray}"


def test_no_dependency_creep_in_package_xml():
    pkg_xml = (PKG / "package.xml").read_text(encoding="utf-8")
    depends = set(re.findall(r"<depend>([^<]+)</depend>", pkg_xml))
    assert depends == {"std_msgs"}, (
        "neo_msgs is the leaf every package depends on; a dependency added here "
        f"is added everywhere. Found: {sorted(depends)}"
    )


# --------------------------------------------------------------------------
# 2. Specific invariants worth pinning
# --------------------------------------------------------------------------

def test_head_command_is_radians():
    """The whole motion path is radians; only the panel converts (see the
    comment in HeadCommand.msg). Someone "tidying" this into degrees to match
    the panel's HeadState view would silently rescale every servo command."""
    names = msg_fields("HeadCommand")
    assert "pan_rad" in names and "tilt_rad" in names
    assert not any(n.endswith("_deg") for n in names)


def test_head_command_priorities_are_ordered_with_gaps():
    consts = {k: int(v) for k, v in msg_consts("HeadCommand").items() if k.startswith("PRIORITY_")}
    order = ["PRIORITY_IDLE", "PRIORITY_GAZE", "PRIORITY_GESTURE",
             "PRIORITY_MANUAL", "PRIORITY_ESTOP"]
    assert list(consts) == order or set(consts) == set(order)

    values = [consts[k] for k in order]
    assert values == sorted(values), f"arbiter priorities out of order: {values}"
    # Gaps, so a future source (e.g. navigation, plan 11) can be inserted
    # without renumbering levels already deployed.
    assert all(b - a >= 2 for a, b in zip(values, values[1:]))


def test_dialog_state_keeps_degraded_and_estop_orthogonal():
    """Degraded is the *normal* operating state (plan 0.3.3): Neo runs the full
    cycle with the laptop away. A flat enum could not express "listening, with
    the link down", so these are flags, not states -- see DialogState.msg."""
    names, consts = msg("DialogState")
    field_names = [f for _, f in names]

    assert "degraded" in field_names and "estop" in field_names
    state_consts = {k for k in consts if k in
                    {"IDLE", "LISTENING", "THINKING", "SPEAKING", "DEGRADED", "ESTOP"}}
    assert state_consts == {"IDLE", "LISTENING", "THINKING", "SPEAKING"}, (
        "DEGRADED/ESTOP must stay bool modifiers, not values of `state`"
    )


def test_reply_source_covers_the_llm_free_path():
    """The info-desk role works end to end with no LLM at all (CLAUDE.md), so
    a KB reply must be expressible without implying a fallback."""
    consts = msg_consts("Reply")
    assert {"SOURCE_KB", "SOURCE_LLM", "SOURCE_FALLBACK"} <= set(consts)
    assert consts["SOURCE_KB"] == "0", "the local template path is the default"


def test_kb_query_keeps_the_three_miss_kinds_distinct():
    """"Not in my directory" and "I haven't learned that block" are different
    sentences; collapsing them is how a robot starts inventing rooms."""
    (_, _), (_, consts) = load_srv(SRV_DIR / "QueryKb.srv")
    assert {"ANSWERED", "ROOM_UNKNOWN", "BLOCK_NOT_SURVEYED", "AMBIGUOUS"} <= set(consts)


# --------------------------------------------------------------------------
# 3. Drift against the Python mirrors
# --------------------------------------------------------------------------

def _mirror_fields(dc) -> set[str]:
    return {f.name for f in dataclass_fields(dc)}


@pytest.fixture(scope="module")
def bridge_types():
    return pytest.importorskip(
        "neo_webapp.bridge.types",
        reason='neo_webapp not installed -- run `pip install -e ".[dev]"` from the repo root',
    )


@pytest.fixture(scope="module")
def perception_types():
    return pytest.importorskip(
        "neo_perception.types",
        reason='neo_perception not installed -- run `pip install -e ".[dev]"` from the repo root',
    )


def test_attention_target_mirror_is_exact(perception_types):
    ros = set(msg_fields("AttentionTarget")) - {"header"}
    assert _mirror_fields(perception_types.AttentionTarget) == ros


def test_engagement_states_match(perception_types):
    ros = {k for k in msg_consts("AttentionTarget")
           if k in {"SCANNING", "ENGAGING", "ENGAGED", "SUSPENDED"}}
    py = {e.value.upper() for e in perception_types.EngagementState}
    assert py == ros


def test_gesture_kinds_match(perception_types):
    ros = {k for k, v in msg_consts("GestureEvent").items() if v.isdigit()}
    py = {g.value.upper() for g in perception_types.GestureKind}
    assert py == ros


def test_link_health_mirror_is_exact(bridge_types):
    ros = set(msg_fields("LinkHealth")) - {"header"}
    assert _mirror_fields(bridge_types.LinkHealth) == ros


def test_link_paths_match(bridge_types):
    ros = {k.removeprefix("PATH_").lower()
           for k in msg_consts("LinkHealth") if k.startswith("PATH_")}
    # The mirror types the paths as a Literal. It also uses
    # `from __future__ import annotations`, so __annotations__ holds strings --
    # get_type_hints is what actually resolves them.
    hints = get_type_hints(bridge_types.LinkHealth)
    py = set(get_args(hints["active_path"]))
    assert py == ros


def test_source_state_mirror_is_exact(bridge_types):
    ros = set(msg_fields("SourceState")) - {"header"}
    assert _mirror_fields(bridge_types.SourcesState) == ros


def test_streams_and_backends_match(bridge_types):
    backends = {k.removeprefix("BACKEND_").lower()
                for k in msg_consts("SourceState") if k.startswith("BACKEND_")}
    assert set(bridge_types.BACKENDS) == backends

    streams = {k.removeprefix("STREAM_").lower()
               for k in msg_consts("SourceState")
               if k.startswith("STREAM_") and k != "STREAM_NONE"}
    assert set(bridge_types.STREAMS) == streams


def test_emotion_mirror_is_a_projection(bridge_types):
    """The panel's EmotionState is a subset: Phase 9 adds the motion params to
    the UI. A subset is fine; a field the message does not have is not."""
    ros = set(msg_fields("EmotionState")) - {"header"}
    assert _mirror_fields(bridge_types.EmotionState) <= ros


def test_dialog_badge_labels_cover_the_flattened_states(bridge_types):
    """The panel renders one badge, flattening `state` + the two flags back to
    the plan's six labels. Every machine state and both modifiers need a label,
    or a real state becomes unrenderable."""
    consts = msg_consts("DialogState")
    machine = {k for k in consts if k in {"IDLE", "LISTENING", "THINKING", "SPEAKING"}}
    labels = set(bridge_types.DIALOG_STATES)

    assert machine <= labels
    assert {"DEGRADED", "ESTOP"} <= labels


def test_head_units_differ_between_panel_and_wire_on_purpose(bridge_types):
    """Not a bug to fix: the panel shows degrees, the wire carries radians, and
    the conversion belongs at that one boundary. Pinned so nobody unifies them
    by accident in either direction."""
    assert {"pan_deg", "tilt_deg"} <= _mirror_fields(bridge_types.HeadState)
    assert {"pan_rad", "tilt_rad"} <= set(msg_fields("HeadCommand"))


def test_transcript_mirror_is_exact(bridge_types):
    """The panel renders transcripts today from an in-process recognizer; the
    ROS node will publish the same fields once asr_router exists. They are the
    same contract and must not become two."""
    ros = set(msg_fields("Transcript")) - {"header"}
    assert _mirror_fields(bridge_types.TranscriptView) == ros


def test_transcript_engines_match(bridge_types):
    """`engine` is a uint8 on the wire and a string in the panel. The set of
    engines has to be the same either way -- the panel shows it verbatim, and a
    bad transcript is not diagnosable without knowing which produced it."""
    ros = {k.removeprefix("ENGINE_").lower()
           for k in msg_consts("Transcript") if k.startswith("ENGINE_")}
    assert ros == {"vosk", "whisper"}


# ---------------------------------------------------------------------------
# Generation hazards.
#
# Both of these shipped and were only caught by an actual `colcon build`. They
# are cheap to check as text, so they belong here rather than being findable
# only on a machine with Jazzy installed -- which is the whole premise of this
# file.
# ---------------------------------------------------------------------------


def test_comments_cannot_close_a_c_block_comment():
    """rosidl copies `.msg` comments verbatim into generated C block comments.

    A star-slash sequence anywhere in one closes that comment early, and the
    generated header then fails to compile with errors pointing at the *build*
    directory -- nothing names the `.msg` that caused it. `SourceState.msg`
    documented the source-mux rule using glob syntax and did exactly this.
    """
    offenders = [
        f"{path.name}:{i}"
        for path in sorted(list(MSG_DIR.glob("*.msg")) + list(SRV_DIR.glob("*.srv")))
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "*/" in line
    ]
    assert not offenders, (
        "these close the generated C block comment early: " + ", ".join(offenders)
    )


def test_package_xml_is_well_formed():
    """`--` is illegal inside an XML comment, and the house style uses it as an
    em-dash everywhere else.

    Both ROS packages shipped with an unparseable manifest this way. rosdep and
    colcon both fail on it, but only once something actually builds the package.
    """
    import xml.dom.minidom

    for manifest in sorted((PKG.parent).glob("*/package.xml")):
        try:
            xml.dom.minidom.parse(str(manifest))
        except Exception as exc:  # noqa: BLE001 -- report which file and why
            raise AssertionError(f"{manifest} is not well-formed XML: {exc}") from exc


def test_colcon_would_find_the_ros_packages():
    """A bare `colcon build` from the repo root finds nothing.

    The root `setup.py` makes colcon identify the root itself as a single
    Python package, so it never descends into `src/`: it reports "0 packages
    finished" and exits 0. That green-but-empty build is why the two failures
    above reached a pushed branch, so the invocation that avoids it is pinned
    both here and in the CI workflow.
    """
    repo = PKG.parents[1]
    assert (repo / "setup.py").exists(), (
        "the shadowing root setup.py is gone; if colcon can now discover src/ "
        "on its own, drop --base-paths from CI and delete this test"
    )
    ci = (repo / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "colcon build --base-paths src" in ci
    assert "colcon test --base-paths src" in ci
