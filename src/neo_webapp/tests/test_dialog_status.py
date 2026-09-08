from __future__ import annotations

import json
from pathlib import Path

from neo_webapp import dialog_status


def test_read_dialog_status_missing_file_returns_defaults(tmp_path: Path):
    view = dialog_status.read_dialog_status(tmp_path / "nope.json")
    assert view.state == "IDLE"
    assert view.chat_source == "none"
    assert view.last_prompt == ""


def test_read_dialog_status_parses_file(tmp_path: Path):
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps(
            {
                "written_at_unix": 123.0,
                "dialog_state": "IDLE",
                "last_prompt": "where is CS-204",
                "last_reply": "B block.",
                "chat_source": "ollama",
                "asr_engine": "vosk",
                "tts_engine": "piper",
            }
        ),
        encoding="utf-8",
    )
    view = dialog_status.read_dialog_status(path)
    assert view.last_prompt == "where is CS-204"
    assert view.last_reply == "B block."
    assert view.chat_source == "ollama"
    assert view.asr_engine == "vosk"
    assert view.tts_engine == "piper"
    assert view.updated_at == 123.0


def test_read_dialog_status_corrupt_file_returns_defaults(tmp_path: Path):
    path = tmp_path / "status.json"
    path.write_text("{not json", encoding="utf-8")
    view = dialog_status.read_dialog_status(path)
    assert view.state == "IDLE"
    assert view.chat_source == "none"


def test_dialog_status_route_requires_auth(client):
    res = client.get("/api/dialog/status")
    assert res.status_code == 401


def test_dialog_status_route_returns_defaults_when_no_file(auth_client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "neo_webapp.api.routes.read_dialog_status",
        lambda: dialog_status.read_dialog_status(tmp_path / "nope.json"),
    )
    res = auth_client.get("/api/dialog/status")
    assert res.status_code == 200
    body = res.json()
    assert body["chat_source"] == "none"
    assert body["last_prompt"] == ""


def test_dialog_status_route_returns_file_contents(auth_client, monkeypatch, tmp_path):
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps(
            {
                "dialog_state": "IDLE",
                "last_prompt": "hello",
                "last_reply": "hi there",
                "chat_source": "degraded",
                "asr_engine": "vosk",
                "tts_engine": "piper",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "neo_webapp.api.routes.read_dialog_status",
        lambda: dialog_status.read_dialog_status(path),
    )
    res = auth_client.get("/api/dialog/status")
    assert res.status_code == 200
    body = res.json()
    assert body["last_prompt"] == "hello"
    assert body["chat_source"] == "degraded"
