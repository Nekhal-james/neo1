from __future__ import annotations

from pathlib import Path

import pytest

cryptography = pytest.importorskip("cryptography")

from model_conn import certs  # noqa: E402


def test_generate_ca_creates_cert_and_key(tmp_path: Path):
    cert_path, key_path = tmp_path / "ca-cert.pem", tmp_path / "ca-key.pem"
    certs.generate_ca(cert_path, key_path)
    assert cert_path.exists()
    assert key_path.exists()


def test_generate_ca_is_idempotent(tmp_path: Path):
    cert_path, key_path = tmp_path / "ca-cert.pem", tmp_path / "ca-key.pem"
    certs.generate_ca(cert_path, key_path)
    first = cert_path.read_bytes()
    certs.generate_ca(cert_path, key_path)
    assert cert_path.read_bytes() == first  # not regenerated


def test_ca_cert_is_a_ca(tmp_path: Path):
    from cryptography import x509

    cert_path, key_path = tmp_path / "ca-cert.pem", tmp_path / "ca-key.pem"
    certs.generate_ca(cert_path, key_path)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is True


def _make_ca(tmp_path: Path) -> tuple[Path, Path]:
    ca_cert, ca_key = tmp_path / "ca-cert.pem", tmp_path / "ca-key.pem"
    certs.generate_ca(ca_cert, ca_key)
    return ca_cert, ca_key


def test_server_cert_signed_by_ca_and_has_sans(tmp_path: Path):
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID

    ca_cert, ca_key = _make_ca(tmp_path)
    cert_path, key_path = tmp_path / "server-cert.pem", tmp_path / "server-key.pem"

    certs.generate_server_cert(
        ca_cert_path=ca_cert,
        ca_key_path=ca_key,
        cert_path=cert_path,
        key_path=key_path,
        dns_names=["neo-brain.local", "localhost"],
        ip_addresses=["192.168.1.50", "127.0.0.1"],
    )

    ca = x509.load_pem_x509_certificate(ca_cert.read_bytes())
    leaf = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert leaf.issuer == ca.subject

    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku

    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns_names = san.get_values_for_type(x509.DNSName)
    assert "neo-brain.local" in dns_names
    assert "localhost" in dns_names


def test_client_cert_signed_by_ca_has_client_auth_eku(tmp_path: Path):
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID

    ca_cert, ca_key = _make_ca(tmp_path)
    cert_path, key_path = tmp_path / "client-cert.pem", tmp_path / "client-key.pem"

    certs.generate_client_cert(
        ca_cert_path=ca_cert, ca_key_path=ca_key, cert_path=cert_path, key_path=key_path
    )

    ca = x509.load_pem_x509_certificate(ca_cert.read_bytes())
    leaf = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert leaf.issuer == ca.subject

    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.CLIENT_AUTH in eku


def test_leaf_certs_chain_via_openssl_semantics(tmp_path: Path):
    """The bug this guards: certs without AuthorityKeyIdentifier/
    SubjectKeyIdentifier fail strict chain validation even though they're
    signed correctly -- see test_tls_proxy.py for the real handshake proof."""
    from cryptography import x509

    ca_cert, ca_key = _make_ca(tmp_path)
    cert_path, key_path = tmp_path / "client-cert.pem", tmp_path / "client-key.pem"
    certs.generate_client_cert(
        ca_cert_path=ca_cert, ca_key_path=ca_key, cert_path=cert_path, key_path=key_path
    )

    ca = x509.load_pem_x509_certificate(ca_cert.read_bytes())
    leaf = x509.load_pem_x509_certificate(cert_path.read_bytes())

    ca_ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    leaf_aki = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    assert leaf_aki.key_identifier == ca_ski.key_identifier
