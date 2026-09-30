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
  const btnType  = document.getElementById("btn-type");
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
  let lastCodec  = null;    // what the session negotiated, to rebuild the decoder with
  let forceJpeg  = false;   // H.264 kept failing on this computer: JPEG from here on
  let hiccups    = [];      // when the decoder had to be rebuilt (last 30 s)
  let backlogs   = [];      // when this computer fell behind decoding (last 20 s)
  // Decode requests queued beyond this (~quarter of a second at 30 fps) mean
  // this computer is not keeping up.
  const MAX_DECODE_QUEUE = 8;

  // Marks a clipboard payload on the (otherwise JPEG) binary stream.
  const CLIP_MAGIC = "LRMMCLIP";

  // Speed/quality presets sent to the agent via screen_start. max_edge caps the
  // captured frame's longest side: smaller = lighter, larger = crisper.
  // All three ask for 30 fps: the agent paces to it and steps down only if that
  // device or link genuinely can't hold it, so a preset is about how the picture
  // looks, not how smooth it is. (Agent v2.2.45+; older agents cap at 24.)
  const PRESETS = {
    balanced: { fps: 30, quality: 72, max_edge: 2400 },  // default — smooth + crisp
    sharp:    { fps: 30, quality: 88, max_edge: 2880 },  // best image quality
    smooth:   { fps: 30, quality: 60, max_edge: 1920 },  // lightest on a thin link
  };

  // ---- live stats (frames + bytes per second) ----
  // Counted over three seconds rather than one: a frame landing either side of a
  // one-second boundary moves a per-second count by a whole frame, so a perfectly
  // even 30 fps stream still reads 29/31/30. The three-second window shows the
  // rate the stream is actually holding.
  const STAT_WINDOW = 3;
  let frameCount = 0;
  let byteCount  = 0;
  const frameHist = [];
  const byteHist  = [];
  setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) { statsEl.textContent = "—"; return; }
    frameHist.push(frameCount); byteHist.push(byteCount);
    while (frameHist.length > STAT_WINDOW) { frameHist.shift(); byteHist.shift(); }
    const secs = frameHist.length;
    const fps  = frameHist.reduce((a, b) => a + b, 0) / secs;
    const bits = byteHist.reduce((a, b) => a + b, 0) * 8 / secs;
    const rate = bits >= 1e6 ? (bits / 1e6).toFixed(1) + " Mbps"
                             : Math.round(bits / 1e3) + " kbps";
    const res  = nativeW ? `${nativeW}×${nativeH}` : "—";
    statsEl.textContent = `${Math.round(fps)} fps · ${rate} · ${res}`;
    frameCount = 0;
    byteCount  = 0;
  }, 1000);

  // ---- diagnostics: where a picture gets stuck ----
  // What this viewer receives and draws goes to the server every ten seconds,
  // for the session log -- next to what the device sent and how long frames
  // waited for this link. A freeze is reported the moment it starts, with what
  // it looks like from here: nothing arriving (the device, or the way here),
  // or arriving but not drawn (this computer).
  const REPORT_EVERY = 10000;
  const FREEZE_AFTER = 2000;
  const diag = { since: performance.now(), recv: 0, recvBytes: 0, drawn: 0, keyWait: 0,
                 decodeQ: 0, gapRecv: 0, gapDraw: 0, freezes: 0, freezeMs: 0, errors: 0,
                 behind: 0, rtts: [], hidden: document.hidden, longMs: 0, buffered: 0,
                 lastRecv: 0, lastDraw: 0, frozenAt: 0, frozenCause: "" };
  let lastClose = null;         // how the previous socket ended, told on the next one

  function report(event, detail) {
    send(Object.assign({ type: "viewer_event", event }, detail || {}));
  }
  function noteRecv(bytes) {
    const now = performance.now();
    if (diag.lastRecv) diag.gapRecv = Math.max(diag.gapRecv, now - diag.lastRecv);
    diag.lastRecv = now; diag.recv++; diag.recvBytes += bytes;
  }
  function noteDraw() {
    const now = performance.now();
    if (diag.lastDraw) diag.gapDraw = Math.max(diag.gapDraw, now - diag.lastDraw);
    diag.lastDraw = now; diag.drawn++;
    if (diag.frozenAt) {
      const ms = Math.round(now - diag.frozenAt);
      diag.freezeMs = Math.max(diag.freezeMs, ms);
      report("unfreeze", { after_ms: ms, cause: diag.frozenCause });
      logActivity(`Picture moving again after ${(ms / 1000).toFixed(1)} s`);
      diag.frozenAt = 0;
    }
  }
  function freezeCause(now) {
    if (document.hidden) return "this tab is in the background";
    const recvAgo = diag.lastRecv ? now - diag.lastRecv : Infinity;
    if (recvAgo > 1500) return `nothing arriving from the server (last frame ${Math.round(recvAgo)} ms ago)`;
    if (decoder) {
      if (!sawKeyframe) return `frames arriving, waiting for a keyframe (${diag.keyWait} set aside)`;
      return `frames arriving but not decoded (decoder ${decoder.state}, queue ${decoder.decodeQueueSize})`;
    }
    return "frames arriving but not drawn (JPEG)";
  }
  setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN || !diag.lastDraw || diag.frozenAt) return;
    const now = performance.now();
    if (now - diag.lastDraw < FREEZE_AFTER) return;
    diag.frozenAt = diag.lastDraw;
    diag.freezes++;
    diag.frozenCause = freezeCause(now);
    report("freeze", { cause: diag.frozenCause, draw_ago: Math.round(now - diag.lastDraw),
                       recv_ago: diag.lastRecv ? Math.round(now - diag.lastRecv) : -1,
                       decode_q: decoder ? decoder.decodeQueueSize : 0, hidden: document.hidden,
                       codec: decoder ? "h264" : "jpeg", quality: selQual.value });
    console.warn(`[remote] picture stopped: ${diag.frozenCause}`);
    logActivity(`Picture stopped: ${diag.frozenCause}`);
  }, 250);
  setInterval(() => {
    const now = performance.now();
    if (ws && ws.readyState === WebSocket.OPEN) {
      const rtts = diag.rtts;
      const stats = {
        secs: (now - diag.since) / 1000, recv: diag.recv, recv_kb: diag.recvBytes / 1024,
        drawn: diag.drawn, key_wait: diag.keyWait, decode_q: diag.decodeQ,
        gap_recv: Math.round(diag.gapRecv), gap_draw: Math.round(diag.gapDraw),
        freezes: diag.freezes, freeze_ms: diag.freezeMs, errors: diag.errors, behind: diag.behind,
        rtt_avg: rtts.length ? Math.round(rtts.reduce((a, b) => a + b, 0) / rtts.length) : -1,
        rtt_max: rtts.length ? Math.round(Math.max(...rtts)) : -1,
        hidden: diag.hidden || document.hidden, long_ms: Math.round(diag.longMs),
        buffered: diag.buffered, codec: decoder ? "h264" : "jpeg", quality: selQual.value,
        size: nativeW ? `${nativeW}x${nativeH}` : "-",
      };
      send(Object.assign({ type: "viewer_stats" }, stats));
      console.info("[remote] last 10 s", stats);
    }
    Object.assign(diag, { since: now, recv: 0, recvBytes: 0, drawn: 0, keyWait: 0, decodeQ: 0,
                          gapRecv: 0, gapDraw: 0, freezes: 0, freezeMs: 0, errors: 0, behind: 0,
                          rtts: [], hidden: document.hidden, longMs: 0, buffered: 0 });
  }, REPORT_EVERY);
  // A ping answered through the server's queue for this viewer, behind the
  // frames: the round trip is how far behind the picture runs.
  setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    diag.buffered = Math.max(diag.buffered, ws.bufferedAmount);
    send({ type: "viewer_ping", t: performance.now() });
  }, 2000);
  // Time the browser's main thread was too busy to do anything else.
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) diag.longMs += e.duration;
    }).observe({ type: "longtask", buffered: false });
  } catch (e) { /* not measured in this browser */ }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) diag.hidden = true;
    report(document.hidden ? "hidden" : "visible", {});
  });

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
           codecs: H264_SUPPORTED && !forceJpeg ? ["h264", "jpeg"] : ["jpeg"] });
  }

  // ---- H.264 decode (WebCodecs) ----
  function closeDecoder() {
    if (decoder) { try { decoder.close(); } catch (e) {} decoder = null; }
    sawKeyframe = false; vpts = 0;
    setTxt("rc-s-proto", "WebSocket · JPEG");
  }
  function setupDecoder(codecString) {
    if (codecString) lastCodec = codecString;
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
            noteDraw();
            setStatus("ok", "Connected");
          } finally { frame.close(); }
        },
        error: (e) => recoverDecoder("decoder error", e && e.message),
      });
      decoder.configure({ codec: lastCodec || "avc1.42E01F", optimizeForLatency: true });
      setTxt("rc-s-proto", "WebSocket · H.264");
    } catch (e) {
      report("decoder_error", { why: "could not set up H.264", detail: String(e && e.message || e) });
      decoder = null;   // stay in JPEG mode
      setTxt("rc-s-proto", "WebSocket · JPEG");
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
    // Falling behind: this computer decodes slower than frames arrive, and the
    // picture would lag further and further. Skip to the next keyframe (the
    // agent sends one every ~2 s) so it catches up instead.
    // (Counted once per time it falls behind, not once per skipped frame.)
    if (!key && sawKeyframe && decoder.decodeQueueSize > MAX_DECODE_QUEUE) { sawKeyframe = false; fellBehind(); }
    if (!sawKeyframe) {                                           // await a keyframe
      if (!key) { diag.keyWait++; return; }
      sawKeyframe = true;
    }
    try {
      decoder.decode(new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: vpts, data: u8 }));
      vpts += 33333;  // ~30fps in µs; only needs to be monotonic
      diag.decodeQ = Math.max(diag.decodeQ, decoder.decodeQueueSize);
    } catch (e) {
      recoverDecoder("decode failed", e && e.message);
    }
  }

  // One bad chunk used to freeze the picture until Reconnect: the decoder was
  // closed and never made again, while frames kept arriving. Now it is rebuilt
  // and picks up at the next keyframe, within about two seconds. If it keeps
  // happening, H.264 is given up for this session and JPEG asked for instead,
  // which decodes every frame on its own.
  function recoverDecoder(why, detail) {
    const now = Date.now();
    hiccups = hiccups.filter((t) => now - t < 30000);
    hiccups.push(now);
    diag.errors++;
    report("decoder_error", { why, detail: `${detail || "-"}; ${hiccups.length} in 30 s` });
    console.warn(`[remote] video ${why}; rebuilding the decoder (${hiccups.length} in 30 s)`);
    if (hiccups.length > 4) {
      forceJpeg = true;
      closeDecoder();
      report("switched", { why: "H.264 kept failing to decode here", detail: "jpeg" });
      logActivity("Video kept failing to decode here — switched to JPEG");
      startCapture();
      return;
    }
    logActivity(`Video ${why} — recovering at the next keyframe`);
    setupDecoder(lastCodec);
  }

  // Falling behind now and then is a busy moment; falling behind again and
  // again means this computer cannot decode this much. Ask for the lighter
  // stream rather than skipping frames for the rest of the session.
  function fellBehind(link) {
    const now = Date.now();
    backlogs = backlogs.filter((t) => now - t < 20000);
    backlogs.push(now);
    if (link) diag.behind++;
    else report("fell_behind", { why: "decoding slower than frames arrive",
                                 decode_q: decoder ? decoder.decodeQueueSize : 0 });
    if (backlogs.length >= 3 && selQual.value !== "smooth") {
      backlogs = [];
      report("switched", { why: link ? "the link could not keep up" : "this computer could not keep up",
                           detail: "smooth" });
      selQual.value = "smooth";
      logActivity(link ? "The connection could not keep up — switched to Smooth"
                       : "This computer could not keep up — switched to Smooth");
      startCapture();
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

  // ---- connect (auto-reconnects on transient browser-side ws drops) ----
  let reconnectTimer = null;
  let reconnectAttempts = 0;
  let userClosed = false;       // true once the user deliberately disconnects
  const MAX_RECONNECT = 8;      // ~30s of trying before we wait for a manual click

  function scheduleReconnect() {
    if (userClosed || reconnectTimer) return;
    if (reconnectAttempts >= MAX_RECONNECT) {
      setStatus("bad", "Disconnected — click Reconnect");
      return;
    }
    reconnectAttempts++;
    const delay = Math.min(400 * Math.pow(1.7, reconnectAttempts - 1), 5000);
    setStatus("connecting", `Reconnecting… (${reconnectAttempts})`);
    reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, delay);
  }

  function connect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (ws) { ws.onclose = null; ws.close(); ws = null; }
    closeDecoder();
    setStatus("connecting", reconnectAttempts ? "Reconnecting…" : "Connecting…");
    frameCount = 0; byteCount = 0;
    diag.lastRecv = 0; diag.lastDraw = 0; diag.frozenAt = 0;

    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/api/devices/${deviceId}/screen`);
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      reconnectAttempts = 0;      // recovered — reset the backoff
      // How the previous socket ended can only be told on this one.
      if (lastClose) { report("closed", lastClose); lastClose = null; }
      startCapture();
      setStatus("connecting", "Starting capture…");
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        // JSON control message (codec negotiation / error).
        try {
          const m = JSON.parse(ev.data);
          if (m.type === "video_info" && m.codec === "h264") { setupDecoder(m.codecString); return; }
          // The server had to skip frames: this link could not take them all.
          if (m.type === "behind") { fellBehind(true); return; }
          if (m.type === "viewer_pong") {
            if (typeof m.t === "number") diag.rtts.push(performance.now() - m.t);
            return;
          }
          if (m.error) setStatus("bad", m.error);
        } catch {}
        return;
      }
      // Clipboard text coming back from the remote?
      if (isClipBlob(ev.data)) {
        const text = new TextDecoder("utf-8").decode(new Uint8Array(ev.data, CLIP_MAGIC.length));
        if (!text) { flash(btnCopy, "Nothing copied"); return; }
        toLocalClipboard(text);
        return;
      }
      byteCount += ev.data.byteLength;
      noteRecv(ev.data.byteLength);
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
        noteDraw();
        setStatus("ok", "Connected");
      };
      img.onerror = () => URL.revokeObjectURL(url);
      img.src = url;
    };

    ws.onclose = (ev) => {
      ws = null;
      // Log the close code so the root cause of transient drops is diagnosable
      // (1006 = abnormal/proxy or network kill, 1001 = going away, 1011 = server).
      if (!userClosed) console.warn(`[remote] screen ws closed: code=${ev.code} reason="${ev.reason||""}" clean=${ev.wasClean}`);
      if (!userClosed) lastClose = { code: ev.code, why: ev.reason || "-",
                                     detail: `clean=${ev.wasClean}${diag.frozenAt ? ", picture was frozen" : ""}` };
      if (userClosed) { setStatus("bad", "Disconnected"); return; }
      scheduleReconnect();        // transient drop — self-heal
    };

    ws.onerror = () => {
      // onerror is always followed by onclose; let onclose drive the reconnect.
      if (!userClosed) setStatus("connecting", "Connection lost — reconnecting…");
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

  // Ctrl+C / Ctrl+V are made to mean what people expect them to mean. Sent
  // through as plain hotkeys they act only on the *remote* machine's own
  // clipboard, so text could cross between the two computers only via the
  // toolbar buttons. Instead:
  //   Ctrl+V  -> the browser's own paste event hands us this computer's
  //              clipboard, which we ship over as `clip_paste` (the agent sets
  //              the remote clipboard and presses Ctrl+V there). Reading it
  //              this way needs no clipboard permission prompt.
  //   Ctrl+C  -> the hotkey still goes over so the remote copies, and a moment
  //     /X       later we pull the result back into this computer's clipboard.
  // `after_copy` has the agent wait until the copy has actually landed on the
  // remote clipboard (an older agent ignores it, and reads after the delay).
  let clipPull = null;
  function pullRemoteClipboard(delayMs) {
    clearTimeout(clipPull);
    clipPull = setTimeout(() => send({ kind: "clip_get", after_copy: true }), delayMs);
  }

  // Putting text on this computer's clipboard. Chrome allows it straight away;
  // Firefox and Safari only within a click or key press, and nobody over plain
  // http -- and by the time the remote's text arrives, the Ctrl+C is long past.
  // Then a button offers it: pressing that is the click the browser wants, and
  // if even that is refused the text is selected, ready for Ctrl+C.
  async function toLocalClipboard(text) {
    try {
      if (!navigator.clipboard || !window.isSecureContext) throw new Error("no clipboard access");
      await navigator.clipboard.writeText(text);
      hideCopyOffer();
      flash(btnCopy, "Copied ✓");
    } catch {
      offerCopy(text);
    }
  }

  function hideCopyOffer() {
    const box = document.getElementById("copy-offer");
    if (box) box.remove();
  }

  function offerCopy(text) {
    hideCopyOffer();
    const box = document.createElement("div");
    box.id = "copy-offer";
    box.className = "copy-offer";
    box.innerHTML = `<div class="co-head"><b>Copied on the remote</b>
        <span>Your browser needs a click to put it on this computer's clipboard.</span>
        <button class="co-close" title="Close">×</button></div>
      <textarea readonly spellcheck="false"></textarea>
      <div class="co-foot"><button class="btn sm" id="co-copy">Copy to my clipboard</button></div>`;
    box.querySelector("textarea").value = text;
    document.body.appendChild(box);
    const done = () => { hideCopyOffer(); canvas.focus(); flash(btnCopy, "Copied ✓"); };
    box.querySelector(".co-close").onclick = () => { hideCopyOffer(); canvas.focus(); };
    box.querySelector("#co-copy").onclick = async () => {
      const area = box.querySelector("textarea");
      try {
        if (!navigator.clipboard || !window.isSecureContext) throw new Error("no clipboard access");
        await navigator.clipboard.writeText(text);
        done();
      } catch {
        // No clipboard API at all (plain http): the old way still works
        // inside a click.
        area.focus();
        area.select();
        let copied = false;
        try { copied = document.execCommand("copy"); } catch { copied = false; }
        if (copied) { done(); return; }
        box.querySelector("#co-copy").textContent = "Selected — press Ctrl+C";
      }
    };
  }

  // "Paste as keystrokes": type the text out instead of pasting it. Plenty of
  // places refuse a paste outright -- UAC prompts, the Windows sign-in screen, a
  // remote session inside the remote session, password boxes that block it --
  // and typed characters are indistinguishable from someone at the keyboard.
  // Chunked so one long message can't stall the input stream, and capped
  // because typing is far slower than pasting.
  const KEYSTROKE_CHUNK = 200;
  const KEYSTROKE_MAX = 10000;
  let typeNextPaste = false;      // set by Ctrl+Shift+V, read by the paste event

  function sendAsKeystrokes(text) {
    // The remote presses Enter for a newline; normalise so a Windows clipboard's
    // CRLF doesn't type it twice.
    let out = String(text || "").replace(/\r\n?/g, "\n");
    const clipped = out.length > KEYSTROKE_MAX;
    out = out.slice(0, KEYSTROKE_MAX);
    if (!out) { flash(btnType, "Clipboard empty"); return; }
    for (let i = 0; i < out.length; i += KEYSTROKE_CHUNK) {
      send({ kind: "key", text: out.slice(i, i + KEYSTROKE_CHUNK) });
    }
    flash(btnType, clipped ? `Typed first ${KEYSTROKE_MAX} chars` : "Typed \u2713");
  }

  // The paste event fires on the focused element and bubbles, so listening on
  // the document catches it and the activeElement check keeps it scoped to the
  // session (not, say, a toolbar input).
  document.addEventListener("paste", (ev) => {
    if (document.activeElement !== canvas) return;
    ev.preventDefault();
    const cd = ev.clipboardData || window.clipboardData;
    const text = cd ? cd.getData("text") : "";
    const asKeystrokes = typeNextPaste;
    typeNextPaste = false;
    if (!text) return;
    if (asKeystrokes) { sendAsKeystrokes(text); return; }
    send({ kind: "clip_paste", text });
    flash(btnClip, "Pasted ✓");
  });

  canvas.addEventListener("keydown", (ev) => {
    const k = ev.key;
    const clipMod = (ev.ctrlKey || ev.metaKey) && !ev.altKey;
    const lower = k.length === 1 ? k.toLowerCase() : k;
    // Let Ctrl+V through untouched so the paste event above can fire. Shift
    // asks for the typed variant -- browsers fire `paste` for that chord too,
    // so it rides the same permission-free path.
    if (clipMod && lower === "v") { typeNextPaste = ev.shiftKey; return; }
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
      // Then mirror the remote clipboard here. The agent waits for the copy to
      // land; the delay is for older agents that read straight away.
      if (clipMod && (lower === "c" || lower === "x")) pullRemoteClipboard(300);
      return;
    }
    // Named non-printable key (Enter, Backspace, arrows, …).
    const named = KEYMAP[k];
    if (named) { ev.preventDefault(); send({ kind: "hotkey", keys: [named] }); return; }
    // Printable character (ev.key already reflects Shift, so capitals work).
    if (k.length === 1) { ev.preventDefault(); send({ kind: "key", text: k }); }
  });

  // ---- toolbar buttons ----
  document.getElementById("btn-reconnect").onclick = () => {
    // Said before the socket goes: someone pressing Reconnect is usually the
    // clearest sign that the picture had stopped.
    report("reconnect", { why: "Reconnect button",
                          detail: diag.frozenAt ? `picture frozen: ${diag.frozenCause}` : "picture was moving" });
    userClosed = false; reconnectAttempts = 0; connect();
  };

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
    userClosed = true;
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (ws) { ws.onclose = null; ws.close(); ws = null; }
    setStatus("bad", "Disconnected");
    setTimeout(() => { if (window.opener) window.close(); else location.href = "/"; }, 250);
  };

  // Copy: pull the remote clipboard to this computer.
  btnCopy.onclick = () => { send({ kind: "clip_get", report_empty: true }); };

  // Paste as keystrokes: the button has to read the clipboard itself (there is
  // no paste event to ride), which is the one path that may prompt for
  // permission -- the Ctrl+Shift+V chord above avoids that.
  btnType.onclick = async () => {
    try {
      sendAsKeystrokes(await navigator.clipboard.readText());
    } catch {
      flash(btnType, "Clipboard blocked");
    }
  };

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
