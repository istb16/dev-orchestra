"""The user's approval of the plan, enforced where the implementer starts.

SKILL.md tells the orchestrator to ask before implementing; these tests are
about the part prose cannot do. `run implementer` refuses a plan nobody
approved -- in the parent and again in a detached worker, whatever `--force`
says -- and an approval stops counting once the plan changes or a design
review runs after it.
"""

from __future__ import annotations

import json
import os
import time
import unittest

from helpers import IsolatedCase
from test_design_review import PLAN, REVISED_PLAN, DesignReviewCase, run_cli

from orchestrator import approval
from orchestrator import jobs as jobs_mod
from orchestrator import ledger as ledger_mod
from orchestrator import review as review_mod
from orchestrator import workspace as ws


class ApprovalCase(DesignReviewCase):
    """The design review's mock panel, plus mock implementer, fixer and architect."""

    def setUp(self):
        super().setUp()
        for role in ("implementer", "review_fixer", "architect"):
            run_cli("config", "set", "%s.provider" % role, "mock")

    def status(self):
        return json.loads(run_cli("status", "--json")[1])

    def approval(self):
        return self.status()["design_approval"]

    def implement(self, *extra):
        return run_cli("run", "implementer", "--prompt", "go", *extra)

    def implementer_events(self):
        state = json.loads(run_cli("state", "show", "--json")[1])
        return [e for e in state.get("events", []) if e.get("stage") == "implementer"]

    def implementer_attempts(self):
        return json.loads(run_cli("budget", "show", "--json")[1])["budgets"]["implementer"]["used"]

    def worker(self, job_id="p-1", paid=False):
        """A worker invocation as `_detached_argv` builds it, on a job of its own.

        ``paid``: as a parent leaves it, having consumed the attempt first.
        """
        job = {"id": job_id, "stage": "implementer", "status": "running"}
        if paid:
            ledger_mod.Ledger(self.workspace).consume("implementer")
        jobs_mod.write_job(self.workspace, job)
        prompt = os.path.join(self.project, "prompt.md")
        ws.write_text(prompt, "go")
        code = run_cli(
            "run",
            "implementer",
            "--force",
            "--prompt-file",
            prompt,
            "--job-file",
            jobs_mod.job_path(self.workspace, job_id),
        )[0]
        return code, jobs_mod.read_job(self.workspace, job_id)

    def date_plan(self, offset):
        """Move the plan's mtime ``offset`` seconds from now."""
        moment = time.time() + offset
        os.utime(self.workspace.plan_path, (moment, moment))


class TestTheGate(ApprovalCase):
    def test_the_gate_is_on_by_default(self):
        self.write_plan()
        code, _, err = self.implement()
        self.assertEqual(code, approval.EXIT_APPROVAL_REQUIRED)
        self.assertIn("is not approved", err)
        self.assertIn("design approve", err)
        budget = json.loads(run_cli("budget", "show", "--json")[1])
        self.assertEqual(budget["budgets"]["implementer"]["used"], 0)
        self.assertEqual(self.implementer_events(), [])

    def test_no_plan_means_no_gate(self):
        self.assertEqual(self.implement()[0], 0)

    def test_approving_lets_the_implementer_run(self):
        self.write_plan()
        code, out, _ = run_cli("design", "approve")
        self.assertEqual(code, 0)
        self.assertIn(approval.plan_digest(PLAN)[:12], out)
        state = json.loads(run_cli("state", "show", "--json")[1])
        self.assertEqual(state["events"][-1]["stage"], "design_approval")
        self.assertEqual(state["events"][-1]["status"], "approved")
        self.assertEqual(self.implement()[0], 0)

    def test_a_revised_plan_needs_approving_again(self):
        self.write_plan()
        run_cli("design", "approve")
        self.write_plan(REVISED_PLAN)
        code, _, err = self.implement()
        self.assertEqual(code, 5)
        self.assertIn("changed after it was approved", err)

    def test_force_does_not_bypass_the_gate(self):
        self.write_plan()
        self.assertEqual(self.implement("--force")[0], 5)

    def test_the_gate_runs_before_detach(self):
        self.write_plan()
        self.assertEqual(self.implement("--detach")[0], 5)
        self.assertEqual(json.loads(run_cli("jobs", "list", "--json")[1]), [])

    def test_only_the_implementer_is_gated(self):
        self.write_plan()
        for role in ("review_fixer", "architect", "m1"):
            self.assertEqual(run_cli("run", role, "--prompt", "go")[0], 0, role)

    def test_a_state_record_event_is_not_an_approval(self):
        self.write_plan()
        run_cli("state", "record", "design_approval", "approved")
        self.assertEqual(self.implement()[0], 5)


class TestTheWorker(ApprovalCase):
    def test_a_worker_is_gated_too(self):
        self.write_plan()
        code, job = self.worker()
        self.assertEqual(code, 5)
        self.assertEqual(job["status"], "failed")
        # The whole refusal, including what to do about it.
        self.assertIn("is not approved", job["error"])
        self.assertIn("Present the plan to the user", job["error"])
        self.assertIn("design approve", job["error"])
        self.assertEqual(self.implementer_events(), [])

    def test_a_plan_edited_after_detach_is_refused_by_the_worker(self):
        self.write_plan()
        run_cli("design", "approve")
        self.write_plan(REVISED_PLAN)
        code, job = self.worker(paid=True)
        self.assertEqual(code, 5)
        self.assertEqual(job["status"], "failed")
        self.assertIn("changed after it was approved", job["error"])
        # The attempt the parent consumed stays consumed: a refusal in the
        # worker is not given back.
        self.assertEqual(self.implementer_attempts(), 1)

    def test_an_approved_detached_run_succeeds(self):
        self.write_plan()
        run_cli("design", "approve")
        code, out, _ = self.implement("--detach", "--json")
        self.assertEqual(code, 0)
        _, waited, _ = run_cli("jobs", "wait", json.loads(out)["id"], "--timeout", "60", "--json")
        finished = json.loads(waited)
        self.assertEqual(finished["status"], "succeeded")
        self.assertIn("claimed_at", finished)


class TestApproving(ApprovalCase):
    def test_no_plan_cannot_be_approved(self):
        code, _, err = run_cli("design", "approve")
        self.assertEqual(code, 2)
        self.assertIn("run the architect first", err)

    def test_approving_twice_is_idempotent(self):
        self.write_plan()
        run_cli("design", "approve")
        code, out, _ = run_cli("design", "approve")
        self.assertEqual(code, 0)
        self.assertIn("already approved", out)
        state = json.loads(run_cli("state", "show", "--json")[1])
        self.assertEqual(len([e for e in state["events"] if e["stage"] == "design_approval"]), 1)

    def test_open_findings_are_printed_not_refused(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        code, out, err = run_cli("design", "approve", "--json")
        self.assertEqual(code, 0)
        self.assertIn("F1", err)
        self.assertIn("still open", err)
        payload = json.loads(out)
        self.assertEqual(payload["open_findings"], ["F1"])
        self.assertIs(payload["open_findings_of_current_plan"], True)
        self.assertFalse(payload["already_approved"])

    def test_open_findings_below_the_blocking_severities_are_named_too(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        data = ws.read_json(self.design.consolidated_json_path)
        data["findings"][0]["severity"] = "low"
        ws.write_json(self.design.consolidated_json_path, data)
        _, out, err = run_cli("design", "approve", "--json")
        self.assertEqual(json.loads(out)["open_findings"], ["F1"])
        self.assertIn("still open", err)

    def test_rejected_findings_are_not_named(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        run_cli("review", "triage", "--design", "F1", "--status", "rejected")
        _, out, err = run_cli("design", "approve", "--json")
        self.assertEqual(json.loads(out)["open_findings"], [])
        self.assertNotIn("still open", err)

    def test_a_round_with_no_report_yet_cannot_be_approved_over(self):
        """A round that has started -- or failed -- has findings nobody has seen."""
        self.write_plan()
        run_cli("review", "run", "--design")
        # What `review run --design` leaves behind before any reviewer answers.
        review_mod.write_design_snapshot(
            self.design, self.workspace.plan_path, "", PLAN, "digest-of-the-next-round"
        )
        code, _, err = run_cli("design", "approve")
        self.assertEqual(code, 2)
        self.assertIn("has no report yet", err)
        self.assertNotIn(approval.STAGE, self.workspace.read_state())
        self.assertEqual(self.implement()[0], 5)

    def test_a_round_every_reviewer_failed_cannot_be_approved_over(self):
        """Nobody reviewed it, so it has no findings to show -- not a clean round."""
        self.write_plan()
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        run_cli("review", "run", "--design")
        del os.environ["DEV_ORCHESTRA_MOCK_FAIL"]
        report = ws.read_json(self.design.consolidated_json_path)
        self.assertIsNone(approval.reported_round(report))
        code, _, err = run_cli("design", "approve")
        self.assertEqual(code, 2)
        self.assertIn("has no report yet", err)
        self.assertNotIn(approval.STAGE, self.workspace.read_state())

    def test_reconsolidating_a_round_with_no_report_does_not_publish_it(self):
        """`review consolidate` mid-round keeps the last reported round, not the new one."""
        self.write_plan()
        run_cli("review", "run", "--design")
        reported = approval.reported_round(ws.read_json(self.design.consolidated_json_path))
        review_mod.write_design_snapshot(
            self.design, self.workspace.plan_path, "", PLAN, "digest-of-the-next-round"
        )
        self.assertEqual(run_cli("review", "consolidate", "--design")[0], 0)
        report = ws.read_json(self.design.consolidated_json_path)
        self.assertEqual(approval.reported_round(report), reported)
        code, _, err = run_cli("design", "approve")
        self.assertEqual(code, 2)
        self.assertIn("has no report yet", err)
        self.assertNotIn(approval.STAGE, self.workspace.read_state())

    def test_an_approval_binds_to_the_round_of_the_findings_it_names(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        _, out, _ = run_cli("design", "approve", "--json")
        report = ws.read_json(self.design.consolidated_json_path)
        self.assertEqual(json.loads(out)["design_round"], report["snapshot"]["round_id"])
        self.assertEqual(json.loads(out)["design_round"], approval.design_round(self.workspace))

    def test_findings_of_an_earlier_revision_are_labelled(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        run_cli("review", "triage", "--design", "F1", "--status", "accepted")
        self.write_plan(REVISED_PLAN)
        _, out, err = run_cli("design", "approve", "--json")
        self.assertIn("from a review of an earlier revision", err)
        self.assertIs(json.loads(out)["open_findings_of_current_plan"], False)

    def test_a_rewritten_request_does_not_make_the_findings_look_old(self):
        """Compared with the frozen plan, not a digest that re-reads the request."""
        self.write_plan()
        self.write_request()
        run_cli("review", "run", "--design")
        self.write_request("# Design request\n\nSomething else entirely.\n")
        _, out, _ = run_cli("design", "approve", "--json")
        self.assertIs(json.loads(out)["open_findings_of_current_plan"], True)

    def test_approving_with_the_gate_off_is_recorded_anyway(self):
        run_cli("config", "set", "design.require_approval", "false")
        self.write_plan()
        code, _, err = run_cli("design", "approve")
        self.assertEqual(code, 0)
        self.assertIn("recorded anyway", err)


class TestStatus(ApprovalCase):
    def test_status_reports_the_approval_state(self):
        self.write_plan()
        payload = self.status()
        self.assertEqual(payload["design_approval"]["state"], "pending")
        self.assertTrue(payload["design_approval"]["pending"])
        self.assertIsNone(payload["design_approval"]["matches_current_plan"])
        self.assertEqual((payload["verdict"], payload["reasons"]), ("continue", []))
        self.assertIn("Plan approval: pending", run_cli("status")[1])

        run_cli("design", "approve")
        payload = self.status()
        self.assertEqual(payload["design_approval"]["state"], "approved")
        self.assertIs(payload["design_approval"]["matches_current_plan"], True)
        self.assertIs(payload["design_approval"]["reviewed_since_approval"], False)
        self.assertEqual(payload["verdict"], "continue")
        self.assertIn("Plan approval: approved", run_cli("status")[1])

        self.write_plan(REVISED_PLAN)
        payload = self.status()
        self.assertEqual(payload["design_approval"]["state"], "stale")
        self.assertEqual(payload["design_approval"]["stale_reason"], "plan-changed")
        self.assertIs(payload["design_approval"]["matches_current_plan"], False)
        self.assertEqual((payload["verdict"], payload["reasons"]), ("continue", []))

    def test_no_plan_is_said_as_such(self):
        self.assertEqual(self.approval()["state"], "no-plan")
        self.assertIn("Plan approval: no plan", run_cli("status")[1])

    def test_require_approval_false_restores_the_old_behaviour(self):
        run_cli("config", "set", "design.require_approval", "false")
        self.write_plan()
        self.assertEqual(self.implement()[0], 0)
        info = self.approval()
        self.assertEqual(info["state"], "not-required")
        self.assertFalse(info["pending"])
        run_cli("design", "approve")
        self.write_plan(REVISED_PLAN)
        self.assertIs(self.approval()["matches_current_plan"], False)

    def test_summary_lists_the_approval(self):
        self.write_plan()
        run_cli("design", "approve")
        self.assertIn("design_approval", run_cli("summary")[1])

    def test_budget_reset_keeps_the_approval(self):
        self.write_plan()
        run_cli("design", "approve")
        run_cli("budget", "reset")
        self.assertEqual(self.approval()["state"], "approved")

    def test_an_exhausted_design_review_still_ends_in_the_question(self):
        run_cli("config", "set", "review.design.max_iterations", "1")
        self.write_plan()
        run_cli("review", "run", "--design")
        payload = self.status()
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertTrue(any("design review budget spent" in r for r in payload["reasons"]))
        self.assertTrue(payload["design_approval"]["pending"])
        self.assertTrue(payload["design_approval"]["design_review_exhausted"])
        self.assertIn("ask the user whether to approve over them", run_cli("status")[1])

        code, _, err = run_cli("design", "approve")
        self.assertEqual(code, 0)
        self.assertIn("F1", err)
        # The user decided to go ahead over the findings; the stop is answered.
        payload = self.status()
        self.assertEqual(payload["verdict"], "continue")
        self.assertFalse(any("design review budget spent" in r for r in payload["reasons"]))
        self.assertEqual(self.implement()[0], 0)
        self.assertEqual(self.status()["verdict"], "continue")


class TestDesignRounds(ApprovalCase):
    def test_a_design_review_after_approval_needs_approving_again(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        run_cli("design", "approve")
        # Within the same second, with the same plan: only the round id differs.
        run_cli("review", "run", "--design")
        info = self.approval()
        self.assertEqual(info["state"], "stale")
        self.assertEqual(info["stale_reason"], "reviewed-since")
        self.assertIs(info["reviewed_since_approval"], True)
        self.assertIs(info["matches_current_plan"], True)
        code, _, err = self.implement()
        self.assertEqual(code, 5)
        self.assertIn("a design review ran after", err)

        run_cli("review", "triage", "--design", "F1", "--status", "rejected")
        self.assertEqual(self.approval()["state"], "stale")
        run_cli("design", "approve")
        self.assertEqual(self.approval()["state"], "approved")

    def test_reconsolidating_and_triaging_leave_the_approval_alone(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        run_cli("design", "approve")
        self.assertEqual(run_cli("review", "consolidate", "--design")[0], 0)
        run_cli("review", "triage", "--design", "F1", "--status", "rejected")
        self.assertEqual(self.approval()["state"], "approved")
        self.assertEqual(self.implement()[0], 0)

    def test_every_design_round_has_its_own_id(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        first = approval.design_round(self.workspace)
        run_cli("review", "run", "--design")
        self.assertTrue(first)
        self.assertNotEqual(first, approval.design_round(self.workspace))

    def test_a_round_written_before_ids_counts_as_no_round(self):
        """An approval over it records None, and the first round with an id is later."""
        self.write_plan()
        run_cli("review", "run", "--design")
        meta = ws.read_json(self.design.snapshot_meta_path)
        meta.pop("round_id")
        ws.write_json(self.design.snapshot_meta_path, meta)
        report = ws.read_json(self.design.consolidated_json_path)
        report["snapshot"].pop("round_id")
        ws.write_json(self.design.consolidated_json_path, report)
        self.assertIsNone(approval.design_round(self.workspace))
        run_cli("design", "approve")
        self.assertEqual(self.approval()["state"], "approved")
        run_cli("review", "run", "--design")
        self.assertEqual(self.approval()["stale_reason"], "reviewed-since")


class TestWorkflowsFromBeforeTheGate(ApprovalCase):
    def test_an_already_implemented_workflow_is_not_asked(self):
        self.write_plan()
        self.date_plan(-60)
        self.workspace.record_event("implementer", "ok", {"provider": "mock"})
        info = self.approval()
        self.assertEqual(info["state"], "implemented-unapproved")
        self.assertFalse(info["pending"])
        lines = run_cli("status")[1].splitlines()
        line = next(text for text in lines if text.startswith("Plan approval:"))
        self.assertIn("nothing to ask", line)
        self.assertNotIn("present", line)

    def test_but_it_is_not_let_through_the_gate(self):
        self.write_plan()
        self.date_plan(-60)
        self.workspace.record_event("implementer", "ok", {"provider": "mock"})
        self.assertEqual(self.implement()[0], 5)

    def test_a_plan_written_after_the_implementer_is_asked_about(self):
        self.write_plan()
        self.date_plan(-60)
        self.workspace.record_event("implementer", "ok", {"provider": "mock"})
        self.write_plan(REVISED_PLAN)
        self.date_plan(60)
        self.assertEqual(self.approval()["state"], "pending")
        self.assertEqual(self.implement()[0], 5)

    def test_a_failed_implementer_run_never_counts(self):
        self.write_plan()
        self.date_plan(-60)
        self.workspace.record_event("implementer", "failed", {"provider": "mock"})
        self.assertEqual(self.approval()["state"], "pending")


class TestTheModule(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace()

    def test_the_digest_is_of_the_plan_alone(self):
        self.assertEqual(approval.plan_digest(PLAN), approval.plan_digest(PLAN))
        self.assertNotEqual(approval.plan_digest(PLAN), approval.plan_digest(REVISED_PLAN))

    def test_a_blank_plan_is_no_plan(self):
        ws.write_text(self.workspace.plan_path, "  \n")
        self.assertEqual(approval.read_plan(self.workspace), ("", ""))
        self.assertEqual(approval.current(self.workspace, True)["state"], "no-plan")

    def test_the_record_and_its_event_are_written_together(self):
        ws.write_text(self.workspace.plan_path, PLAN)
        digest = approval.plan_digest(PLAN)
        approval.record(self.workspace, digest, [], None, None)
        state = self.workspace.read_state()
        self.assertEqual(state[approval.STAGE]["sha256"], digest)
        self.assertIsNone(state[approval.STAGE]["design_round"])
        self.assertEqual(state["events"][-1]["stage"], approval.STAGE)

    def test_the_comparisons_do_not_depend_on_the_setting(self):
        ws.write_text(self.workspace.plan_path, PLAN)
        approval.record(self.workspace, approval.plan_digest(PLAN), [], None, None)
        ws.write_text(self.workspace.plan_path, REVISED_PLAN)
        info = approval.current(self.workspace, False)
        self.assertEqual(info["state"], "not-required")
        self.assertIs(info["matches_current_plan"], False)
        self.assertIs(info["reviewed_since_approval"], False)

    def test_the_refusal_names_the_command(self):
        lines = approval.refusal_lines({"state": "pending", "stale_reason": None}, ".ai/plan.md")
        self.assertIn(".ai/plan.md", lines[0])
        self.assertTrue(any("design approve" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
