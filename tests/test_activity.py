"""Activity lines: what may be shown of a running CLI, and the file they live in."""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import threading
import unittest
from typing import Any, List, cast
from unittest import mock

from helpers import IsolatedCase, present

from orchestrator import activity

PWSH = '"C:\\Program Files\\PowerShell\\7\\pwsh.exe"'
SET_CONTENT = (
    "Set-Content -LiteralPath 'hello.txt' -Value 'hi' -NoNewline; Get-Content -LiteralPath 'hello.txt'"
)


class TestClip(unittest.TestCase):
    def test_escape_sequences_are_removed(self):
        self.assertEqual(activity.clip("\x1b[31mRead\x1b[0m a.py"), "Read a.py")
        self.assertEqual(activity.clip("\x1b]0;title\x07Read a.py"), "Read a.py")
        self.assertEqual(activity.clip("\x1b]8;;https://x\x1b\\Read\x1b]8;;\x1b\\ a.py"), "Read a.py")

    def test_every_spelling_of_an_escape(self):
        cases = {
            # An OSC with no terminator runs to the end of the text.
            "Read \x1b]0;never ends": "Read",
            # The 8-bit C1 spellings of CSI, OSC and ST.
            "\x9b31mRead": "Read",
            "\x9d0;t\x9cRead": "Read",
            "\x9d0;t\x07Read": "Read",
            # A bare two-character escape.
            "\x1bMRead": "Read",
            "\x1b\\Read": "Read",
        }
        for text, expected in cases.items():
            self.assertEqual(activity.clip(text), expected, repr(text))

    def test_an_escape_does_not_shield_a_secret(self):
        line = activity.clip("Read \x1b[31msk-ant-abcdefghijklmnopqrstuvwxyz\x1b[0m")
        self.assertNotIn("abcdefghijklmnop", line)
        self.assertIn("[redacted]", line)
        self.assertNotIn("\x1b", line)

    def test_control_and_bidi_characters_are_removed(self):
        self.assertEqual(activity.clip("Re\x1bad\x85 a.py"), "Read")
        self.assertEqual(activity.clip("Read a\u202e\u2066\u200e.py\x7f\x9f"), "Read a.py")
        self.assertEqual(activity.clip("Read\ra.py"), "Read")

    def test_only_the_first_line_is_kept(self):
        self.assertEqual(activity.clip("Read a.py\nsecret plan"), "Read a.py")
        self.assertEqual(activity.clip("Read a.py\u2028secret plan"), "Read a.py")

    def test_credentials_are_redacted(self):
        line = activity.clip("Read sk-ant-abcdefghijklmnopqrstuvwxyz")
        self.assertNotIn("abcdefghijklmnop", line)
        self.assertIn("[redacted]", line)

    def test_a_long_line_is_cut_to_the_limit(self):
        line = activity.clip("x" * 500)
        self.assertEqual(len(line), activity.LINE_LIMIT)
        self.assertTrue(line.endswith(activity.ELLIPSIS))

    def test_anything_but_a_string_is_nothing(self):
        for value in (None, 3, ["Read"], {"line": "Read"}):
            self.assertEqual(activity.clip(value), "")


class TestShowPath(unittest.TestCase):
    def test_posix(self):
        cases = {
            "/w/repo/src/a.py": "src/a.py",
            "src/a.py": "src/a.py",
            "/etc/passwd": "<outside>/passwd",
            "/w/repo/../other/b.py": "<outside>/b.py",
            "/w/repository/b.py": "<outside>/b.py",
            "": "",
        }
        for path, expected in cases.items():
            self.assertEqual(activity.show_path(path, "/w/repo", posixpath), expected, path)
        self.assertEqual(activity.show_path(None, "/w/repo", posixpath), "")

    def test_windows(self):
        cases = {
            "C:\\w\\repo\\src\\a.py": "src/a.py",
            "c:/W/Repo/src/a.py": "src/a.py",
            "C:\\Data\\me\\.ssh\\id_rsa": "<outside>/id_rsa",
            "D:\\x": "<outside>/x",
        }
        for path, expected in cases.items():
            self.assertEqual(activity.show_path(path, "C:\\w\\repo", ntpath), expected, path)


class TestShowCommand(unittest.TestCase):
    def test_program_and_subcommand_only(self):
        cases = {
            "git status": "Bash: git status",
            "git status\nrm -rf /": "Bash: git status",
            "python -c 'print(1)'": "Bash: python",
            "FOO=secret BAR=x python -c 'x'": "Bash: python",
            '"/usr/bin/git" log --oneline': "Bash: git log",
            "C:\\tools\\git.exe status": "Bash: git status",
            "npm Test": "Bash: npm",
            "npm test": "Bash: npm test",
            "grep don't file": "Bash: grep",
            "": "Bash",
            "A=1": "Bash",
            '""': "Bash",
            "''": "Bash",
        }
        for command, expected in cases.items():
            self.assertEqual(activity.show_command(command), expected, command)
        self.assertEqual(activity.show_command(None), "Bash")

    def test_a_second_word_only_for_a_program_that_takes_a_subcommand(self):
        cases = {
            "echo password": "Bash: echo",
            "grep password file": "Bash: grep",
            "python script": "Bash: python",
            "cat secret": "Bash: cat",
            "make release": "Bash: make release",
            "docker build .": "Bash: docker build",
            "Git.exe status": "Bash: Git status",
        }
        for command, expected in cases.items():
            self.assertEqual(activity.show_command(command), expected, command)

    def test_assignments_before_the_program_are_never_shown(self):
        cases = {
            "$env:DB_PASSWORD='hunter2xyz';": "Bash",
            "$env:DB_PASSWORD='hunter2xyz'; git status": "Bash: git status",
            "$env:DB_PASSWORD = 'two words'; git status": "Bash: git status",
            "$token='abc' git status": "Bash",
            "FOO='two words' npm test": "Bash: npm test",
            'FOO="never closed npm test': "Bash",
            PWSH + " -Command \"$env:DB_PASSWORD='x'; git status\"": "Bash: git status",
            "bash -lc 'FOO=1 BAR=2 cargo build'": "Bash: cargo build",
        }
        for command, expected in cases.items():
            shown = activity.show_command(command)
            self.assertEqual(shown, expected, command)
            for secret in ("hunter2xyz", "two", "words", "abc", "never"):
                self.assertNotIn(secret, shown, command)

    def test_a_program_name_that_is_not_a_plain_word_is_bash_alone(self):
        for command in ("$(cat key) status", "`whoami`", "a;b status", "{x} y", "%SECRET% go"):
            self.assertEqual(activity.show_command(command), "Bash", command)

    def test_wrapped_commands_as_codex_runs_them(self):
        cases = {
            PWSH + " -Command 'git status'": "Bash: git status",
            PWSH + " -NoProfile -Command 'Get-Content -LiteralPath README.md'": "Bash: Get-Content",
            PWSH + ' -Command "%s"' % SET_CONTENT: "Bash: Set-Content",
            "/bin/bash -lc 'git diff --stat'": "Bash: git diff",
            "bash -lc 'FOO=1 npm test'": "Bash: npm test",
            "sh -c 'ls -la'": "Bash: ls",
            "/usr/bin/zsh -lc 'make'": "Bash: make",
            "cmd.exe /c dir": "Bash: dir",
            "powershell.exe -Command Get-ChildItem": "Bash: Get-ChildItem",
        }
        for command, expected in cases.items():
            self.assertEqual(activity.show_command(command), expected, command)

    def test_an_unreadable_wrapped_command_is_bash_alone(self):
        self.assertEqual(activity.show_command("/bin/bash -lc 'git status"), "Bash")
        self.assertEqual(activity.show_command('"C:\\pwsh.exe -Command x'), "Bash")


class TestToolLine(unittest.TestCase):
    cwd = os.path.abspath(os.sep + "w")

    def line(self, name, arguments):
        return activity.tool_line(name, arguments, self.cwd)

    def test_paths(self):
        inside = os.path.join(self.cwd, "a.py")
        for tool in ("Read", "Edit", "Write"):
            self.assertEqual(self.line(tool, {"file_path": inside, "content": "secret"}), tool + " a.py")
        self.assertEqual(self.line("NotebookEdit", {"notebook_path": inside}), "NotebookEdit a.py")
        self.assertEqual(self.line("Read", {}), "Read")

    def test_free_text_is_never_shown(self):
        src = os.path.join(self.cwd, "src")
        self.assertEqual(self.line("Grep", {"pattern": "password", "path": src}), "Grep src")
        self.assertEqual(self.line("Grep", {"pattern": "password"}), "Grep")
        self.assertEqual(self.line("Task", {"description": "steal keys", "prompt": "..."}), "Task")
        self.assertEqual(self.line("Agent", {"prompt": "..."}), "Agent")
        self.assertEqual(self.line("TodoWrite", {"todos": [{"content": "secret plan"}]}), "TodoWrite")
        self.assertEqual(self.line("ExitPlanMode", {"plan": "the whole plan"}), "ExitPlanMode")
        for shell in ("Bash", "PowerShell"):
            shown = self.line(shell, {"command": "git status", "description": "secret"})
            self.assertEqual(shown, "Bash: git status")

    def test_input_that_is_not_a_mapping_gives_the_name(self):
        for arguments in ("a.py", ["a.py"], None):
            self.assertEqual(self.line("Read", arguments), "Read")
            self.assertEqual(self.line("Bash", arguments), "Bash")

    def test_glob_shows_only_a_pattern_inside_the_project(self):
        src = os.path.join(self.cwd, "src")
        elsewhere = os.path.abspath(os.sep + "elsewhere")
        cases = [
            ({"pattern": "**/*.py"}, "Glob **/*.py"),
            ({"pattern": "src/**/*.py", "path": src}, "Glob src/**/*.py in src"),
            ({"pattern": "/" + "home/u/secrets/*"}, "Glob"),
            ({"pattern": "C:/" + "Users/me/.ssh/*"}, "Glob"),
            ({"pattern": "C:\\Users\\me\\.ssh\\*"}, "Glob"),
            ({"pattern": "\\\\server\\share\\*"}, "Glob"),
            ({"pattern": "~/.ssh/*"}, "Glob"),
            ({"pattern": "../other/*"}, "Glob"),
            ({"pattern": "src/../../x/*"}, "Glob"),
            ({"pattern": "/" + "home/u/secrets/*", "path": src}, "Glob src"),
            ({"pattern": "*.py", "path": elsewhere}, "Glob *.py in <outside>/elsewhere"),
            ({"pattern": "  "}, "Glob"),
            ({"pattern": 3}, "Glob"),
        ]
        for arguments, expected in cases:
            self.assertEqual(self.line("Glob", arguments), expected, arguments)

    def test_webfetch_shows_scheme_host_and_port_only(self):
        cases = {
            "https://u:p@h/x?q#f": "WebFetch https://h",
            "http://Host:8080/a": "WebFetch http://host:8080",
            "https://api.telegram.org/bot123456:ABC-secret/getMe": "WebFetch https://api.telegram.org",
            "https://hooks.slack.com/services/T0/B0/secretpart": "WebFetch https://hooks.slack.com",
            "HTTPS://H/x": "WebFetch https://h",
            "http://[::1]:8080/x": "WebFetch http://[::1]:8080",
            "ftp://h/x?q": "WebFetch ftp://h",
            "http://h:abc/": "WebFetch",
            "https:///x": "WebFetch",
            "//h/x": "WebFetch",
            "not a url": "WebFetch",
        }
        for url, expected in cases.items():
            self.assertEqual(self.line("WebFetch", {"url": url, "prompt": "x"}), expected, url)
        self.assertEqual(self.line("WebFetch", {"url": 3}), "WebFetch")

    def test_mcp(self):
        self.assertEqual(self.line("mcp__github__create_issue", {"title": "x"}), "github.create_issue")
        self.assertEqual(self.line("mcp__broken", {}), "mcp__broken")
        self.assertEqual(self.line(None, {}), "")


class TestSink(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.tmp, "job.activity")

    def entries(self, path=None):
        with open(path or self.path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def test_every_line_is_clipped_before_it_is_written_or_echoed(self):
        echoed: List[str] = []
        sink = activity.Sink(self.path, echo=echoed.append)
        token_line = "Read sk-ant-abcdefghijklmnopqrstuvwxyz"
        sink.add(activity.Activity([token_line, "\x1b[1mBash: git\x1b[0m", "\n"], 7))
        entries = self.entries()
        self.assertEqual([entry["line"] for entry in entries], echoed)
        self.assertEqual(echoed[1], "Bash: git")
        self.assertNotIn("abcdefghijklmnop", echoed[0])
        self.assertEqual([entry["n"] for entry in entries], [1, 2])
        self.assertEqual(entries[0]["tokens"], 7)

    def test_context_tokens_only_when_a_count(self):
        sink = activity.Sink()
        for bad in (-1, True, "12", 1.5):
            sink.add(activity.Activity([], cast(Any, bad)))
        self.assertIsNone(sink.context_tokens)
        sink.add(activity.Activity([], 12))
        self.assertEqual(sink.context_tokens, 12)

    def test_a_context_with_no_tool_line_is_still_written(self):
        sink = activity.Sink(self.path)
        sink.add(activity.Activity(["Read a.py"], 10))
        sink.add(activity.Activity([], 10))
        sink.add(activity.Activity([], 25))
        sink.add(activity.Activity([], 25))
        entries = self.entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1]["tokens"], 25)
        self.assertNotIn("n", entries[1])
        report = present(activity.read(self.path))
        self.assertEqual(report["context_tokens"], 25)
        self.assertEqual(report["count"], 1)
        self.assertEqual([line["line"] for line in report["lines"]], ["Read a.py"])
        self.assertFalse(report["dropped"])

    def test_junk_from_a_hook_is_ignored(self):
        sink = activity.Sink(self.path)
        sink.add(None)
        sink.add(activity.Activity(cast(Any, [3, None, "Read a.py"]), None))
        self.assertEqual([entry["line"] for entry in self.entries()], ["Read a.py"])

    def test_rotation_keeps_n_going(self):
        sink = activity.Sink(self.path)
        sink.add(activity.Activity(["Read %d" % i for i in range(1, 503)], None))
        old = self.entries(self.path + ".1")
        self.assertEqual([entry["n"] for entry in old], list(range(1, 501)))
        self.assertEqual([entry["n"] for entry in self.entries()], [501, 502])

    def test_a_failed_rotation_drops_entries_and_read_says_so(self):
        sink = activity.Sink(self.path)
        sink.add(activity.Activity(["Read %d" % i for i in range(1, 501)], None))
        with mock.patch("os.replace", side_effect=PermissionError("in use")):
            sink.add(activity.Activity(["Read 501", "Read 502"], None))
        self.assertEqual(len(self.entries()), 500)
        self.assertFalse(os.path.exists(self.path + ".1"))
        sink.add(activity.Activity(["Read 503"], None))
        self.assertEqual([entry["n"] for entry in self.entries()], [503])
        report = present(activity.read(self.path, since=499))
        self.assertEqual(report["count"], 503)
        self.assertTrue(report["dropped"])
        self.assertEqual([line["n"] for line in report["lines"]], [500, 503])

    def test_the_echo_stops_at_its_limit(self):
        echoed: List[str] = []
        sink = activity.Sink(echo=echoed.append)
        with mock.patch.object(activity, "ECHO_LIMIT", 2):
            sink.add(activity.Activity(["a", "b", "c", "d"], None))
            sink.say("done: ok")
        self.assertEqual(echoed, ["a", "b", activity.ECHO_CUT, "done: ok"])

    def test_recording_restores_the_previous_sink(self):
        outer, inner = activity.Sink(), activity.Sink()
        self.assertIsNone(activity.current())
        with activity.recording(outer):
            with self.assertRaises(RuntimeError), activity.recording(inner):
                self.assertIs(activity.current(), inner)
                raise RuntimeError
            self.assertIs(activity.current(), outer)
            seen = []
            thread = threading.Thread(target=lambda: seen.append(activity.current()))
            thread.start()
            thread.join()
            self.assertEqual(seen, [None])
        self.assertIsNone(activity.current())


class TestRead(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.tmp, "job.activity")

    def write_lines(self, name, lines):
        with open(name, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(lines))

    def entry(self, n, line="Read a.py", tokens=None):
        return json.dumps({"n": n, "s": float(n), "line": line, "tokens": tokens}) + "\n"

    def test_no_file_is_none(self):
        self.assertIsNone(activity.read(self.path))

    def test_untrusted_lines_are_skipped(self):
        lines = [
            "garbage\n",
            "[1, 2]\n",
            json.dumps({"n": "2", "line": "x"}) + "\n",
            json.dumps({"n": True, "line": "x"}) + "\n",
            json.dumps({"n": 0, "line": "x"}) + "\n",
            json.dumps({"n": 3, "line": 5}) + "\n",
            self.entry(1, "Read \x1b[31ma.py\u202e\nsecond line", 40),
            self.entry(4),
            '{"n": 5, "line": "Read b',
        ]
        self.write_lines(self.path, lines)
        report = present(activity.read(self.path))
        self.assertEqual([line["n"] for line in report["lines"]], [1, 4])
        self.assertEqual(report["lines"][0]["line"], "Read a.py")
        self.assertEqual(report["count"], 4)
        self.assertEqual(report["context_tokens"], 40)
        self.assertTrue(report["dropped"])

    def test_the_latest_context_wins_whichever_entry_carries_it(self):
        tokens_only = json.dumps({"s": 9.0, "tokens": 900}) + "\n"
        self.write_lines(self.path + ".1", [self.entry(1, tokens=100), tokens_only])
        self.write_lines(self.path, [self.entry(2), json.dumps({"tokens": True}) + "\n"])
        report = present(activity.read(self.path))
        self.assertEqual(report["context_tokens"], 900)
        self.assertEqual([line["n"] for line in report["lines"]], [1, 2])
        self.write_lines(self.path, [self.entry(2, tokens=1200)])
        self.assertEqual(present(activity.read(self.path))["context_tokens"], 1200)

    def test_only_the_rotated_file(self):
        self.write_lines(self.path + ".1", [self.entry(1), self.entry(2)])
        report = present(activity.read(self.path))
        self.assertEqual(report["count"], 2)
        self.assertFalse(report["dropped"])

    def test_since_latest_and_their_bounds(self):
        self.write_lines(self.path + ".1", [self.entry(n) for n in range(1, 4)])
        self.write_lines(self.path, [self.entry(n) for n in range(3, 8)])
        report = present(activity.read(self.path, since=2, latest=2))
        self.assertEqual([line["n"] for line in report["lines"]], [6, 7])
        self.assertEqual(report["omitted"], 3)
        self.assertFalse(report["dropped"])
        self.assertEqual(present(activity.read(self.path, since=-5, latest=1000))["omitted"], 0)
        self.assertEqual(present(activity.read(self.path, since=7))["lines"], [])
        self.assertEqual(present(activity.read(self.path, latest=0))["omitted"], 7)

    def test_since_below_the_oldest_kept_entry_is_dropped(self):
        self.write_lines(self.path, [self.entry(n) for n in range(501, 504)])
        self.assertTrue(present(activity.read(self.path, since=10))["dropped"])
        self.assertFalse(present(activity.read(self.path, since=500))["dropped"])


if __name__ == "__main__":
    unittest.main()
