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

import base64
import datetime
import hashlib
import ipaddress
import os
import socket


def _data_dir() -> str:
    db = os.environ.get(
        "RMM_DB_PATH",
        os.path.join(os.path.dirname(__file__), "..", "data", "rmm.db"))
    return os.path.dirname(os.path.abspath(db))


def default_cert_path() -> str:
    """The TLS cert path the server serves, mirroring ``run.py``'s resolution."""
    return os.environ.get("RMM_TLS_CERT", os.path.join(_data_dir(), "tls", "cert.pem"))


def cert_fingerprint(cert_path: str | None = None) -> str | None:
    """SHA-256 (hex, lowercase) of the server's leaf TLS certificate, DER-encoded.

    This is exactly the value an agent pins via ``RMM_SERVER_FINGERPRINT`` /
    ``server_fingerprint``: the agent hashes ``getpeercert(binary_form=True)``,
    i.e. the DER of the presented leaf cert, so we hash the same bytes here.
    Returns ``None`` if the cert can't be read (e.g. ``RMM_TLS_MODE=proxy``,
    where TLS is terminated upstream)."""
    path = cert_path or default_cert_path()
    try:
        with open(path) as f:
            pem = f.read()
        # First (leaf) cert only — that's what the agent's getpeercert() returns.
        b64 = []
        in_cert = False
        for line in pem.splitlines():
            if "BEGIN CERTIFICATE" in line:
                in_cert = True
                continue
            if "END CERTIFICATE" in line:
                break
            if in_cert:
                b64.append(line.strip())
        der = base64.b64decode("".join(b64))
        if not der:
            return None
        return hashlib.sha256(der).hexdigest()
    except Exception:
        return None


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
