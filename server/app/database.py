"""SQLite storage layer for the Leuffen RMM server.

Uses the stdlib ``sqlite3`` module directly to keep dependencies light. A single
connection is shared across requests with ``check_same_thread=False``; writes are
serialised by a module-level lock since SQLite only allows one writer at a time.

The schema is multi-tenant: organizations own devices, groups, standards and
nodes. Every device/node/network query is scoped by ``org_id``.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

DB_PATH = os.environ.get(
    "RMM_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "rmm.db")
)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    enroll_key  TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS org_users (
    org_id      TEXT NOT NULL,
    user_email  TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'admin',
    PRIMARY KEY (org_id, user_email),
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS standards (
    id           TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL,
    name         TEXT NOT NULL,
    policy_json  TEXT,    -- thresholds, cadences
    baseline_json TEXT,   -- compliance baseline expectations
    alert_json   TEXT,    -- recipients, sender, enabled rules
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS groups (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL,
    name        TEXT NOT NULL,
    is_default  INTEGER NOT NULL DEFAULT 0,
    os_match    TEXT,            -- 'windows' | 'linux' | 'windows_server' | NULL
    standard_id TEXT,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS devices (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL,
    group_id      TEXT,
    hostname      TEXT NOT NULL,
    os            TEXT,
    os_version    TEXT,
    os_arch       TEXT,
    os_kind       TEXT,          -- 'windows' | 'linux' | 'windows_server'
    manufacturer  TEXT,
    model         TEXT,
    serial        TEXT,
    cpu           TEXT,
    ram_total     INTEGER,
    ip            TEXT,
    mac           TEXT,
    agent_version TEXT,
    logged_in_user TEXT,
    disks_json    TEXT,
    is_node       INTEGER NOT NULL DEFAULT 0,
    inventory_json TEXT,
    compliant     INTEGER,
    compliance_json TEXT,
    created_at    REAL NOT NULL,
    last_seen     REAL,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS metrics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    TEXT NOT NULL,
    ts           REAL NOT NULL,
    cpu_percent  REAL,
    mem_percent  REAL,
    mem_total    INTEGER,
    mem_used     INTEGER,
    disk_percent REAL,
    disk_total   INTEGER,
    disk_used    INTEGER,
    uptime       REAL,
    net_sent     INTEGER,
    net_recv     INTEGER,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_metrics_device_ts ON metrics(device_id, ts);

CREATE TABLE IF NOT EXISTS subnets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id      TEXT NOT NULL,
    cidr         TEXT NOT NULL,
    broadcast    TEXT,
    FOREIGN KEY (node_id) REFERENCES devices(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS network_hosts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id       TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    ip           TEXT NOT NULL,
    mac          TEXT,
    hostname     TEXT,
    manufacturer TEXT,
    first_seen   REAL,
    last_seen    REAL,
    online       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (node_id, ip),
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS alert_state (
    device_id    TEXT NOT NULL,
    rule         TEXT NOT NULL,
    state        TEXT NOT NULL,   -- 'ok' | 'raised'
    since        REAL,
    last_email   REAL,
    PRIMARY KEY (device_id, rule)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    username     TEXT PRIMARY KEY,
    email        TEXT,
    display_name TEXT,
    pw_hash      TEXT NOT NULL,
    is_admin     INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    last_active  REAL
);

CREATE TABLE IF NOT EXISTS recovery_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

-- Phase 2: scripts library + run history.
CREATE TABLE IF NOT EXISTS scripts (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    shell       TEXT NOT NULL DEFAULT 'shell',  -- 'shell' | 'powershell'
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS script_runs (
    id          TEXT PRIMARY KEY,
    script_id   TEXT,
    org_id      TEXT NOT NULL,
    device_id   TEXT NOT NULL,
    name        TEXT,
    status      TEXT NOT NULL,                  -- 'running' | 'ok' | 'failed'
    exit_code   INTEGER,
    output      TEXT,
    created_at  REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_device ON script_runs(device_id, created_at);

-- One-time, write-once enrolment keys (stored hashed; never shown again).
CREATE TABLE IF NOT EXISTS enroll_tokens (
    id         TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    label      TEXT,
    created_at REAL NOT NULL,
    expires_at REAL,
    used_at    REAL,
    device_id  TEXT,
    kind       TEXT NOT NULL DEFAULT 'enroll',  -- 'enroll' (user) | 'internal' (download/update)
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tokens_hash ON enroll_tokens(token_hash);

CREATE TABLE IF NOT EXISTS schedules (
    id               TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL,
    script_id        TEXT NOT NULL,
    name             TEXT,
    target_type      TEXT NOT NULL DEFAULT 'all',   -- 'device' | 'group' | 'all'
    target_id        TEXT,
    trigger          TEXT NOT NULL DEFAULT 'interval', -- 'interval' | 'daily'
    interval_minutes INTEGER,
    at_time          TEXT,                          -- 'HH:MM' (daily)
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_run         REAL,
    next_run         REAL,
    created_at       REAL NOT NULL,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY (script_id) REFERENCES scripts(id) ON DELETE CASCADE
);

-- Files attached to a script, delivered to the device's working directory.
CREATE TABLE IF NOT EXISTS script_files (
    id         TEXT PRIMARY KEY,
    script_id  TEXT NOT NULL,
    name       TEXT NOT NULL,
    content    TEXT NOT NULL,   -- base64
    size       INTEGER NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (script_id) REFERENCES scripts(id) ON DELETE CASCADE
);

-- Monitoring policies: a monitor script + optional remediation, with variables.
CREATE TABLE IF NOT EXISTS monitors (
    id               TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL,
    name             TEXT NOT NULL,
    monitor_script_id     TEXT NOT NULL,
    remediation_script_id TEXT,
    target_type      TEXT NOT NULL DEFAULT 'all',
    target_id        TEXT,
    trigger          TEXT NOT NULL DEFAULT 'interval',
    interval_minutes INTEGER,
    at_time          TEXT,
    variables_json   TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_run         REAL,
    next_run         REAL,
    last_status      TEXT,
    created_at       REAL NOT NULL,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
);
"""

# Default monitoring policy used to seed a new org's standard. Overridable per
# standard via the dashboard later.
DEFAULT_POLICY = {
    "offline_after": float(os.environ.get("ALERT_OFFLINE_AFTER", "120")),
    "cpu_pct": float(os.environ.get("ALERT_CPU_PCT", "90")),
    "cpu_minutes": float(os.environ.get("ALERT_CPU_MINUTES", "10")),
    "disk_free_pct": float(os.environ.get("ALERT_DISK_FREE_PCT", "10")),
    "mem_pct": float(os.environ.get("ALERT_MEM_PCT", "90")),
    "mem_minutes": float(os.environ.get("ALERT_MEM_MINUTES", "10")),
    "metric_interval": float(os.environ.get("RMM_INTERVAL", "30")),
}


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# Settings (key/value) — backs the first-run setup wizard so configuration can
# be entered in the UI instead of environment variables.
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: str | None = None) -> str | None:
    row = get_conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with write() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))


def get_all_settings() -> dict[str, str]:
    return {r["key"]: r["value"] for r in get_conn().execute("SELECT key, value FROM settings")}


def setup_complete() -> bool:
    return get_setting("SETUP_COMPLETE") == "1"


def clear_settings() -> None:
    """Wipe all server configuration so the first-run setup wizard reappears."""
    with write() as conn:
        conn.execute("DELETE FROM settings")


# --------------------------------------------------------------------------- #
# Local accounts (username/password) — used when auth mode is "local".
# Passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib, no extra dependency).
# --------------------------------------------------------------------------- #
def _hash_pw(password: str, salt: bytes | None = None) -> str:
    import base64
    import hashlib
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return "pbkdf2$200000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_pw(password: str, stored: str) -> bool:
    import base64
    import hashlib
    import hmac
    try:
        _, iters, salt_b64, hash_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def create_user(username: str, password: str, is_admin: bool = False,
                email: str | None = None, display_name: str | None = None) -> None:
    with write() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO users
               (username, email, display_name, pw_hash, is_admin, created_at, last_active)
               VALUES (?,?,?,?,?,?,?)""",
            (username.lower(), email, display_name or username, _hash_pw(password),
             1 if is_admin else 0, _now(), None),
        )


def get_user(username: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM users WHERE username=?", (username.lower(),)).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    if not email:
        return None
    row = get_conn().execute("SELECT * FROM users WHERE lower(email)=?", (email.lower(),)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT username, email, display_name, is_admin, created_at, last_active "
        "FROM users ORDER BY is_admin DESC, username").fetchall()]


def set_user_password(username: str, password: str) -> None:
    with write() as conn:
        conn.execute("UPDATE users SET pw_hash=? WHERE username=?",
                     (_hash_pw(password), username.lower()))


def set_user_email(username: str, email: str | None) -> None:
    with write() as conn:
        conn.execute("UPDATE users SET email=? WHERE username=?",
                     ((email or "").lower() or None, username.lower()))


def touch_user(username: str) -> None:
    with write() as conn:
        conn.execute("UPDATE users SET last_active=? WHERE username=?", (_now(), username.lower()))


def delete_user(username: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM users WHERE username=?", (username.lower(),))


def set_totp_secret(username: str, secret: str | None) -> None:
    with write() as conn:
        conn.execute("UPDATE users SET totp_secret=? WHERE username=?", (secret, username.lower()))


def set_totp_enabled(username: str, enabled: bool) -> None:
    with write() as conn:
        conn.execute("UPDATE users SET totp_enabled=? WHERE username=?",
                     (1 if enabled else 0, username.lower()))


# --------------------------------------------------------------------------- #
# 2FA recovery (backup) codes — single-use, stored as SHA-256 hashes.
# --------------------------------------------------------------------------- #
def _norm_code(code: str) -> str:
    return code.strip().lower().replace("-", "").replace(" ", "")


def _rc_hash(code: str) -> str:
    import hashlib
    return hashlib.sha256(_norm_code(code).encode()).hexdigest()


def generate_recovery_codes(username: str, n: int = 10) -> list[str]:
    """Replace any existing codes with ``n`` fresh ones; return the plaintext."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    codes, now = [], _now()
    with write() as conn:
        conn.execute("DELETE FROM recovery_codes WHERE username=?", (username.lower(),))
        for _ in range(n):
            raw = "".join(secrets.choice(alphabet) for _ in range(10))
            codes.append(raw[:5] + "-" + raw[5:])
            conn.execute(
                "INSERT INTO recovery_codes (username, code_hash, used, created_at) VALUES (?,?,0,?)",
                (username.lower(), _rc_hash(raw), now),
            )
    return codes


def consume_recovery_code(username: str, code: str) -> bool:
    """Spend a matching unused code; return True on success."""
    h = _rc_hash(code)
    with write() as conn:
        row = conn.execute(
            "SELECT id FROM recovery_codes WHERE username=? AND code_hash=? AND used=0",
            (username.lower(), h),
        ).fetchone()
        if not row:
            return False
        conn.execute("UPDATE recovery_codes SET used=1 WHERE id=?", (row["id"],))
    return True


def recovery_codes_remaining(username: str) -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) AS n FROM recovery_codes WHERE username=? AND used=0", (username.lower(),)
    ).fetchone()
    return row["n"] if row else 0


def clear_recovery_codes(username: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM recovery_codes WHERE username=?", (username.lower(),))


# --------------------------------------------------------------------------- #
# Scripts (Phase 2)
# --------------------------------------------------------------------------- #
def create_script(org_id: str, name: str, content: str, shell: str = "shell",
                  description: str | None = None, category: str = "Script") -> dict:
    sid = uuid.uuid4().hex
    with write() as conn:
        conn.execute(
            "INSERT INTO scripts (id, org_id, name, description, shell, content, category, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sid, org_id, name, description, shell, content, category, _now()),
        )
    return get_script(sid)


def update_script(script_id: str, name: str, content: str, shell: str,
                  description: str | None, category: str) -> dict:
    with write() as conn:
        conn.execute(
            "UPDATE scripts SET name=?, content=?, shell=?, description=?, category=? WHERE id=?",
            (name, content, shell, description, category, script_id),
        )
    return get_script(script_id)


def get_script(script_id: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
    return dict(row) if row else None


def list_scripts(org_id: str) -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM scripts WHERE org_id=? ORDER BY name", (org_id,)).fetchall()]


def delete_script(script_id: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM scripts WHERE id=?", (script_id,))


def create_run(org_id: str, device_id: str, name: str, script_id: str | None = None) -> str:
    rid = uuid.uuid4().hex
    with write() as conn:
        conn.execute(
            "INSERT INTO script_runs (id, script_id, org_id, device_id, name, status, created_at) "
            "VALUES (?,?,?,?,?,'running',?)",
            (rid, script_id, org_id, device_id, name, _now()),
        )
    return rid


def finish_run(run_id: str, status: str, exit_code: int | None, output: str) -> None:
    with write() as conn:
        conn.execute(
            "UPDATE script_runs SET status=?, exit_code=?, output=?, finished_at=? WHERE id=?",
            (status, exit_code, output, _now(), run_id),
        )


def create_schedule(org_id: str, script_id: str, name: str, target_type: str,
                    target_id: str | None, trigger: str, interval_minutes: int | None,
                    at_time: str | None, next_run: float) -> dict:
    sid = uuid.uuid4().hex
    with write() as conn:
        conn.execute(
            """INSERT INTO schedules (id, org_id, script_id, name, target_type, target_id,
                   trigger, interval_minutes, at_time, enabled, next_run, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,1,?,?)""",
            (sid, org_id, script_id, name, target_type, target_id, trigger,
             interval_minutes, at_time, next_run, _now()),
        )
    return get_schedule(sid)


def get_schedule(schedule_id: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    return dict(row) if row else None


def list_schedules(org_id: str) -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM schedules WHERE org_id=? ORDER BY created_at DESC", (org_id,)).fetchall()]


def due_schedules() -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?",
        (_now(),)).fetchall()]


def set_schedule_enabled(schedule_id: str, enabled: bool, next_run: float | None = None) -> None:
    with write() as conn:
        conn.execute("UPDATE schedules SET enabled=?, next_run=? WHERE id=?",
                     (1 if enabled else 0, next_run, schedule_id))


def mark_schedule_ran(schedule_id: str, next_run: float | None) -> None:
    with write() as conn:
        conn.execute("UPDATE schedules SET last_run=?, next_run=? WHERE id=?",
                     (_now(), next_run, schedule_id))


def delete_schedule(schedule_id: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))


# --------------------------------------------------------------------------- #
# Script file attachments
# --------------------------------------------------------------------------- #
def add_script_file(script_id: str, name: str, content_b64: str, size: int) -> dict:
    fid = uuid.uuid4().hex
    with write() as conn:
        conn.execute(
            "INSERT INTO script_files (id, script_id, name, content, size, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (fid, script_id, name, content_b64, size, _now()),
        )
    return {"id": fid, "name": name, "size": size}


def list_script_files(script_id: str) -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT id, name, size, created_at FROM script_files WHERE script_id=? ORDER BY name",
        (script_id,)).fetchall()]


def files_payload(script_id: str) -> list[dict]:
    """Name + base64 content for delivering attachments to an agent."""
    return [{"name": r["name"], "b64": r["content"]} for r in get_conn().execute(
        "SELECT name, content FROM script_files WHERE script_id=?", (script_id,)).fetchall()]


def get_script_file(file_id: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM script_files WHERE id=?", (file_id,)).fetchone()
    return dict(row) if row else None


def delete_script_file(file_id: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM script_files WHERE id=?", (file_id,))


# --------------------------------------------------------------------------- #
# Monitoring policies
# --------------------------------------------------------------------------- #
def create_monitor(org_id: str, name: str, monitor_script_id: str,
                   remediation_script_id: str | None, target_type: str, target_id: str | None,
                   trigger: str, interval_minutes: int | None, at_time: str | None,
                   variables_json: str | None, next_run: float) -> dict:
    mid = uuid.uuid4().hex
    with write() as conn:
        conn.execute(
            """INSERT INTO monitors (id, org_id, name, monitor_script_id, remediation_script_id,
                   target_type, target_id, trigger, interval_minutes, at_time, variables_json,
                   enabled, next_run, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (mid, org_id, name, monitor_script_id, remediation_script_id, target_type, target_id,
             trigger, interval_minutes, at_time, variables_json, next_run, _now()),
        )
    return get_monitor(mid)


def get_monitor(monitor_id: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
    return dict(row) if row else None


def list_monitors(org_id: str) -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM monitors WHERE org_id=? ORDER BY created_at DESC", (org_id,)).fetchall()]


def due_monitors() -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM monitors WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?",
        (_now(),)).fetchall()]


def set_monitor_enabled(monitor_id: str, enabled: bool, next_run: float | None = None) -> None:
    with write() as conn:
        conn.execute("UPDATE monitors SET enabled=?, next_run=? WHERE id=?",
                     (1 if enabled else 0, next_run, monitor_id))


def mark_monitor_ran(monitor_id: str, next_run: float | None, status: str) -> None:
    with write() as conn:
        conn.execute("UPDATE monitors SET last_run=?, next_run=?, last_status=? WHERE id=?",
                     (_now(), next_run, status, monitor_id))


def delete_monitor(monitor_id: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM monitors WHERE id=?", (monitor_id,))


def list_runs(org_id: str, device_id: str | None = None, limit: int = 50) -> list[dict]:
    conn = get_conn()
    if device_id:
        rows = conn.execute(
            "SELECT * FROM script_runs WHERE org_id=? AND device_id=? ORDER BY created_at DESC LIMIT ?",
            (org_id, device_id, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM script_runs WHERE org_id=? ORDER BY created_at DESC LIMIT ?",
            (org_id, limit)).fetchall()
    return [dict(r) for r in rows]


def init_db() -> None:
    global _conn
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL;")
    _conn.execute("PRAGMA foreign_keys=ON;")
    _conn.executescript(SCHEMA)
    _migrate(_conn)
    _conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Lightweight additive migrations for existing databases."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for col, ddl in (("totp_secret", "TEXT"), ("totp_enabled", "INTEGER NOT NULL DEFAULT 0")):
        if col not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
    scols = {r[1] for r in conn.execute("PRAGMA table_info(scripts)")}
    if "category" not in scols:
        conn.execute("ALTER TABLE scripts ADD COLUMN category TEXT DEFAULT 'Script'")
    dcols = {r[1] for r in conn.execute("PRAGMA table_info(devices)")}
    if "approved" not in dcols:
        # Existing devices stay approved; new ones can require approval.
        conn.execute("ALTER TABLE devices ADD COLUMN approved INTEGER NOT NULL DEFAULT 1")
    if "logged_in_user" not in dcols:
        conn.execute("ALTER TABLE devices ADD COLUMN logged_in_user TEXT")
    if "disks_json" not in dcols:
        conn.execute("ALTER TABLE devices ADD COLUMN disks_json TEXT")
    # Remove any duplicate (node, subnet) rows from before dedup, keeping one.
    conn.execute("DELETE FROM subnets WHERE id NOT IN "
                 "(SELECT MIN(id) FROM subnets GROUP BY node_id, cidr)")
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(enroll_tokens)")}
    if "kind" not in tcols:
        conn.execute("ALTER TABLE enroll_tokens ADD COLUMN kind TEXT NOT NULL DEFAULT 'enroll'")
        # Reclassify machine-minted download/update tokens created before this split.
        conn.execute("UPDATE enroll_tokens SET kind='internal' "
                     "WHERE label IN ('agent-update','installer')")
    # Tidy up: drop expired internal download tokens so they don't accumulate.
    conn.execute("DELETE FROM enroll_tokens WHERE kind='internal' "
                 "AND expires_at IS NOT NULL AND expires_at < ?", (_now(),))


def get_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("Database not initialised; call init_db() first")
    return _conn


@contextmanager
def write() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# --------------------------------------------------------------------------- #
# Organizations, users, standards, groups
# --------------------------------------------------------------------------- #
def create_org(name: str, enroll_key: str | None = None, org_id: str | None = None) -> dict:
    org_id = org_id or uuid.uuid4().hex
    enroll_key = enroll_key or secrets.token_urlsafe(24)
    with write() as conn:
        conn.execute(
            "INSERT INTO organizations (id, name, enroll_key, created_at) VALUES (?,?,?,?)",
            (org_id, name, enroll_key, _now()),
        )
        # Seed default monitoring standard.
        std_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO standards (id, org_id, name, policy_json, baseline_json, alert_json)
               VALUES (?,?,?,?,?,?)""",
            (std_id, org_id, "Default", json.dumps(DEFAULT_POLICY), json.dumps({}),
             json.dumps({"recipients": [], "rules": ["offline", "cpu", "disk", "mem"]})),
        )
        # Seed default OS groups.
        for name_, os_match in (("Windows", "windows"), ("Linux", "linux"),
                                ("Windows Server", "windows_server")):
            conn.execute(
                "INSERT INTO groups (id, org_id, name, is_default, os_match, standard_id) "
                "VALUES (?,?,?,1,?,?)",
                (uuid.uuid4().hex, org_id, name_, os_match, std_id),
            )
    return get_org(org_id)


def get_org(org_id: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()
    return dict(row) if row else None


def get_org_by_key(enroll_key: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM organizations WHERE enroll_key=?", (enroll_key,)
    ).fetchone()
    return dict(row) if row else None


def rotate_org_key(org_id: str) -> str:
    """Generate a new enrolment key for new installs (existing agents keep working)."""
    new_key = secrets.token_urlsafe(24)
    with write() as conn:
        conn.execute("UPDATE organizations SET enroll_key=? WHERE id=?", (new_key, org_id))
    return new_key


# --------------------------------------------------------------------------- #
# One-time enrolment tokens (write-once, single-use)
# --------------------------------------------------------------------------- #
def _sha256_hex(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()


def create_enroll_token(org_id: str, label: str | None = None,
                        ttl_hours: float | None = None, kind: str = "enroll") -> dict:
    """Create a one-time enrolment key. Returns the plaintext token ONCE — only
    its hash is stored, so it can never be shown again.

    ``kind='internal'`` marks machine-minted download/update tokens that authorise
    a re-download for an already-enrolled device; these are hidden from the
    onboarding-keys list and auto-pruned when they expire."""
    tid = uuid.uuid4().hex
    token = "lrmm_" + secrets.token_urlsafe(30)
    exp = (_now() + ttl_hours * 3600) if ttl_hours else None
    with write() as conn:
        conn.execute(
            "INSERT INTO enroll_tokens (id, org_id, token_hash, label, created_at, "
            "expires_at, kind) VALUES (?,?,?,?,?,?,?)",
            (tid, org_id, _sha256_hex(token), label, _now(), exp, kind),
        )
    return {"id": tid, "org_id": org_id, "token": token, "label": label, "expires_at": exp}


def list_enroll_tokens(org_id: str) -> list[dict]:
    """User-facing onboarding keys only (machine-minted download tokens excluded)."""
    return [dict(r) for r in get_conn().execute(
        "SELECT id, label, created_at, expires_at, used_at, device_id "
        "FROM enroll_tokens WHERE org_id=? AND kind='enroll' ORDER BY created_at DESC",
        (org_id,)).fetchall()]


def token_valid_for(org_id: str, token: str) -> bool:
    """True if the token is unused, unexpired and belongs to this org (no consume)."""
    row = get_conn().execute(
        "SELECT org_id, used_at, expires_at FROM enroll_tokens WHERE token_hash=?",
        (_sha256_hex(token),)).fetchone()
    return bool(row and row["org_id"] == org_id and row["used_at"] is None
                and (not row["expires_at"] or _now() <= row["expires_at"]))


def consume_enroll_token(token: str, device_id: str) -> str | None:
    """Spend a valid token for a device; return its org_id, or None if invalid."""
    h = _sha256_hex(token)
    now = _now()
    with write() as conn:
        row = conn.execute(
            "SELECT id, org_id, used_at, expires_at FROM enroll_tokens WHERE token_hash=?",
            (h,)).fetchone()
        if not row or row["used_at"] is not None:
            return None
        if row["expires_at"] and now > row["expires_at"]:
            return None
        conn.execute("UPDATE enroll_tokens SET used_at=?, device_id=? WHERE id=?",
                     (now, device_id, row["id"]))
        return row["org_id"]


def get_enroll_token(token_id: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM enroll_tokens WHERE id=?", (token_id,)).fetchone()
    return dict(row) if row else None


def delete_enroll_token(token_id: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM enroll_tokens WHERE id=?", (token_id,))


def list_orgs() -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM organizations ORDER BY name").fetchall()]


def org_member_count(org_id: str) -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) AS n FROM org_users WHERE org_id=?", (org_id,)).fetchone()
    return row["n"] if row else 0


def add_org_user(org_id: str, email: str, role: str = "admin") -> None:
    with write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO org_users (org_id, user_email, role) VALUES (?,?,?)",
            (org_id, email.lower(), role),
        )


def orgs_for_user(email: str, is_global_admin: bool = False) -> list[dict]:
    if is_global_admin:
        return list_orgs()
    rows = get_conn().execute(
        """SELECT o.* FROM organizations o JOIN org_users u ON u.org_id = o.id
           WHERE u.user_email = ? ORDER BY o.name""",
        (email.lower(),),
    ).fetchall()
    return [dict(r) for r in rows]


def user_role(email: str, org_id: str) -> str | None:
    row = get_conn().execute(
        "SELECT role FROM org_users WHERE user_email=? AND org_id=?",
        (email.lower(), org_id),
    ).fetchone()
    return row["role"] if row else None


def list_groups(org_id: str) -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM groups WHERE org_id=? ORDER BY is_default DESC, name", (org_id,)
    ).fetchall()]


def create_group(org_id: str, name: str) -> dict:
    gid = uuid.uuid4().hex
    with write() as conn:
        conn.execute(
            "INSERT INTO groups (id, org_id, name, is_default) VALUES (?,?,?,0)",
            (gid, org_id, name),
        )
    return dict(get_conn().execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone())


def default_group_for_os(org_id: str, os_kind: str) -> str | None:
    row = get_conn().execute(
        "SELECT id FROM groups WHERE org_id=? AND os_match=? AND is_default=1",
        (org_id, os_kind),
    ).fetchone()
    return row["id"] if row else None


def get_effective_policy(device: dict) -> dict:
    """Resolve the monitoring policy for a device: device→group→org standard."""
    conn = get_conn()
    policy = dict(DEFAULT_POLICY)
    std_row = None
    # Group override first.
    if device.get("group_id"):
        g = conn.execute("SELECT standard_id FROM groups WHERE id=?",
                         (device["group_id"],)).fetchone()
        if g and g["standard_id"]:
            std_row = conn.execute("SELECT policy_json FROM standards WHERE id=?",
                                   (g["standard_id"],)).fetchone()
    if std_row is None:
        std_row = conn.execute(
            "SELECT policy_json FROM standards WHERE org_id=? ORDER BY rowid LIMIT 1",
            (device["org_id"],),
        ).fetchone()
    if std_row and std_row["policy_json"]:
        policy.update(json.loads(std_row["policy_json"]))
    return policy


def alert_config(org_id: str) -> dict:
    row = get_conn().execute(
        "SELECT alert_json FROM standards WHERE org_id=? ORDER BY rowid LIMIT 1", (org_id,)
    ).fetchone()
    return json.loads(row["alert_json"]) if row and row["alert_json"] else {}


# --------------------------------------------------------------------------- #
# Devices & metrics
# --------------------------------------------------------------------------- #
def upsert_device(org_id: str, dev: dict[str, Any], require_approval: bool = False) -> dict:
    """Insert or update a device from an agent ``register`` payload.

    A brand-new device is created with ``approved=0`` when ``require_approval`` is
    set, so it lands in the approval queue; existing devices keep their state.
    """
    now = _now()
    inv = dev.get("inventory") or {}
    os_kind = dev.get("os_kind") or _classify_os(inv)
    with write() as conn:
        existing = conn.execute("SELECT * FROM devices WHERE id=?", (dev["id"],)).fetchone()
        group_id = existing["group_id"] if existing else None
        if group_id is None:
            group_id = default_group_for_os(org_id, os_kind)
        fields = dict(
            org_id=org_id, group_id=group_id, hostname=dev.get("hostname", "unknown"),
            os=inv.get("os") or dev.get("os"), os_version=inv.get("os_version"),
            os_arch=inv.get("os_arch"), os_kind=os_kind,
            manufacturer=inv.get("manufacturer"), model=inv.get("model"),
            serial=inv.get("serial"), cpu=inv.get("cpu"), ram_total=inv.get("ram_total"),
            ip=inv.get("ip") or dev.get("ip"), mac=inv.get("mac") or dev.get("mac"),
            agent_version=inv.get("agent_version"),
            logged_in_user=inv.get("logged_in_user"),
            inventory_json=json.dumps(inv), last_seen=now,
        )
        if existing:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE devices SET {sets} WHERE id=?",
                         (*fields.values(), dev["id"]))
        else:
            fields["approved"] = 0 if require_approval else 1
            cols = ["id", "created_at", *fields.keys()]
            vals = [dev["id"], now, *fields.values()]
            conn.execute(
                f"INSERT INTO devices ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
                vals,
            )
    return get_device(dev["id"])


def set_device_org(device_id: str, org_id: str) -> None:
    """Move a device to another organisation, re-homing its group."""
    dev = get_device(device_id)
    if not dev:
        return
    new_group = default_group_for_os(org_id, dev.get("os_kind") or "linux")
    with write() as conn:
        conn.execute("UPDATE devices SET org_id=?, group_id=? WHERE id=?",
                     (org_id, new_group, device_id))


def set_device_approved(device_id: str, approved: bool) -> None:
    with write() as conn:
        conn.execute("UPDATE devices SET approved=? WHERE id=?",
                     (1 if approved else 0, device_id))


def list_pending(org_id: str | None = None) -> list[dict]:
    conn = get_conn()
    if org_id:
        rows = conn.execute(
            "SELECT * FROM devices WHERE approved=0 AND org_id=? ORDER BY created_at DESC",
            (org_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM devices WHERE approved=0 ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def delete_org(org_id: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM organizations WHERE id=?", (org_id,))


def _classify_os(inv: dict) -> str:
    os_name = (inv.get("os") or "").lower()
    if "windows" in os_name:
        if inv.get("is_server") or "server" in (inv.get("os_version") or "").lower():
            return "windows_server"
        return "windows"
    return "linux"


def touch_device(device_id: str) -> None:
    with write() as conn:
        conn.execute("UPDATE devices SET last_seen=? WHERE id=?", (_now(), device_id))


def set_logged_in_user(device_id: str, user: str | None) -> None:
    """Update the signed-in user from a heartbeat — only writes when it changes."""
    with write() as conn:
        conn.execute(
            "UPDATE devices SET logged_in_user=? "
            "WHERE id=? AND IFNULL(logged_in_user,'') != IFNULL(?,'')",
            (user, device_id, user))


def set_device_disks(device_id: str, disks: list | None) -> None:
    """Store the latest per-volume disk usage reported on a heartbeat."""
    if not disks:
        return
    with write() as conn:
        conn.execute("UPDATE devices SET disks_json=? WHERE id=?",
                     (json.dumps(disks), device_id))


def set_node(device_id: str, is_node: bool) -> None:
    with write() as conn:
        conn.execute("UPDATE devices SET is_node=? WHERE id=?",
                     (1 if is_node else 0, device_id))


def set_compliance(device_id: str, compliant: bool, detail: dict) -> None:
    with write() as conn:
        conn.execute("UPDATE devices SET compliant=?, compliance_json=? WHERE id=?",
                     (1 if compliant else 0, json.dumps(detail), device_id))


def insert_metric(device_id: str, m: dict[str, Any]) -> None:
    with write() as conn:
        conn.execute(
            """INSERT INTO metrics (device_id, ts, cpu_percent, mem_percent, mem_total,
                   mem_used, disk_percent, disk_total, disk_used, uptime, net_sent, net_recv)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (device_id, _now(), m.get("cpu_percent"), m.get("mem_percent"),
             m.get("mem_total"), m.get("mem_used"), m.get("disk_percent"),
             m.get("disk_total"), m.get("disk_used"), m.get("uptime"),
             m.get("net_sent"), m.get("net_recv")),
        )


def list_devices(org_id: str, group_id: str | None = None) -> list[dict]:
    conn = get_conn()
    if group_id:
        rows = conn.execute(
            "SELECT * FROM devices WHERE org_id=? AND group_id=? AND approved=1 ORDER BY hostname",
            (org_id, group_id)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM devices WHERE org_id=? AND approved=1 ORDER BY hostname",
            (org_id,)).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        latest = conn.execute(
            "SELECT * FROM metrics WHERE device_id=? ORDER BY ts DESC LIMIT 1", (d["id"],)
        ).fetchone()
        d["latest"] = dict(latest) if latest else None
        out.append(d)
    return out


def all_devices() -> list[dict]:
    return [dict(r) for r in get_conn().execute("SELECT * FROM devices").fetchall()]


def get_device(device_id: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("inventory_json"):
        d["inventory"] = json.loads(d["inventory_json"])
    if d.get("disks_json"):
        try:
            d["disks"] = json.loads(d["disks_json"])
        except (ValueError, TypeError):
            d["disks"] = []
    # Attach configured subnets so the device drawer can list them for a node.
    if d.get("is_node"):
        d["subnets"] = list_subnets(device_id)
    return d


def get_metrics(device_id: str, limit: int = 200) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM metrics WHERE device_id=? ORDER BY ts DESC LIMIT ?",
        (device_id, min(limit, 1000)),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def set_device_group(device_id: str, group_id: str | None) -> None:
    with write() as conn:
        conn.execute("UPDATE devices SET group_id=? WHERE id=?", (group_id, device_id))


def delete_device(device_id: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM devices WHERE id=?", (device_id,))


def prune_metrics(max_age_seconds: float) -> int:
    cutoff = _now() - max_age_seconds
    with write() as conn:
        return conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,)).rowcount


# --------------------------------------------------------------------------- #
# Nodes, subnets, discovered hosts
# --------------------------------------------------------------------------- #
def list_nodes(org_id: str) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM devices WHERE org_id=? AND is_node=1 ORDER BY hostname", (org_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["subnets"] = list_subnets(d["id"])
        out.append(d)
    return out


def list_subnets(node_id: str) -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM subnets WHERE node_id=? ORDER BY cidr", (node_id,)).fetchall()]


def add_subnet(node_id: str, cidr: str, broadcast: str | None) -> bool:
    """Add a subnet to a node, ignoring an exact duplicate. Returns True if added."""
    with write() as conn:
        if conn.execute("SELECT 1 FROM subnets WHERE node_id=? AND cidr=?",
                        (node_id, cidr)).fetchone():
            return False
        conn.execute("INSERT INTO subnets (node_id, cidr, broadcast) VALUES (?,?,?)",
                     (node_id, cidr, broadcast))
        return True


def delete_subnet(subnet_id: int) -> None:
    with write() as conn:
        conn.execute("DELETE FROM subnets WHERE id=?", (subnet_id,))


def upsert_network_hosts(org_id: str, node_id: str, hosts: list[dict]) -> None:
    now = _now()
    seen_ips = [h.get("ip") for h in hosts if h.get("ip")]
    with write() as conn:
        # Hosts this node saw before but not in this scan have left the network —
        # age them to offline (keeping their last_seen for the UI).
        if seen_ips:
            ph = ",".join("?" * len(seen_ips))
            conn.execute(
                f"UPDATE network_hosts SET online=0 WHERE node_id=? AND ip NOT IN ({ph})",
                (node_id, *seen_ips))
        else:
            conn.execute("UPDATE network_hosts SET online=0 WHERE node_id=?", (node_id,))
        for h in hosts:
            existing = conn.execute(
                "SELECT id, first_seen FROM network_hosts WHERE node_id=? AND ip=?",
                (node_id, h.get("ip")),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE network_hosts SET mac=?, hostname=?, manufacturer=?,
                           last_seen=?, online=1 WHERE id=?""",
                    (h.get("mac"), h.get("hostname"), h.get("manufacturer"),
                     now, existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO network_hosts (org_id, node_id, ip, mac, hostname,
                           manufacturer, first_seen, last_seen, online)
                       VALUES (?,?,?,?,?,?,?,?,1)""",
                    (org_id, node_id, h.get("ip"), h.get("mac"), h.get("hostname"),
                     h.get("manufacturer"), now, now),
                )


def list_network_hosts(org_id: str) -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM network_hosts WHERE org_id=? ORDER BY ip", (org_id,)).fetchall()]


# --------------------------------------------------------------------------- #
# Alert state
# --------------------------------------------------------------------------- #
def get_alert_state(device_id: str, rule: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM alert_state WHERE device_id=? AND rule=?", (device_id, rule)
    ).fetchone()
    return dict(row) if row else None


def set_alert_state(device_id: str, rule: str, state: str,
                    since: float | None, last_email: float | None) -> None:
    with write() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO alert_state (device_id, rule, state, since, last_email)
               VALUES (?,?,?,?,?)""",
            (device_id, rule, state, since, last_email),
        )
