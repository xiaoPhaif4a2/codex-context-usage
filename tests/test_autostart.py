"""Autostart tests mock the registry and never change the runner's startup."""

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
            root = Path(directory) / "app x"
            root.mkdir()
            (root / "start.pyw").touch()
            (root / "python.exe").touch()
            (root / "pythonw.exe").touch()
            command = autostart.launch_command(root, source_root=root, executable=root / "python.exe")
            self.assertEqual(command, subprocess.list2cmdline([str(root / "pythonw.exe"), str(root / "start.pyw"), "--project", str(root), "--quiet-if-running"]))

    def test_missing_pythonw_or_launcher_cannot_register_a_broken_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                autostart.launch_command(root, source_root=root, executable=root / "python.exe")

    def test_excessive_command_length_is_rejected_before_registry_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ("x" * 100)
            root.mkdir()
            (root / "start.pyw").touch()
            (root / "pythonw.exe").touch()
            with self.assertRaisesRegex(ValueError, "260"):
                autostart.launch_command(root, source_root=root, executable=root / "pythonw.exe")

    def test_enable_writes_only_our_current_user_run_entry(self):
        registry = MagicMock()
        with patch.object(autostart, "winreg", registry), patch.object(autostart, "launch_command", return_value='"pythonw.exe" "start.pyw"'):
            autostart.configure(True, Path.cwd())
        registry.CreateKeyEx.assert_called_once_with(registry.HKEY_CURRENT_USER, autostart.RUN_KEY, 0, registry.KEY_SET_VALUE)
        registry.SetValueEx.assert_called_once_with(registry.CreateKeyEx.return_value.__enter__.return_value,
                                                  autostart.VALUE_NAME, 0, registry.REG_SZ, '"pythonw.exe" "start.pyw"')
        registry.DeleteValue.assert_not_called()

    def test_disable_is_idempotent_and_does_not_delete_other_startup_entries(self):
        registry = MagicMock()
        with patch.object(autostart, "winreg", registry):
            autostart.configure(False, Path.cwd())
        registry.DeleteValue.assert_called_once_with(registry.OpenKey.return_value.__enter__.return_value, autostart.VALUE_NAME)
        registry.OpenKey.side_effect = FileNotFoundError
        with patch.object(autostart, "winreg", registry):
            autostart.configure(False, Path.cwd())

    def test_startup_status_reads_only_our_value_and_handles_missing_entry(self):
        registry = MagicMock()
        registry.QueryValueEx.return_value = ("command", registry.REG_SZ)
        with patch.object(autostart, "winreg", registry):
            self.assertEqual(autostart.startup_command(), "command")
            registry.OpenKey.side_effect = FileNotFoundError
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
