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
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git

from orchestrator import cli
from orchestrator import ledger as ledger_mod
from orchestrator import workspace as ws
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
        self.book = ledger_mod.Ledger(ws.Workspace(self.project).ensure(), dict(ledger_mod.DEFAULT_BUDGETS))

    def measured(self, **fields):
        return Usage(source="test", **fields).to_dict()

    def test_runs_accumulate_per_stage(self):
        self.book.record_usage("architect", self.measured(input_tokens=100, output_tokens=10))
        self.book.record_usage("architect", self.measured(input_tokens=50, output_tokens=5))
        account = self.book.token_report()["by_stage"]["architect"]
        self.assertEqual(account["input_tokens"], 150)
        self.assertEqual(account["output_tokens"], 15)
        self.assertEqual(account["runs"], 2)

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
        fresh = ledger_mod.Ledger(ws.Workspace(self.project), dict(ledger_mod.DEFAULT_BUDGETS))
        self.assertEqual(fresh.token_report()["by_stage"]["test"]["input_tokens"], 7)

    def test_a_reset_starts_a_new_account(self):
        self.book.record_usage("test", self.measured(input_tokens=7))
        self.book.reset()
        self.assertEqual(self.book.token_report()["totals"]["runs"], 0)

    def test_recording_a_cost_never_consumes_an_attempt(self):
        """Accounting must not be able to exhaust a budget."""
        before = self.book.remaining("architect")
        self.book.record_usage("architect", self.measured(input_tokens=1))
        self.assertEqual(self.book.remaining("architect"), before)


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
