"""Run the Windows footer badge or an optional loopback-only dashboard."""

from __future__ import annotations

import argparse
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .monitor import Monitor
from .navigation import default_log_roots
from .handoff import handoff_prompt


def make_handler(monitor: Monitor, initial_thread: str | None):
    html = Path(__file__).with_name("dashboard.html").read_bytes()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def local_request(self):
            # Prevent DNS rebinding and cross-origin reads of local metadata.
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                self.send_error(403)
                return False
            origin = self.headers.get("Origin")
            if origin and origin != f"http://127.0.0.1:{self.server.server_port}":
                self.send_error(403)
                return False
            return True

        def status(self):
            status = monitor.snapshot()
            status["initial_thread"] = initial_thread
            active = next((s for s in status["sessions"] if s["id"] == status["active_thread_id"]), None) if status["navigation_known"] else None
            status["prompts"] = {mode: handoff_prompt(active, mode) for mode in ("checkpoint", "handoff")} if active else {}
            return status

        def json_response(self, data):
            content = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            if not self.local_request():
                return
            if urlsplit(self.path).path != "/api/action":
                self.send_error(404)
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.send_error(415)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048:
                    self.send_error(413)
                    return
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError
                with lock:
                    if data.get("action") == "thresholds":
                        w, c = data.get("warning"), data.get("critical")
                        if type(w) not in (int, float) or type(c) not in (int, float):
                            raise ValueError
                        monitor.set_thresholds(w, c)
                    else:
                        monitor.record_action(data.get("thread_id"), data.get("action"))
                    status = self.status()
                self.json_response(status)
            except (ValueError, TypeError, UnicodeDecodeError):
                self.send_error(400, "Invalid action or inactive chat")
            except OSError:
                self.send_error(500, "Unable to save local advice settings")

        def do_GET(self):
            if not self.local_request():
                return
            path = urlsplit(self.path).path
            if path == "/":
                content, mime = html, "text/html; charset=utf-8"
            elif path == "/api/status":
                with lock:
                    status = self.status()
                content = json.dumps(status, ensure_ascii=False).encode("utf-8")
                mime = "application/json; charset=utf-8"
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Codex 上下文使用指示器（本地日志）")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="监控项目目录，默认当前目录")
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--thread-id", help=argparse.SUPPRESS)  # Legacy launch commands: navigation always wins.
    parser.add_argument("--project-only", action="store_true", help="仅列出指定项目的对话，默认支持所有项目")
    parser.add_argument("--desktop-logs", type=Path, help="覆盖自动发现的 Codex 桌面日志目录")
    parser.add_argument("--port", type=int, default=8765, help="本地端口；0 表示自动分配")
    parser.add_argument("--warning", type=float, default=70)
    parser.add_argument("--critical", type=float, default=85)
    parser.add_argument("--open", action="store_true", help="启动后在默认浏览器打开")
    parser.add_argument("--web", action="store_true", help="使用浏览器面板，默认使用 Windows 底部浮层")
    parser.add_argument("--demo", action="store_true", help="显示标注为示例的浮层预览，不保存设置")
    startup = parser.add_mutually_exclusive_group()
    startup.add_argument("--enable-autostart", action="store_true", help="启用当前用户 Windows 登录自启动")
    startup.add_argument("--disable-autostart", action="store_true", help="关闭 Windows 登录自启动")
    startup.add_argument("--autostart-status", action="store_true", help="查看登录自启动状态")
    parser.add_argument("--quiet-if-running", action="store_true", help="重复启动时静默退出，供登录自启动使用")
    parser.add_argument("--managed-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--managed-follower", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.project.is_dir():
        parser.error("项目目录不存在")
    if args.enable_autostart or args.disable_autostart or args.autostart_status:
        from . import autostart
        try:
            if args.autostart_status:
                command = autostart.startup_command()
                print("登录自启动：未启用" if not command else
                      "登录自启动：已启用（当前目录）" if command == autostart.launch_command(args.project) else
                      "登录自启动：已配置，启动路径与当前目录不同")
            else:
                autostart.configure(args.enable_autostart, args.project)
                print("已启用登录时启动轻量监听；指示器随 Codex 窗口启动和关闭。" if args.enable_autostart else "已关闭轻量监听的登录自启动。")
        except (OSError, ValueError, RuntimeError) as error:
            parser.error(str(error))
        return
    if not 0 <= args.port <= 65535:
        parser.error("端口必须介于 0 和 65535")
    if not args.web and not args.demo and not args.managed_child:
        if not 0 < args.warning < args.critical <= 100:
            parser.error("阈值必须满足 0 < 关注容量 < 高占用 <= 100")
        from .watcher import dispatch_follower, run_follower
        from .monitor import normalized_path
        child_args = ["--project", str(args.project.resolve()), "--codex-home", str(args.codex_home.expanduser()),
                      "--warning", str(args.warning), "--critical", str(args.critical)]
        if args.project_only:
            child_args.append("--project-only")
        if args.desktop_logs:
            child_args.extend(["--desktop-logs", str(args.desktop_logs.resolve())])
        project_key = normalized_path(str(args.project.resolve()))
        if args.managed_follower:
            run_follower(project_key, child_args, args.quiet_if_running)
        else:
            dispatch_follower(project_key, child_args, args.quiet_if_running)
        return
    try:
        monitor = Monitor(args.codex_home.expanduser(), args.project.resolve(), args.warning, args.critical, include_all=not args.project_only, log_roots=[args.desktop_logs] if args.desktop_logs else default_log_roots())
    except ValueError as error:
        parser.error(str(error))
    if not args.web:
        from .desktop import run_desktop
        run_desktop(monitor, args.thread_id, args.demo, args.quiet_if_running, args.managed_child)
        return
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(monitor, args.thread_id))
    except OSError as error:
        parser.error(f"无法启动本地服务：{error}。可用 --port 0 自动分配端口。")
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Codex 上下文指示器：{url}\n监控项目：{args.project.resolve()}\n按 Ctrl+C 停止。", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
