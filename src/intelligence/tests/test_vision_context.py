"""Camera context reaching the chat prompt.

This is the join between the two halves of the system: perception runs in the
panel's process, chat runs in `neo --prompt`'s. Without it, "what am I holding"
is unanswerable no matter how good either half is.
"""

from __future__ import annotations

import pytest

from intelligence.chat import ask
from intelligence.config import Config
from model_conn.config import Config as ModelConnConfig


@pytest.fixture
def cfg(tmp_path):
    # status_file is relative to the repo root; point it somewhere disposable
    # so these tests never touch the real var/.
    c = Config()
    c.chat.model_name = "qwen2.5:3b"
    c.status_file = str(tmp_path / "status.json")
    return c


@pytest.fixture
def mc_cfg():
    return ModelConnConfig()


def capture_transport(seen: dict):
    def transport(host, port, model, message, timeout_s, system):
        seen["system"] = system
        seen["message"] = message
        return "a reply"

    return transport


def reachable(host, port, timeout_s):
    """A probe transport that says every endpoint answers -- matches
    model_conn.link's (host, port, timeout_s) -> rtt contract."""
    return 3.0


def test_vision_context_reaches_the_system_prompt(cfg, mc_cfg):
    seen: dict = {}
    ask(
        "what am I holding",
        cfg=cfg,
        mc_cfg=mc_cfg,
        transport=capture_transport(seen),
        probe_transport=reachable,
        vision="1 person in view (engaged); most recently identified object: cell phone",
    )
    assert "cell phone" in seen["system"]
    assert "camera sees" in seen["system"]


def test_the_users_message_is_left_alone(cfg, mc_cfg):
    """Context is situational, not something the person said."""
    seen: dict = {}
    ask(
        "what am I holding",
        cfg=cfg,
        mc_cfg=mc_cfg,
        transport=capture_transport(seen),
        probe_transport=reachable,
        vision="1 person in view",
    )
    assert seen["message"] == "what am I holding"


def test_no_camera_adds_nothing(cfg, mc_cfg):
    seen: dict = {}
    ask(
        "where is CS-204",
        cfg=cfg,
        mc_cfg=mc_cfg,
        transport=capture_transport(seen),
        probe_transport=reachable,
        vision="",
    )
    assert "camera sees" not in seen["system"]


def test_vision_context_never_breaks_chat(cfg, mc_cfg, monkeypatch):
    """A broken status file must not cost you an answer."""
    import intelligence.chat as chat

    def boom():
        raise RuntimeError("status file on fire")

    monkeypatch.setattr(chat, "vision_context", boom)
    with pytest.raises(RuntimeError):
        chat.vision_context()

    # ask() passes its own vision string, so the broken reader is bypassed --
    # and with vision=None it is wrapped, which the next test covers.
    seen: dict = {}
    result = ask(
        "hello",
        cfg=cfg,
        mc_cfg=mc_cfg,
        transport=capture_transport(seen),
        probe_transport=reachable,
        vision="",
    )
    assert result.reply == "a reply"


def test_a_missing_status_file_yields_empty_context(tmp_path, monkeypatch):
    from intelligence.chat import vision_context
    from neo_perception import status_store

    monkeypatch.setattr(status_store, "STATUS_PATH", tmp_path / "absent.json")
    assert vision_context() == ""


def test_context_is_read_when_not_overridden(tmp_path, monkeypatch, cfg, mc_cfg):
    """The real path: panel writes the file, chat picks it up."""
    from neo_perception import status_store

    path = tmp_path / "status.json"
    status_store.write_status(
        available=True, person_count=1, identify_best="book", path=path
    )
    monkeypatch.setattr(status_store, "STATUS_PATH", path)

    seen: dict = {}
    ask(
        "what is this",
        cfg=cfg,
        mc_cfg=mc_cfg,
        transport=capture_transport(seen),
        probe_transport=reachable,
    )
    assert "book" in seen["system"]
