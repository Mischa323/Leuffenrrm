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

  let ws         = null;
  let nativeW    = 0;
  let nativeH    = 0;

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

  // ---- coordinate scaling ----
  function scale(ev) {
    const r = canvas.getBoundingClientRect();
    return {
      x: Math.round((ev.clientX - r.left) / r.width  * (nativeW || canvas.width)),
      y: Math.round((ev.clientY - r.top)  / r.height * (nativeH || canvas.height)),
    };
  }

  // ---- connect ----
  function connect() {
    if (ws) { ws.onclose = null; ws.close(); ws = null; }
    setStatus("connecting", "Connecting…");

    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/api/devices/${deviceId}/screen`);
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      // Server auto-sends screen_start with fps=4; override with better defaults.
      send({ type: "screen_start", fps: 8, quality: 65 });
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

  // ---- canvas input events ----
  canvas.addEventListener("mousemove", (ev) => {
    ev.preventDefault();
    const { x, y } = scale(ev);
    send({ kind: "move", x, y });
  });

  canvas.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    canvas.focus();
    const { x, y } = scale(ev);
    send({ kind: "click", x, y, button: ev.button === 2 ? "right" : "left" });
  });

  canvas.addEventListener("contextmenu", (ev) => ev.preventDefault());

  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    send({ kind: "scroll", dy: ev.deltaY > 0 ? -1 : 1 });
  }, { passive: false });

  canvas.addEventListener("keydown", (ev) => {
    // Send printable characters only; let the browser handle meta keys.
    if (ev.key.length === 1 && !ev.ctrlKey && !ev.metaKey) {
      ev.preventDefault();
      send({ kind: "key", text: ev.key });
    }
  });

  // ---- toolbar buttons ----
  document.getElementById("btn-reconnect").onclick = connect;

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
