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
    # Kill any stray agent processes, then start a single fresh one via the task.
    for args in (["schtasks", "/end", "/tn", "LeuffenRMMAgent"],
                 ["taskkill", "/F", "/IM", "leuffen-rmm-agent.exe"],
                 ["schtasks", "/run", "/tn", "LeuffenRMMAgent"]):
        try:
            subprocess.run(args, capture_output=True, timeout=20)
        except Exception:
            pass


def _apply_settings(url: str, key: str, insecure: bool) -> None:
    """Persist config to machine env (needs admin) and restart the agent."""
    for name, val in (("RMM_SERVER_URL", url), ("RMM_API_KEY", key),
                      ("RMM_INSECURE_TLS", "1" if insecure else "0")):
        subprocess.run(["setx", "/M", name, val], capture_output=True, timeout=20)
    _restart_agent()


def _is_configured() -> bool:
    return bool(os.environ.get("RMM_SERVER_URL") and os.environ.get("RMM_API_KEY")) \
        or bool(_read_status().get("server_url"))


def _self_exe() -> str:
    return sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)


def _elevate(args: str) -> None:
    """Relaunch this program elevated (UAC) with the given argument string."""
    if getattr(sys, "frozen", False):
        target, params = _self_exe(), args
    else:
        target, params = sys.executable, f'"{_self_exe()}" {args}'
    ctypes.windll.shell32.ShellExecuteW(None, "runas", target, params, None, 1)


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

    pad = {"padx": 12, "pady": (6, 0)}
    hint = {"fg": "#6b7280", "font": ("Segoe UI", 8)}

    tk.Label(root, text="Leuffen RMM — agent settings",
             font=("Segoe UI", 11, "bold")).grid(row=0, column=0, columnspan=2,
                                                  sticky="w", padx=12, pady=(12, 8))

    tk.Label(root, text="Server URL").grid(row=1, column=0, sticky="w", **pad)
    url = tk.Entry(root, width=46)
    url.insert(0, os.environ.get("RMM_SERVER_URL") or status.get("server_url") or "")
    url.grid(row=1, column=1, **pad)
    tk.Label(root, text="e.g.  https://rmm.leuffen.it:8000   (include https:// and the port)",
             **hint).grid(row=2, column=1, sticky="w", padx=12)

    tk.Label(root, text="Enrollment key").grid(row=3, column=0, sticky="w", **pad)
    key = tk.Entry(root, width=46)
    key.insert(0, os.environ.get("RMM_API_KEY") or "")
    key.grid(row=3, column=1, **pad)
    tk.Label(root, text="from the dashboard → open an org → Downloads → Enrollment key",
             **hint).grid(row=4, column=1, sticky="w", padx=12)

    insecure = tk.BooleanVar(value=(os.environ.get("RMM_INSECURE_TLS", "1") == "1"))
    tk.Checkbutton(root, text="Accept self-signed certificate (leave on for the default setup)",
                   variable=insecure).grid(row=5, column=1, sticky="w", padx=12, pady=(8, 0))

    def save():
        u, k = url.get().strip(), key.get().strip()
        if not u or not k:
            messagebox.showerror("Leuffen RMM", "Server URL and enrollment key are required.")
            return
        if _is_admin():
            _apply_settings(u, k, bool(insecure.get()))
            messagebox.showinfo("Leuffen RMM", "Settings saved. The agent is reconnecting.")
            root.destroy()
            return
        # Not elevated: write the values to a temp file and re-apply them elevated
        # (preserves what was typed; one UAC prompt).
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w") as f:
            json.dump({"url": u, "key": k, "insecure": bool(insecure.get())}, f)
        _elevate(f'--apply "{path}"')
        messagebox.showinfo("Leuffen RMM", "Approve the administrator prompt to finish saving.")
        root.destroy()

    btns = tk.Frame(root)
    btns.grid(row=6, column=1, sticky="e", padx=12, pady=12)
    tk.Button(btns, text="Cancel", command=root.destroy).pack(side="right", padx=4)
    tk.Button(btns, text="Save", command=save, width=10).pack(side="right", padx=4)
    root.mainloop()


# --------------------------------------------------------------------------- #
# Tray
# --------------------------------------------------------------------------- #
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
        try:
            if _is_admin():
                if getattr(sys, "frozen", False):
                    subprocess.Popen([_self_exe(), "--settings"])
                else:
                    subprocess.Popen([sys.executable, _self_exe(), "--settings"])
            else:
                _elevate("--settings")
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


def _do_apply(path: str) -> None:
    """Elevated re-entry: apply settings from a temp file, then confirm."""
    try:
        with open(path) as f:
            d = json.load(f)
        _apply_settings(d["url"], d["key"], bool(d.get("insecure", True)))
    except Exception:
        return
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo("Leuffen RMM", "Settings saved. The agent is reconnecting.")
        r.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        if "--apply" in sys.argv:
            _do_apply(sys.argv[sys.argv.index("--apply") + 1])
        elif "--settings" in sys.argv:
            settings_dialog()
        else:
            # First run with no configuration → open the settings dialog, then tray.
            if os.name == "nt" and not _is_configured():
                settings_dialog()
            Tray().run()
    except Exception:
        sys.exit(1)
