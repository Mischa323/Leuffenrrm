"use strict";
/* ============================================================================
   Leuffen RMM — dashboard logic (modernised UI, wired to the live API).
   Dependency-free vanilla JS.
   ========================================================================== */
const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };

const state = { me: null, org: null, orgName: null, group: null, tab: "devices", device: null, refresh: null, cache: {} };

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
  $("refresh-global").onclick = () => showGlobal();
  $("org-switch").onclick = cycleOrg;
  document.querySelectorAll(".nav button").forEach((b) => b.onclick = () => selectTab(b.dataset.tab));
  document.querySelectorAll(".dtabs button").forEach((b) => b.onclick = () => selectDrawerTab(b.dataset.dtab));
  $("term-form").onsubmit = onTerm;
  $("approvals-ico").innerHTML = ICON.shieldCheck;
  setupScriptModal();
  setupMonitorModal();
  refreshPendingBadge();
  setInterval(refreshPendingBadge, 30000);
  await showGlobal();
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

  const data = await api("/api/overview");
  const orgs = data.orgs.map((o) => ({ ...o, color: colorFor(o.id) }));
  state.orgs = orgs;
  const tot = orgs.reduce((a, o) => ({ devices: a.devices + o.devices, online: a.online + o.online, offline: a.offline + o.offline, noncompliant: a.noncompliant + o.noncompliant }), { devices: 0, online: 0, offline: 0, noncompliant: 0 });
  const uptime = tot.devices ? ((tot.online / tot.devices) * 100).toFixed(1) : "0";
  $("kpis").innerHTML = [
    kpi("Organisations", orgs.length, "blue", ICON.building, ""),
    kpi("Total devices", tot.devices, "blue", ICON.monitor, ""),
    kpi("Online now", tot.online, "green", ICON.zap, "", `<span class="kdelta up">${ICON.arrowUp} ${uptime}% uptime</span>`),
    kpi("Non-compliant", tot.noncompliant, "amber", ICON.shield, "", tot.noncompliant ? `<span class="kdelta down">needs attention</span>` : `<span class="kdelta up">all clear</span>`),
    kpi("Offline", tot.offline, "red", ICON.bell, "", tot.offline ? `<span class="kdelta down">${tot.offline} down</span>` : `<span class="kdelta">none</span>`),
  ].join("");

  const wrap = $("org-cards"); wrap.innerHTML = "";
  if (!orgs.length) { wrap.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="big">${ICON.building}</div>No organisations yet. Add one in Settings → Organisations.</div>`; return; }
  for (const o of orgs) {
    const c = el("div", "orgcard");
    const onPct = o.devices ? (o.online / o.devices) * 100 : 0, offPct = o.devices ? (o.offline / o.devices) * 100 : 0, ncPct = o.devices ? (o.noncompliant / o.devices) * 100 : 0;
    c.innerHTML = `
      <div class="oc-head">
        <div class="oc-mark" style="background:linear-gradient(140deg, ${o.color}, color-mix(in srgb, ${o.color} 55%, #000))">${initials(o.name)}</div>
        <div style="flex:1"><h3>${o.name}</h3><small>${o.devices} devices · ${o.online} online</small></div>
        <div class="oc-arrow">${ICON.chevR}</div>
      </div>
      <div class="health-bar"><i class="on" style="width:${onPct}%"></i><i class="nc" style="width:${ncPct}%"></i><i class="off" style="width:${offPct}%"></i></div>
      <div class="oc-stats">
        <div class="s"><b><span class="dot-led g"></span>${o.online}</b><span>Online</span></div>
        <div class="s"><b><span class="dot-led m"></span>${o.offline}</b><span>Offline</span></div>
        <div class="s"><b><span class="dot-led r"></span>${o.noncompliant}</b><span>Non-compliant</span></div>
      </div>`;
    c.onclick = () => showOrg(o.id, o.name);
    wrap.appendChild(c);
  }
}
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
  const [devices, hosts, nodes, groups, scripts, schedules, monitors, pending] = await Promise.all([
    api(`/api/orgs/${state.org}/devices`),
    api(`/api/orgs/${state.org}/network/hosts`).catch(() => []),
    api(`/api/orgs/${state.org}/nodes`).catch(() => []),
    api(`/api/orgs/${state.org}/groups`).catch(() => []),
    api(`/api/orgs/${state.org}/scripts`).catch(() => []),
    api(`/api/orgs/${state.org}/schedules`).catch(() => []),
    api(`/api/orgs/${state.org}/monitors`).catch(() => []),
    api(`/api/orgs/${state.org}/pending`).catch(() => []),
  ]);
  state.cache = { devices, hosts, nodes, groups, scripts, schedules, monitors, pending };
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
  if (tab === "devices") { renderDevices(); state.refresh = setInterval(async () => { try { state.cache.devices = await api(`/api/orgs/${state.org}/devices`); renderDevices(); } catch {} }, 5000); }
  else if (tab === "approvals") renderApprovals();
  else if (tab === "network") renderNetwork();
  else if (tab === "nodes") renderNodes();
  else if (tab === "scripts") renderScripts();
  else if (tab === "monitors") renderMonitors();
  else if (tab === "downloads") renderDownloads();
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

/* ---------- monitors (policies) ---------- */
function monStatusBadge(s) {
  if (s === "ok") return `<span class="badge ok">${ICON.check} healthy</span>`;
  if (s === "alert") return `<span class="badge bad">${ICON.alert} alerting</span>`;
  if (s === "error") return `<span class="badge bad">error</span>`;
  return `<span class="badge na">not run</span>`;
}
function scriptName(id) { const s = (state.cache.scripts || []).find((x) => x.id === id); return s ? s.name : "—"; }
function renderMonitors() {
  const mons = state.cache.monitors || [];
  $("mon-sub").textContent = `${mons.length} polic${mons.length === 1 ? "y" : "ies"}`;
  const body = $("mon-body");
  body.innerHTML = mons.length ? "" : `<div class="empty"><div class="big">${ICON.shieldCheck}</div>No monitoring policies yet.<br><span class="muted">Run a monitor script on a schedule and auto-remediate on failure.</span></div>`;
  for (const m of mons) {
    const row = el("div", "tile"); row.style.marginBottom = "10px";
    const rem = m.remediation_script_id ? " → fix: " + escapeHtml(scriptName(m.remediation_script_id)) : " · no remediation";
    row.innerHTML = `<div style="display:flex;align-items:center;gap:12px">
      <div class="os-ico">${ICON.shieldCheck}</div>
      <div style="flex:1"><div style="font-weight:650;display:flex;align-items:center;gap:8px">${escapeHtml(m.name)} ${monStatusBadge(m.last_status)}</div>
        <div class="h-sub">monitor: ${escapeHtml(scriptName(m.monitor_script_id))}${rem} · ${cadenceText(m)} · ${escapeHtml(targetText(m))}${m.last_run ? " · last " + relTime(m.last_run) : ""}</div></div>
      <span class="badge ${m.enabled ? "ok" : "na"}">${m.enabled ? "enabled" : "paused"}</span>
      <button class="btn ghost sm run-now">${ICON.power} Run now</button>
      <button class="btn ghost sm toggle">${m.enabled ? "Pause" : "Resume"}</button>
      <button class="btn ghost sm del">${ICON.trash}</button></div>`;
    row.querySelector(".run-now").onclick = async () => { try { const r = await api(`/api/monitors/${m.id}/run`, { method: "POST" }); toast("Monitor ran: " + r.status); state.cache.monitors = await api(`/api/orgs/${state.org}/monitors`); renderMonitors(); loadRuns(); } catch (e) { toast(e.message); } };
    row.querySelector(".toggle").onclick = async () => { try { await api(`/api/monitors/${m.id}/toggle`, { method: "POST" }); state.cache.monitors = await api(`/api/orgs/${state.org}/monitors`); renderMonitors(); } catch (e) { toast(e.message); } };
    row.querySelector(".del").onclick = async () => { if (!confirm("Delete policy “" + m.name + "”?")) return; try { await api(`/api/monitors/${m.id}`, { method: "DELETE" }); state.cache.monitors = await api(`/api/orgs/${state.org}/monitors`); buildNav(); renderMonitors(); toast("Policy deleted"); } catch (e) { toast(e.message); } };
    body.appendChild(row);
  }
  $("mon-new").onclick = openMonitorForm;
}
function setupMonitorModal() {
  $("mm-close-ico").innerHTML = ICON.chevR.replace('d="m9 6 6 6-6 6"', 'd="M18 6 6 18M6 6l12 12"');
  const close = () => $("monitor-modal").classList.add("hidden");
  $("mm-close").onclick = close; $("mm-cancel").onclick = close;
  $("monitor-modal").addEventListener("click", (e) => { if (e.target === $("monitor-modal")) close(); });
  $("mm-save").onclick = saveMonitor;
}
function openMonitorForm() {
  const scripts = state.cache.scripts || [];
  if (!scripts.length) return toast("Create a script first");
  const opts = scripts.map((s) => `<option value="${s.id}">${escapeHtml(s.name)}${s.category && s.category !== "Script" ? " (" + s.category + ")" : ""}</option>`).join("");
  $("mm-monitor").innerHTML = opts;
  $("mm-remediation").innerHTML = `<option value="">— none —</option>` + opts;
  const groups = (state.cache.groups || []).map((g) => `<option value="group:${g.id}">Group: ${escapeHtml(g.name)}</option>`).join("");
  const devs = (state.cache.devices || []).map((d) => `<option value="device:${d.id}">${escapeHtml(d.hostname)}</option>`).join("");
  $("mm-target").innerHTML = `<option value="all">All devices</option>` + groups + devs;
  $("mm-name").value = ""; $("mm-vars").value = ""; $("mm-cadence").value = "15";
  $("monitor-modal").classList.remove("hidden");
  setTimeout(() => $("mm-name").focus(), 30);
}
async function saveMonitor() {
  const name = $("mm-name").value.trim();
  if (!name) { $("mm-name").focus(); return toast("Name the policy"); }
  const tgt = $("mm-target").value;
  let target_type = "all", target_id = null;
  if (tgt.startsWith("group:")) { target_type = "group"; target_id = tgt.slice(6); }
  else if (tgt.startsWith("device:")) { target_type = "device"; target_id = tgt.slice(7); }
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
  };
  try {
    await api(`/api/orgs/${state.org}/monitors`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("monitor-modal").classList.add("hidden"); toast("Monitoring policy created");
    state.cache.monitors = await api(`/api/orgs/${state.org}/monitors`); buildNav(); renderMonitors();
  } catch (e) { toast(e.message); }
}

/* ---------- drawer ---------- */
let termSocket = null;
async function openDrawer(id) {
  state.device = id;
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
  ["overview", "terminal", "actions"].forEach((t) => $("dtab-" + t).classList.toggle("hidden", t !== tab));
  if (tab === "overview") renderOverview(state.deviceObj);
  if (tab === "terminal") openTerminal(); else closeTerminal();
}
function renderOverview(d) {
  const m = d.latest || {};
  const ram = d.ram_total ? (d.ram_total / 1e9).toFixed(0) : null;
  const cards = d.online ? `
    <div class="stat-grid">
      <div class="sg"><div style="display:flex;align-items:center;justify-content:space-between"><div><div class="l">CPU</div><div class="v">${Math.round(m.cpu_percent || 0)}<small>%</small></div></div>${ringChart(m.cpu_percent, (m.cpu_percent >= 75) ? "var(--bad)" : "var(--accent)")}</div></div>
      <div class="sg"><div style="display:flex;align-items:center;justify-content:space-between"><div><div class="l">Memory</div><div class="v">${Math.round(m.mem_percent || 0)}<small>%</small></div></div>${ringChart(m.mem_percent, "var(--good)")}</div></div>
      <div class="sg"><div style="display:flex;align-items:center;justify-content:space-between"><div><div class="l">Disk</div><div class="v">${Math.round(m.disk_percent || 0)}<small>%</small></div></div>${ringChart(m.disk_percent, (m.disk_percent >= 90) ? "var(--bad)" : "var(--warn)")}</div></div>
      <div class="sg"><div class="l">Uptime</div><div class="v" style="font-size:15px;margin-top:8px">${d.uptimeStr}</div></div>
    </div>` : `<div class="tile" style="margin-bottom:20px;text-align:center;color:var(--text-dim)">Device offline · last seen ${relTime(d.last_seen)}</div>`;
  const rows = [
    ["Operating system", `${d.os || ""} ${d.os_version || ""}`.trim()], ["Architecture", d.os_arch], ["Type", d.os_kind],
    ["Manufacturer", d.manufacturer], ["Model", d.model], ["Serial", d.serial],
    ["Processor", `${d.cpu || ""}${d.cores ? " · " + d.cores + " cores" : ""}`.trim()], ["Memory", ram ? ram + " GB" : null],
    ["Primary IP", d.ip], ["MAC", d.mac], ["Logged-in user", d.logged_in_user], ["Agent version", d.agent_version],
  ].filter((r) => r[1]);
  let nicHtml = "";
  if (d.nics && d.nics.length) nicHtml = `<dt>Interfaces</dt><dd>${d.nics.map((n) => `${n.name}: ${(n.ipv4 || []).join(", ") || "—"}${n.mac ? " · " + n.mac : ""}`).join("<br>")}</dd>`;
  $("dtab-overview").innerHTML = cards + `<div class="sec-label">Inventory</div><dl class="inv">${rows.map((r) => `<dt>${r[0]}</dt><dd>${r[1]}</dd>`).join("")}${nicHtml}</dl>`;
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
function closeDrawer() { closeTerminal(); const d = $("drawer"); d.style.transform = "translateX(100%)"; setTimeout(() => d.classList.add("hidden"), 280); $("scrim").classList.add("hidden"); state.device = null; }

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
