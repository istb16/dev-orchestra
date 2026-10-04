"""End-to-end behaviour of the ``dev-orchestra`` command."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, ClassVar, Dict, Optional
from unittest import mock

from helpers import CLAUDE_HELP, CLAUDE_HELP_NO_FORK, CLAUDE_HELP_OLD, TEST_WORKFLOW, IsolatedCase, has_git

from orchestrator import activity as activity_mod
from orchestrator import cli, cli_workflow, providers
from orchestrator import config as config_mod
from orchestrator import ledger as ledger_mod
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
        self.assertIn("showing preset standard fitted to the installed CLIs", out)
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

    def test_show_says_what_the_design_review_is_set_to(self):
        """It decides whether a whole stage runs, so its state has to be
        visible without reading the JSON."""
        _, out, _ = run_cli("config", "show")
        self.assertIn("design review: auto", out)

    def test_set_puts_the_design_review_on_auto_or_off(self):
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "review.design.enabled", "auto")
        self.assertEqual(config_mod.load(self.project).design_review_settings()["enabled"], "auto")
        self.assertIn("design review: auto", run_cli("config", "show")[1])
        run_cli("config", "set", "review.design.enabled", "false")
        self.assertIs(config_mod.load(self.project).design_review_settings()["enabled"], False)
        self.assertIn("design review: off", run_cli("config", "show")[1])

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

    def test_nothing_redirected_adds_no_keys(self):
        run_cli("config", "setup", "--defaults")
        for scope in ("global", "effective", "project"):
            payload = json.loads(run_cli("config", "show", "--scope", scope, "--json")[1])
            self.assertNotIn("source_real", payload, scope)
            self.assertNotIn("global_real", payload, scope)
        self.assertNotIn("stored at", run_cli("config", "path")[1])


#: ``(name, global file before, installed CLIs, argv)``: every command that
#: writes the global file and names it. A file is a layer, raw text, or absent.
STORED_AT_WRITES = (
    ("setup --defaults", None, ("claude",), ("config", "setup", "--defaults")),
    ("setup --preset", None, ("claude",), ("config", "setup", "--preset", "standard")),
    (
        "reset",
        {"version": 1, "review": {"parallel": False}},
        ("claude",),
        ("config", "reset", "--scope", "global"),
    ),
    ("reset --delete", {"version": 1}, ("claude",), ("config", "reset", "--scope", "global", "--delete")),
    ("reset --delete, no file", None, ("claude",), ("config", "reset", "--scope", "global", "--delete")),
    ("set", {"version": 1}, ("claude",), ("config", "set", "--scope", "global", "review.parallel", "false")),
    (
        "role set",
        None,
        ("codex",),
        ("config", "set", "--scope", "global", "implementer.model.family", "sonnet"),
    ),
    (
        "set, panel recorded",
        None,
        ("claude",),
        ("config", "set", "--scope", "global", "reviewers[0].role", "security"),
    ),
    (
        "prune --dry-run",
        {"version": 1, "preset": "standard", "optimization": {"level": "balanced"}},
        ("claude",),
        ("config", "prune", "--scope", "global", "--dry-run"),
    ),
    (
        "prune, nothing to drop",
        {"version": 1, "optimization": {"low_risk_max_files": 7}},
        ("claude",),
        ("config", "prune", "--scope", "global"),
    ),
    (
        "prune, version recorded",
        "optimization:\n  low_risk_max_files: 7\n",
        ("claude",),
        ("config", "prune", "--scope", "global"),
    ),
    (
        "prune, dropped",
        {"version": 1, "preset": "standard", "optimization": {"level": "balanced"}},
        ("claude",),
        ("config", "prune", "--scope", "global"),
    ),
    (
        "reviewer add, listed",
        {"version": 1, "reviewers": [config_mod.make_reviewer("mine", "mock", "small")]},
        ("claude",),
        ("reviewer", "add", "--scope", "global", "--provider", "mock", "--id", "m1"),
    ),
    (
        "reviewer add, extra",
        {"version": 1},
        ("claude",),
        ("reviewer", "add", "--scope", "global", "--provider", "claude", "--role", "security"),
    ),
    ("reviewer remove", None, ("claude",), ("reviewer", "remove", "--scope", "global", "claude-general-2")),
    (
        "reviewer set",
        None,
        ("claude",),
        ("reviewer", "set", "--scope", "global", "claude-general-2", "--role", "test"),
    ),
)


#: The rows whose message carries a note naming the file a second time.
STORED_AT_NOTES = {
    "role set": "note: implementer is now set by %s (provider ",
    "set, panel recorded": "note: %s now lists the reviewers; ",
}


class TestStoredAtMessages(IsolatedCase):
    """Under a Microsoft Store Python, each message that names the global file
    also says where Windows really keeps it, and nothing else in it moves (#235)."""

    def setUp(self):
        super().setUp()
        self.real = self.redirect_config_home()
        self.path = config_mod.global_config_path()
        self.real_path = os.path.join(self.real, "config.yaml")

    def put_global(self, layer):
        if os.path.isfile(self.path):
            os.remove(self.path)
        if isinstance(layer, str):
            with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(layer)
        elif layer is not None:
            config_mod.write_config_file(self.path, layer, "global")

    def test_each_write_message_names_where_the_file_really_is(self):
        shown = "%s (stored at %s)" % (self.path, self.real_path)
        self.assertEqual(config_mod.shown_location(self.path), shown)
        for name, layer, clis, argv in STORED_AT_WRITES:
            with self.subTest(name):
                self.fake_clis(**dict.fromkeys(clis, True))
                self.put_global(layer)
                with mock.patch.object(config_mod, "real_location", lambda path: path):
                    plain_code, plain, _ = run_cli(*argv)
                self.put_global(layer)
                code, out, _ = run_cli(*argv)
                self.assertEqual((plain_code, code), (0, 0))
                self.assertIn(self.path, plain)
                self.assertNotIn("stored at", plain)
                if name in STORED_AT_NOTES:
                    self.assertIn(STORED_AT_NOTES[name] % self.path, plain)
                self.assertEqual(out, plain.replace(self.path, shown))
                # Every mention, not just one of them, says where it really is.
                self.assertEqual(out.count("stored at"), plain.count(self.path))

    def test_a_project_write_says_nothing_about_it(self):
        self.fake_clis(claude=True)
        code, out, _ = run_cli("config", "set", "--scope", "project", "review.parallel", "false")
        self.assertEqual(code, 0)
        self.assertNotIn("stored at", out)

    def test_config_path(self):
        out = run_cli("config", "path")[1]
        self.assertIn("global:  %s  (not created)\n" % self.path, out)
        self.assertNotIn("stored at", out)
        self.put_global({"version": 1})
        out = run_cli("config", "path")[1]
        self.assertIn("global:  %s (stored at %s)\n" % (self.path, self.real_path), out)

    def test_config_show(self):
        self.put_global({"version": 1})
        out = run_cli("config", "show", "--scope", "global")[1]
        self.assertIn("Source: %s (stored at %s)\n" % (self.path, self.real_path), out)
        payload = json.loads(run_cli("config", "show", "--scope", "global", "--json")[1])
        self.assertEqual(payload["source"], self.path)
        self.assertEqual(payload["source_real"], self.real_path)
        self.assertNotIn("global_real", payload)
        effective = json.loads(run_cli("config", "show", "--json")[1])
        self.assertEqual(effective["global_real"], self.real_path)
        self.assertNotIn("source_real", effective)
        self.assertIn("global: %s)" % self.path, effective["source"])
        # The path ``global_real`` describes is a key of its own.
        self.assertEqual(effective["global"], self.path)
        self.assertIsNone(effective["project"])
        # The effective view's text names the real place too; its JSON keeps
        # ``source`` as it was.
        out = run_cli("config", "show")[1]
        self.assertIn("global: %s (stored at %s))\n" % (self.path, self.real_path), out)
        project = json.loads(run_cli("config", "show", "--scope", "project", "--json")[1])
        self.assertNotIn("source_real", project)
        self.assertNotIn("global_real", project)

    def test_config_show_of_a_missing_file_adds_nothing(self):
        out = run_cli("config", "show", "--scope", "global")[1]
        self.assertIn("Source: %s (not created yet)\n" % self.path, out)
        for scope in ("global", "effective"):
            payload = json.loads(run_cli("config", "show", "--scope", scope, "--json")[1])
            self.assertNotIn("source_real", payload)
            self.assertNotIn("global_real", payload)
        self.assertIsNone(payload["global"])


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
        self.assertEqual(
            [r["id"] for r in json.loads(listing)],
            ["claude-general", "claude-security", "claude-test", "claude-security-2"],
        )

    def test_remove_unknown_reviewer_fails_cleanly(self):
        code, _, err = run_cli("reviewer", "remove", "ghost")
        self.assertEqual(code, 2)
        self.assertIn("no reviewer matches", err)

    def test_removing_every_reviewer_is_allowed(self):
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "remove", "claude-security-2")
        run_cli("reviewer", "remove", "claude-security")
        run_cli("reviewer", "remove", "claude-test")
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

    def listed(self, reviewer_id):
        _, out, _ = run_cli("reviewer", "list", "--json")
        return next(r for r in json.loads(out) if r["id"] == reviewer_id)

    def test_add_when_high_risk_writes_the_condition(self):
        code, _, _ = run_cli("reviewer", "add", "--provider", "mock", "--id", "sec", "--when", "high-risk")
        self.assertEqual(code, 0)
        self.assertEqual(self.listed("sec")["when"], "high-risk")

    def test_add_when_always_writes_no_key(self):
        """The default, so a panel that never uses the condition stays the
        file it would have been without it."""
        run_cli("reviewer", "add", "--provider", "mock", "--id", "sec", "--when", "always")
        self.assertNotIn("when", self.listed("sec"))

    def test_set_when_always_removes_the_condition(self):
        run_cli("reviewer", "add", "--provider", "mock", "--id", "sec", "--when", "high-risk")
        code, _, _ = run_cli("reviewer", "set", "sec", "--when", "always")
        self.assertEqual(code, 0)
        self.assertNotIn("when", self.listed("sec"))

    def test_the_only_reviewer_cannot_become_conditional(self):
        for reviewer_id in ("codex-general", "claude-security", "claude-test"):
            run_cli("reviewer", "remove", reviewer_id)
        code, _, err = run_cli("reviewer", "set", "claude-general", "--when", "high-risk")
        self.assertEqual(code, 2)
        self.assertIn("reviewers: at least one reviewer must run always", err)
        self.assertNotIn("when", self.listed("claude-general"))

    def test_removing_the_last_unconditional_reviewer_is_refused(self):
        run_cli("reviewer", "set", "codex-general", "--when", "high-risk")
        for reviewer_id in ("claude-security", "claude-test"):
            run_cli("reviewer", "remove", reviewer_id)
        code, _, err = run_cli("reviewer", "remove", "claude-general")
        self.assertEqual(code, 2)
        self.assertIn("at least one reviewer must run always", err)
        self.assertEqual(self.listed("claude-general")["id"], "claude-general")

    def broken_panel(self):
        """`gen` always runs; `bad1` and `bad2` are conditional, each with an
        empty role that validation reports."""
        broken = {"provider": "mock", "role": "", "when": "high-risk"}
        config_mod.write_config_file(
            config_mod.global_config_path(),
            dict(
                config_mod.default_config(),
                reviewers=[
                    {"id": "gen", "provider": "mock", "role": "general"},
                    dict(broken, id="bad1"),
                    dict(broken, id="bad2"),
                ],
            ),
        )

    def test_removing_one_of_two_broken_reviewers_is_allowed(self):
        """Only a problem the removal introduced refuses it, so another
        reviewer's problem does not lock the panel."""
        self.broken_panel()
        code, out, err = run_cli("reviewer", "remove", "bad1")
        self.assertEqual(code, 0, err)
        self.assertIn("Removed reviewer bad1", out)
        _, listing, _ = run_cli("reviewer", "list", "--json")
        self.assertEqual([r["id"] for r in json.loads(listing)], ["gen", "bad2"])

    def test_removing_the_last_unconditional_reviewer_of_a_broken_panel_is_refused(self):
        self.broken_panel()
        code, _, err = run_cli("reviewer", "remove", "gen")
        self.assertEqual(code, 2)
        self.assertIn("at least one reviewer must run always", err)
        self.assertNotIn("role", err)
        self.assertEqual(self.listed("gen")["id"], "gen")

    def test_list_shows_the_condition(self):
        run_cli("reviewer", "add", "--provider", "mock", "--id", "sec", "--when", "high-risk")
        _, out, _ = run_cli("reviewer", "list")
        line = next(line for line in out.splitlines() if " sec " in line)
        self.assertIn("general (when: high-risk)", line)
        self.assertNotIn("when:", next(line for line in out.splitlines() if "claude-general" in line))

    def add_db(self, *patterns):
        return run_cli("reviewer", "add", "--provider", "mock", "--id", "db", "--when-paths", *patterns)

    def test_add_when_paths_writes_the_mapping_in_block_form(self):
        code, _, err = self.add_db("*migrate*/*", "*.sql")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.listed("db")["when"], {"paths": ["*migrate*/*", "*.sql"]})
        text = read_file(config_mod.global_config_path())
        self.assertIn('    when:\n      paths:\n        - "*migrate*/*"\n        - "*.sql"\n', text)

    def test_the_written_patterns_load_with_the_bundled_parser(self):
        """Quoted and in block form, so a pattern holding `[` loads without
        PyYAML too."""
        from orchestrator.miniyaml import _parse_node, _read_lines

        self.add_db("*[Mm]igration*/*", "*.sql")
        lines = _read_lines(read_file(config_mod.global_config_path()))
        data, consumed = _parse_node(lines, 0)
        self.assertEqual(consumed, len(lines))
        db = next(r for r in data["reviewers_extra"] if r["id"] == "db")
        self.assertEqual(db["when"]["paths"], ["*[Mm]igration*/*", "*.sql"])

    def test_when_and_when_paths_together_are_refused(self):
        both = ("--when", "high-risk", "--when-paths", "*.sql")
        code, _, err = run_cli("reviewer", "add", "--provider", "mock", "--id", "db", *both)
        self.assertEqual(code, 2)
        self.assertIn("give --when or --when-paths, not both", err)
        self.add_db("*.sql")
        code, _, err = run_cli("reviewer", "set", "db", "--when", "always", "--when-paths", "*.md")
        self.assertEqual(code, 2)
        self.assertIn("give --when or --when-paths, not both", err)
        self.assertEqual(self.listed("db")["when"], {"paths": ["*.sql"]})

    def test_set_when_always_removes_the_mapping(self):
        self.add_db("*.sql")
        self.assertEqual(run_cli("reviewer", "set", "db", "--when", "always")[0], 0)
        self.assertNotIn("when", self.listed("db"))

    def test_set_when_paths_replaces_the_list_wholesale(self):
        self.add_db("*migrate*/*", "*.sql")
        self.assertEqual(run_cli("reviewer", "set", "db", "--when-paths", "*.md")[0], 0)
        self.assertEqual(self.listed("db")["when"], {"paths": ["*.md"]})

    def test_set_when_high_risk_replaces_the_mapping(self):
        self.add_db("*.sql")
        self.assertEqual(run_cli("reviewer", "set", "db", "--when", "high-risk")[0], 0)
        self.assertEqual(self.listed("db")["when"], "high-risk")

    def test_set_when_paths_on_a_high_risk_reviewer_replaces_the_string(self):
        run_cli("reviewer", "add", "--provider", "mock", "--id", "sec", "--when", "high-risk")
        self.assertEqual(run_cli("reviewer", "set", "sec", "--when-paths", "*.sql")[0], 0)
        self.assertEqual(self.listed("sec")["when"], {"paths": ["*.sql"]})

    def test_list_shows_the_patterns(self):
        self.add_db("*.sql")
        _, out, _ = run_cli("reviewer", "list")
        line = next(line for line in out.splitlines() if " db " in line)
        self.assertTrue(line.endswith("general (when: paths *.sql) (extra: global)"), line)

    def test_removing_the_last_unconditional_reviewer_is_refused_when_the_rest_are_path_scoped(self):
        for reviewer_id in ("codex-general", "claude-security", "claude-test"):
            run_cli("reviewer", "remove", reviewer_id)
        self.add_db("*.sql")
        code, _, err = run_cli("reviewer", "remove", "claude-general")
        self.assertEqual(code, 2)
        self.assertIn("at least one reviewer must run always", err)

    def test_an_empty_pattern_is_refused(self):
        code, _, err = self.add_db("*.sql", " ")
        self.assertEqual(code, 2)
        self.assertIn("when.paths[1]: must be a non-empty string", err)

    def test_a_credential_shaped_pattern_is_redacted_in_the_list(self):
        secret = "k" * 16
        self.add_db("*secret=%s*" % secret)
        _, out, _ = run_cli("reviewer", "list")
        self.assertIn("(when: paths *secret=[redacted]*)", out)
        self.assertNotIn(secret, out)

    def test_a_credential_shaped_pattern_is_redacted_in_the_json_list(self):
        secret = "k" * 16
        self.add_db("*.sql", "*secret=%s*" % secret)
        _, out, _ = run_cli("reviewer", "list", "--json")
        self.assertNotIn(secret, out)
        self.assertEqual(self.listed("db")["when"], {"paths": ["*.sql", "*secret=[redacted]*"]})
        self.assertIn(secret, read_file(config_mod.global_config_path()))


class TestConditionPathsFallback(unittest.TestCase):
    """A snapshot frozen before `condition_paths` existed."""

    META: ClassVar[Dict[str, Any]] = {
        "files": ["lib/x.rb"],
        "withheld": [{"path": "gen/schema.rb"}],
        "changed_paths": ["lib/x.rb", "db/migrate/x.rb", "gen/schema.rb"],
    }

    def test_a_first_round_uses_the_whole_change_with_rename_sources(self):
        meta = dict(self.META, incremental_from="")
        self.assertEqual(cli._condition_paths(meta), ["lib/x.rb", "db/migrate/x.rb", "gen/schema.rb"])

    def test_an_incremental_round_uses_the_reviewed_and_withheld_files(self):
        meta = dict(self.META, incremental_from="abc123")
        self.assertEqual(cli._condition_paths(meta), ["lib/x.rb", "gen/schema.rb"])

    def test_a_recorded_list_is_used_as_it_is(self):
        meta = dict(self.META, incremental_from="", condition_paths=["lib/x.rb"])
        self.assertEqual(cli._condition_paths(meta), ["lib/x.rb"])


class TestMalformedPathConditions(IsolatedCase):
    """`reviewer list`, `doctor` and `status` read the config unvalidated, so
    a malformed `when` mapping must reach them as a reviewer that always
    runs; `config validate` names the problem."""

    CASES = (
        (
            '      paths:\n        - "*.sql"\n        - 42\n',
            "reviewers[1].when.paths[1]: must be a non-empty string (got 42)",
        ),
        (
            '      paths: "*.sql"\n',
            "reviewers[1].when.paths: must be a non-empty list of glob patterns",
        ),
        (
            '      paths:\n        - "*.sql"\n      extra: 1\n',
            "reviewers[1].when: a when mapping takes paths only (got keys: extra, paths)",
        ),
    )

    def write_config(self, when):
        self.write(
            ".dev-orchestra.yaml",
            "version: 1\n"
            "reviewers:\n"
            "  - id: gen\n    provider: mock\n    role: general\n"
            "  - id: db\n    provider: mock\n    role: database\n    when:\n" + when,
        )

    def test_each_form_reads_as_always_and_is_named_by_validate(self):
        for when, problem in self.CASES:
            with self.subTest(problem=problem):
                self.write_config(when)
                _, out, _ = run_cli("reviewer", "list")
                self.assertNotIn("when:", out)
                _, payload, _ = run_cli("doctor", "--fast", "--json")
                entries = {entry["id"]: entry for entry in json.loads(payload)["reviewers"]}
                self.assertNotIn("when", entries["db"])
                _, payload, _ = run_cli("status", "--json")
                self.assertEqual(json.loads(payload)["optimization"]["conditional"], [])
                _, out, err = run_cli("config", "validate")
                self.assertIn(problem, out + err)


class TestNonMappingReviewers(IsolatedCase):
    """`doctor`, `reviewer list`, `config show` and `summary` read the config
    unvalidated, so a `reviewers` entry that is not a mapping, or a panel that
    is not a list, must reach them as a skipped entry; `config validate` names
    the problem. `review consolidate` refuses it instead."""

    MIXED = "version: 1\nreviewers:\n  - 3\n  - id: ok\n    provider: mock\n"
    NOT_A_LIST = "version: 1\nreviewers: x\n"
    A_MAPPING = "version: 1\nreviewers:\n  id: a\n  provider: mock\n"
    ALL_BROKEN = "version: 1\nreviewers:\n  - 3\n"
    EMPTY = "version: 1\nreviewers: []\n"
    EXTRA_PROJECT = "version: 1\nreviewers_extra:\n  - id: extra\n    provider: mock\n"
    BROKEN_EXTRA_PROJECT = "version: 1\nreviewers_extra:\n  - 3\n  - id: extra\n    provider: mock\n"
    BROKEN_PROJECT = "version: 1\nreviewers:\n  - 3\n  - id: mine\n    provider: mock\n"
    VALID = "version: 1\nreviewers:\n  - id: valid\n    provider: mock\n"

    NOT_A_MAPPING = "reviewers[0]: must be a mapping"
    NOT_LISTED = "reviewers: must be a list (use [] for none)"
    NONE_CONFIGURED = "no reviewers configured: the independent-review stage will be skipped"

    def write_global(self, text):
        path = config_mod.global_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def panel_problems(self, payload):
        return [
            problem
            for problem in payload["problems"]
            if problem.startswith("reviewers") or problem.startswith("no reviewers")
        ]

    def doctor_json(self):
        code, out, err = run_cli("doctor", "--fast", "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_doctor_text_lists_the_valid_reviewer(self):
        for text in (self.MIXED, self.NOT_A_LIST, self.A_MAPPING, self.ALL_BROKEN, self.EMPTY):
            with self.subTest(config=text):
                self.write_global(text)
                code, _, err = run_cli("doctor", "--fast")
                self.assertEqual(code, 0, err)
        self.write_global(self.MIXED)
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("%-13s %d" % ("Reviewers:", 1), out)
        self.assertIn("    1. ok / mock / default", out)
        self.assertIn("  - %s" % self.NOT_A_MAPPING, out)
        self.assertNotIn("no reviewers configured", out)
        self.write_global(self.NOT_A_LIST)
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("  - %s" % self.NOT_LISTED, out)
        self.assertNotIn("no reviewers configured", out)

    def test_doctor_strict_exits_one_on_a_broken_panel(self):
        for text in (self.MIXED, self.NOT_A_LIST, self.A_MAPPING, self.ALL_BROKEN, self.EMPTY):
            with self.subTest(config=text):
                self.write_global(text)
                code, _, _ = run_cli("doctor", "--fast", "--strict")
                self.assertEqual(code, 1)

    def test_doctor_json_names_only_validates_problem(self):
        cases = (
            (self.MIXED, [("ok", "global")], [self.NOT_A_MAPPING]),
            (self.NOT_A_LIST, [], [self.NOT_LISTED]),
            (self.A_MAPPING, [], [self.NOT_LISTED]),
            (self.ALL_BROKEN, [], [self.NOT_A_MAPPING]),
            (self.EMPTY, [], [self.NONE_CONFIGURED]),
        )
        for text, reviewers, problems in cases:
            with self.subTest(config=text):
                self.write_global(text)
                payload = self.doctor_json()
                listed = [(entry["id"], entry["origin"]) for entry in payload["reviewers"]]
                self.assertEqual(listed, reviewers)
                self.assertEqual(self.panel_problems(payload), problems)

    def test_reviewer_list_skips_the_broken_entry(self):
        self.write_global(self.MIXED)
        code, out, err = run_cli("reviewer", "list")
        self.assertEqual(code, 0, err)
        self.assertIn("2. %-18s %-8s" % ("ok", "mock"), out)
        numbered = [line for line in out.splitlines() if line.split(".", 1)[0].isdigit()]
        self.assertEqual(len(numbered), 1, out)
        self.assertNotIn("1. ", out)
        for text in (self.NOT_A_LIST, self.A_MAPPING, self.ALL_BROKEN):
            with self.subTest(config=text):
                self.write_global(text)
                code, out, err = run_cli("reviewer", "list")
                self.assertEqual(code, 0, err)
                self.assertEqual(out.strip(), "No reviewers configured.")

    def test_reviewer_list_json(self):
        self.write_global(self.MIXED)
        _, out, _ = run_cli("reviewer", "list", "--json")
        listed = json.loads(out)
        self.assertEqual(listed[0], "[invalid entry]")
        self.assertEqual(listed[1]["id"], "ok")
        self.assertEqual(listed[1]["origin"], "global")
        for text in (self.NOT_A_LIST, self.A_MAPPING):
            with self.subTest(config=text):
                self.write_global(text)
                _, out, _ = run_cli("reviewer", "list", "--json")
                self.assertEqual(json.loads(out), [])
        # No entry that is not a mapping shows its contents, whatever it holds:
        # `redact` does not know every credential's shape.
        broken = (
            '"sk-supersecretvalue"',
            '"xoxb-slacktokenvalue"',
            '"AKIAEXAMPLEKEYVALUE"',
            "[nestedsecretvalue]",
            "987654321",
        )
        for entry in broken:
            with self.subTest(entry=entry):
                self.write_global("version: 1\nreviewers:\n  - %s\n  - id: ok\n    provider: mock\n" % entry)
                _, out, _ = run_cli("reviewer", "list", "--json")
                self.assertEqual(json.loads(out)[0], "[invalid entry]")
                self.assertNotIn(entry.strip('"[]'), out)

    def test_config_show_skips_the_broken_entry(self):
        cases = (
            (self.MIXED, ["    2. mock / default / latest / general / ok", "  - %s" % self.NOT_A_MAPPING]),
            (self.NOT_A_LIST, ["    (none configured)", "  - %s" % self.NOT_LISTED]),
            (self.A_MAPPING, ["    (none configured)", "  - %s" % self.NOT_LISTED]),
            (self.ALL_BROKEN, ["    (none configured)", "  - %s" % self.NOT_A_MAPPING]),
            (self.EMPTY, ["    (none configured)"]),
        )
        for text, expected in cases:
            with self.subTest(config=text):
                self.write_global(text)
                code, out, err = run_cli("config", "show")
                self.assertEqual(code, 0, err)
                for line in expected:
                    self.assertIn(line, out)
                if text == self.EMPTY:
                    self.assertNotIn(self.NOT_LISTED, out)
                    self.assertNotIn(self.NOT_A_MAPPING, out)

    def test_layered_extra_keeps_its_origin(self):
        self.write_global(self.MIXED)
        self.write(".dev-orchestra.yaml", self.EXTRA_PROJECT)
        payload = self.doctor_json()
        self.assertEqual([entry["id"] for entry in payload["reviewers"]], ["ok", "extra"])
        self.assertEqual([entry["origin"] for entry in payload["reviewers"]], ["global", "project extra"])
        _, out, _ = run_cli("doctor", "--fast")
        line = next(line for line in out.splitlines() if line.startswith("    2. extra / mock / default"))
        self.assertTrue(line.endswith("general (extra: project)"), line)
        _, out, _ = run_cli("reviewer", "list")
        line = next(line for line in out.splitlines() if line.startswith("3. %-18s" % "extra"))
        self.assertTrue(line.endswith("(extra: project)"), line)
        _, out, _ = run_cli("config", "show")
        self.assertIn("    3. mock / default / latest / general / extra (extra, project file)", out)

    def test_broken_extra_keeps_its_origin(self):
        # A broken extra is not folded into the panel, so the valid one after
        # it still sits third, after the global file's two entries.
        self.write_global(self.MIXED)
        self.write(".dev-orchestra.yaml", self.BROKEN_EXTRA_PROJECT)
        payload = self.doctor_json()
        listed = [(entry["id"], entry["origin"]) for entry in payload["reviewers"]]
        self.assertEqual(listed, [("ok", "global"), ("extra", "project extra")])
        self.assertIn("reviewers_extra[0] in the project file: must be a mapping", payload["problems"])
        _, out, _ = run_cli("doctor", "--fast")
        line = next(line for line in out.splitlines() if line.startswith("    2. extra / mock / default"))
        self.assertTrue(line.endswith("general (extra: project)"), line)
        _, out, _ = run_cli("reviewer", "list")
        line = next(line for line in out.splitlines() if line.startswith("3. %-18s" % "extra"))
        self.assertTrue(line.endswith("(extra: project)"), line)
        _, out, _ = run_cli("config", "show")
        self.assertIn("    3. mock / default / latest / general / extra (extra, project file)", out)

    def test_broken_project_entry_keeps_its_origin(self):
        self.write_global(self.VALID)
        self.write(".dev-orchestra.yaml", self.BROKEN_PROJECT)
        payload = self.doctor_json()
        listed = [(entry["id"], entry["origin"]) for entry in payload["reviewers"]]
        self.assertEqual(listed, [("mine", "project")])
        self.assertIn(self.NOT_A_MAPPING, payload["problems"])
        _, out, _ = run_cli("reviewer", "list")
        line = next(line for line in out.splitlines() if line.startswith("2. %-18s" % "mine"))
        self.assertNotIn("(extra:", line)
        self.assertNotIn("valid", out)
        _, out, _ = run_cli("config", "show")
        self.assertIn("    2. mock / default / latest / general / mine\n", out)

    def review_lines(self, out):
        """The lines listed under `Review:` in the summary."""
        lines = out.splitlines()
        start = lines.index("Review:") + 1
        listed = []
        for line in lines[start:]:
            if not line.startswith("  "):
                break
            listed.append(line)
        return listed

    def test_summary_skips_the_broken_entry(self):
        self.write_global(self.MIXED)
        code, out, err = run_cli("summary")
        self.assertEqual(code, 0, err)
        self.assertIn("  %-14s mock / default" % "ok", out)
        self.write_global(self.NOT_A_LIST)
        code, out, err = run_cli("summary")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.review_lines(out), [])
        self.write_global(self.VALID)
        recorded = {"reviewers": [3, {"id": "ok", "provider": "mock", "model": "fam"}]}
        ws.write_json(self.cli_workspace().consolidated_json_path, recorded)
        code, out, err = run_cli("summary")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.review_lines(out), ["  %-14s mock / fam" % "ok"])

    def test_summary_falls_back_from_a_recorded_non_list(self):
        # A recorded panel that is not a list is treated as missing, so the
        # configured one is listed instead.
        self.write_global(self.VALID)
        for recorded in ({"reviewers": "x"}, {"reviewers": {"id": "ok", "provider": "mock"}}):
            with self.subTest(recorded=recorded):
                ws.write_json(self.cli_workspace().consolidated_json_path, recorded)
                code, out, err = run_cli("summary")
                self.assertEqual(code, 0, err)
                listed = self.review_lines(out)
                self.assertEqual(len(listed), 1, listed)
                self.assertTrue(listed[0].startswith("  %-14s mock / " % "valid"), listed)

    def test_review_consolidate_refuses_a_broken_panel(self):
        cases = (
            (self.MIXED, self.NOT_A_MAPPING),
            (self.ALL_BROKEN, self.NOT_A_MAPPING),
            (self.NOT_A_LIST, self.NOT_LISTED),
            (self.A_MAPPING, self.NOT_LISTED),
        )
        for text, problem in cases:
            with self.subTest(config=text):
                self.write_global(text)
                path = self.cli_workspace().consolidated_json_path
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as handle:
                    handle.write(b'{"sentinel": true}')
                code, _, err = run_cli("review", "consolidate")
                self.assertEqual(code, 2)
                self.assertIn("refusing to consolidate", err)
                self.assertIn(problem, err)
                with open(path, "rb") as handle:
                    self.assertEqual(handle.read(), b'{"sentinel": true}')

    def test_review_consolidate_takes_a_sound_panel(self):
        # No panel, an empty one and a valid one are not the guard's to refuse,
        # whatever consolidating them then does without any reports.
        for text in ("version: 1\n", self.EMPTY, self.VALID):
            with self.subTest(config=text):
                self.write_global(text)
                code, _, err = run_cli("review", "consolidate")
                self.assertNotEqual(code, 2, err)
                self.assertNotIn("refusing to consolidate", err)


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
        run_cli("reviewer", "remove", "claude-security-2")
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

    def test_an_older_copy_of_the_defaults_is_still_the_defaults(self):
        # Configs written before 0.6.0 hold the default list of their day,
        # which lacks patterns added since.
        defaults = config_mod.default_config()["optimization"]["high_risk_paths"]
        self.write_config(high_risk_paths=[p for p in defaults if p not in ("k8s/*", "deploy/*")])
        self.assertEqual(len(self.notes()), 1)

    def test_a_blank_entry_does_not_make_the_defaults_a_choice(self):
        defaults = config_mod.default_config()["optimization"]["high_risk_paths"]
        self.write_config(high_risk_paths=[*defaults, "", "  "])
        self.assertEqual(len(self.notes()), 1)

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


SECRET_SETTINGS = '--settings={"apiKeyHelper":"sk-ant-abcdefghijklmnopqrs"}'


class _Completed:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def pretend_claude_is_installed(case, help_text: Optional[str] = CLAUDE_HELP):
    """The claude adapter answering from fixtures: installed, 2.1.283, and
    ``--help`` reading as ``help_text`` (None: it cannot be read)."""
    from orchestrator.providers.claude import ClaudeProvider

    def capture(self, command, timeout=30):
        return None if help_text is None else _Completed(help_text)

    for name, value in (
        ("which", lambda self: "claude"),
        ("version", lambda self: ("2.1.283 (Claude Code)", None)),
        ("_capture", capture),
    ):
        case.addCleanup(setattr, ClaudeProvider, name, getattr(ClaudeProvider, name))
        setattr(ClaudeProvider, name, value)


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
        run_cli("reviewer", "remove", "claude-security-2")
        run_cli("reviewer", "remove", "claude-security")
        run_cli("reviewer", "remove", "claude-test")
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

    def test_a_round_leaves_out_the_sleep_of_every_reviewer(self):
        os.environ["DEV_ORCHESTRA_MOCK_SUSPENDED"] = "90"
        run_cli("review", "snapshot")
        self.assertEqual(run_cli("review", "run")[0], 0)
        event = self.cli_workspace().read_state()["events"][-1]
        self.assertEqual(len(event["reviewers"]), 2)
        suspended = sum(reviewer["suspended_seconds"] for reviewer in event["reviewers"])
        durations = sum(reviewer["duration_seconds"] for reviewer in event["reviewers"])
        self.assertEqual(suspended, 180)
        self.assertEqual(event["suspended_seconds"], suspended)
        self.assertAlmostEqual(event["charged_seconds"], durations - suspended, places=1)
        payload = json.loads(run_cli("budget", "show", "--json")[1])
        self.assertAlmostEqual(payload["runtime"]["used"], event["charged_seconds"], places=1)
        self.assertEqual(payload["runtime"]["suspended"], 180)

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
        self.assertTrue(self.review_line().endswith("-- final fix pending (fix, re-test, do not re-review)"))

    def review_line(self):
        """The `Review:` line of the text view."""
        out = run_cli("status")[1]
        return next(line for line in out.splitlines() if line.startswith("Review: "))

    def test_a_fix_after_the_last_round_waits_for_the_re_test(self):
        self.last_round()
        self.assertEqual(self.fix()[0], 0)
        self.assertEqual(self.review_status()["final_fix"], "retest")
        self.assertIn("re-run the tests", run_cli("review", "status")[1])
        payload = self.status()
        self.assertEqual((payload["verdict"], payload["reasons"]), ("continue", []))
        self.assertTrue(
            self.review_line().endswith("-- final fix done, re-test pending (record it, do not re-review)")
        )

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
        self.assertTrue(
            self.review_line().endswith("(fix, re-test, do not re-review); identical to the previous round")
        )
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
        run_cli("reviewer", "remove", "claude-security-2")
        run_cli("reviewer", "remove", "claude-security")
        run_cli("reviewer", "remove", "claude-test")
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


PARENT_SESSION = "55555555-5555-4555-8555-555555555555"
UNLISTED_CLAUDE = "1.0.0 (Claude Code)"


def pretend_claude_version(case, version):
    from orchestrator.providers.claude import ClaudeProvider

    case.addCleanup(setattr, ClaudeProvider, "version", ClaudeProvider.version)
    ClaudeProvider.version = lambda self: (version, None)


def record_resume_pass(root, version=UNLISTED_CLAUDE, mechanism=None):
    from orchestrator import verified
    from orchestrator.providers.claude import READ_ONLY_MECHANISM

    verified.record_pass(
        "claude",
        version,
        READ_ONLY_MECHANISM if mechanism is None else mechanism,
        list(verified.REQUIRED_RESUME_CHECKS),
        "sonnet",
        root,
    )


#: A user adapter that is the claude adapter under another name, resuming nothing.
CLAUDE_OFF_ADAPTER = '''\
"""The claude adapter under another name, without resumed sessions."""

from __future__ import annotations

from typing import Optional

from orchestrator.providers.claude import ClaudeProvider


class ClaudeOffProvider(ClaudeProvider):
    name = "claude-off"
    supports_resume = False


def build_provider(executable: Optional[str] = None) -> ClaudeOffProvider:
    return ClaudeOffProvider(executable)
'''


class TestDoctorResume(IsolatedCase):
    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")

    def support(self, *argv):
        return json.loads(run_cli("doctor", "--json", *argv)[1])["providers"]["claude"]["resume_support"]

    def test_a_claude_subclass_with_resume_off_does_not_resume(self):
        pretend_claude_is_installed(self)
        self.write_user_provider("claude_off", CLAUDE_OFF_ADAPTER)
        self.load_user_providers()
        found = json.loads(run_cli("doctor", "--json")[1])["providers"]
        expected = {"status": "unsupported", "detail": "claude-off does not resume sessions"}
        self.assertEqual(found["claude-off"]["resume_support"], expected)
        self.assertEqual(found["claude"]["resume_support"]["status"], "verified")
        _, out, _ = run_cli("doctor")
        self.assertIn("Resume: NOT SUPPORTED -- claude-off does not resume sessions", out)
        fast = json.loads(run_cli("doctor", "--fast", "--json")[1])["providers"]
        self.assertEqual(fast["claude-off"]["resume_support"], {"status": "not-checked"})

    def test_an_unlisted_version_is_unverified_until_recorded(self):
        pretend_claude_is_installed(self)
        pretend_claude_version(self, UNLISTED_CLAUDE)
        self.assertEqual(self.support()["status"], "unverified")
        _, out, _ = run_cli("doctor")
        self.assertIn("Resume: UNVERIFIED -- ", out)
        strict_before = run_cli("doctor", "--strict")[0]
        record_resume_pass(self.project)
        support = self.support()
        self.assertEqual(support["status"], "verified")
        self.assertEqual(support["source"], "record")
        _, out, _ = run_cli("doctor")
        self.assertIn("Resume: verified for claude %s on " % UNLISTED_CLAUDE, out)
        self.assertEqual(run_cli("doctor", "--strict")[0], strict_before)

    def test_fast_does_not_check(self):
        self.assertEqual(self.support("--fast")["status"], "not-checked")
        _, out, _ = run_cli("doctor", "--fast")
        self.assertNotIn("Resume: verified", out)

    def test_a_cli_without_fork_session_is_not_supported(self):
        pretend_claude_is_installed(self, CLAUDE_HELP_NO_FORK)
        support = self.support()
        self.assertEqual(support["status"], "unsupported")
        _, out, _ = run_cli("doctor")
        self.assertIn("Resume: NOT SUPPORTED -- --fork-session not advertised", out)

    def test_codex_with_resume_off_does_not_resume(self):
        from orchestrator.providers.codex import CodexProvider

        for name, value in (
            ("supports_resume", False),
            ("which", lambda self: "codex"),
            ("version", lambda self: ("codex-cli 0.154.0", None)),
            ("configured_model", lambda self: None),
            ("_capture", lambda self, command, timeout=30: _Completed("", 1)),
        ):
            self.addCleanup(setattr, CodexProvider, name, getattr(CodexProvider, name))
            setattr(CodexProvider, name, value)
        _, out, _ = run_cli("doctor")
        self.assertIn("Resume: NOT SUPPORTED -- codex does not resume sessions", out)

    def test_a_newer_claude_is_trusted_and_says_so(self):
        pretend_claude_is_installed(self)
        pretend_claude_version(self, "2.1.285 (Claude Code)")
        strict_verified = run_cli("doctor", "--strict")[0]
        pretend_claude_version(self, "2.1.286 (Claude Code)")
        support = self.support()
        self.assertEqual(support["status"], "trusted")
        self.assertEqual(support["newer_than"], "2.1.285 (Claude Code)")
        _, out, _ = run_cli("doctor")
        trusted = "Resume: trusted for claude 2.1.286 (Claude Code) as newer than 2.1.285 (Claude Code) "
        self.assertIn(trusted + "(verified on ", out)
        tail = ", built-in); not verified itself -- run python scripts/smoke_live.py --provider claude"
        self.assertIn(tail, out)
        self.assertEqual(run_cli("doctor", "--strict")[0], strict_verified)

    def test_codex_once_it_resumes(self):
        from orchestrator.providers import codex as codex_module
        from orchestrator.providers.codex import CodexProvider

        # `codex exec fork --help` as codex-cli 0.156.1 printed it.
        here = os.path.dirname(os.path.abspath(__file__))
        fixture = os.path.join(here, "fixtures", "codex", "fork-help.txt")
        with open(fixture, encoding="utf-8") as handle:
            fork_help = handle.read()

        def capture(self, command, timeout=30):
            if list(command[1:]) == ["exec", "fork", "--help"]:
                return _Completed(fork_help)
            return _Completed("", 1)

        version = ["codex-cli 0.156.1"]
        for name, value in (
            ("which", lambda self: "codex"),
            ("version", lambda self: (version[0], None)),
            ("configured_model", lambda self: None),
            ("_capture", capture),
            ("supports_resume", True),
        ):
            self.addCleanup(setattr, CodexProvider, name, getattr(CodexProvider, name))
            setattr(CodexProvider, name, value)
        from orchestrator.providers import codex as codex_table

        self.addCleanup(setattr, codex_table, "VERIFIED_RESUME", codex_table.VERIFIED_RESUME)
        codex_table.VERIFIED_RESUME = {}

        def line():
            lines = run_cli("doctor")[1].splitlines()
            return [text for text in lines if "Resume:" in text and "codex" in text]

        self.assertIn("UNVERIFIED -- codex codex-cli 0.156.1 has not been verified", line()[0])
        entry = {
            "verified_at": "2026-10-01",
            "read_only_mechanism": CodexProvider().resume_mechanism(),
            "checks": list(CodexProvider.required_resume_checks),
        }
        self.addCleanup(setattr, codex_module, "VERIFIED_RESUME", codex_module.VERIFIED_RESUME)
        codex_module.VERIFIED_RESUME = {"codex-cli 0.156.1": entry}
        self.assertIn("Resume: verified for codex codex-cli 0.156.1 on 2026-10-01 (built-in)", line()[0])
        version[0] = "codex-cli 0.157.0"
        self.assertIn(
            "Resume: trusted for codex codex-cli 0.157.0 as newer than codex-cli 0.156.1 "
            "(verified on 2026-10-01, built-in); not verified itself -- "
            "run python scripts/smoke_live.py --provider codex",
            line()[0],
        )
        codex = json.loads(run_cli("doctor", "--json")[1])["providers"]["codex"]["resume_support"]
        self.assertEqual(codex["status"], "trusted")
        self.assertEqual(codex["newer_than"], "codex-cli 0.156.1")

    def test_a_record_inside_the_checkout_is_not_read(self):
        pretend_claude_is_installed(self)
        pretend_claude_version(self, UNLISTED_CLAUDE)
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(self.project, ".ai", "home")
        support = self.support()
        self.assertEqual(support["status"], "unverified")
        self.assertIn("inside the workspace", support["detail"])

    def test_a_record_inside_the_repository_is_not_read_from_a_subdirectory(self):
        """`run` judges against the repository root, so doctor must too."""
        self.init_git_repo()
        pretend_claude_is_installed(self)
        pretend_claude_version(self, UNLISTED_CLAUDE)
        home = os.path.join(self.project, ".ai", "home")
        os.environ["DEV_ORCHESTRA_HOME"] = home
        subdirectory = os.path.join(self.project, "src")
        os.makedirs(subdirectory)
        # Written as if from the subdirectory, which the record lies outside.
        record_resume_pass(subdirectory)
        os.chdir(subdirectory)
        support = self.support()
        self.assertEqual(support["status"], "unverified")
        self.assertIn("inside the workspace", support["detail"])


class TestPrintCommandResume(IsolatedCase):
    """`run architect --resume --print-command`, with claude as the architect."""

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        pretend_claude_is_installed(self)
        self.write("resume.md", "revise\n")

    def print_command(self):
        return run_cli(
            "run",
            "architect",
            "--resume",
            "--resume-prompt-file",
            "resume.md",
            "--output",
            ".ai/plan.md",
            "--print-command",
        )

    def record_architect_run(self, provider="claude"):
        self.cli_workspace().record_event(
            "architect",
            "ok",
            {
                "mode": "plan",
                "provider": provider,
                "output": ".ai/plan.md",
                "answered": True,
                "session_id": PARENT_SESSION,
            },
        )

    def test_an_unverified_version_prints_a_fresh_command(self):
        pretend_claude_version(self, UNLISTED_CLAUDE)
        self.record_architect_run()
        code, out, err = self.print_command()
        self.assertEqual(code, 0)
        self.assertNotIn("--resume=", out)
        self.assertIn("(unverified)", err)
        self.assertIn("smoke_live.py", err)

    def test_a_recorded_version_prints_the_resumed_command(self):
        pretend_claude_version(self, UNLISTED_CLAUDE)
        record_resume_pass(self.project)
        self.record_architect_run()
        code, out, _ = self.print_command()
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith("--resume=%s --fork-session" % PARENT_SESSION))
        self.assertIn("--tools Read,Grep,Glob --strict-mcp-config --restricted", out)
        budgets = json.loads(run_cli("budget", "show", "--json")[1])["budgets"]
        self.assertEqual(budgets["architect"]["used"], 0)

    def test_2_1_283_resumes_from_the_built_in_table(self):
        self.record_architect_run()
        _, out, _ = self.print_command()
        self.assertIn("--resume=", out)

    def test_a_record_for_other_flags_does_not_count(self):
        pretend_claude_version(self, UNLISTED_CLAUDE)
        record_resume_pass(self.project, mechanism="--other flags")
        self.record_architect_run()
        _, out, _ = self.print_command()
        self.assertNotIn("--resume=", out)


class TestUserAdapterResume(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.write_user_provider("mycli")
        self.load_user_providers()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "architect.provider", "mycli")
        run_cli("config", "set", "architect.model.family", "default")
        self.write("resume.md", "revise\n")
        from orchestrator import providers as registry

        provider = registry.get_provider("mycli")
        cls = type(provider)
        self.addCleanup(setattr, cls, "which", cls.which)
        cls.which = lambda self: "mycli"

    def test_the_command_is_unchanged_and_the_run_goes_fresh(self):
        code, before, _ = run_cli("run", "architect", "--print-command")
        self.assertEqual(code, 0)
        code, out, err = run_cli(
            "run",
            "architect",
            "--resume",
            "--resume-prompt-file",
            "resume.md",
            "--output",
            ".ai/plan.md",
            "--print-command",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, before)
        self.assertIn("running fresh: the provider cannot resume a session", err)


FROZEN = "now lists the reviewers; the panel no longer follows preset standard's fit (recorded %s)"
#: Preset standard's panel with Claude alone installed, and as the frozen note records it.
CLAUDE_FIT = ["claude-general", "claude-general-2", "claude-security", "claude-test", "claude-security-2"]
RECORDED = (
    "claude-general opus, claude-general-2 sonnet, claude-security sonnet, claude-test sonnet, "
    "claude-security-2 opus"
)
CODEX_IMPLEMENTER = {"provider": "codex", "model": {"family": "recommended-coding", "version": "latest"}}


class PresetCase(IsolatedCase):
    def global_layer(self):
        return config_mod.read_config_file(config_mod.global_config_path())

    def write_global(self, data):
        config_mod.write_config_file(config_mod.global_config_path(), data, "global")

    def ids(self, reviewers=None):
        if reviewers is None:
            reviewers = config_mod.load(self.project).reviewers()
        return [reviewer["id"] for reviewer in reviewers]


class TestPresetWriters(PresetCase):
    """Each writer under preset standard, with Claude alone installed unless
    the test says otherwise: what reaches the file, and what is in force after."""

    def setUp(self):
        super().setUp()
        self.fake_clis(claude=True)

    def test_the_first_set_on_a_fresh_machine_keeps_the_fit(self):
        code, _, _ = run_cli("config", "set", "review.parallel", "false")
        self.assertEqual(code, 0)
        self.assertEqual(self.global_layer(), {"version": 1, "review": {"parallel": False}})
        self.assertEqual(self.ids(), CLAUDE_FIT)

    def test_reset_on_a_fresh_machine_keeps_the_fit(self):
        code, out, _ = run_cli("config", "reset", "--scope", "global")
        self.assertEqual(code, 0)
        self.assertEqual(self.global_layer(), {"version": 1})
        self.assertEqual(self.ids(), CLAUDE_FIT)
        self.assertIn("claude-general-2", out)

    def add_security(self):
        return run_cli("reviewer", "add", "--provider", "claude", "--role", "security", "--when", "high-risk")

    def test_adding_a_reviewer_keeps_the_fitted_panel(self):
        code, out, _ = self.add_security()
        self.assertEqual(code, 0)
        layer = self.global_layer()
        self.assertNotIn("reviewers", layer)
        self.assertEqual(self.ids(layer["reviewers_extra"]), ["claude-security-3"])
        self.assertIn("as an extra; the panel still follows preset standard's fit", out)
        self.assertNotIn("now lists the reviewers", out)
        self.assertEqual(self.ids(), [*CLAUDE_FIT, "claude-security-3"])

    def test_adding_a_reviewer_with_both_clis_keeps_both_vendors(self):
        self.fake_clis(claude=True, codex=True)
        self.add_security()
        self.assertNotIn("reviewers", self.global_layer())
        both = ["claude-general", "codex-general", "claude-security", "claude-test", "claude-security-2"]
        self.assertEqual(self.ids(), [*both, "claude-security-3"])

    def test_adding_a_reviewer_keeps_the_panel_dealt_around_the_files_implementer(self):
        self.fake_clis(claude=True, codex=True)
        self.write_global({"version": 1, "implementer": CODEX_IMPLEMENTER})
        in_force = self.ids()
        self.add_security()
        self.assertEqual(self.ids(self.global_layer()["reviewers_extra"]), ["claude-security-2"])
        self.assertEqual(self.ids(), [*in_force, "claude-security-2"])

    def test_removing_a_reviewer_records_the_rest(self):
        code, out, _ = run_cli("reviewer", "remove", "claude-general-2")
        self.assertEqual(code, 0)
        kept = [reviewer_id for reviewer_id in CLAUDE_FIT if reviewer_id != "claude-general-2"]
        self.assertEqual(self.ids(self.global_layer()["reviewers"]), kept)
        self.assertIn(FROZEN % RECORDED, out)

    def test_setting_a_reviewer_records_the_panel(self):
        code, out, _ = run_cli("reviewer", "set", "claude-general-2", "--role", "test")
        self.assertEqual(code, 0)
        reviewers = self.global_layer()["reviewers"]
        self.assertEqual(self.ids(reviewers), CLAUDE_FIT)
        self.assertEqual(reviewers[1]["role"], "test")
        self.assertIn(FROZEN % RECORDED, out)

    def test_an_indexed_set_records_the_panel(self):
        code, out, _ = run_cli("config", "set", "reviewers[0].role", "security")
        self.assertEqual(code, 0)
        reviewers = self.global_layer()["reviewers"]
        self.assertEqual(self.ids(reviewers), CLAUDE_FIT)
        self.assertEqual(reviewers[0]["role"], "security")
        self.assertIn(FROZEN % RECORDED, out)

    def test_a_global_edit_inside_a_project_that_lists_reviewers_says_so(self):
        project_path = os.path.join(self.project, ".dev-orchestra.yaml")
        mine = [config_mod.make_reviewer("mine", "mock", "small")]
        config_mod.write_config_file(project_path, {"version": 1, "reviewers": mine}, "project")
        code, out, _ = run_cli("reviewer", "remove", "--scope", "global", "claude-general-2")
        self.assertEqual(code, 0)
        self.assertIn(FROZEN % RECORDED, out)

    def test_a_second_edit_says_nothing_more(self):
        run_cli("reviewer", "remove", "claude-general-2")
        _, out, _ = run_cli("reviewer", "add", "--provider", "mock", "--id", "m1")
        self.assertNotIn("now lists the reviewers", out)

    def test_a_role_edit_that_leaves_the_fit_says_so(self):
        self.fake_clis(codex=True)
        path = config_mod.global_config_path()
        code, out, _ = run_cli("config", "set", "implementer.model.family", "sonnet")
        self.assertEqual(code, 0)
        self.assertIn(
            "note: implementer is now set by %s (provider claude); preset standard no longer fits it" % path,
            out,
        )
        report = json.loads(run_cli("doctor", "--fast", "--json")[1])
        self.assertIn(
            "Implementer: claude CLI is not installed; set implementer.provider to an installed CLI, "
            "or remove the role from the file so preset standard's fit applies",
            report["problems"],
        )


class TestPresetPrune(PresetCase):
    def test_what_a_preset_install_chose_is_kept(self):
        layer = {
            "version": 1,
            "preset": "quality",
            "optimization": {"level": "quality"},
            "review": {"design": {"enabled": True}},
        }
        self.write_global(layer)
        code, out, _ = run_cli("config", "prune")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.global_layer(), layer)

    def test_a_default_the_preset_sets_otherwise_is_kept(self):
        """Dropped, the level and the implementer would fall through to the fit."""
        self.fake_clis(claude=True, codex=True)
        layer = {
            "version": 1,
            "preset": "quality",
            "implementer": config_mod.default_config()["implementer"],
            "optimization": {"level": "balanced"},
        }
        self.write_global(layer)
        before = config_mod.load(self.project).data
        code, out, _ = run_cli("config", "prune")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.global_layer(), layer)
        self.assertEqual(config_mod.load(self.project).data, before)

    def test_a_default_the_preset_also_holds_is_dropped(self):
        self.write_global({"version": 1, "preset": "standard", "optimization": {"level": "balanced"}})
        code, _, _ = run_cli("config", "prune")
        self.assertEqual(code, 0)
        self.assertEqual(self.global_layer(), {"version": 1, "preset": "standard"})

    def test_a_panel_equal_to_this_machines_fit_is_not_dropped(self):
        from orchestrator import presets

        self.fake_clis(claude=True)
        fitted = presets.expand("standard", ["claude"]).values["reviewers"]
        self.write_global({"version": 1, "preset": "standard", "reviewers": fitted})
        run_cli("config", "prune")
        self.assertEqual(self.global_layer()["reviewers"], fitted)

    def test_a_default_panel_beside_a_codex_implementer_is_kept(self):
        """Dropped, the panel would be dealt around codex instead."""
        self.fake_clis(claude=True, codex=True)
        reviewers = config_mod.default_config()["reviewers"]
        layer = {"version": 1, "implementer": CODEX_IMPLEMENTER, "reviewers": reviewers}
        self.write_global(layer)
        code, _, err = run_cli("config", "prune")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.global_layer()["reviewers"], reviewers)

    def test_a_panel_a_projects_implementer_would_redeal_is_kept(self):
        self.fake_clis(claude=True, codex=True)
        reviewers = config_mod.default_config()["reviewers"]
        self.write_global({"version": 1, "preset": "standard", "reviewers": reviewers})
        project_path = os.path.join(self.project, ".dev-orchestra.yaml")
        config_mod.write_config_file(
            project_path, {"version": 1, "implementer": CODEX_IMPLEMENTER}, "project"
        )
        before = config_mod.load(self.project).data
        code, _, err = run_cli("config", "prune", "--scope", "global")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.global_layer()["reviewers"], reviewers)
        self.assertEqual(config_mod.load(self.project).data, before)


class TestPresetCommands(PresetCase):
    def test_setup_preset_writes_only_the_name(self):
        self.fake_clis(claude=True)
        code, out, _ = run_cli("config", "setup", "--preset", "fast")
        self.assertEqual(code, 0)
        self.assertEqual(self.global_layer(), {"version": 1, "preset": "fast"})
        self.assertIn("optimization level: aggressive  (optimization.level)", out)
        self.assertIn("security / claude-security (when: high-risk)", out)
        self.assertIn("note: codex not found on PATH: reviewer seat 1 (general)", out)

    def test_setup_preset_keeps_what_it_does_not_govern(self):
        self.write_global(
            {
                "version": 1,
                "review": {"parallel": False, "design": {"enabled": False}},
                "implementer": {"provider": "claude"},
                "reviewers": [config_mod.make_reviewer("mine", "mock", "small")],
                "optimization": {"level": "quality"},
            }
        )
        code, _, _ = run_cli("config", "setup", "--preset", "fast")
        self.assertEqual(code, 0)
        self.assertEqual(self.global_layer(), {"version": 1, "preset": "fast", "review": {"parallel": False}})

    def test_an_unknown_preset_is_refused_by_the_parser(self):
        with self.assertRaises(SystemExit) as caught:
            run_cli("config", "setup", "--preset", "nope")
        self.assertEqual(caught.exception.code, 2)

    def test_a_project_setup_cannot_name_a_preset(self):
        code, _, err = run_cli("config", "setup", "--preset", "fast", "--scope", "project")
        self.assertEqual(code, 2)
        self.assertIn("preset: only the global file can name a preset for now", err)
        self.assertFalse(os.path.isfile(os.path.join(self.project, ".dev-orchestra.yaml")))

    def test_setting_the_preset_is_how_to_switch(self):
        self.assertEqual(run_cli("config", "set", "preset", "quality")[0], 0)
        self.assertEqual(run_cli("config", "validate")[0], 0)
        self.assertEqual(config_mod.load(self.project).preset, "quality")

    def test_setting_the_preset_inside_a_project_writes_the_global_file(self):
        project_path = os.path.join(self.project, ".dev-orchestra.yaml")
        project = {"version": 1, "review": {"parallel": False}}
        config_mod.write_config_file(project_path, project, "project")
        code, _, err = run_cli("config", "set", "preset", "fast")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.global_layer()["preset"], "fast")
        self.assertEqual(config_mod.read_config_file(project_path), project)
        self.assertEqual(config_mod.load(self.project).preset, "fast")

    def test_setting_the_preset_in_the_project_scope_is_refused(self):
        project_path = os.path.join(self.project, ".dev-orchestra.yaml")
        project = {"version": 1, "review": {"parallel": False}}
        config_mod.write_config_file(project_path, project, "project")
        code, _, err = run_cli("config", "set", "--scope", "project", "preset", "fast")
        self.assertEqual(code, 2)
        self.assertIn("preset: only the global file can name a preset for now", err)
        self.assertEqual(config_mod.read_config_file(project_path), project)

    def test_reset_drops_an_unknown_preset(self):
        self.write_global({"version": 1, "preset": "nope"})
        code, out, _ = run_cli("config", "reset", "--scope", "global")
        self.assertEqual(code, 0)
        self.assertEqual(self.global_layer(), {"version": 1})
        self.assertIn("note: dropped the unknown preset 'nope'", out)
        self.assertIn("follows preset standard", out)
        self.assertEqual(run_cli("config", "validate")[0], 0)

    def test_setup_preset_keeps_a_roles_options_and_says_it_is_not_fitted(self):
        options = {"permission_mode": "acceptEdits"}
        self.write_global({"version": 1, "implementer": {"provider": "claude", "options": options}})
        code, out, _ = run_cli("config", "setup", "--preset", "fast")
        self.assertEqual(code, 0)
        self.assertEqual(self.global_layer()["implementer"], {"options": options})
        self.assertIn("note: implementer is set by the global file and was not fitted", out)

    def test_setting_an_unknown_preset_warns(self):
        _, _, err = run_cli("config", "set", "preset", "nope")
        self.assertIn("warning: preset: unknown 'nope' (known: fast, quality, standard)", err)

    def test_reset_keeps_the_preset_and_shows_the_result(self):
        self.write_global({"version": 1, "preset": "fast", "review": {"parallel": False}})
        code, out, _ = run_cli("config", "reset", "--scope", "global")
        self.assertEqual(code, 0)
        self.assertEqual(self.global_layer(), {"version": 1, "preset": "fast"})
        self.assertIn("follows preset fast", out)
        self.assertIn("optimization level: aggressive", out)

    def test_delete_goes_back_to_implicit_standard(self):
        self.write_global({"version": 1, "preset": "fast"})
        run_cli("config", "reset", "--scope", "global", "--delete")
        _, out, _ = run_cli("config", "show")
        self.assertIn("Preset: standard (implicit; fitted to", out)
        self.assertIn("No config file found yet -- showing preset standard fitted to the installed CLIs", out)

    def test_show_and_doctor_name_the_preset_and_the_fit(self):
        self.fake_clis(claude=True)
        self.write_global({"version": 1, "preset": "quality"})
        note = "codex not found on PATH: reviewer seat 2 (security) went to claude as claude-security (opus)"
        _, out, _ = run_cli("config", "show")
        self.assertIn("Preset: quality (global; fitted to claude)", out)
        self.assertIn("note: %s" % note, out)
        payload = json.loads(run_cli("config", "show", "--json")[1])
        self.assertEqual(payload["preset"]["name"], "quality")
        self.assertEqual(payload["preset"]["source"], "global")
        self.assertEqual(
            payload["preset"]["notes"],
            [note, "codex not found on PATH: reviewer seat 4 (test) went to claude as claude-test (opus)"],
        )
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("Preset: quality (global; fitted to claude)", out)
        self.assertIn("  - %s" % note, out)
        report = json.loads(run_cli("doctor", "--fast", "--json")[1])
        self.assertEqual(report["config"]["preset"]["name"], "quality")
        self.assertIn(note, report["notes"])

    def test_loading_with_no_file_says_one_line(self):
        from orchestrator import cli_common

        self.fake_clis(claude=True)
        err = io.StringIO()
        with redirect_stderr(err):
            cli_common._load_or_die(self.project)
        expected = (
            "note: no config file; running preset standard fitted to claude "
            "(config setup --preset <name> saves one)"
        )
        self.assertEqual(err.getvalue().splitlines(), [expected])
        self.write_global({"version": 1})
        err = io.StringIO()
        with redirect_stderr(err):
            cli_common._load_or_die(self.project)
        self.assertEqual(err.getvalue(), "")

    def test_a_new_workflow_with_no_file_shows_the_configuration_once(self):
        self.fake_clis()
        _, _, first = run_cli("run", "architect", "--prompt", "x")
        self.assertEqual(first.count("optimization level:"), 1)
        self.assertEqual(first.count("Save it with config setup --preset standard"), 1)
        self.assertEqual(first.count("note: no config file"), 1)
        _, _, second = run_cli("run", "architect", "--prompt", "x")
        self.assertEqual(second.count("note: no config file"), 1)
        self.assertNotIn("optimization level:", second)
        self.assertNotIn("Save it with", second)


def parser_leaves(parser, path=()):
    """Every runnable command under ``parser`` as (path, leaf parser)."""
    groups = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    if not groups:
        yield path, parser
        return
    for name, child in groups[0].choices.items():
        yield from parser_leaves(child, (*path, name))


def minimal_argv(path, leaf):
    """The shortest argv that ``leaf`` accepts: one value per required argument."""
    argv = list(path)
    for action in leaf._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        value = str(next(iter(action.choices))) if action.choices else "x"
        if not action.option_strings:
            if action.nargs in (None, "+"):
                argv.append(value)
        elif action.required:
            argv.extend([action.option_strings[0], value])
    return argv


class TestParserShape(IsolatedCase):
    """What ``build_parser`` registers, pinned at parse level so a command
    that drops out of the tree, or loses its handler, fails here."""

    TOP_LEVEL: ClassVar[set] = {
        "config",
        "model",
        "reviewer",
        "doctor",
        "run",
        "review",
        "design",
        "state",
        "jobs",
        "budget",
        "tokens",
        "optimization",
        "progress",
        "workflow",
        "status",
        "summary",
    }

    #: The commands that take ``--json``, as registered today.
    JSON_COMMANDS: ClassVar[set] = {
        "budget show",
        "config show",
        "config suggest-roles",
        "config validate",
        "design approve",
        "doctor",
        "jobs list",
        "jobs show",
        "jobs wait",
        "model list",
        "optimization report",
        "progress record",
        "review consolidate",
        "review run",
        "review show",
        "review snapshot",
        "review status",
        "reviewer list",
        "run",
        "state show",
        "status",
        "summary",
        "tokens show",
        "workflow list",
        "workflow show",
    }

    def setUp(self):
        super().setUp()
        self.parser = cli.build_parser()

    def parse(self, *argv):
        return self.parser.parse_args(list(argv))

    def test_json_flag(self):
        found = set()
        for path, leaf in parser_leaves(self.parser):
            if "--json" not in leaf._option_string_actions:
                continue
            name = " ".join(path)
            found.add(name)
            with self.subTest(command=name):
                argv = minimal_argv(path, leaf)
                self.assertIs(self.parse(*argv).json, False)
                self.assertIs(self.parse(*argv, "--json").json, True)
        self.assertEqual(found, self.JSON_COMMANDS)

    def assert_usage_error(self, *argv):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.parse(*argv)
        self.assertEqual(caught.exception.code, 2, argv)

    def test_top_level_commands(self):
        groups = [a for a in self.parser._actions if isinstance(a, argparse._SubParsersAction)]
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0].choices), self.TOP_LEVEL)

    def test_every_leaf_parses_to_its_handler(self):
        leaves = list(parser_leaves(self.parser))
        self.assertTrue(
            {
                ("workflow", "use"),
                ("workflow", "remove"),
                ("budget", "consume"),
                ("budget", "reset"),
                ("progress", "record"),
                ("state", "record"),
                ("review", "triage"),
                ("review", "fix-brief"),
                ("review", "consolidate"),
                ("config", "suggest-roles"),
                ("config", "prune"),
                ("config", "setup"),
            }
            <= {path for path, _ in leaves}
        )
        for path, leaf in leaves:
            with self.subTest(command=" ".join(path)):
                func = leaf.get_default("func")
                self.assertIsNotNone(func)
                self.assertEqual(func.__name__, "cmd_" + "_".join(path).replace("-", "_"))
                self.assertIs(self.parse(*minimal_argv(path, leaf)).func, func)

    def test_activity_flags_on_jobs_show_and_wait(self):
        for sub in ("show", "wait"):
            with self.subTest(sub=sub):
                args = self.parse("jobs", sub, "J")
                self.assertEqual((args.since, args.activity), (0, 10))
                args = self.parse(
                    "jobs",
                    sub,
                    "J",
                    "--since",
                    str(activity_mod.SINCE_MAX),
                    "--activity",
                    str(activity_mod.LATEST_MAX),
                )
                self.assertEqual(
                    (args.since, args.activity), (activity_mod.SINCE_MAX, activity_mod.LATEST_MAX)
                )
                args = self.parse("jobs", sub, "J", "--since", "0", "--activity", "0")
                self.assertEqual((args.since, args.activity), (0, 0))
                for flag, bad in (
                    ("--since", "-1"),
                    ("--since", str(activity_mod.SINCE_MAX + 1)),
                    ("--since", "abc"),
                    ("--activity", "-1"),
                    ("--activity", str(activity_mod.LATEST_MAX + 1)),
                    ("--activity", "abc"),
                ):
                    self.assert_usage_error("jobs", sub, "J", "%s=%s" % (flag, bad))

    def test_scope_defaults(self):
        self.assertEqual(self.parse("config", "setup").scope, "global")
        self.assertEqual(self.parse("config", "show").scope, "effective")
        self.assertEqual(self.parse("config", "show", "--scope", "effective").scope, "effective")
        self.assert_usage_error("config", "setup", "--scope", "effective")
        for argv in (
            ("config", "set", "a.b", "c"),
            ("config", "reset"),
            ("config", "prune"),
            ("reviewer", "add", "--provider", providers.available_providers()[0]),
            ("reviewer", "remove", "1"),
            ("reviewer", "set", "1"),
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(self.parse(*argv).scope)

    def test_design_flag_on_review_subcommands(self):
        for argv in (
            ("review", "run"),
            ("review", "consolidate"),
            ("review", "show"),
            ("review", "triage", "F1", "--status", "accepted"),
            ("review", "fix-brief"),
            ("review", "status"),
        ):
            with self.subTest(argv=argv):
                self.assertIs(self.parse(*argv).design, False)
                self.assertIs(self.parse(*argv, "--design").design, True)
        self.assert_usage_error("review", "snapshot", "--design")


if __name__ == "__main__":
    unittest.main()
