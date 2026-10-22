"""The shape of the ``dev-orchestra`` command's parser."""

from __future__ import annotations

import argparse
import io
import unittest
from contextlib import redirect_stderr
from typing import ClassVar

from helpers import IsolatedCase, run_cli

from orchestrator import activity as activity_mod
from orchestrator import cli, providers


def minimal_argv(path, leaf):
    """The shortest argv that ``leaf`` accepts: one value per required argument."""
    argv = list(path)
    for action in leaf._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        value = str(next(iter(action.choices))) if action.choices else "x"
        if not action.option_strings:
            if action.nargs in (None, "+"):
                argv.append(value)
        elif action.required:
            argv.extend([action.option_strings[0], value])
    return argv


class TestParserShape(IsolatedCase):
    """What ``build_parser`` registers, pinned at parse level so a command
    that drops out of the tree, or loses its handler, fails here."""

    TOP_LEVEL: ClassVar[set] = {
        "config",
        "model",
        "reviewer",
        "doctor",
        "hooks",
        "run",
        "review",
        "design",
        "state",
        "jobs",
        "units",
        "budget",
        "tokens",
        "optimization",
        "progress",
        "workflow",
        "status",
        "summary",
    }

    #: The commands that take ``--json``, as registered today.
    JSON_COMMANDS: ClassVar[set] = {
        "budget show",
        "config show",
        "config suggest-roles",
        "config validate",
        "design approve",
        "design questions show",
        "doctor",
        "hooks status",
        "jobs list",
        "jobs show",
        "jobs wait",
        "model list",
        "optimization report",
        "progress record",
        "review consolidate",
        "review run",
        "review show",
        "review snapshot",
        "review status",
        "reviewer list",
        "run",
        "state show",
        "status",
        "summary",
        "tokens show",
        "units merge",
        "units status",
        "workflow list",
        "workflow show",
    }

    def setUp(self):
        super().setUp()
        self.parser = cli.build_parser()

    def parse(self, *argv):
        return self.parser.parse_args(list(argv))

    def test_json_flag(self):
        found = set()
        for path, leaf in cli.leaf_commands(self.parser).items():
            if "--json" not in leaf._option_string_actions:
                continue
            name = " ".join(path)
            found.add(name)
            with self.subTest(command=name):
                argv = minimal_argv(path, leaf)
                self.assertIs(self.parse(*argv).json, False)
                self.assertIs(self.parse(*argv, "--json").json, True)
        self.assertEqual(found, self.JSON_COMMANDS)

    def assert_usage_error(self, *argv):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.parse(*argv)
        self.assertEqual(caught.exception.code, 2, argv)

    def test_top_level_commands(self):
        self.assertEqual({words[0] for words in cli.leaf_commands(self.parser)}, self.TOP_LEVEL)

    def test_every_leaf_parses_to_its_handler(self):
        leaves = list(cli.leaf_commands(self.parser).items())
        self.assertTrue(
            {
                ("workflow", "use"),
                ("workflow", "remove"),
                ("budget", "consume"),
                ("budget", "reset"),
                ("progress", "record"),
                ("state", "record"),
                ("review", "triage"),
                ("review", "fix-brief"),
                ("review", "consolidate"),
                ("config", "suggest-roles"),
                ("config", "setup"),
            }
            <= {path for path, _ in leaves}
        )
        for path, leaf in leaves:
            with self.subTest(command=" ".join(path)):
                func = leaf.get_default("func")
                self.assertIsNotNone(func)
                self.assertEqual(func.__name__, "cmd_" + "_".join(path).replace("-", "_"))
                self.assertIs(self.parse(*minimal_argv(path, leaf)).func, func)

    def test_activity_flags_on_jobs_show_and_wait(self):
        # `jobs wait` takes a cursor per job (`ID=N`), so its --since is a list
        # of (job id or None, N); `jobs show` keeps the single number.
        for sub, unset, at_max in (
            ("show", 0, activity_mod.SINCE_MAX),
            ("wait", [], [(None, activity_mod.SINCE_MAX)]),
        ):
            with self.subTest(sub=sub):
                args = self.parse("jobs", sub, "J")
                self.assertEqual((args.since, args.activity), (unset, 10))
                args = self.parse(
                    "jobs",
                    sub,
                    "J",
                    "--since",
                    str(activity_mod.SINCE_MAX),
                    "--activity",
                    str(activity_mod.LATEST_MAX),
                )
                self.assertEqual((args.since, args.activity), (at_max, activity_mod.LATEST_MAX))
                args = self.parse("jobs", sub, "J", "--since", "0", "--activity", "0")
                self.assertEqual((args.since, args.activity), (0 if sub == "show" else [(None, 0)], 0))
                for flag, bad in (
                    ("--since", "-1"),
                    ("--since", str(activity_mod.SINCE_MAX + 1)),
                    ("--since", "abc"),
                    ("--activity", "-1"),
                    ("--activity", str(activity_mod.LATEST_MAX + 1)),
                    ("--activity", "abc"),
                ):
                    self.assert_usage_error("jobs", sub, "J", "%s=%s" % (flag, bad))

    def test_scope_defaults(self):
        self.assertEqual(self.parse("config", "setup").scope, "global")
        self.assertEqual(self.parse("config", "show").scope, "effective")
        self.assertEqual(self.parse("config", "show", "--scope", "effective").scope, "effective")
        self.assert_usage_error("config", "setup", "--scope", "effective")
        for argv in (
            ("config", "set", "a.b", "c"),
            ("config", "reset"),
            ("reviewer", "add", "--provider", providers.available_providers()[0]),
            ("reviewer", "remove", "1"),
            ("reviewer", "set", "1"),
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(self.parse(*argv).scope)

    def test_config_prune_is_gone(self):
        self.assert_usage_error("config", "prune")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            run_cli("config", "prune")
        self.assertEqual(caught.exception.code, 2)

    def test_design_flag_on_review_subcommands(self):
        for argv in (
            ("review", "run"),
            ("review", "consolidate"),
            ("review", "show"),
            ("review", "triage", "F1", "--status", "accepted"),
            ("review", "fix-brief"),
            ("review", "status"),
        ):
            with self.subTest(argv=argv):
                self.assertIs(self.parse(*argv).design, False)
                self.assertIs(self.parse(*argv, "--design").design, True)
        self.assert_usage_error("review", "snapshot", "--design")


if __name__ == "__main__":
    unittest.main()
