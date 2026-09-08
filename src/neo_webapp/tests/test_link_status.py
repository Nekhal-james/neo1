from __future__ import annotations

import json
from pathlib import Path

from neo_webapp import link_status


def test_read_link_status_missing_file_returns_defaults(tmp_path: Path):
    link, ping = link_status.read_link_status(tmp_path / "nope.json")
    assert link.up is False
    assert link.active_path == "none"
    assert ping is None


def test_read_link_status_parses_file(tmp_path: Path):
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps(
            {
                "link": {
                    "up": True,
                    "active_path": "eth",
                    "rtt_ms": 4.2,
                    "consecutive_failures": 0,
                },
                "ping": {
                    "sent": 5,
                    "received": 5,
                    "loss_pct": 0.0,
                    "rtt_min_ms": 3.1,
                    "rtt_avg_ms": 4.4,
                    "rtt_max_ms": 6.0,
                    "active_path": "eth",
                },
            }
        ),
        encoding="utf-8",
    )
    link, ping = link_status.read_link_status(path)
    assert link.up is True
    assert link.active_path == "eth"
    assert link.rtt_ms == 4.2
    assert ping is not None
    assert ping.sent == 5
    assert ping.rtt_avg_ms == 4.4


def test_read_link_status_corrupt_file_returns_defaults(tmp_path: Path):
    path = tmp_path / "status.json"
    path.write_text("{not json", encoding="utf-8")
    link, ping = link_status.read_link_status(path)
    assert link.up is False
    assert ping is None


def test_link_status_route_requires_auth(client):
    res = client.get("/api/link/status")
    assert res.status_code == 401


def test_link_status_route_returns_defaults_when_no_file(auth_client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "neo_webapp.api.routes.read_link_status",
        lambda: link_status.read_link_status(tmp_path / "nope.json"),
    )
    res = auth_client.get("/api/link/status")
    assert res.status_code == 200
    body = res.json()
    assert body["link"]["up"] is False
    assert body["ping"] is None


def test_link_status_route_returns_file_contents(auth_client, monkeypatch, tmp_path):
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps(
            {
                "link": {
                    "up": True,
                    "active_path": "wifi",
                    "rtt_ms": 12.5,
                    "consecutive_failures": 0,
                },
                "ping": None,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "neo_webapp.api.routes.read_link_status",
        lambda: link_status.read_link_status(path),
    )
    res = auth_client.get("/api/link/status")
    assert res.status_code == 200
    body = res.json()
    assert body["link"]["up"] is True
    assert body["link"]["active_path"] == "wifi"
    assert body["ping"] is None
