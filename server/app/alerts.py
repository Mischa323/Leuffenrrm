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


def _service_state(dev: dict, name: str) -> str | None:
    """Reported status of a named service on a device (lowercased), or None when
    it's not in the device's last service report."""
    try:
        svcs = json.loads(dev.get("services_json") or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(svcs, list):
        return None
    lname = name.lower()
    for s in svcs:
        if isinstance(s, dict) and (s.get("name") or "").lower() == lname:
            return (s.get("status") or "").lower() or None
    return None


def _json_list(s) -> list | None:
    try:
        v = json.loads(s or "")
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, list) else None


def _json_obj(s) -> dict | None:
    try:
        v = json.loads(s or "")
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, dict) else None


# Explicit "bad" markers — anything a disk reports that isn't one of these is
# treated as healthy, so an unfamiliar/OK status never raises a false alarm.
# (Substring match, but careful: "healthy" is a substring of "unhealthy".)
_DISK_BAD = ("unhealthy", "warn", "fail", "critical", "bad", "predict", "error", "degrad")


def _disk_ok(d: dict) -> bool:
    """Whether a reported disk is healthy. Only explicit failure markers raise;
    unknown/blank status is treated as OK (don't alarm on missing data)."""
    status = (d.get("status") or d.get("health") or "").strip().lower()
    if not status:
        return True
    return not any(bad in status for bad in _DISK_BAD)


def evaluate_once() -> list[dict]:
    """Evaluate every rule against every device. Returns auto-remediation triggers
    — newly-raised alerts whose rule has a remediation script, on online devices —
    for the caller (the async alert loop) to actually run."""
    now = time.time()
    online = manager.online_ids()
    remediations: list[dict] = []
    for dev in db.all_devices():
        rules = db.list_effective_monitor_rules(dev)
        if not rules:
            continue
        recipients = db.alert_config(dev["org_id"]).get("recipients") or _default_recipients()
        prev_raised = db.raised_alert_keys(dev["id"])
        # Backup health is rule-driven: evaluated only when a "backup" monitor
        # policy is enabled for this device (off by default — opt in per NAS/org).
        backup_rule = next((r for r in rules if r["metric"] == "backup"), None)
        if backup_rule and dev.get("backups_json"):
            _evaluate_backups(dev, recipients, now, backup_rule)
        metrics = db.get_metrics(dev["id"], limit=200)
        latest = metrics[-1] if metrics else None
        for rule in rules:
            if rule["metric"] in ("wol", "backup"):
                continue  # wol = config policy; backup handled above
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
            if rule["metric"].startswith("service:"):
                # Alert when the named service isn't running on this device. Only
                # evaluate while the device is online (a stale list from an offline
                # device shouldn't flap; the offline monitor covers that case).
                if dev["id"] not in online:
                    continue
                svc_name = rule["metric"].split(":", 1)[1]
                state = _service_state(dev, svc_name)
                raised = state is not None and state != "running"
                meta = {"id": rule["id"], "name": rule["name"], "metric": rule["metric"],
                        "detail": f"Service '{svc_name}' is {state or 'unknown'}"}
                _apply(dev, rule_key, raised, recipients,
                       f"{dev['hostname']}: {rule['name']}",
                       f"Service <b>{_esc(svc_name)}</b> on <b>{_esc(dev['hostname'])}</b> "
                       f"is <b>{_esc(state or 'not reported')}</b> (expected running).",
                       notify, severity, meta)
                continue
            if rule["metric"] == "disk_health":
                disks = _json_list(dev.get("disk_health_json")) or []
                bad = [d for d in disks if isinstance(d, dict) and not _disk_ok(d)]
                raised = bool(bad)
                names = ", ".join(f"{d.get('name')} ({d.get('status') or d.get('health')})" for d in bad[:4])
                meta = {"id": rule["id"], "name": rule["name"], "metric": rule["metric"],
                        "detail": names or "all disks healthy"}
                _apply(dev, rule_key, raised, recipients,
                       f"{dev['hostname']}: {rule['name']}",
                       f"Disk health warning on <b>{_esc(dev['hostname'])}</b>: {_esc(names)}.",
                       notify, severity, meta)
                continue
            if rule["metric"] == "reboot_pending":
                raised = bool(dev.get("reboot_pending"))
                meta = {"id": rule["id"], "name": rule["name"], "metric": rule["metric"],
                        "detail": "Reboot required" if raised else "Up to date"}
                _apply(dev, rule_key, raised, recipients,
                       f"{dev['hostname']}: {rule['name']}",
                       f"<b>{_esc(dev['hostname'])}</b> is waiting on a reboot to finish applying updates.",
                       notify, severity, meta)
                continue
            if rule["metric"] == "av_health":
                av = (_json_obj(dev.get("security_json")) or {}).get("av") or {}
                reasons = []
                if av.get("realtime") is False:
                    reasons.append("real-time protection is off")
                if av.get("threats"):
                    reasons.append(f"{int(av['threats'])} active threat(s)")
                age = av.get("sig_age_days")
                if isinstance(age, (int, float)) and age > (rule["threshold"] or 7):
                    reasons.append(f"definitions {int(age)} days old")
                raised = bool(av) and bool(reasons)
                meta = {"id": rule["id"], "name": rule["name"], "metric": rule["metric"],
                        "detail": "; ".join(reasons) or "healthy"}
                _apply(dev, rule_key, raised, recipients,
                       f"{dev['hostname']}: {rule['name']}",
                       f"Antivirus health issue on <b>{_esc(dev['hostname'])}</b>: "
                       f"{_esc('; '.join(reasons))}.", notify, severity, meta)
                continue
            if rule["metric"] == "firewall":
                fw = (_json_obj(dev.get("security_json")) or {}).get("firewall") or {}
                off = [p for p, v in fw.items() if v is False]
                raised = bool(off)
                meta = {"id": rule["id"], "name": rule["name"], "metric": rule["metric"],
                        "detail": ("off: " + ", ".join(off)) if off else "all profiles on"}
                _apply(dev, rule_key, raised, recipients,
                       f"{dev['hostname']}: {rule['name']}",
                       f"Windows Firewall is disabled on <b>{_esc(dev['hostname'])}</b> "
                       f"(profiles: {_esc(', '.join(off))}).", notify, severity, meta)
                continue
            if rule["metric"] == "bitlocker":
                sec = _json_obj(dev.get("security_json"))
                raised = sec is not None and sec.get("bitlocker_system") is False
                meta = {"id": rule["id"], "name": rule["name"], "metric": rule["metric"],
                        "detail": "system drive unprotected" if raised else "protected"}
                _apply(dev, rule_key, raised, recipients,
                       f"{dev['hostname']}: {rule['name']}",
                       f"The system drive on <b>{_esc(dev['hostname'])}</b> is not protected by "
                       f"BitLocker.", notify, severity, meta)
                continue
            if rule["metric"] == "failed_logons":
                n = (_json_obj(dev.get("security_json")) or {}).get("failed_logons_15m")
                raised = isinstance(n, (int, float)) and n >= rule["threshold"]
                meta = {"id": rule["id"], "name": rule["name"], "metric": rule["metric"],
                        "detail": f"{int(n)} failed logons/15m" if isinstance(n, (int, float)) else "no data"}
                _apply(dev, rule_key, raised, recipients,
                       f"{dev['hostname']}: {rule['name']}",
                       f"<b>{int(n) if isinstance(n, (int, float)) else 0}</b> failed sign-ins on "
                       f"<b>{_esc(dev['hostname'])}</b> in the last 15 minutes "
                       f"(threshold {int(rule['threshold'])}).", notify, severity, meta)
                continue
            if rule["metric"] == "uptime":
                up = latest.get("uptime") if latest else None
                days = (up / 86400.0) if up else None
                raised = days is not None and days >= rule["threshold"]
                meta = {"id": rule["id"], "name": rule["name"], "metric": rule["metric"],
                        "detail": f"up {days:.1f}d" if days is not None else "unknown"}
                _apply(dev, rule_key, raised, recipients,
                       f"{dev['hostname']}: {rule['name']}",
                       (f"<b>{_esc(dev['hostname'])}</b> has been up for {days:.0f} days "
                        f"(threshold {int(rule['threshold'])}d) — a reboot is due."
                        if days is not None else ""), notify, severity, meta)
                continue
            if rule["metric"].startswith("process:"):
                if dev["id"] not in online:
                    continue
                pname = rule["metric"].split(":", 1)[1].strip().lower()
                procs = _json_list(dev.get("processes_json"))
                if not procs:
                    continue  # no process data yet — don't alert
                raised = pname not in {str(p).strip().lower() for p in procs}
                meta = {"id": rule["id"], "name": rule["name"], "metric": rule["metric"],
                        "detail": f"'{pname}' " + ("not running" if raised else "running")}
                _apply(dev, rule_key, raised, recipients,
                       f"{dev['hostname']}: {rule['name']}",
                       f"Process <b>{_esc(pname)}</b> is <b>not running</b> on "
                       f"<b>{_esc(dev['hostname'])}</b>.", notify, severity, meta)
                continue
            if rule["metric"].startswith("eventlog:"):
                events = _json_list(dev.get("events_json"))
                if events is None:
                    continue  # no event data yet
                parts = rule["metric"].split(":", 3)
                log = parts[1] if len(parts) > 1 else "system"
                level = parts[2] if len(parts) > 2 else "error"
                ids = parts[3] if len(parts) > 3 else ""
                id_set = {int(x) for x in ids.split(",") if x.strip().isdigit()}
                want_crit = level == "critical"
                matches = []
                for e in events:
                    if not isinstance(e, dict):
                        continue
                    if log != "both" and (e.get("log", "").lower() != log):
                        continue
                    if want_crit and "crit" not in (e.get("level", "").lower()):
                        continue
                    if id_set and e.get("id") not in id_set:
                        continue
                    matches.append(e)
                raised = bool(matches)
                sample = matches[0] if matches else {}
                meta = {"id": rule["id"], "name": rule["name"], "metric": rule["metric"],
                        "detail": (f"{len(matches)} event(s); e.g. {sample.get('source')} #{sample.get('id')}"
                                   if matches else "no matching events")}
                _apply(dev, rule_key, raised, recipients,
                       f"{dev['hostname']}: {rule['name']}",
                       (f"{len(matches)} matching event-log error(s) on <b>{_esc(dev['hostname'])}</b>"
                        + (f" — e.g. <b>{_esc(sample.get('source') or '')}</b> event {sample.get('id')}: "
                           f"{_esc((sample.get('msg') or '')[:160])}" if matches else "") + "."),
                       notify, severity, meta)
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
        # Auto-remediation: for each rule that just flipped to 'raised' this cycle
        # and carries a remediation script, queue a run on the (online) device.
        # Backup rules use 'backup_*' alert keys; every other rule uses 'rule:<id>'.
        newly = db.raised_alert_keys(dev["id"]) - prev_raised
        if newly and dev["id"] in online:
            for rule in rules:
                sid = rule.get("remediation_script_id")
                if not sid:
                    continue
                hit = (any(k.startswith("backup_") for k in newly)
                       if rule["metric"] == "backup" else f"rule:{rule['id']}" in newly)
                if hit:
                    remediations.append({"device_id": dev["id"], "script_id": sid,
                                         "rule_name": rule["name"]})
    return remediations


def _evaluate_backups(dev: dict, recipients: list[str], now: float, rule: dict) -> None:
    """Raise/clear alerts from a device's Synology Active Backup snapshot.

    Driven by an enabled "backup" monitor rule: the rule's threshold is the stale
    window (hours) and its severity / notify flag are used for the alerts. Reuses
    the same state machine as monitor rules (cooldown, raise/clear emails, incident
    on clear) via :func:`_apply`, with synthetic per-task rule keys."""
    try:
        bk = json.loads(dev.get("backups_json") or "")
    except (ValueError, TypeError):
        return
    if not isinstance(bk, dict):
        return
    host = _esc(dev.get("hostname") or dev.get("id") or "device")
    stale_hours = float(rule.get("threshold") or BACKUP_STALE_HOURS)
    stale_secs = stale_hours * 3600
    severity = rule.get("severity") or "warning"
    notify = bool(rule.get("notify_email", 1))
    rid = rule.get("id")

    # Active Backup for Business (computers & servers). Two independent checks:
    #  (a) FAILED — the most recent run finished with a non-success status
    #      (last_status 3 = completed). Applies to any task (manual or scheduled);
    #      a recent failure is the most important signal.
    #  (b) STALE — a *scheduled* task has had no activity within the window (its
    #      schedule implies it should be running regularly). Manual tasks have no
    #      schedule, so "stale" is undefined for them and would just be noise.
    for t in (bk.get("business") or {}).get("tasks") or []:
        if not isinstance(t, dict) or not t.get("versions"):
            continue  # never run — nothing to assess
        name = t.get("name") or "task"
        last = t.get("last_backup") or 0
        running = bool(t.get("running"))
        status = t.get("last_status")
        failed = (not running) and status is not None and status != 3
        _apply(dev, f"backup_failed:{name}", failed, recipients,
               f"{dev.get('hostname')}: backup FAILED — {name}",
               f"Active Backup task <b>{_esc(name)}</b> on <b>{host}</b>: the last run did not "
               f"complete successfully (status {status}, {_fmt_ago(last)}).",
               notify, severity,
               {"id": rid, "name": f"Backup failed: {name}", "metric": "backup_failed",
                "detail": f"status {status} ({_fmt_ago(last)})"})
        # Stale only when scheduled and not already flagged as failed/running.
        stale = bool(t.get("scheduled")) and not running and not failed and (now - last) > stale_secs
        _apply(dev, f"backup_stale:{name}", stale, recipients,
               f"{dev.get('hostname')}: backup stale — {name}",
               f"Active Backup task <b>{_esc(name)}</b> on <b>{host}</b> has had no backup "
               f"activity in over {stale_hours:.0f}h (last {_fmt_ago(last)}).",
               notify, severity,
               {"id": rid, "name": f"Backup stale: {name}", "metric": "backup_stale",
                "detail": f"Last activity {_fmt_ago(last)}"})

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
                   notify, severity,
                   {"id": rid, "name": f"{label} backup: {name}", "metric": "backup_failed",
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


# --------------------------------------------------------------------------- #
# UniFi (cloud) — device-offline + WAN-down. Server-polled, not agent/rule driven.
# Evaluated right after a poll; keyed by synthetic device ids so it reuses the same
# _apply state machine (alert_state/incidents carry no FK to real devices).
# --------------------------------------------------------------------------- #
def evaluate_unifi_account(acct: dict, snapshot: dict | None) -> None:
    """Raise/clear alerts for one UniFi account's latest snapshot.

    Skips entirely when the poll failed (``snapshot`` falsy or ``ok`` False) so a
    transient API/outage doesn't flap every device offline. A UniFi device is a
    synthetic alert subject ``unifi-<acct>-<mac>``; WAN health is per host.
    """
    if not snapshot or not snapshot.get("ok"):
        return
    org_id = acct.get("org_id")
    acct_id = acct.get("id")
    acct_name = acct.get("name") or "UniFi"
    recipients = db.alert_config(org_id).get("recipients") or _default_recipients()

    for d in snapshot.get("devices") or []:
        mac = d.get("mac")
        if not mac:
            continue
        dname = d.get("name") or mac
        dev = {"id": f"unifi-{acct_id}-{mac}", "hostname": dname, "org_id": org_id}
        raised = d.get("state") == "offline"
        model = d.get("model") or d.get("type") or "device"
        meta = {"id": None, "name": f"UniFi device offline: {dname}",
                "metric": "unifi_offline", "detail": f"{model} ({mac}) is offline"}
        _apply(dev, f"unifi_offline:{mac}", raised, recipients,
               f"{acct_name}: {dname} offline",
               f"UniFi device <b>{_esc(dname)}</b> (<span>{_esc(str(model))}</span>, "
               f"{_esc(mac)}) is <b>offline</b>.",
               True, "warning", meta)

    for w in snapshot.get("isp") or []:
        hid = w.get("host_id")
        if not hid:
            continue
        hname = w.get("host_name") or hid
        dev = {"id": f"unifi-{acct_id}-wan-{hid}", "hostname": hname, "org_id": org_id}
        raised = w.get("status") == "offline"
        meta = {"id": None, "name": f"WAN down: {hname}", "metric": "unifi_wan_down",
                "detail": f"WAN/ISP link down on {hname}"}
        _apply(dev, f"unifi_wan_down:{hid}", raised, recipients,
               f"{acct_name}: WAN down at {hname}",
               f"The internet (WAN) link on <b>{_esc(hname)}</b> is <b>down</b>.",
               True, "critical", meta)
