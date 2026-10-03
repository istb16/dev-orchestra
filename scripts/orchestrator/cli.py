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
import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import activity as activity_mod
from . import approval as approval_mod
from . import config as config_mod
from . import context as context_mod
from . import doctor as doctor_mod
from . import jobs as jobs_mod
from . import ledger as ledger_mod
from . import miniyaml
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
from .cli_review import (
    _adoption_line,
    _approved_as_recorded,
    _code_final_pass,
    _condition_excluded,
    _condition_paths,
    _configured_inline_chars,
    _coverage_advice,
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
    _OPT_ROW,
    _SCORECARD_ALONE,
    _SCORECARD_BIAS,
    _SCORECARD_EFFORT,
    _SCORECARD_FLOORS,
    _SCORECARD_OUTCOMES,
    _SCORECARD_PAIRS,
    _SCORECARD_TOTAL,
    _TOKEN_ROW,
    _context_per_run_row,
    _context_row,
    _counts,
    _figure,
    _pair_exclusions,
    _paired_rows,
    _panel_names,
    _revision_rows,
    _rounds_recorded,
    _runs_row,
    _scorecard_cost_row,
    _scorecard_counts,
    _scorecard_inputs,
    _scorecard_per_accepted,
    _scorecard_rates,
    _scorecard_rows,
    _scorecard_spend,
    _token_row,
    _tools_row,
    _usd,
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

__version__ = "0.17.0"


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
    config_parser = subparsers.add_parser("config", help="show and edit configuration")
    config_sub = config_parser.add_subparsers(dest="subcommand", required=True)

    show = config_sub.add_parser("show", help="show the effective configuration")
    show.add_argument("--scope", choices=["global", "project", "effective"], default="effective")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_config_show)

    path_parser = config_sub.add_parser("path", help="print config file locations")
    path_parser.set_defaults(func=cmd_config_path)

    setup = config_sub.add_parser("setup", help="run the setup wizard")
    setup.add_argument("--scope", choices=["global", "project"], default="global")
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
    setup.set_defaults(func=cmd_config_setup)

    reset = config_sub.add_parser(
        "reset",
        help="clear this layer's overrides (the file keeps only version, and the global file its "
        "preset); --delete removes it",
    )
    reset.add_argument("--scope", choices=["global", "project"], default=None)
    reset.add_argument("--delete", action="store_true", help="delete the config file instead of clearing it")
    reset.set_defaults(func=cmd_config_reset)

    prune = config_sub.add_parser("prune", help="drop values equal to what the layer inherits")
    prune.add_argument("--scope", choices=["global", "project"], default=None)
    prune.add_argument("--dry-run", action="store_true", help="list what would be dropped, write nothing")
    prune.set_defaults(func=cmd_config_prune)

    set_parser = config_sub.add_parser("set", help="set one value, e.g. implementer.model.family opus")
    set_parser.add_argument("path")
    set_parser.add_argument("value")
    set_parser.add_argument("--scope", choices=["global", "project"], default=None)
    set_parser.add_argument("--raw", action="store_true", help="keep the value as a string")
    set_parser.set_defaults(func=cmd_config_set)

    validate = config_sub.add_parser("validate", help="validate the effective configuration")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=cmd_config_validate)

    suggest_roles = config_sub.add_parser(
        "suggest-roles",
        help="propose path-scoped specialist reviewers from the project's files (no model call)",
    )
    suggest_roles.add_argument(
        "--write", action="store_true", help="add them to the project file's reviewers_extra"
    )
    suggest_roles.add_argument("--json", action="store_true")
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

    # model ------------------------------------------------------------------
    model_parser = subparsers.add_parser("model", help="inspect available models")
    model_sub = model_parser.add_subparsers(dest="subcommand", required=True)
    model_list = model_sub.add_parser("list", help="list models the installed CLIs advertise")
    model_list.add_argument("--provider", choices=available_providers(), default=None)
    model_list.add_argument("--json", action="store_true")
    model_list.set_defaults(func=cmd_model_list)

    # reviewer ---------------------------------------------------------------
    reviewer_parser = subparsers.add_parser("reviewer", help="manage the review panel")
    reviewer_sub = reviewer_parser.add_subparsers(dest="subcommand", required=True)

    r_list = reviewer_sub.add_parser("list", help="list configured reviewers")
    r_list.add_argument("--json", action="store_true")
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
        help="when it runs on a code review (default: always; design reviews run every reviewer)",
    )
    r_add.add_argument(
        "--when-paths",
        nargs="+",
        default=None,
        metavar="GLOB",
        help="run on a code review only when a changed path matches one of these (quote each)",
    )
    r_add.add_argument("--scope", choices=["global", "project"], default=None)
    r_add.set_defaults(func=cmd_reviewer_add)

    r_remove = reviewer_sub.add_parser("remove", help="remove a reviewer by id, role, or position")
    r_remove.add_argument("selector")
    r_remove.add_argument("--scope", choices=["global", "project"], default=None)
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
    r_set.add_argument("--scope", choices=["global", "project"], default=None)
    r_set.set_defaults(func=cmd_reviewer_set)

    # doctor -----------------------------------------------------------------
    doctor_parser = subparsers.add_parser("doctor", help="diagnose CLIs, auth and configuration")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument("--fast", action="store_true", help="skip model discovery")
    doctor_parser.add_argument("--strict", action="store_true", help="exit non-zero when problems are found")
    doctor_parser.set_defaults(func=cmd_doctor)

    # run --------------------------------------------------------------------
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
    run_parser.add_argument("--timeout", type=int, default=None, help="total deadline in seconds")
    run_parser.add_argument(
        "--idle-timeout",
        type=float,
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

    # review -----------------------------------------------------------------
    review_parser = subparsers.add_parser("review", help="independent multi-model review pipeline")
    review_sub = review_parser.add_subparsers(dest="subcommand", required=True)

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
    snapshot.add_argument("--json", action="store_true")
    snapshot.set_defaults(func=cmd_review_snapshot)

    def _add_design_flag(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--design",
            action="store_true",
            help="the design review of .ai/plan.md instead of the code review",
        )

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
    review_run.add_argument("--timeout", type=int, default=None)
    review_run.add_argument("--idle-timeout", type=float, default=None)
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
        help="declare the change high-risk: adds when: high-risk reviewers; level and gate are unchanged",
    )
    review_run.add_argument(
        "--progress",
        action="store_true",
        help="echo each reviewer's tool uses to stderr while the round runs",
    )
    review_run.add_argument("--json", action="store_true")
    review_run.set_defaults(func=cmd_review_run)

    consolidate = review_sub.add_parser("consolidate", help="re-parse reports and dedupe findings")
    consolidate.add_argument("--iteration", type=int, default=None)
    consolidate.add_argument("--json", action="store_true")
    consolidate.set_defaults(func=cmd_review_consolidate)

    review_show = review_sub.add_parser("show", help="show the consolidated review")
    review_show.add_argument("--accepted", action="store_true", help="only accepted findings")
    review_show.add_argument("--json", action="store_true")
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
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_review_status)

    for parser_with_scope in (review_run, consolidate, review_show, triage, fix_brief, status):
        _add_design_flag(parser_with_scope)

    # design -----------------------------------------------------------------
    design_parser = subparsers.add_parser("design", help="the user's approval of the plan")
    design_sub = design_parser.add_subparsers(dest="subcommand", required=True)
    design_approve = design_sub.add_parser(
        "approve", help="record the user's approval of the plan as it is now (only after their yes)"
    )
    design_approve.add_argument("--json", action="store_true")
    design_approve.set_defaults(func=cmd_design_approve)

    # state ------------------------------------------------------------------
    state_parser = subparsers.add_parser("state", help="inspect or append run state")
    state_sub = state_parser.add_subparsers(dest="subcommand", required=True)
    state_show = state_sub.add_parser("show")
    state_show.add_argument("--json", action="store_true")
    state_show.set_defaults(func=cmd_state_show)
    state_record = state_sub.add_parser("record")
    state_record.add_argument("stage")
    state_record.add_argument("status")
    state_record.add_argument("--detail", nargs="*", default=None, help="key=value pairs")
    state_record.set_defaults(func=cmd_state_record)

    jobs_parser = subparsers.add_parser("jobs", help="detached runs, so no call blocks forever")
    jobs_sub = jobs_parser.add_subparsers(dest="subcommand", required=True)
    jobs_list = jobs_sub.add_parser("list", help="every recorded job, newest first")
    jobs_list.add_argument("--json", action="store_true")
    jobs_list.set_defaults(func=cmd_jobs_list)

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

    jobs_show = jobs_sub.add_parser("show", help="one job")
    jobs_show.add_argument("job_id")
    jobs_show.add_argument("--output", action="store_true", help="also print its output")
    jobs_show.add_argument("--json", action="store_true")
    _add_activity_flags(jobs_show)
    jobs_show.set_defaults(func=cmd_jobs_show)
    jobs_wait = jobs_sub.add_parser("wait", help="wait for a job, with a deadline of your own")
    jobs_wait.add_argument("job_id")
    jobs_wait.add_argument("--timeout", type=float, default=60.0)
    jobs_wait.add_argument("--poll", type=float, default=1.0)
    jobs_wait.add_argument("--json", action="store_true")
    _add_activity_flags(jobs_wait)
    jobs_wait.set_defaults(func=cmd_jobs_wait)
    jobs_cancel = jobs_sub.add_parser("cancel", help="stop a running job")
    jobs_cancel.add_argument("job_id")
    jobs_cancel.set_defaults(func=cmd_jobs_cancel)

    budget_parser = subparsers.add_parser("budget", help="attempt budgets that stop runaway loops")
    budget_sub = budget_parser.add_subparsers(dest="subcommand", required=True)
    budget_show = budget_sub.add_parser("show", help="what has been spent")
    budget_show.add_argument("--json", action="store_true")
    budget_show.set_defaults(func=cmd_budget_show)
    budget_consume = budget_sub.add_parser("consume", help="claim an attempt at a stage")
    budget_consume.add_argument("stage", help="a stage the orchestrator runs itself, e.g. test")
    budget_consume.add_argument("--force", action="store_true")
    budget_consume.set_defaults(func=cmd_budget_consume)
    budget_reset = budget_sub.add_parser("reset", help="start the budgets again, keeping the token account")
    budget_reset.set_defaults(func=cmd_budget_reset)

    # Separate from `budget` on purpose: attempts are enforced, tokens are only
    # counted, and putting them under one command invites reading one as the
    # other.
    tokens_parser = subparsers.add_parser("tokens", help="what the workflow has spent, per stage")
    tokens_sub = tokens_parser.add_subparsers(dest="subcommand", required=True)
    tokens_show = tokens_sub.add_parser("show", help="the token account (reported, never enforced)")
    tokens_show.add_argument("--json", action="store_true")
    tokens_show.set_defaults(func=cmd_tokens_show)

    optimization_parser = subparsers.add_parser(
        "optimization", help="what optimization.level has decided, over time"
    )
    optimization_sub = optimization_parser.add_subparsers(dest="optimization_command", required=True)
    optimization_report = optimization_sub.add_parser(
        "report", help="rounds refused, panels cut, and what that came to"
    )
    optimization_report.add_argument("--json", action="store_true")
    optimization_report.set_defaults(func=cmd_optimization_report)

    progress_parser = subparsers.add_parser("progress", help="detect a loop that is going nowhere")
    progress_sub = progress_parser.add_subparsers(dest="subcommand", required=True)
    progress_record = progress_sub.add_parser("record", help="record a stage outcome signature")
    progress_record.add_argument("stage")
    progress_record.add_argument("--signature", required=True, help="e.g. the failing test summary")
    progress_record.add_argument("--json", action="store_true")
    progress_record.set_defaults(func=cmd_progress_record)

    workflow_parser = subparsers.add_parser(
        "workflow", help="the workflows in this project and which one is yours"
    )
    workflow_sub = workflow_parser.add_subparsers(dest="subcommand", required=True)
    workflow_list = workflow_sub.add_parser("list", help="every workflow here, most recent first")
    workflow_list.add_argument("--json", action="store_true")
    workflow_list.set_defaults(func=cmd_workflow_list)
    workflow_show = workflow_sub.add_parser("show", help="which workflow this command is in, and why")
    workflow_show.add_argument("--json", action="store_true")
    workflow_show.set_defaults(func=cmd_workflow_show)
    workflow_use = workflow_sub.add_parser("use", help="remember an id for this directory")
    workflow_use.add_argument("id")
    workflow_use.set_defaults(func=cmd_workflow_use)
    workflow_remove = workflow_sub.add_parser("remove", help="delete one finished workflow's artifacts")
    workflow_remove.add_argument("id")
    workflow_remove.add_argument("--yes", action="store_true", help="do not ask")
    workflow_remove.set_defaults(func=cmd_workflow_remove)

    status_parser = subparsers.add_parser(
        "status", help="continue or stop: budgets, stalls and open findings in one verdict"
    )
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=cmd_status)

    summary = subparsers.add_parser("summary", help="print the end-of-run summary")
    summary.add_argument("--json", action="store_true")
    summary.set_defaults(func=cmd_summary)

    return parser


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
