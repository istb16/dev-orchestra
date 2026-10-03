"""`reviewers_extra`: reviewers a file adds beside the panel it inherits.

The panel keeps following the preset's fit or the global file's list, and the
additions stay. Every refusal a project `reviewers` list meets applies to a
project extra too.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git
from test_wizard import ScriptedPrompter, accept_all

from orchestrator import cli, cli_common, doctor, execution, presets
from orchestrator import config as config_mod
from orchestrator import config_policy as policy_mod
from orchestrator import optimization as opt_mod
from orchestrator import review as review_mod
from orchestrator import wizard as wizard_mod

FITTED = ["claude-general", "claude-general-2", "claude-security", "claude-test", "claude-security-2"]
FIT_LABELS = ["fit"] * len(FITTED)
BOTH_FITTED = ["claude-general", "codex-general", "claude-security", "claude-test", "claude-security-2"]


def run_cli(*argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def mock(reviewer_id, role="general", **fields):
    reviewer = config_mod.make_reviewer(reviewer_id, "mock", "small", role)
    reviewer.update(fields)
    return reviewer


def agy(reviewer_id):
    return config_mod.make_reviewer(reviewer_id, "agy", "default")


#: What the wizard tests' files hold beside everything else.
EXTRAS = [mock("x")]


class ExtrasCase(IsolatedCase):
    """Claude alone installed: the fit is claude-general opus, claude-general-2 sonnet,
    claude-security and claude-test on sonnet, and claude-security-2 opus on high-risk changes."""

    def setUp(self):
        super().setUp()
        self.fake_clis(claude=True)

    def project_file(self):
        return os.path.join(self.project, ".dev-orchestra.yaml")

    def write_global(self, data):
        config_mod.write_config_file(config_mod.global_config_path(), dict({"version": 1}, **data), "global")

    def write_project(self, data):
        config_mod.write_config_file(self.project_file(), dict({"version": 1}, **data), "project")

    def global_layer(self):
        return config_mod.read_config_file(config_mod.global_config_path())

    def project_layer(self):
        return config_mod.read_config_file(self.project_file())

    def project_bytes(self):
        with open(self.project_file(), "rb") as handle:
            return handle.read()

    def loaded(self):
        return config_mod.load(self.project, validate_result=False)

    def ids(self, reviewers=None):
        if reviewers is None:
            reviewers = self.loaded().reviewers()
        return [reviewer["id"] for reviewer in reviewers]

    def labels(self):
        return [origin.label() for origin in self.loaded().reviewer_origins]


class TestLayering(ExtrasCase):
    def test_the_fit_then_the_global_extras_then_the_project_extras(self):
        self.write_global({"reviewers_extra": [mock("g1")]})
        self.write_project({"reviewers_extra": [mock("p1")]})
        self.assertEqual(self.ids(), [*FITTED, "g1", "p1"])
        self.assertEqual(self.labels(), [*FIT_LABELS, "global extra", "project extra"])
        self.assertEqual(config_mod.load(self.project).reviewers()[len(FITTED)], mock("g1"))

    def test_a_global_list_keeps_both_files_extras(self):
        self.write_global({"reviewers": [mock("base")], "reviewers_extra": [mock("g1")]})
        self.write_project({"reviewers_extra": [mock("p1")]})
        self.assertEqual(self.ids(), ["base", "g1", "p1"])
        self.assertEqual(self.labels(), ["global", "global extra", "project extra"])

    def test_a_project_list_replaces_the_base_and_the_global_extras(self):
        self.write_global({"reviewers_extra": [mock("g1")]})
        self.write_project({"reviewers": [mock("own")], "reviewers_extra": [mock("p1")]})
        self.assertEqual(self.ids(), ["own", "p1"])
        self.assertEqual(self.labels(), ["project", "project extra"])

    def test_an_empty_project_list_keeps_the_projects_extras(self):
        self.write_project({"reviewers": [], "reviewers_extra": [mock("p1")]})
        self.assertEqual(self.ids(), ["p1"])

    def test_null_and_empty_extras_add_nothing(self):
        for extras in (None, []):
            with self.subTest(extras=extras):
                self.write_global({"reviewers_extra": extras})
                loaded = config_mod.load(self.project)
                self.assertEqual(self.ids(loaded.reviewers()), FITTED)
                self.assertNotIn("reviewers_extra", loaded.data)

    def test_after_an_add_the_base_follows_what_is_installed_and_the_extra_stays(self):
        code, _, err = run_cli("reviewer", "add", "--provider", "mock", "--id", "m1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids(), [*FITTED, "m1"])
        self.fake_clis(claude=True, codex=True)
        self.assertEqual(self.ids(), [*BOTH_FITTED, "m1"])

    def test_with_no_extras_the_defaults_compose_unchanged(self):
        self.assertEqual(config_mod.compose({}, {}, ["claude", "codex"])[0], config_mod.default_config())


class TestCollisions(ExtrasCase):
    def test_an_extra_with_a_fitted_id_is_renamed_and_the_fitted_seat_keeps_it(self):
        self.write_global({"reviewers_extra": [mock("claude-general")]})
        loaded = config_mod.load(self.project)
        self.assertEqual(self.ids(loaded.reviewers()), [*FITTED, "mock-general"])
        self.assertIn(
            "reviewers_extra[0] in the global file: id claude-general is taken by the fitted panel; it runs "
            "as mock-general (reviewer set mock-general --id <name> keeps a name)",
            loaded.preset_notes,
        )
        # What `--only claude-general` selects: the fitted seat, and only it.
        named = [reviewer for reviewer in loaded.reviewers() if reviewer["id"] == "claude-general"]
        self.assertEqual([reviewer["provider"] for reviewer in named], ["claude"])

    def test_a_project_extra_colliding_with_a_global_extra_is_renamed(self):
        self.write_global({"reviewers_extra": [mock("dup")]})
        self.write_project({"reviewers_extra": [mock("dup", role="security")]})
        self.assertEqual(self.ids(), [*FITTED, "dup", "mock-security"])
        self.assertIn(
            "reviewers_extra[0] in the project file: id dup is taken by reviewers_extra[0] in the global "
            "file; it runs as mock-security (reviewer set mock-security --id <name> keeps a name)",
            self.loaded().preset_notes,
        )

    def test_a_new_name_never_takes_an_id_a_later_extra_wrote(self):
        self.write_global({"reviewers_extra": [mock("claude-general"), mock("mock-general")]})
        loaded = self.loaded()
        self.assertEqual(self.ids(loaded.reviewers()), [*FITTED, "mock-general-2", "mock-general"])
        renamed = [note for note in loaded.preset_notes if "is taken by" in note]
        self.assertEqual(len(renamed), 1, loaded.preset_notes)

    def test_two_extras_alike_but_for_their_ids_both_run(self):
        self.write_global({"reviewers_extra": [mock("a"), mock("b")]})
        self.write_project({"reviewers_extra": [mock("c")]})
        self.assertEqual(self.ids(), [*FITTED, "a", "b", "c"])

    def test_a_project_extra_never_takes_a_base_seat(self):
        self.write_project({"reviewers_extra": [mock("claude-general"), mock("claude-general-2")]})
        reviewers = config_mod.load(self.project).reviewers()
        self.assertEqual(reviewers[: len(FITTED)], presets.expand("standard", ["claude"]).values["reviewers"])
        self.assertEqual(self.ids(reviewers), [*FITTED, "mock-general", "mock-general-2"])

    def test_a_base_seeded_beside_an_extra_with_the_same_id_stays_valid(self):
        code, _, err = run_cli("reviewer", "add", "--provider", "codex", "--id", "codex-general")
        self.assertEqual(code, 0, err)
        self.fake_clis(claude=True, codex=True)
        self.assertEqual(self.ids(), [*BOTH_FITTED, "codex-general-2"])
        code, _, err = run_cli("reviewer", "remove", "--scope", "global", "claude-general")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids(self.global_layer()["reviewers"]), BOTH_FITTED[1:])
        code, _, err = run_cli("config", "set", "reviewers[0].role", "security")
        self.assertEqual(code, 0, err)
        code, out, _ = run_cli("config", "validate")
        self.assertEqual(code, 0, out)
        reviewers = self.loaded().reviewers()
        self.assertEqual(self.ids(reviewers), [*BOTH_FITTED[1:], "codex-general-2"])
        self.assertEqual(
            [reviewer["role"] for reviewer in reviewers],
            ["security", "security", "test", "security", "general"],
        )


class TestAnIdANewSeatTook(ExtrasCase):
    """An extra written as claude-security before the fit held a seat by that id."""

    REFUSAL = (
        "reviewer claude-security: reviewers_extra[0] in the %s file runs as claude-security-3, since "
        "claude-security is taken; use claude-security-3 (select the other seat by its position in "
        "reviewer list)"
    )

    def extra(self):
        return config_mod.make_reviewer("claude-security", "claude", "opus", "security")

    def test_it_runs_renamed_and_the_fitted_seat_keeps_the_id(self):
        self.write_project({"reviewers_extra": [self.extra()]})
        reviewers = self.loaded().reviewers()
        self.assertEqual(self.ids(reviewers), [*FITTED, "claude-security-3"])
        self.assertEqual(reviewers[FITTED.index("claude-security")]["model"]["family"], "sonnet")

    def test_editing_it_by_its_old_id_is_refused_and_writes_nothing(self):
        paths = {"project": self.project_file(), "global": config_mod.global_config_path()}
        for scope in ("project", "global"):
            with self.subTest(scope=scope):
                if os.path.exists(paths["project"]):
                    os.remove(paths["project"])
                writer = self.write_project if scope == "project" else self.write_global
                writer({"reviewers_extra": [self.extra()]})
                with open(paths[scope], "rb") as handle:
                    before = handle.read()
                commands = (
                    ("reviewer", "remove", "--scope", scope, "claude-security"),
                    ("reviewer", "set", "--scope", scope, "claude-security", "--model", "sonnet"),
                )
                for command in commands:
                    code, _, err = run_cli(*command)
                    self.assertEqual(code, 2, err)
                    self.assertIn(self.REFUSAL % scope, err)
                    with open(paths[scope], "rb") as handle:
                        self.assertEqual(handle.read(), before)

    def test_it_is_removed_in_place_by_the_id_it_runs_under(self):
        self.write_project({"reviewers_extra": [self.extra()]})
        code, out, err = run_cli("reviewer", "remove", "--scope", "project", "claude-security-3")
        self.assertEqual(code, 0, err)
        self.assertNotIn("now lists the reviewers", out)
        self.assertEqual(self.project_layer(), {"version": 1, "reviewers_extra": []})
        self.assertEqual(self.ids(), FITTED)

    def test_a_reduced_round_still_keeps_the_fitted_general_seat(self):
        self.write_project({"reviewers_extra": [self.extra(), mock("x")]})
        self.assertEqual(self.ids(opt_mod.choose_reviewers(self.loaded().reviewers(), 1)), ["claude-general"])


class TestValidation(ExtrasCase):
    INVALID = (
        ("a scalar", "oops", "reviewers_extra in the global file: must be a list"),
        ("a non-mapping entry", ["oops"], "reviewers_extra[0] in the global file: must be a mapping"),
        (
            "a bad when",
            [mock("g1", when="sometimes")],
            "reviewers_extra[0] in the global file: when: must be one of",
        ),
    )
    PROJECTS = (
        ("project extras", {"reviewers_extra": [mock("p1")]}),
        ("a project list", {"reviewers": [mock("own")]}),
    )

    def test_an_invalid_global_extra_is_reported_once(self):
        for name, extras, expected in self.INVALID:
            for project_name, project in self.PROJECTS:
                with self.subTest(extras=name, project=project_name):
                    self.write_global({"reviewers_extra": extras})
                    self.write_project(project)
                    code, out, _ = run_cli("config", "validate", "--json")
                    self.assertEqual(code, 1)
                    problems = json.loads(out)["problems"]
                    self.assertEqual(len(problems), 1, problems)
                    self.assertTrue(problems[0].startswith(expected), problems)
                    with self.assertRaises(config_mod.ConfigError) as raised:
                        config_mod.load(self.project)
                    self.assertEqual(str(raised.exception).count("reviewers_extra"), 1)

    def test_duplicate_ids_in_one_files_extras_are_reported(self):
        self.write_project({"reviewers_extra": [mock("a"), mock("a")]})
        code, out, _ = run_cli("config", "validate", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(out)["problems"],
            ["reviewers_extra[1] in the project file: duplicate reviewer id 'a'"],
        )

    def test_a_panel_of_conditional_reviewers_is_refused_before_writing(self):
        self.write_global({"reviewers": []})
        code, _, err = run_cli(
            "reviewer", "add", "--scope", "project", "--provider", "mock", "--when-paths", "db/*"
        )
        self.assertEqual(code, 2)
        self.assertIn("reviewers: at least one reviewer must run always", err)
        self.assertFalse(os.path.exists(self.project_file()))


class TestPreWriteCheck(ExtrasCase):
    """Every panel writer refuses, before writing, a panel `load()` would reject."""

    def setUp(self):
        super().setUp()
        self.write_global({"reviewers": []})
        self.write_project({"reviewers_extra": [mock("p1"), mock("p2", when="high-risk")]})
        self.before = self.project_bytes()

    def test_each_writer_refuses_the_last_always_running_extra(self):
        commands = (
            ("config", "set", "--scope", "project", "reviewers_extra[0].when", "high-risk"),
            ("reviewer", "set", "--scope", "project", "p1", "--when-paths", "*.sql"),
            ("reviewer", "remove", "--scope", "project", "p1"),
        )
        for command in commands:
            with self.subTest(command=command[:2]):
                code, _, err = run_cli(*command)
                self.assertEqual(code, 2, err)
                self.assertIn("reviewers: at least one reviewer must run always", err)
                self.assertEqual(self.project_bytes(), self.before)

    def test_a_write_to_a_broken_file_that_adds_no_problem_succeeds(self):
        self.write_project({"reviewers_extra": [mock("p1"), mock("bad", role="")]})
        code, _, err = run_cli("reviewer", "add", "--scope", "project", "--provider", "mock", "--id", "m2")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids(self.project_layer()["reviewers_extra"]), ["p1", "bad", "m2"])

    def test_the_same_mistake_on_another_extra_is_refused(self):
        self.write_project({"reviewers_extra": [mock("p1"), mock("bad", role=""), mock("p3")]})
        before = self.project_bytes()
        code, _, err = run_cli("config", "set", "--scope", "project", "reviewers_extra[2].role", "")
        self.assertEqual(code, 2, err)
        self.assertIn("reviewers_extra[2] in the project file: role must be a non-empty string", err)
        self.assertEqual(self.project_bytes(), before)

    def test_a_high_risk_reviewer_without_patterns_is_refused(self):
        self.write_global({"reviewers": [], "optimization": {"high_risk_paths": []}})
        self.write_project({"reviewers_extra": [mock("p1"), mock("p2")]})
        before = self.project_bytes()
        commands = (
            ("config", "set", "--scope", "project", "reviewers_extra[1].when", "high-risk"),
            ("reviewer", "set", "--scope", "project", "p2", "--when", "high-risk"),
            ("reviewer", "add", "--scope", "project", "--provider", "mock", "--when", "high-risk"),
        )
        for command in commands:
            with self.subTest(command=command[:2]):
                code, _, err = run_cli(*command)
                self.assertEqual(code, 2, err)
                self.assertIn("optimization.high_risk_paths: no pattern in force", err)
                self.assertEqual(self.project_bytes(), before)

    def test_a_whole_list_is_checked_too(self):
        self.write_global({})
        self.write_project({"reviewers_extra": [mock("p2", when="high-risk")]})
        before = self.project_bytes()
        code, _, err = run_cli("config", "set", "--scope", "project", "reviewers", "[]")
        self.assertEqual(code, 2, err)
        self.assertIn("reviewers: at least one reviewer must run always", err)
        self.assertEqual(self.project_bytes(), before)


class TestProjectRefusals(ExtrasCase):
    """A project extra is held to what a project `reviewers` list is."""

    def setUp(self):
        super().setUp()
        self.fake_clis(claude=True, agy=True)
        self.addCleanup(setattr, execution, "execute", execution.execute)

    def test_a_project_extra_on_agy_is_refused(self):
        self.write_project({"reviewers_extra": [agy("gem")]})
        loaded = self.loaded()
        refusals = policy_mod.project_raw_arg_refusals(loaded)
        position = "reviewers[%d]" % len(FITTED)
        self.assertEqual(list(refusals), [position])
        self.assertIn("reviewers on agy are taken only from the global config", refusals[position])
        self.assertEqual(list(policy_mod.reviewer_raw_arg_refusals(loaded)), ["gem"])
        self.assertEqual(list(policy_mod.reviewer_provider_refusals(loaded)), ["gem"])

    def test_a_project_extra_with_raw_arguments_is_refused(self):
        self.write_project({"reviewers_extra": [mock("p1", options={"args": ["--add-dir", "x"]})]})
        refusals = policy_mod.reviewer_raw_arg_refusals(self.loaded())
        self.assertEqual(list(refusals), ["p1"])
        self.assertIn("options.args is set in the project config", refusals["p1"])

    def test_a_global_extra_on_agy_is_warned_and_runs(self):
        self.write_global({"reviewers_extra": [agy("gem")]})
        loaded = self.loaded()
        self.assertEqual(policy_mod.reviewer_raw_arg_refusals(loaded), {})
        self.assertEqual(policy_mod.reviewer_provider_refusals(loaded), {})
        self.assertEqual(list(policy_mod.reviewer_enforcement_warnings(loaded.data)), ["gem"])

    def test_the_writers_refuse_agy_in_the_project_file(self):
        self.write_project({"reviewers_extra": [mock("p1")]})
        before = self.project_bytes()
        commands = (
            ("reviewer", "add", "--scope", "project", "--provider", "agy"),
            ("config", "set", "--scope", "project", "reviewers_extra[0].provider", "agy"),
            ("reviewer", "set", "--scope", "project", "p1", "--provider", "agy"),
        )
        for command in commands:
            with self.subTest(command=command[:2]):
                code, _, err = run_cli(*command)
                self.assertEqual(code, 2, err)
                self.assertIn("reviewers on agy are taken only from the global config", err)
                self.assertEqual(self.project_bytes(), before)

    @unittest.skipUnless(has_git(), "git not available")
    def test_review_run_refuses_a_project_extra_on_agy(self):
        self.fake_clis(agy=True)
        self.init_git_repo()
        self.write("app.py", "a = 1\n")
        self.commit_all("init")
        self.write("app.py", "a = 2\n")
        self.write_global({"optimization": {"level": "quality"}})
        self.write_project({"reviewers_extra": [agy("gem")]})
        review_mod.create_snapshot(self.cli_workspace())
        printed = json.dumps({"conversation_id": "c", "status": "SUCCESS", "response": "NO_FINDINGS"}) + "\n"
        started = []

        def execute(command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
            started.append(list(command))
            return execution.ExecOutcome(0, printed, "", 0.1)

        execution.execute = execute
        _, out, err = run_cli("review", "run")
        self.assertIn("reviewers on agy are taken only from the global config", out + err)
        self.assertEqual(len(started), 1, "only the fitted agy-general runs")


class TestGlobalAgyExtraFromProjectScope(ExtrasCase):
    """A global extra on agy, under a global file that lists no reviewers, is
    left out of a project copy as a fitted agy seat is."""

    def test_an_edit_naming_it_fails_and_says_why(self):
        self.write_global({"reviewers_extra": [agy("agy-x")]})
        left_out = (
            "note: not copied into .dev-orchestra.yaml: agy-x -- a reviewer on agy is taken only from the "
            "global config"
        )
        commands = (
            ("reviewer", "remove", "--scope", "project", "agy-x"),
            ("reviewer", "set", "--scope", "project", "agy-x", "--role", "security"),
        )
        for command in commands:
            with self.subTest(command=command[:2]):
                code, _, err = run_cli(*command)
                self.assertEqual(code, 2, err)
                self.assertIn(left_out, err)
                self.assertFalse(os.path.exists(self.project_file()))


class TestReducedRound(ExtrasCase):
    def test_the_base_general_seat_is_kept(self):
        self.write_global({"reviewers": [mock("gen"), mock("sec", role="security")]})
        self.write_project({"reviewers_extra": [mock("extra")]})
        self.assertEqual(self.ids(opt_mod.choose_reviewers(self.loaded().reviewers(), 1)), ["gen"])

    def test_without_a_general_base_seat_the_first_general_extra_is_kept(self):
        self.write_global({"reviewers": [mock("sec", role="security")]})
        self.write_project({"reviewers_extra": [mock("x"), mock("y")]})
        self.assertEqual(self.ids(opt_mod.choose_reviewers(self.loaded().reviewers(), 1)), ["x"])


class TestWriters(ExtrasCase):
    def test_add_writes_an_extra_and_freezes_nothing(self):
        code, out, err = run_cli("reviewer", "add", "--provider", "mock", "--id", "m1")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(self.global_layer()), ["reviewers_extra", "version"])
        self.assertIn("as an extra; the panel still follows preset standard's fit", out)
        self.assertNotIn("now lists the reviewers", out)

    def test_add_in_a_project_says_it_follows_the_global_list(self):
        self.write_global({"reviewers": [mock("base")]})
        code, out, err = run_cli("reviewer", "add", "--scope", "project", "--provider", "mock", "--id", "m1")
        self.assertEqual(code, 0, err)
        self.assertIn("as an extra; the panel still follows the global file's reviewers", out)
        self.assertEqual(self.ids(), ["base", "m1"])

    def test_add_still_appends_to_a_listed_panel(self):
        self.write_global({"reviewers": [mock("own")]})
        code, out, err = run_cli("reviewer", "add", "--provider", "mock", "--id", "m1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids(self.global_layer()["reviewers"]), ["own", "m1"])
        self.assertNotIn("reviewers_extra", self.global_layer())
        self.assertNotIn("as an extra", out)

    def test_an_explicit_id_of_the_fit_is_refused(self):
        code, _, err = run_cli("reviewer", "add", "--provider", "mock", "--id", "claude-general")
        self.assertEqual(code, 2)
        self.assertIn("reviewer id 'claude-general' already exists", err)
        self.assertFalse(os.path.exists(config_mod.global_config_path()))

    def test_renaming_an_extra_to_a_taken_id_is_refused(self):
        self.write_global({"reviewers_extra": [mock("m1"), mock("m2")]})
        before = self.global_layer()
        commands = (
            ("reviewer", "set", "m1", "--id", "claude-general"),
            ("reviewer", "set", "m1", "--id", "m2"),
            ("config", "set", "reviewers_extra[0].id", "claude-general"),
            ("config", "set", "reviewers_extra[0].id", "m2"),
        )
        for command in commands:
            with self.subTest(command=command):
                code, _, err = run_cli(*command)
                self.assertEqual(code, 2, err)
                self.assertIn("reviewer id %r already exists" % command[-1], err)
                self.assertEqual(self.global_layer(), before)

    def test_the_files_own_extra_is_edited_in_place(self):
        self.write_global({"reviewers_extra": [mock("m1"), mock("m2")]})
        code, _, err = run_cli("reviewer", "set", "m1", "--role", "security")
        self.assertEqual(code, 0, err)
        code, out, err = run_cli("reviewer", "remove", "m2")
        self.assertEqual(code, 0, err)
        self.assertNotIn("now lists the reviewers", out)
        layer = self.global_layer()
        self.assertNotIn("reviewers", layer)
        self.assertEqual(layer["reviewers_extra"], [mock("m1", role="security")])

    def test_a_renamed_extra_is_found_by_the_id_it_runs_under(self):
        self.write_global({"reviewers_extra": [mock("claude-general"), mock("m2")]})
        code, _, err = run_cli("reviewer", "set", "mock-general", "--id", "mine")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids(self.global_layer()["reviewers_extra"]), ["mine", "m2"])
        self.assertEqual(self.ids(), [*FITTED, "mine", "m2"])
        code, _, err = run_cli("reviewer", "remove", "mine")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids(self.global_layer()["reviewers_extra"]), ["m2"])

    def test_a_global_extra_edited_from_a_project_is_seeded(self):
        self.write_global({"reviewers_extra": [mock("g1")]})
        code, out, err = run_cli("reviewer", "set", "--scope", "project", "g1", "--role", "security")
        self.assertEqual(code, 0, err)
        self.assertIn("now lists the reviewers", out)
        reviewers = self.project_layer()["reviewers"]
        self.assertEqual(self.ids(reviewers), [*FITTED, "g1"])
        self.assertEqual(reviewers[len(FITTED)]["role"], "security")
        self.assertNotIn("reviewers_extra", self.project_layer())
        os.remove(self.project_file())
        code, _, err = run_cli("reviewer", "remove", "--scope", "project", "g1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids(self.project_layer()["reviewers"]), FITTED)

    def test_a_fitted_seat_is_seeded_with_the_note_and_the_extras_stay(self):
        self.write_global({"reviewers_extra": [mock("m1")]})
        code, out, err = run_cli("reviewer", "remove", "claude-general-2")
        self.assertEqual(code, 0, err)
        self.assertIn("now lists the reviewers; the panel no longer follows preset standard's fit", out)
        layer = self.global_layer()
        kept = [reviewer_id for reviewer_id in FITTED if reviewer_id != "claude-general-2"]
        self.assertEqual(self.ids(layer["reviewers"]), kept)
        self.assertEqual(self.ids(layer["reviewers_extra"]), ["m1"])
        self.assertEqual(self.ids(), [*kept, "m1"])

    def test_positions_are_the_lists(self):
        self.write_global({"reviewers_extra": [mock("m1")]})
        _, listing, _ = run_cli("reviewer", "list")
        line = listing.splitlines()[len(FITTED)]
        self.assertTrue(line.startswith("%d. m1 " % (len(FITTED) + 1)), listing)
        self.assertTrue(line.endswith(" (extra: global)"), listing)
        code, _, err = run_cli("reviewer", "remove", str(len(FITTED) + 1))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.global_layer()["reviewers_extra"], [])
        self.assertNotIn("reviewers", self.global_layer())

    def test_list_json_gives_each_entrys_origin(self):
        self.write_global({"reviewers_extra": [mock("m1")]})
        _, listing, _ = run_cli("reviewer", "list", "--json")
        self.assertEqual([entry["origin"] for entry in json.loads(listing)], [*FIT_LABELS, "global extra"])


class TestTheWizard(ExtrasCase):
    """It never asks about the key, never writes it, and keeps it as it is."""

    def run_wizard(self, answers, existing, scope="global"):
        prompter = ScriptedPrompter(answers)
        base = cli_common._fitted_base(scope, existing)
        data, save = wizard_mod.run(prompter, existing, base, scope=scope)
        return data, save, "\n".join(prompter.output)

    def test_a_preset_drops_reviewers_and_keeps_the_extras(self):
        existing = {"version": 1, "reviewers": [mock("own")], "reviewers_extra": EXTRAS}
        for answers in (["", ""], ["", "n", *accept_all(customise=False)]):
            with self.subTest(saved_as_is=len(answers) == 2):
                data, save, said = self.run_wizard(answers, existing)
                self.assertTrue(save)
                self.assertEqual(data, {"version": 1, "preset": "standard", "reviewers_extra": EXTRAS})
                self.assertIn("/ x (extra, global file)", said)

    def test_customise_keeps_the_extras_and_shows_them(self):
        data, _, said = self.run_wizard(accept_all(), {"version": 1, "reviewers_extra": EXTRAS})
        self.assertEqual(data["reviewers_extra"], EXTRAS)
        self.assertIn("reviewers_extra (x) is kept as it is; reviewer add/remove manage it.", said)
        summary = said[said.rindex("Configuration") :]
        self.assertIn("/ x (extra, global file)", summary)

    def test_a_project_setup_keeps_the_extras_and_shows_them(self):
        existing = {"version": 1, "reviewers_extra": EXTRAS}
        data, _, said = self.run_wizard(accept_all(customise=False), existing, scope="project")
        self.assertEqual(data["reviewers_extra"], EXTRAS)
        self.assertIn("This file's reviewers_extra (x) is kept as it is", said)
        self.assertIn("/ x (extra, project file)", said[said.rindex("Configuration") :])

    def test_it_never_writes_the_key_into_a_file_that_had_none(self):
        for answers in (accept_all(), ["", ""]):
            with self.subTest(answers=answers[:2]):
                data, _, said = self.run_wizard(answers, None)
                self.assertNotIn("reviewers_extra", data)
                self.assertNotIn("kept as it is", said)


class TestTheOtherCommands(ExtrasCase):
    def test_prune_keeps_the_extras(self):
        self.write_global({"preset": "standard", "reviewers_extra": [mock("m1")]})
        code, out, err = run_cli("config", "prune")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.global_layer()["reviewers_extra"], [mock("m1")])
        self.write_project({"reviewers_extra": [mock("p1")]})
        before = self.project_bytes()
        code, out, err = run_cli("config", "prune", "--scope", "project")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.project_bytes(), before)

    def test_reset_clears_the_extras_and_says_so(self):
        self.write_global({"reviewers_extra": [mock("m1"), mock("m2")]})
        code, out, _ = run_cli("config", "reset", "--scope", "global")
        self.assertEqual(code, 0)
        self.assertIn("removed 2 extra reviewer(s) (reviewers_extra)", out)
        self.assertEqual(self.global_layer(), {"version": 1})

    def test_show_marks_the_extras(self):
        self.write_global({"reviewers_extra": [mock("m1")]})
        _, out, _ = run_cli("config", "show")
        self.assertIn("/ m1 (extra, global file)", out)
        _, out, _ = run_cli("config", "show", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["reviewer_origins"], [*FIT_LABELS, "global extra"])
        self.assertEqual(len(payload["reviewer_origins"]), len(payload["config"]["reviewers"]))
        self.assertNotIn("reviewers_extra", payload["config"])
        _, out, _ = run_cli("config", "show", "--scope", "global", "--json")
        self.assertNotIn("reviewer_origins", json.loads(out))

    def test_doctor_gives_each_reviewers_origin(self):
        self.write_global({"reviewers_extra": [mock("m1")]})
        report = doctor.collect(self.project, probe_models=False)
        self.assertEqual([entry["origin"] for entry in report["reviewers"]], [*FIT_LABELS, "global extra"])
        self.assertIn("/ general (extra: global)", doctor.render(report))

    def test_doctor_notes_a_list_that_holds_the_inherited_panel_plus_more(self):
        fitted = presets.expand("standard", ["claude"]).values["reviewers"]
        note = (
            "reviewers in %s: holds the inherited panel plus m1; move m1 to reviewers_extra and remove "
            "reviewers to keep following it" % config_mod.global_config_path()
        )
        cases = (
            ("the fit plus one", [*fitted, mock("m1")], True),
            ("the fit alone", fitted, False),
            ("a seat of the fit missing", [fitted[0], mock("m1")], False),
        )
        for name, listed, noted in cases:
            with self.subTest(name):
                self.write_global({"reviewers": listed})
                notes = doctor.collect(self.project, probe_models=False)["notes"]
                self.assertEqual(note in notes, noted, notes)


if __name__ == "__main__":
    unittest.main()
