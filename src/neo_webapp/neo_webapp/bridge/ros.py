"""ROS 2 backend.

Activates on the Pi once Phase 1 lands `neo_msgs`. Deliberately the only file in
the package that imports rclpy, so the panel remains runnable on a machine with no
ROS installed.

The rclpy executor runs on its own thread; every hand-off to the API happens via
`loop.call_soon_threadsafe`, because FastAPI's handlers all live on the event loop.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from .base import Bridge
from .types import Backend, NodeStatus, Result, RobotState, Stream

# neo_msgs constants, mirrored as plain data so this module needs no neo_msgs
# import until `start()` actually runs (see probe()).
_STREAM_TO_MSG = {"camera": 1, "mic": 2, "speaker": 3}
_BACKEND_TO_MSG = {"hardware": 0, "webapp": 1}
_BACKEND_FROM_MSG = {0: "hardware", 1: "webapp"}
_STREAM_FROM_MSG = {0: None, 1: "camera", 2: "mic", 3: "speaker"}
_PATH_FROM_MSG = {0: "none", 1: "eth", 2: "wifi"}
_EMOTION_LABEL_NAMES = ("NEUTRAL", "ATTENTIVE", "HAPPY", "CURIOUS", "CONFUSED", "SLEEPY")

SERVICE_TIMEOUT_S = 3.0


class RosUnavailable(RuntimeError):
    """Raised when the ROS backend is requested but its dependencies are absent."""


def probe() -> tuple[bool, str]:
    """Report whether the ROS backend can run here, and why not if it can't."""
    try:
        import rclpy  # noqa: F401
    except ImportError:
        return False, "rclpy not installed"
    try:
        import neo_msgs.msg  # noqa: F401
    except ImportError:
        return False, "neo_msgs not built (Phase 1)"
    return True, "ok"


class RosBridge(Bridge):
    name = "ros"

    def __init__(self, *, node_name: str = "neo_admin_panel") -> None:
        super().__init__()
        ok, why = probe()
        if not ok:
            raise RosUnavailable(why)
        self._node_name = node_name
        self._state = RobotState(backend=self.name)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._node: Any = None
        self._executor: Any = None

    async def start(self) -> None:
        import rclpy
        from rclpy.executors import MultiThreadedExecutor
        from rclpy.node import Node

        self._loop = asyncio.get_running_loop()
        rclpy.init(args=None)
        self._node = Node(self._node_name)
        self._executor = MultiThreadedExecutor()
        self._executor.add_node(self._node)
        self._declare_io()
        self._thread = threading.Thread(
            target=self._executor.spin, name="rclpy-spin", daemon=True
        )
        self._thread.start()

    async def stop(self) -> None:
        import rclpy

        if self._executor is not None:
            self._executor.shutdown()
        if self._node is not None:
            self._node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _declare_io(self) -> None:
        """Publishers, subscriptions and clients for the contracts in plan 1.2.

          subscribe  /dialog/state        neo_msgs/DialogState
                     /link/health         neo_msgs/LinkHealth
                     # future publisher: model_conn/nodes/link_node.py (model-conn
                     # package). Until then, ../link_status.py reads model-conn's
                     # local status file instead, with no ROS involved.
                     /head/state          sensor_msgs/JointState
                     /emotion/state       neo_msgs/EmotionState
                     /sources/state       neo_msgs/SourceState
          publish    /joy                 sensor_msgs/Joy
                     /camera/webapp/image_raw   sensor_msgs/Image
                     /audio/webapp/in     neo_msgs/AudioChunk
          subscribe  /audio/webapp/out    neo_msgs/AudioChunk
          client     /sources/set         neo_msgs/srv/SetSource
                     /head/center         std_srvs/Trigger
                     /system/estop        std_srvs/SetBool
        """
        from neo_msgs.msg import AudioChunk, DialogState, EmotionState, LinkHealth, SourceState
        from neo_msgs.srv import SetSource
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import Image, Joy, JointState
        from std_srvs.srv import SetBool, Trigger

        n = self._node
        latest = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST
        )

        n.create_subscription(DialogState, "/dialog/state", self._on_dialog_state, 10)
        n.create_subscription(LinkHealth, "/link/health", self._on_link_health, 10)
        n.create_subscription(JointState, "/head/state", self._on_head_state, latest)
        n.create_subscription(EmotionState, "/emotion/state", self._on_emotion_state, 10)
        n.create_subscription(SourceState, "/sources/state", self._on_source_state, 10)
        n.create_subscription(AudioChunk, "/audio/webapp/out", self._on_audio_out, latest)

        self._joy_pub = n.create_publisher(Joy, "/joy", latest)
        self._camera_pub = n.create_publisher(Image, "/camera/webapp/image_raw", latest)
        self._mic_pub = n.create_publisher(AudioChunk, "/audio/webapp/in", latest)

        self._set_source_client = n.create_client(SetSource, "/sources/set")
        self._center_client = n.create_client(Trigger, "/head/center")
        self._estop_client = n.create_client(SetBool, "/system/estop")

        self._state.nodes = [NodeStatus(name=self._node_name, state="active")]

    def snapshot(self) -> RobotState:
        return self._state

    # -- subscription callbacks (rclpy thread) -----------------------------
    #
    # Each hops to the asyncio loop before touching self._state, per the module
    # docstring: FastAPI's handlers, which read self._state, all live there.

    def _on_dialog_state(self, msg) -> None:
        if msg.estop:
            flattened = "ESTOP"
        elif msg.degraded:
            flattened = "DEGRADED"
        else:
            flattened = ("IDLE", "LISTENING", "THINKING", "SPEAKING")[msg.state]
        self._loop.call_soon_threadsafe(self._set_dialog_state, flattened)

    def _set_dialog_state(self, value: str) -> None:
        self._state.dialog_state = value

    def _on_link_health(self, msg) -> None:
        self._loop.call_soon_threadsafe(self._set_link_health, msg)

    def _set_link_health(self, msg) -> None:
        link = self._state.link
        link.up = bool(msg.up)
        link.active_path = _PATH_FROM_MSG.get(msg.active_path, "none")
        link.rtt_ms = msg.rtt_ms if msg.up else None
        link.consecutive_failures = msg.consecutive_failures

    def _on_head_state(self, msg) -> None:
        if len(msg.position) < 2:
            return
        self._loop.call_soon_threadsafe(self._set_head_state, msg.position[0], msg.position[1])

    def _set_head_state(self, pan_rad: float, tilt_rad: float) -> None:
        import math

        self._state.head.pan_deg = math.degrees(pan_rad)
        self._state.head.tilt_deg = math.degrees(tilt_rad)

    def _on_emotion_state(self, msg) -> None:
        self._loop.call_soon_threadsafe(self._set_emotion_state, msg)

    def _set_emotion_state(self, msg) -> None:
        self._state.emotion.label = _EMOTION_LABEL_NAMES[msg.label]
        self._state.emotion.intensity = msg.intensity

    def _on_source_state(self, msg) -> None:
        self._loop.call_soon_threadsafe(self._set_source_state, msg)

    def _set_source_state(self, msg) -> None:
        sources = self._state.sources
        sources.camera = _BACKEND_FROM_MSG.get(msg.camera, "hardware")
        sources.mic = _BACKEND_FROM_MSG.get(msg.mic, "hardware")
        sources.speaker = _BACKEND_FROM_MSG.get(msg.speaker, "hardware")
        sources.transitioning = _STREAM_FROM_MSG.get(msg.transitioning)

    def _on_audio_out(self, msg) -> None:
        # Speech, so back-pressured push rather than the drop-on-full path --
        # see Bridge.push_audio_out on why emit_audio_out would truncate it.
        asyncio.run_coroutine_threadsafe(
            self.push_audio_out(bytes(msg.data)), self._loop
        )

    # -- service call bridging (asyncio thread) ----------------------------

    async def _call(self, client, request, timeout_s: float = SERVICE_TIMEOUT_S):
        if not client.service_is_ready():
            raise RosUnavailable(f"{client.srv_name} has no server")
        rclpy_future = client.call_async(request)
        asyncio_future: asyncio.Future = self._loop.create_future()

        def _done(f) -> None:
            if asyncio_future.cancelled():
                return
            try:
                result = f.result()
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller below
                self._loop.call_soon_threadsafe(asyncio_future.set_exception, exc)
            else:
                self._loop.call_soon_threadsafe(asyncio_future.set_result, result)

        rclpy_future.add_done_callback(_done)
        return await asyncio.wait_for(asyncio_future, timeout_s)

    # -- commands ------------------------------------------------------------

    async def set_source(self, stream: Stream, backend: Backend) -> Result:
        from neo_msgs.srv import SetSource

        req = SetSource.Request()
        req.stream = _STREAM_TO_MSG[stream]
        req.backend = _BACKEND_TO_MSG[backend]
        try:
            resp = await self._call(self._set_source_client, req)
        except (RosUnavailable, asyncio.TimeoutError) as exc:
            return Result(ok=False, message=str(exc))
        return Result(ok=resp.success, message=resp.message)

    async def set_estop(self, engaged: bool) -> Result:
        from std_srvs.srv import SetBool

        req = SetBool.Request()
        req.data = engaged
        try:
            resp = await self._call(self._estop_client, req)
        except (RosUnavailable, asyncio.TimeoutError) as exc:
            return Result(ok=False, message=str(exc))
        return Result(ok=resp.success, message=resp.message)

    async def center_head(self) -> Result:
        from std_srvs.srv import Trigger

        req = Trigger.Request()
        try:
            resp = await self._call(self._center_client, req)
        except (RosUnavailable, asyncio.TimeoutError) as exc:
            return Result(ok=False, message=str(exc))
        return Result(ok=resp.success, message=resp.message)

    # -- media in (panel -> robot) ------------------------------------------

    async def publish_joy(self, axes: list[float], buttons: list[int]) -> None:
        from sensor_msgs.msg import Joy

        msg = Joy()
        msg.header.stamp = self._node.get_clock().now().to_msg()
        msg.axes = [float(a) for a in axes]
        msg.buttons = [int(b) for b in buttons]
        self._joy_pub.publish(msg)

    async def publish_camera_frame(self, jpeg: bytes) -> None:
        from sensor_msgs.msg import Image

        # Published pre-encoded (JPEG bytes in a generic Image field) rather
        # than decoded to bgr8: the browser already sends JPEG, and decoding
        # only to re-encode for cv_bridge on the receiving end would cost a
        # frame's worth of CPU the perception pipeline needs instead.
        msg = Image()
        msg.header.stamp = self._node.get_clock().now().to_msg()
        msg.encoding = "jpeg"
        msg.data = list(jpeg)
        self._camera_pub.publish(msg)

    async def publish_mic_chunk(self, pcm_s16le: bytes) -> None:
        from neo_msgs.msg import AudioChunk

        msg = AudioChunk()
        msg.header.stamp = self._node.get_clock().now().to_msg()
        msg.sample_rate = 16000
        msg.channels = 1
        msg.encoding = AudioChunk.ENCODING_PCM_S16LE
        msg.data = list(pcm_s16le)
        self._mic_pub.publish(msg)
