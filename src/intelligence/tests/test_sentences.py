"""Splitting a reply into the sentences Piper speaks one at a time.

Speaking sentence by sentence is what lets Neo start talking before the whole
reply is synthesized (plan 5.5). The splitter is conservative on purpose:
merging two sentences into one synthesis call costs a little latency and
nothing else, while splitting in the wrong place -- after "Dr." or inside
"A.P.J. Abdul Kalam" -- makes the voice pause mid-name.
"""

from __future__ import annotations

import pytest

from intelligence.tts import split_sentences


def _normalized(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Hello. How can I help?", ["Hello.", "How can I help?"]),
        ("Wait! Is that CS-204? Yes.", ["Wait!", "Is that CS-204?", "Yes."]),
        ("Room 204. Take the stairs.", ["Room 204.", "Take the stairs."]),
        ('He said "go left." Then turn right.', ['He said "go left."', "Then turn right."]),
        ("Hmm... Okay.", ["Hmm...", "Okay."]),
        ("One sentence with no final stop", ["One sentence with no final stop"]),
    ],
)
def test_it_splits_at_sentence_ends(text, expected):
    assert split_sentences(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "It is 2.5 metres past the lift.",
        "Ask Dr. Rao in the staff room.",
        "Bring an ID, e.g. your college card.",
        "It is in the A.P.J. Abdul Kalam Hall.",
        "Room No. 12 is upstairs.",
    ],
)
def test_it_does_not_split_inside_a_sentence(text):
    """Decimals, titles, initialisms and abbreviations are not sentence ends."""
    assert split_sentences(text) == [text]


def test_a_lowercase_continuation_is_not_a_new_sentence():
    assert split_sentences("Go past the lab etc. and turn left.") == [
        "Go past the lab etc. and turn left."
    ]


def test_a_single_letter_before_a_stop_is_kept_with_what_follows():
    """"Block B." would be a fine place to split, but so would the "J." in a
    name, and the two are indistinguishable here. Merging is the safe miss."""
    assert split_sentences("It is in block B. The lift is on the left.") == [
        "It is in block B. The lift is on the left."
    ]


def test_nothing_to_say_is_an_empty_list():
    assert split_sentences("") == []
    assert split_sentences("   \n\t ") == []


def test_whitespace_is_normalized():
    assert split_sentences("Hello.\n\n   How are you?") == ["Hello.", "How are you?"]


def test_a_long_sentence_is_wrapped_at_a_comma():
    """A 400-character sentence spoken as one unit would delay first audio by
    the whole sentence, which is the thing streaming exists to prevent."""
    text = ", ".join(["past the library and then the canteen"] * 12) + "."
    pieces = split_sentences(text, max_chars=120)
    assert len(pieces) > 1
    assert all(len(p) <= 120 for p in pieces)
    assert pieces[0].endswith(",")
    assert " ".join(pieces) == _normalized(text)


def test_a_long_run_with_no_commas_wraps_at_a_space():
    text = " ".join(["word"] * 100)
    pieces = split_sentences(text, max_chars=60)
    assert all(len(p) <= 60 for p in pieces)
    assert " ".join(pieces) == text


def test_an_unbreakable_token_is_still_bounded():
    text = "x" * 250
    pieces = split_sentences(text, max_chars=100)
    assert all(len(p) <= 100 for p in pieces)
    assert "".join(pieces) == text


@pytest.mark.parametrize(
    "reply",
    [
        "CS-204 is on the second floor of B block. Take the stairs next to the "
        "library, then turn left. It is the third door on your right!",
        "I can't reach my language model right now, but I can still help you "
        "find rooms and blocks.",
        "Sure. The canteen opens at 8.30 a.m. and closes at 6 p.m. Anything else?",
    ],
)
def test_no_word_is_ever_lost(reply):
    """Whatever the splitting decisions, speaking every piece must say the
    whole reply -- a dropped clause is a wrong direction."""
    assert " ".join(split_sentences(reply)) == _normalized(reply)
