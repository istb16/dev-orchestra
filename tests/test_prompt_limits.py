"""Capping reviewer output, and keeping the prompt that asks for it short.

Two savings that share a template, so they are tested together.

The output cap is the larger of the two. Output is billed at several times the
input rate, and a reviewer's output is billed again when it is consolidated and
again as the fixer's brief -- roughly three times over. An uncapped prompt
invites the padding that costs most: twenty low findings, a screenful of quoted
context each, a patch where a sentence would do.

The prompt diet is the smaller one, and its risk is the interesting part. A
shorter instruction is only a saving if the reviewer still does the same job,
and if the parser still recognises what comes back. So the tests below are
mostly about what must survive compression: every rule the reviewer is held to,
every field the parser reads, and the structure of the finding block.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git

from orchestrator import cli
from orchestrator import config as config_mod
from orchestrator import review as review_mod


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def reviewer(reviewer_id, role="general"):
    return {"id": reviewer_id, "provider": "mock", "role": role}


def finding(index, severity="medium"):
    """One finding, distinct enough from its siblings to survive dedup.

    Same file and a nearby line would be merged as a duplicate, which is the
    consolidation doing its job and would hide what these tests measure.
    """
    return (
        "## Finding\n"
        "- Severity: %s\n"
        "- File: module%d.py\n"
        "- Line: %d\n"
        "- Category: correctness\n"
        "- Problem: module %d mishandles its %s argument\n"
        "- Impact: callers of module %d see the wrong result\n"
        "- Evidence: return arg%d * 0\n"
        "- Fix: multiply by one in module %d\n"
        % (severity, index, index * 100, index, "abcdefgh"[index % 8], index, index, index)
    )


# --------------------------------------------------------------------------- limits


class TestTheLimitsBlock(unittest.TestCase):
    def test_the_cap_names_a_number_the_reviewer_can_act_on(self):
        block = review_mod.render_limits(6)
        self.assertIn("Max 6 findings", block)

    def test_over_the_cap_the_reviewer_is_asked_to_prioritise_not_to_stop(self):
        """A reviewer that hits the cap has found more than six problems. Being
        told to stop looking is the one instruction that could lose a bug."""
        block = review_mod.render_limits(6)
        self.assertIn("worst", block)
        self.assertIn("severity first", block)

    def test_zero_lifts_the_count_cap_and_keeps_every_other_rule(self):
        block = review_mod.render_limits(0)
        self.assertIn("No maximum", block)
        self.assertIn("Evidence", block)
        self.assertIn("NO_FINDINGS", block)
        self.assertIn("No preamble", block)

    def test_both_forms_open_by_asking_for_findings(self):
        """Only the capped form used to, and the one run observed returning a
        prose summary instead of finding blocks -- recorded `unparsed`, and so
        counted as a failed review -- was the uncapped one. One run is not a
        cause; an instruction weaker in one mode than the other is still worth
        levelling."""
        for cap in (0, 6):
            first = review_mod.render_limits(cap).splitlines()[1]
            self.assertIn("findings" if cap else "block", first, cap)

    def test_the_per_finding_caps_are_stated_in_lines(self):
        block = review_mod.render_limits()
        self.assertIn("Evidence: %d lines max" % review_mod.MAX_EVIDENCE_LINES, block)
        self.assertIn("Fix: %d lines max" % review_mod.MAX_FIX_LINES, block)

    def test_the_clean_review_escape_hatch_survives_the_cap(self):
        """`NO_FINDINGS` is how a clean review is distinguished from a broken
        one. A cap that made it unreachable would turn silence into failure."""
        self.assertIn("exactly NO_FINDINGS", review_mod.render_limits(1))


class TestWhatTheTemplateStillSays(IsolatedCase):
    """Compression may shorten a rule. It may not drop one."""

    def setUp(self):
        super().setUp()
        from orchestrator import workspace as ws

        self.workspace = ws.Workspace(self.project)
        self.workspace.ensure()

    def prompt(self, **kwargs):
        return review_mod.build_review_prompt(reviewer("r1"), self.workspace, "diff body", **kwargs)

    def test_the_reviewer_is_still_told_it_is_read_only(self):
        text = self.prompt()
        self.assertIn("Read-only", text)
        self.assertIn("Do not modify, create, or delete files", text)
        self.assertIn("network", text)

    def test_the_reviewer_is_still_told_to_judge_only_this_change(self):
        self.assertIn("Review only the change below", self.prompt())

    def test_the_reviewer_may_still_read_the_rest_of_the_repository(self):
        """Without this a reviewer judges a hunk with no surrounding code and
        reports things the file next door already handles."""
        self.assertIn("Read any file for context", self.prompt())

    def test_every_field_the_parser_reads_is_still_requested(self):
        text = self.prompt()
        for field in ("Severity", "File", "Line", "Category", "Problem", "Impact", "Evidence"):
            self.assertIn("- %s:" % field, text)
        self.assertIn("- Fix:", text)

    def test_the_finding_block_header_is_unchanged(self):
        """The parser splits on this heading. Shortening the prose around it is
        free; renaming it would cost a whole delegated run to a parse failure."""
        self.assertIn("## Finding", self.prompt())

    def test_the_limits_come_after_the_diff(self):
        """Last instruction read is the one best obeyed, and the diff sits
        between the task and the output rules."""
        text = self.prompt()
        self.assertLess(text.index("## Change under review"), text.index("Limits:"))

    def test_the_configured_cap_reaches_the_prompt(self):
        self.assertIn("Max 3 findings", self.prompt(max_findings=3))

    def test_a_custom_template_without_the_limits_placeholder_still_renders(self):
        text = review_mod.build_review_prompt(
            reviewer("r1"), self.workspace, "diff", template="{role}: {diff_section}"
        )
        self.assertIn("general: ", text)


class TestTheDiet(IsolatedCase):
    """The saving itself, guarded so it cannot quietly regress."""

    def setUp(self):
        super().setUp()
        from orchestrator import workspace as ws

        self.workspace = ws.Workspace(self.project)
        self.workspace.ensure()

    def test_the_fixed_part_of_the_prompt_stays_under_its_budget(self):
        """Every reviewer pays this, every round. The number is a ceiling with
        room to add a rule, not a measurement to keep in step with the text."""
        overhead = len(review_mod.build_review_prompt(reviewer("r1"), self.workspace, "")) - len("")
        self.assertLess(overhead, 1400, "the review prompt has grown back")

    def test_the_role_guidance_stays_terse(self):
        for role, guidance in review_mod.ROLE_GUIDANCE.items():
            self.assertLess(len(guidance), 320, role)

    def test_every_role_still_names_what_to_look_for(self):
        """Terse is not empty: a role whose guidance said nothing specific would
        turn a specialist reviewer into a second general one."""
        for role, guidance in review_mod.ROLE_GUIDANCE.items():
            self.assertGreater(len(guidance.split(",")), 4, role)

    def test_an_unknown_role_still_gets_a_framing(self):
        text = review_mod.build_review_prompt(reviewer("r1", role="accessibility"), self.workspace, "diff")
        self.assertIn("accessibility specialist", text)


# --------------------------------------------------------------------------- parsing


class TestTheShorterLabelStillParses(unittest.TestCase):
    """`Recommended fix` became `Fix`, which the parser already accepted."""

    def test_fix_is_read_as_the_recommended_fix(self):
        parsed = review_mod.parse_findings(finding(2), "r1")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["recommended_fix"], "multiply by one in module 2")

    def test_the_old_label_is_still_accepted(self):
        """A reviewer trained on the previous template, or a custom template
        somebody kept, must not silently lose its fix text."""
        body = finding(2).replace("- Fix:", "- Recommended fix:")
        self.assertEqual(
            review_mod.parse_findings(body, "r1")[0]["recommended_fix"], "multiply by one in module 2"
        )


class TestTheFixBrief(unittest.TestCase):
    """Billed twice: once as reviewer output, again as the fixer's input."""

    def consolidated(self, **overrides):
        item = {
            "id": "F1",
            "severity": "high",
            "file": "app.py",
            "line": "2",
            "category": "correctness",
            "problem": "off by one",
            "impact": "wrong total",
            "evidence": "i <= n",
            "recommended_fix": "use <",
            "reported_by": ["r1", "r2"],
            "triage": "accepted",
        }
        item.update(overrides)
        return {"findings": [item]}

    def test_an_empty_field_is_omitted_rather_than_sent_as_a_bare_label(self):
        brief = review_mod.render_fix_brief(self.consolidated(impact="", evidence="   "))
        self.assertNotIn("Impact", brief)
        self.assertNotIn("Evidence", brief)
        self.assertIn("- Problem: off by one", brief)

    def test_who_reported_it_is_not_sent_to_the_fixer(self):
        """The fix is the same whoever noticed, and the finding reached the
        brief only by being accepted during triage."""
        self.assertNotIn("Reported by", review_mod.render_fix_brief(self.consolidated()))

    def test_the_fixer_still_gets_the_location_and_the_fix(self):
        brief = review_mod.render_fix_brief(self.consolidated())
        self.assertIn("app.py:2", brief)
        self.assertIn("- Fix: use <", brief)
        self.assertIn("HIGH", brief)

    def test_nothing_accepted_says_so_in_one_line(self):
        self.assertIn("Nothing to fix", review_mod.render_fix_brief({"findings": []}))


# --------------------------------------------------------------------------- config


class TestConfiguringTheCap(IsolatedCase):
    def test_the_config_ships_the_cap_unset_so_the_level_decides(self):
        """Unset is not uncapped: the level supplies one. The effective
        default is still 6, by a different route."""
        settings = config_mod.default_config()["review"]
        self.assertIsNone(settings["max_findings"])
        self.assertEqual(review_mod.DEFAULT_MAX_FINDINGS, 6)

    def check(self, value):
        data = config_mod.default_config()
        data["review"]["max_findings"] = value
        return config_mod.validate(data)

    def test_zero_is_a_valid_setting(self):
        self.assertEqual(self.check(0), [])

    def test_null_is_a_valid_setting(self):
        """Writing ``max_findings:`` with nothing after it is how a hand-edited
        config says "whatever you were going to do"."""
        self.assertEqual(self.check(None), [])

    def test_a_negative_cap_is_rejected(self):
        self.assertTrue(any("max_findings" in p for p in self.check(-1)), self.check(-1))

    def test_a_boolean_is_rejected(self):
        """``True`` is an ``int`` in Python, and would cap every reviewer at one
        finding while reading like a feature flag in the config file."""
        self.assertTrue(any("max_findings" in p for p in self.check(True)), self.check(True))

    def test_a_string_is_rejected(self):
        self.assertTrue(any("max_findings" in p for p in self.check("6")), self.check("6"))


@unittest.skipUnless(has_git(), "git is required")
class TestTheCapInTheRunningPipeline(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write("app.py", "def add(a, b):\n    return a - b\n")

        self.mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(self.mock_dir)
        self.respond("".join(finding(i) for i in range(1, 4)))
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = self.mock_dir

        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")

    def respond(self, body):
        with open(os.path.join(self.mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(body)

    def set_cap(self, value):
        data = config_mod.load(self.project).data
        data.setdefault("review", {})["max_findings"] = value
        config_mod.write_config_file(config_mod.global_config_path(), data)

    def test_the_configured_cap_is_what_the_reviewer_is_asked_for(self):
        """Asserted through the mock's fail marker, which matches on the prompt
        the provider actually received rather than one rebuilt by the test."""
        self.set_cap(2)
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "Max 2 findings"
        run_cli("review", "snapshot")
        _, out, _ = run_cli("review", "run")
        self.assertIn("0 successful, 1 failed", out)

    def test_the_default_cap_is_not_silently_something_else(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "Max %d findings" % review_mod.DEFAULT_MAX_FINDINGS
        run_cli("review", "snapshot")
        _, out, _ = run_cli("review", "run")
        self.assertIn("0 successful, 1 failed", out)

    def test_a_reviewer_over_the_cap_keeps_every_finding(self):
        """The reviewer ignored the instruction. Dropping its extra findings to
        enforce the number would be this layer deciding which bug to discard."""
        self.set_cap(2)
        run_cli("review", "snapshot")
        code, _, _ = run_cli("review", "run")
        self.assertEqual(code, 0)
        data = json.loads(run_cli("review", "show", "--json")[1])
        self.assertEqual(data["counts"]["findings_total"], 3)

    def test_a_reviewer_over_the_cap_is_reported(self):
        self.set_cap(2)
        run_cli("review", "snapshot")
        _, _, err = run_cli("review", "run")
        self.assertIn("against a cap of 2", err)

    def test_a_reviewer_within_the_cap_says_nothing(self):
        self.set_cap(6)
        run_cli("review", "snapshot")
        _, _, err = run_cli("review", "run")
        self.assertNotIn("cap of", err)

    def test_no_cap_never_reports(self):
        self.set_cap(0)
        run_cli("review", "snapshot")
        _, _, err = run_cli("review", "run")
        self.assertNotIn("cap of", err)

    def test_an_unset_cap_falls_back_to_the_default(self):
        """``max_findings:`` with nothing after it passes validation and arrives
        as None, which must not reach the ``>`` that reports an overshoot."""
        self.set_cap(None)
        run_cli("review", "snapshot")
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertNotIn("cap of", err)


if __name__ == "__main__":
    unittest.main()
