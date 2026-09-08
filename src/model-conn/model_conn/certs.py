"""Private CA + server/client certificate generation for the off-board link.

CLAUDE.md / docs/IMPLEMENTATION_PLAN.md section 0.3.1: mutual TLS via a
private CA, generated once. The server cert (for the laptop) must cover both
the Ethernet address and the Wi-Fi hostname; the client cert (for the Pi) is
what the receiver presents back.

`cryptography` is lazy-imported, same discipline as intelligence's vosk/piper
imports -- this is a `dev`-extra dependency (see root setup.py), not a hard
runtime one, matching neo_webapp's own devcert script.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from pathlib import Path

CA_COMMON_NAME = "Neo private CA"
SERVER_COMMON_NAME = "neo-model-conn-host"
CLIENT_COMMON_NAME = "neo-model-conn-receiver"

_VALIDITY_DAYS = 3650  # 10 years -- this is a private, manually-rotated CA


def _require_cryptography():
    try:
        from cryptography import x509  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "cryptography not installed -- pip install -e \".[dev]\" from the repo root"
        ) from exc


def generate_ca(cert_path: Path, key_path: Path) -> None:
    """Create the root CA if it doesn't already exist. Idempotent."""
    if cert_path.exists() and key_path.exists():
        return
    _require_cryptography()
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Neo (private)"),
        ]
    )
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _load_ca(cert_path: Path, key_path: Path):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    ca_cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    ca_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    return ca_cert, ca_key


def _issue_leaf_cert(
    *,
    ca_cert_path: Path,
    ca_key_path: Path,
    cert_path: Path,
    key_path: Path,
    common_name: str,
    extended_key_usage,
    dns_names: list[str],
    ip_addresses: list[str],
) -> None:
    _require_cryptography()
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    ca_cert, ca_key = _load_ca(ca_cert_path, ca_key_path)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])

    sans: list[x509.GeneralName] = [x509.DNSName(h) for h in dict.fromkeys(dns_names)]
    sans += [x509.IPAddress(ipaddress.ip_address(i)) for i in dict.fromkeys(ip_addresses)]

    now = dt.datetime.now(dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(extended_key_usage, critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
    )
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    cert = builder.sign(ca_key, hashes.SHA256())

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def generate_server_cert(
    *,
    ca_cert_path: Path,
    ca_key_path: Path,
    cert_path: Path,
    key_path: Path,
    dns_names: list[str],
    ip_addresses: list[str],
) -> None:
    """Server cert for the laptop -- SAN must cover both the Ethernet address
    and the Wi-Fi hostname (plan 0.3.1) so either receiver.endpoints entry
    validates."""
    from cryptography.x509.oid import ExtendedKeyUsageOID
    from cryptography import x509

    _issue_leaf_cert(
        ca_cert_path=ca_cert_path,
        ca_key_path=ca_key_path,
        cert_path=cert_path,
        key_path=key_path,
        common_name=SERVER_COMMON_NAME,
        extended_key_usage=x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
        dns_names=dns_names,
        ip_addresses=ip_addresses,
    )


def generate_client_cert(
    *, ca_cert_path: Path, ca_key_path: Path, cert_path: Path, key_path: Path
) -> None:
    """Client cert for the Pi -- presented back to the server, never verified
    by hostname (the server only checks it chains to the CA)."""
    from cryptography.x509.oid import ExtendedKeyUsageOID
    from cryptography import x509

    _issue_leaf_cert(
        ca_cert_path=ca_cert_path,
        ca_key_path=ca_key_path,
        cert_path=cert_path,
        key_path=key_path,
        common_name=CLIENT_COMMON_NAME,
        extended_key_usage=x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
        dns_names=[],
        ip_addresses=[],
    )


def local_hostnames_and_ips() -> tuple[list[str], list[str]]:
    """Best-effort local names/IPs, for the server cert's SAN -- same
    approach as neo_webapp's make_dev_cert.py."""
    import socket

    hostnames = {"localhost", "neo-brain", "neo-brain.local", socket.gethostname()}
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if ":" not in addr:  # IPv4 only
                ips.add(addr)
    except OSError:
        pass
    return sorted(hostnames), sorted(ips)
