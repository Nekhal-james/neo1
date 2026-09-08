from __future__ import annotations

import sys

from model_conn.__main__ import main


def test_prompt_dispatches_to_intelligence(monkeypatch, capsys):
    calls = []

    def fake_run_prompt(text, *, mc_cfg):
        calls.append((text, mc_cfg))
        print("B block.")
        return 0

    import intelligence.cli

    monkeypatch.setattr(intelligence.cli, "run_prompt", fake_run_prompt)

    rc = main(["--prompt", "where is CS-204"])
    assert rc == 0
    assert calls[0][0] == "where is CS-204"
    assert "B block." in capsys.readouterr().out


def test_prompt_reports_clear_error_when_intelligence_not_importable(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "intelligence", None)
    monkeypatch.setitem(sys.modules, "intelligence.cli", None)

    rc = main(["--prompt", "hello"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "could not import intelligence" in out
