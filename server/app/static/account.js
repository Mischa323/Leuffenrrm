"use strict";
/* ============================================================================
   Leuffen RMM — Account page (profile + change password + sessions + prefs).
   Wired to /api/account and /api/account/password.
   ========================================================================== */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const NAV = {
  profile: { icon: "user", t: "Profile" },
  password: { icon: "key", t: "Password" },
  sessions: { icon: "monitor", t: "Sessions" },
  appearance: { icon: "sliders", t: "Appearance" },
};

let acct = { username: null, name: "—", email: "", role: "Member", mode: "dev", local: false, is_global_admin: false };

function toast(msg) {
  const t = $("toast"); t.querySelector("span:last-child").textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 2200);
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.status === 204 ? {} : r.json();
}

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
function secTitle(icon, t, p) { return `<div class="sec-title"><div class="st-ic">${ICON[icon]}</div><div><h2>${t}</h2><p>${p}</p></div></div>`; }

function render() {
  const local = acct.local;
  const roleIcon = acct.is_global_admin ? ICON.shieldCheck : ICON.user;
  $("settings-main").innerHTML = `
    <section class="sec on" data-sec="profile">
      ${secTitle("user", "Profile", "Your identity across Leuffen RMM.")}
      <div class="card-block"><div class="cb-body">
        <div class="acct-hero">
          <div class="big-av">${initials(acct.name)}</div>
          <div><div class="ah-name">${esc(acct.name)}</div>
            <div class="ah-sub"><span class="role-pill ${acct.is_global_admin ? "admin" : "member"}">${roleIcon} ${esc(acct.role)}</span>${acct.username ? `<span class="mono">@${esc(acct.username)}</span>` : ""}<span>${esc(acct.email)}</span></div></div>
        </div>
      </div></div>
      <div class="card-block">
        <div class="cb-head"><h3>Details</h3><p>${local ? "Set your email to link this account to a Microsoft 365 sign-in (matched by email)." : "Managed by your Microsoft 365 directory — edit them in your organisation's profile."}</p></div>
        <div class="cb-body">
          <div class="frow split">
            <div class="frow"><label>Display name</label><input class="inp" value="${esc(acct.name)}" disabled /></div>
            <div class="frow"><label>${acct.username ? "Username" : "Sign-in identity"}</label><input class="inp mono" value="${esc(acct.username || acct.email)}" disabled /></div>
          </div>
          <div class="frow"><label>Email${local ? " <span class='hint' style='font-weight:500'>· links Microsoft 365 sign-in</span>" : ""}</label><input class="inp mono" id="p-email" value="${esc(acct.email)}" ${local ? "" : "disabled"} placeholder="${local ? "you@example.com" : ""}" /></div>
        </div>
        ${local ? `<div class="cb-foot"><span class="saved">${ICON.info} Match this to your M365 email to use either sign-in</span><button class="btn" id="save-email">${ICON.save} Save email</button></div>` : ""}
      </div>
    </section>

    <section class="sec" data-sec="password">
      ${secTitle("key", "Password", "Change the password you use to sign in.")}
      ${local ? passwordCard() + `<div id="twofa-wrap"></div>` : `<div class="callout info"><div class="ic">${ICON.globe}</div><div><div class="ct">${acct.mode === "sso" ? "Managed by Microsoft 365" : "No password for this sign-in mode"}</div><div class="cd">${acct.mode === "sso" ? "You sign in with single sign-on, so there's no separate Leuffen RMM password. Change it through your Microsoft account." : "This server is in dev-login mode — there is no password to change. Switch to local accounts or SSO in Settings."}</div></div></div>
        ${acct.mode === "sso" ? `<a class="btn ghost" href="https://account.microsoft.com/security" target="_blank" rel="noopener" style="align-self:flex-start">${ICON.external} Open Microsoft account security</a>` : ""}`}
    </section>

    <section class="sec" data-sec="sessions">
      ${secTitle("monitor", "Sessions", "Devices currently signed in to your account.")}
      <div class="card-block"><div class="cb-body" style="gap:0">
        <div class="session-row"><div class="si">${ICON.monitor}</div><div class="sm"><div class="t">This browser</div><div class="d">${esc(navigator.platform || "Web")} · current session</div></div><span class="cur">This device</span></div>
      </div>
      <div class="cb-foot"><span class="saved">${ICON.clock} Sessions are signed cookies</span><button class="btn danger" id="signout-all">Sign out</button></div></div>
    </section>

    <section class="sec" data-sec="appearance">
      ${secTitle("sliders", "Appearance", "Personalise your view. Overrides the workspace default — just for you, on this account.")}
      <div class="card-block"><div class="cb-body">
        <div class="toggle-row"><div class="tr-txt"><div class="t">Follow workspace default</div><div class="d">Use the appearance your admin set in Settings. Turn off to customise your own.</div></div><div class="switch" id="ap-follow"></div></div>
      </div></div>
      <div id="ap-personal"></div>
    </section>`;
  wire(local);
}

function passwordCard() {
  return `<div class="card-block">
    <div class="cb-head"><h3>Change password</h3><p>Choose a strong password you don't use anywhere else.</p></div>
    <div class="cb-body">
      <div class="frow"><label>Current password</label><div class="pw-wrap"><input class="inp mono" id="cur-pw" type="password" placeholder="••••••••" /><button class="reveal" data-rev="cur-pw" type="button">${ICON.eye}</button></div></div>
      <div class="frow"><label>New password</label><div class="pw-wrap"><input class="inp mono" id="new-pw" type="password" placeholder="••••••••" /><button class="reveal" data-rev="new-pw" type="button">${ICON.eye}</button></div>
        <div class="pw-meter" id="pw-meter"><i></i><i></i><i></i><i></i></div>
        <div class="requirements" id="reqs"></div>
      </div>
      <div class="frow"><label>Confirm new password</label><div class="pw-wrap"><input class="inp mono" id="confirm-pw" type="password" placeholder="••••••••" /><button class="reveal" data-rev="confirm-pw" type="button">${ICON.eye}</button></div><div class="hint" id="match-hint"></div></div>
    </div>
    <div class="cb-foot"><span class="saved">${ICON.lock} Stored PBKDF2-hashed</span><button class="btn" id="change-pw">${ICON.key} Update password</button></div>
  </div>`;
}

/* ---- two-factor (TOTP) ---- */
function buildTwofa() {
  const wrap = $("twofa-wrap"); if (!wrap) return;
  if (acct.twofa_enabled) {
    const rem = acct.recovery_remaining || 0;
    const low = rem <= 2;
    wrap.innerHTML = `<div class="card-block">
      <div class="cb-head"><h3>Two-factor authentication</h3><p>An authenticator code is required at every sign-in.</p></div>
      <div class="cb-body">
        <div class="callout" style="border-color:var(--good);background:var(--good-soft)"><div class="ic" style="background:var(--good-soft);color:var(--good)">${ICON.shieldCheck}</div><div><div class="ct">Two-factor is on</div><div class="cd">To turn it off, confirm your password.</div></div></div>
        <div class="toggle-row"><div class="tr-txt"><div class="t">Recovery codes</div><div class="d" style="${low ? "color:var(--warn)" : ""}">${rem} unused backup code${rem === 1 ? "" : "s"} remaining. Use one if you lose your authenticator.</div></div><button class="btn ghost" id="tf-regen">${ICON.refresh} Regenerate</button></div>
        <div id="tf-codes"></div>
        <div class="frow"><label>Current password</label><div class="pw-wrap"><input class="inp mono" id="tf-off-pw" type="password" placeholder="••••••••" /><button class="reveal" data-rev="tf-off-pw" type="button">${ICON.eye}</button></div></div>
      </div>
      <div class="cb-foot"><span class="saved">${ICON.lock} TOTP · 6 digits · 30s</span><button class="btn danger" id="tf-disable">Turn off 2FA</button></div>
    </div>`;
    $("tf-disable").onclick = disableTwofa;
    $("tf-regen").onclick = regenRecovery;
    wireReveals();
  } else {
    wrap.innerHTML = `<div class="card-block">
      <div class="cb-head"><h3>Two-factor authentication</h3><p>Add a one-time code from an authenticator app as a second sign-in step.${acct.twofa_enforced ? " <b>Required by your administrator.</b>" : ""}</p></div>
      <div class="cb-body"><div id="tf-setup-body"><button class="btn" id="tf-start">${ICON.key} Set up two-factor</button></div></div>
    </div>`;
    $("tf-start").onclick = startTwofa;
  }
}
async function startTwofa() {
  let res;
  try { res = await api("/api/account/2fa/setup", { method: "POST" }); } catch (e) { return toast(e.message); }
  $("tf-setup-body").innerHTML = `
    <ol style="margin:0 0 14px;padding-left:18px;color:var(--text-dim);font-size:13px;line-height:1.7">
      <li>Open your authenticator app (Google / Microsoft Authenticator, 1Password…).</li>
      <li>Add an account using this secret (or the link below):</li>
    </ol>
    <div class="frow"><label>Setup key</label><input class="inp mono" id="tf-secret" value="${esc(res.secret)}" readonly onclick="this.select()" /></div>
    <div class="frow"><a class="ghost-link" href="${esc(res.otpauth_uri)}" style="font-size:12.5px">${ICON.external} Open in authenticator app</a></div>
    <div class="frow"><label>Enter the 6-digit code to confirm</label><input class="inp mono" id="tf-code" inputmode="numeric" placeholder="123456" style="letter-spacing:.25em" /></div>
    <button class="btn" id="tf-confirm">${ICON.check} Verify &amp; enable</button>`;
  $("tf-confirm").onclick = confirmTwofa;
  $("tf-code").onkeydown = (e) => { if (e.key === "Enter") confirmTwofa(); };
  $("tf-code").focus();
}
async function confirmTwofa() {
  const code = $("tf-code").value.trim();
  if (!/^\d{6}$/.test(code)) { $("tf-code").classList.add("err"); return toast("Enter the 6-digit code"); }
  try {
    const res = await api("/api/account/2fa/enable", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) });
    acct.twofa_enabled = true; acct.recovery_remaining = (res.recovery_codes || []).length;
    buildTwofa(); toast("Two-factor enabled");
    showRecoveryCodes(res.recovery_codes || []);
  } catch (e) { $("tf-code").classList.add("err"); toast(e.message); }
}
async function regenRecovery() {
  if (!confirm("Generate a new set of recovery codes? Your old codes stop working.")) return;
  try {
    const res = await api("/api/account/2fa/recovery", { method: "POST" });
    acct.recovery_remaining = (res.recovery_codes || []).length;
    buildTwofa(); showRecoveryCodes(res.recovery_codes || []); toast("New recovery codes generated");
  } catch (e) { toast(e.message); }
}
function showRecoveryCodes(codes) {
  const host = $("tf-codes"); if (!host || !codes.length) return;
  const text = codes.join("\n");
  host.innerHTML = `<div class="callout warn"><div class="ic">${ICON.key}</div><div style="flex:1">
    <div class="ct">Save your recovery codes</div>
    <div class="cd">Each code works once if you can't use your authenticator. They're shown only now.</div>
    <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:12px">${codes.map((c) => `<code style="font-family:var(--font-mono);background:var(--surface-3);border:1px solid var(--border);border-radius:var(--r-sm);padding:7px 10px;font-size:13px;letter-spacing:.04em;text-align:center">${esc(c)}</code>`).join("")}</div>
    <div style="display:flex;gap:8px;margin-top:10px">
      <button class="btn ghost sm" id="rc-copy">${ICON.copy} Copy</button>
      <button class="btn ghost sm" id="rc-dl">${ICON.download} Download</button>
    </div></div></div>`;
  $("rc-copy").onclick = () => { navigator.clipboard?.writeText(text); toast("Copied"); };
  $("rc-dl").onclick = () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text + "\n"], { type: "text/plain" }));
    a.download = "leuffen-rmm-recovery-codes.txt"; a.click();
  };
}
async function disableTwofa() {
  const pw = $("tf-off-pw").value;
  if (!pw) { $("tf-off-pw").classList.add("err"); return toast("Enter your password"); }
  try {
    await api("/api/account/2fa/disable", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: pw }) });
    acct.twofa_enabled = false; buildTwofa(); toast("Two-factor disabled");
  } catch (e) { $("tf-off-pw").classList.add("err"); toast(e.message); }
}
function wireReveals() {
  document.querySelectorAll("[data-rev]").forEach((b) => b.onclick = () => {
    const inp = $(b.dataset.rev); const on = inp.type === "password";
    inp.type = on ? "text" : "password"; b.innerHTML = on ? ICON.eyeOff : ICON.eye;
  });
}

const REQS = [
  { id: "len", t: "At least 8 characters", test: (p) => p.length >= 8 },
  { id: "case", t: "Upper & lowercase", test: (p) => /[A-Z]/.test(p) && /[a-z]/.test(p) },
  { id: "num", t: "A number", test: (p) => /\d/.test(p) },
  { id: "sym", t: "A symbol", test: (p) => /[^A-Za-z0-9]/.test(p) },
];
function pwScore(p) { return REQS.reduce((n, r) => n + (r.test(p) ? 1 : 0), 0); }
function updatePw() {
  const p = $("new-pw") ? $("new-pw").value : ""; if (!$("pw-meter")) return;
  const sc = pwScore(p), meter = $("pw-meter");
  meter.className = "pw-meter" + (sc <= 1 ? " weak" : sc <= 2 ? " mid" : "");
  [...meter.children].forEach((b, i) => b.classList.toggle("lit", i < sc));
  $("reqs").innerHTML = REQS.map((r) => { const ok = r.test(p); return `<div class="req ${ok ? "met" : ""}"><span class="rc">${ok ? ICON.check : ICON.plus.replace("M12 5v14M5 12h14", "M5 12h14")}</span>${r.t}</div>`; }).join("");
  const cp = $("confirm-pw").value, mh = $("match-hint");
  if (!cp) { mh.textContent = ""; mh.style.color = ""; }
  else if (cp === p) { mh.textContent = "Passwords match."; mh.style.color = "var(--good)"; }
  else { mh.textContent = "Passwords don't match yet."; mh.style.color = "var(--warn)"; }
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
function apCard(following) {
  const dim = following ? 'style="opacity:.5;pointer-events:none"' : "";
  return `<div class="card-block" ${dim} id="ap-card">
    <div class="cb-head"><h3>Your appearance</h3><p>${following ? "Following the workspace default. Turn off the switch above to edit these." : "These override the workspace default for your account."}</p></div>
    <div class="cb-body">
      <div class="frow"><label>Mode</label><div class="segmented" id="ap-theme"></div></div>
      <div class="frow"><label>Accent colour</label><div id="ap-accent" style="display:flex;flex-wrap:wrap;gap:10px"></div></div>
      <div class="frow"><label>Density</label><div class="segmented" id="ap-density"></div></div>
      <div class="frow"><label>Corner roundness <span class="hint" id="ap-round-val" style="font-weight:500"></span></label><input type="range" id="ap-round" min="0" max="1.8" step="0.1" class="ap-range" /></div>
    </div>
  </div>`;
}
function buildPersonalAppearance() {
  const u = Appearance.getUser(), following = u.follow !== false;
  const followSw = $("ap-follow");
  if (followSw) {
    followSw.classList.toggle("on", following);
    followSw.onclick = () => { Appearance.setFollow(!following); buildPersonalAppearance(); toast(following ? "Customising your appearance" : "Following workspace default"); };
  }
  const host = $("ap-personal"); if (host) host.innerHTML = apCard(following);
  const eff = Appearance.effective();
  segControl($("ap-theme"), [{ id: "dark", t: "Dark", icon: "shield" }, { id: "light", t: "Light", icon: "globe" }], eff.theme, (v) => { Appearance.setUser("theme", v); buildPersonalAppearance(); });
  segControl($("ap-density"), [{ id: "comfortable", t: "Comfortable", icon: "sliders" }, { id: "compact", t: "Compact", icon: "sliders" }], eff.density, (v) => { Appearance.setUser("density", v); buildPersonalAppearance(); });
  const acc = $("ap-accent");
  if (acc) {
    acc.innerHTML = "";
    Appearance.ACCENTS.forEach((hex) => {
      const sw = document.createElement("button");
      const on = eff.accent.toLowerCase() === hex.toLowerCase();
      sw.style.cssText = `width:30px;height:30px;border-radius:50%;cursor:pointer;background:${hex};border:2px solid ${on ? "var(--text)" : "transparent"};box-shadow:0 0 0 3px ${on ? "var(--accent-ring)" : "transparent"};transition:.15s`;
      sw.onclick = () => { Appearance.setUser("accent", hex); buildPersonalAppearance(); };
      acc.appendChild(sw);
    });
  }
  const round = $("ap-round");
  if (round) {
    round.value = eff.roundness;
    $("ap-round-val").textContent = eff.roundness == 0 ? "sharp" : eff.roundness >= 1.6 ? "round" : "default";
    round.oninput = (e) => { const v = parseFloat(e.target.value); Appearance.setUser("roundness", v); $("ap-round-val").textContent = v == 0 ? "sharp" : v >= 1.6 ? "round" : "default"; };
  }
}

function wire(local) {
  document.querySelectorAll("[data-rev]").forEach((b) => b.onclick = () => {
    const inp = $(b.dataset.rev); const on = inp.type === "password";
    inp.type = on ? "text" : "password"; b.innerHTML = on ? ICON.eyeOff : ICON.eye;
  });
  buildPersonalAppearance();
  const se = $("save-email");
  if (se) se.onclick = async () => {
    try { const r = await api("/api/account/email", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: $("p-email").value.trim() }) }); acct.email = r.email; toast("Email saved" + (r.email ? " — Microsoft 365 sign-in linked" : "")); }
    catch (e) { toast(e.message); }
  };
  if (local) {
    buildTwofa();
    ["new-pw", "confirm-pw"].forEach((id) => { const e = $(id); if (e) e.oninput = updatePw; });
    updatePw();
    const cp = $("change-pw");
    if (cp) cp.onclick = async () => {
      const cur = $("cur-pw").value, np = $("new-pw").value, conf = $("confirm-pw").value;
      if (!cur) { $("cur-pw").classList.add("err"); toast("Enter your current password"); return; }
      if (pwScore(np) < 3) { $("new-pw").classList.add("err"); toast("Choose a stronger password"); return; }
      if (np !== conf) { $("confirm-pw").classList.add("err"); toast("Passwords don't match"); return; }
      try {
        await api("/api/account/password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ current: cur, new: np }) });
        ["cur-pw", "new-pw", "confirm-pw"].forEach((id) => { $(id).value = ""; $(id).classList.remove("err"); });
        updatePw(); toast("Password updated");
      } catch (e) { toast(e.message); }
    };
  }
  const soa = $("signout-all"); if (soa) soa.onclick = () => { location.href = "/auth/logout"; };
}

async function init() {
  $("toast-ico").innerHTML = ICON.check;
  buildChrome("Account");
  buildNav();
  try { acct = await api("/api/account"); acct.role = acct.is_global_admin ? "Global admin" : "Member"; } catch {}
  render();
  if (location.hash.indexOf("2fa") >= 0) { selectSec("password"); if (acct.local && !acct.twofa_enabled) startTwofa(); }
}
init();
