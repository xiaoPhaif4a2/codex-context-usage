"""Follow observed desktop navigation log records, not rollout activity.

No UI automation, injected code, debug port, or desktop modification is required.
The log marker is an implementation detail; unsupported versions stay unknown.
"""

import os
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROCESS = re.compile(r"codex-desktop-.*-(\d+)-t0-")
MARKER = "[electron-message-handler] IAB_LIFECYCLE received browser sidebar owner sync "
THREAD = re.compile(r"/local/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$)", re.I)


def default_log_roots() -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    roots = [local / "Codex" / "Logs"]
    # Microsoft Store packaging can redirect the same location into LocalCache.
    for package in (local / "Packages").glob("OpenAI.Codex_*"):
        roots.append(package / "LocalCache" / "Local" / "Codex" / "Logs")
    unique = []
    for root in roots:
        if root.is_dir() and not any(root.samefile(other) for other in unique):
            unique.append(root)
    return unique


def field(line: str, name: str) -> str | None:
    found = re.search(r"(?:^|\s)" + re.escape(name) + r'=(?:"([^"\n]*)"|(\S+))', line)
    return (found[1] if found[1] is not None else found[2]) if found else None


class NavigationFile:
    def __init__(self, path: Path, pid: int):
        self.path, self.pid = path, pid
        self.offset = 0
        self.identity = None
        self.routes = {}
        self.focus = None

    def consume(self, line: str):
        if not re.match(r"^\d{4}-\d\d-\d\dT", line):
            return
        stamp = line.split(" ", 1)[0]
        if MARKER in line and " info " in line:
            route, window = field(line, "ownerRoutePath"), field(line, "windowId")
            if route is None or not window or not window.isdigit():
                return
            path = urlsplit(unquote(route)).path
            match = THREAD.match(path)
            self.routes[int(window)] = {"pid": self.pid, "window_id": int(window), "route": route, "thread_id": match[1].lower() if match else None, "timestamp": stamp}
        if field(line, "rendererWindowFocused") == "true":
            window = field(line, "rendererWindowId")
            if window and window.isdigit():
                self.focus = (stamp, int(window))

    def refresh(self):
        stat = self.path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if (self.identity is not None and self.identity != identity) or stat.st_size < self.offset:
            self.__init__(self.path, self.pid)
        self.identity = identity
        if stat.st_size == self.offset:
            return
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            for raw in stream:
                if not raw.endswith(b"\n"):
                    break
                self.offset += len(raw)
                self.consume(raw.decode("utf-8", errors="replace"))


class NavigationLogs:
    def __init__(self, roots: list[Path]):
        self.roots = roots
        self.files = {}
        self.last_scan = -100.0

    def snapshot(self, pid: int | None = None) -> dict:
        if time.monotonic() - self.last_scan >= 2:
            self.last_scan = time.monotonic()
            candidates = []
            for root in self.roots:
                try:
                    for path in root.rglob("*t0*.log"):
                        match = PROCESS.search(path.name)
                        if match:
                            candidates.append((path, int(match[1]), path.stat().st_mtime))
                except OSError:
                    continue
            # Keep recent files from the desktop process, including log rotation.
            candidates.sort(key=lambda row: row[2], reverse=True)
            if pid is None and candidates:
                pid = candidates[0][1]
            selected = {path: process for path, process, _ in candidates if process == pid}
            self.files = {path: self.files.get(path) or NavigationFile(path, process) for path, process in selected.items()}
        routes, focuses = {}, []
        for reader in self.files.values():
            if pid is not None and reader.pid != pid:
                continue
            try:
                reader.refresh()
            except OSError:
                continue
            for window, route in reader.routes.items():
                key = (reader.pid, window)
                if key not in routes or route["timestamp"] > routes[key]["timestamp"]:
                    routes[key] = route
            if reader.focus:
                focuses.append((reader.focus[0], reader.pid, reader.focus[1]))
        if focuses:
            _, process, window = max(focuses)
            route = routes.get((process, window))
        else:
            # One observed primary window is unambiguous; multiple windows aren't.
            route = next(iter(routes.values())) if len(routes) == 1 else None
        return {"known": route is not None, **(route or {})}
