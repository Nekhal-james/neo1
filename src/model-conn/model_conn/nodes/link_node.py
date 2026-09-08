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

from ..config import Config
from ..link import LinkTracker
from ..status_store import write_status

log = logging.getLogger("model_conn.nodes.link_node")


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

    TODO(phase-1): bind the actual rclpy publisher once neo_msgs/LinkHealth
    exists; this class is not instantiated by any console_script yet.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._tracker = LinkTracker(
            cfg.receiver.endpoints, cfg.receiver.probe_timeout_s
        )

    def tick(self) -> None:
        status = self._tracker.check_once()
        write_status(status, None, path=self._cfg.status_path)
        # TODO(phase-1): self._publisher.publish(to_neo_msgs(status))
