"""The state, budget, token and summary commands."""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Optional

from . import jobs as jobs_mod
from . import ledger as ledger_mod
from . import optimization_render as opt_render
from . import optimization_report as opt_report
from . import review as review_mod
from . import workflow as workflow_mod
from . import workspace as ws
from .cli_common import _emit_json, _err, _ledger, _out, _workspace, _wrote_plan

# --------------------------------------------------------------------------- state


def cmd_jobs_list(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    found = jobs_mod.list_jobs(workspace)
    if args.json:
        _emit_json(found)
        return 0
    if not found:
        _out("No jobs recorded.")
        return 0
    for job in found:
        _out(
            "%-34s %-10s %-14s %s"
            % (job.get("id"), job.get("status"), job.get("stage"), job.get("started_at"))
        )
    return 0


def cmd_jobs_show(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    job = jobs_mod.read_job(workspace, args.job_id)
    if job is None:
        _err("no such job: %s" % args.job_id)
        return 2
    act = jobs_mod.read_activity(workspace, job, args.since, args.activity)
    if args.json:
        _emit_json(_with_activity(job, act))
        return 0
    _out(jobs_mod.render(job, act))
    if args.output and job.get("output_file"):
        _out("")
        _out(ws.read_text(str(job["output_file"])))
    return 0


def _with_activity(job: Dict[str, Any], act: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``job`` as ``--json`` prints it: with an ``activity`` key only when the
    job has an activity file. The record on disk is not changed."""
    if act is None:
        return job
    return {**job, "activity": act}


def cmd_jobs_wait(args: argparse.Namespace) -> int:
    """Bounded wait: returning while the job runs is an outcome, not an error."""
    workspace = _workspace(args)
    if jobs_mod.read_job(workspace, args.job_id) is None:
        _err("no such job: %s" % args.job_id)
        return 2
    job = jobs_mod.wait(workspace, args.job_id, timeout=args.timeout, poll=args.poll)
    act = jobs_mod.read_activity(workspace, job, args.since, args.activity)
    if args.json:
        _emit_json(_with_activity(job, act))
    else:
        _out(jobs_mod.render(job, act))
        if job.get("status") == "succeeded" and job.get("output_file"):
            _out("")
            _out(ws.read_text(str(job["output_file"])))
    if job.get("waited_out"):
        return 4
    # A refused `--output` write exits non-zero in the foreground, and waiting
    # on the job is the same caller asking the same question about the same
    # file. The run may well have succeeded; the file it was told to fill did
    # not get filled, and that is what the next command in the chain reads.
    if job.get("output_written") is False:
        return 1
    return 0 if job.get("status") == "succeeded" else 1


def cmd_jobs_cancel(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    try:
        job = jobs_mod.cancel(workspace, args.job_id)
    except KeyError:
        _err("no such job: %s" % args.job_id)
        return 2
    _out(jobs_mod.render(job))
    return 0


def cmd_budget_show(args: argparse.Namespace) -> int:
    book = _ledger(args)
    summary = book.summary()
    if args.json:
        _emit_json(summary)
        return 0
    _out("Workflow started: %s" % summary["started_at"])
    for stage, entry in summary["budgets"].items():
        _out("  %-14s %d/%d used" % (stage, entry["used"], entry["limit"]))
    total = summary["total_delegated_runs"]
    _out("  %-14s %s/%s used" % ("delegated runs", total["used"], total["limit"]))
    runtime = summary["runtime"]
    if runtime["remaining"] is not None:
        # Shown as used/limit like every other budget above it, and labelled:
        # this counts delegated execution, not how long the workflow has been
        # open, so a figure far below the wall clock is not a bug.
        _out("  %-14s %.0f/%ss used (delegated execution)" % ("runtime", runtime["used"], runtime["limit"]))
    # Whether or not the cap is on: the figure says what the runs cost, not
    # how close they are to a limit.
    if runtime["suspended"]:
        _out(
            "  %-14s %.0fs of delegated run time spent asleep was not charged"
            % ("runtime", runtime["suspended"])
        )
    for stage, repeats in (summary["signatures"] or {}).items():
        if repeats and int(repeats) > 1:
            _out("  %-14s same outcome %s times in a row" % (stage, repeats))
    return 0


def cmd_budget_consume(args: argparse.Namespace) -> int:
    """Claim an attempt at a stage the orchestrator runs itself, such as tests."""
    book = _ledger(args)
    try:
        book.consume(args.stage, force=args.force)
    except ledger_mod.BudgetExhausted as exc:
        _err("refusing another %s attempt: %s" % (args.stage, exc))
        _err("Report what is still failing instead of retrying.")
        return ledger_mod.EXIT_BUDGET_EXHAUSTED
    remaining = book.remaining(args.stage)
    suffix = "" if remaining is None else " (%d left)" % remaining
    _out("%s attempt recorded%s" % (args.stage, suffix))
    return 0


def cmd_budget_reset(args: argparse.Namespace) -> int:
    _ledger(args).reset()
    # It used to claim a fresh workflow, which was wrong twice over: the work
    # done so far is still the same workflow, and the token account survives a
    # reset. Saying otherwise invited reading `tokens show` as a contradiction.
    _out("Budgets reset; the token account is kept.")
    return 0


_TOKEN_ROW = "  %-18s %5s %9s %9s %9s %9s %9s %7s %9s"


def _token_row(name: str, account: Dict[str, Any]) -> str:
    def num(field: str) -> str:
        value = int(account.get(field) or 0)
        return ws.fmt_int(value) if value else "-"

    def tool(field: str) -> str:
        """Unreported prints ``-``; a measured zero prints ``0``.

        ``num`` renders both as ``-``, which is right for tokens -- a run that
        billed nothing did not happen -- and wrong here: a reviewer that opened
        no files is a result, and the finding this whole count exists to
        produce. Only ``tool_reported_runs`` separates the two.
        """
        if not int(account.get("tool_reported_runs") or 0):
            return "-"
        return ws.fmt_int(int(account.get(field) or 0))

    runs = "%s/%s" % (account.get("measured_runs") or 0, account.get("runs") or 0)
    cost = float(account.get("cost_usd") or 0.0)
    return _TOKEN_ROW % (
        name,
        runs,
        num("input_tokens"),
        num("output_tokens"),
        num("total_tokens"),
        num("billed_tokens"),
        ws.fmt_usd(cost) if cost else "-",
        tool("tool_uses"),
        tool("tool_output_chars"),
    )


def cmd_tokens_show(args: argparse.Namespace) -> int:
    """What the workflow has spent. Accounting only -- it refuses nothing."""
    book = _ledger(args)
    report = book.token_report()
    if args.json:
        _emit_json(report)
        return 0
    for line in _token_lines(report):
        _out(line)
    return 0


def _token_lines(report: Dict[str, Any]) -> List[str]:
    """``tokens show`` as text: the table, then a note for each way it falls short."""
    totals = report["totals"]
    if not totals["runs"]:
        return ["No delegated runs recorded yet."]

    header = ("stage", "meas.", "input", "output", "total", "billed", "cost", "tools", "tool out")
    lines = [_TOKEN_ROW % header]
    for stage, account in sorted(report["by_stage"].items()):
        lines.append(_token_row(stage, account))
    lines.append(_token_row("ALL", totals))
    if report["by_label"]:
        lines.append("")
        lines.append("Per reviewer and tier:")
        for label, account in sorted(report["by_label"].items()):
            lines.append(_token_row(label, account))

    lines.append("")
    chars = int(totals.get("prompt_chars") or 0)
    if chars:
        lines.append(
            "Prompt text this repo composed: %s chars over %d run(s). "
            "That is the part it can shorten." % (ws.fmt_int(chars), totals["runs"])
        )
    reported_tools = int(totals.get("tool_reported_runs") or 0)
    if reported_tools:
        lines.append(
            "`tools` counts every tool call, whatever it was called. `tool out` is what "
            "those tools printed back -- not source read: `wc -l` returns 3 characters "
            "for a 200-line file and `cat` returns the file. How much of this repository "
            "a delegated run actually read is not knowable from here."
        )
    unknown_tools = int(totals.get("tool_unknown_runs") or 0)
    silent_tools = max(int(totals["runs"]) - reported_tools - unknown_tools, 0)
    if unknown_tools:
        # A run recorded before this was counted cannot say whether it used
        # tools, and "it reported no tool activity" would be a claim about it.
        # Said beside the count below rather than instead of it, now that
        # ``silent_tools`` subtracts these out: a panel with a legacy stage and
        # a Codex stage has both kinds, and the two numbers plus the reported
        # ones account for every run.
        lines.append(
            "%d of %d run(s) predate tool counting and cannot say whether they used "
            "tools, so the tool columns leave them out. That is not the same as "
            "having used none." % (unknown_tools, totals["runs"])
        )
    if silent_tools:
        lines.append(
            "%d of %d run(s) reported no tool activity (Codex does not); the tool "
            "columns cover only the runs that did." % (silent_tools, totals["runs"])
        )
    if not report["complete"]:
        silent = int(totals["runs"]) - int(totals["measured_runs"])
        lines.append(
            "%d of %d run(s) reported no usage, so every total above is a floor, "
            "not a total." % (silent, totals["runs"])
        )
    if not report.get("priced", True):
        unpriced = int(totals["runs"]) - int(totals.get("priced_runs") or 0)
        lines.append(
            "%d of %d run(s) reported tokens but no cost, so the cost column is a "
            "floor even where the token counts are not. Comparing providers on it "
            "understates the ones that price nothing." % (unpriced, totals["runs"])
        )
    return lines


def _workflows_recorded(args: argparse.Namespace, workspace: ws.Workspace) -> List[Dict[str, Any]]:
    """Each workflow's run log, with the workspace its review reports are in.

    Kept apart per workflow, where ``_rounds_recorded`` flattens them: an
    event names its round by a sha and an iteration, and both repeat from one
    workflow to the next, so matching an event to the report of its round only
    means something inside one workflow. Which workflows are read is the same
    rule as there.
    """
    if getattr(args, "workflow", ""):
        return [
            {
                "workflow": workspace.workflow,
                "workspace": workspace,
                "events": list(workspace.read_state().get("events") or []),
            }
        ]

    found: List[Dict[str, Any]] = []
    for entry in workflow_mod.listing(workspace.container):
        state = ws.read_json(os.path.join(entry["dir"], "state.json"), {}) or {}
        # Not a mapping reads as nothing recorded, as it does in the listing.
        events = state.get("events") if isinstance(state, dict) else None
        if isinstance(events, list):
            found.append(
                {
                    "workflow": entry["workflow"],
                    "workspace": ws.Workspace(workspace.root, workspace.container, entry["workflow"]),
                    "events": [item for item in events if isinstance(item, dict)],
                }
            )
    if not any(item["events"] for item in found):
        # Nothing recorded in any workflow: this one's own state, empty or not.
        events = [item for item in (workspace.read_state().get("events") or []) if isinstance(item, dict)]
        found = [{"workflow": workspace.workflow, "workspace": workspace, "events": events}]
    return found


def _rounds_recorded(
    args: argparse.Namespace,
    workspace: ws.Workspace,
    workflows: Optional[List[Dict[str, Any]]] = None,
) -> "tuple[List[Dict[str, Any]], str]":
    """Every review round recorded in this project, and what was read.

    A level's effect is a rate -- how often it refused a round, how often it
    cut the panel -- and a rate needs rounds. Reading the run log rather than
    the ledger was what supplied them, because `budget reset` starts a fresh
    ledger while the event log keeps accumulating. Splitting the run log per
    workflow took that away again: one workflow is a handful of rounds, which
    is not a rate. So the default reads every workflow here.

    `--workflow` narrows it to one, which is the question "what did the level
    do *in this piece of work*" rather than "in this repository".

    ``workflows`` is what ``_workflows_recorded`` returned, for a caller that
    needs both views and should read the logs once.
    """
    if workflows is None:
        workflows = _workflows_recorded(args, workspace)
    events = [event for item in workflows for event in item["events"]]
    if getattr(args, "workflow", ""):
        return events, workspace.relative(workspace.state_path)

    # One sequence out of several logs. The counts do not depend on the order,
    # but "what happened over time" reads wrong when it is per directory.
    events.sort(key=lambda item: str(item.get("at") or ""))
    return events, workspace.relative(workspace.container)


def cmd_optimization_report(args: argparse.Namespace) -> int:
    """What the level decided, over every round this project has recorded."""
    workspace = _workspace(args)
    workflows = _workflows_recorded(args, workspace)
    events, source = _rounds_recorded(args, workspace, workflows)
    report = opt_report.summarise_rounds(events)
    # Beside the event summary rather than inside it: that one reads the run
    # log alone, and what a round found is only in the round's report.
    report["scorecard"] = opt_report.reviewer_scorecard(_scorecard_inputs(workflows))
    report["architect_revisions"] = opt_report.architect_revisions(
        [(item["events"], _wrote_plan(item["workspace"])) for item in workflows]
    )
    if args.json:
        _emit_json(report)
        return 0

    for line in opt_render.report_lines(report, source):
        _out(line)
    return 0


def _scorecard_inputs(workflows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One entry per workflow and stage, for ``opt_report.reviewer_scorecard``.

    Per workflow, because a round is matched to its report by a sha and an
    iteration, and both repeat across workflows. Per stage, because the design
    review keeps its reports in a directory of its own.
    """
    inputs = []
    for item in workflows:
        scopes = (
            ("code", "review", item["workspace"]),
            ("design", "design_review", item["workspace"].design_review()),
        )
        for stage, name, scope in scopes:
            events = [
                event
                for event in item["events"]
                if isinstance(event, dict) and event.get("stage") == name and event.get("status") == "ok"
            ]
            inputs.append(
                {
                    "workflow": item["workflow"],
                    "stage": stage,
                    "events": events,
                    "rounds": review_mod.recorded_rounds(scope),
                }
            )
    return inputs


def cmd_progress_record(args: argparse.Namespace) -> int:
    """Record a stage outcome so a loop that achieves nothing can be stopped."""
    book = _ledger(args)
    repeats = book.register_signature(args.stage, args.signature)
    allowed = int(book.settings.get("max_repeats_without_progress") or 0)
    stop = bool(allowed and repeats >= allowed)
    if args.json:
        _emit_json({"stage": args.stage, "repeats": repeats, "stop": stop})
    elif stop:
        _out(
            "%s has produced the same outcome %d times: stop and report, since "
            "retrying is not making progress." % (args.stage, repeats)
        )
    else:
        _out("%s outcome recorded (seen %d time(s))." % (args.stage, repeats))
    return 0
