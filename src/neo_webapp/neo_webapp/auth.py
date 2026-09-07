"""Authentication for the admin panel.

The panel can drive servos and rewrite campus data, so every mutating endpoint and
every WebSocket sits behind a session (plan section 0.3.1). Single admin account by
design -- this is one operator's console, not a multi-tenant service.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request, WebSocket, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import AuthConfig

SESSION_COOKIE = "neo_session"
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


@dataclass
class _Attempts:
    stamps: list[float] = field(default_factory=list)


class Authenticator:
    def __init__(self, cfg: AuthConfig) -> None:
        self.cfg = cfg
        self._serializer = URLSafeTimedSerializer(
            cfg.session_secret or "insecure-dev-secret", salt="neo-admin-session"
        )
        self._attempts: dict[str, _Attempts] = {}

    # -- login -------------------------------------------------------------

    def throttled(self, client_ip: str) -> float:
        """Seconds remaining in the lockout window, or 0 if not locked out."""
        rec = self._attempts.get(client_ip)
        if rec is None:
            return 0.0
        now = time.monotonic()
        cutoff = now - self.cfg.lockout_window_s
        rec.stamps = [s for s in rec.stamps if s > cutoff]
        if len(rec.stamps) < self.cfg.max_attempts:
            return 0.0
        return self.cfg.lockout_window_s - (now - rec.stamps[0])

    def _record_failure(self, client_ip: str) -> None:
        self._attempts.setdefault(client_ip, _Attempts()).stamps.append(
            time.monotonic()
        )

    def clear_failures(self, client_ip: str) -> None:
        self._attempts.pop(client_ip, None)

    def verify(self, username: str, password: str, client_ip: str) -> bool:
        if not self.cfg.configured:
            return False
        # Verify the hash even on a username mismatch, so response timing does not
        # reveal whether the username was right.
        ok_user = _consttime_eq(username, self.cfg.username)
        try:
            _hasher.verify(self.cfg.password_hash, password)
            ok_pass = True
        except (VerifyMismatchError, InvalidHashError):
            ok_pass = False
        if ok_user and ok_pass:
            self.clear_failures(client_ip)
            return True
        self._record_failure(client_ip)
        return False

    # -- sessions ----------------------------------------------------------

    def issue(self, username: str) -> str:
        return self._serializer.dumps({"u": username})

    def validate(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            data = self._serializer.loads(token, max_age=self.cfg.session_max_age_s)
        except (BadSignature, SignatureExpired):
            return None
        user = data.get("u")
        return user if isinstance(user, str) else None


def _consttime_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= ord(x) ^ ord(y)
    return diff == 0


# -- FastAPI dependencies --------------------------------------------------


def require_session(request: Request) -> str:
    auth: Authenticator = request.app.state.auth
    user = auth.validate(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated"
        )
    return user


async def require_ws_session(websocket: WebSocket) -> str | None:
    """Validate before accepting the handshake.

    Closing with 1008 rather than accepting-then-closing keeps an unauthenticated
    client from ever holding an open media channel.
    """
    auth: Authenticator = websocket.app.state.auth
    user = auth.validate(websocket.cookies.get(SESSION_COOKIE))
    if user is None:
        await websocket.close(code=1008, reason="not authenticated")
        return None
    return user
