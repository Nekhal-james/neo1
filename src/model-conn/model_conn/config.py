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
    enabled: bool = False


@dataclass
class Config:
    host: HostConfig = field(default_factory=HostConfig)
    receiver: ReceiverConfig = field(default_factory=ReceiverConfig)
    tls: TlsConfig = field(default_factory=TlsConfig)
    status_file: str = "var/model_conn/status.json"
    source_path: Path | None = None

    @property
    def status_path(self) -> Path:
        return _resolve(self.status_file)

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
            ),
            receiver=ReceiverConfig(
                endpoints=[_endpoint_from_raw(e) for e in endpoints_raw],
                probe_interval_s=float(receiver_raw.get("probe_interval_s", 2.0)),
                probe_timeout_s=float(receiver_raw.get("probe_timeout_s", 1.0)),
                ping_count=int(receiver_raw.get("ping_count", 5)),
            ),
            tls=TlsConfig(enabled=bool(tls_raw.get("enabled", False))),
            status_file=raw.get("status_file", "var/model_conn/status.json"),
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
