"""``review.start_before_tests``: a round started before the tests, used once they pass on its tree."""

from __future__ import annotations

import json
import os
import unittest

from helpers import IsolatedCase, has_git, run_cli

from orchestrator import review_tests

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
        self.assertEqual(review_tests.refusal(verdict), "")
        self.assertEqual(review_tests.next_step(verdict), "")


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
        self.assertEqual(run_cli("state", "record", "test", "ok")[0], 0)
        events = json.loads(run_cli("state", "show", "--json")[1])["events"]
        self.assertRegex(events[-1].get("tree", ""), r"^[0-9a-f]{40}$")

    def test_a_tree_given_by_hand_is_refused(self):
        code, _, err = run_cli("state", "record", "test", "ok", "--detail", "tree=abc")
        self.assertEqual(code, 2)
        self.assertIn("a test result's tree is read from the working tree", err)

    def test_a_change_made_while_the_tests_ran_drops_the_round(self):
        self.start_round()
        self.write("app.py", "def add(a, b):\n    return a - b  # lint\n")
        run_cli("state", "record", "test", "ok")
        self.assertEqual(self.round_tests()["state"], review_tests.CHANGED)
        self.assertIn("next: this round is not used", run_cli("review", "status")[1])
        code, _, err = self.triage()
        self.assertEqual(code, 2)
        self.assertIn("ran on a different tree", err)

    def test_a_failure_on_the_tree_drops_the_round_until_it_passes_there(self):
        self.start_round()
        run_cli("state", "record", "test", "failed")
        self.assertEqual(self.round_tests()["state"], review_tests.FAILED)
        self.assertEqual(self.triage()[0], 2)
        # Run again on the same tree (a flaky test): the last result decides.
        run_cli("state", "record", "test", "ok")
        self.assertEqual(self.triage()[0], 0)

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
