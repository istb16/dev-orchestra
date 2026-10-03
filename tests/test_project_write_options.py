"""A write role's permission options come only from the global config (#195).

The project file can come with the branch under review. On agy that already
kept ``options.skip_permissions`` and raw arguments out of the project file
(#182); Claude's ``permission_mode`` and Codex's ``sandbox`` / ``approve`` can
loosen a write role just as far, so they are taken the same way.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase

from orchestrator import cli, doctor, execution
from orchestrator import config as config_mod
from orchestrator import config_policy as policy_mod
from orchestrator.providers.claude import ClaudeProvider

CLAUDE_ROLE = "  provider: claude\n  model:\n    family: opus\n"
CODEX_ROLE = "  provider: codex\n  model:\n    family: recommended-coding\n"

REFUSED = "permission bypass and raw arguments"


def run_cli(*argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class _Case(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.fake_clis(claude=True, codex=True)
        self.addCleanup(setattr, execution, "execute", execution.execute)
        self.started = []

        # Every provider spawns through ``execution.execute``; the runs below
        # that are not refused show that this stand-in is the one reached.
        def execute(command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
            self.started.append(list(command))
            return execution.ExecOutcome(0, "", "", 0.1)

        execution.execute = execute

    def write_global(self, text):
        path = config_mod.global_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("version: 1\n" + text)

    def write_project(self, text):
        self.write(".dev-orchestra.yaml", "version: 1\n" + text)

    def refusals(self):
        return policy_mod.project_write_refusals(config_mod.load(self.project))

    def assert_run_refused(self, *argv):
        code, _, err = run_cli("run", *argv)
        self.assertEqual(code, 2, err)
        self.assertIn(REFUSED, err)
        self.assertEqual(self.started, [])

    def assert_run_started(self, *argv):
        _, out, err = run_cli("run", *argv)
        self.assertNotIn(REFUSED, out + err)
        self.assertEqual(len(self.started), 1, out + err)


class TestClaude(_Case):
    def test_a_project_permission_mode_is_refused_whatever_its_value(self):
        for mode in ("bypassPermissions", "acceptEdits"):
            with self.subTest(mode=mode):
                self.write_project(
                    "implementer:\n" + CLAUDE_ROLE + "  options:\n    permission_mode: %s\n" % mode
                )
                refusals = self.refusals()
                self.assertEqual(list(refusals), ["implementer"])
                self.assertIn("options.permission_mode", refusals["implementer"])
                self.assertIn("taken only from the global config or from --extra", refusals["implementer"])

    def test_project_raw_arguments_on_a_claude_write_role_are_refused(self):
        for role in ("implementer", "review_fixer"):
            with self.subTest(role=role):
                self.write_project(
                    role + ":\n" + CLAUDE_ROLE + '  options:\n    args: ["--permission-mode", "x"]\n'
                )
                self.assertEqual(list(self.refusals()), [role])

    def test_the_global_permission_mode_is_allowed(self):
        self.write_global("implementer:\n  options:\n    permission_mode: bypassPermissions\n")
        self.write_project("implementer:\n" + CLAUDE_ROLE)
        self.assertEqual(self.refusals(), {})

    def test_a_project_permission_mode_over_a_global_one_is_refused(self):
        self.write_global("implementer:\n" + CLAUDE_ROLE + "  options:\n    permission_mode: acceptEdits\n")
        self.write_project("implementer:\n  options:\n    permission_mode: bypassPermissions\n")
        self.assertEqual(list(self.refusals()), ["implementer"])

    def test_a_project_tier_option_is_refused_too(self):
        self.write_project(
            "implementer:\n"
            + CLAUDE_ROLE
            + "  model_tiers:\n    light:\n      model:\n        family: sonnet\n"
            "      options:\n        permission_mode: bypassPermissions\n"
        )
        self.assertEqual(list(self.refusals()), ["implementer.model_tiers.light"])

    def test_run_refuses_a_tier_with_a_project_permission_mode(self):
        self.write_project(
            "implementer:\n"
            + CLAUDE_ROLE
            + "  model_tiers:\n    light:\n      model:\n        family: sonnet\n"
            "      options:\n        permission_mode: bypassPermissions\n"
        )
        self.assert_run_refused("implementer", "--tier", "light", "--prompt", "x")

    def test_the_write_walk_in_order(self):
        # ``away`` switches to agy without options of its own: were it read
        # through the role's options, the project skip_permissions would
        # refuse it.
        self.write_project(
            "implementer:\n"
            + CLAUDE_ROLE
            + "  options:\n    permission_mode: acceptEdits\n    skip_permissions: true\n"
            "  model_tiers:\n"
            "    own:\n      model:\n        family: sonnet\n"
            "      options:\n        permission_mode: plan\n"
            "    away:\n      provider: agy\n      model:\n        family: default\n"
            "    0:\n      model:\n        family: sonnet\n"
            "    a.b:\n      provider: codex\n      model:\n        family: recommended-coding\n"
            "      options:\n        sandbox: read-only\n"
            "    broken: 3\n"
        )
        refusals = policy_mod.project_write_refusals(config_mod.load(self.project, validate_result=False))
        self.assertEqual(
            list(refusals),
            [
                "implementer",
                "implementer.model_tiers.own",
                "implementer.model_tiers.0",
                "implementer.model_tiers.a.b",
            ],
        )
        claude = (
            "%s: options.permission_mode / options.args is set in the project config "
            "(.dev-orchestra.yaml); on claude the permission bypass and raw arguments are taken only "
            "from the global config or from --extra"
        )
        self.assertEqual(refusals["implementer.model_tiers.own"], claude % "implementer (tier own)")
        self.assertEqual(refusals["implementer.model_tiers.0"], claude % "implementer (tier 0)")
        self.assertEqual(
            refusals["implementer.model_tiers.a.b"],
            "implementer (tier a.b): options.sandbox / options.approve / options.args is set in the "
            "project config (.dev-orchestra.yaml); on codex the permission bypass and raw arguments are "
            "taken only from the global config or from --extra",
        )

    def test_a_tier_inherits_the_project_permission_mode_of_its_role(self):
        self.write_project(
            "implementer:\n" + CLAUDE_ROLE + "  options:\n    permission_mode: bypassPermissions\n"
            "  model_tiers:\n    light:\n      model:\n        family: sonnet\n"
        )
        self.assertEqual(sorted(self.refusals()), ["implementer", "implementer.model_tiers.light"])

    def test_a_tier_that_switches_to_codex_without_options_takes_none_of_the_role(self):
        self.write_project(
            "implementer:\n" + CLAUDE_ROLE + "  options:\n    permission_mode: bypassPermissions\n"
            "  model_tiers:\n    light:\n      provider: codex\n      model:\n"
            "        family: recommended-coding\n"
        )
        self.assertEqual(list(self.refusals()), ["implementer"])

    def test_a_project_tier_option_on_a_global_role_is_refused(self):
        self.write_global("implementer:\n" + CLAUDE_ROLE)
        self.write_project(
            "implementer:\n  model_tiers:\n    light:\n      model:\n        family: sonnet\n"
            "      options:\n        permission_mode: bypassPermissions\n"
        )
        self.assertEqual(list(self.refusals()), ["implementer.model_tiers.light"])

    def test_run_refuses_before_anything_is_started(self):
        self.write_project(
            "implementer:\n" + CLAUDE_ROLE + "  options:\n    permission_mode: bypassPermissions\n"
        )
        self.assert_run_refused("implementer", "--prompt", "x")

    def test_run_with_the_mode_from_extra_is_not_refused(self):
        self.write_global("design:\n  require_approval: false\n")
        self.write_project("implementer:\n" + CLAUDE_ROLE)
        self.assert_run_started(
            "implementer", "--prompt", "x", "--extra", "--permission-mode", "bypassPermissions"
        )
        self.assertIn("bypassPermissions", self.started[0])

    def test_a_read_only_run_is_not_refused_over_a_project_permission_mode(self):
        self.write_project(
            "architect:\n" + CLAUDE_ROLE + "  options:\n    permission_mode: bypassPermissions\n"
        )
        self.assertEqual(self.refusals(), {})
        # ``fake_clis`` gives no ``--help`` to read, which leaves enforcement
        # unverified and the run refused for that instead.
        original = ClaudeProvider.read_only_enforcement
        self.addCleanup(setattr, ClaudeProvider, "read_only_enforcement", original)
        setattr(ClaudeProvider, "read_only_enforcement", lambda provider: {"status": "verified"})
        self.assert_run_started("architect", "--prompt", "x")

    def test_validate_and_doctor_report_the_refusal(self):
        self.write_project(
            "implementer:\n" + CLAUDE_ROLE + "  options:\n    permission_mode: bypassPermissions\n"
        )
        _, out, err = run_cli("config", "validate", "--json")
        warnings = json.loads(out)["warnings"]
        self.assertTrue([w for w in warnings if w.startswith("implementer:") and REFUSED in w], out + err)
        report = doctor.collect(self.project, probe_models=False)
        self.assertIn("implementer: options.permission_mode", " ".join(report["problems"]))


class TestCodex(_Case):
    def test_a_project_sandbox_or_approve_is_refused(self):
        for option in ("sandbox: danger-full-access", "approve: false"):
            with self.subTest(option=option):
                self.write_project("implementer:\n" + CODEX_ROLE + "  options:\n    %s\n" % option)
                refusals = self.refusals()
                self.assertEqual(list(refusals), ["implementer"])
                self.assertIn("options.sandbox / options.approve", refusals["implementer"])

    def test_project_raw_arguments_on_a_codex_write_role_are_refused(self):
        for role in ("implementer", "review_fixer"):
            with self.subTest(role=role):
                self.write_project(
                    role + ":\n" + CODEX_ROLE + '  options:\n    args: ["-s", "danger-full-access"]\n'
                )
                self.assertEqual(list(self.refusals()), [role])

    def test_empty_project_raw_arguments_are_not_refused(self):
        self.write_project("implementer:\n" + CODEX_ROLE + "  options:\n    args: []\n")
        self.assertEqual(self.refusals(), {})

    def test_the_global_sandbox_is_allowed(self):
        self.write_global("implementer:\n  provider: codex\n  options:\n    sandbox: danger-full-access\n")
        self.write_project("implementer:\n" + CODEX_ROLE)
        self.assertEqual(self.refusals(), {})

    def test_a_project_sandbox_over_a_global_one_is_refused(self):
        self.write_global("implementer:\n" + CODEX_ROLE + "  options:\n    sandbox: workspace-write\n")
        self.write_project("implementer:\n  options:\n    sandbox: danger-full-access\n")
        self.assertEqual(list(self.refusals()), ["implementer"])

    def test_a_project_tier_sandbox_is_refused(self):
        self.write_project(
            "implementer:\n"
            + CODEX_ROLE
            + "  model_tiers:\n    light:\n      model:\n        family: recommended-coding\n"
            "      options:\n        sandbox: danger-full-access\n"
        )
        self.assertEqual(list(self.refusals()), ["implementer.model_tiers.light"])

    def test_a_tier_that_switches_to_claude_without_options_takes_none_of_the_role(self):
        self.write_project(
            "implementer:\n" + CODEX_ROLE + "  options:\n    sandbox: danger-full-access\n"
            "  model_tiers:\n    light:\n      provider: claude\n      model:\n        family: sonnet\n"
        )
        self.assertEqual(list(self.refusals()), ["implementer"])

    def test_run_refuses_before_anything_is_started(self):
        self.write_project("implementer:\n" + CODEX_ROLE + "  options:\n    sandbox: danger-full-access\n")
        self.assert_run_refused("implementer", "--prompt", "x")


if __name__ == "__main__":
    unittest.main()
