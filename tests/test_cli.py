"""End-to-end behaviour of the ``dev-orchestra`` command."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git

from orchestrator import cli
from orchestrator import config as config_mod

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


@unittest.skipUnless(has_git(), "git is required")
class TestReviewPipeline(IsolatedCase):
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
        # These are about the fan-out: two reviewers, one of them failing, the
        # report surviving it. Below `quality` a change this small is reduced
        # to a single reviewer, which is the saving that level is for and
        # leaves nothing to fan out. The panel logic has its own tests.
        run_cli("config", "set", "optimization.level", "quality")

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


if __name__ == "__main__":
    unittest.main()
