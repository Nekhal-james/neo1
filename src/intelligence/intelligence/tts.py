"""Local text-to-speech via Piper.

Piper is local per CLAUDE.md/plan Phase 5 (not an off-board call) -- it's the
one part of the audio stack that never depends on the laptop being present.
`piper-tts` is lazy-imported, same discipline as asr.py's vosk import.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

from .config import Config


def synthesize(text: str, cfg: Config) -> bytes:
    """Returns WAV bytes."""
    if not cfg.tts.piper_model_path:
        raise RuntimeError(
            "tts.piper_model_path is not configured -- set it in "
            "config/intelligence.local.yaml to a downloaded Piper .onnx voice"
        )
    model_path = Path(cfg.tts.piper_model_path)
    if not model_path.exists():
        raise RuntimeError(f"piper voice model not found: {model_path}")

    try:
        from piper import PiperVoice
    except ImportError as exc:
        raise RuntimeError(
            "piper-tts not installed -- pip install -e '.[voice]' from the repo root"
        ) from exc

    config_path = Path(cfg.tts.piper_config_path) if cfg.tts.piper_config_path else None
    voice = PiperVoice.load(str(model_path), config_path=str(config_path) if config_path else None)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize(text, wav_file)
    return buf.getvalue()
