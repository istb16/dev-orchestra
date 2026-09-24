"""The setup wizard, driven with scripted answers instead of a terminal."""

from __future__ import annotations

import unittest

from helpers import IsolatedCase

from orchestrator import cli
from orchestrator import config as config_mod
from orchestrator import wizard as wizard_mod


class ScriptedPrompter(wizard_mod.Prompter):
    """Replays a list of answers; records everything that was printed."""

    def __init__(self, answers, overrides=None):
        self.answers = list(answers)
        # Substring -> answer, for prompts whose position is not worth counting.
        self.overrides = dict(overrides or {})
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


def accept_all(count=40):
    """Press enter for everything: every prompt takes its recommended default."""
    return [""] * count


class TestWizard(IsolatedCase):
    def test_pressing_enter_throughout_yields_the_recommended_config(self):
        prompter = ScriptedPrompter(accept_all())
        data, save = wizard_mod.run(prompter)
        self.assertTrue(save)
        self.assertEqual(data["orchestrator"]["model"]["family"], "sonnet")
        self.assertEqual(data["architect"]["model"]["family"], "fable")
        self.assertEqual(data["implementer"]["model"]["family"], "opus")
        self.assertEqual(data["review_fixer"]["model"]["family"], "opus")
        self.assertEqual(len(data["reviewers"]), 2)
        self.assertEqual(config_mod.validate(data), [])

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
        prompter = ScriptedPrompter([""] * 8 + ["0", ""])
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
            str(provider_index),  # orchestrator CLI: claude
            str(candidates + 1),  # model: "custom"
            "totally-made-up-family",  # the custom value
            "y",  # yes, pin it
            *accept_all(),
        ]
        data, _ = wizard_mod.run(ScriptedPrompter(answers))
        model = data["orchestrator"]["model"]
        self.assertEqual(model["version"], "pinned")
        self.assertEqual(model["id"], "totally-made-up-family")

    def test_reviewer_ids_are_unique_when_roles_repeat(self):
        # 8 enters for the roles, 2 reviewers, both mock/general.
        providers = [name for name, _, _ in wizard_mod.selectable_providers()]
        first = str(providers.index(providers[0]) + 1)
        answers = [""] * 8 + ["2"]
        for _ in range(2):
            answers += [first, "", "1", ""]  # CLI, model, role=general, default id
        answers += ["n", ""]  # no more reviewers, then save
        data, _ = wizard_mod.run(ScriptedPrompter(answers))
        ids = [r["id"] for r in data["reviewers"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_invalid_menu_input_is_re_prompted(self):
        prompter = ScriptedPrompter(["99", "abc", "", *accept_all()])
        wizard_mod.run(prompter)
        self.assertTrue(any("Enter a number" in line for line in prompter.output))

    def test_render_summary_marks_pinned_models(self):
        data = config_mod.default_config()
        data["implementer"]["model"] = {"family": "opus", "version": "pinned", "id": "claude-opus-x"}
        self.assertIn("claude-opus-x / pinned", wizard_mod.render_summary(data))


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
        base = config_mod.deep_merge(config_mod.default_config(), {"review": {"design": {"enabled": True}}})
        prompter = ScriptedPrompter(accept_all())
        data, _ = wizard_mod.run(prompter, None, base)
        self.assertIn("design review: on", "\n".join(prompter.output))
        self.assertNotIn("review", data)

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
        base = cli._layer_base("project", self.project)
        self.assertEqual(base["reviewers"], [])
        prompter = ScriptedPrompter(accept_all())
        data, _ = wizard_mod.run(prompter, None, base)
        self.assertEqual(data["reviewers"], [])


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
        from orchestrator.providers.claude import ClaudeProvider
        from orchestrator.providers.codex import CodexProvider

        for cls in (ClaudeProvider, CodexProvider):
            self.addCleanup(setattr, cls, "which", cls.which)
            cls.which = lambda self: None
        self.path = self.write_user_provider("flaky", self.SOURCE)
        providers.load_user_providers()

    def test_selectable_providers_reports_it_and_goes_on(self):
        failures = []
        names = [name for name, _, _ in wizard_mod.selectable_providers(failures=failures)]
        self.assertEqual(names, ["claude", "codex"])
        self.assertEqual(len(failures), 1)
        name, reason = failures[0]
        self.assertEqual(name, "flaky")
        self.assertIn("RuntimeError: detect exploded", reason)
        self.assertIn(self.path, reason)

    def test_selectable_providers_without_a_failure_list_still_goes_on(self):
        names = [name for name, _, _ in wizard_mod.selectable_providers()]
        self.assertEqual(names, ["claude", "codex"])


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


if __name__ == "__main__":
    unittest.main()
