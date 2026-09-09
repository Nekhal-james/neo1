"""On-demand object identification: "what is this?".

Deliberately request-driven. The object model is a second network, so running it
continuously would roughly double the per-frame cost of a pipeline that has ~4 fps
to spend -- and nobody needs the desk identified sixty times a minute.
"""

from __future__ import annotations

import pytest

from neo_perception.detector import MockDetector, ScriptedFrame
from neo_perception.pipeline import (
    PerceptionPipeline,
    PipelineConfig,
    rank_presented_objects,
)
from neo_perception.tracker import TrackerConfig
from neo_perception.types import BBox, Detection

from .conftest import FRAME_H, FRAME_W, person


def obj(label: str, cx: float, cy: float, size: float, score: float = 0.8) -> Detection:
    half = size / 2
    return Detection(
        bbox=BBox(cx - half, cy - half, cx + half, cy + half),
        score=score,
        label=label,
        class_id=hash(label) % 70 + 1,  # anything but 0 (person)
    )


class TestRanking:
    """Which detection is the thing being *held up*, versus furniture in shot."""

    def test_central_object_beats_an_edge_object_of_equal_size(self):
        held = obj("cell phone", FRAME_W / 2, FRAME_H / 2, 90)
        edge = obj("chair", 40, 40, 90)
        ranked = rank_presented_objects([held, edge], FRAME_W, FRAME_H)
        assert ranked[0].label == "cell phone"

    def test_a_held_object_beats_larger_background_furniture(self):
        """The failure mode to avoid: confidently naming the chair behind you."""
        phone = obj("cell phone", FRAME_W / 2, FRAME_H / 2, 80)
        chair = obj("chair", 70, FRAME_H - 70, 200)
        ranked = rank_presented_objects([phone, chair], FRAME_W, FRAME_H)
        assert ranked[0].label == "cell phone"

    def test_bigger_wins_when_both_are_central(self):
        small = obj("mouse", FRAME_W / 2, FRAME_H / 2, 40)
        big = obj("laptop", FRAME_W / 2, FRAME_H / 2, 160)
        ranked = rank_presented_objects([small, big], FRAME_W, FRAME_H)
        assert ranked[0].label == "laptop"

    def test_people_are_excluded(self):
        """The questioner is not the answer."""
        ranked = rank_presented_objects(
            [person(cx=FRAME_W / 2), obj("book", FRAME_W / 2, FRAME_H / 2, 60)],
            FRAME_W,
            FRAME_H,
        )
        assert [g.label for g in ranked] == ["book"]

    def test_only_people_means_no_guess(self):
        ranked = rank_presented_objects([person(cx=320)], FRAME_W, FRAME_H)
        assert ranked == []

    def test_empty_input(self):
        assert rank_presented_objects([], FRAME_W, FRAME_H) == []

    def test_degenerate_frame_size_is_survivable(self):
        assert rank_presented_objects([obj("book", 0, 0, 10)], 0, 0) == []

    def test_results_are_ordered_by_prominence(self):
        ranked = rank_presented_objects(
            [
                obj("chair", 30, 30, 100),
                obj("book", FRAME_W / 2, FRAME_H / 2, 100),
                obj("tv", FRAME_W - 60, 60, 100),
            ],
            FRAME_W,
            FRAME_H,
        )
        assert ranked[0].label == "book"
        assert [g.prominence for g in ranked] == sorted(
            (g.prominence for g in ranked), reverse=True
        )


class TestOnDemand:
    def build(self, objects: list[Detection]):
        person_detector = MockDetector()
        object_detector = MockDetector([ScriptedFrame(objects)])
        pipeline = PerceptionPipeline(
            PipelineConfig(tracker=TrackerConfig(min_hits=1)),
            person_detector,
            object_detector=object_detector,
        )
        return pipeline, person_detector, object_detector

    def feed(self, pipeline, detector, clock, frame, detections=None):
        detector.frames = [ScriptedFrame(detections or [person(cx=320)])]
        detector.index = 0
        return pipeline.process(frame, clock.tick())

    def test_the_object_model_never_runs_unasked(self, clock, frame):
        pipeline, person_det, object_det = self.build([obj("book", 320, 240, 80)])
        for _ in range(10):
            self.feed(pipeline, person_det, clock, frame)
        assert object_det.calls == 0, "identification must be request-driven"

    def test_a_request_runs_it_exactly_once(self, clock, frame):
        pipeline, person_det, object_det = self.build([obj("book", 320, 240, 80)])
        pipeline.request_identify()

        result = self.feed(pipeline, person_det, clock, frame)
        assert object_det.calls == 1
        assert result.objects[0].label == "book"

        for _ in range(5):
            self.feed(pipeline, person_det, clock, frame)
        assert object_det.calls == 1, "one question, one inference"

    def test_the_result_persists_until_the_next_question(self, clock, frame):
        pipeline, person_det, _ = self.build([obj("book", 320, 240, 80)])
        pipeline.request_identify()
        self.feed(pipeline, person_det, clock, frame)

        result = self.feed(pipeline, person_det, clock, frame)
        assert result.objects[0].label == "book", "the answer should still be readable"

    def test_the_sequence_number_advances_per_question(self, clock, frame):
        pipeline, person_det, _ = self.build([obj("book", 320, 240, 80)])
        assert pipeline.identify_seq == 0

        pipeline.request_identify()
        self.feed(pipeline, person_det, clock, frame)
        assert pipeline.identify_seq == 1

        pipeline.request_identify()
        self.feed(pipeline, person_det, clock, frame)
        assert pipeline.identify_seq == 2

    def test_nothing_recognised_is_not_an_error(self, clock, frame):
        pipeline, person_det, _ = self.build([])
        pipeline.request_identify()
        result = self.feed(pipeline, person_det, clock, frame)
        assert result.objects == []
        assert pipeline.last_identify_error == ""

    def test_a_failing_object_model_does_not_break_the_pipeline(self, clock, frame):
        """A missing model must not take person tracking down with it."""

        class Exploding(MockDetector):
            def infer(self, frame):
                raise RuntimeError("no weights")

        person_det = MockDetector()
        pipeline = PerceptionPipeline(
            PipelineConfig(tracker=TrackerConfig(min_hits=1)),
            person_det,
            object_detector=Exploding(),
        )
        pipeline.request_identify()
        result = self.feed(pipeline, person_det, clock, frame)

        assert "no weights" in pipeline.last_identify_error
        assert result.objects == []
        assert result.attention.person_present is True, "people still tracked"

    def test_identification_does_not_disturb_engagement(self, clock, frame):
        pipeline, person_det, _ = self.build([obj("book", 320, 240, 80)])
        for _ in range(8):
            self.feed(
                pipeline, person_det, clock, frame, [person(cx=320, hand_raised=True)]
            )
        assert pipeline.last_result.attention.engaged is True

        pipeline.request_identify()
        result = self.feed(
            pipeline, person_det, clock, frame, [person(cx=320, hand_raised=True)]
        )
        assert result.attention.engaged is True
        assert result.objects[0].label == "book"
