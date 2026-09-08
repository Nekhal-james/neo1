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
    timeout_s: float = 8.0


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
        p = _config_path(path)
        raw: dict[str, Any] = {}
        if p is not None and p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cfg = cls._from_raw(raw)
        cfg.source_path = p
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
                timeout_s=float(chat_raw.get("timeout_s", 8.0)),
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


def _config_path(explicit: str | Path | None) -> Path | None:
    if explicit:
        return Path(explicit)
    env = os.environ.get("NEO_INTELLIGENCE_CONFIG")
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
