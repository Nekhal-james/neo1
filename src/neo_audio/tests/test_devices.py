"""The ALSA seam, against fake arecord/aplay listings and processes."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from neo_audio import devices
from neo_audio.devices import AlsaDevice, MicCapture, SpeakerPlayback

ARECORD_L = """**** List of CAPTURE Hardware Devices ****
card 2: Camera [USB Camera], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""

APLAY_L = """**** List of PLAYBACK Hardware Devices ****
card 0: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
  Subdevices: 1/1
card 3: Device [USB Audio Device], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
"""

APLAY_HDMI_ONLY = """**** List of PLAYBACK Hardware Devices ****
card 0: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
"""


@pytest.fixture
def alsa(monkeypatch):
    listings = {"arecord": ARECORD_L, "aplay": APLAY_L}
    monkeypatch.setattr(devices.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(
        devices.subprocess, "run", lambda args, **kw: SimpleNamespace(stdout=listings[args[0]])
    )
    return listings


class FakeProc:
    def __init__(self, args, stdout=None, stdin=None, stderr=None, data=b""):
        self.args = args
        self.stdout = io.BytesIO(data) if stdout is not None else None
        self.stdin = io.BytesIO() if stdin is not None else None
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


def test_capture_devices_come_from_arecord(alsa):
    assert devices.capture_devices() == [AlsaDevice(card=2, device=0, name="USB Camera")]
    assert devices.default_capture().plughw == "plughw:2,0"


def test_the_speaker_is_never_hdmi_when_anything_else_is_attached(alsa):
    assert devices.default_playback().plughw == "plughw:3,0"


def test_hdmi_is_used_when_it_is_all_there_is(alsa):
    alsa["aplay"] = APLAY_HDMI_ONLY
    assert devices.default_playback().plughw == "plughw:0,0"


def test_without_alsa_utils_there_are_no_devices_and_a_reason(monkeypatch):
    monkeypatch.setattr(devices.shutil, "which", lambda cmd: None)
    assert devices.capture_devices() == []
    ok, why = MicCapture().available()
    assert not ok and "alsa-utils" in why


def test_the_mic_asks_alsa_for_exactly_what_audio_in_promises(monkeypatch, alsa):
    chunk = b"\x01\x00" * 1600
    spawned = []

    def popen(args, **kw):
        spawned.append(FakeProc(args, data=chunk, **kw))
        return spawned[-1]

    monkeypatch.setattr(devices.subprocess, "Popen", popen)
    mic = MicCapture(sample_rate=16000, chunk_ms=100, retry_s=0)
    assert mic.read() == chunk
    args = spawned[0].args
    assert args[args.index("-D") + 1] == "plughw:2,0"
    assert args[args.index("-r") + 1] == "16000"
    assert args[args.index("-c") + 1] == "1"
    assert args[args.index("-f") + 1] == "S16_LE"


def test_an_unplugged_mic_reads_nothing_releases_and_says_why(monkeypatch, alsa):
    spawned = []

    def popen(args, **kw):
        spawned.append(FakeProc(args, data=b"", **kw))
        return spawned[-1]

    monkeypatch.setattr(devices.subprocess, "Popen", popen)
    mic = MicCapture(retry_s=0)
    assert mic.read() == b""
    assert spawned[0].terminated
    assert "unplugged" in mic.error


def test_the_speaker_knows_when_its_audio_stops_being_audible(monkeypatch, alsa):
    now = [10.0]
    monkeypatch.setattr(devices.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(devices.subprocess, "Popen", lambda args, **kw: FakeProc(args, **kw))

    speaker = SpeakerPlayback(sample_rate=22050)
    speaker.play(b"\x00\x00" * 22050)          # 1.0 s
    assert speaker.busy_until() == pytest.approx(11.0)
    now[0] = 10.2
    speaker.play(b"\x00\x00" * 11025)          # queued behind it: 0.5 s more
    assert speaker.busy_until() == pytest.approx(11.5)
    now[0] = 11.4
    assert speaker.is_playing()
    now[0] = 11.6
    assert not speaker.is_playing()
