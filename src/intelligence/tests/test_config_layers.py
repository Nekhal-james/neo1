"""The local config is merged over the committed one, not swapped for it.

This was wrong for a long time and stayed invisible, because the dataclass
defaults the committed YAML was being replaced by mostly agreed with it. The
one that did not agree cost a real debugging session: `chat.timeout_s` in
config/intelligence.yaml was inert whenever a local file existed.
"""

from __future__ import annotations

import pytest

from intelligence import config as icfg


@pytest.fixture
def layered(tmp_path, monkeypatch):
    base = tmp_path / "intelligence.yaml"
    local = tmp_path / "intelligence.local.yaml"
    monkeypatch.setattr(icfg, "DEFAULT_CONFIG", base)
    monkeypatch.setattr(icfg, "LOCAL_CONFIG", local)
    monkeypatch.delenv("NEO_INTELLIGENCE_CONFIG", raising=False)
    return base, local


def test_local_overrides_only_what_it_names(layered):
    base, local = layered
    base.write_text("chat:\n  model_name: from-base\n  timeout_s: 60.0\n")
    local.write_text('chat:\n  model_name: "from-local"\n')

    cfg = icfg.Config.load()
    assert cfg.chat.model_name == "from-local", "the local file wins where it speaks"
    assert cfg.chat.timeout_s == 60.0, (
        "and stays silent elsewhere -- naming one key must not switch off the rest"
    )


def test_the_merge_reaches_into_nested_sections(layered):
    base, local = layered
    base.write_text("asr:\n  engine: vosk\n  sample_rate: 16000\n")
    local.write_text("asr:\n  sample_rate: 48000\n")

    cfg = icfg.Config.load()
    assert cfg.asr.engine == "vosk"
    assert cfg.asr.sample_rate == 48000


def test_base_alone_still_works(layered):
    base, _ = layered
    base.write_text("chat:\n  model_name: only-base\n")
    assert icfg.Config.load().chat.model_name == "only-base"


def test_an_explicit_path_means_that_file_and_nothing_else(layered, tmp_path):
    base, local = layered
    base.write_text("chat:\n  model_name: from-base\n")
    local.write_text("chat:\n  model_name: from-local\n")
    named = tmp_path / "named.yaml"
    named.write_text("chat:\n  model_name: from-named\n")

    cfg = icfg.Config.load(named)
    assert cfg.chat.model_name == "from-named"
    assert cfg.chat.timeout_s == icfg.ChatConfig.timeout_s, (
        "nothing may be layered over a config the caller named"
    )


def test_the_env_var_also_means_exactly_one_file(layered, monkeypatch, tmp_path):
    base, local = layered
    base.write_text("chat:\n  model_name: from-base\n")
    local.write_text("chat:\n  timeout_s: 5.0\n")
    named = tmp_path / "env.yaml"
    named.write_text("chat:\n  model_name: from-env\n")
    monkeypatch.setenv("NEO_INTELLIGENCE_CONFIG", str(named))

    cfg = icfg.Config.load()
    assert cfg.chat.model_name == "from-env"
    assert cfg.chat.timeout_s == icfg.ChatConfig.timeout_s


def test_no_config_at_all_is_survivable(layered):
    cfg = icfg.Config.load()
    assert cfg.chat.model_name == ""


def test_the_loader_does_not_duplicate_the_dataclass_defaults(layered):
    """They drifted once already: the dataclass said 60 and the loader said 8."""
    base, _ = layered
    base.write_text("chat: {}\n")
    assert icfg.Config.load().chat.timeout_s == icfg.ChatConfig.timeout_s
