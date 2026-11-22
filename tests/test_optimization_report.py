"""What ``optimization report`` counts of the re-review cut and of the tiers the CLI chose."""

from __future__ import annotations

import ast
import unittest
from typing import Any, Dict, List, Optional

import helpers  # noqa: F401  (puts scripts/ on the path)

from orchestrator import auto_tier, cli_workflow, tier_record
from orchestrator import optimization as opt
from orchestrator import optimization_render as opt_render
from orchestrator import optimization_report as opt_report


def review_round(records: List[Dict[str, Any]], status: str = "ok", billed: int = 1000) -> Dict[str, Any]:
    """A code round that ran two reviewers and recorded ``records`` as its conditional decisions."""
    return {
        "stage": "review",
        "status": status,
        "optimization": {"level": "balanced", "gate": "allow", "test_status": "ok", "conditional": records},
        "reviewers": [
            {"id": "m1", "status": "ok", "usage": {"billed_tokens": billed}},
            {"id": "m2", "status": "ok", "usage": {"billed_tokens": billed}},
        ],
    }


def record(name: str, when: str, runs: bool) -> Dict[str, Any]:
    return {"id": name, "when": when, "runs": runs, "reason": "because"}


def run_event(
    stage: str,
    tier: Optional[str],
    source: Optional[str],
    status: str = "ok",
    reason: str = "a reason",
    answered: bool = True,
    cause: str = "light",
) -> Dict[str, Any]:
    """An end event of ``run``; ``source`` None records no ``tier_choice``, as a run without one does."""
    event: Dict[str, Any] = {"stage": stage, "status": status, "tier": tier, "answered": answered}
    if source is not None:
        event["tier_choice"] = {"tier": tier, "source": source, "reason": reason, "cause": cause}
    return event


class ReReviewCounts(unittest.TestCase):
    def test_re_review_records_are_counted_apart_from_conditional_and_relevance(self):
        events = [
            review_round(
                [
                    record("claude-test", opt.WHEN_RE_REVIEW, False),
                    record("claude-security", opt.WHEN_RE_REVIEW, True),
                    record("perf", "paths", False),
                ]
            ),
            review_round([record("claude-test", opt.WHEN_RE_REVIEW, False)]),
        ]
        report = opt_report.summarise_rounds(events)
        re_review = report["re_review"]
        self.assertEqual((re_review["judged"], re_review["added"], re_review["left_out"]), (3, 1, 2))
        self.assertEqual(re_review["left_out_by_level"], {"balanced": 2})
        # 4,000 billed over 4 reviewer runs: 1,000 a seat, two seats left out.
        self.assertEqual(re_review["estimated_saving"], 2000)
        self.assertEqual(report["conditional"], {"added": 0, "left_out": 1, "declared_rounds": 0})
        self.assertEqual(report["relevance"]["judged"], 0)

    def test_a_refused_round_adds_nothing_to_re_review(self):
        events = [review_round([record("claude-test", opt.WHEN_RE_REVIEW, False)], status=opt.REFUSED)]
        report = opt_report.summarise_rounds(events)
        self.assertEqual(report["re_review"]["judged"], 0)
        self.assertEqual(report["conditional"]["left_out"], 0)

    def test_old_events_count_as_zero(self):
        report = opt_report.summarise_rounds([review_round([])])
        self.assertEqual(
            report["re_review"],
            {"judged": 0, "added": 0, "left_out": 0, "left_out_by_level": {}, "estimated_saving": 0},
        )

    def test_relevance_counts_takes_the_when_to_count(self):
        records = [record("a", opt.WHEN_RELEVANCE, False), record("b", opt.WHEN_RE_REVIEW, True)]
        events = [review_round(records)]
        self.assertEqual(opt_report._relevance_counts(events)["judged"], 1)
        self.assertEqual(opt_report._relevance_counts(events)["left_out"], 1)
        counted = opt_report._relevance_counts(events, when=opt.WHEN_RE_REVIEW)
        self.assertEqual((counted["judged"], counted["added"], counted["left_out"]), (1, 1, 0))


class TierChoiceCounts(unittest.TestCase):
    def test_sources_tiers_failures_and_retries(self):
        first = [
            run_event("implementer", "light", tier_record.SOURCE_AUTO, status="failed"),
            run_event("implementer", None, tier_record.SOURCE_AUTO, reason="any wording", cause="retry"),
            # The old sentence alone is not a retry: only the cause is counted.
            run_event("implementer", None, "auto", reason=tier_record.RETRY_REASON, cause="size"),
            run_event("implementer", "light", tier_record.SOURCE_EXPLICIT, answered=False),
        ]
        second = [
            run_event("review_fixer", None, tier_record.SOURCE_OFF),
            run_event("review_fixer", "light", tier_record.SOURCE_AUTO),
            # No choice recorded: no light tier, or from before the CLI chose.
            run_event("review_fixer", None, None),
            run_event("architect", None, None),
        ]
        tiers = opt_report.tier_choices([first, second])
        self.assertEqual(sorted(tiers), ["implementer", "review_fixer"])
        implementer = tiers["implementer"]
        self.assertEqual(implementer["runs"], 4)
        self.assertEqual(implementer["by_source"], {"auto": 3, "explicit": 1, "off": 0})
        self.assertEqual(implementer["by_tier"], {"light": 2, opt_report.OWN_MODEL: 2})
        self.assertEqual(implementer["light_failed"], 2)
        self.assertEqual(implementer["retried_on_role_model"], 1)
        fixer = tiers["review_fixer"]
        self.assertEqual(fixer["runs"], 2)
        self.assertEqual(fixer["by_source"], {"auto": 1, "explicit": 0, "off": 1})
        self.assertEqual(fixer["by_tier"], {"light": 1, opt_report.OWN_MODEL: 1})
        self.assertEqual((fixer["light_failed"], fixer["retried_on_role_model"]), (0, 0))

    def test_nothing_recorded_is_zero(self):
        unchosen = [[run_event("implementer", None, None)]]
        for tiers in (opt_report.tier_choices([]), opt_report.tier_choices(unchosen)):
            with self.subTest(tiers=tiers):
                for group in tiers.values():
                    self.assertEqual(group["runs"], 0)
                    self.assertEqual(group["by_tier"], {})
                    self.assertEqual(sum(group["by_source"].values()), 0)


def judge_kind(judge: Optional[bool], rules: Optional[bool], basis: str = "relevance") -> Dict[str, Any]:
    return {"judge": judge, "rules": rules, "basis": basis, "seats": [], "agrees": None}


def judged_round(
    round_id: str,
    sha: str,
    status: str,
    kinds: Optional[Dict[str, Dict[str, Any]]] = None,
    risk: Optional[str] = None,
    high: Optional[Dict[str, Any]] = None,
    usage: Optional[Dict[str, Any]] = None,
    duration: Optional[float] = None,
) -> Dict[str, Any]:
    """A finished code round whose event carries a risk judge record."""
    event = review_round([])
    event["round_id"] = round_id
    for run in event["reviewers"]:
        run["snapshot"] = sha
    blank = {kind: judge_kind(None, None, "none") for kind in opt_report.JUDGED_KINDS}
    event["risk_judge"] = {
        "mode": "record",
        "status": status,
        "risk": risk,
        "reason": "",
        "usage": usage,
        "duration": duration,
        "high_risk": high or {"judge": None, "rules": False, "declared": False, "agrees": None},
        "kinds": dict(blank, **(kinds or {})),
    }
    return event


def round_report(round_id: str, sha: str, findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "snapshot": {"sha256": sha + "0" * 52, "round_id": round_id},
        "iteration": 1,
        "findings": findings,
    }


def finding(key: str, severity: str, triage: str) -> Dict[str, Any]:
    return {"id": key, "key": key, "severity": severity, "triage": triage, "reported_by": ["m1"]}


class RiskJudgeSummary(unittest.TestCase):
    def inputs(self) -> List[Dict[str, Any]]:
        first = judged_round(
            "r1",
            "a" * 12,
            "ok",
            risk="high",
            high={"judge": True, "rules": True, "declared": False, "agrees": True},
            kinds={
                "security": judge_kind(True, True),
                "test": judge_kind(True, False),
                "architecture": judge_kind(False, False),
                "performance": judge_kind(True, None, "always"),
                "frontend": judge_kind(False, True, "paths"),
                "database": judge_kind(True, None, "always"),
                "backend": judge_kind(False, None, "none"),
            },
            usage={"billed_tokens": 100, "measured": True},
            duration=2.5,
        )
        # A re-run of r1 records `skipped` beside it: the round counts once.
        repeat = judged_round("r1", "a" * 12, "skipped")
        second = judged_round(
            "r2",
            "b" * 12,
            "ok",
            risk="low",
            high={"judge": False, "rules": False, "declared": True, "agrees": True},
            kinds={
                "security": judge_kind(False, False),
                "test": judge_kind(False, False),
                "architecture": judge_kind(False, False),
                "performance": judge_kind(False, None, "none"),
                "frontend": judge_kind(False, None, "none"),
                "database": judge_kind(False, None, "none"),
                "backend": judge_kind(False, None, "none"),
            },
            usage={"billed_tokens": 50, "measured": True},
            duration=1.5,
        )
        third = judged_round("r3", "c" * 12, "timed_out", duration=30.0)
        rounds = [
            round_report(
                "r1", "a" * 12, [finding("k1", "critical", "accepted"), finding("k2", "high", "rejected")]
            ),
            # k1 again, rebuilt as needs-triage: the acceptance above stands.
            round_report(
                "r2", "b" * 12, [finding("k1", "critical", "needs-triage"), finding("k3", "high", "accepted")]
            ),
            round_report("r3", "c" * 12, [finding("k4", "critical", "accepted")]),
        ]
        return [
            {
                "workflow": "issue-1",
                "stage": "code",
                "events": [first, repeat, second, third],
                "rounds": rounds,
            },
            {"workflow": "issue-2", "stage": "code", "events": [review_round([])], "rounds": []},
            {
                "workflow": "issue-1",
                "stage": "design",
                "events": [judged_round("d1", "d" * 12, "ok")],
                "rounds": [],
            },
        ]

    def test_risk_judge_summary_rows(self):
        summary = opt_report.risk_judge_summary(self.inputs())
        self.assertEqual(summary["rounds"], 3)
        self.assertEqual(summary["statuses"], {"ok": 2, "timed_out": 1})
        self.assertEqual(summary["risk"], {"high": 1, "low": 1})
        self.assertEqual(
            summary["high_risk"], {"both": 1, "rules_only": 0, "judge_only": 0, "neither": 1, "declared": 1}
        )
        kinds = summary["kinds"]
        self.assertEqual(list(kinds), list(opt_report.JUDGED_KINDS))

        def columns(kind: str) -> tuple:
            group = kinds[kind]
            return (
                group["both"],
                group["rules_only"],
                group["judge_only"],
                group["neither"],
                group["no_rule"],
                group["no_rule_by_basis"],
            )

        self.assertEqual(columns("security"), (1, 0, 0, 1, 0, {}))
        self.assertEqual(columns("test"), (0, 0, 1, 1, 0, {}))
        self.assertEqual(columns("architecture"), (0, 0, 0, 2, 0, {}))
        self.assertEqual(columns("frontend"), (0, 1, 0, 0, 1, {"none": 1}))
        self.assertEqual(columns("performance"), (0, 0, 0, 0, 2, {"always": 1, "none": 1}))
        self.assertEqual(columns("database"), (0, 0, 0, 0, 2, {"always": 1, "none": 1}))
        self.assertEqual(columns("backend"), (0, 0, 0, 0, 2, {"none": 2}))
        self.assertEqual(
            summary["usage"], {"runs": 3, "measured_runs": 2, "billed_tokens": 150, "seconds": 34.0}
        )
        # issue-2 has no record and the design entry is not read.
        self.assertEqual(
            summary["workflows"],
            [
                {
                    "workflow": "issue-1",
                    "rounds_judged": 2,
                    "rules_high_risk_rounds": 1,
                    "judge_high_risk_rounds": 1,
                    # k1 (r1, high-risk both ways) and k3 (r2); k4 is in r3, which the judge did not answer.
                    "accepted_critical_high": 2,
                    "on_rules_high_risk": 1,
                    "on_judge_high_risk": 1,
                }
            ],
        )

    def test_nothing_recorded_is_zero(self):
        summary = opt_report.risk_judge_summary(
            [{"workflow": "w", "stage": "code", "events": [review_round([])]}]
        )
        self.assertEqual((summary["rounds"], summary["statuses"], summary["workflows"]), (0, {}, []))

    def test_a_round_judged_on_its_re_run_is_reported_as_judged(self):
        """A round first recorded without a run is the one the re-run judged, as ``should_run`` says."""
        answered = {"security": judge_kind(True, True)}
        events = []
        for index, first in enumerate(("budget_spent", "refused", "unconfigured", "failed")):
            round_id = "r%d" % index
            events.append(judged_round(round_id, "a" * 12, first))
            events.append(judged_round(round_id, "a" * 12, "ok", kinds=answered, risk="low", duration=1.0))
            # A later re-run skips it, and is not the round's either.
            events.append(judged_round(round_id, "a" * 12, "skipped"))
        summary = opt_report.risk_judge_summary([{"workflow": "w", "stage": "code", "events": events}])
        self.assertEqual(summary["rounds"], 4)
        self.assertEqual(summary["statuses"], {"ok": 4})
        self.assertEqual(summary["kinds"]["security"]["both"], 4)
        self.assertEqual(summary["usage"]["runs"], 4)

    def test_a_failed_run_that_raised_counts_as_a_run(self):
        """Its attempt was spent though it has neither duration nor usage."""
        raised = judged_round("r1", "a" * 12, "failed")
        raised["risk_judge"]["attempted"] = True
        before_run = judged_round("r2", "b" * 12, "failed")
        summary = opt_report.risk_judge_summary(
            [{"workflow": "w", "stage": "code", "events": [raised, before_run]}]
        )
        self.assertEqual(summary["statuses"], {"failed": 2})
        self.assertEqual(summary["usage"]["runs"], 1)
        self.assertEqual(summary["usage"]["seconds"], 0.0)

    def test_report_renders_risk_judge_section(self):
        inputs = self.inputs()
        events = [event for item in inputs if item["stage"] == "code" for event in item["events"]]
        report = opt_report.summarise_rounds(events)
        report["scorecard"] = opt_report.reviewer_scorecard(inputs)
        report["architect_revisions"] = opt_report.architect_revisions([])
        report["tiers"] = {}
        without = opt_render.report_lines(report, "here")
        self.assertFalse(any(line.startswith("Risk judge") for line in without))

        report["risk_judge"] = opt_report.risk_judge_summary(inputs)
        lines = opt_render.report_lines(report, "here")
        start = lines.index("Risk judge (recorded, not acted on), code review rounds: 3")
        section = lines[start:]
        high = [line for line in section if line.split()[:2] == ["high", "risk"]]
        self.assertEqual(len(high), 1)
        self.assertIn(
            "both x1, rules only x0, judge only x0, neither x1; declared with --high-risk x1", high[0]
        )
        for kind in opt_report.JUDGED_KINDS:
            rows = [line for line in section if line.split()[:1] == [kind]]
            self.assertEqual(len(rows), 1, kind)
        backend = next(line for line in section if line.split()[:1] == ["backend"])
        self.assertIn("no rule x2 (none x2)", backend)
        self.assertEqual(opt_render._risk_judge_lines({}), [])
        self.assertEqual(opt_render._risk_judge_lines({"rounds": 0}), [])


class RecordVocabulary(unittest.TestCase):
    """The readers of the run log import the record vocabulary, not the module that decides."""

    def test_the_readers_do_not_load_the_deciding_module(self):
        for module in (opt_report, cli_workflow):
            with self.subTest(module=module.__name__):
                with open(module.__file__ or "", encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
                imported = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        imported.update(alias.name for alias in node.names)
                        imported.add(node.module or "")
                self.assertNotIn("auto_tier", imported)
                self.assertIn("tier_record", imported)

    def test_the_vocabulary_imports_nothing_from_the_package(self):
        with open(tier_record.__file__ or "", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        relative = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level]
        self.assertEqual(relative, [])
        # One public home: the deciding module does not re-export the vocabulary.
        self.assertFalse(set(auto_tier.__all__) & set(dir(tier_record)))

    def test_one_predicate_marks_a_run_s_end_event(self):
        self.assertTrue(tier_record.is_run_end({"stage": "implementer", "tier": None}))
        self.assertFalse(tier_record.is_run_end({"stage": "implementer", "status": "abandoned"}))
        self.assertFalse(tier_record.is_run_end(["tier"]))
        # `status` reads a run the same way: an event without `tier` does not clear the last choice.
        choice = {"tier": "light", "source": "auto", "reason": "r", "cause": "light"}
        events = [
            {"stage": "implementer", "tier": "light", "tier_choice": choice},
            {"stage": "implementer", "status": "abandoned"},
        ]
        self.assertEqual(cli_workflow._last_tier_choices(events)["implementer"], choice)


if __name__ == "__main__":
    unittest.main()
