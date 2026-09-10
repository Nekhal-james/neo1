"""Local text-to-speech via Piper.

Piper is local per CLAUDE.md/plan Phase 5 (not an off-board call) -- it's the
one part of the audio stack that never depends on the laptop being present.
`piper-tts` is lazy-imported, same discipline as asr.py's vosk import.

Beyond wrapping Piper, this module does two things the callers need:

* **Caches the loaded voice.** Same reasoning as the ASR model cache: loading
  an ONNX voice is slow enough that doing it per utterance would dominate the
  reply latency budget (plan 12.2 allows 500 ms for first audio, total).
* **Hands back raw PCM at a rate you choose.** The admin panel's speaker
  channel is a dumb pipe of s16le samples into a Web Audio context that was
  created at one fixed rate. A voice at a different rate played into it is not
  an error -- it just sounds wrong, at the wrong pitch, which is a genuinely
  confusing bug to chase. Resampling here keeps the wire format one thing.
"""

from __future__ import annotations

import array
import io
import logging
import re
import sys
import threading
import wave
from dataclasses import dataclass

from .config import Config, resolve_path

log = logging.getLogger("intelligence.tts")

ENGINE = "piper"


@dataclass(frozen=True)
class PcmAudio:
    """Mono 16-bit little-endian PCM, with the rate it is actually at."""

    data: bytes
    sample_rate: int

    @property
    def duration_s(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return len(self.data) / 2.0 / self.sample_rate


@dataclass(frozen=True)
class Availability:
    """Whether speech can actually be synthesised, and if not, why not."""

    ok: bool
    reason: str = ""
    engine: str = ENGINE
    model_path: str = ""


_voices: dict[str, object] = {}
_voices_lock = threading.Lock()


def reset_cache() -> None:
    """Drop cached voices. For tests, and for a config change at runtime."""
    with _voices_lock:
        _voices.clear()


def availability(cfg: Config) -> Availability:
    """Check without loading anything. Safe to call on every status poll."""
    path = cfg.tts.piper_model_path
    if not path:
        return Availability(
            False,
            "tts.piper_model_path is not set -- point it at a downloaded Piper "
            ".onnx voice in config/intelligence.local.yaml",
            model_path="",
        )
    # Relative to the repo root, not the working directory -- see resolve_path.
    resolved = resolve_path(path)
    if not resolved.exists():
        return Availability(
            False, f"piper voice model not found: {resolved}", model_path=str(resolved)
        )
    try:
        import piper  # noqa: F401
    except ImportError:
        return Availability(
            False,
            "piper-tts not installed -- pip install -e '.[voice]' from the repo root",
            model_path=str(resolved),
        )
    return Availability(True, model_path=str(resolved))


def _load_voice(cfg: Config):
    if not cfg.tts.piper_model_path:
        raise RuntimeError(
            "tts.piper_model_path is not configured -- set it in "
            "config/intelligence.local.yaml to a downloaded Piper .onnx voice"
        )
    model_path = resolve_path(cfg.tts.piper_model_path)
    if not model_path.exists():
        raise RuntimeError(f"piper voice model not found: {model_path}")

    config_path = (
        resolve_path(cfg.tts.piper_config_path) if cfg.tts.piper_config_path else None
    )
    key = f"{model_path}|{config_path or ''}"
    with _voices_lock:
        cached = _voices.get(key)
        if cached is not None:
            return cached

    try:
        from piper import PiperVoice
    except ImportError as exc:
        raise RuntimeError(
            "piper-tts not installed -- pip install -e '.[voice]' from the repo root"
        ) from exc

    log.info("loading piper voice: %s", model_path)
    voice = PiperVoice.load(str(model_path), config_path=str(config_path) if config_path else None)
    with _voices_lock:
        # Same race note as asr._load_model: a duplicate load costs time, not
        # correctness, and is preferable to holding the lock across it.
        return _voices.setdefault(key, voice)


def synthesize(text: str, cfg: Config) -> bytes:
    """Returns WAV bytes."""
    voice = _load_voice(cfg)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        # piper-tts >=1.2 split `synthesize()` into a streaming AudioChunk
        # iterator; `synthesize_wav()` is the direct-to-wave-file call the old
        # `synthesize(text, wav_file)` signature used to be.
        voice.synthesize_wav(text, wav_file)
    return buf.getvalue()


def _samples(pcm: bytes) -> array.array:
    """s16le bytes -> a signed-short array, whatever the host's byte order."""
    buf = array.array("h")
    buf.frombytes(pcm)
    if sys.byteorder == "big":
        buf.byteswap()
    return buf


def _to_bytes(buf: array.array) -> bytes:
    if sys.byteorder == "big":
        buf = array.array("h", buf)
        buf.byteswap()
    return buf.tobytes()


def resample(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """Mono s16le resample by linear interpolation. Identity when rates match.

    Hand-rolled rather than `audioop.ratecv`, which would be the obvious choice
    but was removed from the standard library in Python 3.13 -- this project
    will outlive that, and a silent import failure on a future Pi image is a
    worse trade than twenty lines here.

    Linear interpolation is enough: this is speech at conversational rates, and
    the artefacts are inaudible next to those of the voice model itself. It is a
    one-off cost per utterance, not a per-frame one.
    """
    if from_rate == to_rate or not pcm or from_rate <= 0 or to_rate <= 0:
        return pcm

    src = _samples(pcm)
    if not src:
        return pcm

    n_out = int(len(src) * to_rate / from_rate)
    if n_out <= 0:
        return b""

    out = array.array("h", bytes(2 * n_out))
    step = from_rate / to_rate
    last = len(src) - 1
    for i in range(n_out):
        pos = i * step
        i0 = int(pos)
        if i0 >= last:
            out[i] = src[last]
            continue
        frac = pos - i0
        a = src[i0]
        value = int(a + (src[i0 + 1] - a) * frac)
        # Interpolation cannot exceed the neighbours it sits between, but
        # clamp anyway: a rounding surprise here becomes an audible click.
        out[i] = -32768 if value < -32768 else (32767 if value > 32767 else value)
    return _to_bytes(out)


def synthesize_pcm(text: str, cfg: Config, target_rate: int | None = None) -> PcmAudio:
    """Synthesize to raw mono s16le, optionally resampled to `target_rate`.

    This is what the admin panel's speaker channel and the eventual `/audio/out`
    publisher both want: a WAV header in the middle of a PCM stream is not
    something either of them can do anything with.
    """
    wav_bytes = synthesize(text, cfg)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        width = wav_file.getsampwidth()
        frames = wav_file.readframes(wav_file.getnframes())

    # Piper emits 16-bit mono, so neither branch below is normally taken. They
    # exist so a surprising voice fails with a sentence instead of as noise --
    # wrong-width samples played as s16le are not quiet, they are loud.
    if width != 2:
        raise RuntimeError(
            f"piper voice produced {width * 8}-bit audio; only 16-bit is supported"
        )
    if channels == 2:
        stereo = _samples(frames)
        mono = array.array("h", bytes(2 * (len(stereo) // 2)))
        for i in range(len(mono)):
            mono[i] = (stereo[2 * i] + stereo[2 * i + 1]) // 2
        frames = _to_bytes(mono)
    elif channels != 1:
        raise RuntimeError(f"piper voice produced {channels} channels; expected mono")

    if target_rate:
        frames = resample(frames, rate, target_rate)
        rate = target_rate

    return PcmAudio(data=frames, sample_rate=rate)


# -- sentence splitting ------------------------------------------------------

_BOUNDARY = re.compile(r"[.!?]+[\"')\]]*\s+")

_ABBREVIATIONS = frozenset(
    {"dr", "mr", "mrs", "ms", "prof", "st", "no", "vs", "etc", "approx", "dept", "jr", "sr"}
)

_SOFT_BREAKS = (", ", "; ", ": ")


def split_sentences(text: str, *, max_chars: int = 200) -> list[str]:
    """Break a reply into the pieces Piper should synthesize one at a time.

    Speaking sentence by sentence is what lets Neo start talking before the
    whole reply has been synthesized (plan 5.5): the wait a person hears is the
    first sentence, not the reply.

    Conservative on purpose, because the two kinds of mistake cost very
    different amounts. Merging two sentences into one synthesis call costs a
    little latency and is otherwise inaudible -- Piper speaks an internal full
    stop perfectly well. Splitting in the wrong place, after "Dr." or inside
    "A.P.J. Abdul Kalam", makes the voice stop dead mid-name. So a stop only
    counts when it is followed by a capital or a digit, and not when the word
    before it is a known abbreviation, a single letter, or itself contains a
    full stop.

    Anything still longer than `max_chars` is wrapped at a comma, then at a
    space, so one long sentence cannot bring back the delay streaming removes.
    No word is ever dropped: joining the pieces with spaces gives back the
    input with its whitespace normalized.
    """
    text = " ".join(text.split())
    if not text:
        return []
    max_chars = max(20, max_chars)

    sentences: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(text):
        following = text[match.end() : match.end() + 1]
        if not following or not (following.isupper() or following.isdigit() or following in "\"'("):
            continue
        word = text[start : match.start()].rsplit(" ", 1)[-1].lstrip("\"'(").lower()
        if word in _ABBREVIATIONS or len(word) <= 1 or "." in word:
            continue
        sentences.append(text[start : match.end()].strip())
        start = match.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)

    pieces: list[str] = []
    for sentence in sentences:
        pieces.extend(_wrap(sentence, max_chars))
    return pieces


def _wrap(sentence: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    while len(sentence) > max_chars:
        cut = max(sentence.rfind(sep, 0, max_chars) for sep in _SOFT_BREAKS)
        if cut > 0:
            head, rest = sentence[: cut + 1], sentence[cut + 1 :]
        else:
            cut = sentence.rfind(" ", 0, max_chars + 1)
            if cut > 0:
                head, rest = sentence[:cut], sentence[cut:]
            else:
                head, rest = sentence[:max_chars], sentence[max_chars:]
        pieces.append(head.strip())
        sentence = rest.strip()
    if sentence:
        pieces.append(sentence)
    return pieces
