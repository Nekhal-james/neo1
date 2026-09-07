"""The joystick is network-mediated, so a dropped connection must stop the head.

A stale stream that keeps driving is the one failure in this package that can
damage hardware or a person, so it is tested at both layers: the pure motion rule,
and the live WebSocket path.
"""

from __future__ import annotations

import time

import pytest

from neo_webapp.bridge.mock import MockBridge


@pytest.mark.asyncio
async def test_fresh_input_moves_the_head():
    bridge = MockBridge(deadman_ms=120)
    await bridge.publish_joy([1.0, 0.0], [])
    for _ in range(10):
        bridge._tick(0.02)
    assert bridge.snapshot().head.pan_deg > 0


@pytest.mark.asyncio
async def test_stale_input_stops_the_head():
    bridge = MockBridge(deadman_ms=120)
    await bridge.publish_joy([1.0, 0.0], [])
    for _ in range(5):
        bridge._tick(0.02)
    moved_to = bridge.snapshot().head.pan_deg
    assert moved_to > 0

    time.sleep(0.15)  # exceed the deadman without sending anything
    assert bridge.joy_is_stale()
    for _ in range(20):
        bridge._tick(0.02)
    assert bridge.snapshot().head.pan_deg == pytest.approx(moved_to)


@pytest.mark.asyncio
async def test_neutral_axes_stop_the_head_immediately():
    bridge = MockBridge(deadman_ms=120)
    await bridge.publish_joy([1.0, 0.0], [])
    for _ in range(5):
        bridge._tick(0.02)
    held = bridge.snapshot().head.pan_deg

    await bridge.publish_joy([0.0, 0.0], [])  # what release sends
    for _ in range(10):
        bridge._tick(0.02)
    assert bridge.snapshot().head.pan_deg == pytest.approx(held)


@pytest.mark.asyncio
async def test_estop_overrides_live_input():
    bridge = MockBridge(deadman_ms=120)
    await bridge.set_estop(True)
    await bridge.publish_joy([1.0, 1.0], [])
    for _ in range(20):
        bridge._tick(0.02)
    head = bridge.snapshot().head
    assert head.pan_deg == 0.0 and head.tilt_deg == 0.0
    assert head.active_source == "estop"


@pytest.mark.asyncio
async def test_soft_limits_are_never_exceeded():
    bridge = MockBridge(deadman_ms=5000)
    await bridge.publish_joy([1.0, 1.0], [])
    for _ in range(1000):  # drive hard into both limits
        bridge._tick(0.02)
    head = bridge.snapshot().head
    assert head.pan_deg == pytest.approx(head.pan_limit_deg[1])
    assert head.tilt_deg == pytest.approx(head.tilt_limit_deg[1])
    assert head.at_limit


def test_disconnect_publishes_neutral(auth_client, bridge):
    """Closing the socket must stop the head without waiting for the deadman."""
    with auth_client.websocket_connect("/ws/joy") as ws:
        ws.send_json({"axes": [1.0, 0.0], "buttons": []})
        time.sleep(0.05)
    time.sleep(0.05)  # shorter than the 120 ms deadman
    # The finally-block neutral has landed, so the simulated head is not driving.
    before = bridge.snapshot().head.pan_deg
    for _ in range(10):
        bridge._tick(0.02)
    assert bridge.snapshot().head.pan_deg == pytest.approx(before)
