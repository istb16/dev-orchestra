"""Reviewer ``instructions`` in the review prompts, and the round that changes them (#350).

A seat's instructions block follows its role guidance, on the code path and
the design path; with none set the prompt is the one it always was. A change
to a watched path -- the project file, or a ``file:`` the instructions read --
is recorded on the snapshot and makes the round high-risk.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from helpers import IsolatedCase, has_git, run_cli
from test_design_review import DesignReviewCase

from orchestrator import config as config_mod
from orchestrator import instructions, review_fanout, review_snapshot, round_progress
from orchestrator import review as review_mod
from orchestrator import workspace as ws
from orchestrator.providers.mock import MockProvider
from orchestrator.review_common import DESIGN_ROLE_GUIDANCE, ROLE_GUIDANCE

BLOCK = "## Project instructions\n\nCheck the migrations."


def reviewer(reviewer_id, role="general"):
    return {"id": reviewer_id, "provider": "mock", "role": role}


class PromptTest(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace()

    def test_code_prompt_appends_after_guidance(self):
        plain = review_mod.build_review_prompt(reviewer("r1"), self.workspace, "diff body").text
        text = review_mod.build_review_prompt(
            reviewer("r1"), self.workspace, "diff body", instructions=BLOCK
        ).text
        guidance = ROLE_GUIDANCE["general"]
        self.assertNotIn(BLOCK, plain)
        self.assertIn(guidance + "\n\n" + BLOCK, text)
        self.assertEqual(text.replace("\n\n" + BLOCK, "", 1), plain)

    def test_code_prompt_unchanged_without_instructions(self):
        plain = review_mod.build_review_prompt(reviewer("r1"), self.workspace, "diff body").text
        empty = review_mod.build_review_prompt(reviewer("r1"), self.workspace, "diff body", instructions="")
        self.assertEqual(empty.text, plain)

    def test_design_prompt_appends_after_guidance(self):
        built = review_mod.build_design_review_prompt(reviewer("r1"), self.workspace, "# Plan\n", "Do it.")
        plain = built.text
        text = review_mod.build_design_review_prompt(
            reviewer("r1"), self.workspace, "# Plan\n", "Do it.", instructions=BLOCK
        ).text
        guidance = DESIGN_ROLE_GUIDANCE["general"]
        self.assertIn(guidance + "\n\n" + BLOCK, text)
        self.assertEqual(text.replace("\n\n" + BLOCK, "", 1), plain)


@unittest.skipUnless(has_git(), "git is required")
class FanoutTest(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "x = 1\n")
        self.commit_all("init")
        self.write("app.py", "x = 2\n")
        self.workspace = self.cli_workspace()
        review_mod.create_snapshot(self.workspace)

    def _prompts(self, options):
        built = {}
        original = review_fanout.build_review_prompt

        def capture(seat, *args, **kwargs):
            result = original(seat, *args, **kwargs)
            built[seat["id"]] = result.text
            return result

        with mock.patch.object(review_fanout, "build_review_prompt", capture):
            review_mod.run_reviews([reviewer("r1"), reviewer("r2")], self.workspace, options)
        return built

    def test_instructions_for_each_seat(self):
        texts = {"r1": "## Project instructions\n\nfor r1", "r2": ""}
        built = self._prompts(
            review_mod.FanoutOptions(parallel=False, instructions_for=lambda seat: texts[seat["id"]])
        )
        self.assertIn(texts["r1"], built["r1"])
        self.assertNotIn("## Project instructions", built["r2"])

    def test_no_instructions_for_is_the_old_prompt(self):
        built = self._prompts(review_mod.FanoutOptions(parallel=False))
        expected = review_mod.build_review_prompt(
            reviewer("r1"), self.workspace, ws.read_text(self.workspace.snapshot_path)
        ).text
        self.assertEqual(built["r1"], expected)


def capture_prompts(case):
    """Record the prompt of every mock run ``case`` makes; the list they go in."""
    prompts = []
    original = MockProvider.run

    def recording(provider, prompt, *args, **kwargs):
        prompts.append(prompt)
        return original(provider, prompt, *args, **kwargs)

    case.enterContext(mock.patch.object(MockProvider, "run", recording))
    return prompts


@unittest.skipUnless(has_git(), "git is required")
class ReviewRunTest(IsolatedCase):
    """The prompt each mock reviewer was given, as ``review run`` built it."""

    MARKER = "Look hard at the migration ordering."
    SEAT = "Session handling lives in app/auth/."

    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "x = 1\n")
        self.commit_all("init")
        self.write("app.py", "x = 2\n")
        run_cli("config", "setup", "--defaults")
        for seat in ("claude-general", "codex-general", "claude-security", "claude-test"):
            run_cli("reviewer", "remove", seat)
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        self.prompts = capture_prompts(self)

    def set_global(self, **blocks):
        data = config_mod.load(self.project).data
        data.update(blocks)
        config_mod.write_config_file(config_mod.global_config_path(), data)

    def review(self):
        run_cli("review", "snapshot")
        _, out, err = run_cli("review", "run")
        self.assertIn("1 successful, 0 failed", out, err)
        (prompt,) = self.prompts
        return prompt, err

    def test_reviewer_instructions_reach_the_prompt(self):
        self.set_global(instructions={"reviewers": self.MARKER})
        prompt, _ = self.review()
        self.assertIn("## Project instructions", prompt)
        self.assertIn("Sources: global inline.\n\n" + self.MARKER, prompt)

    def test_a_seat_own_instructions_reach_the_prompt(self):
        data = config_mod.load(self.project).data
        seats = data["reviewers"]
        seats[0]["instructions"] = self.SEAT
        self.set_global(instructions={"reviewers": self.MARKER}, reviewers=seats)
        prompt, _ = self.review()
        self.assertIn(self.MARKER + "\n\n" + self.SEAT, prompt)
        self.assertIn("Sources: global inline, global inline.", prompt)

    def test_none_set_leaves_the_prompt_alone(self):
        prompt, _ = self.review()
        self.assertNotIn("## Project instructions", prompt)

    def test_unread_file_noted_and_review_goes_on(self):
        self.set_global(instructions={"reviewers": {"file": "docs/missing.md"}})
        prompt, err = self.review()
        self.assertIn("note: instructions.reviewers: docs/missing.md: not found; reviewing without it", err)
        self.assertNotIn("## Project instructions", prompt)

    def test_a_file_two_seats_share_is_noted_once(self):
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m2", "--role", "general")
        self.set_global(instructions={"reviewers": {"file": "docs/missing.md"}})
        run_cli("review", "snapshot")
        # --high-risk keeps the whole panel, which a change this small would shrink to one seat.
        _, out, err = run_cli("review", "run", "--high-risk")
        self.assertIn("2 successful, 0 failed", out, err)
        self.assertEqual(err.count("note: instructions.reviewers: docs/missing.md: not found"), 1, err)

    def test_a_seat_own_unread_file_names_the_seat(self):
        data = config_mod.load(self.project).data
        seats = data["reviewers"]
        seats[0]["instructions"] = {"file": "docs/seat.md"}
        self.set_global(reviewers=seats)
        prompt, err = self.review()
        self.assertIn(
            "note: reviewer seat m1: instructions: docs/seat.md: not found; reviewing without it", err
        )
        self.assertNotIn("instructions.reviewers.m1", err)
        self.assertNotIn("## Project instructions", prompt)

    def test_project_instructions_warned(self):
        self.write(".dev-orchestra.yaml", "version: 1\ninstructions:\n  reviewers: %s\n" % self.MARKER)
        expected = (
            "instructions.reviewers: the project config (.dev-orchestra.yaml) sets reviewer instructions"
        )
        _, err = self.review()
        self.assertIn("warning: " + expected, err)
        out = run_cli("config", "validate")[1]
        self.assertIn("  - " + expected, out)
        loosened = json.loads(run_cli("doctor", "--fast", "--json")[1])["config"]["loosened"]
        self.assertTrue(any(line.startswith(expected) for line in loosened), loosened)


class DesignRunTest(DesignReviewCase):
    """``review run --design``: the design reviewers' text and a seat's own, and never the code reviewers'."""

    def setUp(self):
        super().setUp()
        self.prompts = capture_prompts(self)

    def test_design_and_seat_instructions_reach_the_prompt(self):
        self.write(
            ".dev-orchestra.yaml",
            "version: 1\n"
            "instructions:\n  reviewers: CODE-ROLE-TEXT\n  design_reviewers: DESIGN-ROLE-TEXT\n"
            "reviewers:\n"
            "  - id: m1\n    provider: mock\n    role: general\n    instructions: SEAT-TEXT\n"
            "  - id: m2\n    provider: mock\n    role: architecture\n",
        )
        self.write_plan()
        self.write_request()
        _, out, err = run_cli("review", "run", "--design")
        self.assertIn("2 successful, 0 failed", out, err)
        self.assertEqual(len(self.prompts), 2)
        for prompt in self.prompts:
            self.assertEqual(prompt.count("## Project instructions"), 1)
            self.assertIn("DESIGN-ROLE-TEXT", prompt)
            self.assertNotIn("CODE-ROLE-TEXT", prompt)
        self.assertEqual(sum("DESIGN-ROLE-TEXT\n\nSEAT-TEXT" in prompt for prompt in self.prompts), 1)
        self.assertEqual(sum("SEAT-TEXT" in prompt for prompt in self.prompts), 1)

    def test_unread_design_files_are_noted(self):
        """The design panel's file, once for both seats, and a seat's own, by its seat."""
        self.write(
            ".dev-orchestra.yaml",
            "version: 1\n"
            "instructions:\n  reviewers: CODE-ROLE-TEXT\n  design_reviewers:\n    file: docs/design.md\n"
            "reviewers:\n"
            "  - id: m1\n    provider: mock\n    role: general\n"
            "    instructions:\n      file: docs/m1.md\n"
            "  - id: m2\n    provider: mock\n    role: architecture\n",
        )
        self.write_plan()
        self.write_request()
        _, out, err = run_cli("review", "run", "--design")
        self.assertIn("2 successful, 0 failed", out, err)
        self.assertEqual(
            err.count("note: instructions.design_reviewers: docs/design.md: not found; reviewing without it"),
            1,
            err,
        )
        self.assertIn("note: design reviewer seat m1: instructions: docs/m1.md: not found", err)
        self.assertNotIn("note: instructions.reviewers", err)
        for prompt in self.prompts:
            self.assertNotIn("CODE-ROLE-TEXT", prompt)

    def test_a_plan_naming_a_watched_path_runs_under_auto(self):
        self.write("docs/ai/design.md", "Check the rollback.\n")
        self.write(
            ".dev-orchestra.yaml",
            "version: 1\ninstructions:\n  design_reviewers:\n    file: docs/ai/design.md\n",
        )
        loaded = config_mod.load(self.project, validate_result=False)
        for path in ("docs/ai/design.md", ".dev-orchestra.yml"):
            with self.subTest(path=path):
                self.write_plan("# Plan\n\n## Files to Modify\n\n- `%s`: reword it.\n" % path)
                decision = round_progress.design_decision(loaded, self.workspace)
                self.assertTrue(decision.run, decision.reason)
                self.assertIn(path, decision.reason)
                panel = loaded.design_reviewers()
                plan = round_progress.design_round_plan(loaded, self.workspace, panel, lineage="")
                self.assertIn(path, [token for token, _ in plan.high_risk])


@unittest.skipUnless(has_git(), "git is required")
class SnapshotTest(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "x = 1\n")
        self.write(".dev-orchestra.yaml", "version: 1\n")
        self.write("docs/a[b].md", "one\n")
        self.write("docs/ab.md", "one\n")
        self.commit_all("init")
        self.workspace = self.cli_workspace()

    def test_without_watched_nothing_is_recorded(self):
        self.write(".dev-orchestra.yaml", "version: 1\nreview: {}\n")
        meta = review_mod.create_snapshot(self.workspace)
        self.assertNotIn("instruction_changes", meta)

    def test_unchanged_watched_is_empty(self):
        self.write("app.py", "x = 2\n")
        meta = review_mod.create_snapshot(self.workspace, watched=[".dev-orchestra.yaml"])
        self.assertEqual(meta["instruction_changes"], [])

    def test_changed_project_file(self):
        self.write(".dev-orchestra.yaml", "version: 1\nreview: {}\n")
        meta = review_mod.create_snapshot(self.workspace, watched=[".dev-orchestra.yaml"])
        self.assertEqual(meta["instruction_changes"], [".dev-orchestra.yaml"])

    def test_committed_change_against_base(self):
        base = self.git("rev-parse", "HEAD").stdout.strip()
        self.write(".dev-orchestra.yaml", "version: 1\nreview: {}\n")
        self.commit_all("edit")
        meta = review_mod.create_snapshot(self.workspace, base, watched=[".dev-orchestra.yaml"])
        self.assertEqual(meta["instruction_changes"], [".dev-orchestra.yaml"])

    def test_untracked_file(self):
        self.write("docs/review.md", "new\n")
        meta = review_mod.create_snapshot(self.workspace, watched=[".dev-orchestra.yaml", "docs/review.md"])
        self.assertEqual(meta["instruction_changes"], ["docs/review.md"])

    def test_pathspec_is_literal(self):
        self.write("docs/ab.md", "two\n")
        meta = review_mod.create_snapshot(self.workspace, watched=["docs/a[b].md"])
        self.assertEqual(meta["instruction_changes"], [])
        self.write("docs/a[b].md", "two\n")
        meta = review_mod.create_snapshot(self.workspace, watched=["docs/a[b].md"])
        self.assertEqual(meta["instruction_changes"], ["docs/a[b].md"])

    def test_git_error_gives_none(self):
        changes = review_snapshot._instruction_changes(self.project, "0" * 40, [".dev-orchestra.yaml"])
        self.assertEqual(changes, [])
        self.assertEqual(review_snapshot._instruction_changes(self.project, "", [".dev-orchestra.yaml"]), [])

    def test_untracked_listing_error_gives_none(self):
        """The diff succeeds and the listing of untracked files fails: none, not half."""
        self.write(".dev-orchestra.yaml", "version: 1\nreview: {}\n")
        base = self.git("rev-parse", "HEAD").stdout.strip()
        real = ws.git_bytes

        def second_fails(args, *rest, **kwargs):
            if "ls-files" in args:
                return 1, b"", "fatal"
            return real(args, *rest, **kwargs)

        with mock.patch.object(ws, "git_bytes", side_effect=second_fails):
            changes = review_snapshot._instruction_changes(self.project, base, [".dev-orchestra.yaml"])
        self.assertEqual(changes, [])

    def test_deleted_watched_file(self):
        """Removing an instructions file changes what the reviewers are told."""
        self.write("app.py", "x = 2\n")
        os.remove(os.path.join(self.project, "docs", "ab.md"))
        meta = review_mod.create_snapshot(self.workspace, watched=[".dev-orchestra.yaml", "docs/ab.md"])
        self.assertEqual(meta["instruction_changes"], ["docs/ab.md"])

    def test_an_incremental_round_still_records_the_change(self):
        """Against the base, not the narrowed diff: the fix round stays high-risk."""
        watched = [".dev-orchestra.yaml"]
        self.write(".dev-orchestra.yaml", "version: 1\nreview: {}\n")
        self.write("app.py", "x = 2\n")
        first = review_mod.create_snapshot(self.workspace, watched=watched)
        # The first round reviewed, with a finding the fix answers: the next round narrows.
        ws.write_json(
            self.workspace.consolidated_json_path,
            {"snapshot": {"sha256": first["sha256"]}, "findings": [{"triage": "accepted"}]},
        )
        self.write("app.py", "x = 3\n")
        second = review_mod.create_snapshot(self.workspace, watched=watched)
        self.assertTrue(second["incremental_from"])
        self.assertEqual(second["instruction_changes"], [".dev-orchestra.yaml"])


@unittest.skipUnless(has_git(), "git is required")
class RiskTest(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.fake_clis()
        self.init_git_repo()
        self.write("app.py", "x = 1\n")
        self.write(".dev-orchestra.yaml", "version: 1\n")
        self.commit_all("init")
        self.workspace = self.cli_workspace()

    def loaded(self):
        return config_mod.load(self.project, validate_result=False)

    def test_watched_paths_are_high_risk_patterns(self):
        settings = instructions.risk_settings(self.loaded())
        self.assertIn(".dev-orchestra.yaml", settings["extra_high_risk_paths"])

    def test_instruction_change_makes_the_round_high_risk(self):
        loaded = self.loaded()
        meta = {"files": ["app.py"], "changed_paths": ["app.py"], "condition_paths": ["app.py"]}
        plain = round_progress.round_plan(loaded, self.workspace, meta, [reviewer("r1")])
        self.assertFalse([path for path, _ in plain.high_risk if path == ".dev-orchestra.yaml"])
        meta["instruction_changes"] = [".dev-orchestra.yaml"]
        plan = round_progress.round_plan(loaded, self.workspace, meta, [reviewer("r1")])
        self.assertIn(".dev-orchestra.yaml", [path for path, _ in plan.high_risk])

    def test_deleting_the_project_file_in_force_is_recorded(self):
        """With it gone the next name is in force, and the deleted one is still watched."""
        self.write(".dev-orchestra.yml", "version: 1\n")
        self.commit_all("second name")
        os.remove(os.path.join(self.project, ".dev-orchestra.yaml"))
        loaded = self.loaded()
        self.assertTrue(str(loaded.project_path).endswith(".dev-orchestra.yml"))
        meta = review_mod.create_snapshot(self.workspace, watched=instructions.watched_paths(loaded))
        self.assertEqual(meta["instruction_changes"], [".dev-orchestra.yaml"])

    def test_backslash_file_path_in_a_subdirectory(self):
        self.write("docs/ai/r.md", "one\n")
        project = "version: 1\ninstructions:\n  reviewers:\n    file: 'docs\\ai\\r.md'\n"
        self.write(".dev-orchestra.yaml", project)
        self.commit_all("instructions")
        loaded = self.loaded()
        self.assertIn("docs/ai/r.md", instructions.watched_paths(loaded))
        self.write("docs/ai/r.md", "two\n")
        meta = review_mod.create_snapshot(self.workspace, watched=instructions.watched_paths(loaded))
        self.assertEqual(meta["instruction_changes"], ["docs/ai/r.md"])

    def test_snapshot_records_what_round_plan_reads(self):
        loaded = self.loaded()
        self.write(".dev-orchestra.yaml", "version: 1\nreview: {}\n")
        meta = review_mod.create_snapshot(self.workspace, watched=instructions.watched_paths(loaded))
        self.assertEqual(meta["instruction_changes"], [".dev-orchestra.yaml"])
        plan = round_progress.round_plan(loaded, self.workspace, meta, [reviewer("r1")])
        self.assertIn(".dev-orchestra.yaml", [path for path, _ in plan.high_risk])


if __name__ == "__main__":
    unittest.main()
