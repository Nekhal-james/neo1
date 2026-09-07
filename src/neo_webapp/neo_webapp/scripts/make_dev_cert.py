"""Mint a self-signed certificate for local development.

This exists so `getUserMedia` works today -- browsers refuse camera and microphone
access outside a secure context, so the webapp sources need HTTPS from any device
that is not the host itself (plan section 0.3.1).

This is a *development* certificate. Phase 1 replaces it with one issued by the
project's private CA, which you install once on the devices you use so the browser
stops warning.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import socket
import sys
from pathlib import Path

from ..config import REPO_ROOT


def _local_ips() -> list[str]:
    ips = {"127.0.0.1"}
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            if ":" not in addr:  # keep it to IPv4
                ips.add(addr)
    except OSError:
        pass
    return sorted(ips)


def main(argv: list[str] | None = None) -> int:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("needs cryptography: pip install cryptography", file=sys.stderr)
        return 1

    argv = sys.argv[1:] if argv is None else argv
    out_dir = Path(argv[0]) if argv else (REPO_ROOT / "certs")
    out_dir.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = out_dir / "dev-cert.pem", out_dir / "dev-key.pem"

    hostnames = ["localhost", "neo", "neo.local", socket.gethostname()]
    ips = _local_ips()

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "neo-admin-panel"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Neo (development)"),
        ]
    )
    sans: list[x509.GeneralName] = [x509.DNSName(h) for h in dict.fromkeys(hostnames)]
    sans += [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]

    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=397))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    print(f"cert: {cert_path}")
    print(f"key:  {key_path}")
    print(f"names: {', '.join(hostnames)}")
    print(f"ips:   {', '.join(ips)}")
    print("\nSelf-signed, so the browser will warn once - accept it to proceed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
