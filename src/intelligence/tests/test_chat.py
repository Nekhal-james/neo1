from __future__ import annotations

from pathlib import Path

from intelligence.chat import ask
from model_conn.config import Endpoint


def _probe_transport(responses):
    """responses: {host: rtt_ms | Exception}"""

    def transport(host, port, timeout_s):
        result = responses.get(host)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise RuntimeError(f"no scripted response for {host}")
        return result

    return transport


def _chat_transport(reply_text, calls=None):
    def transport(host, port, model, message, timeout_s, system):
        if calls is not None:
            calls.append((host, port, model, message, system))
        return reply_text

    return transport


def test_ask_uses_healthy_eth_endpoint(cfg, mc_cfg, tmp_path):
    cfg.status_file = str(tmp_path / "status.json")
    calls = []
    result = ask(
        "where is CS-204",
        cfg=cfg,
        mc_cfg=mc_cfg,
        transport=_chat_transport("CS-204 is in B block.", calls),
        probe_transport=_probe_transport({"eth-host": 3.0}),
    )
    assert result.source == "ollama"
    assert result.reply == "CS-204 is in B block."
    assert result.host == "eth-host:11434"
    assert calls[0][0] == "eth-host"


def test_ask_falls_back_to_wifi(cfg, mc_cfg, tmp_path):
    cfg.status_file = str(tmp_path / "status.json")
    result = ask(
        "hello",
        cfg=cfg,
        mc_cfg=mc_cfg,
        transport=_chat_transport("hi"),
        probe_transport=_probe_transport({"eth-host": RuntimeError("down"), "wifi-host": 5.0}),
    )
    assert result.source == "ollama"
    assert result.host == "wifi-host:11434"


def test_ask_falls_back_to_localhost(cfg, mc_cfg, tmp_path):
    cfg.status_file = str(tmp_path / "status.json")
    result = ask(
        "hello",
        cfg=cfg,
        mc_cfg=mc_cfg,
        transport=_chat_transport("hi"),
        probe_transport=_probe_transport(
            {"eth-host": RuntimeError("down"), "wifi-host": RuntimeError("down"), "127.0.0.1": 1.0}
        ),
    )
    assert result.source == "ollama"
    assert result.host == f"127.0.0.1:{mc_cfg.host.ollama_port}"


def test_ask_degraded_when_nothing_reachable(cfg, mc_cfg, tmp_path):
    cfg.status_file = str(tmp_path / "status.json")
    result = ask(
        "hello",
        cfg=cfg,
        mc_cfg=mc_cfg,
        transport=_chat_transport("should not be called"),
        probe_transport=_probe_transport(
            {"eth-host": RuntimeError("down"), "wifi-host": RuntimeError("down"), "127.0.0.1": RuntimeError("down")}
        ),
    )
    assert result.source == "degraded"
    assert "can't reach" in result.reply


def test_ask_degraded_when_chat_call_itself_fails(cfg, mc_cfg, tmp_path):
    cfg.status_file = str(tmp_path / "status.json")

    def failing_transport(host, port, model, message, timeout_s, system):
        raise RuntimeError("model not loaded")

    result = ask(
        "hello",
        cfg=cfg,
        mc_cfg=mc_cfg,
        transport=failing_transport,
        probe_transport=_probe_transport({"eth-host": 1.0}),
    )
    assert result.source == "degraded"


def test_ask_writes_status_file(cfg, mc_cfg, tmp_path):
    status_path = tmp_path / "status.json"
    cfg.status_file = str(status_path)
    ask(
        "where is CS-204",
        cfg=cfg,
        mc_cfg=mc_cfg,
        transport=_chat_transport("B block."),
        probe_transport=_probe_transport({"eth-host": 3.0}),
    )
    import json

    raw = json.loads(status_path.read_text())
    assert raw["last_prompt"] == "where is CS-204"
    assert raw["last_reply"] == "B block."
    assert raw["chat_source"] == "ollama"
    assert raw["asr_engine"] == cfg.asr.engine
    assert raw["tts_engine"] == cfg.tts.engine
