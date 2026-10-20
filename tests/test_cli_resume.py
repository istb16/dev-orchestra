"""Resumed sessions as ``doctor`` and ``run --resume`` see them."""

from __future__ import annotations

import json
import os
import unittest

from helpers import (
    CLAUDE_HELP_NO_FORK,
    UNLISTED_CLAUDE,
    IsolatedCase,
    _Completed,
    codex_fork_help,
    pretend_claude_is_installed,
    pretend_claude_version,
    record_resume_pass,
    run_cli,
)


def smoke_script() -> str:
    """The live check's script as doctor names it: absolute, quoted if it has a space."""
    from orchestrator import doctor

    return '"%s"' % doctor.SMOKE_SCRIPT if " " in doctor.SMOKE_SCRIPT else doctor.SMOKE_SCRIPT


PARENT_SESSION = "55555555-5555-4555-8555-555555555555"


#: A user adapter that is the claude adapter under another name, resuming nothing.
CLAUDE_OFF_ADAPTER = '''\
"""The claude adapter under another name, without resumed sessions."""

from __future__ import annotations

from typing import Optional

from orchestrator.providers.claude import ClaudeProvider


class ClaudeOffProvider(ClaudeProvider):
    name = "claude-off"
    supports_resume = False


def build_provider(executable: Optional[str] = None) -> ClaudeOffProvider:
    return ClaudeOffProvider(executable)
'''


class TestDoctorResume(IsolatedCase):
    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")

    def support(self, *argv):
        return json.loads(run_cli("doctor", "--json", *argv)[1])["providers"]["claude"]["resume_support"]

    def test_a_claude_subclass_with_resume_off_does_not_resume(self):
        pretend_claude_is_installed(self)
        self.write_user_provider("claude_off", CLAUDE_OFF_ADAPTER)
        self.load_user_providers()
        found = json.loads(run_cli("doctor", "--json")[1])["providers"]
        expected = {"status": "unsupported", "detail": "claude-off does not resume sessions"}
        self.assertEqual(found["claude-off"]["resume_support"], expected)
        self.assertEqual(found["claude"]["resume_support"]["status"], "verified")
        _, out, _ = run_cli("doctor")
        self.assertIn("Resume: NOT SUPPORTED -- claude-off does not resume sessions", out)
        fast = json.loads(run_cli("doctor", "--fast", "--json")[1])["providers"]
        self.assertEqual(fast["claude-off"]["resume_support"], {"status": "not-checked"})

    def test_an_unlisted_version_is_unverified_until_recorded(self):
        pretend_claude_is_installed(self)
        pretend_claude_version(self, UNLISTED_CLAUDE)
        self.assertEqual(self.support()["status"], "unverified")
        _, out, _ = run_cli("doctor")
        self.assertIn("Resume: UNVERIFIED -- ", out)
        strict_before = run_cli("doctor", "--strict")[0]
        record_resume_pass(self.project)
        support = self.support()
        self.assertEqual(support["status"], "verified")
        self.assertEqual(support["source"], "record")
        _, out, _ = run_cli("doctor")
        self.assertIn("Resume: verified for claude %s on " % UNLISTED_CLAUDE, out)
        self.assertEqual(run_cli("doctor", "--strict")[0], strict_before)

    def test_fast_does_not_check(self):
        self.assertEqual(self.support("--fast")["status"], "not-checked")
        _, out, _ = run_cli("doctor", "--fast")
        self.assertNotIn("Resume: verified", out)

    def test_a_cli_without_fork_session_is_not_supported(self):
        pretend_claude_is_installed(self, CLAUDE_HELP_NO_FORK)
        support = self.support()
        self.assertEqual(support["status"], "unsupported")
        _, out, _ = run_cli("doctor")
        self.assertIn("Resume: NOT SUPPORTED -- --fork-session not advertised", out)

    def test_codex_with_resume_off_does_not_resume(self):
        from orchestrator.providers.codex import CodexProvider

        for name, value in (
            ("supports_resume", False),
            ("which", lambda self: "codex"),
            ("version", lambda self: ("codex-cli 0.154.0", None)),
            ("configured_model", lambda self: None),
            ("_capture", lambda self, command, timeout=30: _Completed("", 1)),
        ):
            self.addCleanup(setattr, CodexProvider, name, getattr(CodexProvider, name))
            setattr(CodexProvider, name, value)
        _, out, _ = run_cli("doctor")
        self.assertIn("Resume: NOT SUPPORTED -- codex does not resume sessions", out)

    def test_a_newer_claude_is_trusted_and_says_so(self):
        pretend_claude_is_installed(self)
        pretend_claude_version(self, "2.1.285 (Claude Code)")
        strict_verified = run_cli("doctor", "--strict")[0]
        pretend_claude_version(self, "2.1.286 (Claude Code)")
        support = self.support()
        self.assertEqual(support["status"], "trusted")
        self.assertEqual(support["newer_than"], "2.1.285 (Claude Code)")
        _, out, _ = run_cli("doctor")
        trusted = "Resume: trusted for claude 2.1.286 (Claude Code) as newer than 2.1.285 (Claude Code) "
        self.assertIn(trusted + "(verified on ", out)
        tail = ", built-in); not verified itself -- run python %s --provider claude" % smoke_script()
        self.assertIn(tail, out)
        self.assertEqual(run_cli("doctor", "--strict")[0], strict_verified)

    def test_codex_once_it_resumes(self):
        from orchestrator.providers import codex as codex_module
        from orchestrator.providers.codex import CodexProvider

        fork_help = codex_fork_help()

        def capture(self, command, timeout=30):
            if list(command[1:]) == ["exec", "fork", "--help"]:
                return _Completed(fork_help)
            return _Completed("", 1)

        version = ["codex-cli 0.156.1"]
        for name, value in (
            ("which", lambda self: "codex"),
            ("version", lambda self: (version[0], None)),
            ("configured_model", lambda self: None),
            ("_capture", capture),
            ("supports_resume", True),
        ):
            self.addCleanup(setattr, CodexProvider, name, getattr(CodexProvider, name))
            setattr(CodexProvider, name, value)
        from orchestrator.providers import codex as codex_table

        self.addCleanup(setattr, codex_table, "VERIFIED_RESUME", codex_table.VERIFIED_RESUME)
        codex_table.VERIFIED_RESUME = {}

        def line():
            lines = run_cli("doctor")[1].splitlines()
            return [text for text in lines if "Resume:" in text and "codex" in text]

        self.assertIn("UNVERIFIED -- codex codex-cli 0.156.1 has not been verified", line()[0])
        entry = {
            "verified_at": "2026-10-01",
            "read_only_mechanism": CodexProvider().resume_mechanism(),
            "checks": list(CodexProvider.required_resume_checks),
        }
        self.addCleanup(setattr, codex_module, "VERIFIED_RESUME", codex_module.VERIFIED_RESUME)
        codex_module.VERIFIED_RESUME = {"codex-cli 0.156.1": entry}
        self.assertIn("Resume: verified for codex codex-cli 0.156.1 on 2026-10-01 (built-in)", line()[0])
        version[0] = "codex-cli 0.157.0"
        self.assertIn(
            "Resume: trusted for codex codex-cli 0.157.0 as newer than codex-cli 0.156.1 "
            "(verified on 2026-10-01, built-in); not verified itself -- "
            "run python %s --provider codex" % smoke_script(),
            line()[0],
        )
        codex = json.loads(run_cli("doctor", "--json")[1])["providers"]["codex"]["resume_support"]
        self.assertEqual(codex["status"], "trusted")
        self.assertEqual(codex["newer_than"], "codex-cli 0.156.1")

    def test_a_record_inside_the_checkout_is_not_read(self):
        pretend_claude_is_installed(self)
        pretend_claude_version(self, UNLISTED_CLAUDE)
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(self.project, ".ai", "home")
        support = self.support()
        self.assertEqual(support["status"], "unverified")
        self.assertIn("inside the workspace", support["detail"])

    def test_a_record_inside_the_repository_is_not_read_from_a_subdirectory(self):
        """`run` judges against the repository root, so doctor must too."""
        self.init_git_repo()
        pretend_claude_is_installed(self)
        pretend_claude_version(self, UNLISTED_CLAUDE)
        home = os.path.join(self.project, ".ai", "home")
        os.environ["DEV_ORCHESTRA_HOME"] = home
        subdirectory = os.path.join(self.project, "src")
        os.makedirs(subdirectory)
        # Written as if from the subdirectory, which the record lies outside.
        record_resume_pass(subdirectory)
        os.chdir(subdirectory)
        support = self.support()
        self.assertEqual(support["status"], "unverified")
        self.assertIn("inside the workspace", support["detail"])


class TestPrintCommandResume(IsolatedCase):
    """`run architect --resume --print-command`, with claude as the architect."""

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        pretend_claude_is_installed(self)
        self.write("resume.md", "revise\n")

    def print_command(self):
        return run_cli(
            "run",
            "architect",
            "--resume",
            "--resume-prompt-file",
            "resume.md",
            "--output",
            ".ai/plan.md",
            "--print-command",
        )

    def record_architect_run(self, provider="claude"):
        self.cli_workspace().record_event(
            "architect",
            "ok",
            {
                "mode": "plan",
                "provider": provider,
                "output": ".ai/plan.md",
                "answered": True,
                "session_id": PARENT_SESSION,
            },
        )

    def test_an_unverified_version_prints_a_fresh_command(self):
        pretend_claude_version(self, UNLISTED_CLAUDE)
        self.record_architect_run()
        code, out, err = self.print_command()
        self.assertEqual(code, 0)
        self.assertNotIn("--resume=", out)
        self.assertIn("(unverified)", err)
        self.assertIn("smoke_live.py", err)

    def test_a_recorded_version_prints_the_resumed_command(self):
        pretend_claude_version(self, UNLISTED_CLAUDE)
        record_resume_pass(self.project)
        self.record_architect_run()
        code, out, _ = self.print_command()
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith("--resume=%s --fork-session" % PARENT_SESSION))
        self.assertIn("--tools Read,Grep,Glob --strict-mcp-config --restricted", out)
        budgets = json.loads(run_cli("budget", "show", "--json")[1])["budgets"]
        self.assertEqual(budgets["architect"]["used"], 0)

    def test_2_1_283_resumes_from_the_built_in_table(self):
        self.record_architect_run()
        _, out, _ = self.print_command()
        self.assertIn("--resume=", out)

    def test_a_record_for_other_flags_does_not_count(self):
        pretend_claude_version(self, UNLISTED_CLAUDE)
        record_resume_pass(self.project, mechanism="--other flags")
        self.record_architect_run()
        _, out, _ = self.print_command()
        self.assertNotIn("--resume=", out)


class TestUserAdapterResume(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.write_user_provider("mycli")
        self.load_user_providers()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "architect.provider", "mycli")
        run_cli("config", "set", "architect.model.family", "default")
        self.write("resume.md", "revise\n")
        from orchestrator import providers as registry

        provider = registry.get_provider("mycli")
        cls = type(provider)
        self.addCleanup(setattr, cls, "which", cls.which)
        cls.which = lambda self: "mycli"

    def test_the_command_is_unchanged_and_the_run_goes_fresh(self):
        code, before, _ = run_cli("run", "architect", "--print-command")
        self.assertEqual(code, 0)
        code, out, err = run_cli(
            "run",
            "architect",
            "--resume",
            "--resume-prompt-file",
            "resume.md",
            "--output",
            ".ai/plan.md",
            "--print-command",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, before)
        self.assertIn("running fresh: the provider cannot resume a session", err)


if __name__ == "__main__":
    unittest.main()
