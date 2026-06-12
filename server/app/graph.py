"""Microsoft Graph mailer for monitoring alerts.

Sends mail with **application permission** (client-credentials) from a configured
service mailbox: ``POST /users/{GRAPH_SENDER}/sendMail``. Requires the app
registration to hold the ``Mail.Send`` application permission (admin-consented).

If Graph is not configured, :func:`send_mail` logs and returns ``False`` so the
rest of the system keeps working in dev/local setups.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("rmm.graph")

TENANT_ID = os.environ.get("MS_TENANT_ID", "")
CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
SENDER = os.environ.get("GRAPH_SENDER", "")

_GRAPH = "https://graph.microsoft.com/v1.0"


def _configured() -> bool:
    return bool(TENANT_ID and CLIENT_ID and CLIENT_SECRET and SENDER)


def _token() -> str | None:
    try:
        import msal
        app = msal.ConfidentialClientApplication(
            CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{TENANT_ID}",
            client_credential=CLIENT_SECRET,
        )
        res = app.acquire_token_for_client(["https://graph.microsoft.com/.default"])
        return res.get("access_token")
    except Exception as exc:  # pragma: no cover
        log.warning("Graph token error: %s", exc)
        return None


def send_mail(subject: str, html: str, recipients: list[str]) -> bool:
    if not recipients:
        return False
    if not _configured():
        log.info("Graph not configured; would email %s: %s", recipients, subject)
        return False
    token = _token()
    if not token:
        return False
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
        },
        "saveToSentItems": False,
    }
    try:
        r = httpx.post(
            f"{_GRAPH}/users/{SENDER}/sendMail",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=20,
        )
        if r.status_code >= 400:
            log.warning("Graph sendMail failed %s: %s", r.status_code, r.text)
            return False
        return True
    except Exception as exc:  # pragma: no cover
        log.warning("Graph sendMail error: %s", exc)
        return False
