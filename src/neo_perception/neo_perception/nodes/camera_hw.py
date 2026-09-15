"""The Pi's camera, published as /camera/image_raw.

One of the two camera backends; the other is a browser's camera arriving over
the admin panel. Perception cannot tell which is running, which is the point of
the source seam (CLAUDE.md).

Publishes at the *camera's* rate, not the detector's. Perception drops what it
cannot keep up with (its own `target_fps` is 4 on a Pi 4), and there are other
consumers -- the panel's preview, an operator watching -- for whom a smooth
15 fps preview matters and a 4 fps one looks broken.
"""

from __future__ import annotations

import logging

from ..camera import Camera

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
    try:
        import cv_bridge  # noqa: F401
    except ImportError:
        return False, "cv_bridge not installed (apt install ros-jazzy-cv-bridge)"
    return True, "ok"


class CameraHwNode(Node):
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 2).

    subscribe  /sources/state     neo_msgs/SourceState   -- gates capture
    publish    /camera/image_raw  sensor_msgs/Image      @ fps

    Parameters (set per profile in neo_bringup/launch/neo.launch.py):
      device  ""     -- "" auto-detects; "/dev/video0" or an index pins it
      width   640
      height  480
      fps     15.0

    640x480 because that is what the pose model is fed anyway and a Pi 4's USB
    2.0 bus is shared with everything else. Raising it costs bandwidth and
    buys nothing downstream.
    """

    def __init__(self) -> None:
        ok, why = probe()
        if not ok:
            raise RuntimeError(f"ROS camera backend unavailable: {why}")

        from cv_bridge import CvBridge
        from neo_msgs.msg import SourceState
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import Image

        super().__init__("camera_hw")

        self.declare_parameter("device", "")
        self.declare_parameter("width", 640)
        self.declare_parameter("height", 480)
        self.declare_parameter("fps", 15.0)

        device = self.get_parameter("device").value or None
        if isinstance(device, str) and device.isdigit():
            device = int(device)
        fps = float(self.get_parameter("fps").value)

        self._camera = Camera(
            device=device,
            width=int(self.get_parameter("width").value),
            height=int(self.get_parameter("height").value),
            fps=fps,
        )
        self._bridge = CvBridge()
        self._active = True
        self._complained = False

        qos = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST
        )
        self._pub = self.create_publisher(Image, "/camera/image_raw", qos)
        self.create_subscription(SourceState, "/sources/state", self._on_sources, 10)
        self.create_timer(1.0 / max(fps, 1.0), self._on_timer)

        available, why = self._camera.available()
        if available:
            self.get_logger().info(f"camera_hw: publishing /camera/image_raw at {fps:g} fps")
        else:
            # Not fatal: a USB camera can be plugged in after boot, and `read()`
            # re-runs discovery. Refusing to start would need a reboot to fix
            # a loose connector.
            self.get_logger().warning(f"camera_hw: {why}; will retry while it stays unplugged")

    def _on_sources(self, msg) -> None:
        active = msg.camera == _BACKEND_HARDWARE
        if active != self._active:
            self._active = active
            self.get_logger().info(f"camera_hw: {'taking' if active else 'releasing'} the camera")
            if not active:
                # Actually let go of the device: the browser may be about to be
                # the camera, and on Linux a V4L2 node held open by one process
                # is a node nothing else can have.
                self._camera.release()

    def _on_timer(self) -> None:
        if not self._active:
            return
        frame = self._camera.read()
        if frame is None:
            if not self._complained and self._camera.error:
                self.get_logger().warning(f"camera_hw: {self._camera.error}")
                self._complained = True
            return
        if self._complained:
            self.get_logger().info(f"camera_hw: recovered on {self._camera.device_path}")
            self._complained = False

        msg = self._bridge.cv2_to_imgmsg(frame, encoding="bgr8")
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "camera"
        self._pub.publish(msg)

    def destroy_node(self) -> None:
        self._camera.release()
        super().destroy_node()


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start camera backend: {why}")
        return 1

    import rclpy

    rclpy.init(args=argv)
    node = CameraHwNode()
    import signal

    from rclpy.executors import ExternalShutdownException

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        # A second SIGINT (the process group's, then launch's) must not stop the
        # camera from being released; see neo_motion/nodes/head_behavior.py.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
