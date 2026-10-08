"""What a project file may do to the review and approval gates (#279).

The project file can come with the branch under review. Plan approval and a
workspace outside the repository -- where the approval is recorded -- are
taken from the global config alone; the other gates still take effect from
the project file, and `doctor`, `config validate` and `review run` say when
the project file makes one looser than the global config and the defaults.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from helpers import IsolatedCase, has_git, make_dir_link, remove_link

from orchestrator import cli, config_trust, doctor, reply_language
from orchestrator import config as config_mod
from orchestrator import config_policy as policy_mod


def run_cli(*argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class _Case(IsolatedCase):
    def setUp(self):
        super().setUp()
        # No CLI installed, so the preset's fit is the built-in defaults.
        self.fake_clis()

    def write_global(self, text):
        path = config_mod.global_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("version: 1\n" + text)

    def write_project(self, text):
        self.write(".dev-orchestra.yaml", "version: 1\n" + text)

    def loaded(self):
        return config_mod.load(self.project, validate_result=False)

    def notices(self):
        return policy_mod.project_loosening_notices(self.loaded())

    def ignored(self):
        return policy_mod.project_ignored_warnings(self.loaded())


class TestOutsideTheRepository(unittest.TestCase):
    def test_what_leaves_the_repository(self):
        for value in (
            "/srv/ai",
            "C:\\ai",
            "C:ai",
            "\\ai",
            "../ai",
            "..",
            "a/../../ai",
            "..\\ai",
            "a\\..\\..\\ai",
            "\\\\server\\share",
        ):
            with self.subTest(value=value):
                self.assertTrue(config_trust.outside_repository(value))

    def test_what_stays_inside(self):
        for value in (".ai", "work/.ai", "a/../ai", "./ai", "..ai", "", None, 3):
            with self.subTest(value=value):
                self.assertFalse(config_trust.outside_repository(value))

    def test_a_layer_with_nothing_to_drop_is_returned_as_it_is(self):
        layer = {"design": {"require_approval": None}, "workspace": {"dir": ".agent-work"}}
        self.assertIs(config_trust.without_ignored(layer), layer)

    def test_only_the_ignored_keys_are_dropped(self):
        layer = {
            "design": {"require_approval": False, "resume": {"max_age_seconds": 60}},
            "workspace": {"dir": "/elsewhere", "stale_notice_days": 7},
        }
        kept = config_trust.without_ignored(layer)
        self.assertEqual(
            kept,
            {"design": {"resume": {"max_age_seconds": 60}}, "workspace": {"stale_notice_days": 7}},
        )
        # The layer as read is left alone: it is what the warnings are made from.
        self.assertIs(layer["design"]["require_approval"], False)


class TestGlobalOnly(_Case):
    def test_a_project_file_cannot_turn_approval_off(self):
        self.write_project("design:\n  require_approval: false\n")
        loaded = self.loaded()
        self.assertIs(loaded.design_settings()["require_approval"], True)
        [line] = policy_mod.project_ignored_warnings(loaded)
        self.assertIn("design.require_approval: false is set in the project config", line)
        self.assertIn("so it stays required", line)
        self.assertIn("config set --scope global design.require_approval false", line)

    def test_the_global_file_still_can(self):
        self.write_global("design:\n  require_approval: false\n")
        self.write_project("review:\n  max_review_iterations: 2\n")
        loaded = self.loaded()
        self.assertIs(loaded.design_settings()["require_approval"], False)
        self.assertEqual(self.ignored(), [])

    def test_a_project_value_over_a_global_false_is_ignored_too(self):
        self.write_global("design:\n  require_approval: false\n")
        self.write_project("design:\n  require_approval: true\n")
        self.assertIs(self.loaded().design_settings()["require_approval"], False)
        self.assertIn("so it stays not required", self.ignored()[0])

    def test_a_project_null_is_the_default_and_not_reported(self):
        self.write_project("design:\n  require_approval: null\n")
        self.assertIs(self.loaded().design_settings()["require_approval"], True)
        self.assertEqual(self.ignored(), [])

    def test_a_project_workspace_outside_the_repository_is_ignored(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        for value in (elsewhere, "../elsewhere"):
            with self.subTest(value=value):
                self.write_project("workspace:\n  dir: %s\n" % json.dumps(value))
                loaded = self.loaded()
                self.assertEqual(loaded.workspace_dir(self.project), os.path.join(self.project, ".ai"))
                [line] = policy_mod.project_ignored_warnings(loaded)
                self.assertIn("workspace.dir: %s is set in the project config" % value, line)
                self.assertIn("so .ai is used", line)

    def test_a_project_workspace_inside_the_repository_is_kept(self):
        self.write_project("workspace:\n  dir: .agent-work\n")
        loaded = self.loaded()
        self.assertEqual(loaded.workspace_dir(self.project), os.path.join(self.project, ".agent-work"))
        self.assertEqual(policy_mod.project_ignored_warnings(loaded), [])

    def test_a_global_workspace_outside_the_repository_is_kept(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        self.write_global("workspace:\n  dir: %s\n" % json.dumps(elsewhere))
        self.write_project("workspace:\n  dir: %s\n" % json.dumps(os.path.join(self.tmp, "other")))
        self.assertEqual(self.loaded().workspace_dir(self.project), elsewhere)

    def link_outside(self, name):
        """``<project>/<name>``, a link to a directory outside the repository; that directory."""
        elsewhere = os.path.join(self.tmp, "elsewhere-" + name.replace("/", "-"))
        os.makedirs(elsewhere)
        link = os.path.join(self.project, *name.split("/"))
        os.makedirs(os.path.dirname(link), exist_ok=True)
        self.link(link, elsewhere)
        return elsewhere

    def link(self, link, target):
        try:
            make_dir_link(link, target)
        except (OSError, subprocess.CalledProcessError, NotImplementedError) as exc:
            self.skipTest("cannot make a link here: %s" % exc)
        # tearDown, which runs first, may already have removed it with the tree.
        self.addCleanup(lambda: os.path.lexists(link) and remove_link(link))

    def test_a_project_workspace_a_link_takes_outside_is_ignored(self):
        """`dir: inner` with `inner` a committed link out of the repository moves the record too."""
        self.init_git_repo()
        self.link_outside("inner")
        self.link_outside("deeper/link")
        for value in ("inner", "inner/ai", "deeper/link/ai"):
            with self.subTest(value=value):
                self.write_project("workspace:\n  dir: %s\n" % value)
                loaded = self.loaded()
                self.assertEqual(loaded.workspace_dir(self.project), os.path.join(self.project, ".ai"))
                [line] = policy_mod.project_ignored_warnings(loaded)
                self.assertIn("workspace.dir: %s is set in the project config" % value, line)
                self.assertIn("where a link takes it outside the repository", line)
                self.assertIn("so .ai is used", line)
                report = doctor.collect(self.project, probe_models=False)
                self.assertTrue(any("workspace.dir: %s" % value in line for line in report["problems"]))
                settings = reply_language.file_settings(self.project)
                self.assertEqual(config_mod.workspace_dir_of(settings), ".ai")

    def test_the_global_workspace_is_used_instead(self):
        self.init_git_repo()
        self.link_outside("inner")
        self.write_global("workspace:\n  dir: .global-work\n")
        self.write_project("workspace:\n  dir: inner\n")
        loaded = self.loaded()
        self.assertEqual(loaded.workspace_dir(self.project), os.path.join(self.project, ".global-work"))
        self.assertIn("so .global-work is used", self.ignored()[0])

    def test_a_link_the_global_config_or_the_default_goes_through_is_kept(self):
        """The user's own value is not checked: a `.ai` linked elsewhere keeps working."""
        self.init_git_repo()
        self.link_outside(".ai")
        self.link_outside("inner")
        self.assertEqual(self.loaded().workspace_dir(self.project), os.path.join(self.project, ".ai"))
        self.write_global("workspace:\n  dir: inner\n")
        loaded = self.loaded()
        self.assertEqual(loaded.workspace_dir(self.project), os.path.join(self.project, "inner"))
        self.assertEqual(self.ignored(), [])
        # The same value in the project file over it is the user's choice still.
        self.write_project("workspace:\n  dir: inner\n")
        self.assertEqual(self.loaded().workspace_dir(self.project), os.path.join(self.project, "inner"))
        self.assertEqual(self.ignored(), [])

    def test_a_link_inside_the_repository_is_kept(self):
        self.init_git_repo()
        os.makedirs(os.path.join(self.project, "real"))
        self.link(os.path.join(self.project, "inner"), os.path.join(self.project, "real"))
        self.write_project("workspace:\n  dir: inner\n")
        loaded = self.loaded()
        self.assertEqual(loaded.workspace_dir(self.project), os.path.join(self.project, "inner"))
        self.assertEqual(self.ignored(), [])

    def test_the_link_is_judged_where_the_commands_put_the_workspace(self):
        """Outside a git repository the workspace goes in the directory a command runs in, not
        beside the project file above it: the warning has to look for the link there too."""
        self.link_outside("sub/inner")
        self.write_project("workspace:\n  dir: inner\n")
        os.chdir(os.path.join(self.project, "sub"))
        loaded = config_mod.load(None, validate_result=False)
        self.assertEqual(loaded.root, os.getcwd())
        self.assertEqual(loaded.workspace_dir(), os.path.join(os.getcwd(), ".ai"))
        [line] = policy_mod.project_ignored_warnings(loaded)
        self.assertIn("workspace.dir: inner is set in the project config", line)
        self.assertIn("where a link takes it outside the repository", line)
        self.assertIn("so .ai is used", line)

    def test_linked_outside(self):
        self.link_outside("inner")
        self.assertTrue(config_trust.linked_outside(self.project, "inner"))
        self.assertTrue(config_trust.linked_outside(self.project, "inner/x/y"))
        self.assertFalse(config_trust.linked_outside(self.project, ".ai"))
        self.assertFalse(config_trust.linked_outside(self.project, "missing/x"))
        # A value whose text leaves is `outside_repository`'s, and not this one's.
        self.assertFalse(config_trust.linked_outside(self.project, "../x"))
        self.assertFalse(config_trust.linked_outside(self.project, 5))

    def test_the_hook_looks_for_the_workspace_the_commands_use(self):
        self.write_project("workspace:\n  dir: %s\n" % json.dumps(os.path.join(self.tmp, "elsewhere")))
        self.assertNotIn("dir", reply_language.file_settings(self.project).get("workspace", {}))

    def test_config_validate_and_doctor_report_it(self):
        self.write_project("design:\n  require_approval: false\n")
        code, out, _ = run_cli("config", "validate", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(any("design.require_approval" in line for line in json.loads(out)["warnings"]))
        report = doctor.collect(self.project, probe_models=False)
        self.assertTrue(any("design.require_approval" in line for line in report["problems"]))

    def test_doctor_notes_an_ignored_value_that_changes_nothing(self):
        """Only a value that would loosen what is in force fails `doctor --strict`."""
        cases = (
            ("", "design:\n  require_approval: true\n"),
            ("design:\n  require_approval: false\n", "design:\n  require_approval: false\n"),
            ("design:\n  require_approval: false\n", "design:\n  require_approval: true\n"),
        )
        for global_text, project_text in cases:
            with self.subTest(global_text=global_text, project_text=project_text):
                self.write_global(global_text)
                self.write_project(project_text)
                report = doctor.collect(self.project, probe_models=False)
                key = "design.require_approval"
                self.assertFalse(any(key in line for line in report["problems"]))
                self.assertFalse(any(key in line for line in report["config"]["warnings"]))
                self.assertTrue(any(key in line for line in report["notes"]))
                code, out, _ = run_cli("config", "validate", "--json")
                self.assertEqual(code, 0)
                self.assertTrue(any(key in line for line in json.loads(out)["warnings"]))

    def test_the_workspace_named_is_the_one_used(self):
        """A global `dir` no command can use is not named as the one in use (#279)."""
        self.write_global("workspace:\n  dir: 5\n")
        self.write_project("workspace:\n  dir: ../elsewhere\n")
        [line] = self.ignored()
        self.assertIn("so .ai is used", line)

    def test_config_set_writes_it_to_the_global_file(self):
        self.write_project("review:\n  max_review_iterations: 2\n")
        code, _, err = run_cli("config", "set", "design.require_approval", "false")
        self.assertEqual(code, 0, err)
        self.assertIs(self.loaded().design_settings()["require_approval"], False)
        project = config_mod.read_config_file(os.path.join(self.project, ".dev-orchestra.yaml"))
        self.assertNotIn("design", project)

    def test_config_set_refuses_the_project_scope(self):
        self.write_project("review:\n  max_review_iterations: 2\n")
        cases = (("design.require_approval", "false"), ("workspace.dir", "../elsewhere"))
        for key, value in cases:
            with self.subTest(key=key):
                code, _, err = run_cli("config", "set", "--scope", "project", key, value)
                self.assertEqual(code, 2)
                self.assertIn("taken only from the global config", err)
                self.assertIn("config set --scope global %s %s" % (key, value), err)
        path = os.path.join(self.project, ".dev-orchestra.yaml")
        unchanged = {"version": 1, "review": {"max_review_iterations": 2}}
        self.assertEqual(config_mod.read_config_file(path), unchanged)
        code, _, err = run_cli("config", "set", "--scope", "project", "workspace.dir", ".agent-work")
        self.assertEqual(code, 0, err)

    def test_config_set_refuses_a_whole_block_holding_one(self):
        """`design` or `workspace` written as a block is the same write as the key in it."""
        self.write_project("review:\n  max_review_iterations: 2\n")
        cases = (
            ("design", "require_approval: false\n", "design.require_approval false"),
            ("workspace", "dir: ../elsewhere\n", "workspace.dir ../elsewhere"),
        )
        for key, value, suggested in cases:
            with self.subTest(key=key):
                code, _, err = run_cli("config", "set", "--scope", "project", key, value)
                self.assertEqual(code, 2, err)
                self.assertIn("taken only from the global config", err)
                self.assertIn("config set --scope global %s" % suggested, err)
        path = os.path.join(self.project, ".dev-orchestra.yaml")
        unchanged = {"version": 1, "review": {"max_review_iterations": 2}}
        self.assertEqual(config_mod.read_config_file(path), unchanged)
        # Without a scope it goes to the global file, as the key alone does.
        code, _, err = run_cli("config", "set", "design", "require_approval: false\n")
        self.assertEqual(code, 0, err)
        self.assertIs(self.loaded().design_settings()["require_approval"], False)
        self.assertEqual(config_mod.read_config_file(path), unchanged)
        # A block that keeps inside the rules is written.
        code, _, err = run_cli("config", "set", "--scope", "project", "workspace", "dir: .agent-work\n")
        self.assertEqual(code, 0, err)


class TestLoosened(_Case):
    def assert_notice(self, key, *details):
        notices = self.notices()
        found = [line for line in notices if line.startswith(key + ": the project config")]
        self.assertEqual(len(found), 1, notices)
        for detail in details:
            self.assertIn(detail, found[0])
        self.assertIn("can come with the branch under review", found[0])

    def test_each_gate_reports_a_looser_project_value(self):
        cases = [
            ("reviewers: []\n", "reviewers", "drops claude-general"),
            (
                "reviewers:\n  - id: claude-general\n    provider: claude\n"
                "    model:\n      family: sonnet\n",
                "reviewers",
                "drops codex-general, claude-security, claude-test from the code review panel",
            ),
            (
                "review:\n  max_review_iterations: 0\n",
                "review.max_review_iterations",
                "lowers it to 0 from 2",
            ),
            (
                "review:\n  re_review_severities: [critical]\n",
                "review.re_review_severities",
                "over high findings",
            ),
            ('review:\n  exclude: ["src/*"]\n', "review.exclude", "also withholds the diff of src/*"),
            ("review:\n  max_findings: 1\n", "review.max_findings", "at most 1 findings, not 6"),
            ("review:\n  design:\n    enabled: false\n", "review.design.enabled", "turns it off from auto"),
            (
                "review:\n  design:\n    max_iterations: 1\n",
                "review.design.max_iterations",
                "lowers it to 1 from 2",
            ),
            ("optimization:\n  level: aggressive\n", "optimization.level", "to aggressive from balanced"),
            (
                "optimization:\n  high_risk_paths: []\n",
                "optimization.high_risk_paths",
                "drops the patterns *auth*",
            ),
            ("optimization:\n  low_risk_max_files: 50\n", "optimization.low_risk_max_files", "to 50 from 5"),
            (
                "optimization:\n  low_risk_max_lines: 900\n",
                "optimization.low_risk_max_lines",
                "to 900 from 150",
            ),
            ("optimization:\n  security_paths: []\n", "optimization.security_paths", "drops the patterns"),
            (
                "optimization:\n  architecture_paths: []\n",
                "optimization.architecture_paths",
                "drops the patterns",
            ),
        ]
        for text, key, detail in cases:
            with self.subTest(key=key, text=text):
                self.write_project(text)
                self.assert_notice(key, detail)

    def test_the_baseline_is_the_global_file(self):
        self.write_global("review:\n  max_review_iterations: 4\n  exclude: []\n")
        self.write_project("review:\n  max_review_iterations: 3\n  exclude: []\n")
        self.assert_notice("review.max_review_iterations", "lowers it to 3 from 4")
        self.assertEqual(len(self.notices()), 1)

    def test_a_global_skip_off_turned_on_by_the_project(self):
        self.write_global("optimization:\n  skip_unneeded_roles: false\n")
        self.write_project("optimization:\n  skip_unneeded_roles: true\n")
        self.assert_notice("optimization.skip_unneeded_roles", "turns it on")

    def test_a_global_extra_dropped_by_the_project(self):
        self.write_global('optimization:\n  extra_high_risk_paths: ["billing/*"]\n')
        self.write_project("optimization:\n  extra_high_risk_paths: []\n")
        self.assert_notice("optimization.high_risk_paths", "drops the patterns billing/*")

    def test_a_design_panel_shrunk_by_the_project(self):
        self.write_global(
            "review:\n  design:\n    reviewers:\n"
            "      - id: d1\n        provider: claude\n        model:\n          family: sonnet\n"
            "      - id: d2\n        provider: claude\n        model:\n          family: opus\n"
        )
        self.write_project(
            "review:\n  design:\n    reviewers:\n"
            "      - id: d1\n        provider: claude\n        model:\n          family: sonnet\n"
        )
        self.assert_notice("review.design.reviewers", "drops d2 from the design review panel")

    def test_stricter_or_equal_values_are_not_reported(self):
        self.write_project(
            "review:\n  max_review_iterations: 5\n  re_review_severities: [critical, high, medium]\n"
            "  exclude: []\n  max_findings: 0\n  design:\n    enabled: true\n    max_iterations: 2\n"
            "optimization:\n  level: quality\n  low_risk_max_files: 1\n"
            '  extra_high_risk_paths: ["billing/*"]\n  skip_unneeded_roles: false\n'
            "reviewers_extra:\n  - id: extra\n    provider: claude\n    model:\n      family: sonnet\n"
        )
        self.assertEqual(self.notices(), [])

    def test_a_findings_cap_left_to_the_level_or_equal_to_it_is_not_reported(self):
        """`max_findings: null` is the level's cap, which is the baseline's too."""
        for text in ("review:\n  max_findings: null\n", "review:\n  max_findings: 6\n"):
            with self.subTest(text=text):
                self.write_project(text)
                self.assertEqual(self.notices(), [])
        # Over a global cap a null keeps it, as any null but language.reply's does.
        self.write_global("review:\n  max_findings: 3\n")
        self.write_project("review:\n  max_findings: null\n")
        self.assertEqual(self.notices(), [])

    def test_design_review_turned_on_from_auto_is_not_reported(self):
        self.write_project("review:\n  design:\n    enabled: true\n")
        self.assertEqual(self.notices(), [])
        self.write_global("review:\n  design:\n    enabled: true\n")
        self.write_project("review:\n  design:\n    enabled: auto\n")
        self.assert_notice("review.design.enabled", "turns it auto from on")

    def test_a_code_panel_that_takes_out_the_fitted_design_panel(self):
        """Listing `reviewers` puts design rounds on a copy of them, so the fit's design seats go."""
        self.fake_clis(claude=True)
        self.write_project(
            "reviewers:\n  - id: claude-general\n    provider: claude\n    model:\n      family: sonnet\n"
        )
        self.assert_notice(
            "review.design.reviewers", "drops claude-security, claude-test from the design review panel"
        )
        self.assert_notice("reviewers", "from the code review panel")

    def test_a_baseline_that_does_not_compose_reports_nothing(self):
        self.write_project("review:\n  max_findings: 1\n")
        with mock.patch.object(policy_mod, "compose_loaded", side_effect=ValueError("broken")):
            self.assertEqual(self.notices(), [])

    def test_a_gate_no_run_could_read_is_skipped(self):
        """A global `review` that is not a mapping: the baseline cannot be read, and nothing crashes."""
        self.write_global("review: broken\n")
        self.write_project("review:\n  max_findings: 1\n")
        self.assertEqual(self.notices(), [])

    def test_an_empty_severity_list_reads_as_the_default(self):
        self.write_project("review:\n  re_review_severities: []\n")
        self.assertEqual(self.notices(), [])

    def test_severities_are_compared_as_review_status_reads_them(self):
        """Upper case and a bare name block as the default pair does: nothing is loosened."""
        for text in ("[CRITICAL, HIGH]", "[Critical, High]", "critical", "[crit]"):
            with self.subTest(text=text):
                self.write_project("review:\n  re_review_severities: %s\n" % text)
                self.assertEqual(self.notices(), [])
        self.write_project("review:\n  re_review_severities: [CRITICAL]\n")
        [line] = self.notices()
        self.assertIn("over high findings", line)

    def test_no_project_file_reports_nothing(self):
        self.write_global("review:\n  max_review_iterations: 0\n")
        self.assertEqual(self.notices(), [])

    def test_doctor_notes_them_without_failing(self):
        self.write_project("review:\n  max_review_iterations: 0\n")
        report = doctor.collect(self.project, probe_models=False)
        [line] = report["config"]["loosened"]
        self.assertIn(line, report["notes"])
        self.assertNotIn(line, report["problems"])

    def test_config_validate_warns_without_failing(self):
        self.write_project('review:\n  exclude: ["src/*"]\n')
        code, out, _ = run_cli("config", "validate")
        self.assertEqual(code, 0, out)
        self.assertIn("Warnings:", out)
        self.assertIn("review.exclude: the project config", out)

    def test_review_run_warns(self):
        self.write_project("review:\n  max_review_iterations: 0\n  re_review_severities: [critical]\n")
        _, _, err = run_cli("review", "run")
        self.assertIn("warning: review.max_review_iterations: the project config", err)
        self.assertIn("warning: review.re_review_severities: the project config", err)


class TestOnTheMock(_Case):
    """`doctor --strict` and `review run` where nothing else is wrong: every seat on the mock."""

    def setUp(self):
        super().setUp()
        role = {"provider": "mock", "model": {"family": "small", "version": "latest"}}
        data = config_mod.default_config()
        data.update(orchestrator=role, architect=role, implementer=role, review_fixer=role)
        data["reviewers"] = [config_mod.make_reviewer("mock-general", "mock", "small")]
        config_mod.write_config_file(config_mod.global_config_path(), data)

    def test_doctor_strict_fails_only_on_an_ignored_value_that_would_loosen(self):
        self.assertEqual(run_cli("doctor", "--fast", "--strict")[0], 0)
        cases = (
            ("review:\n  max_review_iterations: 0\n", 0),
            ("reveiw:\n  max_review_iterations: 3\n", 0),
            ("design:\n  require_approval: true\n", 0),
            ("design:\n  require_approval: false\n", 1),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.write_project(text)
                code, out, err = run_cli("doctor", "--fast", "--strict")
                self.assertEqual(code, expected, out + err)

    @unittest.skipUnless(has_git(), "git is required")
    def test_review_run_warns_before_the_round_and_the_values_still_apply(self):
        self.init_git_repo()
        self.write("app.py", "x = 1\n")
        self.commit_all("init")
        self.write("app.py", "x = 2\n")
        self.write_project("design:\n  require_approval: false\nreview:\n  max_review_iterations: 0\n")
        code, _, err = run_cli("review", "snapshot")
        self.assertEqual(code, 0, err)
        code, _, err = run_cli("review", "run")
        lines = err.splitlines()
        self.assertTrue(
            lines[0].startswith("warning: design.require_approval: false is set in the project config"), err
        )
        self.assertTrue(lines[1].startswith("warning: review.max_review_iterations: the project config"), err)
        # The loosened value applies: a budget of 0 rounds refuses the first.
        self.assertEqual(code, 3, err)
        self.assertIn("refusing to run review round 1", err)
        self.assertGreater(err.index("refusing to run"), err.index("warning: review.max_review_iterations"))
        # The ignored one does not: plan approval stays required.
        self.assertIs(self.loaded().design_settings()["require_approval"], True)


if __name__ == "__main__":
    unittest.main()
