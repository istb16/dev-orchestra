"""The ``review`` commands and the reviewer scorecard, end to end."""

from __future__ import annotations

import json
import os
import unittest
from typing import Any, ClassVar, Dict

from helpers import IsolatedCase, has_git, read_file, run_cli, spend_the_runtime_budget

from orchestrator import round_progress
from orchestrator import workspace as ws

FINDING = """## Finding
- Severity: high
- File: app.py
- Line: 2
- Category: correctness
- Problem: subtraction was used where addition was intended
- Impact: every caller gets the wrong total
- Evidence: return a - b
- Recommended fix: restore the addition
"""


class TestSnapshotPaths(unittest.TestCase):
    """The path lists a snapshot records, read as they are."""

    META: ClassVar[Dict[str, Any]] = {
        "files": ["lib/x.rb"],
        "withheld": [{"path": "gen/schema.rb"}],
    }

    def test_a_recorded_list_is_used_as_it_is(self):
        meta = dict(self.META, changed_paths=["lib/x.rb", "gen/schema.rb"], condition_paths=["lib/x.rb"])
        self.assertEqual(round_progress._risk_paths(meta), ["lib/x.rb", "gen/schema.rb"])
        self.assertEqual(round_progress._condition_paths(meta), ["lib/x.rb"])

    def test_a_snapshot_without_the_lists_has_no_paths(self):
        """Not rebuilt from ``files`` and ``withheld``. `review run` refuses such a snapshot."""
        self.assertEqual(round_progress._risk_paths(self.META), [])
        self.assertEqual(round_progress._condition_paths(self.META), [])
        self.assertEqual(round_progress._risk_paths({}), [])
        self.assertEqual(round_progress._condition_paths({}), [])

    def test_a_present_empty_list_is_no_paths(self):
        meta = dict(self.META, changed_paths=[], condition_paths=[])
        self.assertEqual(round_progress._risk_paths(meta), [])
        self.assertEqual(round_progress._condition_paths(meta), [])

    def test_a_string_is_not_read_as_a_list(self):
        meta = dict(self.META, changed_paths="lib/x.rb", condition_paths="lib/x.rb")
        self.assertEqual(round_progress._risk_paths(meta), [])
        self.assertEqual(round_progress._condition_paths(meta), [])


@unittest.skipUnless(has_git(), "git is required")
class TestReviewPipeline(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write("app.py", "def add(a, b):\n    return a - b\n")

        self.mock_dir = mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(mock_dir)
        with open(os.path.join(mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING)
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = mock_dir

        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "remove", "claude-security")
        run_cli("reviewer", "remove", "claude-test")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m2", "--role", "security")
        # These are about the fan-out: two reviewers, one of them failing, the
        # report surviving it. Below `quality` a change this small is reduced
        # to a single reviewer, which is the saving that level is for and
        # leaves nothing to fan out. The panel logic has its own tests.
        run_cli("config", "set", "optimization.level", "quality")

    def oversize_snapshot(self):
        """Replace the frozen diff with one too large to inline.

        The limit is lowered rather than the body made enormous. Delivery is
        decided by `review.context.inline_chars` now, and a small limit
        exercises the same branch as the shipped one without a 400KB fixture
        -- while also being the case the setting exists for: somebody who will
        not pay for very large prompts, and takes a `partial` round for it.

        The mock provider never reads a prompt, so the snapshot's size is the
        whole of what decides the round's coverage.
        """
        run_cli("config", "set", "review.context.inline_chars", "4000")
        ws.write_text(self.cli_workspace().snapshot_path, "x" * 4_001)

    def test_snapshot_then_review_then_triage_then_fix_brief(self):
        code, out, _ = run_cli("review", "snapshot")
        self.assertEqual(code, 0)
        self.assertIn("app.py", read_file(self.cli_workspace().snapshot_path))

        code, out, _ = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertIn("2 successful, 0 failed", out)

        _, shown, _ = run_cli("review", "show", "--json")
        data = json.loads(shown)
        self.assertEqual(data["counts"]["findings_total"], 1)
        self.assertEqual(data["findings"][0]["triage"], "needs-triage")

        run_cli("review", "triage", "F1", "--status", "accepted", "--note", "confirmed")
        _, brief, _ = run_cli("review", "fix-brief")
        self.assertIn("F1", brief)
        self.assertIn("restore the addition", brief)

    def edit_snapshot_meta(self, **changes: Any) -> None:
        """The snapshot on disk with ``changes`` applied; a ``None`` value drops the key."""
        path = self.cli_workspace().snapshot_meta_path
        meta = ws.read_json(path)
        for key, value in changes.items():
            if value is None:
                meta.pop(key, None)
            else:
                meta[key] = value
        ws.write_json(path, meta)

    def assert_paths_refused(self) -> None:
        code, out, err = run_cli("review", "run")
        self.assertEqual(code, 2)
        self.assertIn("refusing to run: the snapshot .ai/workflows/", err)
        self.assertIn("review-target.json", err)
        self.assertIn("dev-orchestra review snapshot", err)
        self.assertNotIn("successful", out)
        events = self.cli_workspace().read_state().get("events") or []
        self.assertFalse([event for event in events if event.get("stage") == "review"])

    def test_review_run_refuses_a_snapshot_without_changed_paths(self):
        run_cli("review", "snapshot")
        self.edit_snapshot_meta(changed_paths=None)
        self.assert_paths_refused()
        self.assertIn("no changed_paths list", run_cli("review", "run")[2])

    def test_review_run_refuses_a_snapshot_without_condition_paths(self):
        run_cli("review", "snapshot")
        self.edit_snapshot_meta(condition_paths=None)
        self.assert_paths_refused()

    def test_review_run_refuses_a_path_list_written_as_a_string(self):
        run_cli("review", "snapshot")
        self.edit_snapshot_meta(changed_paths="app.py")
        self.assert_paths_refused()

    def test_review_run_refuses_an_incremental_snapshot_without_condition_paths(self):
        """Refused the same way: an incremental round judges risk by the same lists."""
        run_cli("review", "snapshot")
        self.edit_snapshot_meta(incremental_from="0" * 40, condition_paths=None)
        self.assert_paths_refused()

    def test_review_run_takes_a_present_empty_list(self):
        run_cli("review", "snapshot")
        self.edit_snapshot_meta(changed_paths=[], condition_paths=[])
        self.assertEqual(run_cli("review", "run")[0], 0)

    def test_status_reads_a_snapshot_without_the_lists(self):
        run_cli("review", "snapshot")
        self.edit_snapshot_meta(changed_paths=None, condition_paths="app.py")
        code, out, _ = run_cli("status", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        # What `review run` would refuse, said here first rather than planned as no paths.
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertIn(
            "the snapshot has no changed_paths or condition_paths list; take a fresh one with "
            "`dev-orchestra review snapshot`",
            payload["reasons"],
        )
        self.assertEqual(run_cli("status")[0], 0)

    def test_a_round_leaves_out_the_sleep_of_every_reviewer(self):
        os.environ["DEV_ORCHESTRA_MOCK_SUSPENDED"] = "90"
        run_cli("review", "snapshot")
        self.assertEqual(run_cli("review", "run")[0], 0)
        event = self.cli_workspace().read_state()["events"][-1]
        self.assertEqual(len(event["reviewers"]), 2)
        suspended = sum(reviewer["suspended_seconds"] for reviewer in event["reviewers"])
        durations = sum(reviewer["duration_seconds"] for reviewer in event["reviewers"])
        self.assertEqual(suspended, 180)
        self.assertEqual(event["suspended_seconds"], suspended)
        self.assertAlmostEqual(event["charged_seconds"], durations - suspended, places=1)
        payload = json.loads(run_cli("budget", "show", "--json")[1])
        self.assertAlmostEqual(payload["runtime"]["used"], event["charged_seconds"], places=1)
        self.assertEqual(payload["runtime"]["suspended"], 180)

    def test_review_run_refuses_a_round_past_the_budget(self):
        run_cli("config", "set", "review.max_review_iterations", "1")
        run_cli("review", "snapshot")
        self.assertEqual(run_cli("review", "run")[0], 0)
        self.write("app.py", "def add(a, b):\n    return a * b\n")
        run_cli("review", "snapshot")
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 3)
        self.assertIn("refusing to run review round", err)
        self.assertIn("only the re-review is refused", err)

    def test_force_runs_a_round_past_the_budget(self):
        run_cli("config", "set", "review.max_review_iterations", "1")
        run_cli("review", "snapshot")
        run_cli("review", "run")
        self.write("app.py", "def add(a, b):\n    return a * b\n")
        run_cli("review", "snapshot")
        self.assertEqual(run_cli("review", "run", "--force")[0], 0)

    def test_an_identical_round_is_called_out(self):
        run_cli("review", "snapshot")
        run_cli("review", "run")
        code, _, err = run_cli("review", "run", "--force")
        self.assertEqual(code, 0)
        self.assertIn("changed nothing", err)

    def test_status_recommends_a_re_review_within_budget(self):
        run_cli("review", "snapshot")
        run_cli("review", "run", "--iteration", "1")
        _, out, _ = run_cli("review", "status", "--json")
        status = json.loads(out)
        self.assertTrue(status["re_review_recommended"])
        self.assertEqual(status["blocking"], ["F1"])

    def test_status_stops_recommending_once_the_budget_is_spent(self):
        run_cli("review", "snapshot")
        run_cli("review", "run", "--iteration", "2")
        _, out, _ = run_cli("review", "status", "--json")
        status = json.loads(out)
        self.assertFalse(status["re_review_recommended"])
        self.assertTrue(status["iteration_budget_exhausted"])

    # -- the round that reaches the limit still gets its fix and re-test --

    def last_round(self):
        run_cli("config", "set", "review_fixer.provider", "mock")
        run_cli("review", "snapshot")
        run_cli("review", "run", "--iteration", "2")
        run_cli("review", "triage", "F1", "--status", "accepted")

    def fix(self):
        return run_cli("run", "review_fixer", "--prompt", "fix")

    def review_status(self):
        return json.loads(run_cli("review", "status", "--json")[1])

    def status(self):
        return json.loads(run_cli("status", "--json")[1])

    def test_status_at_the_limit_asks_for_the_final_fix(self):
        self.last_round()
        self.assertEqual(self.review_status()["final_fix"], "pending")
        self.assertIn("fix the accepted findings once more", run_cli("review", "status")[1])
        payload = self.status()
        self.assertEqual(payload["verdict"], "continue")
        self.assertIs(payload["review"]["final_fix_pending"], True)
        self.assertTrue(self.review_line().endswith("-- final fix pending (fix, re-test, do not re-review)"))

    def review_line(self):
        """The `Review:` line of the text view."""
        out = run_cli("status")[1]
        return next(line for line in out.splitlines() if line.startswith("Review: "))

    def test_a_fix_after_the_last_round_waits_for_the_re_test(self):
        self.last_round()
        self.assertEqual(self.fix()[0], 0)
        self.assertEqual(self.review_status()["final_fix"], "retest")
        self.assertIn("re-run the tests", run_cli("review", "status")[1])
        payload = self.status()
        self.assertEqual((payload["verdict"], payload["reasons"]), ("continue", []))
        self.assertTrue(
            self.review_line().endswith("-- final fix done, re-test pending (record it, do not re-review)")
        )

    def test_the_last_fixer_attempt_does_not_stop_the_re_test(self):
        self.last_round()
        run_cli("config", "set", "budgets.review_fixer", "1")
        self.fix()
        payload = self.status()
        self.assertEqual(payload["budgets"]["review_fixer"]["remaining"], 0)
        self.assertEqual((payload["verdict"], payload["reasons"]), ("continue", []))
        self.assertEqual(payload["review"]["final_fix"], "retest")
        run_cli("state", "record", "test", "ok")
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertTrue(any("fixed and re-tested" in r for r in payload["reasons"]))

    def assert_the_re_test_ends_the_loop(self, stage, status, level=None):
        self.last_round()
        self.fix()
        run_cli("state", "record", stage, status)
        if level:
            run_cli("config", "set", "optimization.level", level)
        self.assertEqual(self.review_status()["final_fix"], "done")
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        spent = [r for r in payload["reasons"] if r.startswith("review budget spent (2/2 rounds)")]
        self.assertEqual(len(spent), 1)
        self.assertIn("re-tested", spent[0])
        return payload["reasons"]

    def test_a_recorded_re_test_ends_the_loop(self):
        reasons = self.assert_the_re_test_ends_the_loop("test", "ok")
        self.assertNotIn("the last recorded test run failed; fix it before reviewing", reasons)

    def test_a_failed_re_test_ends_the_loop_and_says_so(self):
        # `quality` lets a round run over red tests; `balanced` is where the gate refuses.
        reasons = self.assert_the_re_test_ends_the_loop("test", "failed", level="balanced")
        self.assertIn("the last recorded test run failed; fix it before reviewing", reasons)

    def test_a_re_test_recorded_under_its_own_stage_name_ends_the_loop_too(self):
        self.assert_the_re_test_ends_the_loop("re-test", "ok")

    def test_a_repeated_final_round_still_gets_its_fix(self):
        run_cli("config", "set", "review_fixer.provider", "mock")
        run_cli("review", "snapshot")
        run_cli("review", "run", "--iteration", "1")
        run_cli("review", "triage", "F1", "--status", "accepted")
        self.fix()
        run_cli("review", "run", "--iteration", "2")
        run_cli("review", "triage", "F1", "--status", "accepted")
        payload = self.status()
        self.assertEqual((payload["verdict"], payload["reasons"]), ("continue", []))
        self.assertEqual(payload["review"]["identical_rounds"], 2)
        self.assertTrue(
            self.review_line().endswith("(fix, re-test, do not re-review); identical to the previous round")
        )
        self.fix()
        run_cli("state", "record", "test", "ok")
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertTrue(any("found exactly what the previous one found" in r for r in payload["reasons"]))
        self.assertTrue(any("fixed and re-tested" in r for r in payload["reasons"]))

    def test_a_failed_fixer_run_does_not_count(self):
        self.last_round()
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        self.assertEqual(self.fix()[0], 1)
        del os.environ["DEV_ORCHESTRA_MOCK_FAIL"]
        self.assertEqual(self.review_status()["final_fix"], "pending")

    def test_an_empty_ok_fixer_run_does_not_count(self):
        self.last_round()
        with open(os.path.join(self.mock_dir, "implement.txt"), "w", encoding="utf-8") as handle:
            handle.write("")
        self.assertEqual(self.fix()[0], 1)
        events = self.cli_workspace().read_state()["events"]
        event = [e for e in events if e.get("stage") == "review_fixer"][-1]
        self.assertEqual(event["status"], "ok")
        self.assertIs(event["answered"], False)
        self.assertEqual(self.review_status()["final_fix"], "pending")

    def test_a_spent_fixer_budget_stops_before_the_fix(self):
        self.last_round()
        run_cli("config", "set", "budgets.review_fixer", "0")
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertTrue(any("no review_fixer attempt left" in r for r in payload["reasons"]))
        self.assertIn("review_fixer has no attempts left", payload["reasons"])
        self.assertIn("no review_fixer attempt is left", run_cli("review", "status")[1])

    def test_a_last_round_with_nothing_accepted_stops_as_before(self):
        """Blocking but not accepted -- untriaged or under investigation --
        leaves nothing to fix, so there is no final fix to wait for."""
        run_cli("config", "set", "review_fixer.provider", "mock")
        run_cli("review", "snapshot")
        run_cli("review", "run", "--iteration", "2")
        for triage in (None, "needs-investigation"):
            if triage:
                run_cli("review", "triage", "F1", "--status", triage)
            self.assertEqual(self.review_status()["final_fix"], "unaccepted")
            self.assertIn("report the remaining findings instead of looping", run_cli("review", "status")[1])
            payload = self.status()
            self.assertEqual(payload["verdict"], "stop-and-report")
            self.assertIs(payload["review"]["final_fix_pending"], False)
            self.assertIn("review budget spent (2/2 rounds) with 1 finding(s) still open", payload["reasons"])

    # -- review.whole_change_recheck: one whole-change round after a clean fix-only one --

    def accept_all(self):
        for finding in json.loads(run_cli("review", "show", "--json")[1])["findings"]:
            run_cli("review", "triage", finding["id"], "--status", "accepted")

    def assert_status_agrees(self, final_fix, next_review):
        """`review status` and `status` read the same report the same way."""
        review_status, payload = self.review_status(), self.status()["review"]
        self.assertEqual((review_status["final_fix"], review_status["next_review"]), (final_fix, next_review))
        self.assertEqual((payload["final_fix"], payload["next_review"]), (final_fix, next_review))

    def clean_fix_round(self, enabled, *snapshot_args):
        """Round 1 finds a High, round 2 reviews its fix and finds only a Low, which is fixed."""
        run_cli("config", "set", "review.whole_change_recheck", "true" if enabled else "false")
        run_cli("review", "snapshot", *snapshot_args)
        self.assertEqual(run_cli("review", "run")[0], 0)
        self.accept_all()
        self.assert_status_agrees(None, "fix" if enabled else None)
        self.write("app.py", "def add(a, b):\n    return a + b  # fixed\n")
        meta = json.loads(run_cli("review", "snapshot", "--json", *snapshot_args)[1])
        self.assertTrue(meta["incremental_from"])
        with open(os.path.join(self.mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING.replace("Severity: high", "Severity: low").replace("Line: 2", "Line: 1"))
        self.assertEqual(run_cli("review", "run")[0], 0)
        self.accept_all()
        self.write("app.py", '"""Adding."""\n\n\ndef add(a, b):\n    return a + b  # fixed\n')

    def test_run_allows_the_spare_round_only_on_a_recheck_snapshot(self):
        self.clean_fix_round(enabled=True)
        status = self.review_status()
        self.assertEqual((status["round_scope"], status["iteration"]), ("fix", 2))
        self.assertIs(status["whole_change_recheck"], True)
        self.assertTrue(status["re_review_recommended"])
        self.assertIn(
            "next: fix the accepted findings and re-test, then re-review the whole change",
            run_cli("review", "status")[1],
        )
        self.assert_status_agrees(None, "whole")
        meta = json.loads(run_cli("review", "snapshot", "--json")[1])
        self.assertFalse(meta["incremental_from"])
        self.assertIs(meta["recheck"], True)
        # Past the spare round the refusal names the setting's own value.
        code, _, err = run_cli("review", "run", "--iteration", "4")
        self.assertEqual(code, 3)
        self.assertIn(
            "the budget is 2 rounds (review.max_review_iterations) "
            "and one spare whole-change round (review.whole_change_recheck)",
            err,
        )
        self.assertEqual(run_cli("review", "run")[0], 0)
        status = self.review_status()
        self.assertEqual((status["round_scope"], status["iteration"]), ("whole", 3))

    def write_review(self, text):
        with open(os.path.join(self.mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def assert_no_spare_round(self, *snapshot_args, setting=None):
        """A snapshot that is whole for another reason is not the loop's recheck."""
        self.clean_fix_round(enabled=True)
        if setting:
            run_cli("config", "set", *setting)
        meta = json.loads(run_cli("review", "snapshot", "--json", *snapshot_args)[1])
        self.assertEqual((meta["scope"], meta["recheck"]), ("whole", False))
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 3)
        self.assertIn(
            "refusing to run review round 3: the budget is 2 rounds (review.max_review_iterations).", err
        )

    def test_full_gets_no_spare_round(self):
        self.assert_no_spare_round("--full")

    def test_incremental_rounds_off_gets_no_spare_round(self):
        self.assert_no_spare_round(setting=("review.incremental_rounds", "false"))

    def test_another_base_starts_a_new_review_with_no_recheck(self):
        """`--base HEAD` after a commit names another commit than the snapshot on
        disk was taken against: a new review, whose first round is no recheck,
        whatever the old report says."""
        self.clean_fix_round(enabled=True)
        self.assertEqual(self.review_status()["next_review"], "whole")
        self.commit_all("fix")
        self.write("app.py", "def add(a, b):\n    return a + b  # again\n")
        meta = json.loads(run_cli("review", "snapshot", "--json", "--base", "HEAD")[1])
        self.assertIs(meta["recheck"], False)
        self.assertEqual(run_cli("review", "run")[0], 0)
        self.assertEqual(self.review_status()["iteration"], 1)

    def test_another_name_for_the_same_base_still_gets_the_recheck(self):
        """The base is compared by commit, as the snapshot's narrowing compares it:
        the sha of `main` after `--base main` is the same change, and its
        closing round is the whole change, not the fix."""
        self.git("branch", "-M", "main")
        self.clean_fix_round(True, "--base", "main")
        sha = self.git("rev-parse", "main").stdout.strip()
        meta = json.loads(run_cli("review", "snapshot", "--json", "--base", sha)[1])
        self.assertFalse(meta["incremental_from"])
        self.assertEqual((meta["scope"], meta["recheck"]), ("whole", True))

    def test_an_untriaged_medium_after_a_fix_round_is_reviewed_whole(self):
        """`next_review` names what `review snapshot` takes: with nothing accepted
        it does not narrow, whatever the scope of the round reported."""
        run_cli("config", "set", "review.max_review_iterations", "4")
        run_cli("config", "set", "review.whole_change_recheck", "true")
        run_cli("review", "snapshot")
        run_cli("review", "run")
        self.accept_all()
        self.write("app.py", "def add(a, b):\n    return a + b  # fixed\n")
        self.assertTrue(json.loads(run_cli("review", "snapshot", "--json")[1])["incremental_from"])
        self.write_review(
            FINDING.replace("Severity: high", "Severity: medium")
            .replace("Line: 2", "Line: 1")
            .replace("subtraction was used where addition was intended", "the function has no docstring")
        )
        self.assertEqual(run_cli("review", "run")[0], 0)
        findings = json.loads(run_cli("review", "show", "--json")[1])["findings"]
        self.assertEqual([f["triage"] for f in findings], ["needs-triage"])
        status = self.review_status()
        self.assertEqual((status["round_scope"], status["next_review"]), ("fix", "whole"))
        self.assertEqual(self.status()["review"]["next_review"], "whole")
        meta = json.loads(run_cli("review", "snapshot", "--json")[1])
        self.assertFalse(meta["incremental_from"])
        self.assertEqual(meta["scope"], "whole")

    def test_two_clean_rounds_are_not_a_repeat(self):
        """Finding nothing twice -- here a round, then `--full` -- is not a fix that changed nothing."""
        self.write_review("NO_FINDINGS")
        run_cli("review", "snapshot")
        self.assertEqual(run_cli("review", "run")[0], 0)
        run_cli("review", "snapshot", "--full")
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertNotIn("changed nothing", err)
        payload = self.status()
        self.assertEqual(payload["review"]["identical_rounds"], 1)
        self.assertFalse(any("found exactly what" in r for r in payload["reasons"]), payload["reasons"])

    def test_a_round_after_the_recheck_narrows_to_its_fix(self):
        run_cli("config", "set", "review.max_review_iterations", "4")
        self.clean_fix_round(enabled=True)
        self.assertIs(json.loads(run_cli("review", "snapshot", "--json")[1])["recheck"], True)
        self.write_review(FINDING.replace("Severity: high", "Severity: medium"))
        self.assertEqual(run_cli("review", "run")[0], 0)
        self.accept_all()
        self.assert_status_agrees(None, "fix")
        self.write("app.py", '"""Adding numbers."""\n\n\ndef add(a, b):\n    return a + b  # fixed\n')
        meta = json.loads(run_cli("review", "snapshot", "--json")[1])
        self.assertTrue(meta["incremental_from"])
        self.assertEqual((meta["scope"], meta["recheck"]), ("fix", False))

    def test_a_clean_recheck_after_a_clean_fix_round_is_not_a_repeat(self):
        run_cli("config", "set", "review.whole_change_recheck", "true")
        run_cli("review", "snapshot")
        run_cli("review", "run")
        self.accept_all()
        self.write("app.py", "def add(a, b):\n    return a + b  # fixed\n")
        run_cli("review", "snapshot")
        self.write_review("NO_FINDINGS")
        self.assertEqual(run_cli("review", "run")[0], 0)
        self.assertEqual(self.review_status()["next_review"], "whole")
        run_cli("review", "snapshot")
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertNotIn("produced the same findings", err)
        status = self.review_status()
        self.assertEqual((status["round_scope"], status["iteration"], status["wanted"]), ("whole", 3, []))
        payload = self.status()
        self.assertEqual(payload["review"]["identical_rounds"], 1)
        self.assertFalse(any("found exactly what" in r for r in payload["reasons"]), payload["reasons"])
        self.assertIn(
            "next: finish -- nothing above Low is open; do not re-review", run_cli("review", "status")[1]
        )

    def test_the_spare_round_is_refused_with_the_option_off(self):
        self.clean_fix_round(enabled=False)
        self.assertFalse(self.review_status()["re_review_recommended"])
        self.assertNotIn("next:", run_cli("review", "status")[1])
        self.assert_status_agrees(None, None)
        self.assertTrue(json.loads(run_cli("review", "snapshot", "--json")[1])["incremental_from"])
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 3)
        self.assertIn("refusing to run review round 3", err)

    def test_a_recheck_round_past_the_budget_gets_the_final_fix(self):
        self.clean_fix_round(enabled=True)
        run_cli("review", "snapshot")
        with open(os.path.join(self.mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING.replace("Severity: high", "Severity: medium"))
        self.assertEqual(run_cli("review", "run")[0], 0)
        self.accept_all()
        status = self.review_status()
        self.assertTrue(status["iteration_budget_exhausted"])
        self.assertFalse(status["re_review_recommended"])
        self.assert_status_agrees("pending", None)
        out = run_cli("review", "status")[1]
        self.assertIn("fix the accepted findings once more", out)
        self.assertIn("next: do not re-review -- the iteration budget is spent", out)
        # The Medium is below review.re_review_severities but keeps the loop open.
        payload = self.status()
        self.assertEqual((payload["verdict"], payload["reasons"]), ("continue", []))
        self.assertEqual(payload["review"]["blocking"], [])
        self.assertEqual(len(payload["review"]["wanted"]), 1)
        self.assertEqual(self.review_status()["wanted"], payload["review"]["wanted"])
        run_cli("config", "set", "review_fixer.provider", "mock")
        self.assertEqual(self.fix()[0], 0)
        run_cli("state", "record", "test", "ok")
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        spent = "review budget spent (3/3 rounds) with 1 finding(s) still open; fixed and re-tested"
        self.assertTrue(any(r.startswith(spent) for r in payload["reasons"]), payload["reasons"])
        # The spare round is spent: a fourth is refused like any round past the budget.
        self.write("app.py", "def add(a, b):\n    return a + b\n\n\nX = 1\n")
        self.assertIs(json.loads(run_cli("review", "snapshot", "--json")[1])["recheck"], False)
        # The cap of the round reported is its own, not the new snapshot's.
        payload = self.status()
        self.assertTrue(any(r.startswith(spent) for r in payload["reasons"]), payload["reasons"])
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 3)
        self.assertIn("refusing to run review round 4", err)
        self.assertEqual(run_cli("review", "run", "--force")[0], 0)
        self.assertEqual(self.review_status()["iteration"], 4)

    def test_a_clean_whole_change_round_finishes(self):
        """The accepted Lows it leaves get the final pass: fix, re-test, report."""
        run_cli("config", "set", "review.whole_change_recheck", "true")
        # At the cap: a finished review is still not an exhausted one.
        run_cli("config", "set", "review.max_review_iterations", "1")
        run_cli("config", "set", "review_fixer.provider", "mock")
        with open(os.path.join(self.mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING.replace("Severity: high", "Severity: low"))
        run_cli("review", "snapshot")
        run_cli("review", "run")
        self.accept_all()
        status = self.review_status()
        self.assertEqual(status["round_scope"], "whole")
        self.assertFalse(status["re_review_recommended"])
        self.assertIs(status["iteration_budget_exhausted"], False)
        self.assert_status_agrees("pending", None)
        out = run_cli("review", "status")[1]
        self.assertIn("next: fix the accepted findings, re-test, and finish -- do not re-review", out)
        self.assertNotIn("iteration budget exhausted", out)
        self.assertEqual((self.status()["verdict"], self.status()["reasons"]), ("continue", []))
        self.assertEqual(self.fix()[0], 0)
        self.assert_status_agrees("retest", None)
        self.assertIn("next: re-run the tests", run_cli("review", "status")[1])
        run_cli("state", "record", "test", "ok")
        self.assert_status_agrees("done", None)
        self.assertIn("next: finish -- nothing above Low was open", run_cli("review", "status")[1])
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertIn(
            "review finished with nothing above Low (1/1 rounds); its accepted findings were fixed "
            "and re-tested, not re-reviewed -- report",
            payload["reasons"],
        )
        self.assertFalse(any("budget spent" in r for r in payload["reasons"]), payload["reasons"])

    def test_a_clean_whole_change_round_with_nothing_accepted_has_no_final_fix(self):
        run_cli("config", "set", "review.whole_change_recheck", "true")
        self.write_review("NO_FINDINGS")
        run_cli("review", "snapshot")
        run_cli("review", "run")
        self.assert_status_agrees(None, None)
        self.assertEqual((self.status()["verdict"], self.status()["reasons"]), ("continue", []))

    def test_partial_reviewer_failure_still_produces_a_report(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "Reviewer: m2 |"
        run_cli("review", "snapshot")
        code, out, _ = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertIn("1 successful, 1 failed", out)
        data = json.loads(run_cli("review", "show", "--json")[1])
        self.assertEqual(data["counts"]["reviewers_failed"], 1)
        self.assertEqual(data["counts"]["findings_total"], 1)

    def test_every_reviewer_failing_returns_non_zero(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        run_cli("review", "snapshot")
        code, _, _ = run_cli("review", "run")
        self.assertEqual(code, 1)

    def test_a_round_that_could_not_inline_the_change_is_not_reported_clean(self):
        """Exit 1 for the same reason every reviewer failing does: the round
        produced no claim that the change is fine. The findings are still in
        the report, and still have to be triaged."""
        run_cli("review", "snapshot")
        self.oversize_snapshot()
        code, out, _ = run_cli("review", "run")
        self.assertEqual(code, 1)
        self.assertIn("PARTIAL", out)
        self.assertIn("0 successful, 0 failed, 2 partial (change handed over as a file)", out)
        data = json.loads(run_cli("review", "show", "--json")[1])
        self.assertEqual(data["counts"]["reviewers_partial"], 2)
        self.assertEqual(data["counts"]["reviewers_failed"], 0)
        self.assertEqual(data["counts"]["reviewers_ok"], 0)
        self.assertEqual(data["counts"]["findings_total"], 1)
        self.assertEqual(data["coverage"]["round"], "unverified")
        self.assertEqual(data["coverage"]["unverified_since"], 1)

    def test_the_json_output_separates_partial_from_failed(self):
        run_cli("review", "snapshot")
        self.oversize_snapshot()
        payload = json.loads(run_cli("review", "run", "--json")[1])
        self.assertEqual(payload["partial"], 2)
        self.assertEqual(payload["failed"], 0)
        self.assertEqual(payload["ok"], 0)
        self.assertEqual([r["delivery"] for r in payload["reviewers"]], ["file", "file"])

    def test_an_ordinary_round_reports_its_coverage_and_says_nothing_new(self):
        run_cli("review", "snapshot")
        code, out, _ = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertIn("2 successful, 0 failed", out)
        self.assertNotIn("partial", out)
        coverage = json.loads(run_cli("review", "show", "--json")[1])["coverage"]
        self.assertEqual(coverage["round"], "complete")
        self.assertEqual(coverage["change"], "complete")
        self.assertIsNone(coverage["unverified_since"])
        self.assertGreater(coverage["change_chars"], 0)

    def test_status_reports_coverage_and_what_would_clear_it(self):
        run_cli("review", "snapshot")
        self.oversize_snapshot()
        run_cli("review", "run")
        _, out, _ = run_cli("review", "status")
        self.assertIn("coverage: round unverified, change unverified", out)
        self.assertIn("not a clean review: 2 reviewer(s) partial", out)
        self.assertIn("split the change and review the parts", out)
        # The other way out, and the one the report cannot name for itself:
        # the limit is a setting, so a reader who decides the prompt is worth
        # paying for raises it rather than cutting the change up.
        self.assertIn("raise review.context.inline_chars", out)
        self.assertIn("<= 4,000 chars", out)
        # Narrowing the diff makes the round smaller, not the change reviewed:
        # `--base` changes what is shown and nothing about what was read, so
        # naming it here would be pointing at the way around the mark.
        self.assertNotIn("--base", out)
        status = json.loads(run_cli("review", "status", "--json")[1])
        self.assertEqual(status["coverage"]["change"], "unverified")
        self.assertEqual(status["reviewers_partial"], 2)

    def test_status_says_the_limit_was_raised_rather_than_repeating_itself(self):
        """The reader who took the advice and raised the limit must not be
        handed it again. The recorded size now fits, so the same snapshot
        would be inlined -- "re-running gives the same answer" is false, the
        change no longer needs splitting, and the limit is already raised."""
        run_cli("review", "snapshot")
        self.oversize_snapshot()
        run_cli("review", "run")
        run_cli("config", "set", "review.context.inline_chars", "10000")
        _, out, _ = run_cli("review", "status")
        self.assertIn("under a lower review.context.inline_chars", out)
        self.assertIn("The limit is 10,000 chars now", out)
        self.assertIn("run review run against it again", out)
        self.assertNotIn("gives the same answer", out)
        self.assertNotIn("split the change", out)

    def test_coverage_names_the_limit_that_handed_the_body_over_after_only(self):
        """`--only` merges entries made under two configurations into one
        snapshot's table, so the freshest entry is not necessarily the one
        whose delivery made the round unverified. The pair printed as the
        round's two numbers has to come off the entry that decided the mark --
        4,001 chars against a limit of 10,000 would not have been a file."""
        run_cli("review", "snapshot")
        self.oversize_snapshot()
        run_cli("review", "run")
        run_cli("config", "set", "review.context.inline_chars", "10000")
        self.assertEqual(run_cli("review", "run", "--only", "m1")[0], 0)
        data = json.loads(run_cli("review", "show", "--json")[1])
        by_id = {entry["id"]: entry for entry in data["reviewers"]}
        self.assertEqual(by_id["m1"]["delivery"], "inline")
        self.assertEqual(by_id["m2"]["delivery"], "file")
        self.assertEqual(data["coverage"]["round"], "unverified")
        self.assertEqual(data["coverage"]["change_chars"], 4_001)
        self.assertEqual(data["coverage"]["inline_chars"], 4_000)
        report = read_file(self.cli_workspace().consolidated_md_path)
        self.assertIn("review.context.inline_chars 4,000", report)

    def test_status_survives_an_inline_chars_it_cannot_read(self):
        """`review status` loads with `validate_result=False` on purpose: it
        is the one review command meant to answer while the config is broken.
        Coercing the setting with a bare `int()` made it the one that died on
        one, with a `ValueError` traceback `main` does not catch."""
        run_cli("review", "snapshot")
        self.oversize_snapshot()
        run_cli("review", "run")
        run_cli("config", "set", "--raw", "review.context.inline_chars", "400k")
        code, out, _ = run_cli("review", "status")
        self.assertEqual(code, 0)
        self.assertIn("not a clean review: 2 reviewer(s) partial", out)
        # The shipped default stands in for the value nobody can read, and the
        # advice is built from it like any other number.
        self.assertIn("The limit is 400,000 chars now", out)

    def test_status_and_the_report_agree_about_a_snapshot_nobody_has_run(self):
        """`review snapshot --full` then `review consolidate` -- the state the
        first fix to this created. What is missing is a reviewer, and both
        readers of the report have to say so: telling the reader to re-snapshot
        with --full sends them to redo the thing they just did."""
        run_cli("review", "snapshot")
        self.oversize_snapshot()
        run_cli("review", "run")
        self.write("app.py", "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
        run_cli("review", "snapshot", "--full")
        _, report, _ = run_cli("review", "consolidate")
        _, status, _ = run_cli("review", "status")
        for text in (report, status):
            self.assertIn("change unverified since round 1", text)
            self.assertIn("no reviewer has run against this snapshot", text)
            self.assertIn("review run", text)
            self.assertNotIn("--full", text)
        # The tally beside it counts the snapshot the coverage line is about.
        self.assertIn("- Reviewers: 0 ok / 2 total, 2 partial", report)
        self.assertIn("- Reviewers of this snapshot: 0 ok / 0 total", report)

    def test_status_before_any_review_reports_no_coverage_rather_than_a_clean_one(self):
        status = json.loads(run_cli("review", "status", "--json")[1])
        self.assertEqual(status["coverage"]["round"], "none")
        self.assertEqual(status["coverage"]["change"], "none")
        self.assertIsNone(status["coverage"]["unverified_since"])
        self.assertEqual(status["reviewers_partial"], 0)

    def test_status_does_not_report_an_unrecorded_coverage_as_none(self):
        """A report from before coverage was recorded is a real round with real
        findings. Calling it `round none, change none` claims it was measured
        and found empty -- `render_consolidation` leaves the line out for that
        reason, and the two commands read the same file."""
        run_cli("review", "snapshot")
        run_cli("review", "run")
        path = self.cli_workspace().consolidated_json_path
        data = json.loads(read_file(path))
        data.pop("coverage")
        ws.write_json(path, data)
        status = json.loads(run_cli("review", "status", "--json")[1])
        self.assertIsNone(status["coverage"])
        _, out, _ = run_cli("review", "status")
        self.assertIn("coverage: not recorded", out)
        self.assertNotIn("round none", out)

    def test_a_round_charges_every_reviewer_it_ran(self):
        """Two reviewers in parallel for 0.2s each delegated 0.4s, not 0.2s."""
        os.environ["DEV_ORCHESTRA_MOCK_DELAY"] = "0.2"
        run_cli("review", "snapshot")
        self.assertEqual(run_cli("review", "run")[0], 0)
        event = self.cli_workspace().read_state()["events"][-1]
        expected = sum(reviewer["duration_seconds"] for reviewer in event["reviewers"])
        self.assertEqual(len(event["reviewers"]), 2)
        # Loosely, because the charge is the sum of the raw measurements and
        # the reviewer rows are each rounded: the point is that it is the sum
        # of two, not the length of the batch.
        self.assertAlmostEqual(event["charged_seconds"], expected, places=1)
        payload = json.loads(run_cli("budget", "show", "--json")[1])
        self.assertAlmostEqual(payload["runtime"]["used"], expected, places=1)

    def test_a_round_is_refused_once_the_runtime_budget_is_spent(self):
        """Review is the biggest consumer of runtime and never asked before."""
        run_cli("review", "snapshot")
        workspace = self.cli_workspace()
        spend_the_runtime_budget(workspace)
        before = len(workspace.read_state().get("events") or [])
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 3)
        self.assertIn("refusing to run review", err)
        self.assertIn("delegated", err)
        state = workspace.read_state()
        self.assertEqual(state["ledger"]["in_flight"], {})
        self.assertEqual(len(state.get("events") or []), before)
        self.assertFalse(os.path.exists(workspace.reviewer_report_path("m1")))
        self.assertEqual(run_cli("review", "run", "--force")[0], 0)

    def test_a_round_that_never_starts_is_charged_nothing(self):
        from orchestrator import workspace as workspace_mod

        workspace = self.cli_workspace()
        workspace_mod.write_text(workspace.snapshot_path, "")
        code, _, _ = run_cli("review", "run")
        self.assertEqual(code, 2)
        state = workspace.read_state()
        self.assertEqual(state["events"][-1]["status"], "failed")
        self.assertEqual(state["events"][-1]["charged_seconds"], 0)
        self.assertEqual(state["ledger"]["runtime_seconds"], 0)

    def test_zero_reviewers_skips_the_stage_without_failing(self):
        run_cli("reviewer", "remove", "m1")
        run_cli("reviewer", "remove", "m2")
        run_cli("review", "snapshot")
        code, out, _ = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertIn("No reviewers configured", out)

    def test_empty_snapshot_is_reported(self):
        self.commit_all("commit the change")
        code, out, _ = run_cli("review", "snapshot")
        self.assertEqual(code, 1)
        self.assertIn("empty", out)

    def test_only_filter_runs_a_subset(self):
        run_cli("review", "snapshot")
        _, out, _ = run_cli("review", "run", "--only", "m2")
        self.assertIn("1 successful", out)

    def test_summary_lists_stages_and_models(self):
        run_cli("review", "snapshot")
        run_cli("review", "run")
        _, out, _ = run_cli("summary")
        self.assertIn("Workflow:", out)
        self.assertIn("Models:", out)
        self.assertIn("Architect", out)

    def test_workspace_is_self_ignoring_by_default(self):
        run_cli("review", "snapshot")
        self.assertTrue(os.path.isfile(os.path.join(self.project, ".ai", ".gitignore")))
        tracked = self.git("status", "--porcelain").stdout
        self.assertNotIn(".ai/", tracked)


@unittest.skipUnless(has_git(), "git is required")
class TestTheScorecardThroughTheCli(IsolatedCase):
    """What each round found and what the owner kept of it, read back by
    `optimization report` from the archived report of every round."""

    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write("app.py", "def add(a, b):\n    return a - b\n")

        mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(mock_dir)
        with open(os.path.join(mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING)
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = mock_dir

        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "remove", "claude-security")
        run_cli("reviewer", "remove", "claude-test")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m2", "--role", "security")
        # Both reviewers on every round: the panel reduction has its own tests.
        run_cli("config", "set", "optimization.level", "quality")
        self.workspace = self.cli_workspace()

    def scorecard(self, *before):
        return json.loads(run_cli(*before, "optimization", "report", "--json")[1])["scorecard"]

    def reviewed_and_accepted(self):
        run_cli("review", "snapshot")
        run_cli("review", "run")
        run_cli("review", "triage", "F1", "--status", "accepted")

    def archived(self):
        return ws.list_files(self.workspace.rounds_dir, ".json")

    def test_a_triaged_round_is_scored_from_its_archive(self):
        self.reviewed_and_accepted()
        card = self.scorecard()["code"]
        self.assertEqual(card["reviewers"]["m1"]["accepted"], 1)
        self.assertEqual(card["panel"]["accepted"], 1)
        self.assertEqual((card["rounds_read"], card["rounds_recorded"]), (1, 1))

        round_id = ws.read_json(self.workspace.snapshot_meta_path)["round_id"]
        self.assertTrue(round_id)
        live = ws.read_json(self.workspace.consolidated_json_path)
        self.assertEqual(live["snapshot"]["round_id"], round_id)
        events = [event for event in self.workspace.read_state()["events"] if event.get("stage") == "review"]
        self.assertEqual(events[-1]["round_id"], round_id)
        self.assertEqual(len(self.archived()), 1)
        self.assertTrue(ws.read_json(self.archived()[0])["findings"][0]["triage_set_at"])

    def test_putting_a_finding_back_withdraws_its_acceptance(self):
        self.reviewed_and_accepted()
        run_cli("review", "triage", "F1", "--status", "needs-triage")
        panel = self.scorecard()["code"]["panel"]
        self.assertEqual((panel["accepted"], panel["open"]), (0, 1))

    def test_the_same_tree_frozen_again_is_a_second_round_not_a_second_finding(self):
        self.reviewed_and_accepted()
        run_cli("review", "snapshot")
        run_cli("review", "run")
        self.assertEqual(len(self.archived()), 2)
        card = self.scorecard()["code"]
        self.assertEqual((card["rounds_recorded"], card["rounds_read"]), (2, 2))
        self.assertEqual(card["panel"]["accepted"], 1)

    def test_a_round_every_reviewer_failed_is_declared_unreviewed(self):
        """Its report is archived all the same, and holds the last round's findings."""
        self.reviewed_and_accepted()
        run_cli("review", "snapshot")
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        run_cli("review", "run")
        del os.environ["DEV_ORCHESTRA_MOCK_FAIL"]
        card = self.scorecard()["code"]
        self.assertEqual((card["rounds_recorded"], card["rounds_read"], card["rounds_unreviewed"]), (2, 1, 1))
        self.assertEqual(card["panel"]["accepted"], 1)
        self.assertEqual(card["reviewers"]["m1"]["failed_runs"], 1)
        _, out, _ = run_cli("optimization", "report")
        self.assertIn("1 of 2 recorded round(s) had a report to read; 1 round(s) no reviewer reviewed.", out)

    def test_a_rerun_with_only_stays_one_round(self):
        run_cli("review", "snapshot")
        run_cli("review", "run")
        run_cli("review", "run", "--only", "m2")
        self.assertEqual(len(self.archived()), 1)
        card = self.scorecard()["code"]
        self.assertEqual(card["rounds_recorded"], 1)
        self.assertEqual(card["panel"]["runs"], 3)
        self.assertEqual(card["reviewers"]["m2"]["runs"], 2)

    def test_every_workflow_is_read_and_workflow_narrows_it_to_one(self):
        self.reviewed_and_accepted()
        other = ws.Workspace(self.project, workflow="elsewhere").ensure()
        run = {"id": "x", "status": "ok", "snapshot": "b" * 12, "usage": {"billed_tokens": 10}}
        other.record_event("review", "ok", {"iteration": 1, "round_id": "r", "reviewers": [run]})
        finding = {"id": "F1", "key": "k", "reported_by": ["x"], "triage": "accepted"}
        report = {"iteration": 1, "snapshot": {"sha256": "b" * 64, "round_id": "r"}, "findings": [finding]}
        ws.write_json(other.consolidated_json_path, report)

        both = self.scorecard()["code"]
        self.assertEqual((both["rounds_read"], both["workflows_read"]), (2, 2))
        one = self.scorecard("--workflow", "elsewhere")["code"]
        self.assertEqual((one["rounds_read"], one["workflows_read"]), (1, 1))
        self.assertEqual(sorted(one["reviewers"]), ["x"])

    def test_a_workflow_with_no_report_prints_no_scorecard(self):
        """A table of zeroes would read as a panel that found nothing."""
        run = {"id": "m1", "status": "ok", "snapshot": "c" * 12, "usage": {"billed_tokens": 10}}
        self.workspace.record_event("review", "ok", {"iteration": 1, "reviewers": [run]})
        _, out, _ = run_cli("optimization", "report")
        self.assertNotIn("Reviewer scorecard", out)
        self.assertNotIn("Review effort", out)
        self.assertEqual(self.scorecard()["code"]["rounds_read"], 0)

    def test_the_text_says_what_the_figures_can_and_cannot_claim(self):
        self.reviewed_and_accepted()
        _, out, _ = run_cli("optimization", "report")
        self.assertIn("Reviewer scorecard, code review: 1 of 1 recorded round(s) had a report to read.", out)
        self.assertIn("Review effort, code and design together:", out)
        self.assertIn("rates withheld under 10 decided", out)
        self.assertIn("upper bound", out)
        self.assertNotIn("biased", out)
        self.assertNotIn("either way", out)
        self.assertNotIn("understates", out)
        for line in out.splitlines():
            if "floor" in line:
                self.assertIn("Cost totals are floors", line)

    def test_a_round_with_no_report_to_read_is_declared(self):
        self.reviewed_and_accepted()
        run = {"id": "m1", "status": "ok", "snapshot": "c" * 12, "usage": {"billed_tokens": 10}}
        self.workspace.record_event("review", "ok", {"iteration": 2, "round_id": "gone", "reviewers": [run]})
        _, out, _ = run_cli("optimization", "report")
        self.assertIn("1 of 2 recorded round(s) had a report to read.", out)
        self.assertIn("biased", out)
        self.assertIn("either way", out)
        self.assertNotIn("understates", out)


if __name__ == "__main__":
    unittest.main()
