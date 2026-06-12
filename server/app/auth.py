"""Authentication: Microsoft Entra / Office 365 SSO (tenant-only) with a dev fallback.

Real SSO uses MSAL's confidential-client OIDC auth-code flow against a single
tenant (so only that tenant's users can sign in). A signed session cookie carries
the user's email after login.

If MS_* is not configured (or ``RMM_DEV_AUTH=1``), the server runs in **dev auth**
mode and signs in a bootstrap admin automatically — so the app is runnable and
verifiable without an Azure app registration.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer

from . import database as db

TENANT_ID = os.environ.get("MS_TENANT_ID", "")
CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("MS_REDIRECT_URI", "http://localhost:8000/auth/callback")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-insecure-secret-change-me")
COOKIE = "rmm_session"
SCOPES = ["User.Read"]

# Comma-separated list of bootstrap/global-admin emails.
BOOTSTRAP_ADMINS = {
    e.strip().lower() for e in os.environ.get("RMM_BOOTSTRAP_ADMIN", "").split(",") if e.strip()
}

DEV_AUTH = os.environ.get("RMM_DEV_AUTH", "").lower() in ("1", "true", "yes") or not CLIENT_ID
DEV_USER = (next(iter(BOOTSTRAP_ADMINS)) if BOOTSTRAP_ADMINS else "admin@localhost")

_serializer = URLSafeSerializer(SESSION_SECRET, salt="rmm-session")


def _msal_app():
    import msal
    authority = f"https://login.microsoftonline.com/{TENANT_ID}"
    return msal.ConfidentialClientApplication(
        CLIENT_ID, authority=authority, client_credential=CLIENT_SECRET
    )


def login_url(state: str) -> str:
    """Return the Microsoft authorize URL (real mode)."""
    return _msal_app().get_authorization_request_url(
        SCOPES, state=state, redirect_uri=REDIRECT_URI
    )


def exchange_code(code: str) -> str:
    """Exchange an auth code for tokens; return the signed-in user's email."""
    result = _msal_app().acquire_token_by_authorization_code(
        code, scopes=SCOPES, redirect_uri=REDIRECT_URI
    )
    if "error" in result:
        raise HTTPException(status_code=401, detail=result.get("error_description", "auth failed"))
    claims = result.get("id_token_claims", {})
    email = (claims.get("preferred_username") or claims.get("email") or "").lower()
    if not email:
        raise HTTPException(status_code=401, detail="No email in token")
    return email


def make_cookie(email: str) -> str:
    return _serializer.dumps({"email": email})


def read_cookie(value: str) -> dict | None:
    try:
        return _serializer.loads(value)
    except BadSignature:
        return None


def is_global_admin(email: str) -> bool:
    return email.lower() in BOOTSTRAP_ADMINS


def current_user(request: Request) -> dict:
    """FastAPI dependency: resolve the signed-in user or 401."""
    if DEV_AUTH:
        email = DEV_USER
    else:
        raw = request.cookies.get(COOKIE)
        data = read_cookie(raw) if raw else None
        if not data:
            raise HTTPException(status_code=401, detail="Not authenticated")
        email = data["email"]
    return {"email": email, "is_global_admin": is_global_admin(email)}


def require_org(user: dict, org_id: str) -> str:
    """Ensure the user may act in ``org_id``; return their role."""
    if user["is_global_admin"]:
        return "admin"
    role = db.user_role(user["email"], org_id)
    if role is None:
        raise HTTPException(status_code=403, detail="No access to this organisation")
    return role
