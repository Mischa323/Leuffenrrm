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
  await showGlobal();
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
  if (!orgs.length) { wrap.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="big">${ICON.building}</div>No organisations yet.</div>`; return; }
  for (const o of orgs) {
    const c = el("div", "orgcard");
    const onPct = o.devices ? (o.online / o.devices) * 100 : 0, offPct = o.devices ? (o.offline / o.devices) * 100 : 0, ncPct = o.devices ? (o.noncompliant / o.devices) * 100 : 0;
    c.innerHTML = `
      <div class="oc-head">
        <div class="oc-mark" style="background:linear-gradient(140deg, ${o.color}, color-mix(in srgb, ${o.color} 55%, #000))">${initials(o.name)}</div>
        <div><h3>${o.name}</h3><small>${o.devices} devices · ${o.online} online</small></div>
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
  const [devices, hosts, nodes, groups] = await Promise.all([
    api(`/api/orgs/${state.org}/devices`),
    api(`/api/orgs/${state.org}/network/hosts`).catch(() => []),
    api(`/api/orgs/${state.org}/nodes`).catch(() => []),
    api(`/api/orgs/${state.org}/groups`).catch(() => []),
  ]);
  state.cache = { devices, hosts, nodes, groups };
}
function buildNav() {
  $("nav-devices-count").textContent = state.cache.devices.length;
  $("nav-network-count").textContent = state.cache.hosts.length;
  $("nav-nodes-count").textContent = state.cache.nodes.length;
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
  ["devices", "network", "nodes", "downloads"].forEach((t) => $("tab-" + t).classList.toggle("hidden", t !== tab));
  clearRefresh();
  if (tab === "devices") { renderDevices(); state.refresh = setInterval(async () => { try { state.cache.devices = await api(`/api/orgs/${state.org}/devices`); renderDevices(); } catch {} }, 5000); }
  else if (tab === "network") renderNetwork();
  else if (tab === "nodes") renderNodes();
  else if (tab === "downloads") renderDownloads();
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

function renderDownloads() {
  const base = location.origin;
  $("downloads-body").innerHTML = `
    <div class="dl-block"><div class="lab">${ICON.linux} Linux (one-liner)</div>
      <div class="code"><button class="btn ghost sm copy" data-c="curl -fsSL ${base}/api/orgs/${state.org}/install.sh | sudo bash">${ICON.copy} Copy</button>curl -fsSL ${base}/api/orgs/${state.org}/install.sh | sudo bash</div></div>
    <div class="dl-block"><div class="lab">${ICON.windows} Windows (PowerShell, admin)</div>
      <div class="code"><button class="btn ghost sm copy" data-c="iwr ${base}/api/orgs/${state.org}/install.ps1 -UseBasicParsing | iex">${ICON.copy} Copy</button>iwr ${base}/api/orgs/${state.org}/install.ps1 -UseBasicParsing | iex</div></div>
    <div class="dl-block"><div class="lab">${ICON.download} Manual install</div>
      <div class="code">Download: ${base}/api/orgs/${state.org}/agent.zip
Run: python agent.py  <span style="color:#5f7088"># config bundled inside</span></div></div>`;
  $("downloads-body").querySelectorAll(".copy").forEach((b) => b.onclick = () => { navigator.clipboard?.writeText(b.dataset.c); toast("Copied to clipboard"); });
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
  $("drawer-meta").innerHTML = `${statusPill(d.online)}<span>${ICON.monitor.replace("<svg", '<svg style="width:13px;height:13px"')} ${d.os || "—"}</span><span class="mono">${d.ip || "—"}</span><span>Agent v${d.agent_version || "—"}</span>`;
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
    ["Primary IP", d.ip], ["MAC", d.mac], ["Agent version", d.agent_version],
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
  const add = $("add-subnet");
  if (add) add.onclick = async () => { const v = $("cidr-input").value.trim(); if (!v) return; try { await api(`/api/devices/${d.id}/subnets`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cidr: v }) }); toast("Subnet " + v + " added"); openDrawer(d.id); } catch (e) { toast(e.message); } };
  const promo = $("promote");
  if (promo) promo.onclick = async () => { try { await api(`/api/devices/${d.id}/promote`, { method: "POST" }); toast("Promoted to network node"); refreshOrgCaches(); openDrawer(d.id); } catch (e) { toast(e.message); } };
  const demo = $("demote");
  if (demo) demo.onclick = async () => { try { await api(`/api/devices/${d.id}/demote`, { method: "POST" }); toast("Demoted to plain agent"); refreshOrgCaches(); openDrawer(d.id); } catch (e) { toast(e.message); } };
  $("dtab-actions").querySelectorAll(".subnet-row .x").forEach((x) => x.onclick = async () => {
    const s = (d.subnets || []).find((su) => su.cidr === x.dataset.cidr);
    if (!s) return;
    try { await api(`/api/subnets/${s.id}`, { method: "DELETE" }); toast("Subnet removed"); openDrawer(d.id); } catch (e) { toast(e.message); }
  });
}
async function act(path, body) {
  try { const r = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); toast(typeof r === "object" ? (r.via ? "Magic packet sent via " + r.via : (r.status || "Done")) : "Done"); } catch (e) { toast(e.message); }
}
async function power(id, action) {
  try { await api(`/api/devices/${id}/power`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }) }); toast(action + " sent"); } catch (e) { toast(e.message); }
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

document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("drawer").classList.contains("hidden")) closeDrawer(); });
init().catch((e) => { document.body.innerHTML = `<div style="padding:48px;font-family:sans-serif;color:#e9eef6;background:#0a0c11;min-height:100vh"><h2>Couldn't load the dashboard</h2><p style="color:#97a3b4">${e.message}</p><p><a href="/auth/login" style="color:#3b82f6">Sign in →</a></p></div>`; });
