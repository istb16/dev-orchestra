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
from pathlib import Path

from helpers import IsolatedCase, has_git

from orchestrator import cli, doctor, execution, miniyaml, presets
from orchestrator import config as config_mod
from orchestrator import config_policy as policy_mod
from orchestrator import review as review_mod
from orchestrator.providers import get_provider, unenforced_warning
from orchestrator.providers.agy import AgyProvider
from orchestrator.providers.base import Detection, Provider
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

AGY_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "agy")

#: The recorded tool run's answer, which it also streamed whole.
AGY_TOOLS_RESPONSE = (
    "[output.txt](file:///C:/sandbox/agy193/output.txt) has been created with the lines of "
    "`input.txt` in reverse order (`gamma`, `beta`, `alpha`).\n"
)


def agy_fixture(name):
    with open(os.path.join(AGY_FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


#: What a fit note on a read-only seat that went to agy ends with.
ROLE_OPT_OUT = "; agy cannot be held to reading -- set %s in the global file to keep it off agy"
SEAT_OPT_OUT = "; agy cannot be held to reading -- list reviewers in the global file to keep them off agy"
DESIGN_SEAT_OPT_OUT = (
    "; agy cannot be held to reading -- list review.design.reviewers in the global file to keep them off agy"
)


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

    def answer(self, response="READY", stdout=None):
        """Replace every child process with agy printing one stream ``result``
        line, or ``stdout`` when given."""
        result = {"conversation_id": "c", "status": "SUCCESS", "response": response}
        printed = json.dumps({"event": "result", "result": result}) + "\n" if stdout is None else stdout
        self.started = []

        def execute(command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
            self.started.append(list(command))
            return execution.ExecOutcome(0, printed, "", 0.1)

        execution.execute = execute

    def unenforced_mock(self, status="unenforced", detail="measured to write"):
        """Mock, reporting ``status`` from a report that is not static --
        as a user adapter that reads ``--help`` would."""
        report = {"status": status, "mechanism": "none", "detail": detail}
        self.assertFalse(MockProvider.static_enforcement)
        original = MockProvider.read_only_enforcement
        self.addCleanup(setattr, MockProvider, "read_only_enforcement", original)
        setattr(MockProvider, "read_only_enforcement", lambda provider: dict(report))

    def used(self, role="architect"):
        return json.loads(run_cli("budget", "show", "--json")[1])["budgets"][role]["used"]

    def events(self, stage="architect"):
        state = self.cli_workspace().read_state()
        return [event for event in state.get("events") or [] if event.get("stage") == stage]


AGY_ROLE = "  provider: agy\n  model:\n    family: default\n"
AGY_TIER = "  model_tiers:\n    light:\n      provider: agy\n      model:\n        family: default\n"
AGY_REVIEWER = "  - id: gem\n    provider: agy\n    role: general\n    model:\n      family: default\n"


class TestProjectSeatRefusals(_GateCase):
    """Roles, tiers and reviewers, from the project file and from the global one."""

    def test_a_role_on_agy_from_the_project_is_refused(self):
        self.write_project("architect:\n" + AGY_ROLE)
        loaded = config_mod.load(self.project)
        refusals = policy_mod.project_raw_arg_refusals(loaded)
        self.assertEqual(list(refusals), ["architect"])
        self.assertIn("taken only from the global config", refusals["architect"])
        self.assertIn("config set --scope global architect.provider agy", refusals["architect"])
        warnings = policy_mod.read_only_enforcement_warnings(loaded.data, list(refusals))
        self.assertEqual([line for line in warnings if line.startswith("architect:")], [])

    def test_the_same_role_from_the_global_file_is_warned_not_refused(self):
        self.write_global("architect:\n" + AGY_ROLE)
        loaded = config_mod.load(self.project)
        self.assertEqual(policy_mod.project_raw_arg_refusals(loaded), {})
        warnings = policy_mod.read_only_enforcement_warnings(loaded.data)
        architect = [line for line in warnings if line.startswith("architect:")]
        self.assertEqual(len(architect), 1, warnings)
        self.assertTrue(architect[0].startswith("architect: %s" % UNENFORCED))

    def test_a_tier_that_names_agy_in_the_project_is_refused_alone(self):
        self.write_project("architect:\n" + AGY_TIER)
        loaded = config_mod.load(self.project, validate_result=False)
        refusals = policy_mod.project_raw_arg_refusals(loaded)
        self.assertEqual(list(refusals), ["architect.model_tiers.light"])
        self.assertIn("architect.model_tiers.light.provider", refusals["architect.model_tiers.light"])

    def test_a_tier_inherits_the_layer_of_the_role_provider(self):
        """No provider of its own: the project file that set the role's chose it."""
        self.write_project(
            "architect:\n" + AGY_ROLE + "  model_tiers:\n    light:\n      model:\n        family: default\n"
        )
        loaded = config_mod.load(self.project, validate_result=False)
        self.assertEqual(
            sorted(policy_mod.project_raw_arg_refusals(loaded)), ["architect", "architect.model_tiers.light"]
        )

    def test_a_global_tier_on_agy_is_not_refused(self):
        self.write_global("architect:\n" + AGY_TIER)
        loaded = config_mod.load(self.project, validate_result=False)
        self.assertEqual(policy_mod.project_raw_arg_refusals(loaded), {})

    def test_a_project_panel_with_an_agy_reviewer_is_refused(self):
        self.write_project("reviewers:\n" + AGY_REVIEWER)
        loaded = config_mod.load(self.project)
        refusals = policy_mod.reviewer_raw_arg_refusals(loaded)
        self.assertEqual(list(refusals), ["gem"])
        self.assertIn("reviewers on agy are taken only from the global config", refusals["gem"])

    def test_a_global_panel_with_an_agy_reviewer_is_warned(self):
        self.write_global("reviewers:\n" + AGY_REVIEWER)
        loaded = config_mod.load(self.project)
        self.assertEqual(policy_mod.reviewer_raw_arg_refusals(loaded), {})
        warned = policy_mod.reviewer_enforcement_warnings(loaded.data)
        self.assertEqual(list(warned), ["gem"])

    def test_run_refuses_a_project_tier_on_agy(self):
        self.write_project("architect:\n" + AGY_TIER)
        self.answer()
        code, _, err = run_cli("run", "architect", "--tier", "light", "--prompt", "x")
        self.assertEqual(code, 2)
        self.assertIn("architect (tier light): provider agy is set in the project config", err)
        self.assertEqual(self.started, [])


#: Every kind of read-only seat at once: roles, tiers with and without options,
#: tier keys that are not plain names, and broken entries that are skipped.
SEAT_WALK_GLOBAL = (
    "architect:\n" + AGY_ROLE + "  model_tiers:\n    keep:\n      model:\n        family: other\n"
    "orchestrator:\n  model_tiers:\n    g:\n      provider: agy\n      model:\n        family: default\n"
)
SEAT_WALK_NOPE = "  - nope\n"
SEAT_WALK_PROJECT = (
    "orchestrator:\n"
    "  provider: mock\n"
    '  options:\n    args: ["--add-dir", "o"]\n'
    "  model_tiers:\n"
    '    own:\n      options:\n        args: ["--add-dir", "t"]\n'
    "    0:\n      model:\n        family: other\n"
    "    a.b:\n      provider: agy\n      model:\n        family: default\n"
    "    broken: 3\n"
    "reviewers:\n"
    "%s"
    "  - id: r-args\n    provider: mock\n    role: general\n"
    '    options:\n      args: ["--add-dir", "r"]\n'
    "  - id: r-agy\n    provider: agy\n    role: general\n    model:\n      family: default\n"
)
PROJECT_ARGS = (
    "%s: options.args is set in the project config (.dev-orchestra.yaml); read-only roles take raw "
    "arguments only from the global config or from --extra"
)
SEAT_WALK_REFUSALS = [
    ("orchestrator", PROJECT_ARGS % "orchestrator"),
    ("orchestrator.model_tiers.own", PROJECT_ARGS % "orchestrator (tier own)"),
    ("orchestrator.model_tiers.0", PROJECT_ARGS % "orchestrator (tier 0)"),
    ("reviewers[1]", PROJECT_ARGS % "reviewer r-args"),
    (
        "orchestrator.model_tiers.a.b",
        "orchestrator (tier a.b): provider agy is set in the project config (.dev-orchestra.yaml); "
        "a read-only seat on agy is taken only from the global config -- if this is intended, run "
        "`dev-orchestra config set --scope global orchestrator.model_tiers.a.b.provider agy` and "
        "remove it from .dev-orchestra.yaml",
    ),
    (
        "reviewers[2]",
        "reviewer r-agy: the reviewers list comes from the project config (.dev-orchestra.yaml) and "
        "this reviewer is on agy; reviewers on agy are taken only from the global config -- if this "
        "is intended, add it there with `dev-orchestra reviewer add --scope global --provider agy` "
        "and remove it from .dev-orchestra.yaml",
    ),
]


class TestSeatWalks(_GateCase):
    """The order and the labels of every walk over the read-only seats."""

    def unenforced(self):
        report = get_provider("agy").read_only_enforcement()
        return unenforced_warning("agy", report), report["detail"]

    def load_walk(self, nope=SEAT_WALK_NOPE):
        self.write_global(SEAT_WALK_GLOBAL)
        self.write_project(SEAT_WALK_PROJECT % nope)
        return config_mod.load(self.project, validate_result=False)

    def test_every_walk_in_order(self):
        loaded = self.load_walk()
        self.assertEqual(
            [tuple(entry)[:7] for entry in policy_mod.read_only_raw_args(loaded)],
            [
                ("orchestrator", "orchestrator", "", "mock", ["--add-dir", "o"], "project", "project"),
                ("orchestrator.model_tiers.g", "orchestrator (tier g)", "", "agy", [], "project", "global"),
                (
                    "orchestrator.model_tiers.own",
                    "orchestrator (tier own)",
                    "",
                    "mock",
                    ["--add-dir", "t"],
                    "project",
                    "project",
                ),
                (
                    "orchestrator.model_tiers.0",
                    "orchestrator (tier 0)",
                    "",
                    "mock",
                    ["--add-dir", "o"],
                    "project",
                    "project",
                ),
                (
                    "orchestrator.model_tiers.a.b",
                    "orchestrator (tier a.b)",
                    "",
                    "agy",
                    [],
                    "project",
                    "project",
                ),
                ("architect", "architect", "", "agy", [], "default", "global"),
                ("architect.model_tiers.keep", "architect (tier keep)", "", "agy", [], "default", "global"),
                (
                    "reviewers[1]",
                    "reviewer r-args",
                    "r-args",
                    "mock",
                    ["--add-dir", "r"],
                    "project",
                    "project",
                ),
                ("reviewers[2]", "reviewer r-agy", "r-agy", "agy", [], "default", "project"),
            ],
        )
        refusals = policy_mod.project_raw_arg_refusals(loaded)
        self.assertEqual(list(refusals.items()), SEAT_WALK_REFUSALS)
        self.assertEqual(
            list(policy_mod.project_provider_refusals(loaded)),
            [
                "orchestrator",
                "orchestrator.model_tiers.own",
                "orchestrator.model_tiers.0",
                "orchestrator.model_tiers.a.b",
                "reviewers[1]",
                "reviewers[2]",
            ],
        )
        self.assertEqual(list(policy_mod.reviewer_raw_arg_refusals(loaded)), ["r-args", "r-agy"])
        self.assertEqual(
            policy_mod.read_only_arg_warnings(loaded), [message for _, message in SEAT_WALK_REFUSALS]
        )
        self.assertEqual(
            [label for label, _, _ in policy_mod._enforcement_warned_seats(loaded.data)],
            ["orchestrator.model_tiers.g", "orchestrator.model_tiers.a.b", "architect", "reviewers[2]"],
        )
        unenforced, _ = self.unenforced()
        self.assertEqual(
            policy_mod._enforcement_warned_seats(loaded.data, list(refusals)),
            [
                ("orchestrator.model_tiers.g", "", "orchestrator (tier g): " + unenforced),
                ("architect", "", "architect: " + unenforced),
            ],
        )

    def test_an_int_tier_key_and_a_broken_role_in_scratch_data(self):
        architect = {"provider": "mock", "model_tiers": {0: {"provider": "agy"}}}
        data = {"orchestrator": 3, "architect": architect}
        unenforced, _ = self.unenforced()
        self.assertEqual(
            policy_mod._enforcement_warned_seats(data),
            [("architect.model_tiers.0", "", "architect (tier 0): " + unenforced)],
        )
        self.assertEqual(policy_mod.read_only_enforcement_warnings(data, ["architect.model_tiers.0"]), [])

    def test_doctor_on_the_same_files(self):
        # Without the non-mapping reviewer: doctor does not skip one.
        self.load_walk(nope="")
        report = doctor.collect(self.project, probe_models=False)
        self.assertEqual(
            [p for p in report["problems"] if "project config (.dev-orchestra.yaml)" in p],
            [message for _, message in SEAT_WALK_REFUSALS],
        )
        _, detail = self.unenforced()
        note = "%s: read-only runs are NOT enforced by agy (allowed, warned) -- %s"
        self.assertEqual(
            [n for n in report["notes"] if "read-only runs" in n],
            [note % ("Orchestrator (tier g)", detail), note % ("Architect", detail)],
        )
        self.assertEqual(report["roles"]["architect"]["read_only"], "unenforced")
        self.assertNotIn("read_only", report["roles"]["orchestrator"])
        self.assertEqual([r.get("read_only") for r in report["reviewers"]], [None, "unenforced"])

    def test_extras_take_the_layer_of_their_own_file(self):
        """An extra's layer is its file's, and its label is its place in the folded panel."""
        self.write_global(
            "reviewers:\n  - id: base\n    provider: mock\n    role: general\n"
            "reviewers_extra:\n"
            "  - id: g-args\n    provider: mock\n    role: general\n"
            '    options:\n      args: ["--add-dir", "g"]\n'
            "  - id: g-plain\n    provider: mock\n    role: general\n"
        )
        # The non-mapping entry is not folded, so p-args is reviewers[3] at position 1.
        self.write_project(
            "reviewers_extra:\n"
            "  - nope\n"
            "  - id: p-args\n    provider: mock\n    role: general\n"
            '    options:\n      args: ["--add-dir", "p"]\n'
            "  - id: p-plain\n    provider: mock\n    role: general\n"
        )
        loaded = config_mod.load(self.project, validate_result=False)
        self.assertEqual(
            [tuple(entry)[:7] for entry in policy_mod.read_only_raw_args(loaded) if entry.reviewer_id],
            [
                ("reviewers[0]", "reviewer base", "base", "mock", [], "default", "global"),
                ("reviewers[1]", "reviewer g-args", "g-args", "mock", ["--add-dir", "g"], "global", "global"),
                ("reviewers[2]", "reviewer g-plain", "g-plain", "mock", [], "default", "global"),
                (
                    "reviewers[3]",
                    "reviewer p-args",
                    "p-args",
                    "mock",
                    ["--add-dir", "p"],
                    "project",
                    "project",
                ),
                ("reviewers[4]", "reviewer p-plain", "p-plain", "mock", [], "default", "project"),
            ],
        )
        refusals = policy_mod.project_raw_arg_refusals(loaded)
        self.assertEqual(list(refusals.items()), [("reviewers[3]", PROJECT_ARGS % "reviewer p-args")])
        self.assertEqual(list(policy_mod.project_provider_refusals(loaded)), ["reviewers[3]", "reviewers[4]"])

    def test_doctor_on_a_role_that_is_not_a_mapping(self):
        self.write_global("architect: 3\n")
        report = doctor.collect(self.project, probe_models=False)
        self.assertEqual(report["roles"]["architect"]["status"], "missing")
        self.assertIn("Architect: not configured", report["problems"])
        self.assertEqual([n for n in report["notes"] if n.startswith("Architect")], [])


class TestProjectWriteRefusals(_GateCase):
    def test_the_bypass_from_the_project_is_refused(self):
        self.write_project("implementer:\n" + AGY_ROLE + "  options:\n    skip_permissions: false\n")
        refusals = policy_mod.project_write_refusals(config_mod.load(self.project))
        self.assertEqual(list(refusals), ["implementer"])
        self.assertIn("taken only from the global config or from --extra", refusals["implementer"])

    def test_raw_arguments_from_the_project_are_refused_on_agy(self):
        self.write_project("review_fixer:\n" + AGY_ROLE + '  options:\n    args: ["--x"]\n')
        refusals = policy_mod.project_write_refusals(config_mod.load(self.project))
        self.assertEqual(list(refusals), ["review_fixer"])

    def test_agy_itself_and_the_global_bypass_are_allowed(self):
        self.write_global("implementer:\n  options:\n    skip_permissions: true\n")
        self.write_project("implementer:\n" + AGY_ROLE)
        self.assertEqual(policy_mod.project_write_refusals(config_mod.load(self.project)), {})

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

    def test_reviewer_add_keeps_the_global_agy_reviewer_in_the_global_file(self):
        """A project addition is an extra: nothing is copied, so the global
        agy reviewer stays where the global file put it and is not refused."""
        self.add_global("agy", "gem")
        code, out, err = run_cli("reviewer", "add", "--scope", "project", "--provider", "mock", "--id", "m1")
        self.assertEqual(code, 0, err)
        self.assertIn("as an extra", out)
        self.assertNotIn("comes from the project config", err)
        project = miniyaml.loads(Path(self.project, ".dev-orchestra.yaml").read_text(encoding="utf-8"))
        self.assertEqual([entry["id"] for entry in project["reviewers_extra"]], ["m1"])
        self.assertNotIn("reviewers", project)

    def test_reviewer_set_leaves_the_global_agy_reviewer_out_of_the_copy(self):
        self.add_global("agy", "gem")
        self.add_global("mock", "m1")
        code, out, err = run_cli("reviewer", "set", "m1", "--scope", "project", "--role", "security")
        self.assertEqual(code, 0, err)
        self.assertIn("gem -- a reviewer on agy is taken only from the global config", out)
        self.assertNotIn("comes from the project config", err)

    def test_reviewer_remove_leaves_it_out_too(self):
        self.add_global("agy", "gem")
        self.add_global("mock", "m1")
        self.add_global("mock", "m2")
        code, out, err = run_cli("reviewer", "remove", "m2", "--scope", "project")
        self.assertEqual(code, 0, err)
        self.assertIn("gem -- a reviewer on agy is taken only from the global config", out)
        self.assertNotIn("comes from the project config", err)


class TestLiveUnenforcedFromTheProject(_GateCase):
    """An adapter whose report is not static is refused on the live one."""

    def test_run_refuses_a_project_seat_whose_adapter_reports_unenforced(self):
        self.unenforced_mock()
        self.write_project("architect:\n  provider: mock\n")
        code, _, err = run_cli("run", "architect", "--prompt", "x")
        self.assertEqual(code, 2)
        self.assertIn("architect: provider mock is set in the project config", err)
        self.assertEqual(self.used(), 0)
        self.assertEqual(self.events(), [])

    def test_the_same_seat_from_the_global_file_runs_warned(self):
        self.unenforced_mock()
        self.write_global("architect:\n  provider: mock\n")
        code, _, err = run_cli("run", "architect", "--prompt", "x")
        self.assertEqual(code, 0, err)
        self.assertIn("read-only is NOT enforced by mock", err)

    def run_agy_architect(self, stdout):
        """``run architect --output plan.md`` on a global agy architect whose
        CLI prints ``stdout``; the exit code, stderr and the plan's path."""
        self.write_global("architect:\n" + AGY_ROLE)
        plan = os.path.join(self.project, "plan.md")
        with open(plan, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("# Old plan\n")
        self.answer(stdout=stdout)
        code, _, err = run_cli("run", "architect", "--prompt", "x", "--output", plan)
        return code, err, plan

    def read(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_an_agy_plan_with_no_answer_leaves_output_unchanged(self):
        code, err, plan = self.run_agy_architect(agy_fixture("stream-json-denied.jsonl"))
        self.assertEqual(code, 1, err)
        self.assertEqual(self.read(plan), "# Old plan\n")
        self.assertFalse(os.path.exists(plan + ".rejected"))
        self.assertIn("agy: no answer:", err)

    def test_partial_agy_text_goes_to_the_rejected_file(self):
        lines = agy_fixture("stream-json-tools.jsonl").splitlines(keepends=True)
        code, err, plan = self.run_agy_architect("".join(lines[:30]))
        self.assertEqual(code, 1, err)
        self.assertEqual(self.read(plan), "# Old plan\n")
        self.assertEqual(self.read(plan + ".rejected"), AGY_TOOLS_RESPONSE)

    def test_partial_agy_text_is_never_printed_as_the_answer(self):
        self.write_global("architect:\n" + AGY_ROLE)
        lines = agy_fixture("stream-json-tools.jsonl").splitlines(keepends=True)
        self.answer(stdout="".join(lines[:30]))
        code, out, err = run_cli("run", "architect", "--prompt", "x")
        self.assertEqual(code, 1, err)
        self.assertNotIn(AGY_TOOLS_RESPONSE.strip()[:40], out + err)
        self.assertIn("agy: no answer:", err)

    def test_the_end_event_records_agy_context(self):
        code, err, _ = self.run_agy_architect(agy_fixture("stream-json-tools.jsonl"))
        self.assertEqual(code, 0, err)
        ended = [event for event in self.events("architect") if "answered" in event]
        self.assertEqual(len(ended), 1, ended)
        self.assertEqual(ended[0]["context_tokens"], 14517)
        self.assertIs(ended[0]["answered"], True)

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


class TestRunChecksEnforcementInOrder(_GateCase):
    """Where `run` reads the enforcement report, relative to what else it does."""

    def seat(self, layer):
        for path in (config_mod.global_config_path(), self.project_file()):
            if os.path.exists(path):
                os.remove(path)
        write = self.write_global if layer == "global" else self.write_project
        write("architect:\n  provider: mock\n")

    def test_the_command_is_printed_before_enforcement_is_checked(self):
        asked = []

        def counted():
            # unenforced_mock restores the original in its own cleanup.
            reported = MockProvider.read_only_enforcement

            def recorded(provider):
                asked.append(provider)
                return reported(provider)

            setattr(MockProvider, "read_only_enforcement", recorded)

        for layer, report in (("global", ("unsupported", "X")), ("global", ()), ("project", ())):
            with self.subTest(layer=layer, report=report):
                self.unenforced_mock(*report)
                counted()
                self.seat(layer)
                code, out, err = run_cli("run", "architect", "--print-command")
                self.assertEqual(code, 0, err)
                self.assertTrue(out.startswith("mock "), out)
                self.assertNotIn("warning:", err)
                self.assertNotIn("refused", err)
                self.assertEqual(asked, [])
        self.seat("global")
        code, _, err = run_cli("run", "architect", "--print-command", "--prompt-file", "missing.md")
        self.assertEqual(code, 0, err)
        self.assertEqual(asked, [])
        # The wrapper is live: a real run asks it.
        code, _, err = run_cli("run", "architect", "--prompt", "x")
        self.assertTrue(asked, err)

    def test_a_refused_report_is_refused_by_run(self):
        self.unenforced_mock("unsupported", "X")
        self.write_global("architect:\n  provider: mock\n")
        code, _, err = run_cli("run", "architect", "--prompt", "x")
        self.assertEqual(code, 2)
        self.assertIn("architect: refused -- X", err)
        self.assertEqual(self.used(), 0)
        self.assertEqual(self.events(), [])

    def test_a_cli_that_is_not_installed_is_not_refused(self):
        self.unenforced_mock("unsupported", "X")
        asked = []
        reported = MockProvider.read_only_enforcement

        def recorded(provider):
            asked.append(provider)
            return reported(provider)

        setattr(MockProvider, "read_only_enforcement", recorded)
        for name, value in (
            ("detect", lambda provider: Detection(False, error="mock is not installed")),
            # The mock's own launch never looks for its CLI; the base one does.
            ("_launch", Provider._launch),
        ):
            self.addCleanup(setattr, MockProvider, name, getattr(MockProvider, name))
            setattr(MockProvider, name, value)
        self.write_global("architect:\n  provider: mock\n")
        code, _, err = run_cli("run", "architect", "--prompt", "x")
        # 127 is the run's own exit code; `run` reports it and exits 1.
        self.assertEqual(code, 1)
        self.assertIn("architect failed (exit 127): mock is not installed", err)
        self.assertNotIn("refused --", err)
        self.assertEqual(asked, [])

    def test_the_warning_is_said_before_the_prompt_is_read(self):
        self.unenforced_mock()
        self.write_global("architect:\n  provider: mock\n")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            cli.main(["run", "architect", "--prompt-file", "missing.md"])
        self.assertIn("prompt file does not exist", str(raised.exception))
        self.assertIn("read-only is NOT enforced by mock", err.getvalue())


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


class TestTheFitOnAgyAlone(_GateCase):
    """No file at all: the read-only seats are fitted to agy, and warned
    wherever a global-file agy seat is."""

    FITTED = ("orchestrator", "architect", "reviewer agy-general")

    def test_config_show_warns_about_every_fitted_seat(self):
        code, out, err = run_cli("config", "show")
        self.assertEqual(code, 0, err)
        for seat in self.FITTED:
            self.assertIn("  Warning: %s: %s" % (seat, UNENFORCED), out)
        self.assertIn(ROLE_OPT_OUT % "architect", out)
        self.assertIn(SEAT_OPT_OUT, out)

    def test_config_validate_warns_about_the_same_seats(self):
        code, out, err = run_cli("config", "validate", "--json")
        self.assertEqual(code, 0, err)
        warnings = json.loads(out)["warnings"]
        for seat in self.FITTED:
            self.assertEqual(len([w for w in warnings if w.startswith("%s: %s" % (seat, UNENFORCED))]), 1)
        loaded = config_mod.load(self.project)
        self.assertEqual(list(policy_mod.reviewer_enforcement_warnings(loaded.data)), ["agy-general"])

    def test_doctor_notes_the_seats_and_finds_no_problem(self):
        report = doctor.collect(self.project, probe_models=False)
        notes = " ".join(report["notes"])
        self.assertIn("Architect: read-only runs are NOT enforced by agy (allowed, warned)", notes)
        self.assertIn("Reviewer agy-general: read-only runs are NOT enforced by agy (allowed, warned)", notes)
        self.assertNotIn("read-only runs", " ".join(report["problems"]))
        self.assertEqual(doctor.exit_code(report), 0, report["problems"])

    @unittest.skipUnless(has_git(), "git not available")
    def test_review_run_warns_about_the_fitted_reviewer(self):
        self.init_git_repo()
        self.write("app.py", "a = 1\n")
        self.commit_all("init")
        self.write("app.py", "a = 2\n")
        self.write_global("optimization:\n  level: quality\n")
        review_mod.create_snapshot(self.cli_workspace())
        self.answer("NO_FINDINGS")
        _, out, err = run_cli("review", "run")
        self.assertEqual(len(self.started), 1, out + err)
        self.assertIn("warning: reviewer agy-general: %s" % UNENFORCED, err)

    def test_a_project_role_on_agy_is_still_refused_beside_the_fitted_orchestrator(self):
        self.write_project("architect:\n" + AGY_ROLE)
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.data["orchestrator"]["provider"], "agy")
        refusals = policy_mod.project_raw_arg_refusals(loaded)
        self.assertEqual(list(refusals), ["architect"])
        warnings = policy_mod.read_only_enforcement_warnings(loaded.data, list(refusals))
        self.assertTrue([w for w in warnings if w.startswith("orchestrator: %s" % UNENFORCED)], warnings)
        report = doctor.collect(self.project, probe_models=False)
        problems = " ".join(report["problems"])
        self.assertIn("architect: provider agy is set in the project config", problems)
        self.assertNotIn("orchestrator: provider agy", problems)
        self.assertIn("Orchestrator: read-only runs are NOT enforced by agy", " ".join(report["notes"]))
        self.answer()
        code, _, err = run_cli("run", "architect", "--prompt", "x")
        self.assertEqual(code, 2)
        self.assertIn("architect: provider agy is set in the project config", err)
        self.assertEqual(self.started, [])


class TestSeedingTheFittedPanel(_GateCase):
    """A reviewer write that copies the fitted panel into the project file
    leaves the agy seat out, which that file could not hold anyway. An add
    copies nothing, so the fitted agy seat keeps running, warned."""

    def test_a_project_add_keeps_the_fitted_agy_seat(self):
        code, out, err = run_cli("reviewer", "add", "--scope", "project", "--provider", "mock", "--id", "m1")
        self.assertEqual(code, 0, err)
        written = config_mod.read_config_file(self.project_file())
        self.assertNotIn("reviewers", written)
        self.assertEqual([reviewer["id"] for reviewer in written["reviewers_extra"]], ["m1"])
        self.assertNotIn("now lists the reviewers", out)
        self.assertNotIn("not copied", out)
        loaded = config_mod.load(self.project)
        self.assertEqual([reviewer["id"] for reviewer in loaded.reviewers()], ["agy-general", "m1"])
        self.assertEqual(policy_mod.reviewer_raw_arg_refusals(loaded), {})
        self.assertEqual(list(policy_mod.reviewer_enforcement_warnings(loaded.data)), ["agy-general"])
        self.assertIn("warning: reviewer agy-general: %s" % UNENFORCED, err)
        self.assertNotIn("comes from the project config", err)

    def test_config_set_on_the_project_extras_edits_its_own_reviewer(self):
        code, _, err = run_cli("reviewer", "add", "--scope", "project", "--provider", "mock", "--id", "m1")
        self.assertEqual(code, 0, err)
        code, _, err = run_cli("config", "set", "--scope", "project", "reviewers_extra[0].role", "security")
        self.assertEqual(code, 0, err)
        written = config_mod.read_config_file(self.project_file())
        self.assertEqual([(r["id"], r["role"]) for r in written["reviewers_extra"]], [("m1", "security")])

    def test_an_edit_that_names_a_seat_left_out_says_why(self):
        left_out = (
            "note: not copied into .dev-orchestra.yaml: agy-general -- a reviewer on agy is taken only "
            "from the global config"
        )
        commands = (
            ("config", "set", "--scope", "project", "reviewers[0].role", "security"),
            ("reviewer", "set", "--scope", "project", "agy-general", "--role", "security"),
            ("reviewer", "remove", "--scope", "project", "agy-general"),
        )
        for command in commands:
            with self.subTest(command=command[:2]):
                code, _, err = run_cli(*command)
                self.assertEqual(code, 2, err)
                self.assertIn(left_out, err)
                self.assertFalse(os.path.exists(self.project_file()))

    def test_a_global_panel_seeded_from_the_fit_keeps_the_agy_seat(self):
        code, out, err = run_cli("reviewer", "set", "--scope", "global", "agy-general", "--role", "security")
        self.assertEqual(code, 0, err)
        written = config_mod.read_config_file(config_mod.global_config_path())
        self.assertEqual([reviewer["id"] for reviewer in written["reviewers"]], ["agy-general"])
        self.assertIn("(recorded agy-general default)", out)
        self.assertNotIn("not copied", out)
        self.assertIn("warning: reviewer agy-general: %s" % UNENFORCED, err)

    def test_a_global_add_keeps_the_fitted_agy_seat(self):
        code, out, err = run_cli("reviewer", "add", "--scope", "global", "--provider", "mock", "--id", "m1")
        self.assertEqual(code, 0, err)
        written = config_mod.read_config_file(config_mod.global_config_path())
        self.assertEqual([reviewer["id"] for reviewer in written["reviewers_extra"]], ["m1"])
        self.assertNotIn("not copied", out)
        self.assertIn("warning: reviewer agy-general: %s" % UNENFORCED, err)


#: (id, role, when) of the panel each preset gets from agy alone.
AGY_PANELS = {
    "quality": [
        ("agy-general", "general", None),
        ("agy-security", "security", None),
        ("agy-architecture", "architecture", None),
    ],
    "standard": [("agy-general", "general", None)],
    "fast": [("agy-general", "general", None), ("agy-security", "security", "high-risk")],
}


class TestPresetsWithAgy(unittest.TestCase):
    def providers_of(self, fit):
        roles = {role: fit.values[role]["provider"] for role in config_mod.KNOWN_ROLES}
        return roles, {reviewer["provider"] for reviewer in fit.values["reviewers"]}

    def test_agy_alone_takes_every_role_and_seat(self):
        for name in presets.NAMES:
            with self.subTest(preset=name):
                fit = presets.expand(name, ["agy"])
                for role in config_mod.KNOWN_ROLES:
                    expected = {"provider": "agy", "model": {"family": "default", "version": "latest"}}
                    self.assertEqual(fit.values[role], expected)
                panel = [(r["id"], r["role"], r.get("when")) for r in fit.values["reviewers"]]
                self.assertEqual(panel, AGY_PANELS[name])
                self.assertEqual({r["provider"] for r in fit.values["reviewers"]}, {"agy"})
                self.assertEqual({r["model"]["family"] for r in fit.values["reviewers"]}, {"default"})

    def test_the_notes_name_what_is_missing_and_how_to_opt_out(self):
        for name in presets.NAMES:
            with self.subTest(preset=name):
                fit = presets.expand(name, ["agy"])
                for role in config_mod.KNOWN_ROLES:
                    note = "claude, codex not found on PATH: %s went to agy (default)" % role
                    if role in config_mod.READ_ONLY_ROLES:
                        note += ROLE_OPT_OUT % role
                    self.assertIn(note, fit.notes)
                for subject, word, opt_out in (
                    ("reviewers", "reviewer seat", SEAT_OPT_OUT),
                    ("review.design.reviewers", "design reviewer seat", DESIGN_SEAT_OPT_OUT),
                ):
                    seats = [n for n, s in zip(fit.notes, fit.subjects, strict=True) if s == subject]
                    self.assertTrue(seats)
                    for note in seats:
                        self.assertTrue(note.startswith("claude, codex not found on PATH: %s" % word), note)
                        if "was not added" in note:
                            self.assertNotIn(opt_out, note)
                        else:
                            self.assertTrue(note.endswith(opt_out), note)
                self.assertFalse([note for note in fit.notes if UNENFORCED in note])
                self.assertEqual(len(fit.notes), len(fit.subjects))
        self.assertIn(
            "claude, codex not found on PATH: reviewer seat 2 (general) was not added; it would repeat "
            "agy-general",
            presets.expand("standard", ["agy"]).notes,
        )

    def test_the_seats_agy_does_not_take_are_named(self):
        """No cheap model is named offline on agy, and a held seat is never put there."""
        skip = "claude, codex not found on PATH: reviewer seat %d (%s) was not added; "
        cheap = skip + "agy has no cheap model named offline"
        held = skip + "agy cannot be held to reading"
        expected = {
            "standard": [held % (3, "security"), cheap % (4, "test")],
            "quality": [held % (6, "test")],
            "fast": [],
        }
        for name, skipped in expected.items():
            with self.subTest(preset=name):
                self.assertEqual(self.skipped(presets.expand(name, ["agy"]), "reviewers"), skipped)

    def skipped(self, fit, subject):
        """The notes of the seats not added to the panel at ``subject``, repeats aside."""
        return [
            note
            for note, about in zip(fit.notes, fit.subjects, strict=True)
            if about == subject and "was not added" in note and "would repeat" not in note
        ]

    def test_the_design_seats_agy_does_not_take_are_named(self):
        skip = "claude, codex not found on PATH: design reviewer seat %d (%s) was not added; "
        cheap = skip + "agy has no cheap model named offline"
        held = skip + "agy cannot be held to reading"
        expected = {
            "standard": [held % (2, "security"), cheap % (3, "test")],
            "quality": [held % (3, "security"), cheap % (4, "test"), held % (5, "architecture")],
            "fast": [],
        }
        for name, skipped in expected.items():
            with self.subTest(preset=name):
                fit = presets.expand(name, ["agy"])
                self.assertEqual(self.skipped(fit, "review.design.reviewers"), skipped)

    def test_with_claude_there_agy_takes_nothing(self):
        for name in presets.NAMES:
            with self.subTest(preset=name):
                roles, panel = self.providers_of(presets.expand(name, ["agy", "claude"]))
                self.assertEqual(set(roles.values()), {"claude"})
                self.assertEqual(panel, {"claude"})


if __name__ == "__main__":
    unittest.main()
