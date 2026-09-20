"""Review snapshotting, fan-out, parsing, deduplication and triage."""

from __future__ import annotations

import os
import unittest

from helpers import IsolatedCase, has_git

from orchestrator import config as config_mod
from orchestrator import review as review_mod
from orchestrator import workspace as ws

FINDING_A = """## Finding
- Severity: high
- File: app/models/user.rb
- Line: 42
- Category: correctness
- Problem: the nil guard is missing before calling profile.name
- Impact: NoMethodError for users without a profile
- Evidence: user.profile.name
- Recommended fix: use safe navigation or validate presence
"""

FINDING_A_REWORDED = """## Finding
- Severity: critical
- File: app/models/user.rb
- Line: 43
- Category: correctness
- Problem: the nil guard is missing before calling profile.name
- Impact: raises NoMethodError when a user has no profile record
- Evidence: user.profile.name
- Recommended fix: use safe navigation or validate presence of the profile
"""

FINDING_B = """## Finding
- Severity: medium
- File: app/controllers/orders_controller.rb
- Line: 10
- Category: performance
- Problem: orders are loaded without including line items
- Impact: N+1 queries on the index page
- Evidence: Order.all.each
- Recommended fix: add includes(:line_items)
"""


def reviewer(reviewer_id, provider="mock", role="general"):
    return config_mod.make_reviewer(reviewer_id, provider, "small", role)


class TestParsing(IsolatedCase):
    def test_parses_all_fields(self):
        findings = review_mod.parse_findings(FINDING_A, "r1")
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["file"], "app/models/user.rb")
        self.assertEqual(finding["line"], "42")
        self.assertEqual(finding["category"], "correctness")
        self.assertIn("nil guard", finding["problem"])
        self.assertEqual(finding["reviewer"], "r1")

    def test_no_findings_sentinel(self):
        self.assertEqual(review_mod.parse_findings("NO_FINDINGS\n", "r1"), [])

    def test_multiple_findings(self):
        self.assertEqual(len(review_mod.parse_findings(FINDING_A + "\n" + FINDING_B, "r1")), 2)

    def test_bold_markdown_variant_is_accepted(self):
        text = (
            "### Finding\n**Severity:** low\n**File:** a.py\n**Line:** 3\n"
            "**Category:** style\n**Problem:** naming\n**Recommendation:** rename it\n"
        )
        findings = review_mod.parse_findings(text, "r1")
        self.assertEqual(findings[0]["severity"], "low")
        self.assertEqual(findings[0]["recommended_fix"], "rename it")

    def test_unknown_severity_defaults_to_medium(self):
        text = FINDING_A.replace("- Severity: high", "- Severity: spicy")
        self.assertEqual(review_mod.parse_findings(text, "r1")[0]["severity"], "medium")

    def test_nit_maps_to_low(self):
        text = FINDING_A.replace("- Severity: high", "- Severity: nit")
        self.assertEqual(review_mod.parse_findings(text, "r1")[0]["severity"], "low")

    def test_prose_without_findings_yields_nothing(self):
        self.assertEqual(review_mod.parse_findings("Looks good to me overall.", "r1"), [])

    def test_report_header_is_not_parsed_as_a_finding(self):
        report = "# Review\n\n- Reviewer: r1\n- Provider: mock\n\n---\n\nNO_FINDINGS\n"
        self.assertEqual(review_mod.parse_findings(report, "r1"), [])

    def test_paths_are_normalised(self):
        text = FINDING_A.replace("- File: app/models/user.rb", "- File: `./app\\models\\user.rb`")
        self.assertEqual(review_mod.parse_findings(text, "r1")[0]["file"], "app/models/user.rb")


class TestConsolidation(IsolatedCase):
    def test_same_issue_from_two_reviewers_is_merged(self):
        findings = review_mod.parse_findings(FINDING_A, "r1") + review_mod.parse_findings(
            FINDING_A_REWORDED, "r2"
        )
        merged = review_mod.consolidate_findings(findings)
        self.assertEqual(len(merged), 1)
        self.assertEqual(sorted(merged[0]["reported_by"]), ["r1", "r2"])
        self.assertEqual(merged[0]["duplicate_count"], 2)

    def test_merge_keeps_the_highest_severity(self):
        findings = review_mod.parse_findings(FINDING_A, "r1") + review_mod.parse_findings(
            FINDING_A_REWORDED, "r2"
        )
        self.assertEqual(review_mod.consolidate_findings(findings)[0]["severity"], "critical")

    def test_different_issues_are_kept_apart(self):
        findings = review_mod.parse_findings(FINDING_A, "r1") + review_mod.parse_findings(FINDING_B, "r2")
        merged = review_mod.consolidate_findings(findings)
        self.assertEqual(len(merged), 2)
        self.assertEqual([f["id"] for f in merged], ["F1", "F2"])

    def test_same_text_in_a_different_file_is_not_a_duplicate(self):
        other = FINDING_A.replace("app/models/user.rb", "app/models/account.rb")
        findings = review_mod.parse_findings(FINDING_A, "r1") + review_mod.parse_findings(other, "r2")
        self.assertEqual(len(review_mod.consolidate_findings(findings)), 2)

    def test_distant_lines_are_not_duplicates(self):
        other = FINDING_A.replace("- Line: 42", "- Line: 900")
        findings = review_mod.parse_findings(FINDING_A, "r1") + review_mod.parse_findings(other, "r2")
        self.assertEqual(len(review_mod.consolidate_findings(findings)), 2)

    def test_ordering_is_by_severity(self):
        findings = review_mod.parse_findings(FINDING_B, "r1") + review_mod.parse_findings(FINDING_A, "r2")
        merged = review_mod.consolidate_findings(findings)
        self.assertEqual(merged[0]["severity"], "high")
        self.assertEqual(merged[0]["id"], "F1")


class TestTriage(IsolatedCase):
    def _data(self):
        findings = review_mod.parse_findings(FINDING_A + FINDING_B, "r1")
        return {"findings": review_mod.consolidate_findings(findings)}

    def test_default_status_is_needs_triage(self):
        self.assertEqual(self._data()["findings"][0]["triage"], "needs-triage")

    def test_accepting_a_finding(self):
        data = self._data()
        review_mod.set_triage(data, "F1", "accepted", "confirmed by hand")
        accepted = review_mod.accepted_findings(data)
        self.assertEqual([f["id"] for f in accepted], ["F1"])

    def test_rejected_findings_are_excluded_from_the_fix_brief(self):
        data = self._data()
        review_mod.set_triage(data, "F1", "rejected", "false positive")
        self.assertIn("Nothing to fix", review_mod.render_fix_brief(data))

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(review_mod.ReviewError):
            review_mod.set_triage(self._data(), "F1", "maybe")

    def test_unknown_finding_id_is_rejected(self):
        with self.assertRaises(review_mod.ReviewError):
            review_mod.set_triage(self._data(), "F99", "accepted")

    def test_blocking_ignores_rejected_and_low_severity(self):
        data = self._data()
        review_mod.set_triage(data, "F1", "rejected")
        self.assertEqual(review_mod.unresolved_blocking(data), [])

    def test_blocking_includes_untriaged_high_severity(self):
        self.assertEqual([f["id"] for f in review_mod.unresolved_blocking(self._data())], ["F1"])

    def test_the_brief_heading_can_ask_for_a_revision_instead_of_a_fix(self):
        data = self._data()
        review_mod.set_triage(data, "F1", "accepted")
        brief = review_mod.render_fix_brief(data, "Revise the plan to address these accepted findings")
        self.assertIn("# Revise the plan to address these accepted findings", brief)
        self.assertNotIn("Fix these accepted findings", brief)
        # The items below the heading are the same either way.
        self.assertIn("F1", brief)


class TestDuplicateCandidates(IsolatedCase):
    """Cross-model duplicates are suggested, never silently merged.

    Prose similarity was measured against real two-provider output and does not
    separate true duplicates from unrelated findings, so the signal here is the
    code each finding quotes.
    """

    def _finding(self, fid, reviewer, problem, evidence, file="cart.py", category="correctness"):
        return {
            "id": fid,
            "file": file,
            "line": "2",
            "category": category,
            "problem": problem,
            "impact": "",
            "evidence": evidence,
            "recommended_fix": "",
            "reported_by": [reviewer],
        }

    def test_two_reviewers_quoting_the_same_code_are_paired(self):
        findings = [
            self._finding("F1", "claude", "the split is unguarded", '`percent = int(code.split("-")[1])`'),
            self._finding("F2", "codex", "coupon parsing assumes a component exists", '`code.split("-")[1]`'),
        ]
        pairs = review_mod.duplicate_candidates(findings)
        self.assertEqual([p["ids"] for p in pairs], [["F1", "F2"]])
        self.assertIn("code.split", pairs[0]["shared_code"])

    def test_wording_alone_never_pairs_findings(self):
        # Same words, no shared code: not enough to suggest a duplicate.
        findings = [
            self._finding("F1", "claude", "the discount is wrong", ""),
            self._finding("F2", "codex", "the discount is wrong", ""),
        ]
        self.assertEqual(review_mod.duplicate_candidates(findings), [])

    def test_findings_from_one_reviewer_are_never_paired(self):
        findings = [
            self._finding("F1", "claude", "unguarded split", '`code.split("-")`'),
            self._finding("F2", "claude", "prefix ignored", '`code.split("-")`'),
        ]
        self.assertEqual(review_mod.duplicate_candidates(findings), [])

    def test_different_files_are_never_paired(self):
        findings = [
            self._finding("F1", "claude", "a", "`code.split(x)`", file="a.py"),
            self._finding("F2", "codex", "b", "`code.split(x)`", file="b.py"),
        ]
        self.assertEqual(review_mod.duplicate_candidates(findings), [])

    def test_pairs_are_ranked_by_how_rare_the_shared_code_is(self):
        findings = [
            self._finding("F1", "claude", "a", "`cart.total` and `rare.token`"),
            self._finding("F2", "claude", "b", "`cart.total`"),
            self._finding("F3", "codex", "c", "`cart.total`"),
            self._finding("F4", "codex", "d", "`rare.token`"),
        ]
        pairs = review_mod.duplicate_candidates(findings)
        # rare.token is shared by two findings, cart.total by three.
        self.assertEqual(pairs[0]["ids"], ["F1", "F4"])
        self.assertLess(pairs[0]["specificity"], pairs[-1]["specificity"])

    def test_consolidation_reports_candidates_without_merging_them(self):
        findings = [
            dict(
                review_mod.parse_findings(FINDING_A, "r1")[0],
                evidence="`user.profile.name`",
                problem="the nil guard is missing",
            ),
            dict(
                review_mod.parse_findings(FINDING_A, "r2")[0],
                evidence="`user.profile.name`",
                problem="a completely different wording for the same defect",
            ),
        ]
        data = review_mod.build_consolidation(ws.Workspace(self.tmp), [], findings)
        self.assertEqual(data["counts"]["findings_total"], 2)
        self.assertEqual(data["counts"]["duplicate_candidates"], 1)
        self.assertEqual(data["findings"][0]["possible_duplicates"], ["F2"])
        rendered = review_mod.render_consolidation(data)
        self.assertIn("Possible duplicates", rendered)
        self.assertIn("F1 ~ F2", rendered)

    def test_no_candidates_means_no_section(self):
        findings = review_mod.parse_findings(FINDING_A, "r1")
        data = review_mod.build_consolidation(ws.Workspace(self.tmp), [], findings)
        self.assertEqual(data["duplicate_candidates"], [])
        self.assertNotIn("Possible duplicates", review_mod.render_consolidation(data))


@unittest.skipUnless(has_git(), "git is required")
class TestSnapshot(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.workspace = self.cli_workspace()

    def test_snapshot_captures_uncommitted_changes(self):
        self.write("app.py", "def add(a, b):\n    return a - b\n")
        meta = review_mod.create_snapshot(self.workspace)
        self.assertIn("app.py", meta["files"])
        self.assertFalse(meta["empty"])
        self.assertIn("return a - b", ws.read_text(self.workspace.snapshot_path))

    def test_snapshot_includes_untracked_files(self):
        self.write("new_module.py", "print('hi')\n")
        meta = review_mod.create_snapshot(self.workspace)
        self.assertIn("new_module.py", meta["untracked_included"])

    def test_orchestrator_config_is_not_part_of_the_snapshot(self):
        # The setup wizard writes this file; it is not the change under review.
        self.write(".dev-orchestra.yaml", "version: 1\n")
        self.write("app.py", "def add(a, b):\n    return a * b\n")
        meta = review_mod.create_snapshot(self.workspace)
        self.assertNotIn(".dev-orchestra.yaml", meta["untracked_included"])
        self.assertNotIn(".dev-orchestra.yaml", meta["files"])
        self.assertIn("app.py", meta["files"])

    def test_workspace_artifacts_are_not_part_of_the_snapshot(self):
        self.write("app.py", "def add(a, b):\n    return a * b\n")
        meta = review_mod.create_snapshot(self.workspace)
        self.assertNotIn(".ai/state.json", meta["files"])

    def test_snapshot_against_a_base_revision(self):
        self.write("app.py", "def add(a, b):\n    return a * b\n")
        self.commit_all("second")
        meta = review_mod.create_snapshot(self.workspace, base="HEAD~1")
        self.assertIn("app.py", meta["files"])

    def test_empty_snapshot_is_flagged(self):
        meta = review_mod.create_snapshot(self.workspace)
        self.assertTrue(meta["empty"])

    def test_snapshot_is_stable_and_hashed(self):
        self.write("app.py", "def add(a, b):\n    return a / b\n")
        first = review_mod.create_snapshot(self.workspace)
        second = review_mod.create_snapshot(self.workspace)
        self.assertEqual(first["sha256"], second["sha256"])

    def test_non_git_directory_is_reported_clearly(self):
        outside = os.path.join(self.tmp, "plain")
        os.makedirs(outside)
        with self.assertRaises(review_mod.ReviewError) as ctx:
            review_mod.create_snapshot(ws.Workspace(outside).ensure())
        self.assertIn("not a git repository", str(ctx.exception))


@unittest.skipUnless(has_git(), "git is required")
class TestFanOut(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write("app.py", "def add(a, b):\n    return a - b\n")
        self.workspace = self.cli_workspace()
        review_mod.create_snapshot(self.workspace)
        self.mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(self.mock_dir)
        with open(os.path.join(self.mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING_A)
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = self.mock_dir

    def test_every_reviewer_writes_its_own_report(self):
        runs = review_mod.run_reviews([reviewer("r1"), reviewer("r2", role="security")], self.workspace)
        self.assertEqual([run.status for run in runs], ["ok", "ok"])
        for reviewer_id in ("r1", "r2"):
            self.assertTrue(os.path.isfile(self.workspace.reviewer_report_path(reviewer_id)))

    def test_reviewers_do_not_see_each_other(self):
        review_mod.run_reviews([reviewer("r1"), reviewer("r2")], self.workspace)
        report = ws.read_text(self.workspace.reviewer_report_path("r2"))
        self.assertNotIn("r1", report.split("---", 1)[1])

    def test_one_failure_does_not_fail_the_batch(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "Reviewer: r2 |"
        runs = review_mod.run_reviews([reviewer("r1"), reviewer("r2"), reviewer("r3")], self.workspace)
        ok, failed, partial = review_mod.summarise_runs(runs)
        self.assertEqual((ok, failed, partial), (2, 1, 0))
        self.assertEqual([r.reviewer["id"] for r in runs if r.status == "failed"], ["r2"])

    def test_a_reviewer_with_an_unknown_provider_fails_alone(self):
        broken = {"id": "bad", "provider": "nonexistent", "role": "general"}
        runs = review_mod.run_reviews([reviewer("r1"), broken], self.workspace)
        self.assertEqual(review_mod.summarise_runs(runs), (1, 1, 0))
        failed = next(run for run in runs if run.status == "failed")
        # Nothing was started, so there is nothing to charge and nothing to
        # account for. This is the one shape of failure that is genuinely free.
        self.assertEqual(failed.duration, 0.0)
        self.assertFalse(failed.invoked)

    def test_a_reviewer_whose_adapter_cannot_read_the_output_still_has_a_duration(self):
        """The other shape: the CLI ran, and only the reading of it failed."""
        from test_providers import BrokenReaderProvider

        original = review_mod.get_provider
        review_mod.get_provider = lambda name: BrokenReaderProvider()
        self.addCleanup(setattr, review_mod, "get_provider", original)
        run = review_mod.run_reviews([reviewer("r1")], self.workspace)[0]
        self.assertEqual(run.status, "failed")
        self.assertGreater(run.duration, 0)
        self.assertTrue(run.invoked)

    def test_zero_reviewers_is_a_no_op(self):
        self.assertEqual(review_mod.run_reviews([], self.workspace), [])

    def test_empty_snapshot_refuses_to_run(self):
        ws.write_text(self.workspace.snapshot_path, "")
        with self.assertRaises(review_mod.ReviewError):
            review_mod.run_reviews([reviewer("r1")], self.workspace)

    def test_sequential_matches_parallel(self):
        parallel = review_mod.run_reviews([reviewer("r1"), reviewer("r2")], self.workspace, parallel=True)
        sequential = review_mod.run_reviews([reviewer("r1"), reviewer("r2")], self.workspace, parallel=False)
        self.assertEqual([r.status for r in parallel], [r.status for r in sequential])

    def test_findings_from_reports_are_deduplicated(self):
        runs = review_mod.run_reviews([reviewer("r1"), reviewer("r2")], self.workspace)
        findings = review_mod.collect_reports(self.workspace, ["r1", "r2"])
        self.assertEqual(len(findings), 2)
        data = review_mod.build_consolidation(self.workspace, [r.to_dict() for r in runs], findings)
        self.assertEqual(data["counts"]["findings_total"], 1)
        self.assertEqual(data["counts"]["duplicates_merged"], 1)
        self.assertEqual(data["counts"]["reviewers_ok"], 2)

    def test_triage_survives_reconsolidation(self):
        runs = review_mod.run_reviews([reviewer("r1")], self.workspace)
        findings = review_mod.collect_reports(self.workspace, ["r1"])
        data = review_mod.build_consolidation(self.workspace, [r.to_dict() for r in runs], findings)
        review_mod.set_triage(data, "F1", "accepted", "verified")
        ws.write_json(self.workspace.consolidated_json_path, data)

        again = review_mod.build_consolidation(self.workspace, [], findings, iteration=2)
        self.assertEqual(again["findings"][0]["triage"], "accepted")

    def test_review_prompt_carries_role_guidance_and_read_only_rules(self):
        prompt = review_mod.build_review_prompt(
            reviewer("r1", role="security"), self.workspace, "diff --git a b"
        ).text
        self.assertIn("security", prompt)
        self.assertIn("injection", prompt)
        self.assertIn("do not modify", prompt.lower())
        self.assertIn("NO_FINDINGS", prompt)

    def test_custom_role_still_gets_a_usable_prompt(self):
        built = review_mod.build_review_prompt(reviewer("r1", role="accessibility"), self.workspace, "diff")
        self.assertIn("accessibility specialist", built.text)

    def test_huge_diffs_are_referenced_by_path_instead_of_inlined(self):
        big = "x" * 4_001
        built = review_mod.build_review_prompt(reviewer("r1"), self.workspace, big, inline_chars=4_000)
        self.assertIn("review-target.diff", built.text)
        self.assertNotIn(big, built.text)
        self.assertEqual(built.delivery, "file")
        self.assertEqual(built.change_chars, len(big))

    def test_a_diff_exactly_on_the_limit_is_still_inlined(self):
        """The boundary is inclusive, and it is the only place the two
        deliveries meet -- an off-by-one here turns a clean round partial."""
        exact = "x" * 4_000
        built = review_mod.build_review_prompt(reviewer("r1"), self.workspace, exact, inline_chars=4_000)
        self.assertEqual(built.delivery, "inline")
        self.assertIn(exact, built.text)


@unittest.skipUnless(has_git(), "git is required")
class TestCoverageOfTheChangeBody(IsolatedCase):
    """A round whose change body went over as a file is never clean.

    Nothing in a reviewer's output tells the two deliveries apart. A reviewer
    handed a path reads what its own paging tool gives it -- Claude Code's
    ``Read`` stops at 2,000 lines by default -- and can answer NO_FINDINGS
    having seen a fifth of the change, which is indistinguishable from a
    genuinely clean review. So the verdict is taken from the one fact this
    tool holds with certainty: whether the body went into the prompt.
    """

    #: The limit these tests configure. Any number will do -- the limit is a
    #: setting now, so the only thing under test is the comparison against it,
    #: and a small one keeps the fixtures small.
    LIMIT = 4_000

    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write("app.py", "def add(a, b):\n    return a - b\n")
        self.workspace = self.cli_workspace()
        review_mod.create_snapshot(self.workspace)
        self.mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(self.mock_dir)
        with open(os.path.join(self.mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING_A)
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = self.mock_dir

    def oversize(self):
        """Put a change too large to inline where the round will read it.

        The mock provider never reads a prompt, so the snapshot's size is the
        whole of what decides the round -- the same handle
        ``test_empty_snapshot_refuses_to_run`` uses from the other end.
        """
        ws.write_text(self.workspace.snapshot_path, "x" * (self.LIMIT + 1))

    def run_one(self, reviewers=None, **kwargs):
        """One round at this class's configured limit.

        Passed explicitly rather than left to the default: every one of these
        tests is about a body measured against a limit, and reading the limit
        from the shipped default would make them a test of that number too.
        """
        kwargs.setdefault("inline_chars", self.LIMIT)
        return review_mod.run_reviews(reviewers or [reviewer("r1")], self.workspace, **kwargs)[0]

    def prompt_for(self, body):
        """One reviewer's prompt for this body, at this class's limit."""
        built = review_mod.build_review_prompt(reviewer("r1"), self.workspace, body, inline_chars=self.LIMIT)
        return built.text

    def answers(self, text):
        """What every reviewer replies. The mock reads its directory first, so
        that has to go before the inline answer is seen."""
        os.environ.pop("DEV_ORCHESTRA_MOCK_DIR", None)
        os.environ["DEV_ORCHESTRA_MOCK_RESPONSE"] = text

    def test_the_limit_is_inclusive_on_both_sides(self):
        self.assertEqual(review_mod.prompt_delivery("x" * self.LIMIT, self.LIMIT), "inline")
        self.assertEqual(review_mod.prompt_delivery("x" * (self.LIMIT + 1), self.LIMIT), "file")

    def test_the_limit_left_unset_is_the_shipped_default(self):
        """Not a second copy of the number: the one in `default_config`, read
        from there, so the two cannot drift apart."""
        default = config_mod.default_config()["review"]["context"]["inline_chars"]
        self.assertEqual(review_mod.default_inline_chars(), default)
        self.assertEqual(review_mod.prompt_delivery("x" * default), "inline")
        self.assertEqual(review_mod.prompt_delivery("x" * (default + 1)), "file")

    def test_the_default_now_inlines_a_body_that_used_to_go_over_as_a_file(self):
        """The behaviour change this stage ships. 350,000 chars is over the
        120,000 that used to decide it and under the 400,000 that does now, so
        it is the band that changed -- and it now goes into the prompt."""
        body = "x" * 350_000
        built = review_mod.build_review_prompt(reviewer("r1"), self.workspace, body)
        self.assertEqual(built.delivery, "inline")
        self.assertIn(body, built.text)

    def test_the_old_limit_is_still_reachable_by_configuring_it(self):
        """`inline_chars: 120000` restores exactly what shipped before."""
        body = "x" * 350_000
        built = review_mod.build_review_prompt(reviewer("r1"), self.workspace, body, inline_chars=120_000)
        self.assertEqual(built.delivery, "file")
        self.assertNotIn(body, built.text)

    def test_an_inline_limit_above_the_budget_still_inlines(self):
        """`inline_chars > max_chars` is allowed, and means what it says: a
        body only ever goes over as a file on a round somebody forced."""
        body = "x" * 500_000
        built = review_mod.build_review_prompt(reviewer("r1"), self.workspace, body, inline_chars=900_000)
        self.assertEqual(built.delivery, "inline")
        self.assertIn(body, built.text)

    def test_the_inlined_prompt_is_byte_for_byte_what_it_always_was(self):
        """The check that this change did not quietly alter what every
        reviewer has been reading for every round so far.

        Raising the default inline limit changed what is *sent* for a body
        between 120,000 and 400,000 chars, and nothing else: at or under
        120,000 -- which is every round this repository has ever recorded --
        the prompt is the same bytes it was.

        Reassembled from the template rather than compared against a golden
        copy: a deliberate edit to the template is still one edit, while a
        stray change to the delivery branch -- a stripped newline, a note
        appended in the wrong place -- fails here.
        """
        diff_text = ws.read_text(self.workspace.snapshot_path)
        self.assertLessEqual(len(diff_text), 120_000)
        built = review_mod.build_review_prompt(reviewer("r1"), self.workspace, diff_text)
        expected = review_mod.REVIEW_PROMPT_TEMPLATE.format(
            reviewer_id="r1",
            role="general",
            role_guidance=review_mod.ROLE_GUIDANCE["general"],
            root=self.workspace.root,
            diff_section="```diff\n%s\n```" % diff_text.rstrip(),
            limits=review_mod.render_limits(review_mod.DEFAULT_MAX_FINDINGS),
        )
        self.assertEqual(built.text, expected)
        self.assertEqual(built.delivery, "inline")
        self.assertEqual(built.change_chars, len(diff_text))

    def test_a_handover_states_the_fact_and_asks_for_nothing_back(self):
        """The reviewer is told what the round is recorded as; it is never
        asked to declare it. A declaration cannot be tested for the case where
        it was not made, and the templates end with "Findings or NO_FINDINGS
        only", which overrides anything asked before it."""
        big = "x" * (self.LIMIT + 1)
        text = self.prompt_for(big)
        self.assertIn("coverage-unverified", text)
        self.assertIn("4,001 chars", text)
        self.assertIn("a paging tool needs more than one call", text)
        self.assertNotIn("PARTIAL_REVIEW", text)

    def test_the_handover_note_quotes_the_configured_limit_not_a_constant(self):
        """The number that decided it is the only one worth printing: a reader
        told "too large to inline" and nothing else cannot tell a large change
        from a low setting."""
        big = "x" * (self.LIMIT + 1)
        text = self.prompt_for(big)
        self.assertIn("review.context.inline_chars (4,000)", text)
        self.assertNotIn("120,000", text)

    def test_findings_are_kept_but_the_round_is_not_clean(self):
        self.oversize()
        run = self.run_one()
        self.assertEqual(run.status, "partial")
        self.assertEqual(run.findings, 1)
        self.assertEqual(run.delivery, "file")
        self.assertEqual(run.change_chars, self.LIMIT + 1)
        self.assertIn("coverage unverified", run.error)

    def test_the_partial_error_quotes_the_configured_limit(self):
        self.oversize()
        run = self.run_one()
        self.assertIn("review.context.inline_chars, 4,000", run.error)
        self.assertNotIn("120,000", run.error)

    def test_no_findings_over_a_handover_is_not_a_clean_review(self):
        """The hole this whole stage exists to close: a reviewer that saw a
        fifth of the change and had nothing to say used to be recorded ok."""
        self.answers("NO_FINDINGS\n")
        self.oversize()
        run = self.run_one()
        self.assertEqual(run.status, "partial")
        self.assertEqual(run.findings, 0)

    def test_an_unreadable_report_still_wins(self):
        """Both are not-ok. "The report cannot be read" is the more specific
        fact, and the one that says the delegated run was wasted."""
        self.answers("Looks good to me.\n")
        self.oversize()
        run = self.run_one()
        self.assertEqual(run.status, "unparsed")
        self.assertEqual(run.delivery, "file")
        self.assertIn("could not be parsed", run.error)

    def test_an_inlined_round_is_recorded_exactly_as_before(self):
        run = self.run_one()
        self.assertEqual(run.status, "ok")
        self.assertEqual(run.delivery, "inline")
        self.assertEqual(run.error, "")

    def test_a_run_that_never_reached_a_prompt_records_no_delivery(self):
        """An unknown provider falls over before the prompt is built, so there
        is no delivery to claim -- and a blank one is left out of the round's
        coverage rather than read as either answer."""
        broken = {"id": "bad", "provider": "nonexistent", "role": "general"}
        run = self.run_one([broken])
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.delivery, "")

    def test_the_run_dict_carries_both(self):
        self.oversize()
        entry = self.run_one().to_dict()
        self.assertEqual(entry["delivery"], "file")
        self.assertEqual(entry["change_chars"], self.LIMIT + 1)
        self.assertEqual(entry["status"], "partial")

    def test_the_run_dict_records_the_limit_that_decided_the_delivery(self):
        """Size alone cannot be read: the limit is configuration, and the
        shipped default is not the only possible answer."""
        self.oversize()
        entry = self.run_one().to_dict()
        self.assertEqual(entry["inline_chars"], self.LIMIT)

    def test_an_inlined_round_records_the_limit_too(self):
        """Not only the round that went over: a reader asking "how close was
        this" needs both numbers on a clean round as well."""
        entry = self.run_one().to_dict()
        self.assertEqual(entry["delivery"], "inline")
        self.assertEqual(entry["inline_chars"], self.LIMIT)

    def test_the_limit_left_to_the_default_is_still_recorded(self):
        """`run_reviews` resolves it once so that no entry says nothing."""
        entry = review_mod.run_reviews([reviewer("r1")], self.workspace)[0].to_dict()
        self.assertEqual(entry["inline_chars"], review_mod.default_inline_chars())

    def test_the_consolidation_carries_the_limit_beside_the_size(self):
        self.oversize()
        runs = review_mod.run_reviews([reviewer("r1")], self.workspace, inline_chars=self.LIMIT)
        data = review_mod.build_consolidation(self.workspace, [r.to_dict() for r in runs], [])
        self.assertEqual(data["coverage"]["round"], "unverified")
        self.assertEqual(data["coverage"]["change_chars"], self.LIMIT + 1)
        self.assertEqual(data["coverage"]["inline_chars"], self.LIMIT)
        self.assertIn("review.context.inline_chars 4,000", review_mod.render_consolidation(data))

    def test_a_round_recorded_before_the_limit_was_written_down_says_nothing(self):
        """An older report has no number, and this does not invent one: it was
        120,000 then, but nothing wrote it down."""
        runs = review_mod.run_reviews([reviewer("r1")], self.workspace)
        entries = [r.to_dict() for r in runs]
        for entry in entries:
            entry.pop("inline_chars")
        data = review_mod.build_consolidation(self.workspace, entries, [])
        self.assertIsNone(data["coverage"]["inline_chars"])

    def test_the_run_dict_is_stamped_with_the_snapshot_it_answers_for(self):
        """The same stamp the report carries. The reviewer table outlives the
        round -- `--only` merges into it -- so coverage can only be derived
        from entries that say which snapshot they were handed."""
        entry = self.run_one().to_dict()
        stamp = review_mod.current_snapshot_stamp(self.workspace)
        self.assertEqual(entry["snapshot"], stamp)
        self.assertEqual(
            review_mod.report_snapshot(ws.read_text(self.workspace.reviewer_report_path("r1"))), stamp
        )

    def test_partial_is_counted_once_and_not_as_a_failure(self):
        """``_counts`` used to read every not-ok status as failed, so a partial
        round would have been counted in both columns."""
        runs = [
            {"id": "r1", "status": "ok"},
            {"id": "r2", "status": "partial"},
            {"id": "r3", "status": "failed"},
        ]
        counts = review_mod._counts([], runs, runs)
        self.assertEqual(counts["reviewers_ok"], 1)
        self.assertEqual(counts["reviewers_partial"], 1)
        self.assertEqual(counts["reviewers_failed"], 1)
        self.assertEqual(
            counts["reviewers_ok"] + counts["reviewers_partial"] + counts["reviewers_failed"],
            counts["reviewers_total"],
        )

    def test_summarise_runs_separates_the_three(self):
        runs = [
            review_mod.ReviewerRun(reviewer("r1"), "ok"),
            review_mod.ReviewerRun(reviewer("r2"), "partial"),
            review_mod.ReviewerRun(reviewer("r3"), "failed"),
        ]
        self.assertEqual(review_mod.summarise_runs(runs), (1, 1, 1))


PLAN = """# Plan

## Proposed Change
Add a NOT NULL column to orders.
"""

REQUEST = """# Design request

## Goal
Record which channel an order came from.
"""


class TestDesignSnapshot(IsolatedCase):
    """Freezing the plan. No git: there is no diff to take."""

    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace().design_review().ensure()
        self.plan = os.path.join(self.tmp, "plan.md")
        self.request = os.path.join(self.tmp, "design-request.md")
        ws.write_text(self.plan, PLAN)
        ws.write_text(self.request, REQUEST)

    def test_the_plan_is_frozen_as_markdown(self):
        meta = review_mod.create_design_snapshot(self.workspace, self.plan, self.request)
        self.assertEqual(meta["strategy"], "plan")
        self.assertTrue(self.workspace.snapshot_path.endswith("review-target.md"))
        self.assertEqual(ws.read_text(self.workspace.snapshot_path), PLAN)

    def test_the_same_plan_and_request_hash_the_same(self):
        first = review_mod.create_design_snapshot(self.workspace, self.plan, self.request)
        second = review_mod.create_design_snapshot(self.workspace, self.plan, self.request)
        self.assertEqual(first["sha256"], second["sha256"])

    def test_rewriting_the_plan_changes_the_hash(self):
        first = review_mod.create_design_snapshot(self.workspace, self.plan, self.request)
        ws.write_text(self.plan, PLAN + "\nBackfill it first.\n")
        second = review_mod.create_design_snapshot(self.workspace, self.plan, self.request)
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_a_different_request_is_a_different_review(self):
        """The same plan answering a different question is not the same round."""
        first = review_mod.create_design_snapshot(self.workspace, self.plan, self.request)
        ws.write_text(self.request, REQUEST + "\nAnd keep the v1 API.\n")
        second = review_mod.create_design_snapshot(self.workspace, self.plan, self.request)
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_previous_sha_is_only_set_once_the_plan_has_moved_on(self):
        first = review_mod.create_design_snapshot(self.workspace, self.plan, self.request)
        self.assertEqual(first["previous_sha"], "")
        ws.write_json(self.workspace.consolidated_json_path, {"snapshot": {"sha256": first["sha256"]}})
        same = review_mod.create_design_snapshot(self.workspace, self.plan, self.request)
        self.assertEqual(same["previous_sha"], "")
        ws.write_text(self.plan, PLAN + "\nBackfill it first.\n")
        revised = review_mod.create_design_snapshot(self.workspace, self.plan, self.request)
        self.assertEqual(revised["previous_sha"], first["sha256"])

    def test_no_plan_names_the_stage_that_writes_one(self):
        with self.assertRaises(review_mod.ReviewError) as ctx:
            review_mod.create_design_snapshot(self.workspace, os.path.join(self.tmp, "absent.md"))
        self.assertIn("run the architect first", str(ctx.exception))

    def test_an_empty_plan_is_the_same_failure(self):
        ws.write_text(self.plan, "\n\n")
        with self.assertRaises(review_mod.ReviewError):
            review_mod.create_design_snapshot(self.workspace, self.plan, self.request)


class TestDesignReviewPrompt(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace().design_review().ensure()

    def test_it_carries_the_plan_the_request_and_the_read_only_rules(self):
        prompt = review_mod.build_design_review_prompt(reviewer("r1"), self.workspace, PLAN, REQUEST).text
        self.assertIn("NOT NULL column", prompt)
        self.assertIn("Record which channel", prompt)
        self.assertIn("do not modify", prompt.lower())
        self.assertIn("NO_FINDINGS", prompt)

    def test_role_guidance_is_about_the_proposal_not_the_diff(self):
        built = review_mod.build_design_review_prompt(reviewer("r1", role="security"), self.workspace, PLAN)
        self.assertIn("authn/authz gaps it creates", built.text)

    def test_a_custom_role_still_gets_a_usable_prompt(self):
        built = review_mod.build_design_review_prompt(
            reviewer("r1", role="accessibility"), self.workspace, PLAN
        )
        self.assertIn("accessibility specialist", built.text)

    def test_a_missing_request_is_said_rather_than_left_blank(self):
        built = review_mod.build_design_review_prompt(reviewer("r1"), self.workspace, PLAN)
        self.assertIn("Not recorded", built.text)

    def test_a_huge_plan_is_referenced_by_path_instead_of_inlined(self):
        big = "x" * 4_001
        built = review_mod.build_design_review_prompt(
            reviewer("r1"),
            self.workspace,
            big,
            inline_chars=4_000,
        )
        self.assertIn("review-target.md", built.text)
        self.assertNotIn(big, built.text)
        self.assertEqual(built.delivery, "file")
        self.assertEqual(built.change_chars, len(big))
        self.assertIn("review.context.inline_chars (4,000)", built.text)

    def test_a_plan_exactly_on_the_limit_is_still_inlined(self):
        exact = "x" * 4_000
        built = review_mod.build_design_review_prompt(
            reviewer("r1"),
            self.workspace,
            exact,
            inline_chars=4_000,
        )
        self.assertEqual(built.delivery, "inline")
        self.assertIn(exact, built.text)

    def test_the_design_path_decides_delivery_with_the_same_number(self):
        """One function, one setting. A plan and a diff of the same size are
        delivered the same way or the record means two things."""
        big = "x" * 350_000
        built = review_mod.build_design_review_prompt(reviewer("r1"), self.workspace, big)
        self.assertEqual(built.delivery, "inline")
        self.assertEqual(built.delivery, review_mod.prompt_delivery(big))

    def test_the_request_is_inlined_whatever_its_size(self):
        """The change body under review is the plan; the request is context,
        and goes in unmeasured exactly as it did before. Making the limit
        apply to it too is a later stage, and until then this is what the
        docs have to say."""
        big = "y" * 4_001
        built = review_mod.build_design_review_prompt(
            reviewer("r1"),
            self.workspace,
            PLAN,
            big,
            inline_chars=4_000,
        )
        self.assertIn(big, built.text)
        self.assertEqual(built.delivery, "inline")
        self.assertEqual(built.change_chars, len(PLAN))

    def test_a_revision_is_told_what_it_was_meant_to_address(self):
        ws.write_json(
            self.workspace.consolidated_json_path,
            {
                "findings": [
                    {
                        "id": "F1",
                        "severity": "high",
                        "file": "plan.md#Proposed Change",
                        "problem": "no backfill is described",
                        "triage": "accepted",
                    }
                ]
            },
        )
        ws.write_json(self.workspace.snapshot_meta_path, {"previous_sha": "abc123"})
        prompt = review_mod.build_design_review_prompt(reviewer("r1"), self.workspace, PLAN).text
        self.assertIn("This plan is a revision", prompt)
        self.assertIn("no backfill is described", prompt)
        self.assertIn("Do not assume a listed item was real", prompt)
        # Who reported it stays out, as it does for a code re-review.
        self.assertNotIn("reported by", prompt.lower())

    def test_a_first_round_carries_no_revision_note(self):
        prompt = review_mod.build_design_review_prompt(reviewer("r1"), self.workspace, PLAN).text
        self.assertNotIn("This plan is a revision", prompt)


if __name__ == "__main__":
    unittest.main()
