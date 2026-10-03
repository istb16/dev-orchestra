"""Whole-output pins on the commands whose text is built apart from printing it.

Each expected text is the full output, compared for equality: a renderer
moved into a function of its own must print what the command printed before,
byte for byte, and an ``assertIn`` cannot say that. The fixtures start from
the real empty values -- ``summarise_rounds([])``, a real ``status`` verdict,
an empty token report -- so every key the renderer reads is there.
"""

from __future__ import annotations

import copy
import datetime
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from helpers import IsolatedCase
from test_design_approval import ApprovalCase
from test_design_review import PLAN, REVISED_PLAN

from orchestrator import approval as approval_mod
from orchestrator import cli, cli_workflow, doctor, optimization_render
from orchestrator import config as config_mod
from orchestrator import ledger as ledger_mod
from orchestrator import optimization as opt_mod
from orchestrator import optimization_report as opt_report
from orchestrator import review as review_mod
from orchestrator import workflow as workflow_mod
from orchestrator import workspace as ws


def run_cli(*argv: str) -> "tuple[int, str, str]":
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def text(lines: List[str]) -> str:
    """What printing ``lines`` one at a time puts on stdout."""
    return "".join(line + "\n" for line in lines)


def merged(base: Dict[str, Any], **changes: Any) -> Dict[str, Any]:
    """A copy of ``base`` with ``changes`` laid over it."""
    result = copy.deepcopy(base)
    result.update(changes)
    return result


# --------------------------------------------------------------------------- optimization report


def _rounds(**changes: Any) -> Dict[str, Any]:
    """``summarise_rounds`` over no events, with ``changes`` laid over it."""
    return merged(opt_report.summarise_rounds([]), **changes)


def _score(**changes: Any) -> Dict[str, Any]:
    """An empty scorecard group, as ``reviewer_scorecard`` finishes one."""
    return merged(opt_report.reviewer_scorecard([])["code"]["panel"], **changes)


def _revisions(**changes: Any) -> Dict[str, Any]:
    """``architect_revisions`` over no workflows, with ``changes`` laid over it."""
    return merged(opt_report.architect_revisions([]), **changes)


def _revision_group(failed: Dict[str, int], **changes: Any) -> Dict[str, Any]:
    group = merged(opt_report.architect_revisions([])["resumed"], **changes)
    group["failed_attempts"].update(failed)
    return group


def _full_rounds() -> Dict[str, Any]:
    empty = opt_report.summarise_rounds([])
    by_context = copy.deepcopy(empty["by_context"])
    by_context["with"].update(
        rounds=2,
        reviewer_runs=4,
        billed_tokens=20000,
        billed_per_round=10000,
        tool_reported_runs=3,
        tool_uses_per_run=5.0,
        tool_output_chars_per_run=2000.0,
        adopted_chars=1500,
        trimmed_chars=200,
        sized_rounds=2,
        change_chars=4000,
        sized_billed_runs=4,
        billed_per_run_per_1k_change_chars=2500.0,
    )
    pair = {
        "workflow": "wf1",
        "snapshot": "abcdef0123456789",
        "change_chars": 5000,
        "panel": [{"id": "m1", "provider": "mock", "model": "fam", "role": "general"}],
        "without_panel": [{"id": "m2", "provider": "mock", "model": None, "role": None}],
        "adopted_chars": 0,
        "context_chars": 2345,
        "trimmed_chars": 100,
        "same_panel": False,
        "delivered": False,
        "undelivered": [{"side": "with", "id": "m1", "status": "failed"}],
        "same_inputs": False,
        "inputs_differ": ["context", "prompt"],
        "nothing_adopted": True,
        "with": {
            "reviewer_runs": 1,
            "billed_tokens": 1500,
            "billed_per_run": 1500.0,
            "tool_reported_runs": 1,
            "tool_uses_per_run": 4.0,
            "tool_output_chars_per_run": 1234.5,
        },
        "without": {
            "reviewer_runs": 1,
            "billed_tokens": 1000,
            "billed_per_run": 1000.0,
            "tool_reported_runs": 0,
            "tool_uses_per_run": None,
            "tool_output_chars_per_run": None,
        },
        "delta": {"billed_per_run": 500.0, "tool_uses_per_run": None, "tool_output_chars_per_run": None},
    }
    paired = merged(empty["paired"], pairs=[pair], pairs_listed=1)
    return _rounds(
        rounds=5,
        ran=3,
        refused=2,
        refused_by={"gate": 1, "context": 1},
        levels={"balanced": 3, "quality": 2},
        gates={"pass": 3, "refuse": 2},
        escalated=5,
        escalation_patterns={"auth/**": 5},
        always_escalated=True,
        panel_reduced=1,
        conditional={"added": 2, "left_out": 1, "declared_rounds": 1},
        rounds_without_a_test_result=1,
        reviewer_runs=6,
        measured_runs=5,
        billed_tokens=30000,
        billed_per_round=10000,
        estimated_saving=10000,
        design_rounds=2,
        design_refused=1,
        design_reviewer_runs=4,
        design_measured_runs=4,
        design_billed_tokens=12000,
        design_billed_per_round=6000,
        tool_reported_runs=4,
        tool_uses_per_run=6.5,
        tool_output_chars_per_run=12345.6,
        design_tool_reported_runs=2,
        design_tool_uses_per_run=3.0,
        design_tool_output_chars_per_run=1500.0,
        by_context=by_context,
        paired=paired,
    )


def _full_scorecard() -> Dict[str, Any]:
    scorecard = opt_report.reviewer_scorecard([])
    m1 = _score(
        reported=3,
        accepted=2,
        rejected=1,
        alone=2,
        alone_accepted=1,
        runs=2,
        failed_runs=1,
        measured_runs=2,
        billed_tokens=12345,
        priced_runs=1,
        cost_usd=0.5,
    )
    sec = _score(alone=0, alone_accepted=0, runs=1, rejection_rate=0.25, when="risk: auth", left_out_rounds=2)
    panel = _score(
        reported=3,
        accepted=2,
        rejected=1,
        runs=3,
        failed_runs=1,
        measured_runs=2,
        billed_tokens=12345,
        priced_runs=1,
        cost_usd=0.5,
        rejection_rate=0.333,
        billed_per_accepted=6172,
        cost_per_accepted=0.25,
    )
    scorecard["code"].update(
        rounds_recorded=4,
        rounds_read=2,
        rounds_unreviewed=1,
        rerun_rounds=1,
        reviewers={"sec": sec, "m1": m1},
        panel=panel,
    )
    scorecard["total"].update(
        accepted=2, rounds_read=2, rounds_recorded=4, billed_tokens=12345, priced_runs=1, cost_usd=0.5
    )
    return scorecard


def _full_revisions() -> Dict[str, Any]:
    resumed = _revision_group(
        {"stalled": 1, "priced": 1},
        runs=1,
        priced_runs=1,
        ratio_runs=1,
        cost_ratio_mean=0.75,
        cost_per_completed_ratio=1.25,
    )
    fresh = _revision_group({"rejected": 1, "failed": 1}, runs=1)
    fallbacks = {"total": 3, "reasons": {"no-session": 1, "budget": 2}}
    return _revisions(attempts=3, resumed=resumed, fresh=fresh, fallbacks=fallbacks)


FULL_TEXT = [
    "Review rounds recorded: 5 (3 ran, 2 refused)",
    "  refused by             context x1, gate x1",
    "  levels in force        balanced x3, quality x2",
    "  gate verdicts          pass x3, refuse x2",
    "  panel reduced          1",
    "  conditional reviewers  added x2, left out x1, declared with --high-risk x1",
    "  escalated (high risk)  5",
    "    caused by            auth/** x5",
    "  every round escalated, so the level you configured never applied. "
    "Narrow optimization.high_risk_paths and optimization.extra_high_risk_paths, "
    "or accept that this repository reviews at quality.",
    "",
    "Reviewer runs: 10 (9 reported usage), 42,000 billed",
    "  code review            6 (5 reported usage), 30,000 billed over 3 round(s), 10,000 each",
    "  design review          4 (4 reported usage), 12,000 billed over 2 round(s), 6,000 each",
    "",
    "Tool activity, per run and only over the runs that reported it:",
    "  code review            6.5 use(s)/run, 12,345.6 observed output chars/run (4 of 6 run(s) reported)",
    "  design review          3.0 use(s)/run, 1,500.0 observed output chars/run (2 of 4 run(s) reported)",
    "  Divided by the runs that reported it, never by every reviewer run:",
    "  Codex reports none, and a mixed panel would otherwise halve the figure",
    "  for no reason but its composition.",
    "  Observed output is what the tools printed back, not source read: `wc -l`",
    "  returns 3 characters for a 200-line file and `cat` returns the file.",
    "",
    "Surrounding context (review.context.surrounding), code review rounds only:",
    "  with context           2 round(s), 4 run(s), 20,000 billed, 10,000 per round; "
    "5.0 use(s)/run, 2,000.0 observed output chars/run (3 of 4 run(s) reported); "
    "1,500 context chars adopted, 200 left out",
    "                         per run and 1k chars of change (2 sized round(s), 4,000 chars): "
    "2,500.0 billed over 4 billed run(s), - observed output chars over 0 reporting run(s)",
    "  without context        0 round(s), 0 run(s), 0 billed, - per round; "
    "- use(s)/run, - observed output chars/run (0 of 0 run(s) reported)",
    "                         per run and 1k chars of change (0 sized round(s), 0 chars): "
    "- billed over 0 billed run(s), - observed output chars over 0 reporting run(s)",
    "  The raw figures move with the size of each change and with the number of reviewers in the",
    "  panel; compare the per-run lines. Codex reports no tool activity, by design: its runs are in",
    "  the billed figures and out of the tool ones, which is why each tool figure names the runs it",
    "  was divided by.",
    "",
    "Paired on one snapshot (--surrounding none vs enclosing: the same frozen diff, tree, panel "
    "and prompt inputs):",
    "  abcdef012345 in wf1   change 5,000 chars; panel m1 (mock, fam, general); "
    "0 context chars adopted (2,345 as carried), 100 left out; panels differ (without: m2 (mock, ?, ?)); "
    "not delivered (with: m1 failed); inputs differ (context, prompt); nothing adopted",
    "      with:    1 run(s), 1,500 billed, 1,500.0 per run; "
    "4.0 use(s)/run, 1,234.5 observed output chars/run (1 of 1 run(s) reported)",
    "      without: 1 run(s), 1,000 billed, 1,000.0 per run; "
    "- use(s)/run, - observed output chars/run (0 of 1 run(s) reported)",
    "      delta:   500.0 billed/run, - use(s)/run, - observed output chars/run",
    "  total, 0 pair(s) counted   with: - billed/run, - use(s)/run, - observed output chars/run; "
    "without: -, -, -; delta: -, -, -",
    "  0 pair(s) is a small-sample observation, not a statistical result. A counted",
    "  pair holds the change, the tree, the panel, the delivered reviews and the other prompt",
    "  inputs equal; what it does not hold equal is the reviewers' own run-to-run variation, so",
    "  one pair says what happened once. Codex reports no tool activity, so the tool figures",
    "  are over the runs that reported them. A pair is listed but left out of the total when",
    '  its panels differ ("panels differ"), when a reviewer run on either side did not deliver',
    '  ("not delivered"), when the two runs had different prompt inputs ("inputs differ"), or',
    '  when the enclosing run adopted nothing ("nothing adopted").',
    "",
    "Reviewer scorecard, code review: 2 of 4 recorded round(s) had a report to read; "
    "1 round(s) no reviewer reviewed.",
    "  m1                     3 reported: 2 accepted, 1 rejected, 0 duplicate, 0 open; "
    "2 found alone (1 accepted)",
    "                         2 run(s) (1 failed), 12,345 billed, $0.50 over 1 priced run(s); "
    "rates withheld under 10 decided",
    "  sec                    0 reported: 0 accepted, 0 rejected, 0 duplicate, 0 open; "
    "0 found alone (0 accepted)",
    "                         1 run(s), nothing reported; 25% rejected, "
    "per accepted withheld under 10 accepted (when: risk: auth; left out of 2 round(s))",
    "  panel                  3 reported: 2 accepted, 1 rejected, 0 duplicate, 0 open",
    "                         3 run(s) (1 failed), 12,345 billed, $0.50 over 1 of 3 run(s); "
    "33% rejected, 6,172 billed / $0.25 per accepted",
    "  Reported and accepted are counted over the rounds whose report could be read; a round",
    "  with none is left out of every figure, its cost included. Before 0.11.0 only a workflow's",
    "  last round was kept, and the lost rounds reviewed the code before its findings were fixed,",
    "  so what survives is not a sample of the whole: a rejection rate over it is biased, and the",
    "  direction of the bias is not known. Per-accepted figures are taken over the readable",
    "  rounds only; the unread rounds could move them either way.",
    "  Cost totals are floors: a run that reported nothing adds nothing, and one that reported",
    "  tokens but no price (Codex) adds no dollars.",
    "  1 round(s) was a --surrounding pair: both runs' cost is in, and its findings and triage",
    "  are the second run's. See the paired block for what it measured.",
    "  Found alone is an upper bound on what dropping the reviewer would lose: a duplicate no",
    "  candidate link joined is counted as found alone.",
    "  Rates are printed from 10 decided findings; below that the counts stand alone.",
    "",
    "Review effort, code and design together: 2 accepted over 2 of 4 recorded round(s); 12,345 billed,",
    "  $0.50 over 1 priced run(s); per accepted withheld under 10 accepted",
    "  Added here because both stages count the same thing, a finding the owner accepted; the",
    "  per-round figures above are not added, because a round of each is a different unit of",
    "  work. Read it beside the two stage figures: the mix of stages moves it, and it is taken",
    "  over the readable rounds only.",
    "",
    "Architect revisions (cost against each workflow's initial design run):",
    "  resumed: 1 revisions (1 priced, 1 with ratio) cost ratio to initial 0.75 mean, "
    "1.25 per completed incl. 1 stalled + 0 rejected + 0 failed attempts (1 priced)",
    "  fresh: 1 revisions (0 priced, 0 with ratio) cost ratio to initial n/a "
    "(initial run has no usable cost); 0 stalled + 1 rejected + 1 failed attempts (0 priced)",
    "  --resume ran fresh because:",
    "    budget x2",
    "    no-session x1",
    "",
    "Design review rounds refused for size: 1 -- nothing ran, and the plan and its",
    "request were over review.context.max_chars.",
    "",
    "Estimated saving from 1 gate-refused round(s): ~10,000 billed tokens.",
    "An estimate: what a round that did not happen would have cost is",
    "unknowable, so this is the mean of the 3 that did.",
    "",
    "1 round(s) refused for size are not priced above: a change over",
    "review.context.max_chars was going to cost more than the mean, so",
    "charging it the mean would understate what was not spent.",
    "",
    "1 of 5 round(s) ran with no test result recorded, so the gate had",
    "nothing to act on and cannot have fired. Record one before `review run`:",
    "  dev-orchestra state record test ok|failed",
]

CODE_ONLY_TEXT = [
    "Review rounds recorded: 2 (2 ran, 0 refused)",
    "  levels in force        fast x2",
    "  gate verdicts          pass x2",
    "  panel reduced          0",
    "  escalated (high risk)  0",
    "",
    "Reviewer runs: 4 (4 reported usage), 8,000 billed",
    "  4,000 billed per round that ran",
]

DESIGN_ONLY_TEXT = [
    "Reviewer runs: 2 (1 reported usage), 3,000 billed",
    "  design review          2 (1 reported usage), 3,000 billed over 1 round(s), 3,000 each",
]

DESIGN_REFUSED_ONLY_TEXT = [
    "Reviewer runs: 0 (0 reported usage), 0 billed",
    "",
    "Design review rounds refused for size: 2 -- nothing ran, and the plan and its",
    "request were over review.context.max_chars.",
]

REVISIONS_ONLY_TEXT = [
    "Architect revisions (cost against each workflow's initial design run):",
    "  resumed: 0 revisions (0 priced, 0 with ratio) cost ratio to initial n/a "
    "(initial run has no usable cost); 0 stalled + 0 rejected + 0 failed attempts (0 priced)",
    "  fresh: 1 revisions (1 priced, 1 with ratio) cost ratio to initial 1.50 mean, "
    "n/a per completed incl. 0 stalled + 0 rejected + 0 failed attempts (0 priced)",
]


def _optimization_cases() -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], List[str]]]:
    """Each fixture: the round summary, the scorecard, the revisions, and the text."""
    empty_scorecard = opt_report.reviewer_scorecard([])
    empty_revisions = opt_report.architect_revisions([])
    code_only = _rounds(
        rounds=2,
        ran=2,
        levels={"fast": 2},
        gates={"pass": 2},
        reviewer_runs=4,
        measured_runs=4,
        billed_tokens=8000,
        billed_per_round=4000,
    )
    design_only = _rounds(
        design_rounds=1,
        design_reviewer_runs=2,
        design_measured_runs=1,
        design_billed_tokens=3000,
        design_billed_per_round=3000,
    )
    fresh = _revision_group({}, runs=1, priced_runs=1, ratio_runs=1, cost_ratio_mean=1.5)
    return {
        "full": (_full_rounds(), _full_scorecard(), _full_revisions(), FULL_TEXT),
        "code only": (code_only, empty_scorecard, empty_revisions, CODE_ONLY_TEXT),
        "design only": (design_only, empty_scorecard, empty_revisions, DESIGN_ONLY_TEXT),
        "design refused only": (
            _rounds(design_refused=2),
            empty_scorecard,
            empty_revisions,
            DESIGN_REFUSED_ONLY_TEXT,
        ),
        "revisions only": (
            _rounds(),
            empty_scorecard,
            _revisions(attempts=1, fresh=fresh),
            REVISIONS_ONLY_TEXT,
        ),
    }


class TestOptimizationReportText(IsolatedCase):
    """``optimization report``, over every outcome and every branch of the full report."""

    def report(
        self, rounds: Dict[str, Any], scorecard: Dict[str, Any], revisions: Dict[str, Any], *extra: str
    ) -> "tuple[int, str, str]":
        # Read through the module at call time, so replacing them here is
        # what the command sees. A copy each call: the command adds keys.
        with mock.patch.multiple(
            opt_report,
            summarise_rounds=mock.Mock(side_effect=lambda events: copy.deepcopy(rounds)),
            reviewer_scorecard=mock.Mock(side_effect=lambda inputs: copy.deepcopy(scorecard)),
            architect_revisions=mock.Mock(side_effect=lambda workflows: copy.deepcopy(revisions)),
        ):
            return run_cli("optimization", "report", *extra)

    def source(self) -> str:
        workspace = self.cli_workspace()
        return workspace.relative(workspace.container)

    def test_each_outcome_prints_exactly_this(self):
        for name, (rounds, scorecard, revisions, lines) in _optimization_cases().items():
            with self.subTest(name):
                code, out, _ = self.report(rounds, scorecard, revisions)
                self.assertEqual(code, 0)
                self.assertEqual(out, text(lines))

    def test_nothing_recorded(self):
        empty = opt_report.architect_revisions([])
        code, out, _ = self.report(_rounds(), opt_report.reviewer_scorecard([]), empty)
        self.assertEqual(code, 0)
        expected = [
            "No review rounds recorded in %s." % self.source(),
            "Run a review, then ask again -- this reads what happened, not what would.",
        ]
        self.assertEqual(out, text(expected))

    def test_report_lines_are_the_text(self):
        source = self.source()
        for name, (rounds, scorecard, revisions, lines) in _optimization_cases().items():
            with self.subTest(name):
                report = merged(rounds, scorecard=scorecard, architect_revisions=revisions)
                self.assertEqual(optimization_render.report_lines(report, source), lines)
        empty = merged(
            _rounds(),
            scorecard=opt_report.reviewer_scorecard([]),
            architect_revisions=opt_report.architect_revisions([]),
        )
        nothing = [
            "No review rounds recorded in %s." % source,
            "Run a review, then ask again -- this reads what happened, not what would.",
        ]
        self.assertEqual(optimization_render.report_lines(empty, source), nothing)

    def test_json_is_the_summary_with_both_attached(self):
        rounds, scorecard, revisions = _full_rounds(), _full_scorecard(), _full_revisions()
        code, out, _ = self.report(rounds, scorecard, revisions, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), merged(rounds, scorecard=scorecard, architect_revisions=revisions))

    def test_recorded_events_reach_the_text_unmocked(self):
        """The command reads the real run log, so its wiring into ``report_lines`` is what is tested."""
        workspace = self.cli_workspace()
        refused = {"level": "balanced", "gate": "refuse", "test_status": "failed"}
        ran = {"level": "fast", "gate": "allow", "test_status": "ok"}
        workspace.record_event("review", "refused", {"refused_by": "gate", "optimization": refused})
        workspace.record_event("review", "ok", {"optimization": ran, "reviewers": []})
        workspace.record_event("review", "ok", {"optimization": ran, "reviewers": []})
        code, out, _ = run_cli("optimization", "report")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "Review rounds recorded: 3 (2 ran, 1 refused)")
        levels = [line for line in lines if line.startswith("  levels in force")]
        self.assertEqual(len(levels), 1)
        self.assertIn("balanced x1", levels[0])
        self.assertIn("fast x2", levels[0])
        code, out, _ = run_cli("optimization", "report", "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual((report["rounds"], report["ran"], report["refused"]), (3, 2, 1))
        self.assertIn("scorecard", report)
        self.assertIn("architect_revisions", report)


# --------------------------------------------------------------------------- tokens show


TOKENS_TEXT = [
    "  stage              meas.     input    output     total    billed      cost   tools  tool out",
    "  review               3/4     1,200       300     1,500     1,500   $0.0123       7     2,500",
    "  ALL                13/14     1,200       300     1,500     1,500   $0.0123       7     2,500",
    "",
    "Per reviewer and tier:",
    "  m1 (balanced)        2/2       600         -       600       600         -       -         -",
    "",
    "Prompt text this repo composed: 12,345 chars over 14 run(s). That is the part it can shorten.",
    "`tools` counts every tool call, whatever it was called. `tool out` is what "
    "those tools printed back -- not source read: `wc -l` returns 3 characters "
    "for a 200-line file and `cat` returns the file. How much of this repository "
    "a delegated run actually read is not knowable from here.",
    "1 of 14 run(s) predate tool counting and cannot say whether they used "
    "tools, so the tool columns leave them out. That is not the same as "
    "having used none.",
    "12 of 14 run(s) reported no tool activity (Codex does not); the tool "
    "columns cover only the runs that did.",
    "1 of 14 run(s) reported no usage, so every total above is a floor, not a total.",
    "2 of 14 run(s) reported tokens but no cost, so the cost column is a "
    "floor even where the token counts are not. Comparing providers on it "
    "understates the ones that price nothing.",
]


class TestTokensShowText(IsolatedCase):
    """``tokens show``, with every note on and with nothing recorded."""

    def token_report(self, **changes: Any) -> Dict[str, Any]:
        """The real report of an empty ledger, with ``changes`` laid over it."""
        return merged(ledger_mod.Ledger(self.cli_workspace()).token_report(), **changes)

    def full_report(self) -> Dict[str, Any]:
        stage = {
            "runs": 4,
            "measured_runs": 3,
            "input_tokens": 1200,
            "output_tokens": 300,
            "total_tokens": 1500,
            "billed_tokens": 1500,
            "cost_usd": 0.0123,
            "tool_reported_runs": 1,
            "tool_uses": 7,
            "tool_output_chars": 2500,
        }
        totals = {
            **stage,
            "runs": 14,
            "measured_runs": 13,
            "tool_unknown_runs": 1,
            "prompt_chars": 12345,
            "priced_runs": 12,
        }
        label = {
            "runs": 2,
            "measured_runs": 2,
            "input_tokens": 600,
            "output_tokens": 0,
            "total_tokens": 600,
            "billed_tokens": 600,
            "cost_usd": 0,
        }
        empty = ledger_mod.Ledger(self.cli_workspace()).token_report()
        return self.token_report(
            totals=merged(empty["totals"], **totals),
            by_stage={"review": stage},
            by_label={"m1 (balanced)": label},
            complete=False,
            priced=False,
        )

    def show(self, report: Dict[str, Any], *extra: str) -> "tuple[int, str, str]":
        with mock.patch.object(ledger_mod.Ledger, "token_report", return_value=copy.deepcopy(report)):
            return run_cli("tokens", "show", *extra)

    def test_every_note(self):
        code, out, _ = self.show(self.full_report())
        self.assertEqual(code, 0)
        self.assertEqual(out, text(TOKENS_TEXT))

    def test_no_runs(self):
        code, out, _ = self.show(self.token_report())
        self.assertEqual(code, 0)
        self.assertEqual(out, "No delegated runs recorded yet.\n")

    def test_json_is_the_report(self):
        report = self.full_report()
        code, out, _ = self.show(report, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), report)


# --------------------------------------------------------------------------- status


WARNING_TEXT = [
    "  ! Another workflow is active in this working tree: other",
    "  ! Artifacts are separate; the files under review are not. For work that really runs in parallel, "
    "give each workflow its own worktree (git worktree add ../name branch).",
]

STATUS_A_TEXT = [
    "Verdict: STOP-AND-REPORT",
    "  - review budget spent",
    "  - the delegated runtime budget is spent",
    *WARNING_TEXT,
    "",
    "Stalled stages:",
    "  review started 2026-10-01T00:00:00Z (125s ago) -- no heartbeat",
    "",
    "Cleared 2 stage(s) whose process is gone: architect, test",
    "",
    "In flight:",
    "  implementer (tok1) since 2026-10-01T00:01:00Z",
    "",
    "Review: round 2/2, 3 accepted, 2 blocking -- final fix pending (fix, re-test, do not re-review); "
    "identical to the previous round",
    "Design review: auto -> run (plan names 3 files), round 1/2, 1 accepted, 1 blocking "
    "-- final revision pending (fold the findings in, do not re-review); identical to the previous round",
    "Plan approval: approved (0123456789ab)",
    "Optimization: quality (escalated from fast -- auth/login.py matches auth/**), tests ok, 1 reviewer; "
    "sec added (touches auth); perf left out (no perf paths)",
    "Tokens: 12,345 billed over 3 run(s), partially reported (dev-orchestra tokens show)",
    "Runtime: 42s of delegated run time spent asleep was not charged (dev-orchestra budget show)",
]

STATUS_B_TEXT = [
    "Verdict: CONTINUE",
    *WARNING_TEXT,
    "",
    "Review: round 1/2, 0 accepted, 0 blocking -- final fix done, re-test pending "
    "(record it, do not re-review)",
    "Design review: on, round 0/2, 0 accepted, 0 blocking",
    "Plan approval: pending -- present .ai/workflows/test/plan.md to the user and ask; a yes is recorded "
    "with `design approve`, never without one (no architect attempt is left for changes; "
    "`budget reset architect` first)",
    "Optimization: balanced, tests not recorded",
    "Tokens: 0 billed over 2 run(s) (dev-orchestra tokens show)",
]

STATUS_C_TEXT = [
    "Verdict: CONTINUE",
    "",
    "Review: round 0/2, 0 accepted, 0 blocking",
    "Design review: off, round 0/2, 0 accepted, 0 blocking",
    "Plan approval: stale -- the plan changed after it was approved (aaaaaaaaaaaa -> bbbbbbbbbbbb); "
    "present it again and ask",
    "Optimization: balanced, tests ok",
]


class TestStatusText(IsolatedCase):
    """``status``, printed through ``cmd_status`` from a real verdict changed to reach each line."""

    def verdict(
        self,
        plan: opt_mod.Plan,
        decision: opt_mod.DesignDecision,
        architect_left: Optional[int],
        **changes: Any,
    ) -> Any:
        """A real ``_status_payload`` result, with ``changes`` laid over its payload.

        A section that is a mapping is updated, so the keys left out keep
        their real values; anything else is replaced.
        """
        workspace = self.cli_workspace()
        loaded = config_mod.load(self.project, validate_result=False)
        book = ledger_mod.Ledger(workspace, ledger_mod.budget_settings(loaded.data))
        real = cli_workflow._status_payload(loaded, workspace, book)
        payload = copy.deepcopy(real.payload)
        for key, value in changes.items():
            if isinstance(payload.get(key), dict) and key != "in_flight":
                payload[key].update(value)
            else:
                payload[key] = value
        return real._replace(
            payload=payload,
            plan=plan,
            design_decision=decision,
            design_pass={"state": None},
            architect_left=architect_left,
        )

    def status(self, verdict: Any, others: List[str]) -> "tuple[int, str, str]":
        with mock.patch.object(cli_workflow, "_status_payload", return_value=verdict):
            with mock.patch.object(workflow_mod, "active_elsewhere", return_value=others):
                return run_cli("status")

    def test_stalls_pending_fixes_and_an_escalated_plan(self):
        plan = opt_mod.Plan(
            "fast",
            "quality",
            opt_mod.GATE_ALLOW,
            "ok",
            10,
            1,
            high_risk=[("auth/login.py", "auth/**")],
            conditional=[
                {"id": "sec", "runs": True, "reason": "touches auth"},
                {"id": "perf", "runs": False, "reason": "no perf paths"},
            ],
        )
        verdict = self.verdict(
            plan,
            opt_mod.DesignDecision("auto", True, "plan names 3 files"),
            1,
            verdict="stop-and-report",
            reasons=["review budget spent", "the delegated runtime budget is spent"],
            stalls=[
                {
                    "stage": "review",
                    "started_at": "2026-10-01T00:00:00Z",
                    "elapsed_seconds": 125.4,
                    "reason": "no heartbeat",
                },
            ],
            abandoned_stages=["architect", "test"],
            in_flight={"tok1": {"stage": "implementer", "started_at": "2026-10-01T00:01:00Z"}},
            review={
                "iteration": 2,
                "max_review_iterations": 2,
                "accepted": 3,
                "blocking": ["F1", "F2"],
                "final_fix": "pending",
                "identical_rounds": 2,
            },
            design_review={
                "iteration": 1,
                "max_iterations": 2,
                "accepted": 1,
                "blocking": ["D1"],
                "final_revision_pending": True,
                "identical_rounds": 2,
            },
            design_approval={"state": "approved", "pending": False, "approved_sha256": "0123456789abcdef"},
            tokens={"totals": {"runs": 3, "billed_tokens": 12345}, "complete": False},
            runtime={"suspended": 42.0},
        )
        code, out, _ = self.status(verdict, ["other"])
        self.assertEqual(code, 0)
        self.assertEqual(out, text(STATUS_A_TEXT))

    def test_a_retest_and_an_approval_with_no_architect_left(self):
        plan = opt_mod.Plan("balanced", "balanced", opt_mod.GATE_WARN, "", 10, None)
        verdict = self.verdict(
            plan,
            opt_mod.DesignDecision("on", True),
            0,
            verdict="continue",
            reasons=[],
            review={
                "iteration": 1,
                "max_review_iterations": 2,
                "accepted": 0,
                "blocking": [],
                "final_fix": "retest",
                "identical_rounds": 0,
            },
            design_review={
                "iteration": 0,
                "max_iterations": 2,
                "accepted": 0,
                "blocking": [],
                "final_revision_pending": False,
                "identical_rounds": 0,
            },
            design_approval={"state": "pending", "pending": True, "design_review_exhausted": False},
            tokens={"totals": {"runs": 2, "billed_tokens": 0}, "complete": True},
            runtime={"suspended": 0},
        )
        code, out, _ = self.status(verdict, ["other"])
        self.assertEqual(code, 0)
        self.assertEqual(out, text(STATUS_B_TEXT))

    def test_the_least_there_is_to_say(self):
        plan = opt_mod.Plan("balanced", "balanced", opt_mod.GATE_ALLOW, "ok", 10, None)
        verdict = self.verdict(
            plan,
            opt_mod.DesignDecision("off", False),
            None,
            verdict="continue",
            reasons=[],
            review={
                "iteration": 0,
                "max_review_iterations": 2,
                "accepted": 0,
                "blocking": [],
                "final_fix": None,
                "identical_rounds": 0,
            },
            design_review={
                "iteration": 0,
                "max_iterations": 2,
                "accepted": 0,
                "blocking": [],
                "final_revision_pending": False,
                "identical_rounds": 0,
            },
            design_approval={
                "state": "stale",
                "pending": False,
                "stale_reason": "plan-changed",
                "approved_sha256": "a" * 64,
                "plan_sha256": "b" * 64,
            },
            tokens={"totals": {"runs": 0, "billed_tokens": 0}, "complete": True},
            runtime={"suspended": 0},
        )
        code, out, _ = self.status(verdict, [])
        self.assertEqual(code, 0)
        self.assertEqual(out, text(STATUS_C_TEXT))


# --------------------------------------------------------------------------- summary


#: A recorded panel with every shape of ``model`` and ``status`` the text reads,
#: and keys the text never shows.
SUMMARY_PANEL = [
    {
        "id": "m1",
        "provider": "claude",
        "model": {"family": "sonnet", "version": "4", "extra": "not-shown"},
        "status": "ok",
    },
    {
        "id": "m2",
        "provider": "codex",
        "model": "gpt-x",
        "status": "failed",
        "error": "boom",
        "invoked": True,
        "options": {"effort": "high"},
    },
    {"id": "m3", "provider": "mock", "model": None},
    {"id": "m4", "provider": "mock"},
    {"id": "m5", "model": "x"},
]

SUMMARY_COUNTS = {"reviewers_ok": 2, "reviewers_total": 3, "findings": 4}

SUMMARY_TEXT = [
    "Workflow:",
    "  architect      OK",
    "  design_review  REFUSED",
    "  implementer    FAILED",
    "  review         OK",
    "  design reviews 1/2 ok",
    "  reviews        2/3 ok",
    "",
    "Models:",
    "  Orchestrator   claude / opus / latest",
    "  Architect      codex / fam / latest",
    "  Implementer    mock / default / 1.2",
    "  Review Fixer   None / default / latest",
    "Review:",
    "  m1             claude / sonnet",
    "  m2             codex / gpt-x (FAILED)",
    "  m3             mock / default",
    "  m4             mock / default",
    "  m5             None / x",
    "",
    "Optimization:",
    "  context        1 round(s) not run: change over review.context.max_chars",
    "  gate           1 round(s) not run: tests recorded as failing",
    "  context        1 design round(s) not run: plan over review.context.max_chars",
    "  panel          1 round(s) cut to one reviewer -- one opinion, not an independent second",
    "  detail         dev-orchestra optimization report",
    "",
    "Tokens:",
    "  architect      3,000 billed",
    "  review         12,000 billed",
    "  total          15,000 billed over 3 run(s) -- $0.2500 (partially reported: a floor)",
]


class SummaryCase(IsolatedCase):
    """A run log, both consolidated reports and a configuration that reach every line of ``summary``."""

    def setUp(self):
        super().setUp()
        workspace = self.cli_workspace()
        refused = {"level": "balanced", "gate": "refuse", "test_status": "failed"}
        workspace.record_event("architect", "ok")
        workspace.record_event("design_review", "refused")
        workspace.record_event("implementer", "failed")
        workspace.record_event("review", "refused", {"refused_by": "gate", "optimization": refused})
        workspace.record_event("review", "refused", {"refused_by": "context", "optimization": refused})
        ran = {"level": "fast", "gate": "allow", "test_status": "ok", "reviewer_limit": 1}
        workspace.record_event("review", "ok", {"optimization": ran, "reviewers": []})
        for path, data in (
            (workspace.consolidated_json_path, {"counts": SUMMARY_COUNTS, "reviewers": SUMMARY_PANEL}),
            (
                workspace.design_review().consolidated_json_path,
                {"counts": {"reviewers_ok": 1, "reviewers_total": 2}},
            ),
        ):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            ws.write_json(path, data)

        loaded = config_mod.load(self.project, validate_result=False)
        loaded.data["orchestrator"] = {"provider": "claude", "model": {"family": "opus", "version": "latest"}}
        loaded.data["architect"] = {"provider": "codex", "model": {"family": "fam"}}
        loaded.data["implementer"] = {"provider": "mock", "model": {"family": "default", "version": "1.2"}}
        loaded.data.pop("review_fixer", None)
        self.loaded = loaded

        empty = ledger_mod.Ledger(workspace).token_report()
        self.tokens = merged(
            empty,
            totals=merged(empty["totals"], runs=3, billed_tokens=15000, cost_usd=0.25),
            by_stage={"review": {"billed_tokens": 12000}, "architect": {"billed_tokens": 3000}},
            complete=False,
        )

    def summary(self, *extra: str) -> "tuple[int, str, str]":
        tokens = copy.deepcopy(self.tokens)
        with mock.patch.object(cli_workflow, "_load_lenient", return_value=self.loaded):
            with mock.patch.object(ledger_mod.Ledger, "token_report", return_value=tokens):
                return run_cli("summary", *extra)


class TestSummaryText(SummaryCase):
    def test_every_section(self):
        code, out, _ = self.summary()
        self.assertEqual(code, 0)
        self.assertEqual(out, text(SUMMARY_TEXT))

    def test_json_keeps_its_three_keys(self):
        code, out, _ = self.summary("--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        stages = {"architect": "ok", "design_review": "refused", "implementer": "failed", "review": "ok"}
        expected = {
            "stages": stages,
            "counts": SUMMARY_COUNTS,
            "tokens": self.tokens,
        }
        self.assertEqual({key: payload[key] for key in expected}, expected)


class TestSummaryJson(SummaryCase):
    """``summary --json`` beside the text: one payload, holding only what the text shows."""

    def test_the_added_keys_hold_exactly_these_fields(self):
        code, out, _ = self.summary("--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        added = {"design_counts", "models", "reviewers", "skipped"}
        self.assertEqual(set(payload), {"stages", "counts", "tokens"} | added)
        self.assertEqual(payload["design_counts"], {"reviewers_ok": 1, "reviewers_total": 2})
        self.assertEqual(set(payload["models"]), {"orchestrator", "architect", "implementer", "review_fixer"})
        for key, model in payload["models"].items():
            self.assertEqual(set(model), {"provider", "family", "version"}, key)
        unset = {"provider": None, "family": "default", "version": "latest"}
        self.assertEqual(payload["models"]["review_fixer"], unset)
        defaulted = {"provider": "codex", "family": "fam", "version": "latest"}
        self.assertEqual(payload["models"]["architect"], defaulted)
        reviewers = [
            {"id": "m1", "provider": "claude", "model": "sonnet", "status": "ok"},
            {"id": "m2", "provider": "codex", "model": "gpt-x", "status": "failed"},
            {"id": "m3", "provider": "mock", "model": "default", "status": "ok"},
            {"id": "m4", "provider": "mock", "model": "default", "status": "ok"},
            {"id": "m5", "provider": None, "model": "x", "status": "ok"},
        ]
        self.assertEqual(payload["reviewers"], reviewers)
        skipped = {
            "refused": 2,
            "refused_by": {"context": 1, "gate": 1},
            "design_refused": 1,
            "panel_reduced": 1,
        }
        self.assertEqual(payload["skipped"], skipped)

    def test_nothing_else_of_a_reviewer_leaks(self):
        _, out, _ = self.summary("--json")
        for key in ("error", "invoked", "options", "extra", "usage", "warnings", "report"):
            self.assertNotIn('"%s"' % key, out)
        for value in ("boom", "not-shown", "effort"):
            self.assertNotIn(value, out)


class TestSummaryOddInputs(SummaryCase):
    """Reports that are missing or empty, and values the lenient config load lets through."""

    def write_report(self, path: str, data: Any) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ws.write_json(path, data)

    def both(self) -> "tuple[List[str], Dict[str, Any]]":
        """The text lines and the ``--json`` payload of one state."""
        code, out, _ = self.summary()
        self.assertEqual(code, 0)
        code, json_out, _ = self.summary("--json")
        self.assertEqual(code, 0)
        return out.splitlines(), json.loads(json_out)

    def section(self, lines: List[str], title: str) -> List[str]:
        """The lines under ``title`` up to the next blank line or heading."""
        start = lines.index(title) + 1
        end = start
        while end < len(lines) and lines[end].startswith("  "):
            end += 1
        return lines[start:end]

    def test_no_reports_falls_back_to_the_configured_panel(self):
        os.remove(self.cli_workspace().consolidated_json_path)
        os.remove(self.cli_workspace().design_review().consolidated_json_path)
        self.loaded.data["reviewers"] = [{"id": "cfg", "provider": "claude", "model": {"family": "opus"}}]
        lines, payload = self.both()
        self.assertEqual(payload["counts"], {})
        self.assertEqual(payload["design_counts"], {})
        self.assertEqual(self.section(lines, "Workflow:"), SUMMARY_TEXT[1:5])
        self.assertEqual(
            payload["reviewers"], [{"id": "cfg", "provider": "claude", "model": "opus", "status": "ok"}]
        )
        self.assertEqual(self.section(lines, "Review:"), ["  cfg            claude / opus"])

    def test_a_design_report_without_counts_is_an_empty_mapping(self):
        self.write_report(self.cli_workspace().design_review().consolidated_json_path, {})
        lines, payload = self.both()
        self.assertEqual(payload["design_counts"], {})
        self.assertEqual(self.section(lines, "Workflow:"), [*SUMMARY_TEXT[1:5], "  reviews        2/3 ok"])

    def test_an_empty_recorded_panel_falls_back_to_config(self):
        self.write_report(
            self.cli_workspace().consolidated_json_path, {"counts": SUMMARY_COUNTS, "reviewers": []}
        )
        self.loaded.data["reviewers"] = [{"id": "cfg", "provider": "codex", "model": "gpt-x"}]
        lines, payload = self.both()
        self.assertEqual(
            payload["reviewers"], [{"id": "cfg", "provider": "codex", "model": "gpt-x", "status": "ok"}]
        )
        self.assertEqual(self.section(lines, "Review:"), ["  cfg            codex / gpt-x"])

    def test_a_configured_panel_that_is_not_a_list_lists_nothing(self):
        self.write_report(
            self.cli_workspace().consolidated_json_path, {"counts": SUMMARY_COUNTS, "reviewers": []}
        )
        self.loaded.data["reviewers"] = {"id": "cfg"}
        lines, payload = self.both()
        self.assertEqual(payload["reviewers"], [])
        self.assertEqual(self.section(lines, "Review:"), [])

    def test_non_string_reviewer_fields_are_shown_as_text(self):
        panel = [{"id": 3, "provider": "mock", "model": {"family": 7}, "status": False}, "junk", None]
        self.write_report(
            self.cli_workspace().consolidated_json_path, {"counts": SUMMARY_COUNTS, "reviewers": panel}
        )
        lines, payload = self.both()
        shown = {"id": "3", "provider": "mock", "model": "7", "status": "False"}
        self.assertEqual(payload["reviewers"], [shown])
        self.assertEqual(self.section(lines, "Review:"), ["  3              mock / 7 (FAILED)"])

    def test_non_string_model_fields_are_shown_as_text(self):
        # What PyYAML's safe_load makes of `version: 2025-01-01` and `version: 4.5`.
        dated = {"family": 5, "version": datetime.date(2025, 1, 1)}
        self.loaded.data["architect"] = {"provider": "codex", "model": dated}
        self.loaded.data["implementer"] = {"provider": "mock", "model": {"version": 4.5}}
        lines, payload = self.both()
        architect = {"provider": "codex", "family": "5", "version": "2025-01-01"}
        implementer = {"provider": "mock", "family": "default", "version": "4.5"}
        self.assertEqual(payload["models"]["architect"], architect)
        self.assertEqual(payload["models"]["implementer"], implementer)
        self.assertIn("  Architect      codex / 5 / 2025-01-01", lines)
        self.assertIn("  Implementer    mock / default / 4.5", lines)

    def test_the_text_survives_a_json_round_trip_of_non_string_fields(self):
        """Read back from ``--json``, the payload still prints the same text: every field is text."""
        panel = [{"id": 3, "provider": 1.5, "model": 2, "status": "ok"}]
        self.write_report(
            self.cli_workspace().consolidated_json_path, {"counts": SUMMARY_COUNTS, "reviewers": panel}
        )
        dated = {"version": datetime.date(2025, 1, 1)}
        self.loaded.data["implementer"] = {"provider": "mock", "model": dated}
        _, out, _ = self.summary("--json")
        _, text_out, _ = self.summary()
        self.assertEqual(text(cli_workflow._summary_lines(json.loads(out))), text_out)


# --------------------------------------------------------------------------- doctor


DOCTOR_MAXIMAL = {
    "platform": {"system": "Linux", "release": "6.1", "python": "3.12.1"},
    "providers": {
        "mock": {"display_name": "Mock", "adapter_error": "boom"},
        "claude": {
            "display_name": "Claude Code",
            "installed": True,
            "version": "2.1.0",
            "authentication": "present",
            "model_selection": "supported",
            "models": [{"label": "opus"}, {"label": "sonnet"}],
            "model_discovery": "static",
            "read_only_enforcement": {"status": "verified", "mechanism": "--permission-mode plan"},
            "resume_support": {"status": "not-checked"},
            "live_check": {
                "status": "passed",
                "entry": {"checked_at": "2026-09-30T10:00:00Z", "skipped": ["a"]},
            },
        },
        "codex": {
            "display_name": "Codex CLI",
            "installed": False,
            "error": "not on PATH",
            "read_only_enforcement": {"status": "unenforced", "detail": "no sandbox"},
            "preset_fit": {"note": "fits quality"},
        },
    },
    "user_providers": {
        "directory": "/srv/u/providers",
        "enabled": True,
        "present": True,
        "loaded": [{"name": "mycli", "path": "/srv/u/providers/mycli.py"}],
        "errors": [{"path": "/srv/u/providers/bad.py", "error": "SyntaxError"}],
    },
    "config": {
        "global": "/cfg/config.yaml",
        "project_override": None,
        "preset": {"name": "quality", "source": "global", "fitted_to": ["claude", "codex"]},
        "using_builtin_defaults": True,
        "pinned": [{"setting": "review.max", "value": 3, "default": 2}],
    },
    "roles": {
        "orchestrator": {
            "label": "Orchestrator",
            "status": "ok",
            "provider": "claude",
            "family": "opus",
            "version_policy": "latest",
        },
        "architect": {
            "label": "Architect",
            "status": "ok",
            "provider": "codex",
            "family": None,
            "version_policy": "pinned",
            "options": {"effort": "high"},
        },
        "implementer": {"label": "Implementer", "status": "missing", "provider": "agy"},
    },
    "reviewers": [
        {
            "id": "r1",
            "status": "ok",
            "provider": "claude",
            "family": "sonnet",
            "role": "security",
            "when": "auth",
            "condition": "touches auth/**",
        },
        {"id": "r2", "status": "ok", "provider": "codex", "origin": "global extra"},
    ],
    "problems": ["claude: something"],
    "notes": ["note one"],
}

DOCTOR_MAXIMAL_TEXT = [
    "AI Development Orchestrator -- doctor",
    "",
    "Environment: Linux 6.1 / Python 3.12.1",
    "",
    "Mock (mock)",
    "  Installed: unknown (adapter failed)",
    "  Source: built-in",
    "  Adapter error: boom",
    "",
    "Claude Code (claude)",
    "  Installed: yes",
    "  Source: built-in",
    "  Version: 2.1.0",
    "  Authentication: present (credential presence only, not verified)",
    "  Model selection: supported",
    "  Models (static): opus, sonnet",
    "  Read-only runs: enforced by --permission-mode plan",
    "  Resume: not checked (--fast)",
    "  Live check: passed for 2.1.0 on 2026-09-30, 1 skipped",
    "",
    "Codex CLI (codex)",
    "  Installed: no",
    "  Source: built-in",
    "  Detail: not on PATH",
    "  Read-only runs: NOT ENFORCED (runs allowed, warned) -- no sandbox",
    "  Preset fitting: fits quality",
    "",
    "User providers",
    "  Directory: /srv/u/providers",
    "  Code in this directory is imported at startup, from outside the plugin.",
    "  Imported: mycli  <- /srv/u/providers/mycli.py",
    "  Failed:   /srv/u/providers/bad.py -- SyntaxError",
    "",
    "Config",
    "  Global: /cfg/config.yaml",
    "  Project override: None",
    "  Preset: quality (global; fitted to claude, codex)",
    "  Source: built-in defaults, fitted as preset quality (`config setup --preset <name>` saves one)",
    "  Pinned at a value the built-in default has moved off:",
    "    review.max                               3 (default 2)",
    "    Deliberate choices look the same as values inherited from an",
    "    older default, so these are reported and never rewritten.",
    "    config prune drops the values equal to the current default, on request.",
    "",
    "Roles",
    "  Orchestrator: claude / opus / latest",
    "  Architect:    codex / default / pinned",
    "  Implementer:  agy / default (missing)",
    "  review_fixer: ? / default (unknown)",
    "      architect options: {'effort': 'high'}",
    "  Reviewers:    2",
    "    1. r1 / claude / sonnet / latest / security (when: touches auth/**)",
    "    2. r2 / codex / default / latest / general (extra: global)",
    "",
    "Problems",
    "  - claude: something",
    "",
    "Notes",
    "  - note one",
]

DOCTOR_MINIMAL = {
    "platform": {"system": "Windows", "release": "11", "python": "3.11.9"},
    "providers": {},
    "user_providers": {},
    "config": {"global": None, "project_override": None},
    "roles": {},
    "reviewers": [],
    "problems": [],
    "notes": [],
}

DOCTOR_MINIMAL_TEXT = [
    "AI Development Orchestrator -- doctor",
    "",
    "Environment: Windows 11 / Python 3.11.9",
    "",
    "User providers",
    "  Directory: None (not present; nothing imported)",
    "",
    "Config",
    "  Global: None",
    "  Project override: None",
    "",
    "Roles",
    "  orchestrator: ? / default (unknown)",
    "  architect:    ? / default (unknown)",
    "  implementer:  ? / default (unknown)",
    "  review_fixer: ? / default (unknown)",
    "  Reviewers:    0",
    "",
    "No problems found.",
]


class TestDoctorRender(IsolatedCase):
    """``doctor.render``, whole, on the most a report holds and on the least."""

    def test_every_section(self):
        self.assertEqual(doctor.render(copy.deepcopy(DOCTOR_MAXIMAL)), text(DOCTOR_MAXIMAL_TEXT))

    def test_the_separators_with_nothing_to_report(self):
        self.assertEqual(doctor.render(copy.deepcopy(DOCTOR_MINIMAL)), text(DOCTOR_MINIMAL_TEXT))


# --------------------------------------------------------------------------- design approve


REFUSED_ROUND = (
    "not recording an approval: the latest design review round has no report yet -- "
    "it is still running or did not finish. Wait for it (or run `review run --design` "
    "again), present its findings, and ask again.\n"
)

UNREVIEWED_NOTE = (
    "note: the latest design review round ended with no reviewer's review (every reviewer "
    "failed), so this plan has no design review findings at all; the approval goes ahead "
    "without one -- say so in the report\n"
)

EARLIER_REVISION_NOTE = (
    "note: 1 design finding(s) open from a review of an earlier revision of this plan: F1 "
    "-- the revision may already address them; say so in the report\n"
)

STILL_OPEN_NOTE = (
    "note: 1 design finding(s) still open: F1 -- approving over them is the user's call; "
    "name them in the report\n"
)


class TestDesignApproveText(ApprovalCase):
    """``design approve``: each outcome's exact text, and the order its two reads come in."""

    def approved_line(self, prefix: str) -> str:
        digest = approval_mod.read_plan(self.workspace)[1]
        return "%s .ai/workflows/test/plan.md (sha256 %s) for workflow test\n" % (prefix, digest[:12])

    def test_a_round_started_after_the_report_is_read_is_refused(self):
        """The report first, then the latest round: a round starting in between is caught.

        Read the other way round, the latest round would be the reported one,
        and the approval would be recorded over a round nobody has seen.
        """
        self.write_plan()
        run_cli("review", "run", "--design")
        # Compared as real paths: on macOS the temporary directory is reached
        # through a symlink (/var -> /private/var), and the command resolves
        # the repository root while the test's workspace does not.
        target = os.path.realpath(self.design.consolidated_json_path)
        original = ws.read_json
        started: List[bool] = []

        def read_json(path: str, default: Any = None) -> Any:
            data = original(path, default)
            if os.path.realpath(path) == target and not started:
                started.append(True)
                review_mod.write_design_snapshot(
                    self.design, self.workspace.plan_path, "", PLAN, "digest-of-the-next-round"
                )
            return data

        with mock.patch.object(ws, "read_json", side_effect=read_json):
            code, out, err = run_cli("design", "approve")
        self.assertEqual(started, [True])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, REFUSED_ROUND)
        self.assertNotIn(approval_mod.STAGE, self.workspace.read_state())

    def test_a_round_with_no_report(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        review_mod.write_design_snapshot(
            self.design, self.workspace.plan_path, "", PLAN, "digest-of-the-next-round"
        )
        code, out, err = run_cli("design", "approve")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, REFUSED_ROUND)
        self.assertNotIn(approval_mod.STAGE, self.workspace.read_state())

    def test_a_round_nobody_reviewed(self):
        self.write_plan()
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        run_cli("review", "run", "--design")
        del os.environ["DEV_ORCHESTRA_MOCK_FAIL"]
        code, out, err = run_cli("design", "approve")
        self.assertEqual(code, 0)
        self.assertEqual(out, self.approved_line("approved"))
        self.assertEqual(err, UNREVIEWED_NOTE)
        recorded = self.workspace.read_state()[approval_mod.STAGE]
        self.assertEqual(recorded["design_round"], approval_mod.design_round(self.workspace))

    def test_findings_of_this_plan(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        code, out, err = run_cli("design", "approve")
        self.assertEqual(code, 0)
        self.assertEqual(out, self.approved_line("approved"))
        self.assertEqual(err, STILL_OPEN_NOTE)

    def test_findings_of_an_earlier_revision(self):
        self.write_plan()
        run_cli("review", "run", "--design")
        run_cli("review", "triage", "--design", "F1", "--status", "accepted")
        self.write_plan(REVISED_PLAN)
        code, out, err = run_cli("design", "approve")
        self.assertEqual(code, 0)
        self.assertEqual(out, self.approved_line("approved"))
        self.assertEqual(err, EARLIER_REVISION_NOTE)

    def test_already_approved(self):
        self.write_plan()
        run_cli("design", "approve")
        code, out, err = run_cli("design", "approve")
        self.assertEqual(code, 0)
        self.assertEqual(out, self.approved_line("already approved"))
        self.assertEqual(err, "")
        events = self.workspace.read_state()["events"]
        self.assertEqual(len([e for e in events if e["stage"] == approval_mod.STAGE]), 1)

    def test_no_plan(self):
        code, out, err = run_cli("design", "approve")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, "no plan to approve at .ai/workflows/test/plan.md -- run the architect first\n")
