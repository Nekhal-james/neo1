"""Configuration loading.

Resolution order: $NEO_INTELLIGENCE_CONFIG, then config/intelligence.local.yaml,
then config/intelligence.yaml. Same shape as model_conn.config -- see that
module's docstring for the reasoning. The local file is for per-machine
overrides (a real vosk/piper model path, the actual chat model name) and is
gitignored; the committed file holds no secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# repo root: .../neo1/src/intelligence/intelligence/config.py -> up 3
REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_CONFIG = CONFIG_DIR / "intelligence.yaml"
LOCAL_CONFIG = CONFIG_DIR / "intelligence.local.yaml"


@dataclass
class ChatConfig:
    model_name: str = ""

    timeout_s: float = 60.0
    """How long to wait for a *reply*, once the host has answered the connect.

    Not the same question as "is the host there", and not the same answer: that
    one is `model_conn`'s `receiver.probe_timeout_s`, and it stays ~1 s so a
    dead link degrades promptly. This one has to survive a cold model load,
    which is ~45 s for a 3B model -- measured, not guessed. At the old 8 s the
    first question after any idle period timed out and returned the degraded
    reply, which on a reception desk is most first questions.

    Raising it costs nothing when the host is absent, because the connect
    timeout is what fails then, in about a second.
    """


@dataclass
class AsrConfig:
    engine: str = "vosk"
    vosk_model_path: str = ""
    sample_rate: int = 16000


@dataclass
class TtsConfig:
    engine: str = "piper"
    piper_model_path: str = ""
    piper_config_path: str = ""


@dataclass
class PromptsConfig:
    system_prompt_path: str = ""


@dataclass
class RagConfig:
    data_dir: str = "src/intelligence/rag/data"


@dataclass
class Config:
    chat: ChatConfig = field(default_factory=ChatConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    rag: RagConfig = field(default_factory=RagConfig)
    status_file: str = "var/intelligence/status.json"
    source_path: Path | None = None

    @property
    def status_path(self) -> Path:
        return _resolve(self.status_file)

    @property
    def rag_data_path(self) -> Path:
        return _resolve(self.rag.data_dir)

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
        chat_raw = raw.get("chat", {}) or {}
        asr_raw = raw.get("asr", {}) or {}
        tts_raw = raw.get("tts", {}) or {}
        prompts_raw = raw.get("prompts", {}) or {}
        rag_raw = raw.get("rag", {}) or {}
        return cls(
            chat=ChatConfig(
                model_name=chat_raw.get("model_name", ""),
                timeout_s=float(chat_raw.get("timeout_s", ChatConfig.timeout_s)),
            ),
            asr=AsrConfig(
                engine=asr_raw.get("engine", "vosk"),
                vosk_model_path=asr_raw.get("vosk_model_path", ""),
                sample_rate=int(asr_raw.get("sample_rate", 16000)),
            ),
            tts=TtsConfig(
                engine=tts_raw.get("engine", "piper"),
                piper_model_path=tts_raw.get("piper_model_path", ""),
                piper_config_path=tts_raw.get("piper_config_path", ""),
            ),
            prompts=PromptsConfig(
                system_prompt_path=prompts_raw.get("system_prompt_path", ""),
            ),
            rag=RagConfig(
                data_dir=rag_raw.get("data_dir", "src/intelligence/rag/data"),
            ),
            status_file=raw.get("status_file", "var/intelligence/status.json"),
        )


def _config_layers(explicit: str | Path | None) -> list[Path]:
    """The files to merge, in increasing precedence.

    An explicit path or `$NEO_INTELLIGENCE_CONFIG` names *one* file and means
    exactly that file -- naming a config and then having a second one silently
    layered over it would be worse than surprising.

    Otherwise the local file is merged **over** the committed one rather than
    replacing it. Replacing was the old behaviour and it was a quiet trap: a
    local file holding one key switched off every other value in the committed
    config, which then fell back to whatever the dataclasses happened to
    default to. Those defaults mostly match the shipped YAML, so nothing looked
    broken -- until one of them did not, and the committed file that plainly
    said otherwise was being ignored.
    """
    if explicit:
        return [Path(explicit)]
    env = os.environ.get("NEO_INTELLIGENCE_CONFIG")
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


def _resolve(p: str | Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (REPO_ROOT / path)


def resolve_path(p: str | Path) -> Path:
    """A configured path as this config means it: relative to the repo root.

    Not to the working directory. The same config file is read by `neo --webapp
    up` in WSL and by `neo --prompt` on Windows, each launched from wherever the
    operator happened to be; an absolute path cannot be right for both, and a
    CWD-relative one is only right by accident. Model paths were CWD-relative
    for a while, so `models/vosk-model-...` worked from the repo root and
    reported "model not found" from anywhere else.
    """
    return _resolve(p)
