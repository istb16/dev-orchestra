"""`progress.wait_seconds`, and `jobs wait` ending early on a job turning stuck (#394)."""

from __future__ import annotations

import itertools
import time
from typing import Any, Dict, Iterable, List
from unittest import mock

from helpers import IsolatedCase, run_cli

from orchestrator import cli_state, config_common
from orchestrator import config as config_mod
from orchestrator import jobs as jobs_mod
from orchestrator import workspace as ws
from orchestrator.summary import describe_progress


class _WaitCase(IsolatedCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.cli_workspace()
        self.job = {"id": "a-1", "stage": "implementer", "status": "running", "started_at": ws.utcnow()}
        jobs_mod.write_job(self.workspace, self.job)

    def set(self, key: str, value: str) -> None:
        self.assertEqual(run_cli("config", "set", "progress." + key, value)[0], 0)

    def waited_timeout(self, *extra: str, watched: bool = True) -> float:
        """The timeout `jobs wait a-1` handed to ``jobs.wait``.

        ``watched``: whether it was handed an ``until`` too (none when the
        stuck notice is off).
        """
        seen: List[float] = []

        def fake_wait(workspace, job_id, timeout=60.0, poll=1.0, until=None):
            self.assertEqual(callable(until), watched)
            seen.append(timeout)
            return dict(self.job, waited_out=True)

        with mock.patch.object(jobs_mod, "wait", side_effect=fake_wait):
            code = run_cli("jobs", "wait", "a-1", *extra)[0]
        self.assertEqual(code, 4)
        self.assertEqual(len(seen), 1)
        return seen[0]


class TestDefaultWait(_WaitCase):
    def test_off_defaults_to_540(self):
        self.set("updates", "false")
        self.assertEqual(config_common.PROGRESS_OFF_WAIT_SECONDS, 540)
        self.assertEqual(self.waited_timeout(), 540)

    def test_auto_keeps_interval(self):
        self.assertEqual(self.waited_timeout(), 180)
        self.set("interval_seconds", "60")
        self.assertEqual(self.waited_timeout(), 60)
        self.set("updates", "true")
        self.assertEqual(self.waited_timeout(), 60)

    def test_wait_seconds_wins_and_timeout_wins_over_it(self):
        self.set("wait_seconds", "300")
        self.assertEqual(self.waited_timeout(), 300)
        self.set("updates", "false")
        self.assertEqual(self.waited_timeout(), 300)
        self.assertEqual(self.waited_timeout("--timeout", "45"), 45)

    def test_several_jobs_use_it_too(self):
        self.set("updates", "false")
        jobs_mod.write_job(self.workspace, dict(self.job, id="b-2"))
        seen: List[float] = []

        watched: List[bool] = []

        def fake_wait_many(workspace, job_ids, any_=False, timeout=60.0, poll=1.0, until=None):
            seen.append(timeout)
            watched.append(callable(until))
            return [dict(self.job, id=job_id, waited_out=True) for job_id in job_ids]

        with mock.patch.object(jobs_mod, "wait_many", side_effect=fake_wait_many):
            self.assertEqual(run_cli("jobs", "wait", "a-1", "b-2")[0], 4)
            self.set("quiet_notice_seconds", "0")
            self.assertEqual(run_cli("jobs", "wait", "a-1", "b-2")[0], 4)
        self.assertEqual(seen, [540, 540])
        self.assertEqual(watched, [True, False])

    def test_no_stuck_notice_hands_no_until(self):
        self.set("quiet_notice_seconds", "0")
        self.assertEqual(self.waited_timeout(watched=False), 180)


class TestStuckEndsTheWait(_WaitCase):
    """`jobs wait` with the real ``until``: only ``read_activity`` is faked."""

    def run_wait(self, stuck: Dict[str, List[bool]], *ids: str) -> "tuple[int, float]":
        """Exit code and seconds taken of `jobs wait <ids> --timeout 60`.

        ``stuck`` answers ``may_be_stuck`` per job, one per read the watch
        makes (``latest`` 0); the report's own reads get no activity.
        """
        answers = {job_id: iter(flags) for job_id, flags in stuck.items()}
        self.reads: Dict[str, int] = dict.fromkeys(stuck, 0)

        def fake(workspace, job, since=0, latest=10, quiet_notice_seconds=0):
            self.assertEqual(quiet_notice_seconds, 600)
            if latest:
                return None
            self.reads[str(job.get("id"))] += 1
            return {"may_be_stuck": next(answers[str(job.get("id"))])}

        begun = time.monotonic()
        with (
            mock.patch.object(cli_state, "STUCK_CHECK_SECONDS", 0.0),
            mock.patch.object(jobs_mod, "read_activity", side_effect=fake),
        ):
            code = run_cli("jobs", "wait", *ids, "--timeout", "60", "--poll", "0.01")[0]
        return code, time.monotonic() - begun

    def test_one_job_turning_stuck(self):
        code, took = self.run_wait({"a-1": [False, False, True]}, "a-1")
        self.assertEqual(code, 4)
        self.assertLess(took, 30)
        self.assertEqual(self.reads, {"a-1": 3})

    def test_several_jobs_each_watched_on_its_own(self):
        """b-2, stuck from the start, never ends the wait; a-1 turning stuck does."""
        jobs_mod.write_job(self.workspace, dict(self.job, id="b-2"))
        code, took = self.run_wait({"a-1": [False, False, False, True], "b-2": [True] * 4}, "a-1", "b-2")
        self.assertEqual(code, 4)
        self.assertLess(took, 30)
        # Each read all its answers: the wait ended on a-1's last, not on b-2.
        self.assertEqual(self.reads, {"a-1": 4, "b-2": 4})


class TestSettings(IsolatedCase):
    def problems(self, progress: Dict[str, Any]) -> List[str]:
        data = config_mod.default_config()
        data["progress"] = progress
        return config_mod.validate(data)

    def test_validation_bounds(self):
        self.assertIsNone(config_mod.default_config()["progress"]["wait_seconds"])
        for value in (30, 540, 570, None):
            self.assertEqual(self.problems({"wait_seconds": value}), [], value)
        problem = "progress.wait_seconds: must be an integer from 30 to 570 or null"
        for value in (29, 571, 3600, 0, -1, True, "540", 540.0):
            self.assertEqual(self.problems({"wait_seconds": value}), [problem], value)

    def test_unusable_values_read_as_null(self):
        def settings(block: Dict[str, Any]) -> config_mod.Progress:
            return config_common.progress_settings_of({"progress": block})

        self.assertEqual(settings({}).wait_seconds, 180)
        self.assertEqual(settings({"wait_seconds": 29}).wait_seconds, 180)
        self.assertEqual(settings({"updates": False, "wait_seconds": "x"}).wait_seconds, 540)
        self.assertEqual(settings({"updates": False, "wait_seconds": 60}).wait_seconds, 60)
        self.assertEqual(settings({"interval_seconds": 90, "wait_seconds": None}).wait_seconds, 90)

    def test_describe_progress_shows_wait_only_when_different(self):
        def line(block: Dict[str, Any]) -> str:
            return describe_progress(config_common.progress_settings_of({"progress": block}))

        self.assertEqual(line({}), "auto, every 180s, stuck notice after 600s")
        self.assertEqual(line({"wait_seconds": 180}), "auto, every 180s, stuck notice after 600s")
        self.assertEqual(
            line({"updates": True, "interval_seconds": 60}), "on, every 60s, stuck notice after 600s"
        )
        self.assertEqual(
            line({"updates": False, "interval_seconds": 60, "quiet_notice_seconds": 0}),
            "off, every 60s, no stuck notice, wait 540s",
        )
        self.assertEqual(line({"wait_seconds": 300}), "auto, every 180s, stuck notice after 600s, wait 300s")


class TestUntil(IsolatedCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.cli_workspace()
        self.job = {"id": "a-1", "stage": "implementer", "status": "running", "started_at": ws.utcnow()}
        jobs_mod.write_job(self.workspace, self.job)

    def test_returns_early_when_job_turns_stuck(self):
        calls: List[str] = []

        def until(job: Dict[str, Any]) -> bool:
            calls.append(str(job.get("id")))
            return len(calls) >= 3

        begun = time.monotonic()
        job = jobs_mod.wait(self.workspace, "a-1", timeout=60, poll=0.01, until=until)
        self.assertLess(time.monotonic() - begun, 30)
        self.assertTrue(job.get("waited_out"))
        self.assertEqual(job.get("status"), "running")
        self.assertEqual(calls, ["a-1"] * 3)

        jobs_mod.write_job(self.workspace, dict(self.job, id="b-2"))
        found = jobs_mod.wait_many(
            self.workspace, ["a-1", "b-2"], timeout=60, poll=0.01, until=lambda job: job.get("id") == "b-2"
        )
        self.assertLess(time.monotonic() - begun, 30)
        self.assertEqual([job.get("waited_out") for job in found], [True, True])

    def test_a_long_poll_is_capped_while_until_watches(self):
        """``--poll 120`` still asks ``until`` every ``UNTIL_POLL_SECONDS``, in both waits."""
        slept: List[float] = []
        answers = iter([False, True])

        def until(job: Dict[str, Any]) -> bool:
            return next(answers)

        with mock.patch.object(jobs_mod.time, "sleep", side_effect=slept.append):
            job = jobs_mod.wait(self.workspace, "a-1", timeout=600, poll=120, until=until)
        self.assertTrue(job.get("waited_out"))
        self.assertEqual(slept, [jobs_mod.UNTIL_POLL_SECONDS])

        slept.clear()
        answers = iter([False, True])
        with mock.patch.object(jobs_mod.time, "sleep", side_effect=slept.append):
            jobs_mod.wait_many(self.workspace, ["a-1"], timeout=600, poll=120, until=until)
        self.assertEqual(slept, [jobs_mod.UNTIL_POLL_SECONDS])

    def test_without_until_the_poll_is_kept(self):
        clock = [1000.0]
        slept: List[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock[0] += seconds

        with (
            mock.patch.object(jobs_mod.time, "sleep", side_effect=sleep),
            mock.patch.object(jobs_mod.time, "monotonic", side_effect=lambda: clock[0]),
        ):
            job = jobs_mod.wait(self.workspace, "a-1", timeout=300, poll=120)
        self.assertTrue(job.get("waited_out"))
        # Two full polls, then only what is left before the deadline.
        self.assertEqual(slept, [120, 120, 60])

    def test_a_finished_job_is_not_asked(self):
        jobs_mod.write_job(self.workspace, dict(self.job, status="succeeded", finished_at=ws.utcnow()))
        until = mock.Mock(return_value=True)
        job = jobs_mod.wait(self.workspace, "a-1", timeout=5, poll=0.01, until=until)
        self.assertNotIn("waited_out", job)
        until.assert_not_called()

    def stuck_sequence(self, flags: Iterable[bool]):
        """``read_activity`` answering ``may_be_stuck`` from ``flags``, one per read."""
        answers = iter(flags)

        def fake(workspace, job, since=0, latest=10, quiet_notice_seconds=0):
            self.assertEqual((since, latest, quiet_notice_seconds), (0, 0, 600))
            return {"may_be_stuck": next(answers)}

        return mock.patch.object(jobs_mod, "read_activity", side_effect=fake)

    def test_turning_stuck_ends_the_wait(self):
        no_throttle = mock.patch.object(cli_state, "STUCK_CHECK_SECONDS", 0.0)
        with no_throttle, self.stuck_sequence([False, False, True]):
            until = cli_state._turns_stuck(self.workspace, ["a-1"], 600)
            assert until is not None
            self.assertFalse(until(self.job))
            self.assertTrue(until(self.job))

    def test_already_stuck_does_not_cut_wait(self):
        # Read at every poll now, so that a recovery is seen; never a change to stuck.
        with (
            mock.patch.object(cli_state, "STUCK_CHECK_SECONDS", 0.0),
            self.stuck_sequence(itertools.repeat(True)),
        ):
            until = cli_state._turns_stuck(self.workspace, ["a-1"], 600)
            assert until is not None
            for _ in range(3):
                self.assertFalse(until(self.job))
            job = jobs_mod.wait(self.workspace, "a-1", timeout=0.2, poll=0.01, until=until)
        self.assertTrue(job.get("waited_out"))

    def test_stuck_at_the_start_then_recovered_then_stuck_again_ends_the_wait(self):
        """Only the change to stuck counts, so a job that recovers is watched again."""
        flags = [True, True, False, False, True, True]
        with mock.patch.object(cli_state, "STUCK_CHECK_SECONDS", 0.0), self.stuck_sequence(flags):
            until = cli_state._turns_stuck(self.workspace, ["a-1"], 600)
            assert until is not None
            self.assertEqual([until(self.job) for _ in flags[1:]], [False, False, False, True, False])

    def test_two_jobs_are_watched_apart(self):
        """b-2 stuck at the start does not end the wait; a-1 turning stuck does."""
        jobs_mod.write_job(self.workspace, dict(self.job, id="b-2"))
        answers = {"a-1": iter([False, False, True]), "b-2": iter([True, True, True])}

        def fake(workspace, job, since=0, latest=10, quiet_notice_seconds=0):
            return {"may_be_stuck": next(answers[str(job.get("id"))])}

        b2 = dict(self.job, id="b-2")
        with (
            mock.patch.object(cli_state, "STUCK_CHECK_SECONDS", 0.0),
            mock.patch.object(jobs_mod, "read_activity", side_effect=fake),
        ):
            until = cli_state._turns_stuck(self.workspace, ["a-1", "b-2"], 600)
            assert until is not None
            self.assertEqual([until(b2), until(self.job)], [False, False])
            self.assertEqual([until(b2), until(self.job)], [False, True])

    def test_the_activity_is_read_at_most_every_ten_seconds(self):
        self.assertEqual(cli_state.STUCK_CHECK_SECONDS, 10.0)
        with self.stuck_sequence([False, True]) as read:
            until = cli_state._turns_stuck(self.workspace, ["a-1"], 600)
            assert until is not None
            self.assertFalse(until(self.job))
            self.assertEqual(read.call_count, 1)
            later = time.monotonic() + 11
            with mock.patch.object(time, "monotonic", return_value=later):
                self.assertTrue(until(self.job))
            self.assertEqual(read.call_count, 2)

    def test_no_stuck_notice_means_no_early_return(self):
        self.assertIsNone(cli_state._turns_stuck(self.workspace, ["a-1"], 0))
