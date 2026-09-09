"""Local speech-to-text via Vosk.

Vosk is the only engine implemented: it's always available (no off-board
dependency), which is the CLAUDE.md invariant that ASR runs on the Pi.
Off-board Whisper (an accuracy upgrade when the link is healthy) is Phase 6
scope and not wired up here -- `cfg.asr.engine` exists so that policy has
somewhere to live once it does.

`vosk` is lazy-imported (not a hard dependency of this package) so
`intelligence` stays importable without the `voice` extra -- same discipline
as neo_perception's detector.py importing ultralytics/cv2 lazily.

Two things here beyond a wrapper, both load-bearing for live use:

* **The model is cached.** Loading a Vosk model takes seconds, and it used to
  happen on every `transcribe()` call. That is survivable for one-shot CLI use
  and completely fatal for streaming, where audio arrives every ~20 ms.
* **`Session` is the streaming interface.** Vosk decodes incrementally and
  reports its own endpoints; a one-shot API throws that away and forces the
  caller to guess when someone stopped talking. Partial results are also what
  makes the admin panel feel alive while you are still speaking.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import Config

log = logging.getLogger("intelligence.asr")

ENGINE = "vosk"


@dataclass
class TranscriptResult:
    text: str
    engine: str = ENGINE
    is_final: bool = True
    confidence: float = 0.0


@dataclass(frozen=True)
class Availability:
    """Whether speech-to-text can actually run, and if not, why not.

    The reason is the point. "ASR unavailable" on a panel with no further
    detail is indistinguishable from a bug; "vosk_model_path is not
    configured" is a thing you can go and fix.
    """

    ok: bool
    reason: str = ""
    engine: str = ENGINE
    model_path: str = ""


# Keyed by model directory. Vosk models are hundreds of MB resident, so two
# paths in one process would be unusual -- but keying by path is still right,
# because reconfiguring to a different model must not keep serving the old one.
_models: dict[str, object] = {}
_models_lock = threading.Lock()


def reset_cache() -> None:
    """Drop cached models. For tests, and for a config change at runtime."""
    with _models_lock:
        _models.clear()


def availability(cfg: Config) -> Availability:
    """Check without loading anything. Safe to call on every status poll."""
    path = cfg.asr.vosk_model_path
    if not path:
        return Availability(
            False,
            "asr.vosk_model_path is not set -- point it at a downloaded Vosk "
            "model directory in config/intelligence.local.yaml",
            model_path="",
        )
    if not Path(path).exists():
        return Availability(False, f"vosk model not found: {path}", model_path=path)
    try:
        import vosk  # noqa: F401
    except ImportError:
        return Availability(
            False,
            "vosk not installed -- pip install -e '.[voice]' from the repo root",
            model_path=path,
        )
    return Availability(True, model_path=path)


def _load_model(cfg: Config):
    """Load (or reuse) the Vosk model named by config.

    Raises RuntimeError with an actionable message rather than letting an
    ImportError or a Kaldi assertion surface -- this is reached from a web
    request and from the CLI, and both want a sentence, not a traceback.
    """
    if not cfg.asr.vosk_model_path:
        raise RuntimeError(
            "asr.vosk_model_path is not configured -- set it in "
            "config/intelligence.local.yaml to a downloaded Vosk model directory"
        )
    model_path = Path(cfg.asr.vosk_model_path)
    if not model_path.exists():
        raise RuntimeError(f"vosk model not found: {model_path}")

    key = str(model_path)
    with _models_lock:
        cached = _models.get(key)
        if cached is not None:
            return cached

    try:
        from vosk import Model
    except ImportError as exc:
        raise RuntimeError(
            "vosk not installed -- pip install -e '.[voice]' from the repo root"
        ) from exc

    log.info("loading vosk model: %s", key)
    model = Model(key)
    with _models_lock:
        # Another thread may have won the race while we were loading. Keep
        # whichever landed first; a duplicate load wastes seconds, not
        # correctness, and holding the lock across the load would stall every
        # other caller for those same seconds.
        return _models.setdefault(key, model)


def _recognizer(cfg: Config, sample_rate: int):
    # Load first: `_load_model` is what validates the config and turns a
    # missing package or path into a sentence. Importing KaldiRecognizer ahead
    # of it would surface a bare ModuleNotFoundError for an unconfigured model
    # path, which says nothing about the actual problem.
    model = _load_model(cfg)
    try:
        from vosk import KaldiRecognizer
    except ImportError as exc:  # pragma: no cover - unreachable via _load_model
        raise RuntimeError(
            "vosk not installed -- pip install -e '.[voice]' from the repo root"
        ) from exc
    return KaldiRecognizer(model, sample_rate)


class Session:
    """A streaming recognition session: feed PCM in, get transcripts out.

    One session per listening window. Vosk keeps decoder state across chunks,
    so this object is stateful and is not safe to share between concurrent
    speakers -- make one per connection.

    Audio must be mono 16-bit little-endian PCM at `sample_rate`, which is what
    the admin panel's mic worklet already produces.
    """

    def __init__(self, cfg: Config, sample_rate: int | None = None) -> None:
        self.cfg = cfg
        self.sample_rate = int(sample_rate or cfg.asr.sample_rate)
        self._rec = _recognizer(cfg, self.sample_rate)
        self._bytes_seen = 0

    @property
    def bytes_seen(self) -> int:
        return self._bytes_seen

    def accept(self, pcm: bytes) -> TranscriptResult | None:
        """Feed one chunk.

        Returns a **final** result when Vosk decides the utterance ended, and
        None otherwise. Letting the recognizer call the endpoint is the whole
        reason to stream: a fixed silence timer in the caller would either clip
        people who pause mid-sentence or make everyone wait out the timeout.

        Call `partial()` between finals for the in-progress text.
        """
        self._bytes_seen += len(pcm)
        if self._rec.AcceptWaveform(pcm):
            return self._parse(self._rec.Result(), is_final=True)
        return None

    def partial(self) -> str:
        """The in-progress hypothesis. Changes as more audio arrives, and is
        not a transcript -- never store it as one."""
        try:
            return json.loads(self._rec.PartialResult()).get("partial", "")
        except (json.JSONDecodeError, AttributeError):
            return ""

    def final(self) -> TranscriptResult:
        """Flush whatever is buffered and end the utterance.

        Call this when the window closes (the browser stopped sending, the
        operator pressed stop) so trailing audio is not silently discarded.
        """
        return self._parse(self._rec.FinalResult(), is_final=True)

    def reset(self) -> None:
        """Start a fresh utterance, reusing the loaded model."""
        self._rec = _recognizer(self.cfg, self.sample_rate)
        self._bytes_seen = 0

    @staticmethod
    def _parse(raw: str, *, is_final: bool) -> TranscriptResult:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        return TranscriptResult(
            text=payload.get("text", ""),
            engine=ENGINE,
            is_final=is_final,
            confidence=float(payload.get("conf", 0.0) or 0.0),
        )


def transcribe(audio_bytes: bytes, sample_rate: int, cfg: Config) -> TranscriptResult:
    """One-shot transcription of a complete utterance.

    For a recorded clip or a CLI invocation. Live audio should use `Session`,
    which reports endpoints and partials instead of making the caller wait for
    the whole recording.
    """
    recognizer = _recognizer(cfg, sample_rate)
    recognizer.AcceptWaveform(audio_bytes)
    result = json.loads(recognizer.FinalResult())
    return TranscriptResult(text=result.get("text", ""), engine=ENGINE)
