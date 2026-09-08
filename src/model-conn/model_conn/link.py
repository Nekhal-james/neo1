"""Probe/ping core for the receiver's link to the off-board host.

Pure Python: the only real I/O is behind the `transport` parameter, which every
public function accepts and defaults to an HTTP GET against Ollama's cheap
`/api/tags` liveness route. Tests inject a fake transport and never touch a
socket -- the same discipline neo_perception's core uses for an explicit clock.

Endpoints are tried in order (Ethernet, then Wi-Fi) per
docs/IMPLEMENTATION_PLAN.md section 0.3.2: Ethernet is deterministic and
preferred, Wi-Fi is the failover, and the Wi-Fi path may simply not exist if
the campus network enables AP isolation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Literal

from .config import Endpoint

PathName = Literal["eth", "wifi", "none"]

Transport = Callable[[str, int, float], float]
"""(host, port, timeout_s) -> elapsed_ms. Raises on failure."""


def _http_get(host: str, port: int, timeout_s: float) -> float:
    import requests

    start = time.monotonic()
    resp = requests.get(f"http://{host}:{port}/api/tags", timeout=timeout_s)
    resp.raise_for_status()
    return (time.monotonic() - start) * 1000.0


@dataclass
class ProbeResult:
    endpoint: Endpoint
    ok: bool
    rtt_ms: float | None
    error: str = ""


def probe_endpoint(
    ep: Endpoint, *, timeout_s: float, transport: Transport = _http_get
) -> ProbeResult:
    """One health check against a single endpoint."""
    try:
        rtt_ms = transport(ep.host, ep.port, timeout_s)
        return ProbeResult(endpoint=ep, ok=True, rtt_ms=rtt_ms)
    except Exception as exc:  # noqa: BLE001 -- any transport failure means "down"
        return ProbeResult(endpoint=ep, ok=False, rtt_ms=None, error=str(exc))


def probe_ordered(
    endpoints: list[Endpoint], *, timeout_s: float, transport: Transport = _http_get
) -> ProbeResult | None:
    """Try each endpoint in order; return the first success, else the last failure.

    Returns None only if `endpoints` is empty.
    """
    last: ProbeResult | None = None
    for ep in endpoints:
        result = probe_endpoint(ep, timeout_s=timeout_s, transport=transport)
        if result.ok:
            return result
        last = result
    return last


@dataclass
class LinkStatus:
    """Mirrors neo_webapp.bridge.types.LinkHealth field-for-field."""

    up: bool = False
    active_path: PathName = "none"
    rtt_ms: float | None = None
    consecutive_failures: int = 0


class LinkTracker:
    """Stateful wrapper holding consecutive_failures across repeated probes.

    Used by `--connection:status` (a single check_once call) and, later, by
    nodes/link_node.py's continuous health-probe loop -- both need the same
    failure-counting behaviour.
    """

    def __init__(
        self,
        endpoints: list[Endpoint],
        timeout_s: float,
        *,
        transport: Transport = _http_get,
    ) -> None:
        self._endpoints = endpoints
        self._timeout_s = timeout_s
        self._transport = transport
        self._consecutive_failures = 0

    def check_once(self) -> LinkStatus:
        result = probe_ordered(
            self._endpoints, timeout_s=self._timeout_s, transport=self._transport
        )
        if result is None or not result.ok:
            self._consecutive_failures += 1
            return LinkStatus(
                up=False,
                active_path="none",
                rtt_ms=None,
                consecutive_failures=self._consecutive_failures,
            )
        self._consecutive_failures = 0
        return LinkStatus(
            up=True,
            active_path=result.endpoint.name,
            rtt_ms=result.rtt_ms,
            consecutive_failures=0,
        )


@dataclass
class PingStats:
    sent: int = 0
    received: int = 0
    loss_pct: float = 0.0
    rtt_min_ms: float | None = None
    rtt_avg_ms: float | None = None
    rtt_max_ms: float | None = None
    active_path: PathName = "none"


def run_ping(
    endpoints: list[Endpoint],
    *,
    count: int,
    timeout_s: float,
    transport: Transport = _http_get,
    sleep: Callable[[float], None] = time.sleep,
    interval_s: float = 0.2,
) -> PingStats:
    """Run `count` sequential probes and aggregate latency/loss stats.

    Each sample tries endpoints in order (eth then wifi), same as a single
    status check. `active_path` reports whichever endpoint answered on the
    *last* successful sample -- good enough for a human-facing ping summary.
    """
    rtts: list[float] = []
    active_path: PathName = "none"
    received = 0

    for i in range(count):
        result = probe_ordered(endpoints, timeout_s=timeout_s, transport=transport)
        if result is not None and result.ok:
            received += 1
            assert result.rtt_ms is not None
            rtts.append(result.rtt_ms)
            active_path = result.endpoint.name
        if i < count - 1:
            sleep(interval_s)

    sent = count
    loss_pct = 0.0 if sent == 0 else (sent - received) / sent * 100.0
    return PingStats(
        sent=sent,
        received=received,
        loss_pct=loss_pct,
        rtt_min_ms=min(rtts) if rtts else None,
        rtt_avg_ms=(sum(rtts) / len(rtts)) if rtts else None,
        rtt_max_ms=max(rtts) if rtts else None,
        active_path=active_path,
    )
