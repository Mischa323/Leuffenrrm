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

let tlsMode = "self-signed", authMethod = "dev";

function render() {
  tlsMode = cfg.RMM_TLS_MODE || "self-signed";
  authMethod = USERS.mode || cfg.RMM_AUTH_MODE || "dev";
  const offlineMin = String(Math.round((parseFloat(cfg.ALERT_OFFLINE_AFTER) || 120) / 60));
  const secure = (cfg.RMM_SECURE_COOKIES ?? "1") === "1";
  const main = $("settings-main");
  main.innerHTML = `
    <section class="sec on" data-sec="general">
      ${secTitle("gear", "General", "Identity and basics for this server.")}
      ${block("Server identity", "Shown in the header, emails and agent installers.",
        `<div class="frow"><label>Display name</label><input class="inp" id="g-name" value="${esc(cfg.RMM_SERVER_NAME || "Leuffen RMM")}" /></div>
         <div class="frow"><label>Public URL</label><input class="inp mono" id="g-url" value="${esc(cfg.RMM_PUBLIC_URL || location.origin)}" /><div class="hint">Used to build agent install commands and email links.</div></div>`, "general")}
    </section>

    <section class="sec" data-sec="orgs">
      ${secTitle("building", "Organisations", "Tenancy — each org isolates its devices and members.")}
      <div class="card-block"><div class="cb-head" style="display:flex;align-items:center;justify-content:space-between"><div><h3>${ORGS.length} organisations</h3><p>Open an org in the dashboard to manage its devices.</p></div><a class="btn ghost" href="/">${ICON.external} Open dashboard</a></div>
        <table class="utable"><thead><tr><th>Organisation</th><th>Devices</th><th>Online</th></tr></thead><tbody>
        ${ORGS.map((o) => `<tr><td><div class="u-cell"><div class="av" style="background:linear-gradient(140deg,${o.color},color-mix(in srgb,${o.color} 55%,#000))">${initials2(o.name)}</div><div><div class="un">${esc(o.name)}</div></div></div></td>
          <td>${o.devices}</td><td>${o.online}</td></tr>`).join("") || `<tr><td colspan="3" class="muted" style="padding:20px">No organisations.</td></tr>`}
        </tbody></table></div>
    </section>

    <section class="sec" data-sec="users">
      ${secTitle("user", "Users & roles", "Who can sign in and what they can do.")}
      <div class="card-block"><div class="cb-head"><h3>${(USERS.users.length || USERS.bootstrap_admins.length)} ${authMethod === "local" ? "local accounts" : "administrators"}</h3><p>Global admins see everything; members are scoped to their organisations.</p></div>
        <table class="utable"><thead><tr><th>User</th><th>Role</th><th>Last active</th></tr></thead><tbody>${usersRows()}</tbody></table></div>
      <div class="callout info"><div class="ic">${ICON.info}</div><div><div class="ct">${authMethod === "local" ? "Local accounts" : "Single sign-on"}</div><div class="cd">${authMethod === "local" ? "Username/password accounts created during setup. Passwords are PBKDF2-hashed." : "With Microsoft 365 SSO, users appear automatically on first sign-in; global admins are listed above."}</div></div></div>
    </section>

    <section class="sec" data-sec="auth">
      ${secTitle("lock", "Authentication", "How users prove who they are.")}
      ${block("Sign-in method", "The active method. Switching applies after a server restart.",
        `<div class="segmented" id="auth-seg"></div><div id="auth-extra" style="margin-top:4px"></div>`, "auth")}
      ${block("Two-factor authentication", "Time-based one-time codes (TOTP) for local accounts.",
        `${toggle("enforce2fa", "Require 2FA for local accounts", "Local users are prompted to set up an authenticator before they can use the dashboard.", (cfg.RMM_ENFORCE_2FA ?? "0") === "1")}
         <div class="callout info"><div class="ic">${ICON.info}</div><div><div class="ct">Per-user enrolment</div><div class="cd">Each user enables 2FA under <b>Account → Password</b>. ${authMethod === "local" ? "" : "Switch to local accounts to use this — SSO 2FA is managed in your identity provider."}</div></div></div>`, "auth-mfa")}
    </section>

    <section class="sec" data-sec="alerts">
      ${secTitle("bell", "Alerts & email", "When and how the server notifies you.")}
      ${block("Email delivery (Microsoft Graph)", "Alerts are sent through your Microsoft 365 tenant.",
        `<div class="frow"><label>Sender mailbox</label><input class="inp mono" id="a-sender" value="${esc(cfg.GRAPH_SENDER || "")}" /><div class="hint">A licensed mailbox with <code>Mail.Send</code> granted to the app.</div></div>
         <div class="frow"><label>From address shown to recipients</label><input class="inp mono" id="a-from" value="${esc(cfg.GRAPH_FROM || "")}" /></div>`, "alerts-mail")}
      ${block("Alert rules", "Thresholds that trigger an alert.",
        `<div class="frow split">
           <div class="frow"><label>Mark device offline after</label>${select("al-offline", ["2", "5", "10", "30"], offlineMin, (v) => v + " min no heartbeat")}</div>
           <div class="frow"><label>High CPU sustained over</label>${select("al-cpu", ["80", "90", "95"], cfg.ALERT_CPU_PCT || "90", (v) => v + "%")}</div>
         </div>
         <div class="frow"><label>Low disk space warning under</label>${select("al-disk", ["5", "10", "15", "20"], cfg.ALERT_DISK_FREE_PCT || "10", (v) => v + "% free")}</div>`, "alerts-rules")}
    </section>

    <section class="sec" data-sec="security">
      ${secTitle("shieldCheck", "Security", "TLS, sessions and access hardening.")}
      ${block("TLS termination", "Change how HTTPS is served. Applies on next restart.",
        `<div class="segmented" id="tls-seg"></div><div id="tls-extra" style="margin-top:4px"></div>`, "security-tls")}
      ${block("Session & access", "",
        `${toggle("secureCookies", "Secure cookies", "Only send session cookies over HTTPS. Disable only behind a TLS-terminating proxy on a trusted network.", secure)}`, "security-session")}
    </section>

    <section class="sec" data-sec="agents">
      ${secTitle("monitor", "Agents", "Defaults applied to every connected agent.")}
      <div class="callout info"><div class="ic">${ICON.info}</div><div><div class="ct">Heartbeat interval</div><div class="cd">Agents report every ~30s by default (set <code>RMM_INTERVAL</code> on the agent). Per-agent auto-update arrives in a later release.</div></div></div>
      <div class="callout warn"><div class="ic">${ICON.alert}</div><div><div class="ct">Danger zone</div><div class="cd">Removing an agent (from a device's Actions) stops monitoring and revokes its key. Re-enrol with a fresh installer from Downloads.</div></div></div>
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
  buildTlsSeg();
  buildAppearance();
  wire();
}

function usersRows() {
  if ((authMethod === "local" || authMethod === "hybrid") && USERS.users.length) {
    return USERS.users.map((u) => `<tr>
      <td><div class="u-cell"><div class="av" style="background:linear-gradient(140deg,${colorFor(u.username)},color-mix(in srgb,${colorFor(u.username)} 50%,#000))">${initials2(u.username)}</div><div><div class="un">${esc(u.display_name || u.username)}</div><div class="ue">${esc(u.email || "@" + u.username)}</div></div></div></td>
      <td><span class="role-pill ${u.is_admin ? "admin" : "member"}">${u.is_admin ? ICON.shieldCheck : ICON.user} ${u.is_admin ? "Global admin" : "Member"}</span></td>
      <td class="muted">${u.last_active ? new Date(u.last_active * 1000).toLocaleString() : "never"}</td></tr>`).join("");
  }
  const admins = USERS.bootstrap_admins.length ? USERS.bootstrap_admins : (USERS.users.map((u) => u.email || u.username));
  if (!admins.length) return `<tr><td colspan="3" class="muted" style="padding:20px">No administrators configured.</td></tr>`;
  return admins.map((e) => `<tr>
    <td><div class="u-cell"><div class="av" style="background:linear-gradient(140deg,${colorFor(e)},color-mix(in srgb,${colorFor(e)} 50%,#000))">${initials2(e)}</div><div><div class="un">${esc(e)}</div></div></div></td>
    <td><span class="role-pill admin">${ICON.shieldCheck} Global admin</span></td>
    <td class="muted">—</td></tr>`).join("");
}

const AUTH_METHODS = [
  { id: "sso", t: "Microsoft 365", d: "Entra (Office 365) SSO.", icon: "globe" },
  { id: "local", t: "Local accounts", d: "Username + password.", icon: "lock" },
  { id: "hybrid", t: "Both", d: "Local accounts + M365 SSO.", icon: "shieldCheck" },
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

function wire() {
  document.querySelectorAll("[data-toggle]:not([data-toggle='ap-dataviz'])").forEach((t) => t.onclick = () => t.classList.toggle("on"));
  document.querySelectorAll(".save-btn").forEach((b) => b.onclick = () => onSave(b.dataset.save));
}
function onSave(which) {
  if (which === "general") return saveKeys({ RMM_SERVER_NAME: $("g-name").value, RMM_PUBLIC_URL: $("g-url").value }, "General settings saved");
  if (which === "auth") return saveKeys({ RMM_AUTH_MODE: authMethod }, "Auth mode saved — restart to apply");
  if (which === "auth-mfa") return saveKeys({ RMM_ENFORCE_2FA: document.querySelector('[data-toggle="enforce2fa"]').classList.contains("on") ? "1" : "0" }, "Two-factor policy saved");
  if (which === "alerts-mail") return saveKeys({ GRAPH_SENDER: $("a-sender").value, GRAPH_FROM: $("a-from").value }, "Email settings saved");
  if (which === "alerts-rules") return saveKeys({ ALERT_OFFLINE_AFTER: String(parseInt($("al-offline").value, 10) * 60), ALERT_CPU_PCT: $("al-cpu").value, ALERT_DISK_FREE_PCT: $("al-disk").value }, "Alert rules saved");
  if (which === "security-tls") return saveKeys({ RMM_TLS_MODE: tlsMode }, "TLS mode saved — restart to apply");
  if (which === "security-session") return saveKeys({ RMM_SECURE_COOKIES: document.querySelector('[data-toggle="secureCookies"]').classList.contains("on") ? "1" : "0" }, "Security saved");
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
