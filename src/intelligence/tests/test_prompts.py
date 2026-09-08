from __future__ import annotations

from pathlib import Path

from intelligence.prompts import load_system_prompt


def test_default_packaged_prompt_loads(cfg):
    text = load_system_prompt(cfg)
    assert "TODO" in text


def test_explicit_system_prompt_path_overrides(cfg, tmp_path):
    custom = tmp_path / "custom_prompt.txt"
    custom.write_text("You are Neo, a campus receptionist.", encoding="utf-8")
    cfg.prompts.system_prompt_path = str(custom)

    text = load_system_prompt(cfg)
    assert text == "You are Neo, a campus receptionist."
