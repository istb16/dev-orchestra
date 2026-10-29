"""``design.require_approval: auto``: the approval is left out only when nobody
is watching, the plan is not high-risk and the design review left nothing
above Low; everything else asks, as ``true`` does.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from test_design_approval import ApprovalCase
from test_design_review import run_cli

from orchestrator import approval, approval_auto
from orchestrator import config as config_mod
from orchestrator import questions as questions_mod
from orchestrator import workspace as ws

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
            # Set, the dedicated variable decides over CI either way.
            ({"CI": "true", approval_auto.UNATTENDED_ENV: "0"}, False),
            ({"CI": "false", approval_auto.UNATTENDED_ENV: "1"}, True),
            ({"CI": "true", approval_auto.UNATTENDED_ENV: ""}, True),
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

    def test_a_round_with_a_failed_reviewer_asks(self):
        # Keep the architecture seat, which a small plan would otherwise leave out.
        run_cli("config", "set", "optimization.skip_unneeded_roles", "false")
        self.mock_findings(LOW_FINDING)
        self.write_plan(SMALL_PLAN)
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "Reviewer: m2 |"
        run_cli("review", "run", "--design")
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("failed", need.reason)

    def test_a_low_finding_asks_where_low_blocks(self):
        run_cli("config", "set", "review.re_review_severities", "[critical, high, medium, low]")
        self.mock_findings(LOW_FINDING)
        self.write_plan(SMALL_PLAN)
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("above Low still open", need.reason)

    def test_a_plan_that_changes_while_judged_asks(self):
        self.write_plan(RISKY_PLAN)
        loaded = config_mod.load(self.project)
        # The decision reads the small plan; the gate then reads the risky one.
        with mock.patch.object(approval_auto.ws, "read_text_strict", return_value=SMALL_PLAN):
            info = approval_auto.current(loaded, self.workspace, CI)
        self.assertTrue(info["required"])
        self.assertEqual(info["reason"], "the plan changed while it was being judged")

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

    def test_a_pending_line_says_why_auto_asked(self):
        self.set_env("CI", "true")
        self.write_plan(RISKY_PLAN)
        out = run_cli("status")[1]
        self.assertIn("asked because the plan touches src/login.py (design.require_approval: auto)", out)

    def test_questions_nobody_answered_are_named_for_the_approver(self):
        self.set_env("CI", "true")
        self.write_plan(RISKY_PLAN)
        state = self.workspace.read_state()
        answer = {"kind": "delegated", "text": questions_mod.UNATTENDED, "answered_at": ws.utcnow()}
        state[questions_mod.STAGE] = {"questions": [{"id": "Q1", "text": "Which?", "answer": answer}]}
        self.workspace.write_state(state)
        self.assertEqual(self.approval()["unattended_questions"], ["Q1"])
        self.assertIn("left to the architect while nobody was watching (Q1)", run_cli("status")[1])
        code, _, err = self.implement()
        self.assertEqual(code, approval.EXIT_APPROVAL_REQUIRED)
        self.assertIn("left to the architect while nobody was watching (Q1)", err)

    def test_status_prints_the_reason(self):
        self.set_env("CI", "true")
        self.write_plan(SMALL_PLAN)
        out = run_cli("status")[1]
        self.assertIn("not required (design.require_approval: auto -- nobody watching", out)
        self.assertIn("design.require_approval: auto", json.dumps(self.approval()["requirement"]))


if __name__ == "__main__":
    unittest.main()
