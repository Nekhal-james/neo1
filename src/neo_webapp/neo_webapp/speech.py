"""Runs speech-to-text and text-to-speech for the admin panel.

The same relationship to `intelligence` that perception_link.py has to
`neo_perception`: the panel drives the real engines -- the ones that will run on
the robot -- so that the audio path can be exercised end to end with no Pi, no
ROS and no microphone attached to anything but a browser.

Optional by construction. If `intelligence` is missing, or Vosk/Piper are not
installed, or no model has been downloaded, the panel keeps working and reports
*why* speech is unavailable rather than failing a request with a traceback.

Everything that touches a model runs in a worker thread. Loading a Vosk model
takes seconds and synthesizing a sentence takes hundreds of milliseconds; either
one on the event loop would stall the state broadcast, the joystick deadman and
every other client for that whole time.
"""

from __future__ import annotations

import asyncio
import io
import logging
import wave

from .bridge.types import AudioView, TranscriptView

log = logging.getLogger(__name__)

try:
    from intelligence import asr as _asr
    from intelligence import tts as _tts
    from intelligence.config import Config as IntelligenceConfig

    SPEECH_AVAILABLE = True
except ImportError as exc:  # pragma: no cover - depends on environment
    log.info("intelligence unavailable (%s); Audio tab will be inert", exc)
    SPEECH_AVAILABLE = False


class SpeechLink:
    """Adapts panel audio to `intelligence`'s recognizer and voice, and back."""

    def __init__(self, *, mic_rate: int = 16000, speaker_rate: int = 22050) -> None:
        self.available = SPEECH_AVAILABLE
        self.mic_rate = mic_rate
        self.speaker_rate = speaker_rate

        self._cfg = None
        self._session = None
        # Serializes access to the streaming session. A Vosk recognizer holds
        # decoder state across chunks and is not safe to enter twice at once --
        # and `to_thread` hands work to a pool, so awaiting is not by itself a
        # guarantee that two feeds cannot overlap.
        self._lock = asyncio.Lock()

        self._utterances = 0
        self._last = TranscriptView()
        self._partial = ""
        self._last_error = ""
        self._speaking = False
        self._asr_avail = None
        self._tts_avail = None

    # -- config ------------------------------------------------------------

    def _config(self):
        if self._cfg is None:
            self._cfg = IntelligenceConfig.load()
        return self._cfg

    def reload_config(self) -> None:
        """Pick up an edited config/intelligence.local.yaml without a restart.

        Drops the cached models too: the point of reloading is usually that a
        model path changed, and continuing to serve the old one would be the
        opposite of what was asked.
        """
        self._cfg = None
        self._session = None
        if SPEECH_AVAILABLE:
            _asr.reset_cache()
            _tts.reset_cache()
        self._asr_avail = self._tts_avail = None

    # -- availability ------------------------------------------------------

    def refresh_availability(self) -> None:
        """Re-stat the model paths. Called from the slow status task.

        Deliberately not called from `view()`: this touches the filesystem, and
        `view()` is read by the 4 Hz state broadcast.
        """
        if not SPEECH_AVAILABLE:
            return
        try:
            cfg = self._config()
            self._asr_avail = _asr.availability(cfg)
            self._tts_avail = _tts.availability(cfg)
        except Exception as exc:  # noqa: BLE001 - a bad config must not kill the task
            log.warning("speech availability check failed: %s", exc)
            self._asr_avail = self._tts_avail = None
            self._last_error = str(exc)

    def view(self, *, listening: bool = False) -> AudioView:
        """The Audio tab's state. Cheap: no file I/O, no model loading."""
        if not SPEECH_AVAILABLE:
            reason = "intelligence is not installed -- pip install -e '.[dev]' from the repo root"
            return AudioView(asr_reason=reason, tts_reason=reason, listening=False)

        asr_avail, tts_avail = self._asr_avail, self._tts_avail
        pending = "checking..."
        return AudioView(
            asr_available=bool(asr_avail and asr_avail.ok),
            asr_reason=asr_avail.reason if asr_avail else pending,
            asr_engine=asr_avail.engine if asr_avail else "",
            tts_available=bool(tts_avail and tts_avail.ok),
            tts_reason=tts_avail.reason if tts_avail else pending,
            tts_engine=tts_avail.engine if tts_avail else "",
            listening=listening,
            partial=self._partial,
            last=self._last,
            utterances=self._utterances,
            audio_bytes=self._session.bytes_seen if self._session else 0,
            speaking=self._speaking,
            last_error=self._last_error,
        )

    # -- speech to text ----------------------------------------------------

    def _ensure_session(self):
        if self._session is None:
            self._session = _asr.Session(self._config(), sample_rate=self.mic_rate)
        return self._session

    async def feed(self, pcm: bytes) -> TranscriptView | None:
        """Feed one chunk of mic PCM.

        Returns a final transcript when the recognizer decides the utterance
        ended, and None otherwise -- letting Vosk call the endpoint rather than
        imposing a fixed silence timer here, which would clip anyone who pauses
        mid-sentence.

        Never raises: the mic websocket must not die because a model is
        missing. The reason surfaces in `view().last_error` instead.
        """
        if not SPEECH_AVAILABLE:
            return None

        async with self._lock:
            try:
                result = await asyncio.to_thread(self._accept, pcm)
            except Exception as exc:  # noqa: BLE001
                self._note_error("speech-to-text", exc)
                return None

        if result is None:
            return None
        self._utterances += 1
        self._partial = ""
        self._last = result
        return result

    def _accept(self, pcm: bytes) -> TranscriptView | None:
        """Runs in a worker thread: model load, decode, partial read."""
        session = self._ensure_session()
        result = session.accept(pcm)
        if result is None:
            self._partial = session.partial()
            return None
        return _to_view(result, grammar_constrained=False)

    async def flush(self) -> TranscriptView | None:
        """End the utterance and return whatever was buffered.

        Called when the mic channel closes. Without it, trailing audio -- often
        the last word -- is silently discarded when someone stops the mic
        instead of pausing long enough for Vosk to endpoint.
        """
        if not SPEECH_AVAILABLE or self._session is None:
            return None
        async with self._lock:
            try:
                result = await asyncio.to_thread(self._session.final)
            except Exception as exc:  # noqa: BLE001
                self._note_error("speech-to-text", exc)
                return None
            self._partial = ""
        if not result.text:
            return None
        self._utterances += 1
        self._last = _to_view(result, grammar_constrained=False)
        return self._last

    async def transcribe_wav(self, wav_bytes: bytes) -> TranscriptView:
        """One-shot transcription of an uploaded WAV.

        The path that makes ASR testable with no microphone at all -- and the
        one that will let a recorded corridor clip be replayed against a tuned
        model later.

        Gets its own recognizer rather than sharing the streaming session: they
        are different utterances, and interleaving them would corrupt both.
        """
        if not SPEECH_AVAILABLE:
            raise RuntimeError("intelligence is not installed")

        pcm, rate = _wav_to_pcm(wav_bytes)
        cfg = self._config()
        result = await asyncio.to_thread(_asr.transcribe, pcm, rate, cfg)
        view = _to_view(result, grammar_constrained=False)
        self._last = view
        self._utterances += 1
        return view

    def reset(self) -> None:
        """Start a fresh utterance, keeping the loaded model."""
        self._session = None
        self._partial = ""
        self._last = TranscriptView()
        self._last_error = ""

    # -- text to speech ----------------------------------------------------

    async def say(self, text: str):
        """Synthesize `text` to PCM at the panel's speaker rate.

        Resampled here rather than in the browser: the speaker channel is a dumb
        pipe into an AudioContext fixed at one rate, so a voice at a different
        rate would simply play at the wrong pitch.
        """
        if not SPEECH_AVAILABLE:
            raise RuntimeError("intelligence is not installed")

        self._speaking = True
        try:
            return await asyncio.to_thread(
                _tts.synthesize_pcm, text, self._config(), self.speaker_rate
            )
        except Exception as exc:
            self._note_error("text-to-speech", exc)
            raise
        finally:
            self._speaking = False

    # -- internals ---------------------------------------------------------

    def _note_error(self, what: str, exc: Exception) -> None:
        self._last_error = f"{what}: {exc}"
        log.warning("%s failed: %s", what, exc)


def _to_view(result, *, grammar_constrained: bool) -> TranscriptView:
    return TranscriptView(
        text=result.text,
        is_final=getattr(result, "is_final", True),
        confidence=getattr(result, "confidence", 0.0),
        engine=result.engine,
        grammar_constrained=grammar_constrained,
    )


def _wav_to_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
    """Unwrap a WAV upload to mono s16le plus its rate.

    Rejects what it cannot handle rather than feeding the recognizer samples it
    will silently mis-decode into plausible-looking nonsense -- which is far
    harder to debug than an error.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            channels = wav_file.getnchannels()
            width = wav_file.getsampwidth()
            rate = wav_file.getframerate()
            frames = wav_file.readframes(wav_file.getnframes())
    except wave.Error as exc:
        raise ValueError(f"not a readable WAV file: {exc}") from exc

    if width != 2:
        raise ValueError(f"expected 16-bit samples, got {width * 8}-bit")
    if channels != 1:
        raise ValueError(f"expected mono audio, got {channels} channels")
    return frames, rate
