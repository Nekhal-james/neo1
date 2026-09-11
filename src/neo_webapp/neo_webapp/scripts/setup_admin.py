"""Create or reset the admin account.

Writes the argon2 hash, a session secret and a recovery-code hash into
config/webapp.local.yaml, which is gitignored. The password itself is never
stored or echoed.

The recovery code is printed once, here. It is what the sign-in page's "Forgot
password?" asks for, so an operator who never runs this again still has a way
back in from a browser.
"""

from __future__ import annotations

import getpass
import os
import sys

from ..auth import MIN_PASSWORD_CHARS, hash_password, hash_recovery_code, new_recovery_code
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

    if len(password) < MIN_PASSWORD_CHARS:
        print(f"use at least {MIN_PASSWORD_CHARS} characters", file=sys.stderr)
        return 1

    code = new_recovery_code()
    path = write_local(
        {
            "auth": {
                "username": username,
                "password_hash": hash_password(password),
                # A fresh secret on every reset: running this is how a forgotten
                # or leaked password is recovered, and sessions signed under the
                # old one should not outlive it.
                "session_secret": new_session_secret(),
                "recovery_hash": hash_recovery_code(code),
            }
        },
        Config.load().secrets_path,
    )
    print(f"admin '{username}' written to {path}")
    if path == LOCAL_CONFIG:
        print("this file holds secrets and is gitignored - keep it that way")
    print()
    print(f"Recovery code: {code}")
    print("Keep it somewhere safe, away from the robot. It resets the password from")
    print("the sign-in page (Forgot password?), works once, and is shown only now.")
    print("Restart the panel for the new password to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
