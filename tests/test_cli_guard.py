"""The suite never starts a real AI CLI, with or without DEV_ORCHESTRA_TEST_ASSUME_NO_CLI.

Each test writes a fake ``claude`` that leaves a marker file when it runs, and
drives a path that used to start whatever ``claude`` it was given: the marker
must not appear unless the test allowed that fake by name.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import unittest

from helpers import ASSUME_NO_CLI, CLI_GUARD_DIR, IsolatedCase, cli_guard, refused_cli_launches

from orchestrator import execution
from orchestrator.providers.claude import ClaudeProvider


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
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def assert_not_run(self) -> None:
        self.assertFalse(os.path.exists(self.marker), "a provider CLI was started")


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
        self.assertEqual(refused_cli_launches()[before:], [[self.fake, "--help"], ["claude", "--help"]])

    def test_a_run_is_refused_as_if_nothing_were_installed(self):
        for command in ([self.fake, "-p"], ["claude", "-p"], ["codex", "exec"], ["agy.exe", "models"]):
            with self.subTest(command=command):
                outcome = execution.execute(command, cwd=self.project, timeout=30)
                self.assertEqual(outcome.exit_code, execution.EXIT_SPAWN_FAILED)
                self.assertIn("never start a real AI CLI", outcome.stderr)
        self.assert_not_run()

    def test_a_shell_command_line_is_refused_too(self):
        with self.assertRaises(FileNotFoundError):
            subprocess.run("claude --version", shell=True, check=False)
        self.assert_not_run()

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
        self.assertEqual(commands, [[self.fake, "--version"]])

    def test_other_programs_still_start(self):
        completed = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
        self.assertEqual(completed.stdout.strip(), "ok")


class TestAnAllowedFake(GuardCase):
    def test_a_fake_named_by_its_path_runs(self):
        self.allow_cli(self.fake)
        output = ClaudeProvider(self.fake)._help_output("--help")
        self.assertIn("claude 9.9.9", output or "")
        self.assertTrue(os.path.exists(self.marker))

    def test_its_bare_name_stays_refused(self):
        self.allow_cli(self.fake)
        self.assertIsNone(ClaudeProvider()._help_output("--help"))
        self.assert_not_run()

    def test_the_allowance_ends_with_the_test(self):
        self.allow_cli(self.fake)
        self.doCleanups()
        self.assertNotIn(cli_guard.normalised(self.fake), cli_guard.allowed())


if __name__ == "__main__":
    unittest.main()
