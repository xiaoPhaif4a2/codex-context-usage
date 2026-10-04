import json
import tempfile
import unittest
from pathlib import Path

from context_indicator.handoff import AlertState, handoff_prompt
from context_indicator.monitor import Monitor, Rollout


def event(used=700, window=1000, total=900000):
    return {"type": "event_msg", "timestamp": "2026-10-04T05:00:00Z", "payload": {"type": "token_count", "info": {"last_token_usage": {"total_tokens": used, "input_tokens": used - 20, "cached_input_tokens": 600, "output_tokens": 20}, "total_token_usage": {"total_tokens": total}, "model_context_window": window}}}


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "log.jsonl"
        self.path.write_bytes(b"")
        self.rollout = Rollout(self.path)

    def append(self, *records):
        with self.path.open("ab") as f:
            for record in records:
                f.write(json.dumps(record).encode() + b"\n")
        self.rollout.refresh()

    def summary(self):
        return self.rollout.summary(70, 85)

    def test_last_usage_includes_cached_input_but_not_cumulative(self):
        self.append(event())
        self.assertEqual(self.summary()["percent"], 70)
        self.assertEqual(self.summary()["level"], "warning")
        self.append(event(850))
        self.assertEqual(self.summary()["level"], "critical")

    def test_invalid_measurements_remain_unknown(self):
        for used, window in ((700, 0), (None, 1000), (700, None), (-1, 1000), (True, 1000), (700, "1000")):
            self.append(event(used if isinstance(used, int) else 700, window))
            if used is None:
                record = event(); record["payload"]["info"]["last_token_usage"].pop("total_tokens")
                self.append(record)
            self.assertIsNone(self.summary()["percent"])

    def test_partial_record_retried_after_newline(self):
        raw = json.dumps(event()).encode()
        self.path.write_bytes(raw[:45])
        self.rollout.refresh()
        self.assertEqual(self.rollout.offset, 0)
        with self.path.open("ab") as f:
            f.write(raw[45:] + b"\n")
        self.rollout.refresh()
        self.assertEqual(self.summary()["percent"], 70)

    def test_corrupt_line_and_unknown_record_do_not_block(self):
        self.path.write_bytes(b"not json\n[]\n")
        self.append({"type": "future", "payload": {}}, event())
        self.assertEqual(self.summary()["percent"], 70)
        self.assertEqual(self.rollout.invalid_records, 1)

    def test_compaction_invalidates_old_measurement(self):
        self.append(event(), {"type": "compacted", "payload": {"message": "summary"}})
        self.assertIsNone(self.summary()["percent"])
        self.assertEqual(self.summary()["compactions"], 1)
        self.append(event(100))
        self.assertEqual(self.summary()["percent"], 10)

    def test_model_switch_waits_for_new_measurement(self):
        self.append({"type": "turn_context", "payload": {"model": "a"}}, event())
        self.append({"type": "turn_context", "payload": {"model": "b"}})
        self.assertIsNone(self.summary()["percent"])
        self.append(event(100, 2000))
        self.assertEqual(self.summary()["percent"], 5)

    def test_null_rate_limit_info_keeps_last_reading(self):
        self.append(event(), {"type": "event_msg", "payload": {"type": "token_count", "info": None}})
        self.assertEqual(self.summary()["percent"], 70)

    def test_file_truncation_resets_measurement(self):
        self.append(event())
        self.path.write_bytes(b"{}\n")
        self.rollout.refresh()
        self.assertIsNone(self.summary()["percent"])

    def test_over_capacity_not_hidden(self):
        self.append(event(1100))
        self.assertEqual(self.summary()["percent"], 110)
        self.assertEqual(self.summary()["remaining_tokens"], 0)

    def test_filters_project_and_subagents(self):
        directory = self.root / "sessions"
        directory.mkdir()
        for identifier, project, source in (("main", str(self.root), "vscode"), ("other", str(self.root / "elsewhere"), "vscode"), ("child", str(self.root), {"subagent": {}})):
            records = [{"type": "session_meta", "payload": {"id": identifier, "cwd": project, "source": source, "instructions": "PRIVATE"}}, event()]
            (directory / f"{identifier}.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        result = Monitor(self.root, self.root).snapshot()
        self.assertEqual([s["id"] for s in result["sessions"]], ["main"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_missing_directory_and_bad_thresholds(self):
        result = Monitor(self.root, self.root).snapshot()
        self.assertFalse(result["source_available"])
        self.assertEqual(result["sessions"], [])
        for warning, critical in ((0, 85), (85, 70), (70, 101), (float("nan"), 85)):
            with self.assertRaises(ValueError):
                Monitor(self.root, self.root, warning, critical)

    def test_log_timestamp_beats_deferred_windows_mtime(self):
        directory = self.root / "sessions"
        directory.mkdir()
        for identifier, stamp in (("old", "2026-10-03T05:00:00Z"), ("new", "2026-10-04T05:00:00Z")):
            record = event()
            record["timestamp"] = stamp
            metadata = {"type": "session_meta", "payload": {"id": identifier, "cwd": str(self.root), "source": "vscode"}}
            (directory / f"{identifier}.jsonl").write_text(json.dumps(metadata) + "\n" + json.dumps(record) + "\n", encoding="utf-8")
        import os
        os.utime(directory / "old.jsonl", (2000000000, 2000000000))
        self.assertEqual(Monitor(self.root, self.root).snapshot()["sessions"][0]["id"], "new")


class AlertTests(unittest.TestCase):
    def test_no_repeated_alert_and_severity_upgrade(self):
        alerts = AlertState()
        session = {"id": "a", "percent": 70, "level": "warning"}
        self.assertTrue(alerts.should_alert(session, 70))
        self.assertFalse(alerts.should_alert(session, 70))
        session.update(percent=85, level="critical")
        self.assertTrue(alerts.should_alert(session, 70))
        self.assertFalse(alerts.should_alert(session, 70))

    def test_hysteresis_and_compaction_rearm(self):
        alerts = AlertState()
        session = {"id": "a", "percent": 70, "level": "warning"}
        alerts.should_alert(session, 70)
        session.update(percent=69, level="normal")
        alerts.should_alert(session, 70)
        session.update(percent=70, level="warning")
        self.assertFalse(alerts.should_alert(session, 70))
        session.update(percent=60, level="normal")
        alerts.should_alert(session, 70)
        session.update(percent=70, level="warning")
        self.assertTrue(alerts.should_alert(session, 70))
        session["compactions"] = 1
        self.assertTrue(alerts.should_alert(session, 70))

    def test_handoff_requests_factual_document(self):
        prompt = handoff_prompt({"id": "abc", "title": "添加 Codex 上下文用量提醒", "percent": 85})
        self.assertIn("添加 Codex 上下文用量提醒", prompt)
        self.assertNotIn("abc", prompt)
        self.assertIn("HANDOFF.md", prompt)
        self.assertIn("未验证", prompt)


if __name__ == "__main__":
    unittest.main()
