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
        self.workspace = ws.Workspace(self.project).ensure()

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
        self.workspace = ws.Workspace(self.project).ensure()
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
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "Reviewer id: r2"
        runs = review_mod.run_reviews([reviewer("r1"), reviewer("r2"), reviewer("r3")], self.workspace)
        ok, failed = review_mod.summarise_runs(runs)
        self.assertEqual((ok, failed), (2, 1))
        self.assertEqual([r.reviewer["id"] for r in runs if r.status == "failed"], ["r2"])

    def test_a_reviewer_with_an_unknown_provider_fails_alone(self):
        broken = {"id": "bad", "provider": "nonexistent", "role": "general"}
        runs = review_mod.run_reviews([reviewer("r1"), broken], self.workspace)
        self.assertEqual(review_mod.summarise_runs(runs), (1, 1))

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
        )
        self.assertIn("security", prompt)
        self.assertIn("injection", prompt)
        self.assertIn("do not modify", prompt.lower())
        self.assertIn("NO_FINDINGS", prompt)

    def test_custom_role_still_gets_a_usable_prompt(self):
        prompt = review_mod.build_review_prompt(reviewer("r1", role="accessibility"), self.workspace, "diff")
        self.assertIn("accessibility specialist", prompt)

    def test_huge_diffs_are_referenced_by_path_instead_of_inlined(self):
        big = "x" * (review_mod.MAX_INLINE_DIFF_CHARS + 1)
        prompt = review_mod.build_review_prompt(reviewer("r1"), self.workspace, big)
        self.assertIn("review-target.diff", prompt)
        self.assertNotIn(big, prompt)


if __name__ == "__main__":
    unittest.main()
