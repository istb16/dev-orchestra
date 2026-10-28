"""The code review loop's next step, with and without ``review.whole_change_recheck``.

Off, the decision must be today's rule exactly: another round while blocking
findings remain and the budget lasts. On, a clean fix-only round is followed
by one whole-change round -- the spare one past the cap if need be -- and a
clean whole-change round finishes the review.
"""

from __future__ import annotations

import unittest
from itertools import product
from typing import Any, Dict, List

from helpers import IsolatedCase, has_git

from orchestrator import review_consolidation, review_loop, review_snapshot
from orchestrator import workspace as ws

BLOCKING = ("critical", "high")


def finding(severity: str, triage: str = "accepted") -> Dict[str, Any]:
    return {"id": "F-%s-%s" % (severity, triage), "severity": severity, "triage": triage}


def report(scope: str, *findings: Dict[str, Any], **snapshot: Any) -> Dict[str, Any]:
    return {"iteration": 1, "snapshot": dict(snapshot, sha256="abc", scope=scope), "findings": list(findings)}


def decide(data: Dict[str, Any], iteration: int, max_iterations: int = 2, enabled: bool = True, **meta: Any):
    return review_loop.decide(enabled, data, meta, BLOCKING, iteration, max_iterations)


class TestAboveLowSeverities(unittest.TestCase):
    def test_medium_and_up(self):
        self.assertEqual(review_loop.above_low_severities(BLOCKING), ["critical", "high", "medium"])

    def test_a_blocking_low_stays_blocking(self):
        self.assertEqual(
            review_loop.above_low_severities(("critical", "low")), ["critical", "high", "medium", "low"]
        )


class TestOffParity(unittest.TestCase):
    """Off gives what ``review status`` computed before the option existed."""

    def test_the_grid(self):
        reports: List[Dict[str, Any]] = [
            {},
            report("whole"),
            report("fix", finding("low")),
            report("fix", finding("medium")),
            report("whole", finding("high")),
            report("fix", finding("critical", "rejected")),
            report("whole", finding("high", "needs-triage")),
        ]
        for data, iteration, max_iterations in product(reports, range(5), range(4)):
            with self.subTest(data=data, iteration=iteration, max_iterations=max_iterations):
                blocking = review_consolidation.unresolved_blocking(data, BLOCKING)
                decision = decide(data, iteration, max_iterations, enabled=False)
                self.assertEqual(decision.recommended, bool(blocking) and iteration < max_iterations)
                self.assertEqual(decision.exhausted, iteration >= max_iterations)
                # The final pass's inputs: today it read blocking, iteration and max.
                self.assertEqual(decision.wanted, blocking)
                self.assertEqual(decision.budget_left, iteration < max_iterations)
                self.assertIsNone(decision.next_review)
                self.assertFalse(decision.recheck_next)
                self.assertFalse(decision.finish)

    def test_no_report_is_off_whatever_the_setting(self):
        self.assertEqual(decide({}, 0), decide({}, 0, enabled=False))


class TestFixCleanGoesWhole(unittest.TestCase):
    def test_only_low_or_nothing(self):
        for data in (
            report("fix"),
            report("fix", finding("low")),
            report("fix", finding("medium", "rejected"), finding("high", "duplicate")),
        ):
            for iteration in (1, 2):
                with self.subTest(data=data, iteration=iteration):
                    decision = decide(data, iteration)
                    self.assertTrue(decision.recommended)
                    self.assertEqual(decision.next_review, "whole")
                    self.assertTrue(decision.recheck_next)
                    self.assertFalse(decision.exhausted)
                    self.assertFalse(decision.finish)

    def test_not_past_the_spare_round(self):
        decision = decide(report("fix"), 3)
        self.assertFalse(decision.recommended)
        self.assertFalse(decision.recheck_next)
        self.assertTrue(decision.exhausted)


class TestWholeCleanFinishes(unittest.TestCase):
    def test_only_low_none_or_all_rejected(self):
        for data in (
            report("whole"),
            report("whole", finding("low")),
            report("whole", finding("critical", "rejected"), finding("medium", "duplicate")),
        ):
            for iteration in (1, 2, 3):
                with self.subTest(data=data, iteration=iteration):
                    decision = decide(data, iteration)
                    self.assertTrue(decision.finish)
                    self.assertFalse(decision.recommended)
                    self.assertIsNone(decision.next_review)
                    self.assertFalse(decision.recheck_next)
                    self.assertEqual(decision.wanted, [])
                    # A finished review is not one that ran out of rounds.
                    self.assertFalse(decision.exhausted)


class TestAboveLowLoops(unittest.TestCase):
    def test_a_medium_loops_on_the_fix(self):
        for scope in ("fix", "whole"):
            with self.subTest(scope=scope):
                decision = decide(report(scope, finding("medium")), 1)
                self.assertTrue(decision.recommended)
                self.assertEqual(decision.next_review, "fix")
                self.assertFalse(decision.recheck_next)
                self.assertFalse(decision.finish)
                self.assertEqual([f["severity"] for f in decision.wanted], ["medium"])

    def test_nothing_accepted_is_whole_again_whatever_the_scope(self):
        """``next_review`` names what ``review snapshot`` will take, and it
        narrows to the fix only when the report has accepted findings."""
        for scope in ("fix", "whole"):
            with self.subTest(scope=scope):
                decision = decide(report(scope, finding("medium", "needs-triage")), 1)
                self.assertEqual(decision.next_review, "whole")

    def test_at_the_cap_it_is_exhausted(self):
        for scope in ("fix", "whole"):
            with self.subTest(scope=scope):
                decision = decide(report(scope, finding("medium")), 2)
                self.assertFalse(decision.recommended)
                self.assertIsNone(decision.next_review)
                self.assertTrue(decision.exhausted)
                self.assertFalse(decision.budget_left)
                self.assertFalse(decision.finish)


class TestSpareRoundOnlyOnce(unittest.TestCase):
    def test_a_medium_in_the_recheck_round_is_exhausted(self):
        decision = decide(report("whole", finding("medium"), recheck=True), 3)
        self.assertTrue(decision.exhausted)
        self.assertFalse(decision.recommended)
        self.assertIsNone(decision.next_review)
        # What makes ``final_fix`` pending: something wanted and no budget left.
        self.assertTrue(decision.wanted)
        self.assertFalse(decision.budget_left)

    def test_the_cap(self):
        self.assertEqual(review_loop.round_cap(True, {"recheck": True}, 2), 3)
        self.assertEqual(review_loop.round_cap(True, {"recheck": False}, 2), 2)
        self.assertEqual(review_loop.round_cap(True, {}, 2), 2)
        self.assertEqual(review_loop.round_cap(False, {"recheck": True}, 2), 2)


class TestLegacyScope(unittest.TestCase):
    def legacy(self) -> Dict[str, Any]:
        return {"iteration": 1, "snapshot": {"sha256": "abc"}, "findings": []}

    def test_the_report_wins(self):
        self.assertEqual(review_loop.round_scope(report("whole"), {"sha256": "abc", "scope": "fix"}), "whole")

    def test_the_matching_snapshot(self):
        legacy = self.legacy()
        self.assertEqual(review_loop.round_scope(legacy, {"sha256": "abc", "scope": "whole"}), "whole")
        self.assertEqual(review_loop.round_scope(legacy, {"sha256": "abc", "incremental_from": ""}), "whole")
        self.assertEqual(review_loop.round_scope(legacy, {"sha256": "abc", "incremental_from": "t"}), "fix")

    def test_otherwise_fix_only(self):
        self.assertEqual(review_loop.round_scope(self.legacy(), {"sha256": "other", "scope": "whole"}), "fix")
        self.assertEqual(review_loop.round_scope(self.legacy(), {}), "fix")
        self.assertEqual(review_loop.round_scope({"snapshot": {}}, {"scope": "whole"}), "fix")


class TestScopeRecorded(IsolatedCase):
    @unittest.skipUnless(has_git(), "git is required for review snapshots")
    def test_the_snapshot_records_its_scope(self):
        self.init_git_repo()
        self.write("a.py", "x = 1\n")
        self.commit_all("init")
        self.write("a.py", "x = 2\n")
        workspace = ws.Workspace(self.project).ensure()
        meta = review_snapshot.create_snapshot(workspace)
        self.assertEqual((meta["scope"], meta["recheck"]), ("whole", False))
        meta = review_snapshot.create_snapshot(workspace, incremental=False, recheck=True)
        self.assertEqual((meta["scope"], meta["recheck"]), ("whole", True))
        self.assertTrue(ws.read_json(workspace.snapshot_meta_path, {})["recheck"])
        # The recheck still records its tree, for the round after it to narrow from.
        meta = review_snapshot.create_snapshot(workspace, recheck=True)
        self.assertEqual((meta["scope"], meta["recheck"]), ("whole", True))
        self.assertTrue(meta["tree"])

    def test_the_report_copies_it(self):
        workspace = ws.Workspace(self.tmp).ensure()
        meta = {"sha256": "abc", "files": [], "round_id": "r", "scope": "fix", "recheck": False}
        ws.write_json(workspace.snapshot_meta_path, meta)
        data = review_consolidation.build_consolidation(workspace, [], [], 1, "")
        self.assertEqual((data["snapshot"]["scope"], data["snapshot"]["recheck"]), ("fix", False))

    def test_a_snapshot_without_it_leaves_it_out(self):
        workspace = ws.Workspace(self.tmp).ensure()
        ws.write_json(workspace.snapshot_meta_path, {"sha256": "abc", "files": [], "round_id": "r"})
        data = review_consolidation.build_consolidation(workspace, [], [], 1, "")
        self.assertNotIn("scope", data["snapshot"])
        self.assertNotIn("recheck", data["snapshot"])


if __name__ == "__main__":
    unittest.main()
