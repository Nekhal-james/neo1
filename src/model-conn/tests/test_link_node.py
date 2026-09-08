from __future__ import annotations

import json
import sys
from pathlib import Path

from model_conn.config import Config, Endpoint
from model_conn.nodes.link_node import LinkNode, probe


def test_probe_false_when_rclpy_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "rclpy", None)
    ok, reason = probe()
    assert ok is False
    assert "rclpy" in reason


def test_probe_false_when_neo_msgs_missing(monkeypatch):
    import types

    fake_rclpy = types.ModuleType("rclpy")
    monkeypatch.setitem(sys.modules, "rclpy", fake_rclpy)
    monkeypatch.setitem(sys.modules, "neo_msgs", None)
    monkeypatch.setitem(sys.modules, "neo_msgs.msg", None)
    ok, reason = probe()
    assert ok is False
    assert "neo_msgs" in reason


def test_link_node_tick_writes_status_file(tmp_path: Path):
    cfg = Config()
    cfg.receiver.endpoints = [Endpoint(name="eth", host="127.0.0.1", port=1)]
    cfg.status_file = str(tmp_path / "status.json")

    node = LinkNode(cfg)
    node.tick()

    raw = json.loads((tmp_path / "status.json").read_text())
    assert raw["link"]["up"] is False
    assert raw["link"]["active_path"] == "none"
