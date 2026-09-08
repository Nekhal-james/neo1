from __future__ import annotations

from model_conn.link import LinkTracker, probe_endpoint, probe_ordered, run_ping


def test_probe_endpoint_success(two_endpoints, transport):
    transport.script("eth", 4.2)
    result = probe_endpoint(two_endpoints[0], timeout_s=1.0, transport=transport)
    assert result.ok
    assert result.rtt_ms == 4.2
    assert result.error == ""


def test_probe_endpoint_failure(two_endpoints, transport):
    transport.script("eth", RuntimeError("connection refused"))
    result = probe_endpoint(two_endpoints[0], timeout_s=1.0, transport=transport)
    assert not result.ok
    assert result.rtt_ms is None
    assert "connection refused" in result.error


def test_probe_ordered_prefers_eth(two_endpoints, transport):
    transport.script("eth", 3.0)
    transport.script("wifi", 9.0)
    result = probe_ordered(two_endpoints, timeout_s=1.0, transport=transport)
    assert result.ok
    assert result.endpoint.name == "eth"
    assert "wifi" not in transport.calls  # never tried -- eth already succeeded


def test_probe_ordered_falls_through_to_wifi(two_endpoints, transport):
    transport.script("eth", RuntimeError("down"))
    transport.script("wifi", 9.0)
    result = probe_ordered(two_endpoints, timeout_s=1.0, transport=transport)
    assert result.ok
    assert result.endpoint.name == "wifi"


def test_probe_ordered_both_fail_returns_last_failure(two_endpoints, transport):
    transport.script("eth", RuntimeError("eth down"))
    transport.script("wifi", RuntimeError("wifi down"))
    result = probe_ordered(two_endpoints, timeout_s=1.0, transport=transport)
    assert not result.ok
    assert result.endpoint.name == "wifi"


def test_probe_ordered_empty_list_returns_none():
    assert probe_ordered([], timeout_s=1.0, transport=lambda *a: 0.0) is None


def test_link_tracker_tracks_consecutive_failures(two_endpoints, transport):
    tracker = LinkTracker(two_endpoints, timeout_s=1.0, transport=transport)

    transport.script("eth", RuntimeError("down"))
    transport.script("wifi", RuntimeError("down"))
    s1 = tracker.check_once()
    assert not s1.up
    assert s1.active_path == "none"
    assert s1.consecutive_failures == 1

    transport.script("eth", RuntimeError("down"))
    transport.script("wifi", RuntimeError("down"))
    s2 = tracker.check_once()
    assert s2.consecutive_failures == 2

    transport.script("eth", 5.0)
    s3 = tracker.check_once()
    assert s3.up
    assert s3.active_path == "eth"
    assert s3.rtt_ms == 5.0
    assert s3.consecutive_failures == 0


def test_run_ping_aggregates_stats(two_endpoints, transport, sleepless):
    # 5 samples: eth succeeds 3x (2.0/4.0/6.0ms) and fails 2x, falling through to
    # a wifi that's also down each time -> 5 sent, 3 received, 40% loss.
    transport.script("eth", 2.0, 4.0, RuntimeError("down"), RuntimeError("down"), 6.0)
    transport.script("wifi", RuntimeError("down"), RuntimeError("down"))

    stats = run_ping(
        two_endpoints, count=5, timeout_s=1.0, transport=transport, sleep=sleepless
    )
    assert stats.sent == 5
    assert stats.received == 3
    assert stats.loss_pct == 40.0
    assert stats.rtt_min_ms == 2.0
    assert stats.rtt_max_ms == 6.0
    assert stats.rtt_avg_ms == (2.0 + 4.0 + 6.0) / 3
    assert stats.active_path == "eth"


def test_run_ping_all_failures(two_endpoints, transport, sleepless):
    for _ in range(3):
        transport.script("eth", RuntimeError("down"))
        transport.script("wifi", RuntimeError("down"))
    stats = run_ping(
        two_endpoints, count=3, timeout_s=1.0, transport=transport, sleep=sleepless
    )
    assert stats.sent == 3
    assert stats.received == 0
    assert stats.loss_pct == 100.0
    assert stats.rtt_min_ms is None
    assert stats.active_path == "none"
