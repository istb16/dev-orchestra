"""The ``config`` and ``reviewer`` commands and presets, end to end."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

from helpers import IsolatedCase, read_file, run_cli

from orchestrator import config as config_mod
from orchestrator import config_paths
from orchestrator import workspace as ws


class TestConfigCommands(IsolatedCase):
    def test_show_without_a_config_uses_defaults(self):
        code, out, _ = run_cli("config", "show")
        self.assertEqual(code, 0)
        self.assertIn("showing preset standard fitted to the installed CLIs", out)
        self.assertIn("claude / sonnet / latest", out)

    def test_config_show_lists_run_deadlines_with_source(self):
        run_cli("config", "set", "run.timeout_seconds.architect", "900", "--scope", "global")
        run_cli("config", "set", "review.timeout_seconds", "600", "--scope", "global")
        self.write(".dev-orchestra.yaml", "version: 1\nrun:\n  timeout_seconds:\n    implementer: 5400\n")
        code, out, err = run_cli("config", "show")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        start = lines.index("  Deadlines")
        self.assertEqual(
            lines[start + 1 : start + 6],
            [
                "    Orchestrator: 1800s (default)  (run.timeout_seconds.orchestrator)",
                "    Architect: 900s (global)  (run.timeout_seconds.architect)",
                "    Implementer: 5400s (project)  (run.timeout_seconds.implementer)",
                "    Review Fixer: 1800s (default)  (run.timeout_seconds.review_fixer)",
                "    Reviewers: 600s (global)  (review.timeout_seconds)",
            ],
        )
        # A preview names no file, so it says no source.
        from orchestrator import summary as summary_mod

        preview = summary_mod.render_summary(config_mod.default_config())
        self.assertIn("    Implementer: 3600s  (run.timeout_seconds.implementer)", preview)

    def test_config_show_reads_a_broken_review_deadline_as_the_default(self):
        """``config show`` loads unvalidated: a bad ``review`` must show, not crash the summary."""
        from orchestrator import summary as summary_mod

        for review in (5, [1, 2], {"timeout_seconds": "abc"}, {"timeout_seconds": 0}):
            with self.subTest(review=review):
                data = config_mod.default_config()
                data["review"] = review
                preview = summary_mod.render_summary(data)
                self.assertIn("    Reviewers: 1800s  (review.timeout_seconds)", preview)

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
        self.assertIn("plan approval: auto", out)
        run_cli("config", "set", "design.require_approval", "true")
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
                with mock.patch.object(config_paths, "real_location", lambda path: path):
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
            ["claude-general", "claude-security", "claude-test"],
        )

    def test_remove_unknown_reviewer_fails_cleanly(self):
        code, _, err = run_cli("reviewer", "remove", "ghost")
        self.assertEqual(code, 2)
        self.assertIn("no reviewer matches", err)

    def test_removing_every_reviewer_is_allowed(self):
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "remove", "claude-security")
        run_cli("reviewer", "remove", "claude-test")
        _, out, _ = run_cli("reviewer", "list")
        self.assertIn("No reviewers configured", out)

    def test_set_role_drops_the_relevance_rule_with_a_note(self):
        run_cli("reviewer", "add", "--provider", "codex", "--role", "security", "--relevance", "security")
        code, out, _ = run_cli("reviewer", "set", "codex-security", "--role", "performance")
        self.assertEqual(code, 0)
        self.assertIn(
            "note: role is now performance; its relevance security was removed "
            "(reviewer set --relevance sets another)",
            out,
        )
        _, listing, _ = run_cli("reviewer", "list", "--json")
        entry = next(r for r in json.loads(listing) if r["id"] == "codex-security")
        self.assertNotIn("relevance", entry)

    def test_set_same_role_keeps_the_relevance_rule(self):
        run_cli("reviewer", "add", "--provider", "codex", "--role", "security", "--relevance", "security")
        code, out, _ = run_cli("reviewer", "set", "codex-security", "--role", "security")
        self.assertEqual(code, 0)
        self.assertNotIn("was removed", out)
        _, listing, _ = run_cli("reviewer", "list", "--json")
        entry = next(r for r in json.loads(listing) if r["id"] == "codex-security")
        self.assertEqual(entry["relevance"], "security")

    def test_set_role_with_relevance_takes_the_new_rule(self):
        run_cli("reviewer", "add", "--provider", "codex", "--role", "security", "--relevance", "security")
        code, out, _ = run_cli(
            "reviewer", "set", "codex-security", "--role", "test", "--relevance", "architecture"
        )
        self.assertEqual(code, 0)
        self.assertNotIn("was removed", out)
        _, listing, _ = run_cli("reviewer", "list", "--json")
        entry = next(r for r in json.loads(listing) if r["id"] == "codex-security")
        self.assertEqual(entry["relevance"], "architecture")

    def test_set_role_keeps_relevance_always(self):
        run_cli("reviewer", "add", "--provider", "codex", "--role", "security", "--relevance", "always")
        code, out, _ = run_cli("reviewer", "set", "codex-security", "--role", "test")
        self.assertEqual(code, 0)
        self.assertNotIn("was removed", out)
        _, listing, _ = run_cli("reviewer", "list", "--json")
        entry = next(r for r in json.loads(listing) if r["id"] == "codex-security")
        self.assertEqual(entry["relevance"], "always")

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


FROZEN = "now lists the reviewers; the panel no longer follows preset standard's fit (recorded %s)"
#: Preset standard's panel with Claude alone installed, and as the frozen note records it.
CLAUDE_FIT = ["claude-general", "claude-general-2", "claude-security", "claude-test"]
RECORDED = "claude-general opus, claude-general-2 sonnet, claude-security sonnet, claude-test sonnet"
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
        self.assertEqual(self.ids(layer["reviewers_extra"]), ["claude-security-2"])
        self.assertIn("as an extra; the panel still follows preset standard's fit", out)
        self.assertNotIn("now lists the reviewers", out)
        self.assertEqual(self.ids(), [*CLAUDE_FIT, "claude-security-2"])

    def test_adding_a_reviewer_with_both_clis_keeps_both_vendors(self):
        self.fake_clis(claude=True, codex=True)
        self.add_security()
        self.assertNotIn("reviewers", self.global_layer())
        both = ["claude-general", "codex-general", "claude-security", "claude-test"]
        self.assertEqual(self.ids(), [*both, "claude-security-2"])

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
        # The list takes the design rounds off the fit's design panel too.
        self.assertIn("design rounds now run this list without when, not preset standard's design panel", out)

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

    def test_setup_preset_replaces_design_reviewers(self):
        extra = config_mod.make_reviewer("x", "mock", "small")
        listed = config_mod.make_reviewer("mine", "mock", "small")
        self.write_global(
            {"version": 1, "review": {"design": {"reviewers": [listed], "reviewers_extra": [extra]}}}
        )
        code, _, err = run_cli("config", "setup", "--preset", "quality")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            self.global_layer(),
            {"version": 1, "preset": "quality", "review": {"design": {"reviewers_extra": [extra]}}},
        )
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.design_panel_source, "fit")
        self.assertEqual(loaded.design_reviewers()[-1]["id"], "x")

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
        why = "was not added; it is a second vendor's opinion and nothing installed stands in for one"
        note = "codex not found on PATH: reviewer seat 2 (general) %s" % why
        _, out, _ = run_cli("config", "show")
        self.assertIn("Preset: quality (global; fitted to claude)", out)
        self.assertIn("note: %s" % note, out)
        payload = json.loads(run_cli("config", "show", "--json")[1])
        self.assertEqual(payload["preset"]["name"], "quality")
        self.assertEqual(payload["preset"]["source"], "global")
        self.assertEqual(
            payload["preset"]["notes"],
            [
                note,
                "codex not found on PATH: reviewer seat 4 (security) %s" % why,
                "codex not found on PATH: design reviewer seat 2 (general) %s" % why,
            ],
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


if __name__ == "__main__":
    unittest.main()
