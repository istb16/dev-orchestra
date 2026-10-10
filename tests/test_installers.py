"""The installers, run for real against temp directories.

Mostly the Antigravity mode, the Claude mode's link handling and exclude
line, and the Codex mode's AGENTS.md block. Nothing here needs Antigravity:
an install is a link or a copy in a `plugins/` folder, and that is what these
tests look at. Every shell found on PATH runs the same cases, one subtest
each -- `sh` off Windows, and `pwsh` and Windows PowerShell wherever they are.
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
        elif mode == "codex":
            args.append("--codex" if shell.posix else "-Codex")
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
                self.assertIn("Left %s in .git/info/exclude" % ENTRY, output)
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

    def test_the_codex_pointer_quotes_the_script_path(self):
        """A checkout path with a space must not split the command line (#297)."""
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = os.path.join(self.tmp, "codex project " + shell.name)
                os.makedirs(project)
                script = os.path.join(REPO_ROOT, "install", "install" + shell.suffix)
                args = ["--codex", "--project", project] if shell.posix else ["-Codex", "-Project", project]
                result = subprocess.run(
                    [*shell.command, script, *args],
                    capture_output=True,
                    text=True,
                    env=dict(os.environ, HOME=self.home),
                    timeout=300,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                pointer = read_text(os.path.join(project, "AGENTS.md"))
                line = next(line for line in pointer.splitlines() if "dev_orchestra.py" in line)
                self.assertRegex(line, r"^    \S+ '[^']+/scripts/dev_orchestra\.py' <command>$")

    def test_the_codex_pointer_keeps_shell_characters_literal(self):
        """A `$`, a `$( )`, a backtick or a ' in the checkout path is not
        expanded when the line runs, in sh or in PowerShell (#297). Nor is a
        typographic quote, which PowerShell also ends a quoted string with."""
        for index, name in enumerate(("it's $HOME $(x) `x`", "Bob\u2019s; $(x) \u2018a\u201a\u201b")):
            self.check_pointer_is_literal(os.path.join(self.tmp, name), "codex-literal-%d" % index)

    def check_pointer_is_literal(self, checkout: str, label: str) -> None:
        os.makedirs(os.path.join(checkout, "install"))
        for shell in SHELLS:
            shutil.copy2(
                os.path.join(REPO_ROOT, "install", "install" + shell.suffix),
                os.path.join(checkout, "install"),
            )
        for shell in SHELLS:
            with self.subTest(shell=shell.name, checkout=os.path.basename(checkout)):
                project = self.fresh(shell, label)
                script = os.path.join(checkout, "install", "install" + shell.suffix)
                args = ["--codex", "--project", project] if shell.posix else ["-Codex", "-Project", project]
                result = subprocess.run(
                    [*shell.command, script, *args],
                    capture_output=True,
                    text=True,
                    env=dict(os.environ, HOME=self.home),
                    timeout=300,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                pointer = read_text(os.path.join(project, "AGENTS.md"))
                line = next(line for line in pointer.splitlines() if "dev_orchestra.py" in line)
                argument = line.strip().split(" ", 1)[1].rsplit(" <command>", 1)[0]
                if shell.posix:
                    expected = os.path.join(checkout, "scripts", "dev_orchestra.py")
                    echo = [shell.command[0], "-c", "printf '%%s' %s" % argument]
                else:
                    expected = checkout + "/scripts/dev_orchestra.py"
                    echo = [shell.command[0], "-NoProfile", "-NonInteractive", "-Command"]
                    # The bytes written as they are: setting [Console]::OutputEncoding
                    # would change the code page of the console every later test shares.
                    echo.append(
                        "$b = [Text.Encoding]::UTF8.GetBytes(%s); "
                        "[Console]::OpenStandardOutput().Write($b, 0, $b.Length)" % argument
                    )
                echoed = subprocess.run(echo, capture_output=True, encoding="utf-8", timeout=60)
                if shell.posix:
                    self.assertEqual(echoed.stdout, expected, echoed.stderr)
                else:
                    # PowerShell writes the checkout by its long name; the temporary
                    # directory may be given by its 8.3 one (RUNNER~1 on CI).
                    self.assertTrue(
                        same_path(echoed.stdout, expected),
                        "%r != %r: %s" % (echoed.stdout, expected, echoed.stderr),
                    )

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
CLAUDE_MARKER = "# added by dev-orchestra install --claude"


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

    def test_a_link_to_the_checkout_through_another_name_is_ours(self):
        """A link made through a junction to the checkout, or an installer run
        through one, still points at this checkout: both sides are compared
        with their links followed, not as written."""
        for shell in SHELLS:
            for through in ("link", "root"):
                with self.subTest(shell=shell.name, through=through):
                    base = self.fresh(shell, "alias-%s" % through)
                    project, dest, checkout = self.link_at_destination(base)
                    alias = os.path.join(base, "alias")
                    make_dir_link(alias, checkout)
                    root = checkout
                    if through == "link":
                        remove_link(dest)
                        make_dir_link(dest, alias)
                    else:
                        root = alias
                    code, output = self.claude(shell, "uninstall", project, root=root)
                    self.assertEqual(code, 0, output)
                    self.assertIn("Removed", output)
                    self.assertNotIn(SKILL_NAME, os.listdir(os.path.dirname(dest)))
                    self.assert_checkout_intact(checkout)

                    make_dir_link(dest, alias if through == "link" else checkout)
                    code, output = self.claude(shell, "install", project, copy=True, root=root)
                    self.assertEqual(code, 0, output)
                    self.assertFalse(is_link(dest), output)
                    self.assertTrue(os.path.isfile(os.path.join(dest, SENTINEL)), output)
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

    def test_uninstall_removes_the_marked_exclude_entry(self):
        """The entry goes in under a marker, and only a marked one comes out (#294)."""
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.git_project(self.fresh(shell, "marked"), exclude="/build\n")
                code, output = self.claude(shell, "install", project, copy=True)
                self.assertEqual(code, 0, output)
                self.assertEqual(self.exclude_lines(project), ["/build", CLAUDE_MARKER, CLAUDE_ENTRY])
                code, output = self.claude(shell, "uninstall", project)
                self.assertEqual(code, 0, output)
                self.assertIn("Removed %s from .git/info/exclude" % CLAUDE_ENTRY, output)
                self.assertEqual(self.exclude_lines(project), ["/build"])

    def test_an_unmarked_entry_stays_and_is_named(self):
        """What an installer before the marker wrote, or the user's own line,
        is neither marked nor duplicated, and the uninstaller leaves it."""
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.git_project(self.fresh(shell, "unmarked"), exclude=CLAUDE_ENTRY + "\n")
                code, output = self.claude(shell, "install", project, copy=True)
                self.assertEqual(code, 0, output)
                self.assertEqual(self.exclude_lines(project), [CLAUDE_ENTRY])
                code, output = self.claude(shell, "uninstall", project)
                self.assertEqual(code, 0, output)
                self.assertEqual(self.exclude_lines(project), [CLAUDE_ENTRY])
                self.assertIn("Left %s in .git/info/exclude" % CLAUDE_ENTRY, output)
                self.assertNotIn(SKILL_NAME, os.listdir(os.path.dirname(self.claude_dest(project))))

    def test_an_exclude_file_with_crlf_line_endings(self):
        """install.ps1 writes CRLF; either shell has to read what the other
        wrote, without adding the entry a second time."""
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.git_project(self.fresh(shell, "crlf"))
                exclude = os.path.join(project, ".git", "info", "exclude")
                with open(exclude, "wb") as handle:
                    handle.write(("/build\r\n%s\r\n%s\r\n" % (CLAUDE_MARKER, CLAUDE_ENTRY)).encode("utf-8"))
                code, output = self.claude(shell, "install", project, copy=True)
                self.assertEqual(code, 0, output)
                lines = self.exclude_lines(project)
                self.assertEqual(lines.count(CLAUDE_MARKER), 1, lines)
                self.assertEqual(lines.count(CLAUDE_ENTRY), 1, lines)
                code, output = self.claude(shell, "uninstall", project)
                self.assertEqual(code, 0, output)
                with open(exclude, "rb") as handle:
                    self.assertEqual(handle.read(), b"/build\r\n")

    def exclude_round_trip(self, shell: Shell, label: str, original: bytes) -> Tuple[bytes, bytes, str]:
        """The exclude file's bytes after an install and after the uninstall,
        and the uninstaller's output."""
        project = self.git_project(self.fresh(shell, label))
        exclude = os.path.join(project, ".git", "info", "exclude")
        with open(exclude, "wb") as handle:
            handle.write(original)
        code, output = self.claude(shell, "install", project, copy=True)
        self.assertEqual(code, 0, output)
        with open(exclude, "rb") as handle:
            installed = handle.read()
        code, output = self.claude(shell, "uninstall", project)
        self.assertEqual(code, 0, output)
        with open(exclude, "rb") as handle:
            return installed, handle.read(), output

    def test_a_last_pattern_without_a_newline_gets_one(self):
        """The marker goes on a line of its own; the newline added for it
        stays after the uninstall, which changes nothing git reads."""
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                # install.ps1 ends lines with the platform's newline when the file has none to copy.
                newline = "\r\n" if not shell.posix and os.name == "nt" else "\n"
                installed, after, _ = self.exclude_round_trip(shell, "open-end", b"/build")
                expected = newline.join(["/build", CLAUDE_MARKER, CLAUDE_ENTRY, ""]).encode()
                self.assertEqual(installed, expected)
                self.assertEqual(after, ("/build" + newline).encode())

    def test_a_marker_with_nothing_below_it_stays(self):
        """A marker on the last line, its entry gone, is the user's to keep:
        the uninstall takes out only the marker and entry the install added."""
        for shell in SHELLS:
            for original in (
                b"/build\n%s\n" % CLAUDE_MARKER.encode(),
                b"/build\n%s" % CLAUDE_MARKER.encode(),
            ):
                with self.subTest(shell=shell.name, original=original):
                    label = "dangling-%d" % len(original)
                    installed, after, _ = self.exclude_round_trip(shell, label, original)
                    lines = installed.decode("utf-8").splitlines()
                    self.assertEqual(lines, ["/build", CLAUDE_MARKER, CLAUDE_MARKER, CLAUDE_ENTRY])
                    self.assertEqual(after.decode("utf-8").splitlines(), ["/build", CLAUDE_MARKER])
                    self.assertTrue(after.startswith(original), after)

    def test_an_unmarked_entry_in_a_crlf_file_stays_and_is_named(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                original = ("/build\r\n%s\r\n" % CLAUDE_ENTRY).encode()
                installed, after, output = self.exclude_round_trip(shell, "crlf-unmarked", original)
                self.assertEqual(installed, original)
                self.assertEqual(after, original)
                self.assertIn("Left %s in .git/info/exclude" % CLAUDE_ENTRY, output)

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
                expected = ["/build", CLAUDE_MARKER, CLAUDE_ENTRY]
                self.assertEqual(written.decode("utf-8").splitlines(), expected)
                code, output = self.claude(shell, "install", project, copy=True)
                self.assertEqual(code, 0, output)
                with open(exclude, "rb") as handle:
                    self.assertEqual(handle.read(), written)

    def test_powershell_keeps_an_exclude_file_as_it_was_written(self):
        """The Claude install's exclude line, after a non-ASCII pattern in a
        file with LF line endings: both stay as they were."""
        for shell in [s for s in SHELLS if not s.posix]:
            with self.subTest(shell=shell.name):
                original = "/ビルド\n".encode()
                project = self.git_project(self.fresh(shell, "exclude-ja"))
                exclude = os.path.join(project, ".git", "info", "exclude")
                with open(exclude, "wb") as handle:
                    handle.write(original)
                code, output = self.claude(shell, "install", project, copy=True)
                self.assertEqual(code, 0, output)
                with open(exclude, "rb") as handle:
                    written = handle.read()
                self.assertTrue(written.startswith(original), written)
                self.assertNotIn(b"\r", written)
                self.assertIn(CLAUDE_ENTRY, written.decode("utf-8").splitlines())

    def test_powershell_leaves_an_exclude_file_that_is_not_utf8_alone(self):
        """The install still succeeds; the exclude line is left to the user."""
        for shell in [s for s in SHELLS if not s.posix]:
            for label, original in NOT_UTF8:
                with self.subTest(shell=shell.name, encoding=label):
                    project = self.git_project(self.fresh(shell, "exclude-" + label))
                    exclude = os.path.join(project, ".git", "info", "exclude")
                    with open(exclude, "wb") as handle:
                        handle.write(original)
                    for action in ("install", "uninstall"):
                        code, output = self.claude(shell, action, project, copy=action == "install")
                        self.assertEqual(code, 0, output)
                        # Windows PowerShell wraps a warning at the console width.
                        self.assertIn("is not UTF-8", " ".join(output.split()))
                        with open(exclude, "rb") as handle:
                            self.assertEqual(handle.read(), original, action)

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

    def test_a_global_install_goes_into_claude_config_dir(self):
        """Copy mode and the default, which links or, where it cannot, copies."""
        for shell in SHELLS:
            for copy in (True, False):
                with self.subTest(shell=shell.name, copy=copy):
                    base = self.fresh(shell, "global-%s" % copy)
                    config_dir = os.path.join(base, "claude-config")
                    env = dict(os.environ, HOME=self.home, USERPROFILE=self.home)
                    env["CLAUDE_CONFIG_DIR"] = config_dir
                    dest = os.path.join(config_dir, "skills", SKILL_NAME)
                    code, output = self.run_installer(shell, "install", copy=copy, env=env, mode="claude")
                    self.assertEqual(code, 0, output)
                    # A re-run replaces what the first one made.
                    code, output = self.run_installer(shell, "install", copy=copy, env=env, mode="claude")
                    self.assertEqual(code, 0, output)
                    skill = os.path.join(dest, "skills", SKILL_NAME, "SKILL.md")
                    self.assertTrue(os.path.isfile(skill), output)
                    if copy or not is_link(dest):
                        self.assertFalse(is_link(dest), output)
                        self.assertTrue(os.path.isfile(os.path.join(dest, SENTINEL)), output)
                        self.assertFalse(os.path.exists(os.path.join(dest, ".git")), output)
                    else:
                        self.assertTrue(same_path(dest, REPO_ROOT), output)
                    code, output = self.run_installer(shell, "uninstall", env=env, mode="claude")
                    self.assertEqual(code, 0, output)
                    self.assertNotIn(SKILL_NAME, os.listdir(os.path.dirname(dest)))
                    self.assertFalse(os.path.exists(os.path.join(self.home, ".claude")), output)

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

    def unmarked_copy(
        self, shell: Shell, project: str, plugin_json: bool = True, root: str = REPO_ROOT
    ) -> str:
        """A copy as the installers wrote it before they added the sentinel."""
        code, output = self.claude(shell, "install", project, copy=True, root=root)
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

    def test_an_older_copy_with_something_added_inside_is_left_alone(self):
        """Not only at the top: a file of the user's anywhere in the copy, hidden
        or not, is something the checkout does not have."""
        planted = (
            os.path.join("scripts", "mine.py"),
            os.path.join("scripts", "orchestrator", ".env"),
            os.path.join("references", "notes", "draft.md"),
            ".env",
            "..x",
        )
        for shell in SHELLS:
            for relative in planted:
                with self.subTest(shell=shell.name, planted=relative):
                    project = self.fresh(shell, "older-inside-%d" % planted.index(relative))
                    dest = self.unmarked_copy(shell, project)
                    path = os.path.join(dest, relative)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    self.write_file(path)
                    for action in ("install", "uninstall"):
                        code, output = self.claude(shell, action, project, copy=action == "install")
                        self.assert_refused(code, output)
                        self.assertTrue(os.path.isfile(path), action)

    def test_an_older_copy_the_cli_ran_from_is_still_replaced(self):
        """Running the CLI from the copy left Python's bytecode caches in it,
        which the checkout does not have under those names: they are made
        again, so they do not make the copy someone else's."""
        caches = (
            os.path.join("scripts", "orchestrator", "__pycache__", "never_in_a_checkout.cpython-399.pyc"),
            os.path.join("scripts", "__pycache__", "stray.cpython-399.pyc"),
        )
        for shell in SHELLS:
            for action in ("install", "uninstall"):
                with self.subTest(shell=shell.name, action=action):
                    base = self.fresh(shell, "ran-%s" % action)
                    # Without caches of its own, unlike a checkout tests ran in.
                    checkout = self.make_checkout(base)
                    project = os.path.join(base, "proj")
                    dest = self.unmarked_copy(shell, project, root=checkout)
                    for relative in caches:
                        path = os.path.join(dest, relative)
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        self.write_file(path)
                    code, output = self.claude(
                        shell, action, project, copy=action == "install", root=checkout
                    )
                    self.assertEqual(code, 0, output)
                    if action == "install":
                        self.assertTrue(os.path.isfile(os.path.join(dest, SENTINEL)), output)
                        self.assertFalse(os.path.exists(os.path.join(dest, caches[0])), output)
                    else:
                        self.assertFalse(os.path.lexists(dest), output)

    def test_an_older_copy_with_more_than_bytecode_in_a_cache_is_left_alone(self):
        """Python writes only .pyc files into a __pycache__ directory, and none
        outside one. Anything else there is not the installer's, and neither
        is a __pycache__ that is a file."""
        cache = os.path.join("scripts", "orchestrator", "__pycache__")
        planted = (
            os.path.join(cache, "notes.txt"),
            os.path.join(cache, "sub", "x.cpython-399.pyc"),
            os.path.join("scripts", "orchestrator", "loose.pyc"),
            os.path.join("scripts", "__pycache__"),
        )
        for shell in SHELLS:
            for relative in planted:
                with self.subTest(shell=shell.name, planted=relative):
                    base = self.fresh(shell, "cache-%d" % planted.index(relative))
                    checkout = self.make_checkout(base)
                    project = os.path.join(base, "proj")
                    dest = self.unmarked_copy(shell, project, root=checkout)
                    path = os.path.join(dest, relative)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    self.write_file(path)
                    for action in ("install", "uninstall"):
                        code, output = self.claude(
                            shell, action, project, copy=action == "install", root=checkout
                        )
                        self.assert_refused(code, output)
                        self.assertTrue(os.path.isfile(path), action)

    def test_an_older_copy_with_a_linked_cache_is_not_followed(self):
        """A __pycache__ that is a link or junction, or one holding a link,
        is not Python's: the copy is left alone, and what the link points at
        survives."""
        orchestrator = os.path.join("scripts", "orchestrator")
        links = (
            os.path.join(orchestrator, "__pycache__"),
            os.path.join(orchestrator, "__pycache__", "linked.pyc"),
        )
        for shell in SHELLS:
            for relative in links:
                with self.subTest(shell=shell.name, link=relative):
                    base = self.fresh(shell, "cache-link-%d" % links.index(relative))
                    checkout = self.make_checkout(base)
                    project = os.path.join(base, "proj")
                    dest = self.unmarked_copy(shell, project, root=checkout)
                    outside = os.path.join(base, "outside")
                    os.makedirs(outside)
                    self.write_file(os.path.join(outside, "keep.cpython-399.pyc"))
                    link = os.path.join(dest, relative)
                    if os.path.isdir(link):
                        remove_tree(link)
                    os.makedirs(os.path.dirname(link), exist_ok=True)
                    try:
                        make_dir_link(link, outside)
                    except (OSError, subprocess.CalledProcessError) as exc:
                        self.skipTest("cannot make a directory link here: %s" % exc)
                    for action in ("install", "uninstall"):
                        code, output = self.claude(
                            shell, action, project, copy=action == "install", root=checkout
                        )
                        self.assert_refused(code, output)
                        self.assertTrue(is_link(link), action)
                        self.assertTrue(os.path.isfile(os.path.join(outside, "keep.cpython-399.pyc")), action)

    def test_an_older_copy_holding_a_link_is_not_followed(self):
        """A link inside the copy, at a name the checkout has or not, is never
        followed: what it points at survives, whatever becomes of the copy,
        and every shell decides alike."""
        for shell in SHELLS:
            for name in ("examples", "linked"):
                with self.subTest(shell=shell.name, name=name):
                    base = self.fresh(shell, "holding-%s" % name)
                    project = os.path.join(base, "proj")
                    dest = self.unmarked_copy(shell, project)
                    outside = os.path.join(base, "outside")
                    os.makedirs(outside)
                    self.write_file(os.path.join(outside, "keep.txt"))
                    link = os.path.join(dest, name)
                    if os.path.isdir(link):
                        remove_tree(link)
                    try:
                        make_dir_link(link, outside)
                    except (OSError, subprocess.CalledProcessError) as exc:
                        self.skipTest("cannot make a directory link here: %s" % exc)
                    for action in ("install", "uninstall"):
                        code, output = self.claude(shell, action, project, copy=action == "install")
                        self.assert_refused(code, output)
                        self.assertTrue(is_link(link), action)
                        self.assertTrue(os.path.isfile(os.path.join(outside, "keep.txt")), action)

    def test_an_older_copy_holding_a_file_where_the_checkout_has_a_directory_is_left_alone(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.fresh(shell, "older-type")
                dest = self.unmarked_copy(shell, project)
                path = os.path.join(dest, "examples")
                remove_tree(path)
                self.write_file(path, "mine\n")
                for action in ("install", "uninstall"):
                    code, output = self.claude(shell, action, project, copy=action == "install")
                    self.assert_refused(code, output)
                    self.assertTrue(os.path.isfile(path), action)

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "needs a directory its owner cannot read")
    def test_an_older_copy_with_an_unreadable_directory_is_left_alone(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.fresh(shell, "older-unreadable")
                dest = self.unmarked_copy(shell, project)
                locked = os.path.join(dest, "references")
                os.chmod(locked, 0)
                self.addCleanup(os.chmod, locked, stat.S_IRWXU)
                for action in ("install", "uninstall"):
                    code, output = self.claude(shell, action, project, copy=action == "install")
                    self.assert_refused(code, output)
                    self.assertTrue(os.path.isdir(locked), action)

    def test_a_relative_link_to_the_checkout_is_ours(self):
        """Followed from where the link is, not from where the installer runs."""
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "relative-target")
                checkout = self.make_checkout(base)
                self.write_file(os.path.join(checkout, "mine.txt"))
                project = os.path.join(base, "proj")
                dest = self.claude_dest(project)
                os.makedirs(os.path.dirname(dest))
                relative = os.path.relpath(checkout, os.path.dirname(dest))
                try:
                    os.symlink(relative, dest, target_is_directory=True)
                except OSError as exc:
                    self.skipTest("cannot make a directory symlink here: %s" % exc)
                code, output = self.claude(shell, "uninstall", project, root=checkout, cwd=base)
                self.assertEqual(code, 0, output)
                self.assertFalse(os.path.lexists(dest), output)
                self.assert_checkout_intact(checkout)

    def test_links_that_point_at_each_other_are_refused_not_followed_for_ever(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "cycle")
                one, two = os.path.join(base, "one"), os.path.join(base, "two")
                project = os.path.join(base, "proj")
                dest = self.claude_dest(project)
                os.makedirs(os.path.dirname(dest))
                try:
                    if os.name == "nt":
                        # A junction needs its target to resolve when it is
                        # made, so every link is made before the cycle closes.
                        os.makedirs(two)
                        make_dir_link(one, two)
                        make_dir_link(dest, one)
                        os.rmdir(two)
                        make_dir_link(two, one)
                    else:
                        os.symlink(two, one)
                        os.symlink(one, two)
                        os.symlink(one, dest)
                except (OSError, subprocess.CalledProcessError) as exc:
                    self.skipTest("cannot make a cycle of links here: %s" % exc)
                for action in ("install", "uninstall"):
                    code, output = self.claude(shell, action, project, copy=action == "install")
                    self.assert_refused(code, output)
                    self.assertTrue(is_link(dest), action)

    def test_a_full_copy_with_its_git_is_explained_not_removed(self):
        """What an earlier install.sh left under Git Bash, whose `ln -s` copies
        the whole checkout (#293). It looks like a clone, so it stays, and the
        refusal says how to remove it."""
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.fresh(shell, "full-copy")
                dest = self.unmarked_copy(shell, project)
                os.makedirs(os.path.join(dest, ".git"))
                for action in ("install", "uninstall"):
                    code, output = self.claude(shell, action, project, copy=action == "install")
                    self.assert_refused(code, output)
                    self.assertIn("made under Git Bash", output)
                    self.assertIn("rm -rf --" if shell.posix else "Remove-Item -LiteralPath", output)
                    self.assertTrue(os.path.isdir(os.path.join(dest, ".git")), action)

    def test_a_clone_of_something_else_gets_no_full_copy_note(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.fresh(shell, "other-clone")
                dest = self.claude_dest(project)
                os.makedirs(os.path.join(dest, ".git"))
                code, output = self.claude(shell, "install", project, copy=True)
                self.assert_refused(code, output)
                self.assertNotIn("made under Git Bash", output)

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

    #: The helpers that decide what is the installer's to remove, by script
    #: type, and how each script spells the start of one.
    OWNERSHIP_HELPERS = (
        (".sh", "%s() {", ("shell_quote", "explain_full_copy", "is_unmarked_copy")),
        (
            ".ps1",
            "function %s {",
            (
                "Get-LinkTarget",
                "Resolve-RealPath",
                "Test-UnmarkedCopy",
                "Test-CheckoutHas",
                "Get-FullCopyNote",
                "Stop-Refused",
                "Test-ReparsePoint",
                "Remove-Link",
            ),
        ),
    )

    def helper_body(self, text: str, opening: str) -> str:
        """The helper that starts with ``opening``, up to its closing brace at column 0."""
        match = re.search(r"^%s\n.*?^\}$" % re.escape(opening), text, re.MULTILINE | re.DOTALL)
        assert match is not None, opening
        return match.group(0)

    def test_the_scripts_agree_on_what_is_theirs_to_remove(self):
        """The install and uninstall scripts each carry their own copy of the
        ownership helpers; a fix to one copy alone would have the pair disagree
        about whether a directory may be removed."""
        for suffix, opening, names in self.OWNERSHIP_HELPERS:
            install = read_text(os.path.join(REPO_ROOT, "install", "install" + suffix))
            uninstall = read_text(os.path.join(REPO_ROOT, "install", "uninstall" + suffix))
            for name in names:
                with self.subTest(script=suffix, helper=name):
                    self.assertEqual(
                        self.helper_body(install, opening % name), self.helper_body(uninstall, opening % name)
                    )

    #: How the PowerShell scripts read and write AGENTS.md and the exclude
    #: file; each script carries its own copy, so that it runs on its own.
    TEXT_HELPERS = ("Read-TextFile", "Get-LineText", "Get-KeptLines", "Write-TextLines")

    def test_the_scripts_agree_on_how_a_text_file_round_trips(self):
        """A fix to one copy alone would have the installer and the
        uninstaller disagree on what a round trip keeps."""
        install = read_text(os.path.join(REPO_ROOT, "install", "install.ps1"))
        uninstall = read_text(os.path.join(REPO_ROOT, "install", "uninstall.ps1"))
        for name in self.TEXT_HELPERS:
            with self.subTest(helper=name):
                opening = "function %s {" % name
                self.assertEqual(self.helper_body(install, opening), self.helper_body(uninstall, opening))


BEGIN = "<!-- BEGIN dev-orchestra -->"
END = "<!-- END dev-orchestra -->"
#: Files the PowerShell installers must leave as they are, by label.
NOT_UTF8 = (
    ("cp932", "# 開発ルール\n".encode("cp932")),
    ("bom-cp932", codecs.BOM_UTF8 + "# 開発ルール\n".encode("cp932")),
    ("utf-16", "# 開発ルール\n".encode("utf-16")),
    ("utf-16-le", "# 開発ルール\n".encode("utf-16-le")),
    ("utf-32", "# 開発ルール\n".encode("utf-32")),
)
#: Text that Windows PowerShell's Get-Content, reading in the ANSI code page,
#: garbled, and that its `-Encoding utf8` then wrote back with a BOM (#292).
JAPANESE = "# 開発ルール\n日本語の説明\n"


@unittest.skipUnless(SHELLS, "needs sh (off Windows), pwsh or powershell")
class TestCodexInstallers(_InstallerCase):
    """Codex mode: the pointer block goes into AGENTS.md and comes out again,
    and what was there before keeps every byte, whatever the shell."""

    def codex(self, shell: Shell, action: str, project: Optional[str], **kwargs) -> Tuple[int, str]:
        return self.run_installer(shell, action, project, mode="codex", **kwargs)

    def read_bytes(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    def write_bytes(self, path: str, data: bytes) -> None:
        with open(path, "wb") as handle:
            handle.write(data)

    def test_a_japanese_agents_md_survives_a_round_trip(self):
        for shell in SHELLS:
            for newline in ("\n", "\r\n"):
                with self.subTest(shell=shell.name, newline=repr(newline)):
                    project = self.fresh(shell, "ja-%d" % len(newline))
                    agents = os.path.join(project, "AGENTS.md")
                    original = JAPANESE.replace("\n", newline).encode("utf-8")
                    self.write_bytes(agents, original)
                    for _ in range(2):
                        code, output = self.codex(shell, "install", project)
                        self.assertEqual(code, 0, output)
                    written = self.read_bytes(agents)
                    self.assertTrue(written.startswith(original), written[:80])
                    text = written.decode("utf-8")
                    self.assertEqual(text.count(BEGIN), 1, text)
                    self.assertEqual(text.count(END), 1, text)
                    self.assertIn("skills/dev-orchestra/SKILL.md", text)
                    if newline == "\r\n" and not shell.posix:
                        # The block ends its lines the way the file does.
                        self.assertNotIn(b"\n", written.replace(b"\r\n", b""), written)
                    elif newline == "\r\n":
                        # install.sh writes the block with LF line endings
                        # and leaves the CRLF lines around it as they were.
                        self.assertNotIn(b"\r", written[len(original) :], written)

                    code, output = self.codex(shell, "uninstall", project)
                    self.assertEqual(code, 0, output)
                    self.assertIn("Removed the pointer block", output)
                    self.assertEqual(self.read_bytes(agents), original)

    def test_a_last_line_without_a_newline_stays_without_one(self):
        """The block goes on a line of its own, ends without a newline in
        turn, and the round trip gives back the file as it was."""
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.fresh(shell, "no-eol")
                agents = os.path.join(project, "AGENTS.md")
                original = "a\n最後の行".encode()
                self.write_bytes(agents, original)
                for _ in range(2):
                    code, output = self.codex(shell, "install", project)
                    self.assertEqual(code, 0, output)
                written = self.read_bytes(agents)
                lines = written.decode("utf-8").splitlines()
                self.assertEqual(lines[:3], ["a", "最後の行", BEGIN], lines)
                self.assertEqual(lines.count(BEGIN), 1, lines)
                self.assertTrue(written.endswith(END.encode()), written[-40:])
                code, output = self.codex(shell, "uninstall", project)
                self.assertEqual(code, 0, output)
                self.assertEqual(self.read_bytes(agents), original)

    def test_a_block_amid_the_users_text(self):
        """Text added after the block stays where it is: a re-run moves the
        block to the end, and the uninstall takes out only the block."""
        for shell in SHELLS:
            for newline in ("\n", "\r\n"):
                with self.subTest(shell=shell.name, newline=repr(newline)):
                    project = self.fresh(shell, "amid-%d" % len(newline))
                    agents = os.path.join(project, "AGENTS.md")
                    head = ("head" + newline).encode()
                    tail = ("tail" + newline).encode()
                    self.write_bytes(agents, head)
                    code, output = self.codex(shell, "install", project)
                    self.assertEqual(code, 0, output)
                    block = self.read_bytes(agents)[len(head) :]
                    self.write_bytes(agents, head + block + tail)

                    code, output = self.codex(shell, "uninstall", project)
                    self.assertEqual(code, 0, output)
                    self.assertEqual(self.read_bytes(agents), head + tail)

                    self.write_bytes(agents, head + block + tail)
                    code, output = self.codex(shell, "install", project)
                    self.assertEqual(code, 0, output)
                    self.assertEqual(self.read_bytes(agents), head + tail + block)

    def test_an_empty_file_and_a_bom_alone_come_back_as_they_were(self):
        for shell in SHELLS:
            for label, original in (("empty", b""), ("bom-only", codecs.BOM_UTF8)):
                with self.subTest(shell=shell.name, file=label):
                    project = self.fresh(shell, label)
                    agents = os.path.join(project, "AGENTS.md")
                    self.write_bytes(agents, original)
                    code, output = self.codex(shell, "install", project)
                    self.assertEqual(code, 0, output)
                    written = self.read_bytes(agents)
                    self.assertTrue(written.startswith(original), written[:40])
                    # install.sh takes a BOM alone for a last line without a
                    # newline, and puts the block on the line after it.
                    first = written.decode("utf-8-sig").lstrip("\n").splitlines()[0]
                    self.assertEqual(first, BEGIN, written[:40])
                    code, output = self.codex(shell, "uninstall", project)
                    self.assertEqual(code, 0, output)
                    self.assertEqual(self.read_bytes(agents), original)

    def test_a_begin_with_no_end_is_left_alone(self):
        """What follows an unfinished block may be the user's own text, so
        neither script takes anything out."""
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.fresh(shell, "no-end")
                agents = os.path.join(project, "AGENTS.md")
                original = ("head\n%s\n## 自分のメモ\n" % BEGIN).encode()
                self.write_bytes(agents, original)
                for action in ("install", "uninstall"):
                    code, output = self.codex(shell, action, project)
                    self.assertEqual(code, 1, output)
                    self.assertIn("with no %s after it" % END, output)
                    self.assertEqual(self.read_bytes(agents), original, action)

    def test_the_shapes_of_an_unfinished_block(self):
        """A finished block followed by an unfinished one, and an END before
        the BEGIN, are unfinished too; only the BEGIN after the last END counts."""
        shapes = {
            "closed-then-open": "head\n%s\nold\n%s\nmid\n%s\n## 自分のメモ\n" % (BEGIN, END, BEGIN),
            "end-before-begin": "head\n%s\nmid\n%s\n## 自分のメモ\n" % (END, BEGIN),
        }
        for shell in SHELLS:
            for label, text in shapes.items():
                with self.subTest(shell=shell.name, shape=label):
                    project = self.fresh(shell, label)
                    agents = os.path.join(project, "AGENTS.md")
                    original = text.encode()
                    self.write_bytes(agents, original)
                    for action in ("install", "uninstall"):
                        code, output = self.codex(shell, action, project)
                        self.assertEqual(code, 1, output)
                        self.assertIn("with no %s after it" % END, output)
                        self.assertEqual(self.read_bytes(agents), original, action)

    def test_the_shapes_of_a_finished_block(self):
        """An END with no BEGIN is not a block, and a BEGIN and END on one line
        are one: uninstall leaves the first and takes out the second."""
        shapes = {
            "end-only": ("head\n%s\ntail\n" % END, "head\n%s\ntail\n" % END),
            "one-line": ("head\n%s pointer %s\ntail\n" % (BEGIN, END), "head\ntail\n"),
        }
        for shell in SHELLS:
            for label, (text, expected) in shapes.items():
                with self.subTest(shell=shell.name, shape=label):
                    project = self.fresh(shell, label)
                    agents = os.path.join(project, "AGENTS.md")
                    self.write_bytes(agents, text.encode())
                    code, output = self.codex(shell, "uninstall", project)
                    self.assertEqual(code, 0, output)
                    self.assertEqual(self.read_bytes(agents), expected.encode())

    def test_a_utf8_bom_is_kept(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = self.fresh(shell, "bom")
                agents = os.path.join(project, "AGENTS.md")
                original = codecs.BOM_UTF8 + JAPANESE.encode()
                self.write_bytes(agents, original)
                for _ in range(2):
                    code, output = self.codex(shell, "install", project)
                    self.assertEqual(code, 0, output)
                written = self.read_bytes(agents)
                self.assertTrue(written.startswith(original), written[:40])
                self.assertEqual(written.count(codecs.BOM_UTF8), 1, written[:40])
                code, output = self.codex(shell, "uninstall", project)
                self.assertEqual(code, 0, output)
                self.assertEqual(self.read_bytes(agents), original)

    def test_a_new_agents_md_has_no_bom(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                project = os.path.join(self.fresh(shell, "new"), "not", "there")
                code, output = self.codex(shell, "install", project)
                self.assertEqual(code, 0, output)
                written = self.read_bytes(os.path.join(project, "AGENTS.md"))
                self.assertFalse(written.startswith(codecs.BOM_UTF8), written[:20])
                self.assertTrue(written.decode("utf-8").startswith(BEGIN), written[:80])

    def test_a_relative_project_from_another_directory(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "relative")
                project = os.path.join(base, "proj")
                os.makedirs(project)
                elsewhere = os.path.join(base, "elsewhere")
                os.makedirs(elsewhere)
                relative = os.path.relpath(project, elsewhere)
                code, output = self.codex(shell, "install", relative, cwd=elsewhere)
                self.assertEqual(code, 0, output)
                self.assertIn(BEGIN, read_text(os.path.join(project, "AGENTS.md")))
                self.assertFalse(os.path.exists(os.path.join(elsewhere, "AGENTS.md")))
                code, output = self.codex(shell, "uninstall", relative, cwd=elsewhere)
                self.assertEqual(code, 0, output)
                self.assertNotIn(BEGIN, read_text(os.path.join(project, "AGENTS.md")))

    def test_the_global_block_goes_into_codex_home(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.name):
                base = self.fresh(shell, "global")
                codex_home = os.path.join(base, "codex")
                env = dict(os.environ, HOME=self.home, USERPROFILE=self.home, CODEX_HOME=codex_home)
                agents = os.path.join(codex_home, "AGENTS.md")
                code, output = self.codex(shell, "install", None, env=env)
                self.assertEqual(code, 0, output)
                self.assertIn(BEGIN, read_text(agents))
                code, output = self.codex(shell, "uninstall", None, env=env)
                self.assertEqual(code, 0, output)
                self.assertNotIn(BEGIN, read_text(agents))
                self.assertFalse(os.path.exists(os.path.join(self.home, ".codex")), output)

    def test_powershell_leaves_a_file_that_is_not_utf8_alone(self):
        """Rewriting it would replace every byte it cannot read, or turn it
        into UTF-8. A UTF-8 BOM in front does not make it UTF-8, and UTF-16
        is what Windows PowerShell's `>` writes."""
        for shell in [s for s in SHELLS if not s.posix]:
            for label, original in NOT_UTF8:
                with self.subTest(shell=shell.name, encoding=label):
                    project = self.fresh(shell, "not-utf8-" + label)
                    agents = os.path.join(project, "AGENTS.md")
                    self.write_bytes(agents, original)
                    for action in ("install", "uninstall"):
                        code, output = self.codex(shell, action, project)
                        self.assertEqual(code, 1, output)
                        self.assertIn("is not UTF-8", output)
                        self.assertEqual(self.read_bytes(agents), original, action)


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

    def test_a_claude_ln_that_copies_instead_is_replaced_by_the_payload(self):
        """Git Bash's `ln -s` copies the whole checkout and exits 0 (#293)."""
        shell = POSIX_SHELLS[0]
        stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(stubs)
        ln = os.path.join(stubs, "ln")
        with open(ln, "w", encoding="utf-8") as handle:
            # Called as `ln -s <root> <dest>`: stand in for the copy with a .git.
            handle.write('#!/bin/sh\nmkdir -p "$3/.git"\nexit 0\n')
        os.chmod(ln, os.stat(ln).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = dict(os.environ, HOME=self.home, PATH=stubs + os.pathsep + os.environ.get("PATH", ""))

        project = self.git_project(self.tmp)
        dest = os.path.join(project, ".claude", "skills", SKILL_NAME)
        code, output = self.run_installer(shell, "install", project, env=env, mode="claude")
        self.assertEqual(code, 0, output)
        self.assertIn("ln -s made a copy, not a link", output)
        self.assertNotIn("Linked", output)
        self.assertIn("Re-run this installer", output)
        self.assertFalse(is_link(dest))
        self.assertFalse(os.path.exists(os.path.join(dest, ".git")))
        for relative in ("plugin.json", "skills/dev-orchestra/SKILL.md", SENTINEL):
            self.assertTrue(os.path.isfile(os.path.join(dest, relative)), relative)
        self.assertEqual(self.exclude_lines(project).count(CLAUDE_ENTRY), 1)
        code, output = self.run_installer(shell, "uninstall", project, env=env, mode="claude")
        self.assertEqual(code, 0, output)
        self.assertNotIn(SKILL_NAME, os.listdir(os.path.dirname(dest)))


if __name__ == "__main__":
    unittest.main()
