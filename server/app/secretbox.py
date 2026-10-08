"""Secrets the server has to use again later, kept encrypted at rest.

A Microsoft 365 client secret is not something to hash: the poller needs it
back to sign in. It is sealed with AES-256-GCM under a key of this server's
own -- ``RMM_SECRET_KEY`` in the environment (base64 of 32 bytes, or any
passphrase), or else a key file next to the database, made on first use and
readable by the server only. A copy of the database on its own then holds no
usable secret.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import database

_PREFIX = "v1:"
_key: bytes | None = None


def _key_file() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(database.DB_PATH)), "secret.key")


def _load_key() -> bytes:
    global _key
    if _key is not None:
        return _key
    raw = (os.environ.get("RMM_SECRET_KEY") or "").strip()
    if raw:
        try:
            decoded = base64.b64decode(raw, validate=True)
            _key = decoded if len(decoded) == 32 else hashlib.sha256(raw.encode()).digest()
        except ValueError:
            _key = hashlib.sha256(raw.encode()).digest()
        return _key
    path = _key_file()
    if os.path.exists(path):
        with open(path, "rb") as fh:
            _key = base64.b64decode(fh.read().strip())
        return _key
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fresh = secrets.token_bytes(32)
    # Created readable by the owner only, before anything is written to it.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.b64encode(fresh))
    _key = fresh
    return _key


def seal(plaintext: str) -> str:
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(_load_key()).encrypt(nonce, plaintext.encode(), b"rmm-secret")
    return _PREFIX + base64.b64encode(nonce + sealed).decode()


def unseal(text: str) -> str:
    if not text.startswith(_PREFIX):
        raise ValueError("not a sealed secret")
    blob = base64.b64decode(text[len(_PREFIX):])
    return AESGCM(_load_key()).decrypt(blob[:12], blob[12:], b"rmm-secret").decode()
