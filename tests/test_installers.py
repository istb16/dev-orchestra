"""The installers, run for real against temp directories.

Mostly the Antigravity mode, and the Claude mode's link handling and exclude
line. Nothing here needs Antigravity: an install is a link or a copy in a
`plugins/` folder, and that is what these tests look at. Every shell found on
PATH runs the same cases, one subtest each -- `sh` off Windows, and `pwsh` and
Windows PowerShell wherever they are.
"""

from __future__ import annotations

import codecs
import os
import re
import shutil
import stat
import subprocess
import unittest
from typing import List, NamedTuple, Optional, Tuple

from helpers import REPO_ROOT, IsolatedCase, is_link, make_dir_link, remove_link, remove_tree

SKILL_NAME = "dev-orchestra"
SENTINEL = ".dev-orchestra-install"
MARKER = "# added by dev-orchestra install --antigravity"
ENTRY = "/.agents/plugins/dev-orchestra"
HAND_REMOVAL = "Remove it by hand"

#: What a copy install carries, and so what a throwaway checkout needs.
PAYLOAD = (
    "plugin.json",
    "skills",
    ".claude-plugin",
    ".codex-plugin",
    "README.md",
    "LICENSE",
    "references",
    "scripts",
    "bin",
    "agents",
    "examples",
)


class Shell(NamedTuple):
    name: str
    command: List[str]
    suffix: str
    posix: bool


def _shells() -> List[Shell]:
    found: List[Shell] = []
    sh = shutil.which("sh")
    # On Windows `sh` is Git Bash, whose `ln -s` copies instead of linking.
    if sh and os.name != "nt":
        found.append(Shell("sh", [sh], ".sh", True))
    for name in ("pwsh", "powershell"):
        path = shutil.which(name)
        if path:
            command = [path, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
            found.append(Shell(name, command, ".ps1", False))
    return found


SHELLS = _shells()


def same_path(one: str, other: str) -> bool:
    return os.path.normcase(os.path.realpath(one)) == os.path.normcase(os.path.realpath(other))


def read_text(path: str) -> str:
    with open(path, encoding="utf-8-sig") as handle:
        return handle.read()


class _InstallerCase(IsolatedCase):
    def tearDown(self) -> None:
        # Links first, so that nothing removing the temp tree can follow one
        # into the checkout it points at.
        for dirpath, dirnames, _ in os.walk(self.tmp):
            for name in list(dirnames):
                path = os.path.join(dirpath, name)
                if is_link(path):
                    remove_link(path)
                    dirnames.remove(name)
        super().tearDown()

    def run_installer(
        self,
        shell: Shell,
        action: str,
        project: Optional[str] = None,
        copy: bool = False,
        root: str = REPO_ROOT,
        env: Optional[dict] = None,
        mode: str = "antigravity",
        cwd: Optional[str] = None,
    ) -> Tuple[int, str]:
        script = os.path.join(root, "install", action + shell.suffix)
        args: List[str] = []
        if mode == "antigravity":
            args.append("--antigravity" if shell.posix else "-Antigravity")
        if project is not None:
            args += ["--project" if shell.posix else "-Project", project]
        if copy:
            args.append("--copy" if shell.posix else "-Copy")
        if env is None:
            env = dict(os.environ, HOME=self.home)
        result = subprocess.run(
            [*shell.command, script, *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=300,
        )
        return result.returncode, result.stdout + result.stderr

    def setUp(self) -> None:
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)

    def fresh(self, shell: Shell, label: str) -> str:
        """A directory of its own for one subtest."""
        path = os.path.join(self.tmp, "%s-%s" % (shell.name, label))
        os.makedirs(path)
        return path

    def git_project(self, base: str, exclude: Optional[str] = None) -> str:
        """A project the installer takes for a repository: it only looks for `.git/`."""
        project = os.path.join(base, "proj")
        os.makedirs(os.path.join(project, ".git", "info"))
        if exclude is not None:
            with open(os.path.join(project, ".git", "info", "exclude"), "w", encoding="utf-8") as handle:
                handle.write(exclude)
        return project

    def make_checkout(self, base: str) -> str:
        """A throwaway checkout: the payload and the installers, nothing else."""
        checkout = os.path.join(base, "checkout")
        os.makedirs(checkout)
        for item in (*PAYLOAD, "install"):
            source = os.path.join(REPO_ROOT, item)
            target = os.path.join(checkout, item)
            if os.path.isdir(source):
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(source, target)
        return checkout

    def hand_removal_command(self, output: str) -> str:
        """The command the refusal prints for the user to paste."""
        lines = output.splitlines()
        for index, line in enumerate(lines):
            if HAND_REMOVAL in line and index + 1 < len(lines):
                return lines[index + 1].strip()
        self.fail(output)

    def write_file(self, path: str, content: str = "x\n") -> None:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def exclude_lines(self, project: str) -> List[str]:
        path = os.path.join(project, ".git", "info", "exclude")
        if not os.path.exists(path):
            return []
        return read_text(path).splitlines()

    def assert_linked(self, shell: Shell, dest: str, output: str) -> None:
        self.assertTrue(same_path(dest, REPO_ROOT), output)
        self.assertIn('"name": "dev-orchestra"', read_text(os.path.join(dest, "plugin.json")))
        match = re.search(r"Linked \((junction|symlink)\) ", output)
        self.assertIsNotNone(match, output)
        if os.name == "nt" and match is not None:
            self.assertEqual(match.group(1), "junction", output)
        self.assertIn("Restart Antigravity", output)

    def assert_refused(self, code: int, output: str) -> None:
        self.assertEqual(code, 1, output)
        self.assertIn(HAND_REMOVAL, output)


@unittest.skipUnless(SHELLS, "needs sh (off Windows), pwsh or powershell")
class TestAntigravityInstallers(_InstallerCase):
    def test_a_project_that_does_not_exist_yet_gets_a_link(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = os.path.join(self.fresh(shell, "new"), "not", "there", "yet")
                code, output = self.run_installer(shell, "install", project)
                self.assertEqual(code, 0, output)
                self.assert_linked(shell, os.path.join(project, ".agents", "plugins", SKILL_NAME), output)

    def test_the_exclude_entry_is_written_once_with_its_marker(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.git_project(self.fresh(shell, "twice"))
                for _ in range(2):
                    code, output = self.run_installer(shell, "install", project)
                    self.assertEqual(code, 0, output)
                lines = self.exclude_lines(project)
                self.assertEqual(lines.count(MARKER), 1, lines)
                self.assertEqual(lines.count(ENTRY), 1, lines)
                self.assertEqual(lines.index(MARKER) + 1, lines.index(ENTRY), lines)

    def test_uninstall_removes_the_link_and_the_marked_entry(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.git_project(self.fresh(shell, "remove"), exclude="/build\n")
                code, output = self.run_installer(shell, "install", project)
                self.assertEqual(code, 0, output)
                code, output = self.run_installer(shell, "uninstall", project)
                self.assertEqual(code, 0, output)
                self.assertNotIn(SKILL_NAME, os.listdir(os.path.join(project, ".agents", "plugins")))
                self.assertEqual(self.exclude_lines(project), ["/build"])
                self.assertIn("Restart Antigravity", output)

    def test_an_entry_that_was_there_before_survives(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.git_project(self.fresh(shell, "kept"), exclude=ENTRY + "\n")
                code, output = self.run_installer(shell, "install", project)
                self.assertEqual(code, 0, output)
                self.assertEqual(self.exclude_lines(project), [ENTRY])
                code, output = self.run_installer(shell, "uninstall", project)
                self.assertEqual(code, 0, output)
                self.assertEqual(self.exclude_lines(project), [ENTRY])
                self.assertNotIn(SKILL_NAME, os.listdir(os.path.join(project, ".agents", "plugins")))

    def test_a_copy_carries_the_payload_and_the_sentinel(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.fresh(shell, "copy")
                dest = os.path.join(project, ".agents", "plugins", SKILL_NAME)
                for _ in range(2):
                    code, output = self.run_installer(shell, "install", project, copy=True)
                    self.assertEqual(code, 0, output)
                self.assertFalse(is_link(dest))
                for relative in ("plugin.json", "skills/dev-orchestra/SKILL.md", SENTINEL):
                    self.assertTrue(os.path.isfile(os.path.join(dest, relative)), relative)
                for absent in ("tests", ".git", ".agents"):
                    self.assertFalse(os.path.exists(os.path.join(dest, absent)), absent)
                code, output = self.run_installer(shell, "uninstall", project)
                self.assertEqual(code, 0, output)
                self.assertNotIn(SKILL_NAME, os.listdir(os.path.dirname(dest)))

    def test_a_directory_without_the_sentinel_is_left_alone(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.git_project(self.fresh(shell, "foreign-dir"))
                dest = os.path.join(project, ".agents", "plugins", SKILL_NAME)
                os.makedirs(dest)
                self.write_file(os.path.join(dest, "mine.txt"))
                for action in ("install", "uninstall"):
                    code, output = self.run_installer(shell, action, project)
                    self.assert_refused(code, output)
                    self.assertTrue(os.path.isfile(os.path.join(dest, "mine.txt")), action)
                # A refused install leaves the exclude file as it was.
                self.assertEqual(self.exclude_lines(project), [])

    def test_a_clone_is_left_alone_even_with_the_sentinel(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.fresh(shell, "clone")
                dest = os.path.join(project, ".agents", "plugins", SKILL_NAME)
                os.makedirs(os.path.join(dest, ".git"))
                self.write_file(os.path.join(dest, SENTINEL))
                for action in ("install", "uninstall"):
                    code, output = self.run_installer(shell, action, project)
                    self.assert_refused(code, output)
                    self.assertTrue(os.path.isdir(os.path.join(dest, ".git")), action)

    def test_a_copy_without_the_sentinel_is_left_alone(self):
        """Every Antigravity copy had one, so the Claude mode's allowance for
        older copies does not apply here."""
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.fresh(shell, "unmarked")
                dest = os.path.join(project, ".agents", "plugins", SKILL_NAME)
                code, output = self.run_installer(shell, "install", project, copy=True)
                self.assertEqual(code, 0, output)
                os.remove(os.path.join(dest, SENTINEL))
                for action in ("install", "uninstall"):
                    code, output = self.run_installer(shell, action, project, copy=action == "install")
                    self.assert_refused(code, output)
                    self.assertTrue(os.path.isfile(os.path.join(dest, "plugin.json")), action)

    def test_a_link_to_another_directory_is_left_alone(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "foreign-link")
                other = os.path.join(base, "other-plugin")
                os.makedirs(other)
                plugins = os.path.join(base, "proj", ".agents", "plugins")
                os.makedirs(plugins)
                dest = os.path.join(plugins, SKILL_NAME)
                make_dir_link(dest, other)
                for action in ("install", "uninstall"):
                    code, output = self.run_installer(shell, action, os.path.join(base, "proj"))
                    self.assert_refused(code, output)
                    self.assertTrue(is_link(dest), action)
                    self.assertTrue(same_path(dest, other), action)
                self.assertEqual(os.listdir(other), [])

    def test_a_dangling_link_is_left_alone(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "dangling")
                gone = os.path.join(base, "gone")
                plugins = os.path.join(base, "proj", ".agents", "plugins")
                os.makedirs(plugins)
                dest = os.path.join(plugins, SKILL_NAME)
                if os.name == "nt":
                    # A junction needs a target when it is made; take it away after.
                    os.makedirs(gone)
                    make_dir_link(dest, gone)
                    os.rmdir(gone)
                else:
                    os.symlink(gone, dest)
                for action in ("install", "uninstall"):
                    code, output = self.run_installer(shell, action, os.path.join(base, "proj"))
                    self.assert_refused(code, output)
                    self.assertIn(SKILL_NAME, os.listdir(plugins), action)
                    self.assertNotIn("Linked", output)
                    # Nothing was copied through the link.
                    self.assertFalse(os.path.exists(gone), action)

    def test_a_link_is_refused_while_the_checkout_has_something_else_to_load(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "autoload")
                checkout = self.make_checkout(base)
                self.write_file(os.path.join(checkout, "hooks.json"), "{}\n")
                project = os.path.join(base, "proj")
                dest = os.path.join(project, ".agents", "plugins", SKILL_NAME)

                code, output = self.run_installer(shell, "install", project, root=checkout)
                self.assertEqual(code, 1, output)
                self.assertIn("hooks.json", output)
                self.assertFalse(os.path.lexists(dest), output)

                code, output = self.run_installer(shell, "install", project, copy=True, root=checkout)
                self.assertEqual(code, 0, output)
                self.assertTrue(os.path.isfile(os.path.join(dest, "plugin.json")))
                self.assertFalse(os.path.exists(os.path.join(dest, "hooks.json")))

    def test_a_refused_relink_keeps_the_existing_link(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "relink")
                checkout = self.make_checkout(base)
                project = os.path.join(base, "proj")
                dest = os.path.join(project, ".agents", "plugins", SKILL_NAME)

                code, output = self.run_installer(shell, "install", project, root=checkout)
                self.assertEqual(code, 0, output)
                self.assertTrue(same_path(dest, checkout), output)

                self.write_file(os.path.join(checkout, "hooks.json"), "{}\n")
                code, output = self.run_installer(shell, "install", project, root=checkout)
                self.assertEqual(code, 1, output)
                self.assertIn("hooks.json", output)
                self.assertTrue(is_link(dest), output)
                self.assertTrue(same_path(dest, checkout), output)

    def test_a_copy_leaves_out_agent_definitions(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "agents")
                checkout = self.make_checkout(base)
                self.write_file(os.path.join(checkout, "agents", "evil.md"), "# evil\n")
                project = os.path.join(base, "proj")
                dest = os.path.join(project, ".agents", "plugins", SKILL_NAME)

                code, output = self.run_installer(shell, "install", project, root=checkout)
                self.assertEqual(code, 1, output)
                self.assertIn("agents/evil.md", output)
                self.assertFalse(os.path.lexists(dest), output)

                code, output = self.run_installer(shell, "install", project, copy=True, root=checkout)
                self.assertEqual(code, 0, output)
                self.assertFalse(os.path.exists(os.path.join(dest, "agents", "evil.md")), output)
                self.assertTrue(os.path.isfile(os.path.join(dest, "agents", "openai.yaml")), output)

    def test_an_exclude_file_with_crlf_line_endings(self):
        """install.ps1 writes CRLF; either shell has to read what the other wrote."""
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.git_project(self.fresh(shell, "crlf"))
                exclude = os.path.join(project, ".git", "info", "exclude")
                with open(exclude, "wb") as handle:
                    handle.write(("/build\r\n%s\r\n%s\r\n" % (MARKER, ENTRY)).encode("utf-8"))

                code, output = self.run_installer(shell, "install", project)
                self.assertEqual(code, 0, output)
                lines = self.exclude_lines(project)
                self.assertEqual(lines.count(MARKER), 1, lines)
                self.assertEqual(lines.count(ENTRY), 1, lines)

                code, output = self.run_installer(shell, "uninstall", project)
                self.assertEqual(code, 0, output)
                self.assertEqual(self.exclude_lines(project), ["/build"])

    def test_the_hand_removal_command_can_be_pasted(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = os.path.join(self.fresh(shell, "paste"), "my proj's")
                other = os.path.join(base, "other-plugin")
                os.makedirs(other)
                self.write_file(os.path.join(other, "mine.txt"))
                plugins = os.path.join(base, "proj", ".agents", "plugins")
                os.makedirs(plugins)
                dest = os.path.join(plugins, SKILL_NAME)
                make_dir_link(dest, other)
                for action in ("install", "uninstall"):
                    code, output = self.run_installer(shell, action, os.path.join(base, "proj"))
                    self.assert_refused(code, output)
                    self.assertTrue(is_link(dest), action)

                command = self.hand_removal_command(output)
                if shell.posix:
                    pasted = [shell.command[0], "-c", command]
                else:
                    pasted = [shell.command[0], "-NoProfile", "-NonInteractive", "-Command", command]
                result = subprocess.run(pasted, capture_output=True, text=True, timeout=300)
                self.assertEqual(result.returncode, 0, command + "\n" + result.stdout + result.stderr)
                self.assertFalse(os.path.lexists(dest), command)
                self.assertTrue(os.path.isfile(os.path.join(other, "mine.txt")), command)

    def test_the_codex_switch_does_not_combine_with_it(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                script = os.path.join(REPO_ROOT, "install", "install" + shell.suffix)
                args = ["--antigravity", "--codex"] if shell.posix else ["-Antigravity", "-Codex"]
                result = subprocess.run(
                    [*shell.command, script, *args],
                    capture_output=True,
                    text=True,
                    env=dict(os.environ, HOME=self.home),
                    timeout=300,
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("cannot be combined", result.stderr)

    def test_a_project_install_into_the_checkout_itself(self):
        """How a developer loads the plugin while working on this repository.

        It changes the checkout, so everything it made is removed afterwards,
        also when an assertion fails part way.
        """
        dest = os.path.join(REPO_ROOT, ".agents", "plugins", SKILL_NAME)
        exclude = os.path.join(REPO_ROOT, ".git", "info", "exclude")
        if not os.path.isdir(os.path.join(REPO_ROOT, ".git")):
            self.skipTest("the checkout has no .git directory")
        if os.path.lexists(dest):
            self.skipTest("something is already installed at %s" % dest)
        saved = None
        if os.path.exists(exclude):
            with open(exclude, "rb") as handle:
                saved = handle.read()
        self.addCleanup(self.restore_checkout, dest, exclude, saved)

        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                code, output = self.run_installer(shell, "install", REPO_ROOT)
                self.assertEqual(code, 0, output)
                self.assertTrue(same_path(dest, REPO_ROOT), output)
                lines = self.exclude_lines(REPO_ROOT)
                self.assertEqual(lines.index(MARKER) + 1, lines.index(ENTRY), lines)
                code, output = self.run_installer(shell, "uninstall", REPO_ROOT)
                self.assertEqual(code, 0, output)
                self.assertNotIn(SKILL_NAME, os.listdir(os.path.dirname(dest)))
                self.assertNotIn(MARKER, self.exclude_lines(REPO_ROOT))

    def restore_checkout(self, dest: str, exclude: str, saved: Optional[bytes]) -> None:
        if is_link(dest):
            remove_link(dest)
        elif os.path.isfile(os.path.join(dest, SENTINEL)):
            remove_tree(dest)
        if saved is None:
            if os.path.exists(exclude):
                os.remove(exclude)
        else:
            with open(exclude, "wb") as handle:
                handle.write(saved)


CLAUDE_ENTRY = "/.claude/skills/dev-orchestra"


@unittest.skipUnless(SHELLS, "needs sh (off Windows), pwsh or powershell")
class TestClaudeInstallers(_InstallerCase):
    """Claude mode: only what the installer made is removed, a link as a link,
    and the exclude line is written once the install succeeded, on a line of
    its own."""

    def claude_dest(self, project: str) -> str:
        return os.path.join(project, ".claude", "skills", SKILL_NAME)

    def claude(self, shell: Shell, action: str, project: str, **kwargs) -> Tuple[int, str]:
        return self.run_installer(shell, action, project, mode="claude", **kwargs)

    def link_at_destination(self, base: str) -> Tuple[str, str, str]:
        """A throwaway checkout with a file of its own, linked from the destination."""
        checkout = self.make_checkout(base)
        self.write_file(os.path.join(checkout, "mine.txt"))
        project = os.path.join(base, "proj")
        dest = self.claude_dest(project)
        os.makedirs(os.path.dirname(dest))
        make_dir_link(dest, checkout)
        return project, dest, checkout

    def assert_checkout_intact(self, checkout: str) -> None:
        for name in ("mine.txt", "plugin.json"):
            self.assertTrue(os.path.isfile(os.path.join(checkout, name)), name)

    def test_a_bom_left_by_an_earlier_install_is_removed(self):
        for shell in [s for s in SHELLS if not s.posix]:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "bom")
                project = self.git_project(base)
                exclude = os.path.join(project, ".git", "info", "exclude")
                with open(exclude, "wb") as handle:
                    handle.write(codecs.BOM_UTF8 + (CLAUDE_ENTRY + "\n").encode("utf-8"))
                code, output = self.claude(shell, "install", project, copy=True)
                self.assertEqual(code, 0, output)
                with open(exclude, "rb") as handle:
                    data = handle.read()
                self.assertFalse(data.startswith(codecs.BOM_UTF8), data)
                self.assertEqual(self.exclude_lines(project).count(CLAUDE_ENTRY), 1)

    def test_a_relative_project_from_another_directory(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "relative")
                project = self.git_project(base)
                elsewhere = os.path.join(base, "elsewhere")
                os.makedirs(elsewhere)
                relative = os.path.relpath(project, elsewhere)
                dest = self.claude_dest(project)
                code, output = self.claude(shell, "install", relative, copy=True, cwd=elsewhere)
                self.assertEqual(code, 0, output)
                self.assertTrue(os.path.isfile(os.path.join(dest, "plugin.json")), output)
                self.assertEqual(self.exclude_lines(project).count(CLAUDE_ENTRY), 1)
                code, output = self.claude(shell, "uninstall", relative, cwd=elsewhere)
                self.assertEqual(code, 0, output)
                self.assertNotIn(SKILL_NAME, os.listdir(os.path.dirname(dest)))

    @unittest.skipUnless(os.name == "nt", "a junction, and the .NET calls that take a relative path wrong")
    def test_a_relative_project_with_a_junction_at_the_destination(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "relative-link")
                project, dest, checkout = self.link_at_destination(base)
                elsewhere = os.path.join(base, "elsewhere")
                os.makedirs(elsewhere)
                relative = os.path.relpath(project, elsewhere)
                code, output = self.claude(shell, "uninstall", relative, cwd=elsewhere, root=checkout)
                self.assertEqual(code, 0, output)
                self.assertFalse(os.path.lexists(dest), output)
                self.assert_checkout_intact(checkout)

    def test_install_replaces_a_link_without_emptying_its_target(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project, dest, checkout = self.link_at_destination(self.fresh(shell, "replace"))
                code, output = self.claude(shell, "install", project, copy=True, root=checkout)
                self.assertEqual(code, 0, output)
                self.assertFalse(is_link(dest), output)
                self.assertTrue(os.path.isfile(os.path.join(dest, "plugin.json")), output)
                self.assert_checkout_intact(checkout)

    def test_uninstall_removes_a_link_without_emptying_its_target(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project, dest, checkout = self.link_at_destination(self.fresh(shell, "unlink"))
                code, output = self.claude(shell, "uninstall", project, root=checkout)
                self.assertEqual(code, 0, output)
                self.assertIn("Removed", output)
                self.assertNotIn(SKILL_NAME, os.listdir(os.path.dirname(dest)))
                self.assert_checkout_intact(checkout)

    def test_a_link_to_another_checkout_is_left_alone(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project, dest, checkout = self.link_at_destination(self.fresh(shell, "other-link"))
                for action in ("install", "uninstall"):
                    code, output = self.claude(shell, action, project, copy=action == "install")
                    self.assert_refused(code, output)
                    self.assertTrue(is_link(dest), action)
                    self.assertTrue(same_path(dest, checkout), action)
                self.assert_checkout_intact(checkout)

    def test_a_dangling_link_is_left_alone(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "dangling")
                gone = os.path.join(base, "gone")
                project = os.path.join(base, "proj")
                dest = self.claude_dest(project)
                os.makedirs(os.path.dirname(dest))
                if os.name == "nt":
                    # A junction needs a target when it is made; take it away after.
                    os.makedirs(gone)
                    make_dir_link(dest, gone)
                    os.rmdir(gone)
                else:
                    os.symlink(gone, dest)
                for action in ("install", "uninstall"):
                    code, output = self.claude(shell, action, project, copy=action == "install")
                    self.assert_refused(code, output)
                    self.assertIn(SKILL_NAME, os.listdir(os.path.dirname(dest)), action)
                    # Nothing was copied through the link.
                    self.assertFalse(os.path.exists(gone), action)

    def test_a_file_symlink_is_left_alone(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "file-link")
                target = os.path.join(base, "target.txt")
                self.write_file(target, "keep me\n")
                project = os.path.join(base, "proj")
                dest = self.claude_dest(project)
                os.makedirs(os.path.dirname(dest))
                try:
                    os.symlink(target, dest)
                except OSError as exc:
                    self.skipTest("cannot make a file symlink here: %s" % exc)
                for action in ("install", "uninstall"):
                    code, output = self.claude(shell, action, project, copy=action == "install")
                    self.assert_refused(code, output)
                    self.assertTrue(os.path.islink(dest), action)
                    self.assertEqual(read_text(target), "keep me\n")

    def test_the_exclude_entry_is_written_once(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.git_project(self.fresh(shell, "twice"))
                for _ in range(2):
                    code, output = self.claude(shell, "install", project, copy=True)
                    self.assertEqual(code, 0, output)
                self.assertEqual(self.exclude_lines(project).count(CLAUDE_ENTRY), 1)

    def test_the_exclude_entry_gets_its_own_line_and_no_bom(self):
        """A one-line file is where PowerShell's if-assignment joined the two."""
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.git_project(self.fresh(shell, "one-line"), exclude="/build\n")
                exclude = os.path.join(project, ".git", "info", "exclude")
                code, output = self.claude(shell, "install", project, copy=True)
                self.assertEqual(code, 0, output)
                with open(exclude, "rb") as handle:
                    written = handle.read()
                self.assertFalse(written.startswith(b"\xef\xbb\xbf"), written)
                self.assertEqual(written.decode("utf-8").splitlines(), ["/build", CLAUDE_ENTRY])
                code, output = self.claude(shell, "install", project, copy=True)
                self.assertEqual(code, 0, output)
                with open(exclude, "rb") as handle:
                    self.assertEqual(handle.read(), written)

    def test_a_copy_carries_the_sentinel(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.fresh(shell, "copy")
                dest = self.claude_dest(project)
                for _ in range(2):
                    code, output = self.claude(shell, "install", project, copy=True)
                    self.assertEqual(code, 0, output)
                for relative in ("plugin.json", "skills/dev-orchestra/SKILL.md", SENTINEL):
                    self.assertTrue(os.path.isfile(os.path.join(dest, relative)), relative)
                self.assertFalse(os.path.exists(os.path.join(dest, ".git")))
                code, output = self.claude(shell, "uninstall", project)
                self.assertEqual(code, 0, output)
                self.assertNotIn(SKILL_NAME, os.listdir(os.path.dirname(dest)))

    def test_what_the_installer_did_not_write_is_left_alone(self):
        """A directory of the user's, and a clone, the sentinel notwithstanding."""
        for shell in SHELLS:
            for label, planted in (("foreign-dir", "mine.txt"), ("clone", ".git")):
                with self.subTest(shell=shell.name, case=label):
                    project = self.git_project(self.fresh(shell, label))
                    dest = self.claude_dest(project)
                    os.makedirs(dest)
                    if planted == ".git":
                        os.makedirs(os.path.join(dest, planted))
                        self.write_file(os.path.join(dest, SENTINEL))
                    else:
                        self.write_file(os.path.join(dest, planted))
                    for action in ("install", "uninstall"):
                        code, output = self.claude(shell, action, project, copy=action == "install")
                        self.assert_refused(code, output)
                        self.assertTrue(os.path.exists(os.path.join(dest, planted)), action)
                    # A refused install leaves the exclude file as it was.
                    self.assertEqual(self.exclude_lines(project), [])

    def unmarked_copy(self, shell: Shell, project: str, plugin_json: bool = True) -> str:
        """A copy as the installers wrote it before they added the sentinel."""
        code, output = self.claude(shell, "install", project, copy=True)
        self.assertEqual(code, 0, output)
        dest = self.claude_dest(project)
        os.remove(os.path.join(dest, SENTINEL))
        if not plugin_json:
            # The copies from before plugin.json was part of the payload.
            os.remove(os.path.join(dest, "plugin.json"))
        return dest

    def test_an_older_copy_without_the_sentinel_is_still_replaced(self):
        for shell in SHELLS:
            for plugin_json in (True, False):
                with self.subTest(shell=shell.name, plugin_json=plugin_json):
                    project = self.fresh(shell, "older-%s" % plugin_json)
                    dest = self.unmarked_copy(shell, project, plugin_json)
                    code, output = self.claude(shell, "install", project, copy=True)
                    self.assertEqual(code, 0, output)
                    self.assertTrue(os.path.isfile(os.path.join(dest, SENTINEL)), output)
                    self.assertTrue(os.path.isfile(os.path.join(dest, "plugin.json")), output)

                    os.remove(os.path.join(dest, SENTINEL))
                    code, output = self.claude(shell, "uninstall", project)
                    self.assertEqual(code, 0, output)
                    self.assertNotIn(SKILL_NAME, os.listdir(os.path.dirname(dest)))

    def test_an_older_copy_with_something_added_is_left_alone(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.fresh(shell, "older-added")
                dest = self.unmarked_copy(shell, project)
                self.write_file(os.path.join(dest, "notes.txt"))
                for action in ("install", "uninstall"):
                    code, output = self.claude(shell, action, project, copy=action == "install")
                    self.assert_refused(code, output)
                    self.assertTrue(os.path.isfile(os.path.join(dest, "notes.txt")), action)

    def assert_checkout_refused(self, code: int, output: str, checkout: str) -> None:
        self.assertEqual(code, 1, output)
        self.assertIn("is this checkout itself", output)
        self.assert_checkout_intact(checkout)
        self.assertTrue(os.path.isdir(os.path.join(checkout, "install")), output)

    def test_a_checkout_at_the_destination_is_never_removed(self):
        """Run from a clone made right where the skill goes. A sentinel in it
        too, so that only the check for the checkout itself stands between it
        and removal."""
        for shell in SHELLS:
            for clone in (True, False):
                with self.subTest(shell=shell.name, clone=clone):
                    base = self.fresh(shell, "self-%s" % clone)
                    project = os.path.join(base, "proj")
                    dest = self.claude_dest(project)
                    os.makedirs(os.path.dirname(dest))
                    os.rename(self.make_checkout(base), dest)
                    self.write_file(os.path.join(dest, "mine.txt"))
                    self.write_file(os.path.join(dest, SENTINEL))
                    if clone:
                        os.makedirs(os.path.join(dest, ".git"))
                    for copy in (False, True):
                        code, output = self.claude(shell, "install", project, copy=copy, root=dest)
                        self.assert_checkout_refused(code, output, dest)
                    code, output = self.claude(shell, "uninstall", project, root=dest)
                    self.assert_checkout_refused(code, output, dest)

    def test_a_checkout_reached_through_a_link_above_the_destination(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "self-linked")
                real_skills = os.path.join(base, "real-skills")
                os.makedirs(real_skills)
                checkout = os.path.join(real_skills, SKILL_NAME)
                os.rename(self.make_checkout(base), checkout)
                self.write_file(os.path.join(checkout, "mine.txt"))
                self.write_file(os.path.join(checkout, SENTINEL))
                project = os.path.join(base, "proj")
                os.makedirs(os.path.join(project, ".claude"))
                make_dir_link(os.path.join(project, ".claude", "skills"), real_skills)
                code, output = self.claude(shell, "install", project, copy=True, root=checkout)
                self.assert_checkout_refused(code, output, checkout)
                code, output = self.claude(shell, "uninstall", project, root=checkout)
                self.assert_checkout_refused(code, output, checkout)

    def test_the_scripts_agree_on_what_a_copy_carries(self):
        """An older copy is recognised by it, in all four scripts alike."""
        sh_list = 'PAYLOAD="%s"' % " ".join(PAYLOAD)
        ps1_list = "$Payload = @(%s)" % ", ".join("'%s'" % item for item in PAYLOAD)
        for action in ("install", "uninstall"):
            for suffix, expected in ((".sh", sh_list), (".ps1", ps1_list)):
                relative = os.path.join("install", action + suffix)
                self.assertIn(expected, read_text(os.path.join(REPO_ROOT, relative)), relative)


POSIX_SHELLS = [shell for shell in SHELLS if shell.posix]
PWSH_ON_POSIX = [shell for shell in SHELLS if shell.name == "pwsh" and os.name != "nt"]


@unittest.skipUnless(
    PWSH_ON_POSIX and os.name != "nt" and os.geteuid() != 0, "needs pwsh off Windows, not as root"
)
class TestPowerShellClaudeCopyFailure(_InstallerCase):
    def test_a_failed_copy_leaves_the_exclude_file_alone(self):
        """Proven by running it here; on Windows by the static ordering test."""
        shell = PWSH_ON_POSIX[0]
        base = self.fresh(shell, "unreadable")
        checkout = self.make_checkout(base)
        project = self.git_project(base)
        # One level below a readable skills/, so that Copy-Payload's Test-Path
        # passes and Copy-Item then fails.
        unreadable = os.path.join(checkout, "skills", "dev-orchestra")
        os.chmod(unreadable, 0)
        try:
            code, output = self.run_installer(
                shell, "install", project, copy=True, root=checkout, mode="claude"
            )
        finally:
            # Before tearDown removes the tree.
            os.chmod(unreadable, 0o755)
        self.assertNotEqual(code, 0, output)
        dest = os.path.join(project, ".claude", "skills", SKILL_NAME)
        self.assertFalse(os.path.isfile(os.path.join(dest, "skills", "dev-orchestra", "SKILL.md")), output)
        self.assertFalse(os.path.exists(os.path.join(project, ".git", "info", "exclude")), output)


@unittest.skipUnless(POSIX_SHELLS, "needs sh, off Windows")
class TestPosixAntigravityInstaller(_InstallerCase):
    def test_a_fresh_home_gets_a_global_link(self):
        shell = POSIX_SHELLS[0]
        dest = os.path.join(self.home, ".gemini", "config", "plugins", SKILL_NAME)
        code, output = self.run_installer(shell, "install")
        self.assertEqual(code, 0, output)
        self.assert_linked(shell, dest, output)
        code, output = self.run_installer(shell, "uninstall")
        self.assertEqual(code, 0, output)
        self.assertNotIn(SKILL_NAME, os.listdir(os.path.dirname(dest)))

    def test_a_failing_ln_falls_back_to_a_copy(self):
        shell = POSIX_SHELLS[0]
        stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(stubs)
        ln = os.path.join(stubs, "ln")
        with open(ln, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho 'ln: not allowed here' >&2\nexit 1\n")
        os.chmod(ln, os.stat(ln).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        # The stub goes first; the rest of PATH stays, so cp and mkdir resolve.
        env = dict(os.environ, HOME=self.home, PATH=stubs + os.pathsep + os.environ.get("PATH", ""))

        project = os.path.join(self.tmp, "proj")
        dest = os.path.join(project, ".agents", "plugins", SKILL_NAME)
        code, output = self.run_installer(shell, "install", project, env=env)
        self.assertEqual(code, 0, output)
        self.assertIn("warning: could not create a symlink", output)
        self.assertIn("ln: not allowed here", output)
        self.assertIn("Re-run this installer", output)
        self.assertFalse(is_link(dest))
        for relative in ("plugin.json", "skills/dev-orchestra/SKILL.md", SENTINEL):
            self.assertTrue(os.path.isfile(os.path.join(dest, relative)), relative)

    def test_a_failed_claude_link_leaves_the_exclude_file_alone(self):
        shell = POSIX_SHELLS[0]
        stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(stubs)
        ln = os.path.join(stubs, "ln")
        with open(ln, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho 'ln: not allowed here' >&2\nexit 1\n")
        os.chmod(ln, os.stat(ln).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = dict(os.environ, HOME=self.home, PATH=stubs + os.pathsep + os.environ.get("PATH", ""))

        project = self.git_project(self.tmp)
        code, output = self.run_installer(shell, "install", project, env=env, mode="claude")
        self.assertEqual(code, 1, output)
        self.assertIn("re-run with --copy", output)
        self.assertFalse(os.path.exists(os.path.join(project, ".git", "info", "exclude")), output)

    def test_an_ln_that_copies_instead_is_replaced_by_the_payload(self):
        """Git Bash's `ln -s` copies the whole checkout and exits 0."""
        shell = POSIX_SHELLS[0]
        stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(stubs)
        ln = os.path.join(stubs, "ln")
        with open(ln, "w", encoding="utf-8") as handle:
            # Called as `ln -s <root> <dest>`: stand in for the copy with a .git.
            handle.write('#!/bin/sh\nmkdir -p "$3/.git"\nexit 0\n')
        os.chmod(ln, os.stat(ln).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = dict(os.environ, HOME=self.home, PATH=stubs + os.pathsep + os.environ.get("PATH", ""))

        project = os.path.join(self.tmp, "proj")
        dest = os.path.join(project, ".agents", "plugins", SKILL_NAME)
        code, output = self.run_installer(shell, "install", project, env=env)
        self.assertEqual(code, 0, output)
        self.assertIn("ln -s made a copy, not a link", output)
        self.assertFalse(os.path.exists(os.path.join(dest, ".git")))
        self.assertTrue(os.path.isfile(os.path.join(dest, SENTINEL)))
        code, output = self.run_installer(shell, "uninstall", project, env=env)
        self.assertEqual(code, 0, output)
        self.assertNotIn(SKILL_NAME, os.listdir(os.path.dirname(dest)))


if __name__ == "__main__":
    unittest.main()
