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
import time
import unittest

from helpers import IsolatedCase

from orchestrator import jobs as jobs_mod
from orchestrator import workspace as ws


class JobCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = ws.Workspace(self.project).ensure()

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
        self.assertEqual(job["status"], "abandoned")
        self.assertIn("is gone", job["error"])

    def test_the_abandoned_status_is_persisted_not_just_reported(self):
        self.record("a-1", pid=999_999)
        jobs_mod.read_job(self.workspace, "a-1")
        raw = ws.read_json(jobs_mod.job_path(self.workspace, "a-1"))
        self.assertEqual(raw["status"], "abandoned")

    def test_a_live_worker_is_left_alone(self):
        self.record("a-1", pid=os.getpid())
        self.assertEqual(jobs_mod.read_job(self.workspace, "a-1")["status"], "running")

    def test_a_finished_job_is_never_reinterpreted(self):
        self.record("a-1", pid=999_999, status="succeeded")
        self.assertEqual(jobs_mod.read_job(self.workspace, "a-1")["status"], "succeeded")

    def test_a_job_with_no_pid_yet_is_left_alone(self):
        self.record("a-1", status="starting")
        self.assertEqual(jobs_mod.read_job(self.workspace, "a-1")["status"], "starting")


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
        self.assertGreaterEqual(elapsed, 1)
        self.assertLess(elapsed, 10)

    def test_a_dead_worker_ends_the_wait_rather_than_timing_it_out(self):
        self.record("a-1", pid=999_999)
        job = jobs_mod.wait(self.workspace, "a-1", timeout=30, poll=0.1)
        self.assertEqual(job["status"], "abandoned")
        self.assertFalse(job.get("waited_out"))


class TestCancel(JobCase):
    def test_cancelling_a_live_worker_stops_it(self):
        import subprocess
        import sys

        from orchestrator import execution

        child = subprocess.Popen(
            [sys.executable, "-c", "import time\nwhile True: time.sleep(0.05)"],
            **execution._spawn_kwargs(),
        )
        try:
            self.record("a-1", pid=child.pid, status="running")
            job = jobs_mod.cancel(self.workspace, "a-1")
            self.assertEqual(job["status"], "cancelled")
            child.wait(timeout=30)
        finally:
            if child.poll() is None:  # pragma: no cover - safety net
                child.kill()

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


class TestWorkerSide(JobCase):
    def test_claim_records_the_worker_pid(self):
        self.record("a-1", status="starting")
        path = jobs_mod.job_path(self.workspace, "a-1")
        jobs_mod.claim(path)
        job = jobs_mod.read_job(self.workspace, "a-1")
        self.assertEqual(job["pid"], os.getpid())
        self.assertEqual(job["status"], "running")

    def test_finish_writes_the_outcome_and_the_output(self):
        self.record("a-1")
        path = jobs_mod.job_path(self.workspace, "a-1")
        jobs_mod.finish(path, "succeeded", output="the answer", detail={"exit_code": 0})
        job = jobs_mod.read_job(self.workspace, "a-1")
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual(ws.read_text(str(job["output_file"])).strip(), "the answer")

    def test_finish_records_a_failure_reason(self):
        self.record("a-1")
        jobs_mod.finish(jobs_mod.job_path(self.workspace, "a-1"), "failed", error="it broke")
        self.assertEqual(jobs_mod.read_job(self.workspace, "a-1")["error"], "it broke")


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

        self.run_cli = run_cli
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

    def test_the_detached_worker_does_not_double_spend_the_budget(self):
        self.run_cli("config", "set", "budgets.implementer", "5")
        code, out, _ = self.run_cli("run", "implementer", "--prompt", "go", "--detach", "--json")
        self.assertEqual(code, 0)
        self._wait_for(json.loads(out)["id"])
        payload = json.loads(self.run_cli("budget", "show", "--json")[1])
        self.assertEqual(payload["budgets"]["implementer"]["used"], 1)

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
        workspace = ws.Workspace(self.project).ensure()
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

    def test_showing_an_unknown_job_fails_cleanly(self):
        code, _, err = self.run_cli("jobs", "show", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no such job", err)


if __name__ == "__main__":
    unittest.main()
