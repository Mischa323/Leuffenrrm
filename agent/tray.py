"""Leuffen RMM — Windows system-tray companion.

Runs in the interactive user session (the main agent runs as SYSTEM in session 0,
which has no tray). It reads the agent's status file and shows a coloured icon:

  green  = connected to the server
  red    = disconnected

Right-click menu: connection status, last sync time, **Force sync now**, open the
dashboard, and quit. "Force sync" drops a flag file the agent watches and reacts to.

Dependencies: pystray, Pillow. Packaged into leuffen-rmm-tray.exe by CI.
"""
from __future__ import annotations

import json
import os
import sys
import time
import webbrowser

import pystray
from PIL import Image, ImageDraw

POLL_SECONDS = 3


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


def _icon(connected: bool) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ring = (40, 44, 52, 255)
    fill = (34, 197, 94, 255) if connected else (239, 68, 68, 255)
    d.ellipse((6, 6, 58, 58), fill=(18, 22, 30, 255), outline=ring, width=3)
    d.ellipse((20, 20, 44, 44), fill=fill)
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


class Tray:
    def __init__(self):
        self.status = _read_status()
        self.icon = pystray.Icon("leuffen-rmm", _icon(self._connected()),
                                 "Leuffen RMM", menu=self._menu())

    def _connected(self) -> bool:
        st = self.status
        # Treat as connected only if the agent said so and updated recently.
        return bool(st.get("connected")) and (time.time() - st.get("updated", 0) < 120)

    def _menu(self) -> pystray.Menu:
        conn = self._connected()
        return pystray.Menu(
            pystray.MenuItem(f"Status: {'Connected' if conn else 'Disconnected'}", None, enabled=False),
            pystray.MenuItem(f"Last sync: {_rel(self.status.get('last_sync'))}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Force sync now", self._force_sync),
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
            icon.icon = _icon(self._connected())
            icon.menu = self._menu()
            icon.update_menu()

    def run(self):
        self.icon.run(setup=self._refresh)


if __name__ == "__main__":
    try:
        Tray().run()
    except Exception:
        sys.exit(1)
