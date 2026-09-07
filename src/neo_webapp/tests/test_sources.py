"""Source selection is the seam the whole robot depends on (plan section 2.1)."""

from __future__ import annotations

import pytest


def test_defaults_to_hardware(auth_client):
    body = auth_client.get("/api/sources").json()
    assert body["sources"] == {
        "camera": "hardware",
        "mic": "hardware",
        "speaker": "hardware",
        "transitioning": None,
    }


@pytest.mark.parametrize("stream", ["camera", "mic", "speaker"])
def test_each_stream_switches_independently(auth_client, stream):
    assert auth_client.post(
        "/api/sources/set", json={"stream": stream, "backend": "webapp"}
    ).status_code == 200

    sources = auth_client.get("/api/sources").json()["sources"]
    assert sources[stream] == "webapp"
    for other in {"camera", "mic", "speaker"} - {stream}:
        assert sources[other] == "hardware", "switching one stream moved another"


def test_switching_back_and_forth(auth_client):
    for backend in ("webapp", "hardware", "webapp"):
        auth_client.post(
            "/api/sources/set", json={"stream": "camera", "backend": backend}
        )
        assert auth_client.get("/api/sources").json()["sources"]["camera"] == backend


def test_rejects_unknown_stream_and_backend(auth_client):
    assert auth_client.post(
        "/api/sources/set", json={"stream": "lidar", "backend": "webapp"}
    ).status_code == 400
    assert auth_client.post(
        "/api/sources/set", json={"stream": "camera", "backend": "carrier-pigeon"}
    ).status_code == 400


def test_a_stream_is_never_left_without_a_backend(auth_client, bridge):
    """No transition may end with an empty selection, even mid-flight."""
    auth_client.post("/api/sources/set", json={"stream": "mic", "backend": "webapp"})
    sources = bridge.snapshot().sources
    assert sources.mic in ("hardware", "webapp")
    assert sources.transitioning is None
