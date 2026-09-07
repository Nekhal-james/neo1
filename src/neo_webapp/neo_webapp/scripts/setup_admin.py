"""Create or reset the admin account.

Writes the argon2 hash and a session secret into config/webapp.local.yaml, which
is gitignored. The password itself is never stored or echoed.
"""

from __future__ import annotations

import getpass
import os
import sys

from ..auth import hash_password
from ..config import LOCAL_CONFIG, Config, new_session_secret, write_local


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    username = argv[0] if argv else "admin"

    # Non-interactive path, for provisioning the Pi over SSH.
    env_pw = os.environ.get("NEO_ADMIN_PASSWORD")
    if env_pw:
        password = env_pw
    else:
        password = getpass.getpass(f"New password for '{username}': ")
        confirm = getpass.getpass("Confirm: ")
        if password != confirm:
            print("passwords do not match", file=sys.stderr)
            return 1

    if len(password) < 10:
        print("use at least 10 characters", file=sys.stderr)
        return 1

    existing = Config.load()
    secret = existing.auth.session_secret or new_session_secret()

    path = write_local(
        {
            "auth": {
                "username": username,
                "password_hash": hash_password(password),
                "session_secret": secret,
            }
        }
    )
    print(f"admin '{username}' written to {path}")
    if path == LOCAL_CONFIG:
        print("this file holds secrets and is gitignored - keep it that way")
    return 0


if __name__ == "__main__":
    sys.exit(main())
