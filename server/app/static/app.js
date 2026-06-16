"use strict";
/* ============================================================================
   Leuffen RMM — dashboard logic (modernised UI, wired to the live API).
   Dependency-free vanilla JS.
   ========================================================================== */
const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };

const state = { me: null, org: null, orgName: null, group: null, tab: "devices", device: null, refresh: null, cache: {}, monView: "policies", templates: [] };

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}

/* ---------- formatting helpers ---------- */
const ACCENTS = ["#3b82f6", "#8b5cf6", "#10b981", "#06b6d4", "#f59e0b", "#ec4899", "#6366f1", "#ef4444"];
function colorFor(id) { let h = 0; for (const c of String(id)) h = (h * 31 + c.charCodeAt(0)) >>> 0; return ACCENTS[h % ACCENTS.length]; }
const GROUP_COLORS = { windows: "#3b82f6", linux: "#f59e0b", windows_server: "#8b5cf6" };
function groupColor(g) { return GROUP_COLORS[g.os_match] || colorFor(g.id); }
function fmtUptime(sec) {
  if (sec == null || Number.isNaN(sec)) return "—";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`; if (h) return `${h}h ${m}m`; return `${m}m`;
}
function relTime(epoch) {
  if (!epoch) return "never"; const s = Date.now() / 1000 - epoch;
  if (s < 60) return "just now"; if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`; return `${Math.floor(s / 86400)}d ago`;
}

/* ---------- visual primitives (from the design system) ---------- */
function toast(msg) {
  const t = $("toast"); t.querySelector("span:last-child").textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 2400);
}
function meterClass(p) { return p >= 90 ? "crit" : p >= 75 ? "warn" : ""; }
function meter(p) {
  if (p == null || Number.isNaN(p)) return '<span class="faint" style="color:var(--text-faint)">—</span>';
  return `<div class="meter ${meterClass(p)}"><div class="track"><i style="width:${p}%"></i></div><span class="pct">${Math.round(p)}%</span></div>`;
}
function sparkline(data, color) {
  if (!data || !data.length) return "";
  const w = 64, h = 22, max = Math.max(...data, 1), min = Math.min(...data, 0), rng = Math.max(max - min, 1);
  const pts = data.map((v, i) => [(i / (data.length - 1)) * w, h - ((v - min) / rng) * (h - 4) - 2]);
  const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const id = "g" + Math.random().toString(36).slice(2, 7); color = color || "var(--accent)";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".35"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    <path d="${d} L${w} ${h} L0 ${h} Z" fill="url(#${id})"/><path d="${d}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}
function ringChart(p, color) {
  const r = 18, c = 2 * Math.PI * r, off = c * (1 - (p || 0) / 100); color = color || "var(--accent)";
  return `<svg class="ring" viewBox="0 0 44 44"><circle cx="22" cy="22" r="${r}" fill="none" stroke="var(--meter-track)" stroke-width="5"/>
    <circle cx="22" cy="22" r="${r}" fill="none" stroke="${color}" stroke-width="5" stroke-linecap="round"
    stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}" transform="rotate(-90 22 22)"/>
    <text x="22" y="22" text-anchor="middle" dominant-baseline="central" font-size="11" font-weight="700" fill="var(--text)">${Math.round(p || 0)}</text></svg>`;
}
const statusPill = (on) => `<span class="status ${on ? "on" : "off"}"><span class="led"></span>${on ? "Online" : "Offline"}</span>`;
const initials = (s) => s.split(/[\s\-@.]+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();

/* ---------- init ---------- */
async function init() {
  state.me = await api("/api/me");
  const name = state.me.email.split("@")[0].replace(/[._]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  $("user-name").textContent = name;
  $("user-email").textContent = state.me.email;
  $("avatar").textContent = initials(state.me.email);
  $("home-crumb").onclick = (e) => { e.preventDefault(); showGlobal(); };
  $("drawer-close-btn").onclick = closeDrawer;
  $("fm-close").onclick = dismissFiles;
  $("files-scrim").onclick = dismissFiles;
  $("refresh-global").onclick = () => showGlobal();
  $("cust-ico").innerHTML = ICON.sliders;
  $("cust-close-ico").textContent = "✕";
  $("customize-dash").onclick = openCustomise;
  $("cust-close").onclick = closeCustomise;
  $("cust-scrim").onclick = closeCustomise;
  $("cust-save").onclick = saveCustomise;
  $("cust-reset").onclick = () => { custDraft = state.dash.catalog.map((w) => ({ id: w.id, enabled: ["totals", "orgs", "attention", "approvals"].includes(w.id) })); renderCustList(); };
  $("org-switch").onclick = cycleOrg;
  document.querySelectorAll(".nav button").forEach((b) => b.onclick = () => selectTab(b.dataset.tab));
  document.querySelectorAll(".dtabs button").forEach((b) => b.onclick = () => selectDrawerTab(b.dataset.dtab));
  $("term-form").onsubmit = onTerm;
  $("approvals-ico").innerHTML = ICON.shieldCheck;
  setupScriptModal();
  setupMonitorModal();
  setupRuleModal();
  document.querySelectorAll("#mon-view-seg button").forEach((b) => b.onclick = () => selectMonView(b.dataset.view));
  state.templates = await api("/api/monitor-templates").catch(() => []);
  refreshPendingBadge();
  setInterval(refreshPendingBadge, 30000);
  await restoreView();
}

async function refreshPendingBadge() {
  let p;
  try { p = await api("/api/pending-count"); } catch { return; }
  const btn = $("approvals-btn"), badge = $("approvals-badge");
  if (p.total > 0) {
    badge.textContent = p.total;
    btn.classList.remove("hidden");
    btn.title = `${p.total} device(s) pending approval`;
    btn.onclick = () => {
      if (p.orgs.length === 1) showOrg(p.orgs[0].id, p.orgs[0].name).then(() => selectTab("approvals"));
      else showGlobal();
    };
  } else {
    btn.classList.add("hidden");
  }
}

/* ---------- global view ---------- */
async function showGlobal() {
  clearRefresh(); state.org = null;
  $("global-view").classList.remove("hidden");
  $("org-view").classList.add("hidden");
  $("org-switch").classList.add("hidden");
  $("crumb-org").classList.add("hidden");
  saveView();
  await loadGlobal();
  // Keep the global overview live in the background.
  state.refresh = setInterval(() => { if (!document.hidden && !state.org) loadGlobal().catch(() => {}); }, 10000);
}
async function loadGlobal() {
  const res = await api("/api/dashboard");
  state.dash = res;
  state.orgs = res.data.orgs.map((o) => ({ ...o, color: colorFor(o.id) }));
  renderDashboard(res.layout, res.data);
}
function renderDashboard(layout, data) {
  const host = $("dashboard-widgets");
  const on = (layout || []).filter((w) => w.enabled);
  if (!on.length) { host.innerHTML = `<div class="empty"><div class="big">${ICON.grid}</div>No widgets enabled.<br><span class="muted">Click <b>Customise</b> to add some.</span></div>`; return; }
  host.innerHTML = on.map((w) => (DASH_WIDGETS[w.id] ? DASH_WIDGETS[w.id](data) : "")).join("");
  host.querySelectorAll("[data-org]").forEach((c) => c.onclick = () => { const o = state.orgs.find((x) => x.id === c.dataset.org); showOrg(c.dataset.org, o ? o.name : c.dataset.org); });
  host.querySelectorAll("[data-dev]").forEach((r) => r.onclick = () => gotoDevice(r.dataset.orgId, r.dataset.dev));
  host.querySelectorAll("[data-approve]").forEach((r) => r.onclick = () => { const o = state.orgs.find((x) => x.id === r.dataset.approve); showOrg(r.dataset.approve, o ? o.name : r.dataset.approve).then(() => selectTab("approvals")); });
}
async function gotoDevice(orgId, devId) {
  const o = state.orgs.find((x) => x.id === orgId);
  await showOrg(orgId, o ? o.name : orgId);
  selectTab("devices");
  openDrawer(devId);
}

/* ---------- dashboard customise ---------- */
let custDraft = null;
function openCustomise() {
  if (!state.dash) return;
  custDraft = state.dash.layout.map((w) => ({ ...w }));
  renderCustList();
  $("cust-scrim").classList.remove("hidden");
  $("cust-panel").classList.remove("hidden");
}
function closeCustomise() { $("cust-scrim").classList.add("hidden"); $("cust-panel").classList.add("hidden"); }
function renderCustList() {
  const cat = Object.fromEntries(state.dash.catalog.map((w) => [w.id, w]));
  $("cust-list").innerHTML = custDraft.map((w, i) => {
    const c = cat[w.id] || { title: w.id, desc: "" };
    return `<div class="cust-row" data-i="${i}">
      <div class="cust-move"><button class="icon-btn xs cust-up" ${i === 0 ? "disabled" : ""}>${ICON.arrowUp}</button><button class="icon-btn xs cust-down" ${i === custDraft.length - 1 ? "disabled" : ""}>${ICON.arrowDown}</button></div>
      <div style="flex:1"><div class="cust-t">${escapeHtml(c.title)}</div><div class="cust-d">${escapeHtml(c.desc)}</div></div>
      <div class="switch ${w.enabled ? "on" : ""}" data-toggle></div></div>`;
  }).join("");
  $("cust-list").querySelectorAll(".cust-row").forEach((row) => {
    const i = +row.dataset.i;
    row.querySelector("[data-toggle]").onclick = () => { custDraft[i].enabled = !custDraft[i].enabled; renderCustList(); };
    row.querySelector(".cust-up").onclick = () => { if (i > 0) { [custDraft[i - 1], custDraft[i]] = [custDraft[i], custDraft[i - 1]]; renderCustList(); } };
    row.querySelector(".cust-down").onclick = () => { if (i < custDraft.length - 1) { [custDraft[i + 1], custDraft[i]] = [custDraft[i], custDraft[i + 1]]; renderCustList(); } };
  });
}
async function saveCustomise() {
  try {
    await api("/api/dashboard", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ layout: custDraft }) });
    state.dash.layout = custDraft;
    renderDashboard(custDraft, state.dash.data);
    closeCustomise(); toast("Dashboard saved");
  } catch (e) { toast(e.message); }
}
function dwidget(title, body, sub) {
  return `<section class="dwidget"><div class="dw-head"><h3>${title}</h3>${sub != null ? `<span class="dw-sub">${sub}</span>` : ""}</div>${body}</section>`;
}
function miniDevRow(x) {
  const dot = x.online ? `<span class="dot-led g"></span>` : `<span class="dot-led m"></span>`;
  return `<tr data-dev="${x.id}" data-org-id="${x.org_id}"><td>${dot} ${escapeHtml(x.hostname)}</td><td class="muted">${escapeHtml(x.org)}</td><td style="text-align:right">${x.reason ? `<span class="badge ${x.reason === "offline" ? "na" : "warn"}">${x.reason}</span>` : ""}</td></tr>`;
}
const DASH_WIDGETS = {
  totals: (d) => {
    const t = d.totals, uptime = t.devices ? ((t.online / t.devices) * 100).toFixed(1) : "0";
    return `<div class="kpis" style="margin-bottom:16px">${[
      kpi("Organisations", t.orgs, "blue", ICON.building, ""),
      kpi("Total devices", t.devices, "blue", ICON.monitor, ""),
      kpi("Online now", t.online, "green", ICON.zap, "", `<span class="kdelta up">${ICON.arrowUp} ${uptime}% uptime</span>`),
      kpi("Non-compliant", t.noncompliant, "amber", ICON.shield, "", t.noncompliant ? `<span class="kdelta down">needs attention</span>` : `<span class="kdelta up">all clear</span>`),
      kpi("Offline", t.offline, "red", ICON.bell, "", t.offline ? `<span class="kdelta down">${t.offline} down</span>` : `<span class="kdelta">none</span>`),
    ].join("")}</div>`;
  },
  orgs: (d) => {
    if (!d.orgs.length) return dwidget("Organisations", `<div class="empty" style="padding:24px">No organisations yet. Add one in Settings → Organisations.</div>`);
    const cards = d.orgs.map((o) => {
      const color = colorFor(o.id);
      const onPct = o.devices ? (o.online / o.devices) * 100 : 0, offPct = o.devices ? (o.offline / o.devices) * 100 : 0, ncPct = o.devices ? (o.noncompliant / o.devices) * 100 : 0;
      return `<div class="orgcard" data-org="${o.id}">
        <div class="oc-head"><div class="oc-mark" style="background:linear-gradient(140deg, ${color}, color-mix(in srgb, ${color} 55%, #000))">${initials(o.name)}</div>
          <div style="flex:1"><h3>${escapeHtml(o.name)}</h3><small>${o.devices} devices · ${o.online} online</small></div><div class="oc-arrow">${ICON.chevR}</div></div>
        <div class="health-bar"><i class="on" style="width:${onPct}%"></i><i class="nc" style="width:${ncPct}%"></i><i class="off" style="width:${offPct}%"></i></div>
        <div class="oc-stats"><div class="s"><b><span class="dot-led g"></span>${o.online}</b><span>Online</span></div>
          <div class="s"><b><span class="dot-led m"></span>${o.offline}</b><span>Offline</span></div>
          <div class="s"><b><span class="dot-led r"></span>${o.noncompliant}</b><span>Non-compliant</span></div></div></div>`;
    }).join("");
    return dwidget("Organisations", `<div class="cards">${cards}</div>`);
  },
  attention: (d) => {
    const body = d.attention.length
      ? `<table class="grid mini"><tbody>${d.attention.map(miniDevRow).join("")}</tbody></table>`
      : `<div class="dw-empty">${ICON.check} All devices online &amp; compliant.</div>`;
    return dwidget("Needs attention", body, d.attention.length || null);
  },
  approvals: (d) => {
    const body = d.approvals.length
      ? `<table class="grid mini"><tbody>${d.approvals.map((x) => `<tr data-approve="${x.org_id}"><td>${osIcon(x.os)} ${escapeHtml(x.hostname)}</td><td class="muted">${escapeHtml(x.org)}</td><td style="text-align:right" class="h-sub">${relTime(x.created_at)}</td></tr>`).join("")}</tbody></table>`
      : `<div class="dw-empty">${ICON.shieldCheck} Nothing waiting for approval.</div>`;
    return dwidget("Pending approvals", body, d.approvals.length || null);
  },
  disk: (d) => {
    const body = d.disk.length
      ? `<table class="grid mini"><tbody>${d.disk.map((x) => `<tr data-dev="${x.id}" data-org-id="${x.org_id}"><td>${escapeHtml(x.hostname)} <span class="mono muted">${escapeHtml((x.mount || "").replace(/\\$/, ""))}</span></td><td class="muted">${escapeHtml(x.org)}</td><td style="text-align:right"><span class="badge ${x.percent >= 95 ? "na" : "warn"}">${Math.round(x.percent)}%</span></td></tr>`).join("")}</tbody></table>`
      : `<div class="dw-empty">${ICON.check} No drives under pressure.</div>`;
    return dwidget("Storage pressure", body, d.disk.length || null);
  },
  monitors: (d) => {
    const body = d.monitors.length
      ? `<table class="grid mini"><tbody>${d.monitors.map((x) => `<tr><td>${ICON.bell} ${escapeHtml(x.name)} ${severityBadge(x.severity)}</td><td class="muted" style="text-align:right">${escapeHtml(x.org)}</td></tr>`).join("")}</tbody></table>`
      : `<div class="dw-empty">${ICON.check} All monitors healthy.</div>`;
    return dwidget("Monitor alerts", body, d.monitors.length || null);
  },
  versions: (d) => {
    const counts = d.versions.counts || {}, latest = d.versions.latest;
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    const total = entries.reduce((a, e) => a + e[1], 0) || 1;
    const body = entries.length
      ? `<div class="ver-bars">${entries.map(([v, n]) => `<div class="ver-row"><span class="ver-name mono">${escapeHtml(v)}${v.replace(/^v/, "") === String(latest) ? ` <span class="badge ok">latest</span>` : ""}</span><div class="ver-track"><i style="width:${(n / total) * 100}%"></i></div><span class="ver-n">${n}</span></div>`).join("")}</div>`
      : `<div class="dw-empty">No agents reporting yet.</div>`;
    return dwidget("Agent versions", body, `latest v${latest}`);
  },
};
function renderApprovals() {
  const pend = state.cache.pending || [];
  $("approvals-sub").textContent = `${pend.length} device${pend.length === 1 ? "" : "s"} waiting`;
  const tb = $("approval-rows");
  tb.innerHTML = pend.length ? "" : `<tr><td colspan="5"><div class="empty"><div class="big">${ICON.shieldCheck}</div>Nothing waiting.<br><span class="muted">New agents appear here for you to approve.</span></div></td></tr>`;
  for (const d of pend) {
    const tr = el("tr"); tr.style.cursor = "default";
    tr.innerHTML = `
      <td><div class="host"><div class="os-ico">${osIcon(d.os)}</div><div><div class="h-name">${escapeHtml(d.hostname)}</div><div class="h-sub">${d.online ? "online now" : "offline"}</div></div></div></td>
      <td>${escapeHtml(d.os || "—")}</td><td class="mono">${escapeHtml(d.ip || "—")}</td><td class="h-sub">${relTime(d.created_at)}</td>
      <td><div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn sm approve">${ICON.check} Approve</button><button class="btn ghost sm reject" title="Reject">${ICON.trash}</button></div></td>`;
    tr.querySelector(".approve").onclick = async () => { try { await api(`/api/devices/${d.id}/approve`, { method: "POST" }); toast("Device approved"); await _reloadApprovals(); } catch (e) { toast(e.message); } };
    tr.querySelector(".reject").onclick = async () => { if (!confirm("Reject and remove " + d.hostname + "?")) return; try { await api(`/api/devices/${d.id}/reject`, { method: "POST" }); toast("Device rejected"); await _reloadApprovals(); } catch (e) { toast(e.message); } };
    tb.appendChild(tr);
  }
}
async function _reloadApprovals() {
  state.cache.pending = await api(`/api/orgs/${state.org}/pending`).catch(() => []);
  state.cache.devices = await api(`/api/orgs/${state.org}/devices`).catch(() => state.cache.devices);
  buildNav(); renderApprovals(); refreshPendingBadge();
}
function kpi(label, val, tone, icon, spark, delta) {
  return `<div class="kpi"><div class="klabel"><span class="ki ${tone}">${icon}</span>${label}</div>
    <div class="kval">${val}</div>${delta || ""}${spark ? `<div class="kspark">${spark}</div>` : ""}</div>`;
}

/* ---------- org view ---------- */
async function showOrg(orgId, name) {
  state.org = orgId; state.orgName = name || orgId; state.group = null;
  $("global-view").classList.add("hidden");
  $("org-view").classList.remove("hidden");
  $("crumb-org").classList.remove("hidden");
  $("crumb-org-name").textContent = state.orgName;
  const sw = $("org-switch"); sw.classList.remove("hidden");
  $("org-switch-name").textContent = state.orgName;
  $("org-switch-dot").style.background = colorFor(orgId);
  await refreshOrgCaches();
  buildNav(); buildGroups();
  selectTab(state.tab);
}
function cycleOrg() {
  const orgs = state.me.orgs; if (orgs.length < 2) return;
  const idx = orgs.findIndex((o) => o.id === state.org);
  const next = orgs[(idx + 1) % orgs.length];
  showOrg(next.id, next.name); toast("Switched to " + next.name);
}
async function refreshOrgCaches() {
  const [devices, hosts, nodes, groups, scripts, schedules, monitors, monitorRules, pending] = await Promise.all([
    api(`/api/orgs/${state.org}/devices`),
    api(`/api/orgs/${state.org}/network/hosts`).catch(() => []),
    api(`/api/orgs/${state.org}/nodes`).catch(() => []),
    api(`/api/orgs/${state.org}/groups`).catch(() => []),
    api(`/api/orgs/${state.org}/scripts`).catch(() => []),
    api(`/api/orgs/${state.org}/schedules`).catch(() => []),
    api(`/api/orgs/${state.org}/monitors`).catch(() => []),
    api(`/api/orgs/${state.org}/monitor-rules`).catch(() => []),
    api(`/api/orgs/${state.org}/pending`).catch(() => []),
  ]);
  state.cache = { devices, hosts, nodes, groups, scripts, schedules, monitors, monitorRules, pending };
}
function buildNav() {
  $("nav-devices-count").textContent = state.cache.devices.length;
  $("nav-approvals-count").textContent = state.cache.pending.length;
  $("nav-network-count").textContent = state.cache.hosts.length;
  $("nav-nodes-count").textContent = state.cache.nodes.length;
  $("nav-scripts-count").textContent = state.cache.scripts.length;
  $("nav-monitors-count").textContent = state.cache.monitors.length;
}
function buildGroups() {
  const groups = state.cache.groups, devs = state.cache.devices;
  const wrap = $("groups"); wrap.innerHTML = "";
  const mk = (id, name, color) => {
    const count = id ? devs.filter((d) => d.group_id === id).length : devs.length;
    const b = el("button", state.group === id ? "active" : "");
    b.innerHTML = `${color ? `<span class="gdot" style="background:${color}"></span>` : ICON.grid.replace("<svg", '<svg style="width:15px;height:15px;opacity:.6"')}${name}<span class="count">${count}</span>`;
    b.onclick = () => { state.group = id; buildGroups(); if (state.tab === "devices") renderDevices(); };
    wrap.appendChild(b);
  };
  mk(null, "All devices", null);
  for (const g of groups) mk(g.id, g.name, groupColor(g));
}
function selectTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  ["devices", "approvals", "network", "nodes", "scripts", "monitors", "downloads"].forEach((t) => $("tab-" + t).classList.toggle("hidden", t !== tab));
  clearRefresh();
  saveView();
  if (tab === "devices") renderDevices();
  else if (tab === "approvals") renderApprovals();
  else if (tab === "network") renderNetwork();
  else if (tab === "nodes") renderNodes();
  else if (tab === "scripts") renderScripts();
  else if (tab === "monitors") renderMonitorsTab();
  else if (tab === "downloads") renderDownloads();
  // Keep the active view live in the background (skip the static Downloads tab).
  if (tab !== "downloads") {
    const every = tab === "devices" ? 5000 : 8000;
    state.refresh = setInterval(() => { if (!document.hidden && state.org && state.tab === tab) refreshTab(tab); }, every);
  }
}
async function refreshTab(tab) {
  if (!state.org) return;
  try {
    if (tab === "devices") { state.cache.devices = await api(`/api/orgs/${state.org}/devices`); renderDevices(); }
    else if (tab === "approvals") { state.cache.pending = await api(`/api/orgs/${state.org}/pending`); buildNav(); renderApprovals(); }
    else if (tab === "network") { state.cache.hosts = await api(`/api/orgs/${state.org}/network/hosts`); buildNav(); renderNetwork(); }
    else if (tab === "nodes") { state.cache.nodes = await api(`/api/orgs/${state.org}/nodes`); buildNav(); renderNodes(); }
    else if (tab === "monitors") { state.cache.monitors = await api(`/api/orgs/${state.org}/monitors`); state.cache.monitorRules = await api(`/api/orgs/${state.org}/monitor-rules`); buildNav(); renderMonitorsTab(); }
    else if (tab === "scripts") { state.cache.scripts = await api(`/api/orgs/${state.org}/scripts`); buildNav(); renderScripts(); }
  } catch {}
}
// Persist the current location so a browser refresh stays put (instead of
// snapping back to the global dashboard).
function saveView() {
  try { location.hash = state.org ? `#/o/${state.org}/${state.tab}` : "#/"; } catch {}
}
async function restoreView() {
  const m = (location.hash || "").match(/^#\/o\/([^/]+)\/([^/]+)/);
  if (m) {
    const org = (state.me.orgs || []).find((o) => o.id === m[1]);
    if (org) {
      const tabs = ["devices", "approvals", "network", "nodes", "scripts", "monitors", "downloads"];
      state.tab = tabs.includes(m[2]) ? m[2] : "devices";
      await showOrg(org.id, org.name);
      return;
    }
  }
  await showGlobal();
}

/* ---------- scripts (Phase 2) ---------- */
const SCRIPT_CATEGORIES = ["Script", "Monitoring", "Installation", "Maintenance", "Security", "Diagnostics", "Network", "Other"];
const CATEGORY_TONE = { Monitoring: "var(--accent)", Installation: "var(--good)", Maintenance: "var(--warn)", Security: "var(--bad)", Diagnostics: "#06b6d4", Network: "#8b5cf6" };
function catBadge(cat) {
  const c = CATEGORY_TONE[cat] || "var(--text-dim)";
  return `<span class="badge" style="color:${c};background:color-mix(in srgb,${c} 14%,transparent);border:1px solid color-mix(in srgb,${c} 30%,transparent)">${escapeHtml(cat || "Script")}</span>`;
}
function renderScripts() {
  const scripts = state.cache.scripts || [];
  $("scripts-sub").textContent = `${scripts.length} script${scripts.length === 1 ? "" : "s"}`;
  const body = $("scripts-body");
  body.innerHTML = scripts.length ? "" : `<div class="empty"><div class="big">${ICON.terminal}</div>No scripts yet.<br><span class="muted">Create one, then run it on any online device.</span></div>`;
  for (const s of scripts) {
    const card = el("div", "tile");
    card.style.marginBottom = "12px";
    const online = (state.cache.devices || []).filter((d) => d.online);
    card.innerHTML = `
      <div style="display:flex;align-items:flex-start;gap:11px">
        <div class="os-ico">${ICON.terminal}</div>
        <div style="flex:1"><div style="font-weight:650;display:flex;align-items:center;gap:8px">${escapeHtml(s.name)} ${catBadge(s.category)}</div><div class="h-sub">${s.shell === "powershell" ? "PowerShell" : "Shell"}${s.description ? " · " + escapeHtml(s.description) : ""}</div></div>
        <button class="btn ghost sm edit">${ICON.terminal} Edit</button>
        <button class="btn ghost sm del">${ICON.trash}</button>
      </div>
      <div class="field" style="margin-top:12px">
        <select class="dev-sel">${online.length ? online.map((d) => `<option value="${d.id}">${escapeHtml(d.hostname)}</option>`).join("") : '<option value="">No online devices</option>'}</select>
        <button class="btn run" ${online.length ? "" : "disabled"}>${ICON.power} Run</button>
      </div>
      <pre class="out hidden" style="margin-top:10px;background:var(--term-bg);border:1px solid var(--border);border-radius:var(--r-md);padding:12px;font-family:var(--font-mono);font-size:12.5px;max-height:260px;overflow:auto;white-space:pre-wrap"></pre>`;
    card.querySelector(".edit").onclick = () => openScriptForm(s);
    card.querySelector(".del").onclick = async () => {
      if (!confirm("Delete script “" + s.name + "”?")) return;
      try { await api(`/api/scripts/${s.id}`, { method: "DELETE" }); toast("Script deleted"); state.cache.scripts = await api(`/api/orgs/${state.org}/scripts`); buildNav(); renderScripts(); } catch (e) { toast(e.message); }
    };
    card.querySelector(".run").onclick = async () => {
      const sel = card.querySelector(".dev-sel"), out = card.querySelector(".out"), btn = card.querySelector(".run");
      if (!sel.value) return;
      btn.disabled = true; out.classList.remove("hidden"); out.textContent = "Running…";
      try {
        const r = await api(`/api/scripts/${s.id}/run`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ device_id: sel.value }) });
        out.textContent = (r.output || "(no output)") + `\n\n[exit ${r.exit_code} · ${r.status}]`;
        toast(r.status === "ok" ? "Script finished" : "Script exited " + r.exit_code);
        loadRuns();
      } catch (e) { out.textContent = e.message; toast(e.message); }
      btn.disabled = false;
    };
    body.appendChild(card);
  }
  $("script-new").onclick = () => openScriptForm(null);
  $("sched-new").onclick = openScheduleForm;
  $("runs-refresh").onclick = loadRuns;
  renderSchedules();
  loadRuns();
}
function cadenceText(s) {
  if (s.trigger === "interval") return `every ${s.interval_minutes} min`;
  if (s.trigger === "daily") return `daily at ${s.at_time}`;
  return s.trigger;
}
function targetText(s) {
  if (s.target_type === "all") return "all devices";
  if (s.target_type === "group") { const g = (state.cache.groups || []).find((x) => x.id === s.target_id); return "group: " + (g ? g.name : "—"); }
  const d = (state.cache.devices || []).find((x) => x.id === s.target_id); return d ? d.hostname : "device";
}
function renderSchedules() {
  const scheds = state.cache.schedules || [];
  $("sched-sub").textContent = `${scheds.length} schedule${scheds.length === 1 ? "" : "s"}`;
  const body = $("sched-body");
  body.innerHTML = scheds.length ? "" : `<div class="empty"><div class="big">${ICON.clock}</div>No schedules yet.<br><span class="muted">Automate a script to run on a cadence.</span></div>`;
  for (const s of scheds) {
    const row = el("div", "tile");
    row.style.marginBottom = "10px";
    row.innerHTML = `<div style="display:flex;align-items:center;gap:12px">
      <div class="os-ico">${ICON.clock}</div>
      <div style="flex:1"><div style="font-weight:650">${escapeHtml(s.name || "Schedule")}</div>
        <div class="h-sub">${cadenceText(s)} · ${escapeHtml(targetText(s))} · next ${s.next_run ? relTime(s.next_run).replace(" ago", "") : "—"}${s.last_run ? " · last " + relTime(s.last_run) : ""}</div></div>
      <span class="badge ${s.enabled ? "ok" : "na"}">${s.enabled ? "enabled" : "paused"}</span>
      <button class="btn ghost sm run-now">${ICON.power} Run now</button>
      <button class="btn ghost sm toggle">${s.enabled ? "Pause" : "Resume"}</button>
      <button class="btn ghost sm del">${ICON.trash}</button></div>`;
    row.querySelector(".run-now").onclick = async () => { try { const r = await api(`/api/schedules/${s.id}/run`, { method: "POST" }); toast(`Ran on ${r.devices} device(s)`); loadRuns(); } catch (e) { toast(e.message); } };
    row.querySelector(".toggle").onclick = async () => { try { await api(`/api/schedules/${s.id}/toggle`, { method: "POST" }); state.cache.schedules = await api(`/api/orgs/${state.org}/schedules`); renderSchedules(); } catch (e) { toast(e.message); } };
    row.querySelector(".del").onclick = async () => { if (!confirm("Delete this schedule?")) return; try { await api(`/api/schedules/${s.id}`, { method: "DELETE" }); state.cache.schedules = await api(`/api/orgs/${state.org}/schedules`); renderSchedules(); toast("Schedule deleted"); } catch (e) { toast(e.message); } };
    body.appendChild(row);
  }
}
async function openScheduleForm() {
  const scripts = state.cache.scripts || [];
  if (!scripts.length) return toast("Create a script first");
  const names = scripts.map((s, i) => `${i + 1}. ${s.name}`).join("\n");
  const pick = prompt("Which script?\n" + names, "1");
  const idx = parseInt(pick, 10) - 1; if (!(idx >= 0 && idx < scripts.length)) return;
  const script = scripts[idx];
  const tgt = (prompt("Target: type 'all', 'group', or a device hostname", "all") || "all").trim();
  let target_type = "all", target_id = null;
  if (tgt.toLowerCase() === "group") {
    const groups = state.cache.groups || [];
    const gpick = prompt("Group?\n" + groups.map((g, i) => `${i + 1}. ${g.name}`).join("\n"), "1");
    const gi = parseInt(gpick, 10) - 1; if (!(gi >= 0 && gi < groups.length)) return;
    target_type = "group"; target_id = groups[gi].id;
  } else if (tgt.toLowerCase() !== "all") {
    const d = (state.cache.devices || []).find((x) => x.hostname.toLowerCase() === tgt.toLowerCase());
    if (!d) return toast("No device named " + tgt);
    target_type = "device"; target_id = d.id;
  }
  const when = (prompt("Cadence: 'every <N> min'  or  'daily HH:MM'", "every 60 min") || "").trim().toLowerCase();
  let body = { script_id: script.id, target_type, target_id };
  const mInt = when.match(/every\s+(\d+)/), mDay = when.match(/daily\s+(\d{1,2}:\d{2})/);
  if (mInt) { body.trigger = "interval"; body.interval_minutes = parseInt(mInt[1], 10); }
  else if (mDay) { body.trigger = "daily"; body.at_time = mDay[1]; }
  else return toast("Couldn't parse the cadence");
  try { await api(`/api/orgs/${state.org}/schedules`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); toast("Schedule created"); state.cache.schedules = await api(`/api/orgs/${state.org}/schedules`); renderSchedules(); } catch (e) { toast(e.message); }
}
let editingScriptId = null;
function setupScriptModal() {
  $("sm-close-ico").innerHTML = ICON.chevR.replace('d="m9 6 6 6-6 6"', 'd="M18 6 6 18M6 6l12 12"');
  $("sm-category").innerHTML = SCRIPT_CATEGORIES.map((c) => `<option value="${c}">${c}</option>`).join("");
  const close = () => $("script-modal").classList.add("hidden");
  $("sm-close").onclick = close;
  $("sm-cancel").onclick = close;
  $("script-modal").addEventListener("click", (e) => { if (e.target === $("script-modal")) close(); });
  // Tab inserts two spaces in the code editor instead of leaving the field.
  $("sm-code").addEventListener("keydown", (e) => {
    if (e.key === "Tab") { e.preventDefault(); const t = e.target, s = t.selectionStart; t.value = t.value.slice(0, s) + "  " + t.value.slice(t.selectionEnd); t.selectionStart = t.selectionEnd = s + 2; }
  });
  $("sm-save").onclick = saveScript;
  $("sm-file-add").onclick = uploadScriptFile;
}
function openScriptForm(existing) {
  editingScriptId = existing ? existing.id : null;
  $("sm-title").textContent = existing ? "Edit script" : "New script";
  $("sm-name").value = existing ? existing.name : "";
  $("sm-category").value = (existing && existing.category) || "Script";
  $("sm-shell").value = (existing && existing.shell) || "shell";
  $("sm-desc").value = (existing && existing.description) || "";
  $("sm-code").value = existing ? existing.content : "#!/bin/sh\n";
  $("sm-save").textContent = existing ? "Save changes" : "Create script";
  // File attachments only apply to a saved script.
  $("sm-files-hint").classList.toggle("hidden", !!existing);
  $("sm-file-input").parentElement.style.display = existing ? "flex" : "none";
  $("sm-files").innerHTML = "";
  if (existing) renderScriptFiles(existing.id);
  $("script-modal").classList.remove("hidden");
  setTimeout(() => $("sm-name").focus(), 30);
}
async function renderScriptFiles(scriptId) {
  let files = [];
  try { files = await api(`/api/scripts/${scriptId}/files`); } catch {}
  const host = $("sm-files");
  host.innerHTML = files.map((f) => `<div style="display:flex;align-items:center;gap:8px;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--r-sm);padding:6px 10px;font-size:13px"><span style="flex:1" class="mono">${escapeHtml(f.name)}</span><span class="h-sub">${(f.size / 1024).toFixed(1)} KB</span><button class="btn ghost sm" data-fid="${f.id}">${ICON.trash}</button></div>`).join("") || `<div class="h-sub">No files attached.</div>`;
  host.querySelectorAll("[data-fid]").forEach((b) => b.onclick = async () => {
    try { await api(`/api/scripts/files/${b.dataset.fid}`, { method: "DELETE" }); renderScriptFiles(scriptId); } catch (e) { toast(e.message); }
  });
}
async function uploadScriptFile() {
  if (!editingScriptId) return toast("Save the script first");
  const inp = $("sm-file-input"); const file = inp.files[0];
  if (!file) return toast("Choose a file");
  const buf = await file.arrayBuffer();
  let bin = ""; const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  try {
    await api(`/api/scripts/${editingScriptId}/files`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: file.name, content_b64: btoa(bin) }) });
    inp.value = ""; toast("File uploaded"); renderScriptFiles(editingScriptId);
  } catch (e) { toast(e.message); }
}
async function saveScript() {
  const body = {
    name: $("sm-name").value.trim(),
    category: $("sm-category").value,
    shell: $("sm-shell").value,
    description: $("sm-desc").value.trim(),
    content: $("sm-code").value,
  };
  if (!body.name) { $("sm-name").focus(); return toast("Give the script a name"); }
  if (!body.content.trim()) { $("sm-code").focus(); return toast("The script body is empty"); }
  try {
    if (editingScriptId) await api(`/api/scripts/${editingScriptId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    else await api(`/api/orgs/${state.org}/scripts`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("script-modal").classList.add("hidden");
    toast(editingScriptId ? "Script saved" : "Script created");
    state.cache.scripts = await api(`/api/orgs/${state.org}/scripts`);
    buildNav(); renderScripts();
  } catch (e) { toast(e.message); }
}
async function loadRuns() {
  let runs = [];
  try { runs = await api(`/api/orgs/${state.org}/runs`); } catch {}
  const tb = $("run-rows"); if (!tb) return;
  tb.innerHTML = runs.length ? "" : `<tr><td colspan="4" class="muted" style="padding:20px;text-align:center">No runs yet.</td></tr>`;
  const dmap = Object.fromEntries((state.cache.devices || []).map((d) => [d.id, d.hostname]));
  for (const r of runs) {
    const badge = r.status === "ok" ? `<span class="badge ok">${ICON.check} ok</span>` : r.status === "running" ? `<span class="badge na">running</span>` : `<span class="badge bad">${ICON.alert} failed</span>`;
    const tr = el("tr");
    tr.innerHTML = `<td>${escapeHtml(r.name || "—")}</td><td>${escapeHtml(dmap[r.device_id] || r.device_id.slice(0, 8))}</td><td>${badge}</td><td class="h-sub">${relTime(r.created_at)}</td>`;
    if (r.output) { tr.style.cursor = "pointer"; tr.onclick = () => alert(r.output); }
    tb.appendChild(tr);
  }
}

function renderDevices() {
  let devs = state.cache.devices || [];
  if (state.group) devs = devs.filter((d) => d.group_id === state.group);
  $("devices-sub").textContent = `${devs.length} device${devs.length === 1 ? "" : "s"}`;
  const tb = $("device-rows"); tb.innerHTML = "";
  if (!devs.length) { tb.innerHTML = `<tr><td colspan="6"><div class="empty"><div class="big">${ICON.monitor}</div>No devices in this group.<br><span class="muted">Install an agent from the Downloads tab.</span></div></td></tr>`; return; }
  for (const d of devs) {
    const m = d.latest || {};
    const comp = d.compliant == null ? `<span class="badge na">N/A</span>`
      : d.compliant ? `<span class="badge ok">${ICON.check} Compliant</span>`
      : `<span class="badge bad">${ICON.alert} Non-compliant</span>`;
    const sub = d.online ? "up " + fmtUptime(m.uptime) : "last seen " + relTime(d.last_seen);
    const tr = el("tr");
    tr.innerHTML = `
      <td><div class="host"><div class="os-ico">${osIcon(d.os)}</div><div><div class="h-name">${d.hostname}</div><div class="h-sub">${d.os || "—"} · ${d.ip || "—"}</div></div></div></td>
      <td>${statusPill(d.online)}<div class="h-sub" style="margin-top:3px">${sub}</div></td>
      <td>${d.online ? meter(m.cpu_percent) : meter(null)}</td>
      <td>${d.online ? meter(m.mem_percent) : meter(null)}</td>
      <td>${d.online ? meter(m.disk_percent) : meter(null)}</td>
      <td>${comp}</td>`;
    tr.onclick = () => openDrawer(d.id);
    tb.appendChild(tr);
  }
}

function renderNetwork() {
  const hosts = state.cache.hosts || [];
  $("network-sub").textContent = `${hosts.length} host${hosts.length === 1 ? "" : "s"} discovered`;
  $("network-scan").onclick = async () => {
    const node = (state.cache.nodes || [])[0];
    if (!node) return toast("Promote a device to a node first");
    try { await api(`/api/devices/${node.id}/scan`, { method: "POST" }); toast("Scan started on " + node.hostname); } catch (e) { toast(e.message); }
  };
  const tb = $("network-rows"); tb.innerHTML = "";
  if (!hosts.length) { tb.innerHTML = `<tr><td colspan="5"><div class="empty"><div class="big">${ICON.wifi}</div>No hosts discovered yet.<br><span class="muted">Promote a device to a node and run a scan.</span></div></td></tr>`; return; }
  for (const h of hosts) {
    const tr = el("tr"); tr.style.cursor = "default";
    tr.innerHTML = `
      <td>${statusPill(h.online)}</td>
      <td class="mono">${h.ip}</td>
      <td class="mono" style="color:var(--text-dim)">${h.mac || "—"}</td>
      <td>${h.hostname && h.hostname !== "—" ? h.hostname : '<span class="muted">unknown</span>'}</td>
      <td><div style="display:flex;align-items:center;justify-content:space-between;gap:10px"><span class="muted">${h.manufacturer || "—"}</span>${h.mac ? `<button class="btn subtle sm wake">${ICON.power} Wake</button>` : ""}</div></td>`;
    const wb = tr.querySelector(".wake");
    if (wb) wb.onclick = async (e) => {
      e.stopPropagation();
      try { const r = await api(`/api/orgs/${state.org}/network/wake`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mac: h.mac }) }); toast(`Magic packet sent via ${r.via || "node"}`); } catch (err) { toast(err.message); }
    };
    tb.appendChild(tr);
  }
}

function renderNodes() {
  const nodes = state.cache.nodes || [];
  const wrap = $("node-grid");
  if (!nodes.length) { wrap.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="big">${ICON.nodes}</div>No relay nodes yet.<br><span class="muted">Open any device → Actions → Promote to node.</span></div>`; return; }
  wrap.innerHTML = "";
  for (const n of nodes) {
    const subs = (n.subnets || []).map((s) => s.cidr);
    const c = el("div", "tile");
    c.innerHTML = `
      <div style="display:flex;align-items:center;gap:11px;margin-bottom:14px">
        <div class="os-ico">${ICON.server}</div>
        <div><div style="font-weight:650">${n.hostname}</div><div class="h-sub">${statusPill(n.online).replace('class="status', 'style="font-size:11px" class="status')}</div></div>
      </div>
      <div style="display:flex;gap:18px;margin-bottom:14px">
        <div><div class="h-sub">Subnets</div><div style="font-weight:650;font-size:16px">${subs.length}</div></div>
        <div><div class="h-sub">Last seen</div><div style="font-weight:650;font-size:13px;margin-top:3px">${relTime(n.last_seen)}</div></div>
      </div>
      ${subs.map((s) => `<div class="subnet-row">${s}</div>`).join("")}
      <button class="btn block scan" style="margin-top:12px;width:100%">${ICON.scan} Scan now</button>`;
    c.querySelector(".scan").onclick = async () => { try { await api(`/api/devices/${n.id}/scan`, { method: "POST" }); toast("Network scan started on " + n.hostname); } catch (e) { toast(e.message); } };
    wrap.appendChild(c);
  }
}

async function renderDownloads() {
  const base = location.origin;
  let info = { tokens: [], insecure_tls: location.protocol === "https:" };
  try { info = await api(`/api/orgs/${state.org}/tokens`); } catch {}
  const ins = info.insecure_tls ? 1 : 0;
  let rel = { available: false };
  try { rel = await api(`/api/agent-release`); } catch {}
  const relLabel = rel.available
    ? `<span class="badge ok" style="margin-left:8px">${escapeHtml(rel.name || rel.tag || "latest")}</span>${rel.size ? ` <span class="h-sub">${(rel.size / 1048576).toFixed(1)} MB${rel.published_at ? " · " + new Date(rel.published_at).toLocaleDateString() : ""}</span>` : ""}`
    : `<span class="badge na" style="margin-left:8px">no build published yet</span>`;
  $("downloads-body").innerHTML = `
    <div class="dl-block"><div class="lab">${ICON.key} Enrolment key — one-time &amp; write-once</div>
      <div class="h-sub" style="margin:4px 0 10px">Generate a key per device. It's shown <b>once</b>, can't be retrieved again, and enrols a <b>single</b> device. Already-enrolled agents reconnect by their identity — no key needed.</div>
      <button class="btn sm" id="gen-token">${ICON.plus} Generate enrolment key</button>
      <div id="token-result" style="margin-top:12px"></div></div>
    <div class="dl-block"><div class="lab">${ICON.windows} Windows — MSI installer ${relLabel}</div>
      <div style="margin:6px 0 8px"><a class="btn sm" href="${base}/api/orgs/${state.org}/install.msi">${ICON.download} Download MSI</a> <span class="h-sub">then install with the generated key</span></div>
      <div class="code">msiexec /i leuffen-rmm-agent.msi /qn RMM_SERVER_URL=${base} RMM_API_KEY=&lt;enrolment-key&gt; RMM_INSECURE_TLS=${ins}</div></div>
    <div class="dl-block"><div class="lab">${ICON.refresh} Update installed agents</div>
      <div class="h-sub" style="margin:4px 0 10px">Push the latest build to every online agent in this organisation. Each updates in place and reconnects automatically.</div>
      <button class="btn sm" id="update-all">${ICON.download} Update all online agents</button></div>
    <div class="dl-block"><div class="lab">${ICON.key} Active enrolment keys</div>
      <div id="token-list"></div></div>`;
  $("update-all").onclick = async () => {
    if (!confirm("Push the latest agent to all online devices in this organisation?")) return;
    const b = $("update-all"); b.disabled = true; const old = b.innerHTML; b.innerHTML = "Sending…";
    try { const r = await api(`/api/orgs/${state.org}/update-agents`, { method: "POST" }); toast(`Update sent to ${r.started} of ${r.online} online agent(s)`); }
    catch (e) { toast(e.message); } finally { b.disabled = false; b.innerHTML = old; }
  };
  $("gen-token").onclick = async () => {
    try {
      const t = await api(`/api/orgs/${state.org}/tokens`, { method: "POST" });
      showNewToken(t.token, base, ins);
      info = await api(`/api/orgs/${state.org}/tokens`); renderTokenList(info.tokens);
    } catch (e) { toast(e.message); }
  };
  renderTokenList(info.tokens);
}
function showNewToken(token, base, ins) {
  const cmds = {
    msi: `msiexec /i leuffen-rmm-agent.msi /qn RMM_SERVER_URL=${base} RMM_API_KEY=${token} RMM_INSECURE_TLS=${ins}`,
    win: `iwr "${base}/api/orgs/${state.org}/install.ps1?token=${token}" -UseBasicParsing | iex`,
    lin: `curl -fsSL "${base}/api/orgs/${state.org}/install.sh?token=${token}" | sudo bash`,
  };
  const row = (label, c) => `<div style="margin-top:8px"><div class="h-sub">${label}</div><div class="code"><button class="btn ghost sm tcopy" data-c="${escapeHtml(c)}">${ICON.copy} Copy</button>${escapeHtml(c)}</div></div>`;
  const host = $("token-result");
  host.innerHTML = `<div class="callout warn"><div class="ic">${ICON.key}</div><div style="flex:1">
    <div class="ct">Copy this now — it won't be shown again</div>
    <div style="margin:8px 0"><code style="font-family:var(--font-mono);background:var(--surface-3);border:1px solid var(--border);border-radius:var(--r-sm);padding:8px 11px;font-size:13px;display:inline-block;word-break:break-all">${escapeHtml(token)}</code>
      <button class="btn ghost sm tcopy" data-c="${escapeHtml(token)}" style="margin-left:8px">${ICON.copy} Copy key</button></div>
    ${row("Tray Settings: paste as the enrolment key, or MSI:", cmds.msi)}
    ${row("Windows (PowerShell, admin):", cmds.win)}
    ${row("Linux (one-liner):", cmds.lin)}
  </div></div>`;
  host.querySelectorAll(".tcopy").forEach((b) => b.onclick = () => { navigator.clipboard?.writeText(b.dataset.c); toast("Copied"); });
}
function renderTokenList(tokens) {
  const host = $("token-list");
  if (!tokens.length) { host.innerHTML = `<div class="h-sub">No keys yet. Generate one above.</div>`; return; }
  host.innerHTML = `<table class="grid"><thead><tr><th>Created</th><th>Status</th><th>Device</th><th></th></tr></thead><tbody>${tokens.map((t) => {
    const used = t.used_at ? `<span class="badge na">used ${relTime(t.used_at)}</span>` : `<span class="badge ok">unused</span>`;
    const dev = t.device_hostname ? `<span class="mono">${escapeHtml(t.device_hostname)}</span>` : (t.device_id ? `<span class="h-sub">removed device</span>` : `<span class="h-sub">—</span>`);
    return `<tr><td class="h-sub">${relTime(t.created_at)}</td><td>${used}</td><td>${dev}</td><td style="text-align:right"><button class="btn ghost sm tdel" data-id="${t.id}">${ICON.trash}</button></td></tr>`;
  }).join("")}</tbody></table>`;
  host.querySelectorAll(".tdel").forEach((b) => b.onclick = async () => {
    try { await api(`/api/orgs/tokens/${b.dataset.id}`, { method: "DELETE" }); const info = await api(`/api/orgs/${state.org}/tokens`); renderTokenList(info.tokens); toast("Key revoked"); } catch (e) { toast(e.message); }
  });
}

/* ---------- monitors (script policies + template rules) ---------- */
function monStatusBadge(s) {
  if (s === "ok") return `<span class="badge ok">${ICON.check} healthy</span>`;
  if (s === "alert") return `<span class="badge bad">${ICON.alert} alerting</span>`;
  if (s === "error") return `<span class="badge bad">error</span>`;
  return `<span class="badge na">not run</span>`;
}
function globalBadge() {
  return `<span class="badge" style="color:var(--accent);background:color-mix(in srgb,var(--accent) 14%,transparent);border:1px solid color-mix(in srgb,var(--accent) 30%,transparent)">${ICON.globe} Global</span>`;
}
function severityBadge(sev) {
  const s = sev || "warning";
  const cls = s === "critical" ? "bad" : s === "info" ? "info" : "warn";
  return `<span class="badge ${cls}">${s}</span>`;
}
function scriptName(id) { const s = (state.cache.scripts || []).find((x) => x.id === id); return s ? s.name : "—"; }
function selectMonView(view) {
  state.monView = view;
  document.querySelectorAll("#mon-view-seg button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $("mon-policies-view").classList.toggle("hidden", view !== "policies");
  $("mon-templates-view").classList.toggle("hidden", view !== "templates");
  $("mon-new").classList.toggle("hidden", view !== "policies");
  if (view === "templates") renderMonitorRules();
}
function renderMonitorsTab() {
  renderMonitors();
  if (state.monView === "templates") renderMonitorRules();
}
function renderMonitors() {
  const mons = state.cache.monitors || [];
  $("mon-sub").textContent = `${mons.length} polic${mons.length === 1 ? "y" : "ies"}`;
  const body = $("mon-body");
  body.innerHTML = mons.length ? "" : `<div class="empty"><div class="big">${ICON.shieldCheck}</div>No monitoring policies yet.<br><span class="muted">Run a monitor script on a schedule and auto-remediate on failure.</span></div>`;
  for (const m of mons) {
    const isGlobal = m.org_id == null;
    const canManage = !isGlobal || state.me.is_global_admin;
    const row = el("div", "tile"); row.style.marginBottom = "10px";
    const rem = m.remediation_script_id ? " → fix: " + escapeHtml(scriptName(m.remediation_script_id)) : " · no remediation";
    row.innerHTML = `<div style="display:flex;align-items:center;gap:12px">
      <div class="os-ico">${ICON.shieldCheck}</div>
      <div style="flex:1"><div style="font-weight:650;display:flex;align-items:center;gap:8px">${escapeHtml(m.name)} ${monStatusBadge(m.last_status)} ${severityBadge(m.severity)} ${isGlobal ? globalBadge() : ""}</div>
        <div class="h-sub">monitor: ${escapeHtml(scriptName(m.monitor_script_id))}${rem} · ${cadenceText(m)} · ${escapeHtml(targetText(m))}${m.last_run ? " · last " + relTime(m.last_run) : ""}</div></div>
      <span class="badge ${m.enabled ? "ok" : "na"}">${m.enabled ? "enabled" : "paused"}</span>
      ${canManage ? `<button class="btn ghost sm run-now">${ICON.power} Run now</button>
      <button class="btn ghost sm edit">${ICON.pencil}</button>
      <button class="btn ghost sm toggle">${m.enabled ? "Pause" : "Resume"}</button>
      <button class="btn ghost sm del">${ICON.trash}</button>` : ""}</div>`;
    if (canManage) {
      row.querySelector(".run-now").onclick = async () => { try { const r = await api(`/api/monitors/${m.id}/run`, { method: "POST" }); toast("Monitor ran: " + r.status); state.cache.monitors = await api(`/api/orgs/${state.org}/monitors`); renderMonitors(); loadRuns(); } catch (e) { toast(e.message); } };
      row.querySelector(".edit").onclick = () => openMonitorForm(m);
      row.querySelector(".toggle").onclick = async () => { try { await api(`/api/monitors/${m.id}/toggle`, { method: "POST" }); state.cache.monitors = await api(`/api/orgs/${state.org}/monitors`); renderMonitors(); } catch (e) { toast(e.message); } };
      row.querySelector(".del").onclick = async () => { if (!confirm("Delete policy “" + m.name + "”?")) return; try { await api(`/api/monitors/${m.id}`, { method: "DELETE" }); state.cache.monitors = await api(`/api/orgs/${state.org}/monitors`); buildNav(); renderMonitors(); toast("Policy deleted"); } catch (e) { toast(e.message); } };
    }
    body.appendChild(row);
  }
  $("mon-new").onclick = openMonitorForm;
}
let monitorScope = "site";
let editingMonitorId = null;
function setupMonitorModal() {
  $("mm-close-ico").innerHTML = ICON.chevR.replace('d="m9 6 6 6-6 6"', 'd="M18 6 6 18M6 6l12 12"');
  const close = () => $("monitor-modal").classList.add("hidden");
  $("mm-close").onclick = close; $("mm-cancel").onclick = close;
  $("monitor-modal").addEventListener("click", (e) => { if (e.target === $("monitor-modal")) close(); });
  $("mm-save").onclick = saveMonitor;
  $("mm-notify-switch").onclick = () => $("mm-notify-switch").classList.toggle("on");
  document.querySelectorAll("#mm-scope-seg button").forEach((b) => b.onclick = () => {
    monitorScope = b.dataset.scope;
    document.querySelectorAll("#mm-scope-seg button").forEach((x) => x.classList.toggle("active", x === b));
    const tgt = $("mm-target");
    if (monitorScope === "global") { tgt.value = "all"; tgt.disabled = true; } else { tgt.disabled = false; }
  });
}
function openMonitorForm(existing) {
  const scripts = state.cache.scripts || [];
  if (!scripts.length) return toast("Create a script first");
  const opts = scripts.map((s) => `<option value="${s.id}">${escapeHtml(s.name)}${s.category && s.category !== "Script" ? " (" + s.category + ")" : ""}</option>`).join("");
  $("mm-monitor").innerHTML = opts;
  $("mm-remediation").innerHTML = `<option value="">— none —</option>` + opts;
  const groups = (state.cache.groups || []).map((g) => `<option value="group:${g.id}">Group: ${escapeHtml(g.name)}</option>`).join("");
  const devs = (state.cache.devices || []).map((d) => `<option value="device:${d.id}">${escapeHtml(d.hostname)}</option>`).join("");
  $("mm-target").innerHTML = `<option value="all">All devices</option>` + groups + devs;
  editingMonitorId = existing ? existing.id : null;
  $("mm-title").textContent = existing ? "Edit monitoring policy" : "New monitoring policy";
  $("mm-save").textContent = existing ? "Save changes" : "Create policy";
  if (existing) {
    $("mm-name").value = existing.name;
    $("mm-monitor").value = existing.monitor_script_id;
    $("mm-remediation").value = existing.remediation_script_id || "";
    $("mm-cadence").value = String(existing.interval_minutes || 15);
    let varsText = "";
    try {
      const vars = existing.variables_json ? JSON.parse(existing.variables_json) : {};
      varsText = Object.entries(vars).map(([k, v]) => `${k}=${v}`).join("\n");
    } catch { }
    $("mm-vars").value = varsText;
    monitorScope = existing.org_id == null ? "global" : "site";
    if (existing.target_type === "group" && existing.target_id) $("mm-target").value = "group:" + existing.target_id;
    else if (existing.target_type === "device" && existing.target_id) $("mm-target").value = "device:" + existing.target_id;
    else $("mm-target").value = "all";
    $("mm-target").disabled = monitorScope === "global";
    $("mm-severity").value = existing.severity || "warning";
    $("mm-notify-switch").classList.toggle("on", existing.notify_email !== 0 && existing.notify_email !== false);
  } else {
    $("mm-name").value = ""; $("mm-vars").value = ""; $("mm-cadence").value = "15";
    monitorScope = "site";
    $("mm-target").value = "all"; $("mm-target").disabled = false;
    $("mm-severity").value = "warning";
    $("mm-notify-switch").classList.add("on");
  }
  document.querySelectorAll("#mm-scope-seg button").forEach((b) => b.classList.toggle("active", b.dataset.scope === monitorScope));
  $("mm-scope-wrap").classList.toggle("hidden", !!existing || !state.me.is_global_admin);
  $("monitor-modal").classList.remove("hidden");
  setTimeout(() => $("mm-name").focus(), 30);
}
async function saveMonitor() {
  const name = $("mm-name").value.trim();
  if (!name) { $("mm-name").focus(); return toast("Name the policy"); }
  let target_type = "all", target_id = null;
  if (monitorScope === "site") {
    const tgt = $("mm-target").value;
    if (tgt.startsWith("group:")) { target_type = "group"; target_id = tgt.slice(6); }
    else if (tgt.startsWith("device:")) { target_type = "device"; target_id = tgt.slice(7); }
  }
  const variables = {};
  for (const line of $("mm-vars").value.split("\n")) {
    const i = line.indexOf("="); if (i <= 0) continue;
    variables[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  const body = {
    name, monitor_script_id: $("mm-monitor").value,
    remediation_script_id: $("mm-remediation").value || null,
    target_type, target_id, trigger: "interval",
    interval_minutes: parseInt($("mm-cadence").value, 10), variables,
    severity: $("mm-severity").value,
    notify_email: $("mm-notify-switch").classList.contains("on"),
  };
  try {
    if (editingMonitorId) {
      await api(`/api/monitors/${editingMonitorId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      toast("Monitoring policy updated");
    } else {
      const path = monitorScope === "global" ? "/api/monitors/global" : `/api/orgs/${state.org}/monitors`;
      await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      toast(monitorScope === "global" ? "Global monitor created" : "Monitoring policy created");
    }
    $("monitor-modal").classList.add("hidden");
    state.cache.monitors = await api(`/api/orgs/${state.org}/monitors`); buildNav(); renderMonitors();
  } catch (e) { toast(e.message); }
}

/* ---------- monitors: template gallery + metric-threshold rules ---------- */
function metricIcon(metric) {
  if (metric === "cpu_percent") return ICON.cpu;
  if (metric === "mem_percent") return ICON.mem;
  if (metric === "disk_percent") return ICON.disk;
  if (metric === "wol") return ICON.power;
  return ICON.wifi;
}
function ruleValueText(r) {
  if (r.metric === "wol") return "Wake-on-LAN · Windows only";
  if (r.metric === "offline") return `unseen for ${Math.round(r.threshold)}s`;
  return `${r.metric.replace("_percent", "")} ≥ ${r.threshold}% for ${r.duration_minutes} min`;
}
function renderMonitorGallery() {
  const gallery = $("mon-gallery");
  gallery.innerHTML = (state.templates || []).map((t) => {
    const detail = t.kind === "policy"
      ? (t.os_support ? "Applies to: " + t.os_support.map((o) => o === "windows_server" ? "Windows Server" : "Windows").join(", ") : "All devices")
      : "Default: " + (t.metric === "offline" ? t.default_threshold + "s unseen" : t.default_threshold + "% for " + t.default_duration_minutes + " min");
    return `
    <div class="tile" data-tmpl="${t.id}" style="display:flex;flex-direction:column;gap:10px">
      <div style="display:flex;align-items:center;gap:10px"><div class="os-ico">${metricIcon(t.metric)}</div><div style="font-weight:650">${escapeHtml(t.name)}</div>${t.kind === "policy" ? `<span class="badge" style="margin-left:auto">policy</span>` : ""}</div>
      <div class="h-sub">${escapeHtml(t.description)}</div>
      <div class="h-sub">${detail}</div>
      <button class="btn ghost sm add-tmpl" style="align-self:flex-start">${ICON.plus} Add</button>
    </div>`;
  }).join("");
  gallery.querySelectorAll(".add-tmpl").forEach((btn) => {
    const id = btn.closest("[data-tmpl]").dataset.tmpl;
    btn.onclick = () => openRuleForm((state.templates || []).find((t) => t.id === id));
  });
}
function renderMonitorRules() {
  renderMonitorGallery();
  const rules = state.cache.monitorRules || [];
  $("mon-rules-sub").textContent = `${rules.length} rule${rules.length === 1 ? "" : "s"}`;
  const body = $("mon-rules-body");
  body.innerHTML = rules.length ? "" : `<div class="empty"><div class="big">${ICON.shieldCheck}</div>No rules yet.<br><span class="muted">Add one from the gallery above.</span></div>`;
  for (const r of rules) {
    const isGlobal = r.org_id == null;
    const canManage = !isGlobal || state.me.is_global_admin;
    const row = el("div", "tile"); row.style.marginBottom = "10px";
    row.innerHTML = `<div style="display:flex;align-items:center;gap:12px">
      <div class="os-ico">${metricIcon(r.metric)}</div>
      <div style="flex:1"><div style="font-weight:650;display:flex;align-items:center;gap:8px">${escapeHtml(r.name)} ${severityBadge(r.severity)} ${isGlobal ? globalBadge() : ""}</div>
        <div class="h-sub">${ruleValueText(r)} · ${escapeHtml(targetText(r))}</div></div>
      <span class="badge ${r.enabled ? "ok" : "na"}">${r.enabled ? "enabled" : "paused"}</span>
      ${canManage ? `<button class="btn ghost sm edit">${ICON.pencil}</button>
      <button class="btn ghost sm toggle">${r.enabled ? "Pause" : "Resume"}</button>
      <button class="btn ghost sm del">${ICON.trash}</button>` : ""}</div>`;
    if (canManage) {
      row.querySelector(".edit").onclick = () => openRuleForm((state.templates || []).find((t) => t.id === r.template_id), r);
      row.querySelector(".toggle").onclick = async () => { try { await api(`/api/monitor-rules/${r.id}/toggle`, { method: "POST" }); state.cache.monitorRules = await api(`/api/orgs/${state.org}/monitor-rules`); renderMonitorRules(); } catch (e) { toast(e.message); } };
      row.querySelector(".del").onclick = async () => { if (!confirm("Delete rule “" + r.name + "”?")) return; try { await api(`/api/monitor-rules/${r.id}`, { method: "DELETE" }); state.cache.monitorRules = await api(`/api/orgs/${state.org}/monitor-rules`); renderMonitorRules(); toast("Rule deleted"); } catch (e) { toast(e.message); } };
    }
    body.appendChild(row);
  }
}
let ruleScope = "site";
let currentTemplate = null;
let editingRuleId = null;
function setupRuleModal() {
  $("rm-close-ico").innerHTML = ICON.chevR.replace('d="m9 6 6 6-6 6"', 'd="M18 6 6 18M6 6l12 12"');
  const close = () => $("rule-modal").classList.add("hidden");
  $("rm-close").onclick = close; $("rm-cancel").onclick = close;
  $("rule-modal").addEventListener("click", (e) => { if (e.target === $("rule-modal")) close(); });
  $("rm-save").onclick = saveRule;
  $("rm-notify-switch").onclick = () => $("rm-notify-switch").classList.toggle("on");
  document.querySelectorAll("#rm-scope-seg button").forEach((b) => b.onclick = () => {
    ruleScope = b.dataset.scope;
    document.querySelectorAll("#rm-scope-seg button").forEach((x) => x.classList.toggle("active", x === b));
    const tgt = $("rm-target");
    if (ruleScope === "global") { tgt.value = "all"; tgt.disabled = true; } else { tgt.disabled = false; }
  });
}
function openRuleForm(tmpl, existing) {
  if (!tmpl) return;
  currentTemplate = tmpl;
  editingRuleId = existing ? existing.id : null;
  ruleScope = existing ? (existing.org_id == null ? "global" : "site") : "site";
  $("rm-title").textContent = existing ? "Edit: " + tmpl.name : "Add: " + tmpl.name;
  $("rm-save").textContent = existing ? "Save changes" : (tmpl.kind === "policy" ? "Apply policy" : "Add rule");
  $("rm-desc").textContent = tmpl.description;
  // A policy (e.g. Wake-on-LAN) has one standard config — only name + scope/target.
  const isPolicy = tmpl.kind === "policy";
  $("rm-metric-row").classList.toggle("hidden", isPolicy);
  $("rm-alert-row").classList.toggle("hidden", isPolicy);
  $("rm-name").value = existing ? existing.name : tmpl.name;
  $("rm-threshold-label").textContent = tmpl.metric === "offline" ? "Unseen for (seconds)" : "Threshold (%)";
  $("rm-threshold").value = existing ? existing.threshold : tmpl.default_threshold;
  $("rm-duration-wrap").classList.toggle("hidden", tmpl.metric === "offline");
  $("rm-duration").value = existing ? (existing.duration_minutes || "") : (tmpl.default_duration_minutes || "");
  const groups = (state.cache.groups || []).map((g) => `<option value="group:${g.id}">Group: ${escapeHtml(g.name)}</option>`).join("");
  const devs = (state.cache.devices || []).map((d) => `<option value="device:${d.id}">${escapeHtml(d.hostname)}</option>`).join("");
  $("rm-target").innerHTML = `<option value="all">All devices</option>` + groups + devs;
  if (existing) {
    if (existing.target_type === "group" && existing.target_id) $("rm-target").value = "group:" + existing.target_id;
    else if (existing.target_type === "device" && existing.target_id) $("rm-target").value = "device:" + existing.target_id;
    else $("rm-target").value = "all";
  }
  $("rm-target").disabled = ruleScope === "global";
  $("rm-severity").value = (existing && existing.severity) || tmpl.default_severity || "warning";
  $("rm-notify-switch").classList.toggle("on", !existing || (existing.notify_email !== 0 && existing.notify_email !== false));
  document.querySelectorAll("#rm-scope-seg button").forEach((b) => b.classList.toggle("active", b.dataset.scope === ruleScope));
  $("rm-scope-wrap").classList.toggle("hidden", !!existing || !state.me.is_global_admin);
  $("rule-modal").classList.remove("hidden");
  setTimeout(() => $("rm-name").focus(), 30);
}
async function saveRule() {
  if (!currentTemplate) return;
  const name = $("rm-name").value.trim();
  if (!name) { $("rm-name").focus(); return toast("Name the rule"); }
  let target_type = "all", target_id = null;
  if (ruleScope === "site") {
    const tgt = $("rm-target").value;
    if (tgt.startsWith("group:")) { target_type = "group"; target_id = tgt.slice(6); }
    else if (tgt.startsWith("device:")) { target_type = "device"; target_id = tgt.slice(7); }
  }
  const isPolicy = currentTemplate.kind === "policy";
  const body = {
    template_id: currentTemplate.id, name,
    threshold: isPolicy ? 0 : parseFloat($("rm-threshold").value),
    duration_minutes: (isPolicy || currentTemplate.metric === "offline") ? null : parseFloat($("rm-duration").value),
    target_type, target_id,
    severity: isPolicy ? (currentTemplate.default_severity || "info") : $("rm-severity").value,
    notify_email: isPolicy ? false : $("rm-notify-switch").classList.contains("on"),
  };
  try {
    if (editingRuleId) {
      await api(`/api/monitor-rules/${editingRuleId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      toast("Rule updated");
    } else {
      const path = ruleScope === "global" ? "/api/monitor-rules/global" : `/api/orgs/${state.org}/monitor-rules`;
      await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      toast(ruleScope === "global" ? "Global rule added" : "Rule added");
    }
    $("rule-modal").classList.add("hidden");
    state.cache.monitorRules = await api(`/api/orgs/${state.org}/monitor-rules`);
    renderMonitorRules();
  } catch (e) { toast(e.message); }
}

/* ---------- drawer ---------- */
let termSocket = null;
async function openDrawer(id) {
  state.device = id;
  state.filePath = "";
  $("scrim").classList.remove("hidden");
  $("drawer").classList.remove("hidden");
  setTimeout(() => { $("drawer").style.transform = "translateX(0)"; }, 20);
  const d = await api(`/api/devices/${id}`);
  let metrics = [];
  try { metrics = await api(`/api/devices/${id}/metrics?limit=40`); } catch {}
  const inv = d.inventory || {};
  d.latest = metrics[metrics.length - 1] || {};
  d.histCpu = metrics.map((x) => x.cpu_percent).filter((v) => v != null);
  d.uptimeStr = fmtUptime(d.latest.uptime);
  d.cores = inv.cores || inv.cpu_count;
  d.nics = inv.nics;
  d.gpu = d.gpu || inv.gpu;
  state.deviceObj = d;
  $("drawer-os-ico").innerHTML = osIcon(d.os);
  $("drawer-title").textContent = d.hostname;
  $("drawer-meta").innerHTML = `${statusPill(d.online)}<span>${ICON.monitor.replace("<svg", '<svg style="width:13px;height:13px"')} ${d.os || "—"}</span><span class="mono">${d.ip || "—"}</span>${d.logged_in_user ? `<span>${ICON.user.replace("<svg", '<svg style="width:13px;height:13px"')} ${escapeHtml(d.logged_in_user)}</span>` : ""}<span>Agent v${d.agent_version || "—"}</span>`;
  selectDrawerTab("overview");
  $("term-output").innerHTML = `<span style="color:#6b7c90">Leuffen RMM agent shell · ${d.hostname} · ${d.os || ""}</span>\n` + (d.online ? "Connected. Type a command and press Enter.\n" : "Device is offline — terminal unavailable.\n");
  renderActions(d);
}
function selectDrawerTab(tab) {
  document.querySelectorAll(".dtabs button").forEach((b) => b.classList.toggle("active", b.dataset.dtab === tab));
  ["overview", "files", "terminal", "actions"].forEach((t) => $("dtab-" + t).classList.toggle("hidden", t !== tab));
  if (tab === "overview") renderOverview(state.deviceObj);
  if (tab === "files") openFiles(); else closeFiles();
  if (tab === "terminal") openTerminal(); else closeTerminal();
}
function renderOverview(d) {
  const m = d.latest || {};
  const ram = d.ram_total ? (d.ram_total / 1e9).toFixed(0) : null;
  const disks = d.disks || [];
  const primary = disks.find((x) => x.primary) || disks[0] || null;
  const diskPct = primary ? primary.percent : m.disk_percent;
  const diskLabel = primary ? primary.mount.replace(/\\$/, "") : "Disk";
  const multi = disks.length > 1;
  const cards = d.online ? `
    <div class="stat-grid">
      <div class="sg"><div style="display:flex;align-items:center;justify-content:space-between"><div><div class="l">CPU</div><div class="v">${Math.round(m.cpu_percent || 0)}<small>%</small></div></div>${ringChart(m.cpu_percent, (m.cpu_percent >= 75) ? "var(--bad)" : "var(--accent)")}</div></div>
      <div class="sg"><div style="display:flex;align-items:center;justify-content:space-between"><div><div class="l">Memory</div><div class="v">${Math.round(m.mem_percent || 0)}<small>%</small></div></div>${ringChart(m.mem_percent, "var(--good)")}</div></div>
      <div class="sg ${multi ? "clickable" : ""}" id="disk-card"${multi ? ' title="Show all drives"' : ""}><div style="display:flex;align-items:center;justify-content:space-between"><div><div class="l">Disk ${diskLabel !== "Disk" ? `<span class="mono" style="opacity:.7">${escapeHtml(diskLabel)}</span>` : ""} ${multi ? `<span style="opacity:.6">${ICON.chevD.replace("<svg", '<svg style="width:11px;height:11px;vertical-align:middle"')}</span>` : ""}</div><div class="v">${Math.round(diskPct || 0)}<small>%</small></div></div>${ringChart(diskPct, (diskPct >= 90) ? "var(--bad)" : "var(--warn)")}</div></div>
      <div class="sg"><div class="l">Uptime</div><div class="v" style="font-size:15px;margin-top:8px">${d.uptimeStr}</div></div>
    </div>
    <div id="disk-detail" class="hidden tile" style="margin-bottom:18px">${diskRows(disks)}</div>` : `<div class="tile" style="margin-bottom:20px;text-align:center;color:var(--text-dim)">Device offline · last seen ${relTime(d.last_seen)}</div>`;
  const rows = [
    ["Operating system", `${d.os || ""} ${d.os_version || ""}`.trim()], ["Architecture", d.os_arch], ["Type", d.os_kind],
    ["Manufacturer", d.manufacturer], ["Model", d.model], ["Serial", d.serial],
    ["Processor", `${d.cpu || ""}${d.cores ? " · " + d.cores + " cores" : ""}`.trim()], ["Graphics", d.gpu], ["Memory", ram ? ram + " GB" : null],
    ["Primary IP", d.ip], ["MAC", d.mac], ["Logged-in user", d.logged_in_user], ["Agent version", d.agent_version],
  ].filter((r) => r[1]);
  let nicHtml = "";
  if (d.nics && d.nics.length) nicHtml = `<dt>Interfaces</dt><dd>${d.nics.map((n) => `${n.name}: ${(n.ipv4 || []).join(", ") || "—"}${n.mac ? " · " + n.mac : ""}`).join("<br>")}</dd>`;
  const histHtml = `<div class="sec-label" style="display:flex;align-items:center;justify-content:space-between">History
    <span class="hist-range" id="hist-range"><button data-r="24h" class="active">24h</button><button data-r="7d">7d</button><button data-r="30d">30d</button></span></div>
    <div id="hist-charts"><div class="muted" style="padding:8px 0;font-size:12.5px">Loading…</div></div>`;
  let polHtml = "";
  if (d.policies && d.policies.length) {
    polHtml = `<div class="sec-label">Applied policies</div><div class="pol-list">` + d.policies.map((p) => `
      <div class="pol-row"><span class="pol-ic">${p.kind === "policy" ? ICON.power : ICON.bell}</span>
        <span class="pol-name">${escapeHtml(p.name)}</span><span class="pol-val muted">${escapeHtml(p.value || "")}</span>
        ${p.supported ? `<span class="badge ok">active</span>` : `<span class="badge na" title="Not supported on this device's OS">not supported</span>`}</div>`).join("") + `</div>`;
  }
  $("dtab-overview").innerHTML = cards + histHtml + polHtml + `<div class="sec-label">Inventory</div><dl class="inv">${rows.map((r) => `<dt>${r[0]}</dt><dd>${r[1]}</dd>`).join("")}${nicHtml}</dl>`;
  const dc = $("disk-card");
  if (dc && multi) dc.onclick = () => $("disk-detail").classList.toggle("hidden");
  $("hist-range").querySelectorAll("button").forEach((b) => b.onclick = () => {
    $("hist-range").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    loadHistory(d.id, b.dataset.r);
  });
  loadHistory(d.id, "24h");
}
async function loadHistory(id, range) {
  const host = $("hist-charts"); if (!host) return;
  let s = [];
  try { s = await api(`/api/devices/${id}/metrics?range=${range}`); } catch (e) { host.innerHTML = `<div class="muted" style="padding:8px 0">${escapeHtml(e.message)}</div>`; return; }
  if (!s.length) { host.innerHTML = `<div class="muted" style="padding:8px 0;font-size:12.5px">No history collected for this range yet.</div>`; return; }
  const pick = (k) => s.map((x) => x[k] == null ? 0 : x[k]);
  host.innerHTML = histRow("CPU", pick("cpu_percent"), "var(--accent)") +
                   histRow("Memory", pick("mem_percent"), "var(--good)") +
                   histRow("Disk", pick("disk_percent"), "var(--warn)");
}
function histRow(label, data, color) {
  const last = data.length ? Math.round(data[data.length - 1]) : 0;
  const peak = data.length ? Math.round(Math.max(...data)) : 0;
  return `<div class="hist-row"><div class="hist-head"><span>${label}</span><span class="muted">now ${last}% · peak ${peak}%</span></div>${areaChart(data, color, 100)}</div>`;
}
function areaChart(data, color, fixedMax) {
  if (!data || !data.length) return "";
  const w = 320, h = 56, max = fixedMax || Math.max(...data, 1), rng = Math.max(max, 1), n = data.length;
  const pts = data.map((v, i) => [(i / Math.max(n - 1, 1)) * w, h - (Math.min(v, max) / rng) * (h - 6) - 3]);
  const path = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const id = "a" + Math.random().toString(36).slice(2, 7);
  return `<svg class="area" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".3"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs><path d="${path} L${w} ${h} L0 ${h} Z" fill="url(#${id})"/><path d="${path}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
}
function diskRows(disks) {
  if (!disks.length) return `<div class="muted" style="font-size:12.5px">No drive details reported yet.</div>`;
  return `<div class="sec-label" style="margin-top:0">All drives</div>` + disks.map((x) => {
    const col = x.percent >= 90 ? "var(--bad)" : x.percent >= 75 ? "var(--warn)" : "var(--good)";
    return `<div style="margin-bottom:12px"><div style="display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:5px">
      <span><b class="mono">${escapeHtml(x.mount.replace(/\\$/, ""))}</b>${x.fs ? ` <span class="muted">${escapeHtml(x.fs)}</span>` : ""}</span>
      <span class="muted">${fmtBytes(x.used)} / ${fmtBytes(x.total)} · ${Math.round(x.percent)}%</span></div>
      <div class="bar" style="height:6px;background:var(--surface-3);border-radius:99px;overflow:hidden"><i style="display:block;height:100%;width:${x.percent}%;background:${col}"></i></div></div>`;
  }).join("");
}
function fmtBytes(n) {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
function renderActions(d) {
  const acts = [
    { t: "Wake (WoL)", d: "Send magic packet", i: ICON.power, c: "go", f: () => act(`/api/devices/${d.id}/wake`, {}) },
    { t: "Lock screen", d: "Lock the session", i: ICON.lock, c: "", f: () => power(d.id, "lock") },
    { t: "Log off", d: "Sign out user", i: ICON.logout, c: "", f: () => power(d.id, "logoff") },
    { t: "Restart", d: "Reboot device", i: ICON.restart, c: "warn", f: () => confirm("Reboot " + d.hostname + "?") && power(d.id, "reboot") },
    { t: "Shut down", d: "Power off device", i: ICON.power, c: "danger", f: () => confirm("Shut down " + d.hostname + "?") && power(d.id, "shutdown") },
    { t: "Remove", d: "Delete from RMM", i: ICON.trash, c: "danger", f: async () => { if (confirm("Remove " + d.hostname + "?")) { try { await api(`/api/devices/${d.id}`, { method: "DELETE" }); toast("Device removed"); closeDrawer(); refreshOrgCaches().then(() => { buildNav(); renderDevices(); }); } catch (e) { toast(e.message); } } } },
  ];
  let html = `<div class="actions-grid">${acts.map((a, i) => `<button class="action ${a.c}" data-i="${i}"><span class="ai">${a.i}</span><span><span class="at">${a.t}</span><br><span class="ad">${a.d}</span></span></button>`).join("")}</div>`;
  html += `<div class="sec-label">Agent</div><div class="tile"><div class="field" style="align-items:center">
      <div style="flex:1;font-size:12.5px" class="muted">Installed: <b>v${d.agent_version || "—"}</b><span id="upd-latest"></span></div>
      <button class="btn" id="update-agent"${d.online ? "" : " disabled"}>${ICON.download} Update agent</button></div>
      <div class="muted" style="font-size:12px;margin-top:8px">Downloads &amp; installs the latest build in place, keeping this device's config.</div></div>`;
  const otherOrgs = (state.me.orgs || []).filter((o) => o.id !== d.org_id);
  if (otherOrgs.length) {
    html += `<div class="sec-label">Organisation</div><div class="tile"><div class="field">
      <select id="move-org">${otherOrgs.map((o) => `<option value="${o.id}">${escapeHtml(o.name)}</option>`).join("")}</select>
      <button class="btn" id="move-org-btn">${ICON.building} Move here</button></div></div>`;
  }
  html += `<div class="sec-label">Network node</div>`;
  if (d.is_node) {
    const subs = (d.subnets || []).map((s) => s.cidr);
    html += `<div class="tile"><div class="muted" style="font-size:12.5px;margin-bottom:10px">This device relays Wake-on-LAN &amp; scans for these subnets:</div>
      ${subs.length ? subs.map((s) => `<div class="subnet-row">${s}<span class="x" title="Remove" data-cidr="${s}">${ICON.trash.replace("<svg", '<svg style="width:14px;height:14px"')}</span></div>`).join("") : '<div class="muted" style="font-size:12px;margin-bottom:8px">No subnets yet.</div>'}
      <div class="field"><input placeholder="10.0.0.0/24" id="cidr-input"/><button class="btn" id="add-subnet">${ICON.plus} Add</button></div>
      <button class="btn ghost" style="margin-top:12px" id="demote">Demote to plain agent</button></div>`;
  } else {
    html += `<div class="tile"><div class="muted" style="font-size:12.5px;margin-bottom:12px">Promote this device to a network node so it can relay Wake-on-LAN and scan its local subnets.</div><button class="btn" id="promote">${ICON.nodes} Promote to network node</button></div>`;
  }
  $("dtab-actions").innerHTML = html;
  $("dtab-actions").querySelectorAll(".action").forEach((b) => b.onclick = () => acts[+b.dataset.i].f());
  const upd = $("update-agent");
  if (upd) {
    upd.onclick = async () => {
      if (!confirm("Download and install the latest agent on " + d.hostname + "?\nThe agent will briefly restart.")) return;
      upd.disabled = true; const old = upd.innerHTML; upd.innerHTML = "Updating…";
      try { await api(`/api/devices/${d.id}/update-agent`, { method: "POST" }); toast("Update sent — agent is upgrading"); }
      catch (e) { toast(e.message); upd.disabled = false; upd.innerHTML = old; }
    };
    upd._dev = d;
    api(`/api/agent-release`).then((r) => {
      const el = $("upd-latest"); if (!el) return;
      const latest = (r && (r.tag || r.name) || "").replace(/^.*?(\d+\.\d+(\.\d+)?).*$/, "$1");
      const installed = (d.agent_version || "").replace(/^v/, "");
      if (latest) el.innerHTML = ` · Latest: <b>v${escapeHtml(latest)}</b>`;
      const upToDate = installed && latest && cmpVer(installed, latest) >= 0;
      if (upToDate) {
        upd.disabled = true;
        upd.innerHTML = `${ICON.check} Up to date`;
        upd.classList.add("ghost");
      }
    }).catch(() => {});
  }
  const mob = $("move-org-btn");
  if (mob) mob.onclick = async () => {
    const oid = $("move-org").value;
    try { await api(`/api/devices/${d.id}/move-org`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ org_id: oid }) }); toast("Device moved"); closeDrawer(); refreshOrgCaches().then(() => { buildNav(); renderDevices(); }); } catch (e) { toast(e.message); }
  };
  const add = $("add-subnet");
  if (add) add.onclick = async () => { const v = $("cidr-input").value.trim(); if (!v) return; try { await api(`/api/devices/${d.id}/subnets`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cidr: v }) }); toast("Subnet " + v + " added"); await refreshNodeViews(); openDrawer(d.id); } catch (e) { toast(e.message); } };
  const promo = $("promote");
  if (promo) promo.onclick = async () => { try { await api(`/api/devices/${d.id}/promote`, { method: "POST" }); toast("Promoted to network node"); await refreshNodeViews(); openDrawer(d.id); } catch (e) { toast(e.message); } };
  const demo = $("demote");
  if (demo) demo.onclick = async () => { try { await api(`/api/devices/${d.id}/demote`, { method: "POST" }); toast("Demoted to plain agent"); await refreshNodeViews(); openDrawer(d.id); } catch (e) { toast(e.message); } };
  $("dtab-actions").querySelectorAll(".subnet-row .x").forEach((x) => x.onclick = async () => {
    const s = (d.subnets || []).find((su) => su.cidr === x.dataset.cidr);
    if (!s) return;
    try { await api(`/api/subnets/${s.id}`, { method: "DELETE" }); toast("Subnet removed"); await refreshNodeViews(); openDrawer(d.id); } catch (e) { toast(e.message); }
  });
}
// Keep the Nodes/Network views (driven by the org cache) in sync after a change
// made from a device drawer.
async function refreshNodeViews() {
  await refreshOrgCaches();
  buildNav();
  renderNodes();
  renderNetwork();
}
async function act(path, body) {
  try { const r = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); toast(typeof r === "object" ? (r.via ? "Magic packet sent via " + r.via : (r.status || "Done")) : "Done"); } catch (e) { toast(e.message); }
}
async function power(id, action) {
  try { await api(`/api/devices/${id}/power`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }) }); toast(action + " sent"); } catch (e) { toast(e.message); }
}
// Compare dotted numeric versions: -1 if a<b, 0 if equal, 1 if a>b.
function cmpVer(a, b) {
  const pa = String(a).split("."), pb = String(b).split(".");
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = parseInt(pa[i] || "0", 10) || 0, y = parseInt(pb[i] || "0", 10) || 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}
function closeDrawer() { closeTerminal(); closeFiles(); const d = $("drawer"); d.style.transform = "translateX(100%)"; setTimeout(() => d.classList.add("hidden"), 280); $("scrim").classList.add("hidden"); state.device = null; }

/* ---------- remote file management (centered modal) ---------- */
function openFiles() {
  if (!state.deviceObj) return;
  const d = state.deviceObj;
  $("fm-host").textContent = `${d.hostname}${d.os ? " · " + d.os : ""}`;
  $("files-scrim").classList.remove("hidden");
  $("files-modal").classList.remove("hidden");
  if (!d.online) { $("files-body").innerHTML = `<div class="tile" style="text-align:center;color:var(--text-dim)">Device is offline — file management unavailable.</div>`; return; }
  renderFiles(state.filePath || "");
}
function closeFiles() { $("files-scrim").classList.add("hidden"); $("files-modal").classList.add("hidden"); }
function dismissFiles() { selectDrawerTab("overview"); }
async function renderFiles(path) {
  state.filePath = path;
  const host = $("files-body");
  host.innerHTML = `<div class="muted" style="padding:14px">Loading…</div>`;
  let res;
  try { res = await api(`/api/devices/${state.device}/files?path=${encodeURIComponent(path)}`); }
  catch (e) { host.innerHTML = `<div class="callout warn"><div class="ic">${ICON.alert}</div><div><div class="ct">Can't open folder</div><div class="cd">${escapeHtml(e.message)}</div></div></div>`; return; }
  const entries = res.entries || [];
  const atRoot = !!res.roots || !path;
  const bar = `<div class="file-bar">
      <button class="btn ghost sm" id="f-up" ${atRoot ? "disabled" : ""}>${ICON.arrowUp} Up</button>
      <span class="file-path mono" title="${escapeHtml(path)}">${escapeHtml(path || "Drives")}</span>
      <span style="flex:1"></span>
      <button class="btn ghost sm" id="f-refresh">${ICON.refresh}</button>
      ${atRoot ? "" : `<button class="btn ghost sm" id="f-mkdir">${ICON.plus} Folder</button>
      <label class="btn ghost sm" style="cursor:pointer;margin:0">${ICON.upload} Upload<input type="file" id="f-upload" hidden></label>`}
    </div>`;
  const rows = entries.map((e, i) => `<tr data-i="${i}">
      <td><span class="file-ico">${e.is_dir ? ICON.folder : ICON.file}</span>${e.is_dir ? `<a class="file-open" data-p="${escapeHtml(e.path)}">${escapeHtml(e.name)}</a>` : `<span>${escapeHtml(e.name)}</span>`}</td>
      <td class="mono file-size" data-p="${escapeHtml(e.path)}">${e.is_dir ? `<button class="btn ghost sm fsize">calc</button>` : fmtBytes(e.size)}</td>
      <td class="muted">${e.modified ? relTime(e.modified) : "—"}</td>
      <td style="text-align:right;white-space:nowrap">${e.is_dir ? "" : `<button class="btn ghost sm fdl" title="Download" data-p="${escapeHtml(e.path)}" data-n="${escapeHtml(e.name)}">${ICON.download}</button>`}<button class="btn ghost sm fdel" title="Delete" data-p="${escapeHtml(e.path)}" data-n="${escapeHtml(e.name)}" data-dir="${e.is_dir ? 1 : 0}">${ICON.trash}</button></td>
    </tr>`).join("");
  host.innerHTML = bar + `<table class="grid file-table"><thead><tr><th>Name</th><th>Size</th><th>Modified</th><th></th></tr></thead><tbody>${rows || `<tr><td colspan="4" class="muted" style="padding:18px">Empty folder.</td></tr>`}</tbody></table>`;

  const up = $("f-up"); if (up) up.onclick = () => renderFiles(res.parent || "");
  $("f-refresh").onclick = () => renderFiles(path);
  const mk = $("f-mkdir");
  if (mk) mk.onclick = async () => {
    const name = (prompt("New folder name?") || "").trim(); if (!name) return;
    const sep = path.includes("\\") || /^[A-Za-z]:/.test(path) ? "\\" : "/";
    try { await api(`/api/devices/${state.device}/files/mkdir`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: path.replace(/[\\/]$/, "") + sep + name }) }); toast("Folder created"); renderFiles(path); } catch (e) { toast(e.message); }
  };
  const upl = $("f-upload");
  if (upl) upl.onchange = async () => {
    const f = upl.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    toast("Uploading " + f.name + "…");
    try {
      const r = await fetch(`/api/devices/${state.device}/files/upload?path=${encodeURIComponent(path)}`, { method: "POST", body: fd });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      toast("Uploaded " + f.name); renderFiles(path);
    } catch (e) { toast(e.message); }
  };
  host.querySelectorAll(".file-open").forEach((a) => a.onclick = () => renderFiles(a.dataset.p));
  host.querySelectorAll(".fsize").forEach((b) => b.onclick = async () => {
    const cell = b.closest(".file-size"); b.disabled = true; b.textContent = "…";
    try { const r = await api(`/api/devices/${state.device}/files/size?path=${encodeURIComponent(cell.dataset.p)}`); cell.textContent = fmtBytes(r.size) + (r.truncated ? "+" : ""); }
    catch (e) { b.disabled = false; b.textContent = "calc"; toast(e.message); }
  });
  host.querySelectorAll(".fdl").forEach((b) => b.onclick = () => {
    const a = document.createElement("a");
    a.href = `/api/devices/${state.device}/files/download?path=${encodeURIComponent(b.dataset.p)}`;
    a.download = b.dataset.n; document.body.appendChild(a); a.click(); a.remove();
  });
  host.querySelectorAll(".fdel").forEach((b) => b.onclick = async () => {
    if (!confirm(`Delete ${b.dataset.dir === "1" ? "folder" : "file"} "${b.dataset.n}"?${b.dataset.dir === "1" ? "\nThis removes everything inside it." : ""}`)) return;
    try { await api(`/api/devices/${state.device}/files/delete`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: b.dataset.p }) }); toast("Deleted"); renderFiles(path); } catch (e) { toast(e.message); }
  });
}

/* ---------- terminal (real WebSocket bridge) ---------- */
function openTerminal() {
  if (termSocket || !state.device) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  termSocket = new WebSocket(`${proto}://${location.host}/api/devices/${state.device}/terminal`);
  termSocket.onmessage = (ev) => {
    const out = $("term-output");
    try { const m = JSON.parse(ev.data); if (m.data) out.innerHTML += escapeHtml(m.data); if (m.error) out.innerHTML += `<span style="color:var(--bad)">[${escapeHtml(m.error)}]</span>\n`; } catch {}
    out.scrollTop = out.scrollHeight;
  };
  termSocket.onclose = () => { termSocket = null; };
}
function closeTerminal() { if (termSocket) { termSocket.close(); termSocket = null; } }
function onTerm(e) {
  e.preventDefault();
  const inp = $("term-input"), cmd = inp.value.trim(); if (!cmd) return;
  const out = $("term-output");
  out.innerHTML += `<span class="p">$</span> ${escapeHtml(cmd)}\n`;
  if (termSocket && termSocket.readyState === 1) termSocket.send(JSON.stringify({ data: cmd }));
  else out.innerHTML += `<span style="color:var(--bad)">[not connected]</span>\n`;
  out.scrollTop = out.scrollHeight; inp.value = "";
}
function escapeHtml(s) { return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }

function clearRefresh() { if (state.refresh) { clearInterval(state.refresh); state.refresh = null; } }

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("script-modal").classList.contains("hidden")) $("script-modal").classList.add("hidden");
  else if (!$("monitor-modal").classList.contains("hidden")) $("monitor-modal").classList.add("hidden");
  else if (!$("drawer").classList.contains("hidden")) closeDrawer();
});
init().catch((e) => { document.body.innerHTML = `<div style="padding:48px;font-family:sans-serif;color:#e9eef6;background:#0a0c11;min-height:100vh"><h2>Couldn't load the dashboard</h2><p style="color:#97a3b4">${e.message}</p><p><a href="/auth/login" style="color:#3b82f6">Sign in →</a></p></div>`; });
