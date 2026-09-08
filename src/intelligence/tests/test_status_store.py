from __future__ import annotations

from pathlib import Path

from intelligence.status_store import read_status, write_status


def _write(path, **overrides):
    kwargs = dict(
        dialog_state="IDLE",
        last_prompt="where is CS-204",
        last_reply="B block.",
        chat_source="ollama",
        asr_engine="vosk",
        tts_engine="piper",
        path=path,
    )
    kwargs.update(overrides)
    write_status(**kwargs)


def test_round_trip(tmp_path: Path):
    path = tmp_path / "status.json"
    _write(path)

    raw = read_status(path)
    assert raw is not None
    assert raw["last_prompt"] == "where is CS-204"
    assert raw["last_reply"] == "B block."
    assert raw["chat_source"] == "ollama"
    assert raw["asr_engine"] == "vosk"
    assert raw["tts_engine"] == "piper"


def test_read_missing_file_returns_none(tmp_path: Path):
    assert read_status(tmp_path / "nope.json") is None


def test_read_corrupt_file_returns_none(tmp_path: Path):
    path = tmp_path / "status.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert read_status(path) is None


def test_sequential_writes_never_produce_a_torn_read(tmp_path: Path):
    path = tmp_path / "status.json"
    _write(path, last_prompt="first")
    _write(path, last_prompt="second")

    raw = read_status(path)
    assert raw is not None
    assert raw["last_prompt"] == "second"
