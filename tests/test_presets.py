"""Presets, and how each is fitted to the CLIs installed on the machine."""

from __future__ import annotations

import unittest

from helpers import IsolatedCase

from orchestrator import config as config_mod
from orchestrator import presets
from orchestrator.providers import get_provider

BOTH = ["claude", "codex"]
ENVIRONMENTS = {"both": BOTH, "claude": ["claude"], "codex": ["codex"], "neither": []}
CODEX = "recommended-coding"

#: (id, provider, family, role, when) for every preset in every environment.
#: Neither CLI installed expands as written, which is the two-vendor panel.
PANELS = {
    ("quality", "both"): [
        ("claude-general", "claude", "fable", "general", None),
        ("codex-security", "codex", CODEX, "security", None),
        ("claude-architecture", "claude", "opus", "architecture", None),
    ],
    ("quality", "claude"): [
        ("claude-general", "claude", "fable", "general", None),
        ("claude-security", "claude", "opus", "security", None),
        ("claude-architecture", "claude", "opus", "architecture", None),
    ],
    ("quality", "codex"): [
        ("codex-general", "codex", CODEX, "general", None),
        ("codex-security", "codex", CODEX, "security", None),
        ("codex-architecture", "codex", CODEX, "architecture", None),
    ],
    ("standard", "both"): [
        ("claude-general", "claude", "opus", "general", None),
        ("codex-general", "codex", CODEX, "general", None),
    ],
    ("standard", "claude"): [
        ("claude-general", "claude", "opus", "general", None),
        ("claude-general-2", "claude", "sonnet", "general", None),
    ],
    ("standard", "codex"): [
        ("codex-general", "codex", CODEX, "general", None),
    ],
    ("fast", "both"): [
        ("codex-general", "codex", CODEX, "general", None),
        ("claude-security", "claude", "opus", "security", "high-risk"),
    ],
    ("fast", "claude"): [
        ("claude-general", "claude", "opus", "general", None),
        ("claude-security", "claude", "opus", "security", "high-risk"),
    ],
    ("fast", "codex"): [
        ("codex-general", "codex", CODEX, "general", None),
        ("codex-security", "codex", CODEX, "security", "high-risk"),
    ],
}
for _name in presets.NAMES:
    PANELS[(_name, "neither")] = PANELS[(_name, "both")]

#: Claude family per role as the preset is written: orchestrator, architect,
#: implementer, review_fixer.
ROLE_FAMILIES = {
    "quality": ("opus", "fable", "fable", "fable"),
    "standard": ("sonnet", "fable", "opus", "opus"),
    "fast": ("sonnet", "opus", "sonnet", "sonnet"),
}


def panel(fit):
    return [
        (r["id"], r["provider"], r["model"]["family"], r["role"], r.get("when"))
        for r in fit.values["reviewers"]
    ]


class TestExpansion(unittest.TestCase):
    def test_every_panel_in_every_environment(self):
        for (name, env), expected in PANELS.items():
            with self.subTest(preset=name, environment=env):
                self.assertEqual(panel(presets.expand(name, ENVIRONMENTS[env])), expected)

    def test_roles_go_to_the_first_installed_cli(self):
        for name in presets.NAMES:
            for env, installed in ENVIRONMENTS.items():
                with self.subTest(preset=name, environment=env):
                    values = presets.expand(name, installed).values
                    families = zip(config_mod.KNOWN_ROLES, ROLE_FAMILIES[name], strict=True)
                    for role, family in families:
                        provider = "codex" if env == "codex" else "claude"
                        if provider == "codex":
                            family = CODEX
                        expected = {"provider": provider, "model": {"family": family, "version": "latest"}}
                        self.assertEqual(values[role], expected)

    def test_no_seat_is_duplicated(self):
        for name in presets.NAMES:
            for env, installed in ENVIRONMENTS.items():
                with self.subTest(preset=name, environment=env):
                    seats = panel(presets.expand(name, installed))
                    self.assertEqual(len({seat[0] for seat in seats}), len(seats))
                    self.assertEqual(len({seat[1:] for seat in seats}), len(seats))

    def test_the_other_keys_each_preset_sets(self):
        quality = presets.expand("quality", BOTH).values
        self.assertEqual(quality["review"], {"design": {"enabled": True}})
        self.assertEqual(quality["optimization"], {"level": "quality"})
        fast = presets.expand("fast", BOTH).values
        self.assertNotIn("review", fast)
        self.assertEqual(fast["optimization"], {"level": "aggressive"})
        standard = presets.expand("standard", BOTH).values
        self.assertNotIn("review", standard)
        self.assertNotIn("optimization", standard)

    def test_standard_on_two_clis_is_the_built_in_defaults_exactly(self):
        data, fit, name, source = config_mod.compose({}, {}, BOTH)
        self.assertEqual(data, config_mod.default_config())
        self.assertEqual((fit.notes, name, source), ([], "standard", "implicit"))
        self.assertEqual(config_mod.compose({}, {}, [])[0], config_mod.default_config())

    def test_fast_keeps_one_reviewer_always_and_one_on_high_risk(self):
        for env, installed in ENVIRONMENTS.items():
            with self.subTest(environment=env):
                conditions = [seat[4] for seat in panel(presets.expand("fast", installed))]
                self.assertEqual(conditions, [None, "high-risk"])

    def test_the_governed_keys_are_what_an_expansion_sets(self):
        values = presets.expand("quality", BOTH).values
        for dotted in presets.GOVERNED:
            self.assertIsNot(config_mod.get_path(values, dotted, None), None, dotted)


class TestNotes(unittest.TestCase):
    def test_two_clis_and_none_need_no_notes(self):
        for name in presets.NAMES:
            self.assertEqual(presets.expand(name, BOTH).notes, [])
            self.assertEqual(presets.expand(name, []).notes, [])

    def test_a_seat_that_changed_vendor_is_named(self):
        self.assertEqual(
            presets.expand("standard", ["claude"]).notes,
            [
                "codex not found on PATH: reviewer seat 2 (general) went to claude as claude-general-2 "
                "(sonnet)"
            ],
        )

    def test_codex_alone_moves_the_roles_and_drops_the_repeat(self):
        fit = presets.expand("standard", ["codex"])
        for role in config_mod.KNOWN_ROLES:
            self.assertIn("claude not found on PATH: %s went to codex (%s)" % (role, CODEX), fit.notes)
        self.assertIn(
            "claude not found on PATH: reviewer seat 1 (general) went to codex as codex-general "
            "(recommended-coding)",
            fit.notes,
        )
        self.assertIn(
            "claude not found on PATH: reviewer seat 2 (general) was not added; it would repeat "
            "codex-general",
            fit.notes,
        )
        self.assertEqual(len(fit.notes), len(fit.subjects))

    def test_render_notes(self):
        self.assertEqual(presets.render_notes(presets.expand("standard", BOTH)), "")
        rendered = presets.render_notes(presets.expand("fast", ["claude"]))
        self.assertTrue(rendered.startswith("note: codex not found on PATH: reviewer seat 1 (general)"))


class TestEveryExpansionWorks(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.fake_clis()

    def test_every_expansion_validates_and_resolves_offline(self):
        for name in presets.NAMES:
            for env, installed in ENVIRONMENTS.items():
                with self.subTest(preset=name, environment=env):
                    data = config_mod.compose({"preset": name}, {}, installed)[0]
                    self.assertEqual(config_mod.validate(data), [])
                    specs = [data[role] for role in config_mod.KNOWN_ROLES]
                    specs.extend(data["reviewers"])
                    for spec in specs:
                        get_provider(spec["provider"]).resolve_model(spec["model"])


class TestInstalledProviders(IsolatedCase):
    def test_only_what_is_on_path_in_fitting_order(self):
        self.fake_clis(codex=True)
        self.assertEqual(presets.installed_providers(), ["codex"])
        self.fake_clis(claude=True, codex=True)
        self.assertEqual(presets.installed_providers(), ["claude", "codex"])

    def test_a_user_adapter_is_never_fitted(self):
        self.fake_clis()
        self.write_user_provider("mycli")
        self.load_user_providers()
        cls = type(get_provider("mycli"))
        self.addCleanup(setattr, cls, "which", cls.which)
        setattr(cls, "which", lambda self: "mycli")
        self.assertEqual(presets.installed_providers(), [])
        loaded = config_mod.load(self.project, validate_result=False)
        self.assertNotIn("mycli", config_mod.referenced_providers(loaded.data))


class TestWithPreset(unittest.TestCase):
    def test_a_fresh_layer_is_only_the_name(self):
        self.assertEqual(presets.with_preset(None, "fast"), {"version": 1, "preset": "fast"})

    def test_what_a_preset_governs_goes_and_the_rest_stays(self):
        layer = {
            "version": 1,
            "preset": "quality",
            "implementer": {"provider": "claude"},
            "reviewers": [],
            "review": {"parallel": False, "design": {"enabled": False}},
            "optimization": {"level": "quality"},
        }
        self.assertEqual(
            presets.with_preset(layer, "fast"),
            {"version": 1, "preset": "fast", "review": {"parallel": False}},
        )
        self.assertEqual(layer["preset"], "quality")

    def test_a_roles_options_stay_and_only_its_provider_and_model_go(self):
        options = {"permission_mode": "acceptEdits"}
        layer = {
            "version": 1,
            "implementer": {"provider": "claude", "model": {"family": "opus"}, "options": options},
        }
        self.assertEqual(
            presets.with_preset(layer, "fast"),
            {"version": 1, "preset": "fast", "implementer": {"options": options}},
        )


class TestTheImplementersVendor(unittest.TestCase):
    def test_the_panel_is_dealt_around_the_implementer_a_file_set(self):
        layer = {
            "preset": "fast",
            "implementer": {"provider": "codex", "model": {"family": CODEX, "version": "latest"}},
        }
        reviewers = config_mod.compose(layer, {}, BOTH)[0]["reviewers"]
        always = [(r["provider"], r["role"]) for r in reviewers if not r.get("when")]
        self.assertEqual(always, [("claude", "general")])


if __name__ == "__main__":
    unittest.main()
