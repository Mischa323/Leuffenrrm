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

# Sign-in mode — only two are offered now:
#   hybrid (default) — local password accounts + optional Microsoft 365 SSO
#   dev              — auto-login a bootstrap admin (evaluation only)
# Legacy values (sso/local) fold into hybrid.
_explicit_mode = os.environ.get("RMM_AUTH_MODE", "").lower()
if os.environ.get("RMM_DEV_AUTH", "").lower() in ("1", "true", "yes") or _explicit_mode == "dev":
    AUTH_MODE = "dev"
else:
    AUTH_MODE = "hybrid"

DEV_AUTH = AUTH_MODE == "dev"
LOCAL_ENABLED = AUTH_MODE == "hybrid"
SSO_ENABLED = AUTH_MODE == "hybrid" and bool(CLIENT_ID)

# Safety: a hybrid server with neither local accounts nor SSO configured would
# lock everyone out — fall back to dev login until it's set up.
if AUTH_MODE == "hybrid" and not SSO_ENABLED:
    try:
        _has_users = db.get_conn().execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    except Exception:
        _has_users = False
    if not _has_users:
        AUTH_MODE, DEV_AUTH, LOCAL_ENABLED = "dev", True, False

DEV_USER = (next(iter(BOOTSTRAP_ADMINS)) if BOOTSTRAP_ADMINS else "admin@localhost")


def resolve_sso_identity(email: str) -> str:
    """Map a Microsoft 365 email onto a local account (by email) when one exists,
    so the same person has one identity and the local account's admin rights."""
    u = db.get_user_by_email(email)
    return u["username"] if u else email.lower()

# Mark session cookies Secure unless explicitly disabled (TLS is on by default).
# Auto-off for plain-HTTP proxy mode without TLS termination.
SECURE_COOKIES = os.environ.get("RMM_SECURE_COOKIES",
                                "0" if os.environ.get("RMM_TLS_MODE") == "none" else "1") == "1"

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


def verify_local(username: str, password: str) -> dict:
    """Verify a local account's password; return the user row (raises on failure)."""
    u = db.get_user(username)
    if not u or not db.verify_pw(password, u["pw_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return u


def is_global_admin(identifier: str) -> bool:
    if identifier.lower() in BOOTSTRAP_ADMINS:
        return True
    u = db.get_user(identifier)
    return bool(u and u["is_admin"])


def current_user(request: Request) -> dict:
    """FastAPI dependency: resolve the signed-in user or 401."""
    if DEV_AUTH:
        # Single-admin evaluation mode: the auto-signed-in user is a global admin.
        return {"email": DEV_USER, "is_global_admin": True}
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
