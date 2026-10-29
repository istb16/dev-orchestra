"""``design.require_approval: auto``: the approval is left out only when nobody
is watching, the plan is not high-risk and the design review left nothing
above Low; everything else asks, as ``true`` does.
"""

from __future__ import annotations

import json
import os
import unittest

from test_design_approval import ApprovalCase
from test_design_review import run_cli

from orchestrator import approval, approval_auto
from orchestrator import config as config_mod

#: Small and contained: `review.design.enabled: auto` skips the design review.
SMALL_PLAN = """# Plan

## Proposed Change
Rename a helper.

## Files to Modify
- `src/util.py`
"""

#: Names a file the default high-risk patterns match.
RISKY_PLAN = """# Plan

## Proposed Change
Tidy the sign-in form.

## Files to Modify
- `src/login.py`
"""

LOW_FINDING = """## Finding
- Severity: low
- File: plan.md#Proposed Change
- Line: n/a
- Category: naming
- Problem: the new name is a little vague
- Impact: readers may look twice
- Evidence: "Rename a helper"
- Recommended fix: say which helper
"""

CI = {"CI": "true"}


class AutoCase(ApprovalCase):
    def requirement(self, environ=None):
        loaded = config_mod.load(self.project)
        return approval_auto.requirement(loaded, self.workspace, CI if environ is None else environ)

    def mock_findings(self, text):
        with open(os.path.join(self.mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(text)


class TestTheDecision(AutoCase):
    def test_auto_is_the_default(self):
        self.assertEqual(config_mod.load(self.project).design_settings()["require_approval"], "auto")

    def test_someone_watching_asks(self):
        self.write_plan(SMALL_PLAN)
        need = self.requirement({})
        self.assertTrue(need.required)
        self.assertIn("someone may be watching", need.reason)

    def test_the_environment_says_who_is_watching(self):
        for environ, expected in (
            ({}, False),
            ({"CI": "true"}, True),
            ({"CI": "1"}, True),
            ({"CI": "false"}, False),
            ({"CI": "0"}, False),
            ({"CI": ""}, False),
            ({approval_auto.UNATTENDED_ENV: "1"}, True),
            ({approval_auto.UNATTENDED_ENV: "off"}, False),
        ):
            with self.subTest(environ=environ):
                self.assertIs(approval_auto.unattended(environ), expected)

    def test_a_skipped_design_review_of_a_small_plan_needs_no_approval(self):
        self.write_plan(SMALL_PLAN)
        need = self.requirement()
        self.assertFalse(need.required, need.reason)
        self.assertIn("skipped", need.reason)

    def test_a_high_risk_plan_asks(self):
        self.write_plan(RISKY_PLAN)
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("src/login.py", need.reason)

    def test_a_design_review_that_has_not_run_asks(self):
        # No Files to Modify: `auto` wants the design review, and it has not run.
        self.write_plan()
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("has not run", need.reason)

    def test_the_design_review_turned_off_asks(self):
        run_cli("config", "set", "review.design.enabled", "false")
        self.write_plan(SMALL_PLAN)
        self.assertTrue(self.requirement().required)

    def test_only_low_findings_left_need_no_approval(self):
        self.mock_findings(LOW_FINDING)
        self.write_plan(SMALL_PLAN)
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)
        need = self.requirement()
        self.assertFalse(need.required, need.reason)
        self.assertIn("nothing above Low", need.reason)

    def test_a_finding_above_low_asks(self):
        self.write_plan(SMALL_PLAN)
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("above Low still open", need.reason)

    def test_a_review_of_an_earlier_revision_asks(self):
        self.mock_findings(LOW_FINDING)
        self.write_plan(SMALL_PLAN)
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)
        self.write_plan(SMALL_PLAN + "\nAnd one more thing.\n")
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("earlier revision", need.reason)

    def test_a_round_run_with_high_risk_asks(self):
        self.mock_findings(LOW_FINDING)
        self.write_plan(SMALL_PLAN)
        self.assertEqual(run_cli("review", "run", "--design", "--high-risk")[0], 0)
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("--high-risk", need.reason)

    def test_true_asks_even_in_ci(self):
        run_cli("config", "set", "design.require_approval", "true")
        self.write_plan(SMALL_PLAN)
        need = self.requirement()
        self.assertEqual((need.mode, need.required), ("on", True))


class TestTheGateUnderAuto(AutoCase):
    def test_the_implementer_runs_unapproved_when_auto_leaves_approval_out(self):
        self.set_env("CI", "true")
        self.write_plan(SMALL_PLAN)
        info = self.approval()
        self.assertEqual(info["state"], "not-required")
        self.assertEqual(info["mode"], "auto")
        self.assertIn("skipped", info["reason"])
        self.assertEqual(self.implement()[0], 0)

    def test_the_implementer_is_refused_with_the_reason(self):
        self.set_env("CI", "true")
        self.write_plan(RISKY_PLAN)
        code, _, err = self.implement()
        self.assertEqual(code, approval.EXIT_APPROVAL_REQUIRED)
        self.assertIn("design.require_approval: auto -- the plan touches src/login.py", err)

    def test_outside_ci_the_gate_is_as_before(self):
        self.write_plan(SMALL_PLAN)
        self.assertEqual(self.approval()["state"], "pending")
        self.assertEqual(self.implement()[0], approval.EXIT_APPROVAL_REQUIRED)

    def test_status_prints_the_reason(self):
        self.set_env("CI", "true")
        self.write_plan(SMALL_PLAN)
        out = run_cli("status")[1]
        self.assertIn("not required (design.require_approval: auto -- nobody watching", out)
        self.assertIn("design.require_approval: auto", json.dumps(self.approval()["requirement"]))


if __name__ == "__main__":
    unittest.main()
