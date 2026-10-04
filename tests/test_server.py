import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from context_indicator.__main__ import make_handler
from context_indicator.monitor import Monitor


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        self.monitor = Monitor(root, root)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.monitor, "abc"))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()

    def test_local_dashboard_and_status(self):
        code, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("上下文".encode(), body)
        code, body = self.request("/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["initial_thread"], "abc")

    def test_rebinding_cross_origin_and_file_paths_denied(self):
        for headers in ({"Host": "evil.test"}, {"Origin": "https://evil.test"}):
            self.assertEqual(self.request("/api/status", headers)[0], 403)
        self.assertEqual(self.request("/../monitor.py")[0], 404)

    def post(self, data, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        self.addCleanup(connection.close)
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        connection.request("POST", "/api/action", json.dumps(data), request_headers)
        response = connection.getresponse()
        return response.status, response.read()

    def viewed_chat(self):
        from context_indicator.navigation import NavigationLogs
        sessions = self.root / "sessions"
        sessions.mkdir()
        records = [{"type": "session_meta", "payload": {"id": "11111111-1111-4111-8111-111111111111", "cwd": str(self.root), "source": "vscode"}},
                   {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"total_tokens": 200}, "model_context_window": 1000}}}]
        (sessions / "chat.jsonl").write_text("\n".join(json.dumps(x) for x in records) + "\n")
        logs = self.root / "Logs"
        logs.mkdir()
        (logs / "codex-desktop-test-123-t0-i1-050000-0.log").write_text("2026-10-04T05:00:00Z info [electron-message-handler] IAB_LIFECYCLE received browser sidebar owner sync ownerRoutePath=/local/11111111-1111-4111-8111-111111111111 windowId=1\n")
        self.monitor.navigation = NavigationLogs([logs])
        self.monitor.last_scan = -float("inf")
        return "11111111-1111-4111-8111-111111111111"

    def test_manual_feedback_and_prompts_use_same_advice_as_native(self):
        identifier = self.viewed_chat()
        code, body = self.post({"action": "quality_issue", "thread_id": identifier})
        self.assertEqual(code, 200)
        status = json.loads(body)
        self.assertEqual(status["sessions"][0]["advice"]["code"], "reported_issue")
        self.assertIn("本次保存不要求新建对话", status["prompts"]["checkpoint"])
        self.assertTrue((self.root / ".context-indicator-advice.json").exists())
        self.assertEqual(self.post({"action": "clear_issue", "thread_id": identifier})[0], 200)
        self.assertFalse(self.monitor.snapshot()["sessions"][0]["advice"]["quality_issue"])

    def test_posts_cannot_mark_unviewed_chat_or_accept_cross_origin_form(self):
        self.viewed_chat()
        payload = {"action": "quality_issue", "thread_id": "other"}
        self.assertEqual(self.post(payload)[0], 400)
        self.assertEqual(self.post(payload, {"Origin": "https://evil.test"})[0], 403)
        self.assertEqual(self.post(payload, {"Host": "evil.test"})[0], 403)
        self.assertEqual(self.post(payload, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post({"action": "thresholds", "warning": True, "critical": 85})[0], 400)
        self.assertEqual(self.post({"action": "thresholds", "warning": 85, "critical": 70})[0], 400)

    def test_browser_threshold_changes_affect_shared_backend_advice(self):
        self.assertEqual(self.post({"action": "thresholds", "warning": 65, "critical": 80})[0], 200)
        self.assertEqual(self.monitor.warning, 65)
        self.assertEqual(self.monitor.critical, 80)


if __name__ == "__main__":
    unittest.main()
