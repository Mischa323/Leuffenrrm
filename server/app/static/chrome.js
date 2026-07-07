"use strict";
/* Shared top chrome for sub-pages (Settings, Account). Renders the header into
   #topbar, fetches the signed-in user, wires theme toggle + dropdown. */

let ME = { name: "—", email: "", role: "", is_global_admin: false };
const initials = (s) => (s || "?").split(/[\s\-@.]+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();

const SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4 12H2m20 0h-2M5.6 5.6 4.2 4.2m15.6 15.6-1.4-1.4M18.4 5.6l1.4-1.4M4.2 19.8l1.4-1.4"/></svg>';
const MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg>';

function buildChrome(crumb) {
  // Local (not a top-level const) so it can't collide with the global `esc`
  // that settings.js / account.js declare — classic scripts share one scope.
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const bar = document.getElementById("topbar");
  bar.innerHTML = `
    <a class="brand" href="/" style="text-decoration:none;color:inherit"><span class="logo">${ICON.shield}</span> Leuffen <span>RMM</span></a>
    <nav class="crumbs">
      <a href="/">Dashboard</a><span class="sep">/</span><a class="here">${crumb}</a>
    </nav>
    <div class="spacer"></div>
    <button class="icon-btn" id="theme-btn" title="Toggle theme"><span id="theme-ico"></span></button>
    <div style="position:relative">
      <button class="icon-btn" id="chrome-bell" title="Alerts">${ICON.bell}<span class="ping hidden" id="chrome-bell-ping"></span></button>
      <div class="menu" id="chrome-bell-menu" style="width:300px;right:0;left:auto"></div>
    </div>
    <div style="position:relative">
      <div class="userchip" id="userchip"><span class="avatar" id="chrome-av">?</span><span class="who"><span id="chrome-name">…</span><small id="chrome-email"></small></span></div>
      <div class="menu" id="usermenu"></div>
    </div>`;

  const ti = document.getElementById("theme-ico");
  const sync = () => ti.innerHTML = document.documentElement.getAttribute("data-theme") === "light" ? MOON : SUN;
  sync();
  window.addEventListener("appearance-change", sync);
  document.getElementById("theme-btn").onclick = () => {
    const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    window.Appearance ? window.Appearance.setUser({ follow: false, theme: next }) : document.documentElement.setAttribute("data-theme", next);
    sync();
  };

  const menu = document.getElementById("usermenu");
  menu.innerHTML = `
    <a href="account.html">${ICON.user} Your account</a>
    <a href="settings.html">${ICON.gear} Admin settings</a>
    <div class="menu-sep"></div>
    <a href="/auth/logout">${ICON.logout} Sign out</a>`;
  const chip = document.getElementById("userchip");
  chip.style.cursor = "pointer";
  chip.onclick = (e) => { e.stopPropagation(); menu.classList.toggle("open"); };
  document.addEventListener("click", () => menu.classList.remove("open"));

  // Notifications bell — same open-alerts feed as the dashboard.
  const bell = document.getElementById("chrome-bell");
  const bmenu = document.getElementById("chrome-bell-menu");
  const bping = document.getElementById("chrome-bell-ping");
  bmenu.innerHTML = `<div style="padding:14px 16px;color:var(--text-dim);font-size:13px">No new notifications</div>`;
  bell.onclick = (e) => { e.stopPropagation(); menu.classList.remove("open"); bmenu.classList.toggle("open"); };
  document.addEventListener("click", () => bmenu.classList.remove("open"));
  fetch("/api/global").then((r) => (r.ok ? r.json() : null)).then((d) => {
    const alerts = (d && d.monitors) || [];
    bping.classList.toggle("hidden", alerts.length === 0);
    if (!alerts.length) return;
    bmenu.innerHTML =
      `<div style="padding:10px 16px 6px;font-size:11px;font-weight:600;color:var(--text-faint);text-transform:uppercase;letter-spacing:.05em">Open alerts</div>` +
      alerts.map((a) => {
        const col = a.severity === "critical" ? "var(--bad)" : "var(--warn)";
        return `<a href="/"><span style="color:${col};flex:none">${ICON.bell}</span><span style="min-width:0"><span style="display:block;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(a.name || "Alert")}</span><small style="color:var(--text-faint)">${esc(a.org || "")}</small></span></a>`;
      }).join("");
  }).catch(() => {});

  // Fill in the real signed-in user.
  fetch("/api/me").then((r) => r.json()).then((me) => {
    ME = { name: me.email.split("@")[0].replace(/[._]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
           email: me.email, role: me.is_global_admin ? "Global admin" : "Member", is_global_admin: me.is_global_admin };
    window.ME = ME;
    document.getElementById("chrome-av").textContent = initials(me.email);
    document.getElementById("chrome-name").textContent = ME.name;
    document.getElementById("chrome-email").textContent = me.email;
    window.dispatchEvent(new CustomEvent("me-loaded", { detail: ME }));
  }).catch(() => {});
}

/* dropdown styles (scoped, injected once) */
const __chromeCss = document.createElement("style");
__chromeCss.textContent = `
  .menu { position: absolute; right: 0; top: calc(100% + 8px); min-width: 210px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--r-md); box-shadow: var(--shadow-lg); padding: 6px; opacity: 0; transform: translateY(-6px); pointer-events: none; transition: .15s; z-index: 50; }
  .menu.open { opacity: 1; transform: none; pointer-events: auto; }
  .menu a { display: flex; align-items: center; gap: 10px; padding: 9px 11px; border-radius: var(--r-sm); color: var(--text); text-decoration: none; font-size: 13px; }
  .menu a:hover { background: var(--surface-hover); }
  .menu a svg { width: 16px; height: 16px; color: var(--text-dim); }
  .menu-sep { height: 1px; background: var(--border); margin: 5px 4px; }
`;
document.head.appendChild(__chromeCss);
window.initials = initials; window.ME = ME;
