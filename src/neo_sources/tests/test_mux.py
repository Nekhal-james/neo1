"""The source manager as the browser's mux: forwards only what is selected.

Built without calling the node's __init__, so it runs with no ROS graph; the
publishers are stand-ins that record what they were asked to send.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from neo_sources.nodes.source_manager import SourceManagerNode
from neo_sources.playback import PlaybackClock
from neo_sources.selection import HARDWARE, WEBAPP, Selection


class Recorder:
    def __init__(self):
        self.sent = []

    def publish(self, msg):
        self.sent.append(msg)


def _node(selection, now):
    node = SourceManagerNode.__new__(SourceManagerNode)
    node._selection = selection
    node._camera_out = Recorder()
    node._mic_out = Recorder()
    node._speaker_out = Recorder()
    node._playing_pub = Recorder()
    node._playback = PlaybackClock(clock=lambda: now[0])
    node._was_playing = False
    node._mic_seq = 0
    return node


def _audio(nbytes=4410, rate=22050):
    return SimpleNamespace(data=b"\x00" * nbytes, sample_rate=rate, channels=1, seq=99)


def test_a_browser_left_open_injects_nothing_while_the_robot_uses_its_own_devices():
    node = _node(Selection(HARDWARE, HARDWARE, HARDWARE), [0.0])
    node._on_webapp_camera(object())
    node._on_webapp_mic(_audio())
    node._on_audio_out(_audio())
    assert node._camera_out.sent == node._mic_out.sent == node._speaker_out.sent == []
    assert not node._playback.playing()


def test_webapp_streams_are_forwarded_onto_the_topics_the_graph_reads():
    node = _node(Selection(WEBAPP, WEBAPP, WEBAPP), [0.0])
    frame = object()
    node._on_webapp_camera(frame)
    assert node._camera_out.sent == [frame]

    for _ in range(3):
        node._on_webapp_mic(_audio())
    assert [m.seq for m in node._mic_out.sent] == [0, 1, 2], "renumbered per stream"


def test_speech_to_the_browser_advances_the_play_cursor():
    now = [10.0]
    node = _node(Selection(HARDWARE, HARDWARE, WEBAPP), now)
    node._on_audio_out(_audio(nbytes=44100))     # 1.0 s at 22.05 kHz
    assert len(node._speaker_out.sent) == 1
    assert node._playback.until == pytest.approx(11.0)


def test_playing_is_published_while_audible_and_once_when_it_stops():
    pytest.importorskip("std_msgs.msg")
    now = [10.0]
    node = _node(Selection(HARDWARE, HARDWARE, WEBAPP), now)
    node._on_playing_timer()
    assert node._playing_pub.sent == [], "nothing to say before anything played"

    node._on_audio_out(_audio(nbytes=44100))
    node._on_playing_timer()
    now[0] = 11.5
    node._on_playing_timer()
    node._on_playing_timer()
    assert [m.data for m in node._playing_pub.sent] == [True, False]
