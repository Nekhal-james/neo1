"""Speech to text, for the window the wake word opens.

Vosk on the Pi, always: the off-board host is the owner's daily-driver laptop
and is usually unavailable, so on-Pi recognition is an invariant rather than a
fallback (CLAUDE.md). Off-board Whisper is an accuracy upgrade layered on later
and is why this node is called a *router* rather than a recogniser.

The window it recognises over is opened by /wake/event and closed by the
endpointer, not by Vosk's own endpointing. Vosk's is tuned for dictation and
waits long enough that Neo feels like it did not hear you; a final result from
Vosk still closes the window early if it arrives first.
"""

from __future__ import annotations

import logging
import re

from intelligence.asr import Session, availability
from intelligence.config import Config as IntelligenceConfig

from ..config import Config
from ..endpointer import Endpoint, Endpointer

log = logging.getLogger(__name__)

try:
    from rclpy.node import Node
except ImportError:  # pragma: no cover - exercised only where ROS is installed
    Node = object

PREROLL_S = 0.4
"""Audio kept from *before* the wake event, and fed to the recogniser when the
window opens.

"NEO, where is CS-204" is one breath: by the time the detector has scored the
window containing "NEO", the next word is already spoken. Without a pre-roll
that word is lost and the question starts mid-sentence. The cost is that the
wake word itself is usually transcribed, which `_strip_wake_word` removes.
"""

WATCHDOG_GRACE_S = 1.0
"""Added to the endpointer's own limits before the wall-clock watchdog acts, so
it only ever closes a window the endpointer could not."""

_WAKE_PREFIX = re.compile(
    r"^(?:(?:hey|hi|ok|okay)\s+)?(?:neo's|neo|nio|neyo)(?:\s+(?:neo|nio|neyo))?[\s,.!?]+(?=\S)",
    re.IGNORECASE,
)
"""The wake word as Vosk renders it at the head of an utterance.

Only renderings that are not also ordinary English. "Near" and "Neil" are
common misrecognitions of "Neo" too, but "near the library, is there a
canteen" is a real question, and silently losing its first word is worse than
leaving a stray "neo" at the front of one -- which the dialog layer reads past.
"""


def probe() -> tuple[bool, str]:
    try:
        import rclpy  # noqa: F401
    except ImportError:
        return False, "rclpy not installed"
    try:
        import neo_msgs.msg  # noqa: F401
    except ImportError:
        return False, "neo_msgs not built (Phase 1)"
    return True, "ok"


def strip_wake_word(text: str) -> str:
    """Drop a leading "neo" the pre-roll caused us to transcribe.

    Only at the head, and only as a whole word: "neo" inside a question is a
    real word to whoever said it, and a question that is *only* the wake word
    is left alone so the dialog layer can treat it as an opening rather than as
    an empty transcript.
    """
    return _WAKE_PREFIX.sub("", text.strip(), count=1)


class AsrRouterNode(Node):
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 5.3).

    subscribe  /audio/in           neo_msgs/AudioChunk
               /wake/event         neo_msgs/WakeEvent    -- opens the window
    publish    /dialog/transcript  neo_msgs/Transcript   (partials, then one final)

    Exactly one final per window, always -- including for a window that heard
    nothing. A wake event with no transcript after it leaves the dialog state
    machine waiting on something that is never coming.
    """

    def __init__(self, config: Config | None = None, intelligence: IntelligenceConfig | None = None) -> None:
        ok, why = probe()
        if not ok:
            raise RuntimeError(f"ROS ASR router unavailable: {why}")

        from neo_msgs.msg import AudioChunk, Transcript, WakeEvent
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy

        super().__init__("asr_router")
        self._config = config or Config.load()
        self._intelligence = intelligence or IntelligenceConfig.load()

        avail = availability(self._intelligence)
        if not avail.ok:
            raise RuntimeError(f"speech-to-text unavailable: {avail.reason}")

        self._sample_rate = self._config.mic.sample_rate
        self._bytes_per_s = self._sample_rate * 2
        self._endpointer = Endpointer(self._config.endpointer, sample_rate=self._sample_rate)
        self._session: Session | None = None
        self._listening = False
        self._preroll = bytearray()
        self._last_partial = ""

        # A queue, not latest-wins: Vosk decodes a chunk slower than real time
        # when the Pi is busy, and a chunk dropped meanwhile is a missing word.
        qos = QoSProfile(
            depth=10, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST
        )
        self.create_subscription(AudioChunk, "/audio/in", self._on_audio, qos)
        self.create_subscription(WakeEvent, "/wake/event", self._on_wake, 10)
        self._pub = self.create_publisher(Transcript, "/dialog/transcript", 10)

        # Load the model now rather than on the first wake: Vosk takes a second
        # or two, and spending it after someone has already said "NEO" reads as
        # the robot ignoring them.
        self._session = Session(self._intelligence, sample_rate=self._sample_rate)
        self._opened_at = 0.0
        self.create_timer(0.25, self._on_watchdog)
        self.get_logger().info(f"asr_router: vosk ready at {self._sample_rate} Hz, waiting on /wake/event")

    def _now(self) -> float:
        return self.get_clock().now().nanoseconds / 1e9

    # -- ROS callbacks -----------------------------------------------------

    def _on_wake(self, msg) -> None:
        if self._listening:
            return
        self._listening = True
        self._opened_at = self._now()
        self._last_partial = ""
        self._endpointer.reset()
        assert self._session is not None
        self._session.reset()
        if self._preroll:
            self._session.accept(bytes(self._preroll))
        self.get_logger().info("listening")

    def _on_audio(self, msg) -> None:
        data = bytes(msg.data)
        if not self._listening:
            self._remember(data)
            return

        assert self._session is not None
        result = self._session.accept(data)
        verdict = self._endpointer.accept(data)

        if result is not None and result.text.strip():
            # Vosk called the endpoint before we did. It is right often enough
            # to trust when it produces actual words.
            self._finish(result.text, result.confidence)
            return

        if verdict is Endpoint.LISTENING:
            partial = self._session.partial()
            if partial and partial != self._last_partial:
                self._last_partial = partial
                self._publish(partial, is_final=False, confidence=0.0)
            return

        if verdict is Endpoint.TIMED_OUT:
            # Nothing was said. Almost always a false accept upstream; close
            # the window with an empty final so the dialog node returns to IDLE.
            self.get_logger().info("window closed: nothing said")
            self._finish("", 0.0)
            return

        if verdict is Endpoint.TOO_LONG:
            self.get_logger().warning("window closed: utterance ran past the limit")

        final = self._session.final()
        self._finish(final.text, final.confidence)

    def _on_watchdog(self) -> None:
        """Close a window that audio stopped arriving for.

        The endpointer measures time in *audio*, so it can only close a window
        while audio keeps coming. If the microphone delivers nothing -- it was
        unplugged, or an operator opened the window from the panel on a robot
        with no mic at all -- the window would never close, the dialog would sit
        in LISTENING, and the wake word, gated on that state, would never listen
        again. This bounds the same limits on the wall clock.
        """
        if not self._listening:
            return
        cfg = self._config.endpointer
        heard = self._endpointer.heard_speech
        limit = cfg.max_utterance_s if heard else cfg.max_wait_for_speech_s
        if self._now() - self._opened_at < limit + WATCHDOG_GRACE_S:
            return
        self.get_logger().warning("window closed: audio stopped arriving")
        text, confidence = "", 0.0
        if heard and self._session is not None:
            final = self._session.final()
            text, confidence = final.text, final.confidence
        self._finish(text, confidence)

    # -- window bookkeeping -------------------------------------------------

    def _remember(self, data: bytes) -> None:
        keep = int(PREROLL_S * self._bytes_per_s)
        self._preroll.extend(data)
        if len(self._preroll) > keep:
            del self._preroll[: len(self._preroll) - keep]

    def _finish(self, text: str, confidence: float) -> None:
        self._listening = False
        self._preroll.clear()
        cleaned = strip_wake_word(text)
        self.get_logger().info(f"heard: {cleaned!r}" if cleaned else "heard nothing")
        self._publish(cleaned, is_final=True, confidence=confidence)

    def _publish(self, text: str, *, is_final: bool, confidence: float) -> None:
        from neo_msgs.msg import Transcript

        msg = Transcript()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.text = text
        msg.is_final = is_final
        msg.confidence = float(confidence)
        msg.engine = Transcript.ENGINE_VOSK
        msg.grammar_constrained = False
        self._pub.publish(msg)


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start ASR router: {why}")
        return 1

    import rclpy

    rclpy.init(args=argv)
    try:
        node = AsrRouterNode()
    except RuntimeError as exc:
        print(f"cannot start ASR router: {exc}")
        rclpy.shutdown()
        return 1
    import signal

    from rclpy.executors import ExternalShutdownException

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        # A second SIGINT (the process group's, then launch's) must not cut the
        # shutdown short; see neo_motion/nodes/head_behavior.py.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
