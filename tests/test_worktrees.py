"""The worktrees work units run in: made from the user's tree, removed without following a link."""

from __future__ import annotations

import os
import unittest
from typing import Any, Dict
from unittest import mock

from helpers import IsolatedCase, has_git, is_link, make_dir_link, read_file, remove_link

from orchestrator import config as config_mod
from orchestrator import units as units_mod
from orchestrator import workspace as ws
from orchestrator import worktrees


class TestSafeWalker(IsolatedCase):
    def test_worktrees_safe_walker_refuses_reparse_point(self):
        target = os.path.join(self.tmp, "target")
        os.makedirs(target)
        kept = os.path.join(target, "kept.txt")
        ws.write_text(kept, "the user's\n")
        tree = os.path.join(self.tmp, "leftover")
        ws.write_text(os.path.join(tree, "sub", "file.txt"), "x\n")
        link = os.path.join(tree, "sub", "env")
        make_dir_link(link, target)
        self.addCleanup(lambda: is_link(link) and remove_link(link))
        self.assertTrue(worktrees._is_link(link))
        worktrees._safe_rmtree(tree)
        self.assertFalse(os.path.lexists(tree))
        self.assertEqual(read_file(kept), "the user's\n")


@unittest.skipUnless(has_git(), "needs git")
class WorktreeCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write(".gitignore", ".venv/\n.claude/settings.local.json\n.dev-orchestra.yaml\n")
        self.write("app.py", "print('hello')\n")
        self.commit_all("base")
        self.workspace = self.cli_workspace()

    def create(self, name: str = "a") -> Dict[str, Any]:
        record = worktrees.create(self.workspace, name, config_mod.load(self.project))
        self.addCleanup(worktrees.remove, self.project, record)
        return record


class TestRemoval(WorktreeCase):
    def test_worktrees_remove_does_not_follow_link(self):
        kept = self.write(".venv/lib/kept.txt", "the user's environment\n")
        record = self.create()
        link = os.path.join(record["worktree"], ".venv")
        self.assertEqual(record["links"], [".venv"])
        self.assertTrue(is_link(link))
        self.assertTrue(os.path.isfile(os.path.join(link, "lib", "kept.txt")))
        self.assertTrue(worktrees.remove(self.project, record))
        self.assertFalse(os.path.lexists(record["worktree"]))
        self.assertEqual(read_file(kept), "the user's environment\n")
        listed = self.git("worktree", "list", "--porcelain").stdout
        self.assertEqual(listed.count("worktree "), 1)

    def test_worktrees_remove_retries_locked(self):
        record = self.create()
        original = worktrees._git_remove
        calls = []

        def locked_twice(root, path):
            calls.append(path)
            if len(calls) <= 2:
                raise PermissionError("a file is in use")
            original(root, path)

        with (
            mock.patch.object(worktrees, "_git_remove", locked_twice),
            mock.patch.object(worktrees.time, "sleep") as slept,
        ):
            self.assertTrue(worktrees.remove(self.project, record))
        self.assertEqual(len(calls), 3)
        self.assertEqual([call.args[0] for call in slept.call_args_list], [0.2, 0.4])
        self.assertFalse(os.path.lexists(record["worktree"]))


class TestCreate(WorktreeCase):
    def test_worktrees_link_failure_skips_checks(self):
        self.write(".venv/lib/kept.txt", "x\n")
        with mock.patch.object(worktrees, "_link", side_effect=OSError("no junctions here")):
            record = self.create()
        self.assertTrue(record["checks_skipped"])
        self.assertEqual(record["links"], [])
        unit = units_mod.Unit("a", "feature", ["app.py"], [])
        header = units_mod.header(unit, "### Unit: a\n1. Do a.\n", record["checks_skipped"])
        self.assertIn("Do not run checks; they run after the merge.", header)
        self.assertNotIn("Run your checks", header)
        self.assertIn("Run your checks", units_mod.header(unit, "", False))

    def test_worktrees_copies_settings_local_and_config(self):
        self.write(".claude/settings.local.json", '{"permissions": {}}\n')
        self.write(".dev-orchestra.yaml", "version: 1\n")
        ws.write_text(self.workspace.plan_path, "# Plan\n")
        record = self.create()
        tree = record["worktree"]
        settings = os.path.join(tree, ".claude", "settings.local.json")
        self.assertEqual(read_file(settings), '{"permissions": {}}\n')
        self.assertEqual(read_file(os.path.join(tree, ".dev-orchestra.yaml")), "version: 1\n")
        plan = os.path.join(tree, os.path.relpath(self.workspace.plan_path, self.project))
        self.assertEqual(read_file(plan), "# Plan\n")

    def test_worktrees_location_is_beside_the_repository(self):
        record = self.create("parser")
        parent, name = os.path.split(record["worktree"])
        self.assertEqual(parent, os.path.join(self.tmp, "project.units"))
        self.assertEqual(name, "%s-parser" % record["base_commit"][:8])


if __name__ == "__main__":
    unittest.main()
