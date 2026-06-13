"use strict";
/* ============================================================================
   Leuffen RMM — Appearance preferences, two tiers:
     • GLOBAL default  (set by admins in Settings → applies to everyone)
     • USER override   (set per person in Account → overrides the global default)
   Effective look = DEFAULTS ← global ← (user overrides, only if not "follow").
   Persisted in localStorage; applied before paint on every page.
   ========================================================================== */
(function () {
  const K_GLOBAL = "rmm_appearance_global";
  const K_USER = "rmm_appearance_user";
  const DEFAULTS = { theme: "dark", accent: "#3b82f6", density: "comfortable", roundness: 1, font: "Onest", dataviz: true };
  const FIELDS = ["theme", "accent", "density", "roundness", "font", "dataviz"];
  const FONT_STACK = {
    Onest: '"Onest", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    Inter: '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    System: '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif',
  };

  const rd = (k, fb) => { try { return JSON.parse(localStorage.getItem(k) || "null") || fb; } catch (e) { return fb; } };
  const wr = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} };

  function getGlobal() { return Object.assign({}, DEFAULTS, rd(K_GLOBAL, {})); }
  function getUser() { return Object.assign({ follow: true }, rd(K_USER, {})); }
  function effective() {
    const g = getGlobal(), u = getUser();
    if (u.follow !== false) return g;
    const out = Object.assign({}, g);
    FIELDS.forEach((f) => { if (u[f] !== undefined) out[f] = u[f]; });
    return out;
  }

  function apply(v) {
    v = v || effective();
    const root = document.documentElement;
    root.setAttribute("data-theme", v.theme);
    root.style.setProperty("--accent", v.accent);
    root.setAttribute("data-density", v.density);
    root.style.setProperty("--density", v.density === "compact" ? "0.82" : "1");
    root.style.setProperty("--radius-scale", String(v.roundness));
    root.setAttribute("data-dataviz", v.dataviz ? "on" : "off");
    root.style.setProperty("--font-sans", FONT_STACK[v.font] || FONT_STACK.Onest);
  }
  function announce() { window.dispatchEvent(new CustomEvent("appearance-change", { detail: effective() })); }

  const Appearance = {
    FONT_STACK,
    ACCENTS: ["#3b82f6", "#6366f1", "#8b5cf6", "#06b6d4", "#10b981", "#f59e0b", "#ef4444", "#ec4899"],
    FONTS: ["Onest", "Inter", "System"],
    FIELDS,
    DEFAULTS,
    effective,
    apply,
    // --- global (workspace default) ---
    getGlobal,
    setGlobal(key, val) { const g = getGlobal(); if (typeof key === "object") Object.assign(g, key); else g[key] = val; wr(K_GLOBAL, g); apply(); announce(); return g; },
    resetGlobal() { localStorage.removeItem(K_GLOBAL); apply(); announce(); return getGlobal(); },
    // --- user (personal override) ---
    getUser,
    setUser(key, val) { const u = getUser(); if (typeof key === "object") Object.assign(u, key); else u[key] = val; wr(K_USER, u); apply(); announce(); return u; },
    setFollow(follow) { const u = getUser(); u.follow = follow; if (follow) FIELDS.forEach((f) => delete u[f]); wr(K_USER, u); apply(); announce(); return u; },
    resetUser() { localStorage.removeItem(K_USER); apply(); announce(); return getUser(); },
  };

  apply();
  window.Appearance = Appearance;
})();
