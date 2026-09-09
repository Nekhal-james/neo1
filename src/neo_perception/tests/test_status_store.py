"""The vision status file: how the panel's camera reaches the other processes.

`neo --webapp up`, `neo --model ... up` and `neo --prompt` are separate processes
started in any order, and any may be absent. Every test here is really about that
asymmetry -- a reader must cope with a file that is missing, stale, or corrupt,
because those are all normal.
"""

from __future__ import annotations

import json
import time

import pytest

from neo_perception.status_store import (
    STALE_AFTER_S,
    read_status,
    write_status,
)


@pytest.fixture
def status_path(tmp_path):
    return tmp_path / "vision" / "status.json"


def write_sample(path, **overrides):
    payload = dict(
        available=True,
        running=True,
        detector="ultralytics",
        state="engaged",
        engaged=True,
        target_id=3,
        target_facing="facing",
        person_count=2,
        identify_best="cell phone",
        identify_guesses=[{"label": "cell phone", "confidence": 0.8, "prominence": 0.4}],
    )
    payload.update(overrides)
    write_status(path=path, **payload)


class TestRoundTrip:
    def test_write_then_read(self, status_path):
        write_sample(status_path)
        vision = read_status(status_path)

        assert vision.available is True
        assert vision.fresh is True
        assert vision.person_count == 2
        assert vision.engaged is True
        assert vision.target_facing == "facing"
        assert vision.last_object == "cell phone"

    def test_the_directory_is_created(self, status_path):
        assert not status_path.parent.exists()
        write_sample(status_path)
        assert status_path.exists()

    def test_writes_are_atomic(self, status_path):
        """A reader must never see a half-written file."""
        for i in range(20):
            write_sample(status_path, person_count=i)
            assert json.loads(status_path.read_text(encoding="utf-8"))
        assert not list(status_path.parent.glob(".status-*.tmp"))


class TestDegradedReads:
    def test_missing_file_is_not_an_error(self, tmp_path):
        vision = read_status(tmp_path / "nope.json")
        assert vision.fresh is False
        assert vision.available is False
        assert vision.describe() == ""

    def test_corrupt_file_is_not_an_error(self, status_path):
        status_path.parent.mkdir(parents=True)
        status_path.write_text("{not json", encoding="utf-8")
        assert read_status(status_path).fresh is False

    def test_a_stale_file_is_not_fresh(self, status_path):
        """The panel exited: the last thing it saw is not what is there now."""
        write_sample(status_path)
        later = time.time() + STALE_AFTER_S + 1
        vision = read_status(status_path, now=later)

        assert vision.fresh is False
        assert vision.describe() == "", "stale state must not reach a prompt"

    def test_freshness_and_availability_are_different_answers(self, status_path):
        """Panel up but no detector, versus panel not running at all."""
        write_sample(status_path, available=False)
        vision = read_status(status_path)
        assert vision.fresh is True and vision.available is False

    def test_a_write_to_an_impossible_path_does_not_raise(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        write_status(available=True, path=blocker / "sub" / "status.json")


class TestDescribe:
    """The line that gets appended to a prompt, so wording matters."""

    def test_nobody_in_view(self, status_path):
        write_sample(status_path, person_count=0, engaged=False, identify_best="")
        assert read_status(status_path).describe() == "nobody in view"

    def test_one_person_is_singular(self, status_path):
        write_sample(status_path, person_count=1, engaged=False, identify_best="")
        assert read_status(status_path).describe() == "1 person in view"

    def test_several_people_is_plural(self, status_path):
        write_sample(status_path, person_count=3, engaged=False, identify_best="")
        assert read_status(status_path).describe() == "3 people in view"

    def test_engagement_is_mentioned(self, status_path):
        write_sample(status_path, person_count=1, engaged=True, identify_best="")
        assert "engaged" in read_status(status_path).describe()

    def test_the_identified_object_is_included(self, status_path):
        """This is what makes "what am I holding" answerable at all."""
        write_sample(status_path, person_count=1, identify_best="cell phone")
        assert "cell phone" in read_status(status_path).describe()

    def test_unavailable_perception_describes_nothing(self, status_path):
        write_sample(status_path, available=False)
        assert read_status(status_path).describe() == ""
