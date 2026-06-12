"""TLS helpers: generate a self-signed certificate on first boot.

The server supports three modes (``RMM_TLS_MODE``):

* ``self-signed`` (default) — generate a cert/key here if missing and serve HTTPS.
* ``file`` — use operator-supplied cert/key (e.g. from Let's Encrypt / certbot).
* ``proxy`` — terminate TLS at a reverse proxy (Caddy/nginx/Traefik); the app
  runs HTTP and trusts ``X-Forwarded-*`` headers.

Only the self-signed path needs code; it uses ``cryptography`` (already a small,
pure-Python dependency of several of our libs).
"""
from __future__ import annotations

import datetime
import ipaddress
import os
import socket


def ensure_self_signed(cert_path: str, key_path: str, hostname: str | None = None) -> None:
    """Create a self-signed cert/key pair at the given paths if they don't exist."""
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    os.makedirs(os.path.dirname(os.path.abspath(cert_path)), exist_ok=True)
    host = hostname or socket.gethostname()

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])

    sans = [x509.DNSName(host), x509.DNSName("localhost")]
    try:
        sans.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    except ValueError:
        pass

    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    os.chmod(key_path, 0o600)
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
