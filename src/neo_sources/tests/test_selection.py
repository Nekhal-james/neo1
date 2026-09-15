"""Source selection as plain data, and the browser playback cursor."""

from __future__ import annotations

import pytest

from neo_sources.playback import PlaybackClock
from neo_sources.selection import (
    HARDWARE,
    STREAM_CAMERA,
    STREAM_MIC,
    STREAM_SPEAKER,
    WEBAPP,
    Selection,
    validate,
)


def test_the_default_is_the_robots_own_devices():
    assert Selection() == Selection(HARDWARE, HARDWARE, HARDWARE)


def test_switching_one_stream_leaves_the_others():
    s = Selection().with_stream(STREAM_MIC, WEBAPP)
    assert (s.camera, s.mic, s.speaker) == (HARDWARE, WEBAPP, HARDWARE)
    assert s.describe() == "camera=hardware, mic=webapp, speaker=hardware"


@pytest.mark.parametrize("stream, backend", [(0, HARDWARE), (4, HARDWARE), (STREAM_CAMERA, 2)])
def test_an_unknown_stream_or_backend_is_refused_not_clamped(stream, backend):
    ok, why = validate(stream, backend)
    assert not ok and "unknown" in why
    with pytest.raises(ValueError):
        Selection().with_stream(stream, backend)


def test_every_real_switch_validates():
    for stream in (STREAM_CAMERA, STREAM_MIC, STREAM_SPEAKER):
        for backend in (HARDWARE, WEBAPP):
            assert validate(stream, backend) == (True, "ok")


def test_the_play_cursor_queues_chunks_behind_each_other():
    now = [100.0]
    clock = PlaybackClock(clock=lambda: now[0])
    clock.add(22050 * 2, 22050)                  # 1.0 s at 22.05 kHz mono S16
    now[0] = 100.25
    clock.add(22050, 22050)                      # 0.5 s, queued behind it
    assert clock.until == pytest.approx(101.5)
    now[0] = 101.4
    assert clock.playing()
    now[0] = 101.6
    assert not clock.playing()


def test_a_gap_starts_the_cursor_from_now_not_from_the_past():
    now = [100.0]
    clock = PlaybackClock(clock=lambda: now[0])
    clock.add(22050 * 2, 22050)
    now[0] = 200.0
    assert clock.add(22050 * 2, 22050) == pytest.approx(201.0)


def test_nonsense_chunks_do_not_move_the_cursor():
    clock = PlaybackClock(clock=lambda: 5.0)
    assert clock.add(0, 22050) == 0.0
    assert clock.add(100, 0) == 0.0
    clock.add(22050 * 2, 22050)
    clock.reset()
    assert not clock.playing()
