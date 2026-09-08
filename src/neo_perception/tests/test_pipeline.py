from __future__ import annotations

import time

import pytest

from neo_perception.detector import MockDetector, ScriptedFrame
from neo_perception.pipeline import (
    AsyncPerception,
    MockFrame,
    PerceptionPipeline,
    PipelineConfig,
)
from neo_perception.tracker import TrackerConfig

from .conftest import person


def test_frame_dimensions_reach_the_attention_target(clock, frame):
    detector = MockDetector([ScriptedFrame([person(cx=320)])])
    pipeline = PerceptionPipeline(PipelineConfig(tracker=TrackerConfig(min_hits=1)), detector)
    result = pipeline.process(frame, clock.tick())
    assert (result.width, result.height) == (frame.width, frame.height)


def test_empty_scene_is_handled(clock, frame):
    pipeline = PerceptionPipeline(PipelineConfig(), MockDetector([ScriptedFrame([])]))
    result = pipeline.process(frame, clock.tick())
    assert result.attention.person_present is False
    assert result.person_count == 0


def test_non_person_detections_are_not_tracked(clock, frame):
    """Object detections must not become gaze candidates."""
    from neo_perception.types import BBox, Detection

    chair = Detection(bbox=BBox(10, 10, 90, 90), score=0.9, label="chair", class_id=56)
    pipeline = PerceptionPipeline(
        PipelineConfig(tracker=TrackerConfig(min_hits=1)),
        MockDetector([ScriptedFrame([chair])]),
    )
    result = pipeline.process(frame, clock.tick())
    assert result.tracks == []
    assert result.attention.person_present is False


def test_inference_time_is_recorded(clock, frame):
    pipeline = PerceptionPipeline(PipelineConfig(), MockDetector([ScriptedFrame([])]))
    result = pipeline.process(frame, clock.tick())
    assert result.inference_ms >= 0.0


def test_reset_clears_all_state(clock, frame):
    detector = MockDetector()
    pipeline = PerceptionPipeline(PipelineConfig(tracker=TrackerConfig(min_hits=1)), detector)
    for _ in range(4):
        detector.frames = [ScriptedFrame([person(cx=320, hand_raised=True)])]
        detector.index = 0
        pipeline.process(frame, clock.tick())

    pipeline.reset()
    assert pipeline.tracker.tracks == []
    assert pipeline.engagement.target_id is None


class TestAsyncLatestWins:
    """The plan's explicit policy: drop frames, never queue them.

    A detection backlog makes the head chase where somebody used to be, which
    looks far more broken than a lower frame rate.
    """

    def test_submitting_faster_than_inference_drops_frames(self):
        slow = _SlowDetector(delay_s=0.05)
        runner = AsyncPerception(
            PerceptionPipeline(PipelineConfig(target_fps=1000), slow)
        )
        runner.start()
        try:
            for _ in range(40):
                runner.submit(MockFrame())
                time.sleep(0.002)
            time.sleep(0.3)
        finally:
            runner.stop()

        assert runner.dropped > 0, "a slow detector must shed frames"
        assert runner.processed < 40, "it must not have processed the whole burst"

    def test_submit_never_blocks(self):
        runner = AsyncPerception(
            PerceptionPipeline(PipelineConfig(target_fps=1000), _SlowDetector(0.05))
        )
        runner.start()
        try:
            start = time.perf_counter()
            for _ in range(20):
                runner.submit(MockFrame())
            elapsed = time.perf_counter() - start
        finally:
            runner.stop()
        assert elapsed < 0.05, f"submit blocked for {elapsed:.3f}s"

    def test_target_fps_is_respected(self):
        runner = AsyncPerception(
            PerceptionPipeline(PipelineConfig(target_fps=10), MockDetector([ScriptedFrame([])]))
        )
        runner.start()
        try:
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                runner.submit(MockFrame())
                time.sleep(0.005)
        finally:
            runner.stop()
        assert runner.processed <= 10, f"ran {runner.processed} times in 0.6 s at 10 fps"

    def test_a_failing_detector_does_not_kill_the_worker(self):
        runner = AsyncPerception(
            PerceptionPipeline(PipelineConfig(target_fps=1000), _ExplodingDetector())
        )
        runner.start()
        try:
            for _ in range(5):
                runner.submit(MockFrame())
                time.sleep(0.02)
            assert runner._thread.is_alive()
        finally:
            runner.stop()

    def test_stop_is_idempotent(self):
        runner = AsyncPerception(
            PerceptionPipeline(PipelineConfig(), MockDetector([ScriptedFrame([])]))
        )
        runner.start()
        runner.stop()
        runner.stop()


class _SlowDetector(MockDetector):
    def __init__(self, delay_s: float) -> None:
        super().__init__([ScriptedFrame([])])
        self.delay_s = delay_s

    def infer(self, frame):
        time.sleep(self.delay_s)
        return super().infer(frame)


class _ExplodingDetector(MockDetector):
    def infer(self, frame):
        raise RuntimeError("model exploded")
