"""neo-audio-check's report, run from somewhere other than the repo root."""

from __future__ import annotations

import shutil

from neo_audio import config as config_module
from neo_audio.scripts import audio_check


def test_a_repo_relative_model_is_found_from_any_directory(tm_zip, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    (repo / "models" / "wakeword").mkdir(parents=True)
    shutil.copy(tm_zip, repo / "models" / "wakeword" / "neo.zip")
    cfg = repo / "audio.yaml"
    cfg.write_text("wakeword: {model_path: models/wakeword/neo.zip}\n", encoding="utf-8")

    monkeypatch.setattr(config_module, "REPO_ROOT", repo)
    monkeypatch.chdir(tmp_path)  # setup-user.sh runs the check from $HOME
    monkeypatch.setattr(audio_check, "capture_devices", lambda: [])
    monkeypatch.setattr(audio_check, "playback_devices", lambda: [])
    monkeypatch.setattr(audio_check, "_report_speech", lambda cfg: None)

    assert audio_check.main(["--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "Wake word: ok" in out, out
    assert "not found" not in out
