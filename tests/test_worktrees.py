"""The worktrees work units run in: made from the user's tree, removed without following a link."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import types
import unittest
from typing import Any, Dict, Sequence
from unittest import mock

from helpers import IsolatedCase, has_git, is_link, make_dir_link, read_file, remove_link

from orchestrator import units as units_mod
from orchestrator import workspace as ws
from orchestrator import worktrees

#: What the CLI hands ``create`` for Claude and a project file (``worktrees.copies_for``).
COPIES = (".claude/settings.local.json", ".dev-orchestra.yaml")


class TestCopies(IsolatedCase):
    def test_worktrees_copies_for_adds_the_project_file(self):
        root = os.path.join(self.tmp, "project")
        project_path = os.path.join(root, ".dev-orchestra.yaml")
        files = (".claude/settings.local.json",)
        self.assertEqual(worktrees.copies_for(root, files, project_path), list(COPIES))
        self.assertEqual(worktrees.copies_for(root, files, None), [".claude/settings.local.json"])
        self.assertEqual(worktrees.copies_for(root, (), None), [])

    def test_worktrees_copies_for_takes_a_bare_string_as_one_file(self):
        """A user adapter's ``worktree_files = "..."``, not split into characters."""
        copies = worktrees.copies_for(self.tmp, ".claude/settings.local.json", None)
        self.assertEqual(copies, [".claude/settings.local.json"])

    def test_worktrees_copy_paths_inside_the_repository(self):
        for relative, inside in (
            ("..dotfile", True),
            ("notes/..hidden", True),
            ("a/../b.txt", True),
            ("..", False),
            (os.path.join("..", "x"), False),
            ("a/../../x", False),
            (os.path.abspath("x"), False),
        ):
            with self.subTest(relative=relative):
                self.assertIs(worktrees._in_repository(relative), inside)

    @unittest.skipUnless(os.name == "nt", "drives are Windows'")
    def test_worktrees_copy_paths_with_a_drive_are_outside(self):
        for relative in ("C:..\\secret.txt", "C:foo", "c:notes\\a.txt"):
            with self.subTest(relative=relative):
                self.assertFalse(worktrees._in_repository(relative))


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
        self.assertTrue(ws.is_link(link))
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

    def create(self, name: str = "a", copies: Sequence[str] = COPIES) -> Dict[str, Any]:
        record = worktrees.create(self.workspace, name, copies)
        self.addCleanup(worktrees.remove, self.project, record, name)
        return record


class TestRemoval(WorktreeCase):
    def test_worktrees_remove_does_not_follow_link(self):
        kept = self.write(".venv/lib/kept.txt", "the user's environment\n")
        record = self.create()
        link = os.path.join(record["worktree"], ".venv")
        self.assertEqual(record["links"], [".venv"])
        self.assertTrue(is_link(link))
        self.assertTrue(os.path.isfile(os.path.join(link, "lib", "kept.txt")))
        self.assertTrue(worktrees.remove(self.project, record, "a"))
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
            self.assertTrue(worktrees.remove(self.project, record, "a"))
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
                self.assertFalse(worktrees.remove(self.project, {"worktree": path, "links": []}, "b"))
        for path in (stray, outside):
            self.assertEqual(read_file(os.path.join(path, "keep.txt")), "kept\n")
        self.assertEqual(read_file(os.path.join(self.project, "app.py")), "print('hello')\n")
        self.assertTrue(os.path.isdir(record["worktree"]))

    def test_worktrees_remove_takes_only_the_named_unit_s_own_worktree(self):
        """A record names a path and a commit: neither reaches another unit's worktree or a stray one."""
        first, second = self.create("a"), self.create("b")
        units_dir = os.path.dirname(first["worktree"])
        # Named after a's commit, as the old check by prefix let through.
        stray = os.path.join(units_dir, "%s-zzz" % first["base_commit"][:8])
        ws.write_text(os.path.join(stray, "keep.txt"), "kept\n")
        for record, name in (
            (dict(first, worktree=second["worktree"]), "a"),
            (second, "a"),
            (dict(first, worktree=stray), "a"),
            (dict(first, worktree=stray), "zzz"),
            (dict(first, base_commit=first["base_commit"][:8]), "a"),
        ):
            with self.subTest(worktree=os.path.basename(record["worktree"]), name=name):
                self.assertFalse(worktrees.remove(self.project, record, name))
        self.assertEqual(read_file(os.path.join(stray, "keep.txt")), "kept\n")
        for record in (first, second):
            self.assertEqual(read_file(os.path.join(record["worktree"], "app.py")), "print('hello')\n")
        self.assertEqual(self.git("worktree", "list", "--porcelain").stdout.count("worktree "), 3)
        self.assertTrue(worktrees.remove(self.project, second, "b"))
        self.assertTrue(os.path.isdir(first["worktree"]))

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
                self.assertFalse(worktrees.remove(self.project, evil, "evil"))
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
        self.assertTrue(worktrees.remove(self.project, {"worktree": gone, "links": [escape]}, "a"))
        self.assertTrue(worktrees.remove(self.project, dict(record, links=[escape]), "a"))
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
        self.assertFalse(worktrees.remove(self.project, dict(record, base_commit="0" * 40), "a"))
        self.assertFalse(worktrees.remove(self.project, record, "b"))
        self.assertTrue(os.path.isdir(tree))
        with mock.patch.object(worktrees.time, "sleep"):
            self.assertTrue(worktrees.remove(self.project, record, "a"))
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

    def test_worktrees_copies_only_what_it_is_given(self):
        self.write(".claude/settings.local.json", '{"permissions": {}}\n')
        record = self.create(copies=())
        self.assertFalse(os.path.lexists(os.path.join(record["worktree"], ".claude", "settings.local.json")))

    def test_worktrees_skips_copies_outside_the_repository(self):
        outside = os.path.join(self.tmp, "outside.txt")
        ws.write_text(outside, "secret\n")
        self.write("notes/kept.txt", "kept\n")
        # Ignored, so only the copy brings it into the worktree.
        self.write(".gitignore", ".venv/\n.claude/settings.local.json\n.dev-orchestra.yaml\n..dotfile\n")
        self.write("..dotfile", "dots\n")
        escapes = [outside, os.path.join("..", "outside.txt"), "notes/../../outside.txt"]
        drive = os.path.splitdrive(self.project)[0]
        if drive:
            # Drive-relative: joined to the root, it climbs out of it.
            escapes.append(drive + os.path.join("..", "outside.txt"))
        record = self.create(copies=[*escapes, "notes/kept.txt", "..dotfile"])
        tree = record["worktree"]
        self.assertEqual(read_file(os.path.join(tree, "notes", "kept.txt")), "kept\n")
        self.assertEqual(read_file(os.path.join(tree, "..dotfile")), "dots\n")
        self.assertFalse(os.path.lexists(os.path.join(os.path.dirname(tree), "outside.txt")))
        self.assertEqual(read_file(outside), "secret\n")

    def test_worktrees_refuses_a_linked_units_directory(self):
        real = ws.is_link

        def linked(path: str) -> bool:
            return os.path.basename(path) == "project.units" or real(path)

        with mock.patch.object(ws, "is_link", linked):
            with self.assertRaises(worktrees.WorktreeError):
                worktrees.create(self.workspace, "a", COPIES)
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
                worktrees.create(self.workspace, "b", COPIES)

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
