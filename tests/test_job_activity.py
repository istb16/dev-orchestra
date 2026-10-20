"""What `jobs wait` and `jobs show` say about a job that is still working."""

from __future__ import annotations

import json
import os
import re
import time
import unittest
from typing import Any, Dict, List
from unittest import mock

from helpers import IsolatedCase, run_cli

from orchestrator import activity, execution
from orchestrator import jobs as jobs_mod
from orchestrator import workspace as ws

#: Keys whose values differ between two identical runs.
_VOLATILE = re.compile(
    r"(^|_)(id|pid|at|job|token|epoch|command|log|path|file|seconds|monotonic|session|duration)s?($|_)"
)


def normalised(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "<v>" if _VOLATILE.search(key) else normalised(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalised(item) for item in value]
    return value


class TestRenderingWithoutActivity(IsolatedCase):
    def test_a_finished_job_reads_as_it_always_did(self):
        workspace = self.cli_workspace()
        job = {
            "id": "a-1",
            "stage": "implementer",
            "status": "succeeded",
            "started_at": "2026-10-03T10:00:00Z",
            "claimed_at": "2026-10-03T10:00:01Z",
            "finished_at": "2026-10-03T10:05:00Z",
            "pid": 4242,
        }
        jobs_mod.write_job(workspace, job)
        expected = "\n".join(
            [
                "a-1  SUCCEEDED",
                "  stage:    implementer",
                "  started:  2026-10-03T10:00:00Z",
                "  finished: 2026-10-03T10:05:00Z",
                "  pid:      4242",
            ]
        )
        self.assertEqual(jobs_mod.render(job), expected)
        self.assertEqual(jobs_mod.render(job, None), expected)
        code, out, _ = run_cli("jobs", "show", "a-1", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn("activity", json.loads(out))
        self.assertEqual(json.loads(out), job)

    def test_elapsed_for_a_running_job(self):
        job = {"id": "a-1", "stage": "implementer", "status": "running", "claimed_at": ws.utcnow()}
        with mock.patch.object(time, "time", return_value=time.time() + 723):
            rendered = jobs_mod.render(job)
        self.assertRegex(rendered, r"  elapsed:  12m0[2-4]s")

    def test_the_activity_block(self):
        job = {"id": "a-1", "stage": "implementer", "status": "running", "started_at": ws.utcnow()}
        act = {
            "count": 37,
            "context_tokens": 84120,
            "lines": [
                {"n": 36, "s": 712.0, "line": "Read a.py"},
                {"n": 37, "s": None, "line": "Bash: python"},
            ],
            "dropped": True,
            "omitted": 5,
        }
        lines = jobs_mod.render(job, act).splitlines()
        tail = lines[lines.index("  activity: 37 tool uses, context 84,120 tokens") :]
        self.assertEqual(
            tail,
            [
                "  activity: 37 tool uses, context 84,120 tokens",
                "    +11:52  Read a.py",
                "    +--:--  Bash: python",
                "    (5 earlier lines not shown)",
                "    (some lines were dropped)",
                "  next:     jobs wait a-1 --since 37",
            ],
        )
        finished = dict(job, status="succeeded", finished_at=ws.utcnow())
        act = dict(act, context_tokens=None, omitted=0, dropped=False)
        rendered = jobs_mod.render(finished, act)
        self.assertIn("  activity: 37 tool uses\n", rendered + "\n")
        self.assertNotIn("next:", rendered)
        self.assertNotIn("elapsed:", rendered)


class TestElapsed(unittest.TestCase):
    NOW = 1_790_000_000.0

    def elapsed(self, **job):
        with mock.patch.object(time, "time", return_value=self.NOW):
            return jobs_mod.elapsed_seconds(job)

    def stamp(self, offset):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.NOW + offset))

    def test_from_the_claim_or_else_the_start(self):
        self.assertEqual(self.elapsed(claimed_at=self.stamp(-60), started_at=self.stamp(-90)), 60)
        self.assertEqual(self.elapsed(started_at=self.stamp(-90)), 90)
        self.assertEqual(self.elapsed(claimed_at="garbage", started_at=self.stamp(-90)), 90)

    def test_no_readable_start_is_none(self):
        self.assertIsNone(self.elapsed())
        self.assertIsNone(self.elapsed(claimed_at="garbage", started_at="2026-13-40"))
        self.assertIsNone(self.elapsed(claimed_at=None, started_at=12))

    def test_up_to_the_finish_when_there_is_one(self):
        self.assertEqual(self.elapsed(claimed_at=self.stamp(-600), finished_at=self.stamp(-300)), 300)
        self.assertIsNone(self.elapsed(claimed_at=self.stamp(-600), finished_at="not a time"))

    def test_a_clock_behind_the_claim_is_zero(self):
        self.assertEqual(self.elapsed(claimed_at=self.stamp(120)), 0)
        self.assertEqual(self.elapsed(claimed_at=self.stamp(0), finished_at=self.stamp(-5)), 0)

    def test_render_with_hours_and_without_a_start(self):
        claimed = self.stamp(-(2 * 3600 + 5))
        job = {"id": "a-1", "stage": "implementer", "status": "running", "claimed_at": claimed}
        with mock.patch.object(time, "time", return_value=self.NOW):
            self.assertIn("  elapsed:  2h00m05s\n", jobs_mod.render(job) + "\n")
            no_start = dict(job, claimed_at="garbage")
            self.assertNotIn("elapsed:", jobs_mod.render(no_start))


class TestFlags(IsolatedCase):
    def running_job_with_activity(self, count=3):
        workspace = self.cli_workspace()
        job = {"id": "a-1", "stage": "implementer", "status": "running", "claimed_at": ws.utcnow()}
        jobs_mod.write_job(workspace, job)
        path = jobs_mod.activity_path(jobs_mod.job_path(workspace, "a-1"))
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for n in range(1, count + 1):
                handle.write(
                    json.dumps({"n": n, "s": float(n), "line": "Read %d.py" % n, "tokens": None}) + "\n"
                )

    def test_the_boundary_values_are_accepted(self):
        self.running_job_with_activity()
        for argv in (
            ("--since", "0"),
            ("--since", str(activity.SINCE_MAX)),
            ("--activity", "0"),
            ("--activity", str(activity.LATEST_MAX)),
        ):
            for command in ("show", "wait"):
                extra = ("--timeout", "0.1", "--poll", "0.1") if command == "wait" else ()
                with self.subTest(argv=argv, command=command):
                    code, out, err = run_cli("jobs", command, "a-1", *argv, *extra)
                    self.assertEqual(code, 4 if command == "wait" else 0, err)
                    self.assertIn("  activity: 3 tool uses\n", out)

    def test_activity_zero_lists_no_lines_but_keeps_the_summary_and_cursor(self):
        self.running_job_with_activity()
        for command in ("show", "wait"):
            extra = ("--timeout", "0.1", "--poll", "0.1") if command == "wait" else ()
            with self.subTest(command=command):
                _, out, _ = run_cli("jobs", command, "a-1", "--activity", "0", *extra)
                self.assertIn("  activity: 3 tool uses\n", out)
                self.assertIn("    (3 earlier lines not shown)\n", out)
                self.assertIn("  next:     jobs wait a-1 --since 3", out)
                self.assertNotIn("Read ", out)
                _, out, _ = run_cli("jobs", command, "a-1", "--activity", "0", "--json", *extra)
                shown = json.loads(out)["activity"]
                self.assertEqual((shown["count"], shown["lines"], shown["omitted"]), (3, [], 3))
        _, out, _ = run_cli("jobs", "show", "a-1", "--activity", str(activity.LATEST_MAX))
        self.assertEqual(out.count("Read "), 3)
        _, out, _ = run_cli("jobs", "show", "a-1", "--since", str(activity.SINCE_MAX))
        self.assertNotIn("Read ", out)

    def test_out_of_range_values_are_usage_errors(self):
        for argv in (
            ("--since", "-1"),
            ("--since", str(activity.SINCE_MAX + 1)),
            ("--activity", "101"),
            ("--activity", "x"),
            ("--activity", "1.5"),
        ):
            for command in ("show", "wait"):
                with self.subTest(argv=argv, command=command), self.assertRaises(SystemExit) as raised:
                    run_cli("jobs", command, "a-1", *argv)
                self.assertEqual(raised.exception.code, 2)


class TestQuiet(IsolatedCase):
    """How long a running job has shown no new tool line, and the stuck notice (#330)."""

    NOW = 1_790_000_000.0

    def stamp(self, offset):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.NOW + offset))

    def job_with(self, entries, status="running"):
        workspace = self.cli_workspace()
        job = {"id": "a-1", "stage": "implementer", "status": status, "claimed_at": self.stamp(-1000)}
        if status != "running":
            job["finished_at"] = self.stamp(0)
        jobs_mod.write_job(workspace, job)
        path = jobs_mod.activity_path(jobs_mod.job_path(workspace, "a-1"))
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(json.dumps(entry) + "\n" for entry in entries))
        return workspace, job

    def read(self, entries, notice=600, status="running", since=0):
        workspace, job = self.job_with(entries, status)
        with mock.patch.object(time, "time", return_value=self.NOW):
            return jobs_mod.read_activity(workspace, job, since, 10, notice)

    def test_from_the_last_lines_wall_clock_time(self):
        entries = [
            {"n": 1, "s": 10.0, "t": self.NOW - 990, "line": "Read a.py", "tokens": None},
            {"n": 2, "s": 300.0, "t": self.NOW - 700, "line": "Bash: pytest", "tokens": None},
            {"s": 900.0, "tokens": 5000},
        ]
        for since in (0, 2):
            act = self.read(entries, since=since)
            assert act is not None
            with self.subTest(since=since):
                self.assertEqual(act["quiet_seconds"], 700)
                self.assertTrue(act["may_be_stuck"])
                self.assertNotIn("last_at", act)
                self.assertNotIn("last_s", act)

    def test_a_file_from_before_t_uses_its_offset(self):
        act = self.read([{"n": 1, "s": 200.0, "line": "Read a.py", "tokens": None}])
        assert act is not None
        self.assertEqual(act["quiet_seconds"], 800)

    def test_no_tool_line_yet_is_the_whole_run(self):
        act = self.read([{"s": 5.0, "tokens": 100}])
        assert act is not None
        self.assertEqual((act["count"], act["quiet_seconds"]), (0, 1000))

    def test_the_threshold(self):
        entries = [{"n": 1, "s": 300.0, "t": self.NOW - 700, "line": "Read a.py", "tokens": None}]
        for notice, stuck in ((699, True), (700, True), (701, False), (0, False)):
            act = self.read(entries, notice=notice)
            assert act is not None
            with self.subTest(notice=notice):
                self.assertIs(act["may_be_stuck"], stuck)
        act = self.read(entries, notice=1, status="succeeded")
        assert act is not None
        self.assertIsNone(act["quiet_seconds"])
        self.assertIs(act["may_be_stuck"], False)

    def test_the_note_comes_before_next(self):
        job = {"id": "a-1", "stage": "implementer", "status": "running", "started_at": ws.utcnow()}
        act = {"count": 2, "lines": [], "quiet_seconds": 723, "may_be_stuck": True}
        lines = jobs_mod.render(job, act).splitlines()
        self.assertEqual(
            lines[-2:],
            ["  note:     may be stuck: no new tool line for 12m03s", "  next:     jobs wait a-1 --since 2"],
        )
        self.assertNotIn("may be stuck", jobs_mod.render(job, dict(act, may_be_stuck=False)))
        finished = dict(job, status="failed", finished_at=ws.utcnow())
        self.assertNotIn("may be stuck", jobs_mod.render(finished, act))

    def test_every_wait_repeats_the_note_while_the_run_is_silent(self):
        workspace = self.cli_workspace()
        job = {"id": "a-1", "stage": "implementer", "status": "running", "claimed_at": ws.utcnow()}
        jobs_mod.write_job(workspace, job)
        path = jobs_mod.activity_path(jobs_mod.job_path(workspace, "a-1"))
        entry = {"n": 1, "s": 1.0, "t": time.time() - 1000, "line": "Read a.py", "tokens": None}
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry) + "\n")
        for since in ("0", "1"):
            code, out, err = run_cli(
                "jobs", "wait", "a-1", "--since", since, "--timeout", "0.1", "--poll", "0.1"
            )
            self.assertEqual(code, 4, err)
            self.assertRegex(
                out, r"\n  note:     may be stuck: no new tool line for 16m[4-5]\ds\n  next:     "
            )
        code, out, _ = run_cli("jobs", "show", "a-1", "--json")
        self.assertTrue(json.loads(out)["activity"]["may_be_stuck"])
        self.assertEqual(run_cli("config", "set", "progress.quiet_notice_seconds", "0")[0], 0)
        _, out, _ = run_cli("jobs", "show", "a-1")
        self.assertNotIn("may be stuck", out)

    def test_wait_defaults_to_the_configured_interval(self):
        workspace = self.cli_workspace()
        job = {"id": "a-1", "stage": "implementer", "status": "running", "claimed_at": ws.utcnow()}
        jobs_mod.write_job(workspace, job)
        for argv, expected in (((), 180), (("--timeout", "5"), 5)):
            with mock.patch.object(jobs_mod, "wait", return_value=dict(job, waited_out=True)) as wait:
                self.assertEqual(run_cli("jobs", "wait", "a-1", *argv)[0], 4)
            with self.subTest(argv=argv):
                self.assertEqual(wait.call_args.kwargs["timeout"], expected)
        self.assertEqual(run_cli("config", "set", "progress.interval_seconds", "45")[0], 0)
        with mock.patch.object(jobs_mod, "wait", return_value=dict(job, waited_out=True)) as wait:
            run_cli("jobs", "wait", "a-1")
        self.assertEqual(wait.call_args.kwargs["timeout"], 45)


class TestDetachedActivity(IsolatedCase):
    """A real worker, reporting its mock run's tool uses beside the job."""

    def setUp(self):
        super().setUp()
        # A worker run in this process would otherwise install its SIGTERM
        # handler into the test runner.
        self.enterContext(mock.patch.object(execution, "end_children_on_sigterm"))
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")
        self.started: List[str] = []

    def tearDown(self):
        # Before the base tearDown leaves the project: a cleanup would run
        # after it, from another directory.
        for job_id in self.started:
            run_cli("jobs", "cancel", job_id)
        super().tearDown()

    def start(self) -> str:
        code, out, err = run_cli("run", "implementer", "--prompt", "go", "--detach", "--json")
        self.assertEqual(code, 0, err)
        job_id = json.loads(out)["id"]
        self.started.append(job_id)
        return job_id

    def entries(self, job_id: str, count: int, timeout: float = 30) -> List[Dict[str, Any]]:
        """The job's activity entries, once there are ``count`` of them."""
        path = jobs_mod.activity_path(jobs_mod.job_path(self.cli_workspace(), job_id))
        deadline = time.monotonic() + timeout
        while True:
            try:
                with open(path, encoding="utf-8") as handle:
                    found = [json.loads(line) for line in handle if line.endswith("\n")]
            except OSError:
                found = []
            if len(found) >= count:
                return found
            self.assertLess(time.monotonic(), deadline, "the worker never wrote %d entries" % count)
            time.sleep(0.1)

    def test_a_running_job_shows_its_tool_uses(self):
        os.environ["DEV_ORCHESTRA_MOCK_ACTIVITY"] = "Read a.py|Bash: pytest"
        os.environ["DEV_ORCHESTRA_MOCK_DELAY"] = "30"
        job_id = self.start()
        self.assertEqual([entry["line"] for entry in self.entries(job_id, 2)], ["Read a.py", "Bash: pytest"])

        code, out, _ = run_cli("jobs", "wait", job_id, "--timeout", "0.5", "--poll", "0.1")
        self.assertEqual(code, 4)
        self.assertRegex(out, r"\n  elapsed:  \d+m\d\ds\n")
        self.assertIn("  activity: 2 tool uses\n", out)
        self.assertIn("Read a.py", out)
        self.assertIn("  next:     jobs wait %s --since 2" % job_id, out)

        code, out, _ = run_cli("jobs", "wait", job_id, "--timeout", "0.1", "--since", "1")
        self.assertEqual(code, 4)
        self.assertIn("Bash: pytest", out)
        self.assertNotIn("Read a.py", out)

        code, out, _ = run_cli("jobs", "show", job_id, "--json")
        self.assertEqual(code, 0)
        shown = json.loads(out)["activity"]
        self.assertEqual(shown["count"], 2)
        self.assertEqual([line["line"] for line in shown["lines"]], ["Read a.py", "Bash: pytest"])
        self.assertIsInstance(shown["elapsed_seconds"], int)
        self.assertFalse(shown["dropped"])
        # Only the output gains the key: the record on disk is as it was.
        record = ws.read_json(jobs_mod.job_path(self.cli_workspace(), job_id))
        self.assertNotIn("activity", record)

    def test_before_its_first_tool_use_a_job_shows_only_how_long_it_has_run(self):
        os.environ["DEV_ORCHESTRA_MOCK_DELAY"] = "30"
        job_id = self.start()
        code, out, _ = run_cli("jobs", "wait", job_id, "--timeout", "0.5", "--poll", "0.1")
        self.assertEqual(code, 4)
        self.assertIn("  elapsed:  ", out)
        self.assertNotIn("activity:", out)
        self.assertNotIn("activity", json.loads(run_cli("jobs", "show", job_id, "--json")[1]))

    def test_activity_changes_nothing_a_run_records(self):
        workspace = self.cli_workspace()

        def finished_run():
            before = len(workspace.read_state().get("events") or [])
            code, out, err = run_cli("run", "implementer", "--prompt", "go", "--detach", "--json")
            self.assertEqual(code, 0, err)
            job_id = json.loads(out)["id"]
            _, out, _ = run_cli("jobs", "wait", job_id, "--timeout", "60", "--json")
            job = json.loads(out)
            self.assertEqual(job["status"], "succeeded")
            with open(job["output_file"], "rb") as handle:
                output = handle.read()
            events = (workspace.read_state().get("events") or [])[before:]
            return job, output, events

        plain = finished_run()
        os.environ["DEV_ORCHESTRA_MOCK_ACTIVITY"] = "Read a.py|Bash: pytest"
        reporting = finished_run()
        self.assertNotIn("activity", plain[0])
        self.assertIn("activity", reporting[0])
        self.assertEqual(reporting[1], plain[1])
        records = [normalised(dict(job, activity=None)) for job in (plain[0], reporting[0])]
        self.assertEqual(records[1], records[0])
        self.assertEqual(normalised(reporting[2]), normalised(plain[2]))


if __name__ == "__main__":
    unittest.main()
