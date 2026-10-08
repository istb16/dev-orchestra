"""Deadlines and poll intervals are numbers of seconds above zero (#272).

Every one of them used to pass whatever it was given: ``0`` or a negative
stalled a run at once, ``true`` was one second, a word ended ``run`` on a
traceback with its in-flight entry left open, and ``jobs wait --poll -1``
was a traceback of its own.
"""

from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Tuple

from helpers import IsolatedCase

from orchestrator import cli, execution
from orchestrator import config as config_mod
from orchestrator import ledger as ledger_mod
from orchestrator.providers.base import Provider
from orchestrator.providers.claude import ClaudeProvider


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def parse(*argv: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(list(argv))


def refusal(*argv: str) -> Tuple[object, str]:
    """The exit code and stderr of parsing ``argv``; (None, "") if it was accepted."""
    err = io.StringIO()
    with redirect_stderr(err):
        try:
            cli.build_parser().parse_args(list(argv))
        except SystemExit as exc:
            return exc.code, err.getvalue()
    return None, ""


class TestTheArgumentsAreChecked(IsolatedCase):
    def assert_refused(self, *argv):
        code, err = refusal(*argv)
        self.assertEqual(code, 2, "%s was accepted" % (argv,))
        self.assertIn("seconds", err)

    def test_run_and_review_deadlines_refuse_what_is_not_a_positive_number(self):
        for command in (["run", "implementer"], ["review", "run"]):
            for value in ("0", "-5", "abc", "1.5", "nan"):
                with self.subTest(command=command, timeout=value):
                    self.assert_refused(*command, "--timeout", value)
            for value in ("0", "-1", "abc", "nan", "inf"):
                with self.subTest(command=command, idle=value):
                    self.assert_refused(*command, "--idle-timeout", value)

    def test_run_and_review_deadlines_take_a_positive_number(self):
        for command in (["run", "implementer"], ["review", "run"]):
            args = parse(*command, "--timeout", "90", "--idle-timeout", "2.5")
            self.assertEqual((args.timeout, args.idle_timeout), (90, 2.5))

    def test_jobs_wait_refuses_a_poll_that_cannot_be_slept(self):
        for value in ("0", "-1", "abc", "inf", "nan"):
            with self.subTest(poll=value):
                self.assert_refused("jobs", "wait", "j1", "--poll", value)
        for value in ("-1", "inf", "nan", "abc"):
            with self.subTest(timeout=value):
                self.assert_refused("jobs", "wait", "j1", "--timeout", value)

    def test_a_number_too_large_to_wait_for_is_a_usage_error(self):
        """A 400-digit deadline was an OverflowError, and 1e22 one in ``time.sleep``."""
        huge = "9" * 400
        for argv in (
            ("run", "implementer", "--timeout", huge),
            ("run", "implementer", "--idle-timeout", huge),
            ("review", "run", "--timeout", "1000000001"),
            ("jobs", "wait", "j1", "--timeout", "1e22"),
            ("jobs", "wait", "j1", "--poll", "1e22"),
        ):
            with self.subTest(argv=argv[:-1]):
                code, err = refusal(*argv)
                self.assertEqual(code, 2)
                self.assertIn(str(execution.MAX_SECONDS), err)
                self.assertNotIn(huge, err)
        self.assertEqual(parse("run", "implementer", "--timeout", str(execution.MAX_SECONDS)).timeout, 10**9)

    def test_jobs_wait_may_look_once(self):
        args = parse("jobs", "wait", "j1", "--timeout", "0", "--poll", "0.25")
        self.assertEqual((args.timeout, args.poll), (0.0, 0.25))


class TestClaudeIdleTimeoutOption(IsolatedCase):
    def test_only_a_positive_number_or_null_is_valid(self):
        provider = ClaudeProvider()
        for value in ("abc", 0, -5, True, False, float("nan"), float("inf"), [1], 10**400, 1e22):
            with self.subTest(value=value):
                problems = provider.validate_options({"idle_timeout": value})
                self.assertTrue(any("options.idle_timeout" in p for p in problems), problems)
        for value in (None, 1, 120, 0.5):
            with self.subTest(value=value):
                self.assertEqual(provider.validate_options({"idle_timeout": value}), [])

    def test_config_validate_reports_it(self):
        data = config_mod.default_config()
        data["implementer"] = {
            "provider": "claude",
            "model": {"family": "default"},
            "options": {"idle_timeout": "abc"},
        }
        problems = config_mod.validate(data)
        self.assertTrue(any("options.idle_timeout" in p for p in problems), problems)


class Streaming(Provider):
    """A user adapter that streams progress and takes ``idle_timeout``."""

    name = "streamer"
    executable = "streamer"
    streams_progress = True
    option_keys = ("args", "idle_timeout")


class TestAnyAdapterTakingIdleTimeout(IsolatedCase):
    """The check is the base adapter's, not Claude's alone."""

    def test_a_user_adapter_gets_the_same_check(self):
        provider = Streaming()
        for value in ("abc", 0, -1, True, 10**400):
            with self.subTest(value=value):
                problems = provider.validate_options({"idle_timeout": value})
                self.assertTrue(any("options.idle_timeout" in p for p in problems), problems)
        self.assertEqual(provider.validate_options({"idle_timeout": 30}), [])

    def test_an_unvalidated_value_is_ignored_rather_than_raised(self):
        provider = Streaming()
        for value in ("abc", 0, -1, True, 10**400):
            with self.subTest(value=value):
                self.assertEqual(provider.idle_timeout({"idle_timeout": value}, 300.0), 300.0)
        self.assertEqual(provider.idle_timeout({"idle_timeout": 12}, 300.0), 12.0)


class TestConfiguredDeadlinesAreChecked(IsolatedCase):
    def test_review_and_run_deadlines_must_be_positive_integers(self):
        cases = [
            (("review", "timeout_seconds"), "review.timeout_seconds"),
            (("review", "idle_timeout_seconds"), "review.idle_timeout_seconds"),
            (("run", "timeout_seconds", "architect"), "run.timeout_seconds.architect"),
        ]
        for path, key in cases:
            for value in (0, -1, True, "abc", 1.5, 10**9 + 1, 10**400):
                with self.subTest(key=key, value=value):
                    data = config_mod.default_config()
                    node = data
                    for part in path[:-1]:
                        node = node[part]
                    node[path[-1]] = value
                    problems = config_mod.validate(data)
                    self.assertTrue(any(p.startswith(key) for p in problems), problems)


class TestAnUnexpectedErrorClosesTheRun(IsolatedCase):
    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")
        from orchestrator.providers import mock as mock_mod

        original = mock_mod.MockProvider.run

        def broken(provider, *args, **kwargs):
            raise ValueError("could not convert string to float: 'abc'")

        setattr(mock_mod.MockProvider, "run", broken)
        self.addCleanup(setattr, mock_mod.MockProvider, "run", original)

    def test_an_interrupt_closes_the_entry_too(self):
        from orchestrator.providers import mock as mock_mod

        def interrupted(provider, *args, **kwargs):
            raise KeyboardInterrupt

        original = mock_mod.MockProvider.run
        setattr(mock_mod.MockProvider, "run", interrupted)
        self.addCleanup(setattr, mock_mod.MockProvider, "run", original)
        code, _, err = run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 130)
        self.assertIn("interrupted", err)
        book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))
        self.assertEqual(book.summary()["in_flight"], {})
        events = json.loads(run_cli("state", "show", "--json")[1])["events"]
        self.assertEqual(events[-1]["status"], "failed")
        self.assertIn("KeyboardInterrupt", events[-1]["error"])

    def test_the_in_flight_entry_is_ended_and_the_error_still_surfaces(self):
        with self.assertRaises(ValueError):
            run_cli("run", "implementer", "--prompt", "go")
        book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))
        self.assertEqual(book.summary()["in_flight"], {})
        events = json.loads(run_cli("state", "show", "--json")[1])["events"]
        self.assertEqual(events[-1]["stage"], "implementer")
        self.assertEqual(events[-1]["status"], "failed")
        self.assertIn("ValueError", events[-1]["error"])


if __name__ == "__main__":
    unittest.main()
