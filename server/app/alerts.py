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

import logging
import os
import time

from . import database as db, graph
from .manager import manager


def _default_recipients() -> list[str]:
    return [e.strip() for e in os.environ.get("RMM_ALERT_RECIPIENTS", "").split(",") if e.strip()]

log = logging.getLogger("rmm.alerts")

EMAIL_COOLDOWN = 3600  # seconds between repeat emails for a still-raised rule


def _avg_recent(metrics: list[dict], field: str, minutes: float) -> float | None:
    cutoff = time.time() - minutes * 60
    vals = [m[field] for m in metrics if m.get(field) is not None and m["ts"] >= cutoff]
    return sum(vals) / len(vals) if vals else None


def evaluate_once() -> None:
    now = time.time()
    online = manager.online_ids()
    for dev in db.all_devices():
        rules = db.list_effective_monitor_rules(dev)
        if not rules:
            continue
        recipients = db.alert_config(dev["org_id"]).get("recipients") or _default_recipients()
        metrics = db.get_metrics(dev["id"], limit=200)
        latest = metrics[-1] if metrics else None
        for rule in rules:
            rule_key = f"rule:{rule['id']}"
            if rule["metric"] == "offline":
                last_seen = dev.get("last_seen") or 0
                raised = (dev["id"] not in online) and (now - last_seen > rule["threshold"])
                _apply(dev, rule_key, raised, recipients,
                       f"{dev['hostname']}: {rule['name']}",
                       f"No heartbeat from <b>{dev['hostname']}</b> for over "
                       f"{int(rule['threshold'])}s.")
                continue
            if not latest:
                continue
            avg = _avg_recent(metrics, rule["metric"], rule["duration_minutes"] or 0)
            raised = avg is not None and avg >= rule["threshold"]
            _apply(dev, rule_key, raised, recipients,
                   f"{dev['hostname']}: {rule['name']}",
                   f"{rule['metric']} averaged {avg:.0f}% over {(rule['duration_minutes'] or 0):.0f} min "
                   f"(threshold {rule['threshold']:.0f}%)." if avg is not None else "")


def _apply(dev: dict, rule: str, raised: bool, recipients: list[str],
           subject: str, body: str) -> None:
    now = time.time()
    state = db.get_alert_state(dev["id"], rule)
    cur = state["state"] if state else "ok"
    if raised:
        if cur != "raised":
            db.set_alert_state(dev["id"], rule, "raised", now, now)
            log.info("ALERT raised: %s %s", dev["hostname"], rule)
            graph.send_mail(f"[RMM] {subject}", f"<p>{body}</p>", recipients)
        else:
            last = (state or {}).get("last_email") or 0
            if now - last > EMAIL_COOLDOWN:
                db.set_alert_state(dev["id"], rule, "raised", state.get("since"), now)
                graph.send_mail(f"[RMM] {subject} (still active)", f"<p>{body}</p>", recipients)
    else:
        if cur == "raised":
            db.set_alert_state(dev["id"], rule, "ok", None, None)
            log.info("ALERT cleared: %s %s", dev["hostname"], rule)
            graph.send_mail(f"[RMM] Resolved: {subject}",
                            f"<p>{dev['hostname']} {rule} has returned to normal.</p>",
                            recipients)
