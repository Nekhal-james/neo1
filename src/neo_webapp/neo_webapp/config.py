"""Configuration loading.

Resolution order: $NEO_WEBAPP_CONFIG, then config/webapp.local.yaml, then
config/webapp.yaml. The local file holds the password hash and session secret and
is gitignored; the committed file holds no secrets.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# repo root: .../neo1/src/neo_webapp/neo_webapp/config.py -> up 3
REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_CONFIG = CONFIG_DIR / "webapp.yaml"
LOCAL_CONFIG = CONFIG_DIR / "webapp.local.yaml"


@dataclass
class TlsConfig:
    enabled: bool = True
    certfile: str = "certs/dev-cert.pem"
    keyfile: str = "certs/dev-key.pem"

    def resolved(self) -> tuple[Path, Path]:
        return (
            _resolve(self.certfile),
            _resolve(self.keyfile),
        )


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8443
    tls: TlsConfig = field(default_factory=TlsConfig)


@dataclass
class AuthConfig:
    username: str = "admin"
    password_hash: str = ""
    session_secret: str = ""
    recovery_hash: str = ""
    """argon2 hash of the one-time recovery code; empty means none is set."""
    session_max_age_s: int = 43200
    max_attempts: int = 5
    lockout_window_s: int = 300

    @property
    def configured(self) -> bool:
        return bool(self.password_hash)


@dataclass
class MediaConfig:
    camera_max_fps: int = 8
    camera_jpeg_quality: float = 0.6
    mic_sample_rate: int = 16000
    speaker_sample_rate: int = 22050
    joy_rate_hz: int = 20
    joy_deadman_ms: int = 300


@dataclass
class PerceptionConfig:
    """Perception for the *simulated* robot, so the Vision tab works with no Pi.

    On the real robot perception is a ROS node; the panel only observes it.
    """

    enabled: bool = True
    target_fps: float = 6.0

    status_interval_s: float = 1.0
    """How often to mirror vision state into var/perception/status.json for
    the other `neo` processes to read."""


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    perception: PerceptionConfig = field(default_factory=PerceptionConfig)
    bridge_backend: str = "auto"
    source_path: Path | None = None

    secrets_path: Path | None = None
    """The file a password change is written to: the one this config's secrets
    are read back from on the next start. None for a config built in code, which
    then cannot change its password from the panel."""

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        layers = _config_layers(path)
        raw: dict[str, Any] = {}
        for layer in layers:
            raw = _deep_merge(raw, yaml.safe_load(layer.read_text(encoding="utf-8")) or {})
        cfg = cls._from_raw(raw)
        cfg.source_path = layers[-1] if layers else None
        # A named file is the whole config, so it is where the hash lives.
        # Otherwise the gitignored local file -- never the committed one.
        named = path or os.environ.get("NEO_WEBAPP_CONFIG")
        cfg.secrets_path = Path(named) if named else LOCAL_CONFIG
        return cfg

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> Config:
        server_raw = raw.get("server", {}) or {}
        tls_raw = server_raw.get("tls", {}) or {}
        auth_raw = raw.get("auth", {}) or {}
        media_raw = raw.get("media", {}) or {}
        perception_raw = raw.get("perception", {}) or {}
        return cls(
            server=ServerConfig(
                host=server_raw.get("host", "0.0.0.0"),
                port=int(server_raw.get("port", 8443)),
                tls=TlsConfig(
                    enabled=bool(tls_raw.get("enabled", True)),
                    certfile=tls_raw.get("certfile", "certs/dev-cert.pem"),
                    keyfile=tls_raw.get("keyfile", "certs/dev-key.pem"),
                ),
            ),
            auth=AuthConfig(
                username=auth_raw.get("username", "admin"),
                password_hash=auth_raw.get("password_hash", ""),
                session_secret=auth_raw.get("session_secret", ""),
                recovery_hash=auth_raw.get("recovery_hash", ""),
                session_max_age_s=int(auth_raw.get("session_max_age_s", 43200)),
                max_attempts=int(auth_raw.get("max_attempts", 5)),
                lockout_window_s=int(auth_raw.get("lockout_window_s", 300)),
            ),
            media=MediaConfig(
                camera_max_fps=int(media_raw.get("camera_max_fps", 8)),
                camera_jpeg_quality=float(media_raw.get("camera_jpeg_quality", 0.6)),
                mic_sample_rate=int(media_raw.get("mic_sample_rate", 16000)),
                speaker_sample_rate=int(media_raw.get("speaker_sample_rate", 22050)),
                joy_rate_hz=int(media_raw.get("joy_rate_hz", 20)),
                joy_deadman_ms=int(media_raw.get("joy_deadman_ms", 300)),
            ),
            perception=PerceptionConfig(
                enabled=bool(perception_raw.get("enabled", True)),
                target_fps=float(perception_raw.get("target_fps", 6.0)),
                status_interval_s=float(
                    perception_raw.get("status_interval_s", 1.0)
                ),
            ),
            bridge_backend=(raw.get("bridge", {}) or {}).get("backend", "auto"),
        )


def _config_layers(explicit: str | Path | None) -> list[Path]:
    """The files to merge, in increasing precedence.

    An explicit path or `$NEO_WEBAPP_CONFIG` names *one* file and means exactly
    that file: layering something else over a config the caller named would be
    worse than surprising, and the test suite depends on it.

    Otherwise the local file is merged **over** the committed one, which is what
    config/webapp.yaml has always said happens. It did not: the local file
    replaced the committed one outright, so a local file holding just the
    password hash switched off every other value in webapp.yaml. Those fell back
    to the dataclass defaults, which mostly match the shipped YAML -- so the bug
    stayed invisible until one of them did not match, and the committed file
    that plainly said otherwise turned out to be inert.
    """
    if explicit:
        return [Path(explicit)]
    env = os.environ.get("NEO_WEBAPP_CONFIG")
    if env:
        return [Path(env)]
    return [p for p in (DEFAULT_CONFIG, LOCAL_CONFIG) if p.exists()]


def _resolve(p: str | Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (REPO_ROOT / path)


def write_local(updates: dict[str, Any], path: Path | None = None) -> Path:
    """Merge `updates` into config/webapp.local.yaml (or `path`), creating it if needed.

    Atomic: this file holds the only copy of the password hash, and a write torn
    by a power cut on an SD card would lock the operator out of the panel.
    """
    target = Path(path) if path is not None else LOCAL_CONFIG
    target.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if target.exists():
        existing = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(existing, updates)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(merged, fh, sort_keys=False, default_flow_style=False)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def new_session_secret() -> str:
    return secrets.token_urlsafe(48)
