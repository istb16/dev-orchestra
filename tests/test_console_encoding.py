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

Then it was reported again, with the tolerance already in place. The guard
only relaxed a `strict` stream, and Windows never hands one over: CPython
gives `sys.stdout` the `surrogateescape` handler there, which rescues lone
surrogates and nothing else. An em dash walked through the guard into exactly
the same crash, and this file passed the whole time because it built its
console with `strict`. The console here now matches the real one.

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
#: What `backslashreplace` leaves behind, spelled without an escape of its own.
ESCAPED_EM_DASH = chr(92) + "u2014"
#: Prose cp932 cannot carry, past the em dash: an accent and an emoji, whose
#: `backslashreplace` forms (`\xe9`, `\U0001f600`) are not JSON escapes.
UNENCODABLE = "Fixed the café parser %s cleanly \U0001f600" % EM_DASH


def cp932_stream(errors="surrogateescape"):
    """A console like the one that reported this.

    `surrogateescape` by default because that is what CPython gives
    `sys.stdout` on Windows. Not `strict`, which is what this file used to
    assume -- and why it passed while the console it was written for kept
    crashing.
    """
    return io.TextIOWrapper(io.BytesIO(), encoding="cp932", errors=errors, newline="")


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestTheConsoleTolerance(unittest.TestCase):
    def test_the_untouched_console_is_the_one_that_crashes(self):
        """What the rest of this class is worth, stated as the failure."""
        stream = cp932_stream()
        with self.assertRaises(UnicodeEncodeError):
            stream.write(EM_DASH)
            stream.flush()

    def test_the_windows_default_is_relaxed(self):
        """The regression: `surrogateescape` is nobody's deliberate choice."""
        stream = cp932_stream()
        saved, sys.stdout = sys.stdout, stream
        try:
            cli.tolerate_console_encoding()
            self.assertEqual(stream.errors, "backslashreplace")
        finally:
            sys.stdout = saved

    def test_a_strict_stream_is_relaxed(self):
        stream = cp932_stream(errors="strict")
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
        self.assertIn(ESCAPED_EM_DASH, written)
        self.assertIn("Fixed the parser", written)

    def test_stderr_is_relaxed_too(self):
        """A failing run's last word goes through `_err`, and config paths and
        provider messages carry non-ASCII just as readily as a summary does."""
        stream = cp932_stream()
        saved, sys.stderr = sys.stderr, stream
        try:
            cli.tolerate_console_encoding()
            cli._err("config is broken %s check it" % EM_DASH)
        finally:
            sys.stderr = saved
        stream.flush()
        self.assertIn("config is broken", stream.buffer.getvalue().decode("cp932"))

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
        stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="surrogateescape")
        saved, sys.stdout = sys.stdout, stream
        try:
            cli.tolerate_console_encoding()
            self.assertEqual(stream.errors, "surrogateescape")
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


class TestAStreamThatCannotBeReconfigured(unittest.TestCase):
    """Reconfiguring covers every stream it can reach. `_out` covers the rest
    -- a wrapper someone else installed, a pipe handed to us already open --
    where the alternative is losing a whole report to one character in it."""

    class Wrapper:
        encoding = "cp932"

        def __init__(self):
            self.written = []

        def write(self, text):
            text.encode(self.encoding)  # strict, and no `reconfigure` to relax
            self.written.append(text)

    def write_through(self, attr, emit):
        stream = self.Wrapper()
        saved = getattr(sys, attr)
        setattr(sys, attr, stream)
        try:
            cli.tolerate_console_encoding()  # reaches nothing here
            emit("Fixed the parser %s cleanly" % EM_DASH)
        finally:
            setattr(sys, attr, saved)
        return "".join(stream.written)

    def test_stdout_keeps_the_message(self):
        written = self.write_through("stdout", cli._out)
        self.assertIn("Fixed the parser", written)
        self.assertIn("cleanly", written)
        self.assertIn(ESCAPED_EM_DASH, written)

    def test_stderr_keeps_the_message(self):
        self.assertIn("Fixed the parser", self.write_through("stderr", cli._err))


class TestEveryOutputPathOnACp932Console(IsolatedCase):
    """`cmd_run` is where it was reported, but every command prints through
    `_out`, and non-ASCII reaches them from delegated agents and config files
    alike. These drive `cli.main` end to end against the real console."""

    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.provider", "mock")
        os.environ["DEV_ORCHESTRA_MOCK_RESPONSE"] = "Fixed the parser %s cleanly" % EM_DASH

    def on_a_cp932_console(self, *argv):
        stream = cp932_stream()
        saved, sys.stdout = sys.stdout, stream
        try:
            code = cli.main(list(argv))
        finally:
            sys.stdout = saved
        stream.flush()
        return code, stream.buffer.getvalue().decode("cp932")

    def test_a_delegated_run_prints_instead_of_crashing(self):
        """The report, verbatim: the run succeeded and printing it did not."""
        code, written = self.on_a_cp932_console("run", "implementer", "--prompt", "go")
        self.assertEqual(code, 0)
        self.assertIn("Fixed the parser", written)
        self.assertIn("cleanly", written)

    def test_the_character_is_named_rather_than_dropped(self):
        _, written = self.on_a_cp932_console("run", "implementer", "--prompt", "go")
        self.assertIn(ESCAPED_EM_DASH, written)

    def test_the_json_form_survives_it_too(self):
        """`--json` promises machine-readable output, and degrading a character
        must not cost it that: `backslashreplace` spells `é` as `\\xe9`, which
        JSON does not define, so the output would parse as nothing. The em dash
        alone hides this -- `\\u2014` happens to be a JSON escape too."""
        run_cli("state", "record", "implementer", "ok", "--detail", "note=%s" % UNENCODABLE)
        code, written = self.on_a_cp932_console("state", "show", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(written)
        self.assertEqual(payload["events"][0]["note"], UNENCODABLE)

    def test_the_summary_survives_it_too(self):
        """The end-of-run report, which is what the crash actually cost."""
        run_cli("run", "implementer", "--prompt", "go")
        code, written = self.on_a_cp932_console("summary")
        self.assertEqual(code, 0)
        self.assertIn("implementer", written)
        self.assertIn("OK", written)
        self.assertIn("Models:", written)


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
