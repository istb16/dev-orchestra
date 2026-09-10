"""The setup wizard, driven with scripted answers instead of a terminal."""

from __future__ import annotations

import unittest

from helpers import IsolatedCase

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


if __name__ == "__main__":
    unittest.main()
