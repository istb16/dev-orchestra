"""Provider adapters: discovery, model resolution, command shape, safety."""

from __future__ import annotations

import unittest

from helpers import IsolatedCase

from orchestrator import providers
from orchestrator.providers import base
from orchestrator.providers.claude import ClaudeProvider, _parse_model_aliases
from orchestrator.providers.codex import CodexProvider
from orchestrator.providers.mock import MockProvider

CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  --mcp-config <configs...>             Load MCP servers from JSON files
  --model <model>                       Model for the current session. Provide
                                        an alias for the latest model (e.g.
                                        'fable', 'opus', or 'sonnet') or a
                                        model's full name (e.g.
                                        'claude-fable-5').
  -n, --name <name>                     Set a display name for this session
  --permission-mode <mode>              Permission mode to use for the session
                                        (choices: "acceptEdits", "auto",
                                        "bypassPermissions", "manual",
                                        "dontAsk", "plan")
  -p, --print                           Print response and exit
"""


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestRegistry(IsolatedCase):
    def test_known_providers(self):
        self.assertEqual(providers.available_providers(), ["claude", "codex", "mock"])

    def test_unknown_provider_raises(self):
        with self.assertRaises(providers.UnknownProviderError):
            providers.get_provider("nonexistent")


class TestClaudeAdapter(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.provider = ClaudeProvider()
        self.provider._capture = lambda command, timeout=30: _FakeCompleted(CLAUDE_HELP)

    def test_aliases_come_from_the_installed_cli_help(self):
        self.assertEqual(_parse_model_aliases(CLAUDE_HELP), ["fable", "opus", "sonnet"])

    def test_full_model_names_in_help_are_not_treated_as_aliases(self):
        self.assertNotIn("claude-fable-5", _parse_model_aliases(CLAUDE_HELP))

    def test_latest_policy_passes_the_alias_through(self):
        resolved = self.provider.resolve_model({"family": "opus", "version": "latest"})
        self.assertEqual(resolved.argument, "opus")
        self.assertEqual(resolved.source, "cli-help")

    def test_pinned_policy_uses_the_exact_id(self):
        resolved = self.provider.resolve_model({"family": "opus", "version": "pinned", "id": "claude-opus-5"})
        self.assertEqual(resolved.argument, "claude-opus-5")
        self.assertEqual(resolved.source, "config-pinned")

    def test_pinned_without_id_raises(self):
        with self.assertRaises(base.ModelResolutionError):
            self.provider.resolve_model({"family": "opus", "version": "pinned"})

    def test_unknown_family_is_refused_rather_than_guessed(self):
        with self.assertRaises(base.ModelResolutionError) as ctx:
            self.provider.resolve_model({"family": "recommended-coding", "version": "latest"})
        self.assertIn("cannot resolve model family", str(ctx.exception))

    def test_help_failure_falls_back_to_documented_aliases(self):
        self.provider._capture = lambda command, timeout=30: _FakeCompleted("", "boom", 1)
        families = {candidate.family for candidate in self.provider.list_models()}
        self.assertIn("opus", families)
        self.assertTrue(all(c.source == "builtin-fallback" for c in self.provider.list_models()))

    def test_fallback_list_contains_no_dated_snapshot_ids(self):
        for candidate in ClaudeProvider.fallback_models:
            self.assertNotRegex(candidate.value, r"\d{6,}")

    def test_read_only_modes_deny_editing_tools(self):
        resolved = self.provider.resolve_model({"family": "opus"})
        for mode in (base.MODE_PLAN, base.MODE_REVIEW):
            command = self.provider.build_command(mode, resolved, self.project)
            self.assertIn("--permission-mode", command)
            self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
            self.assertIn("--disallowed-tools", command)
            self.assertIn("Edit", command[command.index("--disallowed-tools") + 1])

    def test_implement_mode_accepts_edits(self):
        resolved = self.provider.resolve_model({"family": "opus"})
        command = self.provider.build_command(base.MODE_IMPLEMENT, resolved, self.project)
        self.assertEqual(command[command.index("--permission-mode") + 1], "acceptEdits")

    def test_missing_cli_reports_cleanly(self):
        self.provider.which = lambda: None
        result = self.provider.run("hi", base.MODE_PLAN, self.project)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 127)
        self.assertIn("not found", result.stderr)


class TestClaudeRoleOptions(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.provider = ClaudeProvider()
        self.provider._capture = lambda command, timeout=30: _FakeCompleted(CLAUDE_HELP)
        self.resolved = self.provider.resolve_model({"family": "opus"})

    def test_permission_modes_come_from_the_installed_cli(self):
        self.assertEqual(
            self.provider.permission_modes(),
            ["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"],
        )

    def test_implement_mode_honours_a_configured_permission_mode(self):
        command = self.provider.build_command(
            base.MODE_IMPLEMENT, self.resolved, self.project, options={"permission_mode": "bypassPermissions"}
        )
        self.assertEqual(command[command.index("--permission-mode") + 1], "bypassPermissions")

    def test_read_only_modes_ignore_a_configured_permission_mode(self):
        for mode in (base.MODE_PLAN, base.MODE_REVIEW):
            command = self.provider.build_command(
                mode, self.resolved, self.project, options={"permission_mode": "bypassPermissions"}
            )
            self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
            self.assertIn("--disallowed-tools", command)

    def test_extra_args_from_options_are_appended(self):
        command = self.provider.build_command(
            base.MODE_IMPLEMENT, self.resolved, self.project, options={"args": ["--add-dir", "../shared"]}
        )
        self.assertEqual(command[-2:], ["--add-dir", "../shared"])

    def test_unknown_permission_mode_is_rejected(self):
        problems = self.provider.validate_options({"permission_mode": "yolo"})
        self.assertTrue(any("not one of the modes" in p for p in problems))

    def test_unknown_option_key_is_rejected(self):
        problems = self.provider.validate_options({"sandbox": "workspace-write"})
        self.assertTrue(any("not understood by the claude provider" in p for p in problems))

    def test_non_list_args_is_rejected(self):
        self.assertTrue(any("list of strings" in p for p in self.provider.validate_options({"args": "x"})))

    def test_no_options_means_no_problems(self):
        self.assertEqual(self.provider.validate_options(None), [])


class TestCodexRoleOptions(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.provider = CodexProvider()
        self.provider.configured_model = lambda: "gpt-example-1"
        self.resolved = self.provider.resolve_model({"family": "recommended-coding"})

    def test_implement_mode_honours_a_configured_sandbox(self):
        command = self.provider.build_command(
            base.MODE_IMPLEMENT, self.resolved, self.project, options={"sandbox": "danger-full-access"}
        )
        self.assertEqual(command[command.index("-s") + 1], "danger-full-access")

    def test_approve_false_drops_the_approval_flag(self):
        command = self.provider.build_command(
            base.MODE_IMPLEMENT, self.resolved, self.project, options={"approve": False}
        )
        self.assertNotIn("--approve-for-me", command)

    def test_read_only_modes_ignore_a_configured_sandbox(self):
        command = self.provider.build_command(
            base.MODE_REVIEW, self.resolved, self.project, options={"sandbox": "danger-full-access"}
        )
        self.assertEqual(command[command.index("-s") + 1], "read-only")

    def test_invalid_sandbox_is_rejected(self):
        self.assertTrue(any("not one of" in p for p in self.provider.validate_options({"sandbox": "nope"})))

    def test_non_boolean_approve_is_rejected(self):
        self.assertTrue(any("true or false" in p for p in self.provider.validate_options({"approve": "yes"})))


CODEX_CATALOG = """{"models": [
  {"slug": "gpt-example-1", "display_name": "GPT-Example-1", "visibility": "list"},
  {"slug": "gpt-example-3", "display_name": "GPT-Example-3", "visibility": "list"},
  {"slug": "gpt-internal", "display_name": "Internal", "visibility": "hide"},
  {"display_name": "no slug", "visibility": "list"},
  "not an object"
]}"""


class TestCodexAdapter(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.provider = CodexProvider()
        self.provider.configured_model = lambda: "gpt-example-1"
        # Default: behave like a CLI that publishes no catalogue. Tests that
        # care about `codex debug models` opt in with catalogue().
        self.provider.which = lambda: None

    def catalogue(self, stdout=CODEX_CATALOG, returncode=0):
        self.provider.which = lambda: "codex"
        self.provider._capture = lambda command, timeout=30: _FakeCompleted(stdout, "", returncode)

    def test_recommended_family_omits_the_model_flag(self):
        resolved = self.provider.resolve_model({"family": "recommended-coding", "version": "latest"})
        self.assertIsNone(resolved.argument)
        self.assertEqual(resolved.display, "gpt-example-1")
        command = self.provider.build_command(base.MODE_REVIEW, resolved, self.project)
        self.assertNotIn("-m", command)

    def test_configured_model_is_accepted_as_a_family(self):
        resolved = self.provider.resolve_model({"family": "gpt-example-1", "version": "latest"})
        self.assertEqual(resolved.argument, "gpt-example-1")

    def test_unverifiable_family_is_refused(self):
        with self.assertRaises(base.ModelResolutionError) as ctx:
            self.provider.resolve_model({"family": "some-model-i-made-up", "version": "latest"})
        self.assertIn("does not vouch for", str(ctx.exception))

    def test_a_catalogued_family_resolves_to_that_model(self):
        self.catalogue()
        resolved = self.provider.resolve_model({"family": "gpt-example-3", "version": "latest"})
        self.assertEqual(resolved.argument, "gpt-example-3")
        self.assertEqual(resolved.source, "cli-catalog")
        command = self.provider.build_command(base.MODE_REVIEW, resolved, self.project)
        self.assertEqual(command[command.index("-m") + 1], "gpt-example-3")

    def test_the_catalogue_is_fetched_once_per_process(self):
        """Resolution must reuse the memoised list, not re-spawn the CLI.

        `doctor --fast` skips model discovery but still resolves every role,
        so a per-role spawn there would defeat the flag.
        """
        self.provider.which = lambda: "codex"
        calls = []

        def capture(command, timeout=30):
            calls.append(list(command))
            return _FakeCompleted(CODEX_CATALOG)

        self.provider._capture = capture
        for _ in range(3):
            self.provider.resolve_model({"family": "gpt-example-3", "version": "latest"})
        with self.assertRaises(base.ModelResolutionError):
            self.provider.resolve_model({"family": "gpt-example-9", "version": "latest"})
        self.assertEqual(len(calls), 1, calls)

    def test_a_family_missing_from_the_catalogue_is_still_refused(self):
        self.catalogue()
        with self.assertRaises(base.ModelResolutionError):
            self.provider.resolve_model({"family": "gpt-example-9", "version": "latest"})

    def test_hidden_and_malformed_catalogue_entries_are_ignored(self):
        self.catalogue()
        families = [candidate.family for candidate in self.provider.list_models()]
        self.assertIn("gpt-example-3", families)
        self.assertNotIn("gpt-internal", families)
        # The configured model is listed once, from the CLI's own config.
        self.assertEqual(families.count("gpt-example-1"), 1)

    def test_a_cli_without_the_catalogue_command_is_tolerated(self):
        self.catalogue(stdout="unknown subcommand", returncode=2)
        self.assertEqual(
            [candidate.family for candidate in self.provider.list_models()],
            ["recommended-coding", "gpt-example-1"],
        )

    def test_unparseable_catalogue_output_is_tolerated(self):
        self.catalogue(stdout="not json at all")
        self.assertEqual(
            [candidate.family for candidate in self.provider.list_models()],
            ["recommended-coding", "gpt-example-1"],
        )

    def test_pinned_id_passes_through(self):
        resolved = self.provider.resolve_model(
            {"family": "whatever", "version": "pinned", "id": "gpt-example-2"}
        )
        self.assertEqual(resolved.argument, "gpt-example-2")

    def test_review_mode_is_sandboxed_read_only(self):
        resolved = self.provider.resolve_model({"family": "recommended-coding"})
        command = self.provider.build_command(base.MODE_REVIEW, resolved, self.project)
        self.assertEqual(command[command.index("-s") + 1], "read-only")
        self.assertIn("exec", command)

    def test_implement_mode_allows_workspace_writes(self):
        resolved = self.provider.resolve_model({"family": "recommended-coding"})
        command = self.provider.build_command(base.MODE_IMPLEMENT, resolved, self.project)
        self.assertEqual(command[command.index("-s") + 1], "workspace-write")

    def test_no_model_list_means_no_invented_candidates(self):
        self.provider.configured_model = lambda: None
        candidates = self.provider.list_models()
        self.assertEqual([c.family for c in candidates], ["recommended-coding"])

    def test_the_catalogue_is_never_consulted_without_the_cli(self):
        """which() is None here: nothing may be spawned, nothing invented."""
        self.provider.configured_model = lambda: None
        self.provider._capture = lambda command, timeout=30: self.fail("spawned %s" % command)
        self.assertEqual([c.family for c in self.provider.list_models()], ["recommended-coding"])


class TestMockAdapter(IsolatedCase):
    def test_review_mode_reports_no_findings_by_default(self):
        result = MockProvider().run("prompt", base.MODE_REVIEW, self.project)
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout.strip(), "NO_FINDINGS")

    def test_targeted_failure(self):
        import os

        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "reviewer-b"
        provider = MockProvider()
        self.assertTrue(provider.run("Reviewer id: reviewer-a", base.MODE_REVIEW, self.project).ok)
        self.assertFalse(provider.run("Reviewer id: reviewer-b", base.MODE_REVIEW, self.project).ok)


class TestRedaction(IsolatedCase):
    def test_credential_shaped_strings_are_scrubbed(self):
        samples = [
            "key sk-abcdefghijklmnop123",
            "ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrs",
            "token: ghp_abcdefghijklmnopqrstuvwxyz01",
            "Authorization: Bearer abcdefghijklmnopqrstuv",
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NX0.dBjftJeZ4CVPmB92K27u",
        ]
        for sample in samples:
            cleaned = base.redact(sample)
            self.assertIn("[redacted]", cleaned, sample)

    def test_ordinary_text_is_untouched(self):
        text = "implementer finished in 12 seconds using opus"
        self.assertEqual(base.redact(text), text)

    def test_run_results_redact_both_streams(self):
        result = base.RunResult(True, 0, "sk-ant-abcdefghijklmnopqrs", "sk-abcdefghijklmnop123", [], 0.0)
        self.assertNotIn("abcdefghijklmnopqrs", result.stdout)
        self.assertNotIn("abcdefghijklmnop123", result.stderr)


if __name__ == "__main__":
    unittest.main()
