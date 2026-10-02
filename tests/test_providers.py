"""Provider adapters: discovery, model resolution, command shape, safety."""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import sys
import textwrap
import threading
import time
import unittest
from typing import Optional

from helpers import (
    CLAUDE_HELP,
    CLAUDE_HELP_NO_FORK,
    CLAUDE_HELP_NO_RESUME,
    CLAUDE_HELP_OLD,
    IsolatedCase,
    make_dir_link,
    present,
    remove_link,
)

from orchestrator import execution, providers, verified
from orchestrator.providers import agy as agy_module
from orchestrator.providers import base
from orchestrator.providers import claude as claude_module
from orchestrator.providers import codex as codex_module
from orchestrator.providers.agy import AgyProvider
from orchestrator.providers.claude import READ_ONLY_MECHANISM, ClaudeProvider, _parse_model_aliases
from orchestrator.providers.codex import CodexProvider
from orchestrator.providers.mock import MockProvider


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestRegistry(IsolatedCase):
    def test_known_providers(self):
        self.assertEqual(providers.available_providers(), ["agy", "claude", "codex", "mock"])

    def test_unknown_provider_raises(self):
        with self.assertRaises(providers.UnknownProviderError):
            providers.get_provider("nonexistent")

    def test_built_ins_are_recorded_as_built_in(self):
        for name in ("agy", "claude", "codex", "mock"):
            self.assertEqual(present(providers.provider_origin(name)).kind, "builtin")
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
        for name in ("agy", "claude", "codex", "mock"):
            self.assertEqual(present(providers.provider_origin(name)).kind, "builtin")

    def test_a_valid_module_is_registered_with_its_path(self):
        path = self.write_user_provider("mycli")
        assert path is not None
        report = self.load_user_providers()
        self.assertEqual(report["loaded"][0]["name"], "mycli")
        self.assertEqual(report["errors"], [])
        self.assertIn("mycli", providers.available_providers())
        self.assertIsInstance(providers.get_provider("mycli"), base.Provider)
        self.assertEqual(present(providers.provider_origin("mycli")).path, path)
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
        self.assertEqual(providers.available_providers(), ["agy", "claude", "codex", "mock"])

    def test_build_provider_writing_an_extra_name_is_undone(self):
        build = 'registry._REGISTRY["extra"] = build_provider\n    return Adapter(executable)'
        self.write_user_provider("greedy", user_adapter("greedy", build=build))
        report = self.load_user_providers()
        self.assertIn("restored", self.errors(report)["greedy.py"])
        self.assertEqual(providers.available_providers(), ["agy", "claude", "codex", "mock"])
        self.assertEqual(sorted(providers._ORIGINS), ["agy", "claude", "codex", "mock"])

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
        self.assertEqual(present(providers.provider_origin("mycli")).path, first)
        self.assertEqual(type(providers.get_provider("mycli")).__name__, "MyCliProvider")
        self.assertNotIn("evil", providers.available_providers())

    def test_register_outside_the_loader_cannot_claim_a_built_in(self):
        with self.assertRaises(providers.ProviderRegistrationError):
            # A factory that is never called: the registration is refused first.
            providers.register("codex", lambda executable=None: None)  # pyright: ignore[reportArgumentType]
        self.assert_built_ins_intact()

    def test_register_without_an_origin_is_refused_after_bootstrap(self):
        """What a user factory called later from get_provider() would do; it
        must not end up labelled built-in."""
        with self.assertRaises(providers.ProviderRegistrationError):
            # A factory that is never called: the registration is refused first.
            providers.register("late", lambda executable=None: None)  # pyright: ignore[reportArgumentType]
        builtin = providers.ProviderOrigin("builtin", None, "x")
        with self.assertRaises(providers.ProviderRegistrationError):
            # A factory that is never called: the registration is refused first.
            providers.register("late", lambda executable=None: None, builtin)  # pyright: ignore[reportArgumentType]
        self.assertNotIn("late", providers.available_providers())

    def test_register_with_a_user_origin_is_refused_outside_the_loader(self):
        forged = providers.ProviderOrigin("user", "/made/up.py", "made_up")
        with self.assertRaises(providers.ProviderRegistrationError):
            # A factory that is never called: the registration is refused first.
            providers.register("forged", lambda executable=None: None, forged)  # pyright: ignore[reportArgumentType]
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
        self.assertEqual(present(providers.provider_origin("late")).kind, "user")

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
        self.assertEqual(present(providers.provider_origin("mycli")).path, first)

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
        self.assertEqual(providers.available_providers(), ["agy", "claude", "codex", "mock"])

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
        self.assertEqual(providers.available_providers(), ["agy", "claude", "codex", "mock"])

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
        self.assertEqual(providers.available_providers(), ["agy", "claude", "codex", "mock"])
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
        setattr(self.provider, "_capture", lambda command, timeout=30: _FakeCompleted(CLAUDE_HELP))

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
        setattr(self.provider, "_capture", lambda command, timeout=30: _FakeCompleted("", "boom", 1))
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

    def test_read_only_modes_allow_three_tools_no_mcp_and_restricted(self):
        """In this order: `--tools` takes several values, and the boolean flag
        after it is what closes the list."""
        resolved = self.provider.resolve_model({"family": "opus"})
        for mode in (base.MODE_PLAN, base.MODE_REVIEW):
            command = self.provider.build_command(mode, resolved, self.project)
            at = command.index("--tools")
            self.assertEqual(
                command[at : at + 4], ["--tools", "Read,Grep,Glob", "--strict-mcp-config", "--restricted"]
            )
            self.assertLess(command.index("--disallowed-tools"), at)
            self.assertLess(command.index("--permission-mode"), at)

    def test_implement_mode_accepts_edits(self):
        resolved = self.provider.resolve_model({"family": "opus"})
        command = self.provider.build_command(base.MODE_IMPLEMENT, resolved, self.project)
        self.assertEqual(command[command.index("--permission-mode") + 1], "acceptEdits")
        for flag in ("--tools", "--strict-mcp-config", "--restricted"):
            self.assertNotIn(flag, command)

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
        setattr(self.provider, "_capture", lambda command, timeout=30: _FakeCompleted(CLAUDE_HELP))
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


SECRET = "sk-ant-abcdefghijklmnopqrs"


class TestClaudeReadOnlyArguments(IsolatedCase):
    """Raw arguments on a read-only Claude run: `--add-dir <path>` and nothing else."""

    def setUp(self):
        super().setUp()
        self.provider = ClaudeProvider()

    def refused(self, args, source="options.args"):
        return self.provider.refused_read_only_args(args, source)

    def test_each_loosening_spelling_is_refused(self):
        cases = [
            (["--tools", "default"], "'--tools'"),
            (["--tools=default"], "'--tools'"),
            (["--agents", "x"], "'--agents'"),
            (["--plugin-dir", "d"], "'--plugin-dir'"),
            (["--mcp-config", "x.json"], "'--mcp-config'"),
            (["--permission-mode", "acceptEdits"], "'--permission-mode'"),
            (["--allowedTools", "Bash"], "'--allowedTools'"),
            (["--dangerously-skip-permissions"], "'--dangerously-skip-permissions'"),
            (["--restricted"], "'--restricted'"),
            (["Bash"], "a bare value"),
            (["--add-dir"], "has no path after it"),
            (["--add-dir", "--tools"], "'--tools'"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                problems = self.refused(args)
                self.assertTrue(problems)
                self.assertIn(expected, " ".join(problems))
                self.assertIn("only --add-dir <path>", problems[0])

    def test_add_dir_is_accepted_in_both_spellings_and_repeated(self):
        for args in (["--add-dir", "../x"], ["--add-dir=../x"], ["--add-dir", "a", "--add-dir", "b"]):
            with self.subTest(args=args):
                self.assertEqual(self.refused(args), [])

    def test_a_refusal_never_shows_the_value(self):
        helper = '{"apiKeyHelper":"%s"}' % SECRET
        for args in (["--settings", helper], ["--settings=" + helper]):
            with self.subTest(args=args):
                message = " ".join(self.refused(args))
                self.assertNotIn("sk-ant", message)
                self.assertIn("'--settings'", message)
                self.assertIn("token 1 of %d in options.args" % len(args), message)

    def test_the_source_is_named(self):
        self.assertIn("token 2 of 2 in --extra", self.refused(["--add-dir", "--x"], "--extra")[1])


class TestCodexReadOnlyArguments(IsolatedCase):
    """Codex takes no raw arguments on a read-only run, however spelled."""

    def test_every_spelling_is_refused_by_name_only(self):
        provider = CodexProvider()
        cases = [
            (["-s", "workspace-write"], ["'-s'", "a bare value"]),
            (["-sdanger-full-access"], ["'-s'"]),
            (["--sandbox=workspace-write"], ["'--sandbox'"]),
            (["-a", "never"], ["'-a'"]),
            (["--ask-for-approval=never"], ["'--ask-for-approval'"]),
            (["--full-auto"], ["'--full-auto'"]),
            (["--dangerously-bypass-approvals-and-sandbox"], ["'--dangerously-bypass-approvals-and-sand"]),
            (["-c", "sandbox_mode=danger-full-access"], ["'-c'", "a bare value"]),
            (["-c=sandbox_mode=danger-full-access"], ["'-c'"]),
            (["-p", "prof"], ["'-p'"]),
            (["--profile=prof"], ["'--profile'"]),
            (["x"], ["a bare value"]),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                problems = provider.refused_read_only_args(args, "options.args")
                self.assertEqual(len(problems), len(args))
                message = " ".join(problems)
                for name in expected:
                    self.assertIn(name, message)
                for value in ("workspace-write", "danger-full-access", "never", "prof"):
                    self.assertNotIn(value, message.replace("'--profile'", ""))
                self.assertIn("read-only codex runs accept no raw arguments", problems[0])


class TestReadOnlyArgProblems(IsolatedCase):
    """The one list of raw-argument problems, and the refusal built from it."""

    def setUp(self):
        super().setUp()
        self.provider = CodexProvider()
        self.options = {"args": ["--full-auto"]}
        self.extra = ["-s", "workspace-write"]

    def test_options_args_come_before_extra(self):
        for mode in base.READ_ONLY_MODES:
            with self.subTest(mode=mode):
                problems = self.provider.read_only_arg_problems(mode, self.extra, self.options)
                self.assertEqual(
                    problems,
                    self.provider.refused_read_only_args(["--full-auto"], "options.args")
                    + self.provider.refused_read_only_args(self.extra, "--extra"),
                )
                self.assertIn("in options.args", problems[0])
                self.assertIn("in --extra", problems[-1])

    def test_only_extra_offends_with_no_options(self):
        # The common path: a seat with no options passes None.
        expected = self.provider.refused_read_only_args(self.extra, "--extra")
        self.assertTrue(expected)
        for mode in base.READ_ONLY_MODES:
            for options in (None, {}, {"args": []}):
                with self.subTest(mode=mode, options=options):
                    problems = self.provider.read_only_arg_problems(mode, self.extra, options)
                    self.assertEqual(problems, expected)
                    self.assertTrue(all("in --extra" in problem for problem in problems))
            with self.subTest(mode=mode, options="omitted"):
                self.assertEqual(self.provider.read_only_arg_problems(mode, self.extra), expected)

    def test_only_options_offend_with_no_extra(self):
        expected = self.provider.refused_read_only_args(["--full-auto"], "options.args")
        self.assertTrue(expected)
        for mode in base.READ_ONLY_MODES:
            for extra in ([], ()):
                with self.subTest(mode=mode, extra=extra):
                    problems = self.provider.read_only_arg_problems(mode, extra, self.options)
                    self.assertEqual(problems, expected)
                    self.assertTrue(all("in options.args" in problem for problem in problems))
            with self.subTest(mode=mode, extra="omitted"):
                self.assertEqual(self.provider.read_only_arg_problems(mode, options=self.options), expected)

    def test_the_refusal_says_the_same_problems_on_one_line(self):
        for mode in base.READ_ONLY_MODES:
            with self.subTest(mode=mode):
                problems = self.provider.read_only_arg_problems(mode, self.extra, self.options)
                refusal = present(self.provider.read_only_refusal(mode, self.extra, self.options))
                self.assertEqual(refusal.stderr, "; ".join(problems))

    def test_clean_arguments_have_no_problems(self):
        for mode in base.READ_ONLY_MODES:
            for options in (None, {}):
                with self.subTest(mode=mode, options=options):
                    self.assertEqual(self.provider.read_only_arg_problems(mode, [], options), [])
                    self.assertIsNone(self.provider.read_only_refusal(mode, [], options))
            with self.subTest(mode=mode, arguments="omitted"):
                self.assertEqual(self.provider.read_only_arg_problems(mode), [])
                self.assertIsNone(self.provider.read_only_refusal(mode))

    def test_an_implement_run_is_not_held_to_the_allowlist(self):
        self.assertEqual(self.provider.read_only_arg_problems("implement", self.extra, self.options), [])
        self.assertIsNone(self.provider.read_only_refusal("implement", self.extra, self.options))


class TestDescribeRawArgument(unittest.TestCase):
    def test_naming_rules(self):
        describe = base.Provider.describe_raw_argument
        self.assertEqual(describe('--settings={"hooks":1}'), "'--settings'")
        self.assertEqual(describe("--full-auto"), "'--full-auto'")
        self.assertEqual(describe("-sdanger-full-access"), "'-s'")
        self.assertEqual(describe("-c"), "'-c'")
        self.assertEqual(describe("value"), "a bare value")
        self.assertEqual(describe("-"), "a bare value")
        self.assertEqual(describe(""), "a bare value")

    def test_a_long_name_is_cut(self):
        self.assertEqual(base.Provider.describe_raw_argument("--" + "x" * 80), "'%s'" % ("--" + "x" * 38))


class _Recorder:
    """`execution.execute` replaced by a recording, for runs through `Provider.run`."""

    def __init__(self, case):
        self.commands = []
        original = execution.execute
        case.addCleanup(setattr, execution, "execute", original)
        execution.execute = self

    def __call__(self, command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
        self.commands.append(list(command))
        return execution.ExecOutcome(0, "done", "", 0.1)


class TestClaudeReadOnlyRun(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.recorder = _Recorder(self)
        self.provider = ClaudeProvider()
        self.provider.which = lambda: "claude"
        self.provider.version = lambda: ("2.1.283 (Claude Code)", None)
        self.help(CLAUDE_HELP)

    def help(self, stdout, returncode=0, calls=None):
        def capture(command, timeout=30):
            if calls is not None:
                calls.append(list(command))
            if stdout is None:
                return None
            return _FakeCompleted(stdout, "", returncode)

        setattr(self.provider, "_capture", capture)

    def run_claude(self, mode=base.MODE_REVIEW, **kwargs):
        return self.provider.run("prompt", mode, self.project, model_spec={"family": "opus"}, **kwargs)

    def test_a_refused_argument_starts_nothing_and_is_not_echoed(self):
        result = self.run_claude(options={"args": ["--tools", "default"]})
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.invoked)
        self.assertEqual(result.command, ["claude"])
        self.assertEqual(self.recorder.commands, [])
        self.assertIn("'--tools'", result.stderr)
        self.assertNotIn("default", result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)

    def test_add_dir_reaches_the_end_of_the_command(self):
        result = self.run_claude(extra_args=["--add-dir", "../x"])
        self.assertTrue(result.ok)
        command = self.recorder.commands[0]
        self.assertEqual(command[-2:], ["--add-dir", "../x"])
        self.assertIn("--restricted", command)

    def test_implement_forwards_raw_arguments_untouched(self):
        self.run_claude(base.MODE_IMPLEMENT, options={"args": ["--tools", "default"]})
        self.assertEqual(self.recorder.commands[0][-2:], ["--tools", "default"])

    def test_help_is_read_once_for_every_question(self):
        calls = []
        self.help(CLAUDE_HELP, calls=calls)
        self.provider.permission_modes()
        self.provider.list_models()
        self.provider.read_only_enforcement()
        self.provider.resume_support(self.project)
        self.assertEqual(calls, [["claude", "--help"]])

    def test_enforcement_is_verified_by_the_current_help(self):
        report = self.provider.read_only_enforcement()
        self.assertEqual(report["status"], "verified")
        self.assertIn("--restricted", report["mechanism"])
        self.assertIn("--tools Read,Grep,Glob", report["mechanism"])

    def test_an_old_cli_is_unsupported_and_names_only_what_is_missing(self):
        self.help(CLAUDE_HELP_OLD)
        report = self.provider.read_only_enforcement()
        self.assertEqual(report["status"], "unsupported")
        self.assertEqual(report["missing"], ["--tools"])
        self.assertIn("claude 2.1.283", report["detail"])
        result = self.run_claude()
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.invoked)
        self.assertEqual(self.recorder.commands, [])

    def test_unreadable_help_is_unverified_and_refused(self):
        for stdout, code in ((None, 0), ("boom", 1)):
            with self.subTest(returncode=code):
                base.clear_discovery_cache()
                self.help(stdout, code)
                self.assertEqual(self.provider.read_only_enforcement()["status"], "unverified")
                result = self.run_claude()
                self.assertEqual(result.exit_code, 2)
                self.assertFalse(result.invoked)
                self.assertIn("could not read 'claude --help'", result.stderr)

    def test_implement_is_not_held_to_read_only_support(self):
        self.help(None)
        self.assertTrue(self.run_claude(base.MODE_IMPLEMENT).ok)
        self.assertEqual(len(self.recorder.commands), 1)

    def test_a_missing_cli_is_reported_as_missing_not_unverified(self):
        self.provider.which = lambda: None
        self.help(None)
        result = self.run_claude()
        self.assertEqual(result.exit_code, 127)
        self.assertIn("not found on PATH", result.stderr)
        self.assertNotIn("unverified", result.stderr)


PARENT = "33333333-3333-4333-8333-333333333333"
UNLISTED = "1.0.0 (Claude Code)"


class TestClaudeResume(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.provider = ClaudeProvider()
        self.provider.which = lambda: "claude"
        self.provider.version = lambda: (UNLISTED, None)
        self.help(CLAUDE_HELP)

    def help(self, stdout):
        def capture(command, timeout=30):
            return None if stdout is None else _FakeCompleted(stdout, "", 0)

        setattr(self.provider, "_capture", capture)

    def resolved(self):
        return base.ResolvedModel("claude", "opus", "latest", "opus", "opus", "cli-help")

    def record_pass(self, version=UNLISTED, mechanism=READ_ONLY_MECHANISM, checks=None):
        verified.record_pass(
            "claude",
            version,
            mechanism,
            list(verified.REQUIRED_RESUME_CHECKS if checks is None else checks),
            "sonnet",
            self.project,
        )

    def status(self):
        return self.provider.resume_support(self.project)["status"]

    def test_resuming_appends_to_the_read_only_command(self):
        fresh = self.provider.command_line(base.MODE_PLAN, self.resolved(), self.project)
        resumed = self.provider.command_line(
            base.MODE_PLAN, self.resolved(), self.project, resume_session=PARENT
        )
        self.assertEqual(resumed, [*fresh, "--resume=%s" % PARENT, "--fork-session"])
        self.assertEqual(fresh, self.provider.build_command(base.MODE_PLAN, self.resolved(), self.project))
        for flag in ("--permission-mode", "--tools", "--strict-mcp-config", "--restricted"):
            self.assertEqual(resumed.index(flag), fresh.index(flag))

    def test_only_a_uuid_is_resumed(self):
        for value in ("not-a-uuid", "--permission-mode", PARENT + " --tools default"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.provider.resume_args(value)

    def test_an_unlisted_version_is_unverified(self):
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "unverified")
        self.assertIn(UNLISTED, support["detail"])
        self.assertIn("smoke_live.py", support["detail"])

    def test_the_built_in_table_is_used_as_it_stands(self):
        # 2.1.283 passed every required check before release, so it resumes
        # on a machine that never ran them; so does a version newer than the
        # last entry, on trust. One older than every entry does not.
        self.provider.version = lambda: ("2.1.283 (Claude Code)", None)
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "verified")
        self.assertEqual(support["source"], "built-in")
        self.provider.version = lambda: ("2.1.282 (Claude Code)", None)
        self.assertEqual(self.status(), "unverified")
        self.provider.version = lambda: ("2.1.286 (Claude Code)", None)
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "trusted")
        self.assertEqual(support["newer_than"], "2.1.285 (Claude Code)")
        self.assertEqual(support["source"], "built-in")

    def test_a_newer_version_is_trusted_and_says_it_was_not_checked(self):
        self.provider.version = lambda: ("2.1.286 (Claude Code)", None)
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["version"], "2.1.286 (Claude Code)")
        last = claude_module.VERIFIED_RESUME["2.1.285 (Claude Code)"]
        self.assertEqual(support["verified_at"], last["verified_at"])
        self.assertIn("is trusted to resume as newer than 2.1.285 (Claude Code)", support["detail"])
        self.assertIn("itself has not been checked", support["detail"])
        self.assertIn("smoke_live.py --provider claude", support["detail"])

    def test_a_resume_record_vouches_for_the_fresh_read_only_flags(self):
        self.assertEqual(self.provider.resume_mechanism(), READ_ONLY_MECHANISM)
        self.assertEqual(self.provider.required_resume_checks, verified.REQUIRED_RESUME_CHECKS)

    def test_the_help_gate_comes_before_trust(self):
        self.provider.version = lambda: ("2.1.286 (Claude Code)", None)
        self.help(CLAUDE_HELP_NO_FORK)
        self.assertEqual(self.status(), "unsupported")

    def test_a_local_failure_blocks_every_newer_version(self):
        verified.record_fail("claude", "2.1.284 (Claude Code)", ["resumes read-only"], self.project)
        self.provider.version = lambda: ("2.1.286 (Claude Code)", None)
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "unverified")
        self.assertIn("claude 2.1.284 (Claude Code), newer than the last pass", support["detail"])
        self.assertIn("failed the resume check here", support["detail"])

    def test_an_unreadable_record_trusts_nothing_newer(self):
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(self.project, ".ai", "home")
        self.provider.version = lambda: ("2.1.286 (Claude Code)", None)
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "unverified")
        self.assertIn("could not be read", support["detail"])
        self.assertIn("only versions in the built-in table resume", support["detail"])
        self.provider.version = lambda: ("2.1.285 (Claude Code)", None)
        self.assertEqual(self.status(), "verified")

    def test_a_built_in_entry_needs_the_current_mechanism(self):
        entry = {
            "verified_at": "2026-09-27",
            "read_only_mechanism": READ_ONLY_MECHANISM,
            "checks": list(verified.REQUIRED_RESUME_CHECKS),
            "source": "test",
        }
        self.patch_table({UNLISTED: entry})
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "verified")
        self.assertEqual(support["source"], "built-in")
        self.patch_table({UNLISTED: dict(entry, read_only_mechanism="--other")})
        self.assertEqual(self.status(), "unverified")

    def patch_table(self, table):
        original = claude_module.VERIFIED_RESUME
        claude_module.VERIFIED_RESUME = table
        self.addCleanup(setattr, claude_module, "VERIFIED_RESUME", original)

    def test_a_record_verifies_and_a_failure_unverifies(self):
        self.record_pass()
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "verified")
        self.assertEqual(support["source"], "record")
        verified.record_fail("claude", UNLISTED, ["resumes read-only"], self.project)
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "unverified")
        self.assertIn("failed the resume check on this machine", support["detail"])

    def test_a_local_failure_outranks_the_table(self):
        self.patch_table(
            {
                UNLISTED: {
                    "verified_at": "2026-09-27",
                    "read_only_mechanism": READ_ONLY_MECHANISM,
                    "checks": list(verified.REQUIRED_RESUME_CHECKS),
                }
            }
        )
        self.assertEqual(self.status(), "verified")
        verified.record_fail("claude", UNLISTED, ["resumes read-only"], self.project)
        self.assertEqual(self.status(), "unverified")

    def test_an_incomplete_record_does_not_verify(self):
        self.record_pass(mechanism="--other")
        self.assertEqual(self.status(), "unverified")
        self.record_pass(checks=verified.REQUIRED_RESUME_CHECKS[:6])
        self.assertEqual(self.status(), "unverified")

    def test_a_record_is_held_to_the_checks_the_adapter_requires(self):
        """As for Codex: ``required_resume_checks`` is what a record has to
        list, not the module default."""
        fewer = verified.REQUIRED_RESUME_CHECKS[:6]
        self.record_pass(checks=fewer)
        self.assertEqual(self.status(), "unverified")
        setattr(self.provider, "required_resume_checks", fewer)
        self.assertEqual(self.status(), "verified")

    def test_the_help_has_to_list_both_flags(self):
        self.help(CLAUDE_HELP_NO_FORK)
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "unsupported")
        self.assertEqual(support["missing"], ["--fork-session"])
        base.clear_discovery_cache()
        self.help(CLAUDE_HELP_NO_RESUME)
        missing = self.provider.resume_support(self.project)["missing"]
        self.assertEqual(missing, ["--resume", "--fork-session"])

    def test_unreadable_help_or_version_is_unverified(self):
        self.help(None)
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "unverified")
        self.assertIn("--help", support["detail"])
        base.clear_discovery_cache()
        self.help(CLAUDE_HELP)
        self.provider.version = lambda: (None, "boom")
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "unverified")
        self.assertIn("--version", support["detail"])

    def test_the_answer_follows_the_record_without_clearing_a_cache(self):
        self.assertEqual(self.status(), "unverified")
        self.record_pass()
        self.assertEqual(self.status(), "verified")
        verified.record_fail("claude", UNLISTED, ["resumes read-only"], self.project)
        self.assertEqual(self.status(), "unverified")


class TestVerifiedResumeTable(unittest.TestCase):
    def test_every_entry_vouches_for_the_current_flags(self):
        for version, entry in claude_module.VERIFIED_RESUME.items():
            with self.subTest(version=version):
                self.assertRegex(version, r"^\d+\.\d+\.\d+ \(Claude Code\)$")
                self.assertEqual(
                    entry.get("read_only_mechanism"),
                    READ_ONLY_MECHANISM,
                    "re-run scripts/smoke_live.py --provider claude and update VERIFIED_RESUME",
                )
                for name in verified.REQUIRED_RESUME_CHECKS:
                    self.assertIn(name, entry.get("checks") or [])


class TestVerifiedCodexResumeTable(unittest.TestCase):
    def test_every_entry_vouches_for_the_current_flags(self):
        provider = CodexProvider()
        for version, entry in codex_module.VERIFIED_RESUME.items():
            with self.subTest(version=version):
                self.assertRegex(version, r"^codex-cli \d+\.\d+\.\d+$")
                self.assertEqual(
                    entry.get("read_only_mechanism"),
                    provider.resume_mechanism(),
                    "re-run scripts/smoke_live.py --provider codex and update VERIFIED_RESUME",
                )
                for name in provider.required_resume_checks:
                    self.assertIn(name, entry.get("checks") or [])


class TestReadOnlyEnforcementReports(IsolatedCase):
    def test_codex_is_partial_and_says_why(self):
        report = CodexProvider().read_only_enforcement()
        self.assertEqual(report["status"], "partial")
        self.assertIn("MCP", report["detail"])

    def test_agy_is_unenforced_warned_and_static(self):
        report = AgyProvider().read_only_enforcement()
        self.assertEqual(report["status"], "unenforced")
        self.assertIn("unenforced", base.ENFORCEMENT_STATUSES)
        self.assertIn("unenforced", base.WARNED_ENFORCEMENT)
        self.assertNotIn("unenforced", base.REFUSED_ENFORCEMENT)
        self.assertIn(".git/", report["detail"])
        self.assertIn("nothing checks afterwards", report["detail"])
        self.assertTrue(AgyProvider.static_enforcement)
        self.assertFalse(CodexProvider.static_enforcement)

    def test_mock_is_verified(self):
        self.assertEqual(MockProvider().read_only_enforcement()["status"], "verified")

    def test_the_base_reports_nothing(self):
        self.assertEqual(base.Provider().read_only_enforcement()["status"], "unspecified")


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
        setattr(self.provider, "_capture", lambda command, timeout=30: _FakeCompleted(stdout, "", returncode))

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

        setattr(self.provider, "_capture", capture)
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


CODEX_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "codex")
#: The thread ids in the recordings: the parent, its read-only fork and its
#: workspace-write fork.
CODEX_PARENT = "01a0f266-8141-7e62-a53b-8a09a8cf13a3"
CODEX_FORK = "01a0f266-f964-7df1-87f1-bc4cb2b13683"
CODEX_LOOSE_FORK = "01a0f267-79f4-7ae2-a147-11018d834baa"
CODEX_MISSING = "00000000-0000-4000-8000-000000000000"
#: The working directory the recordings name, replaced by each test's own.
CODEX_RECORDED_CWD = "/sandbox/probe181"
#: ``codex exec fork --help`` as codex-cli 0.156.1 printed it.
with open(os.path.join(CODEX_FIXTURES, "fork-help.txt"), encoding="utf-8") as _handle:
    CODEX_FORK_HELP = _handle.read()


def codex_fixture(name):
    with open(os.path.join(CODEX_FIXTURES, name), "r", encoding="utf-8") as handle:
        return handle.read()


class _CodexExecute:
    """`execution.execute` replaced by a Codex that prints ``stdout`` and
    writes ``answer`` to its ``-o`` file."""

    def __init__(self, case, stdout="", answer="READY", exit_code=0, stderr=""):
        self.stdout = stdout
        self.answer = answer
        self.exit_code = exit_code
        self.stderr = stderr
        self.commands = []
        case.addCleanup(setattr, execution, "execute", execution.execute)
        execution.execute = self

    def __call__(self, command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
        self.commands.append(list(command))
        if self.answer and "-o" in command:
            with open(command[command.index("-o") + 1], "w", encoding="utf-8") as handle:
                handle.write(self.answer)
        return execution.ExecOutcome(self.exit_code, self.stdout, self.stderr, 0.1)


class TestCodexResume(IsolatedCase):
    """Forking a Codex session: the command, the refusals and the verdict."""

    def setUp(self):
        super().setUp()
        self.codex_home = os.path.join(self.tmp, "codex-home")
        saved = os.environ.get("CODEX_HOME")
        self.addCleanup(self.restore_codex_home, saved)
        os.environ["CODEX_HOME"] = self.codex_home
        self.provider = CodexProvider()
        self.provider.which = lambda: "codex"
        self.provider.version = lambda: ("codex-cli 0.156.1", None)
        self.provider.configured_model = lambda: "gpt-6-sol"
        self.fork_help(CODEX_FORK_HELP)

    @staticmethod
    def restore_codex_home(saved):
        if saved is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = saved

    def fork_help(self, stdout):
        def capture(command, timeout=30):
            if list(command) == ["codex", "exec", "fork", "--help"] and stdout is not None:
                return _FakeCompleted(stdout, "", 0)
            return _FakeCompleted("", "", 1)

        base.clear_discovery_cache()
        setattr(self.provider, "_capture", capture)

    def rollout(self, fixture, thread_id, cwd=None, day=None, **meta):
        """Write ``fixture`` as ``thread_id``'s rollout, in today's directory
        unless ``day`` says otherwise, with this test's workspace as its cwd."""
        day = day or datetime.date.today()
        directory = os.path.join(
            self.codex_home, "sessions", "%04d" % day.year, "%02d" % day.month, "%02d" % day.day
        )
        os.makedirs(directory, exist_ok=True)
        lines = []
        for line in codex_fixture(fixture).splitlines():
            record = json.loads(line)
            payload = record.get("payload")
            if isinstance(payload, dict):
                if payload.get("cwd") == CODEX_RECORDED_CWD:
                    payload["cwd"] = self.project if cwd is None else cwd
                if record.get("type") == "session_meta":
                    payload.update(meta)
            lines.append(json.dumps(record))
        path = os.path.join(directory, "rollout-2026-09-30T13-00-30-%s.jsonl" % thread_id)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def parent_here(self, **kwargs):
        # An old directory: the parent is found by walking the tree.
        return self.rollout("parent-rollout.jsonl", CODEX_PARENT, day=datetime.date(2026, 1, 2), **kwargs)

    def fork_output(self, thread_id=CODEX_FORK):
        return codex_fixture("fork-json-read-only.jsonl").replace(CODEX_FORK, thread_id)

    def resolved(self, argument: Optional[str] = "gpt-example-3"):
        return base.ResolvedModel("codex", "x", "latest", argument, argument or "codex default", "test")

    def fork_command(self, resolved):
        extra = ["-o", "last.txt"]
        return self.provider.command_line(base.MODE_PLAN, resolved, self.project, extra, None, CODEX_PARENT)

    def run_codex(self, mode=base.MODE_PLAN, family="gpt-6-sol", **kwargs):
        return self.provider.run("prompt", mode, self.project, model_spec={"family": family}, **kwargs)

    def fork(self, **kwargs):
        return self.run_codex(resume_session=CODEX_PARENT, **kwargs)

    # -- the command -------------------------------------------------------

    def test_plan_runs_print_json_events_and_review_runs_do_not(self):
        plan = self.provider.build_command(base.MODE_PLAN, self.resolved(), self.project)
        review = self.provider.build_command(base.MODE_REVIEW, self.resolved(), self.project)
        self.assertIn("--json", plan)
        self.assertNotIn("--json", review)
        self.assertEqual(plan[plan.index("-s") + 1], "read-only")

    def test_the_fork_command(self):
        expected = [
            "codex",
            "exec",
            "fork",
            CODEX_PARENT,
            "-",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "-c",
            'sandbox_mode="read-only"',
            "-m",
            "gpt-example-3",
            "--json",
            "-o",
            "last.txt",
        ]
        self.assertEqual(self.fork_command(self.resolved()), expected)
        # The CLI-default family: the model config.toml names, which the
        # parent ran under and --ignore-user-config would otherwise drop.
        command = self.fork_command(self.resolved(None))
        self.assertEqual(command[command.index("-m") + 1], "gpt-6-sol")
        for flag in ("-s", "-C", "--color"):
            self.assertNotIn(flag, command)

    def test_only_a_uuid_is_forked(self):
        for value in ("not-a-uuid", "--last", CODEX_PARENT + " -s danger-full-access"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.provider.resume_args(value)

    def test_a_raw_sandbox_override_is_still_refused(self):
        executed = _CodexExecute(self)
        self.parent_here()
        result = self.fork(extra_args=["-c", 'sandbox_mode="danger-full-access"'])
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.invoked)
        self.assertFalse(result.resume_rejected)
        self.assertNotIn("danger-full-access", result.stderr)
        self.assertEqual(executed.commands, [])

    # -- the session id ----------------------------------------------------

    def outcome(self, stdout, exit_code=0, stderr=""):
        return execution.ExecOutcome(exit_code, stdout, stderr, 0.1)

    def test_the_session_is_the_first_thread_started(self):
        recorded = codex_fixture("exec-json-ready.jsonl")
        self.assertEqual(self.provider.parse_session(self.outcome(recorded)), {"session_id": CODEX_PARENT})
        second = recorded + '{"type":"thread.started","thread_id":"%s"}\n' % CODEX_FORK
        self.assertEqual(self.provider.parse_session(self.outcome(second))["session_id"], CODEX_PARENT)

    def test_a_thread_id_that_is_not_a_uuid_is_no_session(self):
        for value in ("not-a-uuid", "*", "[0-9]*", CODEX_PARENT + "/../x"):
            with self.subTest(value=value):
                stdout = codex_fixture("exec-json-ready.jsonl").replace(CODEX_PARENT, value)
                self.assertEqual(self.provider.parse_session(self.outcome(stdout)), {})

    def test_a_fresh_plan_run_reads_no_rollout(self):
        executed = _CodexExecute(self, stdout=codex_fixture("exec-json-ready.jsonl"))
        result = self.run_codex()
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(result.stdout, "READY")
        self.assertEqual(result.session_id, CODEX_PARENT)
        self.assertIsNone(result.session_init)
        self.assertIn("--json", executed.commands[0])
        self.assertFalse(os.path.exists(self.codex_home))

    # -- refused before the fork -------------------------------------------

    def assert_refused(self, result, executed, text):
        self.assertFalse(result.ok)
        self.assertFalse(result.invoked)
        self.assertTrue(result.resume_rejected)
        self.assertEqual(result.command, ["codex"])
        self.assertIn(text, result.stderr)
        self.assertEqual(executed.commands, [])

    def test_no_model_to_pass_is_refused(self):
        executed = _CodexExecute(self, stdout=self.fork_output())
        self.parent_here()
        self.provider.configured_model = lambda: None
        result = self.fork(family="recommended-coding")
        self.assert_refused(result, executed, "needs a model; set model.family for the architect")

    def test_a_parent_from_another_workspace_is_refused(self):
        executed = _CodexExecute(self, stdout=self.fork_output())
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        self.parent_here(cwd=other)
        result = self.fork()
        self.assert_refused(result, executed, "the session to resume was not started in this workspace")
        self.assertNotIn(other, result.stderr)
        self.assertNotIn(self.project, result.stderr)

    def test_a_parent_with_no_rollout_is_refused(self):
        executed = _CodexExecute(self, stdout=self.fork_output())
        self.assert_refused(self.fork(), executed, "not started in this workspace")

    def test_a_missing_cli_is_reported_missing_not_as_a_refused_session(self):
        executed = _CodexExecute(self, stdout=self.fork_output())
        self.provider.which = lambda: None
        result = self.fork()
        self.assertEqual(result.exit_code, 127)
        self.assertFalse(result.resume_rejected)
        self.assertIn("not found on PATH", result.stderr)
        self.assertEqual(executed.commands, [])

    def test_a_rollout_naming_another_thread_is_refused(self):
        executed = _CodexExecute(self, stdout=self.fork_output())
        self.parent_here(id=CODEX_FORK)
        self.assert_refused(self.fork(), executed, "not started in this workspace")

    def test_the_same_workspace_spelled_differently_is_accepted(self):
        executed = _CodexExecute(self, stdout=self.fork_output())
        self.rollout("fork-rollout-read-only.jsonl", CODEX_FORK)
        os.makedirs(os.path.join(self.project, "sub"))
        self.parent_here(cwd=os.path.join(self.project, "sub", ".."))
        result = self.fork()
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(len(executed.commands), 1)

    # -- judged after the fork ---------------------------------------------

    def test_a_read_only_fork_is_confirmed_from_its_rollout(self):
        _CodexExecute(self, stdout=self.fork_output(), answer="I could not create it.")
        self.parent_here()
        self.rollout("fork-rollout-read-only.jsonl", CODEX_FORK)
        result = self.fork()
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(result.stdout, "I could not create it.")
        self.assertEqual(result.session_id, CODEX_FORK)
        self.assertEqual(result.session_init, {"sandbox_policy": "read-only", "approval_policy": "never"})

    def assert_unconfirmed(self, result, text):
        self.assertFalse(result.ok)
        self.assertTrue(result.invoked)
        self.assertEqual(result.stdout, "")
        self.assertIsNone(result.session_init)
        self.assertIn("could not be confirmed read-only (%s)" % text, result.stderr)
        self.assertIn("its output is not used", result.stderr)

    def test_a_workspace_write_fork_fails(self):
        _CodexExecute(self, stdout=self.fork_output(CODEX_LOOSE_FORK))
        self.parent_here()
        self.rollout("fork-rollout-workspace-write.jsonl", CODEX_LOOSE_FORK)
        self.assert_unconfirmed(self.fork(), "workspace-write")

    def test_the_parents_history_is_not_the_forks_verdict(self):
        _CodexExecute(self, stdout=self.fork_output())
        self.parent_here()
        self.rollout("fork-rollout-parent-history.jsonl", CODEX_FORK)
        self.assert_unconfirmed(self.fork(), "rollout does not name the parent")

    def test_a_fork_of_another_parent_fails(self):
        _CodexExecute(self, stdout=self.fork_output())
        self.parent_here()
        self.rollout("fork-rollout-read-only.jsonl", CODEX_FORK, forked_from_id=CODEX_MISSING)
        self.assert_unconfirmed(self.fork(), "rollout does not name the parent")

    def test_a_fork_with_no_rollout_fails(self):
        _CodexExecute(self, stdout=self.fork_output())
        self.parent_here()
        self.assert_unconfirmed(self.fork(), "no rollout for the fork")

    def test_a_thread_id_with_glob_characters_never_reaches_the_glob(self):
        _CodexExecute(self, stdout=self.fork_output("*"))
        self.parent_here()
        self.rollout("fork-rollout-read-only.jsonl", CODEX_FORK)
        patterns = []
        original = codex_module.glob.glob

        def recording(pattern, *args, **kwargs):
            patterns.append(pattern)
            return original(pattern, *args, **kwargs)

        self.addCleanup(setattr, codex_module.glob, "glob", original)
        setattr(codex_module.glob, "glob", recording)
        self.assert_unconfirmed(self.fork(), "no rollout for the fork")
        self.assertTrue(patterns)
        for pattern in patterns:
            self.assertTrue(pattern.endswith("-%s.jsonl" % CODEX_PARENT), pattern)

    # -- the answer --------------------------------------------------------

    def test_an_empty_final_message_fails_a_plan_run(self):
        _CodexExecute(self, stdout=codex_fixture("exec-json-ready.jsonl"), answer="")
        result = self.run_codex()
        self.assertFalse(result.ok)
        self.assertEqual(result.stdout, "")
        self.assertIn("codex printed no final message (-o was empty)", result.stderr)

    def test_an_empty_final_message_keeps_the_fallback_in_review(self):
        _CodexExecute(self, stdout="the whole transcript", answer="")
        result = self.run_codex(base.MODE_REVIEW)
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "the whole transcript")

    # -- usage ---------------------------------------------------------------

    def test_usage_comes_from_the_json_events_without_cache_reads(self):
        outcome = self.outcome(codex_fixture("exec-json-ready.jsonl"))
        usage = self.provider.parse_usage(outcome, base.MODE_PLAN)
        assert usage is not None
        self.assertEqual(usage.input_tokens, 3523)
        self.assertEqual(usage.cache_read_tokens, 11904)
        self.assertEqual(usage.output_tokens, 5)
        self.assertEqual(usage.billed_tokens, 3528)
        self.assertEqual(usage.source, "codex json events")
        # A review's output is a transcript, which may quote such an event.
        self.assertIsNone(self.provider.parse_usage(outcome, base.MODE_REVIEW))

    def test_the_prose_footer_is_still_read(self):
        usage = self.provider.parse_usage(self.outcome("answer\ntokens used\n3,877\n"), base.MODE_REVIEW)
        assert usage is not None
        self.assertEqual(usage.total_tokens, 3877)
        self.assertEqual(usage.source, "codex output")

    # -- a missing session -------------------------------------------------

    def test_a_missing_thread_is_rejected(self):
        stderr = codex_fixture("fork-missing-session.stderr")

        def rejected(stdout, exit_code, text, session=CODEX_MISSING):
            outcome = self.outcome(stdout, exit_code, text)
            return self.provider.resume_rejected(outcome, base.MODE_PLAN, None, session)

        self.assertTrue(rejected("", 1, stderr))
        self.assertFalse(rejected("", 1, stderr, CODEX_PARENT))
        self.assertFalse(rejected("", 1, "Error: something else"))
        self.assertFalse(rejected("", 0, stderr))
        self.assertFalse(rejected(codex_fixture("exec-json-ready.jsonl"), 1, stderr))

    # -- whether it resumes ------------------------------------------------

    def resumes(self):
        self.provider.supports_resume = True

    def patch_table(self, table):
        original = codex_module.VERIFIED_RESUME
        codex_module.VERIFIED_RESUME = table
        self.addCleanup(setattr, codex_module, "VERIFIED_RESUME", original)

    def passing_entry(self):
        return {
            "verified_at": "2026-10-01T00:00:00Z",
            "read_only_mechanism": self.provider.resume_mechanism(),
            "checks": list(self.provider.required_resume_checks),
        }

    def test_it_does_not_resume_while_the_flag_is_off(self):
        setattr(self.provider, "supports_resume", False)
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "unsupported")
        self.assertEqual(support["detail"], "codex does not resume sessions")

    def test_a_fork_help_without_ignore_user_config_is_unsupported(self):
        self.resumes()
        self.fork_help(CODEX_FORK_HELP.replace("--ignore-user-config", ""))
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "unsupported")
        self.assertEqual(support["missing"], ["--ignore-user-config"])

    def test_an_unreadable_fork_help_is_unverified(self):
        self.resumes()
        self.fork_help(None)
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "unverified")
        self.assertIn("codex exec fork --help", support["detail"])

    def test_the_table_verifies_and_a_newer_version_is_trusted(self):
        self.resumes()
        self.patch_table({})
        self.assertEqual(self.provider.resume_support(self.project)["status"], "unverified")
        self.patch_table({"codex-cli 0.156.1": self.passing_entry()})
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "verified")
        self.assertEqual(support["source"], "built-in")
        self.provider.version = lambda: ("codex-cli 0.157.0", None)
        support = self.provider.resume_support(self.project)
        self.assertEqual(support["status"], "trusted")
        self.assertEqual(support["newer_than"], "codex-cli 0.156.1")
        self.assertIn("smoke_live.py --provider codex", support["detail"])

    def test_a_claude_shaped_entry_does_not_count(self):
        self.resumes()
        entry = dict(self.passing_entry(), checks=list(verified.REQUIRED_RESUME_CHECKS))
        self.patch_table({"codex-cli 0.156.1": entry})
        self.assertEqual(self.provider.resume_support(self.project)["status"], "unverified")

    def test_what_a_resume_record_vouches_for(self):
        self.assertEqual(
            self.provider.required_resume_checks,
            (
                "stays read-only",
                "resumes read-only",
                "forks the session",
                "reports a missing session",
                "ignores repository config on resume",
            ),
        )
        self.assertEqual(
            self.provider.resume_mechanism(),
            '--ignore-user-config -c sandbox_mode="read-only" '
            "(fork; filesystem sandbox confirmed from the fork's rollout)",
        )
        fresh = self.provider.read_only_enforcement()["mechanism"]
        self.assertNotEqual(self.provider.resume_mechanism(), fresh)


#: ``agy models`` as 1.2.13 prints it: a progress line, then ``id<TAB>name``.
#: The ids are fixtures for the ordering rules, not a catalogue.
AGY_MODELS = (
    "Fetching available models...\n"
    "gemini-3.7-flash-high\tGemini 3.7 Flash (high)\n"
    "gemini-3.8-flash\tGemini 3.8 Flash\n"
    "gemini-3.8-flash-medium\tGemini 3.8 Flash (medium)\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (high)\n"
    "gemini-3.8-flash-low\tGemini 3.8 Flash (low)\n"
    "gemini-2.9-pro-high\tGemini 2.9 Pro (high)\n"
    "gemini-3.1-pro-low\tGemini 3.1 Pro (low)\n"
    "gemini-3.1-pro-high\tGemini 3.1 Pro (high)\n"
)


def agy_result(**fields):
    """One JSON result line, as ``agy --output-format json`` prints it."""
    payload = {"conversation_id": "conv-1", "status": "SUCCESS", "response": "READY"}
    payload.update(fields)
    return json.dumps(payload) + "\n"


#: A thinking model's measured usage: total = input + output, with the
#: thinking already inside output.
AGY_USAGE = {
    "input_tokens": 12527,
    "output_tokens": 215,
    "thinking_tokens": 212,
    "cache_read_tokens": 300,
    "total_tokens": 12742,
}


class _AgyCase(IsolatedCase):
    """An agy adapter whose CLI is faked: nothing is ever spawned."""

    def setUp(self):
        super().setUp()
        self.provider = AgyProvider()
        self.provider.which = lambda: None
        setattr(self.provider, "_capture", lambda command, timeout=30: self.fail("spawned %s" % command))
        self.seen = {}
        self.addCleanup(setattr, execution, "execute", execution.execute)

    def installed(self, models: Optional[str] = AGY_MODELS, returncode: int = 0):
        self.provider.which = lambda: "agy"
        self.provider.version = lambda: ("1.2.13", None)
        self.captures = []

        def capture(command, timeout=30):
            self.captures.append(list(command))
            return None if models is None else _FakeCompleted(models, "", returncode)

        setattr(self.provider, "_capture", capture)

    def answer(self, stdout=None, exit_code=0, stderr="", observe=None):
        """Replace the child process with one that prints ``stdout``."""
        printed = agy_result() if stdout is None else stdout

        def execute(command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
            self.seen["command"] = list(command)
            self.seen["stdin"] = prompt
            if observe is not None:
                observe(command, cwd)
            return execution.ExecOutcome(exit_code, printed, stderr, 0.5)

        execution.execute = execute

    def resolve(self, family):
        return self.provider.resolve_model({"family": family, "version": "latest"})


class TestAgyAdapter(_AgyCase):
    def test_models_are_the_tab_lines_after_the_default(self):
        self.installed()
        candidates = self.provider.list_models()
        self.assertEqual(candidates[0].family, "default")
        self.assertEqual(candidates[0].value, "")
        self.assertEqual(candidates[1].value, "gemini-3.7-flash-high")
        self.assertEqual(candidates[1].label, "Gemini 3.7 Flash (high)")
        self.assertEqual(candidates[1].source, "cli-catalog")
        self.assertNotIn("Fetching available models...", [c.value for c in candidates])
        self.assertEqual(self.captures, [["agy", "models"]])

    def test_a_failing_or_silent_models_command_falls_back(self):
        for models, returncode in ((AGY_MODELS, 1), (None, 0), ("Fetching available models...\n", 0)):
            with self.subTest(models=models, returncode=returncode):
                providers.clear_discovery_cache()
                self.installed(models, returncode)
                candidates = self.provider.list_models()
                self.assertEqual([c.family for c in candidates], ["default"])
                self.assertEqual(candidates[0].source, "builtin-fallback")

    def test_default_omits_the_model_flag(self):
        for family in ("default", "", "recommended", "auto"):
            with self.subTest(family=family):
                resolved = self.resolve(family)
                self.assertIsNone(resolved.argument)
                self.assertEqual(resolved.source, "cli-default")
                command = self.provider.build_command(base.MODE_IMPLEMENT, resolved, self.project)
                self.assertNotIn("--model", command)

    def test_a_family_picks_the_newest_version_then_high(self):
        self.installed()
        self.assertEqual(self.resolve("gemini-flash").argument, "gemini-3.8-flash-high")
        self.assertEqual(self.resolve("gemini-pro").argument, "gemini-3.1-pro-high")
        resolved = self.resolve("gemini-flash")
        command = self.provider.build_command(base.MODE_IMPLEMENT, resolved, self.project)
        self.assertEqual(command[command.index("--model") + 1], "gemini-3.8-flash-high")

    def test_no_suffix_comes_before_medium_and_low(self):
        self.installed("gemini-3.8-flash-low\tlow\ngemini-3.8-flash-medium\tmid\ngemini-3.8-flash\tplain\n")
        self.assertEqual(self.resolve("gemini-flash").argument, "gemini-3.8-flash")

    def test_a_suffixed_family_keeps_its_suffix(self):
        self.installed()
        self.assertEqual(self.resolve("gemini-flash-medium").argument, "gemini-3.8-flash-medium")
        self.assertEqual(self.resolve("gemini-flash-low").argument, "gemini-3.8-flash-low")
        self.assertEqual(self.resolve("gemini-pro-low").argument, "gemini-3.1-pro-low")
        self.assertEqual(self.resolve("gemini-pro-high").argument, "gemini-3.1-pro-high")

    def test_a_listed_id_passes_through(self):
        self.installed()
        resolved = self.resolve("gemini-3.7-flash-high")
        self.assertEqual(resolved.argument, "gemini-3.7-flash-high")
        self.assertEqual(resolved.source, "cli-catalog")

    def test_an_unlisted_name_is_refused(self):
        self.installed()
        for family in ("gemini-9.9-flash", "gemini-pro-medium", "something-else"):
            with self.subTest(family=family):
                with self.assertRaises(base.ModelResolutionError) as ctx:
                    self.resolve(family)
                self.assertIn("does not list", str(ctx.exception))
                self.assertIn("model list --provider agy", str(ctx.exception))

    def test_offline_only_the_default_resolves(self):
        with self.assertRaises(base.ModelResolutionError):
            self.resolve("gemini-flash")
        self.assertIsNone(self.resolve("default").argument)

    def test_pinned_passes_through(self):
        resolved = self.provider.resolve_model({"family": "x", "version": "pinned", "id": "gemini-pinned"})
        self.assertEqual(resolved.argument, "gemini-pinned")

    def test_the_models_are_fetched_once(self):
        self.installed()
        for family in ("gemini-flash", "gemini-pro", "gemini-flash-low"):
            self.resolve(family)
        with self.assertRaises(base.ModelResolutionError):
            self.resolve("gemini-9.9-flash")
        self.assertEqual(len(self.captures), 1)

    def test_auth_is_not_detected(self):
        state, detail = self.provider.auth_status()
        self.assertEqual(state, "unknown")
        self.assertIn("run `agy` once", detail)

    def test_the_contract_attributes(self):
        self.assertEqual(self.provider.name, "agy")
        self.assertFalse(self.provider.streams_progress)
        self.assertFalse(self.provider.supports_resume)
        self.assertEqual(self.provider.local_only_options, ("skip_permissions",))
        self.assertIsInstance(providers.get_provider("agy"), AgyProvider)


class TestAgyRoleOptions(_AgyCase):
    def setUp(self):
        super().setUp()
        self.resolved = self.resolve("default")

    def test_implement_asks_for_json_with_p_last(self):
        command = self.provider.build_command(base.MODE_IMPLEMENT, self.resolved, self.project)
        self.assertEqual(command[:3], ["agy", "--output-format", "json"])
        self.assertEqual(command[-2], "-p")
        for flag in ("--mode", "--sandbox", "--dangerously-skip-permissions"):
            self.assertNotIn(flag, command)

    def test_without_a_run_p_still_gets_a_value(self):
        """`--print-command` builds the command with no run: `-p` alone exits 2."""
        command = self.provider.build_command(base.MODE_IMPLEMENT, self.resolved, self.project)
        self.assertEqual(command[-2], "-p")
        self.assertIn(agy_module.PROMPT_FILE_PLACEHOLDER, command[-1])
        self.assertIn("carry out the instructions in it exactly", command[-1])

    def test_skip_permissions_true_adds_the_flag_to_the_run(self):
        """Through run(), with the child replaced: the flag reaches the argv."""
        self.installed()
        self.answer()
        result = self.provider.run(
            "do it", base.MODE_IMPLEMENT, self.project, options={"skip_permissions": True}
        )
        self.assertTrue(result.ok)
        command = self.seen["command"]
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertEqual(command[-2], "-p")
        self.assertIn("carry out the instructions in it exactly", command[-1])

    def test_skip_permissions_false_or_absent_adds_nothing(self):
        for options in ({"skip_permissions": False}, {}, None):
            with self.subTest(options=options):
                command = self.provider.build_command(
                    base.MODE_IMPLEMENT, self.resolved, self.project, options=options
                )
                self.assertNotIn("--dangerously-skip-permissions", command)

    def test_implement_keeps_raw_arguments_before_p(self):
        command = self.provider.build_command(
            base.MODE_IMPLEMENT, self.resolved, self.project, ["--extra-flag"], {"args": ["--from-config"]}
        )
        self.assertEqual(command[-4:-1], ["--from-config", "--extra-flag", "-p"])

    def test_read_only_modes_drop_the_bypass_and_pass_no_mode(self):
        for mode in (base.MODE_PLAN, base.MODE_REVIEW):
            with self.subTest(mode=mode):
                command = self.provider.build_command(
                    mode, self.resolved, self.project, options={"skip_permissions": True}
                )
                self.assertEqual(command[:-1], ["agy", "--output-format", "json", "-p"])

    def test_raw_arguments_on_a_read_only_run_are_refused(self):
        self.installed()
        self.answer()
        result = self.provider.run("x", base.MODE_REVIEW, self.project, options={"args": ["--x"]})
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.invoked)
        self.assertNotIn("command", self.seen)

    def test_skip_permissions_must_be_a_bool(self):
        problems = self.provider.validate_options({"skip_permissions": "yes"})
        self.assertTrue(any("true or false" in p for p in problems), problems)
        self.assertEqual(self.provider.validate_options({"skip_permissions": True}), [])

    def test_a_short_prompt_is_never_on_the_command_line_and_stdin_is_empty(self):
        """Any local process can read a command line; the file is the owner's."""
        self.installed()
        seen = {}

        def observe(command, cwd):
            match = re.search(r"\.ai/(agy-prompt-[^ ]+\.md)", command[-1])
            assert match is not None, command[-1]
            with open(os.path.join(cwd, ".ai", match.group(1)), encoding="utf-8") as handle:
                seen["text"] = handle.read()

        self.answer(observe=observe)
        result = self.provider.run("Reply READY", base.MODE_REVIEW, self.project)
        self.assertFalse(any("Reply READY" in token for token in self.seen["command"]))
        self.assertEqual(seen["text"], "Reply READY")
        self.assertEqual(self.seen["stdin"], "")
        self.assertEqual(result.usage.prompt_chars, len("Reply READY"))


LONG_PROMPT = "a prompt much longer than ten characters"


class TestAgyPromptFile(_AgyCase):
    """The prompt goes into the workspace's `.ai/`, in a file `-p` names."""

    def setUp(self):
        super().setUp()
        self.ai = os.path.join(self.project, ".ai")
        os.makedirs(self.ai, exist_ok=True)
        self.stale = os.path.join(self.ai, "agy-prompt-stale.md")
        self.keep = os.path.join(self.ai, "keep.md")
        for path in (self.stale, self.keep):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("left over\n")
        self.during = {}

    def observe(self, command, cwd):
        match = re.search(r"\.ai/(agy-prompt-[^ ]+\.md)", command[-1])
        assert match is not None, command[-1]
        path = os.path.join(cwd, ".ai", match.group(1))
        self.during["path"] = path
        with open(path, encoding="utf-8") as handle:
            self.during["text"] = handle.read()

    def test_the_prompt_is_read_from_a_file_that_is_gone_afterwards(self):
        self.installed()
        self.answer(observe=self.observe)
        result = self.provider.run(LONG_PROMPT, base.MODE_IMPLEMENT, self.project)
        self.assertTrue(result.ok)
        self.assertEqual(self.during["text"], LONG_PROMPT)
        self.assertIn("carry out the instructions in it exactly", self.seen["command"][-1])
        self.assertNotIn(LONG_PROMPT, self.seen["command"])
        self.assertFalse(os.path.exists(self.during["path"]))
        self.assertEqual(result.usage.prompt_chars, len(LONG_PROMPT))

    def test_the_file_is_named_after_this_process(self):
        self.installed()
        self.answer(observe=self.observe)
        self.provider.run(LONG_PROMPT, base.MODE_REVIEW, self.project)
        self.assertTrue(os.path.basename(self.during["path"]).startswith("agy-prompt-%d-" % os.getpid()))

    def test_only_a_file_whose_process_is_gone_is_removed_first(self):
        """Reviewers run in parallel, in this process and in others: a file
        another run may still be reading is never removed."""
        live, gone = 4242, 4243
        original = execution.pid_alive
        self.addCleanup(setattr, execution, "pid_alive", original)
        setattr(execution, "pid_alive", lambda pid: pid == live)
        names = {
            "live": "agy-prompt-%d-abc.md" % live,
            "gone": "agy-prompt-%d-abc.md" % gone,
            "mine": "agy-prompt-%d-abc.md" % os.getpid(),
        }
        old = time.time() - agy_module.ORPHAN_MIN_AGE_SECONDS - 60
        for name in names.values():
            path = os.path.join(self.ai, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("in use\n")
            os.utime(path, (old, old))
        self.installed()
        self.answer(observe=self.observe)
        self.provider.run(LONG_PROMPT, base.MODE_REVIEW, self.project)
        left = set(os.listdir(self.ai))
        self.assertNotIn(names["gone"], left)
        for kept in (names["live"], names["mine"], "agy-prompt-stale.md", "keep.md"):
            self.assertIn(kept, left)

    def test_a_recent_file_of_a_process_that_reads_as_gone_is_kept(self):
        """A pid that cannot be opened -- another user's, an elevated one --
        reads as gone; a file it wrote recently may still be in use."""
        original = execution.pid_alive
        self.addCleanup(setattr, execution, "pid_alive", original)
        setattr(execution, "pid_alive", lambda pid: False)
        recent = "agy-prompt-4244-abc.md"
        with open(os.path.join(self.ai, recent), "w", encoding="utf-8") as handle:
            handle.write("in use\n")
        self.installed()
        self.answer(observe=self.observe)
        self.provider.run(LONG_PROMPT, base.MODE_REVIEW, self.project)
        self.assertIn(recent, os.listdir(self.ai))

    def test_a_prompt_that_cannot_be_written_leaves_no_file(self):
        original = os.fdopen

        def failing(handle, *args, **kwargs):
            stream = original(handle, *args, **kwargs)

            class Broken:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    stream.close()
                    return False

                def write(self, text):
                    stream.write(text[:10])
                    raise OSError("disk full")

            return Broken()

        self.addCleanup(setattr, agy_module.os, "fdopen", original)
        setattr(agy_module.os, "fdopen", failing)
        with self.assertRaises(OSError):
            agy_module._write_prompt_file(self.project, LONG_PROMPT)
        self.assertEqual(sorted(os.listdir(self.ai)), ["agy-prompt-stale.md", "keep.md"])

    def test_a_linked_ai_directory_is_refused_and_nothing_outside_is_touched(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        victim = os.path.join(outside, "agy-prompt-1-x.md")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("not ours\n")
        shutil.rmtree(self.ai)
        make_dir_link(self.ai, outside)
        # tearDown, which runs first, may already have removed it with the tree.
        self.addCleanup(lambda: os.path.lexists(self.ai) and remove_link(self.ai))
        self.installed()
        self.answer()
        result = self.provider.run(LONG_PROMPT, base.MODE_REVIEW, self.project)
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.invoked)
        self.assertIn("only inside the workspace", result.stderr)
        self.assertNotIn("command", self.seen)
        self.assertEqual(os.listdir(outside), ["agy-prompt-1-x.md"])

    def test_the_file_is_removed_when_the_run_raises(self):
        self.installed()

        def execute(command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
            self.observe(command, cwd)
            raise RuntimeError("child exploded")

        execution.execute = execute
        with self.assertRaises(RuntimeError):
            self.provider.run(LONG_PROMPT, base.MODE_IMPLEMENT, self.project)
        self.assertFalse(os.path.exists(self.during["path"]))

    def test_a_missing_cli_writes_nothing(self):
        result = self.provider.run(LONG_PROMPT, base.MODE_IMPLEMENT, self.project)
        self.assertEqual(result.exit_code, 127)
        self.assertEqual(sorted(os.listdir(self.ai)), ["agy-prompt-stale.md", "keep.md"])

    def test_parallel_runs_on_one_adapter_each_name_their_own_file(self):
        """Reviewers share one adapter across threads. Both runs write their
        file before either builds its command, and each command must still
        name the file its own run wrote."""
        self.installed()
        barrier = threading.Barrier(2, timeout=10)
        resolve = self.provider.resolve_model

        def resolve_together(model_spec=None):
            barrier.wait()
            return resolve(model_spec)

        setattr(self.provider, "resolve_model", resolve_together)
        read = []

        def execute(command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
            match = re.search(r"\.ai/(agy-prompt-[^ ]+\.md)", command[-1])
            assert match is not None, command[-1]
            with open(os.path.join(cwd, ".ai", match.group(1)), encoding="utf-8") as handle:
                read.append(handle.read())
            return execution.ExecOutcome(0, agy_result(), "", 0.5)

        execution.execute = execute
        prompts = ["%s, the first" % LONG_PROMPT, "%s, the second" % LONG_PROMPT]
        threads = [
            threading.Thread(target=self.provider.run, args=(prompt, base.MODE_REVIEW, self.project))
            for prompt in prompts
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(read), sorted(prompts))


class TestAgyOutput(_AgyCase):
    def setUp(self):
        super().setUp()
        self.installed()

    def run_agy(self, mode=base.MODE_IMPLEMENT):
        return self.provider.run("prompt", mode, self.project)

    def test_a_success_is_its_response_usage_and_conversation(self):
        self.answer(agy_result(usage=AGY_USAGE))
        result = self.run_agy()
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "READY")
        self.assertEqual(result.session_id, "conv-1")
        self.assertEqual(result.warnings, [])
        usage = result.usage
        # Thinking is already inside output: measured, so it is not added.
        self.assertEqual((usage.input_tokens, usage.output_tokens), (12527, 215))
        self.assertEqual(usage.cache_read_tokens, 300)
        self.assertIsNone(usage.total_tokens)
        self.assertIsNone(usage.cost_usd)
        self.assertEqual(usage.source, "agy json result")
        self.assertEqual(usage.billed_tokens, 12527 + 215)

    def test_denied_actions_are_a_warning_on_success_too(self):
        denied = [{"action": "run_command", "display_name": "Run command"}]
        self.answer(agy_result(denied_actions=denied))
        result = self.run_agy()
        self.assertTrue(result.ok)
        self.assertEqual(len(result.warnings), 1)
        warning = result.warnings[0]
        self.assertIn("agy denied 1 action(s): Run command (run_command)", warning)
        self.assertIn("options.skip_permissions: true in the global config", warning)
        self.assertIn(warning, result.stderr)
        self.assertEqual(result.to_dict()["warnings"], [warning])

    def test_exit_3_keeps_the_partial_response_and_names_the_error(self):
        printed = "AGY_ERROR: the tool call failed\n" + agy_result(
            status="ERROR", response="partial answer", error="Error: tool failed"
        )
        self.answer(printed, exit_code=3)
        result = self.run_agy()
        self.assertFalse(result.ok)
        self.assertEqual(result.stdout, "partial answer")
        self.assertIn("AGY_ERROR: the tool call failed", result.stderr)
        self.assertTrue(result.stderr.strip().endswith("Error: tool failed"))
        self.assertIn("agy reported status ERROR", result.warnings)

    def test_an_empty_response_keeps_the_raw_output(self):
        printed = agy_result(conversation_id="", status="ERROR", response="", error="Error: empty prompt.")
        self.answer(printed, exit_code=1, stderr="error: Error: empty prompt.\n")
        result = self.run_agy()
        self.assertFalse(result.ok)
        self.assertEqual(result.stdout, printed)
        self.assertEqual(result.stderr.count("Error: empty prompt."), 1)
        self.assertIsNone(result.session_id)

    def test_output_that_is_not_json_passes_through(self):
        self.answer("plain words\n", stderr="some noise\n")
        result = self.run_agy()
        self.assertEqual(result.stdout, "plain words\n")
        self.assertEqual(result.stderr, "some noise\n")
        self.assertFalse(result.usage.measured)
        self.assertEqual(result.warnings, [])

    def test_a_result_without_usage_is_unmeasured(self):
        self.answer(agy_result())
        self.assertFalse(self.run_agy().usage.measured)

    def test_a_negative_count_is_not_a_count(self):
        """As for Claude and Codex: a count below zero is unreported, not
        subtracted from a total."""
        self.answer(agy_result(usage=dict(AGY_USAGE, input_tokens=-5)))
        usage = self.run_agy().usage
        self.assertIsNone(usage.input_tokens)
        self.assertEqual(usage.output_tokens, 215)

    def test_a_review_run_is_not_refused_and_says_so(self):
        self.answer()
        result = self.run_agy(base.MODE_REVIEW)
        self.assertTrue(result.ok)
        self.assertTrue(result.invoked)
        self.assertEqual(len(result.warnings), 1)
        self.assertTrue(result.warnings[0].startswith("read-only is NOT enforced by agy -- "))
        self.assertIn(agy_module.AGY_UNENFORCED, result.warnings[0])
        self.assertTrue(result.stderr.startswith("read-only is NOT enforced by agy"))

    def test_an_implement_run_has_no_enforcement_warning(self):
        self.answer()
        self.assertEqual(self.run_agy(base.MODE_IMPLEMENT).warnings, [])


class TestRunWarnings(IsolatedCase):
    def test_a_run_result_carries_its_warnings_redacted(self):
        result = base.RunResult(True, 0, "", "", [], 0.0, warnings=["key sk-ant-abcdefghijklmnopqrs"])
        self.assertEqual(result.warnings, ["key [redacted]"])
        self.assertEqual(result.to_dict()["warnings"], ["key [redacted]"])
        self.assertEqual(base.RunResult(True, 0, "", "", [], 0.0).to_dict()["warnings"], [])

    def test_the_base_hook_says_nothing(self):
        outcome = execution.ExecOutcome(0, "", "", 0.0)
        self.assertEqual(base.Provider().run_warnings(outcome, base.MODE_REVIEW), [])


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
    """The patterns themselves are tested with ``workspace.redact``."""

    def test_run_results_redact_both_streams(self):
        result = base.RunResult(
            True,
            0,
            "sk-ant-abcdefghijklmnopqrs",
            "sk-abcdefghijklmnop123",
            [],
            0.0,
            warnings=["token ghp_abcdefghijklmnopqrstuvwxyz01"],
        )
        self.assertNotIn("abcdefghijklmnopqrs", result.stdout)
        self.assertNotIn("abcdefghijklmnop123", result.stderr)
        self.assertEqual(result.warnings, ["token [redacted]"])


if __name__ == "__main__":
    unittest.main()
