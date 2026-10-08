"""The setup wizard, driven with scripted answers instead of a terminal."""

from __future__ import annotations

import unittest

from helpers import IsolatedCase

from orchestrator import config as config_mod
from orchestrator import config_layers
from orchestrator import presets as presets_mod
from orchestrator import summary as summary_mod
from orchestrator import wizard as wizard_mod


class ScriptedPrompter(wizard_mod.Prompter):
    """Replays a list of answers; records everything that was printed."""

    def __init__(self, answers, overrides=None, ask_language=False):
        self.answers = list(answers)
        # Substring -> answer, for prompts whose position is not worth counting.
        # The reply-language question is answered blank unless a test is about it.
        self.overrides = {} if ask_language else {"Reply language": ""}
        self.overrides.update(overrides or {})
        self.output = []
        self.questions = []
        super().__init__(reader=self._answer, writer=self.output.append)

    def _answer(self, question):
        self.questions.append(question)
        for needle, reply in self.overrides.items():
            if needle in question:
                return reply
        if not self.answers:
            raise EOFError("ran out of scripted answers at: %s" % question)
        return self.answers.pop(0)


#: The preset menu's last choice: go through the role questions one by one.
CUSTOMISE = str(len(wizard_mod.PRESET_CHOICES) + 1)


def accept_all(count=40, customise=True):
    """Press enter for everything: every prompt takes its recommended default.

    The global wizard asks for a preset first; ``customise`` answers it with
    "customise each role", so the questions below it are the ones replayed.
    """
    return [*([CUSTOMISE] if customise else []), *[""] * count]


class TestWizard(IsolatedCase):
    def test_pressing_enter_throughout_yields_the_recommended_config(self):
        prompter = ScriptedPrompter(accept_all())
        data, save = wizard_mod.run(prompter)
        self.assertTrue(save)
        self.assertEqual(data["orchestrator"]["model"]["family"], "sonnet")
        self.assertEqual(data["architect"]["model"]["family"], "fable")
        self.assertEqual(data["implementer"]["model"]["family"], "opus")
        self.assertEqual(data["review_fixer"]["model"]["family"], "opus")
        self.assertEqual(len(data["reviewers"]), 4)
        self.assertEqual(config_mod.validate(data), [])

    def test_enter_through_the_reviewers_keeps_the_four_defaults(self):
        prompter = ScriptedPrompter(accept_all())
        data, _ = wizard_mod.run(prompter)
        self.assertIn("   How many reviewers? [4]: ", prompter.questions)
        self.assertEqual(data["reviewers"], config_mod.default_config()["reviewers"])
        ids = [r["id"] for r in data["reviewers"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(data["reviewers"][2]["id"], "claude-security")
        self.assertEqual(data["reviewers"][2]["high_risk_model"], {"family": "opus", "version": "latest"})

    def test_no_risk_pattern_leaves_the_high_risk_seat_unoffered(self):
        # A written seat: the built-in panel has no when: high-risk seat.
        existing = {
            "version": 1,
            "optimization": {"high_risk_paths": []},
            "reviewers": [
                config_mod.make_reviewer("claude-general", "claude", "opus"),
                config_mod.make_reviewer("claude-security-2", "claude", "opus", "security", when="high-risk"),
            ],
        }
        prompter = ScriptedPrompter(accept_all())
        data, _ = wizard_mod.run(prompter, existing)
        self.assertIn("   How many reviewers? [1]: ", prompter.questions)
        self.assertIn(
            "   Not offered: claude-security-2 (when: high-risk); optimization.high_risk_paths "
            "has no pattern in force.",
            prompter.output,
        )
        self.assertNotIn("claude-security-2", [r["id"] for r in data["reviewers"]])
        self.assertEqual(config_mod.validate(data), [])

    def test_the_fallback_panel_is_the_built_in_one_and_reads_no_path(self):
        def installed():
            raise AssertionError("PATH was read")

        self.addCleanup(setattr, presets_mod, "installed_providers", presets_mod.installed_providers)
        setattr(presets_mod, "installed_providers", installed)
        self.assertEqual(wizard_mod.default_reviewer_config(), config_mod.default_config()["reviewers"])

    def test_wizard_menu_text(self):
        prompter = ScriptedPrompter(["", ""])
        wizard_mod.run(prompter)
        said = "\n".join(prompter.output)
        self.assertIn(
            "quality  -- strongest models, Codex beside Claude on both panels, design review auto", said
        )
        self.assertIn(
            "standard -- the built-in defaults: general on Claude and Codex, security on sonnet "
            "(opus on high-risk changes), test on sonnet",
            said,
        )
        self.assertIn(
            "fast     -- lighter models, one reviewer plus a sonnet security one on high-risk changes; "
            "design review off",
            said,
        )

    def test_wizard_preset_summary_shows_design_panel(self):
        prompter = ScriptedPrompter(["", ""])
        wizard_mod.run(prompter)
        said = "\n".join(prompter.output)
        design = said[said.index("  Design reviews") :]
        self.assertNotIn("(the code panel; when conditions ignored)", design)
        self.assertIn("/ general / claude-general (opus when high-risk)", design)
        self.assertIn("/ test / claude-test", design)

    def test_wizard_keeps_high_risk_model_when_provider_kept(self):
        template = dict(config_mod.default_config()["reviewers"][2], relevance="security")
        providers = [("claude", "Claude", True), ("mock", "Mock", True)]
        prompter = ScriptedPrompter(["", "", "", ""])
        reviewer = wizard_mod._ask_reviewer(prompter, providers, template, {"reviewers": []})
        self.assertEqual(reviewer, template)

    def test_wizard_drops_high_risk_model_on_provider_change_with_note(self):
        template = dict(config_mod.default_config()["reviewers"][2], relevance="security")
        providers = [("claude", "Claude", True), ("mock", "Mock", True)]
        prompter = ScriptedPrompter(["2", "", "", ""])
        reviewer = wizard_mod._ask_reviewer(prompter, providers, template, {"reviewers": []})
        self.assertEqual(reviewer["provider"], "mock")
        self.assertNotIn("high_risk_model", reviewer)
        self.assertEqual(reviewer["relevance"], "security")
        self.assertIn(
            "     note: provider is now mock; its high_risk_model was removed "
            "(reviewer set --high-risk-model sets another)",
            prompter.output,
        )

    def ask_with_role(self, template, role):
        """``_ask_reviewer`` over ``template``, answering only the role question."""
        providers = [("claude", "Claude", True), ("mock", "Mock", True)]
        prompter = ScriptedPrompter(["", "", str(config_mod.BUILTIN_ROLES.index(role) + 1), ""])
        return wizard_mod._ask_reviewer(prompter, providers, template, {"reviewers": []}), prompter

    def test_a_relevance_rule_goes_when_the_seat_becomes_general(self):
        template = dict(config_mod.default_config()["reviewers"][2], relevance="security")
        reviewer, prompter = self.ask_with_role(template, "general")
        self.assertEqual(reviewer["role"], "general")
        self.assertNotIn("relevance", reviewer)
        self.assertEqual(config_mod.validate({**config_mod.default_config(), "reviewers": [reviewer]}), [])
        self.assertIn(
            "     note: role is now general; its relevance security was removed "
            "(reviewer set --relevance sets another)",
            prompter.output,
        )

    def test_a_relevance_rule_goes_when_the_seat_takes_another_role(self):
        template = dict(config_mod.default_config()["reviewers"][2], relevance="security")
        reviewer, _prompter = self.ask_with_role(template, "test")
        self.assertEqual(reviewer["role"], "test")
        self.assertNotIn("relevance", reviewer)

    def test_relevance_always_stays_whatever_the_role(self):
        template = dict(config_mod.default_config()["reviewers"][2], relevance="always")
        reviewer, prompter = self.ask_with_role(template, "general")
        self.assertEqual(reviewer["relevance"], "always")
        self.assertFalse([line for line in prompter.output if "relevance" in line])

    def test_saved_config_stores_families_not_snapshot_ids(self):
        data, _ = wizard_mod.run(ScriptedPrompter(accept_all()))
        for key, _title in wizard_mod.ROLE_TITLES:
            model = data[key]["model"]
            self.assertEqual(model["version"], "latest")
            self.assertNotIn("id", model)

    def test_declining_the_save_prompt_is_respected(self):
        prompter = ScriptedPrompter(accept_all(), {"Save configuration?": "n"})
        _, save = wizard_mod.run(prompter)
        self.assertFalse(save)

    def test_zero_reviewers_is_offered_with_a_warning(self):
        # Four roles x (CLI, model) = 8 enters, then "0" reviewers, then save.
        prompter = ScriptedPrompter([CUSTOMISE, *[""] * 8, "0", ""])
        data, save = wizard_mod.run(prompter)
        self.assertEqual(data["reviewers"], [])
        self.assertTrue(save)
        self.assertTrue(any("recommended" in line for line in prompter.output))

    def test_summary_is_shown_before_saving(self):
        prompter = ScriptedPrompter(accept_all())
        wizard_mod.run(prompter)
        rendered = "\n".join(prompter.output)
        self.assertIn("Configuration", rendered)
        self.assertIn("Reviews", rendered)
        self.assertIn("Save configuration?", prompter.questions[-1])

    def test_detected_clis_are_listed_first(self):
        prompter = ScriptedPrompter(accept_all())
        wizard_mod.run(prompter)
        self.assertIn("Detected CLIs:", prompter.output)

    def test_existing_config_is_used_as_the_default(self):
        existing = config_mod.default_config()
        existing["implementer"]["model"]["family"] = "sonnet"
        data, _ = wizard_mod.run(ScriptedPrompter(accept_all()), existing)
        self.assertEqual(data["implementer"]["model"]["family"], "sonnet")

    def test_custom_model_that_cannot_be_resolved_offers_pinning(self):
        provider_index = [name for name, _, _ in wizard_mod.selectable_providers()].index("claude") + 1
        candidates = len(wizard_mod.get_provider("claude").list_models())
        answers = [
            CUSTOMISE,
            str(provider_index),  # orchestrator CLI: claude
            str(candidates + 1),  # model: "custom"
            "totally-made-up-family",  # the custom value
            "y",  # yes, pin it
            *accept_all(customise=False),
        ]
        data, _ = wizard_mod.run(ScriptedPrompter(answers))
        model = data["orchestrator"]["model"]
        self.assertEqual(model["version"], "pinned")
        self.assertEqual(model["id"], "totally-made-up-family")

    def test_reviewer_ids_are_unique_when_roles_repeat(self):
        # 8 enters for the roles, 2 reviewers, both mock/general.
        providers = [name for name, _, _ in wizard_mod.selectable_providers()]
        first = str(providers.index(providers[0]) + 1)
        answers = [CUSTOMISE, *[""] * 8, "2"]
        for _ in range(2):
            answers += [first, "", "1", ""]  # CLI, model, role=general, default id
        answers += ["n", ""]  # no more reviewers, then save
        data, _ = wizard_mod.run(ScriptedPrompter(answers))
        ids = [r["id"] for r in data["reviewers"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_invalid_menu_input_is_re_prompted(self):
        prompter = ScriptedPrompter(["99", "abc", *accept_all()])
        wizard_mod.run(prompter)
        self.assertTrue(any("Enter a number" in line for line in prompter.output))

    def test_render_summary_marks_pinned_models(self):
        data = config_mod.default_config()
        data["implementer"]["model"] = {"family": "opus", "version": "pinned", "id": "claude-opus-x"}
        self.assertIn("claude-opus-x / pinned", summary_mod.render_summary(data))


class TestWhatTheWizardReturns(IsolatedCase):
    """Only what it asked about. A setting nobody was asked for is not a
    decision, and writing it down would pin today's default forever."""

    def test_nothing_it_never_asked_about_comes_back(self):
        data, _ = wizard_mod.run(ScriptedPrompter(accept_all()))
        self.assertEqual(
            sorted(data),
            ["architect", "implementer", "orchestrator", "review_fixer", "reviewers", "version"],
        )

    def test_what_the_layer_already_held_is_kept(self):
        existing = {"version": 1, "review": {"max_review_iterations": 1}}
        data, _ = wizard_mod.run(ScriptedPrompter(accept_all()), existing)
        self.assertEqual(data["review"], {"max_review_iterations": 1})
        self.assertEqual(data["implementer"]["model"]["family"], "opus")

    def test_a_version_the_layer_states_is_left_alone(self):
        data, _ = wizard_mod.run(ScriptedPrompter(accept_all()), {"version": 0})
        self.assertEqual(data["version"], 0)


class TestTheWizardsBase(IsolatedCase):
    """`base` is what the layer being edited would inherit. Offering the
    built-in defaults instead means pressing enter through a project setup
    overrules the global layer with a value nobody chose."""

    def test_the_recommended_answer_comes_from_the_base(self):
        base = config_mod.deep_merge(
            config_mod.default_config(),
            {"implementer": {"provider": "claude", "model": {"family": "sonnet", "version": "latest"}}},
        )
        data, _ = wizard_mod.run(ScriptedPrompter(accept_all()), None, base)
        self.assertEqual(data["implementer"]["model"]["family"], "sonnet")

    def test_without_a_base_it_is_the_built_in_default(self):
        data, _ = wizard_mod.run(ScriptedPrompter(accept_all()))
        self.assertEqual(data["implementer"]["model"]["family"], "opus")

    def test_the_summary_shows_what_will_be_in_force(self):
        """The layer alone would report the design review as off while the
        layer below has it on -- and the summary is what the user says yes to."""
        layer = {"version": 1, "review": {"design": {"enabled": True}}}
        config_mod.write_config_file(config_mod.global_config_path(), layer, "global")
        base = config_layers._fitted_base("project")
        prompter = ScriptedPrompter(accept_all(customise=False))
        data, _ = wizard_mod.run(prompter, None, base, scope="project")
        self.assertIn("design review: on", "\n".join(prompter.output))
        self.assertNotIn("review", data)

    def test_the_global_base_reads_the_preset_name_as_load_does(self):
        """No name, or `null`, means `standard`; an unknown one -- `""`
        included -- fits nothing, and the wizard's base must not read it as
        `standard`."""
        self.addCleanup(setattr, presets_mod, "installed_providers", presets_mod.installed_providers)
        setattr(presets_mod, "installed_providers", lambda: ["codex"])
        self.addCleanup(setattr, config_layers, "_global_file", config_layers._global_file)
        default = config_mod.default_config()["implementer"]["provider"]
        standard = presets_mod.expand("standard", ["codex"]).values["implementer"]["provider"]
        self.assertNotEqual(default, standard)
        for layer, expected in (
            ({"version": 1}, standard),
            ({"version": 1, "preset": None}, standard),
            ({"version": 1, "preset": "standard"}, standard),
            ({"version": 1, "preset": ""}, default),
            ({"version": 1, "preset": "nope"}, default),
        ):
            with self.subTest(layer=layer):
                setattr(config_layers, "_global_file", lambda layer=layer: layer)
                base = config_layers._fitted_base("global")
                self.assertEqual(base, config_mod.compose({"preset": layer.get("preset")}, {}, ["codex"])[0])
                self.assertEqual(base["implementer"]["provider"], expected)

    def test_the_summary_shows_the_default_design_review_as_auto(self):
        prompter = ScriptedPrompter(accept_all())
        wizard_mod.run(prompter)
        self.assertIn("design review: auto", "\n".join(prompter.output))

    def test_a_null_design_switch_in_the_base_is_the_default(self):
        base = config_mod.default_config()
        base["review"]["design"]["enabled"] = None
        prompter = ScriptedPrompter(accept_all())
        wizard_mod.run(prompter, None, base)
        self.assertIn("design review: auto", "\n".join(prompter.output))

    def test_the_reviewer_template_comes_from_the_base_too(self):
        panel = [config_mod.make_reviewer("only-one", "claude", "opus", "general")]
        base = config_mod.deep_merge(config_mod.default_config(), {"reviewers": panel})
        data, _ = wizard_mod.run(ScriptedPrompter(accept_all()), None, base)
        self.assertEqual([r["id"] for r in data["reviewers"]], ["only-one"])

    def test_an_inherited_empty_panel_is_not_refilled(self):
        """`reviewers: []` is a decision -- run no independent review at all --
        and it is only an *absent* panel that means nobody has chosen yet.
        Reading the two the same way turned the global choice back into two
        reviewers for anyone who pressed enter through project setup."""
        config_mod.write_config_file(
            config_mod.global_config_path(), {"version": 1, "reviewers": []}, "global"
        )
        base = config_layers._layer_base("project", self.project)
        self.assertEqual(base["reviewers"], [])
        prompter = ScriptedPrompter(accept_all())
        data, _ = wizard_mod.run(prompter, None, base)
        self.assertEqual(data["reviewers"], [])


class TestThePresetQuestion(IsolatedCase):
    """The global wizard asks for a preset first; saved as is it records the
    name, and adjusted it records only what differs from the preset's fit."""

    def setUp(self):
        super().setUp()
        self.fake_clis()

    def test_it_is_the_first_question(self):
        prompter = ScriptedPrompter(["", ""])
        wizard_mod.run(prompter)
        self.assertEqual(prompter.questions[0], "Choice [2]: ")
        said = "\n".join(prompter.output)
        self.assertIn("Preset (fitted to the CLIs found above):", said)
        self.assertNotIn("1. Orchestrator", said)

    def test_enter_through_saves_the_name_only(self):
        data, save = wizard_mod.run(ScriptedPrompter(["", ""]))
        self.assertTrue(save)
        self.assertEqual(data, {"version": 1, "preset": "standard"})

    def test_adjusting_nothing_saves_the_name_only(self):
        data, save = wizard_mod.run(ScriptedPrompter(["", "n", *accept_all(customise=False)]))
        self.assertTrue(save)
        self.assertEqual(data, {"version": 1, "preset": "standard"})

    def test_adjusting_keeps_a_value_the_file_held_that_equals_a_default(self):
        existing = {"version": 1, "review": {"max_review_iterations": 2}}
        data, _ = wizard_mod.run(ScriptedPrompter(["", "n", *accept_all(customise=False)]), existing)
        self.assertEqual(data, {"version": 1, "preset": "standard", "review": {"max_review_iterations": 2}})

    def test_adjusting_keeps_a_roles_options(self):
        options = {"permission_mode": "plan"}
        existing = {"version": 1, "implementer": {"provider": "claude", "options": options}}
        data, _ = wizard_mod.run(ScriptedPrompter(["", "n", *accept_all(customise=False)]), existing)
        self.assertEqual(data["implementer"]["options"], options)

    def test_adjusting_one_role_saves_that_role_and_the_panel_still_fits(self):
        families = [candidate.family for candidate in wizard_mod.get_provider("claude").list_models()]
        answers = ["", "n", "", str(families.index("opus") + 1), *accept_all(customise=False)]
        data, _ = wizard_mod.run(ScriptedPrompter(answers))
        opus = {"provider": "claude", "model": {"family": "opus", "version": "latest"}}
        self.assertEqual(data, {"version": 1, "preset": "standard", "orchestrator": opus})
        config_mod.write_config_file(config_mod.global_config_path(), data, "global")
        self.fake_clis(claude=True)
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.role("orchestrator"), opus)
        self.assertEqual(
            [r["id"] for r in loaded.reviewers()],
            ["claude-general", "claude-general-2", "claude-security", "claude-test"],
        )

    def test_changing_a_reviewer_saves_the_whole_panel(self):
        security = str(config_mod.BUILTIN_ROLES.index("security") + 1)
        # Preset, adjust, eight role answers, the count, then CLI, model, role.
        answers = ["", "n", *[""] * 8, "", "", "", security, *accept_all(customise=False)]
        prompter = ScriptedPrompter(answers)
        data, _ = wizard_mod.run(prompter)
        self.assertEqual(sorted(data), ["preset", "reviewers", "version"])
        self.assertEqual([r["role"] for r in data["reviewers"]], ["security", "general", "security", "test"])
        # The saved list takes the design rounds with it, and the wizard says so.
        self.assertIn(
            "   note: the reviewers differ from preset standard's fit, so they are saved; design rounds "
            "then run them without when, not the preset's design panel",
            prompter.output,
        )

    def test_adjusting_nothing_says_nothing_of_the_design_panel(self):
        prompter = ScriptedPrompter(["", "n", *accept_all(customise=False)])
        wizard_mod.run(prompter)
        self.assertFalse([line for line in prompter.output if "design rounds then run" in line])

    def test_a_chosen_preset_replaces_what_it_governs_and_keeps_the_rest(self):
        existing = {"version": 1, "implementer": {"provider": "claude"}, "review": {"parallel": False}}
        prompter = ScriptedPrompter(["1", ""])
        data, _ = wizard_mod.run(prompter, existing)
        self.assertEqual(data, {"version": 1, "preset": "quality", "review": {"parallel": False}})
        self.assertIn("optimization level: quality", "\n".join(prompter.output))

    def test_a_project_is_not_asked(self):
        prompter = ScriptedPrompter(accept_all(customise=False))
        data, _ = wizard_mod.run(prompter, None, None, scope="project")
        self.assertNotIn("Preset (fitted", "\n".join(prompter.output))
        self.assertNotIn("preset", data)
        self.assertIn("implementer", data)


class TestAFailingUserAdapter(IsolatedCase):
    """A user adapter that raises while being detected is left off the menu
    and said to have failed; the rest of the menu is still offered."""

    SOURCE = (
        "from orchestrator.providers.base import Provider\n"
        "\n"
        "\n"
        "class Flaky(Provider):\n"
        '    name = "flaky"\n'
        '    executable = "flaky"\n'
        "\n"
        "    def detect(self):\n"
        '        raise RuntimeError("detect exploded")\n'
        "\n"
        "\n"
        "def build_provider(executable=None):\n"
        "    return Flaky(executable)\n"
    )

    def setUp(self):
        super().setUp()
        from orchestrator import providers
        from orchestrator.providers.agy import AgyProvider
        from orchestrator.providers.claude import ClaudeProvider
        from orchestrator.providers.codex import CodexProvider

        for cls in (AgyProvider, ClaudeProvider, CodexProvider):
            self.addCleanup(setattr, cls, "which", cls.which)
            setattr(cls, "which", lambda self: None)
        self.path = self.write_user_provider("flaky", self.SOURCE)
        providers.load_user_providers()

    def test_selectable_providers_reports_it_and_goes_on(self):
        failures = []
        names = [name for name, _, _ in wizard_mod.selectable_providers(failures=failures)]
        self.assertEqual(names, ["agy", "claude", "codex"])
        self.assertEqual(len(failures), 1)
        name, reason = failures[0]
        self.assertEqual(name, "flaky")
        self.assertIn("RuntimeError: detect exploded", reason)
        self.assertIn(self.path, reason)

    def test_selectable_providers_without_a_failure_list_still_goes_on(self):
        names = [name for name, _, _ in wizard_mod.selectable_providers()]
        self.assertEqual(names, ["agy", "claude", "codex"])


class TestAUserAdapterFailingOnModels(IsolatedCase):
    """One that passes detection and raises while its models are asked for:
    reported, and the CLI question asked again over the rest."""

    SOURCE = (
        "import sys\n"
        "\n"
        "from orchestrator.providers.base import Provider\n"
        "\n"
        "\n"
        "class Flaky(Provider):\n"
        '    name = "flaky"\n'
        '    display_name = "Flaky"\n'
        '    executable = "flaky"\n'
        "\n"
        "    def which(self):\n"
        "        return sys.executable\n"
        "\n"
        "    def version(self):\n"
        '        return "flaky 1", None\n'
        "\n"
        "    def list_models(self):\n"
        '        raise RuntimeError("list_models exploded")\n'
        "\n"
        "\n"
        "def build_provider(executable=None):\n"
        "    return Flaky(executable)\n"
    )

    def setUp(self):
        super().setUp()
        from orchestrator import providers

        self.path = self.write_user_provider("flaky", self.SOURCE)
        providers.load_user_providers()
        self.providers = [("flaky", "Flaky", True), ("mock", "Mock", True)]

    def test_the_question_is_asked_again_without_it(self):
        prompter = ScriptedPrompter(["1", "", ""])
        spec = wizard_mod._ask_role(prompter, self.providers, "flaky", "small")
        self.assertEqual(spec["provider"], "mock")
        said = "\n".join(prompter.output)
        self.assertIn("flaky adapter failed (user module %s)" % self.path, said)
        self.assertIn("RuntimeError: list_models exploded", said)

    def test_with_nothing_else_to_choose_it_is_kept(self):
        prompter = ScriptedPrompter(["1"])
        spec = wizard_mod._ask_role(prompter, self.providers[:1], "flaky", "small")
        self.assertEqual(spec, {"provider": "flaky", "model": {"family": "small", "version": "latest"}})


class TestLanguageQuestion(IsolatedCase):
    def test_wizard_asks_language_and_validates(self):
        """Asked once, last before saving, and again until it is a tag or blank."""
        prompter = ScriptedPrompter(["", "japanese", "JA", ""], ask_language=True)
        data, save = wizard_mod.run(prompter)
        self.assertTrue(save)
        self.assertEqual(data["language"], {"reply": "ja"})
        asked = [question for question in prompter.questions if "Reply language" in question]
        self.assertEqual(len(asked), 2)
        self.assertIn("   'japanese' is not a language tag such as ja, ko, zh-TW or en.", prompter.output)
        self.assertTrue(prompter.questions[-1].startswith("Save as is?"))

    def test_blank_sets_nothing_and_keeps_what_the_file_held(self):
        data, _ = wizard_mod.run(ScriptedPrompter(["", "", ""], ask_language=True))
        self.assertNotIn("language", data)
        existing = {"version": 1, "language": {"reply": "ko"}}
        prompter = ScriptedPrompter(["", "", ""], ask_language=True)
        data, _ = wizard_mod.run(prompter, existing)
        self.assertEqual(data["language"], {"reply": "ko"})
        self.assertIn("[ko]", next(q for q in prompter.questions if "Reply language" in q))

    def test_none_clears_it_and_an_offered_tag_is_the_default(self):
        existing = {"version": 1, "language": {"reply": "ko"}}
        data, _ = wizard_mod.run(ScriptedPrompter(["", "none", ""], ask_language=True), existing)
        self.assertNotIn("language", data)
        data, _ = wizard_mod.run(ScriptedPrompter(["", "", ""], ask_language=True), existing, reply="zh-TW")
        self.assertEqual(data["language"], {"reply": "zh-TW"})

    def test_preset_path_asks_it_once_before_save_as_is(self):
        overrides = {"Reply language": "ja", "Save as is?": ""}
        prompter = ScriptedPrompter([""], overrides=overrides, ask_language=True)
        data, save = wizard_mod.run(prompter)
        self.assertTrue(save)
        self.assertEqual(data["language"], {"reply": "ja"})
        asked = [i for i, question in enumerate(prompter.questions) if "Reply language" in question]
        self.assertEqual(len(asked), 1)
        self.assertTrue(prompter.questions[asked[0] + 1].startswith("Save as is?"))

    def test_adjusting_a_preset_keeps_the_language_and_asks_it_once(self):
        """Declining "Save as is?" goes through the roles; the answer survives the fit comparison."""
        overrides = {"Reply language": "ja", "Save as is?": "n"}
        prompter = ScriptedPrompter(accept_all(customise=False), overrides=overrides, ask_language=True)
        data, save = wizard_mod.run(prompter)
        self.assertTrue(save)
        self.assertEqual(data["language"], {"reply": "ja"})
        self.assertEqual(len([q for q in prompter.questions if "Reply language" in q]), 1)

    def test_customising_asks_it_once_before_saving(self):
        prompter = ScriptedPrompter(accept_all(), overrides={"Reply language": "es"}, ask_language=True)
        data, _ = wizard_mod.run(prompter)
        self.assertEqual(data["language"], {"reply": "es"})
        self.assertEqual(len([q for q in prompter.questions if "Reply language" in q]), 1)


if __name__ == "__main__":
    unittest.main()
