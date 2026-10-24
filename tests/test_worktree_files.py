"""``implementation.worktree_files``: copied into a unit's worktree, each skip told."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import unittest
from typing import Any, Dict, Sequence
from unittest import mock

from helpers import IsolatedCase, has_git, is_link, make_dir_link, read_file, remove_link

from orchestrator import workspace as ws
from orchestrator import worktrees


@unittest.skipUnless(has_git(), "needs git")
class TestWorktreeFiles(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write(".gitignore", ".env\nlocal/\nlinked\n")
        self.write("app.py", "print('hello')\n")
        self.commit_all("base")
        self.workspace = self.cli_workspace()

    def create(self, checked: Sequence[str], copies: Sequence[str] = ()) -> Dict[str, Any]:
        record = worktrees.create(self.workspace, "a", copies, checked)
        self.addCleanup(worktrees.remove, self.project, record, "a")
        return record

    def test_worktree_files_copies_an_ignored_file(self):
        self.write(".env", "TOKEN=x\n")
        self.write("local/config.json", "{}\n")
        record = self.create([".env", "local/config.json"])
        self.assertEqual(read_file(os.path.join(record["worktree"], ".env")), "TOKEN=x\n")
        self.assertEqual(read_file(os.path.join(record["worktree"], "local", "config.json")), "{}\n")
        self.assertNotIn("copies_skipped", record)

    def test_worktree_files_skips_a_path_outside_the_repository(self):
        outside = os.path.join(self.tmp, "outside.txt")
        ws.write_text(outside, "secret\n")
        self.write(".env", "TOKEN=x\n")
        escapes = [
            os.path.join("..", "outside.txt"),
            outside,
            # On Windows "/x" is not absolute, yet joins to the drive's root.
            "/outside.txt",
            "\\outside.txt",
            # A `..` is refused anywhere: after a link it would leave the tree.
            "local/../.env",
            "linked/../../outside.txt",
        ]
        record = self.create(escapes)
        self.assertEqual(
            record["copies_skipped"], [{"path": path, "reason": "outside the repository"} for path in escapes]
        )
        self.assertFalse(os.path.lexists(os.path.join(record["worktree"], ".env")))

    def test_worktree_files_copies_some_and_skips_others(self):
        self.write(".env", "TOKEN=x\n")
        self.write("local/config.json", "{}\n")
        record = self.create([".env", "local/missing.json", "local/config.json"])
        self.assertEqual(
            record["copies_skipped"], [{"path": "local/missing.json", "reason": "not in the main tree"}]
        )
        self.assertEqual(read_file(os.path.join(record["worktree"], ".env")), "TOKEN=x\n")
        self.assertEqual(read_file(os.path.join(record["worktree"], "local", "config.json")), "{}\n")

    def test_worktree_files_leaves_a_file_the_worktree_has(self):
        """A tracked file is already in the worktree: it is never copied over."""
        with mock.patch.object(worktrees.shutil, "copy2", wraps=shutil.copy2) as copy:
            record = self.create(["app.py"])
        self.assertNotIn("copies_skipped", record)
        targets = [os.path.normcase(str(call.args[1])) for call in copy.call_args_list]
        self.assertNotIn(os.path.normcase(os.path.join(record["worktree"], "app.py")), targets)

    @unittest.skipIf(os.name == "nt", "file modes are POSIX'")
    def test_worktree_files_keeps_the_mode(self):
        self.write(".env", "#!/bin/sh\n")
        os.chmod(os.path.join(self.project, ".env"), 0o750)
        record = self.create([".env"])
        mode = stat.S_IMODE(os.stat(os.path.join(record["worktree"], ".env")).st_mode)
        self.assertEqual(mode, 0o750)

    def test_worktree_files_skips_a_path_through_a_link(self):
        target = os.path.join(self.tmp, "elsewhere")
        ws.write_text(os.path.join(target, "secret.txt"), "secret\n")
        link = os.path.join(self.project, "linked")
        try:
            make_dir_link(link, target)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("cannot make a link here")
        self.addCleanup(lambda: is_link(link) and remove_link(link))
        record = self.create(["linked/secret.txt"])
        self.assertEqual(record["copies_skipped"], [{"path": "linked/secret.txt", "reason": "a link"}])
        self.assertFalse(os.path.lexists(os.path.join(record["worktree"], "linked")))

    def test_worktree_files_skips_a_missing_file(self):
        record = self.create([".env"])
        self.assertEqual(record["copies_skipped"], [{"path": ".env", "reason": "not in the main tree"}])
        self.assertFalse(os.path.lexists(os.path.join(record["worktree"], ".env")))

    def test_a_provider_copy_through_a_link_is_skipped_and_told(self):
        """``copies`` go through the same check; a link is recorded, as a ``worktree_files`` one is."""
        target = os.path.join(self.tmp, "elsewhere")
        ws.write_text(os.path.join(target, "secret.txt"), "secret\n")
        link = os.path.join(self.project, "linked")
        try:
            make_dir_link(link, target)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("cannot make a link here")
        self.addCleanup(lambda: is_link(link) and remove_link(link))
        record = self.create([], copies=["linked/secret.txt"])
        self.assertEqual(record["copies_skipped"], [{"path": "linked/secret.txt", "reason": "a link"}])
        self.assertFalse(os.path.lexists(os.path.join(record["worktree"], "linked")))

    def test_a_provider_copy_through_a_link_in_the_worktree_is_skipped(self):
        """A step that is a link in the worktree alone: nothing is written through it."""
        self.write(".gitignore", ".env\nlocal/\nlinked\nnode_modules/\n")
        self.write("node_modules/pkg/settings.json", "{}\n")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        real_link = worktrees._link
        # The worktree's node_modules is linked somewhere other than the user's.
        with mock.patch.object(
            worktrees, "_link", side_effect=lambda target, link: real_link(elsewhere, link)
        ):
            record = self.create([], copies=["node_modules/pkg/settings.json"])
        if record["links"] != ["node_modules"]:
            self.skipTest("cannot make a link here")
        self.assertEqual(
            record["copies_skipped"], [{"path": "node_modules/pkg/settings.json", "reason": "a link"}]
        )
        self.assertEqual(os.listdir(elsewhere), [])

    def test_a_provider_copy_is_copied_and_its_usual_skips_are_not_told(self):
        """One the user lacks, or one outside, is silent; a directory is told, and not copied."""
        self.write("local/config.json", "{}\n")
        self.write("local/other.json", "{}\n")
        record = self.create(
            [], copies=["local/config.json", "local/missing.json", "local", "../outside.txt"]
        )
        self.assertEqual(record["copies_skipped"], [{"path": "local", "reason": "not a file"}])
        self.assertEqual(read_file(os.path.join(record["worktree"], "local", "config.json")), "{}\n")
        self.assertEqual(os.listdir(os.path.join(record["worktree"], "local")), ["config.json"])
        self.assertFalse(os.path.lexists(os.path.join(os.path.dirname(record["worktree"]), "outside.txt")))
        self.assertFalse(os.path.lexists(os.path.join(os.path.dirname(self.project), "outside.txt")))

    def test_a_plan_that_is_not_a_file_is_told(self):
        """The plan and the decisions are copied by the CLI: a skip of either is not silent."""
        os.makedirs(self.workspace.plan_path)
        record = self.create([])
        relative = os.path.relpath(self.workspace.plan_path, self.workspace.root)
        self.assertEqual(record["copies_skipped"], [{"path": relative, "reason": "not a file"}])

    def test_worktree_files_skips_a_directory(self):
        self.write("local/config.json", "{}\n")
        record = self.create(["local"])
        self.assertEqual(record["copies_skipped"], [{"path": "local", "reason": "not a file"}])
        self.assertFalse(os.path.lexists(os.path.join(record["worktree"], "local")))


class TestCheckedSkip(IsolatedCase):
    """``_check_copy`` on two plain trees, the way ``create`` asks it."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "main")
        self.tree = os.path.join(self.tmp, "unit")
        ws.write_text(os.path.join(self.root, "local", "config.json"), "{}\n")
        os.makedirs(self.tree)

    def link(self, link: str, target: str) -> None:
        try:
            make_dir_link(link, target)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("cannot make a link here")
        self.addCleanup(lambda: is_link(link) and remove_link(link))

    def test_a_link_on_the_worktree_side_is_skipped(self):
        """The copy would be written through it, outside the worktree."""
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        self.link(os.path.join(self.tree, "local"), elsewhere)
        self.assertEqual(worktrees._check_copy(self.root, self.tree, "local/config.json").reason, "a link")

    def test_a_plain_path_can_be_copied(self):
        source = os.path.join(self.root, "local", "config.json")
        target = os.path.join(self.tree, "local", "config.json")
        for relative in ("local/config.json", "local\\config.json"):
            with self.subTest(relative=relative):
                self.assertEqual(
                    worktrees._check_copy(self.root, self.tree, relative),
                    worktrees._Checked("", source, target),
                )

    def test_a_skip_has_no_paths(self):
        self.assertEqual(
            worktrees._check_copy(self.root, self.tree, "../outside.txt"),
            worktrees._Checked("outside the repository", "", ""),
        )

    def test_linked_on_path_takes_checked_parts(self):
        """The path is split once, and both sides walk the same checked parts."""
        with (
            mock.patch.object(ws, "repository_parts", wraps=ws.repository_parts) as split,
            mock.patch.object(ws, "linked_on_path", wraps=ws.linked_on_path) as walked,
        ):
            self.assertEqual(worktrees._check_copy(self.root, self.tree, "local\\config.json").reason, "")
        split.assert_called_once_with("local\\config.json")
        self.assertEqual(
            walked.call_args_list,
            [mock.call(self.root, ["local", "config.json"]), mock.call(self.tree, ["local", "config.json"])],
        )

    def test_linked_on_path_refuses_a_string(self):
        with self.assertRaises(TypeError):
            ws.linked_on_path(self.root, "local/config.json")

    def test_a_resolved_path_outside_is_skipped(self):
        """Should a step become a link after the walk, the resolved check still refuses, on either side."""
        for side in (self.root, self.tree):
            with (
                self.subTest(side=side),
                mock.patch.object(ws, "linked_on_path", return_value=False),
                mock.patch.object(
                    worktrees, "_within", side_effect=lambda base, path, side=side: base != side
                ),
            ):
                self.assertEqual(
                    worktrees._check_copy(self.root, self.tree, "local/config.json").reason, "a link"
                )


if __name__ == "__main__":
    unittest.main()
