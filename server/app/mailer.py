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
from html import escape as _esc

from . import graph

log = logging.getLogger("rmm.mailer")

# --------------------------------------------------------------------------- #
# Branded email template — mirrors the dashboard's dark look. Email clients
# need inline styles and concrete colours (no CSS vars / external sheets), so
# these hex values track the dashboard tokens in static/styles.css.
# --------------------------------------------------------------------------- #
_C = {
    "bg": "#0a0c11", "surface": "#12161e", "border": "#232b37",
    "text": "#e9eef6", "dim": "#97a3b4", "faint": "#5f6b7c",
    "accent": "#3b82f6", "accent_dark": "#1e3a8a",
    "good": "#34d399", "warn": "#fbbf24", "bad": "#f87171",
}
_FONT = "'Onest',-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"


def _brand_name() -> str:
    return os.environ.get("RMM_SERVER_NAME") or "Leuffen RMM"


def _wordmark() -> str:
    name = _brand_name()
    parts = name.rsplit(" ", 1)
    if len(parts) == 2:
        return (f'<span style="color:{_C["text"]}">{_esc(parts[0])}</span> '
                f'<span style="color:{_C["dim"]};font-weight:600">{_esc(parts[1])}</span>')
    return f'<span style="color:{_C["text"]}">{_esc(name)}</span>'


def button(url: str, label: str) -> str:
    """A dashboard-style primary button for use inside email bodies."""
    return (f'<a href="{_esc(url, quote=True)}" style="display:inline-block;'
            f'background:{_C["accent"]};color:#ffffff;text-decoration:none;'
            f'font-weight:600;font-size:14px;line-height:1;padding:12px 22px;'
            f'border-radius:10px;font-family:{_FONT}">{_esc(label)}</a>')


def status_block(title: str, body_html: str, kind: str = "warn") -> str:
    """A coloured status heading + body for alert-style emails.

    ``kind`` is one of ``good`` / ``warn`` / ``bad`` / ``info``."""
    color = {"good": _C["good"], "warn": _C["warn"], "bad": _C["bad"],
             "info": _C["accent"]}.get(kind, _C["warn"])
    dot = (f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;'
           f'background:{color};margin-right:8px;vertical-align:middle"></span>')
    return (f'<div style="font-size:16px;font-weight:700;color:{color};margin:0 0 10px">'
            f'{dot}{_esc(title)}</div>'
            f'<div style="color:{_C["text"]}">{body_html}</div>')


def shell(inner_html: str) -> str:
    """Wrap body content in the branded, dashboard-styled email frame."""
    if "<!doctype" in inner_html.lower() or "<html" in inner_html.lower():
        return inner_html  # already a full document — don't double-wrap
    logo_letter = _esc((_brand_name().strip() or "L")[0].upper())
    public_url = (os.environ.get("RMM_PUBLIC_URL") or "").rstrip("/")
    foot_link = (f' &middot; <a href="{_esc(public_url, quote=True)}" '
                 f'style="color:{_C["dim"]};text-decoration:none">Open dashboard</a>'
                 if public_url else "")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{_C['bg']};-webkit-text-size-adjust:100%">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_C['bg']};padding:32px 12px">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;background:{_C['surface']};border:1px solid {_C['border']};border-radius:16px;font-family:{_FONT}">
        <tr><td style="padding:24px 28px 16px">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td style="vertical-align:middle">
              <div style="width:30px;height:30px;border-radius:9px;background:{_C['accent']};background:linear-gradient(140deg,{_C['accent']},{_C['accent_dark']});color:#fff;font-weight:800;font-size:15px;text-align:center;line-height:30px">{logo_letter}</div>
            </td>
            <td style="vertical-align:middle;padding-left:10px;font-size:17px;font-weight:700">{_wordmark()}</td>
          </tr></table>
        </td></tr>
        <tr><td style="border-top:1px solid {_C['border']};padding:20px 28px;color:{_C['text']};font-size:14px;line-height:1.6">{inner_html}</td></tr>
        <tr><td style="border-top:1px solid {_C['border']};padding:14px 28px 20px;color:{_C['faint']};font-size:11.5px;line-height:1.5">Automated message from {_esc(_brand_name())}.{foot_link}</td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


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


def is_configured() -> bool:
    """True if either SMTP or Microsoft Graph is set up to deliver mail."""
    if _smtp_cfg():
        return True
    return bool(os.environ.get("MS_TENANT_ID") and os.environ.get("MS_CLIENT_ID")
                and os.environ.get("MS_CLIENT_SECRET") and os.environ.get("GRAPH_SENDER"))


def send_mail(subject: str, html: str, recipients: list[str]) -> bool:
    if not recipients:
        return False
    html = shell(html)  # apply the branded, dashboard-styled frame to every email
    smtp = _smtp_cfg()
    if smtp:
        return _send_smtp(smtp, subject, html, recipients)
    return graph.send_mail(subject, html, recipients)
