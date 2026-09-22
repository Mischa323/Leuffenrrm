"""Authentication: Microsoft Entra / Office 365 SSO (tenant-only) with a dev fallback.

Real SSO uses MSAL's confidential-client OIDC auth-code flow against a single
tenant (so only that tenant's users can sign in). A signed session cookie carries
the user's email after login.

If MS_* is not configured (or ``RMM_DEV_AUTH=1``), the server runs in **dev auth**
mode and signs in a bootstrap admin automatically — so the app is runnable and
verifiable without an Azure app registration.
"""
from __future__ import annotations

import logging
import os

from fastapi import HTTPException, Request
from itsdangerous import (BadData, SignatureExpired, URLSafeSerializer,
                          URLSafeTimedSerializer)

from . import database as db

log = logging.getLogger("rmm.auth")

TENANT_ID = os.environ.get("MS_TENANT_ID", "")
CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("MS_REDIRECT_URI", "http://localhost:8000/auth/callback")
COOKIE = "rmm_session"
SCOPES = ["User.Read"]

# Known-insecure placeholder that must never be used to sign real sessions.
_INSECURE_SECRET = "dev-insecure-secret-change-me"

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


def _resolve_session_secret() -> str:
    """The signing key for session cookies — never the known-insecure default.

    Order of preference:
      1. A strong ``SESSION_SECRET`` from the environment (operator-controlled).
      2. In real (non-dev) mode, a strong random secret generated once and
         persisted in the DB ``settings`` table, so the app is secure-by-default
         and the secret survives restarts (sessions stay valid across reboots).
      3. Dev mode only: the placeholder is tolerated (evaluation, no real users).

    This closes the hole where an unset ``SESSION_SECRET`` left cookies signed
    with a public default that anyone could forge into an admin session."""
    import secrets as _secrets
    env = os.environ.get("SESSION_SECRET", "").strip()
    if env and env != _INSECURE_SECRET and len(env) >= 16:
        return env
    if DEV_AUTH:
        return env or _INSECURE_SECRET
    # Real deployment with no usable secret: generate + persist a strong one.
    try:
        stored = db.get_setting("session_secret")
        if stored and len(stored) >= 16:
            return stored
        gen = _secrets.token_urlsafe(48)
        db.set_setting("session_secret", gen)
        log.warning("SESSION_SECRET was unset or insecure; generated a strong random "
                    "secret and stored it. Set SESSION_SECRET in the environment to "
                    "pin it explicitly (and to share it across multiple instances).")
        return gen
    except Exception:
        # Last resort: a per-process random secret (sessions reset on restart) —
        # still infinitely better than a publicly known default.
        log.error("Could not persist a session secret; using a per-process random one.")
        return _secrets.token_urlsafe(48)


SESSION_SECRET = _resolve_session_secret()

# Mark session cookies Secure unless explicitly disabled (TLS is on by default).
# Auto-off for plain-HTTP proxy mode without TLS termination.
SECURE_COOKIES = os.environ.get("RMM_SECURE_COOKIES",
                                "0" if os.environ.get("RMM_TLS_MODE") == "none" else "1") == "1"

# How long a "keep me signed in" session lasts, in days. It is enforced twice:
# the browser is told to drop the cookie after this long, and the cookie itself
# carries a timestamp so the server refuses it after the same period even if the
# browser hangs on to it.
SESSION_DAYS = int(os.environ.get("RMM_SESSION_DAYS", "30"))

_serializer = URLSafeSerializer(SESSION_SECRET, salt="rmm-session")
_timed_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="rmm-session")


def resolve_sso_identity(email: str) -> str:
    """Map a Microsoft 365 email onto a local account (by email) when one exists,
    so the same person has one identity and the local account's admin rights."""
    u = db.get_user_by_email(email)
    return u["username"] if u else email.lower()


def sso_permitted(email: str) -> bool:
    """Return True if this SSO email is allowed to sign in.

    An SSO user is permitted only when they are a bootstrap admin OR have a
    local account (created by invite or manually). Any valid M365 user who
    is not pre-approved is blocked.
    """
    if email.lower() in BOOTSTRAP_ADMINS:
        return True
    return db.get_user_by_email(email) is not None


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
    """Mint a session cookie value, stamped with the time it was issued."""
    return _timed_serializer.dumps({"email": email})


def read_cookie(value: str) -> dict | None:
    try:
        return _timed_serializer.loads(value, max_age=SESSION_DAYS * 86400)
    except SignatureExpired:
        return None                     # older than SESSION_DAYS: sign in again
    except BadData:                     # BadSignature + malformed payloads
        pass
    # Sessions issued before cookies carried a timestamp have no age to check.
    # They are still accepted so that deploying this doesn't sign everyone out;
    # each is replaced by a timestamped one at that person's next sign-in. Drop
    # this fallback once the fleet has rolled over to force the rotation.
    try:
        return _serializer.loads(value)
    except BadData:
        return None


def cookie_kwargs(remember: bool) -> dict:
    """Cookie options for `set_cookie`.

    Ticking "keep me signed in" is the difference between a cookie the browser
    keeps for SESSION_DAYS and one it throws away when it closes. Either way the
    signature expires after SESSION_DAYS, so a copied cookie is not good forever.
    """
    options = {"httponly": True, "samesite": "lax", "secure": SECURE_COOKIES}
    if remember:
        options["max_age"] = SESSION_DAYS * 86400
    return options


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
    """FastAPI dependency: resolve the signed-in user or 401.

    Two credentials are accepted: the browser's signed session cookie, and the
    desktop console's `Authorization: Bearer <app token>` (see "Desktop console
    tokens" below)."""
    raw_token = _bearer(request)
    if raw_token:
        as_token = user_from_token(raw_token)
        if as_token:
            return as_token
        if not DEV_AUTH:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
    if DEV_AUTH:
        # Single-admin evaluation mode: the auto-signed-in user is a global admin.
        return {"email": DEV_USER, "is_global_admin": True}
    raw = request.cookies.get(COOKIE)
    data = read_cookie(raw) if raw else None
    if not data:
        raise HTTPException(status_code=401, detail="Not authenticated")
    email = data["email"]
    return {"email": email, "is_global_admin": is_global_admin(email)}


def optional_user(request: Request) -> dict | None:
    """Like current_user but returns None instead of raising (for token-or-cookie
    authorised endpoints)."""
    try:
        return current_user(request)
    except HTTPException:
        return None


def require_org(user: dict, org_id: str) -> str:
    """Ensure the user may act in ``org_id``; return their effective role."""
    if user["is_global_admin"]:
        return "admin"
    role = db.user_effective_role(user["email"], org_id)
    if role is None:
        raise HTTPException(status_code=403, detail="No access to this organisation")
    return role


def check_permission(user: dict, org_id: str, permission: str) -> bool:
    """Return True if the user is allowed to perform ``permission`` in ``org_id``.

    Global admins are always allowed. For other users the deny-overrides-allow
    logic in ``db.user_effective_perms`` is applied.
    """
    if user["is_global_admin"]:
        return True
    perms = db.user_effective_perms(user["email"], org_id)
    return perms.get(permission, {}).get("effect") == "allow"


def require_permission(user: dict, org_id: str, permission: str) -> None:
    """Like check_permission but raises 403 on denial."""
    if not check_permission(user, org_id, permission):
        raise HTTPException(status_code=403, detail=f"Permission denied: {permission}")


def require_global(user: dict) -> None:
    """Ensure the user is a global admin (required for global-scoped resources)."""
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")


def require_scope(user: dict, org_id: str | None) -> str:
    """Like :func:`require_org`, but ``org_id=None`` means a global resource —
    only a global admin may act on it."""
    if org_id is None:
        require_global(user)
        return "admin"
    return require_org(user, org_id)


# --------------------------------------------------------------------------- #
# Desktop console tokens
#
# The Windows desktop console (a native app, not a browser) can't carry the
# signed session *cookie*, so it authenticates with a **bearer token**:
#
#   * `make_app_token()` mints a long-lived signed token. The console stores it
#     and sends it as `Authorization: Bearer …` on REST calls and as `?token=`
#     on the interactive WebSockets (browsers can't set WS headers, and neither
#     can every WS client, so both forms are accepted).
#   * `make_app_ticket()` mints a **short-lived, single-use** ticket that the
#     signed-in dashboard hands to the `leuffenrmm://` deep link. The console
#     exchanges it for a real token — which is how a Microsoft 365 SSO user signs
#     the app in without ever typing a password into it.
#
# Both are signed with the same SESSION_SECRET, under their own salts, so a
# session cookie can never be replayed as an app token (or vice versa).
# --------------------------------------------------------------------------- #
APP_TOKEN_DAYS = int(os.environ.get("RMM_APP_TOKEN_DAYS", "30"))
APP_TICKET_SECONDS = int(os.environ.get("RMM_APP_TICKET_SECONDS", "120"))

_app_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="rmm-app-token")
_ticket_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="rmm-app-ticket")

# Tickets are single-use: a redeemed nonce is remembered until it would have
# expired anyway, so a deep-link URL captured from a log or shell history can't
# be replayed. In-memory is enough (same single-process assumption as the
# login rate limiter above).
_used_tickets: dict[str, float] = {}


def make_app_token(identity: str) -> str:
    """Mint a desktop-console bearer token for ``identity`` (a local username or
    an SSO email — whatever the session cookie would carry)."""
    return _app_serializer.dumps({"u": identity})


def read_app_token(raw: str) -> str | None:
    """Return the identity carried by a bearer token, or None if it is invalid,
    expired, or forged."""
    try:
        data = _app_serializer.loads(raw, max_age=APP_TOKEN_DAYS * 86400)
    except (BadData, SignatureExpired):
        return None
    return data.get("u") if isinstance(data, dict) else None


def make_app_ticket(identity: str) -> str:
    """Mint a short-lived single-use ticket for the `leuffenrmm://` hand-off."""
    import secrets as _secrets
    return _ticket_serializer.dumps({"u": identity, "n": _secrets.token_urlsafe(9)})


def read_app_ticket(raw: str) -> str | None:
    """Redeem a hand-off ticket, returning the identity. Each ticket works once."""
    import time as _time
    try:
        data = _ticket_serializer.loads(raw, max_age=APP_TICKET_SECONDS)
    except (BadData, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    nonce = data.get("n") or ""
    now = _time.monotonic()
    for k, exp in list(_used_tickets.items()):       # prune
        if exp < now:
            _used_tickets.pop(k, None)
    if nonce in _used_tickets:
        return None                                   # already redeemed
    _used_tickets[nonce] = now + APP_TICKET_SECONDS
    return data.get("u")


def _identity_still_valid(identity: str) -> bool:
    """A token outlives the account it was minted for unless we check: a deleted
    (or renamed) user's token must stop working immediately."""
    if identity.lower() in BOOTSTRAP_ADMINS:
        return True
    try:
        return db.get_user(identity) is not None
    except Exception:
        return False


def user_from_token(raw: str | None) -> dict | None:
    """Resolve a bearer token into the same user dict `current_user` returns."""
    if not raw:
        return None
    identity = read_app_token(raw)
    if not identity or not _identity_still_valid(identity):
        return None
    return {"email": identity, "is_global_admin": is_global_admin(identity)}


def _bearer(request: Request) -> str | None:
    """Pull the token out of an `Authorization: Bearer …` header."""
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


# --------------------------------------------------------------------------- #
# Where a person may sign in from
#
# Two lists, both live at once, with the usual precedence: a deny rule always
# wins, and a non-empty allow list means *only* those addresses get in. Both
# accept single addresses and CIDR ranges, one per line, with `#` comments.
#
# This never touches agent connections. A mistyped rule should cost someone a
# sign-in, not take the whole fleet offline -- and a blocked agent cannot
# re-enrol itself to recover.
# --------------------------------------------------------------------------- #
def _ip_rules(raw: str) -> list:
    """Parse a rule list, skipping anything malformed.

    An unparseable line is dropped rather than treated as a wildcard: a typo
    must never silently widen access."""
    import ipaddress
    out = []
    for line in (raw or "").replace(",", "\n").splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        try:
            out.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return out


def ip_rules_invalid(raw: str) -> list[str]:
    """The lines of `raw` that are not a valid address or range, for the UI."""
    import ipaddress
    bad = []
    for line in (raw or "").replace(",", "\n").splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            bad.append(entry)
    return bad


def client_ip(request: Request) -> str:
    """The caller's address.

    `X-Forwarded-For` is honoured only when RMM_TRUST_PROXY is set: taking it on
    faith would let anyone put whatever address they like in front of an allow
    list.

    Who resolves the header depends on `RMM_PROXY_IPS`:

    * **Pinned to the proxy** -- uvicorn has already done it, and only for
      connections that actually came from that proxy, so a caller reaching this
      server directly cannot claim anything. Its answer stands.
    * **Left as `*`** -- uvicorn believes any caller, and takes the *first*
      entry, which is precisely the part a browser can write itself. So read the
      header here instead and take the **last** entry: each proxy appends what
      it saw, so the last one is what *our* proxy saw. (A proxy told to
      overwrite rather than append -- `X-Forwarded-For $remote_addr` -- sends
      one entry, and the two readings agree.) A caller who can reach this server
      without passing the proxy can still claim an address; Settings → Security
      says so.
    """
    if os.environ.get("RMM_TRUST_PROXY", "0") == "1" \
            and os.environ.get("RMM_PROXY_IPS", "*").strip() in ("", "*"):
        hops = forwarded_hops(request)
        if hops:
            return hops[-1]
    return request.client.host if request.client else ""


def forwarded_hops(request: Request) -> list[str]:
    """The addresses in `X-Forwarded-For`, as sent. More than one means the
    proxy appends to a header the caller's own browser may have supplied."""
    raw = request.headers.get("x-forwarded-for") or ""
    return [h.strip() for h in raw.split(",") if h.strip()]


def ip_filtering_on() -> bool:
    return bool(os.environ.get("RMM_IP_ALLOW", "").strip()
                or os.environ.get("RMM_IP_DENY", "").strip())


def ip_allowed(address: str) -> bool:
    """True if `address` may sign in / use the API."""
    import ipaddress
    deny = _ip_rules(os.environ.get("RMM_IP_DENY", ""))
    allow = _ip_rules(os.environ.get("RMM_IP_ALLOW", ""))
    if not deny and not allow:
        return True
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        # No usable address (a malformed header, a unix socket). Refuse only
        # when an allow list is in force, since that list is meant to be the
        # complete set of places access can come from.
        return not allow
    if any(ip in net for net in deny):
        return False
    return not allow or any(ip in net for net in allow)


# --------------------------------------------------------------------------- #
# Two-factor enforcement
# --------------------------------------------------------------------------- #
def enforce_2fa() -> bool:
    return os.environ.get("RMM_ENFORCE_2FA", "").lower() in ("1", "true", "yes")


def needs_2fa_enrolment(identity: str) -> bool:
    """True when the policy is on and this person has no authenticator yet.

    Microsoft 365 identities are included: the workspace can require its own
    second factor on top of the tenant's. An SSO identity that has never had a
    local account gets one created when they enrol, purely to hold the secret
    and the recovery codes -- they still sign in through Microsoft.
    """
    if not enforce_2fa() or not identity:
        return False
    try:
        u = db.get_user(identity)
    except Exception:
        return False
    return not (u and u.get("totp_enabled"))
