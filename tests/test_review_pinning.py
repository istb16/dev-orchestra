"""What a review round prints, records and refuses, pinned on both paths.

`review run` and `review run --design` share their round steps: the
refusals, the panel run, the charge, the notes and the report. These tests
hold each path to what it said and recorded before the steps were shared --
exact strings where a shared template builds them, and the order of every
key a caller can see.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import time
import unittest
from contextlib import contextmanager
from typing import List
from unittest import mock

from helpers import IsolatedCase, has_git
from test_agy_gates import AGY_REVIEWER, UNENFORCED, _GateCase
from test_cli import spend_the_runtime_budget
from test_design_review import REVISED_PLAN, DesignReviewCase, run_cli
from test_prompt_limits import finding
from test_review import reviewer
from test_surrounding import MeasuringCase

from orchestrator import config as config_mod
from orchestrator import context as context_mod
from orchestrator import execution, review_fanout
from orchestrator import review as review_mod
from orchestrator import workspace as ws
from orchestrator.providers import ModelResolutionError
from orchestrator.providers.base import ResolvedModel, RunResult
from orchestrator.providers.mock import MockProvider

#: Every event a ledger entry closes starts with these, in this order.
EVENT_HEAD = ["stage", "status", "at", "charged_seconds"]

#: The keys of an ok design round's event after the charge.
DESIGN_TAIL = ["elapsed_seconds", "iteration", "round_id", "reviewers", "findings", "identical_rounds"]

#: The same for a code round.
CODE_TAIL = [*DESIGN_TAIL, "optimization"]

#: The keys every round's JSON payload starts with.
PAYLOAD_HEAD = ["ok", "failed", "partial", "reviewers", "counts"]

REVIEWER_LINE = re.compile(r"^(ok|PARTIAL|FAILED) ")

NO_REVIEWERS = "No reviewers configured -- skipping the independent-review stage.\n"

GATE_WARN_NOTE = (
    "note: no test result recorded for this change, so the review is running unchecked. "
    "`state record test ok` before `review run` lets this stop a review of a red tree."
)


class _Pinning(IsolatedCase):
    """Shared by both cases below. It defines no test, so nothing runs twice."""

    workspace: ws.Workspace
    #: Set by ``_GateCase.answer``.
    started: List[List[str]]

    def events(self, stage):
        events = self.workspace.read_state().get("events") or []
        return [event for event in events if isinstance(event, dict) and event.get("stage") == stage]

    def last_event(self, stage):
        return self.events(stage)[-1]

    def assert_event_keys(self, event, tail):
        self.assertEqual(list(event), [*EVENT_HEAD, *tail])

    def assert_payload_keys(self, out, tail):
        self.assertEqual(list(json.loads(out)), [*PAYLOAD_HEAD, *tail])

    def assert_report_tail(self, out, workspace, summary, extra=()):
        lines = out.splitlines()
        count = 0
        while count < len(lines) and REVIEWER_LINE.match(lines[count]):
            count += 1
        self.assertGreater(count, 0, out)
        consolidated = "Consolidated: %s" % workspace.relative(workspace.consolidated_md_path)
        expected = ["", summary, *extra, consolidated]
        self.assertEqual(lines[count:], expected)

    def labels(self):
        return json.loads(run_cli("tokens", "show", "--json")[1]).get("by_label") or {}

    @contextmanager
    def capture_run_reviews(self):
        """Stop the round at ``run_reviews``, keeping what it was called with.

        The arguments are bound against the real signature with its defaults
        applied, so what is pinned is what the fan-out received, whether a
        caller passed a default or left it out. The options are flattened
        beside the reviewers and the workspace, every default when None.
        """
        captured = {}
        signature = inspect.signature(review_mod.run_reviews)

        def capture(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            options = arguments.pop("options") or review_mod.FanoutOptions()
            arguments.update(options._asdict())
            captured["arguments"] = arguments
            in_flight = self.workspace.read_state()["ledger"]["in_flight"]
            (captured["in_flight"],) = in_flight.values()
            raise review_mod.ReviewError("captured")

        with mock.patch.object(review_mod, "run_reviews", side_effect=capture):
            yield captured

    def enforcement_fixture(self, global_text):
        """Only agy on PATH, and a global file holding exactly ``global_text``."""
        self.fake_clis(agy=True)
        self.addCleanup(setattr, execution, "execute", execution.execute)
        # `reviewer add` and `config set` wrote to no fixed scope; a project
        # panel left here would override the one the test writes.
        project_file = os.path.join(self.project, ".dev-orchestra.yaml")
        if os.path.exists(project_file):
            os.remove(project_file)
        _GateCase.write_global(self, global_text)  # pyright: ignore[reportArgumentType]

    def write_project(self, text):
        _GateCase.write_project(self, text)  # pyright: ignore[reportArgumentType]

    def unenforced_mock(self):
        _GateCase.unenforced_mock(self)  # pyright: ignore[reportArgumentType]

    def answer(self, response):
        _GateCase.answer(self, response)  # pyright: ignore[reportArgumentType]

    def count_mock_runs(self):
        calls = []
        original = MockProvider.run
        self.addCleanup(setattr, MockProvider, "run", original)
        setattr(MockProvider, "run", lambda provider, *a, **k: calls.append(a) or original(provider, *a, **k))
        return calls


# --------------------------------------------------------------------------- design


class TestDesignRoundPinning(_Pinning, DesignReviewCase):
    def design_run(self, *argv):
        return run_cli("review", "run", "--design", *argv)

    def test_d1_the_round_budget_refusal(self):
        run_cli("config", "set", "review.design.max_iterations", "1")
        self.write_plan()
        self.assertEqual(self.design_run()[0], 0)
        self.write_plan(REVISED_PLAN)
        code, _, err = self.design_run()
        self.assertEqual(code, 3)
        self.assertEqual(
            err.splitlines()[-2:],
            [
                "refusing to run design review round 2: the budget is 1 rounds "
                "(review.design.max_iterations).",
                "The round that reached the limit still gets its revision; only the re-review is refused. "
                "Report what is still open, or pass --force to override.",
            ],
        )

    def test_d2_the_runtime_refusal_comes_before_the_context_refusal(self):
        run_cli("config", "set", "review.context.max_chars", "10")
        self.write_plan()
        spend_the_runtime_budget(self.workspace)
        code, _, err = self.design_run()
        self.assertEqual(code, 3)
        self.assertIn("refusing to run design review:", err.splitlines())
        last = "Report what is unresolved instead of retrying, or pass --force to override."
        self.assertEqual(err.splitlines()[-1], last)
        self.assertEqual([e for e in self.events("design_review") if e.get("status") == "refused"], [])
        self.assertFalse(os.path.isfile(self.design.snapshot_path))

    def test_d3_an_empty_panel_is_refused_for_runtime_before_the_freeze(self):
        run_cli("reviewer", "remove", "m1")
        run_cli("reviewer", "remove", "m2")
        self.write_plan()
        spend_the_runtime_budget(self.workspace)
        code, _, _ = self.design_run()
        self.assertEqual(code, 3)
        self.assertFalse(os.path.isfile(self.design.consolidated_json_path))
        self.assertFalse(os.path.isfile(self.design.snapshot_path))

    def test_d3_only_matching_nothing_is_refused_first(self):
        run_cli("reviewer", "remove", "m1")
        run_cli("reviewer", "remove", "m2")
        self.write_plan()
        code, _, err = self.design_run("--only", "x")
        self.assertEqual(code, 2)
        self.assertEqual(err.splitlines(), ["--only x matched no configured reviewer"])

    def test_d3_only_matches_by_role(self):
        # m2's role is architecture, and no reviewer has that id.
        self.write_plan()
        with self.capture_run_reviews() as captured:
            self.design_run("--only", "architecture")
        self.assertEqual([r.get("id") for r in captured["arguments"]["reviewers"]], ["m2"])

    def test_d3_only_drops_a_name_that_matches_nothing(self):
        self.write_plan()
        with self.capture_run_reviews() as captured:
            self.design_run("--only", "m1", "nope")
        self.assertEqual([r.get("id") for r in captured["arguments"]["reviewers"]], ["m1"])

    def test_d4_a_review_error_after_the_warning(self):
        self.enforcement_fixture("reviewers:\n" + AGY_REVIEWER)
        self.write_plan()
        with mock.patch.object(review_mod, "run_reviews", side_effect=review_mod.ReviewError("boom")):
            code, _, err = self.design_run()
        self.assertEqual(code, 2)
        warning = err.index("warning: reviewer gem: " + UNENFORCED)
        self.assertLess(warning, err.index("boom"))
        event = self.last_event("design_review")
        self.assert_event_keys(event, ["elapsed_seconds", "error"])
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["charged_seconds"], 0)
        self.assertEqual(self.workspace.read_state()["ledger"]["in_flight"], {})
        self.assertFalse(os.path.isfile(self.design.consolidated_json_path))

    def test_d5_the_identical_findings_note(self):
        self.write_plan()
        self.design_run()
        self.write_plan(REVISED_PLAN)
        _, _, err = self.design_run()
        self.assertIn(
            "note: design round 2 produced the same findings as the previous round -- "
            "the last revision changed nothing that the reviewers can see.",
            err.splitlines(),
        )

    def test_d5_the_stale_report_note(self):
        self.write_plan()
        self.design_run()
        self.write_plan(REVISED_PLAN)
        _, _, err = self.design_run("--only", "m1")
        note = "note: m2 has no report for this plan; its earlier report was ignored"
        self.assertIn(note, err.splitlines())

    def test_d6_the_report_and_the_event(self):
        self.write_plan()
        code, out, _ = self.design_run("--json")
        self.assertEqual(code, 0)
        self.assert_payload_keys(out, ["plan"])
        self.assert_event_keys(self.last_event("design_review"), DESIGN_TAIL)

        code, out, _ = self.design_run()
        self.assertEqual(code, 0)
        self.assert_report_tail(out, self.design, "2 successful, 0 failed")
        labels = self.labels()
        self.assertIn("design:m1", labels)
        self.assertIn("design:m2", labels)
        self.assertNotIn("m1", labels)
        self.assertNotIn("m2", labels)

    def test_d6_every_reviewer_failed_ends_with_1(self):
        self.write_plan()
        with mock.patch.object(review_fanout, "get_provider", side_effect=RuntimeError("no provider")):
            code, out, _ = self.design_run()
        self.assertEqual(code, 1)
        self.assert_report_tail(out, self.design, "0 successful, 2 failed")
        with mock.patch.object(review_fanout, "get_provider", side_effect=RuntimeError("no provider")):
            code, out, _ = self.design_run("--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual((payload["ok"], payload["failed"], payload["partial"]), (0, 2, 0))

    def test_d6_every_reviewer_partial_ends_with_1(self):
        self.write_oversize_plan()
        code, out, _ = self.design_run()
        self.assertEqual(code, 1)
        summary = "0 successful, 0 failed, 2 partial (change handed over as a file)"
        self.assert_report_tail(out, self.design, summary)
        code, out, _ = self.design_run("--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual((payload["ok"], payload["failed"], payload["partial"]), (0, 0, 2))

    def test_d6_suspended_time_sits_after_the_charge(self):
        os.environ["DEV_ORCHESTRA_MOCK_DELAY"] = "0.2"
        os.environ["DEV_ORCHESTRA_MOCK_SUSPENDED"] = "90"
        self.write_plan()
        self.assertEqual(self.design_run()[0], 0)
        self.assert_event_keys(self.last_event("design_review"), ["suspended_seconds", *DESIGN_TAIL])

    def test_d7_a_refused_reviewer_does_not_run(self):
        self.enforcement_fixture("")
        self.unenforced_mock()
        self.write_project("reviewers:\n  - id: r1\n    provider: mock\n    role: general\n")
        self.write_plan()
        calls = self.count_mock_runs()
        _, out, err = self.design_run()
        self.assertIn("reviewers on mock are taken only from the global config", out + err)
        self.assertEqual(calls, [])
        labels = self.labels()
        self.assertNotIn("r1", labels)
        self.assertNotIn("design:r1", labels)

    def test_d7_the_warning_is_said_once(self):
        self.enforcement_fixture("reviewers:\n" + AGY_REVIEWER)
        self.answer("NO_FINDINGS")
        self.write_plan()
        _, out, err = self.design_run()
        self.assertEqual(len(self.started), 1, out + err)
        self.assertEqual(err.count(UNENFORCED), 1, err)

    def test_d8_force_passes_both_refusals_and_is_charged(self):
        os.environ["DEV_ORCHESTRA_MOCK_DELAY"] = "0.2"
        run_cli("config", "set", "review.design.max_iterations", "1")
        self.write_plan()
        self.design_run()
        self.write_plan(REVISED_PLAN)
        spend_the_runtime_budget(self.workspace)
        code, _, err = self.design_run("--force")
        self.assertEqual(code, 0, err)
        event = self.last_event("design_review")
        self.assertEqual(event["status"], "ok")
        self.assertGreater(event["charged_seconds"], 0)
        self.assertNotIn("charge_skipped", event)

    def test_d9_what_the_panel_is_run_with(self):
        self.write_plan()
        self.write_request()
        run_cli("config", "set", "review.max_findings", "7")
        argv = ("--timeout", "77", "--idle-timeout", "5", "--sequential", "--context", "ctx-x")
        with self.capture_run_reviews() as captured:
            code, _, err = self.design_run(*argv)
        self.assertEqual(code, 2, err)
        arguments = captured["arguments"]
        self.assertEqual([r.get("id") for r in arguments["reviewers"]], ["m1", "m2"])
        self.assertEqual(arguments["extra_context"], "")
        self.assertIsNone(arguments["surrounding"])
        self.assertFalse(arguments["parallel"])
        self.assertEqual(arguments["timeout"], 77)
        self.assertEqual(arguments["idle_timeout"], 5.0)
        self.assertEqual(arguments["max_findings"], 7)
        self.assertFalse(arguments["over_budget"])
        plan_text = ws.read_text(self.workspace.plan_path)
        request_text = ws.read_text(os.path.join(self.workspace.execution_dir, "design-request.md"))
        self.assertEqual(arguments["budget_chars"], len(plan_text) + len(request_text))
        inline = config_mod.load(self.project).context_settings()["inline_chars"]
        self.assertEqual(arguments["inline_chars"], inline)
        self.assertEqual(arguments["refusals"], {})
        self.assertIn("ctx-x", arguments["prompt_for"](arguments["reviewers"][0]).text)
        self.assertEqual(captured["in_flight"]["deadline_seconds"], 77)

    def test_d9_the_idle_timeout_from_the_setting(self):
        self.write_plan()
        run_cli("config", "set", "review.idle_timeout_seconds", "42")
        with self.capture_run_reviews() as captured:
            self.design_run()
        self.assertEqual(captured["arguments"]["idle_timeout"], 42)


# --------------------------------------------------------------------------- code


class TestCodeRoundPinning(_Pinning, MeasuringCase):
    def setUp(self):
        super().setUp()
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m2", "--role", "general")
        self.use_finding()
        run_cli("config", "set", "optimization.level", "quality")
        self.edit_add()

    def snapshot(self, *argv):
        code, out, err = run_cli("review", "snapshot", *argv)
        self.assertEqual(code, 0, out + err)

    def code_run(self, *argv):
        return run_cli("review", "run", *argv)

    def test_c1_the_round_budget_refusal(self):
        run_cli("config", "set", "review.max_review_iterations", "1")
        self.snapshot()
        self.assertEqual(self.code_run()[0], 0)
        self.edit_add("return a // b")
        self.snapshot()
        code, _, err = self.code_run()
        self.assertEqual(code, 3)
        self.assertEqual(
            err.splitlines()[-2:],
            [
                "refusing to run review round 2: the budget is 1 rounds (review.max_review_iterations).",
                "The round that reached the limit still gets its fix and re-test; only the re-review is "
                "refused. Report what is still open, or pass --force to override.",
            ],
        )

    def test_c2_the_context_refusal_comes_before_the_runtime_refusal(self):
        run_cli("config", "set", "review.context.max_chars", "10")
        self.snapshot()
        spend_the_runtime_budget(self.workspace)
        code, _, err = self.code_run()
        self.assertEqual(code, 3)
        event = self.last_event("review")
        self.assertEqual(event["status"], "refused")
        self.assertEqual(event["refused_by"], "context")
        self.assertIn("review.context.max_chars", err)
        self.assertNotIn("refusing to run review:", err)

    def test_c3_the_gate_refusal_comes_before_the_runtime_refusal(self):
        run_cli("config", "set", "optimization.level", "balanced")
        self.snapshot()
        run_cli("state", "record", "test", "failed")
        status = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(status["optimization"]["gate"], "refuse")
        spend_the_runtime_budget(self.workspace)
        code, _, err = self.code_run()
        self.assertEqual(code, 3)
        self.assertIn("refusing to review: the last recorded test run failed.", err)
        self.assertNotIn("refusing to run review:", err)
        self.assertEqual(self.last_event("review")["refused_by"], "gate")

    def test_c4_an_empty_panel_skips_before_the_runtime_refusal(self):
        run_cli("reviewer", "remove", "m1")
        run_cli("reviewer", "remove", "m2")
        spend_the_runtime_budget(self.workspace)
        code, out, _ = self.code_run()
        self.assertEqual(code, 0)
        self.assertEqual(out, NO_REVIEWERS)

    def test_c4_only_matching_nothing_is_refused(self):
        run_cli("reviewer", "remove", "m1")
        run_cli("reviewer", "remove", "m2")
        code, _, err = self.code_run("--only", "x")
        self.assertEqual(code, 2)
        self.assertEqual(err, "--only x matched no configured reviewer\n")

    def test_c4_only_matches_by_role(self):
        self.snapshot()
        with self.capture_run_reviews() as captured:
            self.code_run("--only", "general")
        self.assertEqual([r.get("id") for r in captured["arguments"]["reviewers"]], ["m1", "m2"])

    def test_c4_only_drops_a_name_that_matches_nothing(self):
        self.snapshot()
        with self.capture_run_reviews() as captured:
            self.code_run("--only", "m1", "nope")
        self.assertEqual([r.get("id") for r in captured["arguments"]["reviewers"]], ["m1"])

    def test_c1_force_passes_the_round_budget_and_is_charged(self):
        os.environ["DEV_ORCHESTRA_MOCK_DELAY"] = "0.2"
        run_cli("config", "set", "review.max_review_iterations", "1")
        self.snapshot()
        self.assertEqual(self.code_run()[0], 0)
        self.edit_add("return a // b")
        self.snapshot()
        code, _, err = self.code_run("--force")
        self.assertEqual(code, 0, err)
        event = self.last_review_event()
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["iteration"], 2)
        self.assertGreater(event["charged_seconds"], 0)
        self.assertNotIn("charge_skipped", event)

    def incremental_snapshot(self):
        """A second round whose snapshot is the fix diff, frozen with the context."""
        self.snapshot()
        self.assertEqual(self.code_run()[0], 0)
        run_cli("review", "triage", "F1", "--status", "accepted")
        self.edit_add("return a + b + 0")
        self.snapshot("--surrounding", "enclosing")
        meta = ws.read_json(self.workspace.snapshot_meta_path, {})
        self.assertTrue(meta["incremental_from"])
        return meta

    def test_c2_surrounding_on_an_incremental_round_is_refused_before_any_cost(self):
        meta = self.incremental_snapshot()
        before = len(self.events("review"))
        used = json.loads(run_cli("budget", "show", "--json")[1])["runtime"]["used"]
        code, out, err = self.code_run("--surrounding", "enclosing")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(
            err,
            "refusing to run: --surrounding enclosing on an incremental round (fix diff since %s). The "
            "prompt of a re-review carries the accepted findings of the moment it runs, so two runs on "
            "it would differ in more than the surrounding context. Measure on a whole-change snapshot: "
            "the first round of a change, or a round after a clean or all-rejected review.\n"
            % meta["incremental_from"],
        )
        self.assertEqual(len(self.events("review")), before)
        self.assertEqual(self.workspace.read_state()["ledger"]["in_flight"], {})
        payload = json.loads(run_cli("budget", "show", "--json")[1])
        self.assertEqual(payload["runtime"]["used"], used)

    def test_c2_an_incremental_round_without_the_flag_runs(self):
        self.incremental_snapshot()
        code, _, err = self.code_run()
        self.assertEqual(code, 0, err)
        self.assertNotIn("incremental round", err)
        event = self.last_review_event()
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["iteration"], 2)

    def test_c5_a_review_error_after_the_warning(self):
        self.enforcement_fixture("optimization:\n  level: quality\nreviewers:\n" + AGY_REVIEWER)
        self.snapshot()
        with mock.patch.object(review_mod, "run_reviews", side_effect=review_mod.ReviewError("boom")):
            code, _, err = self.code_run()
        self.assertEqual(code, 2)
        self.assertLess(err.index("warning: reviewer gem: " + UNENFORCED), err.index("boom"))
        event = self.last_event("review")
        self.assert_event_keys(event, ["elapsed_seconds", "error"])
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["charged_seconds"], 0)
        self.assertEqual(self.workspace.read_state()["ledger"]["in_flight"], {})

    def test_c6_the_charge_and_the_usage_labels(self):
        os.environ["DEV_ORCHESTRA_MOCK_DELAY"] = "0.2"
        self.snapshot()
        self.assertEqual(self.code_run()[0], 0)
        event = self.last_review_event()
        self.assertEqual(len(event["reviewers"]), 2)
        expected = sum(r["duration_seconds"] - r.get("suspended_seconds", 0) for r in event["reviewers"])
        self.assertAlmostEqual(event["charged_seconds"], expected, places=1)
        payload = json.loads(run_cli("budget", "show", "--json")[1])
        self.assertAlmostEqual(payload["runtime"]["used"], event["charged_seconds"], places=1)
        labels = self.labels()
        self.assertIn("m1", labels)
        self.assertIn("m2", labels)
        self.assertEqual([label for label in labels if label.startswith("design:")], [])

    def test_c7_the_identical_findings_note_comes_before_the_cap_notes(self):
        run_cli("config", "set", "review.max_findings", "2")
        with open(os.path.join(self.tmp, "mock", "review.txt"), "w", encoding="utf-8") as handle:
            handle.write("".join(finding(i) for i in range(1, 4)))
        self.snapshot()
        self.code_run()
        _, _, err = self.code_run("--force")
        lines = err.splitlines()
        note = (
            "note: round 1 produced the same findings as the previous round -- "
            "the last fix changed nothing that the reviewers can see."
        )
        self.assertIn(note, lines)
        cap = next(index for index, line in enumerate(lines) if "against a cap of 2" in line)
        self.assertLess(lines.index(note), cap)

    def test_c7_the_stale_report_note(self):
        self.snapshot()
        self.code_run()
        self.edit_add("return a // b")
        self.snapshot()
        _, _, err = self.code_run("--only", "m1")
        note = "note: m2 has no report for this snapshot; its earlier report was ignored"
        self.assertIn(note, err.splitlines())

    def test_c7_a_rerun_says_nothing_about_identical_findings(self):
        self.snapshot("--surrounding", "enclosing")
        self.code_run("--surrounding", "none")
        code, _, err = self.code_run("--surrounding", "enclosing")
        self.assertEqual(code, 0, err)
        self.assertNotIn(
            "note: round 1 produced the same findings as the previous round -- "
            "the last fix changed nothing that the reviewers can see.",
            err,
        )

    def test_c8_plain(self):
        self.snapshot()
        code, out, _ = self.code_run("--json")
        self.assertEqual(code, 0)
        self.assert_payload_keys(out, ["optimization"])
        self.assert_event_keys(self.last_review_event(), CODE_TAIL)
        self.assertNotIn("measurement", self.consolidated())
        code, out, _ = self.code_run()
        self.assertEqual(code, 0)
        self.assert_report_tail(out, self.workspace, "2 successful, 0 failed")

    def test_c8_context_on_from_the_config(self):
        self.enable()
        self.snapshot()
        code, out, _ = self.code_run()
        self.assertEqual(code, 0)
        event = self.last_review_event()
        self.assertEqual(event["surrounding"]["adopted"], 1)
        chars = event["surrounding"]["adopted_chars"]
        line = "Surrounding context: 1 symbol(s), %s chars adopted" % "{:,}".format(chars)
        self.assert_report_tail(out, self.workspace, "2 successful, 0 failed", [line])

        code, out, _ = self.code_run("--json")
        self.assertEqual(code, 0)
        self.assert_payload_keys(out, ["optimization", "surrounding"])
        self.assert_event_keys(self.last_review_event(), [*CODE_TAIL, "surrounding"])

    def test_c8_context_on_and_every_reviewer_failed_first(self):
        self.enable()
        self.snapshot()
        with mock.patch.object(review_fanout, "get_provider", side_effect=RuntimeError("no provider")):
            code, out, _ = self.code_run("--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual((payload["ok"], payload["failed"], payload["partial"]), (0, 2, 0))
        self.assertIn("surrounding", payload)
        self.assertNotIn("surrounding", self.last_review_event())

    def test_c8_every_reviewer_failed_ends_with_1(self):
        self.snapshot()
        with mock.patch.object(review_fanout, "get_provider", side_effect=RuntimeError("no provider")):
            code, out, _ = self.code_run()
        self.assertEqual(code, 1)
        self.assert_report_tail(out, self.workspace, "0 successful, 2 failed")

    def test_c8_every_reviewer_partial_ends_with_1(self):
        run_cli("config", "set", "review.context.inline_chars", "10")
        self.snapshot()
        code, out, _ = self.code_run()
        self.assertEqual(code, 1)
        summary = "0 successful, 0 failed, 2 partial (change handed over as a file)"
        self.assert_report_tail(out, self.workspace, summary)
        code, out, _ = self.code_run("--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual((payload["ok"], payload["failed"], payload["partial"]), (0, 0, 2))

    def test_c8_the_surrounding_flag(self):
        self.snapshot("--surrounding", "enclosing")
        measurement = ["surrounding", "rerun", "triage_at_build"]

        code, out, err = self.code_run("--surrounding", "none")
        self.assertEqual(code, 0, err)
        line = (
            "Surrounding context: none (--surrounding none for this run; "
            "review.context.surrounding unchanged)"
        )
        self.assert_report_tail(out, self.workspace, "2 successful, 0 failed", [line])
        self.assertEqual(list(self.consolidated()["measurement"]), measurement)
        code, out, err = self.code_run("--surrounding", "none", "--json")
        self.assertEqual(code, 0, err)
        self.assert_payload_keys(out, ["optimization", "measurement"])
        self.assert_event_keys(self.last_review_event(), [*CODE_TAIL, "measurement"])

        code, out, err = self.code_run("--surrounding", "enclosing")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertTrue(lines[-2].startswith("Surrounding context: "), out)
        self.assertTrue(lines[-2].endswith(" (--surrounding enclosing for this run)"), out)
        self.assertEqual(list(self.consolidated()["measurement"]), measurement)
        code, out, err = self.code_run("--surrounding", "enclosing", "--json")
        self.assertEqual(code, 0, err)
        self.assert_payload_keys(out, ["optimization", "surrounding", "measurement"])
        self.assert_event_keys(self.last_review_event(), [*CODE_TAIL, "measurement", "surrounding"])

    def test_c9_a_refused_reviewer_is_not_accounted(self):
        self.enforcement_fixture("")
        self.unenforced_mock()
        self.write_project(
            "optimization:\n  level: quality\nreviewers:\n  - id: r1\n    provider: mock\n    role: general\n"
        )
        self.snapshot()
        _, out, err = self.code_run()
        self.assertIn("reviewers on mock are taken only from the global config", out + err)
        self.assertNotIn("r1", self.labels())

    def test_c10_what_the_panel_is_run_with(self):
        self.snapshot()
        max_findings = json.loads(run_cli("status", "--json")[1])["optimization"]["max_findings"]
        argv = ("--timeout", "77", "--idle-timeout", "5", "--sequential", "--context", "ctx-x")
        with self.capture_run_reviews() as captured:
            code, _, err = self.code_run(*argv)
        self.assertEqual(code, 2, err)
        arguments = captured["arguments"]
        self.assertEqual([r.get("id") for r in arguments["reviewers"]], ["m1", "m2"])
        self.assertIsNone(arguments["prompt_for"])
        self.assertFalse(arguments["parallel"])
        self.assertEqual(arguments["timeout"], 77)
        self.assertEqual(arguments["extra_context"], "ctx-x")
        self.assertEqual(arguments["idle_timeout"], 5.0)
        self.assertEqual(arguments["max_findings"], max_findings)
        self.assertFalse(arguments["over_budget"])
        self.assertEqual(arguments["budget_chars"], review_mod.snapshot_chars(self.workspace))
        inline = config_mod.load(self.project).context_settings()["inline_chars"]
        self.assertEqual(arguments["inline_chars"], inline)
        self.assertEqual(arguments["surrounding"].mode, "none")
        self.assertEqual(arguments["refusals"], {})
        self.assertEqual(captured["in_flight"]["deadline_seconds"], 77)

    def test_c10_the_idle_timeout_from_the_setting(self):
        self.snapshot()
        run_cli("config", "set", "review.idle_timeout_seconds", "42")
        with self.capture_run_reviews() as captured:
            self.code_run()
        self.assertEqual(captured["arguments"]["idle_timeout"], 42)

    def conditional_panel(self, level):
        """Two specialists and a conditional generalist.

        Cutting the panel before the conditional reviewer is filtered out
        would keep ``sec``, the only ``general`` one; cutting it after keeps
        ``m1``, the first configured.
        """
        run_cli("config", "set", "optimization.level", level)
        run_cli("reviewer", "set", "m1", "--role", "security")
        run_cli("reviewer", "set", "m2", "--role", "performance")
        argv = ("--provider", "mock", "--id", "sec", "--role", "general", "--when", "high-risk")
        run_cli("reviewer", "add", *argv)

    def runtime_used(self):
        return json.loads(run_cli("budget", "show", "--json")[1])["runtime"]["used"]

    def captured_ids(self, captured):
        return [r.get("id") for r in captured["arguments"]["reviewers"]]

    def test_g1_the_notes_in_order_and_the_limit_on_the_filtered_panel(self):
        self.conditional_panel("balanced")
        self.snapshot()
        meta = ws.read_json(self.workspace.snapshot_meta_path, {})
        lines = int(meta["lines_added"]) + int(meta["lines_deleted"])
        with self.capture_run_reviews() as captured:
            code, _, err = self.code_run()
        self.assertEqual(code, 2, err)
        limit = (
            "note: low-risk change (1 file(s), %d line(s)): 1 reviewer instead of the full panel (m1). "
            "Cross-model disagreement is what a second reviewer buys; raise optimization.level or the "
            "low_risk thresholds to keep it." % lines
        )
        notes = ["note: sec (when: high-risk) left out: no high-risk path matched", GATE_WARN_NOTE, limit]
        self.assertEqual([line for line in err.splitlines() if line in notes], notes)
        self.assertEqual(self.captured_ids(captured), ["m1"])

    def test_g1_an_escalated_round_adds_the_conditional_reviewer(self):
        self.conditional_panel("balanced")
        self.write("auth.py", "def check(user):\n    return True\n")
        self.snapshot()
        with self.capture_run_reviews() as captured:
            code, _, err = self.code_run()
        self.assertEqual(code, 2, err)
        said = err.splitlines()
        escalation = "note: balanced → quality: auth.py matches *auth*"
        added = "note: sec (when: high-risk) added: auth.py matches *auth*"
        self.assertLess(said.index(escalation), said.index(added))
        self.assertNotIn(GATE_WARN_NOTE, said)
        self.assertNotIn("low-risk change", err)
        self.assertEqual(self.captured_ids(captured), ["m1", "m2", "sec"])

    def test_g2_the_context_refusal_says_nothing_after_it(self):
        self.conditional_panel("balanced")
        run_cli("config", "set", "review.context.max_chars", "10")
        self.snapshot()
        used = self.runtime_used()
        code, _, err = self.code_run()
        self.assertEqual(code, 3)
        self.assertNotIn(GATE_WARN_NOTE, err.splitlines())
        self.assertNotIn("low-risk change", err)
        self.assertEqual(self.ledger().get("in_flight") or {}, {})
        self.assertEqual([event["status"] for event in self.events("review")], ["refused"])
        self.assertEqual(self.last_event("review")["refused_by"], "context")
        self.assertEqual(self.runtime_used(), used)
        with self.capture_run_reviews() as captured:
            code, _, err = self.code_run("--force")
        self.assertEqual(code, 2, err)
        self.assertTrue(captured["arguments"]["over_budget"])

    def test_g3_the_gate_refusal_opens_nothing(self):
        self.conditional_panel("balanced")
        self.snapshot()
        run_cli("state", "record", "test", "failed")
        code, _, err = self.code_run()
        self.assertEqual(code, 3, err)
        self.assertEqual(self.ledger().get("in_flight") or {}, {})
        refused = [(event["status"], event.get("refused_by")) for event in self.events("review")]
        self.assertEqual(refused, [("refused", "gate")])
        with self.capture_run_reviews() as captured:
            code, _, err = self.code_run("--force")
        self.assertEqual(code, 2, err)
        self.assertEqual(self.captured_ids(captured), ["m1"])

    def test_g4_only_skips_the_limit_and_leaves_nobody_out(self):
        self.conditional_panel("balanced")
        self.snapshot()
        with self.capture_run_reviews() as captured:
            _, _, err = self.code_run("--only", "m1", "m2")
        self.assertEqual(self.captured_ids(captured), ["m1", "m2"])
        self.assertNotIn("low-risk change", err)
        code, _, err = self.code_run("--only", "m1", "m2")
        self.assertEqual(code, 0, err)
        event = self.last_review_event()
        self.assertEqual([run["id"] for run in event["reviewers"]], ["m1", "m2"])
        self.assertEqual(event["optimization"]["conditional"], [])
        self.assertEqual([entry["id"] for entry in self.consolidated()["reviewers"]], ["m1", "m2"])

    def test_g5_a_left_out_reviewers_report_stays_out(self):
        self.conditional_panel("quality")
        self.snapshot()
        code, _, err = self.code_run("--only", "sec")
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(self.workspace.reviewer_report_path("sec")))
        code, _, err = self.code_run()
        self.assertEqual(code, 0, err)
        data = self.consolidated()
        self.assertEqual([entry["id"] for entry in data["reviewers"]], ["m1", "m2"])
        self.assertTrue(data["findings"])
        for item in data["findings"]:
            self.assertNotIn("sec", item["reported_by"])


# --------------------------------------------------------------------------- status


def status_head(budget_key):
    """The keys every ``review status --json`` payload starts with."""
    return [
        "iteration",
        budget_key,
        "blocking",
        "blocking_count",
        "accepted_count",
        "re_review_recommended",
        "iteration_budget_exhausted",
        "coverage",
        "reviewers_partial",
        "over_budget",
        "counts",
    ]


CODE_STATUS = [*status_head("max_review_iterations"), "final_fix", "final_fix_pending"]

DESIGN_STATUS = [
    *status_head("max_iterations"),
    "final_revision",
    "final_revision_pending",
    "enabled",
    "mode",
    "reason",
]


class TestStatusPinning(IsolatedCase):
    """What ``review status`` says about the report on disk, on both paths."""

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        self.workspace = self.cli_workspace()

    def report(self, data, design=False):
        workspace = self.workspace.design_review().ensure() if design else self.workspace
        ws.write_json(workspace.consolidated_json_path, data)

    def status(self, *argv):
        code, out, err = run_cli("review", "status", *argv)
        self.assertEqual(code, 0, err)
        return out

    def payload(self, *argv):
        return json.loads(self.status("--json", *argv))

    def test_the_keys_of_each_payload(self):
        data = {"iteration": 1, "coverage": {"round": "complete", "change": "complete"}, "counts": {}}
        self.report(data)
        self.report(data, design=True)
        self.assertEqual(list(self.payload()), CODE_STATUS)
        self.assertEqual(list(self.payload("--design")), DESIGN_STATUS)

    def test_the_surrounding_context_sits_before_the_final_pass(self):
        block = {"shared": True, "adopted": [], "adopted_chars": 0, "reason": "not frozen"}
        self.report({"iteration": 1, "counts": {}, "surrounding": block})
        self.report({"iteration": 1, "counts": {}, "surrounding": block}, design=True)
        self.assertEqual(list(self.payload()), [*CODE_STATUS[:-2], "surrounding", *CODE_STATUS[-2:]])
        design = [*DESIGN_STATUS[:11], "surrounding", *DESIGN_STATUS[11:]]
        self.assertEqual(list(self.payload("--design")), design)
        self.assertEqual(self.payload()["surrounding"], block)

    def test_a_surrounding_record_that_is_not_one_is_left_out(self):
        self.report({"iteration": 1, "counts": {}, "surrounding": "enclosing"})
        self.assertEqual(list(self.payload()), CODE_STATUS)
        self.assertNotIn("surrounding context", self.status())

    def test_no_report_is_the_one_real_none(self):
        none = {
            "round": "none",
            "change": "none",
            "unverified_since": None,
            "change_chars": None,
            "inline_chars": None,
        }
        coverage = self.payload()["coverage"]
        self.assertEqual(coverage, none)
        self.assertEqual(list(coverage), list(none))
        self.assertIn("coverage: round none, change none\n", self.status())

    def test_a_report_from_before_coverage(self):
        self.report({"iteration": 1, "counts": {}, "coverage": "complete"})
        self.assertIsNone(self.payload()["coverage"])
        self.assertIn("coverage: not recorded -- this report predates it\n", self.status())

    def test_the_over_budget_line(self):
        self.report({"iteration": 1, "counts": {}, "snapshot": {"over_budget": True}})
        self.assertTrue(self.payload()["over_budget"])
        line = (
            "over budget: this round was sent past review.context.max_chars by --force; "
            "say so when you report it\n"
        )
        self.assertIn(line, self.status())


#: One accepted blocking finding, so the final pass has something to fix.
BLOCKING = {"id": "F1", "severity": "high", "triage": "accepted", "title": "t"}


class TestStatusFinalPassPinning(IsolatedCase):
    """The final pass ``review status`` reports once the round budget is spent.

    Each case is built so that swapping two of the keyword arguments the final
    pass is worked out from changes its answer.
    """

    LIMIT = 3

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "review.max_review_iterations", str(self.LIMIT))
        run_cli("config", "set", "review.design.max_iterations", str(self.LIMIT))
        self.workspace = self.cli_workspace()

    def report(self, design=False):
        workspace = self.workspace.design_review().ensure() if design else self.workspace
        data = {"iteration": self.LIMIT, "counts": {}, "findings": [dict(BLOCKING)]}
        ws.write_json(workspace.consolidated_json_path, data)

    def history(self, *events, repeats=None):
        """Write the run log, with ``repeats`` as ``{stage: n}`` in the ledger."""
        state = self.workspace.read_state()
        state["events"] = [ws.new_event(stage, status, detail) for stage, status, detail in events]
        signatures = {stage: {"repeats": n} for stage, n in (repeats or {}).items()}
        state["ledger"] = {
            "epoch": "pinned",
            "last_activity_monotonic": time.time(),
            "attempts": {},
            "signatures": signatures,
        }
        self.workspace.write_state(state)

    def status(self, *argv):
        code, out, err = run_cli("review", "status", *argv)
        self.assertEqual(code, 0, err)
        return out

    def payload(self, *argv):
        return json.loads(self.status("--json", *argv))

    def assert_exhausted(self, out, advice):
        self.assertIn("iteration %d/%d\n" % (self.LIMIT, self.LIMIT), out)
        self.assertIn("iteration budget exhausted -- %s\n" % advice, out)

    # -- code

    ROUND = ("review", "ok", {"reviewers": [{"invoked": True}]})

    def code_case(self, *events, repeats=None):
        self.report()
        self.history(self.ROUND, *events, repeats=repeats)
        payload = self.payload()
        self.assertEqual(payload["max_review_iterations"], self.LIMIT)
        self.assertEqual(payload["blocking"], ["F1"])
        self.assertTrue(payload["iteration_budget_exhausted"])
        self.assertFalse(payload["re_review_recommended"])
        return payload, self.status()

    def test_code_a_fix_without_a_re_test(self):
        payload, out = self.code_case(("review_fixer", "ok", None), repeats={"review": 2})
        self.assertEqual((payload["final_fix"], payload["final_fix_pending"]), ("retest", False))
        advice = (
            "the fix after this round is not re-reviewed; re-run the tests, record them "
            "(state record test ok|failed), then report the remaining findings "
            "(this round repeated the previous round's findings)"
        )
        self.assert_exhausted(out, advice)

    def test_code_a_fix_and_a_re_test(self):
        payload, out = self.code_case(("review_fixer", "ok", None), ("test", "ok", None))
        self.assertEqual((payload["final_fix"], payload["final_fix_pending"]), ("done", False))
        advice = "fixed and re-tested after this round, not re-reviewed; report the remaining findings"
        self.assert_exhausted(out, advice)

    def test_code_a_re_test_without_a_fix_is_still_owed_the_fix(self):
        payload, out = self.code_case(("test", "ok", None), repeats={"review": 2})
        self.assertEqual((payload["final_fix"], payload["final_fix_pending"]), ("pending", True))
        advice = (
            "fix the accepted findings once more (review fix-brief, then run review_fixer), re-test "
            "and record it (state record test ok|failed), and do not re-review; then report what is "
            "still open (this round repeated the previous round's findings)"
        )
        self.assert_exhausted(out, advice)

    def test_code_no_fixer_attempt_left(self):
        run_cli("config", "set", "budgets.review_fixer", "0")
        payload, out = self.code_case(repeats={"review": 2})
        self.assertEqual((payload["final_fix"], payload["final_fix_pending"]), ("blocked", False))
        self.assertIn("no review_fixer attempt is left (budgets.review_fixer)", out)
        self.assertNotIn("repeated the previous round", out)

    # -- design

    DESIGN_ROUND = ("design_review", "ok", {"reviewers": [{"invoked": True}]})

    def design_case(self, *events, repeats=None):
        ws.write_text(self.workspace.plan_path, REVISED_PLAN)
        # Older than any event, so an implementer run below counts as newer.
        past = time.time() - 3600
        os.utime(self.workspace.plan_path, (past, past))
        review = self.workspace.design_review().ensure()
        ws.write_text(review.snapshot_path, REVISED_PLAN)
        self.report(design=True)
        self.history(self.DESIGN_ROUND, *events, repeats=repeats)
        payload = self.payload("--design")
        self.assertEqual(payload["max_iterations"], self.LIMIT)
        self.assertEqual(payload["blocking"], ["F1"])
        self.assertTrue(payload["iteration_budget_exhausted"])
        return payload, self.status("--design")

    def test_design_an_architect_run_that_left_the_plan_unchanged(self):
        architect = ("architect", "ok", {"output": self.workspace.plan_path})
        payload, out = self.design_case(architect, repeats={"design_review": 2})
        self.assertEqual((payload["final_revision"], payload["final_revision_pending"]), ("done", False))
        advice = (
            "the architect ran after this round and left the plan unchanged; present the plan and "
            "the open findings and ask whether to approve over them or to triage them again"
        )
        self.assert_exhausted(out, advice)

    def test_design_a_plan_already_implemented(self):
        payload, out = self.design_case(("implementer", "ok", None), repeats={"design_review": 2})
        self.assertEqual(
            (payload["final_revision"], payload["final_revision_pending"]), ("implemented", False)
        )
        self.assert_exhausted(out, "report the remaining findings instead of looping")

    def test_design_a_revision_still_owed(self):
        payload, out = self.design_case(repeats={"design_review": 2})
        self.assertEqual((payload["final_revision"], payload["final_revision_pending"]), ("pending", True))
        advice = (
            "fold the accepted findings into the plan once more (review fix-brief --design, then "
            "run architect) and do not re-review it; then present the plan and the findings to the "
            "user and ask (this round repeated the previous round's findings)"
        )
        self.assert_exhausted(out, advice)

    def test_design_no_architect_attempt_left(self):
        run_cli("config", "set", "budgets.architect", "0")
        payload, out = self.design_case()
        self.assertEqual((payload["final_revision"], payload["final_revision_pending"]), ("blocked", False))
        self.assertIn("no architect attempt is left (budgets.architect)", out)


# --------------------------------------------------------------------------- fan-out

#: Every reviewer entry starts with these, in this order.
ENTRY_KEYS = [
    "id",
    "provider",
    "role",
    "model",
    "status",
    "error",
    "duration_seconds",
    "findings",
    "report",
    "invoked",
    "delivery",
    "change_chars",
    "inline_chars",
    "snapshot",
    "over_budget",
    "budget_chars",
    "usage",
]


class _Answers:
    """A provider whose run returns one ``RunResult`` or raises one exception."""

    def __init__(self, outcome):
        self.outcome = outcome

    def run(self, *args, **kwargs):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def result(ok=True, stdout="NO_FINDINGS", stderr="", exit_code=0, duration=2.0, **kwargs):
    return RunResult(ok, exit_code, stdout, stderr, [], duration, **kwargs)


@unittest.skipUnless(has_git(), "git is required")
class TestFanoutEntries(IsolatedCase):
    """What one reviewer's entry records, for every way its run can end."""

    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write("app.py", "def add(a, b):\n    return a - b\n")
        self.workspace = self.cli_workspace()
        review_mod.create_snapshot(self.workspace)
        self.stamp = review_mod.current_snapshot_stamp(self.workspace)

    def fan_out(self, **options):
        fan_out = review_mod.FanoutOptions(**options)
        (run,) = review_mod.run_reviews([reviewer("r1")], self.workspace, fan_out)
        return run.to_dict()

    def entry(self, outcome, **options):
        """The entry of a round of one reviewer whose provider answers ``outcome``."""
        with mock.patch.object(review_fanout, "get_provider", return_value=_Answers(outcome)):
            return self.fan_out(**options)

    def assert_entry(self, entry, status, error, tail=()):
        self.assertEqual(list(entry), [*ENTRY_KEYS, *tail])
        self.assertEqual(entry["status"], status)
        self.assertEqual(entry["error"], error)

    def test_a_provider_that_cannot_be_had(self):
        adoption = context_mod.Adoption(mode="enclosing", reason="not frozen")
        with mock.patch.object(review_fanout, "get_provider", side_effect=RuntimeError("no provider")):
            entry = self.fan_out(surrounding=adoption)
        self.assert_entry(entry, "failed", "no provider")
        self.assertEqual((entry["delivery"], entry["snapshot"], entry["change_chars"]), ("", "", 0))
        self.assertFalse(entry["invoked"])

    def test_a_refused_reviewer(self):
        entry = self.entry(result(), refusals={"r1": "reviewer r1: refused here"})
        self.assert_entry(entry, "failed", "reviewer r1: refused here")
        self.assertEqual((entry["delivery"], entry["snapshot"]), ("inline", self.stamp))
        self.assertFalse(entry["invoked"])

    def test_a_model_that_does_not_resolve(self):
        entry = self.entry(ModelResolutionError("mock: 'x' does not resolve"))
        self.assert_entry(entry, "failed", "mock: 'x' does not resolve")
        self.assertEqual(entry["snapshot"], self.stamp)

    def test_any_other_raise(self):
        self.assert_entry(self.entry(ValueError("boom")), "failed", "ValueError: boom")

    def test_a_stalled_run(self):
        outcome = result(False, "", "", 1, stalled=True, idle_for=30.0, suspended=1.5, warnings=["w"])
        entry = self.entry(outcome)
        tail = ["warnings", "suspended_seconds"]
        self.assert_entry(entry, "stalled", "no output for 30s; treated as wedged", tail)
        self.assertEqual((entry["warnings"], entry["suspended_seconds"]), (["w"], 1.5))

    def test_a_run_that_hit_its_deadline(self):
        entry = self.entry(result(False, "", "", 1, duration=5.0, timed_out=True))
        self.assert_entry(entry, "stalled", "hit its 5s deadline")

    def test_a_failed_run_says_its_last_line(self):
        cases = (
            ("", "first\nlast from stderr\n", "last from stderr"),
            ("first\nlast from stdout\n", "", "last from stdout"),
            ("from stdout", "from stderr", "from stderr"),
            ("", "", "exit code 3"),
        )
        for stdout, stderr, error in cases:
            with self.subTest(stdout=stdout, stderr=stderr):
                self.assert_entry(self.entry(result(False, stdout, stderr, 3)), "failed", error)

    def test_an_ok_run_with_the_context_off(self):
        entry = self.entry(result())
        self.assert_entry(entry, "ok", "")
        self.assertEqual(entry["report"], self.workspace.relative(self.workspace.reviewer_report_path("r1")))

    def test_an_ok_run_with_the_context_on(self):
        adoption = context_mod.Adoption(mode="enclosing", reason="not frozen")
        entry = self.entry(result(warnings=["w"]), surrounding=adoption)
        self.assert_entry(entry, "ok", "", ["surrounding", "warnings"])
        self.assertEqual(entry["surrounding"], adoption.record())

    def test_an_unparsed_report(self):
        entry = self.entry(result(stdout="Looks good to me.\n"))
        self.assert_entry(entry, "unparsed", review_mod.unparsed_report_warning("Looks good to me.", []))

    def test_a_partial_run(self):
        entry = self.entry(result(), inline_chars=10)
        chars = format(len(ws.read_text(self.workspace.snapshot_path)), ",")
        error = (
            "coverage unverified: the change body (%s chars) was handed over as a file, not inlined "
            "(over review.context.inline_chars, 10). Findings kept; not a clean review." % chars
        )
        self.assert_entry(entry, "partial", error)
        self.assertEqual((entry["delivery"], entry["inline_chars"]), ("file", 10))

    def test_an_unparsed_report_handed_over_as_a_file_is_unparsed(self):
        entry = self.entry(result(stdout="Looks good to me.\n"), inline_chars=10)
        self.assert_entry(entry, "unparsed", review_mod.unparsed_report_warning("Looks good to me.", []))
        self.assertEqual(entry["delivery"], "file")

    def test_an_unresolved_model_and_an_empty_answer(self):
        entry = self.entry(result(stdout=""))
        self.assert_entry(entry, "ok", "")
        self.assertEqual(entry["model"], "")
        expected = (
            "# Review\n\n- Reviewer: r1\n- Provider: mock\n- Model: unknown\n- Role: general\n"
            "- Snapshot: %s\n\n---\n\nNO_FINDINGS\n" % self.stamp
        )
        self.assertEqual(ws.read_text(self.workspace.reviewer_report_path("r1")), expected)

    def test_the_report_header(self):
        resolved = ResolvedModel("mock", "small", "latest", None, "mock-small", "test")
        entry = self.entry(result(stdout="NO_FINDINGS\n", resolved=resolved))
        self.assertEqual(entry["model"], "mock-small")
        expected = (
            "# Review\n\n- Reviewer: r1\n- Provider: mock\n- Model: mock-small\n- Role: general\n"
            "- Snapshot: %s\n\n---\n\nNO_FINDINGS\n" % self.stamp
        )
        self.assertEqual(ws.read_text(self.workspace.reviewer_report_path("r1")), expected)


if __name__ == "__main__":
    unittest.main()
