"""The ``doctor`` command and user adapters as the commands see them."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from helpers import (
    CLAUDE_HELP_OLD,
    IsolatedCase,
    _Completed,
    pretend_claude_is_installed,
    run_cli,
)

from orchestrator import config as config_mod
from orchestrator import providers


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

    def test_doctor_flags_ignored_options_on_both_panels(self):
        """A design reviewer is read-only too, whatever its label starts with (#295)."""
        data = config_mod.default_config()
        code_seat = config_mod.make_reviewer("r1", "mock", "small")
        design_seat = config_mod.make_reviewer("d1", "mock", "small")
        for seat in (code_seat, design_seat):
            seat["options"] = {"sandbox": "danger-full-access"}
        data["reviewers"] = [code_seat]
        data.setdefault("review", {}).setdefault("design", {})["reviewers"] = [design_seat]
        config_mod.write_config_file(config_mod.global_config_path(), data)
        _, out, _ = run_cli("doctor", "--fast", "--json")
        problems = json.loads(out)["problems"]
        for label in ("Reviewer r1", "Design reviewer d1"):
            expected = "%s: options.sandbox ignored" % label
            self.assertTrue(any(p.startswith(expected) for p in problems), problems)

    def test_doctor_flags_ignored_options_on_a_read_only_tier(self):
        """A tier's own options replace the role's, and are dropped the same way."""
        data = config_mod.default_config()
        role = {"provider": "mock", "model": {"family": "small", "version": "latest"}}
        tiers = {"light": {"model": {"family": "small", "version": "latest"}, "options": {"sandbox": "x"}}}
        data["architect"] = dict(role, model_tiers=tiers)
        data["implementer"] = dict(role, model_tiers=tiers)
        config_mod.write_config_file(config_mod.global_config_path(), data)
        _, out, _ = run_cli("doctor", "--fast", "--json")
        problems = json.loads(out)["problems"]
        self.assertTrue(
            any(p.startswith("Architect (tier light): options.sandbox ignored") for p in problems), problems
        )
        self.assertFalse(any(p.startswith("Implementer (tier light)") for p in problems), problems)

    def test_doctor_flags_ignored_options_on_an_orchestrator_tier(self):
        data = config_mod.default_config()
        role = {"provider": "mock", "model": {"family": "small", "version": "latest"}}
        tiers = {"light": {"model": {"family": "small", "version": "latest"}, "options": {"sandbox": "x"}}}
        data["orchestrator"] = dict(role, model_tiers=tiers)
        config_mod.write_config_file(config_mod.global_config_path(), data)
        _, out, _ = run_cli("doctor", "--fast", "--json")
        problems = json.loads(out)["problems"]
        self.assertTrue(
            any(p.startswith("Orchestrator (tier light): options.sandbox ignored") for p in problems),
            problems,
        )

    def test_ignored_options_are_named_in_the_json_entry(self):
        """A design seat's options are reported once, its high-risk model
        included, and a write role's are not reported at all."""
        data = config_mod.default_config()
        design_seat = config_mod.make_reviewer("d1", "mock", "small")
        design_seat["options"] = {"sandbox": "danger-full-access"}
        design_seat["high_risk_model"] = {"family": "large"}
        data.setdefault("review", {}).setdefault("design", {})["reviewers"] = [design_seat]
        role = {"provider": "mock", "model": {"family": "small", "version": "latest"}}
        data["implementer"] = dict(role, options={"sandbox": "danger-full-access"})
        config_mod.write_config_file(config_mod.global_config_path(), data)
        _, out, _ = run_cli("doctor", "--fast", "--json")
        report = json.loads(out)
        problems = report["problems"]
        (entry,) = [seat for seat in report["design_reviewers"] if seat["id"] == "d1"]
        self.assertEqual(entry["ignored_options"], ["sandbox"])
        self.assertEqual(entry["high_risk_model"]["status"], "ok")
        ignored = [p for p in problems if "ignored -- planning and review stages" in p]
        self.assertEqual(len([p for p in ignored if p.startswith("Design reviewer d1")]), 1, problems)
        self.assertFalse(any(p.startswith("Implementer") for p in ignored), problems)
        self.assertNotIn("ignored_options", report["roles"]["implementer"])

    def test_doctor_reports_no_pinned_values(self):
        """The 0.24.0 removal: a file holding every default, as one from before
        0.6.0 does, has no "Pinned" section and no ``config.pinned``."""
        data = config_mod.default_config()
        data["review"]["max_review_iterations"] = 3
        config_mod.write_config_file(config_mod.global_config_path(), data)
        code, out, _ = run_cli("doctor", "--fast", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn("pinned", json.loads(out)["config"])
        _, text, _ = run_cli("doctor", "--fast")
        self.assertNotIn("Pinned", text)

    def test_strict_mode_exits_non_zero_on_problems(self):
        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "remove", "claude-security")
        run_cli("reviewer", "remove", "claude-test")
        code, _, _ = run_cli("doctor", "--fast", "--strict")
        self.assertEqual(code, 1)


class TestDoctorDefaultPatternsNote(IsolatedCase):
    """A high-risk reviewer judged by the built-in patterns alone is noted.

    Not a problem: the defaults can be exactly right, and `doctor --strict`
    must not fail over a configuration that is working.
    """

    def write_config(self, conditional=("sec",), scoped=(), **optimization):
        role = {"provider": "mock", "model": {"family": "small", "version": "latest"}}
        data = config_mod.default_config()
        data.update(orchestrator=role, architect=role, implementer=role, review_fixer=role)
        data["reviewers"] = [config_mod.make_reviewer("gen", "mock", "small")]
        for name in conditional:
            data["reviewers"].append(config_mod.make_reviewer(name, "mock", "small", when="high-risk"))
        for name, patterns in scoped:
            data["reviewers"].append(config_mod.make_reviewer(name, "mock", "small", paths=patterns))
        data["optimization"].update(optimization)
        config_mod.write_config_file(config_mod.global_config_path(), data)

    def notes(self):
        _, out, _ = run_cli("doctor", "--fast", "--json")
        return json.loads(out)["notes"]

    def test_a_high_risk_reviewer_on_the_defaults_is_noted(self):
        self.write_config()
        code, out, _ = run_cli("doctor", "--fast", "--strict")
        self.assertEqual(code, 0, out)
        self.assertIn("No problems found.", out)
        self.assertIn("\nNotes\n", out)
        self.assertIn("sec: when: high-risk", out)
        self.assertIn("optimization.extra_high_risk_paths", out)
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("sec: "))

    def test_two_high_risk_reviewers_share_one_note(self):
        self.write_config(conditional=("sec", "pay"))
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("sec, pay: "))

    def test_extra_patterns_mean_no_note(self):
        self.write_config(extra_high_risk_paths=["app/guards/*"])
        self.assertEqual(self.notes(), [])

    def test_a_replaced_list_means_no_note(self):
        self.write_config(high_risk_paths=["app/guards/*", "*auth*"])
        self.assertEqual(self.notes(), [])

    def test_the_defaults_written_out_in_any_order_are_still_the_defaults(self):
        defaults = config_mod.default_config()["optimization"]["high_risk_paths"]
        self.write_config(high_risk_paths=list(reversed(defaults)))
        self.assertEqual(len(self.notes()), 1)

    def test_a_narrowed_copy_of_the_defaults_is_a_choice(self):
        defaults = config_mod.default_config()["optimization"]["high_risk_paths"]
        self.write_config(high_risk_paths=[p for p in defaults if p not in ("k8s/*", "deploy/*")])
        self.assertEqual(self.notes(), [])

    def test_a_blank_entry_does_not_make_the_defaults_a_choice(self):
        defaults = config_mod.default_config()["optimization"]["high_risk_paths"]
        self.write_config(high_risk_paths=[*defaults, "", "  "])
        self.assertEqual(len(self.notes()), 1)

    def test_the_defaults_plus_one_pattern_are_a_choice(self):
        """A list that holds every default and one more is not the defaults:
        someone chose it, as ``default_patterns_only`` reads any other set."""
        defaults = config_mod.default_config()["optimization"]["high_risk_paths"]
        self.write_config(high_risk_paths=[*defaults, "app/guards/*"])
        self.assertEqual(self.notes(), [])

    def test_an_empty_list_is_not_noted(self):
        """``[]`` is a set other than the defaults, so this note says nothing;
        no pattern in force is reported as a problem instead."""
        self.write_config(high_risk_paths=[])
        _, out, _ = run_cli("doctor", "--fast", "--json")
        report = json.loads(out)
        self.assertEqual(report["notes"], [])
        self.assertTrue(
            any("optimization.high_risk_paths: no pattern in force" in p for p in report["problems"]),
            report["problems"],
        )

    def test_no_high_risk_reviewer_means_no_note(self):
        self.write_config(conditional=())
        self.assertEqual(self.notes(), [])
        _, out, _ = run_cli("doctor", "--fast")
        self.assertNotIn("Notes", out)

    def test_the_reviewer_line_shows_the_condition(self):
        self.write_config()
        _, out, _ = run_cli("doctor", "--fast")
        line = next(line for line in out.splitlines() if ". sec / " in line)
        self.assertTrue(line.endswith("general (when: high-risk)"), line)
        self.assertNotIn("when:", next(line for line in out.splitlines() if ". gen / " in line))
        _, payload, _ = run_cli("doctor", "--fast", "--json")
        entries = {entry["id"]: entry for entry in json.loads(payload)["reviewers"]}
        self.assertEqual(entries["sec"]["when"], "high-risk")
        self.assertEqual(entries["sec"]["condition"], "high-risk")
        self.assertNotIn("when", entries["gen"])

    def test_the_reviewer_line_shows_the_patterns(self):
        self.write_config(conditional=(), scoped=[("db", ["*migrate*/*", "*.sql"])])
        _, out, _ = run_cli("doctor", "--fast")
        line = next(line for line in out.splitlines() if ". db / " in line)
        self.assertTrue(line.endswith("general (when: paths *migrate*/*, *.sql)"), line)
        _, payload, _ = run_cli("doctor", "--fast", "--json")
        entry = next(entry for entry in json.loads(payload)["reviewers"] if entry["id"] == "db")
        self.assertEqual(entry["when"], "paths")
        self.assertEqual(entry["paths"], ["*migrate*/*", "*.sql"])
        self.assertEqual(entry["condition"], "paths *migrate*/*, *.sql")

    def test_a_path_scoped_panel_on_the_defaults_is_not_noted(self):
        """Its patterns were chosen; the note is about patterns nobody chose."""
        self.write_config(conditional=(), scoped=[("db", ["*.sql"])])
        self.assertEqual(self.notes(), [])
        self.write_config(conditional=("sec",), scoped=[("db", ["*.sql"])])
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("sec: "))

    def test_a_credential_shaped_pattern_is_redacted(self):
        secret = "k" * 16
        self.write_config(conditional=(), scoped=[("db", ["*secret=%s*" % secret])])
        _, out, _ = run_cli("doctor", "--fast")
        _, payload, _ = run_cli("doctor", "--fast", "--json")
        self.assertIn("(when: paths *secret=[redacted]*)", out)
        self.assertNotIn(secret, out + payload)


class TestDoctorLiveCheck(IsolatedCase):
    """Whether scripts/smoke_live.py has run the installed CLI version here.

    A note, never a problem: an unchecked version is when a drift can have
    happened, not proof that one did.
    """

    VERSION = "codex-cli 0.200.0"

    def setUp(self):
        super().setUp()
        self.setUp_config()
        from orchestrator.providers.claude import ClaudeProvider
        from orchestrator.providers.codex import CodexProvider

        for cls, name, value in (
            (ClaudeProvider, "which", lambda self: None),
            (CodexProvider, "which", lambda self: "codex"),
            (CodexProvider, "version", lambda self: (TestDoctorLiveCheck.VERSION, None)),
        ):
            self.addCleanup(setattr, cls, name, getattr(cls, name))
            setattr(cls, name, value)

    def setUp_config(self):
        role = {"provider": "mock", "model": {"family": "small", "version": "latest"}}
        data = config_mod.default_config()
        data.update(orchestrator=role, architect=role, implementer=role, review_fixer=role)
        data["reviewers"] = [config_mod.make_reviewer("gen", "mock", "small")]
        config_mod.write_config_file(config_mod.global_config_path(), data)

    def record(self, version, failed=(), skipped=(), at="2026-09-01T00:00:00Z"):
        from orchestrator import verified

        with mock.patch.object(verified.ws, "utcnow", return_value=at):
            verified.record_smoke("codex", version, list(failed), list(skipped), self.project)

    def doctor(self):
        code, out, _ = run_cli("doctor", "--fast", "--strict")
        report = json.loads(run_cli("doctor", "--fast", "--json")[1])
        # Codex alone is installed, so the preset notes that every role here
        # is set by the file; those are about the fit, not the live check.
        fit_notes = report["config"]["preset"]["notes"]
        report["notes"] = [note for note in report["notes"] if note not in fit_notes]
        return code, out, report

    def live_line(self, out):
        return [line.strip() for line in out.splitlines() if line.strip().startswith("Live check:")]

    def test_a_passing_record_is_a_line_and_no_note(self):
        self.record(self.VERSION, skipped=["x"])
        code, out, report = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertEqual(
            self.live_line(out), ["Live check: passed for %s on 2026-09-01, 1 skipped" % self.VERSION]
        )
        self.assertEqual(report["notes"], [])
        self.assertEqual(report["providers"]["codex"]["live_check"]["status"], "passed")

    def test_a_newer_version_is_a_line_and_one_note(self):
        self.record("codex-cli 0.150.0")
        code, out, report = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertEqual(
            self.live_line(out),
            ["Live check: not run for %s (last passed: codex-cli 0.150.0 on 2026-09-01)" % self.VERSION],
        )
        self.assertEqual(len(report["notes"]), 1)
        note = report["notes"][0]
        self.assertTrue(note.startswith("codex %s has not been live-checked" % self.VERSION), note)
        self.assertIn("(last passed: codex-cli 0.150.0)", note)
        self.assertIn("smoke_live.py --provider codex", note)
        from orchestrator import doctor

        self.assertTrue(os.path.isfile(doctor.SMOKE_SCRIPT))
        self.assertIn(doctor.SMOKE_SCRIPT, note)
        self.assertIn("\nNotes\n", out)
        self.assertEqual(report["providers"]["codex"]["live_check"]["status"], "absent")

    def test_a_failed_record_is_a_line_and_no_note(self):
        self.record(self.VERSION, failed=["reports what it spent", "stays read-only"])
        code, out, report = self.doctor()
        self.assertEqual(code, 0, out)
        expected = "FAILED for %s on 2026-09-01 (reports what it spent, stays read-only)" % self.VERSION
        self.assertEqual(self.live_line(out), ["Live check: " + expected])
        self.assertEqual(report["notes"], [])

    def test_no_record_is_never_run_and_a_note(self):
        code, out, report = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.live_line(out), ["Live check: never run on this machine"])
        self.assertEqual(len(report["notes"]), 1)
        self.assertIn("(last passed: never)", report["notes"][0])
        self.assertEqual(
            report["providers"]["codex"]["live_check"],
            {"status": "absent", "entry": None, "last_passed": None, "problem": None},
        )

    def test_the_mock_and_a_missing_cli_have_no_line(self):
        _, _, report = self.doctor()
        self.assertNotIn("live_check", report["providers"]["mock"])
        self.assertNotIn("live_check", report["providers"]["claude"])

    def test_an_unreadable_version_is_a_line_and_no_note(self):
        from orchestrator.providers.codex import CodexProvider

        self.addCleanup(setattr, CodexProvider, "version", CodexProvider.version)
        CodexProvider.version = lambda self: (None, "unexpected output")
        _, out, report = self.doctor()
        self.assertEqual(self.live_line(out), ["Live check: version unavailable"])
        self.assertEqual(report["notes"], [])
        self.assertEqual(report["providers"]["codex"]["live_check"], {"status": "version-unavailable"})

    def test_a_record_inside_the_workspace_is_a_line_and_no_note(self):
        """The script would refuse to write it, so no note asks for a run."""
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(self.project, ".ai", "home")
        # The configuration moves with the home: write it there too.
        self.setUp_config()
        _, out, report = self.doctor()
        expected = (
            "the live-check record is inside the workspace; point DEV_ORCHESTRA_HOME outside the checkout"
        )
        self.assertEqual(self.live_line(out), ["Live check: " + expected])
        self.assertEqual(report["notes"], [])

    def test_an_unknown_schema_names_the_live_check_record(self):
        from orchestrator import verified

        path = verified.smoke_record_path("codex")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"schema": 999}, handle)
        _, out, _ = self.doctor()
        self.assertEqual(self.live_line(out), ["Live check: the live-check record has an unknown schema"])


class TestDoctorReadOnlyEnforcement(IsolatedCase):
    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")

    def pretend_codex_is_installed(self):
        from orchestrator.providers.codex import CodexProvider

        for name, value in (
            ("which", lambda self: "codex"),
            ("version", lambda self: ("codex-cli 0.154.0", None)),
            ("configured_model", lambda self: None),
            ("_capture", lambda self, command, timeout=30: _Completed("", 1)),
        ):
            self.addCleanup(setattr, CodexProvider, name, getattr(CodexProvider, name))
            setattr(CodexProvider, name, value)

    def doctor(self, *argv):
        return json.loads(run_cli("doctor", "--json", *argv)[1])

    def test_fast_does_not_check(self):
        report = self.doctor("--fast")
        self.assertEqual(report["providers"]["claude"]["read_only_enforcement"]["status"], "not-checked")

    def test_verified_and_partial_are_reported_with_what_they_cover(self):
        pretend_claude_is_installed(self)
        self.pretend_codex_is_installed()
        report = self.doctor()
        claude = report["providers"]["claude"]["read_only_enforcement"]
        self.assertEqual(claude["status"], "verified")
        self.assertIn("--tools Read,Grep,Glob --strict-mcp-config --restricted", claude["mechanism"])
        self.assertEqual(report["providers"]["codex"]["read_only_enforcement"]["status"], "partial")
        _, out, _ = run_cli("doctor")
        self.assertIn("Read-only runs: enforced by --permission-mode plan", out)
        self.assertIn("Read-only runs: enforced by -s read-only (CLI sandbox); ", out)
        self.assertIn("MCP servers were not examined", out)
        self.assertFalse(any("read-only runs are refused" in p for p in report["problems"]))

    def test_an_old_cli_is_not_enforceable_and_a_problem(self):
        pretend_claude_is_installed(self, CLAUDE_HELP_OLD)
        report = self.doctor()
        self.assertEqual(report["providers"]["claude"]["read_only_enforcement"]["status"], "unsupported")
        refused = [p for p in report["problems"] if "read-only runs are refused" in p]
        self.assertTrue(any(p.startswith("Orchestrator:") for p in refused))
        self.assertTrue(any(p.startswith("Architect:") for p in refused))
        self.assertTrue(any(p.startswith("Reviewer claude-general:") for p in refused))
        _, out, _ = run_cli("doctor")
        self.assertIn("Read-only runs: NOT ENFORCEABLE -- ", out)
        self.assertEqual(run_cli("doctor", "--strict")[0], 1)

    def test_a_tier_is_checked_against_its_own_provider(self):
        pretend_claude_is_installed(self, CLAUDE_HELP_OLD)
        self.pretend_codex_is_installed()
        run_cli("config", "set", "architect.provider", "codex")
        run_cli("config", "set", "architect.model_tiers.light.provider", "claude")
        report = self.doctor()
        refused = [p for p in report["problems"] if "read-only runs are refused" in p]
        self.assertFalse(any(p.startswith("Architect:") for p in refused), refused)
        self.assertTrue(any(p.startswith("Architect (tier light):") for p in refused), refused)

    def test_a_tier_keeping_the_provider_is_not_reported_twice(self):
        pretend_claude_is_installed(self, CLAUDE_HELP_OLD)
        run_cli("config", "set", "architect.provider", "claude")
        run_cli("config", "set", "architect.model_tiers.light.model.family", "opus")
        report = self.doctor()
        refused = [p for p in report["problems"] if "read-only runs are refused" in p]
        self.assertTrue(any(p.startswith("Architect:") for p in refused), refused)
        self.assertFalse(any(p.startswith("Architect (tier light):") for p in refused), refused)

    def test_unreadable_help_is_unverified(self):
        pretend_claude_is_installed(self, None)
        _, out, _ = run_cli("doctor")
        self.assertIn("Read-only runs: UNVERIFIED -- could not read 'claude --help'", out)

    def test_the_orchestrator_counts_as_read_only_for_ignored_options(self):
        run_cli("config", "set", "orchestrator.options.permission_mode", "bypassPermissions")
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("Orchestrator: options.permission_mode ignored", out)


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

    def resolve_latest(self, family):
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
            setattr(cls, "which", lambda self: None)

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

    def test_an_adapter_still_defining_the_old_resolve_latest_is_a_problem(self):
        """0.24.0 renamed ``_resolve_latest`` with no alias: such an adapter
        still loads, and doctor says what to rename instead of "No problems"."""
        source = FLAKY_ADAPTER.replace("def resolve_latest(", "def _resolve_latest(") % "none"
        path = self.write_user_provider("flaky", source)
        report = self.load_user_providers()
        self.assertEqual(report["errors"], [])
        self.assertEqual([item["name"] for item in report["loaded"]], ["flaky"])
        self.write_mock_config([config_mod.make_reviewer("flaky-general", "flaky", "default")])

        code, out, _ = run_cli("doctor", "--fast", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual([item["path"] for item in payload["user_providers"]["problems"]], [path])
        problem = "user provider %s: defines _resolve_latest, which 0.24.0 renamed to resolve_latest" % path
        self.assertTrue(any(line.startswith(problem) for line in payload["problems"]), payload["problems"])
        self.assertTrue(
            any("rename the method to resolve_latest" in line for line in payload["problems"]),
            payload["problems"],
        )
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("Problem:  %s -- defines _resolve_latest" % path, out)
        self.assertNotIn("No problems found.", out)
        code, _, _ = run_cli("doctor", "--fast", "--strict")
        self.assertEqual(code, 1)

        # Resolving a model on it says the same, as a message.
        with self.assertRaises(providers.ModelResolutionError) as raised:
            providers.get_provider("flaky").resolve_model({"family": "default"})
        self.assertIsInstance(raised.exception, NotImplementedError)
        self.assertEqual(
            str(raised.exception),
            "flaky: the adapter defines _resolve_latest, which 0.24.0 renamed to resolve_latest; "
            "rename the method to resolve_latest",
        )
        run_cli("config", "set", "implementer.provider", "flaky")
        code, out, err = run_cli("run", "implementer", "--print-command")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("rename the method to resolve_latest", err)
        self.assertNotIn("Traceback", err)

    def test_the_old_name_is_a_problem_below_a_built_in_adapter_too(self):
        """A subclass of a built-in adapter inherits its ``resolve_latest``,
        so its own ``_resolve_latest`` would go uncalled without a word."""
        source = (
            "from orchestrator.providers.mock import MockProvider\n"
            "\n"
            "class Mine(MockProvider):\n"
            "    name = 'mine'\n"
            "\n"
            "    def _resolve_latest(self, family):\n"
            "        raise AssertionError('never called')\n"
            "\n"
            "def build_provider(executable=None):\n"
            "    return Mine(executable)\n"
        )
        path = self.write_user_provider("mine", source)
        report = self.load_user_providers()
        self.assertEqual(report["errors"], [])
        self.assertEqual([item["path"] for item in report["problems"]], [path])
        self.assertIn("never called; rename the method to resolve_latest", report["problems"][0]["problem"])

    def test_defining_both_names_is_not_a_problem(self):
        source = (
            FLAKY_ADAPTER.replace(
                "def resolve_latest(",
                "def _resolve_latest(self, family):\n        return None\n\n    def resolve_latest(",
            )
            % "none"
        )
        self.write_user_provider("flaky", source)
        self.assertEqual(self.load_user_providers()["problems"], [])

    def test_an_adapter_without_resolve_latest_says_it_must_define_one(self):
        source = FLAKY_ADAPTER.replace("def resolve_latest(", "def resolve_other(") % "none"
        self.write_user_provider("flaky", source)
        report = self.load_user_providers()
        self.assertEqual(report["problems"], [])
        with self.assertRaises(providers.ModelResolutionError) as raised:
            providers.get_provider("flaky").resolve_model({"family": "default"})
        self.assertEqual(str(raised.exception), "flaky: the adapter must define resolve_latest(family)")

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


if __name__ == "__main__":
    unittest.main()
