"""A prompt piped in is read as UTF-8, whatever the console's code page (#284).

``sys.stdin.read()`` decoded with the console's encoding, so on a Japanese
Windows -- cp932 -- a UTF-8 prompt reached the provider as mojibake, about
two characters of it for every one that was sent.
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase

from orchestrator import cli, cli_run

PROMPT = "日本語のプロンプトです。変更を確かめてください。"


def console_stdin(data: bytes, encoding: str = "cp932") -> io.TextIOWrapper:
    """A piped stdin as Python hands it over on a console of ``encoding``."""
    return io.TextIOWrapper(io.BytesIO(data), encoding=encoding)


class TestReadingStdin(unittest.TestCase):
    def read(self, data: bytes, encoding: str = "cp932") -> str:
        saved = sys.stdin
        sys.stdin = console_stdin(data, encoding)
        try:
            return cli_run._read_stdin()
        finally:
            sys.stdin = saved

    def test_the_console_code_page_is_what_garbled_it(self):
        """What the rest of this class is worth, stated as the failure."""
        try:
            garbled = console_stdin(PROMPT.encode("utf-8")).read()
        except UnicodeDecodeError:
            return
        self.assertNotEqual(garbled, PROMPT)

    def test_utf8_is_read_as_utf8_on_a_cp932_console(self):
        self.assertEqual(self.read(PROMPT.encode("utf-8")), PROMPT)

    def test_a_bom_is_dropped_and_line_endings_read_as_text(self):
        data = b"\xef\xbb\xbf" + "一行目\r\n二行目\r三行目\n".encode("utf-8")
        self.assertEqual(self.read(data), "一行目\n二行目\n三行目\n")

    def test_bytes_in_the_console_encoding_are_still_read(self):
        """``type`` of a file saved as cp932 pipes cp932; it is not UTF-8."""
        self.assertEqual(self.read(PROMPT.encode("cp932")), PROMPT)

    def test_one_stray_byte_does_not_turn_a_utf8_prompt_into_cp932(self):
        """Reading it all in cp932 for one bad byte would garble every character."""
        self.assertEqual(self.read(PROMPT.encode("utf-8") + b"\x81"), PROMPT + "�")
        self.assertEqual(self.read(b"\xff" + PROMPT.encode("utf-8")), "�" + PROMPT)

    def test_a_short_cp932_prompt_is_still_read_as_cp932(self):
        self.assertEqual(self.read("abc あ".encode("cp932")), "abc あ")

    def test_bytes_neither_can_read_are_replaced_rather_than_raised(self):
        text = self.read(b"ok \xff\xfe end", encoding="utf-8")
        self.assertTrue(text.startswith("ok "))
        self.assertTrue(text.endswith(" end"))
        self.assertIn("�", text)

    def test_a_text_only_stdin_is_read_as_it_is(self):
        saved = sys.stdin
        sys.stdin = io.StringIO(PROMPT)
        try:
            self.assertEqual(cli_run._read_stdin(), PROMPT)
        finally:
            sys.stdin = saved


class TestThePromptThatIsDelegated(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.run_cli("config", "setup", "--defaults")
        self.run_cli("config", "set", "implementer.provider", "mock")
        self.started = []
        from orchestrator.providers import mock as mock_mod

        original = mock_mod.MockProvider.run

        def record(provider, *args, **kwargs):
            self.started.append(args[0] if args else kwargs.get("prompt"))
            return original(provider, *args, **kwargs)

        setattr(mock_mod.MockProvider, "run", record)
        self.addCleanup(setattr, mock_mod.MockProvider, "run", original)

    @staticmethod
    def run_cli(*argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def piped(self, *argv):
        saved = sys.stdin
        sys.stdin = console_stdin(PROMPT.encode("utf-8"))
        try:
            return self.run_cli(*argv)
        finally:
            sys.stdin = saved

    def test_prompt_file_dash_delivers_the_prompt_as_written(self):
        code, _, err = self.piped("run", "implementer", "--prompt-file", "-")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.started, [PROMPT])

    def test_a_bare_pipe_delivers_the_prompt_as_written(self):
        code, _, err = self.piped("run", "implementer")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.started, [PROMPT])

    def test_a_prompt_file_with_a_bom_loses_the_bom(self):
        path = os.path.join(self.project, "brief.md")
        with open(path, "wb") as handle:
            handle.write(b"\xef\xbb\xbf" + PROMPT.encode("utf-8"))
        code, _, err = self.run_cli("run", "implementer", "--prompt-file", path)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.started, [PROMPT])


if __name__ == "__main__":
    unittest.main()
