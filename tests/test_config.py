"""Configuration layering, validation and reviewer management."""

from __future__ import annotations

import os
import unittest

from helpers import IsolatedCase

from orchestrator import config as config_mod
from orchestrator import wizard

DESIGN_DEFAULTS = {
    "require_approval": True,
    "resume": {"max_age_seconds": 3600, "max_context_tokens": None},
}


class TestDefaults(IsolatedCase):
    def test_no_config_falls_back_to_builtin_defaults(self):
        loaded = config_mod.load(self.project)
        self.assertFalse(loaded.exists)
        self.assertTrue(loaded.used_defaults)
        self.assertEqual(loaded.role("orchestrator")["model"]["family"], "sonnet")
        self.assertEqual(loaded.role("architect")["model"]["family"], "fable")
        self.assertEqual(loaded.role("implementer")["model"]["family"], "opus")
        self.assertEqual(loaded.role("review_fixer")["model"]["family"], "opus")
        self.assertEqual(len(loaded.reviewers()), 2)

    def test_defaults_never_pin_a_dated_model_id(self):
        text = str(config_mod.default_config())
        self.assertNotIn("id", config_mod.default_config()["implementer"]["model"])
        for token in ("2026", "2025", "-2024"):
            self.assertNotIn(token, text)

    def test_defaults_are_valid(self):
        self.assertEqual(config_mod.validate(config_mod.default_config()), [])

    def test_the_design_review_is_off_by_default(self):
        """Turning it on costs a reviewer run per panel member per round plus
        an architect re-run, so existing workflows must not inherit it."""
        design = config_mod.default_config()["review"]["design"]
        self.assertEqual(design, {"enabled": False, "max_iterations": 2})

    def test_design_settings_are_filled_in_for_a_config_that_omits_them(self):
        self.write(".dev-orchestra.yaml", "version: 1\nreview:\n  max_review_iterations: 1\n")
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.design_review_settings(), {"enabled": False, "max_iterations": 2})

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
        self.assertTrue(wizard._approval_required({"design": {"require_approval": None}}))
        self.assertFalse(wizard._approval_required({"design": {"require_approval": False}}))

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

    def test_project_config_is_found_from_a_subdirectory(self):
        self.write(".dev-orchestra.yaml", "version: 1\n")
        nested = os.path.join(self.project, "a", "b")
        os.makedirs(nested)
        self.assertEqual(os.path.dirname(config_mod.find_project_config(nested)), self.project)

    def test_search_stops_at_the_git_root(self):
        self.init_git_repo()
        outside = os.path.join(self.tmp, ".dev-orchestra.yaml")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("version: 1\n")
        self.assertIsNone(config_mod.find_project_config(self.project))

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
        return {entry.label: entry for entry in config_mod.read_only_raw_args(loaded)}

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
        self.assertEqual(list(config_mod.project_raw_arg_refusals(loaded)), [label])

    def test_project_args_are_refused_whatever_they_are(self):
        self.write_project(
            'architect:\n  options:\n    args: ["--add-dir", "../x"]\n'
            'implementer:\n  options:\n    args: ["--tools", "default"]\n'
            'reviewers:\n  - id: mine\n    provider: claude\n    options:\n      args: ["--add-dir", "x"]\n'
        )
        loaded = config_mod.load(self.project)
        refusals = config_mod.project_raw_arg_refusals(loaded)
        self.assertEqual(sorted(refusals), ["architect", "reviewers[0]"])
        self.assertIn("set in the project config (.dev-orchestra.yaml)", refusals["architect"])
        self.assertNotIn("../x", refusals["architect"])
        self.assertEqual(list(config_mod.reviewer_raw_arg_refusals(loaded)), ["mine"])

    def test_global_args_are_allowed_when_the_adapter_accepts_them(self):
        self.write_global('architect:\n  options:\n    args: ["--add-dir", "../x"]\n')
        loaded = config_mod.load(self.project)
        self.assertEqual(config_mod.project_raw_arg_refusals(loaded), {})
        self.assertEqual(config_mod.read_only_arg_warnings(loaded), [])

    def test_warnings_cover_both_layers_and_skip_implement_roles(self):
        self.write_global(
            'architect:\n  options:\n    args: ["--permission-mode", "acceptEdits"]\n'
            'implementer:\n  options:\n    args: ["--tools", "default"]\n'
        )
        self.write_project('orchestrator:\n  options:\n    args: ["--add-dir", "x"]\n')
        warnings = config_mod.read_only_arg_warnings(config_mod.load(self.project))
        self.assertEqual(len(warnings), 3, warnings)
        self.assertTrue(warnings[0].startswith("orchestrator: options.args is set in the project config"))
        expected = "architect: read-only run: '--permission-mode' (token 1 of 2 in options.args)"
        self.assertIn(expected, warnings[1])
        self.assertNotIn("acceptEdits", " ".join(warnings))
        self.assertFalse(any(w.startswith("implementer") for w in warnings))

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
        data = config_mod.default_config()
        data["review"]["design"]["enabled"] = "yes"
        self.assertTrue(any("review.design.enabled" in p for p in config_mod.validate(data)))

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
        from orchestrator import providers

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
        self.assertEqual(data["reviewers"], [])

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


if __name__ == "__main__":
    unittest.main()
