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
  const btnCopy  = document.getElementById("btn-copy");
  const btnClip  = document.getElementById("btn-clip");
  // Chrome (session bar + side panel) — all optional, populated best-effort.
  const connPill = document.getElementById("rc-conn");
  const rcSub    = document.getElementById("rc-sub");
  const rcOs     = document.getElementById("rc-os");
  const rcLog    = document.getElementById("rc-log");

  const setTxt = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  function logActivity(msg) {
    if (!rcLog) return;
    if (rcLog.firstChild && rcLog.firstChild.dataset && rcLog.firstChild.dataset.msg === msg) return;
    const row = document.createElement("div");
    row.className = "rc-log-row"; row.dataset.msg = msg;
    const t = document.createElement("span"); t.className = "t"; t.textContent = new Date().toTimeString().slice(0, 8);
    const m = document.createElement("span"); m.textContent = msg;
    row.appendChild(t); row.appendChild(m);
    rcLog.insertBefore(row, rcLog.firstChild);
    while (rcLog.children.length > 40) rcLog.removeChild(rcLog.lastChild);
  }

  let ws         = null;
  let nativeW    = 0;
  let nativeH    = 0;

  // ---- H.264 (WebCodecs) — Phase 1. Negotiated per session; falls back to JPEG
  // when the browser has no VideoDecoder or the agent can't encode H.264. ----
  const H264_SUPPORTED = typeof VideoDecoder !== "undefined";
  let decoder    = null;   // WebCodecs VideoDecoder once a session negotiates H.264
  let sawKeyframe = false;  // ignore delta frames until the first keyframe arrives
  let vpts       = 0;       // monotonic timestamp for EncodedVideoChunk

  // Marks a clipboard payload on the (otherwise JPEG) binary stream.
  const CLIP_MAGIC = "LRMMCLIP";

  // Speed/quality presets sent to the agent via screen_start. max_edge caps the
  // captured frame's longest side: smaller = higher fps, larger = crisper. Tuned
  // up into the range the agent already allows (fps ≤ 24, quality ≤ 90,
  // max_edge ≤ 4096) — a much sharper baseline than before.
  const PRESETS = {
    balanced: { fps: 20, quality: 78, max_edge: 2880 },  // default — crisp + smooth
    sharp:    { fps: 15, quality: 90, max_edge: 4096 },  // best image, full resolution
    smooth:   { fps: 24, quality: 62, max_edge: 1920 },  // highest frame rate, 1080p
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

  // ---- small helpers ----
  function setStatus(state, msg) {
    statusTx.textContent = msg;
    connLbl.textContent  = msg;
    connDot.className    = "led";
    if (connPill) {
      connPill.classList.toggle("live", state === "ok");
      connPill.classList.toggle("bad", state === "bad");
    }
    overlay.classList.toggle("hidden", state === "ok");
    canvas.style.display = state === "ok" ? "block" : "none";
    if (state === "ok") { const s = document.getElementById("rc-s-started"); if (s && s.textContent === "—") s.textContent = new Date().toTimeString().slice(0, 5); }
    logActivity(msg);
  }

  // Update a button's label span (falls back to the button itself) so an icon
  // sitting alongside the label survives a transient flash.
  function flash(btn, label) {
    const lbl = btn.querySelector(".lbl") || btn;
    const orig = lbl.dataset.label || lbl.textContent;
    lbl.dataset.label = orig;
    lbl.textContent = label;
    setTimeout(() => { lbl.textContent = lbl.dataset.label; }, 1500);
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  function startCapture() {
    const p = PRESETS[selQual.value] || PRESETS.balanced;
    send({ type: "screen_start", fps: p.fps, quality: p.quality, max_edge: p.max_edge,
           codecs: H264_SUPPORTED ? ["h264", "jpeg"] : ["jpeg"] });
  }

  // ---- H.264 decode (WebCodecs) ----
  function closeDecoder() {
    if (decoder) { try { decoder.close(); } catch (e) {} decoder = null; }
    sawKeyframe = false; vpts = 0;
  }
  function setupDecoder(codecString) {
    closeDecoder();
    try {
      decoder = new VideoDecoder({
        output: (frame) => {
          try {
            if (frame.displayWidth !== nativeW || frame.displayHeight !== nativeH) {
              nativeW = canvas.width = frame.displayWidth;
              nativeH = canvas.height = frame.displayHeight;
            }
            ctx.drawImage(frame, 0, 0);
            frameCount++;
            setStatus("ok", "Connected");
          } finally { frame.close(); }
        },
        error: () => { setStatus("bad", "Video decoder error"); closeDecoder(); },
      });
      decoder.configure({ codec: codecString || "avc1.42E01F", optimizeForLatency: true });
    } catch (e) {
      decoder = null;   // stay in JPEG mode
    }
  }
  // A H.264 Annex-B access unit is a keyframe if it carries an IDR/SPS/PPS NAL
  // (types 5/7/8) — used to tag the EncodedVideoChunk 'key' vs 'delta'.
  function isKeyAU(u8) {
    for (let i = 0; i + 4 < u8.length; i++) {
      if (u8[i] === 0 && u8[i + 1] === 0 &&
          (u8[i + 2] === 1 || (u8[i + 2] === 0 && u8[i + 3] === 1))) {
        const t = u8[u8[i + 2] === 1 ? i + 3 : i + 4] & 0x1f;
        if (t === 5 || t === 7 || t === 8) return true;
      }
    }
    return false;
  }
  function decodeAU(buf) {
    if (!decoder || decoder.state !== "configured") return;
    const u8 = new Uint8Array(buf);
    const key = isKeyAU(u8);
    if (!sawKeyframe) { if (!key) return; sawKeyframe = true; }  // await first keyframe
    try {
      decoder.decode(new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: vpts, data: u8 }));
      vpts += 33333;  // ~30fps in µs; only needs to be monotonic
    } catch (e) {
      setStatus("bad", "Decode failed"); closeDecoder();
    }
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

  function isClipBlob(buf) {
    if (buf.byteLength < CLIP_MAGIC.length) return false;
    const h = new Uint8Array(buf, 0, CLIP_MAGIC.length);
    for (let i = 0; i < CLIP_MAGIC.length; i++) {
      if (h[i] !== CLIP_MAGIC.charCodeAt(i)) return false;
    }
    return true;
  }

  // ---- connect ----
  function connect() {
    if (ws) { ws.onclose = null; ws.close(); ws = null; }
    closeDecoder();
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
        // JSON control message (codec negotiation / error).
        try {
          const m = JSON.parse(ev.data);
          if (m.type === "video_info" && m.codec === "h264") { setupDecoder(m.codecString); return; }
          if (m.error) setStatus("bad", m.error);
        } catch {}
        return;
      }
      // Clipboard text coming back from the remote?
      if (isClipBlob(ev.data)) {
        const text = new TextDecoder("utf-8").decode(new Uint8Array(ev.data, CLIP_MAGIC.length));
        navigator.clipboard.writeText(text)
          .then(() => flash(btnCopy, "Copied ✓"))
          .catch(() => flash(btnCopy, "Copy blocked"));
        return;
      }
      byteCount += ev.data.byteLength;
      // H.264 mode: feed the access unit to the WebCodecs decoder.
      if (decoder) { decodeAU(ev.data); return; }
      // Binary: JPEG frame (fallback / no WebCodecs).
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
    const lbl = btnSize.querySelector(".lbl") || btnSize;
    lbl.textContent = actual ? "Fit to window" : "Actual size";
  };

  // Disconnect: close the session and return to the dashboard (or close the tab
  // if we were opened in one).
  const btnDisc = document.getElementById("rc-disconnect");
  if (btnDisc) btnDisc.onclick = () => {
    if (ws) { ws.onclose = null; ws.close(); ws = null; }
    setStatus("bad", "Disconnected");
    setTimeout(() => { if (window.opener) window.close(); else location.href = "/"; }, 250);
  };

  // Copy: pull the remote clipboard to this computer.
  btnCopy.onclick = () => { send({ kind: "clip_get" }); };

  // Paste: push this computer's clipboard into the remote.
  btnClip.onclick = async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) { send({ kind: "clip_paste", text }); flash(btnClip, "Pasted ✓"); }
    } catch {
      flash(btnClip, "Clipboard blocked");
    }
  };

  btnLock.onclick = async () => {
    try {
      await fetch(`/api/devices/${deviceId}/power`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "lock" }),
      });
      flash(btnLock, "Locked ✓");
    } catch {
      flash(btnLock, "Failed");
    }
  };

  document.getElementById("btn-fs").onclick = () => {
    const el = document.getElementById("viewport");
    if (document.fullscreenElement) document.exitFullscreen();
    else el.requestFullscreen().catch(() => {});
  };

  document.getElementById("btn-cad").onclick = () => {
    send({ kind: "hotkey", keys: ["ctrl", "alt", "delete"] });
  };

  // ---- fetch device identity (session bar + Session card) ----
  fetch(`/api/devices/${deviceId}`)
    .then((r) => r.ok ? r.json() : null)
    .then((d) => {
      if (!d) return;
      if (d.hostname) {
        devTitle.textContent = d.hostname;
        document.title = `Remote — ${d.hostname} · Leuffen RMM`;
      }
      if (rcSub) rcSub.textContent = [d.os, d.ip].filter(Boolean).join(" · ") || "—";
      if (rcOs && window.osIcon) rcOs.innerHTML = window.osIcon(d.os || "");
      setTxt("rc-s-device", d.hostname || "—");
      setTxt("rc-s-ip", d.ip || "—");
      setTxt("rc-s-os", d.os || "—");
    })
    .catch(() => {});

  // ---- start ----
  setStatus("connecting", "Connecting…");
  connect();
})();
