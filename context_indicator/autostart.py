"""Opt-in current-user logon task; never edit Codex itself."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CodexContextUsage"
TASK_NAME = "CodexContextUsage"

_REGISTER_TASK = r"""
$ErrorActionPreference = 'Stop'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $env:CCI_PYTHON -Argument $env:CCI_ARGUMENTS -WorkingDirectory $env:CCI_SOURCE
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -StartWhenAvailable
Register-ScheduledTask -TaskName 'CodexContextUsage' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
"""
_REMOVE_TASK = r"""
$ErrorActionPreference = 'Stop'
Get-ScheduledTask -TaskName 'CodexContextUsage' -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false
"""
_QUERY_TASK = r"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName 'CodexContextUsage' -ErrorAction SilentlyContinue
if ($task) {
    $action = $task.Actions[0]
    $data = @{ Execute = $action.Execute; Arguments = $action.Arguments } | ConvertTo-Json -Compress
    [Console]::Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($data)))
}
"""


def _launch_parts(project: Path, *, source_root: Path | None = None, executable: Path | None = None):
    source_root = (source_root or Path(__file__).resolve().parents[1]).resolve()
    launcher = source_root / "start.pyw"
    python = (executable or Path(sys.executable)).resolve()
    if python.name.lower() == "python.exe":
        python = python.with_name("pythonw.exe")
    if not launcher.is_file() or not python.is_file() or python.name.lower() != "pythonw.exe":
        raise ValueError("自启动需要项目中的 start.pyw 和已安装的 pythonw.exe。")
    return python, [str(launcher), "--managed-follower", "--project", str(project.resolve()), "--quiet-if-running"], source_root


def _task_command(script: str, env: dict | None = None):
    try:
        return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                              env=env, capture_output=True, check=True, timeout=30,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("无法配置 Windows 任务计划程序的登录启动项。") from error


def launch_command(project: Path, *, source_root: Path | None = None, executable: Path | None = None) -> str:
    python, arguments, _ = _launch_parts(project, source_root=source_root, executable=executable)
    return subprocess.list2cmdline([str(python), *arguments])


def startup_command() -> str | None:
    if os.name != "nt":
        return None
    output = _task_command(_QUERY_TASK).stdout.strip()
    if not output:
        return None
    data = json.loads(base64.b64decode(output).decode("utf-8"))
    return subprocess.list2cmdline([data["Execute"]]) + " " + data["Arguments"]


def configure(enabled: bool, project: Path) -> None:
    if winreg is None:
        raise RuntimeError("自启动当前仅支持 Windows。")
    if enabled:
        python, arguments, source = _launch_parts(project)
        env = dict(os.environ)
        env.update(CCI_PYTHON=str(python), CCI_ARGUMENTS=subprocess.list2cmdline(arguments), CCI_SOURCE=str(source))
        _task_command(_REGISTER_TASK, env)
    else:
        _task_command(_REMOVE_TASK)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass
