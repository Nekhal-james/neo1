from __future__ import annotations


def test_config_route_returns_media_settings(auth_client, config):
    res = auth_client.get("/api/config")
    assert res.status_code == 200
    body = res.json()
    assert body == {
        "joy_deadman_ms": config.media.joy_deadman_ms,
        "joy_rate_hz": config.media.joy_rate_hz,
        "mic_sample_rate": config.media.mic_sample_rate,
        "speaker_sample_rate": config.media.speaker_sample_rate,
        "camera_max_fps": config.media.camera_max_fps,
        "camera_jpeg_quality": config.media.camera_jpeg_quality,
    }
