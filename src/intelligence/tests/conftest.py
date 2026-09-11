from __future__ import annotations

import pytest

from intelligence import asr, tts
from intelligence.config import Config
from model_conn.config import Config as ModelConnConfig
from model_conn.config import Endpoint


@pytest.fixture
def cfg(tmp_path) -> Config:
    cfg = Config()
    # Never the repo's real campus data: once rooms are entered, a chat test's
    # question could start matching one and get a directory answer instead.
    cfg.rag.data_dir = str(tmp_path / "campus")
    cfg.rag.backup_dir = str(tmp_path / "campus-backups")
    cfg.status_file = str(tmp_path / "status.json")
    return cfg


@pytest.fixture
def mc_cfg() -> ModelConnConfig:
    cfg = ModelConnConfig()
    cfg.receiver.endpoints = [
        Endpoint(name="eth", host="eth-host", port=11434),
        Endpoint(name="wifi", host="wifi-host", port=11434),
    ]
    return cfg


@pytest.fixture(autouse=True)
def _clear_speech_caches():
    """ASR models and TTS voices are cached process-wide, keyed by path.

    Tests inject fake `vosk`/`piper` modules through sys.modules, so a cached
    object from one test would be served to the next one under a different fake
    -- passing for the wrong reason, or failing somewhere unrelated. Clear on
    both sides of every test.
    """
    asr.reset_cache()
    tts.reset_cache()
    yield
    asr.reset_cache()
    tts.reset_cache()
