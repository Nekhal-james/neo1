"""ROS 2 node wrapping the link-health core.

Deliberately thin, same discipline as neo_perception's node: every decision --
which endpoint is active, failure counting, ping aggregation -- lives in
link.py, tested without ROS. This node just drives that core on a timer and
publishes the result.

Activates once Phase 1 lands `neo_msgs`. Until then, `--connection:status` and
`--connection:ping` exercise the identical core, and status_store.py's local
JSON file is what neo_webapp reads (see neo_webapp/link_status.py and the
comment next to RosBridge's /link/health stub in bridge/ros.py).
"""

from __future__ import annotations

import logging
from typing import Callable

from ..config import Config
from ..link import LinkStatus, LinkTracker
from ..status_store import write_status

log = logging.getLogger("model_conn.nodes.link_node")

_PATH_TO_MSG = {"none": 0, "eth": 1, "wifi": 2}


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


class LinkNode:
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 1.2 / 0.3.2).

    publish   /link/health   neo_msgs/LinkHealth   every cfg.receiver.probe_interval_s

    Each tick calls LinkTracker.check_once() and both publishes the result and
    writes it to the status file, so the ROS topic and the local file never
    disagree -- neo_webapp can read either depending on what's available.

    `publish` is optional and ROS-free by construction, not gated on `probe()`:
    this class -- like link.py -- must stay directly testable with no rclpy
    installed. `main()` is what supplies a real ROS publish callback; nothing
    here imports rclpy itself.
    """

    def __init__(
        self, cfg: Config, publish: Callable[[LinkStatus], None] | None = None
    ) -> None:
        self._cfg = cfg
        self._tracker = LinkTracker(cfg.receiver.endpoints, cfg.receiver.probe_timeout_s)
        self._publish = publish

    def tick(self) -> None:
        status = self._tracker.check_once()
        write_status(status, None, path=self._cfg.status_path)
        if self._publish is not None:
            self._publish(status)


def to_ros_message(status: LinkStatus):
    """LinkStatus -> neo_msgs/LinkHealth. Raises ImportError with no neo_msgs."""
    from neo_msgs.msg import LinkHealth

    msg = LinkHealth()
    msg.up = status.up
    msg.active_path = _PATH_TO_MSG.get(status.active_path, LinkHealth.PATH_NONE)
    msg.rtt_ms = status.rtt_ms if status.rtt_ms is not None else 0.0
    msg.consecutive_failures = status.consecutive_failures
    return msg


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start link node: {why}")
        print("Use 'neo --connection:status' or 'neo --connection:ping' instead --")
        print("both drive the identical LinkTracker core with no ROS involved.")
        return 1

    import rclpy
    from neo_msgs.msg import LinkHealth
    from rclpy.node import Node

    rclpy.init(args=argv)
    ros_node = Node("link")
    cfg = Config.load()
    publisher = ros_node.create_publisher(LinkHealth, "/link/health", 10)

    def publish(status: LinkStatus) -> None:
        msg = to_ros_message(status)
        msg.header.stamp = ros_node.get_clock().now().to_msg()
        publisher.publish(msg)

    link_node = LinkNode(cfg, publish=publish)
    ros_node.create_timer(cfg.receiver.probe_interval_s, link_node.tick)
    ros_node.get_logger().info(
        f"link_node: probing every {cfg.receiver.probe_interval_s:.1f}s"
    )
    import signal

    from rclpy.executors import ExternalShutdownException

    try:
        rclpy.spin(ros_node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        # A second SIGINT (the process group's, then launch's) must not cut the
        # shutdown short; see neo_motion/nodes/servo_driver.py.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        ros_node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
