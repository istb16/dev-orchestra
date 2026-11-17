"""``auto_tier``: whether the implementer or the review fixer goes to its light tier."""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional, Sequence

import helpers  # noqa: F401  (puts scripts/ on the path)

from orchestrator import auto_tier, tier_record

LIGHT_TIERS = {"light": {"model": {"family": "small"}}}


def a_plan(files: List[str], units: str = "") -> str:
    lines = ["# Plan", "", "## Goal", "Something.", "", "## Files to Modify"]
    lines += ["- `%s`" % path for path in files]
    return "\n".join(lines) + "\n" + units


def units_block(entries: Dict[str, List[str]]) -> str:
    lines = ["", "## Units", "```yaml", "units:"]
    for name, files in entries.items():
        lines += ["  - name: %s" % name, "    kind: feature", "    files: [%s]" % ", ".join(files)]
    return "\n".join([*lines, "```", ""])


def implementer(
    plan_text: Optional[str],
    unit: Optional[str] = None,
    events: Optional[List[Dict[str, Any]]] = None,
    settings: Optional[Dict[str, Any]] = None,
    tiers: Optional[Dict[str, Any]] = None,
    literal_paths: Sequence[str] = (),
) -> Optional[tier_record.TierChoice]:
    return auto_tier.choose(
        "implementer",
        settings or {},
        LIGHT_TIERS if tiers is None else tiers,
        plan_text,
        unit,
        {},
        events or [],
        literal_paths,
    )


def fixer(
    findings: List[Dict[str, Any]], events: Optional[List[Dict[str, Any]]] = None
) -> tier_record.TierChoice:
    report = {"findings": findings}
    choice = auto_tier.choose("review_fixer", {}, LIGHT_TIERS, None, None, report, events or [])
    assert choice is not None
    return choice


def finding(name: str, severity: str, path: Any, triage: str = "accepted") -> Dict[str, Any]:
    return {"id": name, "severity": severity, "file": path, "triage": triage}


class ChooseImplementer(unittest.TestCase):
    def test_three_plain_files_go_light(self):
        choice = implementer(a_plan(["src/alpha.py", "src/beta.py", "tests/test_alpha.py"]))
        assert choice is not None
        self.assertEqual(choice.tier, "light")
        self.assertEqual(choice.source, "auto")
        self.assertEqual(choice.reason, "light: 3 files in Files to Modify, none high-risk")

    def test_four_files_run_the_role_s_model(self):
        choice = implementer(a_plan(["src/a.py", "src/b.py", "src/c.py", "src/d.py"]))
        assert choice is not None
        self.assertIsNone(choice.tier)
        self.assertEqual(choice.reason, "own model: 4 files in Files to Modify (more than 3)")

    def test_a_high_risk_file_runs_the_role_s_model(self):
        choice = implementer(a_plan(["src/auth/token.py"]))
        assert choice is not None
        self.assertIsNone(choice.tier)
        self.assertEqual(choice.reason, "own model: src/auth/token.py matches *auth*")

    def test_a_watched_instruction_path_is_high_risk(self):
        choice = implementer(a_plan(["CLAUDE.md"]), literal_paths=["CLAUDE.md"])
        assert choice is not None
        self.assertIsNone(choice.tier)
        self.assertIn("CLAUDE.md", choice.reason)

    def test_a_glob_runs_the_role_s_model(self):
        choice = implementer(a_plan(["src/*.py"]))
        assert choice is not None
        self.assertIsNone(choice.tier)
        self.assertIn("glob", choice.reason)

    def test_no_files_section_runs_the_role_s_model(self):
        choice = implementer("# Plan\n\n## Goal\nSomething.\n")
        assert choice is not None
        self.assertIsNone(choice.tier)
        self.assertEqual(choice.reason, "own model: no Files to Modify section")

    def test_an_unreadable_plan_runs_the_role_s_model(self):
        for text in (None, "", "  \n"):
            choice = implementer(text)
            assert choice is not None
            self.assertIsNone(choice.tier, text)

    def test_a_unit_is_judged_on_its_own_files(self):
        many = ["src/f%d.py" % index for index in range(6)]
        text = a_plan(many, units_block({"small": ["src/f0.py"], "big": many}))
        small = implementer(text, unit="small")
        assert small is not None
        self.assertEqual(small.tier, "light")
        self.assertEqual(small.reason, "light: 1 file in unit small, none high-risk")
        big = implementer(text, unit="big")
        assert big is not None
        self.assertIsNone(big.tier)
        self.assertEqual(big.reason, "own model: 6 files in unit big (more than 3)")

    def test_a_unit_with_a_glob_or_a_directory_runs_the_role_s_model(self):
        text = a_plan(["src/a.py"], units_block({"g": ["src/*.py"], "d": ["src/pkg"]}))
        for name in ("g", "d"):
            choice = implementer(text, unit=name)
            assert choice is not None
            self.assertIsNone(choice.tier, name)

    def test_an_unknown_unit_runs_the_role_s_model(self):
        choice = implementer(a_plan(["src/a.py"], units_block({"a": ["src/a.py"]})), unit="other")
        assert choice is not None
        self.assertIsNone(choice.tier)
        self.assertEqual(choice.reason, "own model: the plan has no unit other")


class ChooseFixer(unittest.TestCase):
    def test_medium_and_low_findings_go_light_with_no_file_cap(self):
        severities = ("low", "medium")
        findings = [finding("F%d" % index, severities[index % 2], "src/f%d.py" % index) for index in range(6)]
        choice = fixer(findings)
        self.assertEqual(choice.tier, "light")
        self.assertEqual(
            choice.reason, "light: 6 accepted finding(s), all medium or lower, none on a high-risk path"
        )

    def test_rejected_findings_are_not_counted(self):
        choice = fixer([finding("F1", "low", "src/a.py"), finding("F2", "critical", "src/b.py", "rejected")])
        self.assertEqual(choice.tier, "light")

    def test_a_high_finding_runs_the_role_s_model(self):
        choice = fixer([finding("F1", "low", "src/a.py"), finding("F2", "high", "src/b.py")])
        self.assertIsNone(choice.tier)
        self.assertEqual(choice.reason, "own model: F2 is high")

    def test_a_finding_on_a_high_risk_path_runs_the_role_s_model(self):
        choice = fixer([finding("F1", "medium", "src/billing/total.py")])
        self.assertIsNone(choice.tier)
        self.assertEqual(choice.reason, "own model: F1 is on src/billing/total.py, which matches *billing*")

    def test_no_accepted_findings_runs_the_role_s_model(self):
        choice = fixer([])
        self.assertIsNone(choice.tier)
        self.assertEqual(choice.reason, "own model: no accepted findings")


def run_event(status: str, tier: Optional[str], unit: str = "", answered: bool = True) -> Dict[str, Any]:
    event: Dict[str, Any] = {"stage": "implementer", "status": status, "tier": tier, "answered": answered}
    if unit:
        event["unit"] = unit
    return event


class RetryAfterFailedLight(unittest.TestCase):
    PLAN = a_plan(["src/a.py"], units_block({"a": ["src/a.py"], "b": ["src/b.py"]}))

    def test_a_failed_light_run_retries_on_the_role_s_model(self):
        failures = (
            run_event("failed", "light"),
            run_event("stalled", "light"),
            run_event("ok", "light", answered=False),
        )
        for event in failures:
            choice = implementer(self.PLAN, events=[run_event("ok", None), event])
            assert choice is not None
            self.assertIsNone(choice.tier, event)
            self.assertEqual(choice.reason, tier_record.RETRY_REASON)

    def test_only_the_last_run_of_the_stage_counts(self):
        events = [run_event("failed", "light"), run_event("ok", None)]
        self.assertIsNone(tier_record.last_light_failure(events, "implementer"))
        events = [run_event("failed", "light"), {"stage": "review_fixer", "status": "ok", "tier": None}]
        self.assertIsNotNone(tier_record.last_light_failure(events, "implementer"))
        self.assertIsNone(tier_record.last_light_failure(events, "review_fixer"))

    def test_events_without_a_tier_key_are_not_runs(self):
        events = [run_event("failed", "light"), {"stage": "implementer", "status": "abandoned"}]
        self.assertIsNotNone(tier_record.last_light_failure(events, "implementer"))

    def test_a_unit_reads_only_its_own_runs(self):
        events = [run_event("failed", "light", unit="a"), run_event("ok", "light", unit="b")]
        retried = implementer(self.PLAN, unit="a", events=events)
        assert retried is not None
        self.assertEqual(retried.reason, tier_record.RETRY_REASON)
        other = implementer(self.PLAN, unit="b", events=events)
        assert other is not None
        self.assertEqual(other.tier, "light")

    def test_the_fixer_retries_too(self):
        events = [dict(run_event("failed", "light"), stage="review_fixer")]
        choice = fixer([finding("F1", "low", "src/a.py")], events)
        self.assertIsNone(choice.tier)
        self.assertEqual(choice.reason, tier_record.RETRY_REASON)


class NoLightTier(unittest.TestCase):
    def test_no_light_tier_chooses_nothing(self):
        self.assertIsNone(implementer(a_plan(["src/a.py"]), tiers={}))
        self.assertIsNone(implementer(a_plan(["src/a.py"]), tiers={"cheap": {}}))

    def test_auto_tier_off_chooses_nothing(self):
        self.assertIsNone(implementer(a_plan(["src/a.py"]), settings={"auto_tier": False}))

    def test_other_roles_are_not_chosen_for(self):
        text = a_plan(["src/a.py"])
        for role in ("architect", "orchestrator", "general"):
            self.assertIsNone(auto_tier.choose(role, {}, LIGHT_TIERS, text, None, {}, []), role)

    def test_the_choice_as_an_event_records_it(self):
        choice = tier_record.TierChoice("light", "auto", "why", "light")
        self.assertEqual(
            choice.to_dict(), {"tier": "light", "source": "auto", "reason": "why", "cause": "light"}
        )
        with self.assertRaises(TypeError):
            tier_record.TierChoice(None, "off", "--no-auto-tier")  # type: ignore[call-arg]

    def test_auto_tier_exports_only_its_decision(self):
        self.assertEqual(
            sorted(auto_tier.__all__), ["AUTO_TIER_MAX_FILES", "LIGHT_SEVERITIES", "choose", "enabled"]
        )


class Causes(unittest.TestCase):
    """Each choice carries a stable ``cause``: what a report counts, whatever the reason says."""

    def test_each_kind_of_choice_has_its_cause(self):
        cases = (
            (implementer(a_plan(["src/a.py"])), "light"),
            (implementer(a_plan(["src/a.py", "src/b.py", "src/c.py", "src/d.py"])), "size"),
            (implementer(a_plan(["src/auth.py"])), "risk"),
            (implementer(a_plan(["src/*.py"])), "doubt"),
            (implementer(a_plan(["src/a.py"]), events=[run_event("failed", "light")]), "retry"),
            (fixer([finding("F1", "high", "src/a.py")]), "risk"),
            (fixer([finding("F1", "low", "")]), "doubt"),
        )
        for choice, cause in cases:
            assert choice is not None
            self.assertEqual(choice.cause, cause, choice)
            self.assertIn(choice.cause, tier_record.CAUSES)


class BareNamesAndUnknownPaths(unittest.TestCase):
    def test_a_bare_high_risk_name_in_files_to_modify_runs_the_role_s_model(self):
        choice = implementer(a_plan(["Dockerfile", "src/a.py"]))
        assert choice is not None
        self.assertIsNone(choice.tier)
        self.assertEqual(choice.reason, "own model: Dockerfile matches Dockerfile")

    def test_a_bare_name_counts_toward_the_cap(self):
        # Makefile is no high-risk name: only the count can send this plan to the role's model.
        for entry in ("`Makefile`", "Makefile", "**Makefile**: add a target"):
            with self.subTest(entry=entry):
                text = a_plan(["src/a.py", "src/b.py", "src/c.py"]) + "- %s\n" % entry
                choice = implementer(text)
                assert choice is not None
                self.assertIsNone(choice.tier)
                self.assertEqual(choice.cause, "size")
                self.assertEqual(choice.reason, "own model: 4 files in Files to Modify (more than 3)")

    def test_an_unbackticked_bare_high_risk_name_runs_the_role_s_model(self):
        text = "# Plan\n\n## Files to Modify\n- Dockerfile\n- src/a.py\n"
        choice = implementer(text)
        assert choice is not None
        self.assertIsNone(choice.tier)
        self.assertEqual(choice.cause, "risk")
        self.assertEqual(choice.reason, "own model: Dockerfile matches Dockerfile")

    def test_a_bare_name_in_a_fence_or_another_section_is_not_counted(self):
        text = a_plan(["src/a.py", "src/b.py", "src/c.py"]) + "```\n- Makefile\n```\n\n## Notes\n- Makefile\n"
        choice = implementer(text)
        assert choice is not None
        self.assertEqual(choice.tier, "light")

    def test_a_mixed_case_high_risk_finding_path_runs_the_role_s_model(self):
        for path, pattern in (("src/Auth.py", "*auth*"), ("Dockerfile.PROD", "Dockerfile.*")):
            with self.subTest(path=path):
                choice = fixer([finding("F1", "medium", path)])
                self.assertIsNone(choice.tier)
                self.assertEqual(choice.cause, "risk")
                self.assertEqual(choice.reason, "own model: F1 is on %s, which matches %s" % (path, pattern))

    def test_a_finding_with_no_usable_path_runs_the_role_s_model(self):
        no_path: List[Any] = [*auto_tier._NO_PATH, "N/A", "  ", None, 0]
        for path in no_path:
            with self.subTest(path=path):
                choice = fixer([finding("F1", "medium", path)])
                self.assertIsNone(choice.tier)
                self.assertEqual(choice.cause, "doubt")
                self.assertEqual(choice.reason, "own model: F1 names no file")
        unjudged = (("src/*.py", "names a glob"), ("src/", "names a directory"), ("src/pkg", "may name"))
        for path, doubt in unjudged:
            with self.subTest(path=path):
                choice = fixer([finding("F1", "medium", path)])
                self.assertIsNone(choice.tier)
                self.assertIn(doubt, choice.reason)

    def test_a_unit_and_the_whole_plan_share_one_rule_set(self):
        text = a_plan(["src/a.py"], units_block({"up": ["../src/a.py"], "bare": ["Makefile"]}))
        up = implementer(text, unit="up")
        assert up is not None
        self.assertIsNone(up.tier)
        self.assertEqual(up.reason, "own model: unit up names a path through ..: ../src/a.py")
        whole = implementer(a_plan(["../src/a.py"]))
        assert whole is not None
        self.assertEqual(whole.reason, "own model: names a path through ..: ../src/a.py")
        bare = implementer(text, unit="bare")
        assert bare is not None
        self.assertEqual(bare.reason, "own model: unit bare may name a directory: Makefile")


class LightFailed(unittest.TestCase):
    def test_one_predicate_for_a_failed_light_run(self):
        self.assertTrue(tier_record.light_failed(run_event("failed", "light")))
        self.assertTrue(tier_record.light_failed(run_event("ok", "light", answered=False)))
        self.assertFalse(tier_record.light_failed(run_event("ok", "light")))
        self.assertFalse(tier_record.light_failed(run_event("failed", None)))

    def test_a_run_that_failed_before_the_provider_answered_counts(self):
        # The end event of a run whose model could not be resolved: no `answered` key.
        events = [{"stage": "implementer", "status": "failed", "tier": "light", "error": "no model"}]
        self.assertIsNotNone(tier_record.last_light_failure(events, "implementer"))


if __name__ == "__main__":
    unittest.main()
