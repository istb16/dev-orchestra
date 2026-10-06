"""Configuration layering, validation and reviewer management."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from typing import Any, ClassVar, Dict
from unittest import mock

from helpers import IsolatedCase, make_dir_link, present, remove_link

from orchestrator import config as config_mod
from orchestrator import config_policy as policy_mod
from orchestrator import providers, summary

DESIGN_DEFAULTS = {
    "require_approval": True,
    "resume": {"max_age_seconds": 3600, "max_context_tokens": None},
}

#: Preset standard's panel with Claude alone installed.
CLAUDE_FIT = ["claude-general", "claude-general-2", "claude-security", "claude-test"]


class TestDefaults(IsolatedCase):
    def test_no_config_falls_back_to_builtin_defaults(self):
        loaded = config_mod.load(self.project)
        self.assertFalse(loaded.exists)
        self.assertTrue(loaded.used_defaults)
        self.assertEqual(loaded.role("orchestrator")["model"]["family"], "sonnet")
        self.assertEqual(loaded.role("architect")["model"]["family"], "fable")
        self.assertEqual(loaded.role("implementer")["model"]["family"], "opus")
        self.assertEqual(loaded.role("review_fixer")["model"]["family"], "opus")
        self.assertEqual(len(loaded.reviewers()), 4)

    def test_defaults_never_pin_a_dated_model_id(self):
        from orchestrator import presets

        text = str(config_mod.default_config())
        self.assertNotIn("id", config_mod.default_config()["implementer"]["model"])
        for name in presets.NAMES:
            for installed in ([], ["claude"], ["codex"], ["claude", "codex"]):
                values = presets.expand(name, installed).values
                text += str(values)
                specs = [values[role] for role in config_mod.KNOWN_ROLES]
                specs.extend(values["reviewers"])
                for spec in specs:
                    self.assertNotIn("id", spec["model"], (name, installed))
                    self.assertEqual(spec["model"]["version"], "latest")
        for token in ("2026", "2025", "-2024"):
            self.assertNotIn(token, text)

    def test_defaults_are_valid(self):
        self.assertEqual(config_mod.validate(config_mod.default_config()), [])

    def test_the_design_review_is_auto_by_default(self):
        """A round costs a reviewer run per panel member plus an architect
        re-run, so by default only a risky or large plan pays for one."""
        design = config_mod.default_config()["review"]["design"]
        self.assertEqual(design, {"enabled": "auto", "max_iterations": 2})

    def test_design_settings_are_filled_in_for_a_config_that_omits_them(self):
        self.write(".dev-orchestra.yaml", "version: 1\nreview:\n  max_review_iterations: 1\n")
        loaded = config_mod.load(self.project)
        settings = loaded.design_review_settings()
        # The fitted design panel rides along under the same key.
        settings.pop("reviewers")
        self.assertEqual(settings, {"enabled": "auto", "max_iterations": 2})

    def test_the_design_review_mode_keeps_the_old_truth_test(self):
        """Null is the default; anything else not `auto` reads as the truth
        test every reader applied before `auto` existed."""
        cases = [
            (True, "on"),
            (False, "off"),
            ("auto", "auto"),
            (" Auto", "auto"),
            ("AUTO ", "auto"),
            (None, "auto"),
            ("true", "on"),
            ("yes", "on"),
            (1, "on"),
            (0, "off"),
            ("", "off"),
        ]
        for value, mode in cases:
            with self.subTest(value=value):
                self.assertEqual(config_mod.design_review_mode(value), mode)

    def test_plan_approval_is_required_by_default(self):
        self.assertIs(config_mod.default_config()["design"]["require_approval"], True)

    def test_the_approval_setting_is_filled_in_for_a_config_that_omits_it(self):
        self.write(".dev-orchestra.yaml", "version: 1\n")
        self.assertEqual(config_mod.load(self.project).design_settings(), DESIGN_DEFAULTS)

    def test_an_empty_approval_setting_means_the_default(self):
        """`require_approval:` with no value parses as null; it must not turn the gate off."""
        self.write(".dev-orchestra.yaml", "version: 1\ndesign:\n  require_approval:\n")
        loaded = config_mod.load(self.project)
        self.assertEqual(config_mod.validate(loaded.data), [])
        self.assertEqual(loaded.design_settings(), DESIGN_DEFAULTS)
        self.assertTrue(summary._approval_required({"design": {"require_approval": None}}))
        self.assertFalse(summary._approval_required({"design": {"require_approval": False}}))

    def test_the_approval_setting_must_be_a_boolean(self):
        data = config_mod.default_config()
        data["design"]["require_approval"] = "yes"
        self.assertIn("design.require_approval: must be true or false", config_mod.validate(data))
        data["design"] = 3
        self.assertIn("design: must be a mapping", config_mod.validate(data))

    def test_resuming_the_architect_has_an_age_limit_and_no_context_cap(self):
        self.assertEqual(
            config_mod.default_config()["design"]["resume"],
            {"max_age_seconds": 3600, "max_context_tokens": None},
        )

    def test_the_resume_limits_must_be_integers(self):
        data = config_mod.default_config()
        age_problem = "design.resume.max_age_seconds: must be a non-negative integer"
        for age in ("1h", -1, True):
            data["design"]["resume"] = {"max_age_seconds": age}
            with self.subTest(max_age_seconds=age):
                self.assertIn(age_problem, config_mod.validate(data))
        cap_problem = "design.resume.max_context_tokens: must be a positive integer or null"
        for cap in ("big", 0, False):
            data["design"]["resume"] = {"max_context_tokens": cap}
            with self.subTest(max_context_tokens=cap):
                self.assertIn(cap_problem, config_mod.validate(data))
        data["design"]["resume"] = 3
        self.assertIn("design.resume: must be a mapping", config_mod.validate(data))

    def test_one_resume_limit_keeps_the_other_default(self):
        self.write(".dev-orchestra.yaml", "version: 1\ndesign:\n  resume:\n    max_context_tokens: 50000\n")
        loaded = config_mod.load(self.project)
        self.assertEqual(config_mod.validate(loaded.data), [])
        self.assertEqual(
            loaded.design_settings()["resume"], {"max_age_seconds": 3600, "max_context_tokens": 50000}
        )

    def test_the_approval_setting_alone_keeps_the_resume_defaults(self):
        self.write(".dev-orchestra.yaml", "version: 1\ndesign:\n  require_approval: false\n")
        settings = config_mod.load(self.project).design_settings()
        self.assertIs(settings["require_approval"], False)
        self.assertEqual(settings["resume"], DESIGN_DEFAULTS["resume"])

    def test_the_stale_notice_threshold_must_be_a_non_negative_integer(self):
        problem = "workspace.stale_notice_days: must be a non-negative integer (0 = off)"
        for value in (-1, "30", True):
            data = config_mod.default_config()
            data["workspace"]["stale_notice_days"] = value
            with self.subTest(stale_notice_days=value):
                self.assertIn(problem, config_mod.validate(data))
        for value in (0, 30, 36500, None):
            data = config_mod.default_config()
            data["workspace"]["stale_notice_days"] = value
            with self.subTest(stale_notice_days=value):
                self.assertEqual([p for p in config_mod.validate(data) if p.startswith("workspace.")], [])

    def test_the_stale_notice_threshold_has_a_ceiling(self):
        """Commands load unvalidated, so an unbounded number would reach all of them."""
        self.assertEqual(config_mod.STALE_NOTICE_MAX_DAYS, 36500)
        problem = "workspace.stale_notice_days: must be 36500 or less (100 years)"
        for value in (36501, 10**9):
            data = config_mod.default_config()
            data["workspace"]["stale_notice_days"] = value
            with self.subTest(stale_notice_days=value):
                self.assertIn(problem, config_mod.validate(data))
        data = config_mod.default_config()
        data["workspace"]["stale_notice_days"] = 36500
        self.assertNotIn(problem, config_mod.validate(data))

    def test_a_null_workspace_block_is_absent(self):
        """`workspace:` with no value validated before the setting existed, and still does."""
        self.write(".dev-orchestra.yaml", "version: 1\nworkspace:\n")
        loaded = config_mod.load(self.project, validate_result=False)
        self.assertEqual([p for p in config_mod.validate(loaded.data) if p.startswith("workspace")], [])
        self.assertTrue(loaded.workspace_dir(self.project).endswith(".ai"))
        default = config_mod.default_config()["workspace"]["stale_notice_days"]
        self.assertEqual(loaded.stale_notice_days(), default)
        data = config_mod.default_config()
        data["workspace"] = 3
        self.assertIn("workspace: must be a mapping", config_mod.validate(data))

    def test_the_stale_notice_defaults_to_thirty_days(self):
        default = config_mod.default_config()["workspace"]["stale_notice_days"]
        self.assertEqual(default, 30)
        self.assertEqual(config_mod.load(self.project).stale_notice_days(), default)
        for value in ("null", '"30"', "-1", "36501"):
            self.write(".dev-orchestra.yaml", "version: 1\nworkspace:\n  stale_notice_days: %s\n" % value)
            loaded = config_mod.load(self.project, validate_result=False)
            with self.subTest(stale_notice_days=value):
                self.assertEqual(loaded.stale_notice_days(), default)
        for value, expected in (("0", 0), ("36500", 36500)):
            self.write(".dev-orchestra.yaml", "version: 1\nworkspace:\n  stale_notice_days: %s\n" % value)
            with self.subTest(stale_notice_days=value):
                self.assertEqual(config_mod.load(self.project).stale_notice_days(), expected)

    def test_the_context_budget_refuses_nothing_anyone_has_recorded(self):
        """400,000 chars is four times the largest prompt this repository has
        recorded, so shipping it changes no existing workflow."""
        context = config_mod.default_config()["review"]["context"]
        self.assertEqual(
            {key: context[key] for key in ("max_chars", "inline_chars")},
            {"max_chars": 400_000, "inline_chars": 400_000},
        )

    def test_surrounding_context_ships_off_with_a_cap_that_did_not_raise_cost(self):
        """Off by default; 15,000 is the cap measured to leave the cost per run unchanged."""
        context = config_mod.default_config()["review"]["context"]
        self.assertEqual(context["surrounding"], "none")
        self.assertEqual(context["surrounding_chars"], 15_000)

    def test_the_surrounding_mode_accepts_none_enclosing_null_and_false(self):
        for value in ("none", "enclosing", "Enclosing", None, False):
            data = config_mod.default_config()
            data["review"]["context"]["surrounding"] = value
            problems = [p for p in config_mod.validate(data) if "review.context" in p]
            self.assertEqual(problems, [], value)

    def test_the_surrounding_mode_refuses_true_and_unknown_values(self):
        for value in (True, "window", 3):
            data = config_mod.default_config()
            data["review"]["context"]["surrounding"] = value
            self.assertIn(
                "review.context.surrounding: must be one of enclosing, none",
                config_mod.validate(data),
                value,
            )

    def test_a_surrounding_cap_of_zero_is_rejected(self):
        data = config_mod.default_config()
        data["review"]["context"]["surrounding_chars"] = 0
        self.assertTrue(any("review.context.surrounding_chars" in p for p in config_mod.validate(data)))
        data["review"]["context"]["surrounding_chars"] = None
        self.assertFalse(any("review.context.surrounding_chars" in p for p in config_mod.validate(data)))

    def test_the_two_limits_ship_equal_so_there_is_one_boundary(self):
        """At or under it the round runs and is complete, over it the round is
        refused, and a forced round is partial. Nothing in between."""
        context = config_mod.default_config()["review"]["context"]
        self.assertEqual(context["inline_chars"], context["max_chars"])

    def test_context_settings_are_filled_in_for_a_config_that_omits_them(self):
        self.write(".dev-orchestra.yaml", "version: 1\nreview:\n  max_review_iterations: 1\n")
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.context_settings()["max_chars"], 400_000)
        self.assertEqual(loaded.context_settings()["inline_chars"], 400_000)

    def test_an_explicit_null_budget_means_the_default_not_no_limit(self):
        """`null` means "use the default" here because that is what it means
        on `review.max_findings` next door. What it must not mean is "no
        limit": that reading is for a config written before the setting
        existed, and a file naming the key is not one."""
        self.write(".dev-orchestra.yaml", "version: 1\nreview:\n  context:\n    max_chars: null\n")
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.context_settings()["max_chars"], 400_000)

    def test_an_explicit_null_inline_limit_means_the_default_too(self):
        self.write(".dev-orchestra.yaml", "version: 1\nreview:\n  context:\n    inline_chars: null\n")
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.context_settings()["inline_chars"], 400_000)

    def test_naming_one_context_limit_keeps_the_other(self):
        self.write(".dev-orchestra.yaml", "version: 1\nreview:\n  context:\n    inline_chars: 50000\n")
        settings = config_mod.load(self.project).context_settings()
        self.assertEqual(settings["inline_chars"], 50_000)
        self.assertEqual(settings["max_chars"], 400_000)

    def test_naming_one_design_setting_keeps_the_other(self):
        """`review_settings` updates shallowly, which would drop
        `max_iterations` and refuse the first round."""
        self.write(".dev-orchestra.yaml", "version: 1\nreview:\n  design:\n    enabled: true\n")
        settings = config_mod.load(self.project).design_review_settings()
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["max_iterations"], 2)


class TestLayering(IsolatedCase):
    def test_global_config_is_used(self):
        path = config_mod.global_config_path()
        data = config_mod.default_config()
        data["implementer"]["model"]["family"] = "sonnet"
        config_mod.write_config_file(path, data)

        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.global_path, path)
        self.assertEqual(loaded.role("implementer")["model"]["family"], "sonnet")

    def test_project_override_wins_over_global(self):
        config_mod.write_config_file(config_mod.global_config_path(), config_mod.default_config())
        self.write(
            ".dev-orchestra.yaml",
            "version: 1\nimplementer:\n  provider: codex\n"
            "  model:\n    family: recommended-coding\n    version: latest\n",
        )
        loaded = config_mod.load(self.project)
        self.assertIsNotNone(loaded.project_path)
        self.assertEqual(loaded.role("implementer")["provider"], "codex")
        # Untouched roles still come from the global layer.
        self.assertEqual(loaded.role("architect")["provider"], "claude")

    def test_project_override_replaces_the_whole_reviewer_list(self):
        self.write(
            ".dev-orchestra.yaml",
            "version: 1\nreviewers:\n  - id: only-one\n    provider: mock\n    role: security\n",
        )
        loaded = config_mod.load(self.project)
        self.assertEqual([r["id"] for r in loaded.reviewers()], ["only-one"])

    def test_a_file_saved_with_a_bom_keeps_its_first_key(self):
        """Notepad and PowerShell 5 start a UTF-8 file with a BOM (#276)."""
        for name, text in (
            (".dev-orchestra.yaml", "review:\n  max_review_iterations: 5\nversion: 1\n"),
            (".dev-orchestra.json", '{"review": {"max_review_iterations": 5}, "version": 1}\n'),
        ):
            with self.subTest(name=name):
                path = os.path.join(self.project, name)
                with open(path, "wb") as handle:
                    handle.write(b"\xef\xbb\xbf" + text.encode("utf-8"))
                try:
                    self.assertEqual(
                        config_mod.read_config_file(path),
                        {"review": {"max_review_iterations": 5}, "version": 1},
                    )
                    loaded = config_mod.load(self.project)
                    self.assertEqual(loaded.review_settings()["max_review_iterations"], 5)
                finally:
                    os.remove(path)

    def test_project_config_is_found_from_a_subdirectory(self):
        self.write(".dev-orchestra.yaml", "version: 1\n")
        nested = os.path.join(self.project, "a", "b")
        os.makedirs(nested)
        self.assertEqual(os.path.dirname(present(config_mod.find_project_config(nested))), self.project)

    def test_search_stops_at_the_git_root(self):
        self.init_git_repo()
        outside = os.path.join(self.tmp, ".dev-orchestra.yaml")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("version: 1\n")
        self.assertIsNone(config_mod.find_project_config(self.project))

    def test_search_stops_at_a_worktree_or_submodule_root(self):
        """There `.git` is a file, not a directory, and it is the root all the same."""
        with open(os.path.join(self.project, ".git"), "w", encoding="utf-8") as handle:
            handle.write("gitdir: /elsewhere/.git/worktrees/project\n")
        outside = os.path.join(self.tmp, ".dev-orchestra.yaml")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("version: 1\n")
        nested = os.path.join(self.project, "src")
        os.makedirs(nested)
        self.assertIsNone(config_mod.find_project_config(nested))
        self.write(".dev-orchestra.yaml", "version: 1\n")
        self.assertEqual(os.path.dirname(present(config_mod.find_project_config(nested))), self.project)
        # The same walk names the repository root, which the reply-language hooks use.
        self.assertEqual(config_mod.repository_root(nested), self.project)

    def test_repository_root_is_none_outside_a_repository(self):
        nested = os.path.join(self.project, "src")
        os.makedirs(nested)
        found = config_mod.repository_root(nested)
        # The temporary directory may itself sit inside a checkout; never below it.
        self.assertTrue(found is None or not found.startswith(self.project), found)

    def test_search_stops_at_a_dangling_git_symlink(self):
        """Even a `.git` link that points nowhere marks the root."""
        try:
            os.symlink(os.path.join(self.tmp, "missing-git-dir"), os.path.join(self.project, ".git"))
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symlinks are unavailable here: %s" % exc)
        outside = os.path.join(self.tmp, ".dev-orchestra.yaml")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("version: 1\n")
        nested = os.path.join(self.project, "src")
        os.makedirs(nested)
        self.assertIsNone(config_mod.find_project_config(nested))

    def test_empty_config_file_is_tolerated(self):
        self.write(".dev-orchestra.yaml", "")
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.role("orchestrator")["provider"], "claude")


class TestReadOnlyRawArgs(IsolatedCase):
    """Where a read-only run's `options.args` came from, and what is said about it."""

    def write_global(self, text):
        path = config_mod.global_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("version: 1\n" + text)

    def write_project(self, text):
        self.write(".dev-orchestra.yaml", "version: 1\n" + text)

    def entries(self, loaded):
        return {entry.label: entry for entry in policy_mod.read_only_raw_args(loaded)}

    def test_layer_of_names_the_file_that_set_the_key(self):
        self.write_global('architect:\n  options:\n    args: ["--add-dir", "g"]\n')
        self.write_project('orchestrator:\n  options:\n    args: ["--add-dir", "p"]\n')
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.layer_of("orchestrator.options.args"), "project")
        self.assertEqual(loaded.layer_of("architect.options.args"), "global")
        self.assertEqual(loaded.layer_of("implementer.options.args"), "default")

    def test_a_project_mapping_without_args_leaves_them_global(self):
        """Mappings merge key by key, so the args are still the global file's."""
        self.write_global('architect:\n  options:\n    args: ["--add-dir", "g"]\n')
        self.write_project("architect:\n  options:\n    permission_mode: plan\n")
        loaded = config_mod.load(self.project, validate_result=False)
        self.assertEqual(loaded.layer_of("architect.options.args"), "global")
        self.assertEqual(self.entries(loaded)["architect"].args, ["--add-dir", "g"])

    def test_reviewer_paths_are_understood(self):
        self.write_project(
            'reviewers:\n  - id: mine\n    provider: mock\n    options:\n      args: ["--add-dir", "x"]\n'
        )
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.layer_of("reviewers[0].options.args"), "project")
        entry = self.entries(loaded)["reviewers[0]"]
        self.assertEqual(
            (entry.reviewer_id, entry.display, entry.layer), ("mine", "reviewer mine", "project")
        )

    def test_a_tier_with_options_owns_its_args(self):
        self.write_global('architect:\n  options:\n    args: ["--add-dir", "g"]\n')
        self.write_project(
            "architect:\n  model_tiers:\n"
            '    light:\n      options:\n        args: ["--add-dir", "p"]\n'
            "    other:\n      model:\n        family: sonnet\n"
        )
        entries = self.entries(config_mod.load(self.project))
        self.assertEqual(entries["architect.model_tiers.light"].layer, "project")
        self.assertEqual(entries["architect.model_tiers.light"].display, "architect (tier light)")
        # No options of its own: it inherits the role's, and their layer.
        self.assertEqual(entries["architect.model_tiers.other"].layer, "global")
        self.assertEqual(entries["architect.model_tiers.other"].args, ["--add-dir", "g"])
        self.assertEqual(entries["architect"].layer, "global")

    def test_a_tier_name_with_a_dot_is_still_the_projects(self):
        """The name is one key, not a path: splitting it would label the args ``default``."""
        self.write_project(
            'architect:\n  model_tiers:\n    light.v2:\n      options:\n        args: ["--add-dir", "p"]\n'
        )
        loaded = config_mod.load(self.project)
        label = "architect.model_tiers.light.v2"
        self.assertEqual(self.entries(loaded)[label].layer, "project")
        self.assertEqual(list(policy_mod.project_raw_arg_refusals(loaded)), [label])

    def test_project_args_are_refused_whatever_they_are(self):
        self.write_project(
            'architect:\n  options:\n    args: ["--add-dir", "../x"]\n'
            'implementer:\n  options:\n    args: ["--tools", "default"]\n'
            'reviewers:\n  - id: mine\n    provider: claude\n    options:\n      args: ["--add-dir", "x"]\n'
        )
        loaded = config_mod.load(self.project)
        refusals = policy_mod.project_raw_arg_refusals(loaded)
        self.assertEqual(sorted(refusals), ["architect", "reviewers[0]"])
        self.assertIn("set in the project config (.dev-orchestra.yaml)", refusals["architect"])
        self.assertNotIn("../x", refusals["architect"])
        self.assertEqual(list(policy_mod.reviewer_raw_arg_refusals(loaded)), ["mine"])

    def test_global_args_are_allowed_when_the_adapter_accepts_them(self):
        self.write_global('architect:\n  options:\n    args: ["--add-dir", "../x"]\n')
        loaded = config_mod.load(self.project)
        self.assertEqual(policy_mod.project_raw_arg_refusals(loaded), {})
        self.assertEqual(policy_mod.read_only_arg_warnings(loaded), [])

    def test_warnings_cover_both_layers_and_skip_implement_roles(self):
        self.write_global(
            'architect:\n  options:\n    args: ["--permission-mode", "acceptEdits"]\n'
            'implementer:\n  options:\n    args: ["--tools", "default"]\n'
        )
        self.write_project('orchestrator:\n  options:\n    args: ["--add-dir", "x"]\n')
        warnings = policy_mod.read_only_arg_warnings(config_mod.load(self.project))
        self.assertEqual(len(warnings), 3, warnings)
        self.assertTrue(warnings[0].startswith("orchestrator: options.args is set in the project config"))
        expected = "architect: read-only run: '--permission-mode' (token 1 of 2 in options.args)"
        self.assertIn(expected, warnings[1])
        self.assertNotIn("acceptEdits", " ".join(warnings))
        self.assertFalse(any(w.startswith("implementer") for w in warnings))

    def test_warnings_come_in_a_fixed_order(self):
        """Seat-walk refusals, then write refusals, then each global entry's problems."""
        self.write_global('architect:\n  options:\n    args: ["--permission-mode", "acceptEdits"]\n')
        self.write_project(
            'orchestrator:\n  options:\n    args: ["--add-dir", "x"]\n'
            "implementer:\n  provider: claude\n  model:\n    family: opus\n"
            "  options:\n    permission_mode: acceptEdits\n"
        )
        warnings = policy_mod.read_only_arg_warnings(config_mod.load(self.project))
        # The global args are two tokens, so the architect has two problems.
        raw = (
            "architect: read-only run: %s (token %d of 2 in options.args) is not accepted; "
            "read-only claude runs accept only --add-dir <path>"
        )
        self.assertEqual(
            warnings,
            [
                "orchestrator: options.args is set in the project config (.dev-orchestra.yaml); read-only "
                "roles take raw arguments only from the global config or from --extra",
                "implementer: options.permission_mode / options.args is set in the project config "
                "(.dev-orchestra.yaml); on claude the permission bypass and raw arguments are taken only "
                "from the global config or from --extra",
                raw % ("'--permission-mode'", 1),
                raw % ("a bare value", 2),
            ],
        )

    def test_the_write_refusal_asks_the_policys_provider_lookup(self):
        """A test replaces ``config_policy.get_provider``, as it does
        ``review_fanout.get_provider``, and the refusal follows it."""
        self.write_project(
            "implementer:\n  provider: claude\n  model:\n    family: opus\n"
            "  options:\n    permission_mode: acceptEdits\n"
        )
        loaded = config_mod.load(self.project)
        self.assertEqual(list(policy_mod.project_write_refusals(loaded)), ["implementer"])
        with mock.patch.object(
            policy_mod, "get_provider", return_value=SimpleNamespace(local_only_options=())
        ):
            self.assertEqual(policy_mod.project_write_refusals(loaded), {})

    def test_none_of_this_makes_load_raise_or_validate_complain(self):
        self.write_global('architect:\n  options:\n    args: ["--permission-mode", "acceptEdits"]\n')
        self.write_project('orchestrator:\n  options:\n    args: ["--add-dir", "x"]\n')
        loaded = config_mod.load(self.project)
        self.assertEqual(config_mod.validate(loaded.data), [])


class TestValidation(IsolatedCase):
    def test_invalid_provider_is_rejected(self):
        data = config_mod.default_config()
        data["implementer"]["provider"] = "nonexistent"
        problems = config_mod.validate(data)
        self.assertTrue(any("unknown provider" in p for p in problems))

    def test_an_unknown_provider_points_at_the_user_adapter_directory(self):
        data = config_mod.default_config()
        data["implementer"]["provider"] = "mycli"
        problems = config_mod.validate(data)
        expected = "user adapters load from %s" % os.path.join(self.config_home, "providers")
        self.assertTrue(any(expected in p for p in problems), problems)
        self.assertFalse(any("disabled by" in p for p in problems), problems)

    def test_an_unknown_provider_says_when_user_adapters_are_switched_off(self):
        data = config_mod.default_config()
        data["implementer"]["provider"] = "mycli"
        os.environ["DEV_ORCHESTRA_NO_USER_PROVIDERS"] = "1"
        problems = config_mod.validate(data)
        self.assertTrue(any("(disabled by DEV_ORCHESTRA_NO_USER_PROVIDERS)" in p for p in problems), problems)

    def test_referenced_providers_covers_roles_tiers_and_reviewers(self):
        data = config_mod.default_config()
        data["implementer"]["model_tiers"] = {"light": {"provider": "mock"}}
        data["reviewers"] = [config_mod.make_reviewer("mycli-general", "mycli", "default")]
        self.assertEqual(config_mod.referenced_providers(data), ["claude", "mock", "mycli"])

    def test_duplicate_reviewer_id_is_rejected(self):
        data = config_mod.default_config()
        data["reviewers"].append(dict(data["reviewers"][0]))
        problems = config_mod.validate(data)
        self.assertTrue(any("duplicate reviewer id" in p for p in problems))

    def test_zero_reviewers_is_valid(self):
        data = config_mod.default_config()
        data["reviewers"] = []
        self.assertEqual(config_mod.validate(data), [])

    def test_pinned_model_without_id_is_rejected(self):
        data = config_mod.default_config()
        data["implementer"]["model"] = {"family": "opus", "version": "pinned"}
        problems = config_mod.validate(data)
        self.assertTrue(any("model.id is missing" in p for p in problems))

    def test_unknown_version_policy_is_rejected(self):
        data = config_mod.default_config()
        data["implementer"]["model"]["version"] = "newest"
        self.assertTrue(any("model.version" in p for p in config_mod.validate(data)))

    def test_wrong_version_is_rejected(self):
        data = config_mod.default_config()
        data["version"] = 99
        self.assertTrue(any("version must be 1" in p for p in config_mod.validate(data)))

    def test_negative_iteration_budget_is_rejected(self):
        data = config_mod.default_config()
        data["review"]["max_review_iterations"] = -1
        self.assertTrue(any("max_review_iterations" in p for p in config_mod.validate(data)))

    def test_a_non_boolean_design_switch_is_rejected(self):
        def problems(value):
            data = config_mod.default_config()
            data["review"]["design"]["enabled"] = value
            return [p for p in config_mod.validate(data) if p.startswith("review.design")]

        for value in ("yes", "true", 1):
            with self.subTest(value=value):
                self.assertEqual(problems(value), ["review.design.enabled: must be true, false or auto"])
        for value in ("auto", "AUTO ", True, False, None):
            with self.subTest(value=value):
                self.assertEqual(problems(value), [])

    def test_a_negative_design_round_budget_is_rejected(self):
        data = config_mod.default_config()
        data["review"]["design"]["max_iterations"] = -1
        self.assertTrue(any("review.design.max_iterations" in p for p in config_mod.validate(data)))

    def test_a_context_budget_of_zero_is_rejected(self):
        """Zero would read as "no limit" to `over_context`, which is not what
        anyone writing it means; there is no way to switch the limit off."""
        data = config_mod.default_config()
        data["review"]["context"]["max_chars"] = 0
        self.assertTrue(any("review.context.max_chars" in p for p in config_mod.validate(data)))

    def test_a_non_integer_context_budget_is_rejected(self):
        data = config_mod.default_config()
        data["review"]["context"]["max_chars"] = "400000"
        self.assertTrue(any("review.context.max_chars" in p for p in config_mod.validate(data)))

    def test_an_inline_limit_of_zero_is_rejected(self):
        data = config_mod.default_config()
        data["review"]["context"]["inline_chars"] = 0
        self.assertTrue(any("review.context.inline_chars" in p for p in config_mod.validate(data)))

    def test_a_non_integer_inline_limit_is_rejected(self):
        data = config_mod.default_config()
        data["review"]["context"]["inline_chars"] = "400000"
        self.assertTrue(any("review.context.inline_chars" in p for p in config_mod.validate(data)))

    def test_an_inline_limit_above_the_budget_validates(self):
        """Not a mistake. It says "nothing is ever handed over as a file
        except a round a human forced", which is a thing somebody means."""
        data = config_mod.default_config()
        data["review"]["context"]["inline_chars"] = 900_000
        self.assertEqual([p for p in config_mod.validate(data) if "review.context" in p], [])

    def test_an_inline_limit_below_the_budget_validates(self):
        """Also not a mistake: it is what somebody who will not pay for very
        large prompts writes, and it costs them a `partial` round between the
        two numbers rather than a warning here."""
        data = config_mod.default_config()
        data["review"]["context"]["inline_chars"] = 120_000
        self.assertEqual([p for p in config_mod.validate(data) if "review.context" in p], [])

    def test_context_must_be_a_mapping(self):
        data = config_mod.default_config()
        data["review"]["context"] = []
        self.assertTrue(any("review.context: must be a mapping" in p for p in config_mod.validate(data)))

    def test_design_must_be_a_mapping(self):
        data = config_mod.default_config()
        data["review"]["design"] = []
        self.assertTrue(any("review.design: must be a mapping" in p for p in config_mod.validate(data)))

    def test_load_raises_on_invalid_config(self):
        data = config_mod.default_config()
        data["implementer"]["provider"] = "nope"
        config_mod.write_config_file(config_mod.global_config_path(), data)
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.project)


class TestValidateOrder(IsolatedCase):
    """The whole list ``validate`` returns, in the order it says it."""

    def test_every_integer_and_flag_wrong_at_once(self):
        data = config_mod.default_config()
        data["reviewers"] = []
        data["review"] = {
            "max_review_iterations": -1,
            "timeout_seconds": 0,
            "idle_timeout_seconds": 0,
            "incremental_rounds": "yes",
            "max_findings": True,
            "design": {"enabled": "maybe", "max_iterations": -1},
            "context": {
                "max_chars": 0,
                "inline_chars": "x",
                "surrounding_chars": False,
                "surrounding": True,
            },
            "exclude": ["", 3],
        }
        data["design"] = {
            "require_approval": "yes",
            "resume": {"max_age_seconds": -1, "max_context_tokens": 0},
        }
        data["optimization"] = {
            "level": "x",
            "high_risk_paths": "x",
            "extra_high_risk_paths": ["", 3],
            "low_risk_max_files": -1,
            "low_risk_max_lines": True,
        }
        data["budgets"] = {"implementer": -1, "test": "2", "architect": None}
        data["workspace"] = {"stale_notice_days": True}
        self.assertEqual(
            config_mod.validate(data),
            [
                "review.max_review_iterations: must be a non-negative integer",
                "review.timeout_seconds: must be a positive integer",
                "review.idle_timeout_seconds: must be a positive integer or null",
                "review.incremental_rounds: must be true or false",
                "review.max_findings: must be a non-negative integer (0 = no cap)",
                "review.design.enabled: must be true, false or auto",
                "review.design.max_iterations: must be a non-negative integer",
                "review.context.max_chars: must be a positive integer",
                "review.context.inline_chars: must be a positive integer",
                "review.context.surrounding_chars: must be a positive integer",
                "review.context.surrounding: must be one of enclosing, none",
                "review.exclude[0]: must be a non-empty string (got '')",
                "review.exclude[1]: must be a non-empty string (got 3)",
                "design.require_approval: must be true or false",
                "design.resume.max_age_seconds: must be a non-negative integer",
                "design.resume.max_context_tokens: must be a positive integer or null",
                "optimization.level: must be one of aggressive, balanced, quality",
                "optimization.high_risk_paths: must be a list of glob patterns (use [] for none)",
                "optimization.extra_high_risk_paths[0]: must be a non-empty string (got '')",
                "optimization.extra_high_risk_paths[1]: must be a non-empty string (got 3)",
                "optimization.low_risk_max_files: must be a non-negative integer",
                "optimization.low_risk_max_lines: must be a non-negative integer",
                "budgets.implementer: must be a non-negative integer or null",
                "budgets.test: must be a non-negative integer or null",
                "workspace.stale_notice_days: must be a non-negative integer (0 = off)",
            ],
        )

    def test_each_section_wrong_type_in_order(self):
        from orchestrator import presets

        data = config_mod.default_config()
        data["version"] = 2
        data["preset"] = "nope"
        del data["architect"]
        data["reviewers"] = "x"
        for key in ("review", "design", "optimization", "budgets", "workspace"):
            data[key] = 3
        problems = config_mod.validate(
            data,
            project_layer={"preset": "quality", "reviewers_extra": 3},
            global_layer={"reviewers_extra": 3},
        )
        self.assertEqual(
            problems,
            [
                "version must be 1 (got 2)",
                "preset: unknown 'nope' (known: %s)" % ", ".join(presets.NAMES),
                "preset: only the global file can name a preset for now",
                "architect: missing role definition",
                "reviewers: must be a list (use [] for none)",
                "reviewers_extra in the global file: must be a list (use [] for none)",
                "reviewers_extra in the project file: must be a list (use [] for none)",
                "review: must be a mapping",
                "design: must be a mapping",
                "optimization: must be a mapping",
                "budgets: must be a mapping",
                "workspace: must be a mapping",
            ],
        )

    def test_a_valid_panel_keeps_its_rules_before_the_extras(self):
        data = config_mod.default_config()
        data["reviewers"] = [
            {"id": "a", "provider": "mock", "when": "high-risk"},
            {"id": "a", "provider": "mock", "when": {"paths": ["src/**"]}},
        ]
        problems = config_mod.validate(
            data,
            global_layer={"reviewers_extra": [{"id": "g", "provider": "mock", "when": "sometimes"}]},
            project_layer={"reviewers_extra": [{"id": "p"}]},
        )
        self.assertEqual(
            problems,
            [
                "reviewers[1]: duplicate reviewer id 'a'",
                "reviewers: at least one reviewer must run always; every reviewer is conditional "
                "(when: high-risk or when: paths)",
                "reviewers_extra[0] in the global file: when: must be one of always, high-risk, "
                "or a mapping with paths",
                "reviewers_extra[0] in the project file: provider is required",
            ],
        )

    def test_the_lowest_accepted_values_pass(self):
        data = config_mod.default_config()
        review = data["review"]
        review.update(max_review_iterations=0, timeout_seconds=1, idle_timeout_seconds=1, max_findings=0)
        review["design"]["max_iterations"] = 0
        review["context"].update(max_chars=1, inline_chars=1, surrounding_chars=1)
        data["design"]["resume"] = {"max_age_seconds": 0, "max_context_tokens": 1}
        data["optimization"].update(low_risk_max_files=0, low_risk_max_lines=0)
        data["budgets"] = dict.fromkeys(data["budgets"], 0)
        data["workspace"]["stale_notice_days"] = 0
        self.assertEqual(config_mod.validate(data), [])
        review.update(idle_timeout_seconds=None, incremental_rounds=None, max_findings=None)
        review["design"].update(enabled=None, max_iterations=None)
        review["context"].update(max_chars=None, inline_chars=None, surrounding_chars=None)
        review["context"]["surrounding"] = None
        data["design"] = {
            "require_approval": None,
            "resume": {"max_age_seconds": None, "max_context_tokens": None},
        }
        data["optimization"].update(low_risk_max_files=None, low_risk_max_lines=None)
        data["budgets"] = dict.fromkeys(data["budgets"])
        data["workspace"]["stale_notice_days"] = None
        self.assertEqual(config_mod.validate(data), [])

    def test_null_is_refused_where_only_a_missing_key_defaults(self):
        data = config_mod.default_config()
        data["review"].update(max_review_iterations=None, timeout_seconds=None)
        self.assertEqual(
            config_mod.validate(data),
            [
                "review.max_review_iterations: must be a non-negative integer",
                "review.timeout_seconds: must be a positive integer",
            ],
        )
        del data["review"]["max_review_iterations"]
        del data["review"]["timeout_seconds"]
        self.assertEqual(config_mod.validate(data), [])

    def test_the_ceiling_follows_the_lower_bound(self):
        data = config_mod.default_config()
        data["workspace"]["stale_notice_days"] = 36501
        self.assertEqual(
            config_mod.validate(data), ["workspace.stale_notice_days: must be 36500 or less (100 years)"]
        )
        data["workspace"]["stale_notice_days"] = 36500
        self.assertEqual(config_mod.validate(data), [])
        # Below the floor, or not an integer at all, is the floor's message, not the ceiling's.
        for days in (-1, True):
            with self.subTest(days=days):
                data["workspace"]["stale_notice_days"] = days
                self.assertEqual(
                    config_mod.validate(data),
                    ["workspace.stale_notice_days: must be a non-negative integer (0 = off)"],
                )

    def test_every_integer_field_refuses_a_bool_and_a_float(self):
        fields = [
            ("run.timeout_seconds.implementer", "must be a positive integer"),
            ("review.max_review_iterations", "must be a non-negative integer"),
            ("review.timeout_seconds", "must be a positive integer"),
            ("review.idle_timeout_seconds", "must be a positive integer or null"),
            ("review.max_findings", "must be a non-negative integer (0 = no cap)"),
            ("review.design.max_iterations", "must be a non-negative integer"),
            ("review.context.max_chars", "must be a positive integer"),
            ("review.context.inline_chars", "must be a positive integer"),
            ("review.context.surrounding_chars", "must be a positive integer"),
            ("design.resume.max_age_seconds", "must be a non-negative integer"),
            ("design.resume.max_context_tokens", "must be a positive integer or null"),
            ("optimization.low_risk_max_files", "must be a non-negative integer"),
            ("optimization.low_risk_max_lines", "must be a non-negative integer"),
            ("budgets.implementer", "must be a non-negative integer or null"),
            ("workspace.stale_notice_days", "must be a non-negative integer (0 = off)"),
        ]
        for path, message in fields:
            for value in (True, 1.5):
                with self.subTest(path=path, value=value):
                    data = config_mod.default_config()
                    *parents, leaf = path.split(".")
                    block = data
                    for key in parents:
                        block = block.setdefault(key, {})
                    block[leaf] = value
                    self.assertEqual(config_mod.validate(data), ["%s: %s" % (path, message)])

    def test_every_known_preset_validates(self):
        from orchestrator import presets

        for name in presets.NAMES:
            with self.subTest(preset=name):
                data = config_mod.default_config()
                data["preset"] = name
                self.assertEqual(config_mod.validate(data), [])

    def test_a_preset_that_is_not_a_string_is_unknown(self):
        from orchestrator import presets

        # A list is unhashable: it must be reported, not raise on the lookup.
        for preset in (3, ["quality"]):
            with self.subTest(preset=preset):
                data = config_mod.default_config()
                data["preset"] = preset
                self.assertEqual(
                    config_mod.validate(data),
                    ["preset: unknown %r (known: %s)" % (preset, ", ".join(presets.NAMES))],
                )


class TestReviewerConditions(IsolatedCase):
    """`reviewers[].when`, and what a panel of conditional reviewers needs."""

    ALWAYS = (
        "reviewers: at least one reviewer must run always; every reviewer is conditional "
        "(when: high-risk or when: paths)"
    )
    SQL: ClassVar[Dict[str, Any]] = {"paths": ["*.sql"]}
    NO_PATTERNS = (
        "optimization.high_risk_paths: no pattern in force, but reviewers[1] is when: high-risk "
        "and would never run; add patterns to high_risk_paths or extra_high_risk_paths"
    )

    def with_conditions(self, *conditions, **optimization):
        data = config_mod.default_config()
        # The two general seats: the conditions under test are set on them.
        data["reviewers"] = data["reviewers"][:2]
        for reviewer, when in zip(data["reviewers"], conditions, strict=False):
            if when is not None:
                reviewer["when"] = when
        data["optimization"].update(optimization)
        return config_mod.validate(data)

    def test_both_conditions_validate(self):
        self.assertEqual(self.with_conditions("always", "high-risk"), [])
        self.assertEqual(self.with_conditions(None, "High-Risk "), [])

    def test_an_unknown_condition_is_named(self):
        for value in ("sometimes", 1, "paths", ["*.sql"]):
            with self.subTest(value=value):
                self.assertIn(
                    "reviewers[1].when: must be one of always, high-risk, or a mapping with paths",
                    self.with_conditions(None, value),
                )

    def test_a_paths_mapping_validates(self):
        self.assertEqual(self.with_conditions(None, {"paths": ["*migrate*/*", "*.sql"]}), [])

    def test_a_mapping_takes_paths_only(self):
        cases = (
            ({}, "none"),
            ({"path": ["*.sql"]}, "path"),
            ({"paths": ["*.sql"], "x": 1}, "paths, x"),
        )
        for value, keys in cases:
            with self.subTest(value=value):
                self.assertIn(
                    "reviewers[1].when: a when mapping takes paths only (got keys: %s)" % keys,
                    self.with_conditions(None, value),
                )

    def test_paths_must_be_a_non_empty_list(self):
        for value in ("*.sql", [], None):
            with self.subTest(value=value):
                self.assertIn(
                    "reviewers[1].when.paths: must be a non-empty list of glob patterns",
                    self.with_conditions(None, {"paths": value}),
                )

    def test_every_pattern_must_be_a_non_empty_string(self):
        problems = self.with_conditions(None, {"paths": ["*.sql", 42, " "]})
        self.assertIn("reviewers[1].when.paths[1]: must be a non-empty string (got 42)", problems)
        self.assertIn("reviewers[1].when.paths[2]: must be a non-empty string (got ' ')", problems)
        self.assertNotIn("reviewers[1].when.paths[0]", " ".join(problems))

    def test_what_validation_refuses_is_what_reads_as_always(self):
        from orchestrator import optimization as opt

        for value in ({}, {"paths": []}, {"paths": [""]}, {"paths": ["*.sql", 42]}, {"paths": "*.sql"}):
            with self.subTest(value=value):
                self.assertTrue(self.with_conditions(None, value))
                self.assertEqual(opt.reviewer_condition({"when": value}), "always")

    def test_a_panel_of_high_risk_and_path_scoped_reviewers_is_refused(self):
        self.assertIn(self.ALWAYS, self.with_conditions("high-risk", self.SQL))
        self.assertIn(self.ALWAYS, self.with_conditions(self.SQL, self.SQL))

    def test_a_path_scoped_reviewer_needs_no_high_risk_pattern(self):
        problems = self.with_conditions(None, self.SQL, high_risk_paths=[], extra_high_risk_paths=[])
        self.assertEqual(problems, [])

    def test_a_high_risk_reviewer_beside_it_still_needs_one(self):
        data = config_mod.default_config()
        data["reviewers"].append(dict(data["reviewers"][0], id="db", when=self.SQL))
        data["reviewers"][1]["when"] = "high-risk"
        data["optimization"]["high_risk_paths"] = []
        self.assertIn(self.NO_PATTERNS, config_mod.validate(data))

    def test_make_reviewer_writes_the_mapping(self):
        reviewer = config_mod.make_reviewer("db", "mock", None, "database", paths=["*migrate*/*", "*.sql"])
        self.assertEqual(reviewer["when"], {"paths": ["*migrate*/*", "*.sql"]})
        self.assertNotIn("when", config_mod.make_reviewer("gen", "mock", None, when="always"))

    def test_every_reviewer_conditional_is_refused(self):
        self.assertIn(self.ALWAYS, self.with_conditions("high-risk", "high-risk"))

    def test_one_unconditional_reviewer_is_enough(self):
        self.assertNotIn(self.ALWAYS, self.with_conditions("always", "high-risk"))

    def test_an_empty_panel_is_still_valid(self):
        data = config_mod.default_config()
        data["reviewers"] = []
        self.assertEqual(config_mod.validate(data), [])

    def test_a_conditional_reviewer_needs_a_pattern(self):
        self.assertIn(self.NO_PATTERNS, self.with_conditions(None, "high-risk", high_risk_paths=[]))

    def test_an_extra_pattern_is_a_pattern(self):
        problems = self.with_conditions(
            None, "high-risk", high_risk_paths=[], extra_high_risk_paths=["*/providers/*"]
        )
        self.assertEqual(problems, [])

    def test_no_patterns_without_a_conditional_reviewer_stays_allowed(self):
        self.assertEqual(self.with_conditions(None, None, high_risk_paths=[]), [])

    def test_extra_high_risk_paths_is_validated_like_high_risk_paths(self):
        problems = self.with_conditions(extra_high_risk_paths="*/providers/*")
        self.assertIn(
            "optimization.extra_high_risk_paths: must be a list of glob patterns (use [] for none)", problems
        )
        problems = self.with_conditions(extra_high_risk_paths=[""])
        self.assertIn("optimization.extra_high_risk_paths[0]: must be a non-empty string (got '')", problems)

    def test_extra_high_risk_paths_ships_empty(self):
        self.assertEqual(config_mod.default_config()["optimization"]["extra_high_risk_paths"], [])

    def test_the_extra_patterns_are_added_to_the_list_in_force(self):
        from orchestrator import optimization as opt

        self.assertEqual(
            opt.risk_patterns({"high_risk_paths": ["*auth*"], "extra_high_risk_paths": ["*/providers/*"]}),
            ["*auth*", "*/providers/*"],
        )
        added = opt.risk_patterns({"extra_high_risk_paths": ["*/providers/*"]})
        self.assertEqual(added, [*opt.DEFAULT_HIGH_RISK_PATHS, "*/providers/*"])

    def test_a_project_list_replaces_a_global_one(self):
        """Like every list between the layers: a project that sets it
        discards the global value rather than adding to it."""
        data = config_mod.default_config()
        data["optimization"]["extra_high_risk_paths"] = ["*/global/*"]
        config_mod.write_config_file(config_mod.global_config_path(), data)
        self.write(
            ".dev-orchestra.yaml",
            'version: 1\noptimization:\n  extra_high_risk_paths: ["*/providers/*"]\n',
        )
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.optimization_settings()["extra_high_risk_paths"], ["*/providers/*"])


class TestRoleOptions(IsolatedCase):
    def test_valid_options_pass(self):
        data = config_mod.default_config()
        data["implementer"]["options"] = {"permission_mode": "bypassPermissions"}
        problems = [p for p in config_mod.validate(data) if "options" in p]
        self.assertEqual(problems, [])

    def test_options_are_validated_against_the_provider(self):
        data = config_mod.default_config()
        data["implementer"]["options"] = {"sandbox": "workspace-write"}  # a codex key
        self.assertTrue(any("not understood by the claude provider" in p for p in config_mod.validate(data)))

    def test_non_mapping_options_are_rejected(self):
        data = config_mod.default_config()
        data["implementer"]["options"] = ["--permission-mode"]
        self.assertTrue(any("options must be a mapping" in p for p in config_mod.validate(data)))

    def test_options_are_validated_on_a_role_that_names_no_model(self):
        """Omitting `model` is how you let a CLI pick its own, and the model
        checks return early -- which used to skip the option checks with them.
        A typo in a sandbox policy passed `config validate` and was found at
        run time instead."""
        data = config_mod.default_config()
        data["implementer"] = {"provider": "codex", "options": {"sandbox": "nonsense"}}
        self.assertTrue(any("options.sandbox" in p for p in config_mod.validate(data)))

    def test_reviewer_options_are_validated_too(self):
        data = config_mod.default_config()
        data["reviewers"][1]["options"] = {"sandbox": "nonsense"}
        self.assertTrue(any("options.sandbox" in p for p in config_mod.validate(data)))

    def test_absent_options_need_no_provider_lookup(self):
        # The common case must not consult an adapter at all.
        original = providers.get_provider
        providers.get_provider = lambda *a, **k: self.fail("provider was consulted")
        try:
            self.assertEqual(config_mod.validate(config_mod.default_config()), [])
        finally:
            providers.get_provider = original


class TestReviewerManagement(IsolatedCase):
    def test_add_reviewer(self):
        data = config_mod.default_config()
        reviewer = config_mod.make_reviewer("codex-security", "codex", "recommended-coding", "security")
        config_mod.add_reviewer(data, reviewer)
        self.assertEqual([r["id"] for r in data["reviewers"]][-1], "codex-security")
        self.assertEqual(config_mod.validate(data), [])

    def test_add_duplicate_reviewer_id_raises(self):
        data = config_mod.default_config()
        with self.assertRaises(config_mod.ConfigError):
            config_mod.add_reviewer(data, config_mod.make_reviewer("claude-general", "claude", "opus"))

    def test_remove_reviewer_by_id_role_and_position(self):
        data = config_mod.default_config()
        config_mod.add_reviewer(data, config_mod.make_reviewer("codex-security", "codex", None, "security"))

        _, removed = config_mod.remove_reviewer(data, "codex-security")
        self.assertEqual(removed["id"], "codex-security")

        _, removed = config_mod.remove_reviewer(data, "2")
        self.assertEqual(removed["id"], "codex-general")

        _, removed = config_mod.remove_reviewer(data, "general")
        self.assertEqual(removed["id"], "claude-general")
        self.assertEqual([r["id"] for r in data["reviewers"]], ["claude-security", "claude-test"])

    def test_remove_ambiguous_role_raises_with_guidance(self):
        data = config_mod.default_config()
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.remove_reviewer(data, "general")
        self.assertIn("remove by id instead", str(ctx.exception))

    def test_remove_missing_reviewer_raises(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod.remove_reviewer(config_mod.default_config(), "ghost")

    def test_suggested_ids_do_not_collide(self):
        data = config_mod.default_config()
        first = config_mod.suggest_reviewer_id(data, "codex", "security")
        config_mod.add_reviewer(data, config_mod.make_reviewer(first, "codex", None, "security"))
        second = config_mod.suggest_reviewer_id(data, "codex", "security")
        self.assertNotEqual(first, second)
        self.assertEqual(second, "codex-security-2")


class TestPathEditing(IsolatedCase):
    def test_set_and_get_nested_path(self):
        data = config_mod.default_config()
        config_mod.set_path(data, "implementer.model.family", "sonnet")
        self.assertEqual(config_mod.get_path(data, "implementer.model.family"), "sonnet")

    def test_set_indexed_path(self):
        data = config_mod.default_config()
        config_mod.set_path(data, "reviewers[1].role", "security")
        self.assertEqual(data["reviewers"][1]["role"], "security")

    def test_get_missing_path_returns_default(self):
        self.assertEqual(config_mod.get_path({}, "a.b.c", "fallback"), "fallback")

    def test_coerce_scalar_types(self):
        self.assertEqual(config_mod.coerce_scalar("3"), 3)
        self.assertIs(config_mod.coerce_scalar("true"), True)
        self.assertEqual(config_mod.coerce_scalar("opus"), "opus")
        self.assertEqual(config_mod.coerce_scalar("[a, b]"), ["a", "b"])


class TestLanguage(IsolatedCase):
    """``language.reply`` and ``language.rewrite`` (#254)."""

    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from orchestrator import cli

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def problems(self, language):
        data = config_mod.default_config()
        data["language"] = language
        return config_mod.validate(data)

    def test_language_validation(self):
        for tag in ("ja", "JA", "zh-TW", "zh-Hant-TW", "ko", "en", "en-GB", "sr-Latn", "tlh", None):
            self.assertEqual(self.problems({"reply": tag}), [], tag)
        for tag in ("japanese", "j", "日本語", "ja_JP", "ja-", "-ja", "", 1, True, ["ja"]):
            self.assertEqual(
                self.problems({"reply": tag}),
                ["language.reply: must be a language tag such as ja, zh-TW, ko or en, or null"],
                tag,
            )
        for rewrite in (True, False, None):
            self.assertEqual(self.problems({"reply": "ja", "rewrite": rewrite}), [], rewrite)
        for rewrite in ("false", 0, 1, "no"):
            self.assertEqual(
                self.problems({"rewrite": rewrite}), ["language.rewrite: must be true or false"], rewrite
            )
        self.assertEqual(self.problems("ja"), ["language: must be a mapping"])
        self.assertEqual(self.problems(None), [])

    def test_language_settings_fill_in_and_normalise(self):
        settings = config_mod.language_settings_of
        self.assertEqual(settings({}), {"reply": None, "rewrite": True})
        self.assertEqual(settings({"language": None}), {"reply": None, "rewrite": True})
        self.assertEqual(settings({"language": {"reply": "JA"}}), {"reply": "ja", "rewrite": True})
        self.assertEqual(settings({"language": {"reply": "ZH-TW"}})["reply"], "zh-TW")
        self.assertEqual(settings({"language": {"reply": "japanese"}})["reply"], None)
        self.assertEqual(settings({"language": {"reply": "ko", "rewrite": None}})["rewrite"], True)
        self.assertEqual(settings({"language": {"reply": "ko", "rewrite": False}})["rewrite"], False)
        # Only an explicit false turns the check off; an invalid value does not.
        self.assertEqual(settings({"language": {"rewrite": "false"}})["rewrite"], True)
        self.assertEqual(config_mod.load(self.project).language_settings(), {"reply": None, "rewrite": True})

    def test_language_not_reported_as_pinned(self):
        data = config_mod.default_config()
        data["language"] = {"reply": "ja", "rewrite": False}
        self.assertEqual(config_mod.pinned_differences(data), [])

    def test_config_set_language_reply(self):
        code, _, err = self.run_cli("config", "set", "language.reply", "ko")
        self.assertEqual(code, 0, err)
        self.assertNotIn("language", err)
        self.assertEqual(config_mod.load(self.project).language_settings()["reply"], "ko")
        code, _, err = self.run_cli("config", "set", "language.reply", "korean")
        self.assertEqual(code, 0)
        self.assertIn("warning: language.reply: must be a language tag", err)
        code, _, err = self.run_cli("config", "set", "language.reply", "null")
        self.assertEqual(code, 0, err)
        self.assertNotIn("language", err)
        self.assertIsNone(config_mod.load(self.project).language_settings()["reply"])

    def test_a_project_null_undoes_a_global_reply_language(self):
        code, _, err = self.run_cli("config", "set", "language.reply", "ja", "--scope", "global")
        self.assertEqual(code, 0, err)
        code, _, err = self.run_cli("config", "set", "language.reply", "null", "--scope", "project")
        self.assertEqual(code, 0, err)
        loaded = config_mod.load(self.project)
        self.assertIsNone(loaded.language_settings()["reply"])
        self.assertEqual(loaded.layer_of("language.reply"), "project")
        # Only that key: any other null still keeps the value below.
        merged = config_mod.deep_merge(
            {"language": {"reply": "ja", "rewrite": False}, "workspace": {"dir": "w"}},
            {"language": {"reply": None, "rewrite": None}, "workspace": {"dir": None}},
        )
        self.assertEqual(merged, {"language": {"reply": None, "rewrite": False}, "workspace": {"dir": "w"}})


class TestRunTimeout(IsolatedCase):
    """``run.timeout_seconds.<role>``, the total deadline of one `run` (#258)."""

    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from orchestrator import cli

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def write_global(self, text):
        path = config_mod.global_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("version: 1\n" + text)

    def problems(self, run):
        data = config_mod.default_config()
        data["run"] = run
        return config_mod.validate(data)

    def test_run_timeout_defaults_per_role(self):
        loaded = config_mod.load(self.project)
        for role in config_mod.KNOWN_ROLES:
            expected = 3600 if role == "implementer" else 1800
            self.assertEqual(config_mod.run_timeout(loaded, role), (expected, "default"), role)
        self.assertEqual(loaded.review_settings()["timeout_seconds"], 1800)

    def test_run_timeout_project_overrides_global(self):
        self.write_global("run:\n  timeout_seconds:\n    implementer: 5000\n    architect: 900\n")
        self.write(".dev-orchestra.yaml", "version: 1\nrun:\n  timeout_seconds:\n    implementer: 4000\n")
        loaded = config_mod.load(self.project)
        self.assertEqual(config_mod.run_timeout(loaded, "implementer"), (4000, "project"))
        self.assertEqual(config_mod.run_timeout(loaded, "architect"), (900, "global"))
        self.assertEqual(config_mod.run_timeout(loaded, "review_fixer"), (1800, "default"))
        self.assertEqual(loaded.layer_of("run.timeout_seconds.implementer"), "project")
        self.assertEqual(loaded.layer_of("run.timeout_seconds.architect"), "global")
        # A null in a file keeps what is below it, and reads as that.
        self.write(".dev-orchestra.yaml", "version: 1\nrun:\n  timeout_seconds:\n    architect: null\n")
        loaded = config_mod.load(self.project)
        self.assertEqual(config_mod.run_timeout(loaded, "architect"), (900, "global"))

    def test_run_timeout_refuses_bad_values(self):
        message = "run.timeout_seconds.architect: must be a positive integer"
        for value in (0, -1, True, 1.5, "900", None):
            with self.subTest(value=value):
                self.assertEqual(self.problems({"timeout_seconds": {"architect": value}}), [message])
        unknown = (
            "run.timeout_seconds.reviewer: unknown role "
            "(known: orchestrator, architect, implementer, review_fixer)"
        )
        self.assertEqual(self.problems({"timeout_seconds": {"reviewer": 900}}), [unknown])
        self.assertEqual(self.problems(3), ["run: must be a mapping"])
        for timeouts in (900, None):
            with self.subTest(timeouts=timeouts):
                self.assertEqual(
                    self.problems({"timeout_seconds": timeouts}),
                    ["run.timeout_seconds: must be a mapping of role to seconds"],
                )
        # An invalid value the files hold reads as the default where nothing validates.
        self.write(".dev-orchestra.yaml", "version: 1\nrun:\n  timeout_seconds:\n    architect: 0\n")
        loaded = config_mod.load(self.project, validate_result=False)
        self.assertEqual(config_mod.run_timeout(loaded, "architect"), (1800, "default"))
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.project)

    def test_run_timeout_lowest_accepted_value_passes(self):
        self.assertEqual(self.problems({"timeout_seconds": dict.fromkeys(config_mod.KNOWN_ROLES, 1)}), [])
        self.assertEqual(self.problems({"timeout_seconds": {}}), [])
        self.assertEqual(self.problems({}), [])

    def test_run_timeout_is_not_governed(self):
        self.fake_clis(claude=True, codex=True)
        self.write_global("run:\n  timeout_seconds:\n    architect: 900\n")
        code, _, err = self.run_cli("config", "setup", "--preset", "fast")
        self.assertEqual(code, 0, err)
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.preset, "fast")
        self.assertEqual(config_mod.run_timeout(loaded, "architect"), (900, "global"))
        self.assertFalse(any("not fitted" in note for note in loaded.preset_notes), loaded.preset_notes)

    def test_config_set_run_timeout_role(self):
        code, _, err = self.run_cli(
            "config", "set", "run.timeout_seconds.architect", "900", "--scope", "project"
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("warning", err)
        loaded = config_mod.load(self.project)
        self.assertEqual(config_mod.run_timeout(loaded, "architect"), (900, "project"))
        self.assertEqual(config_mod.run_timeout(loaded, "implementer"), (3600, "default"))
        self.assertEqual(loaded.project_layer["run"], {"timeout_seconds": {"architect": 900}})
        self.assertEqual(
            config_mod.validate(
                loaded.data, project_layer=loaded.project_layer, global_layer=loaded.global_layer
            ),
            [],
        )


class TestPruneLayer(IsolatedCase):
    """`prune_layer` reads "equal to what this layer inherits" as evidence that
    the value was never chosen -- the only evidence a pre-0.6.0 file carries,
    which is why it runs on request rather than on every write."""

    def test_a_leaf_equal_to_the_base_is_dropped(self):
        base = {"optimization": {"low_risk_max_files": 5, "low_risk_max_lines": 150}}
        layer = {"version": 1, "optimization": {"low_risk_max_files": 5, "low_risk_max_lines": 50}}
        pruned, dropped = config_mod.prune_layer(layer, base)
        self.assertEqual(pruned, {"version": 1, "optimization": {"low_risk_max_lines": 50}})
        self.assertEqual(dropped, [{"setting": "optimization.low_risk_max_files", "value": 5}])

    def test_a_list_is_dropped_only_when_it_matches_whole(self):
        base = {"review": {"exclude": ["a", "b"]}}
        same, _ = config_mod.prune_layer({"version": 1, "review": {"exclude": ["a", "b"]}}, base)
        self.assertEqual(same, {"version": 1})
        edited, _ = config_mod.prune_layer({"version": 1, "review": {"exclude": ["a", "c"]}}, base)
        self.assertEqual(edited, {"version": 1, "review": {"exclude": ["a", "c"]}})

    def test_a_mapping_emptied_by_its_children_goes_with_them(self):
        base = {"review": {"design": {"enabled": False}}}
        pruned, _ = config_mod.prune_layer({"version": 1, "review": {"design": {"enabled": False}}}, base)
        self.assertEqual(pruned, {"version": 1})

    def test_an_explicit_design_switch_off_is_kept_over_the_default(self):
        """`false` used to equal the default and be pruned; it is now a choice."""
        base = config_mod.default_config()
        layer = {"version": 1, "review": {"design": {"enabled": False}}}
        pruned, _ = config_mod.prune_layer(layer, base)
        self.assertEqual(pruned, layer)
        same, _ = config_mod.prune_layer({"version": 1, "review": {"design": {"enabled": "auto"}}}, base)
        self.assertEqual(same, {"version": 1})

    def test_a_key_the_base_does_not_mention_is_kept(self):
        base = config_mod.default_config()
        layer = dict(config_mod.default_config())
        layer["implementer"] = dict(layer["implementer"], options={"permission_mode": "acceptEdits"})
        pruned, _ = config_mod.prune_layer(layer, base)
        self.assertEqual(pruned["implementer"], {"options": {"permission_mode": "acceptEdits"}})

    def test_the_default_panel_is_dropped_like_any_other_list(self):
        """The only way back for a panel the wizard wrote down: `doctor` never
        reports one, because comparing a panel to the default one would flag
        every installation that added a reviewer."""
        pruned, _ = config_mod.prune_layer(config_mod.default_config(), config_mod.default_config())
        self.assertEqual(pruned, {"version": 1})

    def test_version_survives_and_is_supplied(self):
        kept, _ = config_mod.prune_layer({"version": 0}, config_mod.default_config())
        self.assertEqual(kept, {"version": 0})
        supplied, _ = config_mod.prune_layer({}, config_mod.default_config())
        self.assertEqual(supplied, {"version": 1})


class TestPaths(IsolatedCase):
    def test_env_override_wins(self):
        explicit = os.path.join(self.tmp, "explicit.yaml")
        os.environ["DEV_ORCHESTRA_CONFIG"] = explicit
        self.assertEqual(config_mod.global_config_path(), explicit)

    def test_user_providers_live_in_the_config_directory(self):
        expected = os.path.join(config_mod.global_config_dir(), "providers")
        self.assertEqual(config_mod.user_providers_dir(), expected)
        # A config file named explicitly may sit in a project checkout; the
        # code beside it is not imported.
        os.environ["DEV_ORCHESTRA_CONFIG"] = os.path.join(self.tmp, "elsewhere", "explicit.yaml")
        self.assertEqual(config_mod.user_providers_dir(), expected)

    def test_global_dir_is_platform_appropriate(self):
        os.environ.pop("DEV_ORCHESTRA_HOME")
        directory = config_mod.global_config_dir()
        self.assertTrue(directory.endswith(config_mod.APP_DIR_NAME))
        self.assertTrue(os.path.isabs(directory))

    def test_round_trip_through_disk(self):
        path = config_mod.global_config_path()
        data = config_mod.default_config()
        config_mod.write_config_file(path, data)
        self.assertEqual(config_mod.read_config_file(path), data)


class TestStoredElsewhere(IsolatedCase):
    """Where a Microsoft Store Python really keeps a file (#235)."""

    def windows(self):
        patcher = mock.patch.object(config_mod, "on_windows", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nothing_redirected_is_none(self):
        self.windows()
        with mock.patch.object(config_mod, "real_location", lambda path: path):
            self.assertIsNone(config_mod.stored_elsewhere(config_mod.global_config_path()))
            self.assertEqual(config_mod.shown_location(self.tmp), self.tmp)

    def test_redirected_file_reports_where_it_really_is(self):
        real = self.redirect_config_home()
        path = config_mod.global_config_path()
        expected = os.path.join(real, "config.yaml")
        self.assertEqual(config_mod.stored_elsewhere(path), expected)
        self.assertEqual(config_mod.shown_location(path), "%s (stored at %s)" % (path, expected))
        self.assertEqual(config_mod.stored_elsewhere(config_mod.global_config_dir()), real)
        # The profile and AppData itself are where they seem to be.
        self.assertIsNone(config_mod.stored_elsewhere(os.environ["APPDATA"]))

    def test_user_adapter_hint_names_the_real_folder(self):
        real = self.redirect_config_home()
        expected = "user adapters load from %s (stored at %s)" % (
            config_mod.user_providers_dir(),
            os.path.join(real, "providers"),
        )
        self.assertTrue(config_mod.user_providers_hint().startswith(expected))

    def test_a_redirected_anchor_is_caught_by_the_packages_folder(self):
        """DEV_ORCHESTRA_HOME outside the profile, under a folder that is
        itself redirected: the anchor moves with the file, so only the
        Packages signal sees it."""
        self.windows()
        self.set_env("HOME", os.path.join(self.tmp, "profile"))
        self.set_env("USERPROFILE", os.path.join(self.tmp, "profile"))
        local = os.path.join(self.tmp, "elsewhere", "AppData", "Local")
        home = os.path.join(local, "dev-orchestra")
        os.environ["DEV_ORCHESTRA_HOME"] = home
        packages = os.path.join(local, "Packages", "X", "LocalCache", "Local")

        def fake(path):
            if path.startswith(local) and not path.startswith(os.path.join(local, "Packages")):
                return packages + path[len(local) :]
            return path

        with mock.patch.object(config_mod, "real_location", fake):
            path = config_mod.global_config_path()
            self.assertEqual(config_mod.stored_elsewhere(path), fake(path))

    def test_explicit_config_outside_the_profile(self):
        self.windows()
        self.set_env("HOME", os.path.join(self.tmp, "profile"))
        self.set_env("USERPROFILE", os.path.join(self.tmp, "profile"))
        explicit = os.path.join(self.tmp, "outside", "explicit.yaml")
        os.environ["DEV_ORCHESTRA_CONFIG"] = explicit
        with mock.patch.object(config_mod, "real_location", lambda path: path):
            self.assertIsNone(config_mod.stored_elsewhere(config_mod.global_config_path()))
        moved = os.path.join(self.tmp, "moved", "explicit.yaml")
        with mock.patch.object(config_mod, "real_location", lambda p: moved if p == explicit else p):
            self.assertEqual(config_mod.stored_elsewhere(explicit), moved)

    def test_another_drive_falls_back_to_the_files_folder(self):
        self.windows()
        path = os.path.join(self.tmp, "other-drive", "config.yaml")
        moved = os.path.join(self.tmp, "moved", "config.yaml")
        with mock.patch("os.path.relpath", side_effect=ValueError("path is on mount 'D:'")):
            with mock.patch.object(config_mod, "real_location", lambda p: p):
                self.assertIsNone(config_mod.stored_elsewhere(path))
            with mock.patch.object(config_mod, "real_location", lambda p: moved if p == path else p):
                self.assertEqual(config_mod.stored_elsewhere(path), moved)

    def test_off_windows_nothing_is_reported_even_through_a_link(self):
        target = os.path.join(self.tmp, "target")
        os.makedirs(target)
        link = os.path.join(self.config_home, "linked")
        try:
            make_dir_link(link, target)
        except (OSError, subprocess.CalledProcessError, NotImplementedError) as exc:
            self.skipTest("cannot make a link here: %s" % exc)
        # tearDown, which runs first, may already have removed it with the tree.
        self.addCleanup(lambda: os.path.lexists(link) and remove_link(link))
        with mock.patch.object(config_mod, "on_windows", return_value=False):
            self.assertIsNone(config_mod.stored_elsewhere(os.path.join(link, "config.yaml")))
            self.assertEqual(config_mod.shown_location(link), link)


class TestStoredElsewhereOnDisk(IsolatedCase):
    """The real ``realpath``, only the platform forced: temp folders come with
    their own renames (``/var`` -> ``/private/var`` on macOS, 8.3 short names
    on Windows runners), which must not read as "stored elsewhere"."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(config_mod, "on_windows", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The temp folder is the profile, so the anchor is the same on every runner.
        self.set_env("HOME", self.tmp)
        self.set_env("USERPROFILE", self.tmp)
        packages = "\\appdata\\local\\packages\\"
        real = os.path.realpath(self.tmp).lower().replace("/", "\\")
        if packages in real and packages not in self.tmp.lower().replace("/", "\\"):
            self.skipTest("this Python's temp folder is itself redirected into its package")

    def link(self, link, target):
        try:
            make_dir_link(link, target)
        except (OSError, subprocess.CalledProcessError, NotImplementedError) as exc:
            self.skipTest("cannot make a link here: %s" % exc)
        # tearDown, which runs first, may already have removed it with the tree.
        self.addCleanup(lambda: os.path.lexists(link) and remove_link(link))

    def test_a_plain_temp_folder_is_not_reported(self):
        path = config_mod.global_config_path()
        config_mod.write_config_file(path, config_mod.default_config())
        self.assertIsNone(config_mod.stored_elsewhere(path))
        self.assertIsNone(config_mod.stored_elsewhere(config_mod.global_config_dir()))
        self.assertEqual(config_mod.shown_location(path), path)

    def test_a_linked_folder_above_the_profile_is_not_reported(self):
        target = os.path.join(self.tmp, "target")
        os.makedirs(os.path.join(target, "profile", "dev-orchestra"))
        link = os.path.join(self.tmp, "linked")
        self.link(link, target)
        self.set_env("HOME", os.path.join(link, "profile"))
        self.set_env("USERPROFILE", os.path.join(link, "profile"))
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(link, "profile", "dev-orchestra")
        path = config_mod.global_config_path()
        config_mod.write_config_file(path, config_mod.default_config())
        self.assertIsNone(config_mod.stored_elsewhere(path))

    @unittest.skipUnless(sys.platform.startswith("win"), "8.3 short names are a Windows thing")
    def test_a_profile_spelled_by_its_short_name_is_not_reported(self):
        # Windows only: ctypes.windll exists nowhere else.
        import ctypes

        def spelled(function, path):
            size = function(path, None, 0)
            if not size:
                self.skipTest("cannot spell %s another way here" % path)
            buffer = ctypes.create_unicode_buffer(size)
            function(path, buffer, size)
            return buffer.value

        kernel32 = ctypes.windll.kernel32
        short = spelled(kernel32.GetShortPathNameW, self.tmp)
        long = spelled(kernel32.GetLongPathNameW, self.tmp)
        if short.lower() == long.lower():
            self.skipTest("no 8.3 short name for %s" % self.tmp)
        self.set_env("HOME", short)
        self.set_env("USERPROFILE", short)
        for base in (short, long):
            with self.subTest(base):
                os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(base, "cfg")
                path = config_mod.global_config_path()
                config_mod.write_config_file(path, config_mod.default_config())
                self.assertIsNone(config_mod.stored_elsewhere(path))
                self.assertIsNone(config_mod.stored_elsewhere(config_mod.global_config_dir()))

    def test_a_link_inside_the_config_folder_is_reported(self):
        target = os.path.join(self.tmp, "target")
        os.makedirs(target)
        link = os.path.join(self.config_home, "providers")
        self.link(link, target)
        reported = present(config_mod.stored_elsewhere(os.path.join(link, "x.py")))
        expected = os.path.join(os.path.realpath(target), "x.py")
        self.assertEqual(os.path.normcase(reported), os.path.normcase(expected))


class TestPresetLayering(IsolatedCase):
    """The global file's preset sits between the defaults and the files, and
    reaches only the roles and the panel no file sets."""

    def write_global(self, data):
        config_mod.write_config_file(config_mod.global_config_path(), data, "global")

    def ids(self, loaded):
        return [reviewer["id"] for reviewer in loaded.reviewers()]

    def test_the_global_preset_is_expanded(self):
        self.fake_clis(claude=True, codex=True)
        self.write_global({"version": 1, "preset": "quality"})
        loaded = config_mod.load(self.project)
        self.assertEqual((loaded.preset, loaded.preset_source), ("quality", "global"))
        self.assertEqual(loaded.role("implementer")["model"]["family"], "fable")
        self.assertEqual(
            self.ids(loaded),
            [
                "claude-general",
                "codex-general",
                "claude-security",
                "codex-security",
                "claude-architecture",
                "claude-test",
            ],
        )
        self.assertEqual(loaded.optimization_settings()["level"], "quality")
        self.assertEqual(loaded.design_review_settings()["enabled"], "auto")
        self.assertEqual(loaded.design_panel_source, "fit")

    def test_a_global_override_survives_the_expansion(self):
        self.fake_clis(claude=True, codex=True)
        self.write_global({"version": 1, "preset": "fast", "review": {"parallel": False}})
        loaded = config_mod.load(self.project)
        self.assertIs(loaded.review_settings()["parallel"], False)
        self.assertEqual(loaded.optimization_settings()["level"], "aggressive")

    def test_a_listed_panel_replaces_the_fit_and_its_notes(self):
        self.fake_clis(claude=True)
        panel = [config_mod.make_reviewer("mine", "mock", "small")]
        self.write_global({"version": 1, "preset": "standard", "reviewers": panel})
        loaded = config_mod.load(self.project)
        self.assertEqual(self.ids(loaded), ["mine"])
        self.assertFalse(any("reviewer seat" in note for note in loaded.preset_notes), loaded.preset_notes)

    def test_an_unknown_preset_is_a_problem_and_expands_nothing(self):
        self.fake_clis(claude=True)
        self.write_global({"version": 1, "preset": "bogus"})
        loaded = config_mod.load(self.project, validate_result=False)
        self.assertEqual((loaded.preset, loaded.preset_source), (None, "invalid"))
        self.assertEqual(self.ids(loaded), [r["id"] for r in config_mod.default_config()["reviewers"]])
        self.assertIn(
            "preset: unknown 'bogus' (known: fast, quality, standard)", config_mod.validate(loaded.data)
        )
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.project)

    def test_a_project_file_cannot_name_a_preset(self):
        self.fake_clis(claude=True, codex=True)
        self.write_global({"version": 1, "preset": "fast"})
        self.write(".dev-orchestra.yaml", "version: 1\npreset: quality\n")
        with self.assertRaises(config_mod.ConfigError) as caught:
            config_mod.load(self.project)
        self.assertIn("preset: only the global file can name a preset for now", str(caught.exception))
        loaded = config_mod.load(self.project, validate_result=False)
        self.assertEqual(loaded.preset, "fast")
        self.assertEqual(loaded.optimization_settings()["level"], "aggressive")

    def test_standard_is_implicit_with_no_file(self):
        self.fake_clis(claude=True)
        loaded = config_mod.load(self.project)
        self.assertTrue(loaded.used_defaults)
        self.assertEqual((loaded.preset, loaded.preset_source), ("standard", "implicit"))
        self.assertEqual(self.ids(loaded), CLAUDE_FIT)

    def test_standard_is_implicit_in_a_file_that_names_none(self):
        self.fake_clis(claude=True)
        for layer in ({"version": 1}, {"version": 1, "review": {"parallel": False}}):
            with self.subTest(layer=layer):
                self.write_global(layer)
                loaded = config_mod.load(self.project)
                self.assertFalse(loaded.used_defaults)
                self.assertEqual((loaded.preset, loaded.preset_source), ("standard", "implicit"))
                self.assertEqual(self.ids(loaded), CLAUDE_FIT)

    def test_a_role_a_file_sets_is_not_fitted(self):
        self.fake_clis(codex=True)
        for role_layer in (
            {"model": {"family": "sonnet"}},
            {"options": {"permission_mode": "acceptEdits"}},
        ):
            with self.subTest(implementer=role_layer):
                self.write_global({"version": 1, "implementer": role_layer})
                loaded = config_mod.load(self.project, validate_result=False)
                implementer = loaded.role("implementer")
                self.assertEqual(implementer["provider"], "claude")
                family = role_layer.get("model", {}).get("family", "opus")
                self.assertEqual(implementer["model"]["family"], family)
                self.assertEqual(loaded.role("orchestrator")["provider"], "codex")
                notes = loaded.preset_notes
                self.assertIn("implementer is set by the global file and was not fitted", notes)
                self.assertFalse(any("implementer went to" in note for note in notes))

    def test_a_preset_is_not_reported_as_pinned(self):
        self.fake_clis(claude=True, codex=True)
        self.write_global({"version": 1, "preset": "quality"})
        loaded = config_mod.load(self.project)
        self.assertEqual(config_mod.pinned_differences(loaded.files_data), [])
        pinned = [entry["setting"] for entry in config_mod.pinned_differences(loaded.data)]
        self.assertIn("optimization.level", pinned)


if __name__ == "__main__":
    unittest.main()
