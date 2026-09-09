"""Configuration loading.

Resolution order: $NEO_MODEL_CONN_CONFIG, then config/model_conn.local.yaml, then
config/model_conn.yaml. The local file is for per-machine overrides (e.g. a
different model path on your laptop) and is gitignored; the committed file holds
no secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

# repo root: .../neo1/src/model-conn/model_conn/config.py -> up 3
REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_CONFIG = CONFIG_DIR / "model_conn.yaml"
LOCAL_CONFIG = CONFIG_DIR / "model_conn.local.yaml"

PathName = Literal["eth", "wifi"]


@dataclass
class HostConfig:
    default_model_path: str = ""
    ollama_port: int = 11434
    bind_host: str = "0.0.0.0"
    idle_unload_minutes: int = 30
    # Ollama itself only ever binds to 127.0.0.1 on this port when tls.enabled --
    # the mTLS proxy (tls_proxy.py) takes the public bind_host:ollama_port
    # instead and forwards authenticated requests here. Irrelevant when TLS
    # is off: Ollama binds bind_host:ollama_port directly, as before.
    internal_ollama_port: int = 11435


@dataclass
class Endpoint:
    name: PathName
    host: str
    port: int


@dataclass
class ReceiverConfig:
    endpoints: list[Endpoint] = field(default_factory=list)
    probe_interval_s: float = 2.0
    probe_timeout_s: float = 1.0
    ping_count: int = 5


@dataclass
class TlsConfig:
    # Explicit opt-out for local/dev testing (e.g. everything on one laptop
    # loopback). CLAUDE.md/plan 0.3.1: real deployments must never run
    # without this -- see tls.warn_insecure(), which fires loudly whenever
    # this is False.
    enabled: bool = False
    ca_cert: str = "certs/model_conn/ca-cert.pem"
    # The CA's own private key -- needed only by `neo --tls init` to sign new
    # server/client certs, never by the server or the receiver at request time.
    ca_key: str = "certs/model_conn/ca-key.pem"
    server_cert: str = "certs/model_conn/server-cert.pem"
    server_key: str = "certs/model_conn/server-key.pem"
    client_cert: str = "certs/model_conn/client-cert.pem"
    client_key: str = "certs/model_conn/client-key.pem"
    # The admin panel's own server cert, issued by the SAME CA (plan Phase 1,
    # step 3). Separate from server_cert above, which is the laptop's: the two
    # are different machines with different names, and the panel's is the one
    # a phone has to trust. Sharing a CA is what makes that one install
    # instead of two.
    panel_cert: str = "certs/panel/panel-cert.pem"
    panel_key: str = "certs/panel/panel-key.pem"


@dataclass
class Config:
    host: HostConfig = field(default_factory=HostConfig)
    receiver: ReceiverConfig = field(default_factory=ReceiverConfig)
    tls: TlsConfig = field(default_factory=TlsConfig)
    status_file: str = "var/model_conn/status.json"
    host_status_file: str = "var/model_conn/host.json"
    source_path: Path | None = None

    @property
    def status_path(self) -> Path:
        return _resolve(self.status_file)

    @property
    def host_status_path(self) -> Path:
        """Where `neo --model ... up` publishes what it is serving, so the
        admin panel can show it without being on the same machine's CLI."""
        return _resolve(self.host_status_file)

    @property
    def ca_cert_path(self) -> Path:
        return _resolve(self.tls.ca_cert)

    @property
    def ca_key_path(self) -> Path:
        return _resolve(self.tls.ca_key)

    @property
    def server_cert_path(self) -> Path:
        return _resolve(self.tls.server_cert)

    @property
    def server_key_path(self) -> Path:
        return _resolve(self.tls.server_key)

    @property
    def client_cert_path(self) -> Path:
        return _resolve(self.tls.client_cert)

    @property
    def client_key_path(self) -> Path:
        return _resolve(self.tls.client_key)

    @property
    def panel_cert_path(self) -> Path:
        return _resolve(self.tls.panel_cert)

    @property
    def panel_key_path(self) -> Path:
        return _resolve(self.tls.panel_key)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        p = _config_path(path)
        raw: dict[str, Any] = {}
        if p is not None and p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cfg = cls._from_raw(raw)
        cfg.source_path = p
        return cfg

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> Config:
        host_raw = raw.get("host", {}) or {}
        receiver_raw = raw.get("receiver", {}) or {}
        tls_raw = raw.get("tls", {}) or {}
        endpoints_raw = receiver_raw.get("endpoints", []) or []
        return cls(
            host=HostConfig(
                default_model_path=host_raw.get("default_model_path", ""),
                ollama_port=int(host_raw.get("ollama_port", 11434)),
                bind_host=host_raw.get("bind_host", "0.0.0.0"),
                idle_unload_minutes=int(host_raw.get("idle_unload_minutes", 30)),
                internal_ollama_port=int(host_raw.get("internal_ollama_port", 11435)),
            ),
            receiver=ReceiverConfig(
                endpoints=[_endpoint_from_raw(e) for e in endpoints_raw],
                probe_interval_s=float(receiver_raw.get("probe_interval_s", 2.0)),
                probe_timeout_s=float(receiver_raw.get("probe_timeout_s", 1.0)),
                ping_count=int(receiver_raw.get("ping_count", 5)),
            ),
            tls=TlsConfig(
                enabled=bool(tls_raw.get("enabled", False)),
                ca_cert=tls_raw.get("ca_cert", "certs/model_conn/ca-cert.pem"),
                ca_key=tls_raw.get("ca_key", "certs/model_conn/ca-key.pem"),
                server_cert=tls_raw.get("server_cert", "certs/model_conn/server-cert.pem"),
                server_key=tls_raw.get("server_key", "certs/model_conn/server-key.pem"),
                client_cert=tls_raw.get("client_cert", "certs/model_conn/client-cert.pem"),
                client_key=tls_raw.get("client_key", "certs/model_conn/client-key.pem"),
                panel_cert=tls_raw.get("panel_cert", "certs/panel/panel-cert.pem"),
                panel_key=tls_raw.get("panel_key", "certs/panel/panel-key.pem"),
            ),
            status_file=raw.get("status_file", "var/model_conn/status.json"),
            host_status_file=raw.get(
                "host_status_file", "var/model_conn/host.json"
            ),
        )


_VALID_ENDPOINT_NAMES = ("eth", "wifi")


def _endpoint_from_raw(e: dict[str, Any]) -> Endpoint:
    name = e.get("name", "eth")
    if name not in _VALID_ENDPOINT_NAMES:
        raise ValueError(
            f"receiver.endpoints entry has name={name!r}, must be one of "
            f"{_VALID_ENDPOINT_NAMES} -- a typo here silently breaks eth/wifi failover"
        )
    return Endpoint(name=name, host=e.get("host", ""), port=int(e.get("port", 11434)))


def _config_path(explicit: str | Path | None) -> Path | None:
    if explicit:
        return Path(explicit)
    env = os.environ.get("NEO_MODEL_CONN_CONFIG")
    if env:
        return Path(env)
    if LOCAL_CONFIG.exists():
        return LOCAL_CONFIG
    if DEFAULT_CONFIG.exists():
        return DEFAULT_CONFIG
    return None


def _resolve(p: str | Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (REPO_ROOT / path)
