"""``review.start_before_tests``: a round started before the tests, used once they pass on its tree."""

from __future__ import annotations

import json
import os
import shutil
import unittest
from unittest import mock

from helpers import IsolatedCase, has_git, run_cli

from orchestrator import review_tests
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


class TestRoundVerdictWithoutATree(unittest.TestCase):
    def test_a_snapshot_without_a_tree_holds_nothing_back(self):
        verdict = review_tests.round_verdict(None, {})  # type: ignore[arg-type]
        self.assertEqual(verdict["state"], review_tests.UNCHECKED)
        self.assertEqual(review_tests.refusal(verdict, "accept"), "")
        self.assertEqual(review_tests.next_step(verdict), "")

    def test_an_empty_tree_is_held_back(self):
        snapshot = {"tested_tree": "", "taken_at": "x"}
        verdict = review_tests.round_verdict(None, snapshot)  # type: ignore[arg-type]
        self.assertEqual(verdict["state"], review_tests.CHANGED)
        self.assertIn("git could not record the reviewed tree", verdict["detail"])
        self.assertTrue(review_tests.refusal(verdict, "accept"))
        self.assertTrue(review_tests.needs_new_round(verdict))

    def test_an_unreadable_snapshot_block_is_held_back(self):
        verdict = review_tests.report_verdict(None, {"snapshot": "garbage"})  # type: ignore[arg-type]
        self.assertEqual((verdict or {}).get("state"), review_tests.CHANGED)

    def test_a_report_without_a_snapshot_block_is_unchecked(self):
        verdict = review_tests.report_verdict(None, {"findings": []})  # type: ignore[arg-type]
        self.assertEqual((verdict or {}).get("state"), review_tests.UNCHECKED)


class TestSameTree(unittest.TestCase):
    def setUp(self):
        self.workspace = mock.Mock(root="repo", dir=os.path.join("repo", ".ai"))
        self.workspace.relative.return_value = ".ai"

    def same(self, out, code=0):
        with mock.patch.object(ws, "git", return_value=(code, out, "")) as git:
            result = review_tests.same_tree(self.workspace, "a" * 40, "b" * 40)
        git.assert_called_once()
        return result

    def test_git_failing_is_not_the_same_tree(self):
        self.assertFalse(self.same("", code=1))

    def test_an_odd_field_count_is_not_the_same_tree(self):
        self.assertFalse(self.same("M\0app.py\0D\0"))

    def test_workspace_and_config_changes_are_the_same_tree(self):
        self.assertTrue(self.same("M\0.ai/state.json\0D\0.ai/old.json\0M\0.dev-orchestra.yaml\0"))

    def test_an_added_file_is_not(self):
        self.assertFalse(self.same("A\0test-results.xml\0"))

    def test_an_ordinary_change_is_not(self):
        self.assertFalse(self.same("M\0.ai/state.json\0M\0app.py\0"))

    def test_identical_trees_ask_no_git(self):
        with mock.patch.object(ws, "git") as git:
            self.assertTrue(review_tests.same_tree(self.workspace, "a" * 40, "a" * 40))
        git.assert_not_called()

    def test_no_tree_records_nothing_when_git_cannot_write_one(self):
        with mock.patch.object(ws, "write_tree", return_value=""):
            self.assertEqual(review_tests.tree_detail("repo"), {})


TAKEN = "2026-01-01T00:00:04Z"


class TestLastOn(unittest.TestCase):
    def test_only_results_since_the_snapshot_and_the_newest_before_are_diffed(self):
        events = [
            {"stage": "test", "status": "ok", "tree": "old1", "at": "2026-01-01T00:00:01Z"},
            {"stage": "test", "status": "ok", "tree": "old2", "at": "2026-01-01T00:00:02Z"},
            {"stage": "test", "status": "ok", "tree": "new1", "at": "2026-01-01T00:00:05Z"},
        ]
        with mock.patch.object(review_tests, "same_tree", return_value=False) as same:
            found = review_tests._last_on(None, events, "reviewed", TAKEN)  # type: ignore[arg-type]
        self.assertIsNone(found)
        self.assertEqual([call.args[2] for call in same.call_args_list], ["new1", "old2"])

    def test_an_exact_match_asks_no_git(self):
        events = [{"stage": "test", "status": "failed", "tree": "reviewed", "at": "2026-01-01T00:00:01Z"}]
        with mock.patch.object(review_tests, "same_tree") as same:
            found = review_tests._last_on(None, events, "reviewed", TAKEN)  # type: ignore[arg-type]
        self.assertIs(found, events[0])
        same.assert_not_called()


@unittest.skipUnless(has_git(), "git is required")
class TestStartBeforeTests(IsolatedCase):
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
        for reviewer in ("claude-general", "codex-general", "claude-security", "claude-test"):
            run_cli("reviewer", "remove", reviewer)
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        run_cli("config", "set", "optimization.level", "balanced")
        run_cli("config", "set", "review.start_before_tests", "true")

    def start_round(self):
        self.assertEqual(run_cli("review", "snapshot")[0], 0)
        code, out, err = run_cli("review", "run")
        self.assertEqual(code, 0, out + err)
        return err

    def triage(self):
        return run_cli("review", "triage", "F1", "--status", "accepted")

    def round_tests(self):
        return json.loads(run_cli("review", "status", "--json")[1]).get("round_tests")

    def edit_report(self, change):
        path = self.cli_workspace().consolidated_json_path
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        change(data)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def taken_later(self):
        # The times are to the second: a result recorded just before the
        # snapshot may read as recorded since. Move the snapshot past it.
        self.edit_report(lambda data: data["snapshot"].update(taken_at="9999-12-31T23:59:59Z"))

    def test_the_round_waits_for_a_test_result_on_its_tree(self):
        err = self.start_round()
        self.assertIn("the tests run alongside this round (review.start_before_tests)", err)
        self.assertEqual(self.round_tests()["state"], review_tests.PENDING)
        self.assertIn("next: record the tests on this tree", run_cli("review", "status")[1])
        code, _, err = self.triage()
        self.assertEqual(code, 2)
        self.assertIn("no test result has been recorded on its tree yet", err)
        self.assertEqual(run_cli("review", "fix-brief")[0], 2)

        self.assertEqual(run_cli("state", "record", "test", "ok")[0], 0)
        self.assertEqual(self.round_tests()["state"], review_tests.USABLE)
        self.assertEqual(self.triage()[0], 0)
        code, brief, _ = run_cli("review", "fix-brief")
        self.assertEqual(code, 0)
        self.assertIn("F1", brief)

    def test_the_result_names_the_tree_it_ran_on(self):
        for stage in review_tests.TEST_STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(run_cli("state", "record", stage, "ok")[0], 0)
                events = json.loads(run_cli("state", "show", "--json")[1])["events"]
                self.assertEqual(events[-1].get("stage"), stage)
                self.assertRegex(events[-1].get("tree", ""), r"^[0-9a-f]{40}$")

    def test_a_tree_given_by_hand_is_refused(self):
        for stage in review_tests.TEST_STAGES:
            with self.subTest(stage=stage):
                code, _, err = run_cli("state", "record", stage, "ok", "--detail", "tree=abc")
                self.assertEqual(code, 2)
                self.assertIn("a test result's tree is read from the working tree", err)

    def test_a_tree_given_by_hand_is_refused_with_the_option_off(self):
        run_cli("config", "set", "review.start_before_tests", "false")
        self.assertEqual(run_cli("state", "record", "test", "ok", "--detail", "tree=x")[0], 2)

    def test_a_retest_on_the_tree_makes_the_round_usable(self):
        self.start_round()
        run_cli("state", "record", "re-test", "ok")
        self.assertEqual(self.round_tests()["state"], review_tests.USABLE)

    def test_a_change_made_while_the_tests_ran_drops_the_round(self):
        self.start_round()
        self.write("app.py", "def add(a, b):\n    return a - b  # lint\n")
        run_cli("state", "record", "test", "ok")
        status = json.loads(run_cli("review", "status", "--json")[1])
        self.assertEqual(status["round_tests"]["state"], review_tests.CHANGED)
        # Thrown away: the loop reviews the whole change again.
        self.assertTrue(status["re_review_recommended"])
        self.assertEqual(status["next_review"], "whole")
        self.assertEqual(json.loads(run_cli("status", "--json")[1])["review"]["next_review"], "whole")
        text = run_cli("review", "status")[1]
        self.assertIn("next: this round is not used", text)
        self.assertIn("re-review recommended: yes", text)
        code, _, err = self.triage()
        self.assertEqual(code, 2)
        self.assertIn("ran on a different tree", err)

    def test_a_failure_on_the_tree_drops_the_round_until_it_passes_there(self):
        self.start_round()
        run_cli("state", "record", "test", "failed")
        self.assertEqual(self.round_tests()["state"], review_tests.FAILED)
        self.assertEqual(self.triage()[0], 2)
        self.assertTrue(json.loads(run_cli("review", "status", "--json")[1])["re_review_recommended"])
        # Run again on the same tree (a flaky test): the last result decides.
        run_cli("state", "record", "test", "ok")
        self.assertEqual(self.triage()[0], 0)

    def test_a_failure_after_a_pass_on_the_tree_drops_the_round(self):
        self.start_round()
        run_cli("state", "record", "test", "ok")
        run_cli("state", "record", "test", "failed")
        self.assertEqual(self.round_tests()["state"], review_tests.FAILED)
        self.assertEqual(self.triage()[0], 2)

    def test_a_failure_on_another_tree_after_the_pass_does_not_drop_the_round(self):
        self.start_round()
        run_cli("state", "record", "test", "ok")
        self.write("app.py", "def add(a, b):\n    return a * b\n")
        run_cli("state", "record", "test", "failed")
        self.assertEqual(self.round_tests()["state"], review_tests.USABLE)

    def test_the_round_keeps_counting_after_the_fix_changes_the_tree(self):
        self.start_round()
        run_cli("state", "record", "test", "ok")
        self.assertEqual(self.triage()[0], 0)
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        run_cli("state", "record", "test", "ok")
        self.assertEqual(self.round_tests()["state"], review_tests.USABLE)

    def test_a_failure_on_an_earlier_tree_does_not_refuse_the_round(self):
        run_cli("state", "record", "test", "failed")
        self.write("app.py", "def add(a, b):\n    return a - b  # fixed the tests\n")
        self.start_round()
        self.taken_later()
        # Recorded before the snapshot: the round still waits, not thrown away.
        self.assertEqual(self.round_tests()["state"], review_tests.PENDING)

    def test_a_pass_on_a_tree_that_still_had_a_deleted_file_does_not_count(self):
        self.write("unused.py", "VALUE = 1\n")
        run_cli("state", "record", "test", "ok")
        os.remove(os.path.join(self.project, "unused.py"))
        self.start_round()
        self.assertNotEqual(self.round_tests()["state"], review_tests.USABLE)
        self.assertEqual(self.triage()[0], 2)
        self.taken_later()
        self.assertEqual(self.round_tests()["state"], review_tests.PENDING)

    def test_without_incremental_rounds_the_round_still_keeps_its_tree(self):
        run_cli("config", "set", "review.incremental_rounds", "false")
        meta = json.loads(run_cli("review", "snapshot", "--json")[1])
        self.assertEqual(meta["tree"], "")
        self.assertRegex(meta["tested_tree"], r"^[0-9a-f]{40}$")
        # A failure recorded on this very tree still refuses the round.
        run_cli("state", "record", "test", "failed")
        self.assertEqual(run_cli("review", "run")[0], 3)
        run_cli("state", "record", "test", "ok")
        self.assertEqual(run_cli("review", "run")[0], 0)
        self.assertEqual(self.round_tests()["state"], review_tests.USABLE)

    def test_the_verdict_is_about_the_reported_round_not_the_next_snapshot(self):
        self.start_round()
        run_cli("state", "record", "test", "ok")
        self.assertEqual(self.triage()[0], 0)
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.assertEqual(run_cli("review", "snapshot")[0], 0)
        self.assertEqual(run_cli("review", "fix-brief")[0], 0)
        self.assertEqual(self.round_tests()["state"], review_tests.USABLE)

    def test_a_file_the_tests_added_is_a_different_tree(self):
        self.start_round()
        self.write("test-results.xml", "<testsuite/>\n")
        run_cli("state", "record", "test", "ok")
        self.assertEqual(self.round_tests()["state"], review_tests.CHANGED)

    def test_a_config_change_is_still_the_same_tree(self):
        # Under a directory, so the project's own config is left alone: the
        # name is what counts.
        config = self.write(os.path.join("pkg", ".dev-orchestra.yaml"), "version: 1\n")
        self.start_round()
        with open(config, "a", encoding="utf-8") as handle:
            handle.write("# touched while the tests ran\n")
        run_cli("state", "record", "test", "ok")
        self.assertEqual(self.round_tests()["state"], review_tests.USABLE)

    def test_a_file_the_tests_removed_is_not(self):
        self.write("notes.txt", "kept\n")
        self.start_round()
        os.remove(os.path.join(self.project, "notes.txt"))
        run_cli("state", "record", "test", "ok")
        self.assertEqual(self.round_tests()["state"], review_tests.CHANGED)

    def test_only_accepting_waits_for_the_tests(self):
        self.start_round()
        self.assertEqual(run_cli("review", "triage", "F1", "--status", "rejected")[0], 0)
        code, _, err = self.triage()
        self.assertEqual(code, 2)
        self.assertIn("refusing to accept", err)

    def test_status_says_the_same_as_review_status(self):
        self.start_round()
        review = json.loads(run_cli("status", "--json")[1])["review"]
        self.assertEqual(review["round_tests"]["state"], review_tests.PENDING)
        self.assertIn("tests on the reviewed tree pending: record the tests", run_cli("status")[1])

    def test_a_report_without_its_time_waits_rather_than_drops(self):
        run_cli("state", "record", "test", "ok")
        self.write("app.py", "def add(a, b):\n    return a - b  # later\n")
        self.start_round()
        self.edit_report(lambda data: data["snapshot"].pop("taken_at"))
        self.assertEqual(self.round_tests()["state"], review_tests.PENDING)

    def test_an_empty_tree_in_the_report_is_refused(self):
        self.start_round()
        run_cli("state", "record", "test", "ok")
        self.edit_report(lambda data: data["snapshot"].update(tested_tree=""))
        code, _, err = self.triage()
        self.assertEqual(code, 2)
        self.assertIn("git could not record the reviewed tree", err)

    def test_a_snapshot_git_cannot_write_the_tree_of_is_refused(self):
        with mock.patch.object(ws, "write_tree", return_value=""):
            code, _, err = run_cli("review", "snapshot")
        self.assertNotEqual(code, 0)
        self.assertIn("review.start_before_tests", err)

    def test_a_design_round_does_not_wait_for_the_tests(self):
        self.start_round()
        workspace = self.cli_workspace()
        design = workspace.design_review().consolidated_json_path
        os.makedirs(os.path.dirname(design), exist_ok=True)
        shutil.copyfile(workspace.consolidated_json_path, design)
        code, _, err = run_cli("review", "triage", "--design", "F1", "--status", "accepted")
        self.assertEqual(code, 0, err)
        self.assertEqual(run_cli("review", "fix-brief", "--design")[0], 0)
        # The code round still waits.
        self.assertEqual(self.triage()[0], 2)

    def test_a_round_taken_with_the_option_off_is_unchecked_once_it_is_on(self):
        run_cli("config", "set", "review.start_before_tests", "false")
        run_cli("state", "record", "test", "ok")
        self.start_round()
        run_cli("config", "set", "review.start_before_tests", "true")
        self.assertEqual(self.round_tests()["state"], review_tests.UNCHECKED)
        self.assertIn("tests on the reviewed tree: unchecked", run_cli("review", "status")[1])
        self.assertEqual(self.triage()[0], 0)
        self.assertEqual(run_cli("review", "fix-brief")[0], 0)

    def test_off_the_last_result_still_gates_and_nothing_waits(self):
        run_cli("config", "set", "review.start_before_tests", "false")
        run_cli("state", "record", "test", "failed")
        self.write("app.py", "def add(a, b):\n    return a - b  # fixed the tests\n")
        self.assertEqual(run_cli("review", "snapshot")[0], 0)
        self.assertEqual(run_cli("review", "run")[0], 3)
        run_cli("state", "record", "test", "ok")
        self.assertEqual(run_cli("review", "run")[0], 0)
        self.assertIsNone(self.round_tests())
        self.assertEqual(self.triage()[0], 0)
        events = json.loads(run_cli("state", "show", "--json")[1])["events"]
        self.assertNotIn("tree", [event for event in events if event.get("stage") == "test"][-1])


if __name__ == "__main__":
    unittest.main()
