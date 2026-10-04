"""Read Codex rollout logs without retaining conversation content.

The JSONL format is an observed local implementation detail, not a public API.
Unknown records are ignored; missing measurements stay unknown.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime
from pathlib import Path

from .catalog import ThreadCatalog
from .navigation import NavigationLogs


def normalized_path(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))


def number(value: object) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value) and value >= 0 and value == int(value):
            return int(value)
    return None


class Rollout:
    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.identity = None
        self.metadata: dict = {}
        self.model = None
        self.usage: dict | None = None
        self.updated_at = None
        self.compactions = 0
        self.awaiting_usage = False
        self.modified = 0.0
        self.activity = 0.0
        self.invalid_records = 0

    def refresh(self) -> None:
        stat = self.path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if (self.identity is not None and identity != self.identity) or stat.st_size < self.offset:
            self.__init__(self.path)
        self.identity = identity
        self.modified = stat.st_mtime
        if stat.st_size == self.offset:
            return
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            for raw in stream:
                # A writer may still be appending the final JSON record.
                if not raw.endswith(b"\n"):
                    break
                self.offset += len(raw)
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    self.invalid_records += 1
                    continue
                if isinstance(record, dict):
                    self.consume(record)

    def consume(self, record: dict) -> None:
        stamp = record.get("timestamp")
        if isinstance(stamp, str):
            try:
                self.activity = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except (ValueError, OverflowError, OSError):
                pass
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        kind = record.get("type")
        if kind == "session_meta":
            # Do not retain instructions, messages, tool outputs or credentials.
            self.metadata = {key: payload.get(key) for key in ("id", "cwd", "source")}
        elif kind == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model != self.model:
                if self.model is not None:
                    self.awaiting_usage = True
                self.model = model
        elif kind == "compacted" or (kind == "event_msg" and payload.get("type") == "context_compacted"):
            self.compactions += 1
            self.awaiting_usage = True
        elif kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            # Null info is used for rate limit updates; it is not a new reading.
            if isinstance(info, dict):
                last = info.get("last_token_usage")
                self.usage = {
                    "last": {key: last.get(key) for key in ("total_tokens", "input_tokens", "output_tokens", "cached_input_tokens")} if isinstance(last, dict) else {},
                    "window": info.get("model_context_window"),
                }
                self.updated_at = record.get("timestamp")
                self.awaiting_usage = False

    def summary(self, warning: float, critical: float) -> dict:
        last = self.usage["last"] if self.usage else {}
        window = number(self.usage["window"]) if self.usage else None
        used = number(last.get("total_tokens"))
        # Do not fall back to cumulative usage or subtract cached input tokens.
        ratio = used / window * 100 if used is not None and window and not self.awaiting_usage else None
        level = "unknown" if ratio is None else "critical" if ratio >= critical else "warning" if ratio >= warning else "normal"
        return {
            "id": self.metadata.get("id"),
            "project": self.metadata.get("cwd"),
            "model": self.model,
            "used_tokens": used if ratio is not None else None,
            "context_window": window,
            "input_tokens": number(last.get("input_tokens")),
            "output_tokens": number(last.get("output_tokens")),
            "cached_input_tokens": number(last.get("cached_input_tokens")),
            "remaining_tokens": max(0, window - used) if ratio is not None else None,
            "percent": round(ratio, 2) if ratio is not None else None,
            "level": level,
            "updated_at": self.updated_at,
            "modified": self.modified,
            "activity": self.activity or self.modified,
            "compactions": self.compactions,
            "awaiting_usage": self.awaiting_usage,
            "invalid_records": self.invalid_records,
        }


class Monitor:
    def __init__(self, codex_home: Path, project: Path, warning=70.0, critical=85.0, *, include_all=False, log_roots=None):
        if not 0 < warning < critical <= 100:
            raise ValueError("阈值必须满足 0 < 提醒阈值 < 强提醒阈值 <= 100")
        self.root = codex_home / "sessions"
        self.catalog = ThreadCatalog(codex_home)
        self.navigation = NavigationLogs(log_roots or [])
        self.app_pid = None
        self.include_all = include_all
        self.project = normalized_path(str(project))
        self.warning = warning
        self.critical = critical
        self.rollouts: dict[Path, Rollout] = {}
        self.last_scan = -math.inf
        self.errors: list[str] = []

    def snapshot(self) -> dict:
        now = time.monotonic()
        if now - self.last_scan >= 3:
            self.last_scan = now
            self.errors = []
            try:
                paths = set(self.root.rglob("*.jsonl")) if self.root.is_dir() else set()
                self.rollouts = {p: r for p, r in self.rollouts.items() if p in paths}
                for path in paths:
                    if path not in self.rollouts:
                        self.rollouts[path] = Rollout(path)
            except OSError:
                self.errors.append("无法扫描 Codex 会话目录，请检查目录和权限。")
        sessions = []
        names = self.catalog.read()
        navigation = self.navigation.snapshot(self.app_pid)
        for rollout in self.rollouts.values():
            try:
                rollout.refresh()
            except OSError:
                self.errors.append("某个会话日志暂时无法读取，将在下次刷新重试。")
                continue
            meta = rollout.metadata
            if not isinstance(meta.get("id"), str) or not isinstance(meta.get("cwd"), str):
                continue
            # Root sessions only: a subagent has its own context budget.
            if isinstance(meta.get("source"), dict) and "subagent" in meta["source"]:
                continue
            if not self.include_all and normalized_path(meta["cwd"]) != self.project:
                continue
            summary = rollout.summary(self.warning, self.critical)
            summary["title"] = names.get(meta["id"], {}).get("title") or "未命名对话"
            sessions.append(summary)
        # Windows may defer mtime updates while Codex keeps a log handle open.
        sessions.sort(key=lambda item: item["activity"], reverse=True)
        return {
            "project": self.project,
            "sessions": sessions,
            "thresholds": {"warning": self.warning, "critical": self.critical},
            "errors": list(dict.fromkeys(self.errors))[-3:],
            "source_available": self.root.is_dir(),
            "active_thread_id": navigation.get("thread_id"),
            "navigation_known": navigation["known"],
            "active_route": navigation.get("route"),
            "active_title": names.get(navigation.get("thread_id"), {}).get("title"),
        }
