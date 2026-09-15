"""The attached speaker: /audio/out to ALSA.

The 3.5 mm jack is off on this robot -- its analog audio shares the hardware
PWM block the servos use (CLAUDE.md) -- so this is a USB speaker, and
`default_playback()` prefers anything that is not HDMI for exactly that reason.

Publishes /audio/playing so the half-duplex gate has an *audible* end time to
work from. Writing PCM to `aplay` returns long before the sound stops coming
out of the speaker; a gate built on the write would unmute the mic while Neo
was still mid-sentence and it would transcribe its own reply.
"""

from __future__ import annotations

import logging

from ..config import Config
from ..devices import SpeakerPlayback

log = logging.getLogger(__name__)

try:
    from rclpy.node import Node
except ImportError:  # pragma: no cover - exercised only where ROS is installed
    Node = object

_BACKEND_HARDWARE = 0


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


class SpeakerHwNode(Node):
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 2).

    subscribe  /audio/out       neo_msgs/AudioChunk
               /sources/state   neo_msgs/SourceState   -- gates playback
    publish    /audio/playing   std_msgs/Bool          @ 10 Hz
    """

    def __init__(self, config: Config | None = None) -> None:
        ok, why = probe()
        if not ok:
            raise RuntimeError(f"ROS speaker backend unavailable: {why}")

        from neo_msgs.msg import AudioChunk, SourceState
        from std_msgs.msg import Bool

        super().__init__("speaker_hw")
        self._config = config or Config.load()
        self._speaker = SpeakerPlayback(
            sample_rate=self._config.speaker.sample_rate,
            device=self._config.speaker.device or None,
        )
        self._active = True
        self._was_playing = False

        # Matches tts's depth: a whole sentence arrives in one burst.
        self.create_subscription(AudioChunk, "/audio/out", self._on_audio, 64)
        self.create_subscription(SourceState, "/sources/state", self._on_sources, 10)
        self._playing_pub = self.create_publisher(Bool, "/audio/playing", 10)
        self.create_timer(0.1, self._on_timer)

        available, why = self._speaker.available()
        if available:
            self.get_logger().info(f"speaker_hw: {self._speaker.device_name()}")
        else:
            self.get_logger().warning(f"speaker_hw: {why}")

    def _on_sources(self, msg) -> None:
        active = msg.speaker == _BACKEND_HARDWARE
        if active != self._active:
            self._active = active
            self.get_logger().info(f"speaker_hw: {'taking' if active else 'releasing'} the speaker")
            if not active:
                self._speaker.stop()

    def _on_audio(self, msg) -> None:
        if not self._active:
            return
        if msg.sample_rate != self._speaker.sample_rate:
            # Reopening `aplay` at the new rate rather than resampling here:
            # ALSA's `plughw` already does that conversion, and doing it twice
            # is how audio picks up artefacts nobody can account for later.
            self._speaker.stop()
            self._speaker.sample_rate = msg.sample_rate
        self._speaker.play(bytes(msg.data))

    def _on_timer(self) -> None:
        from std_msgs.msg import Bool

        playing = self._active and self._speaker.is_playing()
        # Publish every tick while playing, and once on the falling edge: the
        # gate needs a prompt "stopped", and a latched-true that never clears
        # would leave the mic muted forever.
        if playing or self._was_playing:
            self._playing_pub.publish(Bool(data=playing))
        self._was_playing = playing

    def destroy_node(self) -> None:
        self._speaker.stop()
        super().destroy_node()


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start speaker backend: {why}")
        return 1

    import rclpy

    rclpy.init(args=argv)
    node = SpeakerHwNode()
    import signal

    from rclpy.executors import ExternalShutdownException

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        # A second SIGINT (the process group's, then launch's) must not leave
        # aplay holding the device; see neo_motion/nodes/head_behavior.py.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
