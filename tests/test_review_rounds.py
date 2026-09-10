"""Multi-round review behaviour.

The original suite only ever exercised a single review round, or a second round
over an unchanged finding set. Real rounds change the findings -- that is what
fixing does -- and every bug covered here only appears once they do.
"""

from __future__ import annotations

import os
import unittest

from helpers import IsolatedCase, has_git

from orchestrator import review as review_mod
from orchestrator import workspace as ws


def finding(problem, file="a.py", severity="high", line="2", reviewer="r1", category="correctness"):
    return {
        "id": "?",
        "file": file,
        "line": line,
        "category": category,
        "problem": problem,
        "impact": "",
        "evidence": "",
        "recommended_fix": "",
        "reviewer": reviewer,
    }


class TestTriageSurvivesRenumbering(IsolatedCase):
    """Ids are positional, so triage must be restored by content."""

    def setUp(self):
        super().setUp()
        self.workspace = ws.Workspace(self.tmp).ensure()

    def _consolidate(self, findings, iteration=1):
        data = review_mod.build_consolidation(self.workspace, [], findings, iteration)
        ws.write_json(self.workspace.consolidated_json_path, data)
        return data

    def test_rejection_survives_when_a_higher_severity_finding_disappears(self):
        critical = finding("null deref in the cache warmer", file="cache.py", severity="critical")
        critical["severity"] = "critical"
        other = finding("style nit about naming", file="views.py", severity="high")
        first = self._consolidate([critical, other])
        self.assertEqual([f["id"] for f in first["findings"]], ["F1", "F2"])
        review_mod.set_triage(first, "F2", "rejected", "false positive")
        ws.write_json(self.workspace.consolidated_json_path, first)

        # The critical finding is fixed; the rejected one is now F1.
        second = self._consolidate([other], iteration=2)
        self.assertEqual(second["findings"][0]["id"], "F1")
        self.assertEqual(second["findings"][0]["triage"], "rejected")
        self.assertEqual(second["findings"][0]["triage_note"], "false positive")

    def test_a_rejected_finding_does_not_come_back_as_blocking(self):
        critical = finding("real bug", file="cache.py")
        critical["severity"] = "critical"
        other = finding("not a bug", file="views.py")
        first = self._consolidate([critical, other])
        review_mod.set_triage(first, "F2", "rejected")
        ws.write_json(self.workspace.consolidated_json_path, first)

        second = self._consolidate([other], iteration=2)
        self.assertEqual(review_mod.unresolved_blocking(second), [])

    def test_acceptance_survives_renumbering(self):
        low = finding("minor thing", file="a.py")
        low["severity"] = "low"
        high = finding("serious thing", file="b.py")
        first = self._consolidate([low, high])
        accepted_id = next(f["id"] for f in first["findings"] if f["file"] == "a.py")
        review_mod.set_triage(first, accepted_id, "accepted", "confirmed")
        ws.write_json(self.workspace.consolidated_json_path, first)

        second = self._consolidate([low], iteration=2)
        self.assertEqual([f["file"] for f in review_mod.accepted_findings(second)], ["a.py"])

    def test_a_different_finding_never_inherits_someone_elses_triage(self):
        first = self._consolidate([finding("problem one", file="a.py")])
        review_mod.set_triage(first, "F1", "accepted")
        ws.write_json(self.workspace.consolidated_json_path, first)

        second = self._consolidate([finding("a completely unrelated problem", file="b.py")], 2)
        self.assertEqual(second["findings"][0]["triage"], "needs-triage")

    def test_findings_carry_a_stable_key(self):
        data = self._consolidate([finding("problem one", file="a.py")])
        self.assertEqual(data["findings"][0]["key"], review_mod.finding_key(data["findings"][0]))


@unittest.skipUnless(has_git(), "git is required")
class TestIterationDerivation(IsolatedCase):
    """The loop budget must not depend on the caller passing a number."""

    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "x = 1\n")
        self.commit_all("init")
        self.workspace = ws.Workspace(self.project).ensure()

    def _snapshot(self, content):
        self.write("app.py", content)
        return review_mod.create_snapshot(self.workspace)

    def test_first_round_is_one(self):
        self._snapshot("x = 2\n")
        self.assertEqual(review_mod.next_iteration(self.workspace), 1)

    def test_a_new_snapshot_advances_the_round(self):
        self._snapshot("x = 2\n")
        ws.write_json(
            self.workspace.consolidated_json_path,
            review_mod.build_consolidation(self.workspace, [], [], 1),
        )
        self._snapshot("x = 3\n")
        self.assertEqual(review_mod.next_iteration(self.workspace), 2)

    def test_re_running_the_same_snapshot_stays_in_the_round(self):
        self._snapshot("x = 2\n")
        ws.write_json(
            self.workspace.consolidated_json_path,
            review_mod.build_consolidation(self.workspace, [], [], 1),
        )
        self.assertEqual(review_mod.next_iteration(self.workspace), 1)

    def test_rounds_keep_advancing_so_the_budget_is_reachable(self):
        seen = []
        for index in range(4):
            self._snapshot("x = %d\n" % (index + 10))
            iteration = review_mod.next_iteration(self.workspace)
            seen.append(iteration)
            ws.write_json(
                self.workspace.consolidated_json_path,
                review_mod.build_consolidation(self.workspace, [], [], iteration),
            )
        self.assertEqual(seen, [1, 2, 3, 4])


@unittest.skipUnless(has_git(), "git is required")
class TestStaleReports(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "x = 1\n")
        self.commit_all("init")
        self.workspace = ws.Workspace(self.project).ensure()
        self.write("app.py", "x = 2\n")
        review_mod.create_snapshot(self.workspace)

    def _write_report(self, reviewer_id, snapshot, problem):
        body = (
            "# Review\n\n- Reviewer: %s\n- Provider: mock\n- Model: m\n- Role: general\n"
            "- Snapshot: %s\n\n---\n\n"
            "## Finding\n- Severity: high\n- File: app.py\n- Line: 1\n- Category: correctness\n"
            "- Problem: %s\n- Evidence: `x`\n- Recommended fix: y\n"
        ) % (reviewer_id, snapshot, problem)
        ws.write_text(self.workspace.reviewer_report_path(reviewer_id), body)

    def test_report_for_the_current_snapshot_is_used(self):
        stamp = review_mod.current_snapshot_stamp(self.workspace)
        self._write_report("r1", stamp, "current issue")
        findings, stale = review_mod.read_reports(self.workspace, ["r1"], stamp)
        self.assertEqual(len(findings), 1)
        self.assertEqual(stale, [])

    def test_report_from_an_earlier_snapshot_is_skipped(self):
        self._write_report("r1", "deadbeefcafe", "already fixed")
        stamp = review_mod.current_snapshot_stamp(self.workspace)
        findings, stale = review_mod.read_reports(self.workspace, ["r1"], stamp)
        self.assertEqual(findings, [])
        self.assertEqual(stale, ["r1"])

    def test_without_an_expected_snapshot_nothing_is_skipped(self):
        self._write_report("r1", "deadbeefcafe", "old")
        findings, stale = review_mod.read_reports(self.workspace, ["r1"])
        self.assertEqual(len(findings), 1)
        self.assertEqual(stale, [])

    def test_a_new_snapshot_invalidates_the_previous_round_of_reports(self):
        stamp = review_mod.current_snapshot_stamp(self.workspace)
        self._write_report("r1", stamp, "issue in round one")
        self.write("app.py", "x = 3\n")
        review_mod.create_snapshot(self.workspace)
        new_stamp = review_mod.current_snapshot_stamp(self.workspace)
        self.assertNotEqual(stamp, new_stamp)
        findings, stale = review_mod.read_reports(self.workspace, ["r1"], new_stamp)
        self.assertEqual(findings, [])
        self.assertEqual(stale, ["r1"])

    def test_report_snapshot_stamp_is_readable(self):
        self._write_report("r1", "abc123abc123", "x")
        text = ws.read_text(self.workspace.reviewer_report_path("r1"))
        self.assertEqual(review_mod.report_snapshot(text), "abc123abc123")

    def test_missing_report_is_not_stale(self):
        findings, stale = review_mod.read_reports(self.workspace, ["never-ran"], "abc")
        self.assertEqual((findings, stale), ([], []))


class TestUnparseableReports(IsolatedCase):
    """A report that cannot be read must never be reported as clean."""

    BOLD = (
        "**Finding 1**\n- Severity: critical\n- File: app.py\n- Line: 10\n"
        "- Category: security\n- Problem: auth is skipped for admin routes\n"
        "- Impact: any user reaches admin endpoints\n- Evidence: `if not user: pass`\n"
        "- Recommended fix: raise instead of passing\n"
    )

    def test_bold_finding_heading_is_parsed(self):
        findings = review_mod.parse_findings(self.BOLD, "r1")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "critical")
        self.assertEqual(findings[0]["file"], "app.py")

    def test_numbered_and_colon_headings_are_parsed(self):
        for heading in ("Finding 2:", "### Finding", "Finding", "**Finding**"):
            text = self.BOLD.replace("**Finding 1**", heading)
            self.assertEqual(len(review_mod.parse_findings(text, "r1")), 1, heading)

    def test_a_report_with_no_heading_at_all_is_recovered(self):
        text = self.BOLD.replace("**Finding 1**\n", "")
        findings = review_mod.parse_findings(text, "r1")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["file"], "app.py")

    def test_several_headingless_findings_are_split_on_severity(self):
        text = self.BOLD.replace("**Finding 1**\n", "") * 2
        self.assertEqual(len(review_mod.parse_findings(text, "r1")), 2)

    def test_prose_only_report_is_flagged_not_treated_as_clean(self):
        text = "The diff looks fine to me overall, nice work.\n"
        self.assertEqual(review_mod.parse_findings(text, "r1"), [])
        self.assertIn("could not be parsed", review_mod.unparsed_report_warning(text, []))

    def test_empty_report_is_flagged(self):
        self.assertIn("empty", review_mod.unparsed_report_warning("", []))

    def test_no_findings_sentinel_is_not_flagged(self):
        for body in ("NO_FINDINGS\n", "**NO_FINDINGS**\n", "  NO_FINDINGS  \n"):
            self.assertEqual(review_mod.unparsed_report_warning(body, []), "", body)

    def test_a_parsed_report_is_not_flagged(self):
        findings = review_mod.parse_findings(self.BOLD, "r1")
        self.assertEqual(review_mod.unparsed_report_warning(self.BOLD, findings), "")

    def test_the_report_header_alone_does_not_count_as_clean(self):
        header = "# Review\n\n- Reviewer: r1\n- Snapshot: abc\n\n---\n\n"
        self.assertIn("empty", review_mod.unparsed_report_warning(header, []))


@unittest.skipUnless(has_git(), "git is required")
class TestUnparseableRunStatus(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "x = 1\n")
        self.commit_all("init")
        self.write("app.py", "x = 2\n")
        self.workspace = ws.Workspace(self.project).ensure()
        review_mod.create_snapshot(self.workspace)

    def _reviewer(self, reviewer_id="r1"):
        return {"id": reviewer_id, "provider": "mock", "model": {"family": "small"}, "role": "general"}

    def test_a_reviewer_that_returns_prose_is_not_counted_as_ok(self):
        os.environ["DEV_ORCHESTRA_MOCK_RESPONSE"] = "Looks good to me.\n"
        runs = review_mod.run_reviews([self._reviewer()], self.workspace)
        self.assertEqual(runs[0].status, "unparsed")
        self.assertIn("could not be parsed", runs[0].error)
        self.assertEqual(review_mod.summarise_runs(runs), (0, 1))

    def test_a_clean_review_is_still_ok(self):
        os.environ["DEV_ORCHESTRA_MOCK_RESPONSE"] = "NO_FINDINGS\n"
        runs = review_mod.run_reviews([self._reviewer()], self.workspace)
        self.assertEqual(runs[0].status, "ok")
        self.assertEqual(runs[0].findings, 0)

    def test_the_report_is_still_written_for_inspection(self):
        os.environ["DEV_ORCHESTRA_MOCK_RESPONSE"] = "Looks good to me.\n"
        review_mod.run_reviews([self._reviewer()], self.workspace)
        self.assertIn("Looks good", ws.read_text(self.workspace.reviewer_report_path("r1")))


if __name__ == "__main__":
    unittest.main()
