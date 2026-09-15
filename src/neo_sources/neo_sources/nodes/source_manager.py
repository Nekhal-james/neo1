"""Owns /sources/set, publishes /sources/state, and is the mux for the browser.

The one place that decides whether the camera, microphone and speaker are the
Pi's hardware or a browser's. Everything downstream of a source is written not
to care which (CLAUDE.md).

Three decisions worth stating, because each was tempting to do the other way:

* **It does not start or stop the hardware backends.** They subscribe to
  /sources/state and gate themselves. A manager that owned their lifecycle
  would be a single process whose crash could leave a microphone hot with
  nothing left to switch it off; self-gating backends fail closed instead.
* **The state topic is latched.** A backend that starts after this node --
  which is every backend, on a Pi where the detector loads a model first --
  must still learn the current selection. Without TRANSIENT_LOCAL durability
  it would sit on its default ("hardware", i.e. holding the device) until
  somebody happened to change a source.
* **It is the mux for the webapp backend.** The admin panel publishes a
  browser's camera and mic on /camera/webapp/image_raw and /audio/webapp/in and
  listens for speech on /audio/webapp/out; nothing else in the graph touches
  those. Only while a stream is switched to webapp does this forward it onto
  the topic the graph reads -- so a browser tab left open cannot inject frames
  or audio into a robot that is using its own devices.
"""

from __future__ import annotations

import logging

from ..playback import PlaybackClock
from ..selection import WEBAPP, Selection, backend_name, stream_name, validate

log = logging.getLogger(__name__)

try:
    from rclpy.node import Node
except ImportError:  # pragma: no cover - exercised only where ROS is installed
    Node = object

PLAYING_HZ = 10.0


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


class SourceManagerNode(Node):
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 2.1).

    service  /sources/set               neo_msgs/SetSource
    publish  /sources/state             neo_msgs/SourceState   (latched, on change)
    mux      /camera/webapp/image_raw -> /camera/image_raw     while camera = webapp
             /audio/webapp/in         -> /audio/in             while mic = webapp
             /audio/out               -> /audio/webapp/out     while speaker = webapp
    publish  /audio/playing             std_msgs/Bool          while speaker = webapp

    /audio/playing is speaker_hw's job while the speaker is the Pi's own. For
    the browser's it has to be *estimated* here, from the play cursor, because
    nothing on the robot can hear the browser -- and without it the wake word
    would score Neo's own reply.

    Parameters:
      initial_camera / initial_mic / initial_speaker -- "hardware" or "webapp".
    The names come from neo.launch.py, which reads them out of the profile's
    `sources:` block and passes them here and nowhere else: everything
    downstream of the mux is forbidden to know which backend is live (plan 1.2).
    """

    def __init__(self, selection: Selection | None = None) -> None:
        ok, why = probe()
        if not ok:
            raise RuntimeError(f"ROS source manager unavailable: {why}")

        from neo_msgs.msg import AudioChunk, SourceState
        from neo_msgs.srv import SetSource
        from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import Image
        from std_msgs.msg import Bool

        super().__init__("source_manager")

        if selection is None:
            self.declare_parameter("initial_camera", "hardware")
            self.declare_parameter("initial_mic", "hardware")
            self.declare_parameter("initial_speaker", "hardware")
            selection = Selection(
                camera=self._backend_param("initial_camera"),
                mic=self._backend_param("initial_mic"),
                speaker=self._backend_param("initial_speaker"),
            )
        self._selection = selection

        latched = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST,
        )
        # Frames are latest-wins: a stale frame is worthless once a newer exists.
        frames = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST
        )
        # Audio is not: a dropped chunk is a hole in a word, never replaced by a
        # newer one. A short queue, still best effort so a stall cannot back up.
        audio_in = QoSProfile(
            depth=10, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST
        )
        # Speech out bursts a whole sentence at once, far faster than real time.
        speech = QoSProfile(
            depth=64, reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST
        )

        self._pub = self.create_publisher(SourceState, "/sources/state", latched)
        self.create_service(SetSource, "/sources/set", self._on_set)

        self._playback = PlaybackClock()
        self._was_playing = False
        self._mic_seq = 0
        self._camera_out = self.create_publisher(Image, "/camera/image_raw", frames)
        self._mic_out = self.create_publisher(AudioChunk, "/audio/in", audio_in)
        self._speaker_out = self.create_publisher(AudioChunk, "/audio/webapp/out", speech)
        self._playing_pub = self.create_publisher(Bool, "/audio/playing", 10)
        self.create_subscription(Image, "/camera/webapp/image_raw", self._on_webapp_camera, frames)
        self.create_subscription(AudioChunk, "/audio/webapp/in", self._on_webapp_mic, audio_in)
        self.create_subscription(AudioChunk, "/audio/out", self._on_audio_out, speech)
        self.create_timer(1.0 / PLAYING_HZ, self._on_playing_timer)

        self._publish()
        self.get_logger().info(f"source_manager: {self._selection.describe()}")

    def _backend_param(self, name: str) -> int:
        value = str(self.get_parameter(name).value).strip().lower()
        if value not in ("hardware", "webapp"):
            self.get_logger().warning(
                f"source_manager: {name}={value!r} is not 'hardware' or 'webapp'; using hardware"
            )
            return 0
        return 0 if value == "hardware" else 1

    @property
    def selection(self) -> Selection:
        return self._selection

    # -- selection -------------------------------------------------------------

    def _on_set(self, request, response):
        ok, why = validate(request.stream, request.backend)
        if not ok:
            response.success = False
            response.message = why
            response.state = self._state_msg()
            self.get_logger().warning(f"source_manager: refused -- {why}")
            return response

        previous = self._selection
        self._selection = self._selection.with_stream(request.stream, request.backend)
        if previous.speaker == WEBAPP and self._selection.speaker != WEBAPP:
            # Whatever was scheduled belongs to a listener who is no longer the
            # speaker; carrying it over would mute the wake word for nothing.
            self._playback.reset()
        self._publish()
        response.success = True
        response.message = f"{stream_name(request.stream)} -> {backend_name(request.backend)}"
        response.state = self._state_msg()
        self.get_logger().info(f"source_manager: {response.message}")
        return response

    def _state_msg(self):
        from neo_msgs.msg import SourceState

        msg = SourceState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.camera = self._selection.camera
        msg.mic = self._selection.mic
        msg.speaker = self._selection.speaker
        # Backends self-gate on receipt, so there is no window in which one is
        # half-activated and nothing to report as in flight.
        msg.transitioning = SourceState.STREAM_NONE
        return msg

    def _publish(self) -> None:
        self._pub.publish(self._state_msg())

    # -- the mux ---------------------------------------------------------------

    def _on_webapp_camera(self, msg) -> None:
        if self._selection.camera == WEBAPP:
            self._camera_out.publish(msg)

    def _on_webapp_mic(self, msg) -> None:
        if self._selection.mic != WEBAPP:
            return
        # Renumbered here: the panel does not number its chunks, and a gap in
        # `seq` is the only way a consumer can tell a dropped chunk from silence.
        msg.seq = self._mic_seq
        self._mic_seq = (self._mic_seq + 1) & 0xFFFFFFFF
        self._mic_out.publish(msg)

    def _on_audio_out(self, msg) -> None:
        if self._selection.speaker != WEBAPP:
            return
        self._speaker_out.publish(msg)
        self._playback.add(len(msg.data), msg.sample_rate, max(1, msg.channels))

    def _on_playing_timer(self) -> None:
        from std_msgs.msg import Bool

        playing = self._selection.speaker == WEBAPP and self._playback.playing()
        # Every tick while playing, and once on the falling edge -- a gate that
        # never hears "stopped" keeps the wake word deaf forever.
        if playing or self._was_playing:
            self._playing_pub.publish(Bool(data=playing))
        self._was_playing = playing


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start source manager: {why}")
        return 1

    import rclpy

    rclpy.init(args=argv)
    node = SourceManagerNode()
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
