"""`review run --progress`: each reviewer's tool uses, echoed while the round runs."""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import unittest
from typing import Dict, List
from unittest import mock

from helpers import IsolatedCase, has_git, run_cli
from test_design_review import DesignReviewCase
from test_job_activity import normalised

from orchestrator import activity, cli_review, review_fanout
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


class TestQuietNotice(unittest.TestCase):
    """The stuck notice in a `review run --progress` log (#330)."""

    def setUp(self):
        self.echoed: List[str] = []
        self.enterContext(mock.patch.object(cli_review, "_err", self.echoed.append))

    def silent(self, echo: cli_review._ProgressEcho, reviewer_id: str, seconds: float) -> activity.Sink:
        sink = echo.sink_for(reviewer_id)
        sink.last_line -= seconds
        return sink

    def test_a_silent_reviewer_is_flagged_and_a_busy_one_is_not(self):
        echo = cli_review._ProgressEcho(600)
        self.silent(echo, "alpha", 723)
        self.silent(echo, "beta", 10)
        echo.flag_quiet()
        self.assertEqual(len(self.echoed), 1)
        match = TAGGED.match(self.echoed[0])
        assert match is not None
        self.assertEqual(match.group("tag"), "alpha")
        self.assertRegex(match.group("line"), r"^may be stuck: no new tool line for 12m0[3-5]s$")
        # Again on the next interval, while it stays silent.
        echo.flag_quiet()
        self.assertEqual(len(self.echoed), 2)

    def test_an_ended_reviewer_is_not_flagged(self):
        echo = cli_review._ProgressEcho(600)
        self.silent(echo, "alpha", 723).end("done: ok")
        echo.flag_quiet()
        self.assertEqual(len(self.echoed), 1)
        self.assertTrue(self.echoed[0].endswith("] done: ok"), self.echoed)

    def test_no_notice_when_it_is_off(self):
        echo = cli_review._ProgressEcho(0)
        self.silent(echo, "alpha", 10**6)
        echo.flag_quiet()
        self.assertEqual(self.echoed, [])

    def test_the_watcher_starts_and_stops_with_the_block(self):
        echo = cli_review._ProgressEcho(600)
        self.silent(echo, "alpha", 723)
        flagged = threading.Event()
        flag_quiet = echo.flag_quiet

        def flag() -> None:
            flag_quiet()
            flagged.set()

        with mock.patch.object(echo, "flag_quiet", flag):
            with echo.watching(0.01):
                self.assertTrue(flagged.wait(10))
                self.assertIn("review-progress-watch", [thread.name for thread in threading.enumerate()])
            self.assertNotIn("review-progress-watch", [thread.name for thread in threading.enumerate()])
        with self.assertRaises(RuntimeError), echo.watching(0.01):
            raise RuntimeError
        self.assertNotIn("review-progress-watch", [thread.name for thread in threading.enumerate()])


class TestRoundWatcher(_SnapshotCase):
    """`_run_panel` watches a round only under --progress with the notice on."""

    def intervals(self, *argv: str) -> List[float]:
        seen: List[float] = []

        def watching(echo: cli_review._ProgressEcho, interval: float):
            seen.append(interval)
            return contextlib.nullcontext()

        with mock.patch.object(cli_review._ProgressEcho, "watching", watching):
            code, _, err = run_cli("review", "run", *argv)
        self.assertEqual(code, 0, err)
        return seen

    def test_the_round_is_watched_at_the_configured_interval(self):
        self.assertEqual(self.intervals("--progress"), [180])
        self.assertEqual(self.intervals(), [])

    def test_no_watcher_when_the_notice_is_off(self):
        # The global file, so the snapshot's tree stays as it was.
        config_mod.write_config_file(
            config_mod.global_config_path(), {"version": 1, "progress": {"quiet_notice_seconds": 0}}
        )
        self.assertEqual(self.intervals("--progress"), [])

    def test_a_real_round_leaves_no_watcher_behind(self):
        code, _, err = run_cli("review", "run", "--progress")
        self.assertEqual(code, 0, err)
        self.assertNotIn("review-progress-watch", [thread.name for thread in threading.enumerate()])


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
