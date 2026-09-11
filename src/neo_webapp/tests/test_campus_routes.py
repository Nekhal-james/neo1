"""The Data tab's API: load, check, save, and try a question."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from intelligence import campus

UI = Path(__file__).resolve().parents[1] / "neo_webapp" / "ui"

ROOM = "- {code: CS-204, name: Computer Science Lab 2, type: lab, block: B, floor: 2, aliases: [cs lab 2]}\n"


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    """Point intelligence's config at a scratch dataset, never the repo's."""
    data, backups = tmp_path / "campus", tmp_path / "backups"
    cfg = tmp_path / "intelligence.yaml"
    cfg.write_text(
        f"rag:\n  data_dir: {data.as_posix()}\n  backup_dir: {backups.as_posix()}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NEO_INTELLIGENCE_CONFIG", str(cfg))
    return data


def test_every_route_needs_a_session(client, data_dir):
    assert client.get("/api/campus").status_code == 401
    for path in ("/api/campus/validate", "/api/campus/save", "/api/campus/try"):
        assert client.post(path, json={"file": "rooms", "text": ROOM}).status_code == 401
    assert not (data_dir / "rooms.yaml").exists()


def test_load_an_empty_dataset_with_the_samples(auth_client, data_dir):
    body = auth_client.get("/api/campus").json()
    assert body["ok"] is True
    assert set(body["files"]) == {"rooms", "graph", "coverage"}
    assert body["files"]["rooms"] == {"text": "", "exists": False, "modified_ns": 0}
    assert body["samples"] == campus.SAMPLES
    assert body["summary"]["rooms"] == 0


def test_save_then_load_round_trips_the_text(auth_client, data_dir):
    text = "# ground floor first\n" + ROOM
    res = auth_client.post("/api/campus/save", json={"file": "rooms", "text": text, "modified_ns": 0})
    assert res.status_code == 200, res.json()
    body = res.json()
    assert body["saved"] == "rooms" and body["backup"] is None
    assert body["summary"]["rooms"] == 1

    loaded = auth_client.get("/api/campus").json()["files"]["rooms"]
    assert loaded["text"] == text
    assert loaded["modified_ns"] == body["modified_ns"]


def test_a_second_save_keeps_a_backup(auth_client, data_dir):
    first = auth_client.post("/api/campus/save", json={"file": "rooms", "text": ROOM}).json()
    second = auth_client.post("/api/campus/save", json={
        "file": "rooms", "text": ROOM.replace("Lab 2", "Lab Two"), "modified_ns": first["modified_ns"],
    })
    assert second.status_code == 200
    assert Path(second.json()["backup"]).read_text(encoding="utf-8") == ROOM


def test_errors_block_the_save_and_say_why(auth_client, data_dir):
    res = auth_client.post("/api/campus/save", json={"file": "rooms", "text": "- {code: A-1, name: One, block: A}\n"})
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["ok"] is False
    assert "floor" in detail["errors"][0]["message"]
    assert not (data_dir / "rooms.yaml").exists()


def test_a_save_over_a_newer_file_is_refused(auth_client, data_dir):
    saved = auth_client.post("/api/campus/save", json={"file": "rooms", "text": ROOM}).json()
    stale = saved["modified_ns"] - 1
    res = auth_client.post("/api/campus/save", json={"file": "rooms", "text": ROOM, "modified_ns": stale})
    assert res.status_code == 409
    assert "changed on disk" in res.json()["detail"]


def test_a_file_is_checked_against_the_others_on_disk(auth_client, data_dir):
    """rooms.yaml names a node; whether it exists is graph.yaml's business."""
    graph = "nodes:\n  - id: reception\n"
    assert auth_client.post("/api/campus/save", json={"file": "graph", "text": graph}).status_code == 200
    body = auth_client.post("/api/campus/validate", json={
        "file": "rooms", "text": ROOM.replace("}", ", node: attic}"),
    }).json()
    assert body["ok"] is False
    assert "node 'attic' is not in graph.yaml" in body["errors"][0]["message"]


def test_only_the_three_files_can_be_written(auth_client, data_dir):
    for name in ("../../config/webapp.local", "rooms.yaml", None):
        res = auth_client.post("/api/campus/save", json={"file": name, "text": ROOM})
        assert res.status_code == 400
    assert not any(data_dir.parent.rglob("webapp.local*"))


def test_try_uses_unsaved_drafts_and_never_writes(auth_client, data_dir):
    res = auth_client.post("/api/campus/try", json={"text": "where is c s two oh four", "drafts": {"rooms": ROOM}})
    assert res.status_code == 200
    body = res.json()
    assert [h["code"] for h in body["hits"]] == ["CS-204"]
    assert body["used_drafts"] == ["rooms"]
    assert body["offline_reply"].startswith("CS-204, Computer Science Lab 2, is in B block")
    assert "Campus directory" in body["context"]
    assert not (data_dir / "rooms.yaml").exists()


def test_try_rejects_bad_drafts(auth_client, data_dir):
    res = auth_client.post("/api/campus/try", json={"text": "hi", "drafts": {"secrets": "x"}})
    assert res.status_code == 400


def test_the_data_tab_documents_every_room_field():
    """The format table is hand-written HTML; this is what keeps it honest."""
    html = (UI / "index.html").read_text(encoding="utf-8")
    tab = html[html.index('id="tab-data"'):]
    documented = set(re.findall(r"<td><code>(\w+)</code>", tab))
    assert documented == set(campus.ROOM_FIELDS)
    for t in campus.ROOM_TYPES:
        assert t in tab
