"""`review run --progress`: each reviewer's tool uses, echoed while the round runs."""

from __future__ import annotations

import json
import os
import re
import unittest
from typing import Dict, List
from unittest import mock

from helpers import IsolatedCase, has_git
from test_cli import run_cli
from test_design_review import DesignReviewCase
from test_job_activity import normalised

from orchestrator import activity, review_fanout
from orchestrator import config as config_mod
from orchestrator import review as review_mod
from orchestrator.providers.mock import MockProvider

TAGGED = re.compile(r"^\[(?P<tag>[^\]]+) \+\d\d:\d\d\] (?P<line>.+)$")

PANEL = (
    # quality: a snapshot this small would otherwise cut the panel to one reviewer.
    "version: 1\noptimization:\n  level: quality\nreviewers:\n"
    "  - id: alpha\n    provider: mock\n    role: general\n"
    "  - id: beta\n    provider: mock\n    role: general\n"
)


class _Raising(MockProvider):
    def _launch(self, *args, **kwargs):
        raise RuntimeError("the adapter fell over")


@unittest.skipUnless(has_git(), "git is required")
class _SnapshotCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        # Committed with the code, so the reviewer ids are not in the diff
        # every prompt carries.
        self.write(".dev-orchestra.yaml", PANEL)
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write("app.py", "def add(a, b):\n    return a - b\n")
        self.workspace = self.cli_workspace()
        review_mod.create_snapshot(self.workspace)


class TestReviewProgress(_SnapshotCase):
    def setUp(self):
        super().setUp()
        os.environ["DEV_ORCHESTRA_MOCK_ACTIVITY"] = "alpha=>Read alpha.py|beta=>Read beta.py"

    def tagged(self, err: str) -> Dict[str, List[str]]:
        """The echoed lines by reviewer; every one of them a whole line."""
        found: Dict[str, List[str]] = {}
        for line in err.splitlines():
            if not line.startswith("["):
                continue
            match = TAGGED.match(line)
            self.assertIsNotNone(match, line)
            assert match is not None
            found.setdefault(match.group("tag"), []).append(match.group("line"))
        return found

    def test_each_tag_carries_only_its_own_reviewers_lines(self):
        code, _, err = run_cli("review", "run", "--progress")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            self.tagged(err),
            {"alpha": ["Read alpha.py", "done: ok"], "beta": ["Read beta.py", "done: ok"]},
        )

    def test_a_failing_reviewer_still_reports_what_it_did(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "alpha"
        _, _, err = run_cli("review", "run", "--progress")
        self.assertEqual(self.tagged(err)["alpha"], ["Read alpha.py", "done: failed"])

    def test_without_progress_nothing_is_echoed(self):
        code, out, err = run_cli("review", "run")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tagged(err), {})
        self.assertNotIn("Read alpha.py", out + err)

    def test_progress_leaves_stdout_and_the_json_as_they_were(self):
        # The same round twice, on the same snapshot: what may differ between
        # the two is the round's place in the sequence, not what it reports.
        rounds = {}
        for argv in ((), ("--progress",)):
            run_cli("budget", "reset")
            review_mod.create_snapshot(self.workspace)
            code, out, err = run_cli("review", "run", "--json", *argv)
            self.assertEqual(code, 0, err)
            self.assertNotIn("Read alpha.py", out)
            self.assertNotIn("done:", out)
            payload = json.loads(out)
            kept = ("ok", "failed", "partial", "reviewers", "counts")
            rounds[argv] = normalised({key: payload[key] for key in kept})
            if argv:
                self.assertEqual(set(self.tagged(err)), {"alpha", "beta"})
            else:
                self.assertEqual(self.tagged(err), {})
        self.assertEqual(rounds[("--progress",)], rounds[()])

    def test_progress_in_a_sequential_round(self):
        code, out, err = run_cli("review", "run", "--sequential", "--progress")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            self.tagged(err),
            {"alpha": ["Read alpha.py", "done: ok"], "beta": ["Read beta.py", "done: ok"]},
        )
        self.assertNotIn("Read alpha.py", out)


class TestDesignReviewProgress(DesignReviewCase):
    def test_a_design_round_echoes_each_reviewer(self):
        self.write_plan()
        os.environ["DEV_ORCHESTRA_MOCK_ACTIVITY"] = "Read plan.md"
        code, out, err = run_cli("review", "run", "--design", "--progress")
        self.assertEqual(code, 0, err)
        echoed: Dict[str, List[str]] = {}
        for line in err.splitlines():
            match = TAGGED.match(line)
            if match:
                echoed.setdefault(match.group("tag"), []).append(match.group("line"))
        self.assertEqual(echoed, {"m1": ["Read plan.md", "done: ok"], "m2": ["Read plan.md", "done: ok"]})
        self.assertNotIn("Read plan.md", out)


class TestFanOutSinks(_SnapshotCase):
    def setUp(self):
        super().setUp()
        self.seen: List[str] = []

    def sinks(self, reviewer_id: str) -> activity.Sink:
        return activity.Sink(echo=self.seen.append)

    def run_one(self) -> review_fanout.ReviewerRun:
        reviewer = config_mod.make_reviewer("solo", "mock", "small", "general")
        options = review_mod.FanoutOptions(parallel=False, activity_for=self.sinks)
        runs = review_mod.run_reviews([reviewer], self.workspace, options)
        return runs[0]

    def test_a_raising_provider_still_says_done_and_leaves_no_sink_behind(self):
        with mock.patch.object(review_fanout, "get_provider", lambda name: _Raising()):
            run = self.run_one()
        self.assertEqual(run.status, "failed")
        self.assertEqual(self.seen, ["done: failed"])
        self.assertIsNone(activity.current())

    def test_the_echo_is_capped_per_reviewer(self):
        os.environ["DEV_ORCHESTRA_MOCK_ACTIVITY"] = "a|b|c|d"
        with mock.patch.object(activity, "ECHO_LIMIT", 2):
            self.run_one()
        self.assertEqual(self.seen, ["a", "b", activity.ECHO_CUT, "done: ok"])


if __name__ == "__main__":
    unittest.main()
