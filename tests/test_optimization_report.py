"""What ``optimization report`` counts of the re-review cut and of the tiers the CLI chose."""

from __future__ import annotations

import ast
import unittest
from typing import Any, Dict, List, Optional

import helpers  # noqa: F401  (puts scripts/ on the path)

from orchestrator import auto_tier, cli_workflow, tier_record
from orchestrator import optimization as opt
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
