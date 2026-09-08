from __future__ import annotations

from pathlib import Path

from model_conn.config import Config
from model_conn.link import _http_get, transport_for


def test_transport_for_returns_plain_http_get_when_disabled():
    cfg = Config()
    cfg.tls.enabled = False
    assert transport_for(cfg) is _http_get


def test_transport_for_returns_https_closure_when_enabled(tmp_path: Path, monkeypatch):
    cfg = Config()
    cfg.tls.enabled = True
    for name in ("ca_cert", "client_cert", "client_key"):
        p = tmp_path / f"{name}.pem"
        p.write_text("fake", encoding="utf-8")
        setattr(cfg.tls, name, str(p))

    captured = {}

    class FakeResp:
        ok = True

        def raise_for_status(self):
            pass

    def fake_get(url, timeout, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResp()

    import requests

    monkeypatch.setattr(requests, "get", fake_get)

    transport = transport_for(cfg)
    assert transport is not _http_get
    transport("some-host", 11434, 1.0)

    assert captured["url"] == "https://some-host:11434/api/tags"
    assert captured["kwargs"]["cert"] == (
        str(tmp_path / "client_cert.pem"),
        str(tmp_path / "client_key.pem"),
    )
    assert captured["kwargs"]["verify"] == str(tmp_path / "ca_cert.pem")
