"""The worktrees work units run in: made from the user's tree, removed without following a link."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import types
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
        self.assertTrue(worktrees.is_link(link))
        worktrees._safe_rmtree(tree)
        self.assertFalse(os.path.lexists(tree))
        self.assertEqual(read_file(kept), "the user's\n")


class TestLink(IsolatedCase):
    def test_worktrees_link_never_goes_through_cmd(self):
        """A path with ``&`` in it would be a second command to ``cmd /c mklink``."""

        def refused(target: str, link: str) -> None:
            raise OSError("no junction")

        fake = types.SimpleNamespace(CreateJunction=refused)
        with (
            mock.patch.object(worktrees, "_IS_WINDOWS", True),
            mock.patch.dict(sys.modules, {"_winapi": fake}),
            mock.patch.object(subprocess, "run") as ran,
        ):
            with self.assertRaises(OSError):
                worktrees._link(os.path.join(self.tmp, "a&b"), os.path.join(self.tmp, "link&c"))
        ran.assert_not_called()


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

    def test_worktrees_remove_refuses_a_path_that_is_not_a_unit_worktree(self):
        record = self.create()
        units_dir = os.path.dirname(record["worktree"])
        stray = os.path.join(units_dir, "deadbeef-b")
        outside = os.path.join(self.tmp, "elsewhere")
        for path in (stray, outside):
            ws.write_text(os.path.join(path, "keep.txt"), "kept\n")
        for path in (stray, outside, self.project, units_dir):
            with self.subTest(path=path):
                self.assertFalse(worktrees.remove(self.project, {"worktree": path, "links": []}))
        for path in (stray, outside):
            self.assertEqual(read_file(os.path.join(path, "keep.txt")), "kept\n")
        self.assertEqual(read_file(os.path.join(self.project, "app.py")), "print('hello')\n")
        self.assertTrue(os.path.isdir(record["worktree"]))

    def link(self, link: str, target: str) -> None:
        try:
            make_dir_link(link, target)
        except (OSError, subprocess.CalledProcessError, NotImplementedError) as exc:
            self.skipTest("cannot make a link here: %s" % exc)
        self.addCleanup(lambda: is_link(link) and remove_link(link))

    def test_worktrees_remove_refuses_a_link_to_the_project_or_another_worktree(self):
        record = self.create()
        units_dir = os.path.dirname(record["worktree"])
        kept = os.path.join(record["worktree"], "app.py")
        for target in (self.project, record["worktree"]):
            with self.subTest(target=target):
                link = os.path.join(units_dir, "%s-evil" % record["base_commit"][:8])
                self.link(link, target)
                evil = {"worktree": link, "base_commit": record["base_commit"], "links": []}
                self.assertFalse(worktrees.remove(self.project, evil))
                remove_link(link)
        self.assertEqual(read_file(kept), "print('hello')\n")
        self.assertEqual(read_file(os.path.join(self.project, "app.py")), "print('hello')\n")
        self.assertEqual(self.git("worktree", "list", "--porcelain").stdout.count("worktree "), 2)

    def test_worktrees_remove_unlinks_only_the_linked_names(self):
        record = self.create()
        target = os.path.join(self.tmp, "victim-target")
        ws.write_text(os.path.join(target, "keep.txt"), "kept\n")
        victim = os.path.join(self.tmp, "victim")
        self.link(victim, target)
        # From the worktree, ../.. is the temporary directory the victim is in.
        escape = os.path.join("..", "..", "victim")
        gone = os.path.join(os.path.dirname(record["worktree"]), "gone")
        self.assertTrue(worktrees.remove(self.project, {"worktree": gone, "links": [escape]}))
        self.assertTrue(worktrees.remove(self.project, dict(record, links=[escape])))
        self.assertFalse(os.path.lexists(record["worktree"]))
        self.assertTrue(is_link(victim))
        self.assertEqual(read_file(os.path.join(victim, "keep.txt")), "kept\n")

    def test_worktrees_remove_finishes_a_removal_git_no_longer_lists(self):
        record = self.create()
        tree = record["worktree"]
        # As a removal that something held a file through leaves it: no .git, unlisted.
        os.unlink(os.path.join(tree, ".git"))
        self.git("worktree", "prune")
        self.assertEqual(self.git("worktree", "list", "--porcelain").stdout.count("worktree "), 1)
        self.assertTrue(os.path.isdir(tree))
        self.assertFalse(worktrees.remove(self.project, dict(record, base_commit="0" * 40)))
        self.assertTrue(os.path.isdir(tree))
        with mock.patch.object(worktrees.time, "sleep"):
            self.assertTrue(worktrees.remove(self.project, record))
        self.assertFalse(os.path.lexists(tree))


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

    def test_worktrees_refuses_a_linked_units_directory(self):
        real = worktrees.is_link

        def linked(path: str) -> bool:
            return os.path.basename(path) == "project.units" or real(path)

        with mock.patch.object(worktrees, "is_link", linked):
            with self.assertRaises(worktrees.WorktreeError):
                worktrees.create(self.workspace, "a", config_mod.load(self.project))
        self.assertEqual(self.git("worktree", "list", "--porcelain").stdout.count("worktree "), 1)

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX owners and modes")
    def test_worktrees_units_directory_is_the_user_s_alone(self):
        self.write(".claude/settings.local.json", '{"env": {"TOKEN": "x"}}\n')
        settings = os.path.join(self.project, ".claude", "settings.local.json")
        os.chmod(settings, 0o600)
        record = self.create()
        units_dir = os.path.dirname(record["worktree"])
        self.assertEqual(stat.S_IMODE(os.stat(units_dir).st_mode) & 0o077, 0)
        copy = os.path.join(record["worktree"], ".claude", "settings.local.json")
        self.assertEqual(stat.S_IMODE(os.stat(copy).st_mode), 0o600)
        mine = os.stat(units_dir).st_uid
        with mock.patch.object(worktrees.os, "getuid", lambda: mine + 1):
            with self.assertRaises(worktrees.WorktreeError):
                worktrees.create(self.workspace, "b", config_mod.load(self.project))

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX owners and modes")
    def test_worktrees_units_directory_already_open_to_others_is_closed(self):
        units_dir = os.path.join(self.tmp, "project.units")
        os.makedirs(units_dir)
        os.chmod(units_dir, 0o755)
        self.create()
        self.assertEqual(stat.S_IMODE(os.stat(units_dir).st_mode) & 0o077, 0)

    def test_worktrees_location_is_beside_the_repository(self):
        record = self.create("parser")
        parent, name = os.path.split(record["worktree"])
        self.assertEqual(parent, os.path.join(self.tmp, "project.units"))
        self.assertEqual(name, "%s-parser" % record["base_commit"][:8])


if __name__ == "__main__":
    unittest.main()
