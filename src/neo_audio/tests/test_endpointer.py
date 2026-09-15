"""Closing the listening window on the shape of the audio."""

from __future__ import annotations

import numpy as np
import pytest

from neo_audio.endpointer import Endpoint, Endpointer, EndpointerConfig

RATE = 16000


def _chunk(level: float, ms: int = 100, seed: int = 0) -> bytes:
    rng = np.random.default_rng(seed)
    samples = rng.standard_normal(RATE * ms // 1000) * level * 32767
    return np.clip(samples, -32768, 32767).astype("<i2").tobytes()


QUIET = 0.002
LOUD = 0.2


def _feed(ep, level, seconds):
    verdicts = []
    for i in range(int(round(seconds * 10))):
        verdicts.append(ep.accept(_chunk(level, seed=i)))
    return verdicts


def test_speech_then_a_pause_closes_the_window():
    ep = Endpointer(EndpointerConfig(silence_to_end_s=0.9), sample_rate=RATE)
    assert set(_feed(ep, QUIET, 0.5)) == {Endpoint.LISTENING}
    assert set(_feed(ep, LOUD, 1.0)) == {Endpoint.LISTENING}
    after = _feed(ep, QUIET, 1.2)
    assert Endpoint.ENDED in after
    assert after.index(Endpoint.ENDED) + 1 == pytest.approx(9, abs=1), "about 0.9 s of quiet"


def test_a_window_where_nobody_speaks_times_out():
    ep = Endpointer(EndpointerConfig(max_wait_for_speech_s=2.0), sample_rate=RATE)
    verdicts = _feed(ep, QUIET, 2.5)
    assert Endpoint.TIMED_OUT in verdicts
    assert Endpoint.ENDED not in verdicts


def test_a_cough_is_not_an_utterance():
    ep = Endpointer(EndpointerConfig(max_wait_for_speech_s=2.0, min_speech_s=0.25), sample_rate=RATE)
    _feed(ep, QUIET, 0.5)
    _feed(ep, LOUD, 0.1)
    verdicts = _feed(ep, QUIET, 2.0)
    assert Endpoint.ENDED not in verdicts
    assert Endpoint.TIMED_OUT in verdicts


def test_talking_past_the_ceiling_is_cut_off():
    ep = Endpointer(EndpointerConfig(max_utterance_s=2.0), sample_rate=RATE)
    _feed(ep, QUIET, 0.3)
    assert Endpoint.TOO_LONG in _feed(ep, LOUD, 2.5)


def test_the_room_noise_floor_is_kept_across_windows():
    ep = Endpointer(sample_rate=RATE)
    _feed(ep, QUIET, 1.0)
    floor = ep.noise_floor
    ep.reset()
    assert ep.noise_floor == floor
    assert not ep.heard_speech


def test_speech_does_not_drag_the_floor_up_to_meet_it():
    ep = Endpointer(sample_rate=RATE)
    _feed(ep, QUIET, 1.0)
    floor = ep.noise_floor
    _feed(ep, LOUD, 3.0)
    assert ep.noise_floor == pytest.approx(floor, rel=0.01)
