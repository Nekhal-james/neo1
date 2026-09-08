from __future__ import annotations

from pathlib import Path

import pytest

from model_conn import tls
from model_conn.config import Config


def test_scheme_http_when_disabled():
    cfg = Config()
    cfg.tls.enabled = False
    assert tls.scheme(cfg) == "http"


def test_scheme_https_when_enabled():
    cfg = Config()
    cfg.tls.enabled = True
    assert tls.scheme(cfg) == "https"


def test_client_request_kwargs_empty_when_disabled():
    cfg = Config()
    cfg.tls.enabled = False
    assert tls.client_request_kwargs(cfg) == {}


def test_client_request_kwargs_raises_when_certs_missing(tmp_path: Path):
    cfg = Config()
    cfg.tls.enabled = True
    cfg.tls.ca_cert = str(tmp_path / "nope-ca.pem")
    cfg.tls.client_cert = str(tmp_path / "nope-client.pem")
    cfg.tls.client_key = str(tmp_path / "nope-client-key.pem")
    with pytest.raises(tls.TlsError, match="run `neo --tls init`"):
        tls.client_request_kwargs(cfg)


def test_client_request_kwargs_returns_cert_and_verify_when_present(tmp_path: Path):
    cfg = Config()
    cfg.tls.enabled = True
    for name in ("ca_cert", "client_cert", "client_key"):
        p = tmp_path / f"{name}.pem"
        p.write_text("fake", encoding="utf-8")
        setattr(cfg.tls, name, str(p))

    kwargs = tls.client_request_kwargs(cfg)
    assert kwargs["cert"] == (str(tmp_path / "client_cert.pem"), str(tmp_path / "client_key.pem"))
    assert kwargs["verify"] == str(tmp_path / "ca_cert.pem")


def test_server_ssl_kwargs_raises_when_certs_missing(tmp_path: Path):
    cfg = Config()
    cfg.tls.ca_cert = str(tmp_path / "nope-ca.pem")
    cfg.tls.server_cert = str(tmp_path / "nope-server.pem")
    cfg.tls.server_key = str(tmp_path / "nope-server-key.pem")
    with pytest.raises(tls.TlsError, match="run `neo --tls init`"):
        tls.server_ssl_kwargs(cfg)


def test_server_ssl_kwargs_returns_uvicorn_kwargs_when_present(tmp_path: Path):
    import ssl

    cfg = Config()
    for name in ("ca_cert", "server_cert", "server_key"):
        p = tmp_path / f"{name}.pem"
        p.write_text("fake", encoding="utf-8")
        setattr(cfg.tls, name, str(p))

    kwargs = tls.server_ssl_kwargs(cfg)
    assert kwargs["ssl_certfile"] == str(tmp_path / "server_cert.pem")
    assert kwargs["ssl_keyfile"] == str(tmp_path / "server_key.pem")
    assert kwargs["ssl_ca_certs"] == str(tmp_path / "ca_cert.pem")
    assert kwargs["ssl_cert_reqs"] == ssl.CERT_REQUIRED


def test_warn_insecure_prints_context(capsys):
    tls.warn_insecure("connection:status")
    out = capsys.readouterr().out
    assert "connection:status" in out
    assert "NO mTLS" in out
