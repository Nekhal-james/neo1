"""The spoken conversation loop: a final transcript in, Neo's reply out loud.

Speech-to-text and text-to-speech each worked on their own before this, and a
person at the desk still could not ask Neo a question with their voice and hear
an answer. This is the join, and it is deliberately small: the recognizer, the
chat path and the voice are the same ones the rest of the panel already uses.

    mic -> SpeechLink.feed -> final transcript
                                    |
                         intelligence.chat.ask          THINKING
                                    |
          SpeechLink.say_sentences -> speaker           SPEAKING

Three rules, each of which exists because the loop is broken without it:

* **Half-duplex** (plan 5.6). While Neo's voice is playing, mic audio does not
  reach the recognizer. Without it the room microphone hears the reply,
  transcribes it, and Neo answers itself -- indefinitely. The gate holds until
  the audio has *finished playing in the browser*, not until the server finished
  pushing it: synthesis runs faster than real time, so pushing is done seconds
  before the listener is.
* **One turn at a time, and no queue.** A transcript that arrives while a turn
  is in progress is dropped, not held. Plan 5 rules out multi-turn barge-in, and
  a queue would answer a question the person has already moved on from.
* **Sentence streaming** (plan 5.5). The reply is synthesized and pushed a
  sentence at a time, so the wait before Neo starts talking is one sentence
  long rather than the whole reply long.

It also drives `dialog_state` -- the badge at the top of the panel -- while the
simulated robot is the only thing that could, and never overwrites ESTOP.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable

from .bridge.types import TranscriptView, VoiceTurnView, VoiceView

if TYPE_CHECKING:
    from .bridge import Bridge
    from .media import MediaManager

log = logging.getLogger(__name__)

SPEAKER_CHUNK_BYTES = 2048
"""Matches the test tone's chunking. Small enough that the browser can start
playing quickly, large enough not to spend the event loop on framing."""

PLAYBACK_LEAD_S = 0.02
"""Mirrors app.js, which schedules every chunk no earlier than 20 ms ahead of
its own clock. The gate models the browser's playback cursor, so it has to use
the same rule or it reopens while the last syllable is still playing."""

TAIL_S = 0.35
"""How long the gate stays shut after playback should have ended: jitter on the
last chunk's delivery, and the room's own reverb reaching the microphone."""

PUSH_TIMEOUT_S = 2.0
"""How long one chunk may wait for the speaker socket before the rest of the
reply is abandoned. A healthy socket takes a chunk in microseconds; two seconds
of nothing means the browser went away mid-sentence."""


@dataclass
class ChatReply:
    reply: str
    source: str


@dataclass
class SpeakResult:
    sample_rate: int = 0
    duration_s: float = 0.0
    bytes: int = 0
    sentences: int = 0
    # From when the turn started (or the say request arrived) to the first
    # sentence being ready -- the wait a person actually hears.
    first_audio_ms: float = 0.0
    speaker_connected: bool = False


AskFn = Callable[[str], ChatReply]


def default_ask(text: str) -> ChatReply:
    """The same path as `POST /api/dialog/ask` and `neo --prompt`.

    So endpoint resolution, mTLS, the degraded reply and the dialog status file
    cannot drift between typing a question and saying it out loud.
    """
    from intelligence.chat import ask
    from intelligence.config import Config as IntelligenceConfig
    from model_conn.config import Config as ModelConnConfig

    result = ask(text, cfg=IntelligenceConfig.load(), mc_cfg=ModelConnConfig.load())
    return ChatReply(reply=result.reply, source=result.source)


class VoiceLoop:
    """Owns the spoken turn, the half-duplex gate, and the speaking state.

    Reads the bridge, media manager and speech link off the app state on every
    use rather than holding references of its own, the way the routes do -- so
    swapping one of them out cannot leave this talking to a stale copy.
    """

    def __init__(
        self,
        state: Any,
        *,
        ask: AskFn = default_ask,
        tail_s: float = TAIL_S,
        push_timeout_s: float = PUSH_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._state = state
        self._ask = ask
        self.tail_s = tail_s
        self.push_timeout_s = push_timeout_s
        self._clock = clock

        # Answer final transcripts out loud. Off until an operator turns it on:
        # with it on, whatever the room says to an open mic goes to the model
        # host, and that should be a choice rather than a side effect.
        self.enabled = False

        self._thinking = False
        self._pushing = False
        # When the audio already pushed will have finished playing, on
        # `self._clock`. Advanced exactly the way app.js advances its own.
        self._play_cursor = 0.0
        self._turns = 0
        self._last = VoiceTurnView()
        self._turn_task: asyncio.Task | None = None
        self._settle_task: asyncio.Task | None = None
        # Utterances are serialized: two replies pushed at once interleave their
        # chunks on the one speaker queue, which plays as noise.
        self._speak_lock = asyncio.Lock()

    # -- collaborators -------------------------------------------------------

    @property
    def bridge(self) -> Bridge:
        return self._state.bridge

    @property
    def media(self) -> MediaManager:
        return self._state.media

    @property
    def speech(self):
        return self._state.speech

    # -- state ---------------------------------------------------------------

    @property
    def busy(self) -> bool:
        """A turn is thinking, or pushing its reply."""
        return self._turn_task is not None and not self._turn_task.done()

    def speaking(self) -> bool:
        """Neo's voice is being pushed, or is still playing in the browser."""
        return self._pushing or self._clock() < self._play_cursor

    def gated(self) -> bool:
        """Mic audio must not reach the recognizer right now (plan 5.6).

        Longer than `speaking()` by the tail on purpose: the last syllable, and
        its echo off the walls, arrive after playback nominally ends.
        """
        return self._pushing or self._clock() < self._play_cursor + self.tail_s

    @property
    def phase(self) -> str:
        if self.speaking():
            return "speaking"
        if self._thinking:
            return "thinking"
        if self.media.active("mic"):
            return "listening"
        return "idle"

    def view(self) -> VoiceView:
        return VoiceView(
            enabled=self.enabled,
            phase=self.phase,
            gated=self.media.active("mic") and self.gated(),
            turns=self._turns,
            last=replace(self._last),
        )

    def publish(self) -> None:
        """Mirror the loop into the state snapshot, and drive the dialog badge.

        The badge only against the simulated robot. On the real one the dialog
        manager node owns `/dialog/state`, and a panel writing its own idea of
        it into the snapshot would fight the robot's.
        """
        snap = self.bridge.snapshot()
        snap.voice = self.view()
        if getattr(self.bridge, "name", "") != "mock":
            return
        if snap.dialog_state == "ESTOP" or snap.head.estop:
            return
        snap.dialog_state = self.phase.upper()

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        self.publish()

    # -- the listening side --------------------------------------------------

    def on_transcript(self, transcript: TranscriptView | None) -> bool:
        """Start a turn for a final transcript. Returns whether one started.

        Never raises and never waits: it is called from the mic websocket's
        receive loop, which has to keep draining audio while the model thinks.
        """
        if transcript is None or not self.enabled:
            return False
        text = (transcript.text or "").strip()
        if not text:
            return False
        if self.busy:
            log.info("voice: dropped %r, a turn is already in progress", text)
            return False
        self._turn_task = asyncio.create_task(self._run_turn(text), name="voice-turn")
        return True

    async def run_turn(self, text: str) -> VoiceTurnView:
        """Run one turn from typed text; returns once the reply has been pushed.

        Does not need answering switched on -- typing a question is already the
        explicit act that switch exists to require.
        """
        if self.busy:
            raise RuntimeError("Neo is already answering")
        self._turn_task = asyncio.create_task(self._run_turn(text), name="voice-turn")
        return await self._turn_task

    async def _run_turn(self, text: str) -> VoiceTurnView:
        turn = VoiceTurnView(heard=text)
        self._turns += 1
        self._last = turn
        started = self._clock()
        self._thinking = True
        self.publish()
        try:
            try:
                reply = await asyncio.to_thread(self._ask, text)
            except Exception as exc:  # noqa: BLE001 -- a failed turn is shown, not raised
                turn.error = f"chat failed: {exc}"
                log.warning("voice turn: %s", turn.error)
                return turn

            turn.reply = reply.reply
            turn.source = reply.source
            turn.think_ms = (self._clock() - started) * 1000.0
            self.publish()
            if not turn.reply.strip():
                return turn

            try:
                spoken = await self.speak(turn.reply, started_at=started)
            except Exception as exc:  # noqa: BLE001
                turn.error = f"speech failed: {exc}"
                log.warning("voice turn: %s", turn.error)
                return turn

            turn.first_audio_ms = spoken.first_audio_ms
            turn.spoken_s = spoken.duration_s
            turn.sentences = spoken.sentences
            if not spoken.speaker_connected:
                turn.error = "no speaker connected, so nobody heard the reply"
            return turn
        finally:
            # `speak()` has already taken the phase to speaking by the time this
            # clears, so the badge goes THINKING -> SPEAKING with no LISTENING
            # flicker in between.
            self._thinking = False
            self.publish()

    # -- the speaking side ---------------------------------------------------

    async def speak(self, text: str, *, started_at: float | None = None) -> SpeakResult:
        """Synthesize `text` sentence by sentence and push it to the speaker.

        Returns once the last sentence has been *pushed*. Playback carries on in
        the browser after that, and the gate stays shut until it has finished.
        Raises whatever synthesis raises, so the say route can tell a missing
        voice (RuntimeError) from an engine that broke.
        """
        async with self._speak_lock:
            started = self._clock() if started_at is None else started_at
            result = SpeakResult()
            self._pushing = True
            self.publish()
            try:
                stream = self.speech.say_sentences(text)
                async with contextlib.aclosing(stream):
                    async for audio in stream:
                        result.sentences += 1
                        result.sample_rate = audio.sample_rate
                        result.bytes += len(audio.data)
                        result.duration_s += audio.duration_s
                        if result.sentences == 1:
                            result.first_audio_ms = (self._clock() - started) * 1000.0
                        if not self.media.active("speaker"):
                            # Still synthesized: that is what reports a missing
                            # or broken voice, and what tells the operator how
                            # long it would have been. But queued with nobody
                            # draining it, it plays as a stale fragment of an old
                            # sentence to whoever connects the speaker next.
                            continue
                        result.speaker_connected = True
                        if not await self._push(audio):
                            break
            finally:
                self._pushing = False
                self.publish()
                self._schedule_settle()
            return result

    async def _push(self, audio) -> bool:
        data = audio.data
        rate = audio.sample_rate or 1
        for offset in range(0, len(data), SPEAKER_CHUNK_BYTES):
            chunk = data[offset : offset + SPEAKER_CHUNK_BYTES]
            if not await self.bridge.push_audio_out(chunk, timeout_s=self.push_timeout_s):
                log.warning("speaker stopped taking audio; abandoning the rest of the reply")
                return False
            # The same cursor app.js keeps: back to back, never behind the
            # clock. It is what tells the gate when playback really ends.
            now = self._clock()
            self._play_cursor = (
                max(self._play_cursor, now + PLAYBACK_LEAD_S) + len(chunk) / 2.0 / rate
            )
        return True

    def _schedule_settle(self) -> None:
        if self._settle_task is not None and not self._settle_task.done():
            return  # the running one re-reads the cursor, so it covers this too
        self._settle_task = asyncio.create_task(self._settle(), name="voice-settle")

    async def _settle(self) -> None:
        """Wait out playback and the tail, then hand the mic back.

        Drops whatever the recognizer was half-holding from around Neo's own
        voice: that is not something anybody said to it.
        """
        while self._pushing or self._clock() < self._play_cursor + self.tail_s:
            remaining = self._play_cursor + self.tail_s - self._clock()
            await asyncio.sleep(min(max(remaining, 0.02), 0.25))
        with contextlib.suppress(Exception):
            self.speech.discard_utterance()
        self.publish()

    async def wait_until_quiet(self) -> None:
        """For tests and shutdown: returns once playback and the tail are over."""
        task = self._settle_task
        if task is not None:
            await task

    async def close(self) -> None:
        for task in (self._turn_task, self._settle_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
