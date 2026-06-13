"""Remote-control handlers: shell, power, files. Cross-platform (Windows + Linux)."""
from __future__ import annotations

import asyncio
import base64
import os
import platform
import subprocess

IS_WIN = platform.system() == "Windows"


async def run_command(cmd: str, timeout: float = 60) -> dict:
    """Run a single shell command, returning combined output + exit code."""
    if IS_WIN:
        argv = ["powershell", "-NoProfile", "-Command", cmd]
    else:
        argv = ["/bin/sh", "-c", cmd]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {"output": out.decode(errors="replace"), "code": proc.returncode}
    except asyncio.TimeoutError:
        return {"output": "(timed out)", "code": 124}
    except Exception as exc:
        return {"output": f"error: {exc}", "code": 1}


async def run_script(content: str, shell: str = "shell", timeout: float = 120,
                     env: dict | None = None, files: list | None = None) -> dict:
    """Run a script in a temp working directory.

    ``env`` values are exported as environment variables (policy variables) and
    ``files`` ([{name, b64}]) are written alongside the script so it can use them.
    """
    import shutil
    import tempfile
    workdir = tempfile.mkdtemp(prefix="rmm-")
    suffix = ".ps1" if shell == "powershell" else (".bat" if (IS_WIN and shell == "cmd") else ".sh")
    script_path = os.path.join(workdir, "script" + suffix)
    try:
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(content)
        for item in (files or []):
            name = os.path.basename(item.get("name") or "")
            if not name:
                continue
            with open(os.path.join(workdir, name), "wb") as f:
                f.write(base64.b64decode(item.get("b64") or ""))
        run_env = dict(os.environ)
        for k, v in (env or {}).items():
            run_env[str(k)] = str(v)
        if shell == "powershell":
            argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path]
        elif IS_WIN:
            argv = ["cmd", "/c", script_path]
        else:
            os.chmod(script_path, 0o700)
            argv = ["/bin/sh", script_path]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            cwd=workdir, env=run_env,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {"output": out.decode(errors="replace"), "code": proc.returncode}
    except asyncio.TimeoutError:
        return {"output": "(timed out)", "code": 124}
    except Exception as exc:
        return {"output": f"error: {exc}", "code": 1}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def power_action(action: str) -> dict:
    """Perform a power/session action for the host OS."""
    try:
        if IS_WIN:
            cmds = {
                "reboot": ["shutdown", "/r", "/t", "5"],
                "shutdown": ["shutdown", "/s", "/t", "5"],
                "logoff": ["shutdown", "/l"],
                "lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
            }
        else:
            cmds = {
                "reboot": ["shutdown", "-r", "now"],
                "shutdown": ["shutdown", "-h", "now"],
                "logoff": ["pkill", "-KILL", "-u", os.environ.get("USER", "")],
                "lock": ["loginctl", "lock-sessions"],
            }
        if action not in cmds:
            return {"ok": False, "error": f"unknown action {action}"}
        subprocess.Popen(cmds[action])
        return {"ok": True, "action": action}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def file_get(path: str, max_bytes: int = 25 * 1024 * 1024) -> dict:
    try:
        size = os.path.getsize(path)
        if size > max_bytes:
            return {"ok": False, "error": "file too large"}
        with open(path, "rb") as f:
            data = f.read()
        return {"ok": True, "name": os.path.basename(path),
                "data": base64.b64encode(data).decode()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def file_put(path: str, b64: str) -> dict:
    try:
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return {"ok": True, "path": path}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
