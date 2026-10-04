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


def make_handler(monitor: Monitor, initial_thread: str | None):
    html = Path(__file__).with_name("dashboard.html").read_bytes()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Prevent DNS rebinding and cross-origin reads of local metadata.
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                self.send_error(403)
                return
            origin = self.headers.get("Origin")
            if origin and origin != f"http://127.0.0.1:{self.server.server_port}":
                self.send_error(403)
                return
            path = urlsplit(self.path).path
            if path == "/":
                content, mime = html, "text/html; charset=utf-8"
            elif path == "/api/status":
                with lock:
                    status = monitor.snapshot()
                status["initial_thread"] = initial_thread
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
    args = parser.parse_args()
    if not args.project.is_dir():
        parser.error("项目目录不存在")
    if not 0 <= args.port <= 65535:
        parser.error("端口必须介于 0 和 65535")
    try:
        monitor = Monitor(args.codex_home.expanduser(), args.project.resolve(), args.warning, args.critical, include_all=not args.project_only, log_roots=[args.desktop_logs] if args.desktop_logs else default_log_roots())
    except ValueError as error:
        parser.error(str(error))
    if not args.web:
        from .desktop import run_desktop
        run_desktop(monitor, args.thread_id, args.demo)
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
