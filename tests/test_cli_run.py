"""The ``run`` command, its budgets and guards, and ``status``, end to end."""

from __future__ import annotations

import io
import json
import os
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, Optional
from unittest import mock

from helpers import (
    CLAUDE_HELP_OLD,
    TEST_WORKFLOW,
    IsolatedCase,
    has_git,
    pretend_claude_is_installed,
    read_file,
    run_cli,
    spend_the_runtime_budget,
)

from orchestrator import cli, cli_workflow
from orchestrator import config as config_mod
from orchestrator import ledger as ledger_mod
from orchestrator import workspace as ws


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

    @unittest.skipUnless(has_git(), "git is required")
    def test_a_subdirectory_file_places_the_workspace_as_it_sets_the_budget(self):
        """`.ai/` used to follow the root's file while budgets followed this one (#282)."""
        self.init_git_repo()
        self.write(
            "pkg/.dev-orchestra.yaml",
            "version: 1\nworkspace:\n  dir: .agent-work\nbudgets:\n  implementer: 1\n",
        )
        os.chdir(os.path.join(self.project, "pkg"))
        code, _, err = run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isdir(os.path.join(self.project, ".agent-work")))
        self.assertFalse(os.path.exists(os.path.join(self.project, ".ai")))
        budget = json.loads(run_cli("budget", "show", "--json")[1])["budgets"]["implementer"]
        self.assertEqual((budget["used"], budget["limit"]), (1, 1))
        code, _, err = run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, ledger_mod.EXIT_BUDGET_EXHAUSTED, err)

    @unittest.skipUnless(has_git(), "git is required")
    def test_an_explicit_cwd_and_the_other_commands_follow_the_subdirectory_file(self):
        """`--cwd` from the root, and `design approve` from the subdirectory (#282)."""
        self.init_git_repo()
        pkg = os.path.join(self.project, "pkg")
        self.write("pkg/.dev-orchestra.yaml", "version: 1\nworkspace:\n  dir: .agent-work\n")
        self.write(".dev-orchestra.yaml", "version: 1\nworkspace:\n  dir: .root-work\n")
        code, _, err = run_cli("--cwd", pkg, "status")
        self.assertEqual(code, 0, err)
        container = os.path.join(self.project, ".agent-work")
        self.assertTrue(os.path.isdir(container))
        for other in (".ai", ".root-work", os.path.join("pkg", ".agent-work")):
            self.assertFalse(os.path.exists(os.path.join(self.project, other)), other)
        workspace = ws.Workspace(self.project, container, TEST_WORKFLOW).ensure()
        ws.write_text(workspace.plan_path, "# Plan\n\nDo it.\n")
        os.chdir(pkg)
        code, _, err = run_cli("design", "approve")
        self.assertEqual(code, 0, err)
        self.assertIn("design_approval", workspace.read_state())
        self.assertFalse(os.path.exists(os.path.join(self.project, ".root-work")))


SECRET_SETTINGS = '--settings={"apiKeyHelper":"sk-ant-abcdefghijklmnopqrs"}'


class TestReadOnlyRuns(IsolatedCase):
    """What `run` refuses for a read-only role, and that refusing costs nothing."""

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")

    def used(self, role="architect"):
        return json.loads(run_cli("budget", "show", "--json")[1])["budgets"][role]["used"]

    def write_project(self, text):
        self.write(".dev-orchestra.yaml", "version: 1\n" + text)

    def tree(self):
        walked = os.walk(self.project)
        return sorted(os.path.join(top, name) for top, dirs, files in walked for name in dirs + files)

    def test_implement_mode_is_refused_for_a_read_only_role(self):
        before = self.tree()
        code, out, err = run_cli("run", "architect", "--mode", "implement", "--prompt", "x")
        # Before `used()`, which opens the workspace itself.
        self.assertEqual(self.tree(), before)
        self.assertFalse(os.path.exists(ws.Workspace(self.project, workflow=TEST_WORKFLOW).state_path))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--mode implement is not accepted", err)
        self.assertEqual(self.used(), 0)

    def test_a_reviewer_is_refused_implement_mode_too(self):
        run_cli("reviewer", "add", "--provider", "mock", "--id", "solo", "--role", "security")
        code, _, err = run_cli("run", "solo", "--mode", "implement", "--prompt", "x")
        self.assertEqual(code, 2)
        self.assertIn("solo: refused", err)

    def test_tightening_an_implement_role_is_still_allowed(self):
        pretend_claude_is_installed(self)
        code, out, _ = run_cli("run", "implementer", "--mode", "review", "--print-command")
        self.assertEqual(code, 0)
        self.assertIn("--restricted", out)

    def test_a_loosening_extra_is_refused_before_anything_is_spent(self):
        code, _, err = run_cli("run", "architect", "--prompt", "x", "--extra", "--tools", "default")
        self.assertEqual(code, 2)
        self.assertIn("'--tools'", err)
        self.assertIn("in --extra", err)
        self.assertEqual(self.used(), 0)

    def test_global_args_and_extra_are_each_refused_on_their_own_line(self):
        run_cli("config", "set", "architect.options.args", '["--restricted"]')
        extra = ["--extra", "--dangerously-skip-permissions"]
        code, _, err = run_cli("run", "architect", "--prompt", "x", *extra)
        self.assertEqual(code, 2)
        lines = [line for line in err.splitlines() if line.startswith("architect: refused -- ")]
        self.assertEqual(len(lines), 2, err)
        self.assertIn("'--restricted'", lines[0])
        self.assertIn("in options.args", lines[0])
        self.assertIn("'--dangerously-skip-permissions'", lines[1])
        self.assertIn("in --extra", lines[1])
        self.assertEqual(self.used(), 0)

    def test_a_refused_value_is_never_printed(self):
        code, _, err = run_cli("run", "architect", "--prompt", "x", "--extra", SECRET_SETTINGS)
        self.assertEqual(code, 2)
        self.assertIn("'--settings'", err)
        self.assertNotIn("sk-ant", err)

    def test_the_printed_command_carries_the_allowlist(self):
        pretend_claude_is_installed(self)
        code, out, _ = run_cli("run", "architect", "--print-command", "--extra", "--add-dir", "../x")
        self.assertEqual(code, 0)
        self.assertIn("--tools Read,Grep,Glob --strict-mcp-config --restricted", out)
        self.assertTrue(out.strip().endswith("--add-dir ../x"))

    def test_an_implement_role_keeps_its_raw_arguments(self):
        pretend_claude_is_installed(self)
        code, out, _ = run_cli("run", "implementer", "--print-command", "--extra", "--tools", "default")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith("--tools default"))

    def test_project_args_are_refused_even_when_they_are_add_dir(self):
        self.write_project('architect:\n  options:\n    args: ["--add-dir", "../x"]\n')
        code, _, err = run_cli("run", "architect", "--prompt", "x", "--extra", "--tools", "default")
        self.assertEqual(code, 2)
        self.assertIn("set in the project config (.dev-orchestra.yaml)", err)
        self.assertNotIn("../x", err)
        # The first check that refuses is the only one said.
        self.assertEqual(err.count("refused --"), 1, err)
        self.assertNotIn("in --extra", err)
        self.assertEqual(self.used(), 0)
        self.assertEqual(run_cli("run", "architect", "--print-command")[0], 2)

    def test_the_same_args_in_the_global_file_are_accepted(self):
        pretend_claude_is_installed(self)
        run_cli("config", "set", "architect.options.args", '["--add-dir", "../x"]')
        code, out, _ = run_cli("run", "architect", "--print-command")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith("--add-dir ../x"))

    def test_a_project_reviewer_with_args_is_refused(self):
        self.write_project(
            "reviewers:\n  - id: mine\n    provider: mock\n    role: general\n"
            '    options:\n      args: ["--add-dir", "../x"]\n'
        )
        code, _, err = run_cli("run", "mine", "--mode", "review", "--prompt", "x")
        self.assertEqual(code, 2)
        self.assertIn("reviewer mine: options.args is set in the project config", err)

    def test_a_project_tier_is_refused_and_the_global_base_is_not(self):
        pretend_claude_is_installed(self)
        run_cli("config", "set", "architect.options.args", '["--add-dir", "../g"]')
        self.write_project(
            'architect:\n  model_tiers:\n    light:\n      options:\n        args: ["--add-dir", "../x"]\n'
        )
        self.assertEqual(run_cli("run", "architect", "--tier", "light", "--print-command")[0], 2)
        code, out, _ = run_cli("run", "architect", "--print-command")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith("--add-dir ../g"))

    def test_an_implement_role_may_take_args_from_the_project(self):
        self.write_project('implementer:\n  provider: mock\n  options:\n    args: ["--anything"]\n')
        code, out, _ = run_cli("run", "implementer", "--print-command")
        self.assertEqual(code, 0)
        self.assertIn("--anything", out)

    def test_a_cli_without_the_flags_is_refused(self):
        pretend_claude_is_installed(self, CLAUDE_HELP_OLD)
        code, _, err = run_cli("run", "architect", "--prompt", "x")
        self.assertEqual(code, 2)
        self.assertIn("does not advertise", err)
        self.assertIn("missing: --tools", err)
        self.assertEqual(self.used(), 0)

    def test_a_missing_cli_is_still_reported_as_missing(self):
        from orchestrator.providers.claude import ClaudeProvider

        self.addCleanup(setattr, ClaudeProvider, "which", ClaudeProvider.which)
        ClaudeProvider.which = lambda self: None
        _, _, err = run_cli("run", "architect", "--prompt", "x")
        self.assertIn("not found on PATH", err)
        self.assertNotIn("unverified", err)

    def test_config_warnings_do_not_stop_other_commands(self):
        status_before = run_cli("status")[0]
        loosening = '["--permission-mode","acceptEdits"]'
        code, _, err = run_cli("config", "set", "architect.options.args", loosening)
        self.assertEqual(code, 0)
        self.assertIn("warning: architect: read-only run: '--permission-mode'", err)
        code, out, _ = run_cli("config", "validate")
        self.assertEqual(code, 0)
        self.assertIn("Warnings:", out)
        payload = json.loads(run_cli("config", "validate", "--json")[1])
        self.assertTrue(payload["valid"])
        self.assertTrue(any("'--permission-mode'" in w for w in payload["warnings"]))
        self.assertEqual(run_cli("status")[0], status_before)
        self.assertEqual(run_cli("budget", "show")[0], 0)
        self.assertEqual(run_cli("run", "architect", "--prompt", "x")[0], 2)

    def test_project_args_are_warned_about_before_a_run(self):
        self.write_project('architect:\n  options:\n    args: ["--add-dir", "x"]\n')
        payload = json.loads(run_cli("config", "validate", "--json")[1])
        self.assertTrue(any("set in the project config" in w for w in payload["warnings"]))
        report = json.loads(run_cli("doctor", "--fast", "--json")[1])
        self.assertTrue(any("set in the project config" in p for p in report["problems"]))
        self.assertEqual(report["config"]["warnings"], payload["warnings"])
        self.assertEqual(run_cli("doctor", "--fast", "--strict")[0], 1)


class TestReadOnlyRefusalsReachTheJob(IsolatedCase):
    """A worker's stderr goes nowhere, so each refusal is in the job record."""

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        self.workspace = self.cli_workspace()
        self.prompt = self.write("brief.md", "plan it\n")

    def refused_job(self, *extra):
        from orchestrator import jobs as jobs_mod

        jobs_mod.write_job(self.workspace, {"id": "p-1", "stage": "architect", "status": "running"})
        argv = ["run", "architect", "--prompt-file", self.prompt]
        argv += ["--job-file", jobs_mod.job_path(self.workspace, "p-1"), *extra]
        code, _, err = run_cli(*argv)
        self.assertEqual(code, 2)
        job = jobs_mod.read_job(self.workspace, "p-1")
        assert job is not None
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["error"])
        self.assertIn(job["error"].splitlines()[0], err)
        self.assertNotIn("sk-ant", job["error"])
        return job

    def test_a_refused_global_arg(self):
        run_cli("config", "set", "architect.options.args", '["--settings=sk-ant-abcdefghijklmnopqrs"]')
        self.assertIn("'--settings'", self.refused_job()["error"])

    def test_a_refused_project_arg(self):
        project = 'version: 1\narchitect:\n  options:\n    args: ["--add-dir", "x"]\n'
        self.write(".dev-orchestra.yaml", project)
        self.assertIn("set in the project config", self.refused_job()["error"])

    def test_a_refused_mode(self):
        self.assertIn("--mode implement", self.refused_job("--mode", "implement")["error"])

    def test_a_cli_that_cannot_enforce(self):
        pretend_claude_is_installed(self, CLAUDE_HELP_OLD)
        self.assertIn("does not advertise", self.refused_job()["error"])


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

        setattr(mock_mod.MockProvider, "run", record)
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
        self.assertIn("Report what is unresolved instead of retrying, or pass --force to override.", err)

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
        self.assertEqual(payload["runtime"], {"used": 0, "limit": 14400, "remaining": 14400, "suspended": 0})
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

    def test_a_run_that_slept_is_charged_only_its_awake_time(self):
        os.environ["DEV_ORCHESTRA_MOCK_SUSPENDED"] = "90"
        self.assertEqual(run_cli("run", "implementer", "--prompt", "go")[0], 0)
        event = self.cli_workspace().read_state()["events"][-1]
        self.assertEqual(event["suspended_seconds"], 90)
        self.assertGreater(event["duration_seconds"], 90)
        self.assertAlmostEqual(event["charged_seconds"], event["duration_seconds"] - 90, delta=0.02)
        payload = json.loads(run_cli("budget", "show", "--json")[1])
        self.assertAlmostEqual(payload["runtime"]["used"], event["charged_seconds"], delta=0.01)
        self.assertEqual(payload["runtime"]["suspended"], 90)
        _, out, _ = run_cli("budget", "show")
        self.assertIn("90s of delegated run time spent asleep was not charged", out)
        _, out, _ = run_cli("status")
        self.assertIn(
            "Runtime: 90s of delegated run time spent asleep was not charged (dev-orchestra budget show)", out
        )
        self.assertEqual(json.loads(run_cli("status", "--json")[1])["runtime"]["suspended"], 90)

    def test_the_sleep_line_shows_with_the_runtime_cap_off(self):
        run_cli("config", "set", "budgets.max_runtime_seconds", "0")
        os.environ["DEV_ORCHESTRA_MOCK_SUSPENDED"] = "90"
        self.assertEqual(run_cli("run", "implementer", "--prompt", "go")[0], 0)
        _, out, _ = run_cli("budget", "show")
        self.assertNotIn("(delegated execution)", out)
        self.assertIn("90s of delegated run time spent asleep was not charged", out)

    def test_a_run_that_did_not_sleep_says_nothing_about_sleep(self):
        self.assertEqual(run_cli("run", "implementer", "--prompt", "go")[0], 0)
        self.assertNotIn("suspended_seconds", self.cli_workspace().read_state()["events"][-1])
        self.assertNotIn("asleep", run_cli("budget", "show")[1])
        self.assertNotIn("asleep", run_cli("status")[1])


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

        book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))
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

    def status_payload(self):
        """The verdict as `_status_payload` builds it, without printing anything."""
        loaded = config_mod.load(self.project, validate_result=False)
        workspace = self.cli_workspace()
        book = ledger_mod.Ledger(workspace, ledger_mod.budget_settings(loaded.data))
        return cli_workflow._status_payload(loaded, workspace, book).payload

    def test_the_payload_of_a_fresh_workflow_says_continue(self):
        payload = self.status_payload()
        self.assertEqual(payload["verdict"], "continue")
        self.assertEqual(payload["reasons"], [])

    def test_the_payload_names_a_spent_budget_as_its_reason(self):
        run_cli("config", "set", "budgets.test", "1")
        run_cli("budget", "consume", "test")
        payload = self.status_payload()
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertEqual(payload["reasons"], ["test has no attempts left"])

    def test_the_text_and_the_json_agree_on_the_verdict_and_the_reasons(self):
        run_cli("config", "set", "budgets.test", "1")
        run_cli("budget", "consume", "test")
        payload = json.loads(run_cli("status", "--json")[1])
        lines = run_cli("status")[1].splitlines()
        self.assertEqual(lines[0], "Verdict: %s" % payload["verdict"].upper())
        reasons = []
        for line in lines[1:]:
            if not line.startswith("  - "):
                break
            reasons.append(line[len("  - ") :])
        self.assertEqual(reasons, payload["reasons"])
        self.assertTrue(reasons)


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


class TestRunIdleDeadline(IsolatedCase):
    """A run that may change files gets a longer idle deadline than a reviewer (#273)."""

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        for role in ("architect", "implementer", "review_fixer"):
            run_cli("config", "set", "%s.provider" % role, "mock")

    def idle_deadline_of(self, *argv: str) -> Optional[float]:
        from orchestrator.providers import mock as mock_mod

        seen: Dict[str, Any] = {}
        original = mock_mod.MockProvider.run

        def recording_run(provider, *args, **kwargs):
            seen["idle_timeout"] = kwargs.get("idle_timeout")
            return original(provider, *args, **kwargs)

        with mock.patch.object(mock_mod.MockProvider, "run", recording_run):
            code, _, err = run_cli("run", *argv, "--prompt", "go")
        self.assertEqual(code, 0, err)
        return seen["idle_timeout"]

    def test_a_run_that_may_change_files_gets_the_longer_idle_deadline(self):
        self.assertEqual(self.idle_deadline_of("implementer"), 1200)
        self.assertEqual(self.idle_deadline_of("review_fixer"), 1200)
        # A larger shared value is kept.
        run_cli("config", "set", "review.idle_timeout_seconds", "1800", "--scope", "global")
        self.assertEqual(self.idle_deadline_of("implementer"), 1800)

    def test_a_read_only_run_keeps_the_shared_idle_deadline(self):
        self.assertEqual(self.idle_deadline_of("architect"), 300)
        # By what the run may do, not by its role.
        self.assertEqual(self.idle_deadline_of("implementer", "--mode", "plan"), 300)
        run_cli("config", "set", "review.idle_timeout_seconds", "120", "--scope", "global")
        self.assertEqual(self.idle_deadline_of("architect"), 120)

    def test_the_flag_still_sets_one_for_any_run(self):
        self.assertEqual(self.idle_deadline_of("implementer", "--idle-timeout", "60"), 60)

    def test_a_null_shared_idle_deadline_reads_as_the_default(self):
        """A null in a file keeps the default, so a write run still gets the minimum, never none."""
        self.write(".dev-orchestra.yaml", "version: 1\nreview:\n  idle_timeout_seconds: null\n")
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.review_settings()["idle_timeout_seconds"], 300)
        self.assertEqual(config_mod.idle_timeout(loaded, "implement"), (1200, config_mod.WRITE_RUN_MINIMUM))
        self.assertEqual(self.idle_deadline_of("implementer"), 1200)
        self.assertEqual(self.idle_deadline_of("architect"), 300)

    def test_a_read_only_role_cannot_ask_for_the_write_run_deadline(self):
        from orchestrator.providers import mock as mock_mod

        with mock.patch.object(mock_mod.MockProvider, "run") as spy:
            code, _, err = run_cli("run", "architect", "--mode", "implement", "--prompt", "go")
        self.assertNotEqual(code, 0)
        self.assertIn("--mode implement is not accepted", err)
        spy.assert_not_called()

    def test_a_roles_own_idle_timeout_comes_before_the_write_run_minimum(self):
        """What the provider actually waits, not just what it was handed."""
        from orchestrator.providers import mock as mock_mod

        seen: Dict[str, Any] = {}
        original = mock_mod.MockProvider.run

        def recording_run(provider, *args, **kwargs):
            seen["passed"] = kwargs.get("idle_timeout")
            seen["effective"] = provider.idle_timeout(kwargs.get("options"), kwargs.get("idle_timeout"))
            return original(provider, *args, **kwargs)

        with (
            mock.patch.object(mock_mod.MockProvider, "streams_progress", True),
            mock.patch.object(mock_mod.MockProvider, "option_keys", ("args", "idle_timeout")),
            mock.patch.object(mock_mod.MockProvider, "run", recording_run),
        ):
            run_cli("config", "set", "implementer.options.idle_timeout", "90")
            code, _, err = run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, {"passed": 1200, "effective": 90.0})

    def test_config_resolves_the_idle_deadline_with_its_source(self):
        loaded = config_mod.load(self.project)
        self.assertEqual(config_mod.idle_timeout(loaded, "review"), (300, "default"))
        self.assertEqual(config_mod.idle_timeout(loaded, "plan"), (300, "default"))
        self.assertEqual(config_mod.idle_timeout(loaded, "implement"), (1200, config_mod.WRITE_RUN_MINIMUM))
        run_cli("config", "set", "review.idle_timeout_seconds", "1800", "--scope", "project")
        loaded = config_mod.load(self.project)
        self.assertEqual(config_mod.idle_timeout(loaded, "implement"), (1800, "project"))

    def stall_origin(self, role: str, *argv: str, option_keys=("args",), streams=True) -> str:
        """What a stalled run of ``role`` says its idle deadline came from."""
        from orchestrator.providers import mock as mock_mod
        from orchestrator.providers.base import RunResult

        def stalled_run(provider, *args, **kwargs):
            resolved = provider.resolve_model({"family": "small"})
            return RunResult(False, 125, "", "", ["mock"], 1.0, resolved, stalled=True, idle_for=1200.0)

        with (
            mock.patch.object(mock_mod.MockProvider, "run", stalled_run),
            mock.patch.object(mock_mod.MockProvider, "streams_progress", streams),
            mock.patch.object(mock_mod.MockProvider, "option_keys", option_keys),
        ):
            _, _, err = run_cli("run", role, *argv, "--prompt", "go")
        self.assertIn(
            "%s produced no output for 1200s and was treated as stalled (not merely slow)" % role, err
        )
        match = re.search(r"its idle deadline is from (.*)\.\n", err)
        self.assertIsNotNone(match, err)
        return match.group(1) if match else ""

    def test_a_stall_names_where_its_idle_deadline_came_from(self):
        self.assertEqual(self.stall_origin("implementer"), "the minimum for a run that may change files")
        self.assertEqual(self.stall_origin("architect"), "review.idle_timeout_seconds, default")
        self.assertEqual(self.stall_origin("implementer", "--idle-timeout", "60"), "--idle-timeout")
        run_cli("config", "set", "review.idle_timeout_seconds", "1800", "--scope", "project")
        self.assertEqual(self.stall_origin("implementer"), "review.idle_timeout_seconds, project")

    def test_a_stall_names_the_roles_option_only_when_the_provider_waits_it(self):
        from orchestrator.providers import mock as mock_mod

        with mock.patch.object(mock_mod.MockProvider, "option_keys", ("args", "idle_timeout")):
            run_cli("config", "set", "implementer.options.idle_timeout", "90")
        self.assertEqual(
            self.stall_origin("implementer", option_keys=("args", "idle_timeout")),
            "implementer options.idle_timeout",
        )
        # Even over the flag, which the provider's own option outranks.
        self.assertEqual(
            self.stall_origin("implementer", "--idle-timeout", "60", option_keys=("args", "idle_timeout")),
            "implementer options.idle_timeout",
        )
        # A provider that takes the option but does not wait it (no progress
        # to time, as Claude without stream-json) is not said to have.
        self.assertEqual(
            self.stall_origin("implementer", option_keys=("args", "idle_timeout"), streams=False),
            "the minimum for a run that may change files",
        )


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
                orphans_possible=fields.get("orphans_possible", False),
            )

        original = mock_mod.MockProvider.run
        setattr(mock_mod.MockProvider, "run", fake_run)
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

    def test_timeout_message_names_the_key(self):
        """A run killed sooner than it used to be says which key raises it (#258)."""
        run_cli("config", "set", "run.timeout_seconds.implementer", "1200")
        _, _, err = self.run_returning(ok=False, exit_code=124, timed_out=True)
        self.assertIn(
            "implementer hit its 1200s deadline (run.timeout_seconds.implementer, global) and was killed; "
            "--timeout raises it for one run.",
            err,
        )
        _, _, err = run_cli("run", "implementer", "--prompt", "go", "--timeout", "5")
        self.assertIn("implementer hit its 5s deadline (--timeout) and was killed.", err)

    def test_a_reviewer_timeout_message_names_the_review_key(self):
        """A reviewer run is bounded by ``review.timeout_seconds``, and says so with its layer."""
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        run_cli("config", "set", "--scope", "project", "review.timeout_seconds", "900")
        self.patch_run(ok=False, exit_code=124, timed_out=True)
        _, _, err = run_cli("run", "m1", "--prompt", "go")
        self.assertIn(
            "m1 hit its 900s deadline (review.timeout_seconds, project) and was killed; "
            "--timeout raises it for one run.",
            err,
        )

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

    def test_a_refused_write_in_a_worker_is_reported_in_order(self):
        """The outcome first, then the refusal, then the second job update."""
        from orchestrator import jobs as jobs_mod

        os.makedirs(self.rejected)
        self.patch_run(ok=False, exit_code=1, stdout="half a plan\n", stderr="boom", orphans_possible=True)
        workspace = self.cli_workspace()
        jobs_mod.write_job(workspace, {"id": "w-1", "stage": "implementer", "status": "running"})
        job_file = jobs_mod.job_path(workspace, "w-1")
        out, err = io.StringIO(), io.StringIO()
        finished = []
        finish = jobs_mod.finish

        def recording(*args, **kwargs):
            finished.append((args, kwargs, len(err.getvalue())))
            finish(*args, **kwargs)

        argv = ["run", "implementer", "--prompt", "go", "--output", self.target, "--job-file", job_file]
        with mock.patch.object(jobs_mod, "finish", recording), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        self.assertEqual(code, 1)
        self.assertEqual(len(finished), 2, finished)
        (first_args, first, _), (second_args, second, printed) = finished
        self.assertEqual(first_args[1], "failed")
        self.assertEqual(first["output"], "half a plan\n")
        self.assertEqual(first["error"], "boom")
        self.assertEqual(second_args[1], "failed")
        detail = {"output_written": False, "output_target": self.target, "rejected_file": None}
        self.assertEqual(second, {"detail": detail})
        text = err.getvalue()
        failed = text.index("implementer failed (exit 1): boom")
        refused = text.index("produced nothing usable")
        unsaved = text.index("half a plan")
        orphans = text.index("may have left orphans")
        self.assertLess(failed, refused)
        self.assertLess(refused, unsaved)
        self.assertLess(unsaved, orphans)
        self.assertLess(unsaved, printed)
        self.assertLessEqual(printed, orphans)

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
        setattr(mock_mod.MockProvider, "run", fake_run)
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


class TestRunJsonInTheForeground(IsolatedCase):
    """``run --json`` without ``--detach`` printed the model's raw text (#286).

    Every role is on the mock adapter, so no real CLI is started.
    """

    def setUp(self):
        super().setUp()
        role = {"provider": "mock", "model": {"family": "small", "version": "latest"}}
        data = config_mod.default_config()
        data.update(orchestrator=role, architect=role, implementer=role, review_fixer=role)
        data["reviewers"] = [config_mod.make_reviewer("mock-general", "mock", "small")]
        config_mod.write_config_file(config_mod.global_config_path(), data)
        self.set_env("DEV_ORCHESTRA_MOCK_RESPONSE", "the answer")

    def test_stdout_is_one_object_with_the_output_inline(self):
        code, out, err = run_cli("run", "orchestrator", "--prompt", "hi", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["stage"], "orchestrator")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["output"].strip(), "the answer")
        self.assertTrue(payload["answered"])
        for key in ("model", "duration_seconds", "session_id", "stalled", "timed_out"):
            self.assertIn(key, payload)
        self.assertNotIn("output_written", payload)

    def test_with_output_the_file_is_named_not_printed(self):
        code, out, err = run_cli("run", "architect", "--prompt", "hi", "--output", ".ai/plan.md", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertNotIn("output", payload)
        # As in a job record: these keys appear only when the write was refused.
        for key in ("output_written", "output_target", "rejected_file"):
            self.assertNotIn(key, payload)
        plan = os.path.join(self.project, ".ai", "workflows", TEST_WORKFLOW, "plan.md")
        with open(plan, encoding="utf-8") as handle:
            self.assertIn("the answer", handle.read())

    def test_a_refused_write_is_named_as_a_job_names_it(self):
        self.set_env("DEV_ORCHESTRA_MOCK_FAIL", "1")
        code, out, _ = run_cli("run", "architect", "--prompt", "hi", "--output", ".ai/plan.md", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertIs(payload["output_written"], False)
        self.assertTrue(payload["output_target"].endswith(os.path.join(TEST_WORKFLOW, "plan.md")))
        # Nothing was printed, so there is nothing kept beside the target.
        self.assertIsNone(payload["rejected_file"])
        self.assertNotIn("output", payload)

    def return_result(self, **fields):
        """Make the mock return a RunResult the test dictates."""
        from orchestrator.providers import mock as mock_mod
        from orchestrator.providers.base import RunResult

        def fake_run(provider, *args, **kwargs):
            resolved = provider.resolve_model(kwargs.get("model_spec") or {"family": "small"})
            extra = dict(fields)
            ok = extra.pop("ok", True)
            return RunResult(
                ok, 0 if ok else 1, extra.pop("stdout", ""), "", ["mock"], 1.0, resolved, **extra
            )

        self.enterContext(mock.patch.object(mock_mod.MockProvider, "run", fake_run))

    def test_a_refused_write_names_the_file_that_kept_the_output(self):
        self.return_result(ok=False, stdout="half a plan\n")
        code, out, _ = run_cli("run", "architect", "--prompt", "hi", "--output", ".ai/plan.md", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["rejected_file"], payload["output_target"] + ".rejected")
        with open(payload["rejected_file"], encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "half a plan\n")

    def test_an_ok_run_that_printed_nothing_has_not_answered(self):
        self.set_env("DEV_ORCHESTRA_MOCK_RESPONSE", " \n")
        code, out, err = run_cli("run", "orchestrator", "--prompt", "hi", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "succeeded")
        self.assertIs(payload["answered"], False)
        self.assertIn("produced no output", err)

    def test_warnings_and_suspended_seconds_only_when_there_are_some(self):
        code, out, _ = run_cli("run", "orchestrator", "--prompt", "hi", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        for key in ("warnings", "suspended_seconds", "resume"):
            self.assertNotIn(key, payload)

        self.return_result(stdout="the answer\n", warnings=["a flag was dropped"], suspended=30.0)
        code, out, _ = run_cli("run", "orchestrator", "--prompt", "hi", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["warnings"], ["a flag was dropped"])
        self.assertEqual(payload["suspended_seconds"], 30.0)

    def test_resume_is_reported_when_it_was_asked_for(self):
        self.write("resume.md", "revise\n")
        argv = ["run", "architect", "--resume", "--resume-prompt-file", "resume.md", "--prompt", "hi"]
        code, out, err = run_cli(*argv, "--output", ".ai/plan.md", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertIn("reason", payload["resume"])
        self.assertIn("outcome", payload["resume"])

    def test_detach_prints_the_job_not_a_run(self):
        from orchestrator import jobs as jobs_mod

        job = {"id": "orchestrator-1", "stage": "orchestrator", "status": "running"}
        self.enterContext(mock.patch.object(jobs_mod, "start", return_value=job))
        code, out, _ = run_cli("run", "orchestrator", "--prompt", "hi", "--detach", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), job)

    def test_a_worker_prints_nothing_its_job_holds_the_outcome(self):
        """The worker a detached run starts gets ``--json`` too; its stdout
        is not the caller's, and a second object there would be noise."""
        from orchestrator import execution
        from orchestrator import jobs as jobs_mod

        self.enterContext(mock.patch.object(execution, "end_children_on_sigterm"))
        workspace = self.cli_workspace()
        jobs_mod.write_job(workspace, {"id": "o-1", "stage": "orchestrator", "status": "running"})
        job_file = jobs_mod.job_path(workspace, "o-1")
        code, out, err = run_cli("run", "orchestrator", "--prompt", "hi", "--job-file", job_file, "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        job = jobs_mod.read_job(workspace, "o-1")
        assert job is not None
        self.assertEqual(job["status"], "succeeded")

    def test_a_run_refused_before_it_starts_prints_nothing_on_stdout(self):
        code, out, err = run_cli("run", "architect", "--mode", "implement", "--prompt", "hi", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())

    def test_print_command_stays_text(self):
        code, out, _ = run_cli("run", "orchestrator", "--print-command", "--json")
        self.assertEqual(code, 0)
        with self.assertRaises(ValueError):
            json.loads(out)

    def test_a_failed_run_is_still_one_object(self):
        self.set_env("DEV_ORCHESTRA_MOCK_FAIL", "1")
        code, out, _ = run_cli("run", "orchestrator", "--prompt", "hi", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("error", payload)
        self.assertFalse(payload["answered"])

    def test_without_json_the_output_is_printed_as_before(self):
        code, out, _ = run_cli("run", "orchestrator", "--prompt", "hi")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "the answer")


if __name__ == "__main__":
    unittest.main()
