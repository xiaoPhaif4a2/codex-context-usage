"""Autostart tests mock Task Scheduler and the legacy registry entry."""

import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from context_indicator import autostart
from context_indicator.desktop import AlreadyRunning, run_desktop


class AutostartTests(unittest.TestCase):
    def test_launcher_quotes_spaces_and_uses_pythonw_with_silent_duplicate_handling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = (Path(directory) / "app x").resolve()
            root.mkdir()
            (root / "start.pyw").touch()
            (root / "python.exe").touch()
            (root / "pythonw.exe").touch()
            command = autostart.launch_command(root, source_root=root, executable=root / "python.exe")
            self.assertEqual(command, subprocess.list2cmdline([str(root / "pythonw.exe"), str(root / "start.pyw"), "--managed-follower", "--project", str(root), "--quiet-if-running"]))

    def test_missing_pythonw_or_launcher_cannot_register_a_broken_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                autostart.launch_command(root, source_root=root, executable=root / "python.exe")

    def test_enable_registers_restarting_logon_task_and_removes_legacy_run_entry(self):
        registry = MagicMock()
        with patch.object(autostart, "winreg", registry), patch.object(autostart, "_launch_parts", return_value=(Path("pythonw.exe"), ["start.pyw", "--managed-follower"], Path.cwd())), patch.object(autostart.subprocess, "run") as run:
            autostart.configure(True, Path.cwd())
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", run.call_args.args[0][-1])
        self.assertIn("-RestartCount 3", run.call_args.args[0][-1])
        self.assertIn("--managed-follower", run.call_args.kwargs["env"]["CCI_ARGUMENTS"])
        registry.DeleteValue.assert_called_once()

    def test_disable_removes_task_and_legacy_entry_idempotently(self):
        registry = MagicMock()
        with patch.object(autostart, "winreg", registry), patch.object(autostart.subprocess, "run") as run:
            autostart.configure(False, Path.cwd())
        self.assertIn("Unregister-ScheduledTask", run.call_args.args[0][-1])
        registry.DeleteValue.assert_called_once_with(registry.OpenKey.return_value.__enter__.return_value, autostart.VALUE_NAME)
        registry.OpenKey.side_effect = FileNotFoundError
        with patch.object(autostart, "winreg", registry), patch.object(autostart.subprocess, "run"):
            autostart.configure(False, Path.cwd())

    def test_startup_status_reads_task_action(self):
        action = {"Execute": "pythonw.exe", "Arguments": '"start.pyw" --managed-follower'}
        encoded = base64.b64encode(json.dumps(action).encode()).decode()
        with patch.object(autostart.subprocess, "run", return_value=MagicMock(stdout=encoded)):
            self.assertEqual(autostart.startup_command(), 'pythonw.exe "start.pyw" --managed-follower')
        with patch.object(autostart.subprocess, "run", return_value=MagicMock(stdout="")):
            self.assertIsNone(autostart.startup_command())

    def test_duplicate_logon_start_is_silent_but_other_failures_are_visible(self):
        with patch("context_indicator.desktop.os.name", "nt"), patch("context_indicator.desktop.Desktop", side_effect=AlreadyRunning):
            run_desktop(None, None, quiet_if_running=True)
            with self.assertRaises(AlreadyRunning):
                run_desktop(None, None)
        with patch("context_indicator.desktop.os.name", "nt"), patch("context_indicator.desktop.Desktop", side_effect=RuntimeError("other")):
            with self.assertRaises(RuntimeError):
                run_desktop(None, None, quiet_if_running=True)


if __name__ == "__main__":
    unittest.main()
