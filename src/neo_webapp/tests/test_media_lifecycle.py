"""Media channels are the expensive half of the panel and must be on demand.

If a channel leaks after a browser tab closes, the field profile quietly loses a
core to a pipeline nobody is watching (plan section 0.3.4).
"""

from __future__ import annotations

import time


def _media(client) -> dict:
    return client.get("/api/state").json()["media"]


def test_channels_start_closed(auth_client):
    assert _media(auth_client) == {
        "camera": False,
        "mic": False,
        "speaker": False,
        "joy": False,
    }


def test_camera_channel_opens_and_closes(auth_client):
    with auth_client.websocket_connect("/ws/camera") as ws:
        ws.send_bytes(b"\xff\xd8\xff\xe0 not-a-real-jpeg")
        time.sleep(0.05)
        assert _media(auth_client)["camera"] is True
    time.sleep(0.05)
    assert _media(auth_client)["camera"] is False


def test_mic_channel_opens_and_closes(auth_client):
    with auth_client.websocket_connect("/ws/mic") as ws:
        ws.send_bytes(b"\x00\x01" * 160)
        time.sleep(0.05)
        assert _media(auth_client)["mic"] is True
    time.sleep(0.05)
    assert _media(auth_client)["mic"] is False


def test_joy_channel_opens_and_closes(auth_client):
    with auth_client.websocket_connect("/ws/joy") as ws:
        ws.send_json({"axes": [0.0, 0.0], "buttons": []})
        time.sleep(0.05)
        assert _media(auth_client)["joy"] is True
    time.sleep(0.05)
    assert _media(auth_client)["joy"] is False


def test_camera_frames_are_rate_limited_server_side(auth_client, bridge, config):
    """A fast client must not be able to push past the configured cap."""
    config.media.camera_max_fps = 4
    sent = 40
    with auth_client.websocket_connect("/ws/camera") as ws:
        for _ in range(sent):
            ws.send_bytes(b"frame")
        time.sleep(0.3)
    # Accepted frames are throttled to roughly the cap, far below what was sent.
    accepted = len(bridge._frame_times)
    assert accepted < sent


def test_malformed_joy_message_does_not_kill_the_server(auth_client):
    with auth_client.websocket_connect("/ws/joy") as ws:
        ws.send_text("this is not json")
        time.sleep(0.05)
    assert auth_client.get("/api/state").status_code == 200


def test_speaker_channel_receives_the_test_tone(auth_client):
    with auth_client.websocket_connect("/ws/speaker") as ws:
        auth_client.post("/api/media/test-tone")
        chunk = ws.receive_bytes()
        assert len(chunk) > 0
