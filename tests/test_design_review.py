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
        self.mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(self.mock_dir)
        with open(os.path.join(self.mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(DESIGN_FINDING)
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = self.mock_dir

        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m2", "--role", "architecture")
        run_cli("config", "set", "architect.provider", "mock")

        self.workspace = self.cli_workspace()
        self.design = self.workspace.design_review()

    def write_plan(self, text=PLAN):
        ws.write_text(self.workspace.plan_path, text)

    def revise_plan(self, text=REVISED_PLAN):
        """Have the mock architect answer with ``text``, written over the plan."""
        with open(os.path.join(self.mock_dir, "plan.txt"), "w", encoding="utf-8") as handle:
            handle.write(text)
        return run_cli("run", "architect", "--prompt", "revise", "--output", ".ai/plan.md")

    def write_oversize_plan(self):
        """A plan the round cannot inline, and the setting that makes it one.

        The limit is lowered rather than the plan made enormous: delivery is
        decided by `review.context.inline_chars` now, and 4,000 exercises the
        same branch as the shipped 400,000 without a 400KB fixture.
        """
        run_cli("config", "set", "review.context.inline_chars", "4000")
        self.write_plan(PLAN + "x" * 4_000)

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
        self.write_oversize_plan()
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
        self.write_oversize_plan()
        run_cli("review", "run", "--design")
        _, out, _ = run_cli("review", "status", "--design")
        self.assertIn("shorten .ai/plan.md", out)
        self.assertIn("raise review.context.inline_chars", out)
        self.assertNotIn("snapshot", out)
        self.assertNotIn("--base", out)
        self.assertNotIn("review.exclude", out)

    def test_status_says_the_limit_was_raised_rather_than_asking_for_a_shorter_plan(self):
        """The plan-side remedy is a shorter plan, but not once the limit has
        been raised past the size the round recorded: the same plan would be
        inlined now, and telling the reader to shorten it is wasted work."""
        self.write_oversize_plan()
        run_cli("review", "run", "--design")
        run_cli("config", "set", "review.context.inline_chars", "20000")
        _, out, _ = run_cli("review", "status", "--design")
        self.assertIn("under a lower review.context.inline_chars", out)
        self.assertIn("run the design round again", out)
        self.assertNotIn("shorten .ai/plan.md", out)
        self.assertNotIn("gives the same answer", out)

    def test_a_revised_plan_that_fits_clears_the_whole_change(self):
        """Every design round is the whole plan -- there is no incremental
        design snapshot -- so a revision that fits inline really is the whole
        change being reviewed again."""
        self.write_oversize_plan()
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
        """Once the last round's findings are folded in, the unreviewed
        revision is not implemented unapproved: it stops like the code review
        does rather than merely reporting."""
        run_cli("config", "set", "review.design.max_iterations", "1")
        run_cli("review", "run", "--design")
        self.revise_plan()
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


class TestTheFinalRevision(DesignReviewCase):
    """The round that reaches the limit still gets its revision; only the
    re-review of that revision is refused."""

    FINAL_PLAN = REVISED_PLAN + "\nBackfill in batches of 10,000 rows.\n"

    def setUp(self):
        super().setUp()
        self.write_plan()
        self.write_request()

    def last_round(self, max_iterations="1"):
        run_cli("config", "set", "review.design.max_iterations", max_iterations)
        run_cli("review", "run", "--design")
        run_cli("review", "triage", "--design", "F1", "--status", "accepted")

    def review_status(self):
        return json.loads(run_cli("review", "status", "--design", "--json")[1])

    def status(self):
        return json.loads(run_cli("status", "--json")[1])

    def status_line(self, prefix):
        return next(line for line in run_cli("status")[1].splitlines() if line.startswith(prefix))

    def architect_events(self):
        return [e for e in self.workspace.read_state()["events"] if e.get("stage") == "architect"]

    def test_status_at_the_limit_asks_for_one_more_revision_not_a_re_review(self):
        self.last_round()
        status = self.review_status()
        self.assertTrue(status["iteration_budget_exhausted"])
        self.assertFalse(status["re_review_recommended"])
        self.assertEqual(status["final_revision"], "pending")
        self.assertIs(status["final_revision_pending"], True)
        _, out, _ = run_cli("review", "status", "--design")
        self.assertIn("fold the accepted findings", out)
        self.assertNotIn("report the remaining findings instead of looping", out)

    def test_status_continues_while_the_final_revision_is_pending(self):
        self.last_round()
        payload = self.status()
        self.assertEqual(payload["verdict"], "continue")
        self.assertFalse(any("design review budget spent" in r for r in payload["reasons"]))
        self.assertIs(payload["design_review"]["final_revision_pending"], True)
        self.assertIn("fold the accepted findings", self.status_line("Plan approval:"))
        self.assertIn("final revision pending", self.status_line("Design review:"))

    def test_a_revision_after_the_last_round_clears_the_pending_flag(self):
        self.last_round()
        self.assertEqual(self.revise_plan()[0], 0)
        self.assertEqual(self.review_status()["final_revision"], "done")
        self.assertIn("not re-reviewed", run_cli("review", "status", "--design")[1])
        code, _, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 3)
        self.assertIn("only the re-review is refused", err)

    def test_an_unchanged_ok_revision_clears_the_pending_flag(self):
        self.last_round()
        self.assertEqual(self.revise_plan(PLAN)[0], 0)
        self.assertEqual(self.review_status()["final_revision"], "done")
        self.assertIn("left the plan unchanged", run_cli("review", "status", "--design")[1])
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertTrue(any("left the plan unchanged" in r for r in payload["reasons"]))
        self.assertIn("triage them again", self.status_line("Plan approval:"))

    def test_an_empty_ok_revision_keeps_the_final_revision_pending(self):
        self.last_round()
        self.assertEqual(self.revise_plan("")[0], 1)
        self.assertEqual(ws.read_text(self.workspace.plan_path), PLAN)
        event = self.architect_events()[-1]
        self.assertEqual(event["status"], "ok")
        self.assertIs(event["answered"], False)
        self.assertEqual(self.review_status()["final_revision"], "pending")
        self.assertEqual(self.status()["verdict"], "continue")

    def test_a_failed_revision_keeps_the_final_revision_pending(self):
        self.last_round()
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        self.assertEqual(self.revise_plan()[0], 1)
        del os.environ["DEV_ORCHESTRA_MOCK_FAIL"]
        self.assertEqual(ws.read_text(self.workspace.plan_path), PLAN)
        self.assertEqual(self.review_status()["final_revision"], "pending")
        self.assertEqual(self.status()["budgets"]["architect"]["used"], 1)

    def test_a_repeated_final_round_still_gets_its_revision(self):
        """The same findings twice is a stop within budget; at the limit the
        last round's findings are still folded in, and the repeat is said."""
        self.last_round("2")
        self.revise_plan()
        run_cli("review", "run", "--design")
        run_cli("review", "triage", "--design", "F1", "--status", "accepted")
        payload = self.status()
        self.assertEqual((payload["verdict"], payload["reasons"]), ("continue", []))
        self.assertEqual(payload["design_review"]["identical_rounds"], 2)
        self.assertIn("identical to the previous round", self.status_line("Design review:"))
        self.assertIn("repeated the previous round's findings", run_cli("review", "status", "--design")[1])

        self.revise_plan(self.FINAL_PLAN)
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertTrue(any("revised after the last round" in r for r in payload["reasons"]))
        self.assertTrue(any("found exactly what the previous one found" in r for r in payload["reasons"]))

    def test_status_stops_once_no_architect_attempt_is_left(self):
        self.last_round()
        run_cli("config", "set", "budgets.architect", "0")
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertTrue(any("no architect attempt left to fold them in" in r for r in payload["reasons"]))
        self.assertNotIn("architect has no attempts left", payload["reasons"])
        self.assertIn("no architect attempt is left", run_cli("review", "status", "--design")[1])
        self.assertEqual(self.review_status()["final_revision"], "blocked")

    def test_architect_exhaustion_is_a_reason_only_without_a_plan(self):
        os.remove(self.workspace.plan_path)
        run_cli("config", "set", "budgets.architect", "0")
        self.assertIn("architect has no attempts left", self.status()["reasons"])
        self.write_plan()
        self.assertNotIn("architect has no attempts left", self.status()["reasons"])

    def test_architect_exhaustion_stops_a_revision_owed_within_budget(self):
        self.last_round("2")
        run_cli("config", "set", "budgets.architect", "0")
        self.assertIsNone(self.review_status()["final_revision"])
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertIn("architect has no attempts left", payload["reasons"])

    def test_a_last_round_with_nothing_accepted_stops_as_before(self):
        run_cli("config", "set", "review.design.max_iterations", "1")
        run_cli("review", "run", "--design")
        for triage in (None, "needs-investigation"):
            if triage:
                run_cli("review", "triage", "--design", "F1", "--status", triage)
            self.assertEqual(self.review_status()["final_revision"], "unaccepted")
            self.assertIn(
                "report the remaining findings instead of looping", run_cli("review", "status", "--design")[1]
            )
            payload = self.status()
            self.assertEqual(payload["verdict"], "stop-and-report")
            self.assertIs(payload["design_review"]["final_revision_pending"], False)
            self.assertIn(
                "design review budget spent (1/1 rounds) with 1 finding(s) still open", payload["reasons"]
            )

    def test_an_architect_run_that_did_not_write_the_plan_is_not_the_revision(self):
        self.last_round()
        with open(os.path.join(self.mock_dir, "plan.txt"), "w", encoding="utf-8") as handle:
            handle.write(REVISED_PLAN)
        self.assertEqual(run_cli("run", "architect", "--prompt", "something else")[0], 0)
        self.assertEqual(run_cli("run", "architect", "--prompt", "notes", "--output", ".ai/notes.md")[0], 0)
        self.assertEqual(self.review_status()["final_revision"], "pending")
        self.assertEqual(self.status()["verdict"], "continue")

    def test_an_approval_survives_turning_the_approval_gate_off(self):
        self.last_round()
        self.revise_plan()
        self.assertEqual(run_cli("design", "approve")[0], 0)
        run_cli("config", "set", "design.require_approval", "false")
        self.assertEqual(self.status()["design_approval"]["state"], "not-required")
        self.assertEqual(self.review_status()["final_revision"], "approved")
        payload = self.status()
        self.assertEqual(payload["design_review"]["final_revision"], "approved")
        self.assertFalse(any("design review budget spent" in r for r in payload["reasons"]))

    def test_max_iterations_one_is_one_round_one_revision_then_ask(self):
        run_cli("config", "set", "implementer.provider", "mock")
        self.last_round()
        self.assertEqual(self.review_status()["final_revision"], "pending")
        self.revise_plan()
        self.assertEqual(self.review_status()["final_revision"], "done")
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertTrue(any("revised after the last round" in r for r in payload["reasons"]))
        self.assertIn("earlier revision", self.status_line("Plan approval:"))

        code, _, err = run_cli("design", "approve")
        self.assertEqual(code, 0)
        self.assertIn("earlier revision", err)
        payload = self.status()
        self.assertEqual(payload["verdict"], "continue")
        self.assertFalse(any("design review budget spent" in r for r in payload["reasons"]))
        self.assertEqual(run_cli("run", "implementer", "--prompt", "go")[0], 0)

    def test_max_iterations_zero_still_allows_the_revision_after_a_hand_run_round(self):
        run_cli("config", "set", "review.design.max_iterations", "0")
        self.assertEqual(run_cli("review", "run", "--design")[0], 3)
        self.assertEqual(run_cli("review", "run", "--design", "--force")[0], 0)
        run_cli("review", "triage", "--design", "F1", "--status", "accepted")
        self.assertEqual(self.review_status()["final_revision"], "pending")
        self.revise_plan()
        self.assertEqual(self.review_status()["final_revision"], "done")


#: A round that put the change in front of a reviewer.
REVIEWED_ROUND = {"stage": "review", "status": "ok", "reviewers": [{"invoked": True}]}


class TestRanSinceLastRound(unittest.TestCase):
    """What `status` counts as the revision, fix or re-test after the last round."""

    ROUND = REVIEWED_ROUND

    def ran(self, *events):
        return cli._ran_since_last_round(list(events), "review", "review_fixer", ("test", "re-test"))

    def test_a_run_after_the_round_counts(self):
        self.assertEqual(self.ran(self.ROUND, {"stage": "review_fixer", "status": "ok"}), (True, False))

    def test_a_run_before_the_round_does_not(self):
        self.assertEqual(self.ran({"stage": "review_fixer", "status": "ok"}, self.ROUND), (False, False))

    def test_a_round_that_reviewed_nothing_is_not_the_round(self):
        refused = {"stage": "review", "status": "refused", "reviewers": []}
        abandoned = {"stage": "review", "status": "abandoned"}
        fixed = {"stage": "review_fixer", "status": "ok"}
        self.assertEqual(self.ran(self.ROUND, fixed, refused, abandoned), (True, False))

    def test_an_ok_that_answered_nothing_does_not_count(self):
        silent = {"stage": "review_fixer", "status": "ok", "answered": False}
        self.assertEqual(self.ran(self.ROUND, silent), (False, False))
        failed = {"stage": "review_fixer", "status": "failed", "answered": False}
        self.assertEqual(self.ran(self.ROUND, failed), (False, False))

    def test_a_test_before_the_run_is_not_its_re_test(self):
        test = {"stage": "test", "status": "ok"}
        fixed = {"stage": "review_fixer", "status": "ok", "answered": True}
        self.assertEqual(self.ran(self.ROUND, test, fixed), (True, False))
        self.assertEqual(self.ran(self.ROUND, fixed, test), (True, True))

    def test_a_re_test_stage_is_accepted_too(self):
        fixed = {"stage": "review_fixer", "status": "ok"}
        self.assertEqual(self.ran(self.ROUND, fixed, {"stage": "re-test", "status": "failed"}), (True, True))


if __name__ == "__main__":
    unittest.main()
