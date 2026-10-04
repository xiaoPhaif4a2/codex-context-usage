"""Explainable handoff heuristics; never infer model quality from token counts."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class AdviceState:
    """Persist only explicit quality flags and user-confirmed checkpoints."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, dict] = {}
        self.signature = None
        self.refresh()

    def refresh(self):
        try:
            signature = self.path.stat().st_mtime_ns
            if signature == self.signature:
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            self.entries = {}
            for identifier, entry in data.items():
                if not isinstance(identifier, str) or not isinstance(entry, dict):
                    continue
                clean = {"quality_issue": entry.get("quality_issue") is True}
                for key in ("checkpoint_turn", "checkpoint_compactions"):
                    value = entry.get(key, 0)
                    clean[key] = value if type(value) is int and value >= 0 else 0
                stamp = entry.get("checkpoint_at")
                if isinstance(stamp, str):
                    try:
                        datetime.fromisoformat(stamp)
                        clean["checkpoint_at"] = stamp
                    except ValueError:
                        pass
                self.entries[identifier] = clean
            self.signature = signature
        except (OSError, ValueError):
            pass

    def update(self, session: dict, action: str):
        self.refresh()
        entries = dict(self.entries)
        entry = dict(entries.get(session["id"], {}))
        if action == "quality_issue":
            entry["quality_issue"] = True
        elif action == "clear_issue":
            entry["quality_issue"] = False
        elif action == "checkpoint_saved":
            entry["checkpoint_turn"] = session.get("turn_index", 0)
            entry["checkpoint_compactions"] = session.get("compactions", 0)
            entry["checkpoint_at"] = datetime.now(timezone.utc).isoformat()
        else:
            raise ValueError("不支持的操作")
        entries[session["id"]] = entry
        # Readers only see a complete JSON document; merge state before each action.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".context-indicator-advice-", suffix=".tmp.json", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(entries, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        except OSError:
            if temporary:
                temporary.unlink(missing_ok=True)
            raise
        self.entries = entries
        self.signature = self.path.stat().st_mtime_ns


def handoff_advice(session: dict, warning: float, critical: float, state: dict | None = None) -> dict:
    """Return an actionable explanation, not a probability of forgetting."""
    state = state or {}
    percent = session.get("percent")
    compactions = session.get("compactions", 0)
    turn = session.get("turn_index", 0)
    recent = session.get("recent_compactions", 0)
    quality_issue = state.get("quality_issue", False)
    checkpoint_turn = state.get("checkpoint_turn", 0)
    if checkpoint_turn > turn:
        checkpoint_turn = 0  # A replaced/truncated log cannot acknowledge future work.
    high_turns = 0
    # Only completed user requests count. Polls and tool calls never add turns.
    for sample in reversed(session.get("completed_turns", [])):
        if sample["turn"] <= checkpoint_turn:
            break
        if sample["compactions"] != compactions or sample.get("percent") is None or sample["percent"] < critical:
            break
        high_turns += 1
    # A new low reading cancels the old high streak immediately.
    if percent is None or percent < critical:
        high_turns = 0
    saved_compactions = state.get("checkpoint_compactions", 0)
    if saved_compactions > compactions:
        saved_compactions = 0
    reasons, code = [], "continue"
    rank, label = 0, "继续工作"
    if quality_issue:
        rank, label, code = 3, "建议阶段后交接", "reported_issue"
        reasons.append("你已标记这个对话出现遗漏要求或混淆状态。")
    elif recent >= 2:
        rank, label, code = 3, "建议阶段后交接", "frequent_compaction"
        reasons.append(f"最近 5 轮用户请求内至少压缩 {recent} 次，建议在阶段结束后整理并切换。")
    elif compactions >= 3 and percent is not None and percent >= warning:
        rank, label, code = 3, "建议阶段后交接", "repeated_growth"
        reasons.append(f"已累计压缩 {compactions} 次，当前占用又达到 {percent:.1f}%。")
    elif high_turns >= 3:
        rank, label, code = 2, "建议留存进度", "sustained_high"
        reasons.append(f"连续 {high_turns} 轮已完成请求的占用不低于 {critical:g}%。")
    elif compactions - saved_compactions >= 2:
        rank, label, code = 2, "建议留存进度", "checkpoint_compaction"
        reasons.append(f"自上次确认留存进度以来已压缩 {compactions - saved_compactions} 次。")
    elif percent is not None and percent >= warning:
        rank, label, code = 1, "留意容量", "capacity_only"
        reasons.append(f"当前占用 {percent:.1f}%，单次高占用不要求换对话。")
    elif percent is None:
        rank, label, code = 0, "等待读数", "unknown"
        reasons.append("尚无有效用量；不根据过期读数判断是否需要交接。")
    elif compactions:
        reasons.append(f"已压缩 {compactions} 次，当前容量已恢复；可以继续工作。")
    else:
        reasons.append("未出现持续高占用或反复压缩信号，可以继续当前对话。")
    if rank == 3:
        action = "在本轮完成或合适的暂停点生成并检查 HANDOFF.md，再新建对话读取它。"
    elif rank == 2:
        action = "本轮或阶段结束后保存进度；可以留在当前对话继续，无需立即切换。"
    else:
        action = "继续工作即可；阶段完成时也可以主动留存进度。"
    if session.get("in_progress") and rank >= 2:
        action = "本轮仍在执行，先等待完成或主动暂停，再保存文档；不要仅因提醒打断任务。"
    return {"rank": rank, "label": label, "code": code, "reasons": reasons, "action": action,
            "high_turns": high_turns, "quality_issue": quality_issue,
            "checkpoint_confirmed": bool(state.get("checkpoint_at")), "checkpoint_at": state.get("checkpoint_at"),
            "basis": "基于日志的经验建议，不代表已检测到记忆丢失；工具不会评估回答是否正确。"}
