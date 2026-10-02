"""The state, budget, token and summary commands."""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Optional

from . import jobs as jobs_mod
from . import ledger as ledger_mod
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
    if args.json:
        _emit_json(job)
        return 0
    _out(jobs_mod.render(job))
    if args.output and job.get("output_file"):
        _out("")
        _out(ws.read_text(str(job["output_file"])))
    return 0


def cmd_jobs_wait(args: argparse.Namespace) -> int:
    """Bounded wait: returning while the job runs is an outcome, not an error."""
    workspace = _workspace(args)
    if jobs_mod.read_job(workspace, args.job_id) is None:
        _err("no such job: %s" % args.job_id)
        return 2
    job = jobs_mod.wait(workspace, args.job_id, timeout=args.timeout, poll=args.poll)
    if args.json:
        _emit_json(job)
    else:
        _out(jobs_mod.render(job))
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
        return "{:,}".format(value) if value else "-"

    def tool(field: str) -> str:
        """Unreported prints ``-``; a measured zero prints ``0``.

        ``num`` renders both as ``-``, which is right for tokens -- a run that
        billed nothing did not happen -- and wrong here: a reviewer that opened
        no files is a result, and the finding this whole count exists to
        produce. Only ``tool_reported_runs`` separates the two.
        """
        if not int(account.get("tool_reported_runs") or 0):
            return "-"
        return "{:,}".format(int(account.get(field) or 0))

    runs = "%s/%s" % (account.get("measured_runs") or 0, account.get("runs") or 0)
    cost = float(account.get("cost_usd") or 0.0)
    return _TOKEN_ROW % (
        name,
        runs,
        num("input_tokens"),
        num("output_tokens"),
        num("total_tokens"),
        num("billed_tokens"),
        ("$%.4f" % cost) if cost else "-",
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

    totals = report["totals"]
    if not totals["runs"]:
        _out("No delegated runs recorded yet.")
        return 0

    header = ("stage", "meas.", "input", "output", "total", "billed", "cost", "tools", "tool out")
    _out(_TOKEN_ROW % header)
    for stage, account in sorted(report["by_stage"].items()):
        _out(_token_row(stage, account))
    _out(_token_row("ALL", totals))
    if report["by_label"]:
        _out("")
        _out("Per reviewer and tier:")
        for label, account in sorted(report["by_label"].items()):
            _out(_token_row(label, account))

    _out("")
    chars = int(totals.get("prompt_chars") or 0)
    if chars:
        _out(
            "Prompt text this repo composed: %s chars over %d run(s). "
            "That is the part it can shorten." % ("{:,}".format(chars), totals["runs"])
        )
    reported_tools = int(totals.get("tool_reported_runs") or 0)
    if reported_tools:
        _out(
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
        _out(
            "%d of %d run(s) predate tool counting and cannot say whether they used "
            "tools, so the tool columns leave them out. That is not the same as "
            "having used none." % (unknown_tools, totals["runs"])
        )
    if silent_tools:
        _out(
            "%d of %d run(s) reported no tool activity (Codex does not); the tool "
            "columns cover only the runs that did." % (silent_tools, totals["runs"])
        )
    if not report["complete"]:
        silent = int(totals["runs"]) - int(totals["measured_runs"])
        _out(
            "%d of %d run(s) reported no usage, so every total above is a floor, "
            "not a total." % (silent, totals["runs"])
        )
    if not report.get("priced", True):
        unpriced = int(totals["runs"]) - int(totals.get("priced_runs") or 0)
        _out(
            "%d of %d run(s) reported tokens but no cost, so the cost column is a "
            "floor even where the token counts are not. Comparing providers on it "
            "understates the ones that price nothing." % (unpriced, totals["runs"])
        )
    return 0


_OPT_ROW = "  %-22s %s"


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
        # A flat `.ai/` that has not been adopted yet, or nothing recorded.
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

    revisions = report["architect_revisions"]
    if (
        not report["rounds"]
        and not report["design_rounds"]
        and not report["design_refused"]
        and not revisions["attempts"]
    ):
        _out("No review rounds recorded in %s." % source)
        _out("Run a review, then ask again -- this reads what happened, not what would.")
        return 0
    if not report["rounds"] and not report["design_rounds"] and not report["design_refused"]:
        # Revisions alone: the review blocks below would all be zeroes.
        for line in _revision_rows(revisions):
            _out(line)
        return 0

    # Every row in this block describes a decision the level made, and no level
    # decides anything for a design round. So the block is code review's alone,
    # and is skipped rather than filled with zeroes when only design rounds
    # were recorded -- a `levels in force` of `-` invites the reader to conclude
    # the dial did nothing, when it was never asked.
    if report["rounds"]:
        _out(
            "Review rounds recorded: %d (%d ran, %d refused)"
            % (report["rounds"], report["ran"], report["refused"])
        )
        if report["refused"]:
            # Which of them refused, because the two mean different things to
            # act on: the gate says fix the tests, the limit says the change
            # is too big to review at all.
            _out(_OPT_ROW % ("refused by", _counts(report["refused_by"])))
        _out(_OPT_ROW % ("levels in force", _counts(report["levels"])))
        _out(_OPT_ROW % ("gate verdicts", _counts(report["gates"])))
        _out(_OPT_ROW % ("panel reduced", report["panel_reduced"]))
        conditional = report.get("conditional") or {}
        if any(conditional.values()):
            # Only once a log holds a conditional decision: a panel without
            # one would print a row of zeroes about a setting it never used.
            row = "added x%d, left out x%d" % (conditional.get("added", 0), conditional.get("left_out", 0))
            if conditional.get("declared_rounds"):
                row += ", declared with --high-risk x%d" % conditional["declared_rounds"]
            _out(_OPT_ROW % ("conditional reviewers", row))
        _out(_OPT_ROW % ("escalated (high risk)", report["escalated"]))
        if report["escalation_patterns"]:
            _out(_OPT_ROW % ("  caused by", _counts(report["escalation_patterns"])))
        if report["always_escalated"]:
            _out(
                "  every round escalated, so the level you configured never applied. "
                "Narrow optimization.high_risk_paths and optimization.extra_high_risk_paths, "
                "or accept that this repository reviews at quality."
            )
        _out("")
    total_runs = report["reviewer_runs"] + report["design_reviewer_runs"]
    total_reported = report["measured_runs"] + report["design_measured_runs"]
    total_billed = report["billed_tokens"] + report["design_billed_tokens"]
    _out(
        "Reviewer runs: %d (%d reported usage), %s billed"
        % (total_runs, total_reported, "{:,}".format(total_billed))
    )
    if report["design_rounds"]:
        # The total above was code review only, so the one command asked what
        # review cost answered with half of it: measured on one workflow,
        # `Reviewer runs: 8` beside four design runs and 350,429 billed tokens
        # that appeared nowhere. Split into rows rather than merged, because a
        # round against a plan and a round against a diff are not the same unit
        # of work and a per-round figure spanning both describes neither.
        if report["rounds"]:
            code_row = _runs_row(
                report["reviewer_runs"],
                report["measured_runs"],
                report["billed_tokens"],
                report["ran"],
                report["billed_per_round"],
            )
            _out(_OPT_ROW % ("code review", code_row))
        design_row = _runs_row(
            report["design_reviewer_runs"],
            report["design_measured_runs"],
            report["design_billed_tokens"],
            report["design_rounds"],
            report["design_billed_per_round"],
        )
        _out(_OPT_ROW % ("design review", design_row))
    elif report["billed_per_round"]:
        _out("  %s billed per round that ran" % "{:,}".format(report["billed_per_round"]))
    if report["tool_reported_runs"] or report["design_tool_reported_runs"]:
        _out("")
        _out("Tool activity, per run and only over the runs that reported it:")
        if report["tool_reported_runs"]:
            row = _tools_row(report, "", report["reviewer_runs"])
            _out(_OPT_ROW % ("code review", row))
        if report["design_tool_reported_runs"]:
            row = _tools_row(report, "design_", report["design_reviewer_runs"])
            _out(_OPT_ROW % ("design review", row))
        _out("  Divided by the runs that reported it, never by every reviewer run:")
        _out("  Codex reports none, and a mixed panel would otherwise halve the figure")
        _out("  for no reason but its composition.")
        _out("  Observed output is what the tools printed back, not source read: `wc -l`")
        _out("  returns 3 characters for a 200-line file and `cat` returns the file.")
    by_context = report.get("by_context") or {}
    # Only once a round has actually carried context: before that there is
    # nothing to compare, and a block of dashes would read as a finding.
    if (by_context.get("with") or {}).get("rounds"):
        _out("")
        _out("Surrounding context (review.context.surrounding), code review rounds only:")
        for name, label in (("with", "with context"), ("without", "without context")):
            group = by_context.get(name) or {}
            _out("  %-22s %s" % (label, _context_row(group, name == "with")))
            _out("  %-22s %s" % ("", _context_per_run_row(group)))
        _out("  The raw figures move with the size of each change and with the number of reviewers in the")
        _out("  panel; compare the per-run lines. Codex reports no tool activity, by design: its runs are in")
        _out("  the billed figures and out of the tool ones, which is why each tool figure names the runs it")
        _out("  was divided by.")
    paired = report.get("paired") or {}
    if paired.get("pairs_listed"):
        _out("")
        for line in _paired_rows(paired):
            _out(line)
    for line in _scorecard_rows(report["scorecard"]):
        _out(line)
    if revisions["attempts"]:
        _out("")
        for line in _revision_rows(revisions):
            _out(line)
    if report["design_refused"]:
        _out("")
        _out(
            "Design review rounds refused for size: %d -- nothing ran, and the plan and its"
            % report["design_refused"]
        )
        _out("request were over review.context.max_chars.")
    if report["estimated_saving"]:
        _out("")
        _out(
            "Estimated saving from %d gate-refused round(s): ~%s billed tokens."
            % (report["refused_by"].get("gate", 0), "{:,}".format(report["estimated_saving"]))
        )
        _out("An estimate: what a round that did not happen would have cost is")
        _out("unknowable, so this is the mean of the %d that did." % report["ran"])
    refused_for_size = report["refused_by"].get("context")
    if refused_for_size:
        _out("")
        _out("%d round(s) refused for size are not priced above: a change over" % refused_for_size)
        _out("review.context.max_chars was going to cost more than the mean, so")
        _out("charging it the mean would understate what was not spent.")
    if report["rounds_without_a_test_result"]:
        _out("")
        _out(
            "%d of %d round(s) ran with no test result recorded, so the gate had"
            % (report["rounds_without_a_test_result"], report["rounds"])
        )
        _out("nothing to act on and cannot have fired. Record one before `review run`:")
        _out("  dev-orchestra state record test ok|failed")
    return 0


def _revision_rows(revisions: Dict[str, Any]) -> List[str]:
    """The ``Architect revisions`` block: one line per group, then why runs went fresh."""
    lines = ["Architect revisions (cost against each workflow's initial design run):"]
    for name in ("resumed", "fresh"):
        group = revisions[name]
        failed = group["failed_attempts"]
        head = "%s: %d revisions (%d priced, %d with ratio)" % (
            name,
            group["runs"],
            group["priced_runs"],
            group["ratio_runs"],
        )
        if group["cost_ratio_mean"] is None:
            # The attempts still cost something even with no ratio to show.
            lines.append(
                "  %s cost ratio to initial n/a (initial run has no usable cost); %d stalled + %d rejected "
                "+ %d failed attempts (%d priced)"
                % (head, failed["stalled"], failed["rejected"], failed["failed"], failed["priced"])
            )
            continue
        per_completed = group["cost_per_completed_ratio"]
        lines.append(
            "  %s cost ratio to initial %.2f mean, %s per completed incl. %d stalled + %d rejected "
            "+ %d failed attempts (%d priced)"
            % (
                head,
                group["cost_ratio_mean"],
                "n/a" if per_completed is None else "%.2f" % per_completed,
                failed["stalled"],
                failed["rejected"],
                failed["failed"],
                failed["priced"],
            )
        )
    reasons = revisions["fallbacks"]["reasons"]
    if reasons:
        lines.append("  --resume ran fresh because:")
        for reason, count in sorted(reasons.items()):
            lines.append("    %s x%d" % (reason, count))
    return lines


def _counts(counter: Dict[str, int]) -> str:
    return ", ".join("%s x%d" % item for item in sorted(counter.items())) or "-"


def _figure(value: Any) -> str:
    """A per-run figure, or ``-`` where nobody reported one."""
    if value is None:
        return "-"
    return "{:,.1f}".format(value) if isinstance(value, float) else "{:,}".format(value)


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


def _usd(value: Optional[float]) -> str:
    # Two places where `tokens show` prints four: a per-accepted figure is read
    # as a price, and a fraction of a cent on it is noise to the reader.
    return "-" if value is None else "$%.2f" % value


_SCORECARD_OUTCOMES = ("reported", "accepted", "rejected", "duplicate", "open")

#: Under a stage whose recorded rounds were not all readable. The rounds that
#: were lost are not a random few: they are the early rounds, before the fixes,
#: so no correction is possible and the most that can be said is that the
#: figures lean -- not by how much, and not which way.
_SCORECARD_BIAS = (
    "  Reported and accepted are counted over the rounds whose report could be read; a round\n"
    "  with none is left out of every figure, its cost included. Before 0.11.0 only a workflow's\n"
    "  last round was kept, and the lost rounds reviewed the %s before its findings were fixed,\n"
    "  so what survives is not a sample of the whole: a rejection rate over it is biased, and the\n"
    "  direction of the bias is not known. Per-accepted figures are taken over the readable\n"
    "  rounds only; the unread rounds could move them either way."
)

_SCORECARD_FLOORS = (
    "  Cost totals are floors: a run that reported nothing adds nothing, and one that reported\n"
    "  tokens but no price (Codex) adds no dollars."
)

_SCORECARD_PAIRS = (
    "  %d round(s) %s a --surrounding pair: both runs' cost is in, and its findings and triage\n"
    "  are the second run's. See the paired block for what it measured."
)

_SCORECARD_ALONE = (
    "  Found alone is an upper bound on what dropping the reviewer would lose: a duplicate no\n"
    "  candidate link joined is counted as found alone.\n"
    "  Rates are printed from %d decided findings; below that the counts stand alone."
)

_SCORECARD_EFFORT = (
    "Review effort, code and design together: %d accepted over %d of %d recorded round(s); %s billed,"
)

#: Under the figure for both stages together. Added although the per-round
#: figures are not: those divide by a round, which is a different unit of work
#: for a plan and for a diff, and this divides by an accepted finding, which is
#: one defect the owner decided to fix whichever stage found it.
_SCORECARD_TOTAL = (
    "  Added here because both stages count the same thing, a finding the owner accepted; the\n"
    "  per-round figures above are not added, because a round of each is a different unit of\n"
    "  work. Read it beside the two stage figures: the mix of stages moves it, and it is taken\n"
    "  over the readable rounds only."
)


def _scorecard_counts(group: Dict[str, Any], alone: bool) -> str:
    counts = tuple(int(group.get(field) or 0) for field in _SCORECARD_OUTCOMES)
    row = "%d reported: %d accepted, %d rejected, %d duplicate, %d open" % counts
    if alone:
        row += "; %d found alone (%d accepted)" % (
            int(group.get("alone") or 0),
            int(group.get("alone_accepted") or 0),
        )
    return row


def _scorecard_spend(group: Dict[str, Any], panel: bool) -> str:
    """Runs and what they cost; the panel says how many of its runs were priced."""
    runs = int(group.get("runs") or 0)
    row = "%d run(s)" % runs
    if group.get("failed_runs"):
        row += " (%d failed)" % int(group["failed_runs"])
    billed = int(group.get("billed_tokens") or 0)
    if not group.get("measured_runs") and not billed:
        return row + ", nothing reported"
    row += ", %s billed" % "{:,}".format(billed)
    priced = int(group.get("priced_runs") or 0)
    if not priced:
        return row + ", no cost reported"
    if panel:
        return row + ", %s over %d of %d run(s)" % (_usd(group.get("cost_usd")), priced, runs)
    return row + ", %s over %d priced run(s)" % (_usd(group.get("cost_usd")), priced)


def _scorecard_per_accepted(group: Dict[str, Any]) -> str:
    per = group.get("billed_per_accepted")
    if per is None:
        return "per accepted withheld under %d accepted" % opt_report.SCORECARD_MIN_DECIDED
    cost = group.get("cost_per_accepted")
    if cost is None:
        # Said, not left out: a reviewer that prices nothing did not find its
        # findings for free, and a missing column reads as if it had.
        return "%s billed per accepted, $ -" % "{:,}".format(per)
    return "%s billed / %s per accepted" % ("{:,}".format(per), _usd(cost))


def _scorecard_rates(group: Dict[str, Any]) -> str:
    rate = group.get("rejection_rate")
    if rate is None:
        # Accepted is a part of decided, so under this threshold both are.
        return "rates withheld under %d decided" % opt_report.SCORECARD_MIN_DECIDED
    return "%.0f%% rejected, %s" % (rate * 100, _scorecard_per_accepted(group))


def _scorecard_cost_row(group: Dict[str, Any], panel: bool) -> str:
    """The second line of a scorecard row: what the runs cost, and the rates."""
    return "%s; %s" % (_scorecard_spend(group, panel), _scorecard_rates(group))


def _scorecard_rows(scorecard: Dict[str, Any]) -> List[str]:
    """``optimization report``'s scorecard: one block per stage, then both together.

    A stage with no report to read is left out rather than printed as zeroes,
    which would read as a panel that found nothing.
    """
    lines: List[str] = []
    for stage, label, subject in (("code", "code review", "code"), ("design", "design review", "plan")):
        block = scorecard.get(stage) or {}
        read = int(block.get("rounds_read") or 0)
        if not read:
            continue
        recorded = int(block.get("rounds_recorded") or 0)
        unreviewed = int(block.get("rounds_unreviewed") or 0)
        head = "Reviewer scorecard, %s: %d of %d recorded round(s)" % (label, read, recorded)
        head += " had a report to read"
        if unreviewed:
            head += "; %d round(s) no reviewer reviewed" % unreviewed
        lines.extend(["", head + "."])
        for name, group in sorted((block.get("reviewers") or {}).items()):
            lines.append(_OPT_ROW % (name, _scorecard_counts(group, True)))
            cost_row = _scorecard_cost_row(group, False)
            if group.get("when"):
                cost_row += " (when: %s; left out of %d round(s))" % (
                    group["when"],
                    int(group.get("left_out_rounds") or 0),
                )
            lines.append(_OPT_ROW % ("", cost_row))
        panel = block.get("panel") or {}
        lines.append(_OPT_ROW % ("panel", _scorecard_counts(panel, False)))
        lines.append(_OPT_ROW % ("", _scorecard_cost_row(panel, True)))
        if recorded - read - unreviewed > 0:
            lines.extend((_SCORECARD_BIAS % subject).splitlines())
        lines.extend(_SCORECARD_FLOORS.splitlines())
        rerun = int(block.get("rerun_rounds") or 0)
        if rerun:
            lines.extend((_SCORECARD_PAIRS % (rerun, "was" if rerun == 1 else "were")).splitlines())
        lines.extend((_SCORECARD_ALONE % opt_report.SCORECARD_MIN_DECIDED).splitlines())
    if not lines:
        return lines
    total = scorecard.get("total") or {}
    priced = int(total.get("priced_runs") or 0)
    cost = "no cost reported"
    if priced:
        cost = "%s over %d priced run(s)" % (_usd(total.get("cost_usd")), priced)
    counts = (
        int(total.get("accepted") or 0),
        int(total.get("rounds_read") or 0),
        int(total.get("rounds_recorded") or 0),
        "{:,}".format(int(total.get("billed_tokens") or 0)),
    )
    lines.append("")
    lines.append(_SCORECARD_EFFORT % counts)
    lines.append("  %s; %s" % (cost, _scorecard_per_accepted(total)))
    lines.extend(_SCORECARD_TOTAL.splitlines())
    return lines


def _paired_rows(paired: Dict[str, Any]) -> List[str]:
    """``optimization report``'s block of runs paired on one snapshot."""
    lines = [
        "Paired on one snapshot (--surrounding none vs enclosing: the same frozen diff, tree, panel "
        "and prompt inputs):"
    ]
    for pair in paired.get("pairs") or []:
        head = "  %s" % str(pair.get("snapshot") or "")[:12]
        if pair.get("workflow"):
            head += " in %s" % pair["workflow"]
        head += "   change %s chars; panel %s; %s context chars adopted (%s as carried), %s left out" % (
            _figure(pair.get("change_chars")),
            _panel_names(pair.get("panel") or []),
            "{:,}".format(int(pair.get("adopted_chars") or 0)),
            "{:,}".format(int(pair.get("context_chars") or 0)),
            "{:,}".format(int(pair.get("trimmed_chars") or 0)),
        )
        for reason in _pair_exclusions(pair):
            head += "; " + reason
        lines.append(head)
        for name in ("with", "without"):
            side = pair.get(name) or {}
            lines.append(
                "      %-8s %d run(s), %s billed, %s per run; %s use(s)/run, %s observed output chars/run "
                "(%d of %d run(s) reported)"
                % (
                    name + ":",
                    int(side.get("reviewer_runs") or 0),
                    "{:,}".format(int(side.get("billed_tokens") or 0)),
                    _figure(side.get("billed_per_run")),
                    _figure(side.get("tool_uses_per_run")),
                    _figure(side.get("tool_output_chars_per_run")),
                    int(side.get("tool_reported_runs") or 0),
                    int(side.get("reviewer_runs") or 0),
                )
            )
        delta = pair.get("delta") or {}
        lines.append(
            "      delta:   %s billed/run, %s use(s)/run, %s observed output chars/run"
            % (
                _figure(delta.get("billed_per_run")),
                _figure(delta.get("tool_uses_per_run")),
                _figure(delta.get("tool_output_chars_per_run")),
            )
        )
    counted = int(paired.get("pairs_total") or 0)
    ours, theirs, delta = paired.get("with") or {}, paired.get("without") or {}, paired.get("delta") or {}
    fields = ("billed_per_run", "tool_uses_per_run", "tool_output_chars_per_run")
    lines.append(
        "  total, %d pair(s) counted   with: %s billed/run, %s use(s)/run, %s observed output chars/run; "
        "without: %s; delta: %s"
        % (
            counted,
            _figure(ours.get(fields[0])),
            _figure(ours.get(fields[1])),
            _figure(ours.get(fields[2])),
            ", ".join(_figure(theirs.get(field)) for field in fields),
            ", ".join(_figure(delta.get(field)) for field in fields),
        )
    )
    lines.extend(
        [
            "  %d pair(s) is a small-sample observation, not a statistical result. A counted" % counted,
            "  pair holds the change, the tree, the panel, the delivered reviews and the other prompt",
            "  inputs equal; what it does not hold equal is the reviewers' own run-to-run variation, so",
            "  one pair says what happened once. Codex reports no tool activity, so the tool figures",
            "  are over the runs that reported them. A pair is listed but left out of the total when",
            '  its panels differ ("panels differ"), when a reviewer run on either side did not deliver',
            '  ("not delivered"), when the two runs had different prompt inputs ("inputs differ"), or',
            '  when the enclosing run adopted nothing ("nothing adopted").',
        ]
    )
    return lines


def _panel_names(panel: List[Dict[str, Any]]) -> str:
    names = []
    for entry in panel:
        details = (entry.get("provider"), entry.get("model") or "?", entry.get("role") or "?")
        names.append("%s (%s, %s, %s)" % (entry.get("id"), *details))
    return ", ".join(names)


def _pair_exclusions(pair: Dict[str, Any]) -> List[str]:
    """Why a listed pair is left out of the total, one clause per reason."""
    reasons = []
    if not pair.get("same_panel"):
        reasons.append("panels differ (without: %s)" % _panel_names(pair.get("without_panel") or []))
    if not pair.get("delivered"):
        undelivered = pair.get("undelivered") or []
        names = ["%s: %s %s" % (run.get("side"), run.get("id"), run.get("status")) for run in undelivered]
        reasons.append("not delivered (%s)" % ", ".join(names))
    if not pair.get("same_inputs"):
        reasons.append("inputs differ (%s)" % ", ".join(pair.get("inputs_differ") or []))
    if pair.get("nothing_adopted"):
        reasons.append("nothing adopted")
    return reasons


def _context_row(group: Dict[str, Any], adopted: bool) -> str:
    """One group's raw figures: rounds, runs, billed and tool activity."""
    row = "%d round(s), %d run(s), %s billed, %s per round" % (
        int(group.get("rounds") or 0),
        int(group.get("reviewer_runs") or 0),
        "{:,}".format(int(group.get("billed_tokens") or 0)),
        _figure(group.get("billed_per_round")),
    )
    row += "; %s use(s)/run, %s observed output chars/run (%d of %d run(s) reported)" % (
        _figure(group.get("tool_uses_per_run")),
        _figure(group.get("tool_output_chars_per_run")),
        int(group.get("tool_reported_runs") or 0),
        int(group.get("reviewer_runs") or 0),
    )
    if adopted:
        row += "; %s context chars adopted, %s left out" % (
            "{:,}".format(int(group.get("adopted_chars") or 0)),
            "{:,}".format(int(group.get("trimmed_chars") or 0)),
        )
    return row


def _context_per_run_row(group: Dict[str, Any]) -> str:
    """The same group per run and per 1k chars of change -- the line to compare."""
    return (
        "per run and 1k chars of change (%d sized round(s), %s chars): %s billed over %d billed run(s), "
        "%s observed output chars over %d reporting run(s)"
        % (
            int(group.get("sized_rounds") or 0),
            "{:,}".format(int(group.get("change_chars") or 0)),
            _figure(group.get("billed_per_run_per_1k_change_chars")),
            int(group.get("sized_billed_runs") or 0),
            _figure(group.get("tool_output_chars_per_run_per_1k_change_chars")),
            int(group.get("sized_tool_runs") or 0),
        )
    )


def _runs_row(runs: int, reported: int, billed: int, rounds: int, per_round: Optional[int]) -> str:
    """One stage's share of the reviewer spend, for a row under the total."""
    row = "%d (%d reported usage), %s billed over %d round(s)" % (
        runs,
        reported,
        "{:,}".format(billed),
        rounds,
    )
    if per_round:
        row += ", %s each" % "{:,}".format(per_round)
    return row


def _tools_row(report: Dict[str, Any], prefix: str, runs: int) -> str:
    """One stage's tool activity, with the denominator it was divided by.

    The denominator is printed because it is the part that can mislead: "6.5
    uses/run" over half a panel is a different claim from the same figure over
    all of it, and only the count says which.
    """
    return "%s use(s)/run, %s observed output chars/run (%d of %d run(s) reported)" % (
        report["%stool_uses_per_run" % prefix],
        "{:,.1f}".format(report["%stool_output_chars_per_run" % prefix]),
        report["%stool_reported_runs" % prefix],
        runs,
    )


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
