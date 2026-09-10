"""Runs the perception pipeline on frames arriving from the admin panel.

Lets the whole gesture-engagement stack be exercised against a real camera --
whichever camera the panel is using -- with no Pi, no ROS, and no servos. The head
it drives is simulated, but the detector, tracker, gestures and engagement logic
are exactly the ones that will run on the robot.

Optional by construction: if `neo_perception`, OpenCV or a model is missing, the
panel keeps working and simply reports perception as unavailable.
"""

from __future__ import annotations

import logging
import time

from .bridge.types import IdentifyView, ObjectGuessView, PalmView, PerceptionView, TrackView

log = logging.getLogger(__name__)

ATTENTION_MAX_AGE_S = 1.0
"""How old a perception result may be before it stops counting.

The pipeline keeps its last result for ever once frames stop arriving, so
without this a stopped camera would leave the head locked on whoever was in the
final frame. One second is several frames even at the Pi's rate.
"""

try:
    from neo_perception.pipeline import AsyncPerception, PerceptionPipeline, PipelineConfig

    PERCEPTION_AVAILABLE = True
except ImportError as exc:  # pragma: no cover - depends on environment
    log.info("neo_perception unavailable (%s); Vision tab will be inert", exc)
    PERCEPTION_AVAILABLE = False

try:
    import cv2
    import numpy as np

    DECODE_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    DECODE_AVAILABLE = False


class PerceptionLink:
    """Adapts panel camera frames to the perception pipeline and back."""

    def __init__(self, *, target_fps: float = 6.0, detector_backend: str = "auto") -> None:
        self.available = PERCEPTION_AVAILABLE and DECODE_AVAILABLE
        self.runner = None
        self._last_gesture = "none"
        self._decode_failures = 0

        if not self.available:
            return

        config = PipelineConfig(detector_backend=detector_backend, target_fps=target_fps)
        # A laptop can afford more than the Pi's 320 working point, but keeping it
        # at 320 means what you tune here is what the robot will actually do.
        self.runner = AsyncPerception(PerceptionPipeline(config))

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self.runner is not None:
            self.runner.start()

    def stop(self) -> None:
        if self.runner is not None:
            self.runner.stop()

    # -- input -------------------------------------------------------------

    def submit_jpeg(self, jpeg: bytes) -> None:
        """Decode a panel camera frame and hand it to the worker.

        Decoding happens on the caller's thread, which is the WebSocket handler --
        cheap next to inference, and it keeps the worker's single slot holding a
        decoded frame rather than a backlog of undecoded bytes.
        """
        if self.runner is None:
            return
        try:
            buf = np.frombuffer(jpeg, dtype=np.uint8)
            frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        except Exception:
            self._decode_failures += 1
            return
        if frame is None:
            self._decode_failures += 1
            return
        self.runner.submit(frame)

    # -- output ------------------------------------------------------------

    def attention(self, max_age_s: float = ATTENTION_MAX_AGE_S):
        """The latest attention target, or None when there is nothing current.

        Deliberately not the pipeline's rate command. That command is right on
        the robot, where the camera rides the head and turning closes the loop;
        the panel's camera does not move with its simulated head, and see
        MockBridge for what integrating it did.
        """
        if self.runner is None:
            return None
        result = self.runner.result
        if result.stamp <= 0.0 or (time.monotonic() - result.stamp) > max_age_s:
            return None
        return result.attention

    def release(self, reason: str = "panel override") -> None:
        if self.runner is not None:
            self.runner.pipeline.release(reason)

    def reset(self) -> None:
        # Otherwise the panel goes on showing a gesture from before the reset.
        self._last_gesture = "none"
        if self.runner is not None:
            self.runner.pipeline.reset()

    def request_identify(self) -> int:
        """Queue an identification. Returns the seq to wait for."""
        if self.runner is None:
            return 0
        self.runner.request_identify()
        return self.runner.pipeline.identify_seq + 1

    def view(self, running: bool) -> PerceptionView:
        if self.runner is None:
            return PerceptionView(available=False)

        result = self.runner.result
        pipeline = self.runner.pipeline
        width = max(result.width, 1)
        height = max(result.height, 1)
        target_id = result.attention.track_id if result.attention.engaged else None

        gestures = {g.track_id: g.kind.value for g in result.gestures}
        if result.gestures:
            self._last_gesture = result.gestures[-1].kind.value

        tracks = [
            TrackView(
                track_id=t.track_id,
                x1=t.bbox.x1 / width,
                y1=t.bbox.y1 / height,
                x2=t.bbox.x2 / width,
                y2=t.bbox.y2 / height,
                confirmed=t.confirmed,
                engaged=t.track_id == target_id,
                gesture=gestures.get(t.track_id, "none"),
                facing=t.facing.value,
                px1=t.person_bbox.x1 / width if t.person_bbox else None,
                py1=t.person_bbox.y1 / height if t.person_bbox else None,
                px2=t.person_bbox.x2 / width if t.person_bbox else None,
                py2=t.person_bbox.y2 / height if t.person_bbox else None,
            )
            for t in result.tracks
        ]

        target_facing = next(
            (t.facing.value for t in result.tracks if t.track_id == target_id),
            "unknown",
        )
        identify = IdentifyView(
            pending=pipeline.identify_pending,
            seq=pipeline.identify_seq,
            error=pipeline.last_identify_error,
            guesses=[
                ObjectGuessView(
                    label=g.label,
                    confidence=g.confidence,
                    prominence=g.prominence,
                    x1=g.bbox.x1 / width,
                    y1=g.bbox.y1 / height,
                    x2=g.bbox.x2 / width,
                    y2=g.bbox.y2 / height,
                )
                for g in result.objects
            ],
        )

        return PerceptionView(
            available=True,
            running=running,
            detector=pipeline.detector.name,
            state=result.attention.state.value,
            engaged=result.attention.engaged,
            target_id=target_id,
            person_count=result.person_count,
            inference_ms=result.inference_ms,
            fps=1000.0 / result.inference_ms if result.inference_ms > 0 else 0.0,
            dropped_frames=result.dropped_frames,
            last_gesture=self._last_gesture,
            release_reason=pipeline.engagement.last_release_reason,
            target_facing=target_facing,
            engage_gesture=pipeline.engagement.cfg.engage_gesture.value,
            identify=identify,
            tracks=tracks,
            aim_x=result.attention.x,
            aim_y=result.attention.y,
            palm=_palm_view(result.palm_check),
            hold_progress=result.hold_progress,
        )

    @staticmethod
    def unavailable_reason() -> str:
        if not PERCEPTION_AVAILABLE:
            return "neo_perception not on the path"
        if not DECODE_AVAILABLE:
            return "opencv/numpy not installed"
        return ""


def _palm_view(check) -> PalmView | None:
    if check is None:
        return None
    return PalmView(
        track_id=check.track_id,
        verdict=check.verdict.value,
        reason=check.reason,
        arm=check.arm,
        lift=check.lift,
        forearm_tilt_deg=check.forearm_tilt_deg,
        forearm_len=check.forearm_len,
        wrist_score=check.wrist_score,
        elbow_score=check.elbow_score,
        shoulder_score=check.shoulder_score,
    )
