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
        self.workspace = self.cli_workspace()

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

    def test_an_idle_ledger_starts_a_fresh_workflow(self):
        book = self.book(test=1, session_idle_reset_seconds=60)
        book.consume("test")
        ledger = book.load()
        ledger["last_activity_monotonic"] = time.time() - 600
        book._write(ledger)
        # A new request should not inherit the previous one's spent budget.
        self.assertEqual(book.remaining("test"), 1)

    def test_reset_clears_the_budgets(self):
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


class TestRuntimeBudget(LedgerCase):
    """What the runtime budget charges, and what it refuses to charge.

    The one rule underneath all of these: execution that was never measured is
    never billed. Issue #40 was the other reading -- a budget named for runtime
    that was really counting how long the ledger had existed, so an interactive
    session spent it all without delegating a single run.
    """

    def test_neither_calendar_time_nor_a_running_stage_spends_the_budget(self):
        book = self.book(max_runtime_seconds=3600)
        token = book.begin("implementer", deadline=1800)
        ledger = book.load()
        ledger["started_monotonic"] = time.time() - 7200
        ledger["in_flight"][token]["started_monotonic"] = time.time() - 7200
        book._write(ledger)
        self.assertEqual(book.runtime_used(), 0.0)
        self.assertEqual(book.runtime_remaining(), 3600.0)
        self.assertIsNone(book.runtime_refusal())
        self.assertEqual(book.check("test"), [])

    def test_ending_a_stage_charges_what_the_caller_measured(self):
        book = self.book(max_runtime_seconds=3600)
        token = book.begin("implementer", deadline=1800)
        book.end(token, "ok", charged_seconds=600)
        self.assertEqual(book.load()["runtime_seconds"], 600)
        self.assertEqual(book.runtime_used(), 600)
        event = self.workspace.read_state()["events"][-1]
        self.assertEqual(event["charged_seconds"], 600)
        self.assertNotIn("charge_skipped", event)
        # The raw gap since `begin` is still recorded, and is not the charge.
        self.assertLess(event["elapsed_seconds"], 60)

    def test_the_charge_is_not_capped_at_the_deadline(self):
        """A batch runs its whole panel; the entry's deadline is one member's."""
        book = self.book(max_runtime_seconds=99_999)
        token = book.begin("review", deadline=1800)
        book.end(token, "ok", charged_seconds=5000)
        self.assertEqual(book.runtime_used(), 5000)

    def test_spending_the_budget_refuses_the_next_run(self):
        book = self.book(max_runtime_seconds=100)
        token = book.begin("implementer")
        book.end(token, "ok", charged_seconds=100)
        self.assertEqual(book.runtime_remaining(), 0.0)
        refusal = book.runtime_refusal()
        self.assertIn("delegated", refusal)
        self.assertIn(refusal, book.check("test"))
        with self.assertRaises(ledger_mod.BudgetExhausted) as ctx:
            book.consume("test")
        self.assertIn(refusal, str(ctx.exception))

    def test_budget_left_means_no_refusal(self):
        book = self.book(max_runtime_seconds=100)
        token = book.begin("implementer")
        book.end(token, "ok", charged_seconds=99)
        self.assertIsNone(book.runtime_refusal())
        self.assertEqual(book.check("test"), [])

    def test_an_abandoned_stage_is_charged_nothing(self):
        """However long it ran: nobody measured it, so nobody may bill it."""
        book = self.book()
        token = book.begin("implementer", deadline=3600)
        ledger = book.load()
        ledger["in_flight"][token]["pid"] = 999_999
        ledger["in_flight"][token]["started_monotonic"] = time.time() - 1800
        book._write(ledger)
        self.assertEqual(book.clear_stalls(), ["implementer"])
        self.assertEqual(book.runtime_used(), 0.0)
        event = self.workspace.read_state()["events"][-1]
        self.assertEqual(event["status"], "abandoned")
        self.assertEqual(event["charged_seconds"], 0)
        self.assertNotIn("charge_skipped", event)

    def test_a_reset_between_begin_and_end_takes_the_claim_away(self):
        book = self.book()
        token = book.begin("implementer")
        book.reset()
        book.end(token, "ok", charged_seconds=100)
        self.assertEqual(book.runtime_used(), 0.0)
        self.assertEqual(book.in_flight(), {})
        event = self.workspace.read_state()["events"][-1]
        # The run happened and is recorded as it happened. What it may not do
        # is bill budgets minted after it started.
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["charged_seconds"], 0)
        self.assertIn("no in-flight entry", event["charge_skipped"])
        self.assertIsNotNone(book.token_report())

    def test_an_idle_reset_between_begin_and_end_takes_it_away_too(self):
        book = self.book(session_idle_reset_seconds=60)
        token = book.begin("implementer")
        before = ledger_mod._epoch(book.load())
        ledger = book.load()
        ledger["last_activity_monotonic"] = time.time() - 600
        book._write(ledger)
        book.end(token, "ok", charged_seconds=100)
        self.assertEqual(book.runtime_used(), 0.0)
        self.assertNotEqual(ledger_mod._epoch(book.load()), before)

    def test_ending_the_same_token_twice_charges_once(self):
        book = self.book()
        token = book.begin("implementer")
        book.end(token, "ok", charged_seconds=10)
        book.end(token, "ok", charged_seconds=10)
        self.assertEqual(book.runtime_used(), 10)
        event = self.workspace.read_state()["events"][-1]
        self.assertEqual(event["charged_seconds"], 0)
        self.assertIn("no in-flight entry", event["charge_skipped"])

    def test_ending_a_token_that_was_never_begun_is_not_an_error(self):
        book = self.book()
        book.end("implementer-deadbeef", "ok", charged_seconds=10)
        self.assertEqual(book.runtime_used(), 0.0)
        event = self.workspace.read_state()["events"][-1]
        self.assertEqual(event["stage"], "implementer")
        self.assertEqual(event["charged_seconds"], 0)

    def test_an_in_flight_entry_is_stamped_with_the_epoch_it_may_charge(self):
        book = self.book()
        token = book.begin("implementer")
        self.assertEqual(book.in_flight()[token]["epoch"], ledger_mod._epoch(book.load()))

    def test_an_entry_from_before_the_stamp_existed_still_charges(self):
        """Its presence in *this* ledger is the proof no reset has happened."""
        book = self.book()
        token = book.begin("implementer")
        ledger = book.load()
        del ledger["in_flight"][token]["epoch"]
        book._write(ledger)
        book.end(token, "ok", charged_seconds=50)
        self.assertEqual(book.runtime_used(), 50)
        self.assertNotIn("charge_skipped", self.workspace.read_state()["events"][-1])

    def test_an_entry_stamped_with_another_epoch_charges_nothing(self):
        book = self.book()
        token = book.begin("implementer")
        ledger = book.load()
        ledger["in_flight"][token]["epoch"] = "0123456789ab"
        book._write(ledger)
        book.end(token, "ok", charged_seconds=50)
        self.assertEqual(book.runtime_used(), 0.0)
        self.assertIn("epoch 0123456789ab", self.workspace.read_state()["events"][-1]["charge_skipped"])

    def test_a_reset_returns_the_runtime_budget_but_keeps_the_account(self):
        book = self.book(max_runtime_seconds=3600)
        token = book.begin("implementer")
        book.record_usage("implementer", {"billed_tokens": 1234, "measured": True})
        book.end(token, "ok", charged_seconds=900)
        book.reset()
        self.assertEqual(book.runtime_remaining(), 3600.0)
        self.assertEqual(book.token_report()["totals"]["billed_tokens"], 1234)

    def test_an_idle_reset_returns_the_runtime_budget_too(self):
        book = self.book(max_runtime_seconds=3600, session_idle_reset_seconds=60)
        token = book.begin("implementer")
        book.end(token, "ok", charged_seconds=900)
        ledger = book.load()
        ledger["last_activity_monotonic"] = time.time() - 600
        book._write(ledger)
        self.assertEqual(book.runtime_remaining(), 3600.0)

    def test_a_ledger_written_before_this_budget_existed_starts_at_zero(self):
        """Not reconstructed from the wall clock: nothing there was measured."""
        book = self.book(max_runtime_seconds=3600)
        ledger = book.load()
        ledger.pop("runtime_seconds", None)
        ledger["started_monotonic"] = time.time() - 7200
        book._write(ledger)
        self.assertEqual(book.runtime_used(), 0.0)
        self.assertEqual(book.runtime_remaining(), 3600.0)
        self.assertEqual(book.summary()["runtime"]["used"], 0)
        json.dumps(book.summary())

    def test_a_charge_that_is_missing_or_negative_is_read_as_nothing(self):
        book = self.book()
        first = book.begin("implementer")
        book.end(first, "ok", charged_seconds=None)
        second = book.begin("implementer")
        book.end(second, "ok", charged_seconds=-30)
        third = book.begin("implementer")
        book.end(third, "ok")
        self.assertEqual(book.runtime_used(), 0.0)


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

    def test_summary_reports_runtime_as_used_and_as_remaining(self):
        book = self.book(max_runtime_seconds=3600)
        token = book.begin("implementer")
        book.end(token, "ok", charged_seconds=600)
        summary = book.summary()
        self.assertEqual(summary["runtime"], {"used": 600, "limit": 3600, "remaining": 3000})
        # The older key still answers what it always answered.
        self.assertEqual(summary["runtime_remaining_seconds"], 3000)

    def test_a_running_stage_does_not_move_the_runtime_summary(self):
        book = self.book(max_runtime_seconds=3600)
        book.begin("implementer", deadline=1800)
        self.assertEqual(book.summary()["runtime"]["used"], 0)

    def test_summary_is_json_serialisable(self):
        book = self.book()
        book.begin("implementer", deadline=60)
        json.dumps(book.summary())


if __name__ == "__main__":
    unittest.main()
