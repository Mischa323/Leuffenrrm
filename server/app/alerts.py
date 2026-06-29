"""Background alert evaluator.

Runs on an interval, evaluating each device against its **monitor rules** —
ordinary, user-managed records in :func:`database.list_effective_monitor_rules`
(site rules scoped to one organisation, plus global rules that apply to every
device everywhere). Each rule says "alert when `metric` has averaged at least
`threshold` over the last `duration_minutes`" (or, for the 'offline' metric,
"alert when the device hasn't been seen for `threshold` seconds"). A per
device/per-rule state machine with cooldown emails once on raise and once on
clear (no spam). Recipients come from the device's own org alert config.
Email goes out via :mod:`graph`.
"""
from __future__ import annotations

import json
import logging
import os
import time
from html import escape as _esc

from . import database as db, mailer
from .manager import manager


def _default_recipients() -> list[str]:
    return [e.strip() for e in os.environ.get("RMM_ALERT_RECIPIENTS", "").split(",") if e.strip()]

log = logging.getLogger("rmm.alerts")

EMAIL_COOLDOWN = 3600  # seconds between repeat emails for a still-raised rule

# Synology Active Backup health (not rule-driven — evaluated whenever a device
# reports backup data). "Stale" = a scheduled task that has run before but has no
# successful backup within the window; recency is a reliable signal. The SaaS
# (M365 / Google) status enum is still provisional in the field, so status-based
# SaaS alerting is opt-in to avoid false-positive emails until it's confirmed.
BACKUP_STALE_HOURS = float(os.environ.get("RMM_BACKUP_STALE_HOURS", "48"))
BACKUP_SAAS_ALERTS = os.environ.get("RMM_BACKUP_SAAS_ALERTS", "0").lower() not in ("", "0", "false", "no", "off")
BACKUP_SAAS_OK_STATUS = 3  # observed "healthy" code; anything else is flagged when SaaS alerts are on


def _fmt_ago(ts: float | None) -> str:
    if not ts:
        return "never"
    h = max(0.0, (time.time() - ts) / 3600)
    return f"{h:.0f}h ago" if h < 48 else f"{h / 24:.0f}d ago"


def _avg_recent(metrics: list[dict], field: str, minutes: float) -> float | None:
    cutoff = time.time() - minutes * 60
    vals = [m[field] for m in metrics if m.get(field) is not None and m["ts"] >= cutoff]
    return sum(vals) / len(vals) if vals else None


def evaluate_once() -> None:
    now = time.time()
    online = manager.online_ids()
    for dev in db.all_devices():
        rules = db.list_effective_monitor_rules(dev)
        has_backups = bool(dev.get("backups_json"))
        if not rules and not has_backups:
            continue
        recipients = db.alert_config(dev["org_id"]).get("recipients") or _default_recipients()
        # Backup health is not rule-driven — evaluate it for any device reporting it.
        if has_backups:
            _evaluate_backups(dev, recipients, now)
        if not rules:
            continue
        metrics = db.get_metrics(dev["id"], limit=200)
        latest = metrics[-1] if metrics else None
        for rule in rules:
            if rule["metric"] == "wol":
                continue  # config policy, not an alerting monitor
            rule_key = f"rule:{rule['id']}"
            notify = bool(rule.get("notify_email", 1))
            severity = rule.get("severity") or "warning"
            if rule["metric"] == "offline":
                last_seen = dev.get("last_seen") or 0
                raised = (dev["id"] not in online) and (now - last_seen > rule["threshold"])
                meta = {"id": rule["id"], "name": rule["name"], "metric": rule["metric"],
                        "detail": f"No heartbeat for {int(rule['threshold'])}s"}
                _apply(dev, rule_key, raised, recipients,
                       f"{dev['hostname']}: {rule['name']}",
                       f"No heartbeat from <b>{dev['hostname']}</b> for over "
                       f"{int(rule['threshold'])}s.", notify, severity, meta)
                continue
            if not latest:
                continue
            avg = _avg_recent(metrics, rule["metric"], rule["duration_minutes"] or 0)
            raised = avg is not None and avg >= rule["threshold"]
            unit = "°C" if rule["metric"].endswith("_temp") else "%"
            label = rule["metric"].replace("_percent", "").replace("_temp", " temp")
            dur = (rule["duration_minutes"] or 0)
            meta = {"id": rule["id"], "name": rule["name"], "metric": rule["metric"],
                    "detail": f"{label} ≥ {rule['threshold']:.0f}{unit} for {dur:.0f} min"}
            _apply(dev, rule_key, raised, recipients,
                   f"{dev['hostname']}: {rule['name']}",
                   f"{rule['metric']} averaged {avg:.0f}{unit} over {dur:.0f} min "
                   f"(threshold {rule['threshold']:.0f}{unit})." if avg is not None else "",
                   notify, severity, meta)


def _evaluate_backups(dev: dict, recipients: list[str], now: float) -> None:
    """Raise/clear alerts from a device's Synology Active Backup snapshot.

    Reuses the same state machine as monitor rules (cooldown, raise/clear emails,
    incident on clear) via :func:`_apply`, with synthetic rule keys per task and a
    ``None`` rule id."""
    try:
        bk = json.loads(dev.get("backups_json") or "")
    except (ValueError, TypeError):
        return
    if not isinstance(bk, dict):
        return
    host = _esc(dev.get("hostname") or dev.get("id") or "device")
    stale_secs = BACKUP_STALE_HOURS * 3600

    # Active Backup for Business (computers & servers): stale when a scheduled
    # task that has run before has no successful backup within the window.
    for t in (bk.get("business") or {}).get("tasks") or []:
        if not isinstance(t, dict) or not t.get("versions") or not t.get("scheduled"):
            continue  # never run, or manual/unscheduled — don't nag
        name = t.get("name") or "task"
        last = t.get("last_backup") or 0
        raised = (now - last) > stale_secs
        _apply(dev, f"backup_stale:{name}", raised, recipients,
               f"{dev.get('hostname')}: backup stale — {name}",
               f"Active Backup task <b>{_esc(name)}</b> on <b>{host}</b> has no successful "
               f"backup in over {BACKUP_STALE_HOURS:.0f}h (last success {_fmt_ago(last)}).",
               True, "warning",
               {"id": None, "name": f"Backup stale: {name}", "metric": "backup_stale",
                "detail": f"Last success {_fmt_ago(last)}"})

    # M365 / Google Workspace: status-based, opt-in (enum still provisional).
    if not BACKUP_SAAS_ALERTS:
        return
    for grp, label in (("microsoft365", "Microsoft 365"), ("google", "Google Workspace")):
        for t in (bk.get(grp) or {}).get("tasks") or []:
            if not isinstance(t, dict):
                continue
            name = t.get("name") or "task"
            status = t.get("status")
            raised = status is not None and status != BACKUP_SAAS_OK_STATUS
            _apply(dev, f"backup_saas:{grp}:{name}", raised, recipients,
                   f"{dev.get('hostname')}: {label} backup issue — {name}",
                   f"{label} Active Backup task <b>{_esc(name)}</b> on <b>{host}</b> reports "
                   f"status {status} (expected healthy).",
                   True, "warning",
                   {"id": None, "name": f"{label} backup: {name}", "metric": "backup_failed",
                    "detail": f"status {status}"})


def _apply(dev: dict, rule: str, raised: bool, recipients: list[str],
           subject: str, body: str, notify: bool = True, severity: str = "warning",
           meta: dict | None = None) -> None:
    now = time.time()
    state = db.get_alert_state(dev["id"], rule)
    cur = state["state"] if state else "ok"
    tag = f"[{severity.upper()}] " if severity != "info" else ""
    kind = "bad" if severity in ("critical", "error") else ("info" if severity == "info" else "warn")
    if raised:
        if cur != "raised":
            db.set_alert_state(dev["id"], rule, "raised", now, now)
            log.info("ALERT raised: %s %s", dev["hostname"], rule)
            if notify:
                mailer.send_mail(f"[RMM] {tag}{subject}",
                                 mailer.status_block(subject, f"<p style='margin:0'>{body}</p>", kind),
                                 recipients)
        else:
            last = (state or {}).get("last_email") or 0
            if now - last > EMAIL_COOLDOWN:
                db.set_alert_state(dev["id"], rule, "raised", state.get("since"), now)
                if notify:
                    mailer.send_mail(f"[RMM] {tag}{subject} (still active)",
                                     mailer.status_block(f"{subject} (still active)",
                                                         f"<p style='margin:0'>{body}</p>", kind),
                                     recipients)
    else:
        if cur == "raised":
            db.set_alert_state(dev["id"], rule, "ok", None, None)
            log.info("ALERT cleared: %s %s", dev["hostname"], rule)
            if meta:
                db.add_incident(dev["id"], dev.get("org_id"), meta.get("id"),
                                meta.get("name") or subject, meta.get("metric"),
                                severity, meta.get("detail"),
                                (state or {}).get("since") or now, now)
            if notify:
                mailer.send_mail(f"[RMM] Resolved: {subject}",
                                 mailer.status_block(f"Resolved: {subject}",
                                                     f"<p style='margin:0'>{dev['hostname']} {rule} has returned to normal.</p>",
                                                     "good"),
                                 recipients)
