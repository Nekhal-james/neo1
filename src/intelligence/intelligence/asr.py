"""Local speech-to-text via Vosk.

Vosk is the only engine implemented: it's always available (no off-board
dependency), which is the CLAUDE.md invariant that ASR runs on the Pi.
Off-board Whisper (an accuracy upgrade when the link is healthy) is Phase 6
scope and not wired up here -- `cfg.asr.engine` exists so that policy has
somewhere to live once it does.

`vosk` is lazy-imported (not a hard dependency of this package) so
`intelligence` stays importable without the `voice` extra -- same discipline
as neo_perception's detector.py importing ultralytics/cv2 lazily.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import Config


@dataclass
class TranscriptResult:
    text: str
    engine: str = "vosk"


def transcribe(audio_bytes: bytes, sample_rate: int, cfg: Config) -> TranscriptResult:
    if not cfg.asr.vosk_model_path:
        raise RuntimeError(
            "asr.vosk_model_path is not configured -- set it in "
            "config/intelligence.local.yaml to a downloaded Vosk model directory"
        )
    model_path = Path(cfg.asr.vosk_model_path)
    if not model_path.exists():
        raise RuntimeError(f"vosk model not found: {model_path}")

    try:
        from vosk import KaldiRecognizer, Model
    except ImportError as exc:
        raise RuntimeError(
            "vosk not installed -- pip install -e '.[voice]' from the repo root"
        ) from exc

    model = Model(str(model_path))
    recognizer = KaldiRecognizer(model, sample_rate)
    recognizer.AcceptWaveform(audio_bytes)
    result = json.loads(recognizer.FinalResult())
    return TranscriptResult(text=result.get("text", ""), engine="vosk")
