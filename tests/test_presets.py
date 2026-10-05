"""Presets, and how each is fitted to the CLIs installed on the machine."""

from __future__ import annotations

import os
import unittest
from typing import Any, List, Tuple

from helpers import IsolatedCase, user_adapter_source

from orchestrator import config as config_mod
from orchestrator import doctor, optimization, presets
from orchestrator import providers as providers_mod
from orchestrator.providers import get_provider

BOTH = ["claude", "codex"]
ENVIRONMENTS = {"both": BOTH, "claude": ["claude"], "codex": ["codex"], "neither": []}
CODEX = "recommended-coding"

#: (id, provider, family, role, when, high-risk family) for every preset in
#: every environment. Neither CLI installed expands as written, which is the
#: two-vendor panel. The sonnet test seat is a cheap one, which only Claude can
#: take, and a high-risk family is kept on Claude only.
PANELS = {
    ("quality", "both"): [
        ("claude-general", "claude", "fable", "general", None, None),
        ("codex-general", "codex", CODEX, "general", None, None),
        ("claude-security", "claude", "opus", "security", None, None),
        ("codex-security", "codex", CODEX, "security", None, None),
        ("claude-architecture", "claude", "opus", "architecture", None, None),
        ("claude-test", "claude", "opus", "test", None, None),
    ],
    ("quality", "claude"): [
        ("claude-general", "claude", "fable", "general", None, None),
        ("claude-security", "claude", "opus", "security", None, None),
        ("claude-architecture", "claude", "opus", "architecture", None, None),
        ("claude-test", "claude", "opus", "test", None, None),
    ],
    ("quality", "codex"): [
        ("codex-general", "codex", CODEX, "general", None, None),
        ("codex-security", "codex", CODEX, "security", None, None),
        ("codex-architecture", "codex", CODEX, "architecture", None, None),
        ("codex-test", "codex", CODEX, "test", None, None),
    ],
    ("quality", "agy"): [
        ("agy-general", "agy", "default", "general", None, None),
        ("agy-security", "agy", "default", "security", None, None),
        ("agy-architecture", "agy", "default", "architecture", None, None),
    ],
    ("standard", "both"): [
        ("claude-general", "claude", "opus", "general", None, None),
        ("codex-general", "codex", CODEX, "general", None, None),
        ("claude-security", "claude", "sonnet", "security", None, "opus"),
        ("claude-test", "claude", "sonnet", "test", None, None),
    ],
    ("standard", "claude"): [
        ("claude-general", "claude", "opus", "general", None, None),
        ("claude-general-2", "claude", "sonnet", "general", None, None),
        ("claude-security", "claude", "sonnet", "security", None, "opus"),
        ("claude-test", "claude", "sonnet", "test", None, None),
    ],
    ("standard", "codex"): [
        ("codex-general", "codex", CODEX, "general", None, None),
        ("codex-security", "codex", CODEX, "security", None, None),
    ],
    ("standard", "agy"): [
        ("agy-general", "agy", "default", "general", None, None),
    ],
    ("fast", "both"): [
        ("codex-general", "codex", CODEX, "general", None, None),
        ("claude-security", "claude", "sonnet", "security", "high-risk", None),
    ],
    ("fast", "claude"): [
        ("claude-general", "claude", "opus", "general", None, None),
        ("claude-security", "claude", "sonnet", "security", "high-risk", None),
    ],
    ("fast", "codex"): [
        ("codex-general", "codex", CODEX, "general", None, None),
        ("codex-security", "codex", CODEX, "security", "high-risk", None),
    ],
    ("fast", "agy"): [
        ("agy-general", "agy", "default", "general", None, None),
        ("agy-security", "agy", "default", "security", "high-risk", None),
    ],
}

#: The same for the design panel.
DESIGN_PANELS = {
    ("quality", "both"): [
        ("claude-general", "claude", "sonnet", "general", None, "opus"),
        ("codex-general", "codex", CODEX, "general", None, None),
        ("claude-security", "claude", "opus", "security", None, None),
        ("claude-test", "claude", "sonnet", "test", None, None),
        ("claude-architecture", "claude", "opus", "architecture", None, None),
    ],
    ("quality", "claude"): [
        ("claude-general", "claude", "sonnet", "general", None, "opus"),
        ("claude-security", "claude", "opus", "security", None, None),
        ("claude-test", "claude", "sonnet", "test", None, None),
        ("claude-architecture", "claude", "opus", "architecture", None, None),
    ],
    ("quality", "codex"): [
        ("codex-general", "codex", CODEX, "general", None, None),
        ("codex-security", "codex", CODEX, "security", None, None),
        ("codex-architecture", "codex", CODEX, "architecture", None, None),
    ],
    ("quality", "agy"): [
        ("agy-general", "agy", "default", "general", None, None),
    ],
    ("standard", "both"): [
        ("claude-general", "claude", "sonnet", "general", None, "opus"),
        ("claude-security", "claude", "sonnet", "security", None, None),
        ("claude-test", "claude", "sonnet", "test", None, None),
    ],
    ("standard", "codex"): [
        ("codex-general", "codex", CODEX, "general", None, None),
        ("codex-security", "codex", CODEX, "security", None, None),
    ],
    ("standard", "agy"): [
        ("agy-general", "agy", "default", "general", None, None),
    ],
    ("fast", "both"): [
        ("claude-general", "claude", "sonnet", "general", None, None),
    ],
    ("fast", "codex"): [
        ("codex-general", "codex", CODEX, "general", None, None),
    ],
    ("fast", "agy"): [
        ("agy-general", "agy", "default", "general", None, None),
    ],
}
for _name in presets.NAMES:
    PANELS[(_name, "neither")] = PANELS[(_name, "both")]
    DESIGN_PANELS[(_name, "neither")] = DESIGN_PANELS[(_name, "both")]
for _name in ("standard", "fast"):
    DESIGN_PANELS[(_name, "claude")] = DESIGN_PANELS[(_name, "both")]

#: Every environment the matrices cover, agy alone included.
MATRIX_ENVIRONMENTS = {**ENVIRONMENTS, "agy": ["agy"]}

#: Claude family per role as the preset is written: orchestrator, architect,
#: implementer, review_fixer.
ROLE_FAMILIES = {
    "quality": ("opus", "fable", "fable", "fable"),
    "standard": ("sonnet", "fable", "opus", "opus"),
    "fast": ("sonnet", "opus", "sonnet", "sonnet"),
}


def seats(reviewers):
    return [
        (
            r["id"],
            r["provider"],
            r["model"]["family"],
            r["role"],
            r.get("when"),
            (r.get("high_risk_model") or {}).get("family"),
        )
        for r in reviewers
    ]


def panel(fit):
    return seats(fit.values["reviewers"])


def design_panel(fit):
    return seats(config_mod.get_path(fit.values, config_mod.DESIGN_PANEL.reviewers) or [])


class TestExpansion(unittest.TestCase):
    def test_fit_matrix_code(self):
        for (name, env), expected in PANELS.items():
            with self.subTest(preset=name, environment=env):
                self.assertEqual(panel(presets.expand(name, MATRIX_ENVIRONMENTS[env])), expected)

    def test_fit_matrix_design(self):
        for (name, env), expected in DESIGN_PANELS.items():
            with self.subTest(preset=name, environment=env):
                self.assertEqual(design_panel(presets.expand(name, MATRIX_ENVIRONMENTS[env])), expected)

    def test_vendor_seats_do_not_rotate_with_the_implementer(self):
        rotated = presets.expand("quality", BOTH, "codex")
        self.assertEqual(panel(rotated), PANELS[("quality", "both")])
        self.assertEqual(design_panel(rotated), DESIGN_PANELS[("quality", "both")])
        self.assertEqual(rotated.notes, [])

    def test_high_risk_seat_is_full_not_cheap(self):
        """The merged security seat is dealt, so Codex alone keeps one; the test seat is cheap."""
        fit = presets.expand("standard", ["codex"])
        self.assertIn("codex-security", [seat[0] for seat in panel(fit)])
        self.assertNotIn("codex-test", [seat[0] for seat in panel(fit)])

    def test_claude_seat_is_dealt_when_claude_absent(self):
        fit = presets.expand("quality", ["codex"])
        self.assertEqual({seat[1] for seat in panel(fit)}, {"codex"})
        self.assertEqual(design_panel(fit)[0][:2], ("codex-general", "codex"))

    def test_presets_never_set_relevance(self):
        for name in presets.NAMES:
            for env, installed in MATRIX_ENVIRONMENTS.items():
                with self.subTest(preset=name, environment=env):
                    values = presets.expand(name, installed).values
                    design = config_mod.get_path(values, config_mod.DESIGN_PANEL.reviewers) or []
                    for reviewer in [*values["reviewers"], *design]:
                        self.assertNotIn("relevance", reviewer)
                    self.assertNotIn("skip_unneeded_roles", values.get("optimization") or {})

    def test_governed_includes_design_reviewers(self):
        self.assertIn("review.design.reviewers", presets.GOVERNED)

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
        self.assertEqual(set(quality["review"]["design"]), {"enabled", "reviewers"})
        self.assertEqual(quality["optimization"], {"level": "quality"})
        fast = presets.expand("fast", BOTH).values
        self.assertEqual(set(fast["review"]["design"]), {"enabled", "reviewers"})
        self.assertEqual(fast["optimization"], {"level": "aggressive"})
        standard = presets.expand("standard", BOTH).values
        self.assertEqual(set(standard["review"]), {"design"})
        self.assertEqual(set(standard["review"]["design"]), {"reviewers"})
        self.assertNotIn("optimization", standard)

    def test_quality_design_auto(self):
        self.assertEqual(presets.expand("quality", BOTH).values["review"]["design"]["enabled"], "auto")

    def test_fast_design_review_off(self):
        self.assertIs(presets.expand("fast", BOTH).values["review"]["design"]["enabled"], False)

    def test_standard_matches_default_config_plus_design_panel(self):
        for installed in (BOTH, []):
            with self.subTest(installed=installed):
                data, fit, name, source = config_mod.compose({}, {}, installed)
                design = data["review"]["design"].pop("reviewers")
                self.assertEqual(data, config_mod.default_config())
                self.assertEqual(seats(design), DESIGN_PANELS[("standard", "both")])
                self.assertEqual((fit.notes, name, source), ([], "standard", "implicit"))
                self.assertEqual([o.label() for o in fit.design_origins], ["fit design"] * 3)

    def test_standard_reads_back_high_risk_family_as_claude_seat(self):
        Seat = presets.Seat
        self.assertEqual(
            presets.PRESETS["standard"].seats,
            (
                Seat("general", "opus", "always"),
                Seat("general", "sonnet", "always"),
                Seat("security", "sonnet", "always", held=True, high_risk_family="opus", vendor="claude"),
                Seat("test", "sonnet", "always", cheap=True, held=True),
            ),
        )
        self.assertEqual(
            presets.PRESETS["quality"].seats[5], Seat("test", "opus", "always", held=True, vendor="claude")
        )

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

    def test_codex_alone_takes_no_cheap_seat_and_keeps_the_other_ids(self):
        fit = presets.expand("standard", ["codex"])
        skipped = [note for note in fit.notes if "no cheap model" in note]
        self.assertEqual(
            skipped,
            [
                "claude not found on PATH: reviewer seat 4 (test) was not added; codex has no cheap "
                "model named offline",
                "claude not found on PATH: design reviewer seat 3 (test) was not added; codex has no cheap "
                "model named offline",
            ],
        )
        subjects = [fit.subjects[fit.notes.index(note)] for note in skipped]
        self.assertEqual(subjects, ["reviewers", "review.design.reviewers"])
        self.assertEqual([seat[0] for seat in panel(fit)], ["codex-general", "codex-security"])

    def test_high_risk_family_dropped_off_claude_note(self):
        fit = presets.expand("standard", ["codex"])
        self.assertIn(
            "claude not found on PATH: reviewer seat 3 (security) went to codex as codex-security "
            "(recommended-coding); no high-risk model on codex",
            fit.notes,
        )
        self.assertIn(
            "claude not found on PATH: design reviewer seat 1 (general) went to codex as codex-general "
            "(recommended-coding); no high-risk model on codex",
            fit.notes,
        )
        self.assertNotIn("high_risk_model", fit.values["reviewers"][1])

    def test_second_vendor_seat_not_added_note(self):
        fit = presets.expand("quality", ["claude"])
        reason = "it is a second vendor's opinion and nothing installed stands in for one"
        self.assertEqual(
            list(zip(fit.notes, fit.subjects, strict=True)),
            [
                ("codex not found on PATH: reviewer seat 2 (general) was not added; " + reason, "reviewers"),
                ("codex not found on PATH: reviewer seat 4 (security) was not added; " + reason, "reviewers"),
                (
                    "codex not found on PATH: design reviewer seat 2 (general) was not added; " + reason,
                    "review.design.reviewers",
                ),
            ],
        )

    def test_agy_design_seat_names_the_design_list_to_keep_it_off_agy(self):
        fit = presets.expand("standard", ["agy"])
        self.assertIn(
            "claude, codex not found on PATH: design reviewer seat 1 (general) went to agy as agy-general "
            "(default); no high-risk model on agy; agy cannot be held to reading -- list "
            "review.design.reviewers in the global file to keep them off agy",
            fit.notes,
        )
        self.assertIn(
            "claude, codex not found on PATH: design reviewer seat 2 (security) was not added; "
            "agy cannot be held to reading",
            fit.notes,
        )

    def test_a_cheap_seat_that_is_added_gets_no_note(self):
        notes = presets.expand("standard", ["claude"]).notes
        self.assertFalse([note for note in notes if "seat 3" in note or "seat 4" in note], notes)

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
            for env, installed in MATRIX_ENVIRONMENTS.items():
                with self.subTest(preset=name, environment=env):
                    data = config_mod.compose({"preset": name}, {}, installed)[0]
                    self.assertEqual(config_mod.validate(data), [])
                    assert_resolves_offline(data)


def assert_resolves_offline(data):
    """Every role and every seat of both panels, its high-risk model included."""
    specs = [data[role] for role in config_mod.KNOWN_ROLES]
    design = config_mod.get_path(data, config_mod.DESIGN_PANEL.reviewers) or []
    for reviewer in [*data["reviewers"], *design]:
        specs.append(reviewer)
        if "high_risk_model" in reviewer:
            specs.append({"provider": reviewer["provider"], "model": reviewer["high_risk_model"]})
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
                self.assertEqual({seat[1] for seat in design_panel(fit)}, {"mycli"})
                data = config_mod.compose({"preset": preset}, {}, ["mycli"])[0]
                self.assertEqual(config_mod.validate(data), [])
                assert_resolves_offline(data)
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
        self.assertIsNone(providers_mod._warned_provider("mycli"))

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
        self.assertEqual([seat[1] for seat in panel(fit)], ["alpha", "beta", "alpha"])

    def test_an_eligible_adapter_takes_every_full_seat_and_no_cheap_one(self):
        self.adapter(enforcement="partial")
        quality = presets.expand("quality", ["mycli"])
        self.assertEqual(
            panel(quality),
            [
                ("mycli-general", "mycli", "default", "general", None, None),
                ("mycli-security", "mycli", "default", "security", None, None),
                ("mycli-architecture", "mycli", "default", "architecture", None, None),
                ("mycli-test", "mycli", "default", "test", None, None),
            ],
        )
        self.assertEqual(
            design_panel(quality),
            [
                ("mycli-general", "mycli", "default", "general", None, None),
                ("mycli-security", "mycli", "default", "security", None, None),
                ("mycli-architecture", "mycli", "default", "architecture", None, None),
            ],
        )
        fit = presets.expand("standard", ["mycli"])
        self.assertEqual(
            panel(fit),
            [
                ("mycli-general", "mycli", "default", "general", None, None),
                ("mycli-security", "mycli", "default", "security", None, None),
            ],
        )
        self.assertEqual(design_panel(fit), panel(fit))
        self.assertIn(
            "claude, codex not found on PATH: reviewer seat 4 (test) was not added; "
            "mycli has no cheap model named offline",
            fit.notes,
        )
        self.assertIn(
            "claude, codex not found on PATH: reviewer seat 3 (security) went to mycli as mycli-security "
            "(default); no high-risk model on mycli",
            fit.notes,
        )
        self.assertFalse([note for note in fit.notes if "reviewer seat 3 (security) was not added" in note])

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

    def test_beside_claude_a_codex_seat_goes_to_no_other_tier(self):
        """Claude fills the seat pool, so neither an adapter nor agy is in it:
        a Codex seat finds no provider but Claude there, and is not added."""
        self.adapter(enforcement="partial")
        skipped = (
            "codex not found on PATH: %s %d (%s) was not added; it is a second vendor's opinion "
            "and nothing installed stands in for one"
        )
        for other in ("mycli", "agy"):
            with self.subTest(other=other):
                fit = presets.expand("quality", ["claude", other])
                self.assertEqual({seat[1] for seat in panel(fit)}, {"claude"})
                self.assertEqual({seat[1] for seat in design_panel(fit)}, {"claude"})
                self.assertIn(skipped % ("reviewer seat", 2, "general"), fit.notes)
                self.assertIn(skipped % ("reviewer seat", 4, "security"), fit.notes)
                self.assertIn(skipped % ("design reviewer seat", 2, "general"), fit.notes)
                self.assertFalse([note for note in fit.notes if "went to %s" % other in note], fit.notes)

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
                    assert_resolves_offline(data)

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

    def test_with_preset_strips_design_list_keeps_design_extras(self):
        extra = config_mod.make_reviewer("x", "claude", "opus", "security")
        layer = {
            "version": 1,
            "review": {"design": {"reviewers": [extra], "reviewers_extra": [extra], "max_iterations": 3}},
        }
        self.assertEqual(
            presets.with_preset(layer, "quality"),
            {
                "version": 1,
                "preset": "quality",
                "review": {"design": {"reviewers_extra": [extra], "max_iterations": 3}},
            },
        )


class TestComposingTheFitDesignPanel(unittest.TestCase):
    def test_file_reviewers_drop_fit_design_panel_and_notes(self):
        reviewers = [config_mod.make_reviewer("m1", "mock", None)]
        for layers in (({"reviewers": reviewers}, {}), ({}, {"reviewers": reviewers})):
            with self.subTest(layers=layers):
                data, fit, _, _ = config_mod.compose(*layers, ["codex"])
                self.assertIsNone(config_mod.get_path(data, config_mod.DESIGN_PANEL.reviewers))
                self.assertNotIn("review.design.reviewers", fit.subjects)
                self.assertNotIn("reviewers", (fit.values.get("review") or {}).get("design") or {})
                self.assertEqual(fit.design_origins, ())

    def test_file_reviewers_keep_the_presets_design_switch(self):
        reviewers = [config_mod.make_reviewer("m1", "mock", None)]
        fit = config_mod.compose({"preset": "fast", "reviewers": reviewers}, {}, BOTH)[1]
        self.assertEqual(fit.values["review"], {"design": {"enabled": False}})
        fit = config_mod.compose({"reviewers": reviewers}, {}, BOTH)[1]
        self.assertNotIn("review", fit.values)

    def test_file_design_list_drops_fit_design_notes(self):
        listed = [config_mod.make_reviewer("d1", "codex", "recommended-coding")]
        layer = {"review": {"design": {"reviewers": listed}}}
        data, fit, _, _ = config_mod.compose(layer, {}, ["codex"])
        self.assertEqual(config_mod.get_path(data, config_mod.DESIGN_PANEL.reviewers), listed)
        self.assertNotIn("review.design.reviewers", fit.subjects)
        self.assertIn("reviewers", fit.subjects)
        self.assertEqual([o.label() for o in fit.design_origins], ["global design"])

    def test_design_extras_join_fit_design_panel(self):
        extra = config_mod.make_reviewer("x", "claude", "opus", "security")
        layer = {"review": {"design": {"reviewers_extra": [extra]}}}
        data, fit, _, _ = config_mod.compose(layer, {}, ["codex"])
        ids = [r["id"] for r in config_mod.get_path(data, config_mod.DESIGN_PANEL.reviewers)]
        self.assertEqual(ids, ["codex-general", "codex-security", "x"])
        labels = [o.label() for o in fit.design_origins]
        self.assertEqual(labels, ["fit design", "fit design", "global design extra"])
        # The fit is still the base, so its notes still apply.
        self.assertIn("review.design.reviewers", fit.subjects)

    def test_listed_code_panel_without_design_keys_uses_code_copy(self):
        layer = {"reviewers": [config_mod.make_reviewer("a", "claude", "opus", "security", when="high-risk")]}
        loaded = config_mod.LoadedConfig(config_mod.compose(layer, {}, BOTH)[0], None, None, False, layer)
        self.assertFalse(loaded.has_design_panel())
        self.assertEqual(loaded.design_panel_source, "code")
        self.assertNotIn("when", loaded.design_reviewers()[0])

    def loaded(self, global_layer, project_layer, installed):
        data, fit, _, _ = config_mod.compose(global_layer, project_layer, installed)
        loaded = config_mod.LoadedConfig(
            data,
            None,
            None,
            False,
            global_layer,
            project_layer,
            design_reviewer_origins=fit.design_origins,
        )
        return data, fit, loaded

    def test_a_project_design_list_over_a_global_preset(self):
        listed = [config_mod.make_reviewer("d1", "codex", "recommended-coding")]
        project = {"review": {"design": {"reviewers": listed}}}
        data, fit, loaded = self.loaded({}, project, ["codex"])
        self.assertEqual(config_mod.get_path(data, config_mod.DESIGN_PANEL.reviewers), listed)
        self.assertNotIn("review.design.reviewers", fit.subjects)
        self.assertIn("reviewers", fit.subjects)
        self.assertEqual([o.label() for o in fit.design_origins], ["project design"])
        self.assertEqual(loaded.design_panel_source, "project")
        # What the fit contributed: its design list went, as for a listed code panel.
        self.assertIsNone(config_mod.get_path(fit.values, config_mod.DESIGN_PANEL.reviewers))

    def test_a_global_design_list_beside_a_project_code_list(self):
        listed = [config_mod.make_reviewer("d1", "codex", "recommended-coding")]
        reviewers = [config_mod.make_reviewer("m1", "mock", None)]
        global_layer = {"review": {"design": {"reviewers": listed}}}
        data, fit, loaded = self.loaded(global_layer, {"reviewers": reviewers}, ["codex"])
        self.assertEqual(config_mod.get_path(data, config_mod.DESIGN_PANEL.reviewers), listed)
        self.assertEqual(data["reviewers"], reviewers)
        self.assertNotIn("review.design.reviewers", fit.subjects)
        self.assertNotIn("reviewers", fit.subjects)
        self.assertEqual([o.label() for o in fit.design_origins], ["global design"])
        self.assertEqual(loaded.design_panel_source, "global")

    def test_the_one_rule_for_following_the_fit(self):
        reviewers = [config_mod.make_reviewer("m1", "mock", None)]
        design_list = {"review": {"design": {"reviewers": reviewers}}}
        design_extras = {"review": {"design": {"reviewers_extra": reviewers}}}
        follows = config_mod.design_panel_follows_fit
        self.assertTrue(follows({}, {}))
        self.assertTrue(follows(design_extras, {"reviewers_extra": reviewers}))
        for layer in ({"reviewers": reviewers}, design_list):
            with self.subTest(layer=layer):
                self.assertFalse(follows(layer, {}))
                self.assertFalse(follows({}, layer))


class TestSeatTags(unittest.TestCase):
    def test_no_cheap_seat_names_a_vendor(self):
        for name, preset in presets.PRESETS.items():
            for seat in (*preset.seats, *preset.design_seats):
                with self.subTest(preset=name, seat=seat):
                    self.assertFalse(seat.cheap and seat.vendor is not None)

    def test_a_cheap_seat_with_a_vendor_is_refused(self):
        seat = presets.Seat("test", "sonnet", "always", cheap=True, vendor=presets.CODEX)
        with self.assertRaises(ValueError):
            presets._fit_panel((seat,), ["claude"], None, {}, "", "reviewer seat", "reviewers")


class TestPopPath(unittest.TestCase):
    def test_it_removes_the_key_and_the_parents_it_empties(self):
        data = {"review": {"design": {"reviewers": [], "enabled": True}, "parallel": False}}
        config_mod.pop_path(data, "review.design.reviewers")
        self.assertEqual(data, {"review": {"design": {"enabled": True}, "parallel": False}})
        config_mod.pop_path(data, "review.design.enabled")
        self.assertEqual(data, {"review": {"parallel": False}})
        config_mod.pop_path(data, "review.parallel")
        self.assertEqual(data, {})

    def test_a_path_that_is_not_there_is_left_alone(self):
        data = {"review": ["not", "a", "mapping"]}
        config_mod.pop_path(data, "review.design.reviewers")
        config_mod.pop_path(data, "optimization.level")
        self.assertEqual(data, {"review": ["not", "a", "mapping"]})


class TestTheImplementersVendor(unittest.TestCase):
    def test_the_panel_is_dealt_around_the_implementer_a_file_set(self):
        layer = {
            "preset": "fast",
            "implementer": {"provider": "codex", "model": {"family": CODEX, "version": "latest"}},
        }
        reviewers = config_mod.compose(layer, {}, BOTH)[0]["reviewers"]
        always = [(r["provider"], r["role"]) for r in reviewers if not r.get("when")]
        self.assertEqual(always, [("claude", "general")])

    def test_the_cheap_seats_stay_on_claude_when_the_full_ones_rotate(self):
        implementer = {"implementer": {"provider": "codex", "model": {"family": CODEX, "version": "latest"}}}
        expected = {
            "standard": [
                ("codex-general", "codex", CODEX, "general", None, None),
                ("claude-general", "claude", "sonnet", "general", None, None),
                ("claude-security", "claude", "sonnet", "security", None, "opus"),
                ("claude-test", "claude", "sonnet", "test", None, None),
            ],
            # Vendor seats: the implementer moves none of them.
            "quality": PANELS[("quality", "both")],
        }
        for name, panel_seats in expected.items():
            with self.subTest(preset=name):
                reviewers = config_mod.compose({"preset": name, **implementer}, {}, BOTH)[0]["reviewers"]
                self.assertEqual(seats(reviewers), panel_seats)


class TestNoRiskPatterns(IsolatedCase):
    """A fitted high-risk seat stays when the files empty the patterns; a written one is refused.

    ``fast`` is the preset with a fitted ``when: high-risk`` seat.
    """

    def assert_seat_kept(self, global_layer, project_layer):
        data, fit, _, _ = config_mod.compose(global_layer, project_layer, BOTH)
        seat = next(r for r in data["reviewers"] if r["id"] == "claude-security")
        self.assertEqual(seat["when"], "high-risk")
        self.assertFalse(any("was not added: optimization.high_risk_paths" in n for n in fit.notes))
        problems = config_mod.validate(
            data, project_layer=project_layer, global_layer=global_layer, origins=fit.origins
        )
        self.assertEqual(problems, [])
        # With no pattern to match, a declared round is how the seat runs.
        records = optimization.condition_reviewers(data["reviewers"], [], declared=True)
        self.assertIn(
            {
                "id": "claude-security",
                "when": "high-risk",
                "runs": True,
                "reason": "declared with --high-risk",
            },
            records,
        )

    def test_the_global_file_keeps_the_fitted_seat(self):
        self.assert_seat_kept({"version": 1, "preset": "fast", "optimization": {"high_risk_paths": []}}, {})

    def test_the_project_file_keeps_the_fitted_seat(self):
        self.assert_seat_kept({"preset": "fast"}, {"version": 1, "optimization": {"high_risk_paths": []}})

    def test_an_extra_high_risk_reviewer_is_refused(self):
        project_layer = {
            "version": 1,
            "optimization": {"high_risk_paths": []},
            "reviewers_extra": [
                config_mod.make_reviewer("x", "claude", "opus", "security", when="high-risk")
            ],
        }
        data, fit, _, _ = config_mod.compose({"preset": "fast"}, project_layer, BOTH)
        ids = [r["id"] for r in data["reviewers"]]
        self.assertIn("claude-security", ids)
        self.assertEqual(ids[-1], "x")
        problems = config_mod.validate(data, project_layer=project_layer, origins=fit.origins)
        self.assertIn(
            "optimization.high_risk_paths: no pattern in force, but reviewers_extra[0] in the project "
            "file is when: high-risk and would never run; add patterns to high_risk_paths or "
            "extra_high_risk_paths",
            problems,
        )

    def test_a_listed_high_risk_reviewer_is_still_refused(self):
        global_layer = {
            "version": 1,
            "optimization": {"high_risk_paths": []},
            "reviewers": [
                config_mod.make_reviewer("a", "claude", "opus", "general"),
                config_mod.make_reviewer("b", "claude", "opus", "security", when="high-risk"),
            ],
        }
        data, _, _, _ = config_mod.compose(global_layer, {}, BOTH)
        self.assertTrue(any("no pattern in force" in p for p in config_mod.validate(data)))


if __name__ == "__main__":
    unittest.main()
