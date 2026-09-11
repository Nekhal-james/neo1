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


# --------------------------------------------------------------------------
# Admin panel certificate (plan Phase 1, step 3)
# --------------------------------------------------------------------------


def test_panel_cert_is_signed_by_the_same_ca(tmp_path: Path):
    """The whole reason the panel cert comes from here rather than from
    neo_webapp's self-signed devcert script: one CA installed on your phone has
    to cover both the panel and the link, or one of the two installs gets
    skipped and you go back to clicking through warnings."""
    from cryptography import x509

    ca_cert, ca_key = _make_ca(tmp_path)
    cert_path, key_path = tmp_path / "panel-cert.pem", tmp_path / "panel-key.pem"

    certs.generate_panel_cert(
        ca_cert_path=ca_cert,
        ca_key_path=ca_key,
        cert_path=cert_path,
        key_path=key_path,
        dns_names=["neo.local"],
        ip_addresses=["192.168.50.2"],
    )

    ca = x509.load_pem_x509_certificate(ca_cert.read_bytes())
    panel = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert panel.issuer == ca.subject
    assert key_path.exists()


def test_panel_cert_is_a_server_cert_with_the_right_sans(tmp_path: Path):
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID

    ca_cert, ca_key = _make_ca(tmp_path)
    cert_path, key_path = tmp_path / "panel-cert.pem", tmp_path / "panel-key.pem"

    certs.generate_panel_cert(
        ca_cert_path=ca_cert,
        ca_key_path=ca_key,
        cert_path=cert_path,
        key_path=key_path,
        dns_names=["neo", "neo.local"],
        ip_addresses=["192.168.50.2"],
    )

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())

    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku

    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert set(san.get_values_for_type(x509.DNSName)) == {"neo", "neo.local"}
    assert [str(i) for i in san.get_values_for_type(x509.IPAddress)] == ["192.168.50.2"]


def test_panel_cert_is_not_a_ca(tmp_path: Path):
    """A leaf that could sign is a leaf that can impersonate anything, and this
    one lives on the machine most exposed to the campus network."""
    from cryptography import x509

    ca_cert, ca_key = _make_ca(tmp_path)
    cert_path, key_path = tmp_path / "panel-cert.pem", tmp_path / "panel-key.pem"
    certs.generate_panel_cert(
        ca_cert_path=ca_cert,
        ca_key_path=ca_key,
        cert_path=cert_path,
        key_path=key_path,
        dns_names=["neo.local"],
        ip_addresses=[],
    )
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is False


def test_panel_names_are_the_pi_not_the_laptop():
    """`local_hostnames_and_ips` names the laptop (neo-brain); the panel runs on
    the Pi (neo). Issuing the panel cert with the laptop's SANs fails much
    later, as a phone refusing to connect."""
    panel_names, _ = certs.panel_hostnames_and_ips()
    host_names, _ = certs.local_hostnames_and_ips()

    assert "neo.local" in panel_names
    assert "neo-brain.local" not in panel_names
    assert "neo-brain.local" in host_names


def test_panel_cert_stays_inside_the_browser_lifetime_limit(tmp_path: Path):
    """Apple platforms reject TLS server certificates valid for more than 398
    days. The link's certs never meet a browser and keep the 10-year lifetime;
    this one does, so it must not."""
    from cryptography import x509

    ca_cert, ca_key = _make_ca(tmp_path)
    cert_path, key_path = tmp_path / "panel-cert.pem", tmp_path / "panel-key.pem"
    certs.generate_panel_cert(
        ca_cert_path=ca_cert,
        ca_key_path=ca_key,
        cert_path=cert_path,
        key_path=key_path,
        dns_names=["neo.local"],
        ip_addresses=[],
    )

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert lifetime.days <= 398


def test_link_certs_keep_the_long_lifetime(tmp_path: Path):
    """The counterpart: no browser is involved in the Pi-to-laptop link, so
    those certs are not subject to the limit and should not be shortened into a
    yearly chore for no benefit."""
    from cryptography import x509

    ca_cert, ca_key = _make_ca(tmp_path)
    cert_path, key_path = tmp_path / "server-cert.pem", tmp_path / "server-key.pem"
    certs.generate_server_cert(
        ca_cert_path=ca_cert,
        ca_key_path=ca_key,
        cert_path=cert_path,
        key_path=key_path,
        dns_names=["neo-brain.local"],
        ip_addresses=[],
    )

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert lifetime.days > 398


def test_panel_names_include_the_pis_mdns_name(monkeypatch):
    """The robot is browsed as https://neo-pi.local. A certificate that names
    only `neo-pi` does not match that, and the phone refuses the panel."""
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "neo-pi")
    monkeypatch.setattr(certs, "_interface_ipv4s", lambda: set())
    names, _ = certs.panel_hostnames_and_ips()
    assert "neo-pi" in names
    assert "neo-pi.local" in names


def test_panel_ips_include_the_real_interface_addresses(monkeypatch):
    """On Ubuntu the hostname resolves to 127.0.1.1, which no other device
    dials. The static address on the laptop cable has to be in the SANs."""
    import socket
    import subprocess

    monkeypatch.setattr(socket, "gethostname", lambda: "neo-pi")
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("127.0.1.1", 0))]
    )

    class Completed:
        stdout = " ".join(["192.168.50.2", "10.1.2.3", "fe80::2ecf:67ff:fe66:776a", "127.0.0.1"])

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Completed())
    _, ips = certs.panel_hostnames_and_ips()
    assert "192.168.50.2" in ips
    assert "10.1.2.3" in ips
    assert not any(":" in ip for ip in ips), "IPv6 link-local is never dialled by name"


def test_an_unavailable_interface_lookup_is_not_an_error(monkeypatch):
    """Windows and macOS have no `hostname -I`; issuing a cert there must still work."""
    import subprocess

    def missing(*a, **k):
        raise FileNotFoundError("hostname")

    monkeypatch.setattr(subprocess, "run", missing)
    assert certs._interface_ipv4s() == set()
    names, ips = certs.panel_hostnames_and_ips()
    assert "neo.local" in names and "127.0.0.1" in ips
