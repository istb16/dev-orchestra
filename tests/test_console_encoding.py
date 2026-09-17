"""What happens when the console cannot encode what a delegated agent wrote.

Reported from real use on Japanese Windows, where the console is cp932: a
delegated run finished, every edit was applied, and then printing the summary
raised `UnicodeEncodeError` on a single em dash. Claude's prose is full of
them.

Losing the summary was the visible half. The worse half was underneath: the
output was printed *before* the ledger was written, so the crash took the
token accounting with it and left the stage marked in flight -- and the next
command then reported that finished run as abandoned. A success read as a
stall.

Two guarantees, one test class each: the console tolerates what it cannot
encode, and nothing printed can decide whether the run happened.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase

from orchestrator import cli
from orchestrator import ledger as ledger_mod

EM_DASH = "—"


def cp932_stream():
    """A console like the one that reported this, as strict as Python makes it."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp932", errors="strict", newline="")


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestTheConsoleTolerance(unittest.TestCase):
    def test_a_strict_stream_is_relaxed(self):
        stream = cp932_stream()
        saved, sys.stdout = sys.stdout, stream
        try:
            cli.tolerate_console_encoding()
            self.assertEqual(stream.errors, "backslashreplace")
        finally:
            sys.stdout = saved

    def test_the_character_survives_as_an_escape_rather_than_a_crash(self):
        """`backslashreplace`, not `replace`: the output is read by the
        orchestrating agent too, and `?` throws away which character it was."""
        stream = cp932_stream()
        saved, sys.stdout = sys.stdout, stream
        try:
            cli.tolerate_console_encoding()
            cli._out("Fixed the parser %s cleanly" % EM_DASH)
        finally:
            sys.stdout = saved
        stream.flush()
        written = stream.buffer.getvalue().decode("cp932")
        self.assertIn("\\u2014", written)
        self.assertIn("Fixed the parser", written)

    def test_an_error_handler_someone_chose_is_left_alone(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp932", errors="ignore")
        saved, sys.stdout = sys.stdout, stream
        try:
            cli.tolerate_console_encoding()
            self.assertEqual(stream.errors, "ignore")
        finally:
            sys.stdout = saved

    def test_a_stream_that_cannot_be_reconfigured_is_not_an_error(self):
        """Every test in this suite replaces stdout with a StringIO."""
        saved, sys.stdout = sys.stdout, io.StringIO()
        try:
            cli.tolerate_console_encoding()  # must not raise
        finally:
            sys.stdout = saved

    def test_utf8_is_left_as_it_is(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
        saved, sys.stdout = sys.stdout, stream
        try:
            cli.tolerate_console_encoding()
            cli._out(EM_DASH)
        finally:
            sys.stdout = saved
        stream.flush()
        self.assertIn(EM_DASH, stream.buffer.getvalue().decode("utf-8"))

    def test_the_command_entry_point_applies_it(self):
        """The guarantee is worthless if nothing calls it."""
        stream = cp932_stream()
        saved, sys.stdout = sys.stdout, stream
        try:
            cli.main(["config", "path"])
        finally:
            sys.stdout = saved
        self.assertEqual(stream.errors, "backslashreplace")


class TestTheBooksAreClosedBeforeAnythingIsPrinted(IsolatedCase):
    """The ordering guarantee, tested by making printing fail.

    A console this suite can rely on does not exist -- CI is UTF-8 -- so the
    failure is injected instead. What matters is not which exception it is but
    that the accounting survives one.
    """

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")
        os.environ["DEV_ORCHESTRA_MOCK_RESPONSE"] = "Fixed the parser %s cleanly" % EM_DASH
        self.original = cli._out

        def explode(text=""):
            if EM_DASH in text:
                raise UnicodeEncodeError("cp932", text, 0, 1, "illegal multibyte sequence")
            return self.original(text)

        cli._out = explode
        self.addCleanup(setattr, cli, "_out", self.original)

    def ledger(self):
        return ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))

    def test_the_run_still_raises_rather_than_pretending_it_printed(self):
        """The crash is not swallowed: a command that could not report is not
        a command that succeeded. It just no longer costs the books."""
        with self.assertRaises(UnicodeEncodeError):
            run_cli("run", "implementer", "--prompt", "go")

    def test_the_tokens_are_recorded_anyway(self):
        with self.assertRaises(UnicodeEncodeError):
            run_cli("run", "implementer", "--prompt", "go")
        report = self.ledger().token_report()
        self.assertEqual(report["totals"]["runs"], 1)

    def test_the_stage_is_not_left_in_flight(self):
        """This is what made the next command call a finished run abandoned."""
        with self.assertRaises(UnicodeEncodeError):
            run_cli("run", "implementer", "--prompt", "go")
        self.assertFalse(self.ledger().summary()["in_flight"])

    def test_the_run_is_recorded_as_ok_rather_than_abandoned(self):
        with self.assertRaises(UnicodeEncodeError):
            run_cli("run", "implementer", "--prompt", "go")
        events = self.cli_workspace().read_state().get("events") or []
        self.assertEqual([event["status"] for event in events], ["ok"])

    def test_and_with_the_console_relaxed_it_simply_works(self):
        """The two halves together, which is what the user sees."""
        cli._out = self.original
        code, out, _ = run_cli("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 0)
        self.assertIn("Fixed the parser", out)
        payload = json.loads(run_cli("tokens", "show", "--json")[1])
        self.assertEqual(payload["totals"]["runs"], 1)


if __name__ == "__main__":
    unittest.main()
