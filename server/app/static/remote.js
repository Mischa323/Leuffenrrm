/* Remote desktop viewer for Leuffen RMM
   Served at /remote/{device_id} — standalone page, opens in a new tab. */
(function () {
  "use strict";

  const deviceId = location.pathname.split("/").filter(Boolean).pop();
  const canvas   = document.getElementById("remote-canvas");
  const ctx      = canvas.getContext("2d");
  const overlay  = document.getElementById("overlay");
  const statusTx = document.getElementById("status-text");
  const connDot  = document.getElementById("conn-dot");
  const connLbl  = document.getElementById("conn-label");
  const devTitle = document.getElementById("dev-title");
  const statsEl  = document.getElementById("stats");
  const selQual  = document.getElementById("sel-quality");
  const btnSize  = document.getElementById("btn-size");
  const btnLock  = document.getElementById("btn-lock");

  let ws         = null;
  let nativeW    = 0;
  let nativeH    = 0;

  // Speed/quality presets sent to the agent via screen_start.
  const PRESETS = {
    balanced: { fps: 12, quality: 60 },
    smooth:   { fps: 18, quality: 45 },
    sharp:    { fps: 8,  quality: 80 },
  };

  // ---- live stats (frames + bytes per second) ----
  let frameCount = 0;
  let byteCount  = 0;
  setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) { statsEl.textContent = "—"; return; }
    const bits = byteCount * 8;
    const rate = bits >= 1e6 ? (bits / 1e6).toFixed(1) + " Mbps"
                             : Math.round(bits / 1e3) + " kbps";
    const res  = nativeW ? `${nativeW}×${nativeH}` : "—";
    statsEl.textContent = `${frameCount} fps · ${rate} · ${res}`;
    frameCount = 0;
    byteCount  = 0;
  }, 1000);

  // ---- connection status helpers ----
  function setStatus(state, msg) {
    statusTx.textContent = msg;
    connLbl.textContent  = msg;
    connDot.className    = state === "ok" ? "ok" : state === "bad" ? "bad" : "";
    overlay.classList.toggle("hidden", state === "ok");
    canvas.style.display = state === "ok" ? "block" : "none";
  }

  // ---- send helper ----
  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  function startCapture() {
    const p = PRESETS[selQual.value] || PRESETS.balanced;
    send({ type: "screen_start", fps: p.fps, quality: p.quality });
  }

  // ---- coordinate scaling (display -> native image pixels) ----
  function scale(ev) {
    const r = canvas.getBoundingClientRect();
    return {
      x: Math.round((ev.clientX - r.left) / r.width  * (nativeW || canvas.width)),
      y: Math.round((ev.clientY - r.top)  / r.height * (nativeH || canvas.height)),
    };
  }

  function btnName(b) {
    return b === 2 ? "right" : b === 1 ? "middle" : "left";
  }

  // ---- connect ----
  function connect() {
    if (ws) { ws.onclose = null; ws.close(); ws = null; }
    setStatus("connecting", "Connecting…");
    frameCount = 0; byteCount = 0;

    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/api/devices/${deviceId}/screen`);
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      startCapture();
      setStatus("connecting", "Starting capture…");
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        // JSON control message (error or info)
        try {
          const m = JSON.parse(ev.data);
          if (m.error) setStatus("bad", m.error);
        } catch {}
        return;
      }
      // Binary: JPEG frame
      byteCount += ev.data.byteLength;
      const blob = new Blob([ev.data], { type: "image/jpeg" });
      const url  = URL.createObjectURL(blob);
      const img  = new Image();
      img.onload = () => {
        if (img.naturalWidth !== nativeW || img.naturalHeight !== nativeH) {
          nativeW = canvas.width  = img.naturalWidth;
          nativeH = canvas.height = img.naturalHeight;
        }
        ctx.drawImage(img, 0, 0);
        URL.revokeObjectURL(url);
        frameCount++;
        setStatus("ok", "Connected");
      };
      img.onerror = () => URL.revokeObjectURL(url);
      img.src = url;
    };

    ws.onclose = () => {
      ws = null;
      setStatus("bad", "Disconnected");
    };

    ws.onerror = () => {
      setStatus("bad", "Connection error");
    };
  }

  // ---- mouse input: separate down/up so windows can be dragged ----
  let dragging = false;

  canvas.addEventListener("mousemove", (ev) => {
    ev.preventDefault();
    const { x, y } = scale(ev);
    send({ kind: "move", x, y });
  });

  canvas.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    canvas.focus();
    dragging = true;
    const { x, y } = scale(ev);
    send({ kind: "down", x, y, button: btnName(ev.button) });
  });

  // Release on window (not just canvas) so a drag that ends off-canvas still
  // sends the button-up.
  window.addEventListener("mouseup", (ev) => {
    if (!dragging) return;
    dragging = false;
    const { x, y } = scale(ev);
    send({ kind: "up", x, y, button: btnName(ev.button) });
  });

  canvas.addEventListener("contextmenu", (ev) => ev.preventDefault());

  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    send({ kind: "scroll", dy: ev.deltaY > 0 ? -1 : 1 });
  }, { passive: false });

  // ---- keyboard: printable text + named keys + modifier combos ----
  const KEYMAP = {
    Enter: "enter", Backspace: "backspace", Tab: "tab", Escape: "esc",
    Delete: "delete", Insert: "insert",
    ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
    Home: "home", End: "end", PageUp: "page_up", PageDown: "page_down",
    F1: "f1", F2: "f2", F3: "f3", F4: "f4", F5: "f5", F6: "f6",
    F7: "f7", F8: "f8", F9: "f9", F10: "f10", F11: "f11", F12: "f12",
  };

  canvas.addEventListener("keydown", (ev) => {
    const k = ev.key;
    // Ctrl/Alt/Meta combinations -> hotkey (e.g. Ctrl+C, Alt+Tab, Win+R).
    if (ev.ctrlKey || ev.altKey || ev.metaKey) {
      const base = KEYMAP[k] || (k.length === 1 ? k.toLowerCase() : null);
      if (!base) return;  // a lone modifier; wait for the real key
      const keys = [];
      if (ev.ctrlKey) keys.push("ctrl");
      if (ev.altKey)  keys.push("alt");
      if (ev.metaKey) keys.push("cmd");
      if (ev.shiftKey) keys.push("shift");
      keys.push(base);
      ev.preventDefault();
      send({ kind: "hotkey", keys });
      return;
    }
    // Named non-printable key (Enter, Backspace, arrows, …).
    const named = KEYMAP[k];
    if (named) { ev.preventDefault(); send({ kind: "hotkey", keys: [named] }); return; }
    // Printable character (ev.key already reflects Shift, so capitals work).
    if (k.length === 1) { ev.preventDefault(); send({ kind: "key", text: k }); }
  });

  // ---- toolbar buttons ----
  document.getElementById("btn-reconnect").onclick = connect;

  selQual.onchange = () => { if (ws && ws.readyState === WebSocket.OPEN) startCapture(); };

  btnSize.onclick = () => {
    const vp = document.getElementById("viewport");
    const actual = vp.classList.toggle("actual");
    btnSize.textContent = actual ? "Fit to window" : "Actual size";
  };

  btnLock.onclick = async () => {
    const orig = btnLock.textContent;
    try {
      await fetch(`/api/devices/${deviceId}/power`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "lock" }),
      });
      btnLock.textContent = "Locked ✓";
    } catch {
      btnLock.textContent = "Failed";
    }
    setTimeout(() => { btnLock.textContent = orig; }, 1500);
  };

  document.getElementById("btn-fs").onclick = () => {
    const el = document.getElementById("viewport");
    if (document.fullscreenElement) document.exitFullscreen();
    else el.requestFullscreen().catch(() => {});
  };

  document.getElementById("btn-cad").onclick = () => {
    send({ kind: "hotkey", keys: ["ctrl", "alt", "delete"] });
  };

  document.getElementById("btn-clip").onclick = async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) send({ kind: "key", text });
    } catch {
      // Clipboard permission denied — silently ignore.
    }
  };

  // ---- fetch device name ----
  fetch(`/api/devices/${deviceId}`)
    .then((r) => r.ok ? r.json() : null)
    .then((d) => {
      if (d && d.hostname) {
        devTitle.textContent = `Remote — ${d.hostname}`;
        document.title = `Remote — ${d.hostname} · Leuffen RMM`;
      }
    })
    .catch(() => {});

  // ---- start ----
  setStatus("connecting", "Connecting…");
  connect();
})();
