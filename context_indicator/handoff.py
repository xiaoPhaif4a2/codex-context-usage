"""A request for Codex to write a factual handoff, not an invented summary."""


def handoff_prompt(session: dict | None, mode: str = "handoff") -> str:
    session = session or {}
    percent = session.get("percent")
    usage = f"最新日志读数约 {percent:.1f}%" if percent is not None else "当前读数未知"
    checkpoint = mode == "checkpoint"
    purpose = "留存阶段进度，仍在当前对话继续" if checkpoint else "准备在合适的暂停点交接到新对话"
    reasons = "；".join(session.get("advice", {}).get("reasons", []))
    return (
        f"这个对话{purpose}（{usage}，对话：{session.get('title') or '当前对话'}）。\n"
        + (f"参考信号：{reasons} 这些是经验提醒，不代表已检测到信息丢失。\n" if reasons else "")
        + "请根据本对话和项目实际状态，在项目根目录生成或更新 HANDOFF.md，保存可核验的工作状态。\n"
        "请包含：\n"
        "1. 原始目标、用户已确认的需求和约束；\n"
        "2. 已完成的工作、关键决策及原因；\n"
        "3. 修改的文件及用途；\n"
        "4. 已运行的验证、结果和未验证的部分；\n"
        "5. 剩余事项、阻塞问题和建议的下一步；\n"
        "6. 必需的启动命令、依赖和工作目录；\n"
        "7. 可以直接粘贴到新对话的接续提示词。\n"
        "以实际证据为准，不确定的内容明确标注，不记录密钥或凭证。"
        + ("完成后告诉我文档路径，然后留在当前对话继续；本次保存不要求新建对话。" if checkpoint else
           "完成后告诉我文档路径，列出需要我核对的交接要点；待我确认后，可在新对话中读取 HANDOFF.md 继续。")
    )


class AlertState:
    """Notify once per severity; re-arm after meaningful recovery/compaction."""

    def __init__(self):
        self.seen: dict[str, tuple[int, tuple[int, bool]]] = {}

    def should_alert(self, session: dict, warning: float) -> bool:
        identifier = session["id"]
        epoch = (session.get("compactions", 0), session.get("advice", {}).get("quality_issue", False))
        previous, old_epoch = self.seen.get(identifier, (0, epoch))
        if epoch != old_epoch:
            previous = 0
        percent = session.get("percent")
        # Capacity alone stays visible on the ring but never demands a handoff.
        level = session.get("advice", {}).get("rank", 0)
        if level < 2:
            if percent is not None and percent < warning - 3:
                previous = 0
            level = 0
        notify = level > previous
        self.seen[identifier] = (max(previous, level), epoch)
        return notify
