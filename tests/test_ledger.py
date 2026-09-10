"""Budgets, heartbeats and the stop verdict.

A guard that only advises is not a guard -- the review budget used to be
advertised by a query command and enforced nowhere, and it never stopped
anything. These tests assert the *refusal*, not the advice.
"""

from __future__ import annotations

import json
import time
import unittest

from helpers import IsolatedCase

from orchestrator import config as config_mod
from orchestrator import ledger as ledger_mod
from orchestrator import workspace as ws


class LedgerCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = ws.Workspace(self.project).ensure()

    def book(self, **overrides):
        settings = dict(ledger_mod.DEFAULT_BUDGETS)
        settings.update(overrides)
        return ledger_mod.Ledger(self.workspace, settings)


class TestAttemptBudgets(LedgerCase):
    def test_attempts_are_counted_down(self):
        book = self.book(test=3)
        self.assertEqual(book.remaining("test"), 3)
        book.consume("test")
        book.consume("test")
        self.assertEqual(book.remaining("test"), 1)

    def test_the_attempt_after_the_last_one_is_refused(self):
        book = self.book(test=2)
        book.consume("test")
        book.consume("test")
        with self.assertRaises(ledger_mod.BudgetExhausted) as ctx:
            book.consume("test")
        self.assertIn("budget of 2", str(ctx.exception))

    def test_force_overrides_an_exhausted_budget(self):
        book = self.book(test=1)
        book.consume("test")
        book.consume("test", force=True)
        self.assertEqual(book.remaining("test"), 0)

    def test_an_unbudgeted_stage_is_never_refused(self):
        book = self.book()
        for _ in range(3):
            book.consume("orchestrator")
        self.assertIsNone(book.remaining("orchestrator"))

    def test_the_total_run_budget_bounds_loops_nobody_anticipated(self):
        book = self.book(total_delegated_runs=3, test=99, implementer=99)
        book.consume("test")
        book.consume("implementer")
        book.consume("test")
        with self.assertRaises(ledger_mod.BudgetExhausted) as ctx:
            book.consume("implementer")
        self.assertIn("delegated runs", str(ctx.exception))

    def test_the_runtime_budget_stops_a_long_workflow(self):
        book = self.book(max_runtime_seconds=3600)
        ledger = book.load()
        ledger["started_monotonic"] = time.time() - 7200
        book._write(ledger)
        self.assertEqual(book.runtime_remaining(), 0.0)
        self.assertTrue(any("longer than" in r for r in book.check("test")))

    def test_an_idle_ledger_starts_a_fresh_workflow(self):
        book = self.book(test=1, session_idle_reset_seconds=60)
        book.consume("test")
        ledger = book.load()
        ledger["last_activity_monotonic"] = time.time() - 600
        book._write(ledger)
        # A new request should not inherit the previous one's spent budget.
        self.assertEqual(book.remaining("test"), 1)

    def test_reset_clears_everything(self):
        book = self.book(test=2)
        book.consume("test")
        book.reset()
        self.assertEqual(book.remaining("test"), 2)

    def test_budgets_come_from_configuration(self):
        data = config_mod.default_config()
        data["budgets"] = {"test": 1}
        settings = ledger_mod.budget_settings(data)
        self.assertEqual(settings["test"], 1)
        # Unset keys keep their defaults rather than vanishing.
        self.assertEqual(settings["implementer"], ledger_mod.DEFAULT_BUDGETS["implementer"])


class TestNoProgress(LedgerCase):
    def test_a_repeated_outcome_is_counted(self):
        book = self.book()
        self.assertEqual(book.register_signature("test", "3 failed"), 1)
        self.assertEqual(book.register_signature("test", "3 failed"), 2)
        self.assertEqual(book.register_signature("test", "3 failed"), 3)

    def test_a_changed_outcome_resets_the_count(self):
        book = self.book()
        book.register_signature("test", "3 failed")
        book.register_signature("test", "3 failed")
        self.assertEqual(book.register_signature("test", "1 failed"), 1)

    def test_repeating_without_progress_refuses_the_next_attempt(self):
        book = self.book(max_repeats_without_progress=2, test=99)
        book.register_signature("test", "same failure")
        book.register_signature("test", "same failure")
        with self.assertRaises(ledger_mod.BudgetExhausted) as ctx:
            book.consume("test")
        self.assertIn("without progress", str(ctx.exception))

    def test_progress_keeps_the_loop_open(self):
        book = self.book(max_repeats_without_progress=2, test=99)
        book.register_signature("test", "3 failed")
        book.register_signature("test", "1 failed")
        book.consume("test")  # must not raise: something changed

    def test_stages_are_tracked_separately(self):
        book = self.book()
        book.register_signature("test", "x")
        book.register_signature("review", "y")
        self.assertEqual(book.repeats("test"), 1)
        self.assertEqual(book.repeats("review"), 1)


class TestInFlightHeartbeat(LedgerCase):
    def test_a_stage_is_recorded_before_it_runs(self):
        book = self.book()
        token = book.begin("implementer", {"provider": "mock"}, deadline=60)
        entry = book.in_flight()[token]
        self.assertEqual(entry["stage"], "implementer")
        self.assertEqual(entry["deadline_seconds"], 60)
        self.assertIn("pid", entry)

    def test_ending_a_stage_clears_it_and_logs_an_event(self):
        book = self.book()
        token = book.begin("implementer")
        book.end(token, "ok", {"model": "mock-small"})
        self.assertEqual(book.in_flight(), {})
        events = self.workspace.read_state()["events"]
        self.assertEqual(events[-1]["stage"], "implementer")
        self.assertEqual(events[-1]["status"], "ok")

    def test_a_live_stage_within_its_deadline_is_not_a_stall(self):
        book = self.book()
        book.begin("implementer", deadline=3600)
        self.assertEqual(book.stalls(), [])

    def test_a_stage_whose_process_is_gone_is_a_stall(self):
        book = self.book()
        token = book.begin("implementer", deadline=3600)
        ledger = book.load()
        ledger["in_flight"][token]["pid"] = 999_999
        book._write(ledger)
        stalls = book.stalls()
        self.assertEqual(len(stalls), 1)
        self.assertIn("is gone", stalls[0]["reason"])
        self.assertFalse(stalls[0]["process_alive"])

    def test_a_stage_far_past_its_deadline_is_a_stall(self):
        book = self.book()
        token = book.begin("implementer", deadline=10)
        ledger = book.load()
        ledger["in_flight"][token]["started_monotonic"] = time.time() - 600
        book._write(ledger)
        self.assertIn("past its", book.stalls()[0]["reason"])

    def test_clearing_stalls_records_them_as_abandoned(self):
        book = self.book()
        token = book.begin("implementer", deadline=3600)
        ledger = book.load()
        ledger["in_flight"][token]["pid"] = 999_999
        book._write(ledger)
        self.assertEqual(book.clear_stalls(), ["implementer"])
        self.assertEqual(book.in_flight(), {})
        self.assertEqual(self.workspace.read_state()["events"][-1]["status"], "abandoned")

    def test_a_live_stall_is_reported_but_not_cleared(self):
        """We must not forget a stage that is still running, only flag it."""
        book = self.book()
        token = book.begin("implementer", deadline=1)
        ledger = book.load()
        ledger["in_flight"][token]["started_monotonic"] = time.time() - 600
        book._write(ledger)
        self.assertEqual(book.clear_stalls(), [])
        self.assertEqual(len(book.in_flight()), 1)

    def test_the_ledger_survives_a_state_file_written_by_an_older_version(self):
        ws.write_json(self.workspace.state_path, {"version": 1, "runs": []})
        book = self.book()
        self.assertEqual(book.remaining("test"), ledger_mod.DEFAULT_BUDGETS["test"])


class TestSummary(LedgerCase):
    def test_summary_reports_what_is_spent(self):
        book = self.book(test=3)
        book.consume("test")
        summary = book.summary()
        self.assertEqual(summary["budgets"]["test"]["used"], 1)
        self.assertEqual(summary["budgets"]["test"]["remaining"], 2)
        self.assertEqual(summary["total_delegated_runs"]["used"], 1)
        self.assertIn("runtime_remaining_seconds", summary)

    def test_summary_is_json_serialisable(self):
        book = self.book()
        book.begin("implementer", deadline=60)
        json.dumps(book.summary())


if __name__ == "__main__":
    unittest.main()
