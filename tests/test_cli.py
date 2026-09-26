"""End-to-end behaviour of the ``dev-orchestra`` command."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git

from orchestrator import cli, providers
from orchestrator import config as config_mod
from orchestrator import workspace as ws

FINDING = """## Finding
- Severity: high
- File: app.py
- Line: 2
- Category: correctness
- Problem: subtraction was used where addition was intended
- Impact: every caller gets the wrong total
- Evidence: return a - b
- Recommended fix: restore the addition
"""


def read_file(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def run_cli(*argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestConfigCommands(IsolatedCase):
    def test_show_without_a_config_uses_defaults(self):
        code, out, _ = run_cli("config", "show")
        self.assertEqual(code, 0)
        self.assertIn("built-in defaults", out)
        self.assertIn("claude / sonnet / latest", out)

    def test_setup_defaults_writes_a_config(self):
        code, out, _ = run_cli("config", "setup", "--defaults")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(config_mod.global_config_path()))
        self.assertIn("Saved global configuration", out)

    def test_setup_without_a_tty_refuses_instead_of_hanging(self):
        saved = sys.stdin
        sys.stdin = io.StringIO("")  # not a TTY, and EOF immediately
        try:
            code, _, err = run_cli("config", "setup")
        finally:
            sys.stdin = saved
        self.assertEqual(code, 2)
        self.assertIn("--defaults", err)

    def test_set_updates_one_value(self):
        run_cli("config", "setup", "--defaults")
        code, _, _ = run_cli("config", "set", "implementer.model.family", "sonnet")
        self.assertEqual(code, 0)
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.role("implementer")["model"]["family"], "sonnet")

    def test_set_coerces_numbers(self):
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "review.max_review_iterations", "3")
        self.assertEqual(config_mod.load(self.project).review_settings()["max_review_iterations"], 3)

    def test_set_turns_the_design_review_on(self):
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "review.design.enabled", "true")
        self.assertIs(config_mod.load(self.project).design_review_settings()["enabled"], True)
        _, out, _ = run_cli("config", "show")
        self.assertIn("design review: on", out)

    def test_show_says_when_the_design_review_is_off(self):
        """It decides whether a whole stage runs, so its state has to be
        visible without reading the JSON."""
        _, out, _ = run_cli("config", "show")
        self.assertIn("design review: off", out)

    def test_show_says_whether_the_plan_needs_approving(self):
        _, out, _ = run_cli("config", "show")
        self.assertIn("plan approval: required", out)
        run_cli("config", "set", "design.require_approval", "false")
        _, out, _ = run_cli("config", "show")
        self.assertIn("plan approval: not required", out)

    def test_project_scope_writes_a_project_file(self):
        code, _, _ = run_cli("config", "set", "--scope", "project", "implementer.provider", "mock")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.project, ".dev-orchestra.yaml")))
        self.assertEqual(config_mod.load(self.project).role("implementer")["provider"], "mock")

    def test_reset_restores_defaults(self):
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.model.family", "sonnet")
        run_cli("config", "reset", "--scope", "global")
        self.assertEqual(config_mod.load(self.project).role("implementer")["model"]["family"], "opus")

    def test_reset_delete_removes_the_file(self):
        run_cli("config", "setup", "--defaults")
        run_cli("config", "reset", "--scope", "global", "--delete")
        self.assertFalse(os.path.isfile(config_mod.global_config_path()))

    def test_validate_reports_problems_and_exit_code(self):
        config_mod.write_config_file(
            config_mod.global_config_path(),
            dict(config_mod.default_config(), implementer={"provider": "nope"}),
        )
        code, out, _ = run_cli("config", "validate")
        self.assertEqual(code, 1)
        self.assertIn("unknown provider", out)

    def test_path_reports_both_layers(self):
        code, out, _ = run_cli("config", "path")
        self.assertEqual(code, 0)
        self.assertIn("global:", out)
        self.assertIn("project:", out)

    def test_show_json_is_machine_readable(self):
        run_cli("config", "setup", "--defaults")
        _, out, _ = run_cli("config", "show", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["config"]["version"], 1)


class TestReviewerCommands(IsolatedCase):
    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")

    def test_add_and_list(self):
        code, out, _ = run_cli("reviewer", "add", "--provider", "codex", "--role", "security")
        self.assertEqual(code, 0)
        self.assertIn("codex-security", out)
        _, listing, _ = run_cli("reviewer", "list")
        self.assertIn("security", listing)

    def test_add_generates_a_unique_id(self):
        run_cli("reviewer", "add", "--provider", "codex", "--role", "security")
        run_cli("reviewer", "add", "--provider", "codex", "--role", "security")
        _, out, _ = run_cli("reviewer", "list", "--json")
        ids = [entry["id"] for entry in json.loads(out)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("codex-security-2", ids)

    def test_duplicate_explicit_id_is_refused(self):
        code, _, err = run_cli("reviewer", "add", "--provider", "codex", "--id", "claude-general")
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)

    def test_remove_by_id(self):
        code, out, _ = run_cli("reviewer", "remove", "codex-general")
        self.assertEqual(code, 0)
        self.assertIn("Removed reviewer codex-general", out)
        _, listing, _ = run_cli("reviewer", "list", "--json")
        self.assertEqual([r["id"] for r in json.loads(listing)], ["claude-general"])

    def test_remove_unknown_reviewer_fails_cleanly(self):
        code, _, err = run_cli("reviewer", "remove", "ghost")
        self.assertEqual(code, 2)
        self.assertIn("no reviewer matches", err)

    def test_removing_every_reviewer_is_allowed(self):
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        _, out, _ = run_cli("reviewer", "list")
        self.assertIn("No reviewers configured", out)

    def test_set_changes_provider_and_role(self):
        code, _, _ = run_cli(
            "reviewer", "set", "codex-general", "--role", "performance", "--provider", "mock"
        )
        self.assertEqual(code, 0)
        _, out, _ = run_cli("reviewer", "list", "--json")
        entry = next(r for r in json.loads(out) if r["id"] == "codex-general")
        self.assertEqual(entry["role"], "performance")
        self.assertEqual(entry["provider"], "mock")

    def test_pin_records_an_explicit_model_id(self):
        run_cli("reviewer", "add", "--provider", "mock", "--id", "pinned", "--pin", "mock-small")
        _, out, _ = run_cli("reviewer", "list", "--json")
        entry = next(r for r in json.loads(out) if r["id"] == "pinned")
        self.assertEqual(entry["model"]["version"], "pinned")
        self.assertEqual(entry["model"]["id"], "mock-small")


class TestDoctor(IsolatedCase):
    def test_doctor_runs_and_reports_roles(self):
        code, out, _ = run_cli("doctor", "--fast")
        self.assertEqual(code, 0)
        self.assertIn("Roles", out)
        self.assertIn("Orchestrator", out)

    def test_doctor_json_never_contains_credentials(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-supersecretvalue123456"
        try:
            _, out, _ = run_cli("doctor", "--fast", "--json")
        finally:
            os.environ.pop("ANTHROPIC_API_KEY")
        self.assertNotIn("supersecretvalue", out)
        payload = json.loads(out)
        self.assertIn("authentication", payload["providers"]["mock"])

    def test_doctor_flags_a_missing_cli(self):
        config_mod.write_config_file(
            config_mod.global_config_path(),
            dict(
                config_mod.default_config(),
                implementer={"provider": "claude", "model": {"family": "opus", "version": "latest"}},
            ),
        )
        from orchestrator.providers.claude import ClaudeProvider

        original = ClaudeProvider.which
        ClaudeProvider.which = lambda self: None
        try:
            _, out, _ = run_cli("doctor", "--fast")
        finally:
            ClaudeProvider.which = original
        self.assertIn("Installed: no", out)
        self.assertIn("CLI is not installed", out)

    def test_doctor_flags_options_that_read_only_roles_ignore(self):
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "architect.options.permission_mode", "bypassPermissions")
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("always run read-only", out)

    def test_doctor_does_not_flag_options_on_the_implementer(self):
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.options.permission_mode", "bypassPermissions")
        _, out, _ = run_cli("doctor", "--fast")
        self.assertNotIn("always run read-only", out)

    def test_strict_mode_exits_non_zero_on_problems(self):
        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        code, _, _ = run_cli("doctor", "--fast", "--strict")
        self.assertEqual(code, 1)


#: A user adapter that raises from whichever method ``RAISE_IN`` names. Its
#: ``which`` and ``version`` are answered in-process, so nothing is spawned.
FLAKY_ADAPTER = """\
import sys

from orchestrator.providers.base import Provider, ResolvedModel

RAISE_IN = %r
_CALLS = 0


class FlakyProvider(Provider):
    name = "flaky"
    display_name = "Flaky"
    executable = "flaky"

    def which(self):
        return sys.executable

    def version(self):
        return "flaky 1", None

    def detect(self):
        if RAISE_IN == "detect":
            raise RuntimeError("detect exploded")
        return super().detect()

    def list_models(self):
        if RAISE_IN == "list_models":
            raise RuntimeError("list_models exploded")
        return super().list_models()

    def _resolve_latest(self, family):
        if RAISE_IN == "resolve":
            raise RuntimeError("resolve exploded")
        if RAISE_IN == "resolve-value":
            raise ValueError("resolve exploded")
        if RAISE_IN == "resolve-type":
            return "not a resolved model"
        return ResolvedModel(self.name, family, "latest", None, "default", "cli-default")

    def build_command(self, mode, resolved, cwd, extra_args=(), options=None):
        return [self.executable]


def build_provider(executable=None):
    global _CALLS
    _CALLS += 1
    if RAISE_IN == "factory" and _CALLS > 1:
        raise RuntimeError("factory exploded")
    return FlakyProvider(executable)
"""


class TestUserProviders(IsolatedCase):
    """User adapters as the commands see them. Nothing here may start a real
    CLI, so the built-in adapters are told theirs are not installed."""

    def setUp(self):
        super().setUp()
        from orchestrator.providers.claude import ClaudeProvider
        from orchestrator.providers.codex import CodexProvider

        for cls in (ClaudeProvider, CodexProvider):
            self.addCleanup(setattr, cls, "which", cls.which)
            cls.which = lambda self: None

    def write_mock_config(self, reviewers=None):
        """Every role on the mock, so the only problems are the ones a test makes."""
        role = {"provider": "mock", "model": {"family": "small", "version": "latest"}}
        data = config_mod.default_config()
        data.update(orchestrator=role, architect=role, implementer=role, review_fixer=role)
        data["reviewers"] = reviewers or [config_mod.make_reviewer("mock-general", "mock", "small")]
        config_mod.write_config_file(config_mod.global_config_path(), data)

    def flaky(self, raise_in, reviewer=True):
        path = self.write_user_provider("flaky", FLAKY_ADAPTER % raise_in)
        self.load_user_providers()
        self.write_mock_config(
            [config_mod.make_reviewer("flaky-general", "flaky", "default")] if reviewer else None
        )
        return path

    def test_doctor_names_where_each_provider_comes_from(self):
        path = self.write_user_provider("mycli")
        self.load_user_providers()
        code, out, _ = run_cli("doctor", "--fast")
        self.assertEqual(code, 0)
        self.assertIn("Source: user module %s" % path, out)
        self.assertIn("Source: built-in", out)
        self.assertIn("User providers", out)
        self.assertIn(os.path.dirname(path), out)
        self.assertIn("imported", out)
        self.assertIn("Imported: mycli", out)

    def test_doctor_json_reports_origins_and_load_errors(self):
        path = self.write_user_provider("mycli")
        broken = self.write_user_provider("broken", "def oops(:\n")
        self.load_user_providers()
        _, out, _ = run_cli("doctor", "--fast", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["providers"]["mycli"]["origin"], {"kind": "user", "path": path})
        self.assertEqual(payload["providers"]["mock"]["origin"]["kind"], "builtin")
        self.assertEqual(payload["user_providers"]["loaded"][0]["path"], path)
        self.assertEqual(payload["user_providers"]["errors"][0]["path"], broken)
        self.assertTrue(any(broken in problem for problem in payload["problems"]))

    def test_strict_doctor_fails_on_a_broken_user_module_alone(self):
        self.write_mock_config()
        code, _, _ = run_cli("doctor", "--fast", "--strict")
        self.assertEqual(code, 0)
        self.write_user_provider("broken", "def oops(:\n")
        self.load_user_providers()
        code, _, _ = run_cli("doctor", "--fast", "--strict")
        self.assertEqual(code, 1)

    def test_doctor_says_when_there_is_no_directory(self):
        self.write_mock_config()
        self.load_user_providers()
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("not present; nothing imported", out)
        self.assertIn("No problems found.", out)

    def test_an_unknown_provider_mentions_modules_that_failed_to_load(self):
        self.write_mock_config([config_mod.make_reviewer("mycli-general", "mycli", "default")])
        self.load_user_providers()
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("unknown provider 'mycli'", out)
        self.assertNotIn("failed to load", out)
        self.write_user_provider("zzz_other", "def oops(:\n")
        self.load_user_providers()
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("1 user provider module(s) failed to load", out)

    def test_an_adapter_raising_in_detect_is_reported_not_raised(self):
        path = self.flaky("detect")
        code, out, _ = run_cli("doctor", "--fast")
        self.assertEqual(code, 0)
        self.assertIn("Adapter error: RuntimeError: detect exploded", out)
        self.assertIn("Installed: unknown (adapter failed)", out)
        self.assertIn(path, out)
        self.assertIn("adapter failed during diagnosis", out)
        self.assertIn("(adapter-error)", out)
        _, out, _ = run_cli("doctor", "--fast", "--json")
        payload = json.loads(out)
        self.assertIn("detect exploded", payload["providers"]["flaky"]["adapter_error"])
        self.assertEqual(payload["providers"]["flaky"]["origin"]["path"], path)
        self.assertEqual(payload["reviewers"][0]["status"], "adapter-error")

    def test_a_factory_raising_after_loading_is_reported_not_raised(self):
        self.flaky("factory")
        code, out, _ = run_cli("doctor", "--fast")
        self.assertEqual(code, 0)
        self.assertIn("factory exploded", out)
        self.assertIn("adapter failed during diagnosis", out)

    def test_an_adapter_raising_in_list_models_is_reported_by_a_full_doctor(self):
        """Without ``--fast``, so the discovery call is actually made."""
        path = self.flaky("list_models")
        code, out, _ = run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("Adapter error: RuntimeError: list_models exploded", out)
        self.assertIn("provider flaky (user module %s): adapter failed during diagnosis" % path, out)
        # The built-in that works is still diagnosed in full.
        self.assertIn("Models (builtin-fallback): mock-small", out)
        _, out, _ = run_cli("doctor", "--json")
        payload = json.loads(out)
        self.assertIn("list_models exploded", payload["providers"]["flaky"]["adapter_error"])
        self.assertEqual(payload["providers"]["flaky"]["origin"], {"kind": "user", "path": path})
        self.assertIn("models", payload["providers"]["mock"])
        self.assertEqual(payload["reviewers"][0]["status"], "adapter-error")
        self.assertTrue(any("list_models exploded" in problem for problem in payload["problems"]))

    def test_an_adapter_raising_while_resolving_is_an_adapter_error(self):
        for raise_in, expected in (("resolve", "resolve exploded"), ("resolve-type", "not a ResolvedModel")):
            with self.subTest(raise_in=raise_in):
                self.flaky(raise_in)
                _, out, _ = run_cli("doctor", "--fast", "--json")
                payload = json.loads(out)
                reviewer = payload["reviewers"][0]
                self.assertEqual(reviewer["status"], "adapter-error")
                self.assertIn(expected, reviewer["error"])
                self.assertNotIn("resolved", reviewer)
                self.assertTrue(any("flaky adapter raised" in problem for problem in payload["problems"]))

    def test_reviewer_add_accepts_a_user_provider(self):
        self.write_user_provider("mycli")
        self.load_user_providers()
        run_cli("config", "setup", "--defaults")
        code, out, err = run_cli("reviewer", "add", "--provider", "mycli", "--role", "general")
        self.assertEqual(code, 0, err)
        self.assertIn("mycli-general", out)
        _, out, _ = run_cli("config", "validate")
        self.assertNotIn("unknown provider", out)

    def test_config_show_names_where_each_provider_comes_from(self):
        path = self.write_user_provider("mycli")
        self.load_user_providers()
        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "add", "--provider", "mycli", "--role", "general")
        _, out, _ = run_cli("config", "show")
        self.assertIn("mycli (user module %s)" % path, out)
        self.assertIn("claude (built-in)", out)
        _, out, _ = run_cli("config", "show", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["providers"]["mycli"], {"kind": "user", "path": path})
        self.assertEqual(payload["providers"]["claude"], {"kind": "builtin", "path": None})

    def test_config_show_points_an_unknown_provider_at_the_directory(self):
        self.write_mock_config([config_mod.make_reviewer("mycli-general", "mycli", "default")])
        _, out, _ = run_cli("config", "show")
        directory = config_mod.user_providers_dir()
        self.assertIn("mycli (no adapter; user adapters load from %s)" % directory, out)

    def test_model_list_reports_a_failing_adapter_and_lists_the_rest(self):
        path = self.flaky("detect", reviewer=False)
        code, out, _ = run_cli("model", "list")
        self.assertEqual(code, 1)
        self.assertIn("flaky: adapter failed (user module %s): RuntimeError: detect exploded" % path, out)
        self.assertIn("mock: installed", out)
        code, out, _ = run_cli("model", "list", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertIn("detect exploded", payload["flaky"]["adapter_error"])
        self.assertEqual(payload["flaky"]["origin"]["path"], path)
        self.assertTrue(payload["mock"]["installed"])
        # Every entry has the same keys, failed or not.
        self.assertEqual(set(payload["flaky"]), set(payload["mock"]))
        self.assertIsNone(payload["mock"]["adapter_error"])
        self.assertEqual(payload["mock"]["origin"], {"kind": "builtin", "path": None})

    def test_model_list_succeeds_for_a_provider_that_works(self):
        self.flaky("detect", reviewer=False)
        code, _, _ = run_cli("model", "list", "--provider", "mock")
        self.assertEqual(code, 0)

    def test_config_set_warns_when_a_user_adapter_raises_while_resolving(self):
        path = self.flaky("resolve", reviewer=False)
        code, _, err = run_cli("config", "set", "implementer.provider", "flaky")
        self.assertEqual(code, 0, err)
        self.assertIn(
            "warning: flaky adapter failed (user module %s): RuntimeError: resolve exploded" % path, err
        )

    def test_config_set_warns_when_a_user_adapter_raises_a_value_error(self):
        """Only an unknown provider goes unmentioned -- validation already says so."""
        path = self.flaky("resolve-value", reviewer=False)
        code, _, err = run_cli("config", "set", "implementer.provider", "flaky")
        self.assertEqual(code, 0, err)
        self.assertIn(
            "warning: flaky adapter failed (user module %s): ValueError: resolve exploded" % path, err
        )

    def test_config_show_says_when_user_adapters_are_switched_off(self):
        self.write_mock_config([config_mod.make_reviewer("mycli-general", "mycli", "default")])
        os.environ[providers.USER_PROVIDERS_DISABLED_ENV] = "1"
        _, out, _ = run_cli("config", "show")
        self.assertIn("(disabled by %s)" % providers.USER_PROVIDERS_DISABLED_ENV, out)
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("user adapters are disabled by %s" % providers.USER_PROVIDERS_DISABLED_ENV, out)


class TestRunCommand(IsolatedCase):
    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")
        run_cli("config", "set", "implementer.model.family", "large")

    def test_print_command_does_not_execute_anything(self):
        code, out, _ = run_cli("run", "implementer", "--print-command")
        self.assertEqual(code, 0)
        self.assertIn("mock implement", out)

    def test_run_writes_output_and_records_state(self):
        target = os.path.join(self.project, "out.txt")
        code, _, _ = run_cli("run", "implementer", "--prompt", "go", "--output", target)
        self.assertEqual(code, 0)
        self.assertIn("mock implement response", read_file(target))
        _, state, _ = run_cli("state", "show", "--json")
        events = json.loads(state)["events"]
        self.assertEqual(events[-1]["stage"], "implementer")
        self.assertEqual(events[-1]["status"], "ok")

    def test_failing_role_returns_non_zero(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        code, _, err = run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 1)
        self.assertIn("implementer failed", err)

    def test_unresolvable_model_stops_before_running(self):
        # The mock provider is always "installed", so this exercises the
        # resolution-failure path even where no real CLI exists (as in CI).
        run_cli("config", "set", "implementer.model.family", "unresolvable")
        code, _, err = run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 2)
        self.assertIn("cannot be resolved", err)

    def test_a_reviewer_id_can_be_run_directly(self):
        run_cli("reviewer", "add", "--provider", "mock", "--id", "solo", "--role", "security")
        code, out, _ = run_cli("run", "solo", "--mode", "review", "--prompt", "review this")
        self.assertEqual(code, 0)
        self.assertIn("NO_FINDINGS", out)


class TestEmptyPromptIsRefused(IsolatedCase):
    """An empty prompt is not a request, so it must not become a run.

    It used to become one: an unreadable `--prompt-file` read as `""` through
    `ws.read_text`'s default, and the provider answered about its own stdin
    after the attempt had already been spent.
    """

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")
        self.started = []
        from orchestrator.providers import mock as mock_mod

        original = mock_mod.MockProvider.run

        def record(provider, *args, **kwargs):
            self.started.append(args[0] if args else kwargs.get("prompt"))
            return original(provider, *args, **kwargs)

        mock_mod.MockProvider.run = record
        self.addCleanup(setattr, mock_mod.MockProvider, "run", original)

    def refusal(self, *argv, stdin=None):
        """Run the CLI over a refused prompt, returning what it complained of."""
        saved = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with self.assertRaises(SystemExit) as caught:
                run_cli(*argv)
        finally:
            sys.stdin = saved
        return str(caught.exception)

    def assert_nothing_was_spent(self):
        payload = json.loads(run_cli("budget", "show", "--json")[1])
        self.assertEqual(payload["budgets"]["implementer"]["used"], 0)
        self.assertEqual(self.started, [])

    def test_a_prompt_file_that_does_not_exist_names_it_and_stops(self):
        missing = os.path.join(self.project, "no-such-plan.md")
        message = self.refusal("run", "implementer", "--prompt-file", missing)
        self.assertIn("does not exist", message)
        self.assertIn(missing, message)
        self.assert_nothing_was_spent()

    def test_a_prompt_file_that_is_empty_says_so_rather_than_missing(self):
        """ "Not there" and "there and empty" are different mistakes."""
        empty = self.write("brief.md", "   \n")
        message = self.refusal("run", "implementer", "--prompt-file", empty)
        self.assertIn("empty", message)
        self.assertNotIn("does not exist", message)
        self.assertIn(empty, message)
        self.assert_nothing_was_spent()

    def test_a_workflow_relative_prompt_file_is_named_as_it_was_written(self):
        """The resolved path is one the caller never typed."""
        message = self.refusal("run", "implementer", "--prompt-file", ".ai/design-request.md")
        self.assertIn(".ai/design-request.md", message)
        self.assertIn("workflows", message)

    def test_an_explicitly_empty_prompt_is_refused_as_an_empty_prompt(self):
        """`--prompt ""` used to be falsy, and fell through to the stdin branch."""
        message = self.refusal("run", "implementer", "--prompt", "", stdin="")
        self.assertIn("--prompt", message)
        self.assert_nothing_was_spent()

    def test_a_pipe_that_carried_nothing_is_refused(self):
        message = self.refusal("run", "implementer", stdin="")
        self.assertIn("stdin", message)
        self.assert_nothing_was_spent()

    def test_an_explicit_stdin_prompt_file_that_carried_nothing_is_refused(self):
        message = self.refusal("run", "implementer", "--prompt-file", "-", stdin="\n\n")
        self.assertIn("stdin", message)
        self.assert_nothing_was_spent()

    def test_a_prompt_file_with_a_prompt_in_it_still_runs(self):
        brief = self.write("brief.md", "implement the thing\n")
        code, out, _ = run_cli("run", "implementer", "--prompt-file", brief)
        self.assertEqual(code, 0)
        self.assertIn("mock implement response", out)
        self.assertEqual(self.started, ["implement the thing\n"])


class TestBudgetsStopLoops(IsolatedCase):
    """The guards must refuse, not advise: a query-only budget stops nothing."""

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")
        run_cli("config", "set", "budgets.test", "2")
        run_cli("config", "set", "budgets.implementer", "2")

    def test_budget_consume_refuses_once_spent(self):
        self.assertEqual(run_cli("budget", "consume", "test")[0], 0)
        self.assertEqual(run_cli("budget", "consume", "test")[0], 0)
        code, _, err = run_cli("budget", "consume", "test")
        self.assertEqual(code, 3)
        self.assertIn("budget of 2", err)

    def test_force_overrides_a_spent_budget(self):
        run_cli("budget", "consume", "test")
        run_cli("budget", "consume", "test")
        self.assertEqual(run_cli("budget", "consume", "test", "--force")[0], 0)

    def test_run_refuses_a_role_whose_budget_is_spent(self):
        for _ in range(2):
            self.assertEqual(run_cli("run", "implementer", "--prompt", "go")[0], 0)
        code, _, err = run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 3)
        self.assertIn("refusing to run implementer", err)

    def test_run_records_its_attempt_in_the_budget(self):
        run_cli("run", "implementer", "--prompt", "go")
        payload = json.loads(run_cli("budget", "show", "--json")[1])
        self.assertEqual(payload["budgets"]["implementer"]["used"], 1)

    def test_budget_reset_starts_a_fresh_workflow(self):
        run_cli("budget", "consume", "test")
        run_cli("budget", "reset")
        payload = json.loads(run_cli("budget", "show", "--json")[1])
        self.assertEqual(payload["budgets"]["test"]["used"], 0)

    def test_progress_record_reports_a_loop_going_nowhere(self):
        first = json.loads(run_cli("progress", "record", "test", "--signature", "3 failed", "--json")[1])
        self.assertFalse(first["stop"])
        second = json.loads(run_cli("progress", "record", "test", "--signature", "3 failed", "--json")[1])
        self.assertTrue(second["stop"])
        self.assertEqual(second["repeats"], 2)

    def test_a_repeated_outcome_then_refuses_the_next_attempt(self):
        run_cli("progress", "record", "test", "--signature", "same")
        run_cli("progress", "record", "test", "--signature", "same")
        code, _, err = run_cli("budget", "consume", "test")
        self.assertEqual(code, 3)
        self.assertIn("without progress", err)

    def test_a_changed_outcome_keeps_the_loop_open(self):
        run_cli("progress", "record", "test", "--signature", "3 failed")
        run_cli("progress", "record", "test", "--signature", "1 failed")
        self.assertEqual(run_cli("budget", "consume", "test")[0], 0)


def spend_the_runtime_budget(workspace, limit=None):
    """Put the runtime budget at its limit without running anything for hours.

    Written straight onto the ledger because the only other way to get there is
    to actually delegate that much execution.
    """
    from orchestrator import ledger as ledger_mod

    settings = dict(ledger_mod.DEFAULT_BUDGETS)
    book = ledger_mod.Ledger(workspace, settings)
    ledger = book.load()
    ledger["runtime_seconds"] = float(limit or settings["max_runtime_seconds"])
    book._write(ledger)
    return book


class TestRuntimeBudgetThroughTheCli(IsolatedCase):
    """The runtime budget charges measured execution, and refuses on it."""

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")
        # Long enough to tell a charge from a rounding error, short enough that
        # the suite does not notice.
        os.environ["DEV_ORCHESTRA_MOCK_DELAY"] = "0.2"

    def test_a_run_charges_what_it_was_measured_to_take(self):
        self.assertEqual(run_cli("run", "implementer", "--prompt", "go")[0], 0)
        event = self.cli_workspace().read_state()["events"][-1]
        self.assertGreater(event["duration_seconds"], 0)
        self.assertEqual(event["charged_seconds"], event["duration_seconds"])
        payload = json.loads(run_cli("budget", "show", "--json")[1])
        self.assertAlmostEqual(payload["runtime"]["used"], event["duration_seconds"], places=2)
        self.assertEqual(payload["runtime"]["limit"], 14400)

    def test_a_run_is_refused_once_the_runtime_budget_is_spent(self):
        spend_the_runtime_budget(self.cli_workspace())
        code, _, err = run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 3)
        self.assertIn("refusing to run implementer", err)
        self.assertIn("delegated", err)
        self.assertEqual(run_cli("run", "implementer", "--prompt", "go", "--force")[0], 0)

    def test_budget_show_reports_runtime_as_delegated_execution(self):
        _, out, _ = run_cli("budget", "show")
        self.assertIn("0/14400s used (delegated execution)", out)

    def test_status_names_the_delegated_budget_when_it_is_spent(self):
        payload = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(payload["runtime"], {"used": 0, "limit": 14400, "remaining": 14400})
        spend_the_runtime_budget(self.cli_workspace())
        payload = json.loads(run_cli("status", "--json")[1])
        self.assertIn("the delegated runtime budget is spent", payload["reasons"])
        self.assertEqual(payload["runtime"]["remaining"], 0)

    def test_half_a_second_left_is_not_reported_as_spent(self):
        """`status` reports the same answer the refusal does, in its reasons and
        in the number it exports. Rounding that number to nearest put the two
        back out of step for the last half-second of the budget -- a reported
        `0` over a budget `run` would still spend from."""
        spend_the_runtime_budget(self.cli_workspace(), limit=14399.6)
        payload = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(payload["runtime_remaining_seconds"], 1)
        self.assertNotIn("the delegated runtime budget is spent", payload["reasons"])


class TestStatusVerdict(IsolatedCase):
    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")

    def test_a_fresh_workflow_says_continue(self):
        payload = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(payload["verdict"], "continue")
        self.assertEqual(payload["reasons"], [])

    def test_a_spent_budget_says_stop_and_report(self):
        run_cli("config", "set", "budgets.test", "1")
        run_cli("budget", "consume", "test")
        payload = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertTrue(any("test" in reason for reason in payload["reasons"]))

    def test_status_reports_and_clears_a_stage_whose_process_died(self):
        from orchestrator import ledger as ledger_mod
        from orchestrator import workspace as workspace_mod

        workspace = workspace_mod.Workspace(self.project).ensure()
        book = ledger_mod.Ledger(workspace, dict(ledger_mod.DEFAULT_BUDGETS))
        token = book.begin("implementer", deadline=3600)
        ledger = book.load()
        ledger["in_flight"][token]["pid"] = 999_999
        book._write(ledger)

        payload = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(payload["abandoned_stages"], ["implementer"])
        self.assertEqual(payload["in_flight"], {})

    def test_status_is_human_readable_too(self):
        code, out, _ = run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("Verdict:", out)
        self.assertIn("Review:", out)


class TestStallReporting(IsolatedCase):
    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")

    def test_a_stalled_run_is_described_as_wedged_not_slow(self):
        """ "Stalled" and "slow" are different diagnoses and must read that way."""
        from orchestrator.providers import mock as mock_mod

        original = mock_mod.MockProvider.run

        def stalled_run(self, *args, **kwargs):
            from orchestrator.providers.base import RunResult

            resolved = self.resolve_model(kwargs.get("model_spec") or {"family": "small"})
            return RunResult(
                False, 125, "", "no output for 300s", ["mock"], 301.0, resolved, stalled=True, idle_for=300.0
            )

        mock_mod.MockProvider.run = stalled_run
        try:
            code, _, err = run_cli("run", "implementer", "--prompt", "go")
        finally:
            mock_mod.MockProvider.run = original
        self.assertEqual(code, 1)
        self.assertIn("stalled", err)
        _, state, _ = run_cli("state", "show", "--json")
        self.assertEqual(json.loads(state)["events"][-1]["status"], "stalled")


class TestOutputGuard(IsolatedCase):
    """``--output`` usually names the file the run was asked to revise.

    Writing it from a bad result destroys the input, which is how a stalled
    Architect replaced a 50KB plan with the fragment it had emitted.
    """

    PLAN = "# Plan\n\nevery section, all of it\n"

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")
        self.target = os.path.join(self.project, "plan.md")
        self.rejected = self.target + ".rejected"
        with open(self.target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(self.PLAN)

    def patch_run(self, **fields):
        """Make the provider return a RunResult the test dictates."""
        from orchestrator.providers import mock as mock_mod
        from orchestrator.providers.base import RunResult

        def fake_run(provider, *args, **kwargs):
            resolved = provider.resolve_model(kwargs.get("model_spec") or {"family": "small"})
            return RunResult(
                fields.get("ok", True),
                fields.get("exit_code", 0),
                fields.get("stdout", ""),
                fields.get("stderr", ""),
                ["mock"],
                1.0,
                resolved,
                timed_out=fields.get("timed_out", False),
                stalled=fields.get("stalled", False),
                idle_for=fields.get("idle_for", 0.0),
            )

        original = mock_mod.MockProvider.run
        mock_mod.MockProvider.run = fake_run
        self.addCleanup(setattr, mock_mod.MockProvider, "run", original)

    def run_returning(self, **fields):
        """Run the implementer with ``--output`` over a dictated result."""
        self.patch_run(**fields)
        return run_cli("run", "implementer", "--prompt", "go", "--output", self.target)

    def test_a_stalled_run_leaves_the_existing_file_alone(self):
        code, _, err = self.run_returning(
            ok=False, exit_code=125, stdout="I'll start by reading", stalled=True, idle_for=300.0
        )
        self.assertEqual(code, 1)
        self.assertEqual(read_file(self.target), self.PLAN)
        self.assertIn("stalled", err)
        self.assertIn("unchanged", err)

    def test_a_failed_run_leaves_the_existing_file_alone(self):
        code, _, err = self.run_returning(ok=False, exit_code=1, stdout="half a plan", stderr="boom")
        self.assertEqual(code, 1)
        self.assertEqual(read_file(self.target), self.PLAN)
        self.assertIn("unchanged", err)

    def test_a_timed_out_run_leaves_the_existing_file_alone(self):
        code, _, _ = self.run_returning(ok=False, exit_code=124, stdout="...", timed_out=True)
        self.assertEqual(code, 1)
        self.assertEqual(read_file(self.target), self.PLAN)

    def test_an_ok_run_that_printed_only_whitespace_writes_nothing(self):
        """A run exiting 0 is not the same as the artifact having been produced."""
        code, _, err = self.run_returning(ok=True, stdout="   \n\n")
        self.assertEqual(code, 1)
        self.assertEqual(read_file(self.target), self.PLAN)
        self.assertIn("unchanged", err)
        self.assertFalse(os.path.exists(self.rejected))

    def test_an_ok_run_with_real_output_writes_as_before(self):
        code, _, err = self.run_returning(ok=True, stdout="# Revised plan\n")
        self.assertEqual(code, 0)
        self.assertEqual(read_file(self.target), "# Revised plan\n")
        self.assertNotIn("unchanged", err)
        self.assertFalse(os.path.exists(self.rejected))

    def test_the_refused_output_is_kept_beside_the_target(self):
        _, _, err = self.run_returning(ok=False, exit_code=1, stdout="I wrote the plan to C:\\...\n")
        self.assertEqual(read_file(self.rejected), "I wrote the plan to C:\\...\n")
        self.assertIn(self.rejected, err)

    def test_a_run_that_printed_nothing_leaves_no_sidecar(self):
        """Including one an earlier attempt left: it is not this run's account."""
        self.write("plan.md.rejected", "what attempt one managed to print\n")
        _, _, err = self.run_returning(ok=False, exit_code=125, stdout="", stalled=True, idle_for=300.0)
        self.assertFalse(os.path.exists(self.rejected))
        self.assertIn(self.target, err)
        self.assertNotIn(".rejected", err)

    def test_a_successful_write_takes_the_previous_attempts_sidecar_with_it(self):
        """Read beside a fresh plan, a stale sidecar reads as a report on it."""
        self.write("plan.md.rejected", "what attempt one managed to print\n")
        code, _, _ = self.run_returning(ok=True, stdout="# Revised plan\n")
        self.assertEqual(code, 0)
        self.assertEqual(read_file(self.target), "# Revised plan\n")
        self.assertFalse(os.path.exists(self.rejected))

    def test_a_sidecar_that_cannot_be_written_costs_only_the_sidecar(self):
        """Preserving the output is the convenience; the failure report is not."""
        os.makedirs(self.rejected)
        code, _, err = self.run_returning(ok=False, exit_code=1, stdout="half a plan\n", stderr="boom")
        self.assertEqual(code, 1)
        self.assertEqual(read_file(self.target), self.PLAN)
        self.assertIn("boom", err)
        self.assertIn("half a plan", err)

    def test_a_run_without_output_still_prints_what_it_produced(self):
        """The guard protects a file from a bad result; a bare run has none."""
        self.patch_run(ok=False, exit_code=1, stdout="half a plan\n", stderr="boom")
        code, out, _ = run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 1)
        self.assertIn("half a plan", out)


class TestASilentRunIsNotASuccess(IsolatedCase):
    """Without `--output` there is no refused write, and there used to be no
    report either: a run that printed whitespace exited 0 over it. Which path
    the caller used says nothing about whether the run answered."""

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")

    def run_returning(self, stdout, stderr=""):
        from orchestrator.providers import mock as mock_mod
        from orchestrator.providers.base import RunResult

        def fake_run(provider, *args, **kwargs):
            resolved = provider.resolve_model(kwargs.get("model_spec") or {"family": "small"})
            return RunResult(True, 0, stdout, stderr, ["mock"], 1.0, resolved)

        original = mock_mod.MockProvider.run
        mock_mod.MockProvider.run = fake_run
        self.addCleanup(setattr, mock_mod.MockProvider, "run", original)
        return run_cli("run", "implementer", "--prompt", "go")

    def test_an_ok_run_that_printed_only_whitespace_exits_non_zero(self):
        code, _, err = self.run_returning("   \n\n")
        self.assertEqual(code, 1)
        self.assertIn("implementer", err)
        self.assertIn("no output", err)

    def test_the_raw_stderr_is_quoted_because_that_is_where_the_refusal_is(self):
        code, _, err = self.run_returning("", stderr="No prompt provided on stdin\n")
        self.assertEqual(code, 1)
        self.assertIn("No prompt provided on stdin", err)

    def test_a_run_that_answered_still_prints_and_exits_zero(self):
        code, out, err = self.run_returning("# Plan\n")
        self.assertEqual(code, 0)
        self.assertIn("# Plan", out)
        self.assertNotIn("no output", err)

    def last_event(self):
        return self.cli_workspace().read_state()["events"][-1]

    def test_the_run_log_says_whether_the_run_answered(self):
        """`status` reads it to tell a revision or fix from an exit 0 over silence."""
        self.run_returning("# Plan\n")
        self.assertIs(self.last_event()["answered"], True)
        self.run_returning("  \n")
        self.assertEqual(self.last_event()["status"], "ok")
        self.assertIs(self.last_event()["answered"], False)

    def test_a_failed_run_never_answered(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(self.last_event()["status"], "failed")
        self.assertIs(self.last_event()["answered"], False)


@unittest.skipUnless(has_git(), "git is required")
class TestReviewPipeline(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write("app.py", "def add(a, b):\n    return a - b\n")

        self.mock_dir = mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(mock_dir)
        with open(os.path.join(mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING)
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = mock_dir

        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m2", "--role", "security")
        # These are about the fan-out: two reviewers, one of them failing, the
        # report surviving it. Below `quality` a change this small is reduced
        # to a single reviewer, which is the saving that level is for and
        # leaves nothing to fan out. The panel logic has its own tests.
        run_cli("config", "set", "optimization.level", "quality")

    def oversize_snapshot(self):
        """Replace the frozen diff with one too large to inline.

        The limit is lowered rather than the body made enormous. Delivery is
        decided by `review.context.inline_chars` now, and a small limit
        exercises the same branch as the shipped one without a 400KB fixture
        -- while also being the case the setting exists for: somebody who will
        not pay for very large prompts, and takes a `partial` round for it.

        The mock provider never reads a prompt, so the snapshot's size is the
        whole of what decides the round's coverage.
        """
        run_cli("config", "set", "review.context.inline_chars", "4000")
        ws.write_text(self.cli_workspace().snapshot_path, "x" * 4_001)

    def test_snapshot_then_review_then_triage_then_fix_brief(self):
        code, out, _ = run_cli("review", "snapshot")
        self.assertEqual(code, 0)
        self.assertIn("app.py", read_file(self.cli_workspace().snapshot_path))

        code, out, _ = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertIn("2 successful, 0 failed", out)

        _, shown, _ = run_cli("review", "show", "--json")
        data = json.loads(shown)
        self.assertEqual(data["counts"]["findings_total"], 1)
        self.assertEqual(data["findings"][0]["triage"], "needs-triage")

        run_cli("review", "triage", "F1", "--status", "accepted", "--note", "confirmed")
        _, brief, _ = run_cli("review", "fix-brief")
        self.assertIn("F1", brief)
        self.assertIn("restore the addition", brief)

    def test_review_run_refuses_a_round_past_the_budget(self):
        run_cli("config", "set", "review.max_review_iterations", "1")
        run_cli("review", "snapshot")
        self.assertEqual(run_cli("review", "run")[0], 0)
        self.write("app.py", "def add(a, b):\n    return a * b\n")
        run_cli("review", "snapshot")
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 3)
        self.assertIn("refusing to run review round", err)
        self.assertIn("only the re-review is refused", err)

    def test_force_runs_a_round_past_the_budget(self):
        run_cli("config", "set", "review.max_review_iterations", "1")
        run_cli("review", "snapshot")
        run_cli("review", "run")
        self.write("app.py", "def add(a, b):\n    return a * b\n")
        run_cli("review", "snapshot")
        self.assertEqual(run_cli("review", "run", "--force")[0], 0)

    def test_an_identical_round_is_called_out(self):
        run_cli("review", "snapshot")
        run_cli("review", "run")
        code, _, err = run_cli("review", "run", "--force")
        self.assertEqual(code, 0)
        self.assertIn("changed nothing", err)

    def test_status_recommends_a_re_review_within_budget(self):
        run_cli("review", "snapshot")
        run_cli("review", "run", "--iteration", "1")
        _, out, _ = run_cli("review", "status", "--json")
        status = json.loads(out)
        self.assertTrue(status["re_review_recommended"])
        self.assertEqual(status["blocking"], ["F1"])

    def test_status_stops_recommending_once_the_budget_is_spent(self):
        run_cli("review", "snapshot")
        run_cli("review", "run", "--iteration", "2")
        _, out, _ = run_cli("review", "status", "--json")
        status = json.loads(out)
        self.assertFalse(status["re_review_recommended"])
        self.assertTrue(status["iteration_budget_exhausted"])

    # -- the round that reaches the limit still gets its fix and re-test --

    def last_round(self):
        run_cli("config", "set", "review_fixer.provider", "mock")
        run_cli("review", "snapshot")
        run_cli("review", "run", "--iteration", "2")
        run_cli("review", "triage", "F1", "--status", "accepted")

    def fix(self):
        return run_cli("run", "review_fixer", "--prompt", "fix")

    def review_status(self):
        return json.loads(run_cli("review", "status", "--json")[1])

    def status(self):
        return json.loads(run_cli("status", "--json")[1])

    def test_status_at_the_limit_asks_for_the_final_fix(self):
        self.last_round()
        self.assertEqual(self.review_status()["final_fix"], "pending")
        self.assertIn("fix the accepted findings once more", run_cli("review", "status")[1])
        payload = self.status()
        self.assertEqual(payload["verdict"], "continue")
        self.assertIs(payload["review"]["final_fix_pending"], True)

    def test_a_fix_after_the_last_round_waits_for_the_re_test(self):
        self.last_round()
        self.assertEqual(self.fix()[0], 0)
        self.assertEqual(self.review_status()["final_fix"], "retest")
        self.assertIn("re-run the tests", run_cli("review", "status")[1])
        payload = self.status()
        self.assertEqual((payload["verdict"], payload["reasons"]), ("continue", []))

    def test_the_last_fixer_attempt_does_not_stop_the_re_test(self):
        self.last_round()
        run_cli("config", "set", "budgets.review_fixer", "1")
        self.fix()
        payload = self.status()
        self.assertEqual(payload["budgets"]["review_fixer"]["remaining"], 0)
        self.assertEqual((payload["verdict"], payload["reasons"]), ("continue", []))
        self.assertEqual(payload["review"]["final_fix"], "retest")
        run_cli("state", "record", "test", "ok")
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertTrue(any("fixed and re-tested" in r for r in payload["reasons"]))

    def assert_the_re_test_ends_the_loop(self, stage, status, level=None):
        self.last_round()
        self.fix()
        run_cli("state", "record", stage, status)
        if level:
            run_cli("config", "set", "optimization.level", level)
        self.assertEqual(self.review_status()["final_fix"], "done")
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        spent = [r for r in payload["reasons"] if r.startswith("review budget spent (2/2 rounds)")]
        self.assertEqual(len(spent), 1)
        self.assertIn("re-tested", spent[0])
        return payload["reasons"]

    def test_a_recorded_re_test_ends_the_loop(self):
        reasons = self.assert_the_re_test_ends_the_loop("test", "ok")
        self.assertNotIn("the last recorded test run failed; fix it before reviewing", reasons)

    def test_a_failed_re_test_ends_the_loop_and_says_so(self):
        # `quality` lets a round run over red tests; `balanced` is where the gate refuses.
        reasons = self.assert_the_re_test_ends_the_loop("test", "failed", level="balanced")
        self.assertIn("the last recorded test run failed; fix it before reviewing", reasons)

    def test_a_re_test_recorded_under_its_own_stage_name_ends_the_loop_too(self):
        self.assert_the_re_test_ends_the_loop("re-test", "ok")

    def test_a_repeated_final_round_still_gets_its_fix(self):
        run_cli("config", "set", "review_fixer.provider", "mock")
        run_cli("review", "snapshot")
        run_cli("review", "run", "--iteration", "1")
        run_cli("review", "triage", "F1", "--status", "accepted")
        self.fix()
        run_cli("review", "run", "--iteration", "2")
        run_cli("review", "triage", "F1", "--status", "accepted")
        payload = self.status()
        self.assertEqual((payload["verdict"], payload["reasons"]), ("continue", []))
        self.assertEqual(payload["review"]["identical_rounds"], 2)
        self.fix()
        run_cli("state", "record", "test", "ok")
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertTrue(any("found exactly what the previous one found" in r for r in payload["reasons"]))
        self.assertTrue(any("fixed and re-tested" in r for r in payload["reasons"]))

    def test_a_failed_fixer_run_does_not_count(self):
        self.last_round()
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        self.assertEqual(self.fix()[0], 1)
        del os.environ["DEV_ORCHESTRA_MOCK_FAIL"]
        self.assertEqual(self.review_status()["final_fix"], "pending")

    def test_an_empty_ok_fixer_run_does_not_count(self):
        self.last_round()
        with open(os.path.join(self.mock_dir, "implement.txt"), "w", encoding="utf-8") as handle:
            handle.write("")
        self.assertEqual(self.fix()[0], 1)
        events = self.cli_workspace().read_state()["events"]
        event = [e for e in events if e.get("stage") == "review_fixer"][-1]
        self.assertEqual(event["status"], "ok")
        self.assertIs(event["answered"], False)
        self.assertEqual(self.review_status()["final_fix"], "pending")

    def test_a_spent_fixer_budget_stops_before_the_fix(self):
        self.last_round()
        run_cli("config", "set", "budgets.review_fixer", "0")
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertTrue(any("no review_fixer attempt left" in r for r in payload["reasons"]))
        self.assertIn("review_fixer has no attempts left", payload["reasons"])
        self.assertIn("no review_fixer attempt is left", run_cli("review", "status")[1])

    def test_a_last_round_with_nothing_accepted_stops_as_before(self):
        """Blocking but not accepted -- untriaged or under investigation --
        leaves nothing to fix, so there is no final fix to wait for."""
        run_cli("config", "set", "review_fixer.provider", "mock")
        run_cli("review", "snapshot")
        run_cli("review", "run", "--iteration", "2")
        for triage in (None, "needs-investigation"):
            if triage:
                run_cli("review", "triage", "F1", "--status", triage)
            self.assertEqual(self.review_status()["final_fix"], "unaccepted")
            self.assertIn("report the remaining findings instead of looping", run_cli("review", "status")[1])
            payload = self.status()
            self.assertEqual(payload["verdict"], "stop-and-report")
            self.assertIs(payload["review"]["final_fix_pending"], False)
            self.assertIn("review budget spent (2/2 rounds) with 1 finding(s) still open", payload["reasons"])

    def test_partial_reviewer_failure_still_produces_a_report(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "Reviewer: m2 |"
        run_cli("review", "snapshot")
        code, out, _ = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertIn("1 successful, 1 failed", out)
        data = json.loads(run_cli("review", "show", "--json")[1])
        self.assertEqual(data["counts"]["reviewers_failed"], 1)
        self.assertEqual(data["counts"]["findings_total"], 1)

    def test_every_reviewer_failing_returns_non_zero(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        run_cli("review", "snapshot")
        code, _, _ = run_cli("review", "run")
        self.assertEqual(code, 1)

    def test_a_round_that_could_not_inline_the_change_is_not_reported_clean(self):
        """Exit 1 for the same reason every reviewer failing does: the round
        produced no claim that the change is fine. The findings are still in
        the report, and still have to be triaged."""
        run_cli("review", "snapshot")
        self.oversize_snapshot()
        code, out, _ = run_cli("review", "run")
        self.assertEqual(code, 1)
        self.assertIn("PARTIAL", out)
        self.assertIn("0 successful, 0 failed, 2 partial (change handed over as a file)", out)
        data = json.loads(run_cli("review", "show", "--json")[1])
        self.assertEqual(data["counts"]["reviewers_partial"], 2)
        self.assertEqual(data["counts"]["reviewers_failed"], 0)
        self.assertEqual(data["counts"]["reviewers_ok"], 0)
        self.assertEqual(data["counts"]["findings_total"], 1)
        self.assertEqual(data["coverage"]["round"], "unverified")
        self.assertEqual(data["coverage"]["unverified_since"], 1)

    def test_the_json_output_separates_partial_from_failed(self):
        run_cli("review", "snapshot")
        self.oversize_snapshot()
        payload = json.loads(run_cli("review", "run", "--json")[1])
        self.assertEqual(payload["partial"], 2)
        self.assertEqual(payload["failed"], 0)
        self.assertEqual(payload["ok"], 0)
        self.assertEqual([r["delivery"] for r in payload["reviewers"]], ["file", "file"])

    def test_an_ordinary_round_reports_its_coverage_and_says_nothing_new(self):
        run_cli("review", "snapshot")
        code, out, _ = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertIn("2 successful, 0 failed", out)
        self.assertNotIn("partial", out)
        coverage = json.loads(run_cli("review", "show", "--json")[1])["coverage"]
        self.assertEqual(coverage["round"], "complete")
        self.assertEqual(coverage["change"], "complete")
        self.assertIsNone(coverage["unverified_since"])
        self.assertGreater(coverage["change_chars"], 0)

    def test_status_reports_coverage_and_what_would_clear_it(self):
        run_cli("review", "snapshot")
        self.oversize_snapshot()
        run_cli("review", "run")
        _, out, _ = run_cli("review", "status")
        self.assertIn("coverage: round unverified, change unverified", out)
        self.assertIn("not a clean review: 2 reviewer(s) partial", out)
        self.assertIn("split the change and review the parts", out)
        # The other way out, and the one the report cannot name for itself:
        # the limit is a setting, so a reader who decides the prompt is worth
        # paying for raises it rather than cutting the change up.
        self.assertIn("raise review.context.inline_chars", out)
        self.assertIn("<= 4,000 chars", out)
        # Narrowing the diff makes the round smaller, not the change reviewed:
        # `--base` changes what is shown and nothing about what was read, so
        # naming it here would be pointing at the way around the mark.
        self.assertNotIn("--base", out)
        status = json.loads(run_cli("review", "status", "--json")[1])
        self.assertEqual(status["coverage"]["change"], "unverified")
        self.assertEqual(status["reviewers_partial"], 2)

    def test_status_says_the_limit_was_raised_rather_than_repeating_itself(self):
        """The reader who took the advice and raised the limit must not be
        handed it again. The recorded size now fits, so the same snapshot
        would be inlined -- "re-running gives the same answer" is false, the
        change no longer needs splitting, and the limit is already raised."""
        run_cli("review", "snapshot")
        self.oversize_snapshot()
        run_cli("review", "run")
        run_cli("config", "set", "review.context.inline_chars", "10000")
        _, out, _ = run_cli("review", "status")
        self.assertIn("under a lower review.context.inline_chars", out)
        self.assertIn("The limit is 10,000 chars now", out)
        self.assertIn("run review run against it again", out)
        self.assertNotIn("gives the same answer", out)
        self.assertNotIn("split the change", out)

    def test_coverage_names_the_limit_that_handed_the_body_over_after_only(self):
        """`--only` merges entries made under two configurations into one
        snapshot's table, so the freshest entry is not necessarily the one
        whose delivery made the round unverified. The pair printed as the
        round's two numbers has to come off the entry that decided the mark --
        4,001 chars against a limit of 10,000 would not have been a file."""
        run_cli("review", "snapshot")
        self.oversize_snapshot()
        run_cli("review", "run")
        run_cli("config", "set", "review.context.inline_chars", "10000")
        self.assertEqual(run_cli("review", "run", "--only", "m1")[0], 0)
        data = json.loads(run_cli("review", "show", "--json")[1])
        by_id = {entry["id"]: entry for entry in data["reviewers"]}
        self.assertEqual(by_id["m1"]["delivery"], "inline")
        self.assertEqual(by_id["m2"]["delivery"], "file")
        self.assertEqual(data["coverage"]["round"], "unverified")
        self.assertEqual(data["coverage"]["change_chars"], 4_001)
        self.assertEqual(data["coverage"]["inline_chars"], 4_000)
        report = read_file(self.cli_workspace().consolidated_md_path)
        self.assertIn("review.context.inline_chars 4,000", report)

    def test_status_survives_an_inline_chars_it_cannot_read(self):
        """`review status` loads with `validate_result=False` on purpose: it
        is the one review command meant to answer while the config is broken.
        Coercing the setting with a bare `int()` made it the one that died on
        one, with a `ValueError` traceback `main` does not catch."""
        run_cli("review", "snapshot")
        self.oversize_snapshot()
        run_cli("review", "run")
        run_cli("config", "set", "--raw", "review.context.inline_chars", "400k")
        code, out, _ = run_cli("review", "status")
        self.assertEqual(code, 0)
        self.assertIn("not a clean review: 2 reviewer(s) partial", out)
        # The shipped default stands in for the value nobody can read, and the
        # advice is built from it like any other number.
        self.assertIn("The limit is 400,000 chars now", out)

    def test_status_and_the_report_agree_about_a_snapshot_nobody_has_run(self):
        """`review snapshot --full` then `review consolidate` -- the state the
        first fix to this created. What is missing is a reviewer, and both
        readers of the report have to say so: telling the reader to re-snapshot
        with --full sends them to redo the thing they just did."""
        run_cli("review", "snapshot")
        self.oversize_snapshot()
        run_cli("review", "run")
        self.write("app.py", "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
        run_cli("review", "snapshot", "--full")
        _, report, _ = run_cli("review", "consolidate")
        _, status, _ = run_cli("review", "status")
        for text in (report, status):
            self.assertIn("change unverified since round 1", text)
            self.assertIn("no reviewer has run against this snapshot", text)
            self.assertIn("review run", text)
            self.assertNotIn("--full", text)
        # The tally beside it counts the snapshot the coverage line is about.
        self.assertIn("- Reviewers: 0 ok / 2 total, 2 partial", report)
        self.assertIn("- Reviewers of this snapshot: 0 ok / 0 total", report)

    def test_status_before_any_review_reports_no_coverage_rather_than_a_clean_one(self):
        status = json.loads(run_cli("review", "status", "--json")[1])
        self.assertEqual(status["coverage"]["round"], "none")
        self.assertEqual(status["coverage"]["change"], "none")
        self.assertIsNone(status["coverage"]["unverified_since"])
        self.assertEqual(status["reviewers_partial"], 0)

    def test_status_does_not_report_an_unrecorded_coverage_as_none(self):
        """A report from before coverage was recorded is a real round with real
        findings. Calling it `round none, change none` claims it was measured
        and found empty -- `render_consolidation` leaves the line out for that
        reason, and the two commands read the same file."""
        run_cli("review", "snapshot")
        run_cli("review", "run")
        path = self.cli_workspace().consolidated_json_path
        data = json.loads(read_file(path))
        data.pop("coverage")
        ws.write_json(path, data)
        status = json.loads(run_cli("review", "status", "--json")[1])
        self.assertIsNone(status["coverage"])
        _, out, _ = run_cli("review", "status")
        self.assertIn("coverage: not recorded", out)
        self.assertNotIn("round none", out)

    def test_a_round_charges_every_reviewer_it_ran(self):
        """Two reviewers in parallel for 0.2s each delegated 0.4s, not 0.2s."""
        os.environ["DEV_ORCHESTRA_MOCK_DELAY"] = "0.2"
        run_cli("review", "snapshot")
        self.assertEqual(run_cli("review", "run")[0], 0)
        event = self.cli_workspace().read_state()["events"][-1]
        expected = sum(reviewer["duration_seconds"] for reviewer in event["reviewers"])
        self.assertEqual(len(event["reviewers"]), 2)
        # Loosely, because the charge is the sum of the raw measurements and
        # the reviewer rows are each rounded: the point is that it is the sum
        # of two, not the length of the batch.
        self.assertAlmostEqual(event["charged_seconds"], expected, places=1)
        payload = json.loads(run_cli("budget", "show", "--json")[1])
        self.assertAlmostEqual(payload["runtime"]["used"], expected, places=1)

    def test_a_round_is_refused_once_the_runtime_budget_is_spent(self):
        """Review is the biggest consumer of runtime and never asked before."""
        run_cli("review", "snapshot")
        workspace = self.cli_workspace()
        spend_the_runtime_budget(workspace)
        before = len(workspace.read_state().get("events") or [])
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 3)
        self.assertIn("refusing to run review", err)
        self.assertIn("delegated", err)
        state = workspace.read_state()
        self.assertEqual(state["ledger"]["in_flight"], {})
        self.assertEqual(len(state.get("events") or []), before)
        self.assertFalse(os.path.exists(workspace.reviewer_report_path("m1")))
        self.assertEqual(run_cli("review", "run", "--force")[0], 0)

    def test_a_round_that_never_starts_is_charged_nothing(self):
        from orchestrator import workspace as workspace_mod

        workspace = self.cli_workspace()
        workspace_mod.write_text(workspace.snapshot_path, "")
        code, _, _ = run_cli("review", "run")
        self.assertEqual(code, 2)
        state = workspace.read_state()
        self.assertEqual(state["events"][-1]["status"], "failed")
        self.assertEqual(state["events"][-1]["charged_seconds"], 0)
        self.assertEqual(state["ledger"]["runtime_seconds"], 0)

    def test_zero_reviewers_skips_the_stage_without_failing(self):
        run_cli("reviewer", "remove", "m1")
        run_cli("reviewer", "remove", "m2")
        run_cli("review", "snapshot")
        code, out, _ = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertIn("No reviewers configured", out)

    def test_empty_snapshot_is_reported(self):
        self.commit_all("commit the change")
        code, out, _ = run_cli("review", "snapshot")
        self.assertEqual(code, 1)
        self.assertIn("empty", out)

    def test_only_filter_runs_a_subset(self):
        run_cli("review", "snapshot")
        _, out, _ = run_cli("review", "run", "--only", "m2")
        self.assertIn("1 successful", out)

    def test_summary_lists_stages_and_models(self):
        run_cli("review", "snapshot")
        run_cli("review", "run")
        _, out, _ = run_cli("summary")
        self.assertIn("Workflow:", out)
        self.assertIn("Models:", out)
        self.assertIn("Architect", out)

    def test_workspace_is_self_ignoring_by_default(self):
        run_cli("review", "snapshot")
        self.assertTrue(os.path.isfile(os.path.join(self.project, ".ai", ".gitignore")))
        tracked = self.git("status", "--porcelain").stdout
        self.assertNotIn(".ai/", tracked)


@unittest.skipUnless(has_git(), "git is required")
class TestTheScorecardThroughTheCli(IsolatedCase):
    """What each round found and what the owner kept of it, read back by
    `optimization report` from the archived report of every round."""

    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write("app.py", "def add(a, b):\n    return a - b\n")

        mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(mock_dir)
        with open(os.path.join(mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING)
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = mock_dir

        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m2", "--role", "security")
        # Both reviewers on every round: the panel reduction has its own tests.
        run_cli("config", "set", "optimization.level", "quality")
        self.workspace = self.cli_workspace()

    def scorecard(self, *before):
        return json.loads(run_cli(*before, "optimization", "report", "--json")[1])["scorecard"]

    def reviewed_and_accepted(self):
        run_cli("review", "snapshot")
        run_cli("review", "run")
        run_cli("review", "triage", "F1", "--status", "accepted")

    def archived(self):
        return ws.list_files(self.workspace.rounds_dir, ".json")

    def test_a_triaged_round_is_scored_from_its_archive(self):
        self.reviewed_and_accepted()
        card = self.scorecard()["code"]
        self.assertEqual(card["reviewers"]["m1"]["accepted"], 1)
        self.assertEqual(card["panel"]["accepted"], 1)
        self.assertEqual((card["rounds_read"], card["rounds_recorded"]), (1, 1))

        round_id = ws.read_json(self.workspace.snapshot_meta_path)["round_id"]
        self.assertTrue(round_id)
        live = ws.read_json(self.workspace.consolidated_json_path)
        self.assertEqual(live["snapshot"]["round_id"], round_id)
        events = [event for event in self.workspace.read_state()["events"] if event.get("stage") == "review"]
        self.assertEqual(events[-1]["round_id"], round_id)
        self.assertEqual(len(self.archived()), 1)
        self.assertTrue(ws.read_json(self.archived()[0])["findings"][0]["triage_set_at"])

    def test_putting_a_finding_back_withdraws_its_acceptance(self):
        self.reviewed_and_accepted()
        run_cli("review", "triage", "F1", "--status", "needs-triage")
        panel = self.scorecard()["code"]["panel"]
        self.assertEqual((panel["accepted"], panel["open"]), (0, 1))

    def test_the_same_tree_frozen_again_is_a_second_round_not_a_second_finding(self):
        self.reviewed_and_accepted()
        run_cli("review", "snapshot")
        run_cli("review", "run")
        self.assertEqual(len(self.archived()), 2)
        card = self.scorecard()["code"]
        self.assertEqual((card["rounds_recorded"], card["rounds_read"]), (2, 2))
        self.assertEqual(card["panel"]["accepted"], 1)

    def test_a_round_every_reviewer_failed_is_declared_unreviewed(self):
        """Its report is archived all the same, and holds the last round's findings."""
        self.reviewed_and_accepted()
        run_cli("review", "snapshot")
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        run_cli("review", "run")
        del os.environ["DEV_ORCHESTRA_MOCK_FAIL"]
        card = self.scorecard()["code"]
        self.assertEqual((card["rounds_recorded"], card["rounds_read"], card["rounds_unreviewed"]), (2, 1, 1))
        self.assertEqual(card["panel"]["accepted"], 1)
        self.assertEqual(card["reviewers"]["m1"]["failed_runs"], 1)
        _, out, _ = run_cli("optimization", "report")
        self.assertIn("1 of 2 recorded round(s) had a report to read; 1 round(s) no reviewer reviewed.", out)

    def test_a_rerun_with_only_stays_one_round(self):
        run_cli("review", "snapshot")
        run_cli("review", "run")
        run_cli("review", "run", "--only", "m2")
        self.assertEqual(len(self.archived()), 1)
        card = self.scorecard()["code"]
        self.assertEqual(card["rounds_recorded"], 1)
        self.assertEqual(card["panel"]["runs"], 3)
        self.assertEqual(card["reviewers"]["m2"]["runs"], 2)

    def test_every_workflow_is_read_and_workflow_narrows_it_to_one(self):
        self.reviewed_and_accepted()
        other = ws.Workspace(self.project, workflow="elsewhere").ensure()
        run = {"id": "x", "status": "ok", "snapshot": "b" * 12, "usage": {"billed_tokens": 10}}
        other.record_event("review", "ok", {"iteration": 1, "round_id": "r", "reviewers": [run]})
        finding = {"id": "F1", "key": "k", "reported_by": ["x"], "triage": "accepted"}
        report = {"iteration": 1, "snapshot": {"sha256": "b" * 64, "round_id": "r"}, "findings": [finding]}
        ws.write_json(other.consolidated_json_path, report)

        both = self.scorecard()["code"]
        self.assertEqual((both["rounds_read"], both["workflows_read"]), (2, 2))
        one = self.scorecard("--workflow", "elsewhere")["code"]
        self.assertEqual((one["rounds_read"], one["workflows_read"]), (1, 1))
        self.assertEqual(sorted(one["reviewers"]), ["x"])

    def test_a_workflow_with_no_report_prints_no_scorecard(self):
        """A table of zeroes would read as a panel that found nothing."""
        run = {"id": "m1", "status": "ok", "snapshot": "c" * 12, "usage": {"billed_tokens": 10}}
        self.workspace.record_event("review", "ok", {"iteration": 1, "reviewers": [run]})
        _, out, _ = run_cli("optimization", "report")
        self.assertNotIn("Reviewer scorecard", out)
        self.assertNotIn("Review effort", out)
        self.assertEqual(self.scorecard()["code"]["rounds_read"], 0)

    def test_the_text_says_what_the_figures_can_and_cannot_claim(self):
        self.reviewed_and_accepted()
        _, out, _ = run_cli("optimization", "report")
        self.assertIn("Reviewer scorecard, code review: 1 of 1 recorded round(s) had a report to read.", out)
        self.assertIn("Review effort, code and design together:", out)
        self.assertIn("rates withheld under 10 decided", out)
        self.assertIn("upper bound", out)
        self.assertNotIn("biased", out)
        self.assertNotIn("either way", out)
        self.assertNotIn("understates", out)
        for line in out.splitlines():
            if "floor" in line:
                self.assertIn("Cost totals are floors", line)

    def test_a_round_with_no_report_to_read_is_declared(self):
        self.reviewed_and_accepted()
        run = {"id": "m1", "status": "ok", "snapshot": "c" * 12, "usage": {"billed_tokens": 10}}
        self.workspace.record_event("review", "ok", {"iteration": 2, "round_id": "gone", "reviewers": [run]})
        _, out, _ = run_cli("optimization", "report")
        self.assertIn("1 of 2 recorded round(s) had a report to read.", out)
        self.assertIn("biased", out)
        self.assertIn("either way", out)
        self.assertNotIn("understates", out)


if __name__ == "__main__":
    unittest.main()
