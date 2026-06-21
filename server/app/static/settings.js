"use strict";
/* ============================================================================
   Leuffen RMM — Admin Settings (wired to /api/settings, /api/orgs, /api/users).
   ========================================================================== */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const NAV = {
  general: { icon: "gear", t: "General" },
  orgs: { icon: "building", t: "Organisations" },
  users: { icon: "user", t: "Users & roles" },
  auth: { icon: "lock", t: "Authentication" },
  alerts: { icon: "bell", t: "Alerts & email" },
  security: { icon: "shieldCheck", t: "Security" },
  agents: { icon: "monitor", t: "Agents" },
  logs: { icon: "terminal", t: "Logs" },
  appearance: { icon: "sliders", t: "Appearance" },
};

let cfg = {}, ORGS = [], USERS = { mode: "dev", users: [], bootstrap_admins: [] };

function toast(msg) {
  const t = $("toast"); t.querySelector("span:last-child").textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 2200);
}
const initials2 = (s) => (s || "?").slice(0, 2).toUpperCase();
const colorFor = (id) => { let h = 0; for (const c of String(id)) h = (h * 31 + c.charCodeAt(0)) >>> 0; return ["#3b82f6", "#8b5cf6", "#10b981", "#06b6d4", "#f59e0b", "#ec4899"][h % 6]; };

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}
async function saveKeys(obj, msg) {
  try { await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(obj) }); toast(msg || "Settings saved"); }
  catch (e) { toast(e.message); }
}

/* ---- lightweight modal ---- */
function modal(title, bodyHtml) {
  const scrim = document.createElement("div");
  scrim.className = "modal-scrim";
  scrim.style.cssText = "display:grid;place-items:center;padding:24px";
  scrim.innerHTML = `<div role="dialog" aria-modal="true" style="width:100%;max-width:420px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-xl);box-shadow:var(--shadow-lg);padding:24px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
        <h3 style="margin:0;font-size:16px;font-weight:700">${title}</h3>
        <button type="button" class="btn ghost sm modal-x" aria-label="Close" style="padding:4px 9px;font-size:18px;line-height:1">&times;</button>
      </div>
      <div class="modal-body">${bodyHtml}</div>
    </div>`;
  document.body.appendChild(scrim);
  const close = () => { scrim.remove(); document.removeEventListener("keydown", onEsc); };
  function onEsc(e) { if (e.key === "Escape") close(); }
  scrim.querySelector(".modal-x").onclick = close;
  scrim.onclick = (e) => { if (e.target === scrim) close(); };
  document.addEventListener("keydown", onEsc);
  return { close, q: (sel) => scrim.querySelector(sel) };
}
const mfield = (label, html, hint) => `<div style="margin-bottom:14px"><label style="display:block;font-size:12px;color:var(--text-dim);margin-bottom:6px">${label}</label>${html}${hint ? `<div class="hint" style="margin-top:5px">${hint}</div>` : ""}</div>`;

/* ---- nav ---- */
function buildNav() {
  document.querySelectorAll("#settings-nav button").forEach((b) => {
    const n = NAV[b.dataset.sec];
    b.innerHTML = `${ICON[n.icon]} ${n.t}`;
    b.onclick = () => selectSec(b.dataset.sec);
  });
}
function selectSec(sec) {
  document.querySelectorAll("#settings-nav button").forEach((b) => b.classList.toggle("active", b.dataset.sec === sec));
  document.querySelectorAll(".sec").forEach((s) => s.classList.toggle("on", s.dataset.sec === sec));
  window.scrollTo(0, 0);
  if (sec === "logs") loadLogs();
  if (sec === "security") loadFingerprint();
}

async function loadFingerprint() {
  const host = $("cert-fp");
  if (!host) return;
  try {
    const r = await api("/api/server-fingerprint");
    if (!r.fingerprint) {
      host.innerHTML = `<div class="callout info"><div class="ic">${ICON.info}</div><div><div class="ct">Not applicable in proxy mode</div><div class="cd">TLS is terminated upstream (<span class="mono">RMM_TLS_MODE=${esc(r.tls_mode)}</span>); pin the certificate at your reverse proxy instead.</div></div></div>`;
      return;
    }
    const mask = "•".repeat(64);
    host.innerHTML = `<div class="frow"><label>SHA-256 fingerprint</label><code class="mono" id="fp-val" style="word-break:break-all">${mask}</code></div>
      <div style="margin-top:8px;display:flex;gap:8px">
        <button class="btn" id="fp-toggle">${ICON.eye} Show</button>
        <button class="btn" id="fp-copy">${ICON.copy || ICON.info} Copy</button>
      </div>
      <div class="callout info" style="margin-top:12px"><div class="ic">${ICON.info}</div><div><div class="ct">Pin it on agents</div><div class="cd">Set <code>RMM_SERVER_FINGERPRINT</code> (or <code>server_fingerprint</code> in the agent's <code>rmm_config.json</code>) to this value. Then set <code>RMM_REQUIRE_DEVICE_SECRET=1</code> once the fleet is updated to enforce per-device identity.</div></div></div>`;
    let shown = false;
    const valEl = $("fp-val"), tgl = $("fp-toggle");
    if (tgl) tgl.onclick = () => {
      shown = !shown;
      valEl.textContent = shown ? r.fingerprint : mask;
      tgl.innerHTML = `${shown ? ICON.eyeOff : ICON.eye} ${shown ? "Hide" : "Show"}`;
    };
    const cp = $("fp-copy");
    if (cp) cp.onclick = () => navigator.clipboard.writeText(r.fingerprint).then(() => toast("Fingerprint copied"));
  } catch (e) {
    host.innerHTML = `<div class="callout warn"><div class="ic">${ICON.alert}</div><div><div class="ct">Couldn't load fingerprint</div><div class="cd">${esc(e.message)}</div></div></div>`;
  }
}

async function loadLogs() {
  const view = $("log-view");
  if (!view) return;
  const lvl = $("log-level") ? $("log-level").value : "all";
  try {
    const r = await api(`/api/logs?limit=400${lvl && lvl !== "all" ? "&level=" + lvl : ""}`);
    if (!r.logs.length) { view.innerHTML = `<div class="muted" style="padding:14px">No log entries yet.</div>`; return; }
    view.innerHTML = r.logs.map((e) => {
      const ts = new Date(e.t * 1000).toLocaleTimeString();
      const cls = e.level === "ERROR" || e.level === "CRITICAL" ? "err" : e.level === "WARNING" ? "warn" : "info";
      return `<div class="log-line"><span class="lt">${ts}</span><span class="ll ${cls}">${esc(e.level)}</span><span class="ln">${esc(e.name)}</span><span class="lm">${esc(e.msg)}</span></div>`;
    }).join("");
    view.scrollTop = view.scrollHeight;
  } catch (e) {
    view.innerHTML = `<div class="callout warn"><div class="ic">${ICON.alert}</div><div><div class="ct">Couldn't load logs</div><div class="cd">${esc(e.message)}</div></div></div>`;
  }
}

function block(title, desc, bodyHtml, saveId) {
  return `<div class="card-block">
    ${title ? `<div class="cb-head"><h3>${title}</h3>${desc ? `<p>${desc}</p>` : ""}</div>` : ""}
    <div class="cb-body">${bodyHtml}</div>
    ${saveId ? `<div class="cb-foot"><span class="saved">${ICON.info} Changes apply on save</span><button class="btn save-btn" data-save="${saveId}">${ICON.save} Save changes</button></div>` : ""}
  </div>`;
}
function secTitle(icon, t, p) { return `<div class="sec-title"><div class="st-ic">${ICON[icon]}</div><div><h2>${t}</h2><p>${p}</p></div></div>`; }
function toggle(id, t, d, on) { return `<div class="toggle-row"><div class="tr-txt"><div class="t">${t}</div><div class="d">${d}</div></div><div class="switch ${on ? "on" : ""}" data-toggle="${id}"></div></div>`; }
function select(id, opts, val, fmt) { return `<select class="inp" id="${id}">${opts.map((o) => `<option value="${o}" ${String(o) === String(val) ? "selected" : ""}>${fmt ? fmt(o) : o}</option>`).join("")}</select>`; }

let tlsMode = "self-signed", authMethod = "dev", smtpTls = "starttls", mailMethod = "smtp";

function render() {
  tlsMode = cfg.RMM_TLS_MODE || "self-signed";
  smtpTls = cfg.SMTP_TLS || "starttls";
  mailMethod = cfg.SMTP_HOST ? "smtp" : (cfg.GRAPH_SENDER ? "graph" : "smtp");
  authMethod = USERS.mode || cfg.RMM_AUTH_MODE || "dev";
  const secure = (cfg.RMM_SECURE_COOKIES ?? "1") === "1";
  const main = $("settings-main");
  main.innerHTML = `
    <section class="sec on" data-sec="general">
      ${secTitle("gear", "General", "Identity and basics for this server.")}
      ${block("Server identity", "Shown in the header, emails and agent installers.",
        `<div class="frow"><label>Display name</label><input class="inp" id="g-name" value="${esc(cfg.RMM_SERVER_NAME || "Leuffen RMM")}" /></div>
         <div class="frow"><label>Public URL</label><input class="inp mono" id="g-url" value="${esc(cfg.RMM_PUBLIC_URL || location.origin)}" /><div class="hint">Used to build agent install commands and email links.</div></div>`, "general")}
      ${block("About this server", "Software version and container updates for the Leuffen RMM server.",
        `<div class="frow"><label>Server version</label><div class="ver-pill mono">${ICON.server} v${esc(cfg.RMM_VERSION || "—")}</div></div>
         <div class="frow"><label>Container update</label><div id="srv-update"><div class="muted">Checking…</div></div></div>`)}
      ${block("What's new", "Release notes for each version of Leuffen RMM.",
        `<div id="changelog-body"><div class="muted">Loading…</div></div>`)}
    </section>

    <section class="sec" data-sec="orgs">
      ${secTitle("building", "Organisations", "Tenancy — each org isolates its devices and members.")}
      <div class="card-block"><div class="cb-head" style="display:flex;align-items:center;justify-content:space-between"><div><h3>${ORGS.length} organisations</h3><p>Each tenant isolates its devices and members.</p></div><button class="btn" id="add-org">${ICON.plus} New organisation</button></div>
        <table class="utable"><thead><tr><th>Organisation</th><th>Devices</th><th>Members</th><th></th></tr></thead><tbody>
        ${ORGS.map((o) => `<tr><td><div class="u-cell"><div class="av" style="background:linear-gradient(140deg,${o.color},color-mix(in srgb,${o.color} 55%,#000))">${initials2(o.name)}</div><div><div class="un">${esc(o.name)}</div></div></div></td>
          <td>${o.devices}</td><td>${o.members ?? "—"}</td><td><div class="u-actions"><a class="btn ghost sm" href="/">Manage</a><button class="btn ghost sm org-del" data-id="${o.id}" data-name="${esc(o.name)}">${ICON.trash}</button></div></td></tr>`).join("") || `<tr><td colspan="4" class="muted" style="padding:20px">No organisations.</td></tr>`}
        </tbody></table></div>
    </section>

    <section class="sec" data-sec="users">
      ${secTitle("user", "Users & roles", "Who can sign in and what they can do.")}
      <div class="card-block">
        <div class="cb-head" style="display:flex;align-items:center;justify-content:space-between">
          <div><h3>${(USERS.users.length || USERS.bootstrap_admins.length)} ${authMethod === "local" ? "local accounts" : "administrators"}</h3><p>Global admins see everything; members are scoped to their organisations.</p></div>
          <button class="btn" id="invite-btn">${ICON.plus} Invite user</button>
        </div>
        <table class="utable"><thead><tr><th>User</th><th>Role</th><th>Last active</th><th></th></tr></thead><tbody>${usersRows()}</tbody></table>
      </div>
      <div class="card-block" id="invites-block" style="display:none">
        <div class="cb-head"><h3>Pending invitations</h3><p>Links expire after 2 days if not accepted.</p></div>
        <div id="invites-list"><div class="muted" style="padding:12px 0">Loading…</div></div>
      </div>
      <div class="card-block">
        <div class="cb-head" style="display:flex;align-items:center;justify-content:space-between">
          <div><h3>Access groups</h3><p>Bundle users together and assign them roles and permissions in organisations. Deny overrides allow across groups.</p></div>
          <button class="btn" id="ag-create-btn">${ICON.plus} New group</button>
        </div>
        <div id="ag-list"><div class="muted" style="padding:12px 0">Loading…</div></div>
      </div>
      <div class="callout info"><div class="ic">${ICON.info}</div><div><div class="ct">${authMethod === "local" ? "Local accounts" : "Single sign-on"}</div><div class="cd">${authMethod === "local" ? "Username/password accounts created during setup. Passwords are PBKDF2-hashed." : "SSO users must have a local account (via invite) before they can sign in — any M365 user without one is blocked."}</div></div></div>
    </section>

    <section class="sec" data-sec="auth">
      ${secTitle("lock", "Authentication", "How users prove who they are.")}
      ${block("Sign-in method", "The active method. Switching applies after a server restart.",
        `<div class="segmented" id="auth-seg"></div><div id="auth-extra" style="margin-top:4px"></div>`, "auth")}
      ${block("Microsoft 365 credentials", "Tenant and app registration for SSO sign-in and Graph mail. Changes apply after a server restart.",
        `<div class="callout info" style="margin-bottom:14px"><div class="ic">${ICON.info}</div><div><div class="ct">Required Entra app permissions</div><div class="cd">
           <b>API permissions</b> — Microsoft Graph → Application → <code>Mail.Send</code> (admin-consented, for alert emails).<br>
           <b>Authentication</b> → Redirect URI (Web): <code>${esc(location.origin)}/auth/callback</code>.<br>
           Find these in <b>Entra admin center → App registrations → your app</b>.
         </div></div></div>
         <div class="frow"><label>Tenant ID</label><input class="inp mono" id="ms-tenant" value="${esc(cfg.MS_TENANT_ID || "")}" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" /></div>
         <div class="frow"><label>Client ID</label><input class="inp mono" id="ms-client" value="${esc(cfg.MS_CLIENT_ID || "")}" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" /></div>
         <div class="frow"><label>Client secret</label><input class="inp mono" type="password" id="ms-secret" value="${esc(cfg.MS_CLIENT_SECRET || "")}" /></div>
         <div class="frow"><label>Redirect URI</label><input class="inp mono" id="ms-redirect" value="${esc(cfg.MS_REDIRECT_URI || (location.origin + "/auth/callback"))}" /><div class="hint">Must match the redirect URI registered in your Entra app.</div></div>
         <div id="sso-validation-msg" style="display:none;color:var(--bad);font-size:12.5px;margin-top:8px"></div>`, "auth-sso")}
      ${block("Two-factor authentication", "Time-based one-time codes (TOTP) for local accounts.",
        `${toggle("enforce2fa", "Require 2FA for local accounts", "Local users are prompted to set up an authenticator before they can use the dashboard.", (cfg.RMM_ENFORCE_2FA ?? "0") === "1")}
         <div class="callout info"><div class="ic">${ICON.info}</div><div><div class="ct">Per-user enrolment</div><div class="cd">Each user enables 2FA under <b>Account → Password</b>. ${authMethod === "local" ? "" : "Switch to local accounts to use this — SSO 2FA is managed in your identity provider."}</div></div></div>`, "auth-mfa")}
    </section>

    <section class="sec" data-sec="alerts">
      ${secTitle("bell", "Alerts & email", "When and how the server notifies you.")}
      ${block("Email delivery", "Choose how alert emails are sent.",
        `<div class="segmented" id="mail-method-seg"></div>
         <div id="mail-method-fields" style="margin-top:14px">
           <div id="mm-smtp">
             <div class="frow"><label>SMTP host</label><input class="inp mono" id="a-smtp-host" value="${esc(cfg.SMTP_HOST || "")}" placeholder="smtp.example.com" /></div>
             <div class="frow"><label>Port</label><input class="inp mono" id="a-smtp-port" value="${esc(cfg.SMTP_PORT || "")}" placeholder="587" style="width:100px" /></div>
             <div class="frow"><label>Encryption</label><div class="segmented" id="smtp-tls-seg"></div></div>
             <div class="frow"><label>Username</label><input class="inp mono" id="a-smtp-user" value="${esc(cfg.SMTP_USER || "")}" placeholder="alerts@example.com" /></div>
             <div class="frow"><label>Password</label><input class="inp mono" type="password" id="a-smtp-pass" value="${esc(cfg.SMTP_PASSWORD || "")}" /></div>
             <div class="frow"><label>From address</label><input class="inp mono" id="a-smtp-from" value="${esc(cfg.SMTP_FROM || "")}" placeholder="Leuffen RMM &lt;alerts@example.com&gt;" /></div>
           </div>
           <div id="mm-graph" style="display:none">
             <div class="callout info" style="margin-bottom:14px"><div class="ic">${ICON.info}</div><div><div class="ct">Required Entra app permissions</div><div class="cd">
               Microsoft Graph → Application → <code>Mail.Send</code> (admin-consented).<br>
               The sender mailbox must be a licensed Microsoft 365 mailbox.<br>
               Set up credentials in <b>Settings → Authentication → Microsoft 365 credentials</b>.
             </div></div></div>
             <div class="frow"><label>Sender mailbox</label><input class="inp mono" id="a-sender" value="${esc(cfg.GRAPH_SENDER || "")}" placeholder="alerts@yourdomain.com" /><div class="hint">A licensed mailbox with <code>Mail.Send</code> granted to the app.</div></div>
             <div class="frow"><label>From address</label><input class="inp mono" id="a-from" value="${esc(cfg.GRAPH_FROM || "")}" placeholder="Leuffen RMM &lt;alerts@yourdomain.com&gt;" /></div>
             <div id="graph-validation-msg" style="display:none;color:var(--bad);font-size:12.5px;margin-top:8px"></div>
           </div>
         </div>`, "alerts-delivery")}
      ${block("Recipients", "Who gets alert emails.",
        `<div class="frow"><label>Alert recipients</label><input class="inp mono" id="a-recipients" value="${esc(cfg.RMM_ALERT_RECIPIENTS || "")}" placeholder="ops@leuffen.it, admin@leuffen.it" /><div class="hint">Comma-separated. Per-organisation recipients can be set in each organisation's settings.</div></div>`, "alerts-recipients")}
      <div class="card-block">
        <div class="cb-head"><h3>Test delivery</h3><p>Send a test email to verify your current mail configuration is working.</p></div>
        <div class="cb-body">
          <div class="frow"><label>Send to</label><input class="inp mono" id="a-test-email" placeholder="you@example.com" /></div>
        </div>
        <div class="cb-foot"><span class="saved">${ICON.info} Uses your active SMTP or Graph config</span><button class="btn" id="a-test-send">${ICON.mail || ICON.bell} Send test email</button></div>
      </div>
      <div class="callout info"><div class="ic">${ICON.info}</div><div><div class="ct">Alert thresholds live in Monitors</div><div class="cd">Add CPU, memory, disk and offline alerts from the <b>Monitors</b> tab's template gallery — as site-only or global rules — instead of a fixed global policy.</div></div></div>
    </section>

    <section class="sec" data-sec="security">
      ${secTitle("shieldCheck", "Security", "TLS, sessions and access hardening.")}
      ${block("TLS termination", "Change how HTTPS is served. Applies on next restart.",
        `<div class="segmented" id="tls-seg"></div><div id="tls-extra" style="margin-top:4px"></div>`, "security-tls")}
      ${block("Session & access", "",
        `${toggle("secureCookies", "Secure cookies", "Only send session cookies over HTTPS. Disable only behind a TLS-terminating proxy on a trusted network.", secure)}`, "security-session")}
      ${block("Agent certificate pinning", "Pin this server's TLS certificate on agents so even a self-signed deployment is safe against man-in-the-middle.",
        `<div id="cert-fp" class="muted">Loading fingerprint…</div>`, null)}
      ${block("Device identity", "On by default for new installs. Safe to enable once your whole fleet runs an agent that supports it (v2.2.x+) — older agents that can't present a secret will be rejected.",
        `${toggle("requireDeviceSecret", "Require device secret", "Reject reconnects that use a device ID without the matching per-device secret, so a stolen device ID alone can't impersonate a machine.", (cfg.RMM_REQUIRE_DEVICE_SECRET ?? "0") === "1")}`, "security-devsecret")}
      <div class="card-block">
        <div class="cb-head"><h3>Danger zone</h3><p>Reset all server configuration and re-run the first-run setup wizard.</p></div>
        <div class="cb-body"><div class="callout warn"><div class="ic">${ICON.alert}</div><div><div class="ct">Reset configuration</div><div class="cd">Clears auth mode, TLS, public URL and secrets, then sends you to <b>Setup</b>. Your devices, organisations and accounts are kept. A restart applies the new settings.</div></div></div></div>
        <div class="cb-foot"><span class="saved">${ICON.alert} This cannot be undone</span><button class="btn danger" id="reset-config">${ICON.trash} Reset &amp; re-run setup</button></div>
      </div>
    </section>

    <section class="sec" data-sec="agents">
      ${secTitle("monitor", "Agents", "Defaults applied to every connected agent.")}
      ${block("Enrolment", "",
        `${toggle("requireApproval", "Require approval for new devices", "New agents wait in the Approvals queue until you approve them, instead of appearing automatically.", (cfg.RMM_REQUIRE_APPROVAL ?? "1") === "1")}`, "agents-approval")}
      <div class="callout info"><div class="ic">${ICON.info}</div><div><div class="ct">Heartbeat interval</div><div class="cd">Agents report every ~30s by default (set <code>RMM_INTERVAL</code> on the agent). Update agents in place from a device's <b>Agent</b> panel, or all at once from <b>Downloads</b>.</div></div></div>
      <div class="callout warn"><div class="ic">${ICON.alert}</div><div><div class="ct">Danger zone</div><div class="cd">Removing an agent (from a device's Actions) stops monitoring and revokes its key. Re-enrol with a fresh installer from Downloads.</div></div></div>
    </section>

    <section class="sec" data-sec="logs">
      ${secTitle("terminal", "Logs", "Recent server activity. Held in memory — newest at the bottom.")}
      <div class="card-block">
        <div class="cb-head" style="display:flex;align-items:center;justify-content:space-between;gap:12px">
          <div><h3>Server log</h3><p>Leuffen RMM server <span class="mono">v${esc(cfg.RMM_VERSION || "—")}</span></p></div>
          <div style="display:flex;align-items:center;gap:8px">
            ${select("log-level", ["all", "INFO", "WARNING", "ERROR"], "all")}
            <button class="btn ghost sm" id="log-refresh">${ICON.refresh} Refresh</button>
          </div>
        </div>
        <div class="cb-body"><div class="log-view" id="log-view"><div class="muted" style="padding:14px">Loading…</div></div></div>
      </div>
    </section>

    <section class="sec" data-sec="appearance">
      ${secTitle("sliders", "Appearance", "The workspace default look. Applies to everyone who hasn't set their own in Account.")}
      ${block("Theme & colour", "Default theme and accent for the dashboard, settings and setup.",
        `<div class="frow"><label>Mode</label><div class="segmented" id="ap-theme"></div></div>
         <div class="frow"><label>Accent colour</label><div id="ap-accent" style="display:flex;flex-wrap:wrap;gap:10px"></div></div>`)}
      ${block("Layout", "",
        `<div class="frow"><label>Density</label><div class="segmented" id="ap-density"></div></div>
         <div class="frow"><label>Corner roundness <span class="hint" id="ap-round-val" style="font-weight:500"></span></label><input type="range" id="ap-round" min="0" max="1.8" step="0.1" class="ap-range" /></div>
         <div class="frow"><label>Interface font</label>${select("ap-font", ["Onest", "Inter", "System"], Appearance.getGlobal().font)}</div>
         ${toggle("ap-dataviz", "Charts & sparklines", "Show inline trend charts and KPI sparklines.", Appearance.getGlobal().dataviz)}`)}
      <div class="callout info"><div class="ic">${ICON.info}</div><div><div class="ct">This is the default, not a lock</div><div class="cd">Each user can override these in <b>Account → Appearance</b>. Changes here apply instantly to anyone following the default.</div></div></div>
      <div style="display:flex;justify-content:flex-end"><button class="btn ghost" id="ap-reset">${ICON.refresh} Reset to defaults</button></div>
    </section>`;

  buildAuthSeg();
  buildMailMethodSeg();
  buildSmtpTlsSeg();
  buildTlsSeg();
  buildAppearance();
  wire();
}

function usersRows() {
  if ((authMethod === "local" || authMethod === "hybrid") && USERS.users.length) {
    return USERS.users.map((u) => `<tr>
      <td><div class="u-cell"><div class="av" style="background:linear-gradient(140deg,${colorFor(u.username)},color-mix(in srgb,${colorFor(u.username)} 50%,#000))">${initials2(u.username)}</div><div><div class="un">${esc(u.display_name || u.username)}</div><div class="ue">${esc(u.email || "@" + u.username)}${u.email && !u.email_verified ? ` <span class="role-pill" style="padding:0 7px;font-size:10.5px" title="Email not verified yet">unverified</span>` : ""}</div></div></div></td>
      <td><span class="role-pill ${u.is_admin ? "admin" : "member"}">${u.is_admin ? ICON.shieldCheck : ICON.user} ${u.is_admin ? "Global admin" : "Member"}</span></td>
      <td class="muted">${u.last_active ? new Date(u.last_active * 1000).toLocaleString() : "never"}</td>
      <td><div class="u-actions"><button class="btn ghost sm user-edit" data-username="${esc(u.username)}" title="Edit user">${ICON.pencil}</button><button class="btn ghost sm user-del" data-username="${esc(u.username)}" title="Delete user">${ICON.trash}</button></div></td></tr>`).join("");
  }
  const admins = USERS.bootstrap_admins.length ? USERS.bootstrap_admins : (USERS.users.map((u) => u.email || u.username));
  if (!admins.length) return `<tr><td colspan="4" class="muted" style="padding:20px">No administrators configured.</td></tr>`;
  return admins.map((e) => `<tr>
    <td><div class="u-cell"><div class="av" style="background:linear-gradient(140deg,${colorFor(e)},color-mix(in srgb,${colorFor(e)} 50%,#000))">${initials2(e)}</div><div><div class="un">${esc(e)}</div></div></div></td>
    <td><span class="role-pill admin">${ICON.shieldCheck} Global admin</span></td>
    <td class="muted">—</td><td></td></tr>`).join("");
}

async function loadInvites() {
  const block = $("invites-block"), list = $("invites-list");
  if (!list) return;
  try {
    const { invites } = await api("/api/invites");
    if (!invites.length) { block.style.display = "none"; return; }
    block.style.display = "";
    list.innerHTML = `<table class="utable"><thead><tr><th>Email</th><th>Role</th><th>Expires</th><th></th></tr></thead><tbody>
      ${invites.map((i) => `<tr>
        <td>${esc(i.email)}</td>
        <td><span class="role-pill ${i.is_admin ? "admin" : "member"}">${i.is_admin ? "Global admin" : "Member"}</span></td>
        <td class="muted">${new Date(i.expires_at * 1000).toLocaleString()}</td>
        <td><button class="btn ghost sm inv-revoke" data-token="${esc(i.token)}" title="Revoke">${ICON.trash}</button></td>
      </tr>`).join("")}
    </tbody></table>`;
    list.querySelectorAll(".inv-revoke").forEach((b) => b.onclick = async () => {
      await api(`/api/invites/${b.dataset.token}`, { method: "DELETE" });
      toast("Invite revoked"); loadInvites();
    });
  } catch (e) { list.innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
}

function openInviteModal() {
  const m = modal("Invite user", `
    ${mfield("Email address", `<input class="inp mono" id="iv-email" type="email" placeholder="person@example.com" />`)}
    ${mfield("Role", `<select class="inp" id="iv-role"><option value="0">Member</option><option value="1">Global admin</option></select>`)}
    ${mfield("How to send", `<select class="inp" id="iv-delivery"><option value="both">Email + link</option><option value="email">Email only</option><option value="link">Link only</option></select>`,
      "The invitee verifies this email with a code when they set up their account.")}
    <div id="iv-err" style="color:var(--bad);font-size:12.5px;min-height:16px;margin-bottom:6px"></div>
    <div id="iv-result" style="display:none"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:4px">
      <button class="btn ghost" id="iv-cancel" type="button">Cancel</button>
      <button class="btn" id="iv-send" type="button">${ICON.mail} Send invite</button>
    </div>`);
  m.q("#iv-cancel").onclick = m.close;
  m.q("#iv-email").focus();
  m.q("#iv-send").onclick = async () => {
    const email = m.q("#iv-email").value.trim();
    const err = m.q("#iv-err"); err.textContent = "";
    if (!email || !email.includes("@")) { err.textContent = "Enter a valid email address"; return; }
    const body = { email, is_admin: m.q("#iv-role").value === "1", delivery: m.q("#iv-delivery").value };
    const btn = m.q("#iv-send"); btn.disabled = true; const orig = btn.innerHTML; btn.textContent = "Sending…";
    try {
      const r = await api("/api/invites", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      loadInvites();
      const parts = [];
      if (r.emailed) parts.push(`Invitation emailed to <b>${esc(email)}</b>.`);
      else if (body.delivery !== "link") parts.push(`<span style="color:var(--warn)">Email couldn't be sent${r.mail_configured ? "" : " (no mail delivery configured)"} — share the link below instead.</span>`);
      const result = m.q("#iv-result");
      result.style.display = "";
      result.innerHTML = `<div style="font-size:12.5px;color:var(--text-dim);margin-bottom:10px">${parts.join(" ")}</div>` +
        (r.invite_url ? `<div style="display:flex;gap:6px">
            <input class="inp mono" id="iv-link" readonly value="${esc(r.invite_url)}" style="flex:1" />
            <button class="btn ghost sm" id="iv-copy" type="button">${ICON.copy} Copy</button>
          </div><div class="hint" style="margin-top:6px">Link expires in 2 days.</div>` : "");
      // Swap the action button to a Done once the invite exists.
      btn.style.display = "none"; m.q("#iv-cancel").textContent = "Done";
      const copy = m.q("#iv-copy");
      if (copy) copy.onclick = () => { navigator.clipboard.writeText(r.invite_url); m.q("#iv-link").select(); toast("Link copied"); };
    } catch (e) { err.textContent = e.message; btn.disabled = false; btn.innerHTML = orig; }
  };
}

function openEditUserModal(u) {
  const m = modal("Edit user", `
    ${mfield("Display name", `<input class="inp" id="eu-name" value="${esc(u.display_name || "")}" placeholder="${esc(u.username)}" />`)}
    ${mfield("Email", `<input class="inp mono" id="eu-email" type="email" value="${esc(u.email || "")}" placeholder="person@example.com" />`)}
    ${mfield("Role", `<select class="inp" id="eu-role"><option value="0" ${u.is_admin ? "" : "selected"}>Member</option><option value="1" ${u.is_admin ? "selected" : ""}>Global admin</option></select>`)}
    ${mfield("Reset password", `<input class="inp mono" id="eu-pw" type="password" autocomplete="new-password" placeholder="Leave blank to keep current" />`, "At least 8 characters.")}
    <div id="eu-err" style="color:var(--bad);font-size:12.5px;min-height:16px;margin-bottom:6px"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:4px">
      <button class="btn ghost" id="eu-cancel" type="button">Cancel</button>
      <button class="btn" id="eu-save" type="button">${ICON.check} Save changes</button>
    </div>`);
  m.q("#eu-cancel").onclick = m.close;
  m.q("#eu-name").focus();
  m.q("#eu-save").onclick = async () => {
    const err = m.q("#eu-err"); err.textContent = "";
    const body = {
      display_name: m.q("#eu-name").value,
      email: m.q("#eu-email").value.trim(),
      is_admin: m.q("#eu-role").value === "1",
    };
    const pw = m.q("#eu-pw").value;
    if (pw) body.password = pw;
    const btn = m.q("#eu-save"); btn.disabled = true; const orig = btn.innerHTML; btn.textContent = "Saving…";
    try {
      await api(`/api/users/${encodeURIComponent(u.username)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      toast("User updated"); m.close();
      USERS = await api("/api/users"); render(); selectSec("users");
    } catch (e) { err.textContent = e.message; btn.disabled = false; btn.innerHTML = orig; }
  };
}

// --------------------------------------------------------------------------- //
// Access groups
// --------------------------------------------------------------------------- //
const PERM_LABELS = {
  terminal: "Remote terminal",
  scripts: "Run scripts",
  power: "Power actions",
  wol: "Wake-on-LAN",
  device_delete: "Delete device",
  agent_delete: "Remove agent",
};
const ALL_PERMS = Object.keys(PERM_LABELS);

let GROUPS = [];

function effectBadge(p) {
  if (p.effect === "deny") {
    const parts = [`<span class="perm-badge deny">✗ Denied`];
    if (p.denied_by && p.denied_by.length) parts.push(` by ${esc(p.denied_by.join(", "))}`);
    parts.push("</span>");
    if (p.allowed_by && p.allowed_by.length)
      parts.push(` <span class="perm-note">(${esc(p.allowed_by.join(", "))} would allow)</span>`);
    return parts.join("");
  }
  const parts = [`<span class="perm-badge allow">✓ Allowed`];
  if (p.allowed_by && p.allowed_by.length) parts.push(` by ${esc(p.allowed_by.join(", "))}`);
  parts.push("</span>");
  return parts.join("");
}

async function loadGroups() {
  const host = $("ag-list"); if (!host) return;
  try {
    const { groups } = await api("/api/access-groups");
    GROUPS = groups;
    if (!groups.length) {
      host.innerHTML = `<div class="muted" style="padding:12px 0">No groups yet. Create one to bundle users and assign permissions per organisation.</div>`;
      return;
    }
    host.innerHTML = groups.map((g) => `
      <div class="ag-card" data-gid="${esc(g.id)}">
        <div class="ag-header">
          <div class="ag-name">${esc(g.name)}</div>
          <div class="u-actions">
            <button class="btn ghost sm ag-rename" data-gid="${esc(g.id)}" data-name="${esc(g.name)}">${ICON.edit || ICON.sliders} Rename</button>
            <button class="btn ghost sm ag-del" data-gid="${esc(g.id)}" data-name="${esc(g.name)}">${ICON.trash}</button>
          </div>
        </div>
        <div class="ag-body">
          <div class="ag-col">
            <div class="ag-col-title">Members</div>
            <div class="ag-members" data-gid="${esc(g.id)}">
              ${g.members.length ? g.members.map((e) => `<div class="ag-member-row">
                <span class="mono" style="font-size:12px">${esc(e)}</span>
                <button class="btn ghost sm ag-rm-member" data-gid="${esc(g.id)}" data-email="${esc(e)}" title="Remove">${ICON.trash}</button>
              </div>`).join("") : `<span class="muted" style="font-size:12px">No members</span>`}
            </div>
            <button class="btn ghost sm ag-add-member" data-gid="${esc(g.id)}" style="margin-top:8px">${ICON.plus} Add user</button>
          </div>
          <div class="ag-col">
            <div class="ag-col-title">Organisation access</div>
            ${g.orgs.length ? g.orgs.map((o) => `
              <div class="ag-org-row" data-gid="${esc(g.id)}" data-oid="${esc(o.org_id)}">
                <div class="ag-org-top">
                  <span class="ag-org-name">${esc(o.org_name)}</span>
                  <span class="role-pill ${o.role}">${o.role}</span>
                  <button class="btn ghost sm ag-rm-org" data-gid="${esc(g.id)}" data-oid="${esc(o.org_id)}" title="Remove org">${ICON.trash}</button>
                </div>
                <div class="ag-perms">
                  ${ALL_PERMS.map((p) => {
                    const ov = o.perms && o.perms[p];
                    const overrideClass = ov === "deny" ? " perm-deny-ov" : ov === "allow" ? " perm-allow-ov" : "";
                    return `<button class="btn ghost sm perm-toggle${overrideClass}" data-gid="${esc(g.id)}" data-oid="${esc(o.org_id)}" data-perm="${p}" title="${PERM_LABELS[p]}">
                      ${ov === "deny" ? "✗" : ov === "allow" ? "✓" : "·"} ${esc(PERM_LABELS[p])}
                    </button>`;
                  }).join("")}
                </div>
              </div>`).join("") : `<span class="muted" style="font-size:12px">No organisations assigned</span>`}
            <button class="btn ghost sm ag-add-org" data-gid="${esc(g.id)}" style="margin-top:8px">${ICON.plus} Add organisation</button>
          </div>
        </div>
      </div>`).join("");

    // wire events
    host.querySelectorAll(".ag-del").forEach((b) => b.onclick = async () => {
      if (!confirm(`Delete group "${b.dataset.name}"? This removes all its org access.`)) return;
      try { await api(`/api/access-groups/${b.dataset.gid}`, { method: "DELETE" }); toast("Group deleted"); loadGroups(); }
      catch (e) { toast(e.message); }
    });
    host.querySelectorAll(".ag-rename").forEach((b) => b.onclick = async () => {
      const name = (prompt("New group name:", b.dataset.name) || "").trim();
      if (!name || name === b.dataset.name) return;
      try { await api(`/api/access-groups/${b.dataset.gid}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }); toast("Renamed"); loadGroups(); }
      catch (e) { toast(e.message); }
    });
    host.querySelectorAll(".ag-add-member").forEach((b) => b.onclick = async () => {
      const email = (prompt("User email to add:") || "").trim().toLowerCase();
      if (!email) return;
      try { await api(`/api/access-groups/${b.dataset.gid}/members`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user_email: email }) }); toast("Member added"); loadGroups(); }
      catch (e) { toast(e.message); }
    });
    host.querySelectorAll(".ag-rm-member").forEach((b) => b.onclick = async () => {
      try { await api(`/api/access-groups/${b.dataset.gid}/members/${encodeURIComponent(b.dataset.email)}`, { method: "DELETE" }); toast("Member removed"); loadGroups(); }
      catch (e) { toast(e.message); }
    });
    host.querySelectorAll(".ag-add-org").forEach((b) => b.onclick = async () => {
      if (!ORGS.length) { toast("No organisations yet"); return; }
      const choices = ORGS.map((o, i) => `${i + 1}. ${o.name}`).join("\n");
      const pick = prompt(`Pick organisation:\n${choices}\n\nEnter number:`);
      if (!pick) return;
      const org = ORGS[parseInt(pick, 10) - 1];
      if (!org) { toast("Invalid choice"); return; }
      const rolePick = prompt("Role (admin / member / viewer):", "member");
      if (!rolePick) return;
      try { await api(`/api/access-groups/${b.dataset.gid}/orgs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ org_id: org.id, role: rolePick.trim() }) }); toast("Org access added"); loadGroups(); }
      catch (e) { toast(e.message); }
    });
    host.querySelectorAll(".ag-rm-org").forEach((b) => b.onclick = async () => {
      if (!confirm("Remove this organisation from the group?")) return;
      try { await api(`/api/access-groups/${b.dataset.gid}/orgs/${b.dataset.oid}`, { method: "DELETE" }); toast("Org access removed"); loadGroups(); }
      catch (e) { toast(e.message); }
    });
    host.querySelectorAll(".perm-toggle").forEach((b) => b.onclick = async () => {
      const { gid, oid, perm } = b.dataset;
      // Cycle: inherit → deny → allow → deny (deny first, then allow)
      const cur = b.classList.contains("perm-deny-ov") ? "deny" : b.classList.contains("perm-allow-ov") ? "allow" : "inherit";
      let next = cur === "inherit" ? "deny" : cur === "deny" ? "allow" : "inherit";
      try {
        if (next === "inherit") {
          await api(`/api/access-groups/${gid}/orgs/${oid}/perms/${perm}`, { method: "DELETE" });
        } else {
          await api(`/api/access-groups/${gid}/orgs/${oid}/perms`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ permission: perm, effect: next }) });
        }
        loadGroups();
      } catch (e) { toast(e.message); }
    });
  } catch (e) { host.innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
}

const AUTH_METHODS = [
  { id: "hybrid", t: "Local + Microsoft 365", d: "Password accounts plus optional M365 SSO.", icon: "shieldCheck" },
  { id: "dev", t: "Dev login", d: "Auto bootstrap admin.", icon: "zap" },
];
function buildAuthSeg() {
  const seg = $("auth-seg"); if (!seg) return;
  seg.innerHTML = "";
  AUTH_METHODS.forEach((m) => {
    const o = document.createElement("button");
    o.className = "seg-opt" + (authMethod === m.id ? " sel" : "");
    o.innerHTML = `<div class="so-top"><span class="so-ic">${ICON[m.icon]}</span><span class="so-t">${m.t}</span></div><div class="so-d">${m.d}</div>`;
    o.onclick = () => { authMethod = m.id; buildAuthSeg(); };
    seg.appendChild(o);
  });
  const ex = $("auth-extra");
  ex.innerHTML = authMethod === "sso"
    ? `<div class="callout info"><div class="ic">${ICON.globe}</div><div><div class="ct">Microsoft 365 SSO</div><div class="cd">Tenant/client credentials are set during setup. Switching here changes the active mode after a restart.</div></div></div>`
    : authMethod === "local"
    ? `<div class="callout info"><div class="ic">${ICON.lock}</div><div><div class="ct">Local accounts</div><div class="cd">Manage accounts under <b>Users &amp; roles</b>. Passwords are PBKDF2-hashed on this server.</div></div></div>`
    : authMethod === "hybrid"
    ? `<div class="callout info"><div class="ic">${ICON.shieldCheck}</div><div><div class="ct">Local accounts + Microsoft 365</div><div class="cd">Both sign-in methods are offered. A Microsoft 365 user is matched to a local account with the same email, so they share one identity and its admin rights. Requires the SSO credentials configured at setup.</div></div></div>`
    : `<div class="callout warn"><div class="ic">${ICON.alert}</div><div><div class="ct">Dev login is on</div><div class="cd">Anyone reaching this server is signed in as bootstrap admin. Switch before production.</div></div></div>`;
}

const TLS_MODES = [
  { id: "self-signed", t: "Self-signed", d: "Auto certificate.", icon: "shield" },
  { id: "file", t: "Certificate file", d: "Your own cert/key.", icon: "lock" },
  { id: "proxy", t: "Behind a proxy", d: "TLS upstream.", icon: "network" },
];
const MAIL_METHODS = [
  { id: "smtp", t: "SMTP", d: "Any mail server" },
  { id: "graph", t: "Microsoft Graph", d: "Microsoft 365 tenant" },
];
function buildMailMethodSeg() {
  const seg = $("mail-method-seg"); if (!seg) return;
  seg.innerHTML = "";
  MAIL_METHODS.forEach((m) => {
    const o = document.createElement("button");
    o.className = "seg-opt" + (mailMethod === m.id ? " sel" : "");
    o.innerHTML = `<div class="so-top"><span class="so-t">${m.t}</span></div><div class="so-d">${m.d}</div>`;
    o.onclick = () => { mailMethod = m.id; buildMailMethodSeg(); };
    seg.appendChild(o);
  });
  const smtp = $("mm-smtp"), graph = $("mm-graph");
  if (smtp) smtp.style.display = mailMethod === "smtp" ? "" : "none";
  if (graph) graph.style.display = mailMethod === "graph" ? "" : "none";
}
const SMTP_TLS_MODES = [
  { id: "starttls", t: "STARTTLS", d: "Port 587 (recommended)" },
  { id: "ssl",      t: "SSL/TLS",  d: "Port 465" },
  { id: "none",     t: "None",     d: "Unencrypted" },
];
function buildSmtpTlsSeg() {
  const seg = $("smtp-tls-seg"); if (!seg) return;
  seg.innerHTML = "";
  SMTP_TLS_MODES.forEach((m) => {
    const o = document.createElement("button");
    o.className = "seg-opt" + (smtpTls === m.id ? " sel" : "");
    o.innerHTML = `<div class="so-top"><span class="so-t">${m.t}</span></div><div class="so-d">${m.d}</div>`;
    o.onclick = () => { smtpTls = m.id; buildSmtpTlsSeg(); };
    seg.appendChild(o);
  });
}
function buildTlsSeg() {
  const seg = $("tls-seg"); if (!seg) return;
  seg.innerHTML = "";
  TLS_MODES.forEach((m) => {
    const o = document.createElement("button");
    o.className = "seg-opt" + (tlsMode === m.id ? " sel" : "");
    o.innerHTML = `<div class="so-top"><span class="so-ic">${ICON[m.icon]}</span><span class="so-t">${m.t}</span></div><div class="so-d">${m.d}</div>`;
    o.onclick = () => { tlsMode = m.id; buildTlsSeg(); };
    seg.appendChild(o);
  });
  const ex = $("tls-extra");
  ex.innerHTML = tlsMode === "file"
    ? `<div class="callout info" style="margin-top:14px"><div class="ic">${ICON.lock}</div><div><div class="ct">Certificate files</div><div class="cd">Set <code>RMM_TLS_CERT</code> / <code>RMM_TLS_KEY</code> (default <code>/data/tls/*</code>).</div></div></div>`
    : tlsMode === "proxy"
    ? `<div class="callout warn" style="margin-top:14px"><div class="ic">${ICON.alert}</div><div><div class="ct">Plain HTTP behind the proxy</div><div class="cd">Trusts <code>X-Forwarded-*</code>. Ensure only your proxy can reach this port.</div></div></div>`
    : `<div class="callout info" style="margin-top:14px"><div class="ic">${ICON.shield}</div><div><div class="ct">Self-signed certificate</div><div class="cd">Generated automatically into the data volume. Browsers warn unless you trust it.</div></div></div>`;
}

function segControl(host, opts, current, onPick) {
  if (!host) return;
  host.innerHTML = ""; host.style.gridAutoFlow = "column";
  opts.forEach((o) => {
    const b = document.createElement("button");
    b.className = "seg-opt" + (current === o.id ? " sel" : "");
    b.innerHTML = `<div class="so-top"><span class="so-ic">${ICON[o.icon]}</span><span class="so-t">${o.t}</span></div>`;
    b.onclick = () => onPick(o.id);
    host.appendChild(b);
  });
}
function buildAppearance() {
  const a = Appearance.getGlobal();
  if (!$("ap-theme")) return;
  segControl($("ap-theme"), [{ id: "dark", t: "Dark", icon: "shield" }, { id: "light", t: "Light", icon: "globe" }], a.theme, (v) => { Appearance.setGlobal("theme", v); buildAppearance(); });
  segControl($("ap-density"), [{ id: "comfortable", t: "Comfortable", icon: "sliders" }, { id: "compact", t: "Compact", icon: "sliders" }], a.density, (v) => { Appearance.setGlobal("density", v); buildAppearance(); });
  const acc = $("ap-accent"); acc.innerHTML = "";
  Appearance.ACCENTS.forEach((hex) => {
    const sw = document.createElement("button");
    const on = a.accent.toLowerCase() === hex.toLowerCase();
    sw.style.cssText = `width:30px;height:30px;border-radius:50%;cursor:pointer;background:${hex};border:2px solid ${on ? "var(--text)" : "transparent"};box-shadow:0 0 0 3px ${on ? "var(--accent-ring)" : "transparent"};transition:.15s`;
    sw.onclick = () => { Appearance.setGlobal("accent", hex); buildAppearance(); };
    acc.appendChild(sw);
  });
  const round = $("ap-round"); round.value = a.roundness;
  $("ap-round-val").textContent = a.roundness == 0 ? "sharp" : a.roundness >= 1.6 ? "round" : "default";
  round.oninput = (e) => { const v = parseFloat(e.target.value); Appearance.setGlobal("roundness", v); $("ap-round-val").textContent = v == 0 ? "sharp" : v >= 1.6 ? "round" : "default"; };
  const font = $("ap-font"); if (font) font.onchange = (e) => Appearance.setGlobal("font", e.target.value);
  const dv = document.querySelector('[data-toggle="ap-dataviz"]');
  if (dv) dv.onclick = () => { dv.classList.toggle("on"); Appearance.setGlobal("dataviz", dv.classList.contains("on")); };
  const rs = $("ap-reset"); if (rs) rs.onclick = () => { Appearance.resetGlobal(); buildAppearance(); toast("Workspace default reset"); };
}

async function reloadOrgs() {
  ORGS = await api("/api/overview").then((d) => d.orgs.map((o) => ({ ...o, color: colorFor(o.id) }))).catch(() => ORGS);
  render(); selectSec("orgs");
}
async function createOrg() {
  const name = (prompt("New organisation name?") || "").trim();
  if (!name) return;
  try { await api("/api/orgs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }); toast("Organisation created"); await reloadOrgs(); }
  catch (e) { toast(e.message); }
}
async function deleteOrg(id, name) {
  if (!confirm(`Delete organisation “${name}”?\n\nAll of its devices, scripts and data are permanently removed.`)) return;
  try { await api(`/api/orgs/${id}`, { method: "DELETE" }); toast("Organisation deleted"); await reloadOrgs(); }
  catch (e) { toast(e.message); }
}

const CL_PAGE_SIZE = 5;
let clSections = [], clPage = 0;

function renderChangelog() {
  const host = $("changelog-body");
  if (!host || !clSections.length) return;
  const total = clSections.length;
  const pages = Math.ceil(total / CL_PAGE_SIZE);
  const slice = clSections.slice(clPage * CL_PAGE_SIZE, (clPage + 1) * CL_PAGE_SIZE);
  let html = `<div class="cl-wrap">${slice.join("")}</div>`;
  if (pages > 1) {
    html += `<div class="cl-pager">
      <button class="btn ghost sm" id="cl-prev" ${clPage === 0 ? "disabled" : ""}>&larr; Newer</button>
      <span class="cl-page-info">${clPage + 1} / ${pages}</span>
      <button class="btn ghost sm" id="cl-next" ${clPage >= pages - 1 ? "disabled" : ""}>Older &rarr;</button>
    </div>`;
  }
  host.innerHTML = html;
  const p = $("cl-prev"); if (p) p.onclick = () => { clPage--; renderChangelog(); };
  const n = $("cl-next"); if (n) n.onclick = () => { clPage++; renderChangelog(); };
}

async function loadChangelog() {
  const host = $("changelog-body");
  if (!host) return;
  try {
    const { md } = await api("/api/changelog");
    if (!md) { host.innerHTML = `<div class="muted">No changelog available.</div>`; return; }
    // Split into per-version sections at each ## heading
    clSections = [];
    let cur = null;
    for (const line of md.split("\n")) {
      if (line.startsWith("## ")) {
        if (cur !== null) clSections.push(cur);
        cur = `<h3 class="cl-ver">${esc(line.slice(3))}</h3>`;
      } else if (cur !== null) {
        if (line.startsWith("### ")) {
          cur += `<div class="cl-cat">${esc(line.slice(4))}</div>`;
        } else if (line.startsWith("- ")) {
          const inner = line.slice(2).replace(/\*\*(.+?)\*\*/g, (_, t) => `<strong>${esc(t)}</strong>`);
          cur += `<div class="cl-item">${inner}</div>`;
        }
      }
    }
    if (cur !== null) clSections.push(cur);
    clPage = 0;
    renderChangelog();
  } catch (e) {
    host.innerHTML = `<div class="muted">${esc(e.message)}</div>`;
  }
}
async function loadServerUpdate() {
  const host = $("srv-update");
  if (!host) return;
  let st;
  try { st = await api("/api/server/update"); }
  catch (e) { host.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
  renderServerUpdate(st);
}
function renderServerUpdate(st) {
  const host = $("srv-update");
  if (!host) return;
  if (!st.available) {
    host.innerHTML = `<div class="upd-row"><span class="badge na">unavailable</span>
      <span class="hint">${esc(st.reason || "In-UI updates are off")}. Mount <code>/var/run/docker.sock</code> and use a registry image to enable one-click updates.</span></div>`;
    return;
  }
  const staged = st.update_staged;
  host.innerHTML = `<div class="upd-row">
      ${staged ? `<span class="badge ok">update ready</span>` : `<span class="badge na">up to date</span>`}
      <button class="btn ghost sm" id="srv-check">${ICON.refresh} Check for updates</button>
      <button class="btn sm" id="srv-apply" ${staged ? "" : "disabled"}>${ICON.download} Update &amp; restart</button>
    </div>
    <div class="hint" style="margin-top:8px">Image <span class="mono">${esc(st.image || "—")}</span></div>`;
  $("srv-check").onclick = async () => {
    const b = $("srv-check"), o = b.innerHTML; b.disabled = true; b.innerHTML = "Checking…";
    try { const r = await api("/api/server/update/check", { method: "POST" }); renderServerUpdate(r); toast(r.update_staged ? "Update available" : "Already up to date"); }
    catch (e) { toast(e.message); b.disabled = false; b.innerHTML = o; }
  };
  $("srv-apply").onclick = async () => {
    if (!confirm("Pull the latest image and restart the server container now?\n\nThe dashboard will be briefly unavailable while it restarts.")) return;
    const b = $("srv-apply"), o = b.innerHTML; b.disabled = true; b.innerHTML = "Updating…";
    try {
      const r = await api("/api/server/update/apply", { method: "POST" });
      host.innerHTML = `<div class="callout info"><div class="ic">${ICON.info}</div><div><div class="ct">Updating…</div><div class="cd">${esc(r.note || "The server is restarting.")} This page will reconnect automatically.</div></div></div>`;
      waitForServerBack();
    } catch (e) { toast(e.message); b.disabled = false; b.innerHTML = o; }
  };
}
function waitForServerBack() {
  let tries = 0;
  const t = setInterval(async () => {
    tries++;
    try { const r = await fetch("/api/health", { cache: "no-store" }); if (r.ok) { clearInterval(t); toast("Server is back — reloading"); setTimeout(() => location.reload(), 800); } }
    catch {}
    if (tries > 60) clearInterval(t);
  }, 3000);
}

function wire() {
  document.querySelectorAll("[data-toggle]:not([data-toggle='ap-dataviz'])").forEach((t) => t.onclick = () => t.classList.toggle("on"));
  document.querySelectorAll(".save-btn").forEach((b) => b.onclick = () => onSave(b.dataset.save));
  const oc = $("add-org"); if (oc) oc.onclick = createOrg;
  document.querySelectorAll(".org-del").forEach((b) => b.onclick = () => deleteOrg(b.dataset.id, b.dataset.name));
  document.querySelectorAll(".user-edit").forEach((b) => b.onclick = () => {
    const u = (USERS.users || []).find((x) => x.username === b.dataset.username);
    if (u) openEditUserModal(u);
  });
  document.querySelectorAll(".user-del").forEach((b) => b.onclick = async () => {
    const un = b.dataset.username;
    if (!confirm(`Delete user "${un}"? This cannot be undone.`)) return;
    try { await api(`/api/users/${encodeURIComponent(un)}`, { method: "DELETE" }); toast("User deleted"); USERS = await api("/api/users"); render(); selectSec("users"); }
    catch (e) { toast(e.message); }
  });
  const ib = $("invite-btn");
  if (ib) ib.onclick = openInviteModal;
  const testSend = $("a-test-send");
  if (testSend) testSend.onclick = async () => {
    const email = ($("a-test-email").value || "").trim();
    if (!email) { toast("Enter a recipient email address"); return; }
    const orig = testSend.innerHTML; testSend.disabled = true; testSend.textContent = "Sending…";
    try { await api("/api/mail/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email }) }); toast(`Test email sent to ${email}`); }
    catch (e) { toast(e.message); }
    finally { testSend.disabled = false; testSend.innerHTML = orig; }
  };
  loadInvites();
  loadGroups();
  const agCreate = $("ag-create-btn");
  if (agCreate) agCreate.onclick = async () => {
    const name = (prompt("Group name:") || "").trim();
    if (!name) return;
    try { await api("/api/access-groups", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }); toast("Group created"); loadGroups(); }
    catch (e) { toast(e.message); }
  };
  const lr = $("log-refresh"); if (lr) lr.onclick = loadLogs;
  const ll = $("log-level"); if (ll) ll.onchange = loadLogs;
  loadServerUpdate();
  loadChangelog();
  const rc = $("reset-config");
  if (rc) rc.onclick = async () => {
    if (!confirm("Reset ALL server configuration and re-run setup?\n\nDevices, organisations and accounts are kept. You'll be sent to the setup wizard.")) return;
    if (!confirm("Are you sure? This clears auth mode, TLS, public URL and secrets.")) return;
    try { await api("/api/admin/reset", { method: "POST" }); toast("Configuration reset — opening setup…"); setTimeout(() => { location.href = "/setup"; }, 800); }
    catch (e) { toast(e.message); }
  };
}
function onSave(which) {
  if (which === "general") return saveKeys({ RMM_SERVER_NAME: $("g-name").value, RMM_PUBLIC_URL: $("g-url").value }, "General settings saved");
  if (which === "auth") return saveKeys({ RMM_AUTH_MODE: authMethod }, "Auth mode saved — restart to apply");
  if (which === "auth-sso") {
    const missing = [];
    if (!$("ms-tenant").value.trim()) missing.push("Tenant ID");
    if (!$("ms-client").value.trim()) missing.push("Client ID");
    if (!$("ms-secret").value.trim()) missing.push("Client secret");
    if (!$("ms-redirect").value.trim()) missing.push("Redirect URI");
    const msg = $("sso-validation-msg");
    if (missing.length) {
      msg.textContent = "Missing required fields: " + missing.join(", ");
      msg.style.display = "";
      return;
    }
    msg.style.display = "none";
    return saveKeys({ MS_TENANT_ID: $("ms-tenant").value, MS_CLIENT_ID: $("ms-client").value, MS_CLIENT_SECRET: $("ms-secret").value, MS_REDIRECT_URI: $("ms-redirect").value }, "SSO credentials saved — restart to apply");
  }
  if (which === "auth-mfa") return saveKeys({ RMM_ENFORCE_2FA: document.querySelector('[data-toggle="enforce2fa"]').classList.contains("on") ? "1" : "0" }, "Two-factor policy saved");
  if (which === "alerts-delivery") {
    if (mailMethod === "smtp") {
      const missing = [];
      if (!$("a-smtp-host").value.trim()) missing.push("SMTP host");
      if (!$("a-smtp-from").value.trim()) missing.push("From address");
      const msg = $("graph-validation-msg"); // reuse slot (hidden in smtp view anyway)
      if (missing.length) { toast("Missing required fields: " + missing.join(", ")); return; }
      return saveKeys({ SMTP_HOST: $("a-smtp-host").value, SMTP_PORT: $("a-smtp-port").value, SMTP_TLS: smtpTls, SMTP_USER: $("a-smtp-user").value, SMTP_PASSWORD: $("a-smtp-pass").value, SMTP_FROM: $("a-smtp-from").value, GRAPH_SENDER: "", GRAPH_FROM: "" }, "SMTP settings saved");
    }
    const missing = [];
    if (!$("a-sender").value.trim()) missing.push("Sender mailbox");
    const msg = $("graph-validation-msg");
    if (missing.length) {
      msg.textContent = "Missing required fields: " + missing.join(", ");
      msg.style.display = "";
      return;
    }
    msg.style.display = "none";
    return saveKeys({ GRAPH_SENDER: $("a-sender").value, GRAPH_FROM: $("a-from").value, SMTP_HOST: "" }, "Graph settings saved");
  }
  if (which === "alerts-recipients") return saveKeys({ RMM_ALERT_RECIPIENTS: $("a-recipients").value }, "Recipients saved");
  if (which === "security-tls") return saveKeys({ RMM_TLS_MODE: tlsMode }, "TLS mode saved — restart to apply");
  if (which === "security-session") return saveKeys({ RMM_SECURE_COOKIES: document.querySelector('[data-toggle="secureCookies"]').classList.contains("on") ? "1" : "0" }, "Security saved");
  if (which === "security-devsecret") return saveKeys({ RMM_REQUIRE_DEVICE_SECRET: document.querySelector('[data-toggle="requireDeviceSecret"]').classList.contains("on") ? "1" : "0" }, "Device-secret policy saved");
  if (which === "agents-approval") return saveKeys({ RMM_REQUIRE_APPROVAL: document.querySelector('[data-toggle="requireApproval"]').classList.contains("on") ? "1" : "0" }, "Enrolment policy saved");
}

async function init() {
  $("toast-ico").innerHTML = ICON.check;
  buildChrome("Settings");
  buildNav();
  try {
    [cfg, ORGS, USERS] = await Promise.all([
      api("/api/settings"),
      api("/api/overview").then((d) => d.orgs.map((o) => ({ ...o, color: colorFor(o.id) }))),
      api("/api/users"),
    ]);
  } catch (e) {
    $("settings-main").innerHTML = `<div class="callout warn"><div class="ic">${ICON.alert}</div><div><div class="ct">Admins only</div><div class="cd">${esc(e.message)} — sign in as a global administrator to manage settings.</div></div></div>`;
    return;
  }
  render();
}
init();
