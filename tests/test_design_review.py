"""Reviewing the plan before it is implemented.

The design review is the code review's machinery pointed at `.ai/plan.md`:
same panel, same fan-out, same read-only mode. What these tests are mostly
about is the part that had to be new -- that it keeps its own artifacts, its
own round counter and its own triage, so a design round can never advance, or
be refused by, the code review's count.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git

from orchestrator import cli
from orchestrator import review as review_mod
from orchestrator import workspace as ws

#: `File:` names a plan section, which is what a design finding has instead of
#: a code location. High severity so it counts as blocking without triage.
DESIGN_FINDING = """## Finding
- Severity: high
- File: plan.md#Proposed Change
- Line: n/a
- Category: completeness
- Problem: the plan never says what happens to existing rows
- Impact: the migration would be written without a backfill
- Evidence: "add a NOT NULL column"
- Recommended fix: state the backfill and its ordering
"""

PLAN = """# Plan

## Proposed Change
Add a NOT NULL column to orders.
"""

REVISED_PLAN = """# Plan

## Proposed Change
Add a nullable column to orders, backfill it, then make it NOT NULL.
"""

REQUEST = """# Design request

## Goal
Record which channel an order came from.
"""


def run_cli(*argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class DesignReviewCase(IsolatedCase):
    """A mock panel of two, a plan, and the request the plan answers."""

    def setUp(self):
        super().setUp()
        mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(mock_dir)
        with open(os.path.join(mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(DESIGN_FINDING)
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = mock_dir

        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m2", "--role", "architecture")

        self.workspace = self.cli_workspace()
        self.design = self.workspace.design_review()

    def write_plan(self, text=PLAN):
        ws.write_text(self.workspace.plan_path, text)

    def write_request(self, text=REQUEST):
        ws.write_text(os.path.join(self.workspace.execution_dir, "design-request.md"), text)


class TestRunningIt(DesignReviewCase):
    def test_no_plan_names_the_stage_that_writes_one(self):
        code, _, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 2)
        self.assertIn("run the architect first", err)

    def test_a_round_writes_its_artifacts_under_reviews_design(self):
        self.write_plan()
        self.write_request()
        code, out, _ = run_cli("review", "run", "--design")
        self.assertEqual(code, 0)
        self.assertIn("2 successful, 0 failed", out)
        self.assertTrue(os.path.isfile(self.design.consolidated_json_path))
        self.assertTrue(os.path.isfile(self.design.reviewer_report_path("m1")))
        self.assertIn("NOT NULL", ws.read_text(self.design.snapshot_path))

    def test_the_code_review_snapshot_is_never_taken(self):
        """A plan is not a diff, so nothing here should touch git or the
        artifacts the code review keeps its round counter in."""
        self.write_plan()
        run_cli("review", "run", "--design")
        self.assertFalse(os.path.isfile(self.workspace.snapshot_path))
        self.assertFalse(os.path.isfile(self.workspace.consolidated_json_path))

    def test_it_works_outside_a_git_repository(self):
        """There is no diff to take, so there is nothing for git to do."""
        self.write_plan()
        self.assertFalse(ws.is_git_repo(self.project))
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)

    def test_a_missing_request_is_noted_rather_than_fatal(self):
        self.write_plan()
        code, _, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 0)
        self.assertIn("no design request at", err)

    def test_the_whole_panel_runs_on_a_small_plan(self):
        """No gate and no panel reduction: there is no test result to judge a
        plan by and no diff to measure, and a design decision is exactly where
        the second opinion is worth its cost."""
        self.write_plan()
        run_cli("state", "record", "test", "failed")
        code, out, _ = run_cli("review", "run", "--design")
        self.assertEqual(code, 0)
        self.assertIn("2 successful, 0 failed", out)

    def test_the_setting_being_off_notes_but_does_not_refuse(self):
        """The setting says whether the orchestrator runs this stage, not
        whether a person may."""
        self.write_plan()
        code, _, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 0)
        self.assertIn("review.design.enabled is false", err)

    def test_enabling_it_silences_the_note(self):
        run_cli("config", "set", "review.design.enabled", "true")
        self.write_plan()
        self.assertNotIn("review.design.enabled", run_cli("review", "run", "--design")[2])

    def test_zero_reviewers_skips_the_stage_without_failing(self):
        run_cli("reviewer", "remove", "m1")
        run_cli("reviewer", "remove", "m2")
        self.write_plan()
        code, out, _ = run_cli("review", "run", "--design")
        self.assertEqual(code, 0)
        self.assertIn("No reviewers configured", out)
        self.assertTrue(os.path.isfile(self.design.consolidated_json_path))

    def test_a_skipped_stage_does_not_spend_a_round(self):
        """The skip is recorded against the plan it actually froze, so the
        first round with a panel is still round one rather than round two of
        a budget a stage that ran nothing already ate into."""
        run_cli("reviewer", "remove", "m1")
        run_cli("reviewer", "remove", "m2")
        self.write_plan()
        run_cli("review", "run", "--design")

        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)
        data = json.loads(run_cli("review", "show", "--design", "--json")[1])
        self.assertEqual(data["iteration"], 1)
        self.assertEqual([f["id"] for f in data["findings"]], ["F1"])

    def test_no_plan_and_no_reviewers_records_nothing(self):
        run_cli("reviewer", "remove", "m1")
        run_cli("reviewer", "remove", "m2")
        code, _, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 2)
        self.assertIn("run the architect first", err)
        self.assertFalse(os.path.isfile(self.design.consolidated_json_path))

    def test_one_reviewer_failing_does_not_fail_the_round(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "Reviewer: m2 |"
        self.write_plan()
        code, out, _ = run_cli("review", "run", "--design")
        self.assertEqual(code, 0)
        self.assertIn("1 successful, 1 failed", out)

    def test_every_reviewer_failing_returns_non_zero(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        self.write_plan()
        self.assertEqual(run_cli("review", "run", "--design")[0], 1)

    def test_a_plan_too_large_to_inline_is_not_a_clean_round_either(self):
        """The change body of a design round is the plan itself, and the same
        rule applies to it: a reviewer handed a path is not a clean review."""
        self.write_plan(PLAN + "x" * review_mod.MAX_INLINE_DIFF_CHARS)
        code, out, _ = run_cli("review", "run", "--design")
        self.assertEqual(code, 1)
        self.assertIn("PARTIAL", out)
        self.assertIn("2 partial (change handed over as a file)", out)
        data = json.loads(run_cli("review", "show", "--design", "--json")[1])
        self.assertEqual(data["coverage"]["round"], "unverified")
        self.assertEqual(data["coverage"]["change"], "unverified")

    def test_status_names_the_plan_side_action_for_an_oversize_plan(self):
        """`review snapshot` writes the code snapshot -- there is no `--design`
        form of it -- so `--base`, `review.exclude` and `--full` cannot be
        aimed at a design round at all. The only thing that makes an oversize
        plan reviewable is a shorter plan, and that is what has to be said."""
        self.write_plan(PLAN + "x" * review_mod.MAX_INLINE_DIFF_CHARS)
        run_cli("review", "run", "--design")
        _, out, _ = run_cli("review", "status", "--design")
        self.assertIn("shorten .ai/plan.md", out)
        self.assertNotIn("snapshot", out)
        self.assertNotIn("--base", out)
        self.assertNotIn("review.exclude", out)

    def test_a_revised_plan_that_fits_clears_the_whole_change(self):
        """Every design round is the whole plan -- there is no incremental
        design snapshot -- so a revision that fits inline really is the whole
        change being reviewed again."""
        self.write_plan(PLAN + "x" * review_mod.MAX_INLINE_DIFF_CHARS)
        run_cli("review", "run", "--design")
        self.write_plan(REVISED_PLAN)
        code, _, _ = run_cli("review", "run", "--design")
        self.assertEqual(code, 0)
        coverage = json.loads(run_cli("review", "show", "--design", "--json")[1])["coverage"]
        self.assertEqual(coverage["change"], "complete")
        self.assertIsNone(coverage["unverified_since"])


class TestTriageAndRevision(DesignReviewCase):
    def setUp(self):
        super().setUp()
        self.write_plan()
        self.write_request()
        run_cli("review", "run", "--design")

    def test_triage_feeds_a_brief_that_asks_for_a_revision(self):
        run_cli("review", "triage", "--design", "F1", "--status", "accepted", "--note", "confirmed")
        _, brief, _ = run_cli("review", "fix-brief", "--design")
        self.assertIn("Revise the plan", brief)
        self.assertIn("F1", brief)
        self.assertIn("backfill", brief)

    def test_the_code_brief_still_asks_for_a_fix(self):
        _, brief, _ = run_cli("review", "fix-brief")
        self.assertNotIn("Revise the plan", brief)

    def test_the_brief_can_be_written_into_the_workflow(self):
        run_cli("review", "triage", "--design", "F1", "--status", "accepted")
        code, _, _ = run_cli(
            "review", "fix-brief", "--design", "--output", ".ai/execution/design-fix-brief.md"
        )
        self.assertEqual(code, 0)
        written = os.path.join(self.workspace.execution_dir, "design-fix-brief.md")
        self.assertIn("Revise the plan", ws.read_text(written))

    def test_status_recommends_another_round_within_budget(self):
        status = json.loads(run_cli("review", "status", "--design", "--json")[1])
        self.assertTrue(status["re_review_recommended"])
        self.assertEqual(status["blocking"], ["F1"])

    def test_status_names_the_budget_it_reports(self):
        """Two budgets, two settings, two key names: a payload that called the
        design budget `max_review_iterations` could not be told from the other
        review's."""
        design = json.loads(run_cli("review", "status", "--design", "--json")[1])
        self.assertEqual(design["max_iterations"], 2)
        self.assertNotIn("max_review_iterations", design)
        code = json.loads(run_cli("review", "status", "--json")[1])
        self.assertEqual(code["max_review_iterations"], 2)
        self.assertNotIn("max_iterations", code)

    def test_status_stops_recommending_once_the_budget_is_spent(self):
        run_cli("config", "set", "review.design.max_iterations", "1")
        status = json.loads(run_cli("review", "status", "--design", "--json")[1])
        self.assertFalse(status["re_review_recommended"])
        self.assertTrue(status["iteration_budget_exhausted"])


class TestRounds(DesignReviewCase):
    def _iteration(self):
        return json.loads(run_cli("review", "show", "--design", "--json")[1])["iteration"]

    def test_the_same_plan_stays_in_the_same_round(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        run_cli("review", "run", "--design")
        self.assertEqual(self._iteration(), 1)

    def test_a_rewritten_plan_is_the_next_round(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        self.write_plan(REVISED_PLAN)
        run_cli("review", "run", "--design")
        self.assertEqual(self._iteration(), 2)

    def test_a_round_past_the_budget_is_refused(self):
        run_cli("config", "set", "review.design.max_iterations", "1")
        self.write_plan()
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)
        self.write_plan(REVISED_PLAN)
        code, _, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 3)
        self.assertIn("refusing to run design review round", err)
        self.assertIn("review.design.max_iterations", err)

    def test_force_runs_a_round_past_the_budget(self):
        run_cli("config", "set", "review.design.max_iterations", "1")
        self.write_plan()
        run_cli("review", "run", "--design")
        self.write_plan(REVISED_PLAN)
        self.assertEqual(run_cli("review", "run", "--design", "--force")[0], 0)

    def test_a_round_charges_every_reviewer_it_ran(self):
        os.environ["DEV_ORCHESTRA_MOCK_DELAY"] = "0.2"
        self.write_plan()
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)
        event = self.workspace.read_state()["events"][-1]
        expected = sum(reviewer["duration_seconds"] for reviewer in event["reviewers"])
        self.assertEqual(len(event["reviewers"]), 2)
        self.assertAlmostEqual(event["charged_seconds"], expected, places=1)
        payload = json.loads(run_cli("budget", "show", "--json")[1])
        self.assertAlmostEqual(payload["runtime"]["used"], expected, places=1)

    def test_a_round_is_refused_once_the_runtime_budget_is_spent(self):
        from test_cli import spend_the_runtime_budget

        self.write_plan()
        spend_the_runtime_budget(self.workspace)
        code, _, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 3)
        self.assertIn("refusing to run design review", err)
        self.assertIn("delegated", err)
        self.assertEqual(self.workspace.read_state()["ledger"]["in_flight"], {})
        self.assertFalse(os.path.isfile(self.design.reviewer_report_path("m1")))
        self.assertEqual(run_cli("review", "run", "--design", "--force")[0], 0)

    def assert_the_first_plan_is_still_frozen_with_its_triage(self):
        frozen = ws.read_text(self.design.snapshot_path)
        self.assertIn("Add a NOT NULL column", frozen)
        self.assertNotIn("backfill", frozen)

        run_cli("review", "consolidate", "--design")
        data = json.loads(run_cli("review", "show", "--design", "--json")[1])
        self.assertEqual([f["id"] for f in data["findings"]], ["F1"])
        self.assertEqual(data["findings"][0]["triage"], "accepted")

    def test_a_refused_round_leaves_the_frozen_plan_where_it_was(self):
        """The reports on disk are stamped with the plan's sha. Freezing the
        revision for a round that is then refused would make every one of them
        stale, which discards the triage the refusal asked to be reported.

        Both refusals, because both ask for the same report: whichever budget
        says no, it has to say so before the freeze."""
        from test_cli import spend_the_runtime_budget

        run_cli("config", "set", "review.design.max_iterations", "1")
        self.write_plan()
        run_cli("review", "run", "--design")
        run_cli("review", "triage", "--design", "F1", "--status", "accepted")

        self.write_plan(REVISED_PLAN)
        self.assertEqual(run_cli("review", "run", "--design")[0], 3)
        self.assert_the_first_plan_is_still_frozen_with_its_triage()

        # Room in the round budget, so the next refusal can only be the runtime.
        run_cli("config", "set", "review.design.max_iterations", "5")
        spend_the_runtime_budget(self.workspace)
        code, _, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 3)
        self.assertIn("runtime budget", err)
        self.assert_the_first_plan_is_still_frozen_with_its_triage()

    def test_a_revision_that_changes_nothing_is_called_out(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        self.write_plan(REVISED_PLAN)
        _, _, err = run_cli("review", "run", "--design")
        self.assertIn("changed nothing", err)


class TestIndependenceFromTheCodeReview(DesignReviewCase):
    def test_review_show_without_the_flag_sees_no_design_findings(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        code, _, err = run_cli("review", "show")
        self.assertEqual(code, 2)
        self.assertIn("no consolidated review found", err)

    def test_triage_is_kept_apart(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        code, _, err = run_cli("review", "triage", "F1", "--status", "accepted")
        self.assertEqual(code, 2)
        self.assertIn("no consolidated review found", err)

    @unittest.skipUnless(has_git(), "git is required")
    def test_a_code_review_after_two_design_rounds_starts_at_round_one(self):
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write_plan()
        run_cli("review", "run", "--design")
        self.write_plan(REVISED_PLAN)
        run_cli("review", "run", "--design")
        self.assertEqual(json.loads(run_cli("review", "show", "--design", "--json")[1])["iteration"], 2)

        self.write("app.py", "def add(a, b):\n    return a - b\n")
        run_cli("review", "snapshot")
        run_cli("review", "run")
        self.assertEqual(json.loads(run_cli("review", "show", "--json")[1])["iteration"], 1)


class TestWhatTheOrchestratorReads(DesignReviewCase):
    def setUp(self):
        super().setUp()
        self.write_plan()

    def test_tokens_are_accounted_apart_from_the_code_review(self):
        run_cli("review", "run", "--design")
        report = json.loads(run_cli("tokens", "show", "--json")[1])
        self.assertEqual(report["by_stage"]["design_review"]["runs"], 2)
        self.assertIn("design:m1", report["by_label"])

    def test_status_reports_the_stage(self):
        run_cli("review", "run", "--design")
        payload = json.loads(run_cli("status", "--json")[1])
        design = payload["design_review"]
        self.assertFalse(design["enabled"])
        self.assertEqual(design["iteration"], 1)
        self.assertEqual(design["blocking"], ["F1"])

    def test_open_design_findings_with_the_budget_spent_stop_the_pipeline(self):
        """Implementing a plan whose known problems are unanswered is the
        mistake this stage exists to prevent, so it stops like the code
        review does rather than merely reporting."""
        run_cli("config", "set", "review.design.max_iterations", "1")
        run_cli("review", "run", "--design")
        payload = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertTrue(any("design review budget spent" in r for r in payload["reasons"]))

    def test_status_says_so_to_a_human_too(self):
        run_cli("review", "run", "--design")
        _, out, _ = run_cli("status")
        self.assertIn("Design review: off, round 1/2", out)

    def test_summary_lists_the_stage(self):
        run_cli("review", "run", "--design")
        _, out, _ = run_cli("summary")
        self.assertIn("design_review", out)
        self.assertIn("design reviews", out)

    def test_optimization_report_counts_the_cost_but_not_the_rate(self):
        """The level decides nothing here, so counting these rounds among the
        code rounds would dilute every rate that report is for. The cost is a
        different question, and the report used to answer it with zero."""
        run_cli("review", "run", "--design")
        report = json.loads(run_cli("optimization", "report", "--json")[1])
        self.assertEqual(report["rounds"], 0)
        self.assertEqual(report["reviewer_runs"], 0)
        self.assertEqual(report["design_rounds"], 1)
        self.assertEqual(report["design_reviewer_runs"], 2)


if __name__ == "__main__":
    unittest.main()
