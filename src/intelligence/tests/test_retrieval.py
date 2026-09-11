"""Vectorless retrieval: what a question matches, and what Neo says about it."""

from __future__ import annotations

import pytest

from intelligence import chat
from intelligence.campus import SAMPLES, save_text, validate
from intelligence.retrieval import (
    CONTEXT_HEADER,
    context,
    floor_words,
    normalize,
    offline_reply,
    retrieve,
)

ROOMS = SAMPLES["rooms"] + """
- code: CS-240
  name: Networks Lab
  type: lab
  block: B
  floor: 2
  aliases: ["lab 2 annex"]

- code: WR-A0
  name: Ground Floor Washroom
  type: facility
  block: A
  floor: 0
  aliases: ["washroom", "toilet"]

- code: WR-A1
  name: First Floor Washroom
  type: facility
  block: A
  floor: 1
  aliases: ["washroom", "toilet"]
"""


@pytest.fixture
def ds():
    v = validate({"rooms": ROOMS, "graph": SAMPLES["graph"], "coverage": SAMPLES["coverage"]})
    assert v.ok, v.errors
    return v.dataset


@pytest.mark.parametrize("spoken", [
    "where is CS-204", "where is cs204", "where is cs 204", "where's C.S. 204?",
    "where is c s two oh four", "where is c s two zero four",
    "where is cs two hundred four", "where is c s two hundred and four",
])
def test_every_way_of_saying_a_code_meets_in_one_place(spoken):
    assert "cs 204" in normalize(spoken)


def test_number_words_are_left_alone_where_they_are_words():
    assert normalize("which one is the library") == "which one is the library"
    assert normalize("oh where is it") == "oh where is it"
    assert normalize("room one") == "room 1"


def test_spelled_letters_join_only_in_front_of_a_number():
    assert normalize("labs in a b block") == "labs in a b block"


@pytest.mark.parametrize("question, code, matched", [
    ("where is CS-204", "CS-204", "code"),
    ("how do I get to the programming lab", "CS-204", "alias"),
    ("where's the central library", "LIB", "name"),
    ("is the library open", "LIB", "alias"),
])
def test_questions_find_their_room(ds, question, code, matched):
    r = retrieve(question, ds)
    assert [h.room.code for h in r.hits] == [code]
    assert r.hits[0].matched == matched


def test_a_longer_match_swallows_a_shorter_one_inside_it(ds):
    """"cs lab 2" is CS-204's alias; nothing else should ride in on "lab 2"."""
    r = retrieve("where is cs lab 2", ds)
    assert [h.room.code for h in r.hits] == ["CS-204"]


def test_a_shared_alias_is_ambiguous_and_neo_asks(ds):
    r = retrieve("where is the washroom", ds)
    assert r.ambiguous
    assert {h.room.code for h in r.hits} == {"WR-A0", "WR-A1"}
    assert offline_reply(ds, r) == "Did you mean WR-A0 in A block, or WR-A1 in A block?"
    assert "ask which one" in context(ds, r)


def test_block_listing_filters_by_type(ds):
    r = retrieve("which labs are in B block", ds)
    assert r.hits == []
    assert [room.code for room in r.listing] == ["CS-204", "CS-240"]
    assert offline_reply(ds, r) == "In B block I know of Computer Science Lab 2, Networks Lab."


def test_an_unsurveyed_block_is_named_honestly(ds):
    r = retrieve("what is in C block", ds)
    assert offline_reply(ds, r) == "I haven't learned C block yet."


def test_an_unknown_code_is_not_invented(ds):
    r = retrieve("where is ME-310", ds)
    assert r.hits == [] and r.unknown_codes == ["ME-310"]
    assert offline_reply(ds, r) == "I don't have ME-310 in my directory yet."
    assert "Not in the directory: ME-310." in context(ds, r)


def test_a_number_after_an_ordinary_word_is_not_a_code(ds):
    r = retrieve("give me 10 minutes to think", ds)
    assert r.unknown_codes == []
    assert offline_reply(ds, r) is None


def test_a_general_question_gets_no_directory_answer(ds):
    r = retrieve("tell me a joke about physics", ds)
    assert offline_reply(ds, r) is None
    assert "No directory entry matched this question." in context(ds, r)


def test_the_offline_answer_reads_like_a_person(ds):
    r = retrieve("where is CS-204", ds)
    assert offline_reply(ds, r) == (
        "CS-204, Computer Science Lab 2, is in B block, on the second floor, East wing, "
        "opposite the lift. From here, go out through the back door and cross the "
        "courtyard to B block, then take the stairs up two floors and turn right."
    )


def test_hand_written_directions_win_over_the_graph(ds):
    reply = offline_reply(ds, retrieve("where is the library", ds))
    assert reply == (
        "The Central Library is in A block, on the first floor, above the main entrance. "
        "Take the stairs on your left up one floor; the library is straight ahead."
    )


def test_context_holds_only_what_matched(ds):
    text = context(ds, retrieve("where is CS-204", ds))
    assert text.startswith(CONTEXT_HEADER)
    assert "CS-204: Computer Science Lab 2 (lab)" in text
    assert "Open 9 am to 5 pm" in text
    assert "Central Library" not in text
    assert "C block: not surveyed yet" in text


def test_an_empty_directory_says_so():
    from intelligence.campus import Dataset

    ds = Dataset()
    text = context(ds, retrieve("where is CS-204", ds))
    assert "empty" in text
    assert offline_reply(ds, retrieve("where is CS-204", ds)) is None


def test_floor_words():
    assert [floor_words(f) for f in (-1, 0, 2, 14)] == [
        "basement", "ground floor", "second floor", "floor 14",
    ]


# -- through chat.ask ------------------------------------------------------


def _unreachable(host, port, timeout_s):
    raise ConnectionError("down")


def test_chat_sends_the_matched_entries_to_the_model(cfg, mc_cfg):
    save_text(cfg.rag_data_path, "rooms", SAMPLES["rooms"], cfg.rag_backup_path)
    seen = {}

    def transport(host, port, model, message, timeout_s, system):
        seen["system"], seen["message"] = system, message
        return "It's in B block."

    result = chat.ask("where is cs two oh four", cfg=cfg, mc_cfg=mc_cfg,
                      transport=transport, probe_transport=lambda host, port, timeout_s: 3.0, vision="")
    assert result.source == "ollama"
    assert "CS-204: Computer Science Lab 2" in seen["system"]
    assert seen["message"] == "where is cs two oh four"


def test_with_no_model_host_the_directory_still_answers(cfg, mc_cfg):
    save_text(cfg.rag_data_path, "rooms", SAMPLES["rooms"], cfg.rag_backup_path)
    result = chat.ask("where is the library", cfg=cfg, mc_cfg=mc_cfg,
                      probe_transport=_unreachable, vision="")
    assert result.source == "kb"
    assert result.reply.startswith("The Central Library is in A block")


def test_with_no_model_host_a_general_question_gets_the_degraded_reply(cfg, mc_cfg):
    save_text(cfg.rag_data_path, "rooms", SAMPLES["rooms"], cfg.rag_backup_path)
    result = chat.ask("what is the capital of France", cfg=cfg, mc_cfg=mc_cfg,
                      probe_transport=_unreachable, vision="")
    assert result.source == "degraded"
    assert result.reply == chat.DEGRADED_REPLY


def test_a_broken_data_file_never_costs_the_answer(cfg, mc_cfg):
    cfg.rag_data_path.mkdir(parents=True)
    (cfg.rag_data_path / "rooms.yaml").write_text("- code: [unclosed\n", encoding="utf-8")
    result = chat.ask("where is the library", cfg=cfg, mc_cfg=mc_cfg,
                      probe_transport=_unreachable, vision="")
    assert result.source == "degraded"
