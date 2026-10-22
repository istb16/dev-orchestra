"""Budgets, heartbeats and the stop verdict.

A guard that only advises is not a guard -- the review budget used to be
advertised by a query command and enforced nowhere, and it never stopped
anything. These tests assert the *refusal*, not the advice.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest

from helpers import SCRIPTS_DIR, TEST_WORKFLOW, IsolatedCase, present

from orchestrator import config as config_mod
from orchestrator import ledger as ledger_mod
from orchestrator import workspace as ws
from orchestrator.config_common import MAX_PARALLEL_UNITS

#: One process charging one piece of a pass: argv is the scripts directory,
#: the project, the workflow, the piece and the pass key; it prints whether
#: the charge spent an attempt.
CONSUME_PIECE = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from orchestrator import ledger, workspace\n"
    "book = ledger.Ledger(\n"
    "    workspace.Workspace(sys.argv[2], workflow=sys.argv[3]),\n"
    "    dict(ledger.DEFAULT_BUDGETS, implementer=5),\n"
    ")\n"
    "print(book.consume('implementer', piece=sys.argv[4], key=sys.argv[5]))\n"
)


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
        self.assertIn("delegated", present(refusal))
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
        # None on purpose: what an old caller passed.
        book.end(first, "ok", charged_seconds=None)  # pyright: ignore[reportArgumentType]
        second = book.begin("implementer")
        book.end(second, "ok", charged_seconds=-30)
        third = book.begin("implementer")
        book.end(third, "ok")
        self.assertEqual(book.runtime_used(), 0.0)


class TestSuspendedTime(LedgerCase):
    """The sleep a run spanned: recorded beside the charge, never in it."""

    def test_the_sleep_is_recorded_and_not_charged(self):
        book = self.book(max_runtime_seconds=3600)
        token = book.begin("implementer")
        book.end(token, "ok", charged_seconds=480, suspended_seconds=120)
        self.assertEqual(book.runtime_used(), 480)
        self.assertEqual(book.load()["runtime_suspended_seconds"], 120)
        event = self.workspace.read_state()["events"][-1]
        self.assertEqual(event["charged_seconds"], 480)
        self.assertEqual(event["suspended_seconds"], 120)
        self.assertEqual(book.summary()["runtime"]["suspended"], 120)

    def test_no_sleep_leaves_the_event_as_it_was(self):
        book = self.book()
        token = book.begin("implementer")
        book.end(token, "ok", charged_seconds=30)
        self.assertNotIn("suspended_seconds", self.workspace.read_state()["events"][-1])
        self.assertEqual(book.summary()["runtime"]["suspended"], 0)

    def test_a_skipped_charge_records_the_sleep_without_counting_it(self):
        book = self.book()
        token = book.begin("implementer")
        book.reset()
        book.end(token, "ok", charged_seconds=100, suspended_seconds=60)
        event = self.workspace.read_state()["events"][-1]
        self.assertIn("charge_skipped", event)
        self.assertEqual(event["suspended_seconds"], 60)
        self.assertEqual(book.load()["runtime_suspended_seconds"], 0)

    def test_a_reset_starts_the_sleep_total_at_zero(self):
        book = self.book()
        token = book.begin("implementer")
        book.end(token, "ok", charged_seconds=10, suspended_seconds=60)
        book.reset()
        self.assertEqual(book.load()["runtime_suspended_seconds"], 0)
        self.assertEqual(book.summary()["runtime"]["suspended"], 0)

    def test_values_that_are_not_finite_non_negative_numbers_land_as_zero(self):
        book = self.book()
        for suspended in (-5, float("nan"), float("inf")):
            token = book.begin("implementer")
            book.end(token, "ok", charged_seconds=10, suspended_seconds=suspended)
            self.assertNotIn("suspended_seconds", self.workspace.read_state()["events"][-1])
        token = book.begin("implementer")
        book.end(token, "ok", charged_seconds=float("nan"))
        self.assertEqual(self.workspace.read_state()["events"][-1]["charged_seconds"], 0)
        self.assertEqual(book.runtime_used(), 30)
        self.assertEqual(book.load()["runtime_suspended_seconds"], 0)

    def test_a_stored_total_that_is_not_a_number_reads_as_zero(self):
        book = self.book()
        book.begin("implementer")
        for stored in ("abc", float("nan"), float("inf")):
            state = self.workspace.read_state()
            state["ledger"]["runtime_suspended_seconds"] = stored
            self.workspace.write_state(state)
            self.assertEqual(book.summary()["runtime"]["suspended"], 0)
        token = book.begin("implementer")
        book.end(token, "ok", charged_seconds=10, suspended_seconds=60)
        self.assertEqual(book.load()["runtime_suspended_seconds"], 60)


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

    def test_a_ledger_with_neither_key_is_identified_by_its_start_time(self):
        started = "2026-09-16T00:00:00+00:00"
        ws.write_json(
            self.workspace.state_path,
            {
                "version": 1,
                "runs": [],
                "ledger": {
                    "started_at": started,
                    "last_activity_monotonic": time.time(),
                    "attempts": {"test": 1},
                },
            },
        )
        book = self.book()
        self.assertEqual(book.workflow_id(), started)
        self.assertEqual(book.load()["attempts"], {"test": 1})

    def test_a_ledger_with_only_the_old_workflow_key_is_identified_by_its_start_time(self):
        """The ``workflow`` key from before 0.4.2 is no longer renamed ``epoch``."""
        started = "2026-09-16T00:00:00+00:00"
        ws.write_json(
            self.workspace.state_path,
            {
                "version": 1,
                "runs": [],
                "ledger": {
                    "workflow": "old",
                    "started_at": started,
                    "last_activity_monotonic": time.time(),
                    "attempts": {"test": 1},
                },
            },
        )
        book = self.book()
        self.assertEqual(book.workflow_id(), started)
        self.assertNotEqual(book.workflow_id(), "old")
        self.assertEqual(book.load()["attempts"], {"test": 1})


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
        self.assertEqual(summary["runtime"], {"used": 600, "limit": 3600, "remaining": 3000, "suspended": 0})
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


class TestOneAttemptPerPass(LedgerCase):
    """The pieces of one plan share one attempt; only a rerun spends another."""

    PLAN = "a" * 64
    REVISED = "b" * 64

    def used(self, book, stage="implementer"):
        return int((book.load().get("attempts") or {}).get(stage, 0))

    def test_ledger_pass_one_attempt_for_all_pieces(self):
        book = self.book(implementer=5)
        spent = [book.consume("implementer", piece=name, key=self.PLAN) for name in ("a", "b", "c")]
        self.assertEqual(spent, [True, False, False])
        self.assertEqual(self.used(book), 1)
        self.assertEqual(book.load()["total_delegated_runs"], 3)
        self.assertEqual(
            book.summary()["passes"], {"implementer": {"key": self.PLAN, "pieces": ["a", "b", "c"]}}
        )

    def test_ledger_architect_pass_frees_at_most_eight_pieces(self):
        """An architect's piece is any name it is given: past eight, each new one spends."""
        book = self.book(architect=20, implementer=20)
        names = ["u%d" % number for number in range(10)]
        spent = [book.consume("architect", piece=name, key="") for name in names]
        self.assertEqual(spent, [True, *[False] * 7, True, True])
        self.assertEqual(self.used(book, "architect"), 3)
        self.assertEqual(book.load()["passes"]["architect"]["pieces"], names)
        # The implementer's pieces are a plan's units, not held to the count.
        spent = [book.consume("implementer", piece=name, key=self.PLAN) for name in names]
        self.assertEqual(spent, [True, *[False] * 9])

    def test_ledger_pass_rerun_spends_attempt(self):
        book = self.book(implementer=5)
        book.consume("implementer", piece="a", key=self.PLAN)
        book.consume("implementer", piece="b", key=self.PLAN)
        # Whatever the first run's outcome: the ledger does not see it.
        self.assertTrue(book.consume("implementer", piece="a", key=self.PLAN))
        self.assertEqual(self.used(book), 2)
        self.assertEqual(book.load()["passes"]["implementer"]["pieces"], ["a", "b"])

    def test_ledger_pass_new_plan_hash_new_pass(self):
        book = self.book(implementer=5)
        book.consume("implementer", piece="a", key=self.PLAN)
        book.consume("implementer", piece="b", key=self.PLAN)
        self.assertTrue(book.consume("implementer", piece="b", key=self.REVISED))
        self.assertEqual(self.used(book), 2)
        self.assertEqual(book.load()["passes"]["implementer"], {"key": self.REVISED, "pieces": ["b"]})
        self.assertFalse(book.consume("implementer", piece="a", key=self.REVISED))

    def test_ledger_pass_budget_reset_new_pass(self):
        book = self.book(implementer=5)
        book.consume("implementer", piece="a", key=self.PLAN)
        book.reset()
        self.assertEqual(book.load()["passes"], {})
        self.assertTrue(book.consume("implementer", piece="b", key=self.PLAN))
        self.assertEqual(self.used(book), 1)

    def test_ledger_pass_concurrent_first_pieces(self):
        """As many processes as ``implementation.max_parallel`` allows, each the first piece it sees.

        Processes, not threads: the ledger's lock is a file lock, and only
        separate processes show it holds between them.
        """
        pieces = [chr(ord("a") + index) for index in range(MAX_PARALLEL_UNITS)]
        workers = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    CONSUME_PIECE,
                    SCRIPTS_DIR,
                    self.project,
                    TEST_WORKFLOW,
                    piece,
                    self.PLAN,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for piece in pieces
        ]
        answers = []
        for worker in workers:
            out, err = worker.communicate(timeout=60)
            self.assertEqual(worker.returncode, 0, err)
            answers.append(out.strip())

        self.assertEqual(sorted(answers), ["False"] * (len(pieces) - 1) + ["True"])
        book = self.book()
        self.assertEqual(self.used(book), 1)
        self.assertEqual(sorted(book.load()["passes"]["implementer"]["pieces"]), pieces)
        self.assertEqual(book.load()["total_delegated_runs"], len(pieces))

    def test_ledger_pass_exhausted_exit_3(self):
        book = self.book(implementer=1)
        book.consume("implementer", piece="a", key=self.PLAN)
        for piece, key in (("x", self.REVISED), ("a", self.PLAN)):
            with self.subTest(piece=piece, key=key[:1]):
                self.assertTrue(book.check("implementer", piece, key))
                with self.assertRaises(ledger_mod.BudgetExhausted):
                    book.consume("implementer", piece=piece, key=key)
        self.assertEqual(book.check("implementer", "b", self.PLAN), [])
        self.assertFalse(book.consume("implementer", piece="b", key=self.PLAN))
        self.assertEqual(ledger_mod.EXIT_BUDGET_EXHAUSTED, 3)

        spent = self.book(implementer=1, total_delegated_runs=2)
        for piece, key in (("c", self.PLAN), ("x", self.REVISED), ("a", self.PLAN)):
            with self.subTest(total=True, piece=piece):
                with self.assertRaises(ledger_mod.BudgetExhausted) as ctx:
                    spent.consume("implementer", piece=piece, key=key)
                self.assertIn("delegated runs", str(ctx.exception))

    def test_ledger_plain_run_unchanged(self):
        book = self.book(implementer=5)
        book.consume("implementer", piece="a", key=self.PLAN)
        self.assertTrue(book.consume("implementer"))
        self.assertTrue(book.consume("implementer"))
        self.assertEqual(self.used(book), 3)
        self.assertEqual(book.load()["passes"], {"implementer": {"key": self.PLAN, "pieces": ["a"]}})

        plain = ledger_mod.Ledger(
            self.cli_workspace("plain"), dict(ledger_mod.DEFAULT_BUDGETS, implementer=2)
        )
        plain.consume("implementer")
        plain.consume("implementer")
        self.assertEqual(plain.load()["passes"], {})
        with self.assertRaises(ledger_mod.BudgetExhausted):
            plain.consume("implementer")


if __name__ == "__main__":
    unittest.main()
