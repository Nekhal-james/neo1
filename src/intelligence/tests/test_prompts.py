from __future__ import annotations

from intelligence.prompts import load_system_prompt
from intelligence.retrieval import CONTEXT_HEADER


def test_default_packaged_prompt_loads(cfg):
    text = load_system_prompt(cfg)
    assert text.startswith("You are Neo")
    assert "TODO" not in text


def test_the_prompt_names_the_section_retrieval_writes(cfg):
    """The prompt tells the model campus facts come only from this section.

    If the header drifted, the model would be told to use a section that never
    arrives, and would refuse every campus question it could have answered.
    """
    assert CONTEXT_HEADER in load_system_prompt(cfg)


def test_the_prompt_carries_the_rules_the_plan_requires(cfg):
    text = load_system_prompt(cfg).lower()
    assert "one to three short sentences" in text        # brevity for speech
    assert "never invent" in text                         # campus facts from context only
    assert "not surveyed" in text                         # coverage-aware misses


def test_explicit_system_prompt_path_overrides(cfg, tmp_path):
    custom = tmp_path / "custom_prompt.txt"
    custom.write_text("You are Neo, a campus receptionist.", encoding="utf-8")
    cfg.prompts.system_prompt_path = str(custom)

    text = load_system_prompt(cfg)
    assert text == "You are Neo, a campus receptionist."
