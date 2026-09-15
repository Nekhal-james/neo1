"""Piper, local, streaming sentence by sentence: /dialog/reply -> /audio/out.

Synthesis lives in its own node rather than inside the dialog node for one
concrete reason: it is slow and it blocks. Piper takes appreciable wall time
for a paragraph, and doing that inside a subscription callback stalls that
node's whole executor -- including the state publishing that tells everything
else what Neo is doing. Here it stalls only synthesis.

Sentence by sentence, not reply by reply: the first sentence starts playing
while the rest is still being synthesised, which is the difference between
answering in ~400 ms and answering after the whole paragraph exists.
"""

from __future__ import annotations

import logging
import queue
import threading

from intelligence.config import Config as IntelligenceConfig
from intelligence.tts import availability, split_sentences, synthesize_pcm

from ..config import Config

log = logging.getLogger(__name__)

try:
    from rclpy.node import Node
except ImportError:  # pragma: no cover - exercised only where ROS is installed
    Node = object

CHUNK_BYTES = 2048
"""Same chunking as the panel's speaker channel, so the two paths stay
comparable when something has to be debugged across both."""


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


class TtsNode(Node):
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 5.5).

    subscribe  /dialog/reply     neo_msgs/Reply
               /dialog/cancel    std_msgs/Empty      -- e-stop and barge-in
    publish    /audio/out        neo_msgs/AudioChunk
               /dialog/speaking  std_msgs/Bool       -- true from first chunk to last

    One reply at a time and no queue (CLAUDE.md, plan 5.6). A reply arriving
    while one is still being spoken replaces it: the newer answer is the one
    that matches what was just asked, and queueing them means Neo answering a
    question nobody remembers asking.
    """

    def __init__(self, config: Config | None = None, intelligence: IntelligenceConfig | None = None) -> None:
        ok, why = probe()
        if not ok:
            raise RuntimeError(f"ROS TTS node unavailable: {why}")

        from neo_msgs.msg import AudioChunk, Reply
        from std_msgs.msg import Bool, Empty

        super().__init__("tts")
        self._config = config or Config.load()
        self._intelligence = intelligence or IntelligenceConfig.load()

        avail = availability(self._intelligence)
        if not avail.ok:
            raise RuntimeError(f"speech synthesis unavailable: {avail.reason}")

        self._rate = self._config.speaker.sample_rate
        self._seq = 0
        self._work: queue.Queue[str | None] = queue.Queue()
        self._generation = 0
        self._lock = threading.Lock()

        self.create_subscription(Reply, "/dialog/reply", self._on_reply, 10)
        self.create_subscription(Empty, "/dialog/cancel", self._on_cancel, 10)
        # Depth 64: a sentence is published far faster than real time, and a
        # reliable writer only keeps `depth` samples to redeliver from.
        self._audio_pub = self.create_publisher(AudioChunk, "/audio/out", 64)
        self._speaking_pub = self.create_publisher(Bool, "/dialog/speaking", 10)

        self._thread = threading.Thread(target=self._run, name="tts", daemon=True)
        self._thread.start()
        self.get_logger().info("tts: piper ready, waiting on /dialog/reply")

    def _on_reply(self, msg) -> None:
        text = (msg.text or "").strip()
        if not text:
            return
        with self._lock:
            self._generation += 1
        # Drain: whatever was pending belongs to the previous answer.
        while not self._work.empty():
            try:
                self._work.get_nowait()
            except queue.Empty:
                break
        self._work.put(text)

    def _on_cancel(self, msg) -> None:
        with self._lock:
            self._generation += 1
        while not self._work.empty():
            try:
                self._work.get_nowait()
            except queue.Empty:
                break
        self.get_logger().info("tts: cancelled")

    def _run(self) -> None:
        while True:
            text = self._work.get()
            if text is None:
                return
            with self._lock:
                generation = self._generation
            self._speak(text, generation)

    def _speak(self, text: str, generation: int) -> None:
        self._publish_speaking(True)
        try:
            for sentence in split_sentences(text):
                with self._lock:
                    if generation != self._generation:
                        return  # superseded mid-reply
                try:
                    pcm = synthesize_pcm(sentence, self._intelligence, target_rate=self._rate)
                except Exception:  # noqa: BLE001 - one bad sentence must not end the node
                    self.get_logger().error(f"could not synthesise {sentence!r}", exc_info=True)
                    continue
                for offset in range(0, len(pcm.data), CHUNK_BYTES):
                    with self._lock:
                        if generation != self._generation:
                            return
                    self._publish_audio(pcm.data[offset : offset + CHUNK_BYTES], pcm.sample_rate)
        finally:
            with self._lock:
                current = self._generation
            if generation == current:
                self._publish_speaking(False)

    def _publish_speaking(self, speaking: bool) -> None:
        from std_msgs.msg import Bool

        self._speaking_pub.publish(Bool(data=speaking))

    def _publish_audio(self, data: bytes, sample_rate: int) -> None:
        from neo_msgs.msg import AudioChunk

        msg = AudioChunk()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.seq = self._seq
        self._seq += 1
        msg.sample_rate = sample_rate
        msg.channels = 1
        msg.encoding = AudioChunk.ENCODING_PCM_S16LE
        msg.data = list(data)
        self._audio_pub.publish(msg)


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start TTS: {why}")
        return 1

    import rclpy

    rclpy.init(args=argv)
    try:
        node = TtsNode()
    except RuntimeError as exc:
        print(f"cannot start TTS: {exc}")
        rclpy.shutdown()
        return 1
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
