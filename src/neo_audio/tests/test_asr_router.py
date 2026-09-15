"""asr_router's pure parts: the wake word the pre-roll captured, and the watchdog.

The node is built without its __init__ (which needs rclpy and a Vosk model);
the watchdog only reads a handful of attributes, set here directly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from neo_audio.config import Config
from neo_audio.nodes.asr_router import WATCHDOG_GRACE_S, AsrRouterNode, strip_wake_word


@pytest.mark.parametrize(
    "heard, expected",
    [
        ("neo where is the library", "where is the library"),
        ("Neo, where is CS 204?", "where is CS 204?"),
        ("hey neo what is this", "what is this"),
        ("neo neo where is the canteen", "where is the canteen"),
        ("nio where is b block", "where is b block"),
        # Left alone:
        ("where is the neo lab", "where is the neo lab"),
        ("neon lights in the hall", "neon lights in the hall"),
        ("near the library is there a canteen", "near the library is there a canteen"),
        ("neo", "neo"),
        ("", ""),
    ],
)
def test_only_a_leading_wake_word_is_removed(heard, expected):
    assert strip_wake_word(heard) == expected


def _router(opened_ago: float, *, heard_speech: bool = False, listening: bool = True):
    node = AsrRouterNode.__new__(AsrRouterNode)
    now = 1000.0
    node._now = lambda: now
    node._listening = listening
    node._opened_at = now - opened_ago
    node._config = Config()
    node._endpointer = SimpleNamespace(heard_speech=heard_speech)
    node._session = SimpleNamespace(final=lambda: SimpleNamespace(text="where is the lab", confidence=0.6))
    node.finished = []
    node._finish = lambda text, confidence: node.finished.append(text)
    node.get_logger = lambda: SimpleNamespace(warning=lambda *a, **k: None)
    return node


def test_a_window_with_no_audio_at_all_is_closed_empty():
    wait = Config().endpointer.max_wait_for_speech_s
    early = _router(wait)
    early._on_watchdog()
    assert early.finished == [], "the endpointer's own limit, plus grace, comes first"

    late = _router(wait + WATCHDOG_GRACE_S + 0.1)
    late._on_watchdog()
    assert late.finished == [""]


def test_speech_that_was_heard_is_flushed_not_discarded():
    limit = Config().endpointer.max_utterance_s
    node = _router(limit + WATCHDOG_GRACE_S + 0.1, heard_speech=True)
    node._on_watchdog()
    assert node.finished == ["where is the lab"]

    patient = _router(Config().endpointer.max_wait_for_speech_s + 5, heard_speech=True)
    patient._on_watchdog()
    assert patient.finished == [], "someone mid-sentence gets the utterance limit, not the wait limit"


def test_the_watchdog_leaves_a_closed_window_alone():
    node = _router(999.0, listening=False)
    node._on_watchdog()
    assert node.finished == []
