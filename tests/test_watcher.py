"""Lifecycle tests simulate Codex windows; never close the user's Codex."""

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from context_indicator.watcher import Follower
from context_indicator.desktop import Desktop
from context_indicator.windows import Windows


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.windows = SimpleNamespace(instance_running=Mock(return_value=False))
        self.child = Mock()
        self.child.poll.return_value = None
        self.launch = Mock(return_value=self.child)
        self.now = 0
        self.follower = Follower(["pythonw.exe", "child"], self.temp.name, self.windows,
                                 launcher=self.launch, clock=lambda: self.now)

    def test_no_codex_means_no_indicator_or_usage_reader_started(self):
        for _ in range(20):
            self.follower.tick(False)
        self.launch.assert_not_called()
        self.assertIsNone(self.follower.child)
        self.assertFalse((Path(self.temp.name) / ".context-indicator-error.log").exists())

    def test_open_launches_once_close_stops_child_and_reopen_launches_again(self):
        self.follower.tick(True)
        for _ in range(20):
            self.follower.tick(True)
        self.assertEqual(self.launch.call_count, 1)
        self.follower.tick(False)
        self.child.terminate.assert_called_once()
        self.child.wait.assert_called_once_with(timeout=3)
        self.assertIsNone(self.follower.child)
        self.follower.tick(True)
        self.assertEqual(self.launch.call_count, 2)

    def test_existing_indicator_is_not_duplicated_or_terminated_as_our_child(self):
        self.windows.instance_running.return_value = True
        self.follower.tick(True)
        self.follower.tick(False)
        self.launch.assert_not_called()
        self.child.terminate.assert_not_called()

    def test_failed_child_has_backoff_instead_of_restarting_every_poll(self):
        self.follower.tick(True)
        self.child.poll.return_value = 1
        self.follower.tick(True)
        for _ in range(10):
            self.follower.tick(True)
        self.assertEqual(self.launch.call_count, 1)
        self.now = 2
        self.follower.tick(True)
        self.assertEqual(self.launch.call_count, 2)

    def test_unresponsive_owned_child_is_killed_on_codex_close(self):
        self.follower.tick(True)
        self.child.wait.side_effect = [subprocess.TimeoutExpired("child", 3), None]
        self.follower.tick(False)
        self.child.kill.assert_called_once()
        self.assertIsNone(self.follower.child)

    def test_window_detection_includes_minimized_window_but_excludes_cli_and_owned_popups(self):
        windows = object.__new__(Windows)
        windows.enum_type = lambda callback: callback
        windows.user = SimpleNamespace(
            IsWindowVisible=lambda hwnd: hwnd != 4,
            GetWindow=lambda hwnd, kind: 10 if hwnd == 3 else 0,
            GetWindowTextLengthW=lambda hwnd: 0 if hwnd == 5 else 10,
            EnumWindows=lambda callback, data: all(callback(hwnd, data) for hwnd in range(1, 6)),
        )
        windows.process = lambda hwnd: (hwnd, "chatgpt.exe" if hwnd != 2 else "codex-cli.exe")
        self.assertEqual(windows.codex_windows(), {1})

    def test_managed_indicator_exits_if_watcher_crashes_and_codex_closes(self):
        indicator = object.__new__(Desktop)
        indicator.managed = True
        indicator.windows = SimpleNamespace(codex_windows=Mock(return_value=set()))
        indicator.no_window_since = time.monotonic() - 2
        indicator.close = Mock()
        indicator.tick()
        indicator.close.assert_called_once_with()

    def test_launcher_uses_task_scheduler_to_escape_codex_process_job(self):
        from context_indicator.watcher import dispatch_follower

        with patch("context_indicator.watcher.Windows") as windows_type, \
             patch("context_indicator.watcher.subprocess.run") as run, \
             patch("context_indicator.watcher.time.sleep"):
            windows_type.return_value.instance_running.side_effect = [False, True]
            dispatch_follower(self.temp.name, ["--project", self.temp.name], False)
        self.assertEqual(run.call_count, 2)
        launch_env = run.call_args_list[0].kwargs["env"]
        self.assertIn("--managed-follower", launch_env["CCI_ARGUMENTS"])
        self.assertIn(self.temp.name, launch_env["CCI_ARGUMENTS"])
        self.assertEqual(run.call_args_list[0].kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
