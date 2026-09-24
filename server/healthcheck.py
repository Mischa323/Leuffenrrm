"""Is the RMM answering? Run by the image's HEALTHCHECK.

It has to work however the server was set up: over HTTPS with its own
certificate (the default), over plain HTTP behind a proxy, and on a port that
may have been chosen in the setup wizard -- which stores it in the database,
not in the environment. So the port is looked up the way run.py does
(environment first, then the database), and both schemes are tried.

Read-only, and quick: it runs every few seconds and must never take a lock on
the database the server is writing to.
"""
import os
import sqlite3
import ssl
import sys
import urllib.request


def port() -> int:
    value = os.environ.get("RMM_PORT")
    if not value:
        path = os.environ.get("RMM_DB_PATH", "/data/rmm.db")
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
            row = conn.execute("SELECT value FROM settings WHERE key='RMM_PORT'").fetchone()
            conn.close()
            value = row[0] if row else None
        except sqlite3.Error:
            value = None
    try:
        return int(value or 8000)
    except ValueError:
        return 8000


def main() -> int:
    p = port()
    loose = ssl.create_default_context()
    loose.check_hostname = False
    loose.verify_mode = ssl.CERT_NONE
    for url, context in ((f"https://127.0.0.1:{p}/health", loose),
                         (f"http://127.0.0.1:{p}/health", None)):
        try:
            with urllib.request.urlopen(url, timeout=3, context=context) as res:
                if res.status == 200:
                    return 0
        except Exception:
            continue
    return 1


if __name__ == "__main__":
    sys.exit(main())
