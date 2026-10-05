"""A design review panel of its own, and a high-risk model per seat.

`review.design.reviewers` replaces the code panel for design rounds, and
`review.design.reviewers_extra` adds to whichever design panel is inherited.
With neither, a design round runs the code panel with every `when` removed,
as it always has. A seat's `high_risk_model` is the model it runs on a round
with a high-risk hit or `--high-risk`, in either panel.
"""

from __future__ import annotations

import json
import os
import unittest

from helpers import IsolatedCase, has_git
from test_design_review import DesignReviewCase, run_cli
from test_reviewers_extra import FITTED, ExtrasCase, agy, mock
from test_wizard import ScriptedPrompter, accept_all

from orchestrator import cli_common, doctor
from orchestrator import config as config_mod
from orchestrator import config_policy as policy_mod
from orchestrator import optimization as opt
from orchestrator import review as review_mod
from orchestrator import wizard as wizard_mod
from orchestrator import workspace as ws
from orchestrator.providers.mock import UNRESOLVABLE_FAMILY


def design(**keys):
    """A file's ``review.design`` holding ``keys``."""
    return {"review": {"design": keys}}


def ids(reviewers):
    return [reviewer["id"] for reviewer in reviewers]


RISKY_PLAN = """# Plan

## Proposed Change
Check the session token in `src/auth.py` before trusting it.

## Files to Modify
- `src/auth.py`
"""

PLAIN_PLAN = """# Plan

## Proposed Change
Round the total in `src/calc.py`.

## Files to Modify
- `src/calc.py`
"""


# --------------------------------------------------------------------------- composing it


class TestComposition(ExtrasCase):
    def test_no_design_keys_design_uses_code_panel_and_ignores_when(self):
        loaded = config_mod.load(self.project)
        self.assertFalse(loaded.has_design_panel())
        self.assertNotIn("reviewers", loaded.data["review"]["design"])
        self.assertEqual(loaded.design_panel_source, "code")
        self.assertEqual(ids(loaded.design_reviewers()), FITTED)
        self.assertEqual(loaded.design_reviewers()[-1], config_mod.without_when(loaded.reviewers()[-1]))
        self.assertTrue([reviewer for reviewer in loaded.reviewers() if "when" in reviewer])
        self.assertFalse([reviewer for reviewer in loaded.design_reviewers() if "when" in reviewer])

    def test_design_reviewers_replace_code_panel_for_design_only(self):
        self.write_global(design(reviewers=[mock("d1"), mock("d2", role="security")]))
        loaded = config_mod.load(self.project)
        self.assertEqual(ids(loaded.design_reviewers()), ["d1", "d2"])
        self.assertEqual(ids(loaded.reviewers()), FITTED)
        self.assertEqual(loaded.design_panel_source, "global")
        labels = [origin.label() for origin in loaded.design_reviewer_origins]
        self.assertEqual(labels, ["global design", "global design"])

    def test_design_extras_join_code_panel_for_design_only(self):
        self.write_global(design(reviewers_extra=[mock("x1")]))
        loaded = config_mod.load(self.project)
        self.assertEqual(ids(loaded.design_reviewers()), [*FITTED, "x1"])
        self.assertEqual(ids(loaded.reviewers()), FITTED)
        self.assertEqual(loaded.design_panel_source, "code")
        labels = [origin.label() for origin in loaded.design_reviewer_origins]
        # The copied seats keep the code panel's origins: the fit, here.
        self.assertEqual(labels, [*(["fit"] * len(FITTED)), "global design extra"])
        self.assertNotIn("reviewers_extra", loaded.data["review"]["design"])

    def test_design_origins_follow_code_extras(self):
        self.write_global({"reviewers_extra": [mock("cx")], **design(reviewers_extra=[mock("x1")])})
        loaded = config_mod.load(self.project)
        self.assertEqual(ids(loaded.design_reviewers()), [*FITTED, "cx", "x1"])
        labels = [origin.label() for origin in loaded.design_reviewer_origins]
        self.assertEqual(labels, [*(["fit"] * len(FITTED)), "global extra", "global design extra"])
        self.assertEqual(len(loaded.design_reviewers()), len(loaded.design_reviewer_origins))

    def test_a_project_code_list_under_global_design_extras(self):
        self.write_global(design(reviewers_extra=[mock("x1")]))
        self.write_project({"reviewers": [mock("p1")]})
        loaded = config_mod.load(self.project)
        self.assertEqual(ids(loaded.design_reviewers()), ["p1", "x1"])
        labels = [origin.label() for origin in loaded.design_reviewer_origins]
        self.assertEqual(labels, ["project", "global design extra"])

    def test_project_design_list_replaces_global_design_list_and_extras(self):
        self.write_global(design(reviewers=[mock("g1")], reviewers_extra=[mock("gx")]))
        self.write_project(design(reviewers=[mock("p1")]))
        loaded = config_mod.load(self.project)
        self.assertEqual(ids(loaded.design_reviewers()), ["p1"])
        self.assertEqual(loaded.design_panel_source, "project")

    def test_a_global_design_list_keeps_both_files_design_extras(self):
        self.write_global(design(reviewers=[mock("g1")], reviewers_extra=[mock("gx")]))
        self.write_project(design(reviewers_extra=[mock("px")]))
        self.assertEqual(ids(config_mod.load(self.project).design_reviewers()), ["g1", "gx", "px"])

    def test_design_extra_id_collision_renamed_with_note(self):
        self.write_global(design(reviewers=[mock("d1")]))
        self.write_project(design(reviewers_extra=[mock("d1", role="security")]))
        loaded = config_mod.load(self.project)
        self.assertEqual(ids(loaded.design_reviewers()), ["d1", "mock-security"])
        self.assertIn(
            "review.design.reviewers_extra[0] in the project file: id d1 is taken by the global "
            "file's design reviewers; it runs as mock-security (reviewer set mock-security --id <name> "
            "keeps a name)",
            loaded.preset_notes,
        )

    def test_inherited_code_seats_lose_when_in_design_panel(self):
        self.write_global(design(reviewers_extra=[mock("x1")]))
        loaded = config_mod.load(self.project)
        code = {reviewer["id"]: reviewer for reviewer in loaded.reviewers()}
        copied = {reviewer["id"]: reviewer for reviewer in loaded.design_reviewers()}
        self.assertEqual(code["claude-security-2"]["when"], "high-risk")
        self.assertNotIn("when", copied["claude-security-2"])

    def test_the_same_id_may_sit_in_both_panels(self):
        self.write_global(design(reviewers=[mock("claude-general")]))
        loaded = config_mod.load(self.project)
        self.assertEqual(ids(loaded.design_reviewers()), ["claude-general"])
        self.assertEqual(loaded.reviewers()[0]["provider"], "claude")
        self.assertEqual(loaded.design_reviewers()[0]["provider"], "mock")


# --------------------------------------------------------------------------- validating it


class TestValidation(ExtrasCase):
    def problems(self):
        loaded = config_mod.load(self.project, validate_result=False)
        return config_mod.validate(
            loaded.data,
            project_layer=loaded.project_layer,
            global_layer=loaded.global_layer,
            origins=loaded.reviewer_origins,
            design_origins=loaded.design_reviewer_origins,
        )

    def test_design_when_paths_refused(self):
        self.write_global(design(reviewers=[mock("d1"), mock("d2", when={"paths": ["*.sql"]})]))
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        expected = "review.design.reviewers[1].when: a design reviewer cannot be when: paths"
        self.assertTrue(problems[0].startswith(expected), problems)
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.project)

    def test_a_design_extra_when_paths_is_refused_too(self):
        self.write_project(design(reviewers_extra=[mock("x1", when={"paths": ["*.sql"]})]))
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("review.design.reviewers_extra[0] in the project file", problems[0])
        self.assertIn("cannot be when: paths", problems[0])

    def test_design_panel_needs_an_always_seat(self):
        self.write_global(design(reviewers=[mock("d1", when="high-risk")]))
        problems = self.problems()
        self.assertIn(
            "review.design.reviewers: at least one reviewer must run always; every reviewer is conditional "
            "(when: high-risk or when: paths)",
            problems,
        )

    def test_a_design_block_that_is_not_a_mapping_is_reported(self):
        """Not replaced by an empty mapping, which would hide it and the global panel."""
        self.write_global(design(reviewers=[mock("d1")]))
        self.write_project({"review": {"design": "d1"}})
        self.assertIn("review.design: must be a mapping", self.problems())
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.project)

    def test_a_design_seat_equal_to_a_code_seat_is_still_checked_without_origins(self):
        data = config_mod.default_config()
        broken = mock("b1")
        broken["provider"] = "nope"
        data["reviewers"].append(broken)
        data["review"]["design"]["reviewers"] = [mock("d1"), config_mod.without_when(broken)]
        problems = config_mod.validate(data)
        self.assertTrue([p for p in problems if p.startswith("review.design.reviewers[1]")], problems)
        self.assertTrue([p for p in problems if p.startswith("reviewers[")], problems)

    def test_review_design_reviewers_must_be_list(self):
        data = config_mod.default_config()
        data["review"]["design"]["reviewers"] = "d1"
        self.assertIn(
            "review.design.reviewers: must be a list (use [] for none)",
            config_mod.validate(data),
        )

    def test_high_risk_model_validation(self):
        cases = (
            ("opus", "high_risk_model must be a mapping"),
            ({"family": " ", "version": "latest"}, "high_risk_model.family must be a non-empty string"),
            ({"family": "opus", "version": "pinned"}, "high_risk_model.version is 'pinned'"),
            ({"family": "opus", "provider": "codex"}, "high_risk_model.provider: not allowed"),
            ({"family": "opus", "options": {}}, "high_risk_model.options: not allowed"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                data = config_mod.default_config()
                data["reviewers"].append(mock("h1", high_risk_model=value))
                found = [problem for problem in config_mod.validate(data) if expected in problem]
                self.assertTrue(found, config_mod.validate(data))
        data = config_mod.default_config()
        data["reviewers"].append(mock("h1", high_risk_model={"family": "opus", "version": "latest"}))
        self.assertEqual(config_mod.validate(data), [])

    def test_referenced_providers_include_design(self):
        data = config_mod.default_config()
        data["review"]["design"]["reviewers"] = [mock("d1")]
        self.assertIn("mock", config_mod.referenced_providers(data))

    def test_optimization_new_keys_validation(self):
        data = config_mod.default_config()
        data["optimization"]["skip_unneeded_roles"] = "yes"
        data["optimization"]["security_paths"] = "*api*"
        data["optimization"]["extra_architecture_paths"] = [""]
        problems = config_mod.validate(data)
        self.assertIn("optimization.skip_unneeded_roles: must be true or false", problems)
        self.assertIn(
            "optimization.security_paths: must be a list of glob patterns (use [] for none)",
            problems,
        )
        blank = "optimization.extra_architecture_paths[0]: must be a non-empty string (got '')"
        self.assertIn(blank, problems)
        data = config_mod.default_config()
        data["optimization"]["security_paths"] = []
        self.assertEqual(config_mod.validate(data), [])

    def test_pinned_differences_ignore_extra_role_paths(self):
        data = config_mod.default_config()
        data["optimization"]["extra_security_paths"] = ["*vault*"]
        data["optimization"]["extra_architecture_paths"] = ["*proto*"]
        self.assertEqual(config_mod.pinned_differences(data), [])


# --------------------------------------------------------------------------- editing it


class TestReviewerCommands(ExtrasCase):
    def design_ids(self):
        return ids(self.loaded().design_reviewers())

    def add_design(self, reviewer_id):
        argv = ("--scope", "global", "--design", "--provider", "mock", "--id", reviewer_id)
        return run_cli("reviewer", "add", *argv)

    def test_reviewer_add_design_goes_to_design_extra(self):
        code, out, err = self.add_design("d1")
        self.assertEqual(code, 0, err)
        self.assertIn("Added design reviewer d1 (mock / ", out)
        self.assertIn("as a design extra; the design panel still follows the code panel", out)
        self.assertEqual(ids(self.global_layer()["review"]["design"]["reviewers_extra"]), ["d1"])
        self.assertEqual(self.design_ids(), [*FITTED, "d1"])
        self.assertEqual(self.ids(), FITTED)

    def test_reviewer_add_design_to_listed_design_panel(self):
        self.write_global(design(reviewers=[mock("d1")]))
        code, out, err = self.add_design("d2")
        self.assertEqual(code, 0, err)
        self.assertNotIn("design extra", out)
        self.assertEqual(ids(self.global_layer()["review"]["design"]["reviewers"]), ["d1", "d2"])
        self.assertEqual(self.design_ids(), ["d1", "d2"])

    def test_reviewer_set_design_seeds_and_drops_when_note(self):
        argv = ("--scope", "global", "--design", "claude-test", "--role", "architecture")
        code, out, err = run_cli("reviewer", "set", *argv)
        self.assertEqual(code, 0, err)
        self.assertIn("copied from the code panel, without its when conditions", out + err)
        listed = self.global_layer()["review"]["design"]["reviewers"]
        self.assertEqual(ids(listed), FITTED)
        self.assertFalse([reviewer for reviewer in listed if "when" in reviewer])
        roles = {reviewer["id"]: reviewer["role"] for reviewer in listed}
        self.assertEqual(roles["claude-test"], "architecture")
        # The code panel is not touched.
        code_roles = {reviewer["id"]: reviewer["role"] for reviewer in self.loaded().reviewers()}
        self.assertEqual(code_roles["claude-test"], "test")

    def test_reviewer_remove_design(self):
        code, _, err = run_cli("reviewer", "remove", "--scope", "global", "--design", "claude-test")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.design_ids(), [name for name in FITTED if name != "claude-test"])
        self.assertEqual(self.ids(), FITTED)

    def test_reviewer_list_design_source(self):
        _, out, _ = run_cli("reviewer", "list", "--design")
        self.assertEqual(out.splitlines()[0], "(design panel: the code panel; when conditions ignored)")
        self.write_global(design(reviewers=[mock("d1")]))
        _, out, _ = run_cli("reviewer", "list", "--design")
        self.assertEqual(out.splitlines()[0], "(design panel: global file)")
        self.assertIn("d1", out)

    def test_reviewer_add_set_clear_high_risk_model(self):
        argv = ("--scope", "global", "--provider", "mock", "--id", "h1", "--high-risk-model", "opus")
        code, _, err = run_cli("reviewer", "add", *argv)
        self.assertEqual(code, 0, err)
        written = self.global_layer()["reviewers_extra"][0]
        self.assertEqual(written["high_risk_model"], {"family": "opus", "version": "latest"})
        self.assertIn("(opus when high-risk)", run_cli("reviewer", "list")[1])
        code, _, err = run_cli("reviewer", "set", "--scope", "global", "h1", "--high-risk-model", "sonnet")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.global_layer()["reviewers_extra"][0]["high_risk_model"]["family"], "sonnet")
        code, _, err = run_cli("reviewer", "set", "--scope", "global", "h1", "--clear-high-risk-model")
        self.assertEqual(code, 0, err)
        self.assertNotIn("high_risk_model", self.global_layer()["reviewers_extra"][0])

    def test_reviewer_add_design_when_paths_refused(self):
        argv = ("--scope", "global", "--design", "--provider", "mock", "--when-paths", "*.sql")
        code, _, err = run_cli("reviewer", "add", *argv)
        self.assertEqual(code, 2)
        self.assertIn("--when-paths applies to the code review only", err)
        self.assertFalse(os.path.exists(config_mod.global_config_path()))

    def test_reviewer_add_set_relevance_and_default(self):
        argv = ("--scope", "global", "--provider", "mock", "--id", "s1", "--role", "security")
        code, _, err = run_cli("reviewer", "add", *argv, "--relevance", "always")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.global_layer()["reviewers_extra"][0]["relevance"], "always")
        code, _, err = run_cli("reviewer", "set", "--scope", "global", "s1", "--relevance", "default")
        self.assertEqual(code, 0, err)
        self.assertNotIn("relevance", self.global_layer()["reviewers_extra"][0])
        before = self.global_layer()
        argv = ("--scope", "global", "--provider", "mock", "--id", "g1", "--relevance", "test")
        code, _, err = run_cli("reviewer", "add", *argv)
        self.assertEqual(code, 2)
        self.assertIn("a general reviewer is never skipped", err)
        self.assertEqual(self.global_layer(), before)

    def test_reviewer_list_shows_relevance(self):
        argv = ("--scope", "global", "--provider", "mock", "--id", "s1", "--role", "security")
        run_cli("reviewer", "add", *argv, "--relevance", "always")
        self.assertIn("(relevance: always)", run_cli("reviewer", "list")[1])

    def add_high_risk_seat(self):
        argv = ("--scope", "global", "--provider", "mock", "--id", "h1", "--high-risk-model", "opus")
        self.assertEqual(run_cli("reviewer", "add", *argv)[0], 0)

    def test_another_provider_removes_the_high_risk_model(self):
        self.add_high_risk_seat()
        code, out, err = run_cli("reviewer", "set", "--scope", "global", "h1", "--provider", "codex")
        self.assertEqual(code, 0, err)
        self.assertNotIn("high_risk_model", self.global_layer()["reviewers_extra"][0])
        self.assertIn("note: model family reset from ", out)
        self.assertIn("; its high_risk_model was removed (--high-risk-model sets another)", out)

    def test_another_provider_with_a_model_removes_it_too(self):
        self.add_high_risk_seat()
        argv = ("--scope", "global", "h1", "--provider", "codex", "--model", "gpt-5")
        code, out, err = run_cli("reviewer", "set", *argv)
        self.assertEqual(code, 0, err)
        written = self.global_layer()["reviewers_extra"][0]
        self.assertNotIn("high_risk_model", written)
        self.assertEqual(written["model"]["family"], "gpt-5")
        self.assertIn(
            "note: provider is now codex; its high_risk_model was removed (--high-risk-model sets another)",
            out,
        )

    def test_another_provider_with_a_high_risk_model_keeps_the_new_one(self):
        self.add_high_risk_seat()
        argv = ("--scope", "global", "h1", "--provider", "codex", "--high-risk-model", "gpt-6")
        code, out, err = run_cli("reviewer", "set", *argv)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.global_layer()["reviewers_extra"][0]["high_risk_model"]["family"], "gpt-6")
        self.assertNotIn("high_risk_model was removed", out)

    def test_the_same_provider_keeps_the_high_risk_model(self):
        self.add_high_risk_seat()
        code, out, err = run_cli("reviewer", "set", "--scope", "global", "h1", "--provider", "mock")
        self.assertEqual(code, 0, err)
        written = self.global_layer()["reviewers_extra"][0]
        self.assertEqual(written["high_risk_model"], {"family": "opus", "version": "latest"})
        self.assertNotIn("note: model family reset", out)
        self.assertNotIn("high_risk_model was removed", out)

    def test_another_provider_with_a_pin_removes_it_too(self):
        self.add_high_risk_seat()
        argv = ("--scope", "global", "h1", "--provider", "codex", "--pin", "gpt-5-2026")
        code, out, err = run_cli("reviewer", "set", *argv)
        self.assertEqual(code, 0, err)
        written = self.global_layer()["reviewers_extra"][0]
        self.assertNotIn("high_risk_model", written)
        self.assertEqual(written["model"]["id"], "gpt-5-2026")
        self.assertNotIn("note: model family reset", out)
        self.assertIn("note: provider is now codex; its high_risk_model was removed", out)

    def test_another_provider_with_clear_high_risk_model(self):
        self.add_high_risk_seat()
        argv = ("--scope", "global", "h1", "--provider", "codex", "--clear-high-risk-model")
        code, _, err = run_cli("reviewer", "set", *argv)
        self.assertEqual(code, 0, err)
        written = self.global_layer()["reviewers_extra"][0]
        self.assertNotIn("high_risk_model", written)
        self.assertEqual(written["provider"], "codex")

    def test_removing_the_only_always_design_seat_is_refused(self):
        self.write_global(design(reviewers=[mock("d1"), mock("d2", when="high-risk")]))
        before = self.global_layer()
        code, _, err = run_cli("reviewer", "remove", "--scope", "global", "--design", "d1")
        self.assertEqual(code, 2)
        self.assertIn("review.design.reviewers: at least one reviewer must run always", err)
        self.assertEqual(self.global_layer(), before)


class TestDesignEditsInAProjectFile(ExtrasCase):
    """`reviewer set/remove --design` and `config set` on design paths, in the project file."""

    def project_layer(self):
        return config_mod.read_config_file(self.project_file())

    def project_design(self):
        return self.project_layer()["review"]["design"]

    def test_a_fitted_panels_agy_seat_is_left_out_of_the_copy(self):
        """No file lists the code panel: its global extra on agy is left out, as `_seed_panel` does."""
        self.write_global({"reviewers_extra": [agy("agy-x")]})
        code, out, err = run_cli("reviewer", "remove", "--scope", "project", "--design", "claude-test")
        self.assertEqual(code, 0, err)
        left_out = (
            "not copied into .dev-orchestra.yaml: agy-x -- a reviewer on agy is taken only from "
            "the global config"
        )
        self.assertIn(left_out, out)
        self.assertEqual(ids(self.project_design()["reviewers"]), [n for n in FITTED if n != "claude-test"])

    def test_a_design_panel_the_global_file_lists_is_copied_whole(self):
        """The user chose those seats: copied as they are, agy included, as `_seed_panel` copies."""
        self.write_global(design(reviewers=[mock("d1"), mock("d2"), agy("gem")]))
        code, out, err = run_cli("reviewer", "remove", "--scope", "project", "--design", "d2")
        self.assertEqual(code, 0, err)
        self.assertEqual(ids(self.project_design()["reviewers"]), ["d1", "gem"])
        self.assertNotIn("not copied", out + err)

    def test_a_code_panel_the_global_file_lists_is_copied_whole(self):
        """No design panel anywhere: the seats come from the code list the global file chose."""
        self.write_global({"reviewers": [mock("g1"), mock("g2"), agy("gem")]})
        code, out, err = run_cli("reviewer", "remove", "--scope", "project", "--design", "g2")
        self.assertEqual(code, 0, err)
        self.assertEqual(ids(self.project_design()["reviewers"]), ["g1", "gem"])
        self.assertNotIn("not copied", out + err)

    def test_the_project_files_own_agy_seat_is_kept_in_the_copy(self):
        """It is the file's own, so the copy changes nothing about where it comes from."""
        self.write_project({"reviewers_extra": [agy("pa")]})
        code, out, err = run_cli("reviewer", "remove", "--scope", "project", "--design", "claude-test")
        self.assertEqual(code, 0, err)
        self.assertIn("pa", ids(self.project_design()["reviewers"]))
        self.assertNotIn("not copied", out + err)

    def test_set_design_edits_the_files_own_design_extra_in_place(self):
        self.write_project(design(reviewers_extra=[mock("px")]))
        argv = ("--scope", "project", "--design", "px", "--role", "security")
        code, _, err = run_cli("reviewer", "set", *argv)
        self.assertEqual(code, 0, err)
        written = self.project_design()
        self.assertEqual(written["reviewers_extra"][0]["role"], "security")
        self.assertNotIn("reviewers", written)

    def test_a_renamed_design_extra_is_not_selected_by_its_written_id(self):
        self.write_global(design(reviewers=[mock("d1")]))
        self.write_project(design(reviewers_extra=[mock("d1", role="security")]))
        before = self.project_layer()
        argv = ("--scope", "project", "--design", "d1", "--role", "test")
        code, _, err = run_cli("reviewer", "set", *argv)
        self.assertEqual(code, 2)
        self.assertIn(
            "reviewer d1: review.design.reviewers_extra[0] in the project file runs as mock-security, "
            "since d1 is taken; use mock-security",
            err,
        )
        self.assertEqual(self.project_layer(), before)

    def test_config_set_design_provider_agy_in_the_project_file_is_refused(self):
        self.write_project(design(reviewers=[mock("d1"), mock("d2")]))
        before = self.project_layer()
        argv = ("--scope", "project", "review.design.reviewers[0].provider", "agy")
        code, _, err = run_cli("config", "set", *argv)
        self.assertEqual(code, 2)
        self.assertIn("reviewer d1:", err)
        self.assertIn("reviewers on agy are taken only from the global config", err)
        self.assertEqual(self.project_layer(), before)

    def test_config_set_that_leaves_no_always_design_seat_is_refused(self):
        self.write_global(design(reviewers=[mock("d1")]))
        before = config_mod.read_config_file(config_mod.global_config_path())
        argv = ("--scope", "global", "review.design.reviewers[0].when", "high-risk")
        code, _, err = run_cli("config", "set", *argv)
        self.assertEqual(code, 2)
        self.assertIn("review.design.reviewers: at least one reviewer must run always", err)
        self.assertEqual(config_mod.read_config_file(config_mod.global_config_path()), before)

    def test_a_design_problem_already_there_does_not_block_another_write(self):
        self.write_global(design(reviewers=[mock("d1", when="high-risk")]))
        argv = ("--scope", "global", "review.design.reviewers[0].role", "security")
        code, _, err = run_cli("config", "set", *argv)
        self.assertEqual(code, 0, err)
        written = config_mod.read_config_file(config_mod.global_config_path())
        self.assertEqual(written["review"]["design"]["reviewers"][0]["role"], "security")


# --------------------------------------------------------------------------- showing it


#: A design seat that runs opus on a high-risk plan.
OPUS_ON_RISK = mock("d1", high_risk_model={"family": "opus", "version": "latest"})


class TestShowingIt(ExtrasCase):
    def test_config_show_design_block_fallback_and_set(self):
        _, out, _ = run_cli("config", "show")
        fallback = "(the code panel; when conditions ignored)  (review.design.reviewers)"
        self.assertIn("  Design reviews\n    %s" % fallback, out)
        self.write_global(design(reviewers=[mock("d1")]))
        _, out, _ = run_cli("config", "show")
        block = out.split("  Design reviews\n", 1)[1]
        self.assertIn("d1", block.splitlines()[0])
        payload = json.loads(run_cli("config", "show", "--json")[1])
        self.assertEqual(payload["design_reviewer_origins"], ["global design"])

    def test_config_show_high_risk_model(self):
        self.write_global(design(reviewers=[OPUS_ON_RISK]))
        self.assertIn("(opus when high-risk)", run_cli("config", "show")[1])

    def test_config_show_skip_unneeded_roles_line(self):
        line = "    skip unneeded roles: %s  (optimization.skip_unneeded_roles)"
        self.assertIn(line % "on", run_cli("config", "show")[1])
        self.write_global({"optimization": {"skip_unneeded_roles": False}})
        self.assertIn(line % "off", run_cli("config", "show")[1])

    def test_doctor_json_design_reviewers(self):
        report = doctor.collect(self.project, probe_models=False)
        self.assertEqual(report["design_panel_source"], "code")
        self.assertEqual(ids(report["design_reviewers"]), FITTED)
        self.assertNotIn("Design:", doctor.render(report))
        self.write_global(design(reviewers=[OPUS_ON_RISK]))
        report = doctor.collect(self.project, probe_models=False)
        self.assertEqual(report["design_panel_source"], "global")
        (entry,) = report["design_reviewers"]
        self.assertEqual((entry["id"], entry["origin"]), ("d1", "global design"))
        self.assertEqual(entry["high_risk_model"]["family"], "opus")
        self.assertIn("Design:", doctor.render(report))

    def test_doctor_reports_a_high_risk_model_that_does_not_resolve(self):
        unresolvable = {"family": UNRESOLVABLE_FAMILY, "version": "latest"}
        self.write_global(design(reviewers=[mock("d1", high_risk_model=unresolvable)]))
        report = doctor.collect(self.project, probe_models=False)
        (entry,) = report["design_reviewers"]
        self.assertEqual(entry["high_risk_model"]["family"], UNRESOLVABLE_FAMILY)
        self.assertEqual(entry["high_risk_model"]["status"], "unresolvable-model")
        found = [p for p in report["problems"] if "(high-risk model)" in p and "cannot be resolved" in p]
        self.assertTrue(found, report["problems"])

    def test_doctor_reports_empty_role_patterns(self):
        judged = [mock("s1", role="security", relevance="security"), mock("a1", role="architecture")]
        empty = {"security_paths": [], "architecture_paths": []}
        self.write_global({"reviewers_extra": judged, "optimization": empty})
        problems = doctor.collect(self.project, probe_models=False)["problems"]
        for key in ("security_paths", "architecture_paths"):
            head = "optimization.%s: no pattern" % key
            self.assertTrue([problem for problem in problems if problem.startswith(head)], problems)
        off = {"security_paths": [], "skip_unneeded_roles": False}
        self.write_global({"reviewers_extra": judged, "optimization": off})
        problems = doctor.collect(self.project, probe_models=False)["problems"]
        self.assertFalse([problem for problem in problems if "security_paths" in problem], problems)

    def test_doctor_ignores_empty_patterns_no_seat_is_judged_by(self):
        """The fitted panel's security seat does not opt in, and it has no architecture seat."""
        self.write_global({"optimization": {"security_paths": [], "architecture_paths": []}})
        problems = doctor.collect(self.project, probe_models=False)["problems"]
        self.assertFalse([problem for problem in problems if "_paths: no pattern" in problem], problems)

    def test_doctor_reports_an_empty_rule_a_design_seat_opts_into(self):
        seat = mock("d1", role="security", relevance="security")
        self.write_global({**design(reviewers_extra=[seat]), "optimization": {"security_paths": []}})
        problems = doctor.collect(self.project, probe_models=False)["problems"]
        head = "optimization.security_paths: no pattern"
        self.assertTrue([problem for problem in problems if problem.startswith(head)], problems)


class TestTheWizard(ExtrasCase):
    def test_wizard_keeps_design_keys(self):
        existing = {"version": 1, **design(reviewers=[mock("d1")])}
        prompter = ScriptedPrompter(accept_all())
        base = cli_common._fitted_base("global", existing)
        data, _save = wizard_mod.run(prompter, existing, base, scope="global")
        said = "\n".join(prompter.output)
        self.assertEqual(data["review"]["design"]["reviewers"], [mock("d1")])
        kept = "This file's review.design.reviewers is kept as it is; reviewer add/set/remove --design"
        self.assertIn(kept + " manage it.", said)
        summary = said[said.rindex("Configuration") :]
        self.assertIn("  Design reviews\n", summary)
        self.assertIn("d1", summary.split("  Design reviews\n", 1)[1])


# --------------------------------------------------------------------------- policy per panel


class TestRefusalsPerPanel(ExtrasCase):
    def setUp(self):
        super().setUp()
        self.fake_clis(claude=True, agy=True)

    def test_project_design_seat_on_agy_refused(self):
        self.write_project(design(reviewers=[mock("d1"), agy("gem")]))
        loaded = self.loaded()
        refusals = policy_mod.reviewer_raw_arg_refusals(loaded, "design")
        self.assertEqual(list(refusals), ["gem"])
        self.assertIn("reviewers on agy are taken only from the global config", refusals["gem"])
        self.assertEqual(policy_mod.reviewer_raw_arg_refusals(loaded, "code"), {})
        self.assertEqual(policy_mod.reviewer_provider_refusals(loaded, "code"), {})

    def test_refusals_keyed_per_panel_same_id(self):
        self.write_global({"reviewers": [mock("r1")]})
        self.write_project(design(reviewers=[mock("d1"), agy("r1")]))
        loaded = self.loaded()
        refusals = policy_mod.reviewer_raw_arg_refusals(loaded, "design")
        self.assertEqual(list(refusals), ["r1"])
        self.assertIn("reviewers on agy are taken only from the global config", refusals["r1"])
        self.assertEqual(policy_mod.reviewer_raw_arg_refusals(loaded, "code"), {})

    def test_a_project_design_seat_copying_a_global_agy_seat_is_refused(self):
        """Same id, provider and options as the global code seat: still the project's own seat."""
        self.write_global({"reviewers": [mock("g1"), agy("r1")]})
        self.write_project(design(reviewers=[mock("d1"), agy("r1")]))
        loaded = self.loaded()
        refusals = policy_mod.reviewer_raw_arg_refusals(loaded, "design")
        self.assertEqual(list(refusals), ["r1"])
        self.assertIn("reviewers on agy are taken only from the global config", refusals["r1"])
        self.assertEqual(policy_mod.reviewer_raw_arg_refusals(loaded, "code"), {})
        raw = [entry for entry in policy_mod.read_only_raw_args(loaded) if entry.reviewer_id == "r1"]
        self.assertEqual([entry.panel for entry in raw], ["code", "design"])

    def test_a_project_design_seat_copying_global_options_args_is_refused(self):
        seat = mock("r2", options={"args": ["--x"]})
        self.write_global({"reviewers": [mock("g1"), seat]})
        self.write_project(design(reviewers=[mock("d1"), dict(seat)]))
        refusals = policy_mod.reviewer_raw_arg_refusals(self.loaded(), "design")
        self.assertEqual(list(refusals), ["r2"])
        self.assertIn("options.args is set in the project config", refusals["r2"])

    def test_a_global_design_seat_copying_a_refused_project_agy_seat_is_warned(self):
        """The project code seat is refused; the global design seat of the same run is not, so it warns."""
        self.write_global(design(reviewers=[mock("d1"), agy("r1")]))
        self.write_project({"reviewers": [mock("g1"), agy("r1")]})
        loaded = self.loaded()
        refused = list(policy_mod.project_raw_arg_refusals(loaded))
        self.assertEqual(refused, ["reviewers[1]"])
        origins = loaded.design_reviewer_origins
        warned = policy_mod.reviewer_enforcement_warnings(loaded.data, refused, "design", origins)
        self.assertEqual(list(warned), ["r1"])
        self.assertTrue(warned["r1"].startswith("design reviewer r1: "), warned)
        self.assertEqual(policy_mod.reviewer_enforcement_warnings(loaded.data, refused, "code", origins), {})
        lines = policy_mod.read_only_enforcement_warnings(loaded.data, refused, origins)
        self.assertEqual([line for line in lines if "r1" in line], [warned["r1"]])

    def test_validate_warns_about_the_global_design_seat(self):
        self.write_global(design(reviewers=[mock("d1"), agy("r1")]))
        self.write_project({"reviewers": [mock("g1"), agy("r1")]})
        warnings = json.loads(run_cli("config", "validate", "--json")[1])["warnings"]
        self.assertTrue([line for line in warnings if line.startswith("design reviewer r1: ")], warnings)

    def test_a_copied_code_seat_meets_its_code_seats_refusal_only(self):
        """Design extras alone: the copied seats are the code panel's runs, listed once."""
        self.write_global({"reviewers": [mock("g1"), agy("r1")], **design(reviewers_extra=[mock("x1")])})
        loaded = self.loaded()
        design_seats = [entry for entry in policy_mod.read_only_raw_args(loaded) if entry.panel == "design"]
        self.assertEqual([entry.reviewer_id for entry in design_seats], ["x1"])
        self.assertEqual(policy_mod.reviewer_raw_arg_refusals(loaded, "design"), {})


# --------------------------------------------------------------------------- deciding a design round


class TestDecidingADesignRound(unittest.TestCase):
    PANEL = (mock("gen"), mock("hr", when="high-risk"))

    def decide(self, text, **keep):
        settings = config_mod.default_config()["optimization"]
        scan = review_mod.plan_tokens(text)
        return opt.decide_design_round(settings, scan, "ok", list(self.PANEL), **keep)

    def test_design_high_risk_seat_runs_on_risky_plan_only(self):
        risky = self.decide(RISKY_PLAN)
        self.assertTrue(risky.is_high_risk)
        self.assertEqual(
            risky.conditional,
            [{"id": "hr", "when": "high-risk", "runs": True, "reason": "src/auth.py matches *auth*"}],
        )
        plain = self.decide(PLAIN_PLAN)
        self.assertFalse(plain.is_high_risk)
        self.assertEqual(
            plain.conditional,
            [{"id": "hr", "when": "high-risk", "runs": False, "reason": "no high-risk path matched"}],
        )

    def test_a_declared_design_round_adds_the_seat_and_is_high_risk(self):
        declared = self.decide(PLAIN_PLAN, declared=True)
        self.assertTrue(declared.is_high_risk)
        self.assertEqual(declared.risk_reason(), "declared with --high-risk")
        self.assertTrue(declared.conditional[0]["runs"])

    def test_design_risk_equals_decide_design_hits(self):
        settings = config_mod.default_config()["optimization"]
        scan = review_mod.plan_tokens(RISKY_PLAN)
        decision = opt.decide_design("auto", settings, scan, plan_state="ok", round_ran=False)
        pairs = [(token, pattern) for token, _section, pattern in opt.design_risk(settings, scan)]
        self.assertEqual(decision.high_risk, pairs)
        self.assertEqual(opt.design_risk(settings, None), [])

    def test_plan_files_matches_decide_design_count(self):
        settings = config_mod.default_config()["optimization"]
        text = PLAIN_PLAN + "- `src/total.py`\n- `docs/calc.md`\n- `tests/test_calc.py`\n"
        scan = review_mod.plan_tokens(text)
        files = opt.plan_files(scan)
        self.assertEqual(files.code, ["src/calc.py", "src/total.py"])
        self.assertEqual(files.doubt, "")
        decision = opt.decide_design("auto", settings, scan, plan_state="ok", round_ran=False)
        self.assertEqual(decision.files, len(files.code))
        unjudged = opt.plan_files(review_mod.plan_tokens("# Plan\n"))
        self.assertEqual(unjudged.doubt, "no Files to Modify section")


class TestRiskModel(unittest.TestCase):
    def test_risk_model_swaps_only_when_set(self):
        plain = mock("m1")
        self.assertEqual(opt.risk_model(plain, True), (plain, False))
        seat = mock("m1", high_risk_model={"family": "opus", "version": "latest"})
        self.assertEqual(opt.risk_model(seat, False), (seat, False))
        switched, moved = opt.risk_model(seat, True)
        self.assertTrue(moved)
        self.assertEqual(switched["model"], {"family": "opus", "version": "latest"})
        self.assertEqual(seat["model"]["family"], "small")

    def test_risk_model_keeps_provider_and_options(self):
        seat = mock("m1", options={"args": ["--x"]}, high_risk_model={"family": "opus", "version": "latest"})
        switched, _ = opt.risk_model(seat, True)
        self.assertEqual((switched["provider"], switched["options"]), ("mock", {"args": ["--x"]}))
        switched["options"]["args"].append("--y")
        self.assertEqual(seat["options"], {"args": ["--x"]})

    def test_the_note_names_both_models(self):
        seat = mock("m1", high_risk_model={"family": "opus", "version": "latest"})
        switched, _ = opt.risk_model(seat, True)
        note = opt.risk_model_note(seat, switched, "src/auth.py matches *auth*")
        self.assertEqual(note, "high-risk round (src/auth.py matches *auth*): m1 runs opus instead of small")

    def test_a_pinned_model_is_named_by_its_id(self):
        pinned = {"family": "opus", "version": "pinned", "id": "opus-2026-01"}
        self.assertEqual(opt.model_label(pinned), "opus-2026-01")
        self.assertEqual(opt.model_label({"family": "opus", "version": "pinned"}), "opus")
        self.assertEqual(opt.model_label(None), "default")
        seat = mock("m1", high_risk_model=pinned)
        switched, _ = opt.risk_model(seat, True)
        note = opt.risk_model_note(seat, switched, "declared with --high-risk")
        expected = "high-risk round (declared with --high-risk): m1 runs opus-2026-01 instead of small"
        self.assertEqual(note, expected)


# --------------------------------------------------------------------------- running it


class TestRunningADesignRound(DesignReviewCase):
    """m1 general and m2 architecture on the code panel, which the design round inherits."""

    def last_design_event(self):
        events = self.workspace.read_state().get("events") or []
        return [event for event in events if event.get("stage") == "design_review"][-1]

    def entries(self):
        return {entry["id"]: entry for entry in self.last_design_event()["reviewers"]}

    def loaded_reviewers(self):
        return config_mod.load(self.project).reviewers()

    def test_design_high_risk_declared_accepted_exit_0(self):
        argv = ("--design", "--provider", "mock", "--id", "d3", "--when", "high-risk")
        self.assertEqual(run_cli("reviewer", "add", *argv)[0], 0)
        self.write_plan()
        code, out, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 0, err)
        self.assertIn("note: d3 (when: high-risk) left out: no high-risk path matched", err.splitlines())
        self.assertIn("2 successful, 0 failed", out)
        code, out, err = run_cli("review", "run", "--design", "--high-risk")
        self.assertEqual(code, 0, err)
        self.assertIn("note: d3 (when: high-risk) added: declared with --high-risk", err.splitlines())
        self.assertIn("3 successful, 0 failed", out)
        self.assertTrue(self.last_design_event()["optimization"]["declared"])

    def test_design_switches_model_on_plan_hit(self):
        self.assertEqual(run_cli("reviewer", "set", "m1", "--high-risk-model", "opus")[0], 0)
        self.write_plan(RISKY_PLAN)
        code, _, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 0, err)
        notes = [line for line in err.splitlines() if line.startswith("note: high-risk round (")]
        self.assertEqual(len(notes), 1, err)
        expected = "note: high-risk round (src/auth.py matches *auth*): m1 runs opus instead of "
        self.assertTrue(notes[0].startswith(expected), notes)

    def test_design_switch_under_enabled_on(self):
        """The plan's scan decides the model whatever review.design.enabled says."""
        run_cli("config", "set", "review.design.enabled", "true")
        self.assertEqual(run_cli("reviewer", "set", "m1", "--high-risk-model", "opus")[0], 0)
        self.write_plan(RISKY_PLAN)
        code, _, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 0, err)
        self.assertIn("m1 runs opus instead of", err)
        self.assertEqual(self.entries()["m1"]["model_slot"], opt.HIGH_RISK_SLOT)
        self.assertEqual(self.entries()["m1"]["model"], "opus")

    def test_no_switch_on_a_plain_plan(self):
        self.assertEqual(run_cli("reviewer", "set", "m1", "--high-risk-model", "opus")[0], 0)
        self.write_plan(PLAIN_PLAN)
        _, _, err = run_cli("review", "run", "--design")
        self.assertNotIn("high-risk round", err)
        self.assertNotIn("model_slot", self.entries()["m1"])
        self.assertNotEqual(self.entries()["m1"]["model"], "opus")

    def test_design_round_records_model_slot_and_conditional(self):
        usual = {entry["id"]: entry for entry in self.loaded_reviewers()}
        self.assertEqual(run_cli("reviewer", "set", "m1", "--high-risk-model", "opus")[0], 0)
        self.write_plan(RISKY_PLAN)
        run_cli("review", "run", "--design")
        entries = self.entries()
        self.assertEqual(entries["m1"]["model_slot"], "high-risk")
        self.assertNotIn("model_slot", entries["m2"])
        # What the provider ran, not only what the entry was labelled.
        self.assertEqual(entries["m1"]["model"], "opus")
        self.assertEqual(entries["m2"]["model"], usual["m2"]["model"]["family"])
        block = self.last_design_event()["optimization"]
        self.assertEqual(block["high_risk"], [{"path": "src/auth.py", "pattern": "*auth*"}])
        # The hit keeps the architecture seat, and says so.
        records = {record["id"]: record for record in block["conditional"]}
        kept = (records["m2"]["runs"], records["m2"]["reason"])
        self.assertEqual(kept, (True, "src/auth.py matches *auth*"))

    def test_consolidate_design_uses_design_panel_and_excludes_left_out(self):
        argv = ("--design", "--provider", "mock", "--id", "d3", "--role", "general")
        self.assertEqual(run_cli("reviewer", "add", *argv)[0], 0)
        self.write_plan("# Plan\n\n## Proposed Change\nReword it.\n\n## Files to Modify\n- `docs/guide.md`\n")
        self.assertEqual(run_cli("review", "run", "--design", "--only", "m2")[0], 0)
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)
        self.assertEqual(run_cli("review", "consolidate", "--design")[0], 0)
        data = ws.read_json(self.design.consolidated_json_path, {})
        reported = {name for item in data["findings"] for name in item["reported_by"]}
        self.assertIn("d3", reported)
        self.assertNotIn("m2", reported)

    def test_review_status_design_lists_design_panel(self):
        self.write_plan(PLAIN_PLAN)
        payload = json.loads(run_cli("review", "status", "--design", "--json")[1])
        self.assertEqual((payload["reviewers"], payload["panel_source"]), (["m1", "m2"], "code"))
        _, out, _ = run_cli("review", "status", "--design")
        self.assertIn("design panel: m1, m2 (the code panel; when conditions ignored)", out)
        self.assertIn("; m2 left out (", out)
        argv = ("--design", "--provider", "mock", "--id", "d3", "--role", "general")
        run_cli("reviewer", "add", *argv)
        payload = json.loads(run_cli("review", "status", "--design", "--json")[1])
        self.assertEqual(payload["reviewers"], ["m1", "m2", "d3"])


# --------------------------------------------------------------------------- code rounds


FINDING = """## Finding
- Severity: high
- File: app.py
- Line: 2
- Category: correctness
- Problem: subtraction where addition was intended
- Impact: every caller gets the wrong total
- Evidence: return a - b
- Fix: restore the addition
"""


#: The panel `config setup --defaults` writes, which each round test replaces.
FITTED_DEFAULTS = ("claude-general", "codex-general", "claude-security-2", "claude-security", "claude-test")


@unittest.skipUnless(has_git(), "git is required")
class TestCodeRoundHighRiskModel(IsolatedCase):
    """m1 general switches to opus on a high-risk round; m2 general does not."""

    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write("app.py", "def add(a, b):\n    return a - b\n")
        mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(mock_dir)
        with open(os.path.join(mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING)
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = mock_dir
        run_cli("config", "setup", "--defaults")
        for name in FITTED_DEFAULTS:
            run_cli("reviewer", "remove", name)
        argv = ("--provider", "mock", "--id", "m1", "--role", "general", "--high-risk-model", "opus")
        run_cli("reviewer", "add", *argv)
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m2", "--role", "general")
        run_cli("state", "record", "test", "ok")
        self.workspace = self.cli_workspace()
        seats = {reviewer["id"]: reviewer for reviewer in config_mod.load(self.project).reviewers()}
        self.usual_model = seats["m2"]["model"]["family"]
        self.assertNotEqual(self.usual_model, "opus")

    def entries(self):
        events = self.workspace.read_state().get("events") or []
        event = [event for event in events if event.get("stage") == "review"][-1]
        return {entry["id"]: entry for entry in event["reviewers"]}

    def test_code_round_high_risk_model_on_path_hit(self):
        self.write("auth.py", "def check():\n    return True\n")
        run_cli("review", "snapshot")
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 0, err)
        self.assertIn("note: high-risk round (auth.py matches *auth*): m1 runs opus instead of", err)
        entries = self.entries()
        self.assertEqual(entries["m1"]["model_slot"], "high-risk")
        self.assertNotIn("model_slot", entries["m2"])
        # What the provider ran, not only what the entry was labelled.
        self.assertEqual((entries["m1"]["model"], entries["m2"]["model"]), ("opus", self.usual_model))

    def test_code_round_high_risk_model_on_declared(self):
        run_cli("review", "snapshot")
        code, _, err = run_cli("review", "run", "--high-risk")
        self.assertEqual(code, 0, err)
        self.assertIn("note: high-risk round (declared with --high-risk): m1 runs opus instead of", err)
        entries = self.entries()
        self.assertEqual(entries["m1"]["model_slot"], "high-risk")
        self.assertEqual((entries["m1"]["model"], entries["m2"]["model"]), ("opus", self.usual_model))

    def test_code_round_high_risk_model_not_on_low_risk(self):
        run_cli("config", "set", "optimization.level", "quality")
        run_cli("review", "snapshot")
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 0, err)
        self.assertNotIn("high-risk round", err)
        self.assertNotIn("model_slot", self.entries()["m1"])
        self.assertEqual(self.entries()["m1"]["model"], self.usual_model)

    def test_code_round_high_risk_model_under_only(self):
        self.write("auth.py", "def check():\n    return True\n")
        run_cli("review", "snapshot")
        code, _, err = run_cli("review", "run", "--only", "m1")
        self.assertEqual(code, 0, err)
        self.assertIn("m1 runs opus instead of", err)
        self.assertEqual(self.entries()["m1"]["model_slot"], "high-risk")
        self.assertEqual(self.entries()["m1"]["model"], "opus")


if __name__ == "__main__":
    unittest.main()
