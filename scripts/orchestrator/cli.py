"""``dev-orchestra`` command line interface.

Only the parts that benefit from being deterministic and reusable live here:
configuration, environment diagnosis, provider invocation, and the mechanical
half of the review pipeline (snapshot, fan-out, parse, dedupe). Judgement calls
-- which stages to run, whether a finding is real -- stay with the orchestrating
agent, which drives these commands.
"""

from __future__ import annotations

import argparse
import os
from typing import Dict, Optional, Sequence, Tuple

from . import claude_hooks, hosts
from . import config as config_mod
from . import review as review_mod
from . import workflow as workflow_mod
from .cli_common import _err, tolerate_console_encoding
from .cli_config import (
    _add_config_parsers,
    _add_doctor_parser,
    _add_model_parsers,
    _add_reviewer_parsers,
)
from .cli_hooks import _add_hooks_parsers
from .cli_review import _add_review_parsers
from .cli_run import _add_run_parser
from .cli_state import (
    _add_budget_parsers,
    _add_jobs_parsers,
    _add_optimization_parsers,
    _add_progress_parsers,
    _add_tokens_parsers,
)
from .cli_units import _add_units_parsers
from .cli_workflow import (
    _add_design_parsers,
    _add_state_parsers,
    _add_status_parser,
    _add_summary_parser,
    _add_workflow_parsers,
)

__version__ = "1.2.1"


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dev-orchestra",
        description="Configuration, diagnostics and review plumbing for the"
        " AI Development Orchestrator skill.",
    )
    parser.add_argument("--version", action="version", version="dev-orchestra %s" % __version__)
    parser.add_argument("--cwd", default=None, help="operate as if run from this directory")
    parser.add_argument(
        "--workflow",
        default="",
        help="the workflow these artifacts belong to (default: this session's)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # config -----------------------------------------------------------------
    _add_config_parsers(subparsers)

    # model ------------------------------------------------------------------
    _add_model_parsers(subparsers)

    # reviewer ---------------------------------------------------------------
    _add_reviewer_parsers(subparsers)

    # doctor -----------------------------------------------------------------
    _add_doctor_parser(subparsers)

    # hooks ------------------------------------------------------------------
    _add_hooks_parsers(subparsers)

    # run --------------------------------------------------------------------
    _add_run_parser(subparsers)

    # review -----------------------------------------------------------------
    _add_review_parsers(subparsers)

    # design -----------------------------------------------------------------
    _add_design_parsers(subparsers)

    # state ------------------------------------------------------------------
    _add_state_parsers(subparsers)

    _add_jobs_parsers(subparsers)
    _add_units_parsers(subparsers)
    _add_budget_parsers(subparsers)
    _add_tokens_parsers(subparsers)
    _add_optimization_parsers(subparsers)
    _add_progress_parsers(subparsers)
    _add_workflow_parsers(subparsers)
    _add_status_parser(subparsers)
    _add_summary_parser(subparsers)

    return parser


def leaf_commands(
    parser: Optional[argparse.ArgumentParser] = None,
) -> Dict[Tuple[str, ...], argparse.ArgumentParser]:
    """Every runnable command of ``parser`` (``build_parser()`` by default),
    as its words -- ``("review", "run")`` -- with the parser that takes its
    flags. The one place that walks argparse's sub-commands, so that the
    manifest check and the tests agree on what every command is."""
    found: Dict[Tuple[str, ...], argparse.ArgumentParser] = {}

    def walk(current: argparse.ArgumentParser, words: Tuple[str, ...]) -> None:
        groups = [action for action in current._actions if isinstance(action, argparse._SubParsersAction)]
        if not groups:
            found[words] = current
            return
        for group in groups:
            for name, child in group.choices.items():
                walk(child, (*words, name))

    walk(parser if parser is not None else build_parser(), ())
    return found


def main(argv: Optional[Sequence[str]] = None) -> int:
    tolerate_console_encoding()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.cwd:
        # Resolve before chdir and keep the absolute form: commands also use
        # args.cwd as a search-start path, and a relative value would otherwise
        # be applied a second time against the directory we just moved into.
        args.cwd = os.path.abspath(args.cwd)
        os.chdir(args.cwd)
    if args.command != "hooks":
        # Keeps an installed hook relay on this checkout; never changes the outcome.
        # Not for `hooks`: its dry runs and status write nothing, and report what is there.
        try:
            claude_hooks.refresh(hosts.PLUGIN_ROOT, os.environ)
        except Exception:
            pass
    try:
        return int(args.func(args) or 0)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    except review_mod.ReviewError as exc:
        _err(str(exc))
        return 2
    except workflow_mod.WorkflowError as exc:
        _err(str(exc))
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        _err("interrupted")
        return 130
