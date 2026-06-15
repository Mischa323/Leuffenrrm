"""Leuffen RMM — Windows system-tray companion.

Runs in the interactive user session (the main agent runs as SYSTEM in session 0,
which has no tray). It reads the agent's status file and shows the Leuffen shield
with a connection indicator:

  green dot = connected to the server
  red dot   = disconnected

Right-click menu: connection status, last sync, **Force sync now**, **Settings…**
(requires admin — elevates via UAC), open the dashboard, and quit.

Run with ``--settings`` to open just the settings dialog (used for elevation).

Dependencies: pystray, Pillow (Tk ships with Python). Packaged to
leuffen-rmm-tray.exe by CI.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import webbrowser

import pystray
from PIL import Image, ImageDraw

POLL_SECONDS = 3
ACCENT = (59, 130, 246, 255)   # Leuffen brand blue
GOOD = (34, 197, 94, 255)
BAD = (239, 68, 68, 255)


def _data_dir() -> str:
    env = os.environ.get("RMM_DATA_DIR")
    if env:
        return env
    if os.name == "nt":
        return os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "LeuffenRMM")
    return os.path.dirname(os.path.abspath(__file__))


STATUS_PATH = os.path.join(_data_dir(), "status.json")
SYNC_FLAG = os.path.join(_data_dir(), "sync_request")


def _read_status() -> dict:
    try:
        with open(STATUS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _logo(connected: bool) -> Image.Image:
    """Leuffen shield with a connection-status dot."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Shield silhouette (brand blue).
    shield = [(13, 13), (51, 13), (51, 33), (32, 55), (13, 33)]
    d.polygon(shield, fill=ACCENT)
    # A subtle check/▲ glyph inside the shield.
    d.line([(24, 28), (30, 35), (42, 21)], fill=(255, 255, 255, 235), width=4, joint="curve")
    # Status dot, bottom-right.
    col = GOOD if connected else BAD
    d.ellipse((41, 41, 61, 61), fill=col, outline=(13, 17, 24, 255), width=3)
    return img


def _rel(ts) -> str:
    if not ts:
        return "never"
    s = time.time() - ts
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} d ago"


# --------------------------------------------------------------------------- #
# Settings dialog (admin only)
# --------------------------------------------------------------------------- #
def _restart_agent() -> None:
    for args in (["schtasks", "/end", "/tn", "LeuffenRMMAgent"],
                 ["schtasks", "/run", "/tn", "LeuffenRMMAgent"]):
        try:
            subprocess.run(args, capture_output=True, timeout=20)
        except Exception:
            pass


def settings_dialog() -> None:
    import tkinter as tk
    from tkinter import messagebox

    status = _read_status()
    root = tk.Tk()
    root.title("Leuffen RMM — Settings")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    admin = _is_admin()
    pad = {"padx": 12, "pady": 6}

    tk.Label(root, text="Server URL").grid(row=0, column=0, sticky="w", **pad)
    url = tk.Entry(root, width=44)
    url.insert(0, os.environ.get("RMM_SERVER_URL") or status.get("server_url") or "")
    url.grid(row=0, column=1, **pad)

    tk.Label(root, text="Enrollment key").grid(row=1, column=0, sticky="w", **pad)
    key = tk.Entry(root, width=44)
    key.insert(0, os.environ.get("RMM_API_KEY") or "")
    key.grid(row=1, column=1, **pad)

    insecure = tk.BooleanVar(value=(os.environ.get("RMM_INSECURE_TLS", "1") == "1"))
    tk.Checkbutton(root, text="Accept self-signed certificate (insecure TLS)",
                   variable=insecure).grid(row=2, column=1, sticky="w", **pad)

    def save():
        u, k = url.get().strip(), key.get().strip()
        if not u or not k:
            messagebox.showerror("Leuffen RMM", "Server URL and enrollment key are required.")
            return
        denied = False
        for name, val in (("RMM_SERVER_URL", u), ("RMM_API_KEY", k),
                          ("RMM_INSECURE_TLS", "1" if insecure.get() else "0")):
            r = subprocess.run(["setx", "/M", name, val], capture_output=True, timeout=20, text=True)
            if r.returncode != 0:
                denied = True
        if denied:
            messagebox.showerror(
                "Leuffen RMM",
                "Administrator rights are required to save these settings.\n\n"
                "Right-click the Leuffen RMM tray icon and choose “Settings…” "
                "(it will prompt for admin), or run this installer as administrator.")
            return
        _restart_agent()
        messagebox.showinfo("Leuffen RMM", "Settings saved. The agent is reconnecting.")
        root.destroy()

    btns = tk.Frame(root)
    btns.grid(row=4, column=1, sticky="e", **pad)
    tk.Button(btns, text="Cancel", command=root.destroy).pack(side="right", padx=4)
    tk.Button(btns, text="Save", command=save).pack(side="right", padx=4)
    root.mainloop()


# --------------------------------------------------------------------------- #
# Tray
# --------------------------------------------------------------------------- #
def _self_exe() -> str:
    return sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)


class Tray:
    def __init__(self):
        self.status = _read_status()
        self.icon = pystray.Icon("leuffen-rmm", _logo(self._connected()),
                                 "Leuffen RMM", menu=self._menu())

    def _connected(self) -> bool:
        st = self.status
        return bool(st.get("connected")) and (time.time() - st.get("updated", 0) < 120)

    def _menu(self) -> pystray.Menu:
        conn = self._connected()
        admin = _is_admin()
        return pystray.Menu(
            pystray.MenuItem(f"Status: {'Connected' if conn else 'Disconnected'}", None, enabled=False),
            pystray.MenuItem(f"Last sync: {_rel(self.status.get('last_sync'))}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Force sync now", self._force_sync),
            pystray.MenuItem("Settings…" + ("" if admin else " (admin)"), self._open_settings),
            pystray.MenuItem("Open dashboard", self._open_dashboard,
                             enabled=bool(self.status.get("server_url"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def _force_sync(self, icon=None, item=None):
        try:
            with open(SYNC_FLAG, "w") as f:
                f.write(str(time.time()))
            icon.notify("Sync requested", "Leuffen RMM")
        except Exception:
            pass

    def _open_settings(self, icon=None, item=None):
        exe = _self_exe()
        try:
            if _is_admin():
                if getattr(sys, "frozen", False):
                    subprocess.Popen([exe, "--settings"])
                else:
                    subprocess.Popen([sys.executable, exe, "--settings"])
            else:
                # Elevate via UAC, then open the settings dialog.
                params = "--settings" if getattr(sys, "frozen", False) else f'"{exe}" --settings'
                target = exe if getattr(sys, "frozen", False) else sys.executable
                ctypes.windll.shell32.ShellExecuteW(None, "runas", target, params, None, 1)
        except Exception:
            pass

    def _open_dashboard(self, icon=None, item=None):
        url = self.status.get("server_url")
        if url:
            webbrowser.open(url)

    def _quit(self, icon=None, item=None):
        self.icon.stop()

    def _refresh(self, icon):
        icon.visible = True
        while True:
            time.sleep(POLL_SECONDS)
            self.status = _read_status()
            icon.icon = _logo(self._connected())
            icon.menu = self._menu()
            icon.update_menu()

    def run(self):
        self.icon.run(setup=self._refresh)


if __name__ == "__main__":
    try:
        if "--settings" in sys.argv:
            settings_dialog()
        else:
            Tray().run()
    except Exception:
        sys.exit(1)
