"""The panel's view of `neo --model ... up`, and asking it questions.

Two separate concerns that together make the panel a client of the model host
rather than a read-only observer of whatever the CLI last did:

* `/api/model/status` -- is a model up, which one, and does it match what chat
  is configured to ask for.
* `/api/dialog/ask`   -- ask it, through the same code path as `neo --prompt`.
"""

from __future__ import annotations

import json
import time

import pytest

from model_conn.status_store import (
    HOST_STALE_AFTER_S,
    host_is_fresh,
    read_status,
    write_host_status,
)


@pytest.fixture
def host_path(tmp_path):
    return tmp_path / "model_conn" / "host.json"


def serve(path, **overrides):
    payload = dict(
        serving=True,
        model_name="qwen2.5-3b-instruct-q4-k-m",
        model_path="/models/qwen.gguf",
        endpoint="https://192.168.50.1:11434",
        tls_enabled=True,
    )
    payload.update(overrides)
    write_host_status(path=path, **payload)


class TestHostStatusFile:
    def test_round_trip(self, host_path):
        serve(host_path)
        payload = read_status(host_path)

        assert payload["serving"] is True
        assert payload["model_name"] == "qwen2.5-3b-instruct-q4-k-m"
        assert payload["tls_enabled"] is True
        assert host_is_fresh(payload) is True

    def test_a_missing_file_is_not_fresh(self, tmp_path):
        assert host_is_fresh(read_status(tmp_path / "nope.json")) is False

    def test_a_corrupt_file_is_not_fresh(self, host_path):
        host_path.parent.mkdir(parents=True)
        host_path.write_text("{{{", encoding="utf-8")
        assert host_is_fresh(read_status(host_path)) is False

    def test_a_killed_host_goes_stale(self, host_path):
        """No clean shutdown ran, so only age can reveal it."""
        serve(host_path)
        payload = read_status(host_path)
        assert host_is_fresh(payload, now=time.time() + HOST_STALE_AFTER_S + 1) is False

    def test_a_clean_shutdown_is_visible_immediately(self, host_path):
        """Ctrl-C writes serving=false rather than waiting out the timeout."""
        serve(host_path)
        write_host_status(serving=False, path=host_path)
        payload = read_status(host_path)

        assert host_is_fresh(payload) is True, "the file is recent"
        assert payload["serving"] is False, "but it says it stopped"

    def test_writes_are_atomic(self, host_path):
        for _ in range(15):
            serve(host_path)
            assert json.loads(host_path.read_text(encoding="utf-8"))
        assert not list(host_path.parent.glob(".host-*.tmp"))

    def test_a_write_to_an_impossible_path_does_not_raise(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        write_host_status(serving=True, path=blocker / "sub" / "host.json")


class TestModelStatusRoute:
    def patch_sources(self, monkeypatch, host_path, configured: str):
        from neo_webapp import model_status

        class FakeMcConfig:
            host_status_path = host_path

            @classmethod
            def load(cls):
                return cls()

        monkeypatch.setattr(
            model_status, "_configured_model", lambda: configured
        )
        # Patch the import inside read_model_status by pre-seeding the module.
        import sys
        import types

        fake = types.ModuleType("model_conn.config")
        fake.Config = FakeMcConfig
        monkeypatch.setitem(sys.modules, "model_conn.config", fake)

    def test_requires_a_session(self, client):
        assert client.get("/api/model/status").status_code == 401

    def test_reports_a_serving_host(self, auth_client, monkeypatch, host_path):
        serve(host_path)
        self.patch_sources(monkeypatch, host_path, "qwen2.5-3b-instruct-q4-k-m")

        body = auth_client.get("/api/model/status").json()
        assert body["reachable"] is True
        assert body["serving"] is True
        assert body["model_name"] == "qwen2.5-3b-instruct-q4-k-m"
        assert body["mismatch"] is False
        assert body["warnings"] == []

    def test_reports_no_host(self, auth_client, monkeypatch, tmp_path):
        self.patch_sources(monkeypatch, tmp_path / "absent.json", "qwen2.5:3b")
        body = auth_client.get("/api/model/status").json()

        assert body["reachable"] is False
        assert body["serving"] is False
        assert body["summary"] == "no model host running"

    def test_a_stale_file_does_not_claim_a_live_model(
        self, auth_client, monkeypatch, host_path
    ):
        """The host was killed; the panel must not keep reporting it up."""
        serve(host_path)
        old = json.loads(host_path.read_text(encoding="utf-8"))
        old["written_at_unix"] = time.time() - (HOST_STALE_AFTER_S + 5)
        host_path.write_text(json.dumps(old), encoding="utf-8")
        self.patch_sources(monkeypatch, host_path, "qwen2.5:3b")

        body = auth_client.get("/api/model/status").json()
        assert body["reachable"] is False
        assert body["model_name"] == "", "stale claims must not be repeated"

    def test_a_model_name_mismatch_is_reported(
        self, auth_client, monkeypatch, host_path
    ):
        """The silent footgun: chat 404s and looks like the link being down."""
        serve(host_path, model_name="qwen2.5-3b-instruct-q4-k-m")
        self.patch_sources(monkeypatch, host_path, "qwen2.5:3b")

        body = auth_client.get("/api/model/status").json()
        assert body["mismatch"] is True
        assert "qwen2.5:3b" in body["warnings"][0]
        assert "qwen2.5-3b-instruct-q4-k-m" in body["warnings"][0]

    def test_an_unset_chat_model_is_reported(
        self, auth_client, monkeypatch, host_path
    ):
        serve(host_path)
        self.patch_sources(monkeypatch, host_path, "")

        body = auth_client.get("/api/model/status").json()
        assert body["warnings"], "an unset model name must not be silent"
        assert "chat.model_name" in body["warnings"][0]

    def test_a_stopped_host_is_not_a_mismatch(
        self, auth_client, monkeypatch, host_path
    ):
        """Nothing is being served, so there is nothing to disagree with."""
        write_host_status(serving=False, path=host_path)
        self.patch_sources(monkeypatch, host_path, "qwen2.5:3b")

        body = auth_client.get("/api/model/status").json()
        assert body["mismatch"] is False
        assert body["summary"] == "model host stopped"


class TestAskRoute:
    def test_requires_a_session(self, client):
        assert client.post("/api/dialog/ask", json={"text": "hi"}).status_code == 401

    def test_empty_text_is_refused(self, auth_client):
        assert auth_client.post("/api/dialog/ask", json={"text": "   "}).status_code == 400

    def test_an_overlong_prompt_is_refused(self, auth_client):
        res = auth_client.post("/api/dialog/ask", json={"text": "x" * 5000})
        assert res.status_code == 413

    def test_a_reply_is_returned(self, auth_client, monkeypatch):
        import intelligence.chat as chat

        class Result:
            reply = "CS-204 is on the second floor of B block."
            source = "ollama"
            host = "192.168.50.1:11434"
            latency_ms = 412.0

        monkeypatch.setattr(chat, "ask", lambda text, **kw: Result())
        body = auth_client.post("/api/dialog/ask", json={"text": "where is CS-204"}).json()

        assert body["reply"].startswith("CS-204")
        assert body["source"] == "ollama"
        assert body["latency_ms"] == pytest.approx(412.0)

    def test_degraded_is_a_normal_answer_not_an_error(self, auth_client, monkeypatch):
        """The host is a daily-driver laptop; unreachable is the common case."""
        import intelligence.chat as chat

        class Result:
            reply = "I can't reach my language model right now."
            source = "degraded"
            host = ""
            latency_ms = 0.0

        monkeypatch.setattr(chat, "ask", lambda text, **kw: Result())
        res = auth_client.post("/api/dialog/ask", json={"text": "hello"})

        assert res.status_code == 200
        assert res.json()["source"] == "degraded"

    def test_a_crash_in_chat_is_reported_not_swallowed(self, auth_client, monkeypatch):
        import intelligence.chat as chat

        def boom(text, **kw):
            raise RuntimeError("config on fire")

        monkeypatch.setattr(chat, "ask", boom)
        res = auth_client.post("/api/dialog/ask", json={"text": "hello"})

        assert res.status_code == 502
        assert "config on fire" in res.json()["detail"]
