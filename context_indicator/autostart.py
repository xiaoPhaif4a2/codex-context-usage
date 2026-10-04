"""Opt-in current-user Windows logon startup; never edit Codex itself."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CodexContextUsage"


def launch_command(project: Path, *, source_root: Path | None = None, executable: Path | None = None) -> str:
    source_root = (source_root or Path(__file__).resolve().parents[1]).resolve()
    launcher = source_root / "start.pyw"
    python = (executable or Path(sys.executable)).resolve()
    if python.name.lower() == "python.exe":
        python = python.with_name("pythonw.exe")
    if not launcher.is_file() or not python.is_file() or python.name.lower() != "pythonw.exe":
        raise ValueError("自启动需要项目中的 start.pyw 和已安装的 pythonw.exe。")
    command = subprocess.list2cmdline([str(python), str(launcher), "--project", str(project.resolve()), "--quiet-if-running"])
    if len(command) > 260:
        raise ValueError("自启动命令超过 Windows Run 的 260 字符限制，请把项目放在较短路径。")
    return command


def startup_command() -> str | None:
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            command, kind = winreg.QueryValueEx(key, VALUE_NAME)
        return command if kind == winreg.REG_SZ and isinstance(command, str) and command else None
    except FileNotFoundError:
        return None


def configure(enabled: bool, project: Path) -> None:
    if winreg is None:
        raise RuntimeError("自启动当前仅支持 Windows。")
    if enabled:
        command = launch_command(project)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            pass
