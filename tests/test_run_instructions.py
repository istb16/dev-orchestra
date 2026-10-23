"""``instructions.<role>`` and ``implementation.unit_tests`` in a run's prompt."""

from __future__ import annotations

import argparse
import os
import subprocess
import types
import unittest
from typing import Any, List
from unittest import mock

from helpers import run_cli
from test_cli_run_units import CHAIN, PAIR, UnitRunCase
from test_units import plan

from orchestrator import cli_run, execution_tree
from orchestrator import config as config_mod
from orchestrator import jobs as jobs_mod
from orchestrator import units as units_mod
from orchestrator import worktrees as worktrees_mod

HEADING = "## Project instructions"
AFTER_MERGE = (
    "Do not run the tests: write the code and its tests, run lint and type checks only, "
    'and report the tests as "not run". This overrides the request\'s instruction to run '
    "tests; they run once on the merged tree."
)


def block(text: str) -> str:
    """The block a global inline ``text`` gives."""
    return (
        "## Project instructions\n\n"
        "Where a project instruction conflicts with any other rule in this prompt or with the "
        "request below, follow those instead.\n"
        "Sources: global inline.\n\n%s\n\n" % text
    )


def unit_header(name: str, checks_skipped: bool = False, unit_tests: str = "worktree") -> str:
    entry = next(entry for entry in PAIR if entry["name"] == name)
    unit = units_mod.Unit(name, "feature", list(entry["files"]), [])
    return units_mod.header(unit, plan(PAIR), checks_skipped, unit_tests)


class RunInstructionsCase(UnitRunCase):
    def setUp(self):
        super().setUp()
        for role in ("architect", "review_fixer"):
            run_cli("config", "set", "%s.provider" % role, "mock")

    def instruct(self, role: str, text: str) -> None:
        code, _, err = run_cli("config", "set", "instructions.%s" % role, text)
        self.assertEqual(code, 0, err)

    def after_merge(self) -> None:
        code, _, err = run_cli("config", "set", "implementation.unit_tests", "after_merge")
        self.assertEqual(code, 0, err)

    def last_prompt(self) -> str:
        return self.calls[-1]["prompt"]

    def set_global(self, block: str, value: Any) -> None:
        """``block`` set to ``value`` in the global file, for a value ``config set`` cannot write."""
        data = config_mod.load(self.project).data
        data[block] = value if block == "instructions" else {**(data.get(block) or {}), **value}
        config_mod.write_config_file(config_mod.global_config_path(), data)

    def prefix(self, role: str) -> str:
        """``_instruction_prefix`` of a run of ``role``: a role, or a reviewer's id."""
        seat = cli_run._Seat(role, None, None, {}, "default", "mock", role, role)
        loaded = config_mod.load(self.project)
        return cli_run._instruction_prefix(argparse.Namespace(job_file=None), loaded, seat)


class TestPrompts(RunInstructionsCase):
    def test_non_unit_prefix(self):
        self.approve(PAIR)
        for role in ("implementer", "architect", "review_fixer"):
            with self.subTest(role=role):
                self.instruct(role, "Use tabs for %s." % role)
                code, _, err = run_cli("run", role, "--prompt", "go")
                self.assertEqual(code, 0, err)
                self.assertEqual(self.last_prompt(), block("Use tabs for %s." % role) + "go")

    def test_unit_order(self):
        self.approve(PAIR)
        self.instruct("implementer", "Use tabs.")
        code, _, err = self.run_unit("a")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.last_prompt(), unit_header("a") + block("Use tabs.") + "go")

    def test_none_byte_identical(self):
        self.approve(PAIR)
        code, _, err = run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.last_prompt(), "go")
        code, _, err = self.run_unit("a")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.last_prompt(), unit_header("a") + "go")
        self.assertNotIn(HEADING, self.last_prompt())
        self.assertNotIn("not copied into its worktree", err)

    def test_after_merge_header(self):
        self.after_merge()
        self.approve(PAIR)
        code, _, err = self.run_unit("a")
        self.assertEqual(code, 0, err)
        prompt = self.last_prompt()
        self.assertEqual(prompt, unit_header("a", unit_tests="after_merge") + "go")
        self.assertIn(AFTER_MERGE, prompt)
        self.assertNotIn("Run your checks before you finish.", prompt)

    def test_after_merge_no_effect_without_unit(self):
        self.after_merge()
        self.approve(PAIR)
        code, _, err = run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.last_prompt(), "go")

    def test_after_merge_in_place_unit(self):
        """A unit of a serial chain runs in place, and is told the same."""
        self.after_merge()
        self.approve(CHAIN)
        code, _, err = self.run_unit("a")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls[-1]["cwd"], self.root)
        self.assertTrue(self.record("a")["in_place"])
        self.assertIn(AFTER_MERGE, self.last_prompt())
        self.assertNotIn("Run your checks before you finish.", self.last_prompt())

    def test_unreadable_file_noted_and_run_goes_on(self):
        self.set_global("instructions", {"implementer": {"file": "docs/missing.md"}})
        self.approve(PAIR)
        code, _, err = run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 0, err)
        self.assertIn("note: instructions.implementer: docs/missing.md: not found; running without it", err)
        self.assertEqual(self.last_prompt(), "go")

    def test_orchestrator_and_reviewer_runs_get_none(self):
        self.set_global(
            "instructions",
            {role: "Text for %s." % role for role in ("orchestrator", "implementer", "reviewers")},
        )
        self.assertEqual(self.prefix("orchestrator"), "")
        self.assertEqual(self.prefix("claude-general"), "")
        self.assertEqual(self.prefix("implementer"), block("Text for implementer."))

    def test_worktree_file_skip_noted(self):
        self.set_global("implementation", {"worktree_files": [".env.test"]})
        self.approve(PAIR)
        code, _, err = self.run_unit("a")
        self.assertEqual(code, 0, err)
        self.assertIn("note: a: .env.test not copied into its worktree (not in the main tree)", err)
        self.assertEqual(
            self.record("a")["copies_skipped"], [{"path": ".env.test", "reason": "not in the main tree"}]
        )

    def test_checks_skipped_wins(self):
        self.after_merge()
        self.write(".venv/lib/kept.txt", "x\n")
        self.approve(PAIR)
        with mock.patch.object(worktrees_mod, "_link", side_effect=OSError("no junctions here")):
            code, _, err = self.run_unit("a")
        self.assertEqual(code, 0, err)
        prompt = self.last_prompt()
        self.assertIn("Do not run checks; they run after the merge.", prompt)
        self.assertNotIn(AFTER_MERGE, prompt)


class TestDetachedWorker(RunInstructionsCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(execution_tree, "end_children_on_sigterm"))
        #: Each worker's argv, as `jobs.start` would have spawned it.
        self.workers: List[List[str]] = []

        def popen(command: List[str], *args: Any, **kwargs: Any) -> Any:
            self.workers.append(list(command[2:]))
            return types.SimpleNamespace(pid=os.getpid())

        fake = types.SimpleNamespace(Popen=popen, DEVNULL=subprocess.DEVNULL)
        self.enterContext(mock.patch.object(jobs_mod, "subprocess", fake))

    def test_worker_not_doubled(self):
        self.approve(PAIR)
        self.instruct("implementer", "Use tabs.")
        code, _, err = self.run_unit("a", "--detach", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.workers), 1)
        code, _, err = run_cli(*self.workers[0])
        self.assertEqual(code, 0, err)
        prompt = self.last_prompt()
        self.assertEqual(prompt.count(HEADING), 1)
        self.assertEqual(prompt, unit_header("a") + block("Use tabs.") + "go")


if __name__ == "__main__":
    unittest.main()
