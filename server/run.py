"""Entrypoint that launches the server with the configured TLS mode.

Environment:
  RMM_TLS_MODE   self-signed (default) | file | proxy
  RMM_HOST       bind address (default 0.0.0.0)
  RMM_PORT       bind port (default 8000)
  RMM_TLS_CERT   cert path (file/self-signed). Default: <data>/tls/cert.pem
  RMM_TLS_KEY    key path  (file/self-signed). Default: <data>/tls/key.pem
  RMM_TLS_HOSTNAME  CN/SAN for the generated self-signed cert
"""
from __future__ import annotations

import os

import uvicorn


def _data_dir() -> str:
    db = os.environ.get("RMM_DB_PATH", os.path.join(os.path.dirname(__file__), "app", "..", "data", "rmm.db"))
    return os.path.dirname(os.path.abspath(db))


def _load_db_settings() -> None:
    """Pull settings saved by the setup wizard into the environment.

    Explicit environment variables win (so container orchestration can still
    override), and the DB fills in anything the operator configured via the UI.
    """
    from app import database
    database.init_db()
    for key, value in database.get_all_settings().items():
        if value is not None and key not in os.environ:
            os.environ[key] = value


def main() -> None:
    _load_db_settings()
    mode = os.environ.get("RMM_TLS_MODE", "self-signed").lower()
    host = os.environ.get("RMM_HOST", "0.0.0.0")
    port = int(os.environ.get("RMM_PORT", "8000"))
    tls_dir = os.path.join(_data_dir(), "tls")
    cert = os.environ.get("RMM_TLS_CERT", os.path.join(tls_dir, "cert.pem"))
    key = os.environ.get("RMM_TLS_KEY", os.path.join(tls_dir, "key.pem"))

    kwargs: dict = {"host": host, "port": port}

    if mode == "proxy":
        # TLS terminated upstream; trust forwarded headers for scheme/client IP.
        kwargs.update(proxy_headers=True, forwarded_allow_ips="*")
        print(f"[tls] mode=proxy — serving HTTP on {host}:{port} behind a reverse proxy")
    elif mode in ("self-signed", "self_signed", "selfsigned"):
        from app import tls
        tls.ensure_self_signed(cert, key, os.environ.get("RMM_TLS_HOSTNAME"))
        kwargs.update(ssl_certfile=cert, ssl_keyfile=key)
        print(f"[tls] mode=self-signed — serving HTTPS on {host}:{port} (cert {cert})")
    elif mode == "file":
        if not (os.path.exists(cert) and os.path.exists(key)):
            raise SystemExit(f"[tls] mode=file but cert/key missing: {cert} / {key}")
        kwargs.update(ssl_certfile=cert, ssl_keyfile=key)
        print(f"[tls] mode=file — serving HTTPS on {host}:{port} (cert {cert})")
    else:
        raise SystemExit(f"[tls] unknown RMM_TLS_MODE={mode!r}")

    if mode != "proxy":
        from app import tls
        fp = tls.cert_fingerprint(cert)
        if fp:
            print(f"[tls] server cert SHA-256 = {fp}")
            print("[tls] pin on agents via RMM_SERVER_FINGERPRINT to harden against MITM "
                  "(also shown in Settings → GET /api/server-fingerprint)")

    uvicorn.run("app.main:app", **kwargs)


if __name__ == "__main__":
    main()
