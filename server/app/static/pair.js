/* Approving a link with LeuffenDoc. The app sent the browser here with its
   address and a challenge; approving issues a single-use code that only that
   app can turn into an API key, and sends the browser back to it. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  try {
    const theme = localStorage.getItem("rmm-theme");
    if (theme) document.documentElement.dataset.theme = theme;
  } catch (e) { /* keep the default */ }
  if (window.ICON && ICON.shield) $("logo").innerHTML = ICON.shield;

  const q = new URLSearchParams(location.search);
  const doc = q.get("doc") || "";
  const challenge = q.get("challenge") || "";
  const state = q.get("state") || "";

  function fail(text) {
    $("body").innerHTML = `<h1>Can't link this</h1><p class="err">${esc(text)}</p>`;
  }

  async function load() {
    let info;
    try {
      const r = await fetch(`/api/pair/info?doc=${encodeURIComponent(doc)}`, { cache: "no-store" });
      if (r.status === 401) { location.href = `/auth/login?next=${encodeURIComponent(location.pathname + location.search)}`; return; }
      info = await r.json();
    } catch (e) { fail("The server did not answer. Try again in a moment."); return; }
    if (!info.target || !challenge || !state) { fail("This link request is incomplete. Start it again from LeuffenDoc."); return; }
    if (!info.may) { fail(`Only a global administrator can link an app. You are signed in as ${info.user}.`); return; }
    const replacing = info.current && info.current !== info.target
      ? `<div class="note">This replaces the current link with <b>${esc(info.current)}</b>.</div>` : "";
    $("body").innerHTML = `
      <h1>Link LeuffenDoc to this RMM?</h1>
      <div class="target">${esc(info.target)}</div>
      <ul>
        <li>people sign in there with their account here — 2FA and IP rules included</li>
        <li>it reads customers, accounts and devices, and keeps them in step</li>
        <li>it shows its documentation under <b>Docs</b> in the device drawer here</li>
      </ul>
      ${replacing}
      <div class="note">Only approve this if you just started it yourself, from that address.
        An API key is issued for it; you can revoke it any time under Settings → API &amp; webhooks.</div>
      <div class="row">
        <button class="btn ghost" id="no">Cancel</button>
        <button class="btn" id="yes">Approve</button>
      </div>`;
    $("no").onclick = () => { $("body").innerHTML = `<h1>Not linked</h1><p class="muted">Nothing was changed. You can close this page.</p>`; };
    $("yes").onclick = async () => {
      $("yes").disabled = true;
      try {
        const r = await fetch("/api/pair/approve", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ doc, challenge, state }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || `${r.status}`);
        $("body").innerHTML = `<h1>Linked</h1><p class="muted">Back to LeuffenDoc…</p>`;
        location.href = data.redirect;
      } catch (e) { $("yes").disabled = false; fail(e.message); }
    };
  }
  load();
})();
