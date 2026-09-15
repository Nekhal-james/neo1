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
import time

from ..pipeline import AsyncPerception, PerceptionPipeline, PipelineConfig
from ..types import EngagementState, GestureKind

log = logging.getLogger(__name__)

try:
    from rclpy.node import Node
except ImportError:  # pragma: no cover - exercised only where ROS is installed
    Node = object

PUBLISH_HZ = 15.0
"""Rate the *latest* pipeline result is republished at.

Independent of `PipelineConfig.target_fps`, which throttles inference on the
worker thread: this timer only reads whatever `AsyncPerception.result` last
produced, so it is safe to run faster than inference without doing any extra
work -- a consumer that missed one publish sees the same result again rather
than waiting a full inference period for it.
"""

_ENGAGEMENT_TO_MSG = {
    EngagementState.SCANNING: 0,
    EngagementState.ENGAGING: 1,
    EngagementState.ENGAGED: 2,
    EngagementState.SUSPENDED: 3,
}

_GESTURE_TO_MSG = {
    GestureKind.NONE: 0,
    GestureKind.RAISED_HAND: 1,
    GestureKind.OPEN_PALM: 2,
    GestureKind.WAVE: 3,
}

IDENTIFY_TIMEOUT_S = 4.0
"""How long /perception/identify waits for the object model before giving up.

Generous on purpose: the object model is a *second* network, loaded lazily on
the first question, and that first load is seconds on a Pi. Every call after it
returns in roughly one inference period. Timing out returns an honest failure
rather than a wrong guess, because the caller is about to say the answer out
loud.
"""


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


class PerceptionNode(Node):
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 1.2).

    subscribe  /camera/image_raw          sensor_msgs/Image
    publish    /perception/detections     vision_msgs/Detection2DArray
               /perception/attention      neo_msgs/AttentionTarget
               /perception/gestures       neo_msgs/GestureEvent
               /perception/objects        vision_msgs/Detection2DArray
    service    /perception/release        std_srvs/Trigger
               /perception/identify       std_srvs/Trigger

    `/perception/identify` answers "what is this?" -- one object-model pass over
    one live frame, ranked so the answer is the thing being *held up* rather
    than the largest thing in shot. It must never run continuously: that is a
    second network and roughly doubles per-frame cost on a Pi that has about
    four frames a second to spend (CLAUDE.md).

    It is a `Trigger` rather than a purpose-built service because `neo_msgs` is
    a frozen contract and a one-string answer fits `Trigger.message` exactly.
    The full ranked list goes out on /perception/objects for the panel.

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

        from cv_bridge import CvBridge
        from neo_msgs.msg import AttentionTarget, GestureEvent
        from rclpy.callback_groups import ReentrantCallbackGroup
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import Image
        from std_srvs.srv import Trigger
        from vision_msgs.msg import Detection2D, Detection2DArray, ObjectHypothesisWithPose

        super().__init__("perception_node")

        self.runner = AsyncPerception(PerceptionPipeline(config or PipelineConfig()))
        self.runner.start()
        self._bridge = CvBridge()
        self._Detection2D = Detection2D
        self._Detection2DArray = Detection2DArray
        self._ObjectHypothesisWithPose = ObjectHypothesisWithPose

        qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
        )
        self.create_subscription(Image, "/camera/image_raw", self._on_image, qos)

        self._detections_pub = self.create_publisher(
            Detection2DArray, "/perception/detections", 10
        )
        self._attention_pub = self.create_publisher(
            AttentionTarget, "/perception/attention", 10
        )
        self._gestures_pub = self.create_publisher(GestureEvent, "/perception/gestures", 10)
        self._objects_pub = self.create_publisher(
            Detection2DArray, "/perception/objects", 10
        )
        self.create_service(Trigger, "/perception/release", self._on_release)
        # Reentrant: this callback waits on the pipeline's worker thread, and on
        # the default (mutually exclusive) group that wait would also block the
        # image subscription -- so the very frame it is waiting for could never
        # arrive. A deadlock that only shows up with a real camera attached.
        self.create_service(
            Trigger,
            "/perception/identify",
            self._on_identify,
            callback_group=ReentrantCallbackGroup(),
        )

        self._timer = self.create_timer(1.0 / PUBLISH_HZ, self._on_timer)
        self.get_logger().info("perception_node: running")

    def _on_image(self, msg) -> None:
        from rclpy.time import Time

        try:
            if msg.encoding == "jpeg":
                # The browser camera, via the admin panel and the source mux: it
                # arrives JPEG-encoded, which cv_bridge does not decode.
                frame = self._decode_jpeg(msg)
            else:
                frame = self._bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8")
        except Exception:  # noqa: BLE001 - a bad frame must not kill the node
            self.get_logger().warning("could not decode /camera/image_raw frame", once=True)
            return
        if frame is None:
            return
        stamp = Time.from_msg(msg.header.stamp).nanoseconds / 1e9
        # Never blocks: AsyncPerception replaces a pending frame rather than
        # queuing, so this callback returns immediately either way.
        self.runner.submit(frame, stamp)

    @staticmethod
    def _decode_jpeg(msg):
        import cv2
        import numpy as np

        return cv2.imdecode(np.frombuffer(bytes(msg.data), dtype=np.uint8), cv2.IMREAD_COLOR)

    def _on_release(self, request, response):
        self.runner.pipeline.release(reason="operator requested")
        response.success = True
        response.message = "released"
        return response

    def _on_identify(self, request, response):
        """Run the object model once and answer with the best label.

        Waits on the pipeline's own worker rather than inferring here: doing it
        on this thread would run a second network on the executor and stall
        every publisher for its duration.
        """
        pipeline = self.runner.pipeline
        before = pipeline.identify_seq
        self.runner.request_identify()

        deadline = time.monotonic() + IDENTIFY_TIMEOUT_S
        while pipeline.identify_seq == before and time.monotonic() < deadline:
            time.sleep(0.02)

        if pipeline.identify_seq == before:
            response.success = False
            response.message = (
                "no frame to identify -- is the camera running?"
                if self.runner.processed == 0
                else "object identification timed out"
            )
            self.get_logger().warning(f"identify: {response.message}")
            return response

        if pipeline.last_identify_error:
            response.success = False
            response.message = pipeline.last_identify_error
            return response

        guesses = pipeline.last_identification
        self._publish_objects(guesses)
        if not guesses:
            # A real answer, not a failure: "I cannot tell what that is" is
            # what the caller should say, and it is true.
            response.success = True
            response.message = ""
            return response

        best = guesses[0]
        response.success = True
        response.message = best.label
        self.get_logger().info(f"identify: {best.label} ({best.confidence:.2f})")
        return response

    def _publish_objects(self, guesses) -> None:
        stamp = self.get_clock().now().to_msg()
        array = self._Detection2DArray()
        array.header.stamp = stamp
        for guess in guesses:
            det = self._Detection2D()
            det.header.stamp = stamp
            det.bbox.center.position.x = guess.bbox.cx
            det.bbox.center.position.y = guess.bbox.cy
            det.bbox.size_x = guess.bbox.width
            det.bbox.size_y = guess.bbox.height
            hyp = self._ObjectHypothesisWithPose()
            hyp.hypothesis.class_id = guess.label
            hyp.hypothesis.score = float(guess.confidence)
            det.results.append(hyp)
            array.detections.append(det)
        self._objects_pub.publish(array)

    def _on_timer(self) -> None:
        result = self.runner.result
        stamp = self.get_clock().now().to_msg()
        self._publish_detections(result, stamp)
        self._publish_attention(result, stamp)
        self._publish_gestures(result, stamp)

    def _publish_detections(self, result, stamp) -> None:
        array = self._Detection2DArray()
        array.header.stamp = stamp
        for track in result.tracks:
            det = self._Detection2D()
            det.header.stamp = stamp
            det.id = str(track.track_id)
            det.bbox.center.position.x = track.bbox.cx
            det.bbox.center.position.y = track.bbox.cy
            det.bbox.size_x = track.bbox.width
            det.bbox.size_y = track.bbox.height
            hyp = self._ObjectHypothesisWithPose()
            hyp.hypothesis.class_id = "person"
            hyp.hypothesis.score = float(track.score)
            det.results.append(hyp)
            array.detections.append(det)
        self._detections_pub.publish(array)

    def _publish_attention(self, result, stamp) -> None:
        from neo_msgs.msg import AttentionTarget

        a = result.attention
        msg = AttentionTarget()
        msg.header.stamp = stamp
        msg.x = a.x
        msg.y = a.y
        msg.confidence = a.confidence
        msg.track_id = a.track_id if a.track_id is not None else AttentionTarget.NO_TRACK
        msg.person_present = a.person_present
        msg.engaged = a.engaged
        msg.state = _ENGAGEMENT_TO_MSG.get(a.state, AttentionTarget.SCANNING)
        self._attention_pub.publish(msg)

    def _publish_gestures(self, result, stamp) -> None:
        from neo_msgs.msg import GestureEvent as GestureEventMsg

        for gesture in result.gestures:
            msg = GestureEventMsg()
            msg.header.stamp = stamp
            msg.track_id = gesture.track_id
            msg.kind = _GESTURE_TO_MSG.get(gesture.kind, GestureEventMsg.NONE)
            msg.confidence = gesture.confidence
            self._gestures_pub.publish(msg)

    def destroy_node(self) -> None:
        self.runner.stop()
        super().destroy_node()


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start perception node: {why}")
        print("The pipeline can be run without ROS via the admin panel, or")
        print("benchmarked with: python -m neo_perception.scripts.bench")
        return 1

    import rclpy
    from rclpy.executors import MultiThreadedExecutor

    rclpy.init(args=argv)
    node = PerceptionNode()
    # Multi-threaded so /perception/identify can wait for the object model
    # without the image subscription -- the source of the frame it is waiting
    # for -- being blocked behind it.
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
