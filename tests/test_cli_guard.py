"""The suite never starts a real AI CLI, with or without DEV_ORCHESTRA_TEST_ASSUME_NO_CLI.

Each test writes a fake ``claude`` that leaves a marker file when it runs, and
drives a path that used to start whatever ``claude`` it was given: the marker
must not appear unless the test allowed that fake by name. The shapes a
launcher may give a command -- ``node`` and the script, ``cmd.exe /c``, a shell
list -- are judged without starting anything, since a guard that missed one
would start the real CLI on a machine that has it.

A refusal is checked by the fact of it and the CLI it names, never by the
exact command: how a CLI is launched may change, through cmd.exe or node.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
import unittest
from typing import List, Sequence
from unittest import mock

from helpers import (
    ASSUME_NO_CLI,
    CLI_GUARD_DIR,
    IsolatedCase,
    cli_guard,
    make_dir_link,
    refused_cli_launches,
    remove_link,
)

from orchestrator import execution
from orchestrator.providers.claude import ClaudeProvider

REFUSAL = "never start a real AI CLI"


def mentions(command: Sequence[str], name: str) -> bool:
    return name in " ".join(command).lower()


class GuardCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.marker = os.path.join(self.tmp, "ran")
        self.fake = self.write_fake("claude")
        self.set_env("PATH", self.bin + os.pathsep + os.environ.get("PATH", ""))

    def write_fake(self, name: str) -> str:
        if os.name == "nt":
            path = os.path.join(self.bin, name + ".cmd")
            body = '@echo off\r\necho ran>"%s"\r\necho %s 9.9.9\r\n' % (self.marker, name)
        else:
            path = os.path.join(self.bin, name)
            body = "#!/bin/sh\necho ran > '%s'\necho %s 9.9.9\n" % (self.marker, name)
        self.write_file(path, body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def write_file(self, path: str, body: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
        return path

    def assert_not_run(self) -> None:
        self.assertFalse(os.path.exists(self.marker), "a provider CLI was started")

    def assert_refused(self, call) -> None:
        with self.assertRaises(FileNotFoundError) as caught:
            call()
        self.assertIn(REFUSAL, str(caught.exception))
        self.assert_not_run()


class TestARealCliIsNeverStarted(GuardCase):
    def test_the_provider_clis_are_hidden_without_the_old_flag(self):
        fake = self.fake
        seen = {}

        class Probe(IsolatedCase):
            def test_probe(self):
                provider = ClaudeProvider(fake)
                seen["which"] = provider.which()
                seen["installed"] = provider.detect().installed

        saved = os.environ.pop(ASSUME_NO_CLI, None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, ASSUME_NO_CLI, saved)
        result = unittest.TestResult()
        Probe("test_probe").run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertEqual(seen, {"which": None, "installed": False})
        self.assert_not_run()

    def test_reading_the_help_is_refused(self):
        # `_help_output` never asks `which`: this is what ran `claude --help`
        # for real even with the flag set.
        before = len(refused_cli_launches())
        self.assertIsNone(ClaudeProvider(self.fake)._help_output("--help"))
        self.assertIsNone(ClaudeProvider()._help_output("--help"))
        self.assert_not_run()
        new = refused_cli_launches()[before:]
        self.assertEqual(len(new), 2, new)
        self.assertTrue(all(mentions(command, "claude") for command in new), new)

    def test_the_refusals_are_read_from_the_installed_guard(self):
        installed = getattr(subprocess.Popen.__init__, "refused", None)
        self.assertIsNotNone(installed)
        self.assertEqual(refused_cli_launches(), list(installed or []))

    def test_a_run_is_refused_as_if_nothing_were_installed(self):
        for command in ([self.fake, "-p"], ["claude", "-p"], ["codex", "exec"], ["agy.exe", "models"]):
            with self.subTest(command=command):
                outcome = execution.execute(command, cwd=self.project, timeout=30)
                self.assertEqual(outcome.exit_code, execution.EXIT_SPAWN_FAILED)
                self.assertIn(REFUSAL, outcome.stderr)
        self.assert_not_run()

    def test_a_shell_command_line_is_refused_too(self):
        self.assert_refused(lambda: subprocess.run("claude --version", shell=True, check=False))
        line = 'echo hi && "%s" --version' % self.fake
        self.assert_refused(lambda: subprocess.run(line, shell=True, check=False))

    @unittest.skipUnless(os.name == "nt", "cmd.exe")
    def test_a_batch_file_behind_cmd_exe_is_refused(self):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        line = '"%s" /d /v:off /s /c ""%s" "--help""' % (comspec, self.fake)
        self.assert_refused(lambda: subprocess.run(line, check=False))
        self.assert_refused(lambda: subprocess.run([comspec, "/c", self.fake, "--help"], check=False))

    def test_the_os_launchers_are_refused(self):
        self.assert_refused(lambda: os.system('"%s" --version' % self.fake))
        self.assert_refused(lambda: os.spawnv(os.P_WAIT, self.fake, [self.fake, "--version"]))
        if hasattr(os, "posix_spawn"):
            self.assert_refused(lambda: os.posix_spawn(self.fake, [self.fake, "--version"], dict(os.environ)))

    def test_a_process_the_test_starts_refuses_as_well(self):
        log = os.path.join(self.tmp, "refused.jsonl")
        self.set_env(cli_guard.REFUSED_LOG_ENV, log)
        self.assertIn(CLI_GUARD_DIR, os.environ["PYTHONPATH"].split(os.pathsep))
        code = (
            "import subprocess, sys\n"
            "try:\n"
            "    subprocess.run([sys.argv[1], '--version'])\n"
            "except FileNotFoundError:\n"
            "    sys.exit(3)\n"
        )
        completed = subprocess.run([sys.executable, "-c", code, self.fake], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assert_not_run()
        with open(log, encoding="utf-8") as handle:
            commands = [json.loads(line)["command"] for line in handle]
        self.assertEqual(len(commands), 1, commands)
        self.assertTrue(mentions(commands[0], "claude"), commands)

    def test_other_programs_still_start(self):
        completed = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
        self.assertEqual(completed.stdout.strip(), "ok")


class TestTheShapesOfALaunch(GuardCase):
    """Judged, not started: each would start the real CLI if the guard missed it."""

    def refused(self, args, shell: bool = False) -> bool:
        return cli_guard.refusal(args, shell) is not None

    def test_node_running_a_provider_package(self):
        script = os.path.join(self.tmp, "lib", "node_modules", "@anthropic-ai", "claude-code", "cli.js")
        for args in (
            ["node", script, "-p"],
            ["node.exe", "--no-warnings", script],
            [os.path.join(self.tmp, "node.exe"), script],
            ["node", os.path.join(self.tmp, "node_modules", "@openai", "codex", "bin", "codex.js")],
            ["bun", "run", script],
            ["deno", "run", "-A", script],
            ["node", os.path.join(self.tmp, "claude.js")],
        ):
            with self.subTest(args=args):
                self.assertTrue(self.refused(args))
        self.assertFalse(self.refused(["node", os.path.join(self.tmp, "server.js")]))

    def test_package_runners(self):
        for args in (
            ["npx", "-y", "@anthropic-ai/claude-code", "-p"],
            ["npx", "@openai/codex@latest", "exec"],
            ["npx", "claude"],
            ["pnpm", "dlx", "@anthropic-ai/claude-code"],
        ):
            with self.subTest(args=args):
                self.assertTrue(self.refused(args))
        self.assertFalse(self.refused(["npx", "prettier", "--check", "."]))

    def test_cmd_exe(self):
        for line in (
            r'"C:\Windows\System32\cmd.exe" /d /v:off /s /c ""C:\npm\claude.cmd" "--help""',
            r'cmd /c "C:\npm\claude.cmd" --help',
            r"cmd.exe /k echo hi & codex exec",
            r"cmd /c echo hi ^& && agy models",
        ):
            with self.subTest(line=line):
                self.assertIsNotNone(cli_guard._judge_windows_line(line, 0))
        self.assertTrue(self.refused(["cmd", "/c", "claude", "--version"]))
        self.assertTrue(self.refused(["cmd.exe", "/d", "/s", "/c", '"claude --version"']))
        self.assertFalse(self.refused(["cmd", "/c", "echo", "hi"]))

    def test_posix_shells_and_prefixes(self):
        for args, shell in (
            (["sh", "-c", "echo hi && claude --version"], False),
            (["bash", "-lc", "FOO=1 codex exec"], False),
            (["sh", "-c", "true; agy models | cat"], False),
            (["env", "FOO=1", "claude"], False),
            (["env", "-i", "PATH=/bin", "nohup", "codex"], False),
            (["FOO=1", "agy", "models"], False),
            ("echo hi && claude", True),
            ("echo hi || exec codex", True),
            (["sh", "-c", "echo hi & claude"], False),
        ):
            with self.subTest(args=args):
                self.assertTrue(self.refused(args, shell))
        for args, shell in (
            (["sh", "-c", "echo claude"], False),
            ("git status", True),
            (["env", "git"], False),
        ):
            with self.subTest(args=args):
                self.assertFalse(self.refused(args, shell))

    def test_powershell(self):
        encoded = base64.b64encode("Write-Host hi; claude -p".encode("utf-16-le")).decode("ascii")
        for args in (
            ["pwsh", "-NoProfile", "-Command", "Write-Host hi; & 'codex' exec"],
            ["powershell.exe", "-c", "claude --version"],
            ["pwsh", "-File", r"C:\npm\claude.ps1", "-p"],
            ["pwsh", "-EncodedCommand", encoded],
        ):
            with self.subTest(args=args):
                self.assertTrue(self.refused(args))
        self.assertFalse(self.refused(["pwsh", "-Command", "Get-ChildItem"]))

    @unittest.skipUnless(os.name == "nt", "CreateProcess reads an unquoted path with spaces")
    def test_an_unquoted_path_with_spaces(self):
        self.assertTrue(self.refused(r"C:\Program Files\tools\claude.exe --help"))
        self.assertFalse(self.refused(r"C:\Program Files\Git\cmd\git.exe status"))

    def test_an_allowed_fake_behind_a_launcher_runs(self):
        self.allow_cli(self.fake)
        line = '"%s" /d /v:off /s /c ""%s" "--help""' % (os.environ.get("COMSPEC") or "cmd.exe", self.fake)
        self.assertIsNone(cli_guard._judge_windows_line(line, 0))
        self.assertFalse(self.refused(["sh", "-c", "'%s' --help" % self.fake]))


class TestTheInstalledLocations(GuardCase):
    """An npm install found on PATH: the shims, the script they run, its package."""

    def setUp(self):
        super().setUp()
        self.npm = os.path.join(self.tmp, "npm")
        self.package = os.path.join(self.npm, "node_modules", "cli-wrapper")
        self.script = self.write_file(os.path.join(self.package, "bin", "run.js"), "// the CLI\n")
        self.write_file(
            os.path.join(self.npm, "claude.cmd"),
            "@ECHO off\r\nSET dp0=%~dp0\r\n"
            '"%dp0%\\node.exe" "%dp0%\\node_modules\\cli-wrapper\\bin\\run.js" %*\r\n',
        )
        self.write_file(
            os.path.join(self.npm, "codex"),
            '#!/bin/sh\nbasedir=$(dirname "$0")\n'
            'exec node "$basedir/node_modules/cli-wrapper/bin/run.js" "$@"\n',
        )
        self.found = cli_guard.discover(self.npm, ".EXE;.CMD")

    def test_the_shims_and_what_they_run_are_found(self):
        self.assertIn(cli_guard.normalised(os.path.join(self.npm, "claude.cmd")), self.found.files)
        self.assertIn(cli_guard.normalised(os.path.join(self.npm, "codex")), self.found.files)
        self.assertIn(cli_guard.normalised(self.script), self.found.files)
        self.assertEqual(self.found.packages, {cli_guard.normalised(self.package)})

    def test_anything_that_runs_them_is_refused(self):
        other = os.path.join(self.package, "lib", "main.js")
        commands: List[List[str]] = [
            ["node", self.script],
            [sys.executable, other],
            ["sh", "-c", "node '%s'" % self.script],
            ["env", "node", self.script],
        ]
        with mock.patch.object(cli_guard, "_locations", self.found):
            for command in commands:
                with self.subTest(command=command):
                    self.assertIsNotNone(cli_guard.refusal(command))
            self.assertIsNone(cli_guard.refusal(["node", os.path.join(self.tmp, "server.js")]))


class TestAnAllowedFake(GuardCase):
    def test_a_fake_named_by_its_path_runs(self):
        self.allow_cli(self.fake)
        output = ClaudeProvider(self.fake)._help_output("--help")
        self.assertIn("claude 9.9.9", output or "")
        self.assertTrue(os.path.exists(self.marker))

    def test_its_bare_name_stays_refused(self):
        # Asked of the guard itself: a launcher may turn the bare name into
        # the path it finds first on PATH, which here is the allowed fake.
        self.allow_cli(self.fake)
        self.assertIsNotNone(cli_guard.refusal(["claude", "--help"]))
        self.assertIsNotNone(cli_guard.refusal("claude --help", shell=True))

    def test_the_allowance_ends_with_the_test(self):
        self.allow_cli(self.fake)
        self.doCleanups()
        self.assertNotIn(cli_guard.normalised(self.fake), cli_guard.allowed())


class TestLoadingAsSitecustomize(GuardCase):
    def test_a_second_spelling_of_the_directory_does_not_loop(self):
        link = os.path.join(self.tmp, "guard-link")
        make_dir_link(link, CLI_GUARD_DIR)
        # Removed here, before the temporary directory is, so nothing walks into it.
        try:
            other = os.path.join(self.tmp, "other")
            count = os.path.join(self.tmp, "loaded")
            self.write_file(
                os.path.join(other, "sitecustomize.py"),
                "with open(%r, 'a') as handle:\n    handle.write('x')\n" % count,
            )
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([link, CLI_GUARD_DIR, other]))
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import subprocess; print(subprocess.Popen.__init__.refuses_provider_clis)",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertEqual(completed.stdout.strip(), "True", completed.stderr)
            with open(count, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "x")
        finally:
            remove_link(link)


if __name__ == "__main__":
    unittest.main()
