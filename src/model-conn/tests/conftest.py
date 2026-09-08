"""Fake transport, so link.py's tests never touch a socket."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pytest

from model_conn.config import Endpoint


@dataclass
class FakeTransport:
    """Scripted per-endpoint-name responses: rtt_ms on success, or an exception."""

    responses: dict[str, list[float | Exception]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def __call__(self, host: str, port: int, timeout_s: float) -> float:
        # host is used as the lookup key in these tests -- see `endpoints()` below.
        self.calls.append(host)
        queue = self.responses.get(host, [])
        if not queue:
            raise RuntimeError(f"no scripted response left for {host}")
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def script(self, host: str, *results: float | Exception) -> None:
        self.responses.setdefault(host, []).extend(results)


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


def endpoints() -> list[Endpoint]:
    """host is used as the FakeTransport lookup key -- 'eth'/'wifi' as both name and host."""
    return [
        Endpoint(name="eth", host="eth", port=11434),
        Endpoint(name="wifi", host="wifi", port=11434),
    ]


@pytest.fixture
def two_endpoints() -> list[Endpoint]:
    return endpoints()


@pytest.fixture
def sleepless() -> Callable[[float], None]:
    return lambda _s: None
