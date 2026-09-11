"""Authentication for the admin panel.

The panel can drive servos and rewrite campus data, so every mutating endpoint and
every WebSocket sits behind a session (plan section 0.3.1). Single admin account by
design -- this is one operator's console, not a multi-tenant service.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request, WebSocket, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import AuthConfig

SESSION_COOKIE = "neo_session"
MIN_PASSWORD_CHARS = 10
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


# -- recovery codes ----------------------------------------------------------
#
# The forgot-password path. A code is shown once, stored only as an argon2 hash,
# and works once. There is no email or SMS to fall back on -- the robot has
# neither -- so the code *is* the second factor that proves the person resetting
# is the owner, not just someone who can reach the panel on the campus network.

_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
"""No 0/O, 1/I/L: the code is read off a sticky note or a phone and typed back."""

RECOVERY_CODE_CHARS = 20
"""20 symbols from 31 is about 99 bits: unguessable, lockout or no lockout."""


def new_recovery_code() -> str:
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_CHARS))
    return "-".join(raw[i:i + 5] for i in range(0, RECOVERY_CODE_CHARS, 5))


def normalize_recovery_code(code: str) -> str:
    """Case, spaces and dashes are not part of the code."""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def hash_recovery_code(code: str) -> str:
    return _hasher.hash(normalize_recovery_code(code))


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

    def matches_current(self, password: str) -> bool:
        """Whether `password` is the current one, without counting as an attempt."""
        try:
            return _hasher.verify(self.cfg.password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            return False

    def verify_recovery(self, code: str, client_ip: str) -> bool:
        """Check a recovery code. A miss counts toward the same lockout as a
        wrong password, so the reset form is not a second way in."""
        if not self.cfg.recovery_hash:
            return False
        try:
            ok = _hasher.verify(self.cfg.recovery_hash, normalize_recovery_code(code))
        except (VerifyMismatchError, InvalidHashError):
            ok = False
        if ok:
            self.clear_failures(client_ip)
        else:
            self._record_failure(client_ip)
        return ok

    def replace_credentials(self, password_hash: str, session_secret: str,
                            recovery_hash: str | None = None) -> None:
        """Adopt a new password and session secret (and recovery code, if given).

        The new secret invalidates every session signed with the old one, so a
        password change signs out every other browser -- which is the point of
        changing it after a device is lost or a password leaks.
        """
        self.cfg.password_hash = password_hash
        self.cfg.session_secret = session_secret
        if recovery_hash is not None:
            self.cfg.recovery_hash = recovery_hash
        self._serializer = URLSafeTimedSerializer(session_secret, salt="neo-admin-session")

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
