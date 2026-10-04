"""Small ring + percentage badge attached visually to the Codex window footer."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, font
from urllib.parse import quote

from .handoff import AlertState, handoff_prompt
from .monitor import Monitor
from .windows import Windows
from .catalog import chat_labels

COLORS = {"normal": "#687480", "warning": "#ba790b", "critical": "#d14b44", "unknown": "#9199a2"}


class Desktop:
    def __init__(self, monitor: Monitor, initial_thread: str | None, demo=False):
        self.windows = Windows()
        if not demo and not self.windows.acquire_instance(monitor.project):
            raise RuntimeError("该项目的上下文指示器已在运行。请将 Codex 窗口置于前台查看。")
        self.monitor = monitor
        self.demo = demo
        self.settings_path = Path(monitor.project) / ".context-indicator.json"
        self.settings = self.load_settings()
        # Old saved / launch IDs must never pin the indicator to the wrong page.
        self.pending_id = None
        self.pending_started = 0.0
        self.display_key = None
        self.anchor_x = self.settings.get("anchor_x", 0.74)
        self.bottom = self.settings.get("bottom", 5)
        self.status = {"sessions": [], "errors": []}
        self.alerts = AlertState()
        self.readings = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Codex 上下文指示器")
        self.root.overrideredirect(True)
        self.root.configure(bg="#f1f3f5")
        self.root.attributes("-topmost", True)
        self.badge_width = 320
        self.badge_font = font.Font(root=self.root, family="Microsoft YaHei UI", size=9)
        self.canvas = tk.Canvas(self.root, width=self.badge_width, height=26, bg="#f1f3f5", highlightthickness=0, cursor="hand2")
        self.canvas.pack()
        self.windows.floating_style(self.root)
        self.details = None
        self.tip = None
        self.toast = None
        self.toast_timer = None
        self.tip_timer = None
        self.last_rect = None
        self.drag = None
        self.canvas.bind("<Enter>", self.enter)
        self.canvas.bind("<Leave>", lambda _: self.hide_tip())
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.move)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.canvas.bind("<Button-3>", self.menu)
        self.draw()
        if demo:
            self.status = {"sessions": [{"id": "demo", "title": "添加 Codex 上下文用量提醒", "project": monitor.project, "model": "预览示例", "percent": 74.0, "used_tokens": 191216, "context_window": 258400, "level": "warning", "compactions": 0, "updated_at": "示例数据"}], "errors": [], "active_thread_id": "demo", "navigation_known": True}
            self.draw()
            self.root.deiconify()
            self.root.geometry("320x26+440+710")
            self.show_details()
        else:
            threading.Thread(target=self.read_loop, daemon=True).start()
        self.root.after(300, self.tick)

    def load_settings(self):
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                return {}
            # Treat settings as data and validate before using native coordinates.
            x, bottom = settings.get("anchor_x", 0.74), settings.get("bottom", 5)
            if not isinstance(x, (int, float)) or not 0 <= x <= 1:
                settings["anchor_x"] = 0.74
            if not isinstance(bottom, (int, float)) or not 0 <= bottom <= 2000:
                settings["bottom"] = 5
            w, c = settings.get("warning", self.monitor.warning), settings.get("critical", self.monitor.critical)
            if isinstance(w, (int, float)) and isinstance(c, (int, float)) and 0 < w < c <= 100:
                self.monitor.warning, self.monitor.critical = w, c
            return settings
        except (OSError, ValueError):
            return {}

    def save_settings(self):
        if self.demo:
            return
        data = {"anchor_x": self.anchor_x, "bottom": self.bottom, "warning": self.monitor.warning, "critical": self.monitor.critical}
        try:
            self.settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            if self.details:
                self.feedback.set("设置无法保存，下次启动将恢复默认值。")

    def read_loop(self):
        while not self.stop.is_set():
            try:
                result = self.monitor.snapshot()
            except Exception:
                result = {"sessions": [], "errors": ["读取日志失败，请检查 Codex 会话目录。"]}
            try:
                self.readings.get_nowait()
            except queue.Empty:
                pass
            self.readings.put(result)
            self.stop.wait(0.5)

    def current(self):
        sessions = self.status["sessions"]
        identifier = self.status.get("active_thread_id") if self.status.get("navigation_known") else None
        return next((s for s in sessions if s["id"] == identifier), None) if identifier else None

    def draw(self):
        session = self.current()
        percent = session.get("percent") if session else None
        color = COLORS[session["level"]] if session else COLORS["unknown"]
        self.canvas.delete("all")
        self.canvas.create_oval(9, 6, 23, 20, outline="#d7dce1", width=2)
        if percent is not None and percent > 0:
            self.canvas.create_arc(9, 6, 23, 20, start=90, extent=-min(percent, 99.99) * 3.6, style="arc", outline=color, width=2)
        self.canvas.create_text(32, 13, anchor="w", text=f"{percent:.0f}%" if percent is not None else "—%", fill=color, font=("Segoe UI", 10))
        title = session.get("title") if session else self.status.get("active_title")
        if not title:
            title = "未打开 Codex 对话" if self.status.get("navigation_known") and not self.status.get("active_thread_id") else "正在识别当前对话"
        short_title = title
        while self.badge_font.measure(short_title) > self.badge_width - 100 and len(short_title) > 1:
            short_title = short_title[:-2] + "…"
        self.canvas.create_text(78, 13, anchor="w", text=short_title, fill=color, font=self.badge_font)
        self.canvas.create_text(self.badge_width - 10, 13, text="⌄", fill=color, font=("Segoe UI", 9))

    def tick(self):
        try:
            self.status = self.readings.get_nowait()
            key = (self.status.get("active_thread_id"), self.status.get("navigation_known"), self.status.get("active_title"))
            if key != self.display_key:
                self.hide_tip()
                self.hide_toast()
                self.display_key = key
            self.draw()
            self.refresh_details()
        except queue.Empty:
            pass
        rect = None if self.demo else self.windows.active_rect()
        if rect:
            self.monitor.app_pid = self.windows.target_pid
            self.last_rect = rect
            left, top, width, height = rect
            x = left + max(0, min(width - self.badge_width, int(width * self.anchor_x)))
            y = top + max(0, height - 26 - int(self.bottom))
            self.root.deiconify()
            self.windows.place(self.root, x, y, self.badge_width, 26)
            session = self.current()
            if session and self.alerts.should_alert(session, self.monitor.warning):
                self.show_toast(session)
        elif not self.demo:
            self.root.withdraw()
            self.hide_tip()
            # An owned dialog should not cover unrelated apps either.
            if self.toast:
                self.toast.withdraw()
        if rect and self.toast:
            self.toast.deiconify()
            self.position_popup(self.toast, 380, 94)
        self.root.after(300, self.tick)

    def position_popup(self, popup, width, height):
        self.root.update_idletasks()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if self.last_rect:
            left, top, client_width, _ = self.last_rect
            x = max(left, min(x + self.badge_width - width, left + client_width - width))
            y = max(top, y - height - 8)
        else:
            y -= height + 8
        popup.geometry(f"{width}x{height}{x:+d}{y:+d}")

    def enter(self, event):
        self.tip_timer = self.root.after(450, self.show_tip)

    def hide_tip(self):
        if self.tip_timer:
            self.root.after_cancel(self.tip_timer)
            self.tip_timer = None
        if self.tip:
            self.tip.destroy()
            self.tip = None

    def show_tip(self):
        self.hide_tip()
        session = self.current()
        percent = session.get("percent") if session else None
        title = f"上下文已用 {percent:.1f}%" if percent is not None else "等待上下文读数"
        text = title + "\n最新日志读数 · 点击详情 · 拖动调整位置"
        if session:
            text += "\n当前对话：" + (session.get("title") or "未命名对话")
        else:
            text += "\n尚未获得当前对话的有效用量"
        self.tip = tk.Toplevel(self.root)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        tk.Label(self.tip, text=text, bg="#25282d", fg="white", justify="left", padx=12, pady=8, font=("Microsoft YaHei UI", 9)).pack(fill="both", expand=True)
        self.windows.floating_style(self.tip)
        self.position_popup(self.tip, 380, 104)

    def show_toast(self, session):
        self.hide_toast()
        self.toast = tk.Toplevel(self.root)
        self.toast.overrideredirect(True)
        self.toast.attributes("-topmost", True)
        title = session.get("title") or "未命名对话"
        text = f"{title[:28]}\n上下文已用 {session['percent']:.1f}% · 建议准备交接\n点击指示器，复制生成 HANDOFF.md 的提示词。"
        tk.Label(self.toast, text=text, bg="#fff8e8", fg="#725009", justify="left", padx=12, pady=12, font=("Microsoft YaHei UI", 9)).pack(fill="both", expand=True)
        self.windows.floating_style(self.toast)
        self.position_popup(self.toast, 380, 94)
        self.toast_timer = self.root.after(12000, self.hide_toast)

    def hide_toast(self):
        if self.toast_timer:
            self.root.after_cancel(self.toast_timer)
            self.toast_timer = None
        if self.toast:
            self.toast.destroy()
            self.toast = None

    def press(self, event):
        self.hide_tip()
        self.drag = (event.x_root, event.y_root, False)

    def move(self, event):
        if not self.drag or not self.last_rect:
            return
        start_x, start_y, moved = self.drag
        if abs(event.x_root - start_x) + abs(event.y_root - start_y) < 4 and not moved:
            return
        left, top, width, height = self.last_rect
        self.anchor_x = max(0, min(1, (event.x_root - left - 40) / max(width, 1)))
        self.bottom = max(0, min(2000, top + height - event.y_root - 13))
        self.drag = (start_x, start_y, True)

    def release(self, event):
        if self.drag and self.drag[2]:
            self.save_settings()
        else:
            self.show_details()
        self.drag = None

    def menu(self, event):
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="详情 / 切换对话", command=self.show_details)
        menu.add_command(label="复制交接提示词", command=self.copy_prompt)
        menu.add_command(label="恢复底部位置", command=self.reset_position)
        menu.add_separator()
        menu.add_command(label="退出指示器", command=self.close)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def reset_position(self):
        self.anchor_x, self.bottom = 0.74, 5
        self.save_settings()

    def show_details(self):
        self.hide_tip()
        self.hide_toast()
        if self.details and self.details.winfo_exists():
            self.details.deiconify()
            self.details.lift()
            return
        self.details = tk.Toplevel(self.root)
        self.details.title("上下文使用情况")
        self.details.attributes("-topmost", True)
        self.details.geometry("570x520")
        if self.demo:
            self.details.geometry("570x520+200+160")
        self.details.minsize(520, 480)
        self.details.protocol("WM_DELETE_WINDOW", self.close_details)
        frame = ttk.Frame(self.details, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Codex 上下文", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(frame, text="自动跟随 Codex 当前对话 · 最新日志读数", foreground="#727a84").pack(anchor="w", pady=(4, 15))
        self.session_var = tk.StringVar()
        self.selector = ttk.Combobox(frame, textvariable=self.session_var, state="readonly")
        self.selector.pack(fill="x")
        self.selector.bind("<<ComboboxSelected>>", self.select_session)
        self.description = tk.StringVar()
        ttk.Label(frame, textvariable=self.description, justify="left", wraplength=510).pack(anchor="w", pady=15)
        self.progress = ttk.Progressbar(frame, maximum=100)
        self.progress.pack(fill="x", pady=(0, 16))
        limits = ttk.Frame(frame)
        limits.pack(fill="x")
        ttk.Label(limits, text="提醒 %").pack(side="left")
        self.warning_var = tk.StringVar(value=str(self.monitor.warning))
        ttk.Entry(limits, textvariable=self.warning_var, width=6).pack(side="left", padx=(5, 15))
        ttk.Label(limits, text="强提醒 %").pack(side="left")
        self.critical_var = tk.StringVar(value=str(self.monitor.critical))
        ttk.Entry(limits, textvariable=self.critical_var, width=6).pack(side="left", padx=5)
        ttk.Button(limits, text="保存", command=self.set_thresholds).pack(side="left", padx=8)
        ttk.Button(frame, text="复制提示词：请 Codex 生成交接文档", command=self.copy_prompt).pack(fill="x", pady=(20, 8))
        ttk.Label(frame, text="粘贴到当前对话并发送 → 确认 HANDOFF.md 已生成 →\n新建对话，输入“请读取 HANDOFF.md 并继续未完成的工作”。", justify="left").pack(anchor="w")
        self.feedback = tk.StringVar()
        ttk.Label(frame, textvariable=self.feedback, foreground="#ba790b", wraplength=510).pack(anchor="w", pady=10)
        ttk.Label(frame, text="切换 Codex 对话时自动更新；下拉选择会同步打开对应对话。\n右键指示器可退出；拖动圆环可调整位置。", foreground="#727a84", justify="left").pack(anchor="w", side="bottom")
        self.refresh_details()

    def refresh_details(self):
        if not self.details:
            return
        sessions = self.status["sessions"]
        current_id = self.status.get("active_thread_id")
        popup = self.selector.tk.call("ttk::combobox::PopdownWindow", str(self.selector))
        # Keep names and their ID mapping stable while the user chooses a row.
        if not self.selector.tk.call("winfo", "ismapped", popup):
            self.choices = [None] + [s["id"] for s in sessions]
            self.selector["values"] = ["自动跟随当前对话"] + chat_labels(sessions)
            if current_id in self.choices:
                self.selector.current(self.choices.index(current_id))
            else:
                self.session_var.set(self.status.get("active_title") or "正在识别当前对话")
        session = self.current()
        if session:
            percent = session.get("percent")
            usage = f"{percent:.1f}%  ·  {session['used_tokens']:,} / {session['context_window']:,} tokens" if percent is not None else "等待有效读数（压缩/模型切换后需新的 token 事件）" if session.get("awaiting_usage") else "日志尚未提供有效用量和窗口大小"
            stamp = session.get("updated_at") or "暂无"
            if stamp != "示例数据":
                try:
                    from datetime import datetime
                    stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M:%S")
                except ValueError:
                    pass
            self.description.set(f"当前对话：{session.get('title') or '未命名对话'}\n{usage}\n模型：{session.get('model') or '未知'}\n读数时间：{stamp}\n最近调用输入 + 输出 / 日志报告的窗口；包含缓存输入。")
            self.progress["value"] = min(percent or 0, 100)
        else:
            title = self.status.get("active_title")
            self.description.set(f"当前对话：{title}\n尚未获得这个对话的本地上下文读数。" if title else "尚未识别当前 Codex 对话。请在 Codex 中打开一个对话。\n首页、新建空对话或缺少导航日志时显示未知。")
            self.progress["value"] = 0
        errors = self.status.get("errors", [])
        if errors:
            self.feedback.set("；".join(errors))
        if self.pending_id:
            if current_id == self.pending_id:
                self.pending_id = None
                self.feedback.set("已切换，正在跟随当前对话。")
            elif time.monotonic() - self.pending_started > 8:
                self.pending_id = None
                self.feedback.set("尚未收到页面切换信号，请在 Codex 中打开目标对话。")

    def select_session(self, event):
        index = self.selector.current()
        if index < 0:
            return
        identifier = self.choices[index]
        if identifier is None or identifier == self.status.get("active_thread_id"):
            self.refresh_details()
            return
        if self.demo:
            self.status["active_thread_id"] = identifier
            self.draw()
            self.refresh_details()
            return
        session = next((s for s in self.status["sessions"] if s["id"] == identifier), None)
        if session is None:
            self.feedback.set("这个对话暂不可读，请重新选择。")
            return
        title = session["title"]
        try:
            os.startfile("codex://threads/" + quote(identifier, safe=""))
            self.pending_id = identifier
            self.pending_started = time.monotonic()
            self.feedback.set(f"正在切换到“{title}”…")
        except OSError:
            self.feedback.set("无法打开 Codex 对话链接，请在 Codex 侧栏选择对应名称。")
            self.refresh_details()

    def set_thresholds(self):
        try:
            w, c = float(self.warning_var.get()), float(self.critical_var.get())
            if not 0 < w < c <= 100:
                raise ValueError
        except ValueError:
            self.feedback.set("请输入 0 < 提醒阈值 < 强提醒阈值 ≤ 100。")
            return
        self.monitor.warning, self.monitor.critical = w, c
        self.alerts = AlertState()
        self.save_settings()
        self.feedback.set("阈值已保存。")

    def copy_prompt(self):
        if self.current() is None:
            self.show_details()
            self.feedback.set("尚未识别当前对话，请先在 Codex 中打开一个本地对话。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(handoff_prompt(self.current()))
        self.root.update_idletasks()
        self.show_details()
        self.feedback.set(f"提示词已复制。请粘贴到“{self.current().get('title') or '当前对话'}”并发送。")

    def close_details(self):
        if self.details:
            self.details.destroy()
            self.details = None

    def close(self):
        self.stop.set()
        self.windows.release_instance()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def run_desktop(monitor: Monitor, thread: str | None, demo=False):
    if os.name != "nt":
        raise RuntimeError("底部浮层当前支持 Windows；其他系统请使用 --web。")
    Desktop(monitor, thread, demo).run()
