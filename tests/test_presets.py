"""Presets, and how each is fitted to the CLIs installed on the machine."""

from __future__ import annotations

import os
import unittest
from typing import Any, List, Tuple

from helpers import IsolatedCase, user_adapter_source

from orchestrator import config as config_mod
from orchestrator import doctor, presets
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
            for env, installed in {**ENVIRONMENTS, "agy": ["agy"]}.items():
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

    def test_a_user_adapter_that_does_not_opt_in_is_never_fitted(self):
        self.fake_clis()
        self.write_user_provider("mycli")
        self.load_user_providers()
        cls = type(get_provider("mycli"))
        self.addCleanup(setattr, cls, "which", cls.which)
        setattr(cls, "which", lambda self: "mycli")
        self.assertEqual(presets.installed_providers(), [])
        loaded = config_mod.load(self.project, validate_result=False)
        self.assertNotIn("mycli", config_mod.referenced_providers(loaded.data))


#: A declaration that opts in, with the family the minimal adapter resolves.
OPTED_IN = 'preset_family = "default"\n'


class TestUserAdaptersInPresets(IsolatedCase):
    """User adapters that declare ``preset_family``, fitted only where the
    built-in CLIs are missing, and never by anything a file says."""

    def setUp(self):
        super().setUp()
        self.fake_clis()

    def adapters(self, *specs, on_path=True):
        """Write and load one adapter per ``(name, attributes, enforcement)``,
        each on PATH unless ``on_path`` is false. Nothing is ever spawned."""
        for name, attributes, enforcement in specs:
            self.write_user_provider(name, user_adapter_source(name, attributes, enforcement))
        self.assertEqual(self.load_user_providers()["errors"], [])
        for name, _attributes, _enforcement in specs:
            cls = type(get_provider(name))
            replacements: List[Tuple[str, Any]] = [("_capture", lambda provider, command, timeout=30: None)]
            if on_path:
                replacements.append(("which", lambda provider: provider.executable))
            for attribute, value in replacements:
                self.addCleanup(setattr, cls, attribute, getattr(cls, attribute))
                setattr(cls, attribute, value)

    def adapter(self, attributes=OPTED_IN, enforcement=None, on_path=True):
        self.adapters(("mycli", attributes, enforcement), on_path=on_path)

    def fit_line(self):
        """The doctor report, and its ``Preset fitting:`` lines."""
        report = doctor.collect(self.project, probe_models=False)
        lines = [line for line in doctor.render(report).splitlines() if line.startswith("  Preset fitting:")]
        return report, lines

    def assert_write_roles_only(self, name="mycli", family="default"):
        self.assertEqual(presets.installed_providers(), [name])
        for preset in presets.NAMES:
            with self.subTest(preset=preset):
                fit = presets.expand(preset, [name])
                for role in ("implementer", "review_fixer"):
                    self.assertEqual(fit.values[role]["provider"], name)
                    self.assertEqual(fit.values[role]["model"]["family"], family)
                    self.assertIn(
                        "claude, codex, agy not found on PATH: %s went to %s (%s)" % (role, name, family),
                        fit.notes,
                    )
                as_written = presets.expand(preset, [])
                for role in config_mod.READ_ONLY_ROLES:
                    self.assertEqual(fit.values[role], as_written.values[role])
                self.assertEqual(fit.values["reviewers"], as_written.values["reviewers"])

    def test_unspecified_enforcement_takes_the_write_roles_only(self):
        """The ``localllm`` report: a local model that declared its family."""
        self.adapter('preset_family = "qwen3.6-35b-a3b"\n', "unspecified")
        self.assert_write_roles_only(family="qwen3.6-35b-a3b")
        _, lines = self.fit_line()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("family qwen3.6-35b-a3b; takes the write roles only", lines[0])
        self.assertIn("its read-only enforcement is unspecified", lines[0])

    def test_partial_enforcement_takes_every_role_and_seat(self):
        self.adapter(enforcement="partial")
        self.assertEqual(presets.installed_providers(), ["mycli"])
        expected = {"provider": "mycli", "model": {"family": "default", "version": "latest"}}
        for preset in presets.NAMES:
            with self.subTest(preset=preset):
                fit = presets.expand(preset, ["mycli"])
                for role in config_mod.KNOWN_ROLES:
                    self.assertEqual(fit.values[role], expected)
                self.assertEqual({seat[1] for seat in panel(fit)}, {"mycli"})
                self.assertIn("claude, codex not found on PATH: architect went to mycli (default)", fit.notes)
                self.assertIn(
                    "claude, codex not found on PATH: reviewer seat 1 (general) went to mycli as "
                    "mycli-general (default)",
                    fit.notes,
                )
                self.assertFalse([note for note in fit.notes if "agy cannot be held" in note])
                data = config_mod.compose({"preset": preset}, {}, ["mycli"])[0]
                self.assertEqual(config_mod.validate(data), [])
                specs = [data[role] for role in config_mod.KNOWN_ROLES]
                specs.extend(data["reviewers"])
                for spec in specs:
                    get_provider(spec["provider"]).resolve_model(spec["model"])
        report, lines = self.fit_line()
        self.assertTrue(report["providers"]["mycli"]["preset_fit"]["seats"])
        self.assertEqual(report["providers"]["mycli"]["preset_fit"]["family"], "default")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("takes the read-only roles and reviewer seats when neither Claude nor Codex", lines[0])
        self.assertIn("(read-only is partial)", lines[0])
        rendered = doctor.render(report)
        self.assertIn("  Read-only runs: enforced by --read-only; shell commands still run", rendered)

    def test_verified_is_as_good_as_partial(self):
        self.adapter(enforcement="verified")
        fit = presets.expand("quality", presets.installed_providers())
        self.assertEqual(fit.values["architect"]["provider"], "mycli")
        self.assertEqual({seat[1] for seat in panel(fit)}, {"mycli"})

    def test_a_status_below_the_bar_takes_the_write_roles_only(self):
        for status in ("unenforced", "unsupported", "unverified"):
            with self.subTest(status=status):
                self.adapters(("mycli", OPTED_IN, status))
                self.assert_write_roles_only()
                _, lines = self.fit_line()
                self.assertIn("its read-only enforcement is %s" % status, lines[0])
                self.assertIn("a read-only seat needs a static verified or partial report", lines[0])

    def test_a_report_that_is_not_static_is_never_asked(self):
        attributes = (
            OPTED_IN
            + "calls = []\n\n"
            + "def read_only_enforcement(self):\n"
            + "    type(self).calls.append(1)\n"
            + "    return {'status': 'verified', 'mechanism': 'x', 'detail': 'x'}\n"
        )
        self.adapter(attributes)
        self.assert_write_roles_only()
        _, lines = self.fit_line()
        self.assertIn("its read-only report is not static (static_enforcement = False)", lines[0])
        self.assertEqual(vars(type(get_provider("mycli")))["calls"], [])

    def test_a_static_report_on_the_instance_is_ignored_everywhere(self):
        attributes = (
            OPTED_IN
            + "def __init__(self, executable=None):\n"
            + "    super().__init__(executable)\n"
            + "    self.static_enforcement = True\n\n"
            + "def read_only_enforcement(self):\n"
            + "    return {'status': 'unenforced', 'mechanism': 'x', 'detail': 'x'}\n"
        )
        self.adapter(attributes)
        self.assert_write_roles_only()
        report, lines = self.fit_line()
        self.assertIn("its read-only report is not static (static_enforcement = False)", lines[0])
        # doctor --fast and the config warnings agree with the fit: the report
        # is not a constant, so none of them asks it.
        entry = report["providers"]["mycli"]
        self.assertEqual(entry["read_only_enforcement"], {"status": "not-checked"})
        self.assertIsNone(config_mod._warned_provider("mycli"))

    def test_a_static_report_may_be_any_mapping(self):
        attributes = (
            OPTED_IN
            + "static_enforcement = True\n\n"
            + "def read_only_enforcement(self):\n"
            + "    import types\n"
            + "    return types.MappingProxyType({'status': 'verified', 'mechanism': 'x', 'detail': 'x'})\n"
        )
        self.adapter(attributes)
        self.assertTrue(presets.user_fit(get_provider("mycli")).seats)
        fit = presets.expand("quality", presets.installed_providers())
        self.assertEqual(fit.values["architect"]["provider"], "mycli")
        _, lines = self.fit_line()
        self.assertIn("(read-only is verified)", lines[0])

    def test_a_report_that_raises_takes_the_write_roles_only(self):
        attributes = (
            OPTED_IN
            + "static_enforcement = True\n\n"
            + "def read_only_enforcement(self):\n"
            + "    raise RuntimeError('no report today')\n"
        )
        self.adapter(attributes)
        self.assert_write_roles_only()
        report, lines = self.fit_line()
        self.assertIn("read_only_enforcement() raised RuntimeError: no report today", lines[0])
        entry = report["providers"]["mycli"]
        self.assertTrue(entry["installed"])
        self.assertEqual(entry["read_only_enforcement"]["status"], "error")
        self.assertIn("preset_fit", entry)
        raised = [problem for problem in report["problems"] if "read_only_enforcement()" in problem]
        self.assertEqual(len(raised), 1, report["problems"])
        self.assertTrue(raised[0].startswith("provider mycli (user module "), raised[0])
        rendered = doctor.render(report)
        self.assertIn("  Read-only runs: adapter error -- RuntimeError: no report today", rendered)
        self.assertIn("  Preset fitting: ", rendered)

    def test_a_which_that_raises_is_not_installed(self):
        raising = OPTED_IN + "def which(self):\n    raise RuntimeError('no PATH today')\n"
        self.adapter(raising, "partial", on_path=False)
        self.assertEqual(presets.installed_providers(), [])

    def test_a_declaration_on_the_instance_does_not_opt_in(self):
        attributes = (
            "def __init__(self, executable=None):\n"
            "    super().__init__(executable)\n"
            "    self.preset_family = 'default'\n"
        )
        self.adapter(attributes, "partial")
        self.assertEqual(presets.installed_providers(), [])
        for preset in presets.NAMES:
            self.assertEqual(presets.expand(preset, ["mycli"]), presets.expand(preset, []))
        _, lines = self.fit_line()
        self.assertEqual(lines, [])

    def test_an_invalid_declaration_is_not_fitted_at_all(self):
        for declared in ("3", '""', '"  "'):
            with self.subTest(declared=declared):
                self.adapters(("mycli", "preset_family = %s\n" % declared, "partial"))
                self.assertEqual(presets.installed_providers(), [])
                for preset in presets.NAMES:
                    self.assertEqual(presets.expand(preset, ["mycli"]), presets.expand(preset, []))
                _, lines = self.fit_line()
                invalid = "preset_family must be a non-empty string; the adapter is not fitted to presets"
                self.assertEqual(lines, ["  Preset fitting: %s" % invalid])

    def test_a_padded_declaration_is_stored_stripped(self):
        self.adapter('preset_family = " default "\n', "partial")
        self.assertEqual(presets.user_fit(get_provider("mycli")).family, "default")
        fit = presets.expand("standard", presets.installed_providers())
        self.assertEqual(fit.values["architect"]["model"]["family"], "default")

    def test_a_name_that_is_not_registered_is_not_fitted(self):
        for preset in presets.NAMES:
            with self.subTest(preset=preset):
                self.assertEqual(presets.expand(preset, ["nosuch"]), presets.expand(preset, []))
                claude = presets.expand(preset, ["claude"])
                self.assertEqual(presets.expand(preset, ["claude", "nosuch"]), claude)

    def test_two_adapters_are_fitted_in_name_order(self):
        self.adapters(("beta", OPTED_IN, "partial"), ("alpha", OPTED_IN, "partial"))
        self.assertEqual(presets.installed_providers(), ["alpha", "beta"])
        for preset in presets.NAMES:
            with self.subTest(preset=preset):
                in_order = presets.expand(preset, ["alpha", "beta"])
                self.assertEqual(presets.expand(preset, ["beta", "alpha"]), in_order)
        fit = presets.expand("standard", ["beta", "alpha"])
        self.assertEqual(fit.values["architect"]["provider"], "alpha")
        self.assertEqual([seat[1] for seat in panel(fit)], ["alpha", "beta"])

    def test_a_project_implementer_rotates_the_panel_between_eligible_adapters(self):
        """The one influence a project file has, as between Claude and Codex."""
        self.adapters(("alpha", OPTED_IN, "partial"), ("beta", OPTED_IN, "partial"))
        project = {"implementer": {"provider": "beta"}}
        first = {"standard": "beta", "fast": "alpha"}
        for preset, expected in first.items():
            with self.subTest(preset=preset):
                reviewers = config_mod.compose({"preset": preset}, project, ["alpha", "beta"])[0]["reviewers"]
                always = [r["provider"] for r in reviewers if not r.get("when")]
                self.assertEqual(always[0], expected)
                self.assertTrue({r["provider"] for r in reviewers} <= {"alpha", "beta"})

    def test_a_seat_a_file_sets_keeps_no_note_naming_the_adapter(self):
        self.adapter(enforcement="partial")
        installed = presets.installed_providers()
        notes = config_mod.compose({"architect": {"provider": "mock"}}, {}, installed)[1].notes
        self.assertFalse([note for note in notes if "architect went to mycli" in note], notes)
        self.assertIn("claude, codex not found on PATH: orchestrator went to mycli (default)", notes)
        reviewers = [{"id": "m1", "provider": "mock", "role": "general"}]
        notes = config_mod.compose({"reviewers": reviewers}, {}, installed)[1].notes
        self.assertFalse([note for note in notes if "reviewer seat" in note], notes)
        self.assertIn("claude, codex not found on PATH: architect went to mycli (default)", notes)

    def test_with_claude_installed_the_adapter_takes_nothing(self):
        self.adapter(enforcement="partial")
        for preset in presets.NAMES:
            with self.subTest(preset=preset):
                claude = presets.expand(preset, ["claude"])
                self.assertEqual(presets.expand(preset, ["claude", "mycli"]), claude)
        self.fake_clis(claude=True)
        _, lines = self.fit_line()
        self.assertIn("when neither Claude nor Codex is installed", lines[0])

    def test_eligible_adapter_and_agy_share_the_roles(self):
        self.adapter(enforcement="partial")
        for preset in presets.NAMES:
            with self.subTest(preset=preset):
                fit = presets.expand(preset, ["agy", "mycli"])
                self.assertEqual(fit.values["implementer"]["provider"], "agy")
                self.assertEqual(fit.values["review_fixer"]["provider"], "agy")
                self.assertEqual(fit.values["orchestrator"]["provider"], "mycli")
                self.assertEqual(fit.values["architect"]["provider"], "mycli")
                self.assertEqual({seat[1] for seat in panel(fit)}, {"mycli"})

    def test_write_only_adapter_and_agy_leave_everything_to_agy(self):
        self.adapter(enforcement="unspecified")
        for preset in presets.NAMES:
            with self.subTest(preset=preset):
                fit = presets.expand(preset, ["agy", "mycli"])
                self.assertEqual({fit.values[role]["provider"] for role in config_mod.KNOWN_ROLES}, {"agy"})
                self.assertEqual({seat[1] for seat in panel(fit)}, {"agy"})
                self.assertEqual(fit, presets.expand(preset, ["agy"]))

    def test_expansions_with_agy_and_an_adapter_validate_and_resolve_offline(self):
        for enforcement in ("partial", "unspecified"):
            self.adapters(("mycli", OPTED_IN, enforcement))
            for preset in presets.NAMES:
                with self.subTest(enforcement=enforcement, preset=preset):
                    data = config_mod.compose({"preset": preset}, {}, ["agy", "mycli"])[0]
                    self.assertEqual(config_mod.validate(data), [])
                    specs = [data[role] for role in config_mod.KNOWN_ROLES]
                    specs.extend(data["reviewers"])
                    for spec in specs:
                        get_provider(spec["provider"]).resolve_model(spec["model"])

    def test_turning_user_adapters_off_turns_them_out_of_the_fit(self):
        self.adapter(enforcement="partial")
        os.environ["DEV_ORCHESTRA_NO_USER_PROVIDERS"] = "1"
        self.load_user_providers()
        self.assertEqual(presets.installed_providers(), [])

    def test_a_project_file_cannot_opt_an_adapter_in(self):
        self.adapter(attributes="", enforcement="partial")
        self.fake_clis(claude=True)
        installed = presets.installed_providers()
        self.assertEqual(installed, ["claude"])
        data = config_mod.compose({}, {"implementer": {"provider": "mycli"}}, installed)[0]
        for role in config_mod.READ_ONLY_ROLES:
            self.assertEqual(data[role]["provider"], "claude")
        self.assertEqual({r["provider"] for r in data["reviewers"]}, {"claude"})

    def test_a_registered_adapter_that_does_not_opt_in_changes_no_fit(self):
        self.adapter(attributes="", enforcement="partial")
        for preset in presets.NAMES:
            for env, installed in ENVIRONMENTS.items():
                with self.subTest(preset=preset, environment=env):
                    without = presets.expand(preset, installed)
                    self.assertEqual(presets.expand(preset, [*installed, "mycli"]), without)


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
