"""Findings a reviewer marks pre-existing are kept apart from triage (#385).

A medium or low problem that was there before the change, and that the change
does not make worse, is not the change's to fix. It is listed in its own
section and its own ``pre_existing`` key, with ``P`` ids and no triage, and
nothing that reads ``findings`` -- the counts, the signature, the blocking
list, the fix brief and the loop -- sees it.

A critical or high one is not taken on one reviewer's word and stays in
``findings``, and so does one the last report holds and has not rejected --
in the same words or at the same place: on a fix-only round an unfixed
accepted finding re-reported as pre-existing keeps its triage.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List

from helpers import IsolatedCase, has_git, run_cli

from orchestrator import review_consolidation, review_loop
from orchestrator import workspace as ws
from orchestrator.review_common import (
    DESIGN_REVIEW_PROMPT_TEMPLATE,
    PRE_EXISTING_CATEGORIES,
    REVIEW_PROMPT_TEMPLATE,
)
from orchestrator.review_parsing import parse_findings
from orchestrator.review_round_context import render_round_context

BLOCKING = ("critical", "high")
#: The util.py finding in other words, as a reviewer may put it on a later round.
REWORDED = "exceptions raised in the helper are silently discarded"

REPORT = """## Finding
- Severity: high
- File: app.py
- Line: 2
- Category: correctness
- Problem: subtraction was used where addition was intended
- Impact: every caller gets the wrong total
- Evidence: return a - b
- Fix: restore the addition

## Finding
- Severity: critical
- File: db.py
- Line: 40
- Category: pre-existing
- Problem: the connection is never closed on the error path
- Impact: connections leak under load
- Evidence: conn = open_db()
- Fix: close it in a finally block

## Finding
- Severity: high
- File: auth.py
- Line: 12
- Category: Preexisting
- Problem: the token is compared with == rather than a constant-time compare
- Impact: timing side channel
- Evidence: if token == expected
- Fix: use hmac.compare_digest

## Finding
- Severity: medium
- File: util.py
- Line: 7
- Category: pre_existing
- Problem: the helper swallows every exception
- Impact: failures go unreported
- Evidence: except Exception: pass
- Fix: log and re-raise

## Finding
- Severity: low
- File: names.py
- Line: 3
- Category: pre-existing
- Problem: the variable name is misleading
- Impact: readers misjudge what it holds
- Evidence: total = count
- Fix: rename it
"""


def build(findings: List[Dict[str, Any]], last: Dict[str, Any] | None = None, scope: str = "whole"):
    meta = {"sha256": "abc", "files": [], "scope": scope}
    return review_consolidation.consolidation_from(meta, last or {}, [], findings)


def parsed() -> List[Dict[str, Any]]:
    return parse_findings(REPORT, "r1")


def by_file(name: str) -> Dict[str, Any]:
    return next(f for f in parsed() if f["file"] == name)


def mild_pre_existing() -> List[Dict[str, Any]]:
    """The medium and low pre-existing findings: the ones that are set apart."""
    return [by_file("util.py"), by_file("names.py")]


class TestSplit(unittest.TestCase):
    def test_split(self):
        data = build(parsed())
        self.assertEqual({f["file"] for f in data["findings"]}, {"app.py", "db.py", "auth.py"})
        self.assertEqual(data["counts"]["findings_total"], 3)
        pre = data["pre_existing"]
        self.assertEqual([f["id"] for f in pre], ["P1", "P2"])
        self.assertEqual([f["file"] for f in pre], ["util.py", "names.py"])
        for entry in pre:
            self.assertNotIn("triage", entry)
            self.assertNotIn("triage_note", entry)
            self.assertEqual(entry["key"], review_consolidation.finding_key(entry))
            self.assertEqual(entry["reported_by"], ["r1"])

    def test_every_spelling(self):
        for category in PRE_EXISTING_CATEGORIES:
            with self.subTest(category=category):
                finding = dict(by_file("util.py"), category=category)
                data = build([finding])
                self.assertEqual(data["findings"], [])
                self.assertEqual([f["id"] for f in data["pre_existing"]], ["P1"])

    def test_always_present_and_empty_without_any(self):
        data = build([parsed()[0]])
        self.assertEqual(data["pre_existing"], [])

    def test_merged_across_reviewers(self):
        finding = by_file("util.py")
        other = dict(finding, reviewer="r2")
        data = build([finding, other])
        self.assertEqual(len(data["pre_existing"]), 1)
        self.assertEqual(data["pre_existing"][0]["reported_by"], ["r1", "r2"])
        self.assertEqual(data["duplicate_candidates"], [])


class TestSevereStaysInTriage(unittest.TestCase):
    """A critical or high "pre-existing" is one reviewer's word, and the diff can steer it."""

    def test_severe_is_a_finding(self):
        data = build([by_file("db.py"), by_file("auth.py")])
        self.assertEqual(data["pre_existing"], [])
        self.assertEqual(
            {(f["file"], f["category"], f["triage"]) for f in data["findings"]},
            {("db.py", "pre-existing", "needs-triage"), ("auth.py", "preexisting", "needs-triage")},
        )
        self.assertEqual(len(review_consolidation.unresolved_blocking(data, BLOCKING)), 2)
        self.assertNotEqual(
            review_consolidation.findings_signature(data), review_consolidation.NO_FINDINGS_SIGNATURE
        )

    def test_severe_is_not_a_clean_round(self):
        data = build([by_file("db.py")], scope="whole")
        decision = review_loop.decide(True, data, {}, BLOCKING, 1, 2)
        self.assertFalse(decision.finish)
        self.assertEqual([f["file"] for f in decision.wanted], ["db.py"])
        decision = review_loop.decide(False, data, {}, BLOCKING, 1, 2)
        self.assertEqual([f["file"] for f in decision.wanted], ["db.py"])

    def test_severe_reaches_the_fix_brief_once_accepted(self):
        data = build([by_file("db.py")])
        review_consolidation.set_triage(data, "F1", "accepted")
        brief = review_consolidation.render_fix_brief(data)
        self.assertIn("db.py:40", brief)
        self.assertIn("- Category: pre-existing", brief)

    def test_markdown_shows_the_category(self):
        text = review_consolidation.render_consolidation(build([by_file("db.py")]))
        self.assertIn("### F1 [CRITICAL] pre-existing", text)
        self.assertNotIn("## Pre-existing, not triaged", text)


class TestEarlierTriage(unittest.TestCase):
    """A finding the last report holds is not set apart by a later "pre-existing"."""

    def last_with(self, triage: str) -> Dict[str, Any]:
        last = build([dict(by_file("util.py"), category="correctness")])
        review_consolidation.set_triage(last, "F1", triage, "noted")
        return last

    def test_accepted_is_carried(self):
        data = build([by_file("util.py")], last=self.last_with("accepted"), scope="fix")
        self.assertEqual(data["pre_existing"], [])
        (entry,) = data["findings"]
        self.assertEqual(entry["triage"], "accepted")
        self.assertEqual(entry["triage_note"], "noted")
        self.assertIn("triage_set_at", entry)
        self.assertEqual(review_consolidation.accepted_findings(data), [entry])
        decision = review_loop.decide(True, data, {}, BLOCKING, 1, 2)
        self.assertFalse(decision.finish)
        self.assertEqual(decision.wanted, [entry])

    def test_untriaged_is_kept(self):
        data = build([by_file("util.py")], last=self.last_with("needs-investigation"))
        self.assertEqual(data["pre_existing"], [])
        self.assertEqual(data["findings"][0]["triage"], "needs-investigation")

    def test_rejected_is_set_apart(self):
        data = build([by_file("util.py")], last=self.last_with("rejected"))
        self.assertEqual(data["findings"], [])
        self.assertEqual([f["id"] for f in data["pre_existing"]], ["P1"])
        self.assertNotIn("triage", data["pre_existing"][0])

    def test_merged_wording_matches(self):
        """The reviewer may re-report it in the words of any report merged into it."""
        short = dict(by_file("util.py"), category="correctness")
        longer = dict(short, reviewer="r2", problem=short["problem"] + " it is given")
        last = build([short, longer])
        (merged,) = last["findings"]
        self.assertEqual(merged["problem"], longer["problem"])
        review_consolidation.set_triage(last, "F1", "accepted")
        again = dict(short, category="pre-existing")
        data = build([again], last=last)
        self.assertEqual(data["pre_existing"], [])
        (entry,) = data["findings"]
        self.assertEqual(entry["triage"], "accepted")

    def test_unrelated_is_still_set_apart(self):
        data = build([by_file("names.py")], last=self.last_with("accepted"))
        self.assertEqual(data["findings"], [])
        self.assertEqual([f["file"] for f in data["pre_existing"]], ["names.py"])

    def test_recognised_report_merged_with_new_wording(self):
        """Merged with a wording the last report never saw, it keeps the decision it matched."""
        again = by_file("util.py")
        longer = dict(again, reviewer="r2", category="correctness", problem=again["problem"] + " it is given")
        data = build([again, longer], last=self.last_with("accepted"), scope="fix")
        self.assertEqual(data["pre_existing"], [])
        (entry,) = data["findings"]
        self.assertEqual(entry["problem"], longer["problem"])
        self.assertEqual(entry["triage"], "accepted")
        self.assertEqual(entry["triage_note"], "noted")
        self.assertIn("util.py:7", review_consolidation.render_fix_brief(data))
        self.assertFalse(review_loop.decide(True, data, {}, BLOCKING, 1, 2).finish)

    def test_new_words_at_the_same_place(self):
        """A reviewer seldom repeats its words: the same file and nearby lines keep it in triage.

        Kept, but undecided: standing near an accepted finding does not make
        it that finding, so it never reaches the fix brief untriaged.
        """
        reworded = dict(by_file("util.py"), line="9", problem=REWORDED)
        data = build([reworded], last=self.last_with("accepted"), scope="fix")
        self.assertEqual(data["pre_existing"], [])
        (entry,) = data["findings"]
        self.assertEqual(entry["triage"], "needs-triage")
        self.assertEqual(entry.get("triage_note", ""), "")
        self.assertNotIn("triage_set_at", entry)
        self.assertEqual(review_consolidation.accepted_findings(data), [])
        self.assertNotIn(REWORDED, review_consolidation.render_fix_brief(data))
        self.assertFalse(review_loop.decide(True, data, {}, BLOCKING, 1, 2).finish)

    def test_duplicate_nearby_does_not_hide_a_new_report(self):
        reworded = dict(by_file("util.py"), line="9", problem=REWORDED)
        data = build([reworded], last=self.last_with("duplicate"))
        (entry,) = data["findings"]
        self.assertEqual(entry["triage"], "needs-triage")

    def test_two_without_a_line_are_not_the_same_place(self):
        """Sharing a file is not sharing a place: a line-less report is set apart as usual."""
        last = build([dict(by_file("util.py"), category="correctness", line="n/a")])
        review_consolidation.set_triage(last, "F1", "accepted", "noted")
        unrelated = dict(by_file("util.py"), line="n/a", problem=REWORDED)
        data = build([unrelated], last=last)
        self.assertEqual(data["findings"], [])
        self.assertEqual([f["id"] for f in data["pre_existing"]], ["P1"])

    def test_rejected_and_accepted_wordings_merged_are_left_to_triage(self):
        """Reports matching a rejected and an accepted finding disagree: neither decision is carried.

        Carrying the rejection could drop a real issue; carrying the
        acceptance could send rejected noise to the fixer. Triage decides.
        """
        key = review_consolidation.finding_key
        noise = {"file": "util.py", "problem": "the helper swallows every exception"}
        real = {"file": "util.py", "problem": "the helper ignores the timeout"}
        previous = {
            key(noise): dict(noise, triage="rejected", triage_note="noise"),
            key(real): dict(real, triage="accepted", triage_note="real"),
        }
        merged = [{"problem": noise["problem"]}, {"problem": real["problem"]}]
        entry = dict(noise, key=key(noise), merged_reports=merged)
        self.assertIsNone(review_consolidation._prior_triage(entry, previous))
        entry = dict(real, key=key(real), merged_reports=merged)
        self.assertIsNone(review_consolidation._prior_triage(entry, previous))

    def test_own_rejected_wording_alone_stays_rejected(self):
        key = review_consolidation.finding_key
        noise = {"file": "util.py", "problem": "the helper swallows every exception"}
        previous = {key(noise): dict(noise, triage="rejected", triage_note="noise")}
        found = review_consolidation._prior_triage(dict(noise, key=key(noise)), previous)
        assert found is not None
        self.assertEqual(found["triage"], "rejected")

    def test_new_words_elsewhere_are_set_apart(self):
        far = dict(by_file("util.py"), line="40", problem=REWORDED)
        data = build([far], last=self.last_with("accepted"))
        self.assertEqual(data["findings"], [])
        self.assertEqual([f["id"] for f in data["pre_existing"]], ["P1"])

    def test_rejected_place_does_not_hold(self):
        reworded = dict(by_file("util.py"), line="9", problem=REWORDED)
        data = build([reworded], last=self.last_with("rejected"))
        self.assertEqual(data["findings"], [])
        self.assertEqual([f["id"] for f in data["pre_existing"]], ["P1"])

    def test_own_key_wins_over_merged_wording(self):
        """Another finding's merged wording never takes a finding's triage; odd reports are skipped."""
        problem = "the helper swallows every exception"
        own = {"file": "util.py", "line": "7", "problem": problem}
        own.update(triage="accepted", triage_note="own")
        other = {
            "file": "util.py",
            "line": "60",
            "problem": "a longer wording of another issue",
            "triage": "rejected",
            "triage_note": "other",
            "merged_reports": [{"problem": problem}, "x", None],
        }
        last = {"findings": [own, other]}
        again = dict(by_file("util.py"), category="correctness")
        (entry,) = build([again], last=last)["findings"]
        self.assertEqual(entry["triage"], "accepted")
        self.assertEqual(entry["triage_note"], "own")

    def test_malformed_merged_reports_are_skipped(self):
        last = {"findings": [{"file": "util.py", "line": "7", "problem": "x", "merged_reports": ["x", None]}]}
        data = build([by_file("names.py")], last=last)
        self.assertEqual([f["file"] for f in data["pre_existing"]], ["names.py"])


class TestSeverityForms(unittest.TestCase):
    """Where an odd or missing severity puts a pre-existing finding."""

    def test_missing_severity_is_medium(self):
        finding = by_file("util.py")
        del finding["severity"]
        data = build([finding])
        self.assertEqual(data["findings"], [])
        self.assertEqual([f["id"] for f in data["pre_existing"]], ["P1"])

    def test_capitalised_severe_stays(self):
        data = build([dict(by_file("util.py"), severity="High")])
        self.assertEqual(data["pre_existing"], [])
        self.assertEqual([f["file"] for f in data["findings"]], ["util.py"])

    def test_severe_rejected_stays_rejected(self):
        last = build([dict(by_file("db.py"), category="correctness")])
        review_consolidation.set_triage(last, "F1", "rejected", "by design")
        data = build([by_file("db.py")], last=last)
        self.assertEqual(data["pre_existing"], [])
        (entry,) = data["findings"]
        self.assertEqual(entry["triage"], "rejected")
        self.assertEqual(entry["triage_note"], "by design")
        self.assertEqual(review_consolidation.unresolved_blocking(data, BLOCKING), [])


class TestMixedAtOneLocation(unittest.TestCase):
    """One reviewer's pre-existing and another's correctness at one place stay two entries."""

    def test_mixed_reports_stay_separate(self):
        pre = dict(by_file("util.py"), reviewer="r1")
        normal = dict(
            pre,
            reviewer="r2",
            severity="high",
            category="correctness",
            problem="the new retry loop never stops after the helper swallows the exception",
        )
        data = build([pre, normal])
        (finding,) = data["findings"]
        self.assertEqual(finding["id"], "F1")
        self.assertEqual(finding["reported_by"], ["r2"])
        self.assertEqual(finding["category"], "correctness")
        (entry,) = data["pre_existing"]
        self.assertEqual(entry["id"], "P1")
        self.assertEqual(entry["reported_by"], ["r1"])
        self.assertEqual(review_consolidation.unresolved_blocking(data, BLOCKING), [finding])
        self.assertNotEqual(
            review_consolidation.findings_signature(data), review_consolidation.NO_FINDINGS_SIGNATURE
        )
        self.assertEqual(
            review_consolidation.findings_signature(build([pre])), review_consolidation.NO_FINDINGS_SIGNATURE
        )


class TestOneIssueTwoLabels(unittest.TestCase):
    """The same words at the same place are one entry, whichever side of the split it lands."""

    def test_pre_existing_and_normal_merge_into_a_finding(self):
        pre = by_file("util.py")
        normal = dict(pre, reviewer="r2", category="correctness")
        data = build([pre, normal])
        self.assertEqual(data["pre_existing"], [])
        (entry,) = data["findings"]
        self.assertEqual(entry["reported_by"], ["r1", "r2"])
        self.assertEqual(data["counts"]["duplicates_merged"], 1)
        self.assertEqual(
            [report["category"] for report in entry["merged_reports"]], ["pre_existing", "correctness"]
        )

    def test_high_and_medium_pre_existing_merge_into_a_finding(self):
        medium = by_file("util.py")
        high = dict(medium, reviewer="r2", severity="high")
        data = build([medium, high])
        self.assertEqual(data["pre_existing"], [])
        (entry,) = data["findings"]
        self.assertEqual(entry["severity"], "high")
        self.assertEqual(sorted(entry["reported_by"]), ["r1", "r2"])

    def test_medium_and_low_pre_existing_merge_apart(self):
        medium = by_file("util.py")
        low = dict(medium, reviewer="r2", severity="low")
        data = build([medium, low])
        self.assertEqual(data["findings"], [])
        (entry,) = data["pre_existing"]
        self.assertEqual(entry["id"], "P1")
        self.assertEqual(entry["severity"], "medium")
        self.assertEqual(entry["reported_by"], ["r1", "r2"])


class TestDesignRound(unittest.TestCase):
    """The design prompt does not offer the category, and a design round sets nothing apart."""

    def test_design_report_saying_pre_existing_is_triaged(self):
        report = (
            "## Finding\n- Severity: medium\n- File: plan.md#Proposed Change\n- Line: n/a\n"
            "- Category: pre-existing\n- Problem: the plan keeps the old cache\n- Impact: stale reads\n"
            "- Evidence: keep the cache\n- Fix: drop it\n"
        )
        meta = {"sha256": "abc", "files": ["plan.md"], "strategy": "plan"}
        data = review_consolidation.consolidation_from(meta, {}, [], parse_findings(report, "d1"))
        self.assertEqual(data["pre_existing"], [])
        (entry,) = data["findings"]
        self.assertEqual(entry["id"], "F1")
        self.assertEqual(entry["triage"], "needs-triage")
        self.assertEqual(data["counts"]["findings_total"], 1)


class TestConfiguredBlocking(unittest.TestCase):
    """A severity ``review.blocking_severities`` names is not set apart on the label alone."""

    def test_medium_blocking_keeps_medium(self):
        meta = {"sha256": "abc", "files": [], "scope": "whole"}
        data = review_consolidation.consolidation_from(
            meta, {}, [], mild_pre_existing(), blocking=("critical", "high", "medium")
        )
        self.assertEqual([f["file"] for f in data["findings"]], ["util.py"])
        self.assertEqual([f["file"] for f in data["pre_existing"]], ["names.py"])
        blocking = ("critical", "high", "medium")
        self.assertEqual(len(review_consolidation.unresolved_blocking(data, blocking)), 1)

    def test_default_sets_medium_apart(self):
        self.assertEqual(len(build(mild_pre_existing())["pre_existing"]), 2)


class TestNearEdges(unittest.TestCase):
    """``_near``'s 10 lines, at the edge, with ranges and with a line on one side only."""

    def last_at(self, line: str) -> Dict[str, Any]:
        last = build([dict(by_file("util.py"), category="correctness", line=line)])
        review_consolidation.set_triage(last, "F1", "accepted")
        return last

    def test_edges(self):
        for old, new, kept in (
            ("7", "17", True),
            ("7", "18", False),
            ("17", "7", True),
            ("7", "5-9", True),
            ("5-9", "7", True),
            ("7", "16-20", True),
            ("7", "18-20", False),
            ("7", "n/a", False),
            ("n/a", "7", False),
        ):
            with self.subTest(old=old, new=new):
                reworded = dict(by_file("util.py"), line=new, problem=REWORDED)
                data = build([reworded], last=self.last_at(old))
                if kept:
                    (entry,) = data["findings"]
                    self.assertEqual(entry["triage"], "needs-triage")
                    self.assertEqual(data["pre_existing"], [])
                else:
                    self.assertEqual(data["findings"], [])
                    self.assertEqual(len(data["pre_existing"]), 1)


class TestPriorRejection(unittest.TestCase):
    """A rejection is carried by a merged wording only when every merged report was rejected."""

    PROBLEM = "the session token is written to the debug log"

    def last_rejected(self) -> Dict[str, Any]:
        old = dict(by_file("util.py"), category="security", problem=self.PROBLEM)
        last = build([old])
        review_consolidation.set_triage(last, "F1", "rejected", "debug only")
        return last

    def test_new_report_merged_with_a_rejected_one_is_not_rejected(self):
        again = dict(by_file("util.py"), category="security", problem=self.PROBLEM)
        new = dict(again, reviewer="r2", severity="high", problem=self.PROBLEM + " in production builds too")
        data = build([again, new], last=self.last_rejected())
        (entry,) = data["findings"]
        self.assertEqual(entry["problem"], new["problem"])
        self.assertEqual(entry["triage"], "needs-triage")
        self.assertEqual(entry["triage_note"], "")
        self.assertEqual(review_consolidation.unresolved_blocking(data, BLOCKING), [entry])

    def test_every_report_rejected_stays_rejected(self):
        key = review_consolidation.finding_key
        first = {"file": "util.py", "problem": self.PROBLEM}
        second = {"file": "util.py", "problem": self.PROBLEM + " in production builds too"}
        previous = {
            key(first): dict(first, triage="rejected", triage_note="debug only"),
            key(second): dict(second, triage="rejected", triage_note="debug only"),
        }
        merged = [{"problem": first["problem"]}, {"problem": second["problem"]}]
        # Its own wording a key of neither, every merged wording rejected: still rejected.
        unseen = key({"file": "util.py", "problem": "unseen"})
        entry = {"file": "util.py", "key": unseen, "merged_reports": merged}
        found = review_consolidation._prior_triage(entry, previous)
        assert found is not None
        self.assertEqual(found["triage"], "rejected")
        # One more report in words never seen: not rejected.
        entry["merged_reports"] = [*merged, {"problem": "unseen"}]
        self.assertIsNone(review_consolidation._prior_triage(entry, previous))


class TestCarryAfterMerging(unittest.TestCase):
    """What a merged entry carries, whichever report gave it its wording."""

    LONG = "the session token is written to the debug log in production builds too"
    SHORT = "the session token is written to the debug log"

    def last_with(self, problem: str, triage: str) -> Dict[str, Any]:
        last = build([dict(by_file("util.py"), category="security", problem=problem)])
        review_consolidation.set_triage(last, "F1", triage, "noted")
        return last

    def test_rejected_longer_wording_with_a_new_shorter_report_is_not_rejected(self):
        """The rejected report gives the merged entry its key; the new one still leaves it to triage."""
        again = dict(by_file("util.py"), category="security", problem=self.LONG)
        new = dict(again, reviewer="r2", severity="high", problem=self.SHORT)
        data = build([again, new], last=self.last_with(self.LONG, "rejected"))
        (entry,) = data["findings"]
        self.assertEqual(entry["problem"], self.LONG)
        self.assertEqual(entry["triage"], "needs-triage")
        self.assertEqual(review_consolidation.unresolved_blocking(data, BLOCKING), [entry])

    def test_accepted_beside_new_words_carries_accepted(self):
        """An accepted report merged with one in new words keeps it accepted, and so blocking."""
        key = review_consolidation.finding_key
        accepted = {"file": "util.py", "problem": self.SHORT}
        previous = {key(accepted): dict(accepted, triage="accepted", triage_note="real")}
        entry = dict(
            accepted,
            key=key({"file": "util.py", "problem": self.LONG}),
            merged_reports=[{"problem": self.SHORT}, {"problem": self.LONG}],
        )
        found = review_consolidation._prior_triage(entry, previous)
        assert found is not None
        self.assertEqual(found["triage"], "accepted")

    def test_each_report_place_is_looked_up(self):
        """Two pre-existing reports merged: one near an accepted finding keeps the entry in triage."""
        earlier = dict(by_file("util.py"), category="correctness", line="24", problem="a different thing")
        last = build([earlier])
        review_consolidation.set_triage(last, "F1", "accepted", "noted")
        first = dict(by_file("util.py"), line="7")
        second = dict(first, reviewer="r2", line="15")
        data = build([first, second], last=last)
        self.assertEqual(data["pre_existing"], [])
        (entry,) = data["findings"]
        self.assertEqual(entry["triage"], "needs-triage")
        self.assertEqual(len(entry.get("merged_reports") or []), 2)


class TestOpenMatchInteractions(unittest.TestCase):
    def test_own_rejected_wording_beside_an_open_finding_is_set_apart(self):
        """Its own wording rejected, it is not looked for by place, even beside an accepted finding."""
        rejected = dict(by_file("util.py"), category="correctness")
        accepted = dict(rejected, line="12", problem="the retry count is never reset")
        last = build([rejected, dict(accepted, reviewer="r1")])
        ids = {f["problem"]: f["id"] for f in last["findings"]}
        review_consolidation.set_triage(last, ids[rejected["problem"]], "rejected", "noise")
        review_consolidation.set_triage(last, ids[accepted["problem"]], "accepted")
        data = build([by_file("util.py")], last=last)
        self.assertEqual(data["findings"], [])
        self.assertEqual([f["file"] for f in data["pre_existing"]], ["util.py"])

    def test_duplicate_candidates_never_pair_a_p_with_an_f(self):
        pre = dict(by_file("util.py"), evidence="`helper_call()` swallows")
        normal = dict(
            pre,
            reviewer="r2",
            severity="high",
            category="correctness",
            line="9",
            problem="retries never stop",
            evidence="`helper_call()` loops",
        )
        data = build([pre, normal])
        self.assertEqual([f["id"] for f in data["findings"]], ["F1"])
        self.assertEqual([f["id"] for f in data["pre_existing"]], ["P1"])
        self.assertEqual(data["duplicate_candidates"], [])
        self.assertNotIn("possible_duplicates", data["findings"][0])

    def test_reviewer_with_only_pre_existing_findings(self):
        runs = [
            {"id": "r1", "status": "ok", "findings": 2, "snapshot": "abc"},
            {"id": "r2", "status": "ok", "findings": 0, "snapshot": "abc"},
        ]
        meta = {"sha256": "abc", "files": [], "scope": "whole"}
        data = review_consolidation.consolidation_from(meta, {}, runs, mild_pre_existing())
        self.assertEqual(data["reviewers"], runs)
        self.assertEqual(data["counts"]["findings_total"], 0)
        self.assertEqual(data["counts"]["reviewers_ok"], 2)
        self.assertEqual(data["counts"]["reviewers_total"], 2)
        self.assertEqual(len(data["pre_existing"]), 2)
        text = review_consolidation.render_consolidation(data)
        self.assertIn("ok [None] r1 / None / unknown model -- 2 finding(s)", text)


class TestNotTriageMaterial(unittest.TestCase):
    def test_not_triage_material(self):
        data = build(mild_pre_existing())
        for entry in data["pre_existing"]:
            # Even a stray triage field must not make one blocking.
            entry["triage"] = "accepted"
        self.assertEqual(review_consolidation.unresolved_blocking(data, ("medium", "low")), [])
        self.assertEqual(
            review_consolidation.findings_signature(data), review_consolidation.NO_FINDINGS_SIGNATURE
        )
        brief = review_consolidation.render_fix_brief(data)
        self.assertIn("Nothing to fix", brief)
        self.assertNotIn("util.py", brief)

    def test_signature_matches_a_round_without_them(self):
        with_pre = build(parsed())
        without = build([f for f in parsed() if f["file"] not in ("util.py", "names.py")])
        self.assertEqual(
            review_consolidation.findings_signature(with_pre),
            review_consolidation.findings_signature(without),
        )


class TestOnlyPreExistingRoundIsClean(unittest.TestCase):
    def test_only_pre_existing_round_is_clean(self):
        whole = build(mild_pre_existing(), scope="whole")
        decision = review_loop.decide(True, whole, {}, BLOCKING, 1, 2)
        self.assertTrue(decision.finish)
        self.assertEqual(decision.wanted, [])

        fix = build(mild_pre_existing(), scope="fix")
        decision = review_loop.decide(True, fix, {}, BLOCKING, 1, 2)
        self.assertTrue(decision.recheck_next)
        self.assertEqual(decision.next_review, "whole")
        self.assertEqual(decision.wanted, [])


class TestMarkdownSection(unittest.TestCase):
    def test_markdown_section(self):
        text = review_consolidation.render_consolidation(build(parsed()))
        self.assertIn("## Pre-existing, not triaged", text)
        self.assertIn("- Pre-existing (not triaged): 2", text)
        self.assertIn("### P1 [MEDIUM]", text)
        self.assertIn("- Location: `util.py`:7", text)
        self.assertIn("- Reported by: r1", text)
        self.assertIn("- Recommended fix: log and re-raise", text)
        self.assertLess(text.index("## Findings"), text.index("## Pre-existing, not triaged"))
        section = text[text.index("## Pre-existing, not triaged") :]
        self.assertNotIn("Triage:", section)

    def test_absent_when_empty(self):
        text = review_consolidation.render_consolidation(build([parsed()[0]]))
        self.assertNotIn("Pre-existing", text)

    def test_report_without_the_key_renders_unchanged(self):
        data = build([parsed()[0]])
        old = dict(data)
        del old["pre_existing"]
        self.assertEqual(
            review_consolidation.render_consolidation(old),
            review_consolidation.render_consolidation(data),
        )


class TestTemplate(unittest.TestCase):
    def test_template_mentions_pre_existing(self):
        category = [line for line in REVIEW_PROMPT_TEMPLATE.splitlines() if line.startswith("- Category:")]
        self.assertEqual(len(category), 1)
        self.assertIn("pre-existing", category[0])
        self.assertIn("did not cause, worsen or expose it", category[0])
        self.assertNotIn("pre-existing", DESIGN_REVIEW_PROMPT_TEMPLATE)

    def test_template_category_is_parsed_as_pre_existing(self):
        finding = parse_findings(REPORT, "r1")[1]
        self.assertEqual(finding["category"], "pre-existing")


class TestFixRoundContext(IsolatedCase):
    """On a fix-only round, "this change" is said to be the whole change."""

    def test_incremental_round_says_whole_change(self):
        workspace = self.cli_workspace()
        text = render_round_context(workspace, {"incremental_from": "a" * 64})
        self.assertIn('"this change" means the whole change, not this fix', text)
        self.assertNotIn("this change", render_round_context(workspace, {}))


class TestCli(IsolatedCase):
    """``review triage`` and ``review show`` over a report with ``pre_existing``."""

    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace()
        self.data = build(parsed())
        ws.write_json(self.workspace.consolidated_json_path, self.data)

    def stored(self) -> Dict[str, Any]:
        return ws.read_json(self.workspace.consolidated_json_path, {})

    def test_a_p_id_cannot_be_triaged(self):
        code, _, err = run_cli("review", "triage", "P1", "--status", "rejected")
        self.assertNotEqual(code, 0)
        self.assertIn("no finding with id 'P1'", err)
        self.assertEqual(self.stored()["pre_existing"], self.data["pre_existing"])
        self.assertNotIn("triage", self.stored()["pre_existing"][0])

    def test_show_accepted_leaves_pre_existing(self):
        code, _, err = run_cli("review", "triage", "F1", "--status", "rejected")
        self.assertEqual(code, 0, err)
        code, out, err = run_cli("review", "show", "--accepted", "--json")
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(shown["findings"], [])
        self.assertEqual(shown["pre_existing"], self.data["pre_existing"])

    def test_legacy_report_shows_and_triages(self):
        legacy = dict(self.data)
        del legacy["pre_existing"]
        ws.write_json(self.workspace.consolidated_json_path, legacy)
        code, out, err = run_cli("review", "show")
        self.assertEqual(code, 0, err)
        self.assertIn("## Findings", out)
        self.assertNotIn("Pre-existing", out)
        code, out, err = run_cli("review", "show", "--json")
        self.assertEqual(code, 0, err)
        self.assertNotIn("pre_existing", json.loads(out))
        code, _, err = run_cli("review", "triage", "F1", "--status", "rejected", "--note", "intended")
        self.assertEqual(code, 0, err)
        stored = self.stored()
        self.assertEqual(stored["findings"][0]["triage"], "rejected")
        self.assertNotIn("pre_existing", stored)


@unittest.skipUnless(has_git(), "git is required")
class TestConsolidateCli(IsolatedCase):
    """``review consolidate --json`` over a real report: ``pre_existing`` beside ``findings``."""

    def test_consolidate_json(self):
        self.init_git_repo()
        self.write("app.py", "x = 1\n")
        self.commit_all("init")
        self.write("app.py", "x = 2\n")
        run_cli("config", "setup", "--defaults")
        for seat in ("claude-general", "codex-general", "claude-security", "claude-test"):
            run_cli("reviewer", "remove", seat)
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        code, _, err = run_cli("review", "snapshot")
        self.assertEqual(code, 0, err)
        workspace = self.cli_workspace()
        ws.write_text(workspace.reviewer_report_path("m1"), REPORT)
        code, out, err = run_cli("review", "consolidate", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual({f["file"] for f in data["findings"]}, {"app.py", "db.py", "auth.py"})
        self.assertEqual(
            [(f["id"], f["file"]) for f in data["pre_existing"]], [("P1", "util.py"), ("P2", "names.py")]
        )
        self.assertEqual(data["counts"]["findings_total"], 3)
        stored = ws.read_json(workspace.consolidated_json_path, {})
        self.assertEqual(stored["pre_existing"], data["pre_existing"])

    def test_configured_blocking_reaches_consolidate(self):
        """``review.re_review_severities`` naming medium keeps a medium pre-existing finding in triage."""
        self.init_git_repo()
        self.write("app.py", "x = 1\n")
        self.commit_all("init")
        self.write("app.py", "x = 2\n")
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "review.re_review_severities", "[critical, high, medium]")
        for seat in ("claude-general", "codex-general", "claude-security", "claude-test"):
            run_cli("reviewer", "remove", seat)
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        code, _, err = run_cli("review", "snapshot")
        self.assertEqual(code, 0, err)
        ws.write_text(self.cli_workspace().reviewer_report_path("m1"), REPORT)
        code, out, err = run_cli("review", "consolidate", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIn("util.py", {f["file"] for f in data["findings"]})
        self.assertEqual([f["file"] for f in data["pre_existing"]], ["names.py"])


if __name__ == "__main__":
    unittest.main()
