"""Time-based one-time passwords (RFC 6238) using only the standard library.

Used for optional 2FA on local accounts. Compatible with Google Authenticator,
Microsoft Authenticator, 1Password, etc. (SHA1, 6 digits, 30s period).
"""
from __future__ import annotations

import base64
import hmac
import secrets
import struct
import time
from hashlib import sha1
from urllib.parse import quote

_B32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def generate_secret(length: int = 20) -> str:
    """Return a base32-encoded random secret (no padding)."""
    return base64.b32encode(secrets.token_bytes(length)).decode().rstrip("=")


def _code_at(secret: str, counter: int, digits: int = 6) -> str:
    # Restore base32 padding before decoding.
    pad = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret.upper() + pad)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify(secret: str, code: str, window: int = 1, period: int = 30) -> bool:
    """Validate a code, allowing +/- ``window`` time steps for clock drift."""
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit():
        return False
    counter = int(time.time() // period)
    for drift in range(-window, window + 1):
        if hmac.compare_digest(_code_at(secret, counter + drift), code):
            return True
    return False


def provisioning_uri(secret: str, account: str, issuer: str = "Leuffen RMM") -> str:
    """Build an otpauth:// URI for QR codes / manual entry."""
    label = quote(f"{issuer}:{account}")
    return (f"otpauth://totp/{label}?secret={secret}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30")
