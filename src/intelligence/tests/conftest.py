from __future__ import annotations

import pytest

from intelligence.config import Config
from model_conn.config import Config as ModelConnConfig
from model_conn.config import Endpoint


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def mc_cfg() -> ModelConnConfig:
    cfg = ModelConnConfig()
    cfg.receiver.endpoints = [
        Endpoint(name="eth", host="eth-host", port=11434),
        Endpoint(name="wifi", host="wifi-host", port=11434),
    ]
    return cfg
