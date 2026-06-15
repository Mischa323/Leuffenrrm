"""Background alert evaluator.

Runs on an interval, evaluating each device against its **effective monitoring
policy** (device → group → org standard) for: offline, sustained high CPU, low
disk, sustained high memory. A per-device/per-rule state machine with cooldown
emails once on raise and once on clear (no spam). Recipients come from the org's
alerting standard. Email goes out via :mod:`graph`.
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
        policy = db.get_effective_policy(dev)
        cfg = db.alert_config(dev["org_id"])
        recipients = cfg.get("recipients") or _default_recipients()
        rules_enabled = set(cfg.get("rules") or ["offline", "cpu", "disk", "mem"])
        metrics = db.get_metrics(dev["id"], limit=200)
        latest = metrics[-1] if metrics else None

        # offline
        if "offline" in rules_enabled:
            last_seen = dev.get("last_seen") or 0
            offline = (dev["id"] not in online) and (now - last_seen > policy["offline_after"])
            _apply(dev, "offline", offline, recipients,
                   f"{dev['hostname']} is offline",
                   f"No heartbeat from <b>{dev['hostname']}</b> for over "
                   f"{int(policy['offline_after'])}s.")

        if latest:
            if "cpu" in rules_enabled:
                avg = _avg_recent(metrics, "cpu_percent", policy["cpu_minutes"])
                raised = avg is not None and avg >= policy["cpu_pct"]
                _apply(dev, "cpu", raised, recipients,
                       f"High CPU on {dev['hostname']}",
                       f"CPU averaged {avg:.0f}% over {policy['cpu_minutes']:.0f} min "
                       f"(threshold {policy['cpu_pct']:.0f}%)." if avg else "")
            if "mem" in rules_enabled:
                avg = _avg_recent(metrics, "mem_percent", policy["mem_minutes"])
                raised = avg is not None and avg >= policy["mem_pct"]
                _apply(dev, "mem", raised, recipients,
                       f"High memory on {dev['hostname']}",
                       f"Memory averaged {avg:.0f}% over {policy['mem_minutes']:.0f} min "
                       f"(threshold {policy['mem_pct']:.0f}%)." if avg else "")
            if "disk" in rules_enabled and latest.get("disk_percent") is not None:
                free = 100 - latest["disk_percent"]
                raised = free <= policy["disk_free_pct"]
                _apply(dev, "disk", raised, recipients,
                       f"Low disk on {dev['hostname']}",
                       f"Only {free:.0f}% disk free (threshold {policy['disk_free_pct']:.0f}%).")


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
