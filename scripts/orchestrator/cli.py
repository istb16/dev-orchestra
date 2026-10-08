"""``dev-orchestra`` command line interface.

Only the parts that benefit from being deterministic and reusable live here:
configuration, environment diagnosis, provider invocation, and the mechanical
half of the review pipeline (snapshot, fan-out, parse, dedupe). Judgement calls
-- which stages to run, whether a finding is real -- stay with the orchestrating
agent, which drives these commands.
"""

from __future__ import annotations

import argparse
import codecs
import copy
import hashlib
import json
import math
import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import activity as activity_mod
from . import approval as approval_mod
from . import claude_hooks, hosts, miniyaml
from . import config as config_mod
from . import context as context_mod
from . import doctor as doctor_mod
from . import jobs as jobs_mod
from . import ledger as ledger_mod
from . import optimization as opt_mod
from . import presets as presets_mod
from . import review as review_mod
from . import wizard as wizard_mod
from . import workflow as workflow_mod
from . import workspace as ws
from .cli_common import (
    _TOLERANT_ERRORS,
    _UNSET,
    DEFAULT_MODES,
    _container,
    _emit_json,
    _encodes_everything,
    _err,
    _in_workflow,
    _layer_base,
    _layer_path,
    _ledger,
    _load_or_die,
    _out,
    _read_layer,
    _refuse_if_exhausted,
    _resolve_scope,
    _review_workspace,
    _seed_list,
    _workspace,
    _write,
    _wrote_plan,
    tolerate_console_encoding,
)
from .cli_config import (
    _both_conditions,
    _describe_referenced_provider,
    _prune_value,
    _redacted_reviewer,
    _render_layer,
    _warn_unresolvable,
    cmd_config_path,
    cmd_config_prune,
    cmd_config_reset,
    cmd_config_set,
    cmd_config_setup,
    cmd_config_show,
    cmd_config_suggest_roles,
    cmd_config_validate,
    cmd_doctor,
    cmd_model_list,
    cmd_reviewer_add,
    cmd_reviewer_list,
    cmd_reviewer_remove,
    cmd_reviewer_set,
)
from .cli_hooks import cmd_hooks_install, cmd_hooks_status, cmd_hooks_uninstall
from .cli_review import (
    _adoption_line,
    _approved_as_recorded,
    _code_final_pass,
    _condition_excluded,
    _condition_paths,
    _configured_inline_chars,
    _design_final_pass,
    _design_request_path,
    _final_pass_advice,
    _iteration,
    _lineage,
    _measurement_block,
    _measurement_inputs,
    _measurement_rerun,
    _merge_runs,
    _no_review_yet,
    _panel_summary,
    _ran_since_last_round,
    _refuse_if_over_context,
    _refuse_if_runtime_spent,
    _reviewed_something,
    _reviewer_line,
    _risk_paths,
    _run_design_review,
    _same_snapshot_report,
    _surrounding_snapshot_lines,
    _surrounding_status_lines,
    _triage_changed_since_build,
    _triage_record,
    cmd_review_consolidate,
    cmd_review_fix_brief,
    cmd_review_run,
    cmd_review_show,
    cmd_review_snapshot,
    cmd_review_status,
    cmd_review_triage,
)
from .cli_run import (
    _RESUME_NO_BUDGET,
    _RESUME_NOT_SUPPORTED,
    _RESUME_REASONS,
    _RESUME_REJECTED,
    _announce_resume,
    _answered,
    _both_paths,
    _detached_argv,
    _read_prompt,
    _read_prompt_file,
    _refuse_run,
    _refuse_unless_approved,
    _Refused,
    _remove_if_present,
    _require_prompt,
    _resume_candidate,
    _resume_refusal,
    _save_output,
    cmd_run,
)
from .cli_state import (
    _TOKEN_ROW,
    _rounds_recorded,
    _scorecard_inputs,
    _token_row,
    _workflows_recorded,
    cmd_budget_consume,
    cmd_budget_reset,
    cmd_budget_show,
    cmd_jobs_cancel,
    cmd_jobs_list,
    cmd_jobs_show,
    cmd_jobs_wait,
    cmd_optimization_report,
    cmd_progress_record,
    cmd_tokens_show,
)
from .cli_workflow import (
    _REFUSAL_CAUSE,
    _approval_advice,
    _approval_line,
    _context_refusal,
    _refusal_reason,
    _workflow_warning,
    cmd_design_approve,
    cmd_state_record,
    cmd_state_show,
    cmd_status,
    cmd_summary,
    cmd_workflow_list,
    cmd_workflow_remove,
    cmd_workflow_show,
    cmd_workflow_use,
)
from .optimization_render import (
    _OPT_ROW,
    _SCORECARD_ALONE,
    _SCORECARD_BIAS,
    _SCORECARD_EFFORT,
    _SCORECARD_FLOORS,
    _SCORECARD_OUTCOMES,
    _SCORECARD_PAIRS,
    _SCORECARD_TOTAL,
    _context_per_run_row,
    _context_row,
    _counts,
    _figure,
    _pair_exclusions,
    _paired_rows,
    _panel_names,
    _revision_rows,
    _runs_row,
    _scorecard_cost_row,
    _scorecard_counts,
    _scorecard_per_accepted,
    _scorecard_rates,
    _scorecard_rows,
    _scorecard_spend,
    _tools_row,
    _usd,
)
from .providers import (
    MODE_IMPLEMENT,
    MODE_PLAN,
    MODE_REVIEW,
    MODES,
    READ_ONLY_MODES,
    REFUSED_ENFORCEMENT,
    ModelResolutionError,
    UnknownProviderError,
    adapter_failure,
    available_providers,
    describe_exception,
    describe_origin,
    get_provider,
    origin_payload,
    provider_origin,
    redact,
)

__version__ = "0.22.0"


# --------------------------------------------------------------------------- parser


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
    ``allow_zero``; a whole one when ``whole``. Anything else -- a word, a
    negative, ``nan``, ``inf`` -- is a usage error (exit 2), where it used to
    reach a deadline that fired at once, never fired, or a traceback."""

    def parse(text: str) -> float:
        try:
            value: float = int(text) if whole else float(text)
        except ValueError:
            kind = "a whole number of seconds" if whole else "a number of seconds"
            raise argparse.ArgumentTypeError("%r is not %s" % (text, kind)) from None
        if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
            floor = "0 or more" if allow_zero else "more than 0"
            raise argparse.ArgumentTypeError("%s seconds: must be %s" % (text, floor))
        return value

    return parse


def _add_design_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--design",
        action="store_true",
        help="the design review of .ai/plan.md instead of the code review",
    )


def _add_activity_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--since",
        type=_bounded_int(0, activity_mod.SINCE_MAX),
        default=0,
        help="only tool uses after this one (the cursor `next:` prints)",
    )
    parser.add_argument(
        "--activity",
        type=_bounded_int(0, activity_mod.LATEST_MAX),
        default=10,
        help="how many of the latest tool uses to show (default 10)",
    )


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")


def _add_scope_flag(parser: argparse.ArgumentParser, default: Optional[str] = None) -> None:
    parser.add_argument("--scope", choices=["global", "project"], default=default)


def _add_command_group(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], name: str, help: str
) -> argparse._SubParsersAction[argparse.ArgumentParser]:
    return subparsers.add_parser(name, help=help).add_subparsers(dest="subcommand", required=True)


def _add_no_hooks_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-hooks",
        action="store_true",
        help="save language.reply without adding or removing the Claude Code hooks",
    )


def _add_dry_run_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")


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
    _add_budget_parsers(subparsers)
    _add_tokens_parsers(subparsers)
    _add_optimization_parsers(subparsers)
    _add_progress_parsers(subparsers)
    _add_workflow_parsers(subparsers)
    _add_status_parser(subparsers)
    _add_summary_parser(subparsers)

    return parser


def _add_config_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    config_sub = _add_command_group(subparsers, "config", "show and edit configuration")

    show = config_sub.add_parser("show", help="show the effective configuration")
    show.add_argument("--scope", choices=["global", "project", "effective"], default="effective")
    _add_json_flag(show)
    show.set_defaults(func=cmd_config_show)

    path_parser = config_sub.add_parser("path", help="print config file locations")
    path_parser.set_defaults(func=cmd_config_path)

    setup = config_sub.add_parser("setup", help="run the setup wizard")
    _add_scope_flag(setup, default="global")
    setup_choice = setup.add_mutually_exclusive_group()
    setup_choice.add_argument(
        "--preset",
        choices=presets_mod.NAMES,
        help="save this preset without prompting; it is fitted to the installed CLIs at load time",
    )
    setup_choice.add_argument(
        "--defaults", action="store_true", help="write the recommended config without prompting"
    )
    setup.add_argument("--force", action="store_true", help="prompt even without a TTY")
    setup.add_argument(
        "--language",
        default=None,
        metavar="TAG",
        help="the language replies are written in (language.reply), e.g. ja, ko, zh-TW",
    )
    _add_no_hooks_flag(setup)
    setup.set_defaults(func=cmd_config_setup)

    reset = config_sub.add_parser(
        "reset",
        help="clear this layer's overrides (the file keeps only version, and the global file its "
        "preset); --delete removes it",
    )
    _add_scope_flag(reset)
    reset.add_argument("--delete", action="store_true", help="delete the config file instead of clearing it")
    reset.set_defaults(func=cmd_config_reset)

    prune = config_sub.add_parser("prune", help="drop values equal to what the layer inherits")
    _add_scope_flag(prune)
    prune.add_argument("--dry-run", action="store_true", help="list what would be dropped, write nothing")
    prune.set_defaults(func=cmd_config_prune)

    set_parser = config_sub.add_parser("set", help="set one value, e.g. implementer.model.family opus")
    set_parser.add_argument("path")
    set_parser.add_argument("value")
    _add_scope_flag(set_parser)
    set_parser.add_argument("--raw", action="store_true", help="keep the value as a string")
    _add_no_hooks_flag(set_parser)
    set_parser.set_defaults(func=cmd_config_set)

    validate = config_sub.add_parser("validate", help="validate the effective configuration")
    _add_json_flag(validate)
    validate.set_defaults(func=cmd_config_validate)

    suggest_roles = config_sub.add_parser(
        "suggest-roles",
        help="propose path-scoped specialist reviewers from the project's files (no model call)",
    )
    suggest_roles.add_argument(
        "--write", action="store_true", help="add them to the project file's reviewers_extra"
    )
    _add_json_flag(suggest_roles)
    suggest_roles.add_argument(
        "--provider",
        choices=available_providers(),
        default=None,
        help="the CLI they run on (default: the first installed of claude, codex)",
    )
    suggest_roles.add_argument(
        "--model", default=None, help="model family (default: the provider's cheap one)"
    )
    suggest_roles.set_defaults(func=cmd_config_suggest_roles)


def _add_model_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    model_sub = _add_command_group(subparsers, "model", "inspect available models")
    model_list = model_sub.add_parser("list", help="list models the installed CLIs advertise")
    model_list.add_argument("--provider", choices=available_providers(), default=None)
    _add_json_flag(model_list)
    model_list.set_defaults(func=cmd_model_list)


def _add_reviewer_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    reviewer_sub = _add_command_group(subparsers, "reviewer", "manage the review panel")

    r_list = reviewer_sub.add_parser("list", help="list configured reviewers")
    _add_design_panel_flag(r_list)
    _add_json_flag(r_list)
    r_list.set_defaults(func=cmd_reviewer_list)

    r_add = reviewer_sub.add_parser("add", help="add a reviewer")
    r_add.add_argument("--provider", required=True, choices=available_providers())
    r_add.add_argument("--model", default=None, help="model family (default: provider's recommended)")
    r_add.add_argument("--role", default="general")
    r_add.add_argument("--id", default=None)
    r_add.add_argument("--pin", default=None, help="pin an exact model id instead of tracking latest")
    r_add.add_argument(
        "--when",
        choices=list(opt_mod.REVIEWER_CONDITIONS),
        default=None,
        help="when it runs (default: always; a design round on the code panel runs every reviewer)",
    )
    r_add.add_argument(
        "--when-paths",
        nargs="+",
        default=None,
        metavar="GLOB",
        help="run on a code review only when a changed path matches one of these (quote each)",
    )
    r_add.add_argument(
        "--high-risk-model",
        default=None,
        metavar="FAMILY",
        help="the model family it runs on a high-risk round",
    )
    _add_relevance_flag(r_add, "")
    _add_design_panel_flag(r_add)
    _add_scope_flag(r_add)
    r_add.set_defaults(func=cmd_reviewer_add)

    r_remove = reviewer_sub.add_parser("remove", help="remove a reviewer by id, role, or position")
    r_remove.add_argument("selector")
    _add_design_panel_flag(r_remove)
    _add_scope_flag(r_remove)
    r_remove.set_defaults(func=cmd_reviewer_remove)

    r_set = reviewer_sub.add_parser("set", help="change an existing reviewer")
    r_set.add_argument("selector")
    r_set.add_argument("--provider", choices=available_providers(), default=None)
    r_set.add_argument("--model", default=None)
    r_set.add_argument("--role", default=None)
    r_set.add_argument("--id", default=None)
    r_set.add_argument("--pin", default=None)
    r_set.add_argument("--when", choices=list(opt_mod.REVIEWER_CONDITIONS), default=None)
    r_set.add_argument(
        "--when-paths",
        nargs="+",
        default=None,
        metavar="GLOB",
        help="replace the condition with these patterns (quote each)",
    )
    risk_model = r_set.add_mutually_exclusive_group()
    risk_model.add_argument(
        "--high-risk-model",
        default=None,
        metavar="FAMILY",
        help="the model family it runs on a high-risk round",
    )
    risk_model.add_argument(
        "--clear-high-risk-model", action="store_true", help="run the usual model on high-risk rounds too"
    )
    _add_relevance_flag(r_set, "default")
    _add_design_panel_flag(r_set)
    _add_scope_flag(r_set)
    r_set.set_defaults(func=cmd_reviewer_set)


def _add_design_panel_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--design",
        action="store_true",
        help="the design review's panel (review.design.reviewers) instead of the code review's",
    )


def _add_relevance_flag(parser: argparse.ArgumentParser, default_choice: str) -> None:
    """``--relevance``: the rule that may leave the seat out of a round with nothing for it."""
    choices = [*opt_mod.RELEVANCE_RULES, opt_mod.RELEVANCE_ALWAYS]
    text = "the rule that may leave it out of a round with nothing for it; always: never"
    if default_choice:
        choices.append(default_choice)
        text += "; %s: its role's own" % default_choice
    parser.add_argument("--relevance", choices=choices, default=None, help=text)


def _add_doctor_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    doctor_parser = subparsers.add_parser("doctor", help="diagnose CLIs, auth and configuration")
    _add_json_flag(doctor_parser)
    doctor_parser.add_argument("--fast", action="store_true", help="skip model discovery")
    doctor_parser.add_argument("--strict", action="store_true", help="exit non-zero when problems are found")
    doctor_parser.set_defaults(func=cmd_doctor)


def _add_hooks_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    hooks_sub = _add_command_group(
        subparsers, "hooks", "the reply-language hooks in Claude Code's user settings"
    )
    hooks_install = hooks_sub.add_parser("install", help="add them, or bring them up to date")
    _add_dry_run_flag(hooks_install)
    hooks_install.set_defaults(func=cmd_hooks_install)
    hooks_uninstall = hooks_sub.add_parser("uninstall", help="remove them, the relay and its record")
    _add_dry_run_flag(hooks_uninstall)
    hooks_uninstall.set_defaults(func=cmd_hooks_uninstall)
    hooks_status = hooks_sub.add_parser("status", help="whether they are installed and current")
    _add_json_flag(hooks_status)
    hooks_status.set_defaults(func=cmd_hooks_status)


def _add_run_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    run_parser = subparsers.add_parser("run", help="run one configured role against a prompt")
    run_parser.add_argument(
        "role", help="orchestrator | architect | implementer | review_fixer | <reviewer id>"
    )
    run_parser.add_argument(
        "--tier",
        default=None,
        help="run this role on one of its configured model_tiers (e.g. light)",
    )
    run_parser.add_argument("--prompt", default=None)
    run_parser.add_argument("--prompt-file", default=None, help="path, or - for stdin")
    run_parser.add_argument("--mode", choices=list(MODES), default=None)
    run_parser.add_argument(
        "--output",
        default=None,
        help="write the response here instead of stdout; kept as it is when the run produced none",
    )
    run_parser.add_argument(
        "--timeout",
        type=_seconds(whole=True),
        default=None,
        help="total deadline in seconds (default: run.timeout_seconds.<role>; "
        "review.timeout_seconds for a reviewer)",
    )
    run_parser.add_argument(
        "--idle-timeout",
        type=_seconds(),
        default=None,
        help="treat as stalled after this long with no output (streaming providers only)",
    )
    run_parser.add_argument("--force", action="store_true", help="run even though a budget is spent")
    run_parser.add_argument(
        "--detach",
        action="store_true",
        help="start the run in its own process and return a job id immediately",
    )
    run_parser.add_argument(
        "--resume",
        action="store_true",
        help="architect only: revise this workflow's plan by continuing the last architect session",
    )
    run_parser.add_argument(
        "--resume-prompt-file",
        default=None,
        help="with --resume: the prompt for a continued session (a fresh run gets --prompt-file)",
    )
    run_parser.add_argument("--json", action="store_true", help="machine-readable output")
    run_parser.add_argument("--job-file", default=None, help=argparse.SUPPRESS)
    run_parser.add_argument("--print-command", action="store_true", help="print the CLI invocation and exit")
    run_parser.add_argument("--extra", nargs=argparse.REMAINDER, help="extra args passed to the provider CLI")
    run_parser.set_defaults(func=cmd_run)


def _add_review_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    review_sub = _add_command_group(subparsers, "review", "independent multi-model review pipeline")

    snapshot = review_sub.add_parser("snapshot", help="freeze the change under review")
    snapshot.add_argument("--base", default=None, help="revision to diff against (default: HEAD)")
    snapshot.add_argument("--no-untracked", action="store_true")
    snapshot.add_argument(
        "--no-exclude",
        action="store_true",
        help="send every changed file, including generated and vendored ones",
    )
    snapshot.add_argument(
        "--full",
        action="store_true",
        help="diff the whole change even on a re-review, not just what the fix changed",
    )
    snapshot.add_argument(
        "--surrounding",
        choices=context_mod.SURROUNDING_MODES,
        default=None,
        help="override review.context.surrounding for this snapshot only",
    )
    _add_json_flag(snapshot)
    snapshot.set_defaults(func=cmd_review_snapshot)

    review_run = review_sub.add_parser("run", help="run every reviewer against the frozen snapshot")
    review_run.add_argument(
        "--request",
        default=None,
        help="the design request the plan answers (--design; default .ai/execution/design-request.md)",
    )
    review_run.add_argument(
        "--iteration",
        type=int,
        default=None,
        help="review round; derived from the snapshot when omitted",
    )
    review_run.add_argument("--sequential", action="store_true")
    review_run.add_argument("--only", nargs="*", default=None, help="reviewer ids or roles to run")
    review_run.add_argument("--context", default=None, help="extra context for reviewers")
    review_run.add_argument("--base", default=None)
    review_run.add_argument("--timeout", type=_seconds(whole=True), default=None)
    review_run.add_argument("--idle-timeout", type=_seconds(), default=None)
    review_run.add_argument(
        "--force",
        action="store_true",
        help="run past the review iteration budget, or with the tests recorded as failing",
    )
    review_run.add_argument(
        "--surrounding",
        choices=context_mod.SURROUNDING_MODES,
        default=None,
        help="override review.context.surrounding for this run only",
    )
    review_run.add_argument(
        "--high-risk",
        action="store_true",
        help="declare the change high-risk: adds when: high-risk reviewers and keeps every role; "
        "level and gate are unchanged",
    )
    review_run.add_argument(
        "--progress",
        action="store_true",
        help="echo each reviewer's tool uses to stderr while the round runs",
    )
    _add_json_flag(review_run)
    review_run.set_defaults(func=cmd_review_run)

    consolidate = review_sub.add_parser("consolidate", help="re-parse reports and dedupe findings")
    consolidate.add_argument("--iteration", type=int, default=None)
    _add_json_flag(consolidate)
    consolidate.set_defaults(func=cmd_review_consolidate)

    review_show = review_sub.add_parser("show", help="show the consolidated review")
    review_show.add_argument("--accepted", action="store_true", help="only accepted findings")
    _add_json_flag(review_show)
    review_show.set_defaults(func=cmd_review_show)

    triage = review_sub.add_parser("triage", help="record a triage decision for findings")
    triage.add_argument("ids", nargs="+")
    triage.add_argument("--status", required=True, choices=list(review_mod.TRIAGE_STATUSES))
    triage.add_argument("--note", default=None)
    triage.set_defaults(func=cmd_review_triage)

    fix_brief = review_sub.add_parser("fix-brief", help="emit the accepted-findings brief for the fixer")
    fix_brief.add_argument("--output", default=None)
    fix_brief.set_defaults(func=cmd_review_fix_brief)

    status = review_sub.add_parser("status", help="report whether a re-review is warranted")
    _add_json_flag(status)
    status.set_defaults(func=cmd_review_status)

    for parser_with_scope in (review_run, consolidate, review_show, triage, fix_brief, status):
        _add_design_flag(parser_with_scope)


def _add_design_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    design_sub = _add_command_group(subparsers, "design", "the user's approval of the plan")
    design_approve = design_sub.add_parser(
        "approve", help="record the user's approval of the plan as it is now (only after their yes)"
    )
    _add_json_flag(design_approve)
    design_approve.set_defaults(func=cmd_design_approve)


def _add_state_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    state_sub = _add_command_group(subparsers, "state", "inspect or append run state")
    state_show = state_sub.add_parser("show")
    _add_json_flag(state_show)
    state_show.set_defaults(func=cmd_state_show)
    state_record = state_sub.add_parser("record")
    state_record.add_argument("stage")
    state_record.add_argument("status")
    state_record.add_argument("--detail", nargs="*", default=None, help="key=value pairs")
    state_record.set_defaults(func=cmd_state_record)


def _add_jobs_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    jobs_sub = _add_command_group(subparsers, "jobs", "detached runs, so no call blocks forever")
    jobs_list = jobs_sub.add_parser("list", help="every recorded job, newest first")
    _add_json_flag(jobs_list)
    jobs_list.set_defaults(func=cmd_jobs_list)

    jobs_show = jobs_sub.add_parser("show", help="one job")
    jobs_show.add_argument("job_id")
    jobs_show.add_argument("--output", action="store_true", help="also print its output")
    _add_json_flag(jobs_show)
    _add_activity_flags(jobs_show)
    jobs_show.set_defaults(func=cmd_jobs_show)
    jobs_wait = jobs_sub.add_parser("wait", help="wait for a job, with a deadline of your own")
    jobs_wait.add_argument("job_id")
    jobs_wait.add_argument("--timeout", type=_seconds(allow_zero=True), default=60.0)
    jobs_wait.add_argument("--poll", type=_seconds(), default=1.0)
    _add_json_flag(jobs_wait)
    _add_activity_flags(jobs_wait)
    jobs_wait.set_defaults(func=cmd_jobs_wait)
    jobs_cancel = jobs_sub.add_parser("cancel", help="stop a running job")
    jobs_cancel.add_argument("job_id")
    jobs_cancel.set_defaults(func=cmd_jobs_cancel)


def _add_budget_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    budget_sub = _add_command_group(subparsers, "budget", "attempt budgets that stop runaway loops")
    budget_show = budget_sub.add_parser("show", help="what has been spent")
    _add_json_flag(budget_show)
    budget_show.set_defaults(func=cmd_budget_show)
    budget_consume = budget_sub.add_parser("consume", help="claim an attempt at a stage")
    budget_consume.add_argument("stage", help="a stage the orchestrator runs itself, e.g. test")
    budget_consume.add_argument("--force", action="store_true")
    budget_consume.set_defaults(func=cmd_budget_consume)
    budget_reset = budget_sub.add_parser("reset", help="start the budgets again, keeping the token account")
    budget_reset.set_defaults(func=cmd_budget_reset)


def _add_tokens_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    # Separate from `budget` on purpose: attempts are enforced, tokens are only
    # counted, and putting them under one command invites reading one as the
    # other.
    tokens_sub = _add_command_group(subparsers, "tokens", "what the workflow has spent, per stage")
    tokens_show = tokens_sub.add_parser("show", help="the token account (reported, never enforced)")
    _add_json_flag(tokens_show)
    tokens_show.set_defaults(func=cmd_tokens_show)


def _add_optimization_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    optimization_parser = subparsers.add_parser(
        "optimization", help="what optimization.level has decided, over time"
    )
    optimization_sub = optimization_parser.add_subparsers(dest="optimization_command", required=True)
    optimization_report = optimization_sub.add_parser(
        "report", help="rounds refused, panels cut, and what that came to"
    )
    _add_json_flag(optimization_report)
    optimization_report.set_defaults(func=cmd_optimization_report)


def _add_progress_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    progress_sub = _add_command_group(subparsers, "progress", "detect a loop that is going nowhere")
    progress_record = progress_sub.add_parser("record", help="record a stage outcome signature")
    progress_record.add_argument("stage")
    progress_record.add_argument("--signature", required=True, help="e.g. the failing test summary")
    _add_json_flag(progress_record)
    progress_record.set_defaults(func=cmd_progress_record)


def _add_workflow_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    workflow_sub = _add_command_group(
        subparsers, "workflow", "the workflows in this project and which one is yours"
    )
    workflow_list = workflow_sub.add_parser("list", help="every workflow here, most recent first")
    _add_json_flag(workflow_list)
    workflow_list.set_defaults(func=cmd_workflow_list)
    workflow_show = workflow_sub.add_parser("show", help="which workflow this command is in, and why")
    _add_json_flag(workflow_show)
    workflow_show.set_defaults(func=cmd_workflow_show)
    workflow_use = workflow_sub.add_parser("use", help="remember an id for this directory")
    workflow_use.add_argument("id")
    workflow_use.set_defaults(func=cmd_workflow_use)
    workflow_remove = workflow_sub.add_parser("remove", help="delete one finished workflow's artifacts")
    workflow_remove.add_argument("id")
    workflow_remove.add_argument("--yes", action="store_true", help="do not ask")
    workflow_remove.add_argument(
        "--force", action="store_true", help="delete it even with a stage in flight or a job unfinished"
    )
    workflow_remove.set_defaults(func=cmd_workflow_remove)


def _add_status_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    status_parser = subparsers.add_parser(
        "status", help="continue or stop: budgets, stalls and open findings in one verdict"
    )
    _add_json_flag(status_parser)
    status_parser.set_defaults(func=cmd_status)


def _add_summary_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    summary = subparsers.add_parser("summary", help="print the end-of-run summary")
    _add_json_flag(summary)
    summary.set_defaults(func=cmd_summary)


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
