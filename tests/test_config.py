"""Configuration layering, validation and reviewer management."""

from __future__ import annotations

import os
import unittest

from helpers import IsolatedCase

from orchestrator import config as config_mod


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


class TestValidation(IsolatedCase):
    def test_invalid_provider_is_rejected(self):
        data = config_mod.default_config()
        data["implementer"]["provider"] = "nonexistent"
        problems = config_mod.validate(data)
        self.assertTrue(any("unknown provider" in p for p in problems))

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


class TestPaths(IsolatedCase):
    def test_env_override_wins(self):
        explicit = os.path.join(self.tmp, "explicit.yaml")
        os.environ["DEV_ORCHESTRA_CONFIG"] = explicit
        self.assertEqual(config_mod.global_config_path(), explicit)

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
