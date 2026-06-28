// Lucide-style inline SVG icons (stroke). Returns markup strings.
const I = (p, o = {}) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${o.w || 2}" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;

const ICON = {
  shield: I('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/>'),
  search: I('<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>'),
  bell: I('<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>'),
  grid: I('<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>'),
  monitor: I('<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8m-4-4v4"/>'),
  network: I('<rect x="9" y="2" width="6" height="6" rx="1"/><rect x="3" y="16" width="6" height="6" rx="1"/><rect x="15" y="16" width="6" height="6" rx="1"/><path d="M12 8v4m0 0H6v4m6-4h6v4"/>'),
  nodes: I('<circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/><path d="M10.5 7 6.5 16.7M13.5 7l4 9.7"/>'),
  download: I('<path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>'),
  link: I('<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>'),
  upload: I('<path d="M12 21V9m0 0 4 4m-4-4-4 4"/><path d="M4 7V5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v2"/>'),
  folder: I('<path d="M3 7a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.7.9l.8 1.2H19a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'),
  file: I('<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>'),
  chevR: I('<path d="m9 6 6 6-6 6"/>'),
  chevD: I('<path d="m6 9 6 6 6-6"/>'),
  building: I('<rect x="4" y="2" width="16" height="20" rx="2"/><path d="M9 22v-4h6v4M8 6h.01M12 6h.01M16 6h.01M8 10h.01M12 10h.01M16 10h.01M8 14h.01M12 14h.01M16 14h.01"/>'),
  cpu: I('<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v2m6-2v2M9 20v2m6-2v2M2 9h2m-2 6h2m16-6h2m-2 6h2"/>'),
  mem: I('<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 7v10m4-10v10m4-10v10"/>'),
  disk: I('<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/><path d="m16.5 7.5-3 3"/>'),
  gpu: I('<rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="8" cy="12" r="2.5"/><circle cx="15" cy="12" r="2.5"/><path d="M2 18v2m4-2v2"/>'),
  thermo: I('<path d="M14 14.76V5a2 2 0 0 0-4 0v9.76a4 4 0 1 0 4 0Z"/>'),
  server: I('<rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/>'),
  nas: I('<rect x="6" y="3" width="12" height="18" rx="2"/><path d="M9 7h6M9 11h6"/><path d="M15 16.5h.01"/>'),
  cloud: I('<path d="M17.5 19a4.5 4.5 0 0 0 .5-8.97A6 6 0 0 0 6.3 9.5 4 4 0 0 0 7 19Z"/>'),
  check: I('<path d="M20 6 9 17l-5-5"/>'),
  alert: I('<path d="M10.3 3.3 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.3a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4m0 4h.01"/>'),
  power: I('<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/>'),
  restart: I('<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>'),
  lock: I('<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>'),
  logout: I('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5M21 12H9"/>'),
  zap: I('<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/>'),
  trash: I('<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>'),
  windows: I('<path d="M3 5.5 10 4.5v7H3zM10 4.3 21 3v8.5H10zM3 12.5h7v7L3 18.5zM10 12.5h11V21l-11-1.3z" stroke-width="1.4"/>'),
  linux: I('<path d="M12 3c2.5 0 3.5 2.5 3.5 5 0 1.8 1 2.8 1.7 4.2.8 1.5 2 3 2 5 0 2-2 3.8-7.2 3.8S5 19.2 5 17.2c0-2 1.2-3.5 2-5C7.5 10.8 8.5 9.8 8.5 8 8.5 5.5 9.5 3 12 3Z"/><path d="M10 9h.01M14 9h.01" stroke-width="2.4"/>'),
  copy: I('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10" stroke-width="1.7"/>', { w: 1.7 }),
  terminal: I('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m5 0h4"/>'),
  refresh: I('<path d="M21 12a9 9 0 0 1-9 9 9 9 0 0 1-6.7-3M3 12a9 9 0 0 1 9-9 9 9 0 0 1 6.7 3"/><path d="M21 3v5h-5M3 21v-5h5"/>'),
  filter: I('<path d="M3 4h18l-7 8v6l-4 2v-8L3 4Z"/>'),
  plus: I('<path d="M12 5v14M5 12h14"/>'),
  scan: I('<path d="M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2M3 12h18"/>'),
  server: I('<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>'),
  globe: I('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18Z"/>'),
  wifi: I('<path d="M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M12 19.5h.01"/>'),
  arrowUp: I('<path d="M12 19V5m0 0-6 6m6-6 6 6"/>'),
  arrowDown: I('<path d="M12 5v14m0 0 6-6m-6 6-6-6"/>'),
  user: I('<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/>'),
  mail: I('<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m3 6 9 7 9-7"/>'),
  key: I('<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.5 12.5 9-9M16 4l3 3M19 7l2-2"/>'),
  gear: I('<circle cx="12" cy="12" r="3.2"/><path d="M19.4 13a7.8 7.8 0 0 0 0-2l2-1.5-2-3.4-2.3 1a7.6 7.6 0 0 0-1.7-1l-.3-2.5h-4l-.3 2.5a7.6 7.6 0 0 0-1.7 1l-2.3-1-2 3.4L4.6 11a7.8 7.8 0 0 0 0 2l-2 1.5 2 3.4 2.3-1a7.6 7.6 0 0 0 1.7 1l.3 2.5h4l.3-2.5a7.6 7.6 0 0 0 1.7-1l2.3 1 2-3.4Z"/>', { w: 1.6 }),
  save: I('<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"/><path d="M17 21v-8H7v8M7 3v5h8"/>', { w: 1.7 }),
  clock: I('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
  sliders: I('<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>', { w: 1.7 }),
  info: I('<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/>'),
  eye: I('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>'),
  eyeOff: I('<path d="M3 3l18 18M10.6 10.7a3 3 0 0 0 4.2 4.2M9.9 5.2A9.5 9.5 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.4 4.3M6.1 6.2A17 17 0 0 0 2 12s3.5 7 10 7a9.3 9.3 0 0 0 3-.5"/>'),
  external: I('<path d="M15 3h6v6M21 3l-9 9M10 5H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5"/>', { w: 1.7 }),
  bolt: I('<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/>'),
  shieldCheck: I('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>'),
  pencil: I('<path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/>'),
  camera: I('<path d="M3 8a2 2 0 0 1 2-2h2l1.5-2h7L19 6h0a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><circle cx="12" cy="13" r="3.5"/>'),
};

function osIcon(os = "") {
  const s = os.toLowerCase();
  if (s.includes("synology") || s.includes("dsm") || s.includes("nas")) return ICON.nas;
  if (s.includes("win")) return ICON.windows;
  if (s.includes("ubuntu") || s.includes("linux") || s.includes("debian") || s.includes("raspberry") || s.includes("pi os")) return ICON.linux;
  return ICON.server;
}
window.ICON = ICON; window.osIcon = osIcon;
