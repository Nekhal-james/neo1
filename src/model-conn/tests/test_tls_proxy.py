"""End-to-end mTLS proof: a real TLS handshake through the proxy, not mocks.

This is the one place worth paying for real sockets and real certs (via
certs.py against a temp CA) instead of monkeypatching -- the entire point of
this module is that the handshake itself enforces the client cert, which a
mock can't prove.
"""

from __future__ import annotations

import http.server
import socket
import threading
import time
from pathlib import Path

import pytest

from model_conn import certs, tls
from model_conn.config import Config

pytest.importorskip("uvicorn", reason="fastapi/uvicorn are pip-only (see neo_webapp), not "
                                       "installed under colcon test's system Python")

from model_conn.tls_proxy import start_proxy_thread  # noqa: E402 -- after the guard above


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _EchoUpstream(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"hello from ollama")

    def log_message(self, *a):
        pass  # keep test output clean


@pytest.fixture
def tls_cfg(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.tls.enabled = True
    cfg.tls.ca_cert = str(tmp_path / "ca-cert.pem")
    cfg.tls.ca_key = str(tmp_path / "ca-key.pem")
    cfg.tls.server_cert = str(tmp_path / "server-cert.pem")
    cfg.tls.server_key = str(tmp_path / "server-key.pem")
    cfg.tls.client_cert = str(tmp_path / "client-cert.pem")
    cfg.tls.client_key = str(tmp_path / "client-key.pem")
    cfg.host.bind_host = "127.0.0.1"
    cfg.host.ollama_port = _free_port()
    cfg.host.internal_ollama_port = _free_port()

    certs.generate_ca(cfg.ca_cert_path, cfg.ca_key_path)
    certs.generate_server_cert(
        ca_cert_path=cfg.ca_cert_path,
        ca_key_path=cfg.ca_key_path,
        cert_path=cfg.server_cert_path,
        key_path=cfg.server_key_path,
        dns_names=["localhost"],
        ip_addresses=["127.0.0.1"],
    )
    certs.generate_client_cert(
        ca_cert_path=cfg.ca_cert_path,
        ca_key_path=cfg.ca_key_path,
        cert_path=cfg.client_cert_path,
        key_path=cfg.client_key_path,
    )
    return cfg


@pytest.fixture
def fake_ollama(tls_cfg: Config):
    server = http.server.HTTPServer(("127.0.0.1", tls_cfg.host.internal_ollama_port), _EchoUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


@pytest.fixture
def proxy(tls_cfg: Config, fake_ollama):
    start_proxy_thread(tls_cfg)
    # uvicorn's Server.run() binds asynchronously; give it a moment.
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((tls_cfg.host.bind_host, tls_cfg.host.ollama_port)) == 0:
                break
        time.sleep(0.1)
    else:
        pytest.fail("proxy did not start listening in time")
    return tls_cfg


def test_request_with_valid_client_cert_succeeds(proxy: Config):
    import requests

    resp = requests.get(
        f"https://127.0.0.1:{proxy.host.ollama_port}/anything",
        **tls.client_request_kwargs(proxy),
        timeout=5,
    )
    assert resp.status_code == 200
    assert resp.text == "hello from ollama"


def test_request_with_no_client_cert_is_rejected(proxy: Config):
    import requests

    # OpenSSL surfaces "no/invalid client cert" as either a TLS alert
    # (SSLError) or a bare connection reset, depending on exactly when in the
    # handshake it gives up -- both mean the same thing: rejected, not served.
    with pytest.raises((requests.exceptions.SSLError, requests.exceptions.ConnectionError)):
        requests.get(
            f"https://127.0.0.1:{proxy.host.ollama_port}/anything",
            verify=str(proxy.ca_cert_path),
            timeout=5,
        )


def test_request_with_wrong_ca_cert_is_rejected(proxy: Config, tmp_path):
    """A client cert not signed by *our* CA must be refused, not just any cert."""
    import requests

    other_ca_cert = tmp_path / "other-ca-cert.pem"
    other_ca_key = tmp_path / "other-ca-key.pem"
    other_client_cert = tmp_path / "other-client-cert.pem"
    other_client_key = tmp_path / "other-client-key.pem"
    certs.generate_ca(other_ca_cert, other_ca_key)
    certs.generate_client_cert(
        ca_cert_path=other_ca_cert,
        ca_key_path=other_ca_key,
        cert_path=other_client_cert,
        key_path=other_client_key,
    )

    with pytest.raises((requests.exceptions.SSLError, requests.exceptions.ConnectionError)):
        requests.get(
            f"https://127.0.0.1:{proxy.host.ollama_port}/anything",
            cert=(str(other_client_cert), str(other_client_key)),
            verify=str(proxy.ca_cert_path),
            timeout=5,
        )
