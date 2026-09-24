"""Provider adapters: discovery, model resolution, command shape, safety."""

from __future__ import annotations

import os
import sys
import textwrap
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

    def test_built_ins_are_recorded_as_built_in(self):
        for name in ("claude", "codex", "mock"):
            self.assertEqual(providers.provider_origin(name).kind, "builtin")
            self.assertEqual(providers.describe_origin(name), "built-in")


def user_adapter(name: str, body: str = "", build: str = "return Adapter(executable)") -> str:
    """Source for a user module providing ``name``; ``body`` runs at import."""
    template = textwrap.dedent(
        """\
        import orchestrator.providers as registry
        from orchestrator.providers.base import Provider, ResolvedModel


        class Adapter(Provider):
            name = %r
            executable = "user-adapter-not-on-path"

            def _resolve_latest(self, family):
                return ResolvedModel(self.name, family, "latest", None, "default", "cli-default")

            def build_command(self, mode, resolved, cwd, extra_args=(), options=None):
                return [self.executable]


        def build_provider(executable=None):
            %s
        """
    )
    return template % (name, build) + textwrap.dedent(body)


class TestUserProviders(IsolatedCase):
    def errors(self, report):
        return {os.path.basename(item["path"]): item["error"] for item in report["errors"]}

    def assert_built_ins_intact(self):
        self.assertIsInstance(providers.get_provider("codex"), CodexProvider)
        self.assertIsInstance(providers.get_provider("claude"), ClaudeProvider)
        self.assertIsInstance(providers.get_provider("mock"), MockProvider)
        for name in ("claude", "codex", "mock"):
            self.assertEqual(providers.provider_origin(name).kind, "builtin")

    def test_a_valid_module_is_registered_with_its_path(self):
        path = self.write_user_provider("mycli")
        report = self.load_user_providers()
        self.assertEqual(report["loaded"][0]["name"], "mycli")
        self.assertEqual(report["errors"], [])
        self.assertIn("mycli", providers.available_providers())
        self.assertIsInstance(providers.get_provider("mycli"), base.Provider)
        self.assertEqual(providers.provider_origin("mycli").path, path)
        self.assertEqual(providers.describe_origin("mycli"), "user module %s" % path)

    def test_a_built_in_name_is_refused(self):
        self.write_user_provider("mine", user_adapter("codex"))
        report = self.load_user_providers()
        self.assertEqual(report["loaded"], [])
        self.assertIn("built-in", self.errors(report)["mine.py"])
        self.assert_built_ins_intact()

    def test_calling_register_at_import_time_is_refused(self):
        body = """
        registry.register("codex", lambda executable=None: Adapter(executable))
        """
        self.write_user_provider("sneaky", user_adapter("sneaky", body))
        report = self.load_user_providers()
        self.assertIn("register through build_provider", self.errors(report)["sneaky.py"])
        self.assertNotIn("sneaky", providers.available_providers())
        self.assertNotIn(providers.USER_MODULE_PREFIX + "sneaky", sys.modules)
        self.assert_built_ins_intact()

    def test_writing_the_registry_at_import_time_is_undone(self):
        body = """
        registry._REGISTRY["codex"] = lambda executable=None: Adapter(executable)
        """
        self.write_user_provider("sneaky", user_adapter("sneaky", body))
        report = self.load_user_providers()
        self.assertIn("restored", self.errors(report)["sneaky.py"])
        self.assertNotIn("sneaky", providers.available_providers())
        self.assert_built_ins_intact()

    def test_build_provider_registering_an_extra_name_is_refused(self):
        build = 'registry.register("extra", build_provider)\n    return Adapter(executable)'
        self.write_user_provider("greedy", user_adapter("greedy", build=build))
        report = self.load_user_providers()
        self.assertIn("register through build_provider", self.errors(report)["greedy.py"])
        self.assertEqual(providers.available_providers(), ["claude", "codex", "mock"])

    def test_build_provider_writing_an_extra_name_is_undone(self):
        build = 'registry._REGISTRY["extra"] = build_provider\n    return Adapter(executable)'
        self.write_user_provider("greedy", user_adapter("greedy", build=build))
        report = self.load_user_providers()
        self.assertIn("restored", self.errors(report)["greedy.py"])
        self.assertEqual(providers.available_providers(), ["claude", "codex", "mock"])
        self.assertEqual(sorted(providers._ORIGINS), ["claude", "codex", "mock"])

    def test_build_provider_overwriting_a_built_in_is_undone(self):
        build = 'registry._REGISTRY["codex"] = build_provider\n    return Adapter(executable)'
        self.write_user_provider("greedy", user_adapter("greedy", build=build))
        report = self.load_user_providers()
        self.assertIn("restored", self.errors(report)["greedy.py"])
        self.assertNotIn("greedy", providers.available_providers())
        self.assert_built_ins_intact()

    def test_a_later_file_cannot_overwrite_an_earlier_user_entry(self):
        first = self.write_user_provider("a_mycli")
        body = """
        registry._REGISTRY["mycli"] = build_provider
        registry._ORIGINS["mycli"] = registry.ProviderOrigin("user", "elsewhere", "elsewhere")
        """
        self.write_user_provider("b_evil", user_adapter("evil", body))
        report = self.load_user_providers()
        self.assertEqual([item["name"] for item in report["loaded"]], ["mycli"])
        self.assertIn("restored", self.errors(report)["b_evil.py"])
        self.assertEqual(providers.provider_origin("mycli").path, first)
        self.assertEqual(type(providers.get_provider("mycli")).__name__, "MyCliProvider")
        self.assertNotIn("evil", providers.available_providers())

    def test_register_outside_the_loader_cannot_claim_a_built_in(self):
        with self.assertRaises(providers.ProviderRegistrationError):
            providers.register("codex", lambda executable=None: None)
        self.assert_built_ins_intact()

    def test_register_without_an_origin_is_refused_after_bootstrap(self):
        """What a user factory called later from get_provider() would do; it
        must not end up labelled built-in."""
        with self.assertRaises(providers.ProviderRegistrationError):
            providers.register("late", lambda executable=None: None)
        builtin = providers.ProviderOrigin("builtin", None, "x")
        with self.assertRaises(providers.ProviderRegistrationError):
            providers.register("late", lambda executable=None: None, builtin)
        self.assertNotIn("late", providers.available_providers())

    def test_register_with_a_user_origin_is_refused_outside_the_loader(self):
        forged = providers.ProviderOrigin("user", "/made/up.py", "made_up")
        with self.assertRaises(providers.ProviderRegistrationError):
            providers.register("forged", lambda executable=None: None, forged)
        self.assertNotIn("forged", providers.available_providers())

    def test_a_later_build_provider_call_cannot_register_an_extra_name(self):
        """The loader's own call is guarded; so is every get_provider() after it."""
        build = (
            "if _CALLS[0]:\n"
            "        registry.register('extra', build_provider,"
            " registry.ProviderOrigin('user', '/made/up.py', 'x'))\n"
            "    _CALLS[0] += 1\n"
            "    return Adapter(executable)"
        )
        self.write_user_provider("late", user_adapter("late", "_CALLS = [0]\n", build=build))
        report = self.load_user_providers()
        self.assertEqual(report["errors"], [])
        with self.assertRaises(providers.ProviderRegistrationError):
            providers.get_provider("late")
        self.assertNotIn("extra", providers.available_providers())

    def test_a_later_build_provider_call_writing_the_registry_is_undone(self):
        build = (
            "if _CALLS[0]:\n"
            "        registry._REGISTRY['extra'] = build_provider\n"
            "    _CALLS[0] += 1\n"
            "    return Adapter(executable)"
        )
        self.write_user_provider("late", user_adapter("late", "_CALLS = [0]\n", build=build))
        self.load_user_providers()
        with self.assertRaisesRegex(providers.ProviderRegistrationError, "restored"):
            providers.get_provider("late")
        self.assertNotIn("extra", providers.available_providers())
        self.assertEqual(providers.provider_origin("late").kind, "user")

    def test_the_name_is_read_once(self):
        """A ``name`` that raises on a second read must not take the CLI down."""
        body = """
        _READS = [0]


        class Flaky(Adapter):
            @property
            def name(self):
                _READS[0] += 1
                if _READS[0] > 1:
                    raise RuntimeError("read twice")
                return "onceonly"


        def build_provider(executable=None):
            return Flaky(executable)
        """
        self.write_user_provider("once", user_adapter("unused", body))
        report = self.load_user_providers()
        self.assertEqual(report["errors"], [])
        self.assertEqual([item["name"] for item in report["loaded"]], ["onceonly"])
        self.assertIn("onceonly", providers.available_providers())

    def test_sys_exit_at_import_time_is_a_load_error(self):
        self.write_user_provider("quitter", "import sys\nsys.exit(3)\n")
        self.write_user_provider("mycli")
        report = self.load_user_providers()
        self.assertIn("SystemExit", self.errors(report)["quitter.py"])
        self.assertIn("mycli", providers.available_providers())

    def test_the_first_file_to_claim_a_name_wins(self):
        first = self.write_user_provider("a_mycli")
        self.write_user_provider("b_mycli")
        report = self.load_user_providers()
        self.assertEqual([item["path"] for item in report["loaded"]], [first])
        self.assertIn(first, self.errors(report)["b_mycli.py"])
        self.assertEqual(providers.provider_origin("mycli").path, first)

    def test_a_syntax_error_does_not_stop_the_others(self):
        self.write_user_provider("broken", "def oops(:\n")
        self.write_user_provider("mycli")
        report = self.load_user_providers()
        self.assertIn("mycli", providers.available_providers())
        self.assertIn("SyntaxError", self.errors(report)["broken.py"])
        self.assertNotIn(providers.USER_MODULE_PREFIX + "broken", sys.modules)

    def test_a_relative_import_gets_a_hint(self):
        self.write_user_provider("relative", "from .base import Provider\n")
        report = self.load_user_providers()
        self.assertIn("orchestrator.providers.base", self.errors(report)["relative.py"])

    def test_modules_breaking_the_contract_are_refused(self):
        self.write_user_provider("nobuild", "X = 1\n")
        not_a_provider = "def build_provider(executable=None):\n    return object()\n"
        self.write_user_provider("notprovider", not_a_provider)
        self.write_user_provider("badname", user_adapter("Bad Name"))
        self.write_user_provider("basename", user_adapter("base"))
        report = self.load_user_providers()
        errors = self.errors(report)
        self.assertIn("build_provider", errors["nobuild.py"])
        self.assertIn("not an orchestrator.providers.base.Provider", errors["notprovider.py"])
        self.assertIn("not usable", errors["badname.py"])
        self.assertIn("not usable", errors["basename.py"])
        self.assertEqual(providers.available_providers(), ["claude", "codex", "mock"])

    def test_private_hidden_and_non_python_entries_are_skipped(self):
        self.write_user_provider("_private", "raise RuntimeError('imported')\n")
        self.write_user_provider(".hidden", "raise RuntimeError('imported')\n")
        directory = os.path.dirname(self.write_user_provider("mycli"))
        with open(os.path.join(directory, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("not python")
        os.makedirs(os.path.join(directory, "package.py"))
        report = self.load_user_providers()
        self.assertEqual(report["errors"], [])
        self.assertEqual([item["name"] for item in report["loaded"]], ["mycli"])

    def test_files_load_in_sorted_order(self):
        self.write_user_provider("b", user_adapter("bee"))
        self.write_user_provider("a", user_adapter("ay"))
        report = self.load_user_providers()
        self.assertEqual([item["name"] for item in report["loaded"]], ["ay", "bee"])

    def test_a_missing_directory_is_quiet(self):
        report = self.load_user_providers()
        self.assertFalse(report["present"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(providers.available_providers(), ["claude", "codex", "mock"])

    def test_the_switch_disables_loading(self):
        self.write_user_provider("mycli")
        os.environ[providers.USER_PROVIDERS_DISABLED_ENV] = "1"
        report = self.load_user_providers()
        self.assertFalse(report["enabled"])
        self.assertEqual(report["loaded"], [])
        self.assertNotIn("mycli", providers.available_providers())

    def test_loading_again_forgets_a_deleted_module(self):
        path = self.write_user_provider("mycli")
        self.load_user_providers()
        os.remove(path)
        self.load_user_providers()
        self.assertNotIn("mycli", providers.available_providers())

    def test_loading_again_forgets_what_the_old_file_discovered(self):
        """Discovery is memoised by module and class name, which an edit keeps."""
        template = textwrap.dedent(
            """\
            from orchestrator.providers.base import Provider


            class Adapter(Provider):
                name = "mycli"
                executable = "mycli"

                def which(self):
                    return %r


            def build_provider(executable=None):
                return Adapter(executable)
            """
        )
        self.write_user_provider("mycli", template % None)
        self.load_user_providers()
        self.assertFalse(providers.get_provider("mycli").detect().installed)
        self.write_user_provider("mycli", template % sys.executable)
        self.load_user_providers()
        self.assertTrue(providers.get_provider("mycli").detect().installed)

    def test_unload_leaves_only_the_built_ins(self):
        self.write_user_provider("mycli")
        self.load_user_providers()
        providers.unload_user_providers()
        self.assertEqual(providers.available_providers(), ["claude", "codex", "mock"])
        self.assertFalse(any(name.startswith(providers.USER_MODULE_PREFIX) for name in sys.modules))

    def test_a_copied_class_name_does_not_share_the_built_in_detection(self):
        """Discovery is memoised by class; a user module that kept
        ``CodexProvider`` and ``codex`` from a copy must still be detected on
        its own."""
        original = CodexProvider.which
        CodexProvider.which = lambda self: None
        self.addCleanup(setattr, CodexProvider, "which", original)
        source = textwrap.dedent(
            """\
            import sys

            from orchestrator.providers.base import Provider


            class CodexProvider(Provider):
                name = "mycodex"
                executable = "codex"

                def which(self):
                    return sys.executable

                def version(self):
                    return "user 1", None


            def build_provider(executable=None):
                return CodexProvider(executable)
            """
        )
        self.write_user_provider("mycodex", source)
        self.load_user_providers()
        self.assertFalse(providers.get_provider("codex").detect().installed)
        self.assertTrue(providers.get_provider("mycodex").detect().installed)


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


class BrokenReaderProvider(base.Provider):
    """A CLI that runs fine and an adapter that falls over reading it.

    Imported by ``test_review`` too: the point it exists to make is about what
    reaches the review path, and the two halves have to agree about the shape.
    """

    name = "broken-reader"
    display_name = "Broken reader (test)"
    executable = "python"

    #: Which half raises. ``parse_usage`` is the more likely one in real life --
    #: it reads a stream format the CLI owns -- but both are outside our control.
    raise_in = "postprocess"

    def which(self):
        return sys.executable

    def version(self):
        return "test 0", None

    def auth_status(self):
        return "present", "no credentials required"

    def _resolve_latest(self, family):
        return base.ResolvedModel(self.name, family or "test", "latest", None, "test", "builtin-fallback")

    def build_command(self, mode, resolved, cwd, extra_args=(), options=None):
        return [sys.executable, "-c", "print('hi')"]

    def postprocess(self, outcome, mode):
        if self.raise_in == "postprocess":
            raise ValueError("boom")
        return outcome.stdout, outcome.stderr

    def parse_usage(self, outcome, mode):
        if self.raise_in == "parse_usage":
            raise ValueError("boom")
        return None


class TestMeasurementSurvivesABrokenAdapter(IsolatedCase):
    """A child that ran was measured, and the measurement must reach the caller.

    It used not to: ``postprocess`` and ``parse_usage`` sat outside every
    ``try``, so an adapter raising there took the whole ``RunResult`` with it.
    The review path caught the exception two frames up, where the duration was
    no longer knowable, and recorded a reviewer that had run for minutes as
    having taken no time at all.
    """

    def _run(self, raise_in):
        provider = BrokenReaderProvider()
        provider.raise_in = raise_in
        return provider.run("prompt", base.MODE_REVIEW, self.project)

    def test_a_postprocess_failure_returns_a_measured_failure(self):
        result = self._run("postprocess")
        self.assertFalse(result.ok)
        self.assertGreater(result.duration, 0)
        self.assertTrue(result.invoked)
        self.assertIn("ValueError: boom", result.stderr)

    def test_a_parse_usage_failure_leaves_the_answer_alone(self):
        """The invoice is unreadable, not the answer. Failing the run here
        would throw away output that parsed fine -- and the run was paid for
        either way, so the cost it cannot state is unknown, not zero."""
        result = self._run("parse_usage")
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout.strip(), "hi")
        self.assertGreater(result.duration, 0)
        self.assertFalse(result.usage.measured)

    def test_the_run_reports_itself_as_unmeasured_rather_than_free(self):
        """Nothing was parsed, so the account must call the total a floor."""
        self.assertFalse(self._run("postprocess").usage.measured)


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
