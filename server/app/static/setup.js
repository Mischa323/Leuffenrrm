"use strict";
/* ============================================================================
   Leuffen RMM — first-run setup wizard (wired to /api/setup).
   ========================================================================== */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const X_ICON = ICON.chevR.replace("m9 6 6 6-6 6", "M18 6 6 18M6 6l12 12");
const EYE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3l18 18M10.6 10.7a3 3 0 0 0 4.2 4.2M9.9 5.2A9.5 9.5 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.4 4.3M6.1 6.2A17 17 0 0 0 2 12s3.5 7 10 7a9.3 9.3 0 0 0 3-.5"/></svg>';

const STEPS = [
  { t: "Welcome", d: "Overview & checks" },
  { t: "Authentication", d: "How users sign in" },
  { t: "Administrators", d: "Who gets full access" },
  { t: "Security", d: "TLS & network" },
  { t: "Review", d: "Confirm & launch" },
];
const AUTH_METHODS = [
  { id: "hybrid", t: "Local + Microsoft 365", d: "Username/password accounts on this server, plus optional Microsoft 365 SSO (matched by email). Recommended.", icon: "shieldCheck" },
  { id: "dev", t: "Dev login", d: "Auto sign-in a bootstrap admin. For evaluation only — not for production.", icon: "zap" },
];
const TLS_MODES = [
  { id: "self-signed", t: "Self-signed", d: "Auto-generate a certificate. Fastest start; browsers show a warning.", icon: "shield" },
  { id: "file", t: "Certificate file", d: "Use your own cert.pem & key.pem on disk.", icon: "lock" },
  { id: "proxy", t: "Behind a proxy", d: "TLS terminated upstream (Caddy, nginx, Cloudflare).", icon: "network" },
];

const state = {
  step: 0,
  authMethod: "hybrid",
  tenant: "", client: "", secret: "", redirect: location.origin + "/auth/callback",
  publicUrl: location.origin,
  admins: [],
  accounts: [],
  draft: { username: "", password: "", email: "" },
  tlsMode: "self-signed",
  certPath: "/data/tls/cert.pem", keyPath: "/data/tls/key.pem",
  host: "0.0.0.0", port: "8000", sessionSecret: "",
};

function hydrateIcons() {
  $("brand-logo").innerHTML = ICON.shield;
  $("foot-ico").innerHTML = ICON.lock;
  $("w-ic-1").innerHTML = ICON.zap;
  $("back-ico").innerHTML = ICON.chevR.replace("m9 6 6 6-6 6", "M15 6l-6 6 6 6");
  $("next-ico").innerHTML = ICON.chevR;
  $("gen-ico").innerHTML = ICON.refresh;
  $("go-ico").innerHTML = ICON.chevR;
  $("toast-ico").innerHTML = ICON.check;
}
function toast(msg) {
  const t = $("toast"); t.querySelector("span:last-child").textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 2200);
}

function buildStepper() {
  const wrap = $("stepper"); wrap.innerHTML = "";
  STEPS.forEach((s, i) => {
    const node = document.createElement("div");
    node.className = "step" + (i === state.step ? " active" : "") + (i < state.step ? " done" : "");
    node.innerHTML = `
      <div class="rail"><div class="knob">${i < state.step ? ICON.check : i + 1}</div><div class="line"></div></div>
      <div class="body"><div class="t">${s.t}</div><div class="d">${s.d}</div></div>`;
    node.onclick = () => { if (i < state.step) goTo(i); };
    wrap.appendChild(node);
  });
}

function buildEnvChecks() {
  const checks = [
    { t: "SQLite database", d: "data/rmm.db is writable" },
    { t: "Agent WebSocket endpoint", d: "Listening on /api/agents/ws" },
    { t: "Outbound email (Microsoft Graph)", d: "Optional — configure later for alerts" },
  ];
  $("env-checks").innerHTML = checks.map((c) => `
    <div class="callout info" style="margin-top:0">
      <div class="ic" style="background:var(--good-soft);color:var(--good)">${ICON.check}</div>
      <div><div class="ct">${c.t}</div><div class="cd">${c.d}</div></div>
    </div>`).join("");
}

function buildAuth() {
  const seg = $("auth-seg"); seg.innerHTML = ""; seg.style.gridAutoFlow = "row";
  AUTH_METHODS.forEach((m) => {
    const o = document.createElement("button");
    o.className = "seg-opt" + (state.authMethod === m.id ? " sel" : "");
    o.innerHTML = `<div class="so-top"><span class="so-ic">${ICON[m.icon]}</span><span class="so-t">${m.t}</span></div><div class="so-d">${m.d}</div>`;
    o.onclick = () => { state.authMethod = m.id; buildAuth(); };
    seg.appendChild(o);
  });
  const ex = $("auth-extra");
  if (state.authMethod === "hybrid") {
    ex.innerHTML = `
      <div class="callout info"><div class="ic">${ICON.lock}</div><div><div class="ct">Local accounts are always available</div><div class="cd">You'll create your first account — the global admin — in the next step. Passwords are PBKDF2-hashed on this server.</div></div></div>
      <div class="sec-label" style="margin-top:6px">Microsoft 365 SSO <span class="hint" style="font-weight:500">· optional</span></div>
      <div class="fld"><label>Directory (tenant) ID</label><input class="inp mono" id="f-tenant" placeholder="leave blank to skip SSO" value="${esc(state.tenant)}" /></div>
      <div class="fld"><label>Application (client) ID</label><input class="inp mono" id="f-client" placeholder="00000000-0000-0000-0000-000000000000" value="${esc(state.client)}" /></div>
      <div class="fld"><label>Client secret</label><input class="inp mono" id="f-secret" type="password" placeholder="••••••••••••••••" value="${esc(state.secret)}" /></div>
      <div class="fld"><label>Redirect URI</label><input class="inp mono" id="f-redirect" placeholder="https://rmm.example.com/auth/callback" value="${esc(state.redirect)}" /><div class="hint">Add this exact URL to your app registration's <b>Redirect URIs</b>. A Microsoft 365 user is matched to the local account with the same email.</div></div>`;
    ["tenant", "client", "secret", "redirect"].forEach((k) => { $("f-" + k).oninput = (e) => { state[k] = e.target.value; e.target.classList.remove("err"); }; });
  } else {
    ex.innerHTML = `<div class="callout warn"><div class="ic">${ICON.alert}</div><div><div class="ct">Dev login signs you in automatically</div><div class="cd">No password prompt — anyone who can reach this server gets bootstrap-admin access. Use only on a trusted machine while evaluating, then switch to local + Microsoft 365.</div></div></div>`;
  }
}

function buildAdmin() {
  const head = state.authMethod === "hybrid"
    ? { h2: "Create accounts", p: "Add the local accounts that can sign in. The first account is your global administrator; add teammates now or later. Set an email to also allow Microsoft 365 sign-in for that person." }
    : { h2: "Bootstrap administrator", p: "Dev login signs in a single bootstrap admin. Set which email it uses." };
  $("admin-h2").textContent = head.h2;
  $("admin-p").textContent = head.p;
  const body = $("admin-body");
  if (state.authMethod === "hybrid") {
    body.innerHTML = `
      <div class="fld"><label>Accounts</label><div class="acct-list" id="acct-list"></div></div>
      <div class="acct-form">
        <div class="af-grid">
          <div class="fld" style="gap:6px"><label>Username</label><input class="inp" id="d-user" placeholder="jdoe" value="${esc(state.draft.username)}" /></div>
          <div class="fld" style="gap:6px"><label>Password</label>
            <div class="pw-wrap"><input class="inp mono" id="d-pass" type="password" placeholder="••••••••" value="${esc(state.draft.password)}" /><button class="reveal" id="d-reveal" type="button">${EYE}</button></div>
          </div>
        </div>
        <div class="fld" style="gap:6px;margin-top:12px"><label>Email <span class="hint" style="font-weight:500">· optional, links Microsoft 365 sign-in</span></label><input class="inp mono" id="d-email" type="email" placeholder="jdoe@example.com" value="${esc(state.draft.email || "")}" /></div>
        <div class="pw-meter" id="pw-meter"><i></i><i></i><i></i><i></i></div>
        <div class="pw-strength" id="pw-strength">Use at least 8 characters.</div>
        <button class="btn ghost" id="add-acct" style="margin-top:13px">${ICON.plus} Add account</button>
      </div>`;
    renderAccounts();
    const du = $("d-user"), dp = $("d-pass"), de = $("d-email");
    du.oninput = (e) => state.draft.username = e.target.value;
    dp.oninput = (e) => { state.draft.password = e.target.value; updateStrength(); };
    de.oninput = (e) => state.draft.email = e.target.value;
    $("d-reveal").onclick = () => { const on = dp.type === "password"; dp.type = on ? "text" : "password"; $("d-reveal").innerHTML = on ? EYE_OFF : EYE; };
    $("add-acct").onclick = addAccount;
    dp.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); addAccount(); } };
    updateStrength();
  } else {
    const label = state.authMethod === "dev" ? "Bootstrap admin email" : "Administrator emails";
    const hint = state.authMethod === "dev" ? "The dev session signs in as this address." : "Press Enter or comma to add. At least one is required.";
    body.innerHTML = `
      <div class="fld">
        <label>${label}</label>
        <div class="chips" id="admin-chips"><input id="admin-input" placeholder="name@example.com  ↵" type="email" /></div>
        <div class="hint">${hint}</div>
      </div>
      <div class="callout info"><div class="ic">${ICON.nodes}</div><div><div class="ct">Everyone else</div><div class="cd">Non-admin users get access per-organisation. Invite them and set roles from the dashboard once setup is complete.</div></div></div>`;
    renderChips();
    const ai = $("admin-input");
    ai.onkeydown = (e) => { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addAdmin(); } else if (e.key === "Backspace" && !ai.value && state.admins.length) { state.admins.pop(); renderChips(); } };
    ai.onblur = () => { if (ai.value.trim()) addAdmin(); };
  }
}

function pwScore(p) { let s = 0; if (p.length >= 8) s++; if (p.length >= 12) s++; if (/[A-Z]/.test(p) && /[a-z]/.test(p)) s++; if (/\d/.test(p) && /[^A-Za-z0-9]/.test(p)) s++; return Math.min(s, 4); }
function updateStrength() {
  const meter = $("pw-meter"); if (!meter) return;
  const p = state.draft.password || "", sc = pwScore(p);
  meter.className = "pw-meter" + (sc <= 1 ? " weak" : sc <= 2 ? " mid" : "");
  [...meter.children].forEach((b, i) => b.classList.toggle("lit", i < sc));
  const labels = ["Use at least 8 characters.", "Weak — add length.", "Fair — mix cases & symbols.", "Good password.", "Strong password."];
  $("pw-strength").textContent = p ? labels[sc] : labels[0];
}
function renderAccounts() {
  const list = $("acct-list"); if (!list) return;
  if (!state.accounts.length) { list.innerHTML = `<div class="muted" style="font-size:12.5px;padding:4px 0">No accounts yet — add your global admin below.</div>`; return; }
  list.innerHTML = state.accounts.map((a, i) => `
    <div class="acct-item">
      <div class="av">${esc(a.username.slice(0, 2).toUpperCase())}</div>
      <div class="am"><div class="u">${esc(a.username)}</div><div class="pw">${a.email ? esc(a.email) : "•".repeat(Math.min(a.password.length, 14))}</div></div>
      ${a.admin ? '<span class="role-tag">Global admin</span>' : ""}
      ${state.accounts.length > 1 ? `<div class="x" data-i="${i}">${X_ICON}</div>` : ""}
    </div>`).join("");
  list.querySelectorAll(".x").forEach((x) => x.onclick = () => {
    const i = +x.dataset.i; state.accounts.splice(i, 1);
    if (!state.accounts.some((a) => a.admin) && state.accounts[0]) state.accounts[0].admin = true;
    renderAccounts();
  });
}
function addAccount() {
  const u = (state.draft.username || "").trim(), p = state.draft.password || "", em = (state.draft.email || "").trim();
  if (!u) { $("d-user").classList.add("err"); toast("Enter a username"); return; }
  if (state.accounts.some((a) => a.username.toLowerCase() === u.toLowerCase())) { $("d-user").classList.add("err"); toast("That username already exists"); return; }
  if (p.length < 8) { $("d-pass").classList.add("err"); toast("Password must be at least 8 characters"); return; }
  if (em && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(em)) { $("d-email").classList.add("err"); toast("Enter a valid email or leave it blank"); return; }
  state.accounts.push({ username: u, password: p, email: em, admin: state.accounts.length === 0 });
  state.draft = { username: "", password: "", email: "" };
  ["d-user", "d-pass", "d-email"].forEach((id) => { $(id).value = ""; $(id).classList.remove("err"); });
  renderAccounts(); updateStrength(); toast("Account added");
}
function renderChips() {
  const wrap = $("admin-chips"); if (!wrap) return;
  wrap.querySelectorAll(".chip").forEach((c) => c.remove());
  const input = $("admin-input");
  state.admins.forEach((email, i) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `${esc(email)}<span class="x">${X_ICON}</span>`;
    chip.querySelector(".x").onclick = () => { state.admins.splice(i, 1); renderChips(); };
    wrap.insertBefore(chip, input);
  });
}
function addAdmin() {
  const input = $("admin-input");
  const val = input.value.trim().replace(/,$/, "");
  if (!val) return;
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(val)) { input.classList.add("err"); toast("Enter a valid email address"); return; }
  if (!state.admins.includes(val)) state.admins.push(val);
  input.value = ""; input.classList.remove("err"); renderChips();
}

function buildTls() {
  const seg = $("tls-seg"); seg.innerHTML = "";
  TLS_MODES.forEach((m) => {
    const o = document.createElement("button");
    o.className = "seg-opt" + (state.tlsMode === m.id ? " sel" : "");
    o.innerHTML = `<div class="so-top"><span class="so-ic">${ICON[m.icon]}</span><span class="so-t">${m.t}</span></div><div class="so-d">${m.d}</div>`;
    o.onclick = () => { state.tlsMode = m.id; buildTls(); };
    seg.appendChild(o);
  });
  const extra = $("tls-extra");
  if (state.tlsMode === "file") {
    extra.innerHTML = `
      <div class="fld"><label>Certificate path</label><input class="inp mono" id="f-cert" value="${esc(state.certPath)}" /></div>
      <div class="fld"><label>Private key path</label><input class="inp mono" id="f-key" value="${esc(state.keyPath)}" /></div>`;
    $("f-cert").oninput = (e) => state.certPath = e.target.value;
    $("f-key").oninput = (e) => state.keyPath = e.target.value;
  } else if (state.tlsMode === "proxy") {
    extra.innerHTML = `<div class="callout warn"><div class="ic">${ICON.alert}</div><div><div class="ct">Plain HTTP behind the proxy</div><div class="cd">The server trusts <code>X-Forwarded-*</code> headers. Make sure only your reverse proxy can reach this port.</div></div></div>`;
  } else {
    extra.innerHTML = `<div class="callout info"><div class="ic">${ICON.shield}</div><div><div class="ct">A certificate will be generated</div><div class="cd">Self-signed for the hostname below. Great for a quick start or internal networks.</div></div></div>`;
  }
}

function validateStep() {
  if (state.step === 1 && state.authMethod === "hybrid") {
    // SSO is optional, but if any field is filled the core three are required.
    const any = state.tenant || state.client || state.secret;
    if (any && !(state.tenant && state.client && state.secret)) {
      ["f-tenant", "f-client", "f-secret"].forEach((id) => { if ($(id) && !$(id).value.trim()) $(id).classList.add("err"); });
      toast("Complete the Microsoft 365 fields, or clear them to skip SSO"); return false;
    }
  }
  if (state.step === 2) {
    if (state.authMethod === "hybrid" && state.accounts.length === 0) { toast("Create at least one account"); return false; }
    if (state.authMethod === "dev" && state.admins.length === 0) { toast("Add at least one administrator"); return false; }
  }
  return true;
}
function showStep() {
  document.querySelectorAll(".panel-step").forEach((p) => p.classList.toggle("on", +p.dataset.step === state.step));
  $("cur-step").textContent = state.step + 1;
  $("back-btn").style.visibility = state.step === 0 ? "hidden" : "visible";
  const last = state.step === STEPS.length - 1;
  $("next-btn").innerHTML = last ? `${ICON.power} Launch server` : `Continue ${ICON.chevR}`;
  if (state.step === 1) buildAuth();
  if (state.step === 2) buildAdmin();
  if (last) buildReview();
  buildStepper();
  document.querySelector(".setup-scroll").scrollTop = 0;
}
function goTo(i) { state.step = i; showStep(); }
function next() { if (!validateStep()) return; if (state.step === STEPS.length - 1) { launch(); return; } state.step++; showStep(); }
function back() { if (state.step > 0) { state.step--; showStep(); } }

function buildReview() {
  let auth, access;
  if (state.authMethod === "hybrid") {
    const admin = state.accounts.find((a) => a.admin);
    const sso = !!state.client;
    auth = { v: sso ? "Local accounts + Microsoft 365" : "Local accounts", sub: state.accounts.length + " account" + (state.accounts.length === 1 ? "" : "s") + (sso ? " · SSO matched by email" : " · SSO not configured"), ok: state.accounts.length > 0 };
    access = { ic: "logout", t: "Accounts", v: state.accounts.map((a) => a.username).join(", "), sub: admin ? "Global admin: " + admin.username : "", ok: state.accounts.length > 0 };
  } else {
    auth = { v: "Dev login", sub: "Bootstrap admin signed in automatically — not for production", ok: true };
    access = { ic: "logout", t: "Bootstrap admin", v: state.admins[0] || "admin@localhost", sub: "", ok: true };
  }
  const tls = TLS_MODES.find((m) => m.id === state.tlsMode);
  const items = [
    { ic: "lock", t: "Authentication", v: auth.v, sub: auth.sub, step: 1, ok: auth.ok },
    { ic: access.ic, t: access.t, v: access.v, sub: access.sub, step: 2, ok: access.ok },
    { ic: "globe", t: "TLS mode", v: tls.t, sub: state.tlsMode === "file" ? state.certPath : (state.tlsMode === "proxy" ? "HTTP behind reverse proxy" : "Auto-generated certificate"), step: 3, ok: true },
    { ic: "network", t: "Bind address", v: state.host + ":" + state.port, sub: "", step: 3, ok: true },
    { ic: "shield", t: "Session secret", v: state.sessionSecret ? "Configured" : "Auto-generated on save", sub: state.sessionSecret ? "•".repeat(18) : "A random secret will be created", step: 3, ok: true },
  ];
  $("review-list").innerHTML = items.map((it) => `
    <div class="review-item">
      <div class="ri-ic">${ICON[it.ic]}</div>
      <div class="ri-main"><div class="ri-t">${it.t}</div><div class="ri-v">${esc(it.v)}${it.sub ? `<span class="sub">${esc(it.sub)}</span>` : ""}</div></div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:8px">
        ${it.ok ? `<span class="badge-ok">${ICON.check} Ready</span>` : ""}
        <button class="ri-edit" data-step="${it.step}">Edit</button>
      </div>
    </div>`).join("");
  $("review-list").querySelectorAll(".ri-edit").forEach((b) => b.onclick = () => goTo(+b.dataset.step));
}

function buildPayload() {
  const p = {
    auth_mode: state.authMethod,
    tls_mode: state.tlsMode,
    public_url: state.publicUrl || location.origin,
    host: state.host, port: state.port,
    session_secret: state.sessionSecret || "",
  };
  if (state.authMethod === "hybrid") {
    p.accounts = state.accounts;
    if (state.client) { p.tenant_id = state.tenant; p.client_id = state.client; p.client_secret = state.secret; p.redirect_uri = state.redirect; }
  } else {
    p.admins = state.admins;
  }
  if (state.tlsMode === "file") { p.cert_path = state.certPath; p.key_path = state.keyPath; }
  return p;
}

async function launch() {
  const ov = $("launch"); ov.classList.add("on");
  const log = $("launch-log"); log.innerHTML = "";
  const push = (txt, ok) => { log.innerHTML += `<div>${ok ? '<span class="ok">✓</span> ' : '<span style="color:var(--accent)">→</span> '}${txt}</div>`; log.scrollTop = log.scrollHeight; };
  push("Writing configuration …");
  let res;
  try {
    const r = await fetch("/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(buildPayload()) });
    res = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(res.detail || "Setup failed");
  } catch (e) {
    ov.classList.remove("on");
    toast(e.message);
    return;
  }
  const lines = [
    state.authMethod === "hybrid"
      ? `Auth mode: local accounts (${state.accounts.length})${state.client ? " + Microsoft 365 SSO" : ""}`
      : "Auth mode: dev login (bootstrap admin)",
    state.tlsMode === "self-signed" ? "TLS: self-signed certificate" : `TLS mode: ${state.tlsMode}`,
    `Bind: ${state.host}:${state.port}`,
  ];
  let i = 0;
  const tick = () => {
    if (i < lines.length) { push(lines[i], false); i++; setTimeout(tick, 360); return; }
    push("Configuration saved", true);
    ov.classList.add("done");
    $("launch-spin").innerHTML = ICON.check;
    $("launch-title").textContent = "You're all set";
    $("launch-sub").textContent = res.restart_recommended
      ? "Restart the server to apply all settings (e.g. docker compose restart), then open the dashboard."
      : "Leuffen RMM is configured. Open the dashboard to add your first device.";
    $("launch-actions").style.display = "block";
  };
  setTimeout(tick, 400);
}

async function init() {
  hydrateIcons();
  buildEnvChecks();
  // Prefill from server defaults.
  try {
    const s = await fetch("/api/setup/status").then((r) => r.json());
    state.publicUrl = (s.public_url || location.origin).replace(/\/$/, "");
    state.redirect = state.publicUrl + "/auth/callback";
    if (s.tls_mode) state.tlsMode = s.tls_mode;
    if (s.host) state.host = s.host;
    if (s.port) state.port = String(s.port);
  } catch {}
  buildTls();
  $("f-host").value = state.host; $("f-port").value = state.port;
  $("f-host").oninput = (e) => state.host = e.target.value;
  $("f-port").oninput = (e) => state.port = e.target.value;
  $("f-secret-key").oninput = (e) => state.sessionSecret = e.target.value;
  $("gen-secret").onclick = () => {
    const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz0123456789";
    let s = ""; for (let i = 0; i < 40; i++) s += chars[Math.floor(Math.random() * chars.length)];
    state.sessionSecret = s; $("f-secret-key").value = s; toast("Generated a random secret");
  };
  $("next-btn").onclick = next;
  $("back-btn").onclick = back;
  showStep();
}
init();
