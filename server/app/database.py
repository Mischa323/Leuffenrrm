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


def init_db() -> None:
    global _conn
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL;")
    _conn.execute("PRAGMA foreign_keys=ON;")
    _conn.executescript(SCHEMA)
    _conn.commit()


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


def list_orgs() -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM organizations ORDER BY name").fetchall()]


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
def upsert_device(org_id: str, dev: dict[str, Any]) -> dict:
    """Insert or update a device from an agent ``register`` payload."""
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
            inventory_json=json.dumps(inv), last_seen=now,
        )
        if existing:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE devices SET {sets} WHERE id=?",
                         (*fields.values(), dev["id"]))
        else:
            cols = ["id", "created_at", *fields.keys()]
            vals = [dev["id"], now, *fields.values()]
            conn.execute(
                f"INSERT INTO devices ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
                vals,
            )
    return get_device(dev["id"])


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
            "SELECT * FROM devices WHERE org_id=? AND group_id=? ORDER BY hostname",
            (org_id, group_id)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM devices WHERE org_id=? ORDER BY hostname", (org_id,)).fetchall()
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


def add_subnet(node_id: str, cidr: str, broadcast: str | None) -> None:
    with write() as conn:
        conn.execute("INSERT INTO subnets (node_id, cidr, broadcast) VALUES (?,?,?)",
                     (node_id, cidr, broadcast))


def delete_subnet(subnet_id: int) -> None:
    with write() as conn:
        conn.execute("DELETE FROM subnets WHERE id=?", (subnet_id,))


def upsert_network_hosts(org_id: str, node_id: str, hosts: list[dict]) -> None:
    now = _now()
    with write() as conn:
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
