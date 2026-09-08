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

from .bridge.types import PerceptionView, TrackView

log = logging.getLogger(__name__)

try:
    from neo_perception.pipeline import AsyncPerception, PerceptionPipeline, PipelineConfig
    from neo_perception.types import GestureKind

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

    def gaze_axes(self) -> tuple[float, float]:
        """Pan/tilt rates in the same [-1, 1] convention as the joystick."""
        if self.runner is None:
            return (0.0, 0.0)
        command = self.runner.command
        return (command.pan, command.tilt)

    def release(self, reason: str = "panel override") -> None:
        if self.runner is not None:
            self.runner.pipeline.release(reason)

    def reset(self) -> None:
        if self.runner is not None:
            self.runner.pipeline.reset()

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
            )
            for t in result.tracks
        ]

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
            tracks=tracks,
            aim_x=result.attention.x,
            aim_y=result.attention.y,
        )

    @staticmethod
    def unavailable_reason() -> str:
        if not PERCEPTION_AVAILABLE:
            return "neo_perception not on the path"
        if not DECODE_AVAILABLE:
            return "opencv/numpy not installed"
        return ""
