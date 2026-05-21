"""TLS certificate management for the Clear Skies API.

Provides auto-generation of a self-signed Ed25519 certificate on first start
(ADR-038: TLS by default for all connections).  Operators who supply their own
cert/key pair via --tls-cert / --tls-key bypass auto-generation entirely.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import logging
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)


def ensure_tls_cert(
    config_dir: Path,
    cert_path: Path | None = None,
    key_path: Path | None = None,
) -> tuple[Path, Path]:
    """Return (cert_path, key_path), generating a self-signed cert if needed.

    If both cert_path and key_path are provided (operator-supplied), verify
    both exist and return them — no generation.

    Otherwise look for {config_dir}/api-cert.pem + api-key.pem.  If present,
    return them.  If absent, generate a self-signed Ed25519 X.509 cert and
    write the PEM files, then return them.

    Raises:
        FileNotFoundError: operator-supplied cert or key path does not exist.
    """
    if cert_path is not None and key_path is not None:
        # Operator-supplied paths — verify they exist, then trust them.
        if not cert_path.exists():
            raise FileNotFoundError(f"TLS cert not found: {cert_path}")
        if not key_path.exists():
            raise FileNotFoundError(f"TLS key not found: {key_path}")
        return cert_path, key_path

    auto_cert = config_dir / "api-cert.pem"
    auto_key = config_dir / "api-key.pem"

    if auto_cert.exists() and auto_key.exists():
        return auto_cert, auto_key

    logger.info(
        "No TLS cert found at %s — generating self-signed Ed25519 cert (10-year validity).",
        config_dir,
    )
    _generate_self_signed(auto_cert, auto_key)
    return auto_cert, auto_key


def compute_fingerprint(cert_path: Path) -> str:
    """Return the SHA-256 fingerprint of the cert at cert_path.

    Format: ``SHA-256:AB:CD:EF:...`` (uppercase hex, colon-separated octets).
    """
    pem_data = cert_path.read_bytes()
    cert = x509.load_pem_x509_certificate(pem_data)
    der = cert.public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der).digest()
    hex_pairs = ":".join(f"{b:02X}" for b in digest)
    return f"SHA-256:{hex_pairs}"


def _generate_self_signed(cert_path: Path, key_path: Path) -> None:
    """Generate an Ed25519 self-signed cert and write PEM files."""
    private_key = Ed25519PrivateKey.generate()

    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "clearskies-api")]
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    san = x509.SubjectAlternativeName(
        [
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            x509.IPAddress(ipaddress.IPv6Address("::1")),
        ]
    )

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(san, critical=False)
        .sign(private_key, algorithm=None)
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    # Restrict key file permissions — silently ignored on Windows.
    try:
        key_path.chmod(0o600)
    except (OSError, NotImplementedError):
        pass

    logger.info("Self-signed TLS cert written to %s", cert_path)
