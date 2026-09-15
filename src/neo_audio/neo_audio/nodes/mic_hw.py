"""The Pi's USB microphone, published as /audio/in.

One of the two mic backends. The other is a browser's microphone arriving over
the admin panel's WebSocket; downstream nodes cannot tell which is running,
which is the whole point of the source seam (CLAUDE.md).

This node gates itself on /sources/state rather than being started and stopped
by the source manager. A backend that owns its own on/off is a backend that
cannot be left half-activated by a crash in the thing that was supposed to
switch it, and "the mic is still hot after switching to the browser" is the
failure that matters here.
"""

from __future__ import annotations

import logging
import threading

from ..config import Config
from ..devices import MicCapture

log = logging.getLogger(__name__)

try:
    from rclpy.node import Node
except ImportError:  # pragma: no cover - exercised only where ROS is installed
    Node = object

_BACKEND_HARDWARE = 0
_STREAM_MIC = 2


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


class MicHwNode(Node):
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 2).

    subscribe  /sources/state   neo_msgs/SourceState   -- gates capture
    publish    /audio/in        neo_msgs/AudioChunk    @ chunk_ms

    Capture runs on its own thread because `arecord` reads block for a whole
    chunk. Doing that in a timer callback would stall the executor for 100 ms
    at a time and starve the /sources/state subscription that is supposed to be
    able to switch this node off.
    """

    def __init__(self, config: Config | None = None) -> None:
        ok, why = probe()
        if not ok:
            raise RuntimeError(f"ROS mic backend unavailable: {why}")

        from neo_msgs.msg import AudioChunk, SourceState
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy

        super().__init__("mic_hw")
        self._config = config or Config.load()

        self._capture = MicCapture(
            sample_rate=self._config.mic.sample_rate,
            device=self._config.mic.device or None,
            chunk_ms=self._config.mic.chunk_ms,
        )
        self._seq = 0
        self._active = True
        self._stop = threading.Event()

        # BEST_EFFORT, but a short queue rather than latest-wins. Unlike a
        # camera frame, a dropped audio chunk is never replaced by a newer one:
        # it is a hole in the very word the wake word is listening for.
        qos = QoSProfile(
            depth=10, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST
        )
        self._pub = self.create_publisher(AudioChunk, "/audio/in", qos)
        self.create_subscription(SourceState, "/sources/state", self._on_sources, 10)

        available, why = self._capture.available()
        if available:
            self.get_logger().info(f"mic_hw: {self._capture.device_name()} at {self._capture.sample_rate} Hz")
        else:
            # Not fatal. The mic is a USB device that can be plugged in after
            # boot, and the capture loop retries -- refusing to start would
            # mean a reboot to recover from a loose plug.
            self.get_logger().warning(f"mic_hw: {why}; will retry while it stays unplugged")

        self._thread = threading.Thread(target=self._run, name="mic_hw", daemon=True)
        self._thread.start()

    def _on_sources(self, msg) -> None:
        active = msg.mic == _BACKEND_HARDWARE
        if active == self._active:
            return
        self._active = active
        self.get_logger().info(f"mic_hw: {'taking' if active else 'releasing'} the microphone")
        if not active:
            # Release the device outright rather than reading and discarding:
            # the browser is about to be the mic, and a USB device held open by
            # a process that is ignoring it is still a device nobody else can have.
            self._capture.stop()

    def _run(self) -> None:
        from neo_msgs.msg import AudioChunk

        while not self._stop.is_set():
            if not self._active:
                self._stop.wait(0.2)
                continue
            data = self._capture.read()
            if not data:
                self._stop.wait(0.2)
                continue
            msg = AudioChunk()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.seq = self._seq
            self._seq += 1
            msg.sample_rate = self._capture.sample_rate
            msg.channels = 1
            msg.encoding = AudioChunk.ENCODING_PCM_S16LE
            msg.data = list(data)
            self._pub.publish(msg)

    def destroy_node(self) -> None:
        self._stop.set()
        self._capture.stop()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        super().destroy_node()


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start mic backend: {why}")
        return 1

    import rclpy

    rclpy.init(args=argv)
    node = MicHwNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
