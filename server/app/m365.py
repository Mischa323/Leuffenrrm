"""Microsoft 365 tenants, read from Microsoft Graph.

Server-side integration like UniFi: per organisation, an app registration in
the customer's own tenant (tenant id, client id, client secret) with read-only
**application** permissions and admin consent. The server polls it and keeps a
snapshot -- the tenant and its domains, the subscriptions and when they renew,
the users with their licences, the shared mailboxes, the groups and Teams with
their members, the SharePoint sites, the app registrations with the date their
secrets expire, and security defaults / Conditional Access -- which the
alerter checks (expiring app secrets, suspended subscriptions) and LeuffenDoc
documents (``GET /api/v1/m365-tenants``).

Each part is read on its own: a permission that was not granted costs that
part only, and the snapshot says which permission it needs. Nothing is ever
written to the tenant. Values are stored language-neutral (``shared``,
``team``, ``True``); each interface words them itself.
"""
from __future__ import annotations

import datetime
import logging
import os

import httpx

log = logging.getLogger("rmm.m365")

# Microsoft's addresses; the environment can point them elsewhere (the
# end-to-end tests run a Graph of their own).
GRAPH = os.environ.get("RMM_M365_GRAPH_URL") or "https://graph.microsoft.com/v1.0"
LOGIN = os.environ.get("RMM_M365_LOGIN_URL") or "https://login.microsoftonline.com"
_TIMEOUT = 20.0
_MAX_MAILBOX_CHECKS = 300          # mailboxSettings is one call per mailbox
_MAX_MEMBER_LISTS = 80             # members are one call per group

# What the app registration needs, and what for -- shown when connecting.
PERMISSIONS = [
    ("Organization.Read.All", "tenant, domains and subscriptions"),
    ("User.Read.All", "users and their licences"),
    ("GroupMember.Read.All", "groups, Teams and distribution lists with their members"),
    ("MailboxSettings.Read", "which mailboxes are shared"),
    ("Application.Read.All", "app registrations and when their secrets expire"),
    ("Policy.Read.All", "security defaults and Conditional Access"),
    ("Sites.Read.All", "SharePoint sites (optional)"),
]

# The names on the invoice for the subscriptions an MSP meets every day; any
# other is shown by its part number.
SKUS = {
    "SPB": "Microsoft 365 Business Premium",
    "O365_BUSINESS_PREMIUM": "Microsoft 365 Business Standard",
    "O365_BUSINESS_ESSENTIALS": "Microsoft 365 Business Basic",
    "O365_BUSINESS": "Microsoft 365 Apps for business",
    "OFFICESUBSCRIPTION": "Microsoft 365 Apps for enterprise",
    "SPE_E3": "Microsoft 365 E3", "SPE_E5": "Microsoft 365 E5",
    "SPE_F1": "Microsoft 365 F3", "M365_F1": "Microsoft 365 F1",
    "ENTERPRISEPACK": "Office 365 E3", "ENTERPRISEPREMIUM": "Office 365 E5",
    "STANDARDPACK": "Office 365 E1", "DESKLESSPACK": "Office 365 F3",
    "EXCHANGESTANDARD": "Exchange Online (Plan 1)", "EXCHANGEENTERPRISE": "Exchange Online (Plan 2)",
    "EXCHANGEDESKLESS": "Exchange Online Kiosk", "EXCHANGEARCHIVE_ADDON": "Exchange Online Archiving",
    "EMS": "Enterprise Mobility + Security E3", "EMSPREMIUM": "Enterprise Mobility + Security E5",
    "AAD_PREMIUM": "Microsoft Entra ID P1", "AAD_PREMIUM_P2": "Microsoft Entra ID P2",
    "INTUNE_A": "Microsoft Intune Plan 1", "ATP_ENTERPRISE": "Microsoft Defender for Office 365 (Plan 1)",
    "THREAT_INTELLIGENCE": "Microsoft Defender for Office 365 (Plan 2)",
    "DEFENDER_ENDPOINT_P1": "Microsoft Defender for Endpoint P1",
    "MDATP_XPLAT": "Microsoft Defender for Endpoint P2",
    "Microsoft_Teams_Exploratory_Dept": "Microsoft Teams Exploratory",
    "TEAMS_EXPLORATORY": "Microsoft Teams Exploratory", "Teams_Ess": "Microsoft Teams Essentials",
    "MCOEV": "Teams Phone Standard", "MCOPSTN1": "Teams Calling Plan (domestic)",
    "PHONESYSTEM_VIRTUALUSER": "Teams Phone Resource Account",
    "MCOMEETADV": "Microsoft 365 Audio Conferencing",
    "POWER_BI_STANDARD": "Power BI (free)", "POWER_BI_PRO": "Power BI Pro",
    "FLOW_FREE": "Power Automate (free)", "POWERAPPS_VIRAL": "Power Apps (trial)",
    "PROJECTPROFESSIONAL": "Project Plan 3", "VISIOCLIENT": "Visio Plan 2",
    "WIN_DEF_ATP": "Microsoft Defender for Endpoint", "WIN10_VDA_E3": "Windows 10/11 Enterprise E3",
    "STREAM": "Microsoft Stream", "RIGHTSMANAGEMENT": "Azure Information Protection P1",
}


class GraphError(Exception):
    """Graph or the sign-in refused, said in a sentence somebody can act on."""


def product_name(part_number: str) -> str:
    return SKUS.get(part_number or "", part_number or "unknown")


# --------------------------------------------------------------------------- #
# Talking to Graph
# --------------------------------------------------------------------------- #
_SIGNIN_HINTS = {
    "AADSTS7000215": "the client secret is wrong",
    "AADSTS7000222": "the client secret has expired — create a new one",
    "AADSTS700016": "this app registration does not exist in this tenant (wrong client id, or no consent yet)",
    "AADSTS90002": "this tenant does not exist (check the tenant id)",
    "AADSTS900023": "the tenant id is not a valid id or domain",
    "AADSTS65001": "an administrator has not consented to this app yet",
}


def token(tenant_id: str, client_id: str, secret: str) -> str:
    try:
        r = httpx.post(f"{LOGIN}/{tenant_id}/oauth2/v2.0/token", timeout=_TIMEOUT, data={
            "grant_type": "client_credentials", "client_id": client_id,
            "client_secret": secret, "scope": "https://graph.microsoft.com/.default"})
    except httpx.HTTPError as exc:
        raise GraphError(f"cannot reach Microsoft ({type(exc).__name__})") from exc
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or "access_token" not in body:
        text = body.get("error_description") or body.get("error") or f"HTTP {r.status_code}"
        for code, said in _SIGNIN_HINTS.items():
            if code in text:
                raise GraphError(f"{said} ({code})")
        raise GraphError(text.splitlines()[0][:200])
    return body["access_token"]


def test_credentials(tenant_id: str, client_id: str, secret: str) -> tuple[bool, str]:
    try:
        token(tenant_id, client_id, secret)
        return True, "ok"
    except GraphError as exc:
        return False, str(exc)


def _get(client: httpx.Client, url: str, **params) -> dict:
    try:
        r = client.get(url if url.startswith("http") else GRAPH + url, params=params or None)
    except httpx.HTTPError as exc:
        raise GraphError(f"no answer from Microsoft Graph ({type(exc).__name__})") from exc
    if r.status_code == 403:
        raise GraphError("forbidden")
    if r.status_code == 404:
        raise GraphError("not found")
    if r.status_code >= 400:
        try:
            message = r.json().get("error", {}).get("message") or f"HTTP {r.status_code}"
        except ValueError:
            message = f"HTTP {r.status_code}"
        raise GraphError(message[:200])
    return r.json()


def _all(client: httpx.Client, url: str, **params) -> list:
    out: list = []
    body = _get(client, url, **params)
    for _ in range(100):
        out.extend(body.get("value") or [])
        nxt = body.get("@odata.nextLink")
        if not nxt:
            break
        body = _get(client, nxt)
    return out


def _date(value) -> str:
    return str(value or "")[:10]


# --------------------------------------------------------------------------- #
# Reading a tenant
# --------------------------------------------------------------------------- #
def collect(tenant_id: str, client_id: str, secret: str) -> dict:
    """A snapshot of the tenant: ``{ok, error, tenant, subscriptions, users,
    mailboxes, groups, sites, apps, security_defaults, ca, problems,
    fetched_at}``. ``ok`` is False only when signing in failed."""
    snap: dict = {"ok": False, "error": None, "problems": []}
    try:
        bearer = token(tenant_id, client_id, secret)
    except GraphError as exc:
        snap["error"] = str(exc)
        return snap
    with httpx.Client(timeout=_TIMEOUT, headers={"Authorization": f"Bearer {bearer}"}) as client:
        def part(name: str, permission: str, fn) -> None:
            try:
                fn()
            except GraphError as exc:
                snap["problems"].append({"part": name, "permission": permission, "error": str(exc)})
                log.info("m365 %s %s: %s", tenant_id, name, exc)

        skus: dict = {}
        candidates: list = []

        def tenant() -> None:
            org = (_all(client, "/organization") or [{}])[0]
            domains = [{"name": d.get("name"), "default": bool(d.get("isDefault")), "initial": bool(d.get("isInitial"))}
                       for d in org.get("verifiedDomains") or [] if d.get("name")]
            domains.sort(key=lambda d: (not d["default"], d["name"]))
            snap["tenant"] = {"id": org.get("id") or tenant_id, "name": org.get("displayName") or "",
                              "default_domain": next((d["name"] for d in domains if d["default"]), ""),
                              "domains": domains}

        def subscriptions() -> None:
            renewals: dict = {}
            try:
                for s in _all(client, "/directory/subscriptions"):
                    if s.get("skuId"):
                        renewals[s["skuId"]] = s
            except GraphError:
                pass                       # the counts still come; only the dates are missing
            rows = []
            for sku in _all(client, "/subscribedSkus"):
                skus[sku.get("skuId")] = product_name(sku.get("skuPartNumber"))
                seats = (sku.get("prepaidUnits") or {}).get("enabled") or 0
                if not seats and not sku.get("consumedUnits"):
                    continue                  # a free offer nobody took
                rows.append({"sku": sku.get("skuPartNumber") or "", "product": skus[sku.get("skuId")],
                             "seats": seats, "used": sku.get("consumedUnits") or 0,
                             "renews": _date((renewals.get(sku.get("skuId")) or {}).get("nextLifecycleDateTime")),
                             "status": sku.get("capabilityStatus") or ""})
            snap["subscriptions"] = sorted(rows, key=lambda r: r["product"].lower())

        def users() -> None:
            rows = []
            for u in _all(client, "/users", **{"$select": "id,displayName,userPrincipalName,mail,accountEnabled,"
                                                         "assignedLicenses,jobTitle,userType",
                                              "$top": "999"}):
                licences = sorted(skus.get(l.get("skuId"), "licence") for l in u.get("assignedLicenses") or [])
                if not u.get("accountEnabled") and not licences and u.get("mail"):
                    candidates.append(u)        # perhaps a shared mailbox; see below
                    continue
                rows.append(_user(u, licences))
            snap["users"] = sorted(rows, key=lambda r: r["name"].lower())

        def mailboxes() -> None:
            rows = []
            for u in candidates[:_MAX_MAILBOX_CHECKS]:
                try:
                    purpose = (_get(client, f"/users/{u['id']}/mailboxSettings", **{"$select": "userPurpose"})
                               .get("userPurpose") or "")
                except GraphError as exc:
                    if str(exc) == "forbidden":
                        raise
                    continue
                if purpose in ("shared", "room", "equipment"):
                    rows.append({"address": u.get("mail") or "", "name": u.get("displayName") or "", "kind": purpose})
                else:
                    snap.setdefault("users", []).append(_user(u, []))   # disabled and unlicensed: left
            order = {"shared": 0, "room": 1, "equipment": 2}
            snap["mailboxes"] = sorted(rows, key=lambda r: (order[r["kind"]], r["address"].lower()))

        def groups() -> None:
            rows, listed = [], 0
            for g in _all(client, "/groups", **{"$select": "id,displayName,mail,mailEnabled,securityEnabled,"
                                                           "groupTypes,resourceProvisioningOptions",
                                               "$top": "999"}):
                types = g.get("groupTypes") or []
                if "Team" in (g.get("resourceProvisioningOptions") or []):
                    kind = "team"
                elif "Unified" in types:
                    kind = "m365"
                elif g.get("mailEnabled") and not g.get("securityEnabled"):
                    kind = "distribution"
                elif g.get("mailEnabled"):
                    kind = "mail_security"
                else:
                    kind = "security"
                dynamic = "DynamicMembership" in types
                members: list = []
                # Who is in a list or a team is what gets asked; a security
                # group's members are a different question, and often many.
                if kind != "security" and not dynamic and listed < _MAX_MEMBER_LISTS:
                    listed += 1
                    members = sorted(p.get("displayName") or "" for p in
                                     _all(client, f"/groups/{g['id']}/members", **{"$select": "displayName", "$top": "999"})
                                     if p.get("displayName"))
                rows.append({"name": g.get("displayName") or "", "mail": g.get("mail") or "", "kind": kind,
                             "dynamic": dynamic, "members": members[:200]})
            order = {"team": 0, "m365": 1, "distribution": 2, "mail_security": 3, "security": 4}
            snap["groups"] = sorted(rows, key=lambda r: (order[r["kind"]], r["name"].lower()))

        def sites() -> None:
            snap["sites"] = sorted(({"name": s.get("displayName") or s.get("name") or "", "url": s.get("webUrl") or ""}
                                    for s in _all(client, "/sites", search="*")
                                    if "-my.sharepoint.com" not in (s.get("webUrl") or "")),
                                   key=lambda r: r["name"].lower())

        def apps() -> None:
            rows = []
            for a in _all(client, "/applications", **{"$select": "displayName,appId,passwordCredentials,keyCredentials",
                                                     "$top": "999"}):
                for kind, creds in (("secret", a.get("passwordCredentials")), ("certificate", a.get("keyCredentials"))):
                    for c in creds or []:
                        rows.append({"app": a.get("displayName") or a.get("appId") or "", "app_id": a.get("appId") or "",
                                     "kind": kind, "name": c.get("displayName") or "",
                                     "key_id": c.get("keyId") or "", "expires": _date(c.get("endDateTime"))})
            snap["apps"] = sorted(rows, key=lambda r: (r["expires"] or "9999", r["app"].lower()))

        def security() -> None:
            snap["security_defaults"] = bool(_get(client, "/policies/identitySecurityDefaultsEnforcementPolicy")
                                             .get("isEnabled"))

        def conditional_access() -> None:
            states = {"enabled": "enabled", "disabled": "disabled", "enabledForReportingButNotEnforced": "report"}
            snap["ca"] = sorted(({"name": p.get("displayName") or "", "state": states.get(p.get("state"), p.get("state") or "")}
                                 for p in _all(client, "/identity/conditionalAccess/policies")),
                                key=lambda r: r["name"].lower())

        part("tenant", "Organization.Read.All", tenant)
        part("subscriptions", "Organization.Read.All", subscriptions)
        part("users", "User.Read.All", users)
        part("mailboxes", "MailboxSettings.Read", mailboxes)
        if "mailboxes" not in snap and "users" in snap:
            # Which of them are shared could not be read: listed as what they
            # are for sure -- accounts that cannot sign in.
            snap["users"] = sorted(snap["users"] + [_user(u, []) for u in candidates], key=lambda r: r["name"].lower())
        part("groups", "GroupMember.Read.All", groups)
        part("sites", "Sites.Read.All", sites)
        part("apps", "Application.Read.All", apps)
        part("security_defaults", "Policy.Read.All", security)
        part("conditional_access", "Policy.Read.All + Entra ID P1", conditional_access)
    snap["ok"] = True
    snap["fetched_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    return snap


def _user(u: dict, licences: list) -> dict:
    return {"name": u.get("displayName") or "", "upn": u.get("userPrincipalName") or "",
            "licenses": licences, "job": u.get("jobTitle") or "",
            "enabled": bool(u.get("accountEnabled")), "guest": u.get("userType") == "Guest"}


def summary(snap: dict | None) -> dict:
    """Counts for a card: users, licences in use, shared mailboxes, groups,
    and app credentials that expire within 30 days (or already have)."""
    snap = snap or {}
    soon = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    return {
        "users": sum(1 for u in snap.get("users") or [] if u.get("enabled") and not u.get("guest")),
        "guests": sum(1 for u in snap.get("users") or [] if u.get("guest")),
        "seats": sum(int(s.get("seats") or 0) for s in snap.get("subscriptions") or []),
        "used": sum(int(s.get("used") or 0) for s in snap.get("subscriptions") or []),
        "shared": sum(1 for m in snap.get("mailboxes") or [] if m.get("kind") == "shared"),
        "groups": len(snap.get("groups") or []),
        "expiring": sum(1 for a in snap.get("apps") or [] if a.get("expires") and a["expires"] <= soon),
    }
