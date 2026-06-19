"""Unified mailer: sends alert emails via SMTP or Microsoft Graph.

SMTP is used when ``SMTP_HOST`` is set; Graph is used when ``MS_TENANT_ID`` /
``MS_CLIENT_ID`` / ``MS_CLIENT_SECRET`` / ``GRAPH_SENDER`` are all set.
SMTP takes priority when both are configured. If neither is configured the call
is a no-op (logged at INFO level so dev setups stay quiet).
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import graph

log = logging.getLogger("rmm.mailer")


def _smtp_cfg() -> dict | None:
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": os.environ.get("SMTP_USER", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", "").strip(),
        "from": os.environ.get("SMTP_FROM", "").strip(),
        # starttls (default, port 587) | ssl (port 465) | none
        "tls": os.environ.get("SMTP_TLS", "starttls").lower(),
    }


def _send_smtp(cfg: dict, subject: str, html: str, recipients: list[str]) -> bool:
    sender = cfg["from"] or cfg["user"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))
    try:
        if cfg["tls"] == "ssl":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx) as s:
                if cfg["user"]:
                    s.login(cfg["user"], cfg["password"])
                s.sendmail(sender, recipients, msg.as_string())
        elif cfg["tls"] == "starttls":
            with smtplib.SMTP(cfg["host"], cfg["port"]) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
                if cfg["user"]:
                    s.login(cfg["user"], cfg["password"])
                s.sendmail(sender, recipients, msg.as_string())
        else:  # none
            with smtplib.SMTP(cfg["host"], cfg["port"]) as s:
                if cfg["user"]:
                    s.login(cfg["user"], cfg["password"])
                s.sendmail(sender, recipients, msg.as_string())
        log.info("SMTP sent '%s' to %s", subject, recipients)
        return True
    except Exception as exc:
        log.warning("SMTP send failed: %s", exc)
        return False


def send_mail(subject: str, html: str, recipients: list[str]) -> bool:
    if not recipients:
        return False
    smtp = _smtp_cfg()
    if smtp:
        return _send_smtp(smtp, subject, html, recipients)
    return graph.send_mail(subject, html, recipients)
