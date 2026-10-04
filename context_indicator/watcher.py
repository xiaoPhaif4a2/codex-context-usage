"""Lightweight window listener starts/stops a separate indicator process."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .windows import Windows


_REGISTER_TASK = r"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute $env:CCI_PYTHON -Argument $env:CCI_ARGUMENTS -WorkingDirectory $env:CCI_SOURCE
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName $env:CCI_TASK_NAME -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $env:CCI_TASK_NAME
"""
_REMOVE_TASK = r"""
$ErrorActionPreference = 'Stop'
Get-ScheduledTask -TaskName $env:CCI_TASK_NAME -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false
"""


def dispatch_follower(project, child_args, quiet_if_running=False):
    """Start the listener outside a Codex-launched terminal's Windows job tree."""
    if os.name != "nt":
        raise RuntimeError("桌面跟随当前支持 Windows；其他系统请使用 --web。")
    windows = Windows()
    if windows.instance_running(project + ":follower"):
        if quiet_if_running:
            return
        raise RuntimeError("该项目的 Codex 跟随程序已在运行。")
    python = Path(sys.executable)
    if python.name.lower() == "python.exe" and python.with_name("pythonw.exe").is_file():
        python = python.with_name("pythonw.exe")
    env = dict(os.environ)
    env.update(CCI_PYTHON=str(python), CCI_SOURCE=str(Path(__file__).resolve().parents[1]),
               CCI_ARGUMENTS=subprocess.list2cmdline(["-m", "context_indicator", "--managed-follower",
                                                    "--quiet-if-running", *child_args]),
               CCI_TASK_NAME="CodexContextUsageLaunch-" + uuid.uuid4().hex)
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    try:
        subprocess.run([*command, _REGISTER_TASK], env=env, check=True, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        for _ in range(50):
            if windows.instance_running(project + ":follower"):
                return
            time.sleep(0.1)
        raise RuntimeError("指示器监听未能启动；请检查 Windows 任务计划程序。")
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError("无法通过 Windows 任务计划程序启动指示器监听。" + (" " + detail if detail else "")) from error
    finally:
        subprocess.run([*command, _REMOVE_TASK], env=env, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)


class Follower:
    def __init__(self, command, project, windows, *, launcher=subprocess.Popen, clock=time.monotonic):
        self.command, self.project, self.windows = command, project, windows
        self.launcher, self.clock = launcher, clock
        self.child = None
        self.retry_at = 0.0
        self.failures = 0

    def stop_child(self):
        if self.child:
            if self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=3)
            self.child = None

    def tick(self, codex_open: bool):
        if not codex_open:
            self.stop_child()
            self.retry_at, self.failures = 0.0, 0
            return
        if self.child:
            result = self.child.poll()
            if result is None:
                return
            self.child = None
            self.failures += 1
            self.retry_at = self.clock() + min(30, 2 ** min(self.failures, 5))
        if self.clock() < self.retry_at or self.windows.instance_running(self.project):
            return
        source = Path(__file__).resolve().parents[1]
        try:
            with (Path(self.project) / ".context-indicator-error.log").open("ab") as errors:
                self.child = self.launcher(self.command, cwd=source, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=errors,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            self.failures += 1
            self.retry_at = self.clock() + min(30, 2 ** min(self.failures, 5))


def run_follower(project, child_args, quiet_if_running=False):
    if os.name != "nt":
        raise RuntimeError("桌面跟随当前支持 Windows；其他系统请使用 --web。")
    windows = Windows()
    if not windows.acquire_instance(project + ":follower"):
        if quiet_if_running:
            return
        raise RuntimeError("该项目的 Codex 跟随程序已在运行。")
    stop = None
    follower = None
    try:
        stop = windows.follower_event(project)
        python = Path(sys.executable)
        if python.name.lower() == "python.exe" and python.with_name("pythonw.exe").is_file():
            python = python.with_name("pythonw.exe")
        command = [str(python), "-m", "context_indicator", "--managed-child", "--quiet-if-running", *child_args]
        follower = Follower(command, project, windows)
        while windows.kernel.WaitForSingleObject(stop, 0) != 0:
            follower.tick(bool(windows.codex_windows()))
            # Named-event wait wakes immediately when the user chooses Stop.
            if windows.kernel.WaitForSingleObject(stop, 1000) == 0:
                break
    except KeyboardInterrupt:
        pass
    finally:
        if follower:
            follower.stop_child()
        if stop:
            windows.kernel.CloseHandle(stop)
        windows.release_instance()
