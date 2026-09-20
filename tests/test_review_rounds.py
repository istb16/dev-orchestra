"""Multi-round review behaviour.

The original suite only ever exercised a single review round, or a second round
over an unchanged finding set. Real rounds change the findings -- that is what
fixing does -- and every bug covered here only appears once they do.
"""

from __future__ import annotations

import os
import unittest

from helpers import IsolatedCase, has_git

from orchestrator import cli as cli_mod
from orchestrator import review as review_mod
from orchestrator import workspace as ws

SNAPSHOT = "snapshot-one"


def run(status, delivery, change_chars, reviewer_id="r1", snapshot=SNAPSHOT):
    """A reviewer entry as ``ReviewerRun.to_dict`` writes it, trimmed to the
    keys the round's coverage and its rendering read.

    ``snapshot`` is the stamp the entry was recorded against. Coverage reads
    only the entries whose stamp is the snapshot being reported on, so an
    entry without one answers for no round at all."""
    return {
        "id": reviewer_id,
        "role": "general",
        "status": status,
        "delivery": delivery,
        "change_chars": change_chars,
        "snapshot": snapshot,
    }


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
        self.workspace = self.cli_workspace()

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
        self.workspace = self.cli_workspace()
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
        self.workspace = self.cli_workspace()
        review_mod.create_snapshot(self.workspace)

    def _reviewer(self, reviewer_id="r1"):
        return {"id": reviewer_id, "provider": "mock", "model": {"family": "small"}, "role": "general"}

    def test_a_reviewer_that_returns_prose_is_not_counted_as_ok(self):
        os.environ["DEV_ORCHESTRA_MOCK_RESPONSE"] = "Looks good to me.\n"
        runs = review_mod.run_reviews([self._reviewer()], self.workspace)
        self.assertEqual(runs[0].status, "unparsed")
        self.assertIn("could not be parsed", runs[0].error)
        self.assertEqual(review_mod.summarise_runs(runs), (0, 1, 0))

    def test_a_clean_review_is_still_ok(self):
        os.environ["DEV_ORCHESTRA_MOCK_RESPONSE"] = "NO_FINDINGS\n"
        runs = review_mod.run_reviews([self._reviewer()], self.workspace)
        self.assertEqual(runs[0].status, "ok")
        self.assertEqual(runs[0].findings, 0)

    def test_the_report_is_still_written_for_inspection(self):
        os.environ["DEV_ORCHESTRA_MOCK_RESPONSE"] = "Looks good to me.\n"
        review_mod.run_reviews([self._reviewer()], self.workspace)
        self.assertIn("Looks good", ws.read_text(self.workspace.reviewer_report_path("r1")))


class TestCoverageCarriesAcrossRounds(IsolatedCase):
    """An incremental round inlines the fix, and only the fix.

    So its own coverage says nothing about the whole change, and if the whole
    change's state did not carry forward, one fix-only round would launder a
    partial one: accept, fix, inline the fix, NO_FINDINGS, clean -- with most
    of the change still unread. Carried on the workflow and the branch: the
    round counter's key without the base, because the base is how a reader
    would narrow the round rather than review the change.
    """

    def setUp(self):
        super().setUp()
        self.workspace = ws.Workspace(self.tmp).ensure()

    def round(
        self,
        delivery,
        status="ok",
        iteration=1,
        incremental=False,
        lineage="",
        chars=130_412,
        snapshot=SNAPSHOT,
    ):
        """One recorded round: a reviewer entry plus the snapshot it saw."""
        ws.write_json(
            self.workspace.snapshot_meta_path,
            {"sha256": snapshot, "incremental_from": "tree" if incremental else ""},
        )
        data = review_mod.build_consolidation(
            self.workspace, [run(status, delivery, chars, snapshot=snapshot)], [], iteration, lineage
        )
        ws.write_json(self.workspace.consolidated_json_path, data)
        return data["coverage"]

    def test_a_handover_marks_the_round_and_the_change(self):
        coverage = self.round("file", status="partial")
        self.assertEqual(coverage["round"], "unverified")
        self.assertEqual(coverage["change"], "unverified")
        self.assertEqual(coverage["unverified_since"], 1)
        self.assertEqual(coverage["change_chars"], 130_412)

    def test_an_incremental_round_that_inlines_the_fix_does_not_clear_it(self):
        """The round is complete -- the fix was shown in full -- and the change
        is not. This is the CRITICAL from the first review round, coming back
        through the door marked "round 2 was clean"."""
        self.round("file", status="partial")
        coverage = self.round("inline", iteration=2, incremental=True, chars=400)
        self.assertEqual(coverage["round"], "complete")
        self.assertEqual(coverage["change"], "unverified")
        self.assertEqual(coverage["unverified_since"], 1)

    def test_the_whole_change_inlined_and_reviewed_clears_it(self):
        self.round("file", status="partial")
        self.round("inline", iteration=2, incremental=True, chars=400)
        coverage = self.round("inline", iteration=3, chars=90_000)
        self.assertEqual(coverage["change"], "complete")
        self.assertIsNone(coverage["unverified_since"])

    def test_a_round_nobody_completed_clears_nothing(self):
        """Inlined for everybody and read by nobody: the change is no more
        reviewed than it was before the round ran."""
        self.round("file", status="partial")
        coverage = self.round("inline", status="failed", iteration=2, chars=90_000)
        self.assertEqual(coverage["round"], "complete")
        self.assertEqual(coverage["unverified_since"], 1)

    def test_narrowing_the_base_does_not_drop_the_mark(self):
        """`--base` is the other way of saying which change, and narrowing it
        is the first thing a reader reaches for when a round went over the
        limit. Keyed on the base, the mark would be cleared by the attempt to
        deal with it -- so it is carried on the workflow and branch alone.

        The narrowed round fails here, so nothing about it could clear the
        mark on its own merit: what is under test is that changing the base
        did not throw the mark away before that question was even asked.

        The mark survives; the round number does not travel with it. See
        ``test_the_mark_outlives_a_lineage_change_and_its_number_does_not``."""
        self.round("file", status="partial", lineage="wf|feature-a|")
        coverage = self.round(
            "inline",
            status="failed",
            iteration=2,
            lineage="wf|feature-a|HEAD~1",
            chars=400,
            snapshot="snapshot-two",
        )
        self.assertEqual(coverage["change"], "unverified")
        self.assertIsNone(coverage["unverified_since"])

    def test_the_mark_outlives_a_lineage_change_and_its_number_does_not(self):
        """`review status` prints `iteration N/M` from `next_iteration` and the
        mark from here, in one breath. The count restarts on a lineage change,
        so a round number carried across one names a round that count does not
        have: `iteration 1/2` beside "change unverified since round 3". The
        safety property is the mark, not the number, so the mark is stated
        without a round rather than with one that is not there."""
        self.round("file", status="partial", lineage="wf|feature-a|", iteration=3)
        lineage = "wf|feature-a|HEAD~1"
        iteration = review_mod.next_iteration(self.workspace, lineage, "snapshot-two")
        self.assertEqual(iteration, 1)
        coverage = self.round(
            "inline",
            status="failed",
            iteration=iteration,
            lineage=lineage,
            chars=400,
            snapshot="snapshot-two",
        )
        self.assertEqual(coverage["change"], "unverified")
        self.assertIsNone(coverage["unverified_since"])

    def test_a_mark_that_lost_its_number_still_carries(self):
        """Read from `unverified_since` alone, the mark would be dropped by the
        round after the one that dropped the number -- which is the laundering
        the carry exists to stop, one round later."""
        self.round("file", status="partial", lineage="wf|feature-a|")
        narrowed = "wf|feature-a|HEAD~1"
        self.round("inline", status="failed", iteration=1, lineage=narrowed, chars=400, snapshot="two")
        coverage = self.round(
            "inline", status="failed", iteration=2, lineage=narrowed, chars=400, snapshot="three"
        )
        self.assertEqual(coverage["change"], "unverified")
        self.assertIsNone(coverage["unverified_since"])

    def test_another_review_carries_nothing_over(self):
        self.round("file", status="partial", lineage="wf|feature-a|")
        coverage = self.round("inline", iteration=1, lineage="wf|feature-b|", chars=90_000)
        self.assertEqual(coverage["change"], "complete")
        self.assertIsNone(coverage["unverified_since"])

    def test_an_unkeyed_round_still_continues_the_last_one(self):
        """`review consolidate` and the older commands pass no lineage, and
        must not silently start a new review -- the same condition
        `next_iteration` applies."""
        self.round("file", status="partial", lineage="wf|feature-a|")
        coverage = self.round("inline", iteration=2, incremental=True, chars=400)
        self.assertEqual(coverage["unverified_since"], 1)

    def test_a_snapshot_nobody_has_reviewed_clears_nothing(self):
        """`review snapshot --full` then `review consolidate`. The reviewer
        table handed to the consolidation still holds the previous round's
        entries -- that is what keeps the table complete -- and reading them as
        this round's would report a snapshot nobody has opened as reviewed in
        full, with zero reviewers having run."""
        self.round("file", status="partial")
        ws.write_json(self.workspace.snapshot_meta_path, {"sha256": "snapshot-two"})
        data = review_mod.build_consolidation(self.workspace, [run("ok", "inline", 90_000)], [], 2, "")
        self.assertEqual(data["coverage"]["round"], "none")
        self.assertEqual(data["coverage"]["change"], "unverified")
        self.assertEqual(data["coverage"]["unverified_since"], 1)
        self.assertIsNone(data["coverage"]["change_chars"])

    def test_one_reviewer_re_run_is_measured_against_this_snapshot(self):
        """`--only` puts a fresh entry in a table that still holds older ones.
        Both the round's answer and its size come from the entries stamped
        with the snapshot being reported on, wherever in the table they sit."""
        self.round("file", status="partial")
        ws.write_json(self.workspace.snapshot_meta_path, {"sha256": "snapshot-two"})
        table = [
            run("partial", "file", 130_412, "r1"),
            run("ok", "inline", 400, "r2", snapshot="snapshot-two"),
        ]
        data = review_mod.build_consolidation(self.workspace, table, [], 2, "")
        self.assertEqual(data["coverage"]["round"], "complete")
        self.assertEqual(data["coverage"]["change_chars"], 400)
        self.assertEqual(data["coverage"]["change"], "complete")

    def test_the_pair_comes_off_the_entry_that_handed_the_body_over(self):
        """The limit is no longer one number per round: `--only` re-runs a
        reviewer after the setting has changed, and the fresh entry sits ahead
        of the retained one carrying the old limit. Both numbers have to come
        off the entry whose delivery made the round unverified -- 4,001 chars
        against a limit of 10,000 would have been inlined, so a line naming
        that limit beside that size contradicts itself."""
        ws.write_json(self.workspace.snapshot_meta_path, {"sha256": "snapshot-two"})
        table = [
            dict(run("ok", "inline", 4_001, "r1", snapshot="snapshot-two"), inline_chars=10_000),
            dict(run("partial", "file", 4_001, "r2", snapshot="snapshot-two"), inline_chars=4_000),
        ]
        data = review_mod.build_consolidation(self.workspace, table, [], 1, "")
        self.assertEqual(data["coverage"]["round"], "unverified")
        self.assertEqual(data["coverage"]["change_chars"], 4_001)
        self.assertEqual(data["coverage"]["inline_chars"], 4_000)

    def test_a_round_with_no_reviewers_says_so_rather_than_guessing(self):
        data = review_mod.build_consolidation(self.workspace, [], [], 1, "")
        self.assertEqual(data["coverage"]["round"], "none")
        self.assertEqual(data["coverage"]["change"], "none")
        self.assertIsNone(data["coverage"]["change_chars"])

    def test_entries_from_before_this_version_are_not_read_as_either_answer(self):
        """Every recorded run so far was under the limit, so reading them as
        inline would even be right -- but the record does not say so, and the
        docs say the same."""
        data = review_mod.build_consolidation(self.workspace, [{"id": "r1", "status": "ok"}], [], 1, "")
        self.assertEqual(data["coverage"]["round"], "none")

    def test_a_round_with_no_reviewers_does_not_clear_a_carried_mark(self):
        self.round("file", status="partial")
        data = review_mod.build_consolidation(self.workspace, [], [], 2, "")
        self.assertEqual(data["coverage"]["change"], "unverified")
        self.assertEqual(data["coverage"]["unverified_since"], 1)

    def test_one_reviewer_of_two_over_the_limit_marks_the_round(self):
        """Coverage is a property of the round, not of a reviewer: a change
        one reviewer read in full is still a change another did not."""
        ws.write_json(self.workspace.snapshot_meta_path, {"sha256": SNAPSHOT})
        runs = [run("ok", "inline", 400), run("partial", "file", 130_412, "r2")]
        data = review_mod.build_consolidation(self.workspace, runs, [], 1, "")
        self.assertEqual(data["coverage"]["round"], "unverified")

    def test_consolidating_the_same_round_twice_gives_the_same_answer(self):
        """`review consolidate` re-runs the derivation over its own output."""
        first = self.round("file", status="partial")
        again = self.round("file", status="partial")
        self.assertEqual(first, again)

    def test_a_design_round_clears_it_with_a_revised_plan(self):
        """A design snapshot has no `incremental_from`, so every design round
        is the whole plan -- a revision that fits inline is the whole change."""
        self.round("file", status="partial", chars=130_412)
        ws.write_json(self.workspace.snapshot_meta_path, {"sha256": "plan-two-sha", "strategy": "plan"})
        data = review_mod.build_consolidation(
            self.workspace, [run("ok", "inline", 9_000, snapshot="plan-two-sha")], [], 2, ""
        )
        self.assertEqual(data["coverage"]["change"], "complete")


class TestCoverageInTheConsolidatedReport(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = ws.Workspace(self.tmp).ensure()

    def render(self, runs, iteration=1, incremental=False):
        ws.write_json(
            self.workspace.snapshot_meta_path,
            {"sha256": SNAPSHOT, "incremental_from": "tree" if incremental else ""},
        )
        data = review_mod.build_consolidation(self.workspace, runs, [], iteration)
        ws.write_json(self.workspace.consolidated_json_path, data)
        return review_mod.render_consolidation(data)

    def test_a_clean_round_says_so_in_one_line(self):
        text = self.render([run("ok", "inline", 400)])
        self.assertIn("- Coverage: round complete, change complete", text)
        self.assertIn("- ok [general]", text)

    def test_a_handover_is_marked_on_the_reviewer_and_in_the_heading(self):
        text = self.render([run("partial", "file", 130_412)])
        self.assertIn("round unverified", text)
        self.assertIn("130,412 chars", text)
        self.assertIn("not a clean review", text)
        self.assertIn("- PARTIAL [general]", text)
        self.assertIn(", 1 partial", text)

    def test_a_carried_mark_says_what_would_clear_it(self):
        self.render([run("partial", "file", 130_412)])
        text = self.render([run("ok", "inline", 400)], iteration=2, incremental=True)
        self.assertIn("change unverified since round 1", text)
        self.assertIn("snapshot --full", text)

    def test_a_carried_mark_on_a_snapshot_nobody_ran_against_says_that(self):
        """Not "this round inlined the fix only": nothing was inlined, because
        nobody ran. What is missing is a reviewer, and the line has to say so
        rather than send the reader after a narrower diff."""
        self.render([run("partial", "file", 130_412)])
        ws.write_json(self.workspace.snapshot_meta_path, {"sha256": "snapshot-two"})
        data = review_mod.build_consolidation(self.workspace, [run("ok", "inline", 400)], [], 2)
        self.assertIn("no reviewer has run against this snapshot", review_mod.render_consolidation(data))

    def test_a_report_from_before_this_version_gets_no_coverage_line(self):
        """Saying `round none` would claim it had been measured."""
        self.assertNotIn("Coverage:", review_mod.render_consolidation({"counts": {}, "reviewers": []}))


class TestTheReviewerTally(IsolatedCase):
    """The reviewer table outlives the round; a tally quoted beside a coverage
    value must not. `2 ok / 2 total` next to "no reviewer has run against this
    snapshot" is one report describing two snapshots."""

    def setUp(self):
        super().setUp()
        self.workspace = ws.Workspace(self.tmp).ensure()
        ws.write_json(self.workspace.snapshot_meta_path, {"sha256": SNAPSHOT})

    def test_both_tallies_are_named_where_they_differ(self):
        table = [run("ok", "inline", 400, "r1"), run("ok", "inline", 400, "r2", snapshot="older")]
        data = review_mod.build_consolidation(self.workspace, table, [], 1)
        counts = data["counts"]
        self.assertEqual((counts["reviewers_ok"], counts["reviewers_total"]), (2, 2))
        self.assertEqual((counts["snapshot_reviewers_ok"], counts["snapshot_reviewers_total"]), (1, 1))
        text = review_mod.render_consolidation(data)
        self.assertIn("- Reviewers: 2 ok / 2 total", text)
        self.assertIn("- Reviewers of this snapshot: 1 ok / 1 total", text)

    def test_one_tally_when_the_table_is_this_snapshot(self):
        """The scoped entries are a subset of the table, so equal totals are
        the same set and a second line would say it twice."""
        data = review_mod.build_consolidation(self.workspace, [run("ok", "inline", 400)], [], 1)
        text = review_mod.render_consolidation(data)
        self.assertIn("- Reviewers: 1 ok / 1 total", text)
        self.assertNotIn("Reviewers of this snapshot", text)


class TestTheTwoReadersDescribeOneSnapshot(IsolatedCase):
    """`consolidated.md` and `review status` read one file, so they must not
    describe it differently: each names the one thing that is missing, and
    both name the same one. Round one went over the limit here, so every round
    below carries the mark and differs only in what is missing now.
    """

    def setUp(self):
        super().setUp()
        self.workspace = ws.Workspace(self.tmp).ensure()
        ws.write_json(self.workspace.snapshot_meta_path, {"sha256": SNAPSHOT})
        ws.write_json(
            self.workspace.consolidated_json_path,
            review_mod.build_consolidation(self.workspace, [run("partial", "file", 130_412)], [], 1),
        )

    def round_two(self, runs, incremental=False):
        ws.write_json(
            self.workspace.snapshot_meta_path,
            {"sha256": "snapshot-two", "incremental_from": "tree" if incremental else ""},
        )
        return review_mod.build_consolidation(self.workspace, runs, [], 2)

    def both(self, data, design=False):
        """What each command puts in front of a reader, for one report."""
        return (
            review_mod.render_consolidation(data),
            "\n".join(cli_mod._coverage_advice(data["coverage"], data["counts"], False, 400_000, design)),
        )

    def test_a_snapshot_nobody_ran_against_names_the_missing_reviewer(self):
        """`review snapshot --full` then `review consolidate`: sending the
        reader to re-snapshot with --full tells them to redo what they just
        did, and the reviewer they lack goes unmentioned."""
        for text in self.both(self.round_two([run("ok", "inline", 400)])):
            self.assertIn("no reviewer has run against this snapshot", text)
            self.assertIn("review run", text)
            self.assertNotIn("--full", text)

    def test_a_round_no_reviewer_came_back_ok_for_names_the_reviewers(self):
        """The whole change was inlined and read by nobody: neither "inlined
        the fix only" nor --full is true of it."""
        data = self.round_two([run("failed", "inline", 400, snapshot="snapshot-two")])
        for text in self.both(data):
            self.assertIn("no reviewer came back ok", text)
            self.assertIn("re-run the reviewers that did not", text)
            self.assertNotIn("--full", text)

    def test_a_fix_only_round_names_the_snapshot(self):
        data = self.round_two([run("ok", "inline", 400, snapshot="snapshot-two")], incremental=True)
        for text in self.both(data):
            self.assertIn("--full", text)
            self.assertNotIn("no reviewer", text)

    def test_the_design_advice_names_the_design_round_not_a_shorter_plan(self):
        """A plan nobody has read is not an oversize plan."""
        _, advice = self.both(self.round_two([run("ok", "inline", 400)]), design=True)
        self.assertIn("no reviewer has run against this plan", advice)
        self.assertNotIn("shorten", advice)

    def test_the_partial_count_in_the_advice_counts_this_snapshot(self):
        """The advice's `N reviewer(s) partial` is quoted beside a coverage
        value derived from this snapshot's entries, so it counts that set and
        not the table, which still holds round one's partial reviewer."""
        table = [
            run("partial", "file", 130_412, "r1"),
            run("partial", "file", 130_412, "r2", snapshot="snapshot-two"),
        ]
        data = self.round_two(table)
        self.assertEqual(data["counts"]["reviewers_partial"], 2)
        _, advice = self.both(data)
        self.assertIn("1 reviewer(s) partial", advice)


if __name__ == "__main__":
    unittest.main()
