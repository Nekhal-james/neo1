"""mTLS enforcement points -- STUBBED for this pass.

CLAUDE.md and docs/IMPLEMENTATION_PLAN.md section 0.3.1 are explicit: an
unauthenticated off-board inference endpoint is an open proxy on the campus
network and must never run, even briefly for testing. This module exists so
every place that eventually needs a real certificate is named and importable
now, but none of the actual cert-loading/verification is implemented yet.

TODO(mTLS): replace every `warn_insecure()` call site with real client-cert
verification (host side, serving `up`) and real client-cert presentation
(receiver side, `--connection:status`/`--connection:ping`) once the private CA
from plan section 0.3.1 exists. Do not remove the warning until that lands.
"""

from __future__ import annotations

import logging

log = logging.getLogger("model_conn.tls")

_BANNER = "!" * 70


class TlsNotImplemented(RuntimeError):
    """Raised if code tries to actually use TLS material that doesn't exist yet."""


def warn_insecure(context: str) -> None:
    """Log an impossible-to-miss warning. Call once per process start, not per-request."""
    log.warning(_BANNER)
    log.warning("INSECURE: %s is running WITHOUT mTLS (model_conn.tls is stubbed)", context)
    log.warning("This is a Phase 6 TODO, not a shipped security posture.")
    log.warning(_BANNER)
    print(f"\n[model-conn] WARNING: {context} has NO mTLS. See model_conn/tls.py.\n")


def load_server_context():
    raise TlsNotImplemented("server cert loading not implemented (mTLS is stubbed)")


def load_client_context():
    raise TlsNotImplemented("client cert loading not implemented (mTLS is stubbed)")
