"""A CLI npm installed as a ``.cmd`` is started the way ``doctor`` found it (#269).

``shutil.which`` finds ``claude.cmd`` through PATHEXT, but ``Popen`` given
the bare name only looks for ``.exe``: ``doctor`` said installed while
``--version`` could not run and every run exited 126. A batch file runs under
cmd.exe, which reads ``&``, ``%VAR%`` and quotes in its arguments as its own
syntax, so it is never handed an argument it could read that way.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

from helpers import IsolatedCase

from orchestrator import execution
from orchestrator.providers import base as provider_base
from orchestrator.providers.claude import ClaudeProvider

NPM_SHIM = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@scope\cli\cli.js" %*
"""

OLD_NPM_SHIM = r"""@IF EXIST "%~dp0\node.exe" (
  "%~dp0\node.exe"  "%~dp0\node_modules\cli\bin\cli.js" %*
) ELSE (
  @SETLOCAL
  @SET PATHEXT=%PATHEXT:;.JS;=;%
  node  "%~dp0\node_modules\cli\bin\cli.js" %*
)
"""

EXE_SHIM = '@ECHO off\r\n"%dp0%\\node_modules\\tool\\bin\\tool.exe"   %*\r\n'

#: A batch file that is no npm shim: it shows what cmd.exe handed it.
ECHO_BATCH = "@echo off\r\necho ARGS:%*\r\necho VERSION 1.2.3\r\n"


class _TempDir(IsolatedCase):
    def put(self, relative: str, content: str = "") -> str:
        path = os.path.join(self.tmp, "bin", relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        return path


class TestHowABatchFileIsLaunched(_TempDir):
    """The decisions, on any platform: nothing here is started."""

    def test_an_npm_shim_runs_node_and_its_script_directly(self):
        shim = self.put("claude.cmd", NPM_SHIM)
        node = self.put("node.exe")
        script = self.put(os.path.join("node_modules", "@scope", "cli", "cli.js"))
        launch = execution.windows_launch(["claude", "-p", 'x"&y'], shim)
        self.assertEqual(launch, [node, script, "-p", 'x"&y'])

    def test_an_older_npm_shim_is_read_too(self):
        shim = self.put("cli.cmd", OLD_NPM_SHIM)
        node = self.put("node.exe")
        script = self.put(os.path.join("node_modules", "cli", "bin", "cli.js"))
        self.assertEqual(execution.windows_launch(["cli", "a"], shim), [node, script, "a"])

    def test_a_shim_without_a_node_beside_it_uses_the_one_on_path(self):
        shim = self.put("claude.cmd", NPM_SHIM)
        script = self.put(os.path.join("node_modules", "@scope", "cli", "cli.js"))
        node = os.path.join(self.tmp, "nodejs", "node.exe")
        with mock.patch.object(execution, "find_program", return_value=node) as which:
            launch = execution.windows_launch(["claude"], shim)
        which.assert_called_once_with("node")
        self.assertEqual(launch, [node, script])

    def test_a_node_that_is_itself_a_batch_file_is_not_started_directly(self):
        """A version manager's ``node.cmd`` would skip cmd.exe's quoting."""
        shim = self.put("claude.cmd", NPM_SHIM)
        self.put(os.path.join("node_modules", "@scope", "cli", "cli.js"))
        node = os.path.join(self.tmp, "nodejs", "node.cmd")
        with mock.patch.object(execution, "find_program", return_value=node):
            launch = execution.windows_launch(["claude", "x&y"], shim)
        self.assertIsInstance(launch, str)
        self.assertIn('"%s" "x&y"' % shim, launch)

    def test_a_shim_for_an_exe_runs_the_exe(self):
        shim = self.put("tool.cmd", EXE_SHIM)
        exe = self.put(os.path.join("node_modules", "tool", "bin", "tool.exe"))
        self.assertEqual(execution.windows_launch(["tool", "--version"], shim), [exe, "--version"])

    def test_a_shim_whose_target_is_missing_is_left_to_cmd(self):
        shim = self.put("claude.cmd", NPM_SHIM)
        launch = execution.windows_launch(["claude", "a"], shim)
        self.assertIsInstance(launch, str)

    def test_any_other_batch_file_runs_under_cmd_with_every_argument_quoted(self):
        batch = self.put("fakecli.CMD", ECHO_BATCH)
        launch = execution.windows_launch(["fakecli", "plain", "a b", "x&y|z", "C:\\dir\\", ""], batch)
        self.assertIsInstance(launch, str)
        self.assertIn(' /d /v:off /s /c ""%s" "plain" "a b" "x&y|z" "C:\\dir\\\\" """' % batch, launch)

    def test_cmd_syntax_in_an_argument_is_refused_without_repeating_it(self):
        batch = self.put("fakecli.cmd", ECHO_BATCH)
        for argument in ('say "hi"', "%PATH%", "wow!", "two\nlines", "cr\rhere"):
            with self.subTest(argument=argument):
                with self.assertRaises(execution.LaunchRefused) as caught:
                    execution.windows_launch(["fakecli", "sk-secret-" + argument], batch)
                message = str(caught.exception)
                self.assertIn("fakecli.cmd", message)
                self.assertIn("batch file", message)
                self.assertNotIn("sk-secret", message)

    def test_a_program_that_is_not_a_batch_file_runs_from_the_path_found(self):
        exe = self.put("claude.exe")
        self.assertEqual(execution.windows_launch(["claude", "-p"], exe), [exe, "-p"])

    def test_a_name_nothing_found_is_left_as_it_was(self):
        self.assertEqual(execution.windows_launch(["nothere", "x"], None), ["nothere", "x"])

    @unittest.skipIf(os.name == "nt", "POSIX only")
    def test_posix_launches_are_unchanged(self):
        self.assertEqual(execution.launchable(["claude", "a&b"]), ["claude", "a&b"])


class TestThePathSearch(_TempDir):
    """``shutil.which`` on Windows looks in the current directory first; this does not."""

    def test_only_absolute_path_entries_are_searched(self):
        here = os.path.join(self.tmp, "repo")
        installed = os.path.join(self.tmp, "npm")
        for directory in (here, installed):
            os.makedirs(directory)
            with open(os.path.join(directory, "claude.cmd"), "w") as handle:
                handle.write("")
        os.chdir(here)
        # ".", "" and a relative entry all name the repository under review.
        path = ";".join([".", "", "repo", installed])
        found = execution.search_path("claude", path, ".com;.exe;.bat;.cmd")
        self.assertEqual(found, os.path.join(installed, "claude.cmd"))
        self.assertTrue(os.path.isabs(found or ""))
        self.assertIsNone(execution.search_path("claude", ".;repo", ".com;.exe;.bat;.cmd"))

    def test_pathext_is_tried_in_order_and_a_named_extension_is_kept(self):
        installed = os.path.join(self.tmp, "npm")
        os.makedirs(installed)
        for name in ("codex.cmd", "codex.exe"):
            with open(os.path.join(installed, name), "w") as handle:
                handle.write("")
        self.assertEqual(
            execution.search_path("codex", installed, ".exe;.cmd"), os.path.join(installed, "codex.exe")
        )
        self.assertEqual(
            execution.search_path("codex.cmd", installed, ".exe;.cmd"), os.path.join(installed, "codex.cmd")
        )

    def touch(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("")
        return os.path.realpath(path)

    def test_a_name_with_a_directory_is_looked_up_where_it_says_not_on_path(self):
        tools = self.touch("tools", "claude.cmd")
        self.touch("npm", "tools", "claude.cmd")  # what a PATH search would find
        npm = os.path.join(self.tmp, "npm")
        absolute = os.path.join(self.tmp, "tools", "claude")
        # No absolute entry on PATH at all: the file is still where it says.
        found = execution.search_path(absolute, ".", ".exe;.cmd")
        self.assertEqual(os.path.realpath(found or ""), tools)
        os.chdir(self.tmp)
        found = execution.search_path(os.path.join("tools", "claude"), npm, ".exe;.cmd")
        self.assertTrue(os.path.isabs(found or ""))
        self.assertEqual(os.path.realpath(found or ""), tools)
        self.assertIsNone(execution.search_path(os.path.join("missing", "claude"), npm, ".exe;.cmd"))

    def test_launch_starts_the_file_the_search_found(self):
        exe = self.touch("tools", "claude.exe")
        os.chdir(self.tmp)
        relative = os.path.join("tools", "claude")
        with (
            mock.patch.object(execution, "IS_WINDOWS", True),
            mock.patch.dict(os.environ, {"PATHEXT": ".exe;.cmd"}),
        ):
            found = execution.find_program(relative)
            launch = execution.launchable([relative, "-p"])
            with self.assertRaises(FileNotFoundError):
                execution.launchable([os.path.join("missing", "claude")])
        self.assertEqual(os.path.realpath(found or ""), exe)
        self.assertEqual(launch, [found, "-p"])

    def test_a_name_with_a_directory_and_its_extension_is_taken_as_it_is(self):
        tools = self.touch("tools", "claude.cmd")
        self.touch("tools", "claude.cmd.exe")  # not tried: the name has its extension
        os.chdir(self.tmp)
        found = execution.search_path(os.path.join("tools", "claude.cmd"), ".", ".exe;.cmd")
        self.assertTrue(os.path.isabs(found or ""))
        self.assertEqual(os.path.realpath(found or ""), tools)

    def test_a_directory_where_the_name_points_is_not_the_program(self):
        os.makedirs(os.path.join(self.tmp, "tools", "claude"))
        os.makedirs(os.path.join(self.tmp, "tools", "codex.cmd"))
        os.chdir(self.tmp)
        self.assertIsNone(execution.search_path(os.path.join("tools", "claude"), ".", ".exe;.cmd"))
        self.assertIsNone(execution.search_path(os.path.join("tools", "codex.cmd"), ".", ".exe;.cmd"))
        # Beside the directory, the file with an extension is the one found.
        cmd = self.touch("tools", "claude.cmd")
        found = execution.search_path(os.path.join("tools", "claude"), ".", ".exe;.cmd")
        self.assertEqual(os.path.realpath(found or ""), cmd)

    def test_a_qualified_cmd_is_launched_as_the_file_the_search_found(self):
        """Not an npm shim: it runs under cmd.exe, and that line names the
        file ``doctor`` would report, by its absolute path."""
        self.touch("tools", "claude.cmd")
        os.chdir(self.tmp)
        for name in (os.path.join("tools", "claude"), os.path.join("tools", "claude.cmd")):
            with self.subTest(name=name):
                with (
                    mock.patch.object(execution, "IS_WINDOWS", True),
                    mock.patch.dict(os.environ, {"PATHEXT": ".exe;.cmd"}),
                ):
                    found = execution.find_program(name)
                    launch = execution.launchable([name, "-p"])
                self.assertTrue(os.path.isabs(found or ""))
                self.assertEqual(launch, execution.batch_command_line(found or "", ["-p"]))
                self.assertIn('"%s" "-p"' % found, launch)


@unittest.skipUnless(os.name == "nt", "a .cmd only runs on Windows")
class TestTheCurrentDirectoryOnWindows(_TempDir):
    def test_a_cli_committed_to_the_repository_is_never_started(self):
        marker = os.path.join(self.tmp, "ran")
        with open(os.path.join(self.project, "planted.cmd"), "w", newline="") as handle:
            handle.write('@echo off\r\necho planted> "%s"\r\n' % marker)
        self.set_env("PATH", os.path.join(self.tmp, "empty"))
        self.assertIsNone(execution.find_program("planted"))
        outcome = execution.execute(["planted", "--version"], self.project, timeout=60)
        self.assertEqual(outcome.exit_code, execution.EXIT_SPAWN_FAILED)
        self.assertIn("not found on PATH", outcome.stderr)
        self.assertFalse(os.path.exists(marker))

    def test_doctor_does_not_find_it_either(self):
        with open(os.path.join(self.project, "planted.cmd"), "w") as handle:
            handle.write("@echo off\r\n")
        self.set_env("PATH", os.path.join(self.tmp, "empty"))
        self.assertIsNone(provider_base.Provider("planted").which())


@unittest.skipUnless(os.name == "nt", "a .cmd only runs on Windows")
class TestABatchFileOnWindows(_TempDir):
    def setUp(self):
        super().setUp()
        self.batch = self.put("fakecli.cmd", ECHO_BATCH)
        self.set_env("PATH", os.path.dirname(self.batch) + os.pathsep + os.environ.get("PATH", ""))
        provider_base.clear_discovery_cache()

    def test_a_bare_name_found_through_pathext_runs(self):
        outcome = execution.execute(["fakecli", "a b", "x&echo INJECTED", "p|q"], self.project, timeout=60)
        self.assertEqual(outcome.exit_code, 0, outcome.stderr)
        lines = outcome.stdout.splitlines()
        self.assertEqual(lines[0], 'ARGS:"a b" "x&echo INJECTED" "p|q"')
        self.assertNotIn("INJECTED", lines[1:])

    def test_the_program_behind_the_batch_file_gets_each_argument_whole(self):
        relay = self.put(
            "relay.cmd",
            '@"%s" -c "import sys, json; print(json.dumps(sys.argv[1:]))" %%*\r\n' % sys.executable,
        )
        arguments = ["plain", "a b", "x&y", "p|q", "<in>", "(paren)", "caret^", "C:\\dir\\", "", "tab\there"]
        outcome = execution.execute([relay, *arguments], self.project, timeout=60)
        self.assertEqual(outcome.exit_code, 0, outcome.stderr)
        self.assertEqual(json.loads(outcome.stdout.strip().splitlines()[-1]), arguments)

    def test_an_argument_cmd_would_expand_is_refused_and_nothing_runs(self):
        marker = os.path.join(self.tmp, "ran")
        outcome = execution.execute(["fakecli", '"&echo x>"%s' % marker], self.project, timeout=60)
        self.assertEqual(outcome.exit_code, execution.EXIT_SPAWN_FAILED)
        self.assertIn("batch file", outcome.stderr)
        self.assertFalse(os.path.exists(marker))

    def test_the_version_doctor_reads_is_the_installed_one(self):
        provider = ClaudeProvider("fakecli")
        # What `which()` finds unpatched; the suite may hide the built-in CLIs from it.
        self.assertEqual(
            os.path.normcase(execution.find_program("fakecli") or ""), os.path.normcase(self.batch)
        )
        version, error = provider.version()
        self.assertIsNone(error)
        self.assertEqual(version, 'ARGS:"--version"')


if __name__ == "__main__":
    unittest.main()
