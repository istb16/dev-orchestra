"""The text every shared number, size, cost and config load produces.

Most of what is here pins output that was formatted in place at each call
site -- thousands separators, ``size unrecorded``, four-decimal dollars --
before it went through one helper, so that moving it there could change
nothing a reader sees. A figure under 1,000 has no separator to lose, so the
pins use figures over it.
"""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from itertools import product
from typing import Any, Dict, List
from unittest import mock

from helpers import IsolatedCase, has_git, present

from orchestrator import (
    cli,
    cli_common,
    cli_review,
    cli_state,
    cli_workflow,
    optimization_render,
    review_consolidation,
    review_coverage,
    review_snapshot,
)
from orchestrator import config as config_mod
from orchestrator import context as context_mod
from orchestrator import ledger as ledger_mod
from orchestrator import optimization as opt_mod
from orchestrator import presets as presets_mod
from orchestrator import review as review_mod
from orchestrator import suggest as suggest_mod
from orchestrator import workspace as ws
from orchestrator.providers import Usage


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def candidate(chars: int, symbol: str = "add") -> Dict[str, Any]:
    """An adopted symbol as the surrounding context records it."""
    return {
        "path": "app.py",
        "symbol": symbol,
        "kind": "function",
        "relation": "enclosing",
        "start": 1,
        "end": 40,
        "chars": chars,
        "text": "def %s(a, b):\n    return a + b\n" % symbol,
    }


def trimmed(chars: int, reason: str = "budget") -> Dict[str, Any]:
    return dict(candidate(chars, "sub"), reason=reason)


# --------------------------------------------------------------------------- the helpers


class TestFormatHelpers(unittest.TestCase):
    def test_fmt_int(self):
        self.assertEqual(ws.fmt_int(0), "0")
        self.assertEqual(ws.fmt_int(1234), "1,234")
        self.assertEqual(ws.fmt_int(-1234), "-1,234")

    def test_fmt_int_coerces_nothing(self):
        """It raises where ``"{:,}".format`` raised, rather than guessing a number."""
        nothing: Any = None
        empty: Any = ""
        with self.assertRaises(TypeError):
            ws.fmt_int(nothing)
        with self.assertRaises(ValueError):
            ws.fmt_int(empty)

    def test_fmt_size(self):
        for unrecorded in (0, None, "", "1234", True):
            with self.subTest(chars=unrecorded):
                self.assertEqual(ws.fmt_size(unrecorded), "size unrecorded")
        self.assertEqual(ws.fmt_size(1234), "1,234")
        self.assertEqual(ws.fmt_size(-5), "-5")

    def test_fmt_usd(self):
        self.assertEqual(ws.fmt_usd(0.0123), "$0.0123")
        self.assertEqual(ws.fmt_usd(0.00004), "$0.0000")
        self.assertEqual(ws.fmt_usd(1234.5), "$1234.5000")

    def test_per_run_figure(self):
        """An int goes through ``fmt_int``, a float keeps one place, and nothing is a dash."""
        figure = optimization_render._figure
        self.assertEqual(figure(None), "-")
        self.assertEqual(figure(0), "0")
        self.assertEqual(figure(-1234), "-1,234")
        self.assertEqual(figure(1234), "1,234")
        self.assertEqual(figure(1234.5), "1,234.5")
        self.assertEqual(figure(0.0), "0.0")


class TestReadSnapshotMeta(IsolatedCase):
    def test_a_missing_file_reads_as_empty(self):
        self.assertEqual(ws.Workspace(self.tmp).ensure().read_snapshot_meta(), {})

    def test_what_a_file_holds(self):
        """Empty or unreadable is ``{}``; anything else comes back as read, a list included."""
        workspace = ws.Workspace(self.tmp).ensure()
        os.makedirs(os.path.dirname(workspace.snapshot_meta_path), exist_ok=True)
        cases = (
            ("{not json", {}),
            ("null", {}),
            ("[]", {}),
            ("[1]", [1]),
            ('{"a": 1}', {"a": 1}),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                ws.write_text(workspace.snapshot_meta_path, text)
                self.assertEqual(workspace.read_snapshot_meta(), expected)


class TestSnapshotStamp(IsolatedCase):
    """The stamp every report is compared against, so its form decides what is stale."""

    def test_no_sha_is_unknown(self):
        for meta in ({}, {"sha256": ""}):
            with self.subTest(meta=meta):
                self.assertEqual(review_snapshot.snapshot_stamp(meta), "unknown")

    def test_the_first_twelve_characters_of_the_sha(self):
        sha = "0123456789ab" + "f" * 52
        self.assertEqual(review_snapshot.snapshot_stamp({"sha256": sha}), "0123456789ab")

    def test_the_current_stamp_reads_the_meta_on_disk(self):
        workspace = ws.Workspace(self.tmp).ensure()
        self.assertEqual(review_snapshot.current_snapshot_stamp(workspace), "unknown")
        ws.write_json(workspace.snapshot_meta_path, {"sha256": "a" * 64})
        self.assertEqual(review_snapshot.current_snapshot_stamp(workspace), "a" * 12)


class TestSurroundingRecordWalk(unittest.TestCase):
    """How a surrounding block is walked, and which entries of its lists are read."""

    def test_listed_keeps_only_mappings(self):
        self.assertEqual(review_consolidation.listed({"k": [1, {}, None, {"a": 1}]}, "k"), [{}, {"a": 1}])

    def test_listed_with_nothing_under_the_key(self):
        for record in ({}, {"k": None}, {"k": []}):
            with self.subTest(record=record):
                self.assertEqual(review_consolidation.listed(record, "k"), [])

    def test_a_block_split_by_reviewer_without_records(self):
        for block in ({"shared": False}, {"shared": False, "by_reviewer": None}):
            with self.subTest(block=block):
                self.assertEqual(review_consolidation.surrounding_records(block), [])

    def test_a_block_split_by_reviewer(self):
        block = {"shared": False, "by_reviewer": {"r1": {"mode": "enclosing"}, "r2": None}}
        expected = [("r1", {"mode": "enclosing"}), ("r2", None)]
        self.assertEqual(review_consolidation.surrounding_records(block), expected)

    def test_a_shared_block_is_its_own_record(self):
        for block in ({"shared": True}, {"mode": "enclosing"}):
            with self.subTest(block=block):
                self.assertEqual(review_consolidation.surrounding_records(block), [("", block)])


class TestLoadLenient(IsolatedCase):
    def test_it_loads_without_validating(self):
        with mock.patch.object(config_mod, "load") as load:
            self.assertIs(cli_common._load_lenient("somewhere"), load.return_value)
        load.assert_called_once_with("somewhere", validate_result=False)

    def test_an_invalid_file_is_returned_where_the_strict_load_exits(self):
        self.write(".dev-orchestra.yaml", "version: 1\nreview:\n  context:\n    max_chars: 0\n")
        err = io.StringIO()
        with redirect_stderr(err):
            loaded = cli_common._load_lenient()
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(loaded.data["review"]["context"]["max_chars"], 0)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            cli_common._load_or_die()
        self.assertEqual(stopped.exception.code, 2)

    def test_a_file_that_does_not_parse_raises_and_prints_nothing(self):
        self.write(".dev-orchestra.yaml", "[1]\n")
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(config_mod.ConfigError):
            cli_common._load_lenient()
        self.assertEqual(err.getvalue(), "")


# --------------------------------------------------------------------------- cli_state


class TestTokenTable(unittest.TestCase):
    def test_a_row_separates_thousands_and_prints_four_decimal_dollars(self):
        account = {
            "measured_runs": 1,
            "runs": 2,
            "input_tokens": 1234,
            "output_tokens": 0,
            "total_tokens": None,
            "billed_tokens": 1_234_567,
            "cost_usd": 0.0123,
            "tool_reported_runs": 1,
            "tool_uses": 1500,
            "tool_output_chars": 0,
        }
        cells = ("review", "1/2", "1,234", "-", "-", "1,234,567", "$0.0123", "1,500", "0")
        self.assertEqual(cli_state._token_row("review", account), cli_state._TOKEN_ROW % cells)

    def test_a_zero_cost_prints_a_dash(self):
        account = {"runs": 1, "measured_runs": 1, "input_tokens": 2000, "billed_tokens": 2000, "cost_usd": 0}
        cells = ("codex", "1/1", "2,000", "-", "-", "2,000", "-", "-", "-")
        self.assertEqual(cli_state._token_row("codex", account), cli_state._TOKEN_ROW % cells)


class TestOptimizationReportFigures(unittest.TestCase):
    """The renderers behind ``optimization report``, called with figures over 1,000."""

    def test_scorecard_spend(self):
        group = {"runs": 2, "measured_runs": 2, "billed_tokens": 12345, "priced_runs": 1, "cost_usd": 0.5}
        expected = "2 run(s), 12,345 billed, $0.50 over 1 of 2 run(s)"
        self.assertEqual(optimization_render._scorecard_spend(group, True), expected)
        unpriced = dict(group, priced_runs=0)
        expected = "2 run(s), 12,345 billed, no cost reported"
        self.assertEqual(optimization_render._scorecard_spend(unpriced, False), expected)

    def test_scorecard_per_accepted_priced_and_unpriced(self):
        priced = {"billed_per_accepted": 12345, "cost_per_accepted": 0.25}
        expected = "12,345 billed / $0.25 per accepted"
        self.assertEqual(optimization_render._scorecard_per_accepted(priced), expected)
        unpriced = {"billed_per_accepted": 12345, "cost_per_accepted": None}
        expected = "12,345 billed per accepted, $ -"
        self.assertEqual(optimization_render._scorecard_per_accepted(unpriced), expected)

    def test_scorecard_effort_line(self):
        scorecard = {
            "code": {"rounds_read": 1, "rounds_recorded": 1, "reviewers": {}, "panel": {}},
            "total": {"accepted": 3, "rounds_read": 1, "rounds_recorded": 1, "billed_tokens": 12345},
        }
        expected = "Review effort, code and design together: 3 accepted over 1 of 1 recorded round(s); "
        self.assertIn(expected + "12,345 billed,", optimization_render._scorecard_rows(scorecard))

    def test_paired_head_line(self):
        pair = {
            "snapshot": "abcdef0123456789",
            "change_chars": 5000,
            "panel": [],
            "adopted_chars": 1234,
            "context_chars": 2345,
            "trimmed_chars": 3456,
            "same_panel": True,
            "delivered": True,
            "same_inputs": True,
        }
        expected = (
            "  abcdef012345   change 5,000 chars; panel ; 1,234 context chars adopted (2,345 as carried), "
            "3,456 left out"
        )
        self.assertEqual(optimization_render._paired_rows({"pairs": [pair], "pairs_total": 1})[1], expected)

    def test_context_row(self):
        group = {
            "rounds": 2,
            "reviewer_runs": 3,
            "billed_tokens": 12345,
            "billed_per_round": 6172,
            "adopted_chars": 1234,
            "trimmed_chars": 5678,
        }
        expected = (
            "2 round(s), 3 run(s), 12,345 billed, 6,172 per round; - use(s)/run, - observed output chars/run "
            "(0 of 3 run(s) reported); 1,234 context chars adopted, 5,678 left out"
        )
        self.assertEqual(optimization_render._context_row(group, True), expected)

    def test_runs_row_with_a_per_round_figure(self):
        expected = "4 (4 reported usage), 12,345 billed over 2 round(s), 6,172 each"
        self.assertEqual(optimization_render._runs_row(4, 4, 12345, 2, 6172), expected)


def round_event(status: str = "ok", billed: int = 1000) -> Dict[str, Any]:
    """A code round as ``review run`` records it, with one reviewer when it ran."""
    return {
        "stage": "review",
        "status": status,
        "optimization": {
            "requested_level": "balanced",
            "level": "balanced",
            "escalated": False,
            "gate": "allow",
            "test_status": "ok",
            "reviewer_limit": None,
        },
        "reviewers": [] if status == opt_mod.REFUSED else [{"usage": {"billed_tokens": billed}}],
    }


class TestTheReportCommand(IsolatedCase):
    def test_the_estimated_saving_separates_thousands(self):
        workspace = self.cli_workspace()
        workspace.record_event("review", "ok", round_event(billed=1500))
        workspace.record_event("review", opt_mod.REFUSED, round_event(status=opt_mod.REFUSED))
        code, out, _ = run_cli("optimization", "report")
        self.assertEqual(code, 0)
        self.assertIn("Estimated saving from 1 gate-refused round(s): ~1,500 billed tokens.", out)


# --------------------------------------------------------------------------- tokens, status, summary


class TestTokenLines(IsolatedCase):
    """`tokens show`, `status` and `summary`: what each prints about spend.

    The summary lines are asserted whole, padding included.
    """

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        self.book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))

    def record(self, stage: str, usage: Usage) -> None:
        self.book.record_usage(stage, usage.to_dict())

    def status_line(self) -> str:
        code, out, _ = run_cli("status")
        self.assertEqual(code, 0)
        lines = [line for line in out.splitlines() if line.startswith("Tokens:")]
        self.assertEqual(len(lines), 1, out)
        return lines[0]

    def summary_block(self) -> List[str]:
        code, out, _ = run_cli("summary")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        return lines[lines.index("Tokens:") + 1 :]

    def test_the_prompt_line_separates_thousands(self):
        self.record("implementer", Usage(source="claude", input_tokens=10, prompt_chars=12345))
        code, out, _ = run_cli("tokens", "show")
        self.assertEqual(code, 0)
        expected = (
            "Prompt text this repo composed: 12,345 chars over 1 run(s). That is the part it can shorten."
        )
        self.assertIn(expected, out)

    def test_complete_totals_with_a_cost(self):
        self.record("implementer", Usage(input_tokens=1000, output_tokens=234, cost_usd=0.0123))
        self.record("review", Usage(input_tokens=1_000_000, cost_usd=0.5))
        expected = "Tokens: 1,001,234 billed over 2 run(s) (dev-orchestra tokens show)"
        self.assertEqual(self.status_line(), expected)
        block = [
            "  implementer" + " " * 4 + "1,234 billed",
            "  review" + " " * 9 + "1,000,000 billed",
            "  total" + " " * 10 + "1,001,234 billed over 2 run(s) -- $0.5123",
        ]
        self.assertEqual(self.summary_block(), block)

    def test_partial_totals_with_no_cost(self):
        self.record("implementer", Usage(source="claude", input_tokens=2000, output_tokens=500))
        self.record("implementer", Usage())
        expected = "Tokens: 2,500 billed over 2 run(s), partially reported (dev-orchestra tokens show)"
        self.assertEqual(self.status_line(), expected)
        block = [
            "  implementer" + " " * 4 + "2,500 billed",
            "  total" + " " * 10 + "2,500 billed over 2 run(s) (partially reported: a floor)",
        ]
        self.assertEqual(self.summary_block(), block)

    def test_a_stage_with_no_billed_tokens_reads_as_zero(self):
        self.record("implementer", Usage(source="claude", input_tokens=1000))
        self.record("review", Usage(source="claude", input_tokens=1500, cost_usd=0.0123))
        state = self.cli_workspace().read_state()
        del state["ledger"]["tokens"]["by_stage"]["review"]["billed_tokens"]
        self.cli_workspace().write_state(state)
        expected = "Tokens: 1,000 billed over 2 run(s) (dev-orchestra tokens show)"
        self.assertEqual(self.status_line(), expected)
        block = [
            "  implementer" + " " * 4 + "1,000 billed",
            "  review" + " " * 9 + "0 billed",
            "  total" + " " * 10 + "1,000 billed over 2 run(s) -- $0.0123",
        ]
        self.assertEqual(self.summary_block(), block)


class TestRefusalReason(unittest.TestCase):
    """``_refusal_reason`` is not shared: 0 prints ``0``, and only an int is a figure."""

    def test_every_size_and_limit(self):
        template = (
            "the last %s round was refused: the change body (%s chars) is over "
            "review.context.max_chars (%s), so nothing was reviewed"
        )
        sizes = ((0, "0"), (None, "size unrecorded"), (12345, "12,345"))
        limits = (({"max_chars": 0}, "0"), ({}, "the limit"), ({"max_chars": "x"}, "the limit"))
        stages = ((False, "review"), (True, "design review"))
        for (chars, size), (field, limit), (design, stage) in product(sizes, limits, stages):
            with self.subTest(chars=chars, limit=field, design=design):
                event: Dict[str, Any] = {"context": {**field, "chars": chars}}
                expected = template % (stage, size, limit)
                self.assertEqual(cli_workflow._refusal_reason(event, design), expected)


# --------------------------------------------------------------------------- cli_review


class TestReviewCommandFigures(IsolatedCase):
    def test_adoption_line(self):
        adoption = context_mod.Adoption(mode="enclosing", adopted=[candidate(12345)], surrounding_chars=15000)
        self.assertEqual(cli_review._adoption_line(adoption), "1 symbol(s), 12,345 chars adopted")

    def test_surrounding_snapshot_lines(self):
        workspace = ws.Workspace(self.tmp).ensure()
        block = {"candidates": 3, "chars": 12345, "tree": "abcdef1234567", "path": ".ai/x.json"}
        expected = [
            "  context:  enclosing -- 3 symbol(s), 12,345 chars frozen from tree abcdef1... at .ai/x.json",
            "            adopted at review run within review.context.surrounding_chars (15,000)",
        ]
        settings = {"surrounding_chars": 15000}
        lines = cli_review._surrounding_snapshot_lines(workspace, {"surrounding": block}, settings)
        self.assertEqual(lines, expected)


class TestCoverageAdvice(unittest.TestCase):
    """The size in ``review status``'s advice: 0 and missing read as unrecorded."""

    def advice(self, coverage: Dict[str, Any]) -> List[str]:
        unverified = {"round": "unverified", "change": "unverified", **coverage}
        return review_coverage.coverage_advice(unverified, {"snapshot_reviewers_partial": 1}, False, 400_000)

    def test_a_size_over_the_limit(self):
        expected = (
            "not a clean review: 1 reviewer(s) partial, the change body (500,000 chars) was handed "
            "over as a file. Re-running the same snapshot gives the same answer: split the "
            "change and review the parts, so each part fits inline (<= 400,000 chars), or raise "
            "review.context.inline_chars, then snapshot again."
        )
        self.assertEqual(self.advice({"change_chars": 500_000}), [expected])

    def test_a_size_under_a_raised_limit(self):
        expected = (
            "not a clean review: 1 reviewer(s) partial, the change body (130,412 chars) was handed "
            "over as a file under a lower review.context.inline_chars. The limit is 400,000 chars now, "
            "so this snapshot fits inline: run review run against it again."
        )
        self.assertEqual(self.advice({"change_chars": 130_412}), [expected])

    def test_zero_and_missing_sizes(self):
        expected = (
            "not a clean review: 1 reviewer(s) partial, the change body (size unrecorded chars) was handed "
            "over as a file. Re-running the same snapshot gives the same answer: split the "
            "change and review the parts, so each part fits inline (<= 400,000 chars), or raise "
            "review.context.inline_chars, then snapshot again."
        )
        for coverage in ({"change_chars": 0}, {}):
            with self.subTest(coverage=coverage):
                self.assertEqual(self.advice(coverage), [expected])


#: A carried mark with the round it started at, in every unclean state but
#: the round's own handover.
SINCE = "change unverified since round 2"


def unclean(state: str) -> Dict[str, Any]:
    """``{"coverage", "counts"}`` in one of ``coverage_state``'s four states."""
    coverage: Dict[str, Any] = {"change": "unverified", "unverified_since": 2}
    if state == "round_unverified":
        coverage.update(round="unverified", change_chars=130_412)
        return {"coverage": coverage, "counts": {"snapshot_reviewers_partial": 2}}
    if state == "no_reviewer":
        return {"coverage": dict(coverage, round="none"), "counts": {}}
    ok = 0 if state == "none_ok" else 1
    return {"coverage": dict(coverage, round="complete"), "counts": {"snapshot_reviewers_ok": ok}}


class TestEveryCoverageAdvice(unittest.TestCase):
    """``review status``'s advice, verbatim, for every state on both paths."""

    def advice(self, state: str, design: bool, limit: int = 100_000, spent: bool = False) -> List[str]:
        case = unclean(state)
        return review_coverage.coverage_advice(case["coverage"], case["counts"], spent, limit, design)

    def test_a_round_handed_over_as_a_file(self):
        expected = {
            (False, False): (
                "not a clean review: 2 reviewer(s) partial, the change body (130,412 chars) was handed "
                "over as a file. Re-running the same snapshot gives the same answer: split the change "
                "and review the parts, so each part fits inline (<= 100,000 chars), or raise "
                "review.context.inline_chars, then snapshot again."
            ),
            (True, False): (
                "not a clean review: 2 reviewer(s) partial, the plan (130,412 chars) was handed over as "
                "a file. Re-running the same plan gives the same answer: shorten .ai/plan.md to fit "
                "inline (<= 100,000 chars), or raise review.context.inline_chars, then run the design "
                "round again."
            ),
            (False, True): (
                "not a clean review: 2 reviewer(s) partial, the change body (130,412 chars) was handed "
                "over as a file under a lower review.context.inline_chars. The limit is 400,000 chars "
                "now, so this snapshot fits inline: run review run against it again."
            ),
            (True, True): (
                "not a clean review: 2 reviewer(s) partial, the plan (130,412 chars) was handed over as "
                "a file under a lower review.context.inline_chars. The limit is 400,000 chars now, so "
                "the plan fits inline: run the design round again."
            ),
        }
        for (design, raised), line in expected.items():
            with self.subTest(design=design, raised=raised):
                limit = 400_000 if raised else 100_000
                self.assertEqual(self.advice("round_unverified", design, limit), [line])

    def test_where_a_raised_limit_begins(self):
        raised = "was handed over as a file under a lower review.context.inline_chars"
        same = "Re-running the same snapshot gives the same answer"
        cases = (
            ("equal to the limit", 130_412, 130_412, raised),
            ("one over the limit", 130_413, 130_412, same),
            ("no size recorded", None, 400_000, same),
            ("a size of 0", 0, 400_000, same),
            ("a bool", True, 400_000, same),
            ("a string", "130412", 400_000, same),
            ("a limit of 0", 130_412, 0, same),
        )
        for name, chars, limit, wording in cases:
            with self.subTest(name):
                case = unclean("round_unverified")
                coverage = dict(case["coverage"], change_chars=chars)
                (line,) = review_coverage.coverage_advice(coverage, case["counts"], False, limit)
                self.assertIn(wording, line)

    def test_the_carried_states(self):
        expected = {
            ("no_reviewer", False): (
                "%s -- no reviewer has run against this snapshot; run the reviewers against it with "
                "review run." % SINCE
            ),
            ("no_reviewer", True): (
                "%s -- no reviewer has run against this plan; run the design round." % SINCE
            ),
            ("none_ok", False): (
                "%s -- no reviewer came back ok for this snapshot; re-run the reviewers that did not." % SINCE
            ),
            ("none_ok", True): (
                "%s -- no reviewer came back ok for this plan; re-run the reviewers that did not." % SINCE
            ),
            ("fix_only", False): (
                "%s -- re-snapshot with --full once the whole change fits inline, then run again." % SINCE
            ),
            ("fix_only", True): (
                "%s -- no round has shown a reviewer the whole plan; shorten .ai/plan.md until it fits "
                "inline, then run the design round again." % SINCE
            ),
        }
        for (state, design), line in expected.items():
            with self.subTest(state=state, design=design):
                self.assertEqual(self.advice(state, design), [line])

    def test_the_budget_line_follows_an_unverified_change(self):
        exhausted = "iteration budget exhausted -- report the change as not reviewed in full"
        for state in ("round_unverified", "no_reviewer", "none_ok", "fix_only"):
            for design in (False, True):
                with self.subTest(state=state, design=design):
                    lines = self.advice(state, design, spent=True)
                    self.assertEqual(lines[1:], [exhausted])
                    self.assertEqual(lines[:1], self.advice(state, design))

    def test_no_state_is_no_advice(self):
        clean = {"round": "complete", "change": "complete"}
        for coverage in ({}, clean, {"round": "none", "change": "none"}):
            for spent in (False, True):
                with self.subTest(coverage=coverage, spent=spent):
                    self.assertEqual(review_coverage.coverage_advice(coverage, {}, spent, 100_000), [])


class TestEveryCoverageHeadline(unittest.TestCase):
    """``consolidated.md``'s ``- Coverage:`` line, verbatim, for every state."""

    def line(self, state: str, **coverage: Any) -> str:
        case = unclean(state)
        return review_coverage.coverage_headline({**case["coverage"], **coverage}, case["counts"])

    def test_every_state(self):
        expected = {
            "round_unverified": (
                "- Coverage: round unverified -- the change body (130,412 chars) was handed over as a "
                "file; not a clean review"
            ),
            "no_reviewer": (
                "- Coverage: %s -- no reviewer has run against this snapshot; run the reviewers against "
                "it with review run" % SINCE
            ),
            "none_ok": (
                "- Coverage: %s -- no reviewer came back ok for this snapshot; re-run the reviewers that "
                "did not" % SINCE
            ),
            "fix_only": (
                "- Coverage: %s -- this round inlined the fix only; snapshot --full once the whole change "
                "fits inline" % SINCE
            ),
        }
        for state, line in expected.items():
            with self.subTest(state=state):
                self.assertEqual(self.line(state), line)

    def test_the_recorded_limit_rides_along(self):
        expected = (
            "- Coverage: round unverified -- the change body (130,412 chars) was handed over as a file "
            "(review.context.inline_chars 100,000); not a clean review"
        )
        self.assertEqual(self.line("round_unverified", inline_chars=100_000), expected)

    def test_a_mark_with_no_round_number(self):
        for since in (None, True, "2"):
            with self.subTest(since=since):
                expected = (
                    "- Coverage: change unverified -- no reviewer has run against this snapshot; run the "
                    "reviewers against it with review run"
                )
                self.assertEqual(self.line("no_reviewer", unverified_since=since), expected)

    def test_the_default_line(self):
        cases = (
            ({}, "- Coverage: round none, change none"),
            ({"round": "complete", "change": "complete"}, "- Coverage: round complete, change complete"),
            ({"round": "complete", "change": "none"}, "- Coverage: round complete, change none"),
        )
        for coverage, expected in cases:
            with self.subTest(coverage=coverage):
                self.assertEqual(review_coverage.coverage_headline(coverage, {}), expected)


class TestTheWordingTable(unittest.TestCase):
    def test_every_state_is_worded_on_both_paths(self):
        """An advice line for each, and a headline for the code states only."""
        states = ("round_unverified", "no_reviewer", "none_ok", "fix_only", "round_raised")
        for state in states:
            for design in (False, True):
                with self.subTest(state=state, design=design):
                    wording = review_coverage._WORDING[(state, design)]
                    self.assertTrue(wording.advice)
                    headline = not design and state != "round_raised"
                    self.assertEqual(wording.headline is not None, headline)
        self.assertEqual(len(review_coverage._WORDING), len(states) * 2)


@unittest.skipUnless(has_git(), "git is required for review snapshots")
class TestSnapshotWarning(IsolatedCase):
    def test_the_warning_separates_thousands(self):
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "review.context.max_chars", "2000")
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write("big.py", "# %s\n" % ("x" * 3000))
        code, out, _ = run_cli("review", "snapshot")
        self.assertEqual(code, 0)
        chars = review_mod.snapshot_chars(self.cli_workspace())
        self.assertGreaterEqual(chars, 1000)
        expected = (
            "  WARNING:  %s chars is over review.context.max_chars (2,000) -- review run will "
            "refuse this change.\n" % format(chars, ",")
        )
        self.assertIn(expected, out)


# --------------------------------------------------------------------------- context


def context_record() -> Dict[str, Any]:
    """One reviewer's surrounding context: a symbol adopted, one left out for the budget."""
    return {
        "adopted": [candidate(12345)],
        "adopted_chars": 12345,
        "trimmed": [trimmed(5678)],
        "trimmed_chars": 5678,
    }


class TestContextWords(unittest.TestCase):
    def test_summary(self):
        expected = "1 symbol(s), 12,345 chars adopted; 1 left out (5,678 chars, budget)"
        self.assertEqual(context_mod.summary(context_record()), expected)

    def test_brief(self):
        self.assertEqual(context_mod.brief(context_record()), "1 symbol(s), 12,345 chars adopted, 1 left out")

    def test_status_line(self):
        expected = "enclosing, 12,345 chars adopted, 1 symbol(s) left out (budget)"
        self.assertEqual(context_mod.status_line(context_record()), expected)
        nothing = {"adopted_chars": 0, "reason": "no budget"}
        self.assertEqual(context_mod.status_line(nothing), "enclosing, 0 chars adopted -- no budget")


# --------------------------------------------------------------------------- review_consolidation


class TestConsolidationLines(unittest.TestCase):
    def test_coverage_line_with_zero_and_missing_sizes(self):
        expected = (
            "- Coverage: round unverified -- the change body (size unrecorded chars) was handed over "
            "as a file; not a clean review"
        )
        for coverage in ({"round": "unverified", "change_chars": 0}, {"round": "unverified"}):
            with self.subTest(coverage=coverage):
                self.assertEqual(review_coverage.coverage_headline(coverage, {}), expected)

    def test_coverage_line_with_a_size_and_a_limit(self):
        coverage = {"round": "unverified", "change_chars": 130_412, "inline_chars": 400_000}
        expected = (
            "- Coverage: round unverified -- the change body (130,412 chars) was handed over as a file "
            "(review.context.inline_chars 400,000); not a clean review"
        )
        self.assertEqual(review_coverage.coverage_headline(coverage, {}), expected)

    def test_over_budget_line(self):
        tail = " chars, over review.context.max_chars -- reviewed only because --force was given"
        for snapshot in ({"budget_chars": 0}, {}):
            with self.subTest(snapshot=snapshot):
                line = review_consolidation._over_budget_line(snapshot)
                self.assertEqual(line, "- Change: size unrecorded" + tail)
        line = review_consolidation._over_budget_line({"budget_chars": 4001})
        self.assertEqual(line, "- Change: 4,001" + tail)

    def test_surrounding_names(self):
        record = {"adopted": [candidate(1234)], "adopted_chars": 12345}
        expected = ["Shown (1 symbol(s), 12,345 chars):", "- app.py:1-40 add (function, 1,234 chars)"]
        self.assertEqual(review_consolidation._surrounding_names(record, "Shown")[:2], expected)


def crowded_record() -> Dict[str, Any]:
    """A record with six symbols left out and six files not extracted: one over each cut."""
    return {
        "adopted": [candidate(12345)],
        "adopted_chars": 12345,
        "trimmed": [dict(candidate(100, "s%d" % index), reason="budget") for index in range(6)],
        "trimmed_chars": 600,
        "skipped": [{"path": "f%d.py" % index, "reason": "syntax error"} for index in range(6)],
    }


#: ``context_record()``'s headline in ``consolidated.md``.
SHARED_LINE = (
    "- Surrounding context: enclosing -- 1 symbol(s), 12,345 chars adopted; 1 left out (5,678 chars, budget)"
)

SHOWN = [
    "Shown to every reviewer (1 symbol(s), 12,345 chars):",
    "- app.py:1-40 add (function, 12,345 chars)",
    "",
    "Left out (budget):",
    "- app.py:1-40 sub (function, 5,678 chars)",
]


class TestSurroundingWords(unittest.TestCase):
    """Both renderings of the surrounding context, verbatim."""

    def by_reviewer(self) -> Dict[str, Any]:
        return {"shared": False, "by_reviewer": {"m1": context_record(), "m2": None}}

    def test_status_lines_shared(self):
        expected = [
            "surrounding context: enclosing, 12,345 chars adopted, 6 symbol(s) left out (budget)",
            "  left out: app.py:1-40 s0",
            "  left out: app.py:1-40 s1",
            "  left out: app.py:1-40 s2",
            "  left out: app.py:1-40 s3",
            "  left out: app.py:1-40 s4",
            "  and 1 more",
            "  not extracted: f0.py -- syntax error",
            "  not extracted: f1.py -- syntax error",
            "  not extracted: f2.py -- syntax error",
            "  not extracted: f3.py -- syntax error",
            "  not extracted: f4.py -- syntax error",
            "  and 1 more not extracted",
        ]
        block = {"shared": True, **crowded_record()}
        self.assertEqual(cli_review._surrounding_status_lines(block), expected)

    def test_status_lines_by_reviewer(self):
        expected = [
            "surrounding context (m1): enclosing, 12,345 chars adopted, 1 symbol(s) left out (budget)",
            "  left out: app.py:1-40 sub",
            "surrounding context (m2): none (ran with review.context.surrounding none)",
        ]
        self.assertEqual(cli_review._surrounding_status_lines(self.by_reviewer()), expected)

    def test_status_lines_with_nothing_adopted(self):
        block = {"shared": True, "adopted": [], "adopted_chars": 0, "reason": "not frozen"}
        expected = ["surrounding context: enclosing, 0 chars adopted -- not frozen"]
        self.assertEqual(cli_review._surrounding_status_lines(block), expected)

    def test_headline(self):
        block = {"shared": True, **context_record()}
        self.assertEqual(review_consolidation._surrounding_line(block), SHARED_LINE)
        split = (
            "- Surrounding context: differs by reviewer -- m1: 1 symbol(s), 12,345 chars adopted, "
            "1 left out; m2: none"
        )
        self.assertEqual(review_consolidation._surrounding_line(self.by_reviewer()), split)

    def test_section_shared(self):
        expected = ["", "## Surrounding context", "", *SHOWN]
        block = {"shared": True, **context_record()}
        self.assertEqual(review_consolidation._surrounding_section(block), expected)

    def test_section_by_reviewer(self):
        expected = [
            "",
            "## Surrounding context",
            "",
            "### m1",
            "",
            "Shown (1 symbol(s), 12,345 chars):",
            *SHOWN[1:],
            "",
            "### m2",
            "",
            "None (ran with review.context.surrounding none).",
        ]
        self.assertEqual(review_consolidation._surrounding_section(self.by_reviewer()), expected)

    def test_section_with_nothing_adopted_and_a_file_not_extracted(self):
        skipped = [{"path": "bad.py", "reason": "syntax error"}]
        record = {"adopted": [], "reason": "not frozen", "skipped": skipped}
        expected = [
            "",
            "## Surrounding context",
            "",
            "Nothing adopted (not frozen).",
            "",
            "Not extracted (1 file(s)):",
            "- `bad.py` -- syntax error",
        ]
        self.assertEqual(review_consolidation._surrounding_section({"shared": True, **record}), expected)

    def test_section_with_no_name_to_give(self):
        for block in (
            {"shared": True, "adopted": [], "reason": "no budget"},
            {"shared": False, "by_reviewer": {"m1": None, "m2": {"adopted": []}}},
        ):
            with self.subTest(block=block):
                self.assertEqual(review_consolidation._surrounding_section(block), [])


#: A report holding one of everything ``render_consolidation`` prints.
FULL_REPORT: Dict[str, Any] = {
    "generated_at": "2026-01-02T03:04:05Z",
    "iteration": 2,
    "snapshot": {"sha256": "abcdef0123456789", "over_budget": True, "budget_chars": 4001},
    "coverage": {"round": "complete", "change": "complete"},
    "surrounding": {"shared": True, **context_record()},
    "reviewers": [
        {"id": "r1", "role": "general", "provider": "mock", "model": "m-1", "status": "ok", "findings": 2},
        {"id": "r2", "role": "security", "provider": "mock", "model": "", "status": "partial", "error": "x"},
        {"id": "r3", "role": "general", "provider": "codex", "model": "y", "status": "failed", "error": "z"},
    ],
    "counts": {
        "findings_total": 2,
        "duplicates_merged": 1,
        "reviewers_ok": 1,
        "reviewers_total": 3,
        "reviewers_partial": 1,
        "snapshot_reviewers_ok": 1,
        "snapshot_reviewers_total": 2,
        "snapshot_reviewers_partial": 0,
    },
    "duplicate_candidates": [{"ids": ["F1", "F2"], "file": "a.py", "shared_code": ["foo.bar", "baz_qux"]}],
    "findings": [
        {
            "id": "F1",
            "severity": "high",
            "category": "correctness",
            "file": "a.py",
            "line": "2",
            "reported_by": ["r1", "r2"],
            "possible_duplicates": ["F2", "F2"],
            "triage": "accepted",
            "triage_note": "real",
            "problem": "p1",
            "impact": "i1",
            "evidence": "e1",
            "recommended_fix": "f1",
        },
        {"id": "F2", "severity": "low", "file": "a.py", "reported_by": ["r3"], "problem": "p2"},
    ],
}


class TestTheConsolidatedReport(IsolatedCase):
    """``render_consolidation`` whole, and the order of what ``build_consolidation`` returns."""

    def test_every_section_verbatim(self):
        expected = [
            "# Consolidated review",
            "",
            "- Generated: 2026-01-02T03:04:05Z",
            "- Iteration: 2",
            "- Snapshot: abcdef012345",
            "- Reviewers: 1 ok / 3 total, 1 partial",
            "- Reviewers of this snapshot: 1 ok / 2 total",
            "- Coverage: round complete, change complete",
            SHARED_LINE,
            "- Change: 4,001 chars, over review.context.max_chars -- reviewed only because --force was given",
            "- Findings: 2 (1 duplicate report(s) merged)",
            "",
            "## Reviewers",
            "",
            "- ok [general] r1 / mock / m-1 -- 2 finding(s)",
            "- PARTIAL [security] r2 / mock / unknown model -- x",
            "- FAILED [general] r3 / codex / y -- z",
            "",
            "## Surrounding context",
            "",
            *SHOWN,
            "",
            "## Possible duplicates (confirm during triage)",
            "",
            "Different reviewers quoting the same code. Auto-merge stays conservative on",
            "purpose -- collapsing two distinct bugs hides one -- so these are suggestions.",
            "Mark a confirmed one with `review triage <id> --status duplicate`.",
            "",
            "- F1 ~ F2  (`a.py`, shared: `foo.bar`, `baz_qux`)",
            "",
            "## Findings",
            "",
            "### F1 [HIGH] correctness",
            "",
            "- Location: `a.py`:2",
            "- Reported by: r1, r2",
            "- Possible duplicate of: F2",
            "- Triage: accepted (real)",
            "- Problem: p1",
            "- Impact: i1",
            "- Evidence: e1",
            "- Recommended fix: f1",
            "",
            "### F2 [LOW] general",
            "",
            "- Location: `a.py`:n/a",
            "- Reported by: r3",
            "- Possible duplicate of: none",
            "- Triage: needs-triage",
            "- Problem: p2",
            "- Impact: ",
            "- Evidence: ",
            # The document is right-stripped, so the last label loses its space.
            "- Recommended fix:",
        ]
        self.assertEqual(review_consolidation.render_consolidation(FULL_REPORT), "\n".join(expected) + "\n")

    def test_an_empty_report_verbatim(self):
        expected = (
            "# Consolidated review\n\n- Generated: None\n- Iteration: None\n- Snapshot: \n"
            "- Reviewers: None ok / None total\n- Findings: None (None duplicate report(s) merged)\n\n"
            "## Reviewers\n\n\n## Findings\n\nNo findings were reported.\n"
        )
        empty = {"counts": {}, "reviewers": []}
        self.assertEqual(review_consolidation.render_consolidation(empty), expected)

    def build(self, runs: List[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        workspace = ws.Workspace(self.tmp).ensure()
        meta = {"sha256": "abcdef0123456789", "files": ["a.py"], "round_id": "round-1"}
        ws.write_json(workspace.snapshot_meta_path, meta)
        return review_consolidation.build_consolidation(workspace, runs, [], 1, "", **kwargs)

    def test_the_order_of_a_built_report(self):
        run: Dict[str, Any] = {"id": "r1", "status": "ok", "delivery": "inline", "snapshot": "abcdef012345"}
        data = self.build([dict(run, surrounding=context_record())], completed_round="round-1")
        head = ["generated_at", "iteration", "lineage", "snapshot", "coverage"]
        tail = ["reviewers", "counts", "duplicate_candidates", "findings"]
        self.assertEqual(list(data), [*head, "surrounding", *tail])
        snapshot = ["sha256", "files", "over_budget", "budget_chars"]
        self.assertEqual(list(data["snapshot"]), [*snapshot, "round_id"])
        coverage = ["round", "change", "unverified_since", "change_chars", "inline_chars"]
        self.assertEqual(list(data["coverage"]), coverage)
        reviewers = ["reviewers_total", "reviewers_ok", "reviewers_partial", "reviewers_failed"]
        counts = [
            "findings_total",
            "by_severity",
            "duplicates_merged",
            *reviewers,
            *["snapshot_" + key for key in reviewers],
            "duplicate_candidates",
        ]
        self.assertEqual(list(data["counts"]), counts)

        data = self.build([run], unreviewed_round="round-1")
        self.assertEqual(list(data), [*head, *tail])
        self.assertEqual(list(data["snapshot"]), [*snapshot, "unreviewed_round"])


# --------------------------------------------------------------------------- review_snapshot


class TestWithheldLines(unittest.TestCase):
    def test_exact_and_inexact_counts(self):
        self.assertEqual(review_snapshot.withheld_lines([{"added": 1200, "deleted": 34}]), "1,234")
        withheld = [{"added": 1200, "deleted": 34}, {"added": None, "deleted": None}]
        self.assertEqual(review_snapshot.withheld_lines(withheld), "1,234+")


# --------------------------------------------------------------------------- suggest


class TestSuggestRender(unittest.TestCase):
    def test_counts_over_a_thousand(self):
        listing = suggest_mod.Listing(
            root="/repo",
            source="git",
            listed=["f%d.py" % index for index in range(1234)],
            evidence=[],
            truncated=False,
            exclude=(),
            notes=[],
        )
        suggestion = suggest_mod.Suggestion(
            role="database",
            paths=["db/**"],
            evidence=["db/schema.sql"],
            matches=1200,
            withheld_matches=1001,
        )
        reviewer: Dict[str, Any] = {"id": "claude-database", "provider": "claude"}
        reviewer["model"] = {"family": "large"}
        result = suggest_mod.Result(listing, [suggestion], [reviewer], [])
        matched = (
            "    matches 1,200 of 1,234 files; 1,001 of them are withheld by review.exclude, "
            "and those still bring it in"
        )
        expected = [
            "Listed 1,234 files in /repo (git ls-files).",
            "",
            "Proposed:",
            "  claude-database  claude / large  role database",
            "    when.paths: db/**",
            "    evidence: db/schema.sql",
            matched,
            suggest_mod.FOOTER,
        ]
        self.assertEqual(suggest_mod.render(result).splitlines(), expected)


# --------------------------------------------------------------------------- broken configuration


class TestBrokenConfiguration(IsolatedCase):
    """What the lenient loads do today with a project file that is invalid or does not parse."""

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")

    def break_project_file(self) -> str:
        """Write a project file that does not parse, and return what reading it raises."""
        self.write(".dev-orchestra.yaml", "[1]\n")
        return "%s: top level must be a mapping\n" % present(config_mod.find_project_config())

    def test_config_show_lists_the_problems_of_an_invalid_file(self):
        self.write(".dev-orchestra.yaml", "version: 1\nreview:\n  context:\n    max_chars: 0\n")
        code, out, err = run_cli("config", "show")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("\nProblems:\n", out)
        self.assertIn("  - review.context.max_chars: must be a positive integer\n", out)

    def test_config_reset_survives_a_project_file_that_does_not_parse(self):
        message = self.break_project_file()
        code, out, err = run_cli("config", "reset", "--scope", "global")
        self.assertEqual(code, 0)
        expected = (
            "Reset global configuration: overrides cleared, every value now follows preset %s and the "
            "built-in defaults (%s)\n" % (presets_mod.DEFAULT, config_mod.global_config_path())
        )
        self.assertEqual(out, expected)
        self.assertEqual(err, message)

    def test_reviewer_add_survives_a_project_file_that_does_not_parse(self):
        """The write is done; the warnings after it are skipped without a word."""
        self.break_project_file()
        code, out, err = run_cli("reviewer", "add", "--scope", "global", "--provider", "mock", "--id", "m9")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertTrue(out.startswith("Added reviewer m9 (mock / "), out)
        self.assertEqual(len(out.splitlines()), 1, out)

    def test_config_set_writes_and_then_fails_on_a_project_file_that_does_not_parse(self):
        """No `try` of its own: the reload after the write raises, and `main` reports it."""
        message = self.break_project_file()
        code, out, err = run_cli("config", "set", "--scope", "global", "review.context.max_chars", "5000")
        self.assertEqual(code, 2)
        global_path = config_mod.global_config_path()
        self.assertEqual(out, "review.context.max_chars = 5000  (global: %s)\n" % global_path)
        self.assertEqual(err, message)
        written = config_mod.read_config_file(global_path)
        self.assertEqual(written["review"]["context"]["max_chars"], 5000)


if __name__ == "__main__":
    unittest.main()
