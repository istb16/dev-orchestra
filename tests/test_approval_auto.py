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

from orchestrator import approval, approval_auto, doctor
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
            # Read without surrounding blanks and in any case.
            ({"CI": " FALSE "}, False),
            ({"CI": " 1 "}, True),
            ({"CI": "  "}, False),
            ({approval_auto.UNATTENDED_ENV: "No"}, False),
            ({approval_auto.UNATTENDED_ENV: " 0 ", "CI": "true"}, False),
            ({approval_auto.UNATTENDED_ENV: "  ", "CI": "true"}, True),
        ):
            with self.subTest(environ=environ):
                self.assertIs(approval_auto.unattended(environ), expected)

    def test_the_setting_is_read_in_any_case(self):
        for value, expected in (
            ("AUTO", "auto"),
            (" auto ", "auto"),
            (None, "auto"),
            (True, "on"),
            (False, "off"),
        ):
            with self.subTest(value=value):
                self.assertEqual(config_mod.approval_mode(value), expected)

    def test_who_is_watching_by_the_setting(self):
        loaded = config_mod.load(self.project)
        self.assertFalse(approval_auto.nobody_watching(loaded, {}))
        self.assertTrue(approval_auto.nobody_watching(loaded, CI))
        run_cli("config", "set", "design.require_approval", "true")
        self.assertFalse(approval_auto.nobody_watching(config_mod.load(self.project), CI))
        run_cli("config", "set", "design.require_approval", "false")
        self.assertTrue(approval_auto.nobody_watching(config_mod.load(self.project), {}))

    def test_a_plan_that_cannot_be_read_asks(self):
        need = self.requirement()
        self.assertEqual((need.required, need.reason), (True, "no readable plan"))
        self.write_plan("  \n\t\n")
        need = self.requirement()
        self.assertEqual((need.required, need.reason), (True, "no readable plan"))
        with open(self.workspace.plan_path, "wb") as handle:
            handle.write(b"# Plan\n\xff\xfe not text\n")
        need = self.requirement()
        self.assertEqual((need.required, need.reason), (True, "no readable plan"))

    def test_every_high_risk_hit_is_counted(self):
        self.write_plan(RISKY_PLAN + "- `src/auth/session.py`\n")
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("the plan touches src/login.py and 1 more", need.reason)

    def test_a_configured_extra_pattern_asks(self):
        run_cli("config", "set", "--scope", "global", "optimization.extra_high_risk_paths", "[src/util.py]")
        self.write_plan(SMALL_PLAN)
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("the plan touches src/util.py", need.reason)

    def test_a_watched_path_asks(self):
        self.write_plan(SMALL_PLAN.replace("src/util.py", ".dev-orchestra.yaml"))
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("the plan touches .dev-orchestra.yaml", need.reason)

    def test_a_project_file_cannot_empty_the_high_risk_patterns(self):
        run_cli("config", "set", "--scope", "project", "optimization.high_risk_paths", "[]")
        run_cli("config", "set", "--scope", "project", "optimization.extra_high_risk_paths", "[]")
        self.assertEqual(config_mod.load(self.project).optimization_settings()["high_risk_paths"], [])
        self.write_plan(RISKY_PLAN)
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("src/login.py", need.reason)

    def test_a_project_file_cannot_make_the_design_review_look_skippable(self):
        # Past the token scan, the design review's own decision still runs on
        # the patterns a project file cannot narrow.
        run_cli("config", "set", "--scope", "project", "optimization.high_risk_paths", "[]")
        run_cli("config", "set", "--scope", "project", "optimization.extra_high_risk_paths", "[]")
        self.write_plan(RISKY_PLAN)
        with mock.patch.object(approval_auto.opt_mod, "design_risk", return_value=[]):
            need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("the design review has not run", need.reason)

    def test_a_global_file_can_narrow_the_high_risk_patterns(self):
        run_cli("config", "set", "--scope", "global", "optimization.high_risk_paths", "[]")
        self.write_plan(RISKY_PLAN)
        need = self.requirement()
        self.assertFalse(need.required, need.reason)
        self.assertNotIn("touches", need.reason)

    def test_a_project_file_can_add_extra_high_risk_patterns(self):
        run_cli("config", "set", "--scope", "project", "optimization.extra_high_risk_paths", "[src/util.py]")
        self.write_plan(SMALL_PLAN)
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("the plan touches src/util.py", need.reason)

    def test_a_project_pattern_that_is_not_a_list_adds_nothing(self):
        defaults = approval_auto.trusted_risk_settings(config_mod.load(self.project))["high_risk_paths"]
        for value in ("src/util.py", None, {"a": 1}):
            with self.subTest(value=value):
                loaded = config_mod.load(self.project)
                block = {"high_risk_paths": value, "extra_high_risk_paths": value}
                loaded.project_layer = {"optimization": block}
                settings = approval_auto.trusted_risk_settings(loaded)
                self.assertEqual(settings["extra_high_risk_paths"], [])
                self.assertEqual(settings["high_risk_paths"], defaults)

    def test_a_skip_the_global_config_would_not_make_asks(self):
        run_cli("config", "set", "--scope", "global", "review.design.enabled", "true")
        run_cli("config", "set", "--scope", "project", "review.design.enabled", "auto")
        self.write_plan(SMALL_PLAN)
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("review.design.enabled: auto from the project config", need.reason)

    def test_a_clean_round_by_the_project_panel_asks(self):
        self.mock_findings(LOW_FINDING)
        self.write_plan(SMALL_PLAN)
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)
        self.assertFalse(self.requirement().required)
        self.write(
            ".dev-orchestra.yaml",
            "version: 1\nreviewers:\n  - id: m1\n    provider: mock\n    role: general\n",
        )
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertEqual(need.reason, "the design review panel is the project config's")

    def test_a_project_file_can_add_high_risk_patterns(self):
        run_cli("config", "set", "--scope", "project", "optimization.high_risk_paths", "[src/util.py]")
        self.write_plan(SMALL_PLAN)
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("the plan touches src/util.py", need.reason)

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

    def test_the_design_review_turned_off_after_a_clean_round_asks(self):
        self.mock_findings(LOW_FINDING)
        self.write_plan(SMALL_PLAN)
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)
        self.assertFalse(self.requirement().required)
        run_cli("config", "set", "review.design.enabled", "false")
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("review.design.enabled: false", need.reason)

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

    def test_high_risk_once_declared_stays(self):
        self.mock_findings(LOW_FINDING)
        self.write_plan(SMALL_PLAN)
        self.assertEqual(run_cli("review", "run", "--design", "--high-risk")[0], 0)
        self.assertEqual(run_cli("review", "run", "--design", "--force")[0], 0)
        need = self.requirement()
        self.assertTrue(need.required)
        self.assertIn("--high-risk", need.reason)

    def test_high_risk_is_known_without_the_round_event(self):
        # Neither a round whose event is not written yet, nor a recorded event
        # without the flag, hides it.
        self.mock_findings(LOW_FINDING)
        self.write_plan(SMALL_PLAN)
        self.assertEqual(run_cli("review", "run", "--design", "--high-risk")[0], 0)
        state = self.workspace.read_state()
        state["events"] = [e for e in state.get("events") or [] if e.get("stage") != "design_review"]
        self.workspace.write_state(state)
        self.assertIn("--high-risk", self.requirement().reason)
        latest = approval.design_round(self.workspace)
        run_cli("state", "record", "design_review", "done", "--detail", "round_id=%s" % latest)
        self.assertIn("--high-risk", self.requirement().reason)

    def test_high_risk_is_known_from_an_event_of_a_round_before_the_key(self):
        self.mock_findings(LOW_FINDING)
        self.write_plan(SMALL_PLAN)
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)
        meta_path = self.workspace.design_review().snapshot_meta_path
        meta = ws.read_json(meta_path, {})
        meta.pop("declared_high_risk", None)
        ws.write_json(meta_path, meta)
        self.assertNotIn("--high-risk", self.requirement().reason)
        state = self.workspace.read_state()
        state.setdefault("events", []).append(
            {"stage": "design_review", "status": "done", "optimization": {"declared": True}}
        )
        self.workspace.write_state(state)
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
        self.assertIn("1 design reviewer(s) failed and 0 ran partial", need.reason)

    def test_a_round_that_did_not_review_the_whole_plan_asks(self):
        whole = {
            "counts": {"snapshot_reviewers_ok": 2},
            "coverage": {"round": "complete"},
        }
        self.assertEqual(approval_auto._short_coverage(whole), "")
        for change, reason in (
            (
                {"counts": {"snapshot_reviewers_ok": 1, "snapshot_reviewers_partial": 1}},
                "0 design reviewer(s) failed and 1 ran partial",
            ),
            ({"counts": {"snapshot_reviewers_ok": 0}}, "no design reviewer finished"),
            ({"coverage": {"round": "file"}}, "did not show the whole plan"),
            ({"coverage": None}, "did not show the whole plan"),
        ):
            with self.subTest(change=change):
                self.assertIn(reason, approval_auto._short_coverage(dict(whole, **change)))

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
        real = ws.read_text_strict
        reads = []

        def first_small(path, *args, **kwargs):
            # The decision's read sees the small plan; every read after it, the risky one.
            reads.append(path)
            return SMALL_PLAN if len(reads) == 1 else real(path, *args, **kwargs)

        with mock.patch.object(ws, "read_text_strict", side_effect=first_small):
            info = approval_auto.current(loaded, self.workspace, CI)
        self.assertEqual(reads[0], self.workspace.plan_path)
        self.assertEqual(info["plan_sha256"], approval.plan_digest(RISKY_PLAN))
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
        self.assertIn(
            "is not approved (required (design.require_approval: auto -- the plan touches src/login.py)).",
            err,
        )
        self.assertIn("asked because the plan touches src/login.py", run_cli("status")[1])

    def test_the_notes_and_refusal_under_auto(self):
        info = {
            "mode": "auto",
            "nobody_watching": True,
            "reason": "the plan touches src/login.py",
            "requirement": "required (design.require_approval: auto -- the plan touches src/login.py)",
            "unattended_questions": ["Q1", "Q2"],
        }
        self.assertEqual(
            approval_auto.pending_notes(info),
            [
                "; asked because the plan touches src/login.py (design.require_approval: auto)",
                "; also present the design questions left to the architect "
                "while nobody was watching (Q1, Q2)",
            ],
        )
        lines = approval_auto.refusal_lines(info, ".ai/plan.md")
        self.assertEqual(
            lines[0],
            "refusing to run implementer: the plan at .ai/plan.md is not approved "
            "(required (design.require_approval: auto -- the plan touches src/login.py)).",
        )
        self.assertIn("open design findings, and the design questions left to the architect", lines[1])
        self.assertIn("(Q1, Q2)", lines[1])
        # Someone watching: the reason is the gate's own, not a note.
        watched = dict(info, nobody_watching=False, unattended_questions=[])
        self.assertEqual(approval_auto.pending_notes(watched), [])

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

    def test_status_json_carries_the_requirement(self):
        self.set_env("CI", "true")
        self.write_plan(RISKY_PLAN)
        info = json.loads(run_cli("status", "--json")[1])["design_approval"]
        self.assertEqual(info["mode"], "auto")
        self.assertEqual(info["reason"], "the plan touches src/login.py")
        self.assertEqual(
            info["requirement"], "required (design.require_approval: auto -- the plan touches src/login.py)"
        )
        self.assertIs(info["nobody_watching"], True)
        self.assertEqual(info["unattended_questions"], [])
        run_cli("config", "set", "design.require_approval", "true")
        info = json.loads(run_cli("status", "--json")[1])["design_approval"]
        self.assertEqual((info["mode"], info["reason"]), ("on", None))
        self.assertEqual(info["requirement"], "required (design.require_approval: true)")
        self.assertIs(info["nobody_watching"], False)

    def test_an_approval_auto_leaves_out_is_recorded_with_a_note(self):
        self.set_env("CI", "true")
        self.write_plan(SMALL_PLAN)
        code, _, err = run_cli("design", "approve")
        self.assertEqual(code, 0, err)
        self.assertIn(
            "note: approval is not required (design.require_approval: auto -- nobody watching; "
            "the design review was skipped",
            err,
        )
        self.assertIn("recorded anyway", err)

    def test_doctor_notes_questions_only_when_approval_is_off(self):
        run_cli("config", "set", "design.ask_questions", "auto")
        notes = doctor.collect(self.project, probe_models=False)["notes"]
        self.assertFalse(any("design.ask_questions is" in note for note in notes))
        run_cli("config", "set", "design.require_approval", "false")
        notes = doctor.collect(self.project, probe_models=False)["notes"]
        self.assertTrue(any("design.ask_questions is auto" in note for note in notes))


if __name__ == "__main__":
    unittest.main()
