"use strict";
// Leuffen RMM dashboard — dependency-free vanilla JS.

const state = { me: null, org: null, group: null, tab: "devices", device: null, refresh: null };

const $ = (id) => document.getElementById(id);
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.remove("hidden");
  setTimeout(() => t.classList.add("hidden"), 2500);
}
function bar(pct, label) {
  if (pct == null) return '<span class="muted">—</span>';
  const cls = pct >= 90 ? "crit" : pct >= 75 ? "warn" : "";
  return `<div class="bar ${cls}"><i style="width:${pct}%"></i><span>${label ?? Math.round(pct) + "%"}</span></div>`;
}
const dot = (on) => `<span class="dot ${on ? "online" : "offline"}"></span>`;

// ---- bootstrap ------------------------------------------------------------ //
async function init() {
  state.me = await api("/api/me");
  $("user").textContent = state.me.email;
  $("home-link").onclick = (e) => { e.preventDefault(); showGlobal(); };
  $("drawer-close").onclick = closeDrawer;
  setupTabs();
  setupDrawerTabs();
  setupTerminalForm();
  showGlobal();
}

// ---- global view ---------------------------------------------------------- //
async function showGlobal() {
  clearRefresh();
  state.org = null;
  $("global-view").classList.remove("hidden");
  $("org-view").classList.add("hidden");
  $("org-select").classList.add("hidden");
  $("home-link").textContent = "Global";
  const data = await api("/api/overview");
  const cards = $("org-cards");
  cards.innerHTML = "";
  for (const o of data.orgs) {
    const el = document.createElement("div");
    el.className = "card";
    el.innerHTML = `<h3>${o.name}</h3>
      <div class="stat"><span>Devices</span><b>${o.devices}</b></div>
      <div class="stat"><span>Online</span><b style="color:var(--green)">${o.online}</b></div>
      <div class="stat"><span>Offline</span><b style="color:var(--muted)">${o.offline}</b></div>
      <div class="stat"><span>Non-compliant</span><b style="color:${o.noncompliant ? 'var(--red)' : 'var(--muted)'}">${o.noncompliant}</b></div>`;
    el.onclick = () => showOrg(o.id, o.name);
    cards.appendChild(el);
  }
  if (!data.orgs.length) cards.innerHTML = '<p class="muted">No organisations yet.</p>';
}

// ---- org view ------------------------------------------------------------- //
async function showOrg(orgId, name) {
  state.org = orgId; state.group = null;
  $("global-view").classList.add("hidden");
  $("org-view").classList.remove("hidden");
  $("home-link").textContent = `Global / ${name}`;
  // org selector for quick switching
  const sel = $("org-select"); sel.classList.remove("hidden"); sel.innerHTML = "";
  for (const o of state.me.orgs) {
    const opt = document.createElement("option");
    opt.value = o.id; opt.textContent = o.name; opt.selected = o.id === orgId;
    sel.appendChild(opt);
  }
  sel.onchange = () => showOrg(sel.value, sel.options[sel.selectedIndex].text);
  await loadGroups();
  selectTab(state.tab);
}

async function loadGroups() {
  const groups = await api(`/api/orgs/${state.org}/groups`);
  const gf = $("group-filter");
  gf.innerHTML = "";
  const mk = (id, label) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = (state.group === id || (!state.group && id === null)) ? "active" : "";
    b.onclick = () => { state.group = id; loadGroups(); if (state.tab === "devices") loadDevices(); };
    gf.appendChild(b);
  };
  mk(null, "All devices");
  for (const g of groups) mk(g.id, g.name);
}

function setupTabs() {
  for (const b of document.querySelectorAll("#org-tabs button")) {
    b.onclick = () => selectTab(b.dataset.tab);
  }
}
function selectTab(tab) {
  state.tab = tab;
  for (const b of document.querySelectorAll("#org-tabs button"))
    b.classList.toggle("active", b.dataset.tab === tab);
  for (const t of ["devices", "network", "nodes", "downloads"])
    $("tab-" + t).classList.toggle("hidden", t !== tab);
  clearRefresh();
  if (tab === "devices") { loadDevices(); state.refresh = setInterval(loadDevices, 5000); }
  else if (tab === "network") loadNetwork();
  else if (tab === "nodes") loadNodes();
  else if (tab === "downloads") loadDownloads();
}

async function loadDevices() {
  const q = state.group ? `?group_id=${state.group}` : "";
  const devs = await api(`/api/orgs/${state.org}/devices${q}`);
  const rows = $("device-rows");
  rows.innerHTML = "";
  for (const d of devs) {
    const m = d.latest || {};
    const comp = d.compliant == null ? '<span class="badge na">n/a</span>'
      : d.compliant ? '<span class="badge ok">compliant</span>'
      : '<span class="badge bad">non-compliant</span>';
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${dot(d.online)}</td><td>${d.hostname}</td>
      <td>${d.os || "—"}</td><td>${d.ip || "—"}</td>
      <td>${bar(m.cpu_percent)}</td><td>${bar(m.mem_percent)}</td>
      <td>${bar(m.disk_percent)}</td><td>${comp}</td>`;
    tr.onclick = () => openDrawer(d.id);
    rows.appendChild(tr);
  }
  if (!devs.length) rows.innerHTML = '<tr><td colspan="8" class="muted" style="padding:30px;text-align:center">No devices. Install an agent from the Downloads tab.</td></tr>';
}

async function loadNetwork() {
  const hosts = await api(`/api/orgs/${state.org}/network/hosts`);
  const rows = $("network-rows");
  rows.innerHTML = "";
  for (const h of hosts) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${dot(h.online)}</td><td>${h.ip}</td><td>${h.mac || "—"}</td>
      <td>${h.hostname || "—"}</td><td>${h.manufacturer || "—"}</td>
      <td>${h.mac ? `<button data-mac="${h.mac}">Wake</button>` : ""}</td>`;
    const btn = tr.querySelector("button");
    if (btn) btn.onclick = async () => {
      try { const r = await api(`/api/orgs/${state.org}/network/wake`,
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mac: h.mac }) });
        toast(`Wake sent via ${r.via}`); } catch (e) { toast(e.message); }
    };
    rows.appendChild(tr);
  }
  if (!hosts.length) rows.innerHTML = '<tr><td colspan="6" class="muted" style="padding:30px;text-align:center">No hosts discovered. Promote a device to a node and run a scan.</td></tr>';
}

async function loadNodes() {
  const nodes = await api(`/api/orgs/${state.org}/nodes`);
  const list = $("node-list");
  list.innerHTML = nodes.length ? "" : '<p class="muted">No nodes yet. Open a device → Actions → Promote to node.</p>';
  for (const n of nodes) {
    const div = document.createElement("div");
    div.className = "card"; div.style.cursor = "default";
    const subs = n.subnets.map((s) => `${s.cidr}`).join(", ") || "none";
    div.innerHTML = `<h3>${dot(n.online)}${n.hostname}</h3>
      <div class="stat"><span>Subnets</span><span>${subs}</span></div>`;
    const scan = document.createElement("button");
    scan.textContent = "Scan now"; scan.style.marginTop = "10px";
    scan.onclick = async () => { try { await api(`/api/devices/${n.id}/scan`, { method: "POST" }); toast("Scan started"); } catch (e) { toast(e.message); } };
    div.appendChild(scan);
    list.appendChild(div);
  }
}

async function loadDownloads() {
  const base = location.origin;
  $("download-cmds").innerHTML = `
    <h4>Linux</h4>
    <pre>curl -fsSL ${base}/api/orgs/${state.org}/install.sh | sudo bash</pre>
    <h4>Windows (PowerShell, as admin)</h4>
    <pre>iwr ${base}/api/orgs/${state.org}/install.ps1 -UseBasicParsing | iex</pre>
    <h4>Manual</h4>
    <pre>Download: ${base}/api/orgs/${state.org}/agent.zip
Run: python agent.py  (config is bundled inside)</pre>`;
}

// ---- device drawer -------------------------------------------------------- //
let termSocket = null;
function setupDrawerTabs() {
  for (const b of document.querySelectorAll(".drawer-tabs button")) {
    b.onclick = () => selectDrawerTab(b.dataset.dtab);
  }
}
function selectDrawerTab(tab) {
  for (const b of document.querySelectorAll(".drawer-tabs button"))
    b.classList.toggle("active", b.dataset.dtab === tab);
  for (const t of ["overview", "terminal", "actions"])
    $("dtab-" + t).classList.toggle("hidden", t !== tab);
  if (tab === "terminal") openTerminal();
  else closeTerminal();
}

async function openDrawer(id) {
  state.device = id;
  $("drawer").classList.remove("hidden");
  selectDrawerTab("overview");
  const d = await api(`/api/devices/${id}`);
  $("drawer-title").innerHTML = `${dot(d.online)}${d.hostname}`;
  const inv = d.inventory || {};
  const rows = [
    ["OS", d.os], ["Version", d.os_version], ["Arch", d.os_arch], ["Kind", d.os_kind],
    ["Manufacturer", d.manufacturer], ["Model", d.model], ["Serial", d.serial],
    ["CPU", d.cpu], ["RAM", d.ram_total ? (d.ram_total / 1e9).toFixed(1) + " GB" : null],
    ["Primary IP", d.ip], ["Primary MAC", d.mac], ["Agent", d.agent_version],
  ];
  let html = "<dl>" + rows.filter((r) => r[1]).map((r) => `<dt>${r[0]}</dt><dd>${r[1]}</dd>`).join("");
  if (inv.nics) {
    html += "<dt>Interfaces</dt><dd>" + inv.nics.map((n) =>
      `${n.name}: ${(n.ipv4 || []).join(", ") || "—"} ${n.mac ? "(" + n.mac + ")" : ""}`).join("<br>") + "</dd>";
  }
  html += "</dl>";
  $("inventory").innerHTML = html;
  renderActions(d);
}

function renderActions(d) {
  const grid = $("actions-grid");
  grid.innerHTML = "";
  const mk = (label, cls, fn) => { const b = document.createElement("button"); b.textContent = label; if (cls) b.className = cls; b.onclick = fn; grid.appendChild(b); };
  mk("Wake (WoL)", "ghost", () => act(`/api/devices/${d.id}/wake`, {}));
  mk("Lock", "ghost", () => power(d.id, "lock"));
  mk("Log off", "ghost", () => power(d.id, "logoff"));
  mk("Reboot", "warn", () => confirm("Reboot " + d.hostname + "?") && power(d.id, "reboot"));
  mk("Shut down", "danger", () => confirm("Shut down " + d.hostname + "?") && power(d.id, "shutdown"));
  mk("Delete", "danger", async () => { if (confirm("Remove " + d.hostname + "?")) { await api(`/api/devices/${d.id}`, { method: "DELETE" }); closeDrawer(); loadDevices(); } });

  // node controls
  const nc = $("node-controls");
  nc.innerHTML = "";
  if (d.is_node) {
    const subs = document.createElement("div");
    api(`/api/devices/${d.id}`).then(() => {});
    nc.appendChild(subs);
    const form = document.createElement("div");
    form.innerHTML = `<input id="cidr-input" placeholder="192.168.1.0/24" />`;
    const add = document.createElement("button"); add.textContent = "Add subnet";
    add.onclick = async () => {
      const cidr = $("cidr-input").value.trim(); if (!cidr) return;
      try { await api(`/api/devices/${d.id}/subnets`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cidr }) }); toast("Subnet added"); } catch (e) { toast(e.message); }
    };
    form.appendChild(add); nc.appendChild(form);
    const demote = document.createElement("button"); demote.textContent = "Demote to plain agent"; demote.className = "ghost"; demote.style.marginTop = "10px";
    demote.onclick = async () => { await api(`/api/devices/${d.id}/demote`, { method: "POST" }); toast("Demoted"); openDrawer(d.id); };
    nc.appendChild(demote);
  } else {
    const promote = document.createElement("button"); promote.textContent = "Promote to network node";
    promote.onclick = async () => { await api(`/api/devices/${d.id}/promote`, { method: "POST" }); toast("Promoted to node"); openDrawer(d.id); };
    nc.appendChild(promote);
  }
}

async function act(path, body) {
  try { const r = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); toast(JSON.stringify(r.status || r)); } catch (e) { toast(e.message); }
}
async function power(id, action) {
  try { await api(`/api/devices/${id}/power`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }) }); toast(action + " sent"); } catch (e) { toast(e.message); }
}

function closeDrawer() { closeTerminal(); $("drawer").classList.add("hidden"); state.device = null; }

// ---- terminal ------------------------------------------------------------- //
function setupTerminalForm() {
  $("term-form").onsubmit = (e) => {
    e.preventDefault();
    const input = $("term-input");
    const cmd = input.value; if (!cmd) return;
    $("term-output").textContent += `$ ${cmd}\n`;
    if (termSocket && termSocket.readyState === 1) termSocket.send(JSON.stringify({ data: cmd }));
    input.value = "";
  };
}
function openTerminal() {
  if (termSocket || !state.device) return;
  $("term-output").textContent = "";
  const proto = location.protocol === "https:" ? "wss" : "ws";
  termSocket = new WebSocket(`${proto}://${location.host}/api/devices/${state.device}/terminal`);
  termSocket.onmessage = (ev) => {
    try { const m = JSON.parse(ev.data); if (m.data) $("term-output").textContent += m.data; if (m.error) $("term-output").textContent += "[" + m.error + "]\n"; } catch {}
    $("term-output").scrollTop = $("term-output").scrollHeight;
  };
  termSocket.onclose = () => { termSocket = null; };
}
function closeTerminal() { if (termSocket) { termSocket.close(); termSocket = null; } }

function clearRefresh() { if (state.refresh) { clearInterval(state.refresh); state.refresh = null; } }

init().catch((e) => { document.body.innerHTML = `<p style="padding:40px;color:#ef4444">Error: ${e.message}. <a href="/auth/login">Sign in</a></p>`; });
