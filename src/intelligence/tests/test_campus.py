"""The campus dataset: validation, saving, and the samples the panel serves."""

from __future__ import annotations

import pytest

from intelligence import campus
from intelligence.campus import SAMPLES, validate


def messages(v, severity=None):
    return [i.message for i in v.issues if severity is None or i.severity == severity]


def test_the_samples_the_panel_shows_are_clean():
    """The operator copies these. They must be data Neo accepts without a word."""
    v = validate(SAMPLES)
    assert v.issues == []
    assert [r.code for r in v.dataset.rooms] == ["CS-204", "LIB"]
    assert v.dataset.route("b2_lab_corridor") == [
        "go out through the back door and cross the courtyard to B block",
        "take the stairs up two floors and turn right",
    ]


def test_no_files_is_an_empty_dataset_not_an_error():
    v = validate({})
    assert v.ok and v.dataset.empty


def test_a_misspelt_field_is_an_error_with_a_suggestion():
    """`alias:` for `aliases:` would otherwise drop every alias without a trace."""
    v = validate({"rooms": "- code: X-1\n  name: X\n  block: A\n  floor: 0\n  alias: [x]\n"})
    assert not v.ok
    assert any("did you mean 'aliases'" in m for m in messages(v, "error"))


def test_required_fields_and_types():
    v = validate({"rooms": (
        "- code: A-1\n  name: One\n  block: A\n"                 # no floor
        "- code: A-2\n  name: Two\n  block: A\n  floor: two\n"   # floor not a number
        "- code: A-3\n  name: Three\n  block: A\n  floor: 0\n  type: canteen\n"
    )})
    errors = messages(v, "error")
    assert any("missing required field(s): floor" in m for m in errors)
    assert any("whole number" in m for m in errors)
    assert any("'type' must be one of" in m for m in errors)
    assert v.dataset.rooms == []


def test_errors_point_at_the_line():
    v = validate({"rooms": "- code: A-1\n  name: One\n  block: A\n  floor: 0\n\n- code: A-2\n  name: Two\n"})
    [issue] = v.errors
    assert issue.line == 6


def test_block_written_with_the_word_block_is_rejected():
    v = validate({"rooms": "- code: A-1\n  name: One\n  block: B Block\n  floor: 0\n"})
    assert any("write the block as 'B'" in m for m in messages(v, "error"))


def test_duplicate_codes_are_an_error():
    v = validate({"rooms": "- {code: A-1, name: One, block: A, floor: 0}\n- {code: a-1, name: Two, block: A, floor: 0}\n"})
    assert any("already used" in m for m in messages(v, "error"))


def test_an_alias_that_is_another_rooms_code_is_an_error():
    v = validate({"rooms": (
        "- {code: CS-204, name: Lab Two, block: B, floor: 2}\n"
        "- {code: CS-240, name: Lab Four, block: B, floor: 2, aliases: [cs 204]}\n"
    )})
    assert any("is a room code and also names another room" in m for m in messages(v, "error"))


def test_a_shared_alias_is_only_a_warning():
    """Washrooms on every floor are real; Neo asks which one."""
    v = validate({"rooms": (
        "- {code: WR-0, name: Ground Washroom, block: A, floor: 0, aliases: [washroom]}\n"
        "- {code: WR-1, name: First Washroom, block: A, floor: 1, aliases: [washroom]}\n"
    )})
    assert v.ok
    assert any("will ask which one" in m for m in messages(v, "warning"))


def test_a_generic_alias_is_warned_about():
    v = validate({"rooms": "- {code: A-1, name: One, block: A, floor: 0, aliases: [lab]}\n"})
    assert v.ok
    assert any("too general" in m for m in messages(v, "warning"))


def test_graph_references_are_checked():
    rooms = "- {code: A-1, name: One, block: A, floor: 0, node: nowhere}\n"
    graph = (
        "nodes:\n  - id: reception\n  - id: hall\n"
        "edges:\n  - {from: reception, to: attic, instruction: up}\n"
    )
    v = validate({"rooms": rooms, "graph": graph})
    errors = messages(v, "error")
    assert any("unknown node(s): attic" in m for m in errors)
    assert any("node 'nowhere' is not in graph.yaml" in m for m in errors)


def test_edges_are_one_way_unless_reversible():
    graph = (
        "nodes:\n  - id: reception\n  - id: hall\n  - id: stairs\n"
        "edges:\n"
        "  - {from: hall, to: reception, instruction: walk back}\n"
        "  - {from: stairs, to: reception, instruction: come down, reverse_instruction: go up}\n"
    )
    ds = validate({"graph": graph}).dataset
    assert ds.route("hall") is None
    assert ds.route("stairs") == ["go up"]


def test_coverage_consistency_warnings():
    rooms = (
        "- {code: A-1, name: One, block: A, floor: 3}\n"
        "- {code: C-1, name: Cee, block: C, floor: 0}\n"
        "- {code: D-1, name: Dee, block: D, floor: 0}\n"
    )
    coverage = "blocks:\n  A: {status: complete, floors: [0, 1]}\n  C: {status: not_surveyed}\n"
    v = validate({"rooms": rooms, "coverage": coverage})
    assert v.ok
    warnings = " | ".join(messages(v, "warning"))
    assert "floor 3, which is not in this block's floors" in warnings
    assert "marked not_surveyed but has rooms" in warnings
    assert "block D is not in coverage.yaml" in warnings


def test_bad_coverage_status_is_an_error():
    v = validate({"coverage": "blocks:\n  A: {status: done}\n"})
    assert any("'status' must be one of" in m for m in messages(v, "error"))


def test_broken_yaml_reports_a_line():
    v = validate({"rooms": "- code: A-1\n  name: [unclosed\n"})
    [issue] = v.errors
    assert "not valid YAML" in issue.message and issue.line is not None


def test_one_bad_room_does_not_hide_the_good_ones():
    v = validate({"rooms": "- {code: A-1, name: One, block: A, floor: 0}\n- {code: A-2}\n"})
    assert not v.ok
    assert [r.code for r in v.dataset.rooms] == ["A-1"]


def test_save_is_atomic_keeps_a_backup_and_the_text_verbatim(tmp_path):
    data, backups = tmp_path / "data", tmp_path / "backups"
    first = "# my comment\n- {code: A-1, name: One, block: A, floor: 0}\n"
    assert campus.save_text(data, "rooms", first, backups) is None
    assert (data / "rooms.yaml").read_text(encoding="utf-8") == first

    backup = campus.save_text(data, "rooms", "- {code: A-2, name: Two, block: A, floor: 0}", backups)
    assert backup is not None and backup.read_text(encoding="utf-8") == first
    assert (data / "rooms.yaml").read_text(encoding="utf-8").endswith("floor: 0}\n")
    assert not list(data.glob(".rooms.*.tmp"))


def test_backups_are_pruned(tmp_path):
    data, backups = tmp_path / "data", tmp_path / "backups"
    for i in range(6):
        campus.save_text(data, "coverage", f"blocks: {{A{i}: {{status: not_surveyed}}}}\n", backups, keep=3)
    assert len(list(backups.glob("coverage-*.yaml"))) == 3


def test_only_the_three_files_can_be_named(tmp_path):
    with pytest.raises(ValueError):
        campus.file_path(tmp_path, "../../config/webapp.local")


def test_load_dataset_picks_up_a_save_without_a_restart(tmp_path):
    data, backups = tmp_path / "data", tmp_path / "backups"
    assert campus.load_dataset(data).empty
    campus.save_text(data, "rooms", "- {code: A-1, name: One, block: A, floor: 0}\n", backups)
    assert [r.code for r in campus.load_dataset(data).rooms] == ["A-1"]
