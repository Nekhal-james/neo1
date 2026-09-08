from __future__ import annotations

from pathlib import Path

from model_conn.link import LinkStatus, PingStats
from model_conn.status_store import read_status, write_status


def test_round_trip(tmp_path: Path):
    path = tmp_path / "status.json"
    link = LinkStatus(up=True, active_path="eth", rtt_ms=4.2, consecutive_failures=0)
    ping = PingStats(
        sent=5,
        received=5,
        loss_pct=0.0,
        rtt_min_ms=3.1,
        rtt_avg_ms=4.4,
        rtt_max_ms=6.0,
        active_path="eth",
    )
    write_status(link, ping, path=path)

    raw = read_status(path)
    assert raw is not None
    assert raw["link"] == {
        "up": True,
        "active_path": "eth",
        "rtt_ms": 4.2,
        "consecutive_failures": 0,
    }
    assert raw["ping"]["sent"] == 5
    assert raw["ping"]["rtt_avg_ms"] == 4.4


def test_round_trip_no_ping(tmp_path: Path):
    path = tmp_path / "status.json"
    write_status(LinkStatus(), None, path=path)
    raw = read_status(path)
    assert raw is not None
    assert raw["ping"] is None


def test_read_missing_file_returns_none(tmp_path: Path):
    assert read_status(tmp_path / "nope.json") is None


def test_read_corrupt_file_returns_none(tmp_path: Path):
    path = tmp_path / "status.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert read_status(path) is None


def test_sequential_writes_never_produce_a_torn_read(tmp_path: Path):
    path = tmp_path / "status.json"
    write_status(LinkStatus(up=True, active_path="eth"), None, path=path)
    write_status(LinkStatus(up=False, active_path="none"), None, path=path)

    raw = read_status(path)
    assert raw is not None
    # Must match exactly one of the two writes, never a mix of fields.
    assert raw["link"] == {
        "up": False,
        "active_path": "none",
        "rtt_ms": None,
        "consecutive_failures": 0,
    }
