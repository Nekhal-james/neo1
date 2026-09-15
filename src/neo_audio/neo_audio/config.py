"""Configuration for the audio path.

Resolution order: $NEO_AUDIO_CONFIG, then config/audio.yaml with
config/audio.local.yaml merged over it. Same shape and the same reasoning as
`intelligence.config` -- see that module's `_config_layers` docstring for why
the local file is merged rather than substituted, and `resolve_path` for why
relative paths resolve against the repo root and never the working directory.

The per-machine file is where a wake word model path and an ALSA device name
belong: both differ between the Pi, WSL and a developer's laptop, and neither
is a secret.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .endpointer import EndpointerConfig
from .wakeword import WakeWordConfig

# repo root: .../neo1/src/neo_audio/neo_audio/config.py -> up 3
REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_CONFIG = CONFIG_DIR / "audio.yaml"
LOCAL_CONFIG = CONFIG_DIR / "audio.local.yaml"


@dataclass
class MicConfig:
    device: str = ""
    """An ALSA name like `plughw:2,0`. Empty means "the first capture device",
    which is right on a Pi with exactly one USB mic and wrong the moment there
    are two -- name it explicitly then."""

    sample_rate: int = 16000
    """16 kHz: what /audio/in promises and what Vosk wants. The wake word
    resamples to 44.1 kHz internally; it only reads 0-5 kHz, so nothing it
    needs is lost by capturing here."""

    chunk_ms: int = 100


@dataclass
class SpeakerConfig:
    device: str = ""
    sample_rate: int = 22050
    """What Piper produces, and what /audio/out promises."""


@dataclass
class Config:
    mic: MicConfig = field(default_factory=MicConfig)
    speaker: SpeakerConfig = field(default_factory=SpeakerConfig)
    wakeword: WakeWordConfig = field(default_factory=WakeWordConfig)
    endpointer: EndpointerConfig = field(default_factory=EndpointerConfig)
    source_path: Path | None = None

    @property
    def wakeword_model_path(self) -> Path | None:
        """The configured model, as an absolute path, or None if unset."""
        if not self.wakeword.model_path:
            return None
        return resolve_path(self.wakeword.model_path)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        layers = _config_layers(path)
        raw: dict[str, Any] = {}
        for layer in layers:
            raw = _deep_merge(raw, yaml.safe_load(layer.read_text(encoding="utf-8")) or {})
        cfg = cls._from_raw(raw)
        cfg.source_path = layers[-1] if layers else None
        return cfg

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> Config:
        mic = raw.get("mic", {}) or {}
        speaker = raw.get("speaker", {}) or {}
        wake = raw.get("wakeword", {}) or {}
        end = raw.get("endpointer", {}) or {}

        # Every default below is read off the dataclass rather than written out
        # again: a loader that restates a default is a second place for it to
        # drift, and chat.timeout_s drifted exactly that way once.
        resolved = cls(
            mic=MicConfig(
                device=mic.get("device", MicConfig.device),
                sample_rate=int(mic.get("sample_rate", MicConfig.sample_rate)),
                chunk_ms=int(mic.get("chunk_ms", MicConfig.chunk_ms)),
            ),
            speaker=SpeakerConfig(
                device=speaker.get("device", SpeakerConfig.device),
                sample_rate=int(speaker.get("sample_rate", SpeakerConfig.sample_rate)),
            ),
            wakeword=WakeWordConfig(
                model_path=wake.get("model_path", WakeWordConfig.model_path),
                threshold=float(wake.get("threshold", WakeWordConfig.threshold)),
                threshold_with_person=float(
                    wake.get("threshold_with_person", WakeWordConfig.threshold_with_person)
                ),
                consecutive_hits=int(wake.get("consecutive_hits", WakeWordConfig.consecutive_hits)),
                refractory_s=float(wake.get("refractory_s", WakeWordConfig.refractory_s)),
                hop_frames=int(wake.get("hop_frames", WakeWordConfig.hop_frames)),
            ),
            endpointer=EndpointerConfig(
                silence_to_end_s=float(
                    end.get("silence_to_end_s", EndpointerConfig.silence_to_end_s)
                ),
                max_wait_for_speech_s=float(
                    end.get("max_wait_for_speech_s", EndpointerConfig.max_wait_for_speech_s)
                ),
                max_utterance_s=float(end.get("max_utterance_s", EndpointerConfig.max_utterance_s)),
                speech_start_ratio=float(
                    end.get("speech_start_ratio", EndpointerConfig.speech_start_ratio)
                ),
                min_speech_s=float(end.get("min_speech_s", EndpointerConfig.min_speech_s)),
                noise_floor_alpha=float(
                    end.get("noise_floor_alpha", EndpointerConfig.noise_floor_alpha)
                ),
            ),
        )
        return resolved


def _config_layers(explicit: str | Path | None) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    env = os.environ.get("NEO_AUDIO_CONFIG")
    if env:
        return [Path(env)]
    return [p for p in (DEFAULT_CONFIG, LOCAL_CONFIG) if p.exists()]


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_path(p: str | Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (REPO_ROOT / path)
