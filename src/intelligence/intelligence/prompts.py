"""Load the system prompt from a file, not a string literal.

docs/IMPLEMENTATION_PLAN.md Phase 6 step 4: "Prompt scaffolding in versioned
files under neo_dialog/prompts/, not string literals". This is that file, one
level up from a ROS package name that doesn't exist yet.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config

PACKAGED_PROMPT = Path(__file__).resolve().parent / "prompts" / "system_prompt.txt"


def load_system_prompt(cfg: Config) -> str:
    """cfg.prompts.system_prompt_path overrides the packaged placeholder."""
    path = Path(cfg.prompts.system_prompt_path) if cfg.prompts.system_prompt_path else PACKAGED_PROMPT
    return path.read_text(encoding="utf-8").strip()
