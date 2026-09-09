"""The host status file `neo --model ... up` publishes.

The receiver's link status answers "can I reach the host". This answers "is a
model up, and which one" -- which nothing on the receiver side can, and which
the admin panel needs, because asking for the wrong model name fails as a 404
that looks exactly like the link being down.
"""

from __future__ import annotations

import time

import pytest

from model_conn.ollama import _start_host_heartbeat
from model_conn.status_store import (
    HOST_STALE_AFTER_S,
    host_is_fresh,
    read_status,
    write_host_status,
)


@pytest.fixture
def host_path(tmp_path):
    return tmp_path / "host.json"


def test_round_trip(host_path):
    write_host_status(
        serving=True,
        model_name="qwen2.5:3b",
        model_path="/models/q.gguf",
        endpoint="https://10.0.0.1:11434",
        tls_enabled=True,
        path=host_path,
    )
    payload = read_status(host_path)

    assert payload["serving"] is True
    assert payload["model_name"] == "qwen2.5:3b"
    assert payload["endpoint"] == "https://10.0.0.1:11434"
    assert host_is_fresh(payload)


def test_freshness_expires(host_path):
    write_host_status(serving=True, path=host_path)
    payload = read_status(host_path)
    assert host_is_fresh(payload, now=time.time() + HOST_STALE_AFTER_S + 1) is False


def test_missing_and_corrupt_files_are_not_fresh(tmp_path):
    assert host_is_fresh(read_status(tmp_path / "absent.json")) is False
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert host_is_fresh(read_status(bad)) is False


class TestHeartbeat:
    """A one-shot write would keep claiming the model was up long after the
    process was killed -- and killed is the normal way a foreground `up` ends."""

    def config_for(self, host_path, monkeypatch):
        from model_conn.config import Config

        cfg = Config()
        monkeypatch.setattr(
            type(cfg), "host_status_path", property(lambda self: host_path)
        )
        return cfg

    def test_publishes_immediately(self, host_path, monkeypatch):
        cfg = self.config_for(host_path, monkeypatch)
        stop = _start_host_heartbeat(
            cfg, model_name="m", model_path="/p", endpoint="http://x"
        )
        try:
            payload = read_status(host_path)
            assert payload["serving"] is True
            assert payload["model_name"] == "m"
        finally:
            stop()

    def test_stopping_marks_it_not_serving(self, host_path, monkeypatch):
        """Clean Ctrl-C shows up in the panel at once, not after the timeout."""
        cfg = self.config_for(host_path, monkeypatch)
        stop = _start_host_heartbeat(
            cfg, model_name="m", model_path="/p", endpoint="http://x"
        )
        stop()

        payload = read_status(host_path)
        assert payload["serving"] is False
        assert host_is_fresh(payload) is True, "recent, but reporting stopped"

    def test_the_heartbeat_thread_does_not_outlive_stop(self, host_path, monkeypatch):
        import threading

        cfg = self.config_for(host_path, monkeypatch)
        before = threading.active_count()
        stop = _start_host_heartbeat(
            cfg, model_name="m", model_path="/p", endpoint="http://x"
        )
        stop()
        time.sleep(0.1)
        assert threading.active_count() <= before
