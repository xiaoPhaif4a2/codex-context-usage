"""Read Codex rollout logs without retaining conversation content.

The JSONL format is an observed local implementation detail, not a public API.
Unknown records are ignored; missing measurements stay unknown.
"""

from __future__ import annotations

import json
import math
import os
import time
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from .catalog import ThreadCatalog
from .navigation import NavigationLogs
from .advice import AdviceState, handoff_advice


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
        self.turn_id = None
        self.turn_index = 0
        self.in_progress = False
        self.usage_turn = None
        self.completed_turns = deque(maxlen=8)
        self.completed_ids = set()
        self.compaction_history = deque(maxlen=12)
        self.compaction_pending = False
        self.compaction_form = None
        self.compaction_stamp = None

    def begin_turn(self, identifier):
        if not isinstance(identifier, str) or not identifier or identifier == self.turn_id or identifier in self.completed_ids:
            return
        self.turn_id = identifier
        self.turn_index += 1
        self.in_progress = True

    def ratio(self):
        if not self.usage or self.awaiting_usage:
            return None
        window = number(self.usage["window"])
        used = number(self.usage["last"].get("total_tokens"))
        return round(used / window * 100, 2) if used is not None and window else None

    def complete_turn(self, identifier):
        identifier = identifier or self.turn_id
        if not identifier or identifier != self.turn_id or identifier in self.completed_ids:
            return
        self.completed_ids.add(identifier)
        self.in_progress = False
        self.completed_turns.append({"turn": self.turn_index, "compactions": self.compactions,
                                     "percent": self.ratio() if self.usage_turn == identifier else None})

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
            self.begin_turn(payload.get("turn_id"))
            model = payload.get("model")
            if isinstance(model, str) and model != self.model:
                if self.model is not None:
                    self.awaiting_usage = True
                    self.completed_turns.clear()
                self.model = model
        elif kind == "compacted" or (kind == "event_msg" and payload.get("type") == "context_compacted"):
            # Some versions emit two record forms for the same compaction.
            # Match paired notifications, without merging unrelated later events.
            stamp = record.get("timestamp")
            same_stamp = isinstance(stamp, str) and stamp == self.compaction_stamp
            close_pair = stamp is None and self.compaction_stamp is None
            try:
                gap = (datetime.fromisoformat(stamp.replace("Z", "+00:00")) - datetime.fromisoformat(self.compaction_stamp.replace("Z", "+00:00"))).total_seconds()
                close_pair = 0 <= gap <= 1
            except (AttributeError, ValueError, TypeError):
                pass
            duplicate = same_stamp or (self.compaction_pending and kind != self.compaction_form and close_pair)
            if not duplicate:
                self.compactions += 1
                self.compaction_history.append({"at": record.get("timestamp"), "turn": self.turn_index,
                                                "before_percent": self.ratio(), "after_percent": None})
                self.compaction_form = kind
                self.compaction_stamp = record.get("timestamp")
            self.compaction_pending = True
            self.awaiting_usage = True
        elif kind == "event_msg" and payload.get("type") == "task_started":
            self.begin_turn(payload.get("turn_id"))
        elif kind == "event_msg" and payload.get("type") == "task_complete":
            self.complete_turn(payload.get("turn_id"))
        elif kind == "event_msg" and payload.get("type") == "turn_aborted":
            self.in_progress = False
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
                self.usage_turn = self.turn_id
                if self.ratio() is not None:
                    self.compaction_pending = False
                if self.compaction_history and self.compaction_history[-1]["after_percent"] is None:
                    self.compaction_history[-1]["after_percent"] = self.ratio()
                if not self.in_progress and self.completed_turns and self.completed_turns[-1]["turn"] == self.turn_index:
                    self.completed_turns[-1].update(percent=self.ratio(), compactions=self.compactions)

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
            "turn_index": self.turn_index,
            "in_progress": self.in_progress,
            "completed_turns": list(self.completed_turns),
            "recent_compactions": sum(item["turn"] > max(0, self.turn_index - 5) for item in self.compaction_history),
            "last_compaction": dict(self.compaction_history[-1]) if self.compaction_history else None,
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
        self.advice_state = AdviceState(project / ".context-indicator-advice.json")
        self.lock = threading.RLock()

    def record_action(self, identifier: str, action: str):
        with self.lock:
            snapshot = self._snapshot()
            session = next((s for s in snapshot["sessions"] if s["id"] == identifier), None)
            if session is None or identifier != snapshot["active_thread_id"] or not snapshot["navigation_known"]:
                raise ValueError("请先在 Codex 中打开目标对话，等待导航更新")
            self.advice_state.update(session, action)

    def set_thresholds(self, warning: float, critical: float):
        if not 0 < warning < critical <= 100:
            raise ValueError("阈值必须满足 0 < 关注容量 < 高占用 <= 100")
        with self.lock:
            self.warning, self.critical = warning, critical

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot()

    def _snapshot(self) -> dict:
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
        self.advice_state.refresh()
        navigation = self.navigation.snapshot(self.app_pid)
        selected = {}
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
            identifier = meta["id"]
            old = selected.get(identifier)
            preferred = names.get(identifier, {}).get("rollout_path")
            if old:
                if preferred and normalized_path(str(old.path)) == normalized_path(preferred):
                    continue
                new_key = (rollout.activity or rollout.modified, str(rollout.path))
                old_key = (old.activity or old.modified, str(old.path))
                if not (preferred and normalized_path(str(rollout.path)) == normalized_path(preferred)) and new_key <= old_key:
                    continue
            selected[identifier] = rollout
        for rollout in selected.values():
            meta = rollout.metadata
            summary = rollout.summary(self.warning, self.critical)
            summary["title"] = names.get(meta["id"], {}).get("title") or "未命名对话"
            summary["advice"] = handoff_advice(summary, self.warning, self.critical, self.advice_state.entries.get(meta["id"]))
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
