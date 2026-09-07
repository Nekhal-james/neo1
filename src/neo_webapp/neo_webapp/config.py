"""Configuration loading.

Resolution order: $NEO_WEBAPP_CONFIG, then config/webapp.local.yaml, then
config/webapp.yaml. The local file holds the password hash and session secret and
is gitignored; the committed file holds no secrets.
"""

from __future__ import annotations

import os
import secrets
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
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    bridge_backend: str = "auto"
    source_path: Path | None = None

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
        server_raw = raw.get("server", {}) or {}
        tls_raw = server_raw.get("tls", {}) or {}
        auth_raw = raw.get("auth", {}) or {}
        media_raw = raw.get("media", {}) or {}
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
            bridge_backend=(raw.get("bridge", {}) or {}).get("backend", "auto"),
        )


def _config_path(explicit: str | Path | None) -> Path | None:
    if explicit:
        return Path(explicit)
    env = os.environ.get("NEO_WEBAPP_CONFIG")
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


def write_local(updates: dict[str, Any]) -> Path:
    """Merge `updates` into config/webapp.local.yaml, creating it if needed."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if LOCAL_CONFIG.exists():
        existing = yaml.safe_load(LOCAL_CONFIG.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(existing, updates)
    LOCAL_CONFIG.write_text(
        yaml.safe_dump(merged, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return LOCAL_CONFIG


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
