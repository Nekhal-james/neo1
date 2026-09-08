"""ROS 2 node wrapping the perception pipeline.

The node is deliberately thin: it converts messages, runs the pipeline, and
publishes. Every decision -- tracking, gestures, engagement, gaze -- lives in the
pure-Python core, where it is tested without ROS, without a camera, and without a
robot.

Activates once Phase 1 lands `neo_msgs`. Until then the core is exercised through
the admin panel, which drives the identical pipeline.
"""

from __future__ import annotations

import logging

from ..pipeline import AsyncPerception, PerceptionPipeline, PipelineConfig

log = logging.getLogger(__name__)


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


class PerceptionNode:
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 1.2).

    subscribe  /camera/image_raw          sensor_msgs/Image
    publish    /perception/detections     vision_msgs/Detection2DArray
               /perception/attention      neo_msgs/AttentionTarget
               /perception/gestures       neo_msgs/GestureEvent
    service    /perception/release        std_srvs/Trigger

    Two things this node must preserve, both already handled by `AsyncPerception`:

    * **Latest-wins input.** Subscribe with depth 1 and BEST_EFFORT, and hand the
      frame straight to `submit()`. Never let images queue -- a backlog makes the
      head chase where somebody used to be.
    * **Inference off the executor thread.** The pipeline owns its worker; the
      node's callback must return immediately.

    Gaze is published as an attention target, *not* as a servo command. The head
    arbiter in `neo_motion` decides whether to act on it, so an operator on the
    joystick always outranks the robot's own idea of where to look.
    """

    def __init__(self, config: PipelineConfig | None = None) -> None:
        ok, why = probe()
        if not ok:
            raise RuntimeError(f"ROS perception node unavailable: {why}")
        self.runner = AsyncPerception(PerceptionPipeline(config or PipelineConfig()))
        raise NotImplementedError("phase-1: bind publishers/subscriptions to neo_msgs")


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start perception node: {why}")
        print("The pipeline can be run without ROS via the admin panel, or")
        print("benchmarked with: python -m neo_perception.scripts.bench")
        return 1
    raise NotImplementedError("phase-1: bind publishers/subscriptions to neo_msgs")
