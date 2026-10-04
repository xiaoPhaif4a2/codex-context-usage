"""Regression: follow the viewed chat, not the launch chat or busiest rollout."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from context_indicator.desktop import Desktop
from context_indicator.monitor import Monitor
from context_indicator.navigation import NavigationLogs, NavigationFile
from context_indicator.catalog import chat_labels
from unittest.mock import Mock, patch

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


def navigation(route, window=1, stamp="2026-10-04T05:00:00.000Z"):
    # conversationId deliberately differs from the true viewed ownerRoutePath.
    return f"{stamp} info [electron-message-handler] IAB_LIFECYCLE received browser sidebar owner sync browserTabId=null conversationId=client-new-thread:other originWebContentsId=1 ownerRoutePath={route} windowId={window}\n"


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs = self.root / "Logs"
        self.logs.mkdir()
        self.log = self.logs / "codex-desktop-test-123-t0-i1-050000-0.log"
        self.log.write_bytes(b"")

    def append(self, text):
        with self.log.open("ab") as stream:
            stream.write(text.encode("utf-8"))

    def test_navigation_follows_page_and_ignores_backend_activity(self):
        reader = NavigationLogs([self.logs])
        self.append(navigation("/local/" + A))
        self.assertEqual(reader.snapshot(123)["thread_id"], A)
        self.append(f"2026-10-04T05:00:01.000Z info [AppServerConnection] response_routed conversationId={B} method=turn/start\n")
        self.assertEqual(reader.snapshot(123)["thread_id"], A)
        self.append(navigation("/local/" + B, stamp="2026-10-04T05:00:02.000Z"))
        self.assertEqual(reader.snapshot(123)["thread_id"], B)
        self.append(navigation("/", stamp="2026-10-04T05:00:03.000Z"))
        status = reader.snapshot(123)
        self.assertTrue(status["known"])
        self.assertIsNone(status["thread_id"])

    def test_partial_line_and_truncation(self):
        line = navigation("/local/" + A)
        self.append(line[:-1])
        reader = NavigationLogs([self.logs])
        self.assertFalse(reader.snapshot(123)["known"])
        self.append("\n")
        self.assertEqual(reader.snapshot(123)["thread_id"], A)
        self.log.write_bytes(b"{}\n")
        self.assertFalse(reader.snapshot(123)["known"])

    def test_process_identity_prevents_tracking_old_app(self):
        self.append(navigation("/local/" + A))
        self.assertFalse(NavigationLogs([self.logs]).snapshot(999)["known"])

    def test_multiple_windows_use_explicit_focus_or_stay_unknown(self):
        self.append(navigation("/local/" + A, window=1))
        self.append(navigation("/local/" + B, window=2))
        reader = NavigationLogs([self.logs])
        self.assertFalse(reader.snapshot(123)["known"])
        self.append("2026-10-04T05:00:03.000Z info [electron-message-handler] client.performance.span rendererWindowFocused=true rendererWindowId=1\n")
        self.assertEqual(reader.snapshot(123)["thread_id"], A)

    def test_new_draft_and_cloud_pages_do_not_reuse_context(self):
        reader = NavigationFile(self.log, 123)
        for route in ("/", "/local/client-new-thread:abc", "/chatgpt/cloud-chat"):
            reader.consume(navigation(route))
            self.assertIsNone(reader.routes[1]["thread_id"])

    def test_monitor_cross_project_switch_and_rename(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        db = self.root / "state_5.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE threads (id TEXT, name TEXT, title TEXT)")
        for identifier, title, used in ((A, "添加 Codex 上下文用量提醒", 300), (B, "继续讨论", 750)):
            project = self.root / identifier
            records = [{"type": "session_meta", "payload": {"id": identifier, "cwd": str(project), "source": "vscode"}}, {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"total_tokens": used}, "model_context_window": 1000}}}]
            (sessions / f"{identifier}.jsonl").write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
            conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (identifier, title, "raw prompt"))
        conn.commit()
        conn.close()
        monitor = Monitor(self.root, self.root, include_all=True, log_roots=[self.logs])
        self.append(navigation("/local/" + A))
        status = monitor.snapshot()
        app = SimpleNamespace(status=status)
        self.assertEqual(Desktop.current(app)["title"], "添加 Codex 上下文用量提醒")
        self.assertEqual(Desktop.current(app)["percent"], 30)
        self.append(navigation("/local/" + B, stamp="2026-10-04T05:00:02.000Z"))
        app.status = monitor.snapshot()
        self.assertEqual(Desktop.current(app)["title"], "继续讨论")
        self.assertEqual(Desktop.current(app)["percent"], 75)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE threads SET name=? WHERE id=?", ("重新命名后的对话", B))
        conn.commit()
        conn.close()
        app.status = monitor.snapshot()
        self.assertEqual(Desktop.current(app)["title"], "重新命名后的对话")

    def test_picker_shows_names_and_opens_chat_before_changing_usage(self):
        sessions = [{"id": A, "title": "添加 Codex 上下文用量提醒", "percent": 30}, {"id": B, "title": "继续讨论", "percent": 75}]
        labels = chat_labels(sessions)
        self.assertIn("继续讨论", labels[1])
        self.assertNotIn(B, str(labels))
        app = SimpleNamespace(selector=SimpleNamespace(current=lambda: 2), choices=[None, A, B], status={"sessions": sessions, "active_thread_id": A}, demo=False, feedback=Mock())
        with patch("context_indicator.desktop.os.startfile", create=True) as open_chat:
            Desktop.select_session(app, None)
        open_chat.assert_called_once_with("codex://threads/" + B)
        self.assertEqual(app.status["active_thread_id"], A)
        self.assertEqual(app.pending_id, B)

    def test_duplicate_names_still_have_readable_unique_labels(self):
        labels = chat_labels([{"id": A, "title": "继续讨论", "project": str(self.root), "percent": 30}, {"id": B, "title": "继续讨论", "project": str(self.root), "percent": 75}])
        self.assertNotEqual(labels[0], labels[1])
        self.assertTrue(all("继续讨论" in label for label in labels))
        self.assertNotIn(A, str(labels))


class TrackingRegressionTests(unittest.TestCase):
    def test_navigation_overrides_launch_thread(self):
        app = SimpleNamespace(selected_id="launch", status={"sessions": [{"id": "launch"}, {"id": "viewed"}], "active_thread_id": "viewed", "navigation_known": True})
        self.assertEqual(Desktop.current(app)["id"], "viewed")

    def test_home_never_displays_previous_chat_usage(self):
        app = SimpleNamespace(selected_id="launch", status={"sessions": [{"id": "launch"}], "active_thread_id": None, "navigation_known": True})
        self.assertIsNone(Desktop.current(app))

    def test_real_chat_name_is_exposed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sessions").mkdir()
            records = [{"type": "session_meta", "payload": {"id": "a", "cwd": str(root), "source": "vscode"}}]
            (root / "sessions" / "a.jsonl").write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
            with sqlite3.connect(root / "state_5.sqlite") as conn:
                conn.execute("CREATE TABLE threads (id TEXT, name TEXT, title TEXT)")
                conn.execute("INSERT INTO threads VALUES (?, ?, ?)", ("a", "添加 Codex 上下文用量提醒", "原始长消息"))
            conn.close()
            self.assertEqual(Monitor(root, root).snapshot()["sessions"][0].get("title"), "添加 Codex 上下文用量提醒")


if __name__ == "__main__":
    unittest.main()
