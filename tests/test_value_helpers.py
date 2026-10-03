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
    review_consolidation,
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
        for unrecorded in (0, None, ""):
            with self.subTest(chars=unrecorded):
                self.assertEqual(ws.fmt_size(unrecorded), "size unrecorded")
        self.assertEqual(ws.fmt_size(1234), "1,234")
        self.assertEqual(ws.fmt_size(-5), "-5")

    def test_fmt_usd(self):
        self.assertEqual(ws.fmt_usd(0.0123), "$0.0123")
        self.assertEqual(ws.fmt_usd(0.00004), "$0.0000")
        self.assertEqual(ws.fmt_usd(1234.5), "$1234.5000")


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
        self.assertEqual(cli_state._scorecard_spend(group, True), expected)
        unpriced = dict(group, priced_runs=0)
        expected = "2 run(s), 12,345 billed, no cost reported"
        self.assertEqual(cli_state._scorecard_spend(unpriced, False), expected)

    def test_scorecard_per_accepted_priced_and_unpriced(self):
        priced = {"billed_per_accepted": 12345, "cost_per_accepted": 0.25}
        self.assertEqual(cli_state._scorecard_per_accepted(priced), "12,345 billed / $0.25 per accepted")
        unpriced = {"billed_per_accepted": 12345, "cost_per_accepted": None}
        self.assertEqual(cli_state._scorecard_per_accepted(unpriced), "12,345 billed per accepted, $ -")

    def test_scorecard_effort_line(self):
        scorecard = {
            "code": {"rounds_read": 1, "rounds_recorded": 1, "reviewers": {}, "panel": {}},
            "total": {"accepted": 3, "rounds_read": 1, "rounds_recorded": 1, "billed_tokens": 12345},
        }
        expected = "Review effort, code and design together: 3 accepted over 1 of 1 recorded round(s); "
        self.assertIn(expected + "12,345 billed,", cli_state._scorecard_rows(scorecard))

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
        self.assertEqual(cli_state._paired_rows({"pairs": [pair], "pairs_total": 1})[1], expected)

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
        self.assertEqual(cli_state._context_row(group, True), expected)

    def test_runs_row_with_a_per_round_figure(self):
        expected = "4 (4 reported usage), 12,345 billed over 2 round(s), 6,172 each"
        self.assertEqual(cli_state._runs_row(4, 4, 12345, 2, 6172), expected)


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
        return cli_review._coverage_advice(unverified, {"snapshot_reviewers_partial": 1}, False, 400_000)

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
                self.assertEqual(review_consolidation._coverage_line(coverage, {}), expected)

    def test_coverage_line_with_a_size_and_a_limit(self):
        coverage = {"round": "unverified", "change_chars": 130_412, "inline_chars": 400_000}
        expected = (
            "- Coverage: round unverified -- the change body (130,412 chars) was handed over as a file "
            "(review.context.inline_chars 400,000); not a clean review"
        )
        self.assertEqual(review_consolidation._coverage_line(coverage, {}), expected)

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
