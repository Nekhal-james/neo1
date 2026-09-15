"""The dialog node's decisions, without a ROS graph.

`answer()` routes a question -- to the camera or to chat -- and `_on_watchdog()`
bounds every state. Both are exercised on a node built without its __init__,
which needs rclpy, with publishing replaced by a list.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from intelligence.nodes import dialog_node as dn


def _node(identified=("bottle", True)):
    node = dn.DialogNode.__new__(dn.DialogNode)
    node._cfg = object()
    node._mc_cfg = object()
    node._ask_perception = lambda: identified
    return node


def test_what_is_this_asks_the_camera_and_never_the_model_host(monkeypatch):
    monkeypatch.setattr(dn, "ask", lambda *a, **k: pytest.fail("the model host cannot see the room"))
    reply, source, _ms = _node().answer("what is this")
    assert reply == "That looks like a bottle."
    assert source == dn._SOURCE_KB


def test_nothing_identified_is_admitted_rather_than_guessed(monkeypatch):
    monkeypatch.setattr(dn, "ask", lambda *a, **k: pytest.fail("not a chat question"))
    reply, _source, _ms = _node(identified=("", False)).answer("what am i holding")
    assert "cannot see" in reply


@pytest.mark.parametrize(
    "chat_source, wire_source",
    [("ollama", dn._SOURCE_LLM), ("kb", dn._SOURCE_KB), ("degraded", dn._SOURCE_FALLBACK)],
)
def test_other_questions_go_to_chat_with_their_provenance(monkeypatch, chat_source, wire_source):
    calls = []

    def fake_ask(text, **kw):
        calls.append(text)
        return SimpleNamespace(reply="B block, first floor.", source=chat_source, latency_ms=42.0)

    monkeypatch.setattr(dn, "ask", fake_ask)
    reply, source, ms = _node().answer("where is CS-204")
    assert calls == ["where is CS-204"]
    assert (reply, source, ms) == ("B block, first floor.", wire_source, 42.0)


# -- the watchdog ----------------------------------------------------------------


def _watched(state, *, in_state_for=0.0, reply_published_ago=None):
    node = dn.DialogNode.__new__(dn.DialogNode)
    now = time.monotonic()
    node._state = state
    node._state_since = now - in_state_for
    node._reply_published_at = None if reply_published_ago is None else now - reply_published_ago
    node.events = []
    node._publish_state = lambda s: node.events.append(dn._NAMES[s])
    node._republish = lambda: node.events.append("heartbeat")
    node.get_logger = lambda: SimpleNamespace(warning=lambda *a, **k: None)
    return node


@pytest.mark.parametrize(
    "state, kwargs",
    [
        (dn._LISTENING, {"in_state_for": dn.LISTENING_TIMEOUT_S + 1}),
        (dn._THINKING, {"in_state_for": 30, "reply_published_ago": dn.REPLY_START_TIMEOUT_S + 1}),
        (dn._SPEAKING, {"in_state_for": dn.SPEAKING_TIMEOUT_S + 1}),
    ],
)
def test_a_stuck_turn_returns_to_idle_so_the_wake_word_listens_again(state, kwargs):
    node = _watched(state, **kwargs)
    node._on_watchdog()
    assert node.events == ["IDLE"]
    assert node._reply_published_at is None


@pytest.mark.parametrize(
    "state, kwargs",
    [
        (dn._IDLE, {"in_state_for": 3600}),
        (dn._LISTENING, {"in_state_for": 2}),
        (dn._THINKING, {"in_state_for": 50}),       # the model host is allowed its time
        (dn._THINKING, {"in_state_for": 5, "reply_published_ago": 2}),
        (dn._SPEAKING, {"in_state_for": 30}),
    ],
)
def test_a_healthy_state_is_only_republished(state, kwargs):
    node = _watched(state, **kwargs)
    node._on_watchdog()
    assert node.events == ["heartbeat"]
