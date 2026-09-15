"""Which questions are about something in front of the camera."""

from __future__ import annotations

import pytest

from intelligence.intents import describe_object, no_object_seen, wants_object_identification


@pytest.mark.parametrize(
    "question",
    [
        "what is this",
        "What's this?",
        "what is that thing",
        "what are these",
        "hey, what is this",
        "um what am i holding",
        "okay so what's that",
        "what do you see",
        "identify this object",
        "can you tell me what this is",
        "look at this",
    ],
)
def test_questions_about_the_thing_being_held_up(question):
    assert wants_object_identification(question)


@pytest.mark.parametrize(
    "question",
    [
        "what time is this class",
        "what block is this room in",
        "what is this place",
        "what is this college's phone number",
        "where is CS-204",
        "what is the library",
        "who is this",
        "what are these labs for",
        "",
        "   ",
    ],
)
def test_campus_questions_that_merely_contain_what_and_this(question):
    assert not wants_object_identification(question)


def test_answers_are_hedged_and_use_the_right_article():
    assert describe_object("bottle") == "That looks like a bottle."
    assert describe_object("apple") == "That looks like an apple."
    assert "not sure" in describe_object("")
    assert "camera" in no_object_seen()
