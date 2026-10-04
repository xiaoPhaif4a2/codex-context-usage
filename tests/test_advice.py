"""Real event sequences distinguish compression, capacity and handoff advice."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from context_indicator.advice import AdviceState, handoff_advice
from context_indicator.handoff import AlertState, handoff_prompt
from context_indicator.monitor import Monitor, Rollout


def usage(percent):
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"total_tokens": percent * 10}, "model_context_window": 1000}}}


def task(kind, identifier):
    return {"type": "event_msg", "payload": {"type": kind, "turn_id": identifier}}


class AdviceTests(unittest.TestCase):
    def setUp(self):
        self.rollout = Rollout(Path("unused.jsonl"))
        self.rollout.metadata = {"id": "chat", "cwd": "project"}

    def run_turn(self, identifier, percent, compact=False):
        self.rollout.consume(task("task_started", identifier))
        if compact:
            self.rollout.consume({"type": "compacted", "timestamp": f"stamp-{identifier}", "payload": {}})
        self.rollout.consume(usage(percent))
        self.rollout.consume(task("task_complete", identifier))

    def advice(self, state=None):
        return handoff_advice(self.rollout.summary(70, 85), 70, 85, state)

    def test_single_high_reading_is_not_a_handoff_alarm(self):
        self.run_turn("first", 87)
        self.assertEqual(self.advice()["rank"], 1)
        session = self.rollout.summary(70, 85)
        session["advice"] = self.advice()
        self.assertFalse(AlertState().should_alert(session, 70))

    def test_reported_87_to_20_sequence_keeps_chat_and_explains_compaction(self):
        self.run_turn("first", 87)
        self.run_turn("second", 20, compact=True)
        session = self.rollout.summary(70, 85)
        self.assertEqual(session["id"], "chat")
        self.assertEqual(session["last_compaction"]["before_percent"], 87)
        self.assertEqual(session["last_compaction"]["after_percent"], 20)
        self.assertEqual(session["compactions"], 1)
        self.assertEqual(self.advice()["rank"], 0)
        self.assertIn("可以继续", self.advice()["reasons"][0])

    def test_tool_calls_polls_and_duplicate_turn_records_do_not_create_streak(self):
        self.rollout.consume(task("task_started", "a"))
        self.rollout.consume({"type": "turn_context", "payload": {"turn_id": "a", "model": "m"}})
        for _ in range(20):
            self.rollout.consume(usage(87))
            self.advice()
        self.rollout.consume(task("task_complete", "a"))
        self.rollout.consume(task("task_complete", "a"))
        self.assertEqual(len(self.rollout.completed_turns), 1)
        self.assertEqual(self.advice()["high_turns"], 1)
        self.assertEqual(self.advice()["rank"], 1)

    def test_three_completed_high_turns_suggest_checkpoint_without_forcing_switch(self):
        for identifier in ("a", "b", "c"):
            self.run_turn(identifier, 87)
        advice = self.advice()
        self.assertEqual(advice["code"], "sustained_high")
        self.assertEqual(advice["rank"], 2)
        self.assertIn("无需立即切换", advice["action"])

    def test_unfinished_third_turn_does_not_complete_streak(self):
        self.run_turn("a", 87)
        self.run_turn("b", 87)
        self.rollout.consume(task("task_started", "c"))
        self.rollout.consume(usage(90))
        self.assertEqual(self.advice()["rank"], 1)

    def test_compaction_and_low_reading_reset_high_streak(self):
        for identifier in ("a", "b", "c"):
            self.run_turn(identifier, 87)
        self.run_turn("d", 20, compact=True)
        self.assertEqual(self.advice()["rank"], 0)
        self.run_turn("e", 87)
        self.assertEqual(self.advice()["high_turns"], 1)

    def test_missing_measurement_does_not_prove_persistent_high_usage(self):
        self.run_turn("a", 87)
        self.rollout.consume(task("task_started", "b"))
        self.rollout.consume(task("task_complete", "b"))
        self.run_turn("c", 87)
        self.assertEqual(self.advice()["high_turns"], 1)

    def test_older_logs_without_turn_events_do_not_invent_three_turns(self):
        for _ in range(20):
            self.rollout.consume(usage(90))
        self.assertEqual(self.advice()["rank"], 1)
        self.assertEqual(self.advice()["high_turns"], 0)

    def test_two_compactions_within_five_turns_suggest_handoff_even_at_low_usage(self):
        self.run_turn("a", 20, compact=True)
        self.run_turn("b", 20, compact=True)
        self.assertEqual(self.advice()["code"], "frequent_compaction")
        self.assertEqual(self.advice()["rank"], 3)
        self.rollout.consume(task("task_started", "c"))
        self.assertIn("先等待", self.advice()["action"])

    def test_low_usage_handoff_signal_does_not_repeat_toast_on_every_poll(self):
        self.run_turn("a", 20, compact=True)
        self.run_turn("b", 20, compact=True)
        session = self.rollout.summary(70, 85)
        session["advice"] = self.advice()
        alerts = AlertState()
        self.assertTrue(alerts.should_alert(session, 70))
        for _ in range(20):
            self.assertFalse(alerts.should_alert(session, 70))

    def test_checkpoint_from_a_longer_replaced_log_does_not_hide_new_streak(self):
        for identifier in ("a", "b", "c"):
            self.run_turn(identifier, 87)
        self.assertEqual(self.advice({"checkpoint_turn": 100})["rank"], 2)

    def test_spaced_compactions_only_need_checkpoint_and_recent_window_expires(self):
        self.run_turn("a", 20, compact=True)
        for index in range(5):
            self.run_turn(str(index), 20)
        self.run_turn("b", 20, compact=True)
        self.assertEqual(self.advice()["code"], "checkpoint_compaction")
        self.assertEqual(self.advice()["rank"], 2)

    def test_checkpoint_confirmation_suppresses_same_checkpoint_but_not_future_work(self):
        for identifier in ("a", "b", "c"):
            self.run_turn(identifier, 87)
        state = {"checkpoint_turn": 3, "checkpoint_compactions": 0}
        self.assertEqual(self.advice(state)["rank"], 1)
        for identifier in ("d", "e", "f"):
            self.run_turn(identifier, 87)
        self.assertEqual(self.advice(state)["rank"], 2)

    def test_manual_quality_issue_is_explicit_and_can_be_cleared(self):
        self.run_turn("a", 20)
        self.assertEqual(self.advice({"quality_issue": True})["code"], "reported_issue")
        self.assertEqual(self.advice({"quality_issue": False})["rank"], 0)

    def test_capacity_regrowth_after_three_compactions_suggests_stage_handoff(self):
        for index in range(3):
            self.run_turn(f"compact-{index}", 20, compact=True)
            for turn in range(5):
                self.run_turn(f"after-{index}-{turn}", 20)
        self.run_turn("growth", 75)
        self.assertEqual(self.advice()["code"], "repeated_growth")

    def test_model_change_invalidates_old_high_turns(self):
        self.rollout.consume({"type": "turn_context", "payload": {"model": "a"}})
        for identifier in ("a", "b", "c"):
            self.run_turn(identifier, 87)
        self.rollout.consume({"type": "turn_context", "payload": {"model": "b"}})
        self.assertEqual(self.advice()["code"], "unknown")
        self.run_turn("d", 87)
        self.assertEqual(self.advice()["high_turns"], 1)

    def test_two_record_forms_of_compaction_are_counted_once(self):
        self.run_turn("a", 87)
        self.rollout.consume({"type": "compacted", "timestamp": "same", "payload": {}})
        self.rollout.consume({"type": "event_msg", "timestamp": "same", "payload": {"type": "context_compacted"}})
        self.assertEqual(self.rollout.compactions, 1)

    def test_later_compaction_without_intermediate_reading_is_not_a_duplicate(self):
        self.run_turn("a", 87)
        self.rollout.consume({"type": "compacted", "timestamp": "2026-10-04T05:00:00Z", "payload": {}})
        self.rollout.consume({"type": "event_msg", "timestamp": "2026-10-04T05:01:00Z", "payload": {"type": "context_compacted"}})
        self.assertEqual(self.rollout.compactions, 2)

    def test_late_final_usage_updates_completed_turn_without_adding_turn(self):
        self.run_turn("a", 80)
        self.rollout.consume(usage(90))
        self.assertEqual(len(self.rollout.completed_turns), 1)
        self.assertEqual(self.advice()["high_turns"], 1)

    def test_prompt_distinguishes_checkpoint_from_handoff_and_copy_does_not_acknowledge(self):
        session = self.rollout.summary(70, 85)
        checkpoint = handoff_prompt(session, "checkpoint")
        handoff = handoff_prompt(session)
        self.assertIn("本次保存不要求新建对话", checkpoint)
        self.assertIn("待我确认", handoff)
        self.assertIn("HANDOFF.md", checkpoint)
        self.assertNotIn("checkpoint_confirmed", session)


class PersistenceTests(unittest.TestCase):
    def test_atomic_replace_is_seen_even_when_file_timestamp_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "advice.json"
            store = AdviceState(path)
            store.update({"id": "a"}, "quality_issue")
            original_mtime = path.stat().st_mtime_ns
            AdviceState(path).update({"id": "a"}, "clear_issue")
            os.utime(path, ns=(original_mtime, original_mtime))
            store.refresh()
            self.assertFalse(store.entries["a"]["quality_issue"])

    def test_flags_and_confirmations_are_per_chat_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "advice.json"
            store = AdviceState(path)
            store.update({"id": "a", "turn_index": 3, "compactions": 1}, "quality_issue")
            store.update({"id": "b", "turn_index": 5, "compactions": 2}, "checkpoint_saved")
            reload = AdviceState(path)
            self.assertTrue(reload.entries["a"]["quality_issue"])
            self.assertFalse(reload.entries["b"]["quality_issue"])
            self.assertEqual(reload.entries["b"]["checkpoint_turn"], 5)
            reload.update({"id": "a"}, "clear_issue")
            store.refresh()
            self.assertFalse(store.entries["a"]["quality_issue"])

    def test_corrupt_settings_and_untrusted_values_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "advice.json"
            path.write_text('not json')
            self.assertEqual(AdviceState(path).entries, {})
            path.write_text(json.dumps({"a": {"quality_issue": "yes", "checkpoint_turn": True, "checkpoint_compactions": -1}}))
            self.assertEqual(AdviceState(path).entries["a"], {"quality_issue": False, "checkpoint_turn": 0, "checkpoint_compactions": 0})

    def test_catalog_log_wins_over_newer_duplicate_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            sessions.mkdir()
            authoritative = sessions / "authoritative.jsonl"
            for name, stamp, percent in (("authoritative", "2026-10-04T05:00:00Z", 20), ("copy", "2026-10-04T06:00:00Z", 90)):
                records = [{"type": "session_meta", "payload": {"id": "a", "cwd": str(root), "source": "vscode"}}, {**usage(percent), "timestamp": stamp}]
                (sessions / f"{name}.jsonl").write_text("\n".join(json.dumps(row) for row in records) + "\n")
            conn = sqlite3.connect(root / "state_5.sqlite")
            conn.execute("CREATE TABLE threads (id TEXT, name TEXT, rollout_path TEXT)")
            conn.execute("INSERT INTO threads VALUES (?, ?, ?)", ("a", "Named chat", str(authoritative)))
            conn.commit()
            conn.close()
            snapshot = Monitor(root, root).snapshot()
            self.assertEqual(len(snapshot["sessions"]), 1)
            self.assertEqual(snapshot["sessions"][0]["percent"], 20)


if __name__ == "__main__":
    unittest.main()
