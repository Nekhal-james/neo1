"""Bridge selection.

`auto` prefers the real robot and falls back to the simulation with a loud log
line, so a Pi that has lost its ROS environment does not silently look healthy.
"""

from __future__ import annotations

import logging

from .base import Bridge
from .mock import MockBridge
from .ros import RosBridge, RosUnavailable, probe
from .types import (
    BACKENDS,
    STREAMS,
    Backend,
    Result,
    RobotState,
    Stream,
)

log = logging.getLogger(__name__)

__all__ = [
    "BACKENDS",
    "STREAMS",
    "Backend",
    "Bridge",
    "MockBridge",
    "Result",
    "RobotState",
    "RosBridge",
    "RosUnavailable",
    "Stream",
    "make_bridge",
    "probe",
]


def make_bridge(backend: str = "auto", **kwargs) -> Bridge:
    if backend == "mock":
        return MockBridge(**kwargs)
    if backend == "ros":
        return RosBridge()
    if backend == "auto":
        ok, why = probe()
        if ok:
            return RosBridge()
        log.warning("ROS backend unavailable (%s); falling back to mock", why)
        return MockBridge(**kwargs)
    raise ValueError(f"unknown bridge backend: {backend!r}")
