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

        TODO(phase-1): fill in once neo_msgs exists. The subscriptions write into
        `self._state` via `_loop.call_soon_threadsafe`; the publishers are driven
        from the media bridges.

          subscribe  /dialog/state        neo_msgs/DialogState
                     /link/health         neo_msgs/LinkHealth
                     # future publisher: model_conn/nodes/link_node.py (model-conn
                     # package). Until then, ../link_status.py reads model-conn's
                     # local status file instead, with no ROS involved.
                     /head/state          sensor_msgs/JointState
                     /emotion/state       neo_msgs/EmotionState
                     /sources/state       neo_msgs/SourcesState
          publish    /joy                 sensor_msgs/Joy
                     /camera/webapp/image_raw   sensor_msgs/Image
                     /audio/webapp/in     neo_msgs/AudioChunk
          subscribe  /audio/webapp/out    neo_msgs/AudioChunk
          client     /sources/set         neo_msgs/srv/SetSource
                     /head/center         std_srvs/Trigger
                     /system/estop        std_srvs/SetBool
        """
        self._state.nodes = [NodeStatus(name="(discovering)", state="missing")]

    def snapshot(self) -> RobotState:
        return self._state

    async def set_source(self, stream: Stream, backend: Backend) -> Result:
        raise NotImplementedError("phase-1: call /sources/set")

    async def set_estop(self, engaged: bool) -> Result:
        raise NotImplementedError("phase-1: call /system/estop")

    async def center_head(self) -> Result:
        raise NotImplementedError("phase-1: call /head/center")

    async def publish_joy(self, axes: list[float], buttons: list[int]) -> None:
        raise NotImplementedError("phase-1: publish sensor_msgs/Joy")

    async def publish_camera_frame(self, jpeg: bytes) -> None:
        raise NotImplementedError("phase-1: publish /camera/webapp/image_raw")

    async def publish_mic_chunk(self, pcm_s16le: bytes) -> None:
        raise NotImplementedError("phase-1: publish /audio/webapp/in")
