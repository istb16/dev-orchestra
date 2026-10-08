"""Detached jobs: a bounded wait beats an open-ended block.

The point is structural. While a delegated run is in progress the caller is
inside that call, so a blocked agent cannot even report that it is blocked.
These tests care most about the two ways that guarantee could be lost: a wait
that does not return, and a worker that dies leaving a job that claims forever
to be running.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import types
import unittest
from unittest import mock

from helpers import IsolatedCase, has_git, present

from orchestrator import jobs as jobs_mod
from orchestrator import workspace as ws


class JobCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace()

    def record(self, job_id="stage-1", **fields):
        job = {
            "id": job_id,
            "stage": "implementer",
            "status": "running",
            "started_at": ws.utcnow(),
            "output_file": jobs_mod.output_path(self.workspace, job_id),
        }
        job.update(fields)
        jobs_mod.write_job(self.workspace, job)
        return job


class TestJobRecords(JobCase):
    def test_a_job_round_trips(self):
        self.record("a-1", stage="architect")
        job = jobs_mod.read_job(self.workspace, "a-1")
        assert job is not None
        self.assertEqual(job["stage"], "architect")
        self.assertEqual(job["status"], "running")

    def test_an_unknown_job_is_none(self):
        self.assertIsNone(jobs_mod.read_job(self.workspace, "nope"))

    def test_jobs_are_listed_newest_first(self):
        self.record("a-1", started_at="2026-01-01T00:00:00Z", status="succeeded")
        self.record("a-2", started_at="2026-02-01T00:00:00Z", status="succeeded")
        self.assertEqual([job["id"] for job in jobs_mod.list_jobs(self.workspace)], ["a-2", "a-1"])

    def test_job_ids_are_unique_per_stage(self):
        self.assertNotEqual(jobs_mod.new_job_id("test"), jobs_mod.new_job_id("test"))


class TestDeadWorkerDetection(JobCase):
    def test_a_running_job_whose_worker_is_gone_becomes_abandoned(self):
        self.record("a-1", pid=999_999)
        job = jobs_mod.read_job(self.workspace, "a-1")
        assert job is not None
        self.assertEqual(job["status"], "abandoned")
        self.assertIn("is gone", job["error"])

    def test_the_abandoned_status_is_persisted_not_just_reported(self):
        self.record("a-1", pid=999_999)
        jobs_mod.read_job(self.workspace, "a-1")
        raw = ws.read_json(jobs_mod.job_path(self.workspace, "a-1"))
        self.assertEqual(raw["status"], "abandoned")

    def test_a_live_worker_is_left_alone(self):
        self.record("a-1", pid=os.getpid())
        self.assertEqual(present(jobs_mod.read_job(self.workspace, "a-1"))["status"], "running")

    def test_a_finished_job_is_never_reinterpreted(self):
        self.record("a-1", pid=999_999, status="succeeded")
        self.assertEqual(present(jobs_mod.read_job(self.workspace, "a-1"))["status"], "succeeded")

    def test_a_job_with_no_pid_yet_is_left_alone(self):
        self.record("a-1", status="starting")
        self.assertEqual(present(jobs_mod.read_job(self.workspace, "a-1"))["status"], "starting")


class TestBoundedWait(JobCase):
    def test_waiting_returns_immediately_for_a_finished_job(self):
        self.record("a-1", status="succeeded")
        started = time.monotonic()
        job = jobs_mod.wait(self.workspace, "a-1", timeout=30)
        self.assertEqual(job["status"], "succeeded")
        self.assertLess(time.monotonic() - started, 5)

    def test_waiting_on_a_running_job_returns_at_the_deadline(self):
        """The whole point: the wait ends even though the job does not."""
        self.record("a-1", pid=os.getpid())
        started = time.monotonic()
        job = jobs_mod.wait(self.workspace, "a-1", timeout=1, poll=0.1)
        elapsed = time.monotonic() - started
        self.assertTrue(job["waited_out"])
        self.assertEqual(job["status"], "running")
        # wait() stops at its own `start + 1`. On Windows the clock is coarse
        # enough for both starts to read the same, and `(t + 1) - t` then
        # rounds to a hair under 1 -- a float artefact, not an early return.
        self.assertGreaterEqual(elapsed, 1 - 1e-9)
        self.assertLess(elapsed, 10)

    def test_a_dead_worker_ends_the_wait_rather_than_timing_it_out(self):
        self.record("a-1", pid=999_999)
        job = jobs_mod.wait(self.workspace, "a-1", timeout=30, poll=0.1)
        self.assertEqual(job["status"], "abandoned")
        self.assertFalse(job.get("waited_out"))


class TestCancel(JobCase):
    def cancel_a_stopped_worker(self, note=None):
        """Cancel a job whose worker the kill confirmed gone, after its
        SIGTERM handler wrote ``note`` (None: nothing) beside the record."""
        from orchestrator import execution

        self.record("a-1", pid=4242, pid_started="t0", status="running")
        if note is not None:
            with open(jobs_mod.stop_note_path(jobs_mod.job_path(self.workspace, "a-1")), "w") as handle:
                handle.write(note)
        with (
            mock.patch.object(execution, "pid_alive", lambda pid: True),
            mock.patch.object(execution, "process_started", lambda pid: "t0"),
            mock.patch.object(execution, "kill_tree", lambda pid, grace, verified: True),
        ):
            return jobs_mod.cancel(self.workspace, "a-1")

    def test_a_cli_group_the_worker_could_not_end_is_recorded(self):
        job = self.cancel_a_stopped_worker(
            "warning: the CLI's process group (pid 77) may still be running\n"
            "warning: the CLI's process group (pid 78) may still be running\n"
        )
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(
            job["error"],
            "cancelled by request; the CLI's process group (pid 77) may still be running; "
            "the CLI's process group (pid 78) may still be running",
        )
        recorded = jobs_mod.read_job(self.workspace, "a-1")
        assert recorded is not None
        self.assertEqual(recorded["error"], job["error"])

    def test_a_worker_that_ended_its_cli_leaves_the_plain_message(self):
        self.assertEqual(self.cancel_a_stopped_worker()["error"], "cancelled by request")
        self.assertEqual(self.cancel_a_stopped_worker("")["error"], "cancelled by request")

    def test_cancelling_a_live_worker_stops_it(self):
        import subprocess
        import sys

        from orchestrator import execution

        child = subprocess.Popen(
            [sys.executable, "-c", "import time\nwhile True: time.sleep(0.05)"],
            **execution._spawn_kwargs(),
        )
        try:
            self.record(
                "a-1", pid=child.pid, pid_started=execution.process_started(child.pid), status="running"
            )
            job = jobs_mod.cancel(self.workspace, "a-1")
            self.assertEqual(job["status"], "cancelled")
            child.wait(timeout=30)
        finally:
            if child.poll() is None:  # pragma: no cover - safety net
                child.kill()

    def test_a_worker_still_there_after_the_kill_is_not_reported_stopped(self):
        """The kill is confirmed, not assumed: every signal is sent and lands
        nowhere, and the job says the worker may still be running."""
        import signal

        from orchestrator import execution

        self.record("a-1", pid=4242, pid_started="t0", status="running")
        sent = []
        with (
            mock.patch.object(execution, "pid_alive", lambda pid: True),
            mock.patch.object(execution, "process_started", lambda pid: "t0"),
            mock.patch.object(execution, "KILL_GRACE_SECONDS", 0.2),
            mock.patch.object(
                execution.subprocess, "run", lambda args, **kwargs: sent.append(("run", args[0]))
            ),
            mock.patch.object(execution.os, "kill", lambda pid, sig: sent.append(("kill", sig))),
            mock.patch.object(execution.os, "getpgid", lambda pid: pid, create=True),
            mock.patch.object(
                execution.os, "killpg", lambda pgid, sig: sent.append(("killpg", sig)), create=True
            ),
        ):
            job = jobs_mod.cancel(self.workspace, "a-1")
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["error"], "cancelled by request (the worker may still be running)")
        # It escalates like a timed-out run: the whole tree, then the pid alone.
        if execution.IS_WINDOWS:
            self.assertEqual(sent, [("run", "taskkill"), ("kill", signal.SIGTERM)])
        else:
            self.assertEqual(
                sent, [("killpg", signal.SIGTERM), ("killpg", signal.SIGKILL), ("kill", signal.SIGKILL)]
            )

    def test_a_worker_that_exits_on_the_first_signal_is_reported_plainly(self):
        import signal

        from orchestrator import execution

        self.record("a-1", pid=4242, pid_started="t0", status="running")
        sent = []
        alive = [True]

        def stop(*args, **kwargs):
            sent.append(args)
            alive[0] = False

        def killpg(pgid, sig):
            # The group went with its leader.
            if sig == 0 and not alive[0]:
                raise ProcessLookupError
            stop(pgid, sig)

        with (
            mock.patch.object(execution, "pid_alive", lambda pid: alive[0]),
            mock.patch.object(execution, "process_started", lambda pid: "t0"),
            mock.patch.object(execution, "KILL_GRACE_SECONDS", 0.2),
            mock.patch.object(execution.subprocess, "run", stop),
            mock.patch.object(execution.os, "kill", stop),
            mock.patch.object(execution.os, "getpgid", lambda pid: pid, create=True),
            mock.patch.object(execution.os, "killpg", killpg, create=True),
        ):
            job = jobs_mod.cancel(self.workspace, "a-1")
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["error"], "cancelled by request")
        if execution.IS_WINDOWS:
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0][0], "taskkill")
        else:
            self.assertEqual(sent, [(4242, signal.SIGTERM)])

    def test_a_group_that_outlives_the_kill_is_reported_may_still_be_running(self):
        """The worker exits on SIGTERM, but its group never reads gone: the
        kill is not confirmed. Driven as POSIX on every OS."""
        import signal

        from orchestrator import execution

        self.record("a-1", pid=4242, status="running")
        sent = []
        alive = [True]

        def killpg(pgid, sig):
            if sig == 0:
                return
            sent.append(("killpg", sig))
            alive[0] = False

        with (
            mock.patch.object(execution, "IS_WINDOWS", False),
            mock.patch.object(execution, "_reap", lambda pid: None),
            mock.patch.object(execution, "pid_alive", lambda pid: alive[0]),
            mock.patch.object(execution, "KILL_GRACE_SECONDS", 0.2),
            mock.patch.object(execution.signal, "SIGKILL", 9, create=True),
            mock.patch.object(execution.os, "kill", lambda pid, sig: sent.append(("kill", sig))),
            mock.patch.object(execution.os, "getpgid", lambda pid: pid, create=True),
            mock.patch.object(execution.os, "killpg", killpg, create=True),
        ):
            job = jobs_mod.cancel(self.workspace, "a-1")
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["error"], "cancelled by request (the worker may still be running)")
        self.assertEqual(sent, [("killpg", signal.SIGTERM), ("killpg", 9)])

    def test_cancelling_a_job_whose_worker_is_already_gone_says_abandoned(self):
        """Reporting "cancelled" would claim credit for something that already happened."""
        self.record("a-1", pid=999_999, status="running")
        self.assertEqual(jobs_mod.cancel(self.workspace, "a-1")["status"], "abandoned")

    def test_cancelling_a_finished_job_changes_nothing(self):
        self.record("a-1", status="succeeded")
        self.assertEqual(jobs_mod.cancel(self.workspace, "a-1")["status"], "succeeded")

    def test_cancelling_an_unknown_job_raises(self):
        with self.assertRaises(KeyError):
            jobs_mod.cancel(self.workspace, "nope")


class TestReusedPid(JobCase):
    """A worker that vanished without a word, with a reboot say, leaves a pid
    the system may hand to anyone. It is told apart by its start time (#268)."""

    def bystander(self):
        """A process that has nothing to do with any job, ended after the test."""
        import sys

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

        def end():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=30)

        self.addCleanup(end)
        return child

    def require_start_times(self):
        """Without a start time to read now, a recorded one cannot be compared
        and the pid is unverified, not someone else's."""
        from orchestrator import execution

        if execution.process_started(os.getpid()) is None:
            self.skipTest("no start time to read on this platform")

    def test_a_pid_another_process_now_has_is_not_stopped(self):
        self.require_start_times()
        child = self.bystander()
        self.record("a-1", pid=child.pid, pid_started="the worker's", status="running")
        job = jobs_mod.cancel(self.workspace, "a-1")
        self.assertEqual(job["status"], "abandoned")
        self.assertIn("is gone", job["error"])
        time.sleep(0.5)
        self.assertIsNone(child.poll())

    def test_a_pid_another_process_now_has_marks_the_job_abandoned(self):
        self.require_start_times()
        self.record("a-1", pid=os.getpid(), pid_started="the worker's")
        job = present(jobs_mod.read_job(self.workspace, "a-1"))
        self.assertEqual(job["status"], "abandoned")
        self.assertEqual(ws.read_json(jobs_mod.job_path(self.workspace, "a-1"))["status"], "abandoned")

    def test_the_worker_it_was_recorded_for_is_left_running(self):
        from orchestrator import execution

        started = execution.process_started(os.getpid())
        if started is None:
            self.skipTest("no start time to read on this platform")
        self.record("a-1", pid=os.getpid(), pid_started=started)
        self.assertEqual(present(jobs_mod.read_job(self.workspace, "a-1"))["status"], "running")

    def cancel_an_unverified_worker(self, stopped, **fields):
        """Cancel a job whose live pid nothing vouches for; the kill answers
        ``stopped``. Returns the job and the ``verified`` each kill was given."""
        from orchestrator import execution

        self.record("a-1", pid=4242, status="running", **fields)
        alive = [True]
        asked = []

        def kill_tree(pid, grace, verified):
            asked.append(verified)
            alive[0] = not stopped
            return stopped

        with (
            mock.patch.object(execution, "pid_alive", lambda pid: alive[0]),
            mock.patch.object(execution, "process_started", lambda pid: None),
            mock.patch.object(execution, "kill_tree", kill_tree),
        ):
            return jobs_mod.cancel(self.workspace, "a-1"), asked

    def test_an_older_record_is_killed_only_as_unverified(self):
        job, asked = self.cancel_an_unverified_worker(stopped=True)
        self.assertEqual(asked, [False])
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["error"], "cancelled by request")

    def test_an_older_record_that_was_not_stopped_stays_unfinished(self):
        """Marking it cancelled would let `workflow remove` delete the
        workflow under a worker that may still be writing to it."""
        job, _ = self.cancel_an_unverified_worker(stopped=False)
        self.assertEqual(job["status"], "running")
        self.assertIn("pid 4242 was not stopped", job["not_stopped"])
        raw = ws.read_json(jobs_mod.job_path(self.workspace, "a-1"))
        self.assertEqual(raw["status"], "running")
        self.assertNotIn("not_stopped", raw)
        self.assertIn("not cancelled", jobs_mod.render(job))

    def test_a_start_time_that_cannot_be_read_now_is_unverified_not_gone(self):
        """A recorded start and a live pid whose start is unreadable (an
        OpenProcess denied on Windows) is not taken for a reused pid."""
        from orchestrator import execution

        self.record("a-1", pid=4242, pid_started="t0")
        with (
            mock.patch.object(execution, "pid_alive", lambda pid: True),
            mock.patch.object(execution, "process_started", lambda pid: None),
        ):
            self.assertIsNone(jobs_mod._worker_alive(4242, {"pid_started": "t0"}))
            self.assertEqual(present(jobs_mod.read_job(self.workspace, "a-1"))["status"], "running")
        job, asked = self.cancel_an_unverified_worker(stopped=True, pid_started="t0")
        self.assertEqual(asked, [False])
        self.assertEqual(job["status"], "cancelled")

    @unittest.skipUnless(os.name == "nt", "the case the issue measured")
    def test_an_older_record_leaves_a_live_bystander_running_on_windows(self):
        child = self.bystander()
        self.record("a-1", pid=child.pid, status="running")
        job = jobs_mod.cancel(self.workspace, "a-1")
        self.assertEqual(job["status"], "running")
        self.assertTrue(job["not_stopped"])
        time.sleep(0.5)
        self.assertIsNone(child.poll())


class TestWriteBackUnderTheLock(JobCase):
    """A worker that finishes between the parent's read and its write keeps
    its outcome (#271): the parent re-reads the record under the lock."""

    def finish_then(self, answer):
        """A stand-in that lets the worker record success, then answers."""
        job_file = jobs_mod.job_path(self.workspace, "a-1")

        def stand_in(*args, **kwargs):
            jobs_mod.finish(job_file, "succeeded", detail={"output_written": True})
            return answer

        return stand_in

    def assert_succeeded_on_disk(self, job):
        raw = ws.read_json(jobs_mod.job_path(self.workspace, "a-1"))
        self.assertEqual(raw["status"], "succeeded")
        self.assertTrue(raw["output_written"])
        self.assertNotIn("error", raw)
        self.assertEqual(job["status"], "succeeded")

    def test_a_worker_that_finishes_during_the_check_is_not_marked_abandoned(self):
        from orchestrator import execution

        self.record("a-1", pid=999_999)
        with mock.patch.object(execution, "pid_alive", self.finish_then(False)):
            job = present(jobs_mod.read_job(self.workspace, "a-1"))
        self.assert_succeeded_on_disk(job)

    def test_a_worker_that_finishes_as_it_is_cancelled_keeps_its_outcome(self):
        from orchestrator import execution

        self.record("a-1", pid=4242)
        with (
            mock.patch.object(jobs_mod, "_worker_alive", lambda pid, job: True),
            mock.patch.object(execution, "kill_tree", self.finish_then(True)),
        ):
            job = jobs_mod.cancel(self.workspace, "a-1")
        self.assert_succeeded_on_disk(job)

    def test_a_record_claimed_by_another_pid_meanwhile_is_left_alone(self):
        from orchestrator import execution

        self.record("a-1", pid=999_999)
        job_file = jobs_mod.job_path(self.workspace, "a-1")

        def reclaimed(pid):
            jobs_mod.update(job_file, lambda job: job.update(pid=os.getpid()))
            return False

        with mock.patch.object(execution, "pid_alive", reclaimed):
            jobs_mod.read_job(self.workspace, "a-1")
        self.assertEqual(ws.read_json(job_file)["status"], "running")


class TestProcessStarted(unittest.TestCase):
    def setUp(self):
        from orchestrator import execution

        self.execution = execution
        if execution.process_started(os.getpid()) is None:
            self.skipTest("no start time to read on this platform")

    def test_it_is_the_same_each_time_it_is_read(self):
        self.assertEqual(
            self.execution.process_started(os.getpid()), self.execution.process_started(os.getpid())
        )

    def test_another_process_reads_differently(self):
        import sys

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertNotEqual(
                self.execution.process_started(child.pid), self.execution.process_started(os.getpid())
            )
        finally:
            child.kill()
            child.wait(timeout=30)

    def test_no_process_reads_as_none(self):
        self.assertIsNone(self.execution.process_started(0))
        self.assertIsNone(self.execution.process_started(999_999))


class TestWorkerSide(JobCase):
    def test_claim_records_the_worker_pid(self):
        self.record("a-1", status="starting")
        path = jobs_mod.job_path(self.workspace, "a-1")
        jobs_mod.claim(path)
        job = jobs_mod.read_job(self.workspace, "a-1")
        assert job is not None
        self.assertEqual(job["pid"], os.getpid())
        self.assertEqual(job["status"], "running")

    def test_claim_records_when_the_worker_started_in_place_of_the_launchers(self):
        """The parent recorded the process it spawned, which on Windows can be
        a launcher in front of the worker; nothing of it may vouch for the
        worker's pid."""
        from orchestrator import execution

        self.record("a-1", status="running", pid=4242, pid_started="the launcher's")
        path = jobs_mod.job_path(self.workspace, "a-1")
        with mock.patch.object(execution, "process_started", lambda pid: "w" if pid == os.getpid() else None):
            self.assertEqual(jobs_mod.claim(path)["pid_started"], "w")
        with mock.patch.object(execution, "process_started", lambda pid: None):
            self.assertNotIn("pid_started", jobs_mod.claim(path))

    def test_finish_writes_the_outcome_and_the_output(self):
        self.record("a-1")
        path = jobs_mod.job_path(self.workspace, "a-1")
        jobs_mod.finish(path, "succeeded", output="the answer", detail={"exit_code": 0})
        job = jobs_mod.read_job(self.workspace, "a-1")
        assert job is not None
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual(ws.read_text(str(job["output_file"])).strip(), "the answer")

    def test_finish_records_a_failure_reason(self):
        self.record("a-1")
        jobs_mod.finish(jobs_mod.job_path(self.workspace, "a-1"), "failed", error="it broke")
        self.assertEqual(present(jobs_mod.read_job(self.workspace, "a-1"))["error"], "it broke")


def spawning(popen):
    """Stand ``popen`` in for ``Popen`` as the jobs module sees it, and nowhere else."""
    stand_in = types.SimpleNamespace(Popen=popen, DEVNULL=subprocess.DEVNULL)
    return mock.patch.object(jobs_mod, "subprocess", stand_in)


class TestStartAgainstAFastWorker(JobCase):
    def test_the_parent_does_not_overwrite_what_the_worker_already_wrote(self):
        """A worker that finishes before the parent records its pid keeps its outcome."""
        workspace = self.workspace

        class FinishedAtOnce:
            pid = 999_999

            def __init__(self, command, **_):
                path = command[command.index("--job-file") + 1]
                jobs_mod.finish(path, "failed", error="refused")

        with spawning(FinishedAtOnce):
            job = jobs_mod.start(
                workspace,
                "implementer",
                [
                    "run",
                    "implementer",
                    "--prompt-file",
                    jobs_mod.PROMPT_FILE,
                    "--job-file",
                    jobs_mod.JOB_FILE,
                ],
            )
        on_disk = ws.read_json(jobs_mod.job_path(workspace, job["id"]))
        for record in (job, on_disk):
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["error"], "refused")
        self.assertEqual(on_disk["pid"], 999_999)

    def test_a_worker_not_yet_heard_from_is_recorded_as_running(self):
        class Spawned:
            pid = 4242

            def __init__(self, command, **_):
                pass

        with spawning(Spawned):
            job = jobs_mod.start(
                self.workspace,
                "implementer",
                [
                    "run",
                    "implementer",
                    "--prompt-file",
                    jobs_mod.PROMPT_FILE,
                    "--job-file",
                    jobs_mod.JOB_FILE,
                ],
            )
        on_disk = ws.read_json(jobs_mod.job_path(self.workspace, job["id"]))
        self.assertEqual((on_disk["status"], on_disk["pid"]), ("running", 4242))

    def test_the_spawned_process_is_recorded_with_when_it_started(self):
        from orchestrator import execution

        with spawning(_Spawned), mock.patch.object(execution, "process_started", lambda pid: "s%d" % pid):
            job = jobs_mod.start(self.workspace, "implementer", WORKER_ARGV)
        self.assertEqual(ws.read_json(jobs_mod.job_path(self.workspace, job["id"]))["pid_started"], "s4242")


class _Spawned:
    pid = 4242

    def __init__(self, command, **_):
        pass


WORKER_ARGV = ["run", "architect", "--prompt-file", jobs_mod.PROMPT_FILE, "--job-file", jobs_mod.JOB_FILE]


class TestStartRecordsWhatTheWorkerCannotKnow(JobCase):
    def start(self, argv=WORKER_ARGV, **kwargs):
        with spawning(_Spawned):
            return jobs_mod.start(self.workspace, "architect", list(argv), prompt="F", **kwargs)

    def test_force_is_recorded(self):
        self.assertIs(self.start(force=True)["force"], True)
        self.assertIs(self.start()["force"], False)
        job = self.start(force=True)
        claimed = jobs_mod.claim(jobs_mod.job_path(self.workspace, job["id"]))
        self.assertIs(claimed["force"], True)

    def test_the_resume_prompt_is_copied_like_the_fresh_one(self):
        argv = [*WORKER_ARGV, "--resume-prompt-file", jobs_mod.RESUME_PROMPT_FILE]
        job = self.start(argv, resume_prompt="R")
        path = job["resume_prompt_file"]
        self.assertEqual(ws.read_text(path), "R")
        self.assertTrue(path.endswith("%s.resume-prompt" % job["id"]))
        self.assertIn(path, job["command"])
        self.assertNotIn(jobs_mod.RESUME_PROMPT_FILE, job["command"])

    def test_no_resume_prompt_leaves_nothing_behind(self):
        job = self.start()
        self.assertNotIn("resume_prompt_file", job)
        leftover = os.path.join(jobs_mod.jobs_dir(self.workspace), "%s.resume-prompt" % job["id"])
        self.assertFalse(os.path.exists(leftover))

    def test_the_placeholder_and_the_prompt_go_together(self):
        with self.assertRaises(ValueError):
            self.start(resume_prompt="R")
        with self.assertRaises(ValueError):
            self.start([*WORKER_ARGV, "--resume-prompt-file", jobs_mod.RESUME_PROMPT_FILE])


class TestRendering(JobCase):
    def test_render_mentions_the_status_and_stage(self):
        job = self.record("a-1", status="succeeded")
        rendered = jobs_mod.render(job)
        self.assertIn("SUCCEEDED", rendered)
        self.assertIn("implementer", rendered)

    def test_render_calls_out_a_wait_that_timed_out(self):
        job = dict(self.record("a-1"), waited_out=True)
        self.assertIn("still running", jobs_mod.render(job))


class TestDetachedRun(IsolatedCase):
    """The real thing: a worker process, started and reaped through the CLI."""

    def setUp(self):
        super().setUp()
        from test_cli import run_cli

        from orchestrator import execution

        self.run_cli = run_cli
        # A worker run in this process would otherwise install its SIGTERM
        # handler into the test runner.
        self.installed = self.enterContext(mock.patch.object(execution, "end_children_on_sigterm"))
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")

    def _wait_for(self, job_id, timeout=60):
        _, out, _ = self.run_cli("jobs", "wait", job_id, "--timeout", str(timeout), "--json")
        return json.loads(out)

    def test_a_detached_run_returns_a_job_immediately_and_completes(self):
        code, out, _ = self.run_cli("run", "implementer", "--prompt", "go", "--detach", "--json")
        self.assertEqual(code, 0)
        job = json.loads(out)
        self.assertIn(job["status"], ("running", "starting", "succeeded"))
        finished = self._wait_for(job["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertIn("mock implement response", ws.read_text(str(finished["output_file"])))

    def test_the_worker_is_given_the_prompt_it_was_started_with(self):
        """The one thing the mock cannot tell us by answering.

        It ignores the prompt, so every test here passed while the worker was
        told `--prompt-file -` with its stdin on DEVNULL and delegated an empty
        prompt. These two ask the provider what it was handed: the mock fails a
        run whose prompt contains `$DEV_ORCHESTRA_MOCK_FAIL`, and records the
        prompt's length in the usage it reports.
        """
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "needle"
        _, out, _ = self.run_cli(
            "run", "implementer", "--prompt", "a prompt with a needle in it", "--detach", "--json"
        )
        self.assertEqual(self._wait_for(json.loads(out)["id"])["status"], "failed")

        del os.environ["DEV_ORCHESTRA_MOCK_FAIL"]
        prompt = "counted to the character"
        _, out, _ = self.run_cli("run", "implementer", "--prompt", prompt, "--detach", "--json")
        self.assertEqual(self._wait_for(json.loads(out)["id"])["status"], "succeeded")
        report = json.loads(self.run_cli("tokens", "show", "--json")[1])
        self.assertEqual(report["by_stage"]["implementer"]["prompt_chars"], len(prompt))

    def test_extra_provider_args_do_not_swallow_the_workers_own_options(self):
        """`--extra` is REMAINDER, so it takes everything after it.

        Both worker options were appended past it, which made them provider
        arguments: the worker read no prompt, claimed no job, and the run ended
        `abandoned` after the parent had already spent the attempt.
        """
        prompt = "not for the provider to eat"
        _, out, _ = self.run_cli(
            "run", "implementer", "--prompt", prompt, "--detach", "--json", "--extra", "--verbose"
        )
        finished = self._wait_for(json.loads(out)["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertIn("claimed_at", finished)
        report = json.loads(self.run_cli("tokens", "show", "--json")[1])
        self.assertEqual(report["by_stage"]["implementer"]["prompt_chars"], len(prompt))

    def test_a_worker_records_why_it_could_not_read_its_prompt(self):
        """Its stderr is DEVNULL, so a complaint left there is a lost cause."""
        workspace = self.cli_workspace()
        jobs_mod.write_job(workspace, {"id": "p-1", "stage": "implementer", "status": "running"})
        with self.assertRaises(SystemExit) as raised:
            self.run_cli(
                "run",
                "implementer",
                "--force",
                "--prompt-file",
                os.path.join(self.project, "gone.md"),
                "--job-file",
                jobs_mod.job_path(workspace, "p-1"),
            )
        job = jobs_mod.read_job(workspace, "p-1")
        assert job is not None
        self.assertEqual(job["status"], "failed")
        self.assertIn("gone.md", job["error"])
        self.assertIn("does not exist", job["error"])
        # Passed through as it was raised: a path is not a credential.
        self.assertEqual(job["error"], str(raised.exception))

    def test_only_a_worker_makes_sigterm_end_its_cli(self):
        """`jobs cancel` reaches a worker's CLI only through this handler; the
        parent that detached it, or a plain run, keeps the default action."""
        code, _, err = self.run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 0, err)
        self.installed.assert_not_called()

        code, out, err = self.run_cli("run", "implementer", "--prompt", "go", "--detach", "--json")
        self.assertEqual(code, 0, err)
        self.installed.assert_not_called()
        self._wait_for(json.loads(out)["id"])

        workspace = self.cli_workspace()
        jobs_mod.write_job(workspace, {"id": "w-1", "stage": "implementer", "status": "running"})
        job_file = jobs_mod.job_path(workspace, "w-1")
        code, _, err = self.run_cli("run", "implementer", "--force", "--prompt", "go", "--job-file", job_file)
        self.assertEqual(code, 0, err)
        self.installed.assert_called_once_with(jobs_mod.stop_note_path(job_file))

    def test_the_detached_worker_does_not_double_spend_the_budget(self):
        self.run_cli("config", "set", "budgets.implementer", "5")
        code, out, _ = self.run_cli("run", "implementer", "--prompt", "go", "--detach", "--json")
        self.assertEqual(code, 0)
        self._wait_for(json.loads(out)["id"])
        payload = json.loads(self.run_cli("budget", "show", "--json")[1])
        self.assertEqual(payload["budgets"]["implementer"]["used"], 1)

    def test_the_worker_records_into_the_workflow_it_was_started_in(self):
        """The worker resolves its workflow again; left to itself it picks the
        environment's or the session's, not the one the parent was told."""
        self.assertNotEqual(os.environ.get("DEV_ORCHESTRA_WORKFLOW"), "named")
        code, out, _ = self.run_cli(
            "--workflow", "named", "run", "implementer", "--prompt", "go", "--detach", "--json"
        )
        self.assertEqual(code, 0)
        job = json.loads(out)
        code, out, err = self.run_cli(
            "--workflow", "named", "jobs", "wait", job["id"], "--timeout", "60", "--json"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "succeeded")
        for workflow, used in (("named", 1), ("", 0)):
            prefix = ["--workflow", workflow] if workflow else []
            budget = json.loads(self.run_cli(*prefix, "budget", "show", "--json")[1])
            self.assertEqual(budget["budgets"]["implementer"]["used"], used, workflow)
        state = json.loads(self.run_cli("--workflow", "named", "state", "show", "--json")[1])
        ended = [e for e in state.get("events") or [] if e.get("stage") == "implementer"]
        self.assertEqual([e.get("status") for e in ended], ["ok"])
        elsewhere = json.loads(self.run_cli("state", "show", "--json")[1])
        self.assertEqual([e for e in elsewhere.get("events") or [] if e.get("stage") == "implementer"], [])
        self.assertEqual(job["command"][:2], ["--cwd", os.getcwd()])
        self.assertEqual(job["command"][2:5], ["--workflow", "named", "run"])

    @unittest.skipUnless(has_git(), "git is required")
    def test_a_worker_reads_the_project_file_its_parent_read(self):
        """From a subdirectory with a file of its own, the worker used to start in
        the repository root and run the model the root's file names (#282)."""
        self.run_cli("config", "set", "implementer.model.family", "small")
        self.init_git_repo()
        self.write(".dev-orchestra.yaml", "version: 1\nimplementer:\n  model:\n    family: medium\n")
        self.write("pkg/.dev-orchestra.yaml", "version: 1\nimplementer:\n  model:\n    family: large\n")
        os.chdir(os.path.join(self.project, "pkg"))
        code, _, err = self.run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("run", "implementer", "--prompt", "go", "--detach", "--json")
        self.assertEqual(code, 0, err)
        job = json.loads(out)
        self.assertEqual(job["command"][:2], ["--cwd", os.getcwd()])
        self.assertEqual(self._wait_for(job["id"])["status"], "succeeded")
        # A relative --cwd from the root reaches the worker as the directory it names.
        os.chdir(self.project)
        # Named before the run: --cwd changes this process's directory too.
        expected = os.path.join(os.getcwd(), "pkg")
        code, out, err = self.run_cli(
            "--cwd", "pkg", "run", "implementer", "--prompt", "go", "--detach", "--json"
        )
        self.assertEqual(code, 0, err)
        job = json.loads(out)
        self.assertEqual(job["command"][:2], ["--cwd", expected])
        self.assertEqual(self._wait_for(job["id"])["status"], "succeeded")
        state = json.loads(self.run_cli("state", "show", "--json")[1])
        ran = [e.get("model") for e in state.get("events") or [] if e.get("stage") == "implementer"]
        self.assertEqual(ran, ["large", "large", "large"])
        # The root's own file is the one a run from the root reads.
        os.chdir(self.project)
        code, _, err = self.run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 0, err)
        state = json.loads(self.run_cli("state", "show", "--json")[1])
        ran = [e.get("model") for e in state.get("events") or [] if e.get("stage") == "implementer"]
        self.assertEqual(ran, ["large", "large", "large", "medium"])

    def test_a_worker_leaves_the_current_workflow_alone(self):
        """The pointer may have moved on since the parent returned."""
        from orchestrator import workflow as workflow_mod

        container = self.cli_workspace().container
        workflow_mod.write_pointer(container, "elsewhere", "requested")
        named = ws.Workspace(self.cli_workspace().root, container, "named").ensure()
        jobs_mod.write_job(named, {"id": "w-1", "stage": "implementer", "status": "running"})
        code, _, err = self.run_cli(
            "--workflow",
            "named",
            "run",
            "implementer",
            "--force",
            "--prompt",
            "go",
            "--job-file",
            jobs_mod.job_path(named, "w-1"),
        )
        self.assertEqual(code, 0, err)
        record = jobs_mod.read_job(named, "w-1")
        assert record is not None
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(workflow_mod.read_pointer(container), "elsewhere")

    def test_a_detached_failure_is_recorded_as_failed(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        _, out, _ = self.run_cli("run", "implementer", "--prompt", "go", "--detach", "--json")
        finished = self._wait_for(json.loads(out)["id"])
        self.assertEqual(finished["status"], "failed")

    def test_waiting_out_a_job_exits_with_its_own_code(self):
        self.record_running_job()
        code, _, _ = self.run_cli("jobs", "wait", "stuck-1", "--timeout", "1", "--poll", "0.1")
        self.assertEqual(code, 4)

    def record_running_job(self):
        workspace = self.cli_workspace()
        jobs_mod.write_job(
            workspace,
            {
                "id": "stuck-1",
                "stage": "implementer",
                "status": "running",
                "started_at": ws.utcnow(),
                "pid": os.getpid(),
                "output_file": jobs_mod.output_path(workspace, "stuck-1"),
            },
        )

    def test_jobs_list_and_show_work_through_the_cli(self):
        _, out, _ = self.run_cli("run", "implementer", "--prompt", "go", "--detach", "--json")
        job_id = json.loads(out)["id"]
        self._wait_for(job_id)
        self.assertIn(job_id, self.run_cli("jobs", "list")[1])
        code, shown, _ = self.run_cli("jobs", "show", job_id, "--output")
        self.assertEqual(code, 0)
        self.assertIn("mock implement response", shown)

    def test_a_refused_output_write_is_recorded_in_the_job(self):
        """The worker's stderr goes nowhere, so the job record has to carry it."""
        target = os.path.join(self.project, "plan.md")
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("# Plan\n\nevery section, all of it\n")
        os.environ["DEV_ORCHESTRA_MOCK_RESPONSE"] = "   \n"
        _, out, _ = self.run_cli(
            "run", "implementer", "--prompt", "go", "--detach", "--output", target, "--json"
        )
        job_id = self._wait_for(json.loads(out)["id"])["id"]
        record = self._settled(job_id)
        self.assertFalse(record["output_written"])
        self.assertEqual(record["output_target"], target)
        self.assertEqual(ws.read_text(target), "# Plan\n\nevery section, all of it\n")
        self.assertIn("was not updated", self.run_cli("jobs", "show", job_id)[1])
        # The run succeeded; the file it was told to fill did not get filled,
        # and `jobs wait` is how a chained caller learns which one it got.
        self.assertEqual(self.run_cli("jobs", "wait", job_id)[0], 1)

    def _settled(self, job_id, timeout=10):
        """The job record once the worker's last write has landed.

        The outcome is recorded before the output is saved -- nothing after the
        accounting may decide whether the run happened -- so a refusal is a
        second update, arriving just after the wait can already return.
        """
        deadline = time.monotonic() + timeout
        while True:
            job = json.loads(self.run_cli("jobs", "show", job_id, "--json")[1])
            if "output_written" in job or time.monotonic() >= deadline:
                return job
            time.sleep(0.1)

    def test_showing_an_unknown_job_fails_cleanly(self):
        code, _, err = self.run_cli("jobs", "show", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no such job", err)


class TestDetachedDeadline(IsolatedCase):
    """The deadline a detached `run` records, by role (#258); no worker is started."""

    def setUp(self):
        super().setUp()
        from test_cli import run_cli

        self.run_cli = run_cli
        run_cli("config", "setup", "--defaults")
        for role in ("architect", "implementer"):
            run_cli("config", "set", "%s.provider" % role, "mock")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")

    def recorded(self, *argv):
        with spawning(_Spawned):
            code, out, err = self.run_cli("run", *argv, "--prompt", "go", "--detach", "--json")
        self.assertEqual(code, 0, err)
        job = json.loads(out)
        on_disk = ws.read_json(jobs_mod.job_path(self.cli_workspace(), job["id"]))
        self.assertEqual(on_disk["timeout_seconds"], job["timeout_seconds"])
        return job

    def test_detached_implementer_records_3600_by_default(self):
        job = self.recorded("implementer")
        self.assertEqual(job["timeout_seconds"], 3600)
        # The worker works it out again from the same key, not from a flag.
        self.assertNotIn("--timeout", job["command"])
        self.assertEqual(self.recorded("architect")["timeout_seconds"], 1800)

    def test_run_timeout_key_sets_job_deadline(self):
        self.run_cli("config", "set", "run.timeout_seconds.architect", "900")
        self.assertEqual(self.recorded("architect")["timeout_seconds"], 900)
        self.assertEqual(self.recorded("implementer")["timeout_seconds"], 3600)

    def test_timeout_flag_overrides_key(self):
        self.run_cli("config", "set", "run.timeout_seconds.implementer", "5000")
        job = self.recorded("implementer", "--timeout", "120")
        self.assertEqual(job["timeout_seconds"], 120)
        self.assertIn("--timeout", job["command"])

    def test_run_reviewer_id_keeps_review_timeout(self):
        self.run_cli("config", "set", "review.timeout_seconds", "700")
        self.run_cli("config", "set", "run.timeout_seconds.implementer", "5000")
        self.assertEqual(self.recorded("m1")["timeout_seconds"], 700)

    def test_review_timeout_no_longer_bounds_run(self):
        self.run_cli("config", "set", "review.timeout_seconds", "900")
        self.assertEqual(self.recorded("implementer")["timeout_seconds"], 3600)
        self.assertEqual(self.recorded("architect")["timeout_seconds"], 1800)
        self.assertEqual(self.recorded("m1")["timeout_seconds"], 900)


class TestDetachedRuntimeCharges(IsolatedCase):
    """What a worker in another process may charge the ledger it shares.

    Every other test of this lives inside one process, where a token opened and
    closed by the same code cannot be surprised by a reset between the two.
    That is precisely the case the epoch stamp exists for, so it is the case
    worth paying a real worker to exercise.
    """

    def setUp(self):
        super().setUp()
        from test_cli import run_cli

        self.run_cli = run_cli
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")

    def _wait_for(self, job_id, timeout=60):
        _, out, _ = self.run_cli("jobs", "wait", job_id, "--timeout", str(timeout), "--json")
        return json.loads(out)

    def ledger(self):
        return self.cli_workspace().read_state().get("ledger") or {}

    def events(self):
        return self.cli_workspace().read_state().get("events") or []

    def _wait_for_in_flight(self, timeout=30):
        """Catch the worker while it is running, not after."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            in_flight = self.ledger().get("in_flight") or {}
            if in_flight:
                return in_flight
            time.sleep(0.1)
        self.fail("the worker never registered an in-flight stage")

    def test_a_detached_worker_charges_the_ledger_it_shares_with_its_parent(self):
        os.environ["DEV_ORCHESTRA_MOCK_DELAY"] = "0.5"
        _, out, _ = self.run_cli("run", "implementer", "--prompt", "go", "--detach", "--json")
        finished = self._wait_for(json.loads(out)["id"])
        self.assertEqual(finished["status"], "succeeded")
        event = self.events()[-1]
        self.assertGreater(event["duration_seconds"], 0)
        self.assertAlmostEqual(event["charged_seconds"], finished["duration_seconds"], places=2)
        self.assertAlmostEqual(self.ledger()["runtime_seconds"], event["charged_seconds"], places=1)
        self.assertEqual(self.ledger()["in_flight"], {})
        payload = json.loads(self.run_cli("budget", "show", "--json")[1])
        self.assertAlmostEqual(payload["runtime"]["used"], event["charged_seconds"], places=2)

    def test_a_detached_worker_leaves_its_sleep_out_of_the_shared_ledger(self):
        os.environ["DEV_ORCHESTRA_MOCK_DELAY"] = "0.5"
        os.environ["DEV_ORCHESTRA_MOCK_SUSPENDED"] = "90"
        _, out, _ = self.run_cli("run", "implementer", "--prompt", "go", "--detach", "--json")
        finished = self._wait_for(json.loads(out)["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["suspended_seconds"], 90)
        event = self.events()[-1]
        self.assertEqual(event["suspended_seconds"], 90)
        self.assertAlmostEqual(event["charged_seconds"], event["duration_seconds"] - 90, delta=0.02)
        self.assertAlmostEqual(self.ledger()["runtime_seconds"], event["charged_seconds"], places=1)
        self.assertEqual(self.ledger()["runtime_suspended_seconds"], 90)
        payload = json.loads(self.run_cli("budget", "show", "--json")[1])
        self.assertAlmostEqual(payload["runtime"]["used"], event["charged_seconds"], places=2)
        self.assertEqual(payload["runtime"]["suspended"], 90)

    def test_a_reset_while_the_worker_runs_leaves_the_new_budget_unbilled(self):
        os.environ["DEV_ORCHESTRA_MOCK_DELAY"] = "3"
        _, out, _ = self.run_cli("run", "implementer", "--prompt", "go", "--detach", "--json")
        self._wait_for_in_flight()
        self.assertEqual(self.run_cli("budget", "reset")[0], 0)
        finished = self._wait_for(json.loads(out)["id"])
        # The run itself is untouched: it succeeded, and it is recorded as
        # having succeeded. What it lost is the right to bill budgets that were
        # minted after it started.
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(self.ledger()["runtime_seconds"], 0)
        self.assertEqual(self.ledger()["in_flight"], {})
        event = self.events()[-1]
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["charged_seconds"], 0)
        self.assertIn("no in-flight entry", event["charge_skipped"])

    def test_a_cancelled_worker_is_charged_nothing(self):
        """Nobody measured it, so nobody bills it -- however long it ran."""
        os.environ["DEV_ORCHESTRA_MOCK_DELAY"] = "30"
        _, out, _ = self.run_cli("run", "implementer", "--prompt", "go", "--detach", "--json")
        self._wait_for_in_flight()
        self.assertEqual(self.run_cli("jobs", "cancel", json.loads(out)["id"])[0], 0)
        # `status` is what buries the entry, and the kill it follows is not
        # instant on either platform.
        deadline = time.monotonic() + 30
        while True:
            status = json.loads(self.run_cli("status", "--json")[1])
            if status["abandoned_stages"] or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        self.assertEqual(status["abandoned_stages"], ["implementer"])
        event = self.events()[-1]
        self.assertEqual(event["status"], "abandoned")
        self.assertEqual(event["charged_seconds"], 0)
        self.assertEqual(self.ledger()["runtime_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
