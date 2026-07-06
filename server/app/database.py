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
    gpu           TEXT,
    software_json TEXT,
    software_at   REAL,
    cpu           TEXT,
    ram_total     INTEGER,
    ip            TEXT,
    mac           TEXT,
    agent_version TEXT,
    logged_in_user TEXT,
    disks_json    TEXT,
    hyperv_json   TEXT,
    backups_json  TEXT,
    services_json TEXT,
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
    gpu_percent     REAL,
    gpu_temp        REAL,
    gpu_mem_percent REAL,
    cpu_temp        REAL,
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

-- SNMP monitoring targets, polled by a node over UDP/161 (v1/v2c).
CREATE TABLE IF NOT EXISTS snmp_targets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id       TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    name         TEXT,
    host         TEXT NOT NULL,
    port         INTEGER NOT NULL DEFAULT 161,
    version      TEXT NOT NULL DEFAULT '2c',
    community    TEXT NOT NULL DEFAULT 'public',
    oids         TEXT,                       -- JSON: [{oid, label}]
    interval     INTEGER NOT NULL DEFAULT 300,
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_poll    REAL,
    last_ok      INTEGER,
    last_error   TEXT,
    created_at   REAL,
    FOREIGN KEY (node_id) REFERENCES devices(id) ON DELETE CASCADE,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
);

-- Time-series of SNMP readings (one row per OID per poll).
CREATE TABLE IF NOT EXISTS snmp_readings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id    INTEGER NOT NULL,
    ts           REAL NOT NULL,
    oid          TEXT NOT NULL,
    label        TEXT,
    value_num    REAL,
    value_text   TEXT,
    type         TEXT,
    FOREIGN KEY (target_id) REFERENCES snmp_targets(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_snmp_readings_target_ts ON snmp_readings(target_id, ts);

-- UniFi Site Manager (cloud) accounts, polled server-side via the UniFi API.
-- snapshot_json holds the latest normalised {hosts, devices, isp, edges} snapshot.
CREATE TABLE IF NOT EXISTS unifi_accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        TEXT NOT NULL,
    name          TEXT,
    api_key       TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    interval      INTEGER NOT NULL DEFAULT 300,
    host_ids_json TEXT,                        -- JSON list of console ids to include ([] = all)
    snapshot_json TEXT,
    last_poll     REAL,
    last_ok       INTEGER,
    last_error    TEXT,
    created_at    REAL,
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

-- Historical log of monitor-rule issues per device. A row is written when an
-- alert clears (raised -> ok), capturing how long it was raised. Current/active
-- issues live in alert_state; this is the resolved-issue history.
CREATE TABLE IF NOT EXISTS incidents (
    id          TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL,
    org_id      TEXT,
    rule_id     TEXT,
    name        TEXT NOT NULL,
    metric      TEXT,
    severity    TEXT,
    detail      TEXT,
    opened_at   REAL NOT NULL,
    resolved_at REAL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_device ON incidents(device_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    username       TEXT PRIMARY KEY,
    email          TEXT,
    display_name   TEXT,
    pw_hash        TEXT NOT NULL,
    is_admin       INTEGER NOT NULL DEFAULT 0,
    email_verified INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    last_active    REAL
);

CREATE TABLE IF NOT EXISTS recovery_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    token          TEXT PRIMARY KEY,
    email          TEXT NOT NULL,
    is_admin       INTEGER NOT NULL DEFAULT 0,
    delivery       TEXT NOT NULL DEFAULT 'both',  -- 'email' | 'link' | 'both'
    verify_code    TEXT,
    verify_expires REAL,
    created_at     REAL NOT NULL,
    expires_at     REAL NOT NULL,
    used_at        REAL
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

-- User access groups (separate from device groups).
-- An access group bundles users together and grants them roles in organisations.
-- Deny effects take precedence over allow across all groups a user belongs to.
CREATE TABLE IF NOT EXISTS access_groups (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS access_group_members (
    group_id   TEXT NOT NULL,
    user_email TEXT NOT NULL,
    PRIMARY KEY (group_id, user_email),
    FOREIGN KEY (group_id) REFERENCES access_groups(id) ON DELETE CASCADE
);

-- Base role a group gives users in an org (admin/member/viewer).
CREATE TABLE IF NOT EXISTS access_group_orgs (
    group_id TEXT NOT NULL,
    org_id   TEXT NOT NULL,
    role     TEXT NOT NULL DEFAULT 'member',
    PRIMARY KEY (group_id, org_id),
    FOREIGN KEY (group_id) REFERENCES access_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (org_id)   REFERENCES organizations(id) ON DELETE CASCADE
);

-- Per-action permission overrides layered on top of the base role.
-- effect = 'allow' | 'deny'  (deny wins across all groups)
-- permission = 'terminal' | 'scripts' | 'power' | 'wol' | 'device_delete' | 'agent_delete'
CREATE TABLE IF NOT EXISTS access_group_perms (
    group_id   TEXT NOT NULL,
    org_id     TEXT NOT NULL,
    permission TEXT NOT NULL,
    effect     TEXT NOT NULL DEFAULT 'allow',
    PRIMARY KEY (group_id, org_id, permission),
    FOREIGN KEY (group_id) REFERENCES access_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (org_id)   REFERENCES organizations(id) ON DELETE CASCADE
);

-- Per-user configurable dashboard layout.
CREATE TABLE IF NOT EXISTS dashboard_prefs (
    user_email  TEXT PRIMARY KEY,
    layout_json TEXT,
    updated_at  REAL
);

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
-- org_id NULL = global monitor, applied across every organisation's devices.
CREATE TABLE IF NOT EXISTS monitors (
    id               TEXT PRIMARY KEY,
    org_id           TEXT,
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
    notify_email     INTEGER NOT NULL DEFAULT 1,
    severity         TEXT NOT NULL DEFAULT 'warning',
    last_run         REAL,
    next_run         REAL,
    last_status      TEXT,
    created_at       REAL NOT NULL,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
);

-- Template-backed metric-threshold monitors (the disk/CPU/memory/offline
-- "alert if X stays above Y for Z minutes" rules). org_id NULL = global rule,
-- evaluated against every device in every organisation.
CREATE TABLE IF NOT EXISTS monitor_rules (
    id               TEXT PRIMARY KEY,
    org_id           TEXT,
    template_id      TEXT NOT NULL,
    name             TEXT NOT NULL,
    metric           TEXT NOT NULL,    -- 'cpu_percent' | 'mem_percent' | 'disk_percent' | 'offline'
    threshold        REAL NOT NULL,    -- percent, or seconds-unseen for 'offline'
    duration_minutes REAL,             -- sustained period (ignored for 'offline')
    target_type      TEXT NOT NULL DEFAULT 'all',
    target_id        TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1,
    notify_email     INTEGER NOT NULL DEFAULT 1,
    severity         TEXT NOT NULL DEFAULT 'warning',
    created_at       REAL NOT NULL,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
);
"""


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
                email: str | None = None, display_name: str | None = None,
                email_verified: bool = False) -> None:
    with write() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO users
               (username, email, display_name, pw_hash, is_admin, email_verified, created_at, last_active)
               VALUES (?,?,?,?,?,?,?,?)""",
            (username.lower(), email, display_name or username, _hash_pw(password),
             1 if is_admin else 0, 1 if email_verified else 0, _now(), None),
        )


def update_user(username: str, *, display_name: str | None = None,
                email: str | None = None, is_admin: bool | None = None) -> None:
    """Admin edit of an account. Only the provided fields are changed.

    Pass ``display_name``/``email`` as an empty string to clear them; ``None``
    leaves the field untouched."""
    sets, args = [], []
    if display_name is not None:
        sets.append("display_name=?"); args.append(display_name.strip() or username)
    if email is not None:
        sets.append("email=?"); args.append((email or "").lower().strip() or None)
    if is_admin is not None:
        sets.append("is_admin=?"); args.append(1 if is_admin else 0)
    if not sets:
        return
    args.append(username.lower())
    with write() as conn:
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE username=?", args)


def set_user_email_verified(username: str, verified: bool = True) -> None:
    with write() as conn:
        conn.execute("UPDATE users SET email_verified=? WHERE username=?",
                     (1 if verified else 0, username.lower()))


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
        "SELECT username, email, display_name, is_admin, email_verified, created_at, last_active "
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


INVITE_TTL = 2 * 24 * 3600  # 2 days


def create_invite(email: str, is_admin: bool, delivery: str = "both") -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    if delivery not in ("email", "link", "both"):
        delivery = "both"
    with write() as conn:
        conn.execute(
            "INSERT INTO invites (token, email, is_admin, delivery, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (token, email.lower(), int(is_admin), delivery, now, now + INVITE_TTL),
        )
    return token


def set_invite_code(token: str, code: str | None, expires: float | None) -> None:
    with write() as conn:
        conn.execute("UPDATE invites SET verify_code=?, verify_expires=? WHERE token=?",
                     (code, expires, token))


def get_invite(token: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()
    return dict(row) if row else None


def list_invites() -> list[dict]:
    now = _now()
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM invites WHERE used_at IS NULL AND expires_at > ? ORDER BY created_at DESC", (now,)
    ).fetchall()]


def use_invite(token: str) -> None:
    with write() as conn:
        conn.execute("UPDATE invites SET used_at=? WHERE token=?", (_now(), token))


def delete_invite(token: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM invites WHERE token=?", (token,))


# --------------------------------------------------------------------------- #
# Access groups — user permission groups with deny-overrides-allow resolution
# --------------------------------------------------------------------------- #
ALL_PERMISSIONS = ("terminal", "scripts", "power", "wol", "device_delete", "agent_delete")


def create_access_group(name: str) -> dict:
    gid = uuid.uuid4().hex
    with write() as conn:
        conn.execute("INSERT INTO access_groups (id, name, created_at) VALUES (?,?,?)",
                     (gid, name.strip(), _now()))
    return get_access_group(gid)


def get_access_group(group_id: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM access_groups WHERE id=?", (group_id,)).fetchone()
    return dict(row) if row else None


def list_access_groups() -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM access_groups ORDER BY name").fetchall()]


def rename_access_group(group_id: str, name: str) -> None:
    with write() as conn:
        conn.execute("UPDATE access_groups SET name=? WHERE id=?", (name.strip(), group_id))


def delete_access_group(group_id: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM access_groups WHERE id=?", (group_id,))


def list_access_group_members(group_id: str) -> list[str]:
    return [r[0] for r in get_conn().execute(
        "SELECT user_email FROM access_group_members WHERE group_id=? ORDER BY user_email",
        (group_id,)).fetchall()]


def add_access_group_member(group_id: str, user_email: str) -> None:
    with write() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO access_group_members (group_id, user_email) VALUES (?,?)",
            (group_id, user_email.lower()))


def remove_access_group_member(group_id: str, user_email: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM access_group_members WHERE group_id=? AND user_email=?",
                     (group_id, user_email.lower()))


def list_access_group_orgs(group_id: str) -> list[dict]:
    rows = get_conn().execute(
        "SELECT ago.*, o.name AS org_name FROM access_group_orgs ago "
        "JOIN organizations o ON o.id = ago.org_id WHERE ago.group_id=? ORDER BY o.name",
        (group_id,)).fetchall()
    result = []
    for r in rows:
        entry = dict(r)
        perms = get_conn().execute(
            "SELECT permission, effect FROM access_group_perms WHERE group_id=? AND org_id=?",
            (group_id, r["org_id"])).fetchall()
        entry["perms"] = {p["permission"]: p["effect"] for p in perms}
        result.append(entry)
    return result


def set_access_group_org(group_id: str, org_id: str, role: str) -> None:
    with write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO access_group_orgs (group_id, org_id, role) VALUES (?,?,?)",
            (group_id, org_id, role))


def remove_access_group_org(group_id: str, org_id: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM access_group_orgs WHERE group_id=? AND org_id=?",
                     (group_id, org_id))
        conn.execute("DELETE FROM access_group_perms WHERE group_id=? AND org_id=?",
                     (group_id, org_id))


def set_access_group_perm(group_id: str, org_id: str, permission: str, effect: str) -> None:
    if permission not in ALL_PERMISSIONS or effect not in ("allow", "deny"):
        raise ValueError(f"Invalid permission '{permission}' or effect '{effect}'")
    with write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO access_group_perms (group_id, org_id, permission, effect) "
            "VALUES (?,?,?,?)", (group_id, org_id, permission, effect))


def remove_access_group_perm(group_id: str, org_id: str, permission: str) -> None:
    with write() as conn:
        conn.execute(
            "DELETE FROM access_group_perms WHERE group_id=? AND org_id=? AND permission=?",
            (group_id, org_id, permission))


def user_access_group_ids(user_email: str) -> list[str]:
    return [r[0] for r in get_conn().execute(
        "SELECT group_id FROM access_group_members WHERE user_email=?",
        (user_email.lower(),)).fetchall()]


def orgs_for_user_via_groups(email: str) -> list[dict]:
    """Orgs accessible through access-group membership (not via direct org_users)."""
    rows = get_conn().execute(
        """SELECT DISTINCT o.* FROM organizations o
           JOIN access_group_orgs ago ON ago.org_id = o.id
           JOIN access_group_members agm ON agm.group_id = ago.group_id
           WHERE agm.user_email=? ORDER BY o.name""",
        (email.lower(),)).fetchall()
    return [dict(r) for r in rows]


def user_effective_role(email: str, org_id: str) -> str | None:
    """Best role the user has in org_id across direct assignment and group membership.
    Returns 'admin' > 'member' > 'viewer' > None."""
    _rank = {"admin": 3, "member": 2, "viewer": 1}
    best: str | None = None
    # Direct assignment
    direct = user_role(email, org_id)
    if direct:
        best = direct
    # Group-based assignments
    for row in get_conn().execute(
        """SELECT ago.role FROM access_group_orgs ago
           JOIN access_group_members agm ON agm.group_id = ago.group_id
           WHERE agm.user_email=? AND ago.org_id=?""",
        (email.lower(), org_id)).fetchall():
        r = row["role"]
        if best is None or _rank.get(r, 0) > _rank.get(best, 0):
            best = r
    return best


def user_effective_perms(email: str, org_id: str) -> dict:
    """Compute effective allow/deny for each action in org_id.

    Returns a dict like:
      { 'terminal': {'effect': 'allow', 'denied_by': [], 'allowed_by': ['Devs']} }
    Deny wins across all groups; per-action entries without an explicit override
    inherit from the base role (admin → allow all; member → allow most; viewer → deny scripts/power/etc.).
    """
    role = user_effective_role(email, org_id)
    if role is None:
        return {p: {"effect": "deny", "denied_by": ["no access"], "allowed_by": []} for p in ALL_PERMISSIONS}

    # Role-based defaults
    _role_allows = {
        "admin":  set(ALL_PERMISSIONS),
        "member": {"terminal", "scripts", "power", "wol"},
        "viewer": set(),
    }
    role_allowed = _role_allows.get(role, set())

    # Collect per-group explicit perms
    rows = get_conn().execute(
        """SELECT ag.name AS gname, agp.permission, agp.effect
           FROM access_group_perms agp
           JOIN access_groups ag ON ag.id = agp.group_id
           JOIN access_group_members agm ON agm.group_id = agp.group_id
           WHERE agm.user_email=? AND agp.org_id=?""",
        (email.lower(), org_id)).fetchall()

    denied_by: dict[str, list[str]] = {p: [] for p in ALL_PERMISSIONS}
    allowed_by: dict[str, list[str]] = {p: [] for p in ALL_PERMISSIONS}
    for row in rows:
        p, eff, gname = row["permission"], row["effect"], row["gname"]
        if eff == "deny":
            denied_by[p].append(gname)
        else:
            allowed_by[p].append(gname)

    result = {}
    for p in ALL_PERMISSIONS:
        if denied_by[p]:
            result[p] = {"effect": "deny", "denied_by": denied_by[p], "allowed_by": allowed_by[p]}
        elif allowed_by[p]:
            result[p] = {"effect": "allow", "denied_by": [], "allowed_by": allowed_by[p]}
        else:
            # Fall back to role default
            eff = "allow" if p in role_allowed else "deny"
            result[p] = {"effect": eff, "denied_by": [], "allowed_by": []}
    return result


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
def create_monitor(org_id: str | None, name: str, monitor_script_id: str,
                   remediation_script_id: str | None, target_type: str, target_id: str | None,
                   trigger: str, interval_minutes: int | None, at_time: str | None,
                   variables_json: str | None, next_run: float,
                   notify_email: bool = True, severity: str = "warning") -> dict:
    mid = uuid.uuid4().hex
    with write() as conn:
        conn.execute(
            """INSERT INTO monitors (id, org_id, name, monitor_script_id, remediation_script_id,
                   target_type, target_id, trigger, interval_minutes, at_time, variables_json,
                   enabled, notify_email, severity, next_run, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
            (mid, org_id, name, monitor_script_id, remediation_script_id, target_type, target_id,
             trigger, interval_minutes, at_time, variables_json,
             1 if notify_email else 0, severity, next_run, _now()),
        )
    return get_monitor(mid)


def update_monitor(monitor_id: str, name: str, monitor_script_id: str,
                   remediation_script_id: str | None, target_type: str, target_id: str | None,
                   trigger: str, interval_minutes: int | None, at_time: str | None,
                   variables_json: str | None, next_run: float,
                   notify_email: bool = True, severity: str = "warning") -> dict | None:
    with write() as conn:
        conn.execute(
            """UPDATE monitors SET name=?, monitor_script_id=?, remediation_script_id=?,
                   target_type=?, target_id=?, trigger=?, interval_minutes=?, at_time=?,
                   variables_json=?, notify_email=?, severity=?, next_run=?
               WHERE id=?""",
            (name, monitor_script_id, remediation_script_id, target_type, target_id,
             trigger, interval_minutes, at_time, variables_json,
             1 if notify_email else 0, severity, next_run, monitor_id),
        )
    return get_monitor(monitor_id)


def get_monitor(monitor_id: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
    return dict(row) if row else None


def list_monitors(org_id: str) -> list[dict]:
    """Monitors visible to an org: its own site monitors plus every global one."""
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM monitors WHERE org_id=? OR org_id IS NULL ORDER BY created_at DESC",
        (org_id,)).fetchall()]


def list_global_monitors() -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM monitors WHERE org_id IS NULL ORDER BY created_at DESC").fetchall()]


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


# --------------------------------------------------------------------------- #
# Monitor rules — template-backed metric thresholds (CPU/mem/disk/offline).
# --------------------------------------------------------------------------- #
def create_monitor_rule(org_id: str | None, template_id: str, name: str, metric: str,
                        threshold: float, duration_minutes: float | None,
                        target_type: str, target_id: str | None,
                        notify_email: bool = True, severity: str = "warning") -> dict:
    rid = uuid.uuid4().hex
    with write() as conn:
        conn.execute(
            """INSERT INTO monitor_rules (id, org_id, template_id, name, metric, threshold,
                   duration_minutes, target_type, target_id, enabled, notify_email, severity,
                   created_at)
               VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?)""",
            (rid, org_id, template_id, name, metric, threshold, duration_minutes,
             target_type, target_id, 1 if notify_email else 0, severity, _now()),
        )
    return get_monitor_rule(rid)


def update_monitor_rule(rule_id: str, name: str, threshold: float,
                        duration_minutes: float | None, target_type: str,
                        target_id: str | None, notify_email: bool = True,
                        severity: str = "warning") -> dict | None:
    with write() as conn:
        conn.execute(
            """UPDATE monitor_rules SET name=?, threshold=?, duration_minutes=?,
                   target_type=?, target_id=?, notify_email=?, severity=?
               WHERE id=?""",
            (name, threshold, duration_minutes, target_type, target_id,
             1 if notify_email else 0, severity, rule_id),
        )
    return get_monitor_rule(rule_id)


def get_monitor_rule(rule_id: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM monitor_rules WHERE id=?", (rule_id,)).fetchone()
    return dict(row) if row else None


def list_monitor_rules(org_id: str) -> list[dict]:
    """Rules visible to an org: its own site rules plus every global one."""
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM monitor_rules WHERE org_id=? OR org_id IS NULL ORDER BY created_at DESC",
        (org_id,)).fetchall()]


def list_global_monitor_rules() -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM monitor_rules WHERE org_id IS NULL ORDER BY created_at DESC").fetchall()]


def list_effective_monitor_rules(device: dict) -> list[dict]:
    """Enabled rules that apply to ``device``: its org's site rules ∪ global rules,
    filtered down by each rule's target (device / group / all)."""
    rows = get_conn().execute(
        "SELECT * FROM monitor_rules WHERE enabled=1 AND (org_id=? OR org_id IS NULL)",
        (device["org_id"],),
    ).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        if r["target_type"] == "all":
            out.append(r)
        elif r["target_type"] == "group" and device.get("group_id") and r["target_id"] == device["group_id"]:
            out.append(r)
        elif r["target_type"] == "device" and r["target_id"] == device["id"]:
            out.append(r)
    return out


def set_monitor_rule_enabled(rule_id: str, enabled: bool) -> None:
    with write() as conn:
        conn.execute("UPDATE monitor_rules SET enabled=? WHERE id=?",
                     (1 if enabled else 0, rule_id))


def delete_monitor_rule(rule_id: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM monitor_rules WHERE id=?", (rule_id,))
        conn.execute("DELETE FROM alert_state WHERE rule=?", (f"rule:{rule_id}",))


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
    if "email_verified" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
        # Accounts that predate email verification are treated as already verified
        # (they were created by an admin/setup before the check existed).
        conn.execute("UPDATE users SET email_verified=1")
    icols = {r[1] for r in conn.execute("PRAGMA table_info(invites)")}
    if "delivery" not in icols:
        conn.execute("ALTER TABLE invites ADD COLUMN delivery TEXT NOT NULL DEFAULT 'both'")
    if "verify_code" not in icols:
        conn.execute("ALTER TABLE invites ADD COLUMN verify_code TEXT")
        conn.execute("ALTER TABLE invites ADD COLUMN verify_expires REAL")
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
    if "hyperv_json" not in dcols:
        conn.execute("ALTER TABLE devices ADD COLUMN hyperv_json TEXT")
    if "backups_json" not in dcols:
        conn.execute("ALTER TABLE devices ADD COLUMN backups_json TEXT")
    if "services_json" not in dcols:
        conn.execute("ALTER TABLE devices ADD COLUMN services_json TEXT")
    if "gpu" not in dcols:
        conn.execute("ALTER TABLE devices ADD COLUMN gpu TEXT")
    if "software_json" not in dcols:
        conn.execute("ALTER TABLE devices ADD COLUMN software_json TEXT")
        conn.execute("ALTER TABLE devices ADD COLUMN software_at REAL")
    # Remove any duplicate (node, subnet) rows from before dedup, keeping one.
    conn.execute("DELETE FROM subnets WHERE id NOT IN "
                 "(SELECT MIN(id) FROM subnets GROUP BY node_id, cidr)")
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(enroll_tokens)")}
    if "kind" not in tcols:
        conn.execute("ALTER TABLE enroll_tokens ADD COLUMN kind TEXT NOT NULL DEFAULT 'enroll'")
        # Reclassify machine-minted download/update tokens created before this split.
        conn.execute("UPDATE enroll_tokens SET kind='internal' "
                     "WHERE label IN ('agent-update','installer')")
    if "use_count" not in tcols:
        conn.execute("ALTER TABLE enroll_tokens ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0")
    # Tidy up: drop expired internal download tokens so they don't accumulate.
    conn.execute("DELETE FROM enroll_tokens WHERE kind='internal' "
                 "AND expires_at IS NOT NULL AND expires_at < ?", (_now(),))
    # monitors.org_id used to be NOT NULL; rebuild the table to allow NULL
    # (global monitors) on databases created before that changed.
    mcol = next((r for r in conn.execute("PRAGMA table_info(monitors)") if r[1] == "org_id"), None)
    if mcol is not None and mcol[3] == 1:  # notnull flag set
        conn.executescript("""
            CREATE TABLE monitors_new (
                id               TEXT PRIMARY KEY,
                org_id           TEXT,
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
            INSERT INTO monitors_new SELECT id, org_id, name, monitor_script_id,
                remediation_script_id, target_type, target_id, trigger, interval_minutes,
                at_time, variables_json, enabled, last_run, next_run, last_status, created_at
                FROM monitors;
            DROP TABLE monitors;
            ALTER TABLE monitors_new RENAME TO monitors;
        """)
    # Removed standard-policy alerting (cpu/mem/disk/offline are now ordinary
    # monitor_rules rows) — drop stale state left by the old hardcoded rules.
    conn.execute("DELETE FROM alert_state WHERE rule IN ('offline', 'cpu', 'disk', 'mem')")
    mocols = {r[1] for r in conn.execute("PRAGMA table_info(monitors)")}
    if "notify_email" not in mocols:
        conn.execute("ALTER TABLE monitors ADD COLUMN notify_email INTEGER NOT NULL DEFAULT 1")
    if "severity" not in mocols:
        conn.execute("ALTER TABLE monitors ADD COLUMN severity TEXT NOT NULL DEFAULT 'warning'")
    mrcols = {r[1] for r in conn.execute("PRAGMA table_info(monitor_rules)")}
    if "notify_email" not in mrcols:
        conn.execute("ALTER TABLE monitor_rules ADD COLUMN notify_email INTEGER NOT NULL DEFAULT 1")
    if "severity" not in mrcols:
        conn.execute("ALTER TABLE monitor_rules ADD COLUMN severity TEXT NOT NULL DEFAULT 'warning'")
    # GPU + temperature metrics (added later; backfill columns on old databases).
    metcols = {r[1] for r in conn.execute("PRAGMA table_info(metrics)")}
    for col in ("gpu_percent", "gpu_temp", "gpu_mem_percent", "cpu_temp"):
        if col not in metcols:
            conn.execute(f"ALTER TABLE metrics ADD COLUMN {col} REAL")
    # UniFi per-console selection (added later).
    ucols = {r[1] for r in conn.execute("PRAGMA table_info(unifi_accounts)")}
    if ucols and "host_ids_json" not in ucols:
        conn.execute("ALTER TABLE unifi_accounts ADD COLUMN host_ids_json TEXT")
    ocols = {r[1] for r in conn.execute("PRAGMA table_info(organizations)")}
    if "auto_update" not in ocols:
        # Per-org agent auto-update override: 'inherit' (use global default) | 'on' | 'off'.
        conn.execute("ALTER TABLE organizations ADD COLUMN auto_update TEXT NOT NULL DEFAULT 'inherit'")
    if "cpu_temp_driver" not in ocols:
        # Per-org opt-in for the Windows CPU-die sensor (LibreHardwareMonitor /
        # WinRing0 kernel driver): 'inherit' (use global default) | 'on' | 'off'.
        conn.execute("ALTER TABLE organizations ADD COLUMN cpu_temp_driver TEXT NOT NULL DEFAULT 'inherit'")


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
        # Seed default standard (compliance baseline + alert recipients only —
        # monitoring rules are now ordinary monitor_rules rows, added explicitly).
        std_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO standards (id, org_id, name, policy_json, baseline_json, alert_json)
               VALUES (?,?,?,?,?,?)""",
            (std_id, org_id, "Default", None, json.dumps({}),
             json.dumps({"recipients": []})),
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


def set_org_auto_update(org_id: str, mode: str) -> None:
    """Set an org's agent auto-update override: 'inherit' | 'on' | 'off'."""
    if mode not in ("inherit", "on", "off"):
        raise ValueError("mode must be inherit|on|off")
    with write() as conn:
        conn.execute("UPDATE organizations SET auto_update=? WHERE id=?", (mode, org_id))


def set_org_cpu_temp_driver(org_id: str, mode: str) -> None:
    """Set an org's Windows CPU-temp driver opt-in: 'inherit' | 'on' | 'off'."""
    if mode not in ("inherit", "on", "off"):
        raise ValueError("mode must be inherit|on|off")
    with write() as conn:
        conn.execute("UPDATE organizations SET cpu_temp_driver=? WHERE id=?", (mode, org_id))


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


# ---------- shareable download-link tokens (kind='download') ----------

def create_download_token(org_id: str, label: str | None = None,
                          ttl_days: float = 7) -> dict:
    """Create a multi-use, time-limited download token for the MSI/zip.

    Unlike enrolment tokens these are never consumed on use — they remain valid
    until they expire or are explicitly revoked by an admin."""
    tid = uuid.uuid4().hex
    token = "lrmm_dl_" + secrets.token_urlsafe(30)
    exp = _now() + ttl_days * 86400
    with write() as conn:
        conn.execute(
            "INSERT INTO enroll_tokens (id, org_id, token_hash, label, created_at, "
            "expires_at, kind, use_count) VALUES (?,?,?,?,?,?,?,0)",
            (tid, org_id, _sha256_hex(token), label, _now(), exp, "download"),
        )
    return {"id": tid, "org_id": org_id, "token": token, "label": label, "expires_at": exp}


def list_download_tokens(org_id: str) -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT id, label, created_at, expires_at, use_count "
        "FROM enroll_tokens WHERE org_id=? AND kind='download' ORDER BY created_at DESC",
        (org_id,)).fetchall()]


def download_token_valid(org_id: str, token: str, count: bool = True) -> bool:
    """True if the download token belongs to this org and has not expired.

    ``count`` bumps the use counter on an actual download; pass ``count=False`` for
    read-only validation (e.g. Synology package-source catalog/icon polling, which
    would otherwise inflate the count on every Package Center refresh)."""
    row = get_conn().execute(
        "SELECT org_id, expires_at FROM enroll_tokens "
        "WHERE token_hash=? AND kind='download'",
        (_sha256_hex(token),)).fetchone()
    if not row or row["org_id"] != org_id:
        return False
    if row["expires_at"] and _now() > row["expires_at"]:
        return False
    if count:
        with write() as conn:
            conn.execute("UPDATE enroll_tokens SET use_count=use_count+1 WHERE token_hash=?",
                         (_sha256_hex(token),))
    return True


def get_dashboard_layout(email: str) -> list | None:
    row = get_conn().execute(
        "SELECT layout_json FROM dashboard_prefs WHERE user_email=?", (email.lower(),)).fetchone()
    if row and row["layout_json"]:
        try:
            return json.loads(row["layout_json"])
        except (ValueError, TypeError):
            return None
    return None


def set_dashboard_layout(email: str, layout: list) -> None:
    with write() as conn:
        conn.execute(
            "INSERT INTO dashboard_prefs (user_email, layout_json, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(user_email) DO UPDATE SET layout_json=excluded.layout_json, "
            "updated_at=excluded.updated_at",
            (email.lower(), json.dumps(layout), _now()))


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
        """SELECT DISTINCT o.* FROM organizations o
           WHERE o.id IN (
               SELECT org_id FROM org_users WHERE user_email=?
               UNION
               SELECT ago.org_id FROM access_group_orgs ago
               JOIN access_group_members agm ON agm.group_id = ago.group_id
               WHERE agm.user_email=?
           ) ORDER BY o.name""",
        (email.lower(), email.lower()),
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
            serial=inv.get("serial"), gpu=inv.get("gpu"),
            cpu=inv.get("cpu"), ram_total=inv.get("ram_total"),
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


def set_device_software(device_id: str, software: list) -> None:
    with write() as conn:
        conn.execute("UPDATE devices SET software_json=?, software_at=? WHERE id=?",
                     (json.dumps(software), _now(), device_id))


def get_device_software(device_id: str) -> dict:
    """Cached installed-software list + when it was last collected."""
    row = get_conn().execute(
        "SELECT software_json, software_at FROM devices WHERE id=?", (device_id,)).fetchone()
    if not row or not row["software_json"]:
        return {"software": [], "collected_at": None}
    try:
        return {"software": json.loads(row["software_json"]), "collected_at": row["software_at"]}
    except (ValueError, TypeError):
        return {"software": [], "collected_at": None}


def set_device_disks(device_id: str, disks: list | None) -> None:
    """Store the latest per-volume disk usage reported on a heartbeat."""
    if not disks:
        return
    with write() as conn:
        conn.execute("UPDATE devices SET disks_json=? WHERE id=?",
                     (json.dumps(disks), device_id))


def set_device_hyperv(device_id: str, hyperv: dict | None) -> None:
    """Store the latest Hyper-V host summary + per-VM usage from a heartbeat."""
    if not hyperv:
        return
    with write() as conn:
        conn.execute("UPDATE devices SET hyperv_json=? WHERE id=?",
                     (json.dumps(hyperv), device_id))


def set_device_backups(device_id: str, backups: dict | None) -> None:
    """Store the latest Active Backup task snapshot reported on a heartbeat."""
    if not backups:
        return
    with write() as conn:
        conn.execute("UPDATE devices SET backups_json=? WHERE id=?",
                     (json.dumps(backups), device_id))


def set_device_services(device_id: str, services: list | None) -> None:
    """Store the latest service list (name/display/status) for the service monitor.
    Sent only periodically by the agent, so ignore empty refreshes."""
    if not services:
        return
    with write() as conn:
        conn.execute("UPDATE devices SET services_json=? WHERE id=?",
                     (json.dumps(services), device_id))


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
                   mem_used, disk_percent, disk_total, disk_used, uptime, net_sent, net_recv,
                   gpu_percent, gpu_temp, gpu_mem_percent, cpu_temp)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (device_id, _now(), m.get("cpu_percent"), m.get("mem_percent"),
             m.get("mem_total"), m.get("mem_used"), m.get("disk_percent"),
             m.get("disk_total"), m.get("disk_used"), m.get("uptime"),
             m.get("net_sent"), m.get("net_recv"),
             m.get("gpu_percent"), m.get("gpu_temp"), m.get("gpu_mem_percent"),
             m.get("cpu_temp")),
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
    if d.get("hyperv_json"):
        try:
            d["hyperv"] = json.loads(d["hyperv_json"])
        except (ValueError, TypeError):
            d["hyperv"] = None
    if d.get("backups_json"):
        try:
            d["backups"] = json.loads(d["backups_json"])
        except (ValueError, TypeError):
            d["backups"] = None
    if d.get("services_json"):
        try:
            d["services"] = json.loads(d["services_json"])
        except (ValueError, TypeError):
            d["services"] = None
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


def get_metrics_series(device_id: str, since: float, points: int = 120) -> list[dict]:
    """Down-sampled CPU/mem/disk history since a timestamp, averaged into buckets
    so a 30-day look-back stays light to query and chart."""
    now = _now()
    bucket = max((now - since) / max(points, 1), 1.0)
    rows = get_conn().execute(
        "SELECT MIN(ts) AS ts, AVG(cpu_percent) AS cpu, AVG(mem_percent) AS mem, "
        "AVG(disk_percent) AS disk, AVG(gpu_percent) AS gpu, AVG(gpu_temp) AS gpu_temp, "
        "AVG(cpu_temp) AS cpu_temp FROM metrics WHERE device_id=? AND ts>=? "
        "GROUP BY CAST((ts - ?) / ? AS INT) ORDER BY ts",
        (device_id, since, since, bucket)).fetchall()
    return [{"ts": r["ts"], "cpu_percent": r["cpu"], "mem_percent": r["mem"],
             "disk_percent": r["disk"], "gpu_percent": r["gpu"],
             "gpu_temp": r["gpu_temp"], "cpu_temp": r["cpu_temp"]} for r in rows]


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
# SNMP monitoring
# --------------------------------------------------------------------------- #
SNMP_READING_RETENTION = 30 * 24 * 3600   # keep ~30 days of readings


def _snmp_row(r) -> dict:
    d = dict(r)
    try:
        d["oids"] = json.loads(d["oids"]) if d.get("oids") else []
    except (ValueError, TypeError):
        d["oids"] = []
    return d


def add_snmp_target(org_id: str, node_id: str, host: str, *, name: str | None = None,
                    port: int = 161, version: str = "2c", community: str = "public",
                    oids: list | None = None, interval: int = 300,
                    enabled: bool = True) -> int:
    with write() as conn:
        cur = conn.execute(
            """INSERT INTO snmp_targets
               (org_id, node_id, name, host, port, version, community, oids, interval,
                enabled, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (org_id, node_id, name, host, int(port), str(version), community,
             json.dumps(oids or []), int(interval), 1 if enabled else 0, time.time()))
        return cur.lastrowid


def get_snmp_target(target_id: int) -> dict | None:
    row = get_conn().execute("SELECT * FROM snmp_targets WHERE id=?", (target_id,)).fetchone()
    return _snmp_row(row) if row else None


def list_snmp_targets(org_id: str) -> list[dict]:
    return [_snmp_row(r) for r in get_conn().execute(
        "SELECT * FROM snmp_targets WHERE org_id=? ORDER BY name, host", (org_id,)).fetchall()]


def list_snmp_targets_for_node(node_id: str, enabled_only: bool = True) -> list[dict]:
    q = "SELECT * FROM snmp_targets WHERE node_id=?"
    if enabled_only:
        q += " AND enabled=1"
    return [_snmp_row(r) for r in get_conn().execute(q, (node_id,)).fetchall()]


_SNMP_FIELDS = {"name", "host", "port", "version", "community", "oids", "interval", "enabled"}


def update_snmp_target(target_id: int, fields: dict) -> None:
    sets, vals = [], []
    for k, v in fields.items():
        if k not in _SNMP_FIELDS:
            continue
        if k == "oids":
            v = json.dumps(v or [])
        elif k == "enabled":
            v = 1 if v else 0
        elif k in ("port", "interval"):
            v = int(v)
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return
    vals.append(target_id)
    with write() as conn:
        conn.execute(f"UPDATE snmp_targets SET {', '.join(sets)} WHERE id=?", vals)


def delete_snmp_target(target_id: int) -> None:
    with write() as conn:
        conn.execute("DELETE FROM snmp_targets WHERE id=?", (target_id,))


def save_snmp_result(target_id: int, ok: bool, error: str | None,
                     readings: list[dict]) -> None:
    """Record a poll result: target status + one reading row per OID, pruning old."""
    now = time.time()
    with write() as conn:
        if not conn.execute("SELECT 1 FROM snmp_targets WHERE id=?", (target_id,)).fetchone():
            return
        conn.execute("UPDATE snmp_targets SET last_poll=?, last_ok=?, last_error=? WHERE id=?",
                     (now, 1 if ok else 0, error, target_id))
        for r in readings or []:
            num = r.get("num")
            if num is None and isinstance(r.get("value"), (int, float)) and not isinstance(r.get("value"), bool):
                num = r.get("value")
            val = r.get("value")
            conn.execute(
                """INSERT INTO snmp_readings (target_id, ts, oid, label, value_num, value_text, type)
                   VALUES (?,?,?,?,?,?,?)""",
                (target_id, now, r.get("oid"), r.get("label"),
                 float(num) if isinstance(num, (int, float)) else None,
                 None if val is None else str(val), r.get("type")))
        conn.execute("DELETE FROM snmp_readings WHERE target_id=? AND ts < ?",
                     (target_id, now - SNMP_READING_RETENTION))


def latest_snmp_readings(target_id: int) -> list[dict]:
    """Most recent reading per OID for a target."""
    rows = get_conn().execute(
        """SELECT r.* FROM snmp_readings r
           JOIN (SELECT oid, MAX(ts) AS mts FROM snmp_readings WHERE target_id=? GROUP BY oid) m
             ON r.oid = m.oid AND r.ts = m.mts
           WHERE r.target_id=? ORDER BY r.label, r.oid""",
        (target_id, target_id)).fetchall()
    return [dict(r) for r in rows]


def snmp_reading_series(target_id: int, oid: str, since: float) -> list[dict]:
    rows = get_conn().execute(
        """SELECT ts, value_num FROM snmp_readings
           WHERE target_id=? AND oid=? AND ts>=? AND value_num IS NOT NULL ORDER BY ts""",
        (target_id, oid, since)).fetchall()
    return [{"ts": r["ts"], "v": r["value_num"]} for r in rows]


# --------------------------------------------------------------------------- #
# UniFi cloud accounts
# --------------------------------------------------------------------------- #
def _unifi_row(r, redact: bool = True) -> dict:
    d = dict(r)
    if redact:
        d["key_set"] = bool(d.get("api_key"))
        d.pop("api_key", None)
    try:
        d["snapshot"] = json.loads(d["snapshot_json"]) if d.get("snapshot_json") else None
    except (ValueError, TypeError):
        d["snapshot"] = None
    d.pop("snapshot_json", None)
    try:
        d["host_ids"] = json.loads(d["host_ids_json"]) if d.get("host_ids_json") else []
    except (ValueError, TypeError):
        d["host_ids"] = []
    d.pop("host_ids_json", None)
    return d


def add_unifi_account(org_id: str, name: str | None, api_key: str, *,
                      interval: int = 300, enabled: bool = True,
                      host_ids: list | None = None) -> int:
    with write() as conn:
        cur = conn.execute(
            """INSERT INTO unifi_accounts (org_id, name, api_key, enabled, interval,
                   host_ids_json, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (org_id, name, api_key, 1 if enabled else 0, int(interval),
             json.dumps(host_ids or []), time.time()))
        return cur.lastrowid


def get_unifi_account(account_id: int, redact: bool = False) -> dict | None:
    row = get_conn().execute("SELECT * FROM unifi_accounts WHERE id=?", (account_id,)).fetchone()
    return _unifi_row(row, redact=redact) if row else None


def list_unifi_accounts(org_id: str) -> list[dict]:
    return [_unifi_row(r, redact=True) for r in get_conn().execute(
        "SELECT * FROM unifi_accounts WHERE org_id=? ORDER BY name, id", (org_id,)).fetchall()]


def list_unifi_accounts_all(enabled_only: bool = True) -> list[dict]:
    """All accounts with keys, for the server-side poller."""
    q = "SELECT * FROM unifi_accounts"
    if enabled_only:
        q += " WHERE enabled=1"
    return [_unifi_row(r, redact=False) for r in get_conn().execute(q).fetchall()]


_UNIFI_FIELDS = {"name", "enabled", "interval", "api_key", "host_ids"}


def update_unifi_account(account_id: int, fields: dict) -> None:
    sets, vals = [], []
    for k, v in fields.items():
        if k not in _UNIFI_FIELDS:
            continue
        col = k
        if k == "api_key":
            if not (v or "").strip():
                continue  # empty -> keep existing key
        elif k == "enabled":
            v = 1 if v else 0
        elif k == "interval":
            v = int(v)
        elif k == "host_ids":
            col = "host_ids_json"
            v = json.dumps(v or [])
        sets.append(f"{col}=?")
        vals.append(v)
    if not sets:
        return
    vals.append(account_id)
    with write() as conn:
        conn.execute(f"UPDATE unifi_accounts SET {', '.join(sets)} WHERE id=?", vals)


def delete_unifi_account(account_id: int) -> None:
    with write() as conn:
        conn.execute("DELETE FROM unifi_accounts WHERE id=?", (account_id,))
        # Clear live alert state for this account's synthetic devices
        # (unifi-<id>-<mac> / unifi-<id>-wan-<host>) so a deleted account can't
        # leave a stuck 'raised' row behind. The trailing '-' keeps id 5 from
        # matching id 50.
        conn.execute("DELETE FROM alert_state WHERE device_id LIKE ?", (f"unifi-{account_id}-%",))


def save_unifi_result(account_id: int, ok: bool, error: str | None,
                      snapshot: dict | None) -> None:
    """Store a poll result: status + (on success) the normalised snapshot."""
    now = time.time()
    with write() as conn:
        if not conn.execute("SELECT 1 FROM unifi_accounts WHERE id=?", (account_id,)).fetchone():
            return
        if snapshot is not None:
            conn.execute(
                "UPDATE unifi_accounts SET last_poll=?, last_ok=?, last_error=?, snapshot_json=? WHERE id=?",
                (now, 1 if ok else 0, error, json.dumps(snapshot), account_id))
        else:
            conn.execute(
                "UPDATE unifi_accounts SET last_poll=?, last_ok=?, last_error=? WHERE id=?",
                (now, 1 if ok else 0, error, account_id))


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


def list_raised_rule_alerts(org_id: str) -> list[dict]:
    """Monitor rules currently alerting on a device in this org (site or global rule)."""
    rows = get_conn().execute(
        """SELECT a.rule, a.since, d.id AS device_id, d.hostname, mr.name AS rule_name,
                  mr.severity AS severity
           FROM alert_state a
           JOIN devices d ON d.id = a.device_id
           JOIN monitor_rules mr ON 'rule:' || mr.id = a.rule
           WHERE a.state='raised' AND d.org_id=?""",
        (org_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Incidents (per-device resolved-issue history)
# --------------------------------------------------------------------------- #
def add_incident(device_id: str, org_id: str | None, rule_id: str | None, name: str,
                 metric: str | None, severity: str | None, detail: str | None,
                 opened_at: float, resolved_at: float | None) -> str:
    iid = uuid.uuid4().hex
    with write() as conn:
        conn.execute(
            """INSERT INTO incidents (id, device_id, org_id, rule_id, name, metric, severity,
                   detail, opened_at, resolved_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (iid, device_id, org_id, rule_id, name, metric, severity, detail,
             opened_at, resolved_at, _now()),
        )
    return iid


def list_incidents(device_id: str, limit: int = 100) -> list[dict]:
    """Resolved incident history for a device, most recently resolved first."""
    rows = get_conn().execute(
        """SELECT * FROM incidents WHERE device_id=?
           ORDER BY resolved_at DESC, opened_at DESC LIMIT ?""",
        (device_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def device_active_alerts(device_id: str) -> list[dict]:
    """Monitor-rule alerts currently raised on a device, with the rule's details."""
    rows = get_conn().execute(
        """SELECT a.since, mr.id AS rule_id, mr.name, mr.metric, mr.severity,
                  mr.threshold, mr.duration_minutes
           FROM alert_state a
           JOIN monitor_rules mr ON 'rule:' || mr.id = a.rule
           WHERE a.device_id=? AND a.state='raised'
           ORDER BY a.since DESC""",
        (device_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def prune_incidents(older_than_days: int = 90) -> None:
    cutoff = _now() - older_than_days * 86400
    with write() as conn:
        conn.execute("DELETE FROM incidents WHERE resolved_at IS NOT NULL AND resolved_at < ?", (cutoff,))
