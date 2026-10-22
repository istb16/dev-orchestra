"""``run implementer --unit``: a work unit, in a worktree of its own or in place."""

from __future__ import annotations

import json
import os
import subprocess
import types
import unittest
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

from helpers import IsolatedCase, has_git, is_link, read_file, run_cli
from test_units import plan

from orchestrator import execution_tree
from orchestrator import jobs as jobs_mod
from orchestrator import units as units_mod
from orchestrator import workspace as ws
from orchestrator import worktrees as worktrees_mod
from orchestrator.providers.mock import MockProvider

PAIR = [{"name": "a", "files": ["a.py"]}, {"name": "b", "files": ["b.py"]}]
CHAIN = [{"name": "a", "files": ["a.py"]}, {"name": "b", "files": ["b.py"], "depends_on": ["a"]}]


@unittest.skipUnless(has_git(), "needs git")
class UnitRunCase(IsolatedCase):
    """A committed project, a mock implementer, and a record of every run it makes."""

    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write(".gitignore", ".venv/\n")
        self.write("app.py", "print('hello')\n")
        self.commit_all("base")
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")
        self.workspace = self.cli_workspace()
        self.units_parent = os.path.join(self.tmp, "project.units")
        #: What each run of the mock was given: its cwd and prompt.
        self.calls: List[Dict[str, Any]] = []
        #: Files the "agent" writes in its cwd, relative path -> text.
        self.edits: Dict[str, str] = {}
        #: Called with the run's cwd while the agent "runs".
        self.during: Optional[Callable[[str], None]] = None
        original = MockProvider.run

        def recording(provider, prompt, mode, cwd, *args, **kwargs):
            self.calls.append({"cwd": cwd, "prompt": prompt})
            for relative, text in self.edits.items():
                ws.write_text(os.path.join(cwd, relative), text)
            if self.during is not None:
                self.during(cwd)
            return original(provider, prompt, mode, cwd, *args, **kwargs)

        self.enterContext(mock.patch.object(MockProvider, "run", recording))

    def approve(self, entries: List[Dict[str, Any]], extra: str = "") -> None:
        ws.write_text(self.workspace.plan_path, plan(entries) + extra)
        code, _, err = run_cli("design", "approve")
        self.assertEqual(code, 0, err)

    def run_unit(self, name: str, *extra: str):
        return run_cli("run", "implementer", "--unit", name, "--prompt", "go", *extra)

    def record(self, name: str) -> Dict[str, Any]:
        return units_mod.load_state(self.workspace)["units"].get(name) or {}

    def set_records(self, entries: Dict[str, Dict[str, Any]]) -> None:
        key = units_mod.pass_key(self.workspace, "implementer")
        ws.write_json(self.workspace.units_path, {"plan_sha256": key, "units": entries})

    def used(self) -> int:
        return json.loads(run_cli("budget", "show", "--json")[1])["budgets"]["implementer"]["used"]

    def worktree_count(self) -> int:
        return self.git("worktree", "list", "--porcelain").stdout.count("worktree ")

    def assert_no_worktree(self) -> None:
        left = os.listdir(self.units_parent) if os.path.isdir(self.units_parent) else []
        self.assertEqual(left, [])
        self.assertEqual(self.worktree_count(), 1)

    def implementer_events(self) -> List[Dict[str, Any]]:
        events = self.workspace.read_state().get("events") or []
        return [event for event in events if event.get("stage") == "implementer"]


class TestRefusals(UnitRunCase):
    def test_run_unit_refused_unapproved_exit_5(self):
        ws.write_text(self.workspace.plan_path, plan(PAIR))
        code, _, err = self.run_unit("a")
        self.assertEqual(code, 5, err)
        self.assertEqual(self.record("a"), {})
        self.assertFalse(os.path.exists(self.units_parent))
        self.assertEqual(self.worktree_count(), 1)
        self.assertEqual(self.calls, [])

    def test_run_unit_refused_dependency_not_merged(self):
        self.approve(CHAIN)
        code, _, err = self.run_unit("b")
        self.assertEqual(code, 2)
        self.assertIn("depends on a (pending), not merged yet", err)
        self.assertEqual(self.used(), 0)
        self.assertEqual(self.record("b"), {})
        self.assertEqual(self.calls, [])

    def test_run_unit_refused_parallel_cap(self):
        run_cli("config", "set", "implementation.max_parallel", "1")
        self.approve(PAIR)
        self.set_records({"a": {"status": "running", "job": "implementer-1", "in_place": False}})
        code, _, err = self.run_unit("b")
        self.assertEqual(code, 2)
        self.assertIn("implementation.max_parallel is 1, and a is running", err)
        self.assertEqual(self.used(), 0)
        self.assertEqual(self.calls, [])

    def test_run_unit_in_place_blocks_others(self):
        self.approve(PAIR)
        self.set_records({"a": {"status": "running", "job": "implementer-1", "in_place": True}})
        code, _, err = self.run_unit("b")
        self.assertEqual(code, 2)
        self.assertIn("a is running in place", err)
        self.assertEqual(self.calls, [])

    def test_run_unit_refused_for_another_role(self):
        self.approve(PAIR)
        code, _, err = run_cli("run", "review_fixer", "--unit", "a", "--prompt", "go")
        self.assertEqual(code, 2)
        self.assertIn("--unit applies to implementer", err)

    def test_run_unit_refused_until_its_result_is_merged(self):
        self.approve(PAIR)
        for status, said in (
            ("succeeded", "run units merge a first"),
            ("conflicted", "units merge a --resolved"),
        ):
            with self.subTest(status=status):
                self.set_records({"a": {"status": status, "in_place": False, "conflicts": ["a.py"]}})
                code, _, err = self.run_unit("a")
                self.assertEqual(code, 2)
                self.assertIn(said, err)
                self.assertEqual(self.record("a")["status"], status)
                self.assertEqual(self.record("a")["conflicts"], ["a.py"])
        self.assertEqual(self.used(), 0)
        self.assertEqual(self.calls, [])

    def test_run_unit_refused_unknown_unit(self):
        self.approve(PAIR)
        code, _, err = self.run_unit("ghost")
        self.assertEqual(code, 2)
        self.assertIn("has no such unit", err)
        self.assertEqual(self.record("ghost"), {})

    def test_run_unit_refused_while_an_earlier_plan_holds_a_worktree(self):
        self.approve(PAIR)
        self.set_records({"a": {"status": "failed", "in_place": False, "worktree": self.units_parent}})
        self.approve(PAIR, extra="\nRevised.\n")
        code, _, err = self.run_unit("b")
        self.assertEqual(code, 2)
        self.assertIn("units of an earlier plan are running or hold a worktree (a)", err)
        self.assertEqual(self.record("a")["worktree"], self.units_parent)
        self.assertEqual(self.calls, [])

    def test_run_unit_worktree_failure_puts_the_record_back(self):
        self.approve(PAIR)
        before = {"status": "failed", "in_place": False, "worktree": None, "finished_at": "t"}
        self.set_records({"a": dict(before)})
        failing = mock.patch.object(worktrees_mod, "create", side_effect=worktrees_mod.WorktreeError("no"))
        with failing:
            code, _, err = self.run_unit("a")
        self.assertEqual(code, 2)
        self.assertIn("no worktree could be made: no", err)
        self.assertEqual(self.record("a"), before)
        self.assertEqual(self.used(), 0)
        self.assertEqual(self.calls, [])


class TestWorktreeRuns(UnitRunCase):
    def test_run_unit_mock_runs_in_worktree(self):
        self.approve(PAIR)
        self.edits = {"a.py": "A = 1\n"}
        code, _, err = self.run_unit("a")
        self.assertEqual(code, 0, err)
        cwd = self.calls[0]["cwd"]
        self.assertEqual(os.path.dirname(cwd), self.units_parent)
        self.assertTrue(self.calls[0]["prompt"].startswith("# Work unit: a (feature)\n"))
        self.assertIn("### Unit: a", self.calls[0]["prompt"])
        self.assertTrue(self.calls[0]["prompt"].endswith("go"))
        # The books stay in the main .ai/.
        self.assertEqual(self.used(), 1)
        events = self.implementer_events()
        self.assertEqual(events[-1]["unit"], "a")
        self.assertTrue(events[-1]["attempt_spent"])
        record = self.record("a")
        self.assertEqual(record["status"], "succeeded")
        self.assertFalse(record["in_place"])
        self.assertIsNone(record["worktree"])
        self.assertRegex(record["result_tree"], r"^[0-9a-f]{40}$")
        self.assertIn("a.py", read_file(os.path.join(self.workspace.units_dir, "a.diff")))
        # Not merged: the user's tree does not have it yet.
        self.assertFalse(os.path.exists(os.path.join(self.project, "a.py")))

    def test_run_unit_not_in_place_with_independent_peer(self):
        self.approve(PAIR)
        self.assertEqual(self.run_unit("a")[0], 0)
        self.assertNotEqual(self.calls[0]["cwd"], self.project)
        self.assertFalse(self.record("a")["in_place"])

    def test_run_unit_base_includes_uncommitted_and_untracked(self):
        self.approve(PAIR)
        self.write("app.py", "print('edited')\n")
        self.write("notes/new.txt", "untracked\n")
        seen: Dict[str, str] = {}

        def look(cwd: str) -> None:
            seen["app"] = read_file(os.path.join(cwd, "app.py"))
            seen["new"] = read_file(os.path.join(cwd, "notes", "new.txt"))

        self.during = look
        self.assertEqual(self.run_unit("a")[0], 0)
        self.assertEqual(seen, {"app": "print('edited')\n", "new": "untracked\n"})

    def test_run_unit_leaves_user_index_and_head(self):
        self.approve(PAIR)
        self.write("app.py", "print('edited')\n")
        before = (
            self.git("status", "--porcelain").stdout,
            self.git("rev-parse", "HEAD").stdout,
            self.git("branch", "--list").stdout,
        )
        self.edits = {"a.py": "A = 1\n"}
        self.assertEqual(self.run_unit("a")[0], 0)
        after = (
            self.git("status", "--porcelain").stdout,
            self.git("rev-parse", "HEAD").stdout,
            self.git("branch", "--list").stdout,
        )
        self.assertEqual(after, before)

    def test_run_unit_result_tree_excludes_links_and_workspace(self):
        kept = self.write(".venv/lib/kept.txt", "env\n")
        self.approve(PAIR)
        seen: Dict[str, bool] = {}

        def look(cwd: str) -> None:
            seen["linked"] = is_link(os.path.join(cwd, ".venv"))
            seen["plan"] = os.path.isfile(
                os.path.join(cwd, os.path.relpath(self.workspace.plan_path, self.project))
            )

        self.during = look
        self.edits = {"a.py": "A = 1\n"}
        self.assertEqual(self.run_unit("a")[0], 0)
        self.assertEqual(seen, {"linked": True, "plan": True})
        names = self.git("ls-tree", "-r", "--name-only", self.record("a")["result_tree"]).stdout.split()
        self.assertIn("a.py", names)
        self.assertFalse([name for name in names if name.startswith((".venv", ".ai"))], names)
        self.assertEqual(read_file(kept), "env\n")

    def test_run_unit_worktree_removed_on_success_and_failure(self):
        self.approve(PAIR)
        self.assertEqual(self.run_unit("a")[0], 0)
        self.assertEqual(self.record("a")["status"], "succeeded")
        self.assert_no_worktree()
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        code, _, _ = self.run_unit("b")
        self.assertEqual(code, 1)
        self.assertEqual(self.record("b")["status"], "failed")
        self.assertIsNone(self.record("b")["worktree"])
        self.assert_no_worktree()
        self.assertTrue(os.path.isfile(os.path.join(self.workspace.units_dir, "b.diff")))

    def test_run_unit_whose_result_tree_could_not_be_written_is_failed(self):
        self.approve(PAIR)
        original = ws.write_tree
        worktrees = os.path.normcase(os.path.join(self.units_parent, ""))

        def failing(root: str, *args: Any, **kwargs: Any) -> str:
            if os.path.normcase(os.path.abspath(root)).startswith(worktrees):
                return ""
            return original(root, *args, **kwargs)

        with mock.patch.object(ws, "write_tree", failing):
            _, _, err = self.run_unit("a")
        self.assertIn("its result tree could not be written", err)
        record = self.record("a")
        self.assertEqual(record["status"], "failed")
        self.assertIsNone(record["result_tree"])
        self.assertIn("result tree", record["error"])
        self.assert_no_worktree()
        # Failed, it can be run again.
        code, _, err = self.run_unit("a")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.record("a")["status"], "succeeded")
        self.assertIsNone(self.record("a").get("error"))


class TestInPlace(UnitRunCase):
    def test_run_unit_in_place_for_serial_chain(self):
        self.approve(CHAIN)
        self.edits = {"a.py": "A = 1\n"}
        code, _, err = self.run_unit("a")
        self.assertEqual(code, 0, err)
        self.edits = {"b.py": "B = 2\n"}
        code, _, err = self.run_unit("b")
        self.assertEqual(code, 0, err)
        self.assertEqual([call["cwd"] for call in self.calls], [self.project, self.project])
        for name, path in (("a", "a.py"), ("b", "b.py")):
            record = self.record(name)
            self.assertEqual(record["status"], "merged")
            self.assertTrue(record["in_place"])
            self.assertIsNone(record["worktree"])
            self.assertTrue(record["merged_at"])
            self.assertIn(path, read_file(os.path.join(self.workspace.units_dir, "%s.diff" % name)))
        self.assertEqual(read_file(os.path.join(self.project, "b.py")), "B = 2\n")
        self.assertFalse(os.path.exists(self.units_parent))
        # Both pieces of one pass: one attempt.
        self.assertEqual(self.used(), 1)
        self.assertEqual([event["attempt_spent"] for event in self.implementer_events()], [True, False])


class TestPassBudget(UnitRunCase):
    def test_ledger_pass_exhausted_exit_3(self):
        three = [*PAIR, {"name": "c", "files": ["c.py"]}]
        run_cli("config", "set", "budgets.implementer", "1")
        self.approve(three)
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        self.assertEqual(self.run_unit("a")[0], 1)
        # A rerun in the same pass spends an attempt, and there is none left.
        code, _, err = self.run_unit("a")
        self.assertEqual(code, 3, err)
        self.assert_no_worktree()
        self.assertEqual(self.record("a")["status"], "failed")
        del os.environ["DEV_ORCHESTRA_MOCK_FAIL"]
        # A piece of the open pass that has not run yet still runs.
        self.assertEqual(self.run_unit("b")[0], 0)
        self.assertEqual(self.used(), 1)
        # A new pass spends an attempt.
        self.approve(three, extra="\nRevised.\n")
        code, _, err = self.run_unit("a")
        self.assertEqual(code, 3, err)
        self.assert_no_worktree()
        self.assertEqual(self.record("a"), {})
        # A spent total_delegated_runs refuses a rerun of a failed unit and the
        # pass's later pieces alike.
        run_cli("config", "set", "budgets.implementer", "10")
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        self.assertEqual(self.run_unit("a")[0], 1)
        del os.environ["DEV_ORCHESTRA_MOCK_FAIL"]
        run_cli("config", "set", "budgets.total_delegated_runs", "3")
        for name in ("a", "b", "c"):
            with self.subTest(name=name):
                code, _, err = self.run_unit(name)
                self.assertEqual(code, 3, err)
                self.assertIn("its budget of 3 delegated runs", err)
                self.assert_no_worktree()
        self.approve(three, extra="\nRevised again.\n")
        code, _, err = self.run_unit("a")
        self.assertEqual(code, 3, err)
        self.assert_no_worktree()


class TestDetached(UnitRunCase):
    """``--detach``: the parent claims the unit and starts a worker, run here in-process."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(execution_tree, "end_children_on_sigterm"))
        #: Each worker's argv, as `jobs.start` would have spawned it.
        self.workers: List[List[str]] = []
        entry = jobs_mod._entry_point()

        def popen(command: List[str], *args: Any, **kwargs: Any) -> Any:
            self.assertEqual(command[1], entry)
            self.workers.append(list(command[2:]))
            return types.SimpleNamespace(pid=os.getpid())

        fake = types.SimpleNamespace(Popen=popen, DEVNULL=subprocess.DEVNULL)
        self.enterContext(mock.patch.object(jobs_mod, "subprocess", fake))

    def start(self, name: str) -> Dict[str, Any]:
        code, out, err = self.run_unit(name, "--detach", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.workers), 1)
        return json.loads(out)

    def job(self, job_id: str) -> Dict[str, Any]:
        found = jobs_mod.read_job(self.workspace, job_id)
        assert found is not None
        return found

    def test_run_unit_detached_worker_runs_in_the_worktree(self):
        self.approve(PAIR)
        self.edits = {"a.py": "A = 1\n"}
        job = self.start("a")
        record = self.record("a")
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["job"], job["id"])
        self.assertEqual((job["unit"], job["worktree"]), ("a", record["worktree"]))
        argv = self.workers[0]
        self.assertEqual(argv[argv.index("--unit") + 1], "a")
        self.assertEqual(self.calls, [])

        code, _, err = run_cli(*argv)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls[0]["cwd"], record["worktree"])
        self.assertTrue(self.calls[0]["prompt"].startswith("# Work unit: a (feature)\n"))
        code, out, err = run_cli("jobs", "wait", job["id"], "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "succeeded")
        record = self.record("a")
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["job"], job["id"])
        self.assertIsNone(record["worktree"])
        self.assert_no_worktree()
        # One attempt, spent by the parent.
        self.assertEqual(self.used(), 1)

    def test_run_unit_detached_worker_refused_when_its_worktree_is_gone(self):
        self.approve(PAIR)
        job = self.start("a")
        self.assertTrue(worktrees_mod.remove(self.project, self.record("a")))
        code, _, err = run_cli(*self.workers[0])
        self.assertEqual(code, 2)
        self.assertIn("its worktree is gone", err)
        finished = self.job(job["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertIn("its worktree is gone", finished["error"])
        self.assertEqual(self.record("a")["status"], "failed")
        self.assertEqual(self.calls, [])
        self.assert_no_worktree()

    def test_run_unit_detached_worker_refused_by_a_gate_releases_the_unit(self):
        """The parent spends the attempt before handing over, so the worker
        never meets the budget; what it can meet is a gate, such as an
        approval gone stale."""
        self.approve(PAIR)
        job = self.start("a")
        ws.write_text(self.workspace.plan_path, plan(PAIR) + "\nChanged after the approval.\n")
        code, _, err = run_cli(*self.workers[0])
        self.assertEqual(code, 5, err)
        self.assertEqual(self.job(job["id"])["status"], "failed")
        record = self.record("a")
        self.assertEqual(record["status"], "failed")
        self.assertIsNone(record["worktree"])
        self.assertEqual(self.calls, [])
        self.assert_no_worktree()


class TestClean(UnitRunCase):
    """``units clean``: worktrees left behind, and runs that are gone."""

    def running(self, **fields: Any) -> Dict[str, Any]:
        return {"status": "running", "in_place": False, "job": None, "started_at": ws.utcnow(), **fields}

    def test_run_unit_records_the_foreground_process(self):
        self.approve(PAIR)
        seen: Dict[str, Any] = {}
        self.during = lambda cwd: seen.update(self.record("a"))
        self.assertEqual(self.run_unit("a")[0], 0)
        self.assertEqual(seen["pid"], os.getpid())
        self.assertIsNone(seen["job"])

    def test_units_clean_fails_a_foreground_run_that_is_gone(self):
        self.approve(PAIR)
        self.set_records({"a": self.running(in_place=True, pid=4242, pid_started="t0")})
        with mock.patch.object(execution_tree, "pid_alive", return_value=False):
            code, _, err = run_cli("units", "clean")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.record("a")["status"], "failed")
        # Nothing runs in place any more, so the other unit may start.
        self.assertEqual(self.run_unit("b")[0], 0)

    def test_units_clean_keeps_a_live_foreground_run(self):
        self.approve(PAIR)
        me = os.getpid()
        self.set_records({"a": self.running(pid=me, pid_started=execution_tree.process_started(me))})
        code, _, err = run_cli("units", "clean")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.record("a")["status"], "running")

    def test_units_clean_fails_a_record_with_neither_job_nor_pid_after_the_grace(self):
        self.approve(PAIR)
        self.set_records({"a": self.running(started_at="2020-01-01T00:00:00Z")})
        self.assertEqual(run_cli("units", "clean")[0], 0)
        self.assertEqual(self.record("a")["status"], "failed")

    def test_units_clean_keeps_a_record_with_neither_job_nor_pid_in_the_grace(self):
        self.approve(PAIR)
        for pid in (None, True, 0):
            with self.subTest(pid=pid):
                self.set_records({"a": self.running(pid=pid)})
                self.assertEqual(run_cli("units", "clean")[0], 0)
                self.assertEqual(self.record("a")["status"], "running")

    def test_units_clean_fails_a_foreground_run_whose_pid_was_reused(self):
        self.approve(PAIR)
        self.set_records({"a": self.running(pid=os.getpid(), pid_started="t0")})
        with (
            mock.patch.object(execution_tree, "pid_alive", return_value=True),
            mock.patch.object(execution_tree, "process_started", return_value="t1"),
        ):
            self.assertEqual(run_cli("units", "clean")[0], 0)
        self.assertEqual(self.record("a")["status"], "failed")

    def test_units_clean_fails_a_unit_whose_job_is_gone(self):
        self.approve(PAIR)
        jobs_mod.write_job(self.workspace, {"id": "implementer-gone", "status": "abandoned"})
        self.set_records({"a": self.running(job="implementer-gone")})
        code, _, err = run_cli("units", "clean")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.record("a")["status"], "failed")

    def test_units_clean_keeps_a_running_unit_worktree(self):
        self.approve(PAIR)
        me = os.getpid()
        job = {"id": "implementer-live", "status": "running", "started_at": ws.utcnow()}
        jobs_mod.write_job(
            self.workspace, {**job, "pid": me, "pid_started": execution_tree.process_started(me)}
        )
        tree = os.path.join(self.units_parent, "deadbeef-a")
        kept = os.path.join(tree, "a.py")
        ws.write_text(kept, "A = 1\n")
        self.set_records({"a": self.running(job="implementer-live", worktree=tree)})
        code, out, err = run_cli("units", "clean")
        self.assertEqual(code, 0, err)
        self.assertIn("a is running; its worktree stays", out)
        self.assertEqual(read_file(kept), "A = 1\n")
        self.assertEqual(self.record("a")["status"], "running")

    def test_units_clean_never_removes_what_is_not_a_unit_worktree(self):
        self.approve(PAIR)
        victims = [
            os.path.join(self.tmp, "victim"),
            os.path.join(self.units_parent, "deadbeef-b"),
            self.project,
        ]
        for victim in victims[:2]:
            ws.write_text(os.path.join(victim, "keep.txt"), "kept\n")
        for victim in victims:
            with self.subTest(victim=victim):
                self.set_records({"a": {"status": "failed", "in_place": False, "worktree": victim}})
                code, _, err = run_cli("units", "clean")
                self.assertEqual(code, 1)
                self.assertIn("could not remove a's worktree", err)
                self.assertEqual(self.record("a")["worktree"], victim)
        for victim in victims[:2]:
            self.assertEqual(read_file(os.path.join(victim, "keep.txt")), "kept\n")
        self.assertEqual(read_file(os.path.join(self.project, "app.py")), "print('hello')\n")


if __name__ == "__main__":
    unittest.main()
