"""What every command's parser shares: argument types and the common flags."""

from __future__ import annotations

import argparse
from typing import Callable

from . import clocks


def _bounded_int(low: int, high: int) -> Callable[[str], int]:
    """An argparse type: a whole number from ``low`` to ``high``. Anything
    else is a usage error (exit 2)."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError("%r is not a whole number" % text) from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError("%d is not from %d to %d" % (value, low, high))
        return value

    return parse


def _seconds(*, whole: bool = False, allow_zero: bool = False) -> Callable[[str], float]:
    """An argparse type: a finite number of seconds, above zero unless
    ``allow_zero``, at most ``clocks.MAX_SECONDS``; a whole one when
    ``whole``. Anything else -- a word, a negative, ``nan``, ``inf``, a
    number too large to wait for -- is a usage error (exit 2), where it used
    to reach a deadline that fired at once, never fired, or a traceback."""

    def parse(text: str) -> float:
        try:
            value: float = int(text) if whole else float(text)
        except ValueError:
            kind = "a whole number of seconds" if whole else "a number of seconds"
            raise argparse.ArgumentTypeError("%r is not %s" % (text, kind)) from None
        if not clocks.is_seconds(value, allow_zero=allow_zero):
            rule = "from 0 to %d" if allow_zero else "above 0 and at most %d"
            shown = text if len(text) <= 40 else text[:37] + "..."
            raise argparse.ArgumentTypeError("%s seconds: must be %s" % (shown, rule % clocks.MAX_SECONDS))
        return value

    return parse


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")


def _add_command_group(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], name: str, help: str
) -> argparse._SubParsersAction[argparse.ArgumentParser]:
    return subparsers.add_parser(name, help=help).add_subparsers(dest="subcommand", required=True)
