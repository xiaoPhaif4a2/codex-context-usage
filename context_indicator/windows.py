"""Win32 window tracking for the companion badge; no app patching or injection."""

from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import wintypes
from pathlib import PureWindowsPath


class Windows:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user.GetAncestor.restype = wintypes.HWND
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsIconic.argtypes = [wintypes.HWND]
        self.user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.instance = None
        self.target = None
        self.target_pid = None
        try:
            self.user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            self.user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            self.user.SetProcessDPIAware()

    def acquire_instance(self, project: str) -> bool:
        name = "Local\\CodexContextIndicator-" + hashlib.sha256(project.encode()).hexdigest()[:24]
        ctypes.set_last_error(0)
        handle = self.kernel.CreateMutexW(None, False, name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS
            self.kernel.CloseHandle(handle)
            return False
        self.instance = handle
        return True

    def release_instance(self):
        if self.instance:
            self.kernel.CloseHandle(self.instance)
            self.instance = None

    def process(self, hwnd) -> tuple[int, str]:
        pid = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return pid.value, ""
        try:
            text = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(text))
            if self.kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(size)):
                return pid.value, PureWindowsPath(text.value).name.lower()
            return pid.value, ""
        finally:
            self.kernel.CloseHandle(handle)

    def active_rect(self) -> tuple[int, int, int, int] | None:
        foreground = self.user.GetForegroundWindow()
        pid, name = self.process(foreground)
        if name in ("codex.exe", "chatgpt.exe"):
            self.target = foreground
            self.target_pid = pid
        elif pid != os.getpid():
            return None
        if not self.target or not self.user.IsWindowVisible(self.target) or self.user.IsIconic(self.target):
            return None
        rect = wintypes.RECT()
        point = wintypes.POINT()
        if not self.user.GetClientRect(self.target, ctypes.byref(rect)) or not self.user.ClientToScreen(self.target, ctypes.byref(point)):
            return None
        return point.x, point.y, rect.right, rect.bottom

    def floating_style(self, tk_window) -> None:
        tk_window.update_idletasks()
        hwnd = self.user.GetAncestor(tk_window.winfo_id(), 2)
        style = self.user.GetWindowLongW(hwnd, -20)
        # Tool window: no taskbar entry. No activate: clicks do not steal typing focus.
        self.user.SetWindowLongW(hwnd, -20, style | 0x80 | 0x08000000)

    def place(self, tk_window, x: int, y: int, width: int, height: int):
        hwnd = self.user.GetAncestor(tk_window.winfo_id(), 2)
        self.user.SetWindowPos(hwnd, wintypes.HWND(-1), x, y, width, height, 0x0010 | 0x0040)
