"""The gates around agy: what a project file may not set, and what is said once.

agy cannot be held to reading, and its permission bypass is local-only. A
project file -- which a branch under review can change -- must not put it on
a read-only seat, nor enable its bypass or raw arguments on a write role.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git

from orchestrator import cli, doctor, execution, presets
from orchestrator import config as config_mod
from orchestrator import review as review_mod
from orchestrator.providers.agy import AgyProvider
from orchestrator.providers.mock import MockProvider

AGY_MODELS = (
    "Fetching available models...\n"
    "gemini-3.7-flash-high\tGemini 3.7 Flash (high)\n"
    "gemini-3.8-flash\tGemini 3.8 Flash\n"
    "gemini-3.8-flash-low\tGemini 3.8 Flash (low)\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (high)\n"
    "gemini-3.1-pro-high\tGemini 3.1 Pro (high)\n"
)

UNENFORCED = "read-only is NOT enforced by agy"


def run_cli(*argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class _Completed:
    def __init__(self, stdout):
        self.stdout, self.stderr, self.returncode = stdout, "", 0


class _GateCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.fake_clis(agy=True)
        self.addCleanup(setattr, execution, "execute", execution.execute)

    def write_global(self, text):
        path = config_mod.global_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("version: 1\n" + text)

    def write_project(self, text):
        self.write(".dev-orchestra.yaml", "version: 1\n" + text)

    def project_file(self):
        return os.path.join(self.project, ".dev-orchestra.yaml")

    def answer(self, response="READY"):
        """Replace every child process with agy printing one JSON result."""
        printed = json.dumps({"conversation_id": "c", "status": "SUCCESS", "response": response}) + "\n"
        self.started = []

        def execute(command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
            self.started.append(list(command))
            return execution.ExecOutcome(0, printed, "", 0.1)

        execution.execute = execute

    def unenforced_mock(self):
        """Mock, reporting ``unenforced`` from a report that is not static --
        as a user adapter that reads ``--help`` would."""
        report = {"status": "unenforced", "mechanism": "none", "detail": "measured to write"}
        self.assertFalse(MockProvider.static_enforcement)
        original = MockProvider.read_only_enforcement
        self.addCleanup(setattr, MockProvider, "read_only_enforcement", original)
        setattr(MockProvider, "read_only_enforcement", lambda provider: dict(report))


AGY_ROLE = "  provider: agy\n  model:\n    family: default\n"
AGY_TIER = "  model_tiers:\n    light:\n      provider: agy\n      model:\n        family: default\n"
AGY_REVIEWER = "  - id: gem\n    provider: agy\n    role: general\n    model:\n      family: default\n"


class TestProjectSeatRefusals(_GateCase):
    """Roles, tiers and reviewers, from the project file and from the global one."""

    def test_a_role_on_agy_from_the_project_is_refused(self):
        self.write_project("architect:\n" + AGY_ROLE)
        loaded = config_mod.load(self.project)
        refusals = config_mod.project_raw_arg_refusals(loaded)
        self.assertEqual(list(refusals), ["architect"])
        self.assertIn("taken only from the global config", refusals["architect"])
        self.assertIn("config set --scope global architect.provider agy", refusals["architect"])
        self.assertEqual(config_mod.read_only_enforcement_warnings(loaded.data, list(refusals)), [])

    def test_the_same_role_from_the_global_file_is_warned_not_refused(self):
        self.write_global("architect:\n" + AGY_ROLE)
        loaded = config_mod.load(self.project)
        self.assertEqual(config_mod.project_raw_arg_refusals(loaded), {})
        warnings = config_mod.read_only_enforcement_warnings(loaded.data)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertTrue(warnings[0].startswith("architect: %s" % UNENFORCED))

    def test_a_tier_that_names_agy_in_the_project_is_refused_alone(self):
        self.write_project("architect:\n" + AGY_TIER)
        loaded = config_mod.load(self.project, validate_result=False)
        refusals = config_mod.project_raw_arg_refusals(loaded)
        self.assertEqual(list(refusals), ["architect.model_tiers.light"])
        self.assertIn("architect.model_tiers.light.provider", refusals["architect.model_tiers.light"])

    def test_a_tier_inherits_the_layer_of_the_role_provider(self):
        """No provider of its own: the project file that set the role's chose it."""
        self.write_project(
            "architect:\n" + AGY_ROLE + "  model_tiers:\n    light:\n      model:\n        family: default\n"
        )
        loaded = config_mod.load(self.project, validate_result=False)
        self.assertEqual(
            sorted(config_mod.project_raw_arg_refusals(loaded)), ["architect", "architect.model_tiers.light"]
        )

    def test_a_global_tier_on_agy_is_not_refused(self):
        self.write_global("architect:\n" + AGY_TIER)
        loaded = config_mod.load(self.project, validate_result=False)
        self.assertEqual(config_mod.project_raw_arg_refusals(loaded), {})

    def test_a_project_panel_with_an_agy_reviewer_is_refused(self):
        self.write_project("reviewers:\n" + AGY_REVIEWER)
        loaded = config_mod.load(self.project)
        refusals = config_mod.reviewer_raw_arg_refusals(loaded)
        self.assertEqual(list(refusals), ["gem"])
        self.assertIn("reviewers on agy are taken only from the global config", refusals["gem"])

    def test_a_global_panel_with_an_agy_reviewer_is_warned(self):
        self.write_global("reviewers:\n" + AGY_REVIEWER)
        loaded = config_mod.load(self.project)
        self.assertEqual(config_mod.reviewer_raw_arg_refusals(loaded), {})
        warned = config_mod.reviewer_enforcement_warnings(loaded.data)
        self.assertEqual(list(warned), ["gem"])


class TestProjectWriteRefusals(_GateCase):
    def test_the_bypass_from_the_project_is_refused(self):
        self.write_project("implementer:\n" + AGY_ROLE + "  options:\n    skip_permissions: false\n")
        refusals = config_mod.project_write_refusals(config_mod.load(self.project))
        self.assertEqual(list(refusals), ["implementer"])
        self.assertIn("taken only from the global config or from --extra", refusals["implementer"])

    def test_raw_arguments_from_the_project_are_refused_on_agy(self):
        self.write_project("review_fixer:\n" + AGY_ROLE + '  options:\n    args: ["--x"]\n')
        refusals = config_mod.project_write_refusals(config_mod.load(self.project))
        self.assertEqual(list(refusals), ["review_fixer"])

    def test_agy_itself_and_the_global_bypass_are_allowed(self):
        self.write_global("implementer:\n  options:\n    skip_permissions: true\n")
        self.write_project("implementer:\n" + AGY_ROLE)
        self.assertEqual(config_mod.project_write_refusals(config_mod.load(self.project)), {})

    def test_a_project_bypass_is_refused_by_run_before_anything_is_started(self):
        self.write_project("implementer:\n" + AGY_ROLE + "  options:\n    skip_permissions: true\n")
        self.answer()
        code, _, err = run_cli("run", "implementer", "--prompt", "x")
        self.assertEqual(code, 2)
        self.assertIn("permission bypass and raw arguments", err)
        self.assertEqual(self.started, [])


class TestProjectScopeWrites(_GateCase):
    def add_global(self, provider, reviewer_id):
        code, _, err = run_cli(
            "reviewer", "add", "--scope", "global", "--provider", provider, "--id", reviewer_id
        )
        self.assertEqual(code, 0, err)

    def test_config_set_refuses_agy_on_a_read_only_role(self):
        code, _, err = run_cli("config", "set", "--scope", "project", "architect.provider", "agy")
        self.assertEqual(code, 2)
        self.assertIn("taken only from the global config", err)
        self.assertFalse(os.path.exists(self.project_file()))

    def test_config_set_refuses_agy_on_a_tier(self):
        code, _, err = run_cli(
            "config", "set", "--scope", "project", "orchestrator.model_tiers.light.provider", "agy"
        )
        self.assertEqual(code, 2)
        self.assertIn("orchestrator (tier light)", err)

    def test_reviewer_add_refuses_agy(self):
        code, _, err = run_cli("reviewer", "add", "--scope", "project", "--provider", "agy", "--id", "gem")
        self.assertEqual(code, 2)
        self.assertIn("reviewer gem", err)
        self.assertFalse(os.path.exists(self.project_file()))

    def test_reviewer_set_refuses_agy(self):
        self.write_project("reviewers:\n  - id: m1\n    provider: mock\n    role: general\n")
        code, _, err = run_cli("reviewer", "set", "m1", "--scope", "project", "--provider", "agy")
        self.assertEqual(code, 2)
        self.assertIn("reviewers on agy are taken only from the global config", err)

    def test_reviewer_add_says_the_copied_agy_reviewer_is_refused(self):
        """A project panel starts as a copy of the global one, agy reviewers
        included, and those are refused from then on: said, not silent."""
        self.add_global("agy", "gem")
        code, _, err = run_cli("reviewer", "add", "--scope", "project", "--provider", "mock", "--id", "m1")
        self.assertEqual(code, 0, err)
        self.assertIn("warning: reviewer gem: the reviewers list comes from the project config", err)

    def test_reviewer_set_says_so_too(self):
        self.add_global("agy", "gem")
        self.add_global("mock", "m1")
        code, _, err = run_cli("reviewer", "set", "m1", "--scope", "project", "--role", "security")
        self.assertEqual(code, 0, err)
        self.assertIn("warning: reviewer gem: the reviewers list comes from the project config", err)

    def test_reviewer_remove_says_so_too(self):
        self.add_global("agy", "gem")
        self.add_global("mock", "m1")
        self.add_global("mock", "m2")
        code, _, err = run_cli("reviewer", "remove", "m2", "--scope", "project")
        self.assertEqual(code, 0, err)
        self.assertIn("warning: reviewer gem: the reviewers list comes from the project config", err)


class TestLiveUnenforcedFromTheProject(_GateCase):
    """An adapter whose report is not static is refused on the live one."""

    def test_run_refuses_a_project_seat_whose_adapter_reports_unenforced(self):
        self.unenforced_mock()
        self.write_project("architect:\n  provider: mock\n")
        code, _, err = run_cli("run", "architect", "--prompt", "x")
        self.assertEqual(code, 2)
        self.assertIn("architect: provider mock is set in the project config", err)

    def test_the_same_seat_from_the_global_file_runs_warned(self):
        self.unenforced_mock()
        self.write_global("architect:\n  provider: mock\n")
        code, _, err = run_cli("run", "architect", "--prompt", "x")
        self.assertEqual(code, 0, err)
        self.assertIn("read-only is NOT enforced by mock", err)

    @unittest.skipUnless(has_git(), "git not available")
    def test_review_run_refuses_project_reviewers_whose_adapter_reports_unenforced(self):
        self.unenforced_mock()
        self.init_git_repo()
        self.write("app.py", "a = 1\n")
        self.commit_all("init")
        self.write("app.py", "a = 2\n")
        self.write_project(
            "optimization:\n  level: quality\nreviewers:\n  - id: r1\n    provider: mock\n    role: general\n"
        )
        review_mod.create_snapshot(self.cli_workspace())
        calls = []
        original = MockProvider.run
        self.addCleanup(setattr, MockProvider, "run", original)
        setattr(MockProvider, "run", lambda provider, *a, **k: calls.append(a) or original(provider, *a, **k))
        _, out, err = run_cli("review", "run")
        self.assertIn("reviewers on mock are taken only from the global config", out + err)
        self.assertEqual(calls, [], "a refused reviewer must not run")


class TestEnforcementWarningIsSaidOnce(_GateCase):
    def test_run_prints_it_once(self):
        self.write_global("architect:\n" + AGY_ROLE)
        self.answer()
        code, _, err = run_cli("run", "architect", "--prompt", "x")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(err.count(UNENFORCED), 1, err)

    @unittest.skipUnless(has_git(), "git not available")
    def test_review_run_prints_it_once_per_reviewer(self):
        self.init_git_repo()
        self.write("app.py", "a = 1\n")
        self.commit_all("init")
        self.write("app.py", "a = 2\n")
        self.write_global("optimization:\n  level: quality\nreviewers:\n" + AGY_REVIEWER)
        review_mod.create_snapshot(self.cli_workspace())
        self.answer("NO_FINDINGS")
        _, out, err = run_cli("review", "run")
        self.assertEqual(len(self.started), 1, out + err)
        self.assertEqual(err.count(UNENFORCED), 1, err)


class TestPrintCommand(_GateCase):
    def test_the_printed_agy_command_gives_p_one_value(self):
        self.write_global("implementer:\n" + AGY_ROLE)
        code, out, err = run_cli("run", "implementer", "--print-command")
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"-p [\"']Read the file \.ai/agy-prompt-<pid>-<random>\.md ")


class TestModelList(_GateCase):
    def test_the_families_to_configure_are_listed_with_what_they_resolve_to(self):
        original = AgyProvider._capture
        self.addCleanup(setattr, AgyProvider, "_capture", original)
        setattr(AgyProvider, "_capture", lambda provider, command, timeout=30: _Completed(AGY_MODELS))
        code, out, _ = run_cli("model", "list", "--provider", "agy")
        self.assertEqual(code, 0)
        self.assertIn("families to put in a config", out)
        self.assertRegex(out, r"family=gemini-flash +now gemini-3\.8-flash-high")
        self.assertRegex(out, r"family=gemini-flash-low +now gemini-3\.8-flash-low")
        self.assertRegex(out, r"family=gemini-pro +now gemini-3\.1-pro-high")
        self.assertRegex(out, r"family=default +now agy default")
        # One that nothing listed resolves to is left out.
        self.assertNotIn("family=gemini-pro-low", out)
        code, out, _ = run_cli("model", "list", "--provider", "agy", "--json")
        families = {item["family"]: item["resolves_to"] for item in json.loads(out)["agy"]["families"]}
        self.assertEqual(families["gemini-flash"], "gemini-3.8-flash-high")


class TestDoctor(_GateCase):
    def notes_and_problems(self):
        report = doctor.collect(self.project, probe_models=False)
        return " ".join(report["notes"]), " ".join(report["problems"])

    def test_a_global_seat_on_agy_is_a_note(self):
        self.write_global("architect:\n" + AGY_ROLE)
        notes, problems = self.notes_and_problems()
        self.assertIn("Architect: read-only runs are NOT enforced by agy (allowed, warned)", notes)
        self.assertNotIn("Architect: read-only runs", problems)
        self.assertNotIn("provider agy is set in the project config", problems)

    def test_a_project_seat_on_agy_is_a_problem_and_not_also_a_note(self):
        self.write_project("architect:\n" + AGY_ROLE)
        notes, problems = self.notes_and_problems()
        self.assertIn("architect: provider agy is set in the project config", problems)
        self.assertNotIn("Architect: read-only runs are NOT enforced", notes)


class TestPresetsWithAgy(unittest.TestCase):
    def providers_of(self, fit):
        roles = {role: fit.values[role]["provider"] for role in config_mod.KNOWN_ROLES}
        return roles, {reviewer["provider"] for reviewer in fit.values["reviewers"]}

    def test_agy_alone_takes_only_the_write_roles(self):
        for name in presets.NAMES:
            with self.subTest(preset=name):
                roles, panel = self.providers_of(presets.expand(name, ["agy"]))
                self.assertEqual(roles["implementer"], "agy")
                self.assertEqual(roles["review_fixer"], "agy")
                self.assertNotEqual(roles["orchestrator"], "agy")
                self.assertNotEqual(roles["architect"], "agy")
                self.assertNotIn("agy", panel)
                fit = presets.expand(name, ["agy"])
                self.assertEqual(fit.values["implementer"]["model"]["family"], "default")

    def test_with_claude_there_agy_takes_nothing(self):
        for name in presets.NAMES:
            with self.subTest(preset=name):
                roles, panel = self.providers_of(presets.expand(name, ["agy", "claude"]))
                self.assertEqual(set(roles.values()), {"claude"})
                self.assertEqual(panel, {"claude"})


if __name__ == "__main__":
    unittest.main()
