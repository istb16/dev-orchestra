"""``run implementer --unit``: a work unit, in a worktree of its own or in place."""

from __future__ import annotations

import json
import os
import unittest
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

from helpers import IsolatedCase, has_git, is_link, read_file, run_cli
from test_units import plan

from orchestrator import units as units_mod
from orchestrator import workspace as ws
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
        self.assertEqual(self.run_unit("a")[0], 0)
        # A rerun in the same pass spends an attempt, and there is none left.
        code, _, err = self.run_unit("a")
        self.assertEqual(code, 3, err)
        self.assert_no_worktree()
        self.assertEqual(self.record("a")["status"], "succeeded")
        # A piece of the open pass that has not run yet still runs.
        self.assertEqual(self.run_unit("b")[0], 0)
        self.assertEqual(self.used(), 1)
        # A new pass spends an attempt.
        self.approve(three, extra="\nRevised.\n")
        code, _, err = self.run_unit("a")
        self.assertEqual(code, 3, err)
        self.assert_no_worktree()
        self.assertEqual(self.record("a"), {})
        # A spent total_delegated_runs refuses all three kinds.
        run_cli("config", "set", "budgets.implementer", "10")
        self.assertEqual(self.run_unit("a")[0], 0)
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


if __name__ == "__main__":
    unittest.main()
