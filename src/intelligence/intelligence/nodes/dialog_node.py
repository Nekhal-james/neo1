"""ROS 2 node wrapping chat/asr/tts -- same thin-wrapper discipline as
model_conn/nodes/link_node.py and neo_perception/nodes/perception_node.py.

Activates once Phase 1 lands `neo_msgs`. Until then, `neo --prompt` exercises
chat.ask() directly, and status_store.py's local JSON file is what
neo_webapp reads (see neo_webapp/dialog_status.py).
"""

from __future__ import annotations

import logging

from model_conn.config import Config as ModelConnConfig

from ..chat import ask
from ..config import Config
from ..tts import synthesize_pcm

log = logging.getLogger("intelligence.nodes.dialog_node")

try:
    from rclpy.node import Node
except ImportError:  # pragma: no cover - exercised only where ROS is installed
    Node = object

# neo_msgs/DialogState state constants, mirrored as plain ints so this module
# needs no neo_msgs import at parse time (see probe()).
_IDLE, _LISTENING, _THINKING, _SPEAKING = 0, 1, 2, 3

SPEAKER_CHUNK_BYTES = 2048
"""Same chunk size as neo_webapp/voice.py's speaker channel (plan 5.6) -- not
load-bearing here since /audio/out is a topic, not a backpressured queue, but
consistent chunking keeps the two paths comparable when debugging."""

AUDIO_OUT_SAMPLE_RATE = 22050


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


class DialogNode(Node):
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 1.2 / 5).

    subscribe  /dialog/transcript   neo_msgs/Transcript   (from asr_router)
    publish    /dialog/state        neo_msgs/DialogState  (IDLE/THINKING/SPEAKING/...)
               /audio/out           neo_msgs/AudioChunk   (from tts.synthesize_pcm)

    Each *final* transcript calls chat.ask() and publishes the reply's audio as
    a run of AudioChunk messages, same core functions `neo --prompt` and a
    future ASR pipeline both call -- this class only adds the ROS plumbing.

    One turn at a time, no queue (CLAUDE.md, plan 5.6): a transcript that
    arrives while a reply is already in flight is dropped rather than queued,
    the same rule the panel's voice loop enforces at its own edge.
    """

    def __init__(self, cfg: Config, mc_cfg: ModelConnConfig) -> None:
        ok, why = probe()
        if not ok:
            raise RuntimeError(f"ROS dialog node unavailable: {why}")

        from neo_msgs.msg import AudioChunk, DialogState, Transcript

        super().__init__("dialog")

        self._cfg = cfg
        self._mc_cfg = mc_cfg
        self._busy = False
        self._seq = 0

        self.create_subscription(Transcript, "/dialog/transcript", self._on_transcript, 10)
        self._state_pub = self.create_publisher(DialogState, "/dialog/state", 10)
        self._audio_pub = self.create_publisher(AudioChunk, "/audio/out", 10)

        self._publish_state(_IDLE)
        self.get_logger().info("dialog_node: waiting on /dialog/transcript")

    # -- core, reused by neo --prompt and any future caller -----------------

    def on_transcript(self, text: str, mc_cfg: ModelConnConfig | None = None) -> str:
        result = ask(text, cfg=self._cfg, mc_cfg=mc_cfg or self._mc_cfg)
        return result.reply

    # -- ROS callbacks -----------------------------------------------------

    def _on_transcript(self, msg) -> None:
        if not msg.is_final:
            return
        if self._busy:
            # One turn at a time, no queue -- see class docstring.
            self.get_logger().warning("dropping transcript: a reply is already in flight")
            return
        self._busy = True
        try:
            self._publish_state(_THINKING)
            reply = self.on_transcript(msg.text)
            self._speak(reply)
        finally:
            self._publish_state(_IDLE)
            self._busy = False

    def _speak(self, text: str) -> None:
        self._publish_state(_SPEAKING)
        try:
            pcm = synthesize_pcm(text, self._cfg, target_rate=AUDIO_OUT_SAMPLE_RATE)
        except Exception:  # noqa: BLE001 - a TTS failure must not crash the node
            self.get_logger().error("speech synthesis failed", exc_info=True)
            return
        for offset in range(0, len(pcm.data), SPEAKER_CHUNK_BYTES):
            self._publish_audio(pcm.data[offset : offset + SPEAKER_CHUNK_BYTES], pcm.sample_rate)

    def _publish_state(self, state: int, **flags) -> None:
        from neo_msgs.msg import DialogState

        msg = DialogState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.state = state
        msg.degraded = flags.get("degraded", False)
        msg.estop = flags.get("estop", False)
        msg.follow_up_open = flags.get("follow_up_open", False)
        self._state_pub.publish(msg)

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
        print(f"cannot start dialog node: {why}")
        print("Use 'neo --prompt \"...\"' instead -- it drives chat.ask() directly.")
        return 1

    import rclpy

    rclpy.init(args=argv)
    node = DialogNode(Config.load(), ModelConnConfig.load())
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
