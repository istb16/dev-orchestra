"""The line past which a change is not reviewed at all.

`review.context.max_chars` is the one budget that refuses rather than trims.
Everything here is about the three things that makes true: that nothing runs
and nothing is spent, that the refusal is visible to every reader whose job is
to say what was skipped, and that forcing it records what forcing it actually
did -- which is not always what the defaults suggest.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git

from orchestrator import cli
from orchestrator import optimization as opt_mod
from orchestrator import review as review_mod
from orchestrator import workspace as ws
from orchestrator.providers import mock as mock_mod

#: Small enough to keep the fixtures small, and under `INLINE_LIMIT` below --
#: which is the point of several tests here: over this budget is not the same
#: as over `review.context.inline_chars`, and the code must not confuse the
#: two. The shipped defaults make them equal; these tests do not, because a
#: rule only tested where its two inputs coincide is a rule nobody has tested.
LIMIT = 2000

#: The inline limit those tests configure, above `LIMIT` so that the band
#: between the two exists at all: a body in it is refused by the budget and
#: would still have gone into the prompt whole.
INLINE_LIMIT = 3000

PLAN = """# Plan

## Proposed Change
Add a NOT NULL column to orders.
"""


def run_cli(*argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class MockPanelCase(IsolatedCase):
    """One mock reviewer, answering NO_FINDINGS."""

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        self.workspace = self.cli_workspace()

    def set_limit(self, chars=LIMIT):
        run_cli("config", "set", "review.context.max_chars", str(chars))

    def set_inline_limit(self, chars=INLINE_LIMIT):
        """Lower the inline limit as well, to keep the two apart.

        Shipped, `inline_chars` equals `max_chars` and every refused round
        would also have gone over as a file. Left that way here, the tests
        below could not tell which limit any of these answers came from.
        """
        run_cli("config", "set", "review.context.inline_chars", str(chars))

    def events(self):
        return self.workspace.read_state().get("events") or []


@unittest.skipUnless(has_git(), "git is required")
class TestTheCodeRound(MockPanelCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write("app.py", "def add(a, b):\n    return a - b\n")
        review_mod.create_snapshot(self.workspace)

    def body_of(self, chars):
        """Put a change body of exactly this size where the round will read it.

        Written straight into the frozen snapshot rather than produced from
        git: the size is the whole of what this budget decides on, and a diff
        of an exact length is not something git can be asked for. The same
        handle the coverage tests use from the other end.
        """
        ws.write_text(self.workspace.snapshot_path, "x" * chars)

    def test_a_change_exactly_on_the_limit_is_still_reviewed(self):
        """The boundary is inclusive: the documented number is the largest
        change that still runs, not the first one refused."""
        self.set_limit()
        self.body_of(LIMIT)
        code, out, _ = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertIn("1 successful, 0 failed", out)

    def test_a_null_budget_is_the_default_and_still_refuses(self):
        """`null` is "use the default", never "no limit" -- the no-limit
        reading belongs to a config written before the setting existed."""
        run_cli("config", "set", "review.context.max_chars", "null")
        self.body_of(400_001)
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 3)
        self.assertIn("400,000", err)

    def test_one_character_over_it_is_refused(self):
        self.set_limit()
        self.body_of(LIMIT + 1)
        code, out, err = run_cli("review", "run")
        self.assertEqual(code, 3)
        self.assertIn("2,001 chars", err)
        self.assertIn("2,000", err)
        self.assertIn("review.context.max_chars", err)
        self.assertEqual(out, "")

    def test_the_refusal_names_the_ways_under_the_limit_and_who_forces_it(self):
        self.set_limit()
        self.body_of(LIMIT + 1)
        err = run_cli("review", "run")[2]
        self.assertIn("--base", err)
        self.assertIn("review.exclude", err)
        self.assertIn("split the change", err)
        self.assertIn("this change was not reviewed", err)
        self.assertIn("belongs to a human", err)

    def test_nothing_ran_and_nothing_was_reviewed(self):
        self.set_limit()
        self.body_of(LIMIT + 1)
        run_cli("review", "run")
        self.assertFalse(os.path.isfile(self.workspace.consolidated_json_path))
        self.assertFalse(os.path.isfile(self.workspace.reviewer_report_path("m1")))
        tokens = json.loads(run_cli("tokens", "show", "--json")[1])
        self.assertEqual(tokens["totals"]["runs"], 0)

    def test_the_refusal_is_recorded_the_way_a_report_can_count_it(self):
        """The defect this repeats is issue #42's: a refusal event with no
        `optimization` block is invisible to `summarise_rounds`, which selects
        rounds on that key."""
        self.set_limit()
        self.body_of(LIMIT + 1)
        run_cli("review", "run")
        event = [e for e in self.events() if e.get("stage") == "review"][-1]
        self.assertEqual(event["status"], opt_mod.REFUSED)
        self.assertEqual(event["refused_by"], "context")
        self.assertIsInstance(event.get("optimization"), dict)
        self.assertEqual(event["context"], {"chars": LIMIT + 1, "max_chars": LIMIT})

        report = opt_mod.summarise_rounds(self.events())
        self.assertEqual((report["rounds"], report["ran"], report["refused"]), (1, 0, 1))
        self.assertEqual(report["refused_by"], {"context": 1})

    def test_the_report_splits_the_two_refusals_and_prices_only_the_gate(self):
        self.set_limit()
        self.body_of(LIMIT + 1)
        run_cli("review", "run")
        code, out, _ = run_cli("optimization", "report")
        self.assertEqual(code, 0)
        self.assertIn("refused by", out)
        self.assertIn("context x1", out)
        # The saving is the mean of the rounds that ran, and a round refused
        # for size was never going to be an average round.
        self.assertNotIn("Estimated saving", out)
        self.assertIn("not priced above", out)

    def test_the_final_report_says_what_it_skipped_and_why(self):
        self.set_limit()
        self.body_of(LIMIT + 1)
        run_cli("review", "run")
        out = run_cli("summary")[1]
        self.assertIn("context", out)
        self.assertIn("1 round(s) not run: change over review.context.max_chars", out)

    def test_forcing_it_runs_the_round_and_records_that_it_was_over(self):
        self.set_limit()
        self.body_of(LIMIT + 1)
        code, out, _ = run_cli("review", "run", "--force")
        self.assertEqual(code, 0)
        self.assertIn("1 successful, 0 failed", out)

        data = ws.read_json(self.workspace.consolidated_json_path, {})
        self.assertTrue(data["snapshot"]["over_budget"])
        self.assertEqual([run["over_budget"] for run in data["reviewers"]], [True])
        self.assertIn("over review.context.max_chars", ws.read_text(self.workspace.consolidated_md_path))
        status = json.loads(run_cli("review", "status", "--json")[1])
        self.assertTrue(status["over_budget"])
        self.assertIn("--force", run_cli("review", "status")[1])

    def test_a_round_under_the_limit_is_not_marked_over_budget(self):
        self.set_limit()
        self.body_of(LIMIT)
        run_cli("review", "run")
        data = ws.read_json(self.workspace.consolidated_json_path, {})
        self.assertFalse(data["snapshot"]["over_budget"])
        self.assertNotIn("over review.context.max_chars", ws.read_text(self.workspace.consolidated_md_path))
        self.assertFalse(json.loads(run_cli("review", "status", "--json")[1])["over_budget"])

    def test_forcing_a_body_that_still_fits_inline_is_not_partial(self):
        """The consequence is the inline limit's to decide, not this budget's.
        With `max_chars` set below `review.context.inline_chars` a forced round
        goes into the prompt whole, and promising partial would be a promise
        the code does not keep. The shipped defaults make the two equal, so
        this case exists only under a configuration -- which is exactly why the
        message is computed rather than asserted."""
        self.set_limit()
        self.set_inline_limit()
        self.body_of(LIMIT + 1)
        err = run_cli("review", "run")[2]
        self.assertIn("still fits in the prompt", err)
        self.assertIn("review.context.inline_chars is 3,000", err)
        self.assertNotIn("comes back partial", err)

        code, out, _ = run_cli("review", "run", "--force")
        self.assertEqual(code, 0)
        self.assertIn("1 successful, 0 failed", out)
        self.assertNotIn("partial", out)
        data = ws.read_json(self.workspace.consolidated_json_path, {})
        self.assertEqual([run["status"] for run in data["reviewers"]], ["ok"])

    def test_forcing_a_body_over_the_inline_limit_is_partial_and_says_so_first(self):
        self.set_limit()
        self.set_inline_limit()
        self.body_of(INLINE_LIMIT + 1)
        err = run_cli("review", "run")[2]
        self.assertIn("every reviewer comes back partial", err)
        self.assertIn("review.context.inline_chars (3,000)", err)

        code, out, _ = run_cli("review", "run", "--force")
        self.assertEqual(code, 1)
        self.assertIn("1 partial", out)
        data = ws.read_json(self.workspace.consolidated_json_path, {})
        self.assertEqual([run["status"] for run in data["reviewers"]], ["partial"])
        self.assertEqual([run["inline_chars"] for run in data["reviewers"]], [INLINE_LIMIT])
        self.assertTrue(data["snapshot"]["over_budget"])

    def test_the_shipped_defaults_make_a_forced_round_partial(self):
        """One boundary, with nothing configured: at or under it the round runs
        and is complete, over it it is refused, and forcing it is partial."""
        self.body_of(400_001)
        err = run_cli("review", "run")[2]
        self.assertIn("every reviewer comes back partial", err)
        self.assertIn("review.context.inline_chars (400,000)", err)

    def test_an_inline_limit_above_the_budget_leaves_a_forced_round_complete(self):
        """Allowed, and not a mistake: it says a body is only ever handed over
        as a file on a round somebody forced past `max_chars` -- and not even
        then, if it still fits."""
        self.set_limit()
        self.set_inline_limit(900_000)
        self.body_of(LIMIT + 1)
        self.assertEqual(run_cli("review", "run", "--force")[0], 0)
        data = ws.read_json(self.workspace.consolidated_json_path, {})
        self.assertEqual([run["delivery"] for run in data["reviewers"]], ["inline"])
        self.assertEqual([run["status"] for run in data["reviewers"]], ["ok"])

    def test_status_stops_on_a_refusal_rather_than_reading_an_older_round(self):
        """The hole: a refusal leaves the previous consolidation in place, so
        without this `status` answers `continue` out of a clean review of a
        change nobody has reviewed since."""
        self.set_limit()
        self.body_of(LIMIT)
        run_cli("review", "run")
        self.assertEqual(json.loads(run_cli("status", "--json")[1])["verdict"], "continue")

        self.body_of(LIMIT + 1)
        run_cli("review", "run")
        payload = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertIn("review.context.max_chars", " ".join(payload["reasons"]))
        self.assertEqual(payload["review"]["refused_for_size"], {"chars": LIMIT + 1, "max_chars": LIMIT})
        # The earlier round's report is still on disk, which is exactly why
        # `status` could not be left to read it.
        self.assertTrue(os.path.isfile(self.workspace.consolidated_json_path))

    def test_a_later_round_supersedes_the_refusal(self):
        self.set_limit()
        self.body_of(LIMIT + 1)
        run_cli("review", "run")
        self.body_of(LIMIT)
        run_cli("review", "run")
        payload = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(payload["verdict"], "continue")
        self.assertIsNone(payload["review"]["refused_for_size"])

    def test_a_round_whose_panel_never_started_does_not_supersede_it(self):
        """The narrowed retry after a refusal is the round most likely to fall
        over before it starts -- a reviewer whose model cannot be resolved is
        still recorded as an entry, with `invoked` false. Counting entries
        rather than started reviewers let that round clear the refusal and
        answer `continue` with the change still unread by anyone."""
        self.set_limit()
        self.body_of(LIMIT + 1)
        run_cli("review", "run")

        run_cli("reviewer", "set", "m1", "--model", mock_mod.UNRESOLVABLE_FAMILY)
        self.body_of(LIMIT)
        run_cli("review", "run")
        event = [e for e in self.events() if e.get("stage") == "review"][-1]
        self.assertEqual([run["invoked"] for run in event["reviewers"]], [False])

        payload = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertEqual(payload["review"]["refused_for_size"], {"chars": LIMIT + 1, "max_chars": LIMIT})

    def test_a_dead_round_cleared_by_status_itself_does_not_supersede_it(self):
        """`status` clears stalled stages before it reads the events, and
        clearing one appends an `abandoned` event to the very stage it is
        about to read. Bookkeeping is not a round that reviewed anything, and
        a rule that looked only at the last event let `status` erase the
        refusal it was called to report."""
        from orchestrator import ledger as ledger_mod

        self.set_limit()
        self.body_of(LIMIT + 1)
        run_cli("review", "run")

        book = ledger_mod.Ledger(self.workspace, dict(ledger_mod.DEFAULT_BUDGETS))
        token = book.begin("review", {"iteration": 2, "reviewers": ["m1"]}, deadline=3600)
        ledger = book.load()
        ledger["in_flight"][token]["pid"] = 999_999
        book._write(ledger)

        payload = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(payload["abandoned_stages"], ["review"])
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertEqual(payload["review"]["refused_for_size"], {"chars": LIMIT + 1, "max_chars": LIMIT})

    def test_a_second_refusal_does_not_supersede_the_first(self):
        """Two refused rounds mean nothing was reviewed twice, which is more
        reason to stop than one. Here the gate refuses the round after the
        size refusal, and the test result that caused it is then fixed -- so
        the size refusal is the only thing left saying the change is unread."""
        self.set_limit()
        self.body_of(LIMIT + 1)
        run_cli("review", "run")

        run_cli("state", "record", "test", "failed")
        self.assertEqual(run_cli("review", "run")[0], 3)
        events = [e for e in self.events() if e.get("stage") == "review"]
        self.assertEqual([e.get("refused_by") for e in events], ["context", "gate"])

        run_cli("state", "record", "test", "ok")
        payload = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertEqual(payload["review"]["refused_for_size"], {"chars": LIMIT + 1, "max_chars": LIMIT})

    def test_a_second_size_refusal_is_the_one_reported(self):
        self.set_limit()
        self.body_of(LIMIT + 1)
        run_cli("review", "run")
        self.body_of(LIMIT + 2)
        run_cli("review", "run")
        payload = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(payload["review"]["refused_for_size"], {"chars": LIMIT + 2, "max_chars": LIMIT})

    def test_the_snapshot_command_warns_and_still_writes_it(self):
        """Taking a snapshot spends nothing, and refusing here would leave
        nothing to narrow from."""
        self.set_limit()
        self.write("big.py", "# %s\n" % ("x" * 3000))
        code, out, _ = run_cli("review", "snapshot")
        self.assertEqual(code, 0)
        self.assertIn("WARNING", out)
        self.assertIn("review.context.max_chars", out)
        self.assertTrue(os.path.isfile(self.workspace.snapshot_path))

    def test_a_snapshot_under_the_limit_says_nothing(self):
        self.set_limit()
        code, out, _ = run_cli("review", "snapshot")
        self.assertEqual(code, 0)
        self.assertNotIn("WARNING", out)

    def test_the_json_snapshot_carries_the_same_warning_as_numbers(self):
        """`--json` is the form a wrapper reads, and a warning only a human
        sees is not one the wrapper can act on before `review run` exits 3."""
        self.set_limit()
        self.write("big.py", "# %s\n" % ("x" * 3000))
        code, out, _ = run_cli("review", "snapshot", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["over_context"])
        self.assertEqual(payload["max_chars"], LIMIT)
        self.assertGreater(payload["change_chars"], LIMIT)
        # The command's own output, not a second copy of the limit in `.ai/`:
        # the snapshot's metadata describes the frozen diff and nothing else.
        self.assertNotIn("over_context", ws.read_json(self.workspace.snapshot_meta_path, {}))

    def test_a_json_snapshot_under_the_limit_says_it_is_under(self):
        self.set_limit()
        payload = json.loads(run_cli("review", "snapshot", "--json")[1])
        self.assertFalse(payload["over_context"])
        self.assertEqual(payload["max_chars"], LIMIT)


class TestTheDesignRound(MockPanelCase):
    """The change body of a design round is the plan *and* the request."""

    def setUp(self):
        super().setUp()
        self.design = self.workspace.design_review()

    def write_plan(self, text=PLAN):
        ws.write_text(self.workspace.plan_path, text)

    def write_request(self, chars):
        path = os.path.join(self.workspace.execution_dir, "design-request.md")
        ws.write_text(path, "r" * chars)

    def test_the_request_is_measured_with_the_plan(self):
        """A plan that fits on its own is still refused once the request it
        answers is counted -- the request goes into every prompt whole, and is
        not a rounding error."""
        self.set_limit()
        plan = PLAN + "p" * (LIMIT - len(PLAN) - 100)
        self.write_plan(plan)
        self.assertLess(len(plan), LIMIT)
        self.write_request(200)

        code, _, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 3)
        self.assertIn("review.context.max_chars", err)
        self.assertIn("the request it answers", err)
        # `review snapshot` has no --design form, so the code-side remedies
        # cannot be aimed at a plan and are not offered.
        self.assertNotIn("--base", err)
        self.assertNotIn("review.exclude", err)

    def test_the_plan_alone_would_have_run(self):
        self.set_limit()
        self.write_plan(PLAN + "p" * (LIMIT - len(PLAN) - 100))
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)

    def test_a_refusal_leaves_the_previous_round_frozen_where_it_was(self):
        """Refusing after the freeze would re-freeze the plan and strand every
        report of the round before it against a snapshot that no longer
        exists -- discarding the triage the refusal asks to be reported."""
        self.write_plan()
        self.write_request(50)
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)
        frozen = ws.read_text(self.design.snapshot_path)
        consolidated = ws.read_json(self.design.consolidated_json_path, {})

        self.set_limit(10)
        code, _, _ = run_cli("review", "run", "--design")
        self.assertEqual(code, 3)
        self.assertEqual(ws.read_text(self.design.snapshot_path), frozen)
        self.assertEqual(ws.read_json(self.design.consolidated_json_path, {}), consolidated)
        self.assertTrue(os.path.isfile(self.design.reviewer_report_path("m1")))

    def test_a_refused_design_round_is_counted(self):
        """The design tally counts rounds that ran, so a refused one would be
        invisible there for the same reason it was in the code report."""
        self.set_limit(10)
        self.write_plan()
        run_cli("review", "run", "--design")
        report = opt_mod.summarise_rounds(self.events())
        self.assertEqual(report["design_rounds"], 0)
        self.assertEqual(report["design_refused"], 1)

        out = run_cli("optimization", "report")[1]
        self.assertIn("Design review rounds refused for size: 1", out)
        self.assertIn("1 design round(s) not run", run_cli("summary")[1])

    def test_status_stops_on_a_refused_design_round(self):
        self.set_limit(10)
        self.write_plan()
        run_cli("review", "run", "--design")
        payload = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(payload["verdict"], "stop-and-report")
        self.assertIn("design review round was refused", " ".join(payload["reasons"]))
        self.assertEqual(payload["design_review"]["refused_for_size"]["max_chars"], 10)

    def test_forcing_it_runs_the_round_and_records_that_it_was_over(self):
        self.set_limit(10)
        self.write_plan()
        code, out, _ = run_cli("review", "run", "--design", "--force")
        self.assertEqual(code, 0)
        self.assertIn("1 successful, 0 failed", out)
        data = ws.read_json(self.design.consolidated_json_path, {})
        self.assertTrue(data["snapshot"]["over_budget"])

    def test_the_report_prints_the_size_the_limit_measured(self):
        """Two sizes, and the `Change:` line names a limit, so it has to be
        the limit's: the plan *and* the request. `coverage.change_chars` is
        the body a reviewer was handed, which here is the plan alone, and
        printing that one beside `over review.context.max_chars` would put a
        number in the record that the refusal never used."""
        self.set_limit(10)
        self.write_plan()
        self.write_request(500)
        self.assertEqual(run_cli("review", "run", "--design", "--force")[0], 0)

        measured = len(PLAN) + 500
        data = ws.read_json(self.design.consolidated_json_path, {})
        self.assertEqual(data["snapshot"]["budget_chars"], measured)
        self.assertEqual(data["coverage"]["change_chars"], len(PLAN))
        self.assertIn(
            "- Change: {:,} chars, over review.context.max_chars".format(measured),
            ws.read_text(self.design.consolidated_md_path),
        )

    def test_a_plan_that_fits_inline_is_not_promised_partial(self):
        """The pair decides the refusal; the plan alone decides delivery, and
        only the plan is ever handed over as a file. A plan under the inline
        limit whose request takes the pair over the budget goes into every
        prompt whole, so promising partial would be a promise not kept."""
        self.set_limit(INLINE_LIMIT + 1000)
        self.set_inline_limit()
        plan = PLAN + "p" * (INLINE_LIMIT - len(PLAN))
        self.assertEqual(len(plan), INLINE_LIMIT)
        self.write_plan(plan)
        self.write_request(2000)

        code, _, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 3)
        self.assertIn("still fits in the prompt", err)
        self.assertNotIn("comes back partial", err)

        code, out, _ = run_cli("review", "run", "--design", "--force")
        self.assertEqual(code, 0)
        self.assertNotIn("partial", out)
        data = ws.read_json(self.design.consolidated_json_path, {})
        self.assertEqual([run["status"] for run in data["reviewers"]], ["ok"])
        self.assertEqual([run["delivery"] for run in data["reviewers"]], ["inline"])


if __name__ == "__main__":
    unittest.main()
