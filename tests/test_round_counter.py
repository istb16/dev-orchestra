"""Which review a round belongs to, and when the count starts again.

The round counter lives in the consolidated report, which is per project and
outlives any one change. On its own that made it count *snapshots ever taken
here*. Reported from real use: a second branch with an unrelated change opened
at round 3 and was refused -- and `budget reset`, which says in so many words
that this is now a fresh workflow, did not help, because the one counter that
stopped the work was not in the ledger it resets. There was no supported way
to clear it.

The guard still has to work, which is the other half of every test here. A
review -> fix -> re-review loop on one change must still run out, or this
budget stops being one.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git

from orchestrator import cli
from orchestrator import ledger as ledger_mod
from orchestrator import review as review_mod
from orchestrator import workspace as ws

FINDING = """## Finding
- Severity: high
- File: app.py
- Line: 1
- Category: correctness
- Problem: the wrong operator
- Impact: every caller gets the wrong answer
- Evidence: a = 1
- Fix: use the right one
"""


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestTheLineageKey(IsolatedCase):
    """The key itself, without a review to wrap it."""

    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "a = 1\n")
        self.commit_all("init")
        self.workspace = self.cli_workspace()

    def lineage(self, workflow="w1"):
        return review_mod.review_lineage(self.workspace, workflow)

    def test_the_same_workflow_and_branch_is_the_same_review(self):
        self.assertEqual(self.lineage(), self.lineage())

    def test_a_different_workflow_is_a_different_review(self):
        self.assertNotEqual(self.lineage("w1"), self.lineage("w2"))

    def test_a_different_branch_is_a_different_review(self):
        before = self.lineage()
        self.git("checkout", "-q", "-b", "other")
        self.assertNotEqual(before, self.lineage())

    def test_the_branch_name_is_in_it(self):
        self.git("checkout", "-q", "-b", "feature/x")
        self.assertIn("feature/x", self.lineage())

    def test_a_detached_head_keys_on_nothing_rather_than_on_the_word_head(self):
        """It has no name to key on, so the counter behaves as it did before:
        carrying on is the cautious direction for a loop guard to fail in."""
        self.git("checkout", "-q", "--detach")
        self.assertNotIn("HEAD", self.lineage())

    def test_the_base_is_part_of_it(self):
        """`--base` is the other way of saying which change is under review."""
        review_mod.create_snapshot(self.workspace)
        without = self.lineage()
        self.write("app.py", "a = 2\n")
        review_mod.create_snapshot(self.workspace, base="HEAD")
        self.assertNotEqual(without, self.lineage())


class TestTheWorkflowId(IsolatedCase):
    """What `budget reset` has to change for the reset to mean anything."""

    def book(self):
        from orchestrator import config as config_mod

        return ledger_mod.Ledger(self.cli_workspace(), config_mod.default_config()["budgets"])

    def test_a_reset_within_the_same_second_still_changes_it(self):
        """It was the start time, and `utcnow` counts in seconds -- so a reset
        straight after the last one produced the same string and the reset
        silently did nothing. The test suite runs faster than a second."""
        book = self.book()
        first = book.workflow_id()
        book.reset()
        self.assertNotEqual(first, book.workflow_id())

    def test_it_is_stable_while_the_workflow_is(self):
        book = self.book()
        self.assertEqual(book.workflow_id(), book.workflow_id())

    def test_it_is_settled_on_disk_rather_than_invented_per_read(self):
        """`load` mints a fresh ledger for a workflow that has not started and
        does not write it, so reading the identity without recording it gave a
        different answer every time -- and a counter keyed on that resets
        continuously, which is no budget at all."""
        first = self.book().workflow_id()
        self.assertEqual(self.book().workflow_id(), first)


class TestNextIteration(IsolatedCase):
    """The counter, driven directly."""

    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace()

    def record(self, iteration, lineage, sha="abc"):
        ws.write_json(
            self.workspace.consolidated_json_path,
            {"iteration": iteration, "lineage": lineage, "snapshot": {"sha256": sha}},
        )
        ws.write_json(self.workspace.snapshot_meta_path, {"sha256": sha})

    def test_no_previous_review_is_round_one(self):
        self.assertEqual(review_mod.next_iteration(self.workspace, "w1"), 1)

    def test_the_same_lineage_and_snapshot_stays_in_its_round(self):
        self.record(2, "w1")
        self.assertEqual(review_mod.next_iteration(self.workspace, "w1"), 2)

    def test_the_same_lineage_and_a_new_snapshot_advances(self):
        self.record(2, "w1", sha="abc")
        ws.write_json(self.workspace.snapshot_meta_path, {"sha256": "def"})
        self.assertEqual(review_mod.next_iteration(self.workspace, "w1"), 3)

    def test_a_different_lineage_starts_again(self):
        self.record(2, "w1")
        self.assertEqual(review_mod.next_iteration(self.workspace, "w2"), 1)

    def test_a_report_written_before_lineages_existed_starts_again(self):
        """Upgrading resets the count once, which is the direction of the fix
        rather than a surprise: the alternative is honouring a number that was
        counting the wrong thing."""
        ws.write_json(self.workspace.consolidated_json_path, {"iteration": 2, "snapshot": {}})
        self.assertEqual(review_mod.next_iteration(self.workspace, "w1"), 1)

    def test_no_lineage_asked_for_behaves_as_before(self):
        """The library default. Callers that do not key on a review get the
        old counting, not a reset on every call."""
        self.record(2, "w1")
        self.assertEqual(review_mod.next_iteration(self.workspace), 2)


@unittest.skipUnless(has_git(), "git is required")
class TestTheCounterInThePipeline(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "a = 1\n")
        self.commit_all("init")
        mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(mock_dir)
        with open(os.path.join(mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING)
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = mock_dir
        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")

    def round(self, value):
        """One review round over a fresh edit."""
        self.write("app.py", "a = %d\n" % value)
        run_cli("review", "snapshot")
        return run_cli("review", "run")

    def spend_the_budget(self):
        for value in (2, 3):
            self.round(value)

    def iteration(self):
        data = ws.read_json(self.cli_workspace().consolidated_json_path, {}) or {}
        return int(data.get("iteration") or 0)

    def test_the_same_branch_still_runs_out(self):
        """The loop this budget exists to stop."""
        self.spend_the_budget()
        code, _, err = self.round(4)
        self.assertEqual(code, ledger_mod.EXIT_BUDGET_EXHAUSTED)
        self.assertIn("round 3", err)

    def test_another_branch_starts_again(self):
        """Another branch is another change, and it arrives at round one."""
        self.spend_the_budget()
        self.git("checkout", "-q", "-b", "other")
        code, out, _ = self.round(9)
        self.assertEqual(code, 0)
        self.assertIn("1 successful", out)
        self.assertEqual(self.iteration(), 1)

    def test_budget_reset_clears_it_too(self):
        """It said "this is now a fresh workflow" and then refused the first
        round of that workflow."""
        self.spend_the_budget()
        run_cli("budget", "reset")
        code, _, _ = self.round(4)
        self.assertEqual(code, 0)
        self.assertEqual(self.iteration(), 1)

    def test_a_reset_does_not_discard_the_findings(self):
        """The counter restarts; the report is a work product and stays."""
        self.spend_the_budget()
        run_cli("budget", "reset")
        self.round(4)
        self.assertTrue(json.loads(run_cli("review", "show", "--json")[1])["findings"])

    def test_a_changed_base_starts_again(self):
        self.spend_the_budget()
        run_cli("review", "snapshot", "--base", "HEAD")
        code, _, _ = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertEqual(self.iteration(), 1)

    def test_coming_back_to_a_branch_does_not_resume_its_count(self):
        """Nothing tracks a count per branch -- only whether this round
        continues the last one. Returning after working elsewhere reviews
        rather than refuses, which is the direction to fail in."""
        self.spend_the_budget()
        self.git("checkout", "-q", "-b", "other")
        self.round(9)
        self.git("checkout", "-q", "-")
        self.assertEqual(self.round(5)[0], 0)

    def test_re_running_the_same_snapshot_stays_in_its_round(self):
        """Unchanged: a reviewer failing and being run again is not a new
        round."""
        self.round(2)
        self.assertEqual(self.iteration(), 1)
        run_cli("review", "run")
        self.assertEqual(self.iteration(), 1)

    def test_an_explicit_iteration_still_wins(self):
        self.write("app.py", "a = 7\n")
        run_cli("review", "snapshot")
        run_cli("review", "run", "--iteration", "2")
        self.assertEqual(self.iteration(), 2)

    def test_the_lineage_is_recorded_beside_the_count(self):
        """So the next round can tell whether it continues this one."""
        self.round(2)
        data = ws.read_json(self.cli_workspace().consolidated_json_path, {}) or {}
        self.assertTrue(data.get("lineage"))


if __name__ == "__main__":
    unittest.main()
