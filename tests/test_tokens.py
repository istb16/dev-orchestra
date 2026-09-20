"""Token accounting: what a run cost, and how much of that is actually known.

This is measurement, not a budget -- nothing here refuses a run. The property
worth protecting is therefore not a limit but *honesty*, and these tests are
mostly about the ways a cost report can lie:

* a CLI that reports nothing must not make a stage look free
* a total summed over partial data must not read as authoritative
* a figure the CLI never printed must never be invented from the prompt we sent
* a run that failed still spent what it spent

The last one matters because a failed reviewer is the cheapest thing to forget
and one of the more expensive things to pay for twice.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git

from orchestrator import cli
from orchestrator import ledger as ledger_mod
from orchestrator.providers import Usage
from orchestrator.providers.claude import parse_stream_usage
from orchestrator.providers.codex import parse_usage_text

FINDING = """## Finding
- Severity: high
- File: app.py
- Line: 2
- Category: correctness
- Problem: subtraction was used where addition was intended
- Impact: every caller gets the wrong total
- Evidence: return a - b
- Recommended fix: restore the addition
"""


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def stream(*events):
    return "".join(json.dumps(event) + "\n" for event in events)


def _row(out, name):
    """One row of the `tokens show` table, split into its columns."""
    for line in out.splitlines():
        columns = line.split()
        if columns and columns[0] == name:
            return columns
    raise AssertionError("no row for %r in:\n%s" % (name, out))


def _uninstalled():
    from orchestrator.providers import Detection

    return Detection(installed=False, error="not installed")


RESULT = {
    "type": "result",
    "subtype": "success",
    "result": "done",
    "total_cost_usd": 0.0421,
    "usage": {
        "input_tokens": 8200,
        "output_tokens": 2100,
        "cache_read_input_tokens": 15000,
        "cache_creation_input_tokens": 1200,
    },
}


# --------------------------------------------------------------------------- usage


class TestUsageArithmetic(unittest.TestCase):
    def test_billed_tokens_exclude_cache_reads(self):
        """A well-cached run must not outrank an expensive one.

        Cache reads are roughly a tenth of the price of fresh input, so folding
        them into one headline figure would put the cheapest stage at the top
        of the table.
        """
        usage = Usage(input_tokens=1000, output_tokens=100, cache_read_tokens=50_000, cache_write_tokens=200)
        self.assertEqual(usage.billed_tokens, 1300)

    def test_a_cli_that_only_reports_a_total_still_has_one_figure(self):
        self.assertEqual(Usage(total_tokens=900).billed_tokens, 900)

    def test_an_unreported_run_is_not_a_zero_cost_run(self):
        usage = Usage()
        self.assertFalse(usage.measured)
        self.assertIsNone(usage.billed_tokens)
        self.assertEqual(usage.source, "unreported")

    def test_a_cost_without_a_breakdown_is_not_a_measurement(self):
        """Money and tokens are separate claims; one does not imply the other."""
        self.assertFalse(Usage(cost_usd=0.5).measured)

    def test_tool_activity_starts_unreported_rather_than_at_zero(self):
        usage = Usage()
        self.assertIsNone(usage.tool_uses)
        self.assertIsNone(usage.tool_uses_by_name)
        self.assertIsNone(usage.tool_output_chars)

    def test_tool_activity_is_not_a_token_measurement(self):
        """`measured` means the CLI reported tokens. A run that counted its
        tool calls and nothing else has still not said what it cost."""
        self.assertFalse(Usage(tool_uses=9, tool_output_chars=4000).measured)


# --------------------------------------------------------------------------- claude


class TestClaudeUsage(unittest.TestCase):
    def test_the_result_event_is_read_field_by_field(self):
        usage = parse_stream_usage(stream({"type": "system"}, RESULT))
        self.assertEqual(usage.input_tokens, 8200)
        self.assertEqual(usage.output_tokens, 2100)
        self.assertEqual(usage.cache_read_tokens, 15000)
        self.assertEqual(usage.cache_write_tokens, 1200)
        self.assertAlmostEqual(usage.cost_usd, 0.0421)
        self.assertEqual(usage.source, "claude result event")

    def test_the_four_counts_are_never_summed_into_one(self):
        """Keeping them apart is the whole point: they are priced differently."""
        usage = parse_stream_usage(stream(RESULT))
        self.assertNotEqual(usage.input_tokens, usage.billed_tokens)
        self.assertEqual(usage.billed_tokens, 8200 + 2100 + 1200)

    def test_a_result_event_with_no_usage_reports_nothing(self):
        self.assertIsNone(parse_stream_usage(stream({"type": "result", "result": "done"})))

    def test_a_cost_alone_is_still_worth_recording(self):
        usage = parse_stream_usage(stream({"type": "result", "result": "x", "total_cost_usd": 0.02}))
        self.assertAlmostEqual(usage.cost_usd, 0.02)
        self.assertFalse(usage.measured)

    def test_output_that_is_not_a_stream_reports_nothing(self):
        self.assertIsNone(parse_stream_usage("just some prose\nno json here\n"))

    def test_a_schema_change_degrades_to_unreported_rather_than_wrong(self):
        """Strings and booleans where numbers were are not token counts."""
        event = {"type": "result", "result": "x", "usage": {"input_tokens": "8200", "output_tokens": True}}
        self.assertIsNone(parse_stream_usage(stream(event)))

    def test_the_last_result_event_wins(self):
        stale = dict(RESULT, usage={"input_tokens": 1, "output_tokens": 1})
        usage = parse_stream_usage(stream(stale, RESULT))
        self.assertEqual(usage.input_tokens, 8200)


# --------------------------------------------------------------------------- codex


class TestCodexUsage(unittest.TestCase):
    """What the CLI prints is a single total, on its own two lines.

    The text this is read from also contains the agent's answer, which is why
    every test below is about what must *not* be mistaken for accounting. A
    reviewer reading this repository writes about token counts: one real
    review quoted ``input_tokens: 12`` as a finding's evidence, and the
    parser recorded twelve billed tokens for that run and threw away the
    total the CLI had printed.
    """

    def test_the_footer_the_cli_actually_prints(self):
        """Label on its own line, number on the next."""
        usage = parse_usage_text("codex\nOK\ntokens used\n3,877\nOK\n")
        self.assertEqual(usage.total_tokens, 3877)
        self.assertEqual(usage.billed_tokens, 3877)

    def test_a_printed_total_is_read_as_a_total(self):
        usage = parse_usage_text("working...\ntokens used: 12,345\n")
        self.assertEqual(usage.total_tokens, 12345)
        self.assertIsNone(usage.input_tokens)
        self.assertEqual(usage.billed_tokens, 12345)

    def test_the_final_figure_wins_over_progress_updates(self):
        """Also what makes the footer win over an answer that quotes one: the
        CLI prints its accounting after the answer it is accounting for."""
        self.assertEqual(parse_usage_text("tokens used: 5\ntokens used: 900\n").total_tokens, 900)

    def test_prose_about_tokens_is_not_accounting(self):
        """The exact string that broke a real run."""
        self.assertIsNone(parse_usage_text("The fixture says input_tokens: 12"))

    def test_an_answer_discussing_tokens_does_not_override_the_footer(self):
        usage = parse_usage_text("an answer mentioning input_tokens: 12\ntokens used\n9,876\n")
        self.assertEqual(usage.total_tokens, 9876)

    def test_a_split_is_not_parsed_at_all(self):
        """These patterns were speculative -- against a format this CLI has
        never emitted -- and the only thing they ever matched was a reviewer
        writing about token accounting. The total is what was actually said."""
        usage = parse_usage_text("tokens used: 125\ninput tokens: 100\noutput tokens: 25\n")
        self.assertEqual(usage.total_tokens, 125)
        self.assertIsNone(usage.input_tokens)
        self.assertIsNone(usage.output_tokens)
        self.assertEqual(usage.billed_tokens, 125)

    def test_a_label_with_no_number_is_not_a_measurement(self):
        """The number group used to accept a bare comma, which is not one."""
        self.assertIsNone(parse_usage_text('"input_tokens": self.input_tokens,'))
        self.assertIsNone(parse_usage_text("tokens used\nunknown\n"))

    def test_a_footer_indented_inside_an_answer_is_still_not_read(self):
        """Anchored to the start of a line, so a quoted footer in a code block
        does not become the report."""
        self.assertIsNone(parse_usage_text("evidence: `tokens used: 1` appears in the test"))

    def test_prose_the_adapter_does_not_recognise_reports_nothing(self):
        """A wording change must produce no number, not a wrong one."""
        self.assertIsNone(parse_usage_text("finished successfully\n"))

    def test_stderr_is_searched_when_stdout_is_silent(self):
        self.assertEqual(parse_usage_text("", "tokens used: 42").total_tokens, 42)


class TestNothingRanIsNotAFailureToReport(IsolatedCase):
    def test_a_missing_cli_produces_a_run_that_was_never_invoked(self):
        """The mock provider overrides run() wholesale, so this exercises the
        real adapter path rather than the double."""
        from orchestrator.providers.claude import ClaudeProvider

        provider = ClaudeProvider()
        original = ClaudeProvider.detect
        ClaudeProvider.detect = lambda self: _uninstalled()
        try:
            result = provider.run("prompt", "review", self.project)
        finally:
            ClaudeProvider.detect = original
        self.assertFalse(result.ok)
        self.assertFalse(result.invoked)
        self.assertFalse(result.usage.measured)

    def test_a_run_that_reached_the_cli_is_marked_invoked(self):
        from orchestrator.providers import get_provider

        result = get_provider("mock").run("prompt", "review", self.project)
        self.assertTrue(result.invoked)


# --------------------------------------------------------------------------- ledger


class TestLedgerAccount(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))

    def measured(self, **fields):
        return Usage(source="test", **fields).to_dict()

    def test_runs_accumulate_per_stage(self):
        self.book.record_usage("architect", self.measured(input_tokens=100, output_tokens=10))
        self.book.record_usage("architect", self.measured(input_tokens=50, output_tokens=5))
        account = self.book.token_report()["by_stage"]["architect"]
        self.assertEqual(account["input_tokens"], 150)
        self.assertEqual(account["output_tokens"], 15)
        self.assertEqual(account["runs"], 2)

    def test_an_idle_workflow_still_has_an_account(self):
        """`load` hands back a blank ledger once one has been idle past
        `session_idle_reset_seconds`, which is right for a budget and wrong for
        the account: the account refuses nothing, so resetting it only hides
        what was spent. Reported from real use as "tokens show says no runs
        while state.json holds 446,430"."""
        self.book.record_usage("review", self.measured(input_tokens=446430))
        state = self.cli_workspace().read_state()
        idle = float(state["ledger"]["last_activity_monotonic"])
        limit = float(ledger_mod.DEFAULT_BUDGETS["session_idle_reset_seconds"])
        state["ledger"]["last_activity_monotonic"] = idle - limit - 60
        self.cli_workspace().write_state(state)

        self.assertEqual(self.book.token_report()["totals"]["billed_tokens"], 446430)
        # And the budget really has reset, which is the behaviour being kept.
        self.assertGreater(time.time(), 0)
        self.assertEqual(self.book.summary()["total_delegated_runs"]["used"], 0)

    def test_a_run_that_priced_nothing_is_counted_apart(self):
        """A CLI can report its tokens and no money -- Codex does, on every
        run. Counting that as measured made a cost total that omits one
        provider entirely look complete."""
        self.book.record_usage("review", self.measured(input_tokens=100, cost_usd=1.5))
        self.book.record_usage("review", self.measured(input_tokens=100))
        report = self.book.token_report()
        self.assertEqual(report["totals"]["runs"], 2)
        self.assertEqual(report["totals"]["priced_runs"], 1)
        self.assertTrue(report["complete"])
        self.assertFalse(report["priced"])

    def test_an_account_too_old_to_say_makes_no_claim(self):
        """`priced_runs` did not exist before 0.4.2, and reading its absence as
        zero made every older account report its own costs as unreported --
        the caveat firing over data that never disagreed with it."""
        self.book.record_usage("review", self.measured(input_tokens=100, cost_usd=1.5))
        state = self.cli_workspace().read_state()
        del state["ledger"]["tokens"]["by_stage"]["review"]["priced_runs"]
        self.cli_workspace().write_state(state)
        self.assertTrue(self.book.token_report()["priced"])

    def test_every_run_pricing_itself_needs_no_caveat(self):
        self.book.record_usage("review", self.measured(input_tokens=100, cost_usd=1.5))
        self.assertTrue(self.book.token_report()["priced"])

    def test_reviewers_are_accounted_for_individually(self):
        """The review stage is the one that runs the same diff N times over."""
        self.book.record_usage("review", self.measured(input_tokens=400), label="claude-general")
        self.book.record_usage("review", self.measured(input_tokens=380), label="codex-general")
        report = self.book.token_report()
        self.assertEqual(report["by_stage"]["review"]["input_tokens"], 780)
        self.assertEqual(report["by_label"]["claude-general"]["input_tokens"], 400)
        self.assertEqual(report["by_label"]["codex-general"]["input_tokens"], 380)

    def test_a_silent_cli_raises_the_run_count_but_not_the_measured_count(self):
        self.book.record_usage("review", self.measured(input_tokens=400))
        self.book.record_usage("review", Usage().to_dict())
        account = self.book.token_report()["by_stage"]["review"]
        self.assertEqual((account["runs"], account["measured_runs"]), (2, 1))
        self.assertEqual(account["input_tokens"], 400)

    def test_a_mixed_panel_is_divided_by_the_runs_that_reported_tools(self):
        """Codex reports no tool activity. Summing over `runs` would halve a
        Claude reviewer's figure for no reason but who it ran beside."""
        self.book.record_usage(
            "review",
            self.measured(
                input_tokens=1,
                tool_uses=4,
                tool_uses_by_name={"Bash": 3, "Read": 1},
                tool_output_chars=900,
            ),
            label="claude-general",
        )
        self.book.record_usage("review", self.measured(input_tokens=1), label="codex-general")
        account = self.book.token_report()["by_stage"]["review"]
        self.assertEqual((account["runs"], account["tool_reported_runs"]), (2, 1))
        self.assertEqual(account["tool_uses"], 4)
        self.assertEqual(account["tool_output_chars"], 900)
        self.assertEqual(account["tool_uses_by_name"], {"Bash": 3, "Read": 1})

    def test_the_breakdown_by_name_merges_key_by_key(self):
        for names in ({"Bash": 2, "Read": 1}, {"Bash": 1, "Grep": 5}):
            self.book.record_usage(
                "review", self.measured(tool_uses=sum(names.values()), tool_uses_by_name=names)
            )
        account = self.book.token_report()["by_stage"]["review"]
        self.assertEqual(account["tool_uses_by_name"], {"Bash": 3, "Read": 1, "Grep": 5})
        self.assertEqual(account["tool_uses"], 9)

    def test_a_run_that_used_no_tools_is_a_report_not_a_silence(self):
        """The finding this counting exists to produce is a reviewer that
        opened nothing. Recording it as "did not say" would lose it."""
        usage = self.measured(tool_uses=0, tool_uses_by_name={}, tool_output_chars=0)
        self.book.record_usage("review", usage)
        account = self.book.token_report()["by_stage"]["review"]
        self.assertEqual((account["tool_reported_runs"], account["tool_uses"]), (1, 0))
        self.assertTrue(self.book.token_report()["totals"]["tool_known"])

    def test_an_account_from_before_tool_counting_says_unknown_not_none_used(self):
        """`tool_reported_runs` did not exist before this, and reading its
        absence as zero would present "we did not count" as "no tools"."""
        self.book.record_usage("review", self.measured(tool_uses=4))
        state = self.cli_workspace().read_state()
        del state["ledger"]["tokens"]["by_stage"]["review"]["tool_reported_runs"]
        self.cli_workspace().write_state(state)
        totals = self.book.token_report()["totals"]
        self.assertFalse(totals["tool_known"])
        # And the numbers beside it are still summed rather than lost.
        self.assertEqual(totals["tool_uses"], 4)

    def test_recording_a_counted_run_does_not_erase_what_predates_counting(self):
        """The first counted run *creates* `tool_reported_runs`, so a caveat
        that rests on the key being absent disappears exactly when the upgraded
        pipeline runs again -- and the earlier runs are then reported as having
        used no tools."""
        for _ in range(3):
            self.book.record_usage("review", self.measured(input_tokens=10))
        state = self.cli_workspace().read_state()
        del state["ledger"]["tokens"]["by_stage"]["review"]["tool_reported_runs"]
        self.cli_workspace().write_state(state)

        self.book.record_usage("review", self.measured(input_tokens=10, tool_uses=4))
        totals = self.book.token_report()["totals"]
        self.assertFalse(totals["tool_known"])
        self.assertEqual(totals["tool_unknown_runs"], 3)
        self.assertEqual((totals["runs"], totals["tool_reported_runs"]), (4, 1))
        # And a later run does not re-stamp the three it already carried.
        self.book.record_usage("review", self.measured(input_tokens=10, tool_uses=1))
        self.assertEqual(self.book.token_report()["totals"]["tool_unknown_runs"], 3)

    def test_an_unreported_run_does_not_reopen_the_legacy_account(self):
        """The run that closes the account may be one that reports nothing --
        Codex, or Claude under `output_format: json`. Stamping the count
        without also creating the key left the account open, so that run
        re-stamped a larger count and was itself reported as predating the
        counting it ran alongside."""
        for _ in range(3):
            self.book.record_usage("review", self.measured(input_tokens=10))
        state = self.cli_workspace().read_state()
        del state["ledger"]["tokens"]["by_stage"]["review"]["tool_reported_runs"]
        self.cli_workspace().write_state(state)

        self.book.record_usage("review", self.measured(input_tokens=10))
        totals = self.book.token_report()["totals"]
        self.assertEqual(totals["tool_unknown_runs"], 3)
        self.assertEqual((totals["runs"], totals["tool_reported_runs"]), (4, 0))

        self.book.record_usage("review", self.measured(input_tokens=10, tool_uses=2))
        totals = self.book.token_report()["totals"]
        self.assertEqual(totals["tool_unknown_runs"], 3)
        self.assertEqual((totals["runs"], totals["tool_reported_runs"]), (5, 1))

    def test_a_malformed_breakdown_does_not_cost_the_run(self):
        self.book.record_usage("review", {"tool_uses": 2, "tool_uses_by_name": "lots"})
        self.book.record_usage("review", {"tool_uses": "some", "tool_output_chars": 5})
        account = self.book.token_report()["by_stage"]["review"]
        self.assertEqual((account["runs"], account["tool_reported_runs"]), (2, 1))
        self.assertEqual(account["tool_uses_by_name"], {})

    def test_a_partially_reported_account_declares_itself_incomplete(self):
        """The totals are a floor, and the report has to say so."""
        self.book.record_usage("architect", self.measured(input_tokens=10))
        self.assertTrue(self.book.token_report()["complete"])
        self.book.record_usage("implementer", Usage().to_dict())
        self.assertFalse(self.book.token_report()["complete"])

    def test_totals_span_every_stage(self):
        self.book.record_usage("architect", self.measured(input_tokens=100, cost_usd=0.01))
        self.book.record_usage("review", self.measured(input_tokens=200, cost_usd=0.02))
        totals = self.book.token_report()["totals"]
        self.assertEqual(totals["input_tokens"], 300)
        self.assertAlmostEqual(totals["cost_usd"], 0.03)
        self.assertEqual(totals["runs"], 2)

    def test_an_empty_account_reports_nothing_rather_than_zeroes(self):
        report = self.book.token_report()
        self.assertEqual(report["by_stage"], {})
        self.assertEqual(report["totals"]["runs"], 0)
        self.assertFalse(report["complete"])

    def test_a_malformed_report_is_not_worth_losing_the_run_over(self):
        self.book.record_usage("architect", None)
        self.book.record_usage("architect", {"input_tokens": "lots", "cost_usd": "some"})
        account = self.book.token_report()["by_stage"]["architect"]
        self.assertEqual(account["runs"], 2)
        self.assertEqual(account["input_tokens"], 0)
        self.assertEqual(account["cost_usd"], 0.0)

    def test_the_account_survives_a_reload(self):
        self.book.record_usage("test", self.measured(input_tokens=7))
        fresh = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))
        self.assertEqual(fresh.token_report()["by_stage"]["test"]["input_tokens"], 7)

    def test_recording_a_cost_never_consumes_an_attempt(self):
        """Accounting must not be able to exhaust a budget."""
        before = self.book.remaining("architect")
        self.book.record_usage("architect", self.measured(input_tokens=1))
        self.assertEqual(self.book.remaining("architect"), before)


class TestTheAccountSurvivesAReset(IsolatedCase):
    """A reset resets the budgets. It does not reset the account.

    0.4.2 stopped an idle ledger *hiding* the account. It kept destroying it:
    every writer goes through `load`, so the first write after a six-hour gap
    persisted the blank ledger `load` had minted. Measured on the workflow for
    issue #33, where `$13.78` over six stages became `$5.11` over one and the
    architect's and implementer's runs were gone from the ledger entirely.
    """

    def setUp(self):
        super().setUp()
        self.book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))

    def measured(self, **fields):
        return Usage(source="test", **fields).to_dict()

    def go_stale(self):
        """Backdate the ledger past `session_idle_reset_seconds`."""
        workspace = self.cli_workspace()
        state = workspace.read_state()
        limit = float(ledger_mod.DEFAULT_BUDGETS["session_idle_reset_seconds"])
        state["ledger"]["last_activity_monotonic"] = time.time() - limit - 60
        workspace.write_state(state)
        return state["ledger"]

    def test_an_idle_reset_keeps_the_account_and_clears_the_budgets(self):
        self.book.record_usage("architect", self.measured(input_tokens=462124), label="claude-large")
        self.book.consume("architect")
        was = self.go_stale()

        self.book.record_usage("review_fixer", self.measured(input_tokens=166298))

        report = self.book.token_report()
        self.assertEqual(report["by_stage"]["architect"]["input_tokens"], 462124)
        self.assertEqual(report["by_stage"]["review_fixer"]["input_tokens"], 166298)
        self.assertEqual(report["by_label"]["claude-large"]["input_tokens"], 462124)
        # The budgets, meanwhile, really did reset -- that is the behaviour
        # this change is careful not to touch.
        ledger = self.cli_workspace().read_state()["ledger"]
        self.assertEqual(ledger["attempts"], {})
        self.assertEqual(ledger["total_delegated_runs"], 0)
        self.assertNotEqual(ledger["epoch"], was["epoch"])

    def test_budget_reset_keeps_the_account_and_clears_the_budgets(self):
        """A human resetting the budgets to keep working has not asked to be
        told the work so far was free."""
        self.book.record_usage("implementer", self.measured(input_tokens=351701))
        self.book.consume("implementer")
        before = self.cli_workspace().read_state()["ledger"]["epoch"]

        self.book.reset()

        self.assertEqual(self.book.token_report()["totals"]["input_tokens"], 351701)
        ledger = self.cli_workspace().read_state()["ledger"]
        self.assertEqual(ledger["attempts"], {})
        self.assertEqual(ledger["total_delegated_runs"], 0)
        self.assertNotEqual(ledger["epoch"], before)

    def test_a_run_reported_during_a_reset_is_not_lost(self):
        """Carrying the account made `reset` a read-modify-write, and it was
        the one mutating method holding no lock. A detached worker reporting
        its usage between that read and the write is erased by the write --
        the loss carrying the account exists to prevent, arriving by the other
        door."""
        self.book.record_usage("architect", self.measured(input_tokens=462124))

        def report_usage():
            book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))
            book.record_usage("review_fixer", self.measured(input_tokens=166298))

        worker = threading.Thread(target=report_usage)
        fresh = self.book._fresh

        def fresh_while_a_worker_reports(previous=None):
            ledger = fresh(previous)
            worker.start()
            # Long enough for the report to land if nothing holds it off, and
            # not joined to completion: under the lock it cannot land until
            # `reset` releases, and waiting for it here would be the deadlock.
            worker.join(timeout=0.5)
            return ledger

        self.book._fresh = fresh_while_a_worker_reports
        try:
            self.book.reset()
        finally:
            self.book._fresh = fresh
            worker.join(timeout=30)

        report = self.book.token_report()
        self.assertEqual(report["by_stage"]["architect"]["input_tokens"], 462124)
        self.assertEqual(report["by_stage"]["review_fixer"]["input_tokens"], 166298)

    def test_the_first_ledger_of_all_has_a_blank_account(self):
        """There is nothing on disk to carry from, and that is not an error."""
        self.assertEqual(self.book.load()["tokens"], {"by_stage": {}, "by_label": {}})

    def test_a_ledger_too_old_to_have_an_account_resets_without_raising(self):
        """This reads ledgers written by older versions, where `tokens` is
        absent -- and, if something else mangled it, `None`, not a dict, or a
        dict whose `by_stage` or `by_label` is not one."""
        for tokens in (
            {},
            {"tokens": None},
            {"tokens": []},
            {"tokens": "446430"},
            # A section mangled on its own: healed by the reset until the
            # account started being carried across, and `record_usage` raises
            # on it -- `by_stage.get(stage)` -- if it is carried as found.
            {"tokens": {"by_stage": "oops"}},
            {"tokens": {"by_stage": {}, "by_label": []}},
        ):
            with self.subTest(tokens=tokens):
                workspace = self.cli_workspace()
                state = workspace.read_state()
                old = {"epoch": "old", "attempts": {"test": 1}}
                old.update(tokens)
                state["ledger"] = old
                workspace.write_state(state)
                self.assertEqual(self.book.reset()["tokens"], {"by_stage": {}, "by_label": {}})

    def test_mangling_one_part_of_the_account_does_not_discard_the_rest(self):
        """What is dropped is the mangled part, not the account around it --
        blanking the whole thing on any bad input would be the data loss this
        carries the account to prevent. Both levels: a mangled section beside a
        healthy one, and a mangled entry beside a healthy entry within it."""
        self.book.record_usage("architect", self.measured(input_tokens=462124))
        workspace = self.cli_workspace()
        state = workspace.read_state()
        state["ledger"]["tokens"]["by_stage"]["implementer"] = "oops"
        state["ledger"]["tokens"]["by_label"] = "oops"
        workspace.write_state(state)

        tokens = self.book.reset()["tokens"]
        self.assertEqual(tokens["by_stage"]["architect"]["input_tokens"], 462124)
        self.assertNotIn("implementer", tokens["by_stage"])
        self.assertEqual(tokens["by_label"], {})
        # The stage whose entry was mangled can be recorded again, rather than
        # raising in `_accumulate` on every run from here on.
        self.book.record_usage("implementer", self.measured(input_tokens=351701))
        report = self.book.token_report()
        self.assertEqual(report["by_stage"]["implementer"]["input_tokens"], 351701)
        self.assertEqual(report["by_stage"]["architect"]["input_tokens"], 462124)

    def test_the_carried_account_is_a_copy(self):
        """`_fresh`'s result is written to disk and then mutated by
        `record_usage`; handing it the dict the caller is still holding is how
        one of these bugs starts."""
        self.book.record_usage("architect", self.measured(input_tokens=100))
        old = self.cli_workspace().read_state()["ledger"]
        fresh = self.book._fresh(old)

        fresh["tokens"]["by_stage"]["architect"]["input_tokens"] = 999
        self.assertEqual(old["tokens"]["by_stage"]["architect"]["input_tokens"], 100)


# --------------------------------------------------------------------------- cli


class TestTokensCommand(IsolatedCase):
    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")
        run_cli("config", "set", "implementer.model.family", "large")

    def test_nothing_recorded_says_so(self):
        code, out, _ = run_cli("tokens", "show")
        self.assertEqual(code, 0)
        self.assertIn("No delegated runs", out)

    def test_a_run_lands_in_the_account(self):
        run_cli("run", "implementer", "--prompt", "go")
        code, out, _ = run_cli("tokens", "show")
        self.assertEqual(code, 0)
        self.assertIn("implementer", out)
        payload = json.loads(run_cli("tokens", "show", "--json")[1])
        self.assertEqual(payload["by_stage"]["implementer"]["runs"], 1)
        self.assertGreater(payload["totals"]["billed_tokens"], 0)

    def test_the_cost_column_says_when_it_is_a_floor(self):
        """The tokens can be complete while the money is not, and the two
        caveats are different sentences."""
        book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))
        book.record_usage("review", Usage(source="t", input_tokens=100).to_dict())
        _, out, _ = run_cli("tokens", "show")
        self.assertIn("no cost", out)
        self.assertNotIn("reported no usage", out)

    def test_the_prompt_we_composed_is_counted_separately(self):
        """It is the only part of the input this repository can shorten."""
        run_cli("run", "implementer", "--prompt", "a prompt of a known length")
        payload = json.loads(run_cli("tokens", "show", "--json")[1])
        self.assertEqual(
            payload["by_stage"]["implementer"]["prompt_chars"], len("a prompt of a known length")
        )

    def test_a_failed_run_is_still_recorded(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        self.assertEqual(run_cli("run", "implementer", "--prompt", "go")[0], 1)
        payload = json.loads(run_cli("tokens", "show", "--json")[1])
        self.assertEqual(payload["by_stage"]["implementer"]["runs"], 1)
        # The mock fails without reporting anything, which is exactly the case
        # that must not read as a free run.
        self.assertEqual(payload["by_stage"]["implementer"]["measured_runs"], 0)
        self.assertFalse(payload["complete"])

    def test_an_incomplete_account_says_the_totals_are_a_floor(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        run_cli("run", "implementer", "--prompt", "go")
        _, out, _ = run_cli("tokens", "show")
        self.assertIn("floor", out)

    def test_a_run_that_never_started_a_cli_is_not_in_the_account(self):
        """A failed run is recorded because it spent something. A run that
        never started one spent nothing, and counting it as unreported would
        make the account call itself a floor over a run with nothing to
        report."""
        run_cli("config", "set", "implementer.model.family", "unresolvable")
        self.assertEqual(run_cli("run", "implementer", "--prompt", "go")[0], 2)
        payload = json.loads(run_cli("tokens", "show", "--json")[1])
        self.assertEqual(payload["totals"]["runs"], 0)

    def test_a_real_run_alongside_one_that_never_started_stays_complete(self):
        """The "totals are a floor" caveat must fire on missing data, not on
        a run there was never any data for."""
        run_cli("run", "implementer", "--prompt", "go")
        run_cli("config", "set", "implementer.model.family", "unresolvable")
        run_cli("run", "implementer", "--prompt", "go")
        payload = json.loads(run_cli("tokens", "show", "--json")[1])
        self.assertEqual(payload["totals"]["runs"], 1)
        self.assertTrue(payload["complete"])

    def test_a_measured_zero_prints_zero_and_an_unreported_run_prints_a_dash(self):
        """The two columns the existing `num()` could not render: it prints `-`
        for a zero, which is right for tokens -- a run that billed nothing did
        not happen -- and wrong for a reviewer that opened no files."""
        book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))
        book.record_usage(
            "review",
            Usage(source="claude", input_tokens=10, tool_uses=0, tool_output_chars=0).to_dict(),
        )
        book.record_usage("implementer", Usage(source="codex", input_tokens=10).to_dict())
        _, out, _ = run_cli("tokens", "show")
        self.assertEqual(_row(out, "review")[-2:], ["0", "0"])
        self.assertEqual(_row(out, "implementer")[-2:], ["-", "-"])

    def test_the_footer_says_how_many_runs_the_tool_columns_cover(self):
        book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))
        book.record_usage("review", Usage(source="claude", tool_uses=4, tool_output_chars=80).to_dict())
        book.record_usage("review", Usage(source="codex", input_tokens=10).to_dict())
        _, out, _ = run_cli("tokens", "show")
        self.assertIn("1 of 2 run(s) reported no tool activity", out)
        self.assertIn("not source read", out)

    def test_an_account_that_predates_tool_counting_says_it_cannot_say(self):
        book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))
        book.record_usage("review", Usage(source="claude", tool_uses=4).to_dict())
        state = self.cli_workspace().read_state()
        del state["ledger"]["tokens"]["by_stage"]["review"]["tool_reported_runs"]
        self.cli_workspace().write_state(state)
        _, out, _ = run_cli("tokens", "show")
        self.assertEqual(_row(out, "review")[-2:], ["-", "-"])
        self.assertIn("predate tool counting", out)
        self.assertNotIn("reported no tool activity", out)

    def test_a_later_counted_run_does_not_retire_that_caveat(self):
        """Running the upgraded pipeline once is what made the caveat vanish:
        the run that can say creates the key the caveat was read from, and the
        runs that cannot are then reported as having used no tools."""
        book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))
        book.record_usage("review", Usage(source="claude", input_tokens=10).to_dict())
        state = self.cli_workspace().read_state()
        del state["ledger"]["tokens"]["by_stage"]["review"]["tool_reported_runs"]
        self.cli_workspace().write_state(state)
        book.record_usage("review", Usage(source="claude", tool_uses=4, tool_output_chars=80).to_dict())
        _, out, _ = run_cli("tokens", "show")
        self.assertIn("1 of 2 run(s) predate tool counting", out)
        self.assertNotIn("reported no tool activity", out)

    def test_both_caveats_are_printed_when_a_panel_has_both(self):
        """They stopped overlapping when the silent count began subtracting the
        unknown one, and a panel can hold a legacy stage and a Codex stage at
        once. Saying only the first leaves the other runs unaccounted for."""
        book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))
        book.record_usage("architect", Usage(source="claude", input_tokens=10).to_dict())
        state = self.cli_workspace().read_state()
        del state["ledger"]["tokens"]["by_stage"]["architect"]["tool_reported_runs"]
        self.cli_workspace().write_state(state)
        book.record_usage("review", Usage(source="claude", tool_uses=4, tool_output_chars=80).to_dict())
        book.record_usage("review", Usage(source="codex", input_tokens=10).to_dict())
        _, out, _ = run_cli("tokens", "show")
        self.assertIn("1 of 3 run(s) predate tool counting", out)
        self.assertIn("1 of 3 run(s) reported no tool activity", out)

    def test_a_real_run_through_the_pipeline_reports_its_tools(self):
        run_cli("run", "implementer", "--prompt", "go")
        payload = json.loads(run_cli("tokens", "show", "--json")[1])
        account = payload["by_stage"]["implementer"]
        self.assertEqual(account["tool_reported_runs"], 1)
        self.assertEqual(account["tool_uses"], 0)

    def test_status_reports_the_account_without_acting_on_it(self):
        run_cli("run", "implementer", "--prompt", "go")
        payload = json.loads(run_cli("status", "--json")[1])
        self.assertGreater(payload["tokens"]["totals"]["billed_tokens"], 0)
        # Cost is reported, never enforced: it may not change the verdict.
        self.assertEqual(payload["verdict"], "continue")

    def test_summary_shows_what_the_workflow_spent(self):
        run_cli("run", "implementer", "--prompt", "go")
        code, out, _ = run_cli("summary")
        self.assertEqual(code, 0)
        self.assertIn("Tokens:", out)


@unittest.skipUnless(has_git(), "git is required for review snapshots")
class TestReviewAccounting(IsolatedCase):
    """Reviewers are the most duplicated cost in the pipeline: same diff, once
    per reviewer, once per round. Their accounting is per reviewer for that
    reason -- a per-round total cannot show which reviewer to drop."""

    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write("app.py", "def add(a, b):\n    return a - b\n")

        mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(mock_dir)
        with open(os.path.join(mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING)
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = mock_dir

        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m2", "--role", "security")
        # The duplication is the subject here, so keep both reviewers: at
        # `balanced` a change this small is reduced to one, which is the point
        # of that setting and the end of this measurement.
        run_cli("config", "set", "optimization.level", "quality")

    def test_every_reviewer_is_accounted_for_by_name(self):
        run_cli("review", "snapshot")
        self.assertEqual(run_cli("review", "run")[0], 0)
        payload = json.loads(run_cli("tokens", "show", "--json")[1])
        self.assertEqual(payload["by_stage"]["review"]["runs"], 2)
        self.assertEqual(sorted(payload["by_label"]), ["m1", "m2"])
        for label in ("m1", "m2"):
            self.assertGreater(payload["by_label"][label]["input_tokens"], 0)

    def test_the_diff_is_charged_once_per_reviewer(self):
        """The duplication this measures is the point of measuring it."""
        run_cli("review", "snapshot")
        run_cli("review", "run")
        payload = json.loads(run_cli("tokens", "show", "--json")[1])
        stage = payload["by_stage"]["review"]["prompt_chars"]
        one = payload["by_label"]["m1"]["prompt_chars"]
        self.assertGreater(one, 0)
        # Two reviewers, two near-identical prompts: the stage total is about
        # twice one reviewer's, and that ratio is the cost of independence.
        self.assertGreater(stage, one * 1.5)

    def test_a_reviewer_that_failed_still_appears(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "m2"
        run_cli("review", "snapshot")
        run_cli("review", "run")
        payload = json.loads(run_cli("tokens", "show", "--json")[1])
        self.assertEqual(sorted(payload["by_label"]), ["m1", "m2"])
        self.assertEqual(payload["by_label"]["m2"]["runs"], 1)

    def test_a_second_round_adds_to_the_account_rather_than_replacing_it(self):
        run_cli("review", "snapshot")
        run_cli("review", "run")
        first = json.loads(run_cli("tokens", "show", "--json")[1])["by_stage"]["review"]["runs"]
        self.write("app.py", "def add(a, b):\n    return b - a\n")
        run_cli("review", "snapshot")
        run_cli("review", "run")
        second = json.loads(run_cli("tokens", "show", "--json")[1])["by_stage"]["review"]["runs"]
        self.assertEqual((first, second), (2, 4))


if __name__ == "__main__":
    unittest.main()
