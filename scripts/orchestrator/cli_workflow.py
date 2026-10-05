"""The workflow, design approval, jobs and optimization report commands."""

from __future__ import annotations

import argparse
import os
import shutil
from typing import Any, Dict, List, NamedTuple, Optional, cast

from . import approval as approval_mod
from . import config as config_mod
from . import ledger as ledger_mod
from . import optimization as opt_mod
from . import optimization_report as opt_report
from . import review as review_mod
from . import workflow as workflow_mod
from . import workspace as ws
from .cli_common import _container, _emit_json, _err, _ledger, _load_lenient, _out, _workspace, _wrote_plan
from .cli_review import (
    _approved_as_recorded,
    _code_final_pass,
    _design_decision,
    _design_final_pass,
    _design_lineage,
    _design_round_plan,
    _left_out_suffix,
    _ran_since_last_round,
    _reviewed_something,
    _round_plan,
)
from .summary import ROLE_TITLES

# --------------------------------------------------------------------------- workflow


def _workflow_warning(workspace: ws.Workspace) -> List[str]:
    """Other workflows that look live in this same working tree.

    Separate directories separate the reports, not the files being reported
    on: there is one working tree here and `git diff` reads all of it. Saying
    so is the whole point -- the layout would otherwise suggest an isolation it
    cannot provide.
    """
    if not workspace.workflow:
        return []
    others = workflow_mod.active_elsewhere(workspace.container, workspace.workflow)
    if not others:
        return []
    return [
        "Another workflow is active in this working tree: %s" % ", ".join(others),
        "Artifacts are separate; the files under review are not. For work that"
        " really runs in parallel, give each workflow its own worktree"
        " (git worktree add ../name branch).",
    ]


def cmd_workflow_list(args: argparse.Namespace) -> int:
    _, container = _container(args)
    entries = workflow_mod.listing(container)
    current, _ = workflow_mod.resolve(container, getattr(args, "workflow", "") or "")
    if args.json:
        _emit_json({"current": current, "workflows": entries})
        return 0
    if not entries:
        _out("No workflows recorded yet in %s" % container)
        return 0
    for entry in entries:
        marks = []
        if entry["workflow"] == current:
            marks.append("current")
        if entry["in_flight"]:
            marks.append("in flight: %s" % ", ".join(entry["in_flight"]))
        _out(
            "%s  %s  runs=%d  %s%s"
            % (
                entry["workflow"],
                entry["updated_at"] or entry["started_at"] or "-",
                entry["runs"],
                ("%s/%s" % (entry["last_stage"], entry["last_status"])) if entry["last_stage"] else "-",
                ("  [%s]" % "; ".join(marks)) if marks else "",
            )
        )
    return 0


def cmd_workflow_show(args: argparse.Namespace) -> int:
    root, container = _container(args)
    try:
        workflow, origin = workflow_mod.resolve(container, getattr(args, "workflow", "") or "")
    except workflow_mod.WorkflowError as exc:
        _err(str(exc))
        return 2
    others = workflow_mod.active_elsewhere(container, workflow)
    payload = {
        "workflow": workflow,
        "origin": origin,
        "dir": workflow_mod.workflow_dir(container, workflow),
        "container": container,
        "root": root,
        "also_active": others,
    }
    if args.json:
        _emit_json(payload)
        return 0
    _out("Workflow: %s (from: %s)" % (workflow, origin))
    _out("Artifacts: %s" % payload["dir"])
    if others:
        for line in _workflow_warning(ws.Workspace(root, container, workflow)):
            _out(line)
    return 0


def cmd_workflow_use(args: argparse.Namespace) -> int:
    """Pin an id for callers whose host exports no session of its own."""
    _, container = _container(args)
    try:
        workflow = workflow_mod.normalise(args.id)
    except workflow_mod.WorkflowError as exc:
        _err(str(exc))
        return 2
    workflow_mod.write_pointer(container, workflow, "requested")
    _out("This directory now defaults to workflow %s" % workflow)
    if workflow_mod.session_id():
        _err(
            "Note: this session exports a session id, which wins over the"
            " pointer. Pass --workflow %s, or set %s, to override it." % (workflow, workflow_mod.WORKFLOW_ENV)
        )
    return 0


def cmd_workflow_remove(args: argparse.Namespace) -> int:
    """Delete one workflow's artifacts. The reports are work; ask first."""
    _, container = _container(args)
    try:
        workflow = workflow_mod.normalise(args.id)
    except workflow_mod.WorkflowError as exc:
        _err(str(exc))
        return 2
    directory = workflow_mod.workflow_dir(container, workflow)
    if not os.path.isdir(directory):
        _err("No such workflow: %s" % workflow)
        return 1
    if not args.yes:
        _err("Refusing to delete %s without --yes" % directory)
        return 2
    current, _ = workflow_mod.resolve(container, getattr(args, "workflow", "") or "")
    if workflow == current:
        _err("Refusing to delete the workflow this session is in (%s)" % workflow)
        return 2
    shutil.rmtree(directory)
    _out("Removed %s" % directory)
    return 0


#: What each ``refused_by`` means to somebody reading the final report, since
#: the two ask for different things: fix the tests, or make the change smaller.
_REFUSAL_CAUSE = {
    "gate": "tests recorded as failing",
    "context": "change over review.context.max_chars",
}


def _context_refusal(events: List[Dict[str, Any]], stage: str) -> Optional[Dict[str, Any]]:
    """The context refusal ``stage`` is still sitting on, if it is.

    Outstanding means: refused for size, and nothing has reviewed the change
    since. A refusal deliberately leaves the previous round's consolidation in
    place -- that is how its triage survives -- so without this, an oversized
    change refused today still answers ``continue`` from a clean review of
    something else, which was the hole this closes.

    Only a round that actually reviewed something supersedes it, because only
    that round's own coverage can say where the change stands afterwards. It
    is emphatically not "the last thing that happened to this stage": a stage
    collects entries nobody reviewed anything for. ``status`` itself writes
    one, calling ``clear_stalls`` before it reads these events, so a killed
    round left in flight would otherwise clear the very refusal this call is
    looking for.

    Nor does a refusal supersede a refusal. Two refused rounds mean nothing
    was reviewed twice -- a gate refusal after a size refusal, then a passing
    test run, still leaves the oversized change unread -- which is more reason
    to stop and report, not less.
    """
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("stage") != stage:
            continue
        if _reviewed_something(event):
            return None
        if event.get("status") == opt_mod.REFUSED and event.get("refused_by") == "context":
            return event
    return None


def _refusal_reason(event: Dict[str, Any], design: bool = False) -> str:
    """One refusal, in the words the final report has to use.

    The size and the limit come from the event because a refused round writes
    no consolidation to read them from -- that is the whole point of it.
    """
    context = event.get("context") or {}
    chars = context.get("chars")
    limit = context.get("max_chars")
    return (
        "the last %s round was refused: the change body (%s chars) is over "
        "review.context.max_chars (%s), so nothing was reviewed"
        % (
            "design review" if design else "review",
            "{:,}".format(chars) if isinstance(chars, int) else "size unrecorded",
            "{:,}".format(limit) if isinstance(limit, int) else "the limit",
        )
    )


def _approval_line(
    info: Dict[str, Any],
    plan_relative: str,
    design_pass: Optional[Dict[str, Any]] = None,
    architect_left: Optional[int] = None,
) -> str:
    """The `Plan approval:` line of `status`, worded as what to do next."""
    line = _approval_advice(info, plan_relative, design_pass or {})
    # Not a stop: nothing needs the architect until the user asks for a
    # change, and then `run architect` refuses and says so.
    if info["pending"] and architect_left == 0 and (design_pass or {}).get("state") != "blocked":
        line += " (no architect attempt is left for changes; `budget reset architect` first)"
    return line


def _approval_advice(info: Dict[str, Any], plan_relative: str, design_pass: Dict[str, Any]) -> str:
    state = info["state"]
    final = design_pass.get("state")
    open_findings = ", ".join(info.get("open_findings") or [])
    if info["pending"] and info.get("design_review_exhausted") and final == "pending":
        return (
            "%s -- the design review budget is spent; fold the accepted findings (%s) into %s first "
            "(review fix-brief --design, then run architect) without re-reviewing, then present the "
            "plan and ask" % (state, open_findings, plan_relative)
        )
    if info["pending"] and final == "done" and design_pass.get("plan_changed") is True:
        return (
            "%s -- present %s to the user with %s still open from a review of an earlier revision "
            "(the design review budget is spent, so this revision is not re-reviewed) and ask; a yes "
            "is recorded with `design approve`, never without one" % (state, plan_relative, open_findings)
        )
    if info["pending"] and final == "done" and design_pass.get("plan_changed") is False:
        return (
            "%s -- the architect left %s unchanged over %s (the design review budget is spent); "
            "present the plan and the findings and ask whether to approve over them (`design approve`) "
            "or to triage them again" % (state, plan_relative, open_findings)
        )
    if info["pending"] and final == "blocked":
        return (
            "%s -- the design review budget is spent with %s open and no architect attempt is left to "
            "fold them in; report them and ask the user whether to approve over them (`design approve`) "
            "or to free an attempt (`budget reset`) and revise" % (state, open_findings)
        )
    if info["pending"] and info.get("design_review_exhausted"):
        # A spent design review is a stop-and-report, and the report has to
        # end in the question or the orchestrator reads the stop as the end.
        return (
            "%s -- the design review budget is spent with %s open; report them and ask the user "
            "whether to approve over them (`design approve`) or to revise"
            % (state, ", ".join(info.get("open_findings") or []))
        )
    if state == "pending":
        return (
            "pending -- present %s to the user and ask; a yes is recorded with `design approve`, "
            "never without one" % plan_relative
        )
    if state == "stale" and info.get("stale_reason") == "plan-changed":
        return "stale -- the plan changed after it was approved (%s -> %s); present it again and ask" % (
            str(info.get("approved_sha256") or "")[:12],
            str(info.get("plan_sha256") or "")[:12],
        )
    if state == "stale":
        return "stale -- a design review ran after the plan was approved; present its findings and ask again"
    if state == "approved":
        return "approved (%s)" % str(info.get("approved_sha256") or "")[:12]
    if state == "not-required":
        return "not required (design.require_approval: false)"
    if state == "implemented-unapproved":
        return (
            "not recorded; the implementer already ran on this plan (this gate is newer than the "
            "workflow) -- nothing to ask unless it is to run again, which needs `design approve`"
        )
    return "no plan"


class _Status(NamedTuple):
    """The `status` verdict: the `--json` payload, and what only the text view reads."""

    payload: Dict[str, Any]
    plan: opt_mod.Plan
    design_decision: opt_mod.DesignDecision
    design_pass: Dict[str, Any]
    architect_left: Optional[int]


def cmd_status(args: argparse.Namespace) -> int:
    """One verdict the orchestrator can act on: continue, or stop and report."""
    loaded = _load_lenient(args.cwd)
    workspace = _workspace(args)
    book = _ledger(args, workspace)
    status = _status_payload(loaded, workspace, book)
    if args.json:
        _emit_json(status.payload)
        return 0
    warning = _workflow_warning(workspace)
    for line in _status_lines(status, workspace.relative(workspace.plan_path), warning):
        _out(line)
    return 0


def _status_payload(
    loaded: config_mod.LoadedConfig, workspace: ws.Workspace, book: ledger_mod.Ledger
) -> _Status:
    """Everything the verdict is built from, read once; nothing is printed."""
    abandoned = book.clear_stalls()

    review_data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    settings = loaded.review_settings()
    severities = tuple(settings.get("re_review_severities") or ("critical", "high"))
    blocking = review_mod.unresolved_blocking(review_data, severities)
    iteration = int(review_data.get("iteration", 0) or 0)
    max_iterations = int(settings.get("max_review_iterations", 2))
    summary = book.summary()

    # The design review is read whether or not it is enabled: a round that was
    # run by hand, or left open when the setting was turned back off, is still
    # an open finding about the plan the implementation would follow.
    design_data = ws.read_json(workspace.design_review().consolidated_json_path, {}) or {}
    design_settings = loaded.design_review_settings()
    design_blocking = review_mod.unresolved_blocking(design_data, severities)
    design_iteration = int(design_data.get("iteration", 0) or 0)
    design_max = int(design_settings.get("max_iterations", 2))
    design_decision = _design_decision(loaded, workspace)
    design_exhausted = bool(design_blocking) and design_iteration >= design_max
    approval_info = approval_mod.current(workspace, bool(loaded.design_settings().get("require_approval")))
    # The same list `design approve` names, whatever the severity: the user is
    # asked about what the approval will record, not only what blocks.
    design_open = [str(f.get("id")) for f in approval_mod.open_findings(design_data)]
    plan_text, plan_digest = approval_mod.read_plan(workspace)
    of_current_plan = approval_mod.findings_of_current_plan(workspace, plan_text)
    approval_info["open_findings"] = design_open
    approval_info["open_findings_of_current_plan"] = of_current_plan if design_open else None
    approval_info["design_review_exhausted"] = design_exhausted

    events = [event for event in (workspace.read_state().get("events") or []) if isinstance(event, dict)]
    refused_for_size = _context_refusal(events, "review")
    design_refused_for_size = _context_refusal(events, "design_review")

    # The round that reached the limit still gets its revision and its fix;
    # the limit refuses only the re-review. Until those are done the spent
    # budget is not a stop.
    architect_left = summary["budgets"].get("architect", {}).get("remaining")
    plan_approved = _approved_as_recorded(approval_info)
    design_accepted = bool(review_mod.accepted_findings(design_data))
    design_pass = _design_final_pass(
        design_blocking,
        iteration=design_iteration,
        max_iterations=design_max,
        of_current_plan=of_current_plan,
        ran_since=_ran_since_last_round(events, "design_review", "architect", counts=_wrote_plan(workspace))[
            0
        ],
        architect_left=architect_left,
        approved=plan_approved,
        implemented=approval_mod.implemented_since_plan(workspace, events),
        accepted=design_accepted,
    )
    fixed, retested = _ran_since_last_round(events, "review", "review_fixer", ("test", "re-test"))
    review_pass = _code_final_pass(
        blocking,
        iteration=iteration,
        max_iterations=max_iterations,
        fixed_since=fixed,
        retested_since=retested,
        fixer_left=summary["budgets"].get("review_fixer", {}).get("remaining"),
        accepted=bool(review_mod.accepted_findings(review_data)),
    )
    review_repeats = int((summary["signatures"] or {}).get("review") or 0)
    design_repeats = int((summary["signatures"] or {}).get("design_review") or 0)

    reasons: List[str] = []
    if refused_for_size:
        reasons.append(_refusal_reason(refused_for_size))
    if design_refused_for_size:
        reasons.append(_refusal_reason(design_refused_for_size, design=True))
    if review_pass["state"] in ("done", "blocked", "unaccepted"):
        suffix = ""
        if review_pass["state"] == "blocked":
            suffix = " and no review_fixer attempt left"
        elif review_pass["state"] == "done":
            suffix = "; fixed and re-tested after the last round, not re-reviewed -- report"
        reasons.append(
            "review budget spent (%d/%d rounds) with %d finding(s) still open%s"
            % (iteration, max_iterations, len(blocking), suffix)
        )
    # A repeat does not stop the fix still owed to the last round.
    if review_repeats > 1 and review_pass["state"] not in ("pending", "retest"):
        reasons.append("the last review round found exactly what the previous one found")
    # Left out once the user has approved this plan (`approved`): they were
    # shown the open findings and decided to go ahead over them, which is what
    # the stop was waiting for. Kept, it would say stop at every stage that
    # follows. Left out too while the last round's revision is still owed.
    if design_pass["state"] in ("done", "blocked", "implemented", "unaccepted"):
        suffix = ""
        if design_pass["state"] == "blocked":
            suffix = " and no architect attempt left to fold them in"
        elif design_pass["state"] == "done" and design_pass["plan_changed"] is True:
            suffix = "; revised after the last round, not re-reviewed -- present the plan and ask"
        elif design_pass["state"] == "done" and design_pass["plan_changed"] is False:
            suffix = (
                "; the architect left the plan unchanged after the last round -- present the plan "
                "and the findings and ask"
            )
        reasons.append(
            "design review budget spent (%d/%d rounds) with %d finding(s) still open%s"
            % (design_iteration, design_max, len(design_blocking), suffix)
        )
    if design_repeats > 1 and design_pass["state"] != "pending":
        reasons.append("the last design review round found exactly what the previous one found")
    for stage, entry in summary["budgets"].items():
        if entry["remaining"] != 0:
            continue
        # With a plan written, the architect is needed again only for a
        # revision a design round still owes before its limit: at the limit
        # the revision says so in its own reason above, an approved plan is
        # not revised, and a change asked for at approval is refused by
        # `run architect`.
        if (
            stage == "architect"
            and plan_digest
            and (design_pass["state"] is not None or not design_blocking or plan_approved)
        ):
            continue
        # A fix already made needs its re-test or its report, not the fixer.
        if stage == "review_fixer" and review_pass["state"] in ("retest", "done"):
            continue
        reasons.append("%s has no attempts left" % stage)
    total = summary["total_delegated_runs"]
    if total["limit"] and total["used"] >= int(total["limit"]):
        reasons.append("no delegated runs left in this workflow")
    # Asked of the ledger rather than of the rounded summary key: advice that
    # says stop while `run` still goes is the mismatch this budget exists to
    # remove, and half a second of remaining runtime is enough to cause it.
    if book.runtime_refusal():
        reasons.append("the delegated runtime budget is spent")

    # What the next `review run` would decide, so the orchestrator finds out
    # here rather than by being refused. Cheap: the snapshot meta is already
    # on disk, and nothing is delegated to work it out.
    meta = workspace.read_snapshot_meta()
    plan = _round_plan(loaded, settings, workspace, meta, loaded.reviewers())
    if plan.gate == opt_mod.GATE_REFUSE:
        reasons.append("the last recorded test run failed; fix it before reviewing")
    # And what the next `review run --design` would decide about its panel.
    design_plan = _design_round_plan(
        loaded, workspace, loaded.design_reviewers(), lineage=_design_lineage(workspace, book)
    )

    payload = {
        "verdict": "stop-and-report" if reasons else "continue",
        "reasons": reasons,
        "stalls": summary["stalls"],
        "abandoned_stages": abandoned,
        "in_flight": summary["in_flight"],
        "review": {
            "iteration": iteration,
            "max_review_iterations": max_iterations,
            "blocking": [f["id"] for f in blocking],
            "accepted": len(review_mod.accepted_findings(review_data)),
            # The refusal itself, not a flag: the size and the limit are what
            # the report has to name, and they are not in the consolidation --
            # a refused round writes no consolidation at all.
            "refused_for_size": (refused_for_size or {}).get("context") or None,
            # The ledger's repeat count, named as the run-log event names it.
            "identical_rounds": review_repeats,
            "final_fix": review_pass["state"],
            "final_fix_pending": review_pass["pending"],
        },
        "design_review": {
            # Whether the stage runs: `on`, or `auto` and this plan calls for it.
            "enabled": design_decision.run,
            "mode": design_decision.mode,
            "reason": design_decision.reason or None,
            "iteration": design_iteration,
            "max_iterations": design_max,
            "blocking": [f["id"] for f in design_blocking],
            "accepted": len(review_mod.accepted_findings(design_data)),
            "refused_for_size": (design_refused_for_size or {}).get("context") or None,
            "identical_rounds": design_repeats,
            "final_revision": design_pass["state"],
            "final_revision_pending": design_pass["pending"],
            "optimization": design_plan.to_dict(),
        },
        # Not a reason and not a verdict: `run implementer` enforces it, and a
        # stop-and-report here would read as "give up" where the answer is to
        # ask the user.
        "design_approval": approval_info,
        "budgets": summary["budgets"],
        "total_delegated_runs": total,
        "runtime_remaining_seconds": summary["runtime_remaining_seconds"],
        "runtime": summary["runtime"],
        # Reported, never enforced: no verdict here turns on what a run cost.
        "tokens": summary["tokens"],
        "optimization": plan.to_dict(),
        "workflow": workspace.workflow,
        # Named here because status is the command the orchestrator consults
        # before every stage: if another session is working in this tree, that
        # is the moment to know, not after two reviews disagree about what the
        # change even is.
        "also_active": workflow_mod.active_elsewhere(workspace.container, workspace.workflow)
        if workspace.workflow
        else [],
    }
    return _Status(payload, plan, design_decision, design_pass, architect_left)


def _status_lines(status: _Status, plan_relative: str, warning: List[str]) -> List[str]:
    """The text view of `status`, read from the payload wherever it holds the value.

    ``warning`` is what ``_workflow_warning`` said about this working tree.
    """
    payload = status.payload
    plan = status.plan
    review = payload["review"]
    design = payload["design_review"]
    lines = ["Verdict: %s" % payload["verdict"].upper()]
    for reason in payload["reasons"]:
        lines.append("  - %s" % reason)
    for line in warning:
        lines.append("  ! %s" % line)
    if payload["stalls"]:
        lines.append("")
        lines.append("Stalled stages:")
        for stall in payload["stalls"]:
            lines.append(
                "  %s started %s (%.0fs ago) -- %s"
                % (stall["stage"], stall["started_at"], stall["elapsed_seconds"], stall["reason"])
            )
    abandoned = payload["abandoned_stages"]
    if abandoned:
        lines.append("")
        lines.append("Cleared %d stage(s) whose process is gone: %s" % (len(abandoned), ", ".join(abandoned)))
    if payload["in_flight"]:
        lines.append("")
        lines.append("In flight:")
        for token, entry in payload["in_flight"].items():
            lines.append("  %s (%s) since %s" % (entry.get("stage"), token, entry.get("started_at")))
    lines.append("")
    line = "Review: round %d/%d, %d accepted, %d blocking" % (
        review["iteration"],
        review["max_review_iterations"],
        review["accepted"],
        len(review["blocking"]),
    )
    if review["final_fix"] == "pending":
        line += " -- final fix pending (fix, re-test, do not re-review)"
    elif review["final_fix"] == "retest":
        line += " -- final fix done, re-test pending (record it, do not re-review)"
    if review["final_fix"] in ("pending", "retest") and review["identical_rounds"] > 1:
        line += "; identical to the previous round"
    lines.append(line)
    line = "Design review: %s, round %d/%d, %d accepted, %d blocking" % (
        status.design_decision.label(),
        design["iteration"],
        design["max_iterations"],
        design["accepted"],
        len(design["blocking"]),
    )
    if design["final_revision_pending"]:
        line += " -- final revision pending (fold the findings in, do not re-review)"
        if design["identical_rounds"] > 1:
            line += "; identical to the previous round"
    line += _left_out_suffix((design.get("optimization") or {}).get("conditional") or [])
    lines.append(line)
    lines.append(
        "Plan approval: %s"
        % _approval_line(
            payload["design_approval"],
            plan_relative,
            status.design_pass,
            status.architect_left,
        )
    )
    line = "Optimization: %s" % plan.level
    if plan.escalated:
        line += " (escalated from %s -- %s)" % (plan.requested, plan.escalation_note().split(": ", 1)[-1])
    line += ", tests %s" % (plan.test_status or "not recorded")
    if plan.reviewer_limit is not None:
        line += ", %d reviewer" % plan.reviewer_limit
    for record in plan.conditional:
        line += "; %s %s (%s)" % (record["id"], "added" if record["runs"] else "left out", record["reason"])
    lines.append(line)
    tokens = payload["tokens"]["totals"]
    if tokens["runs"]:
        lines.append(
            "Tokens: %s billed over %d run(s)%s (dev-orchestra tokens show)"
            % (
                ws.fmt_int(int(tokens["billed_tokens"] or 0)) or "0",
                tokens["runs"],
                "" if payload["tokens"]["complete"] else ", partially reported",
            )
        )
    if payload["runtime"]["suspended"]:
        lines.append(
            "Runtime: %.0fs of delegated run time spent asleep was not charged (dev-orchestra budget show)"
            % payload["runtime"]["suspended"]
        )
    return lines


def cmd_design_approve(args: argparse.Namespace) -> int:
    """Record the user's yes to the plan as it is now.

    Open design findings are printed rather than refused: going ahead over
    them is the user's decision, and this command is only ever the record of
    one. What it adds is whether the findings are about this plan or about an
    earlier revision of it, which the report has to say.
    """
    loaded = _load_lenient(args.cwd)
    workspace = _workspace(args)
    plan_relative = workspace.relative(workspace.plan_path)
    plan_text, digest = approval_mod.read_plan(workspace)
    if not digest:
        _err("no plan to approve at %s -- run the architect first" % plan_relative)
        return 2

    # The findings and the round they belong to come out of one read of one
    # report, so they cannot describe two different rounds. A round that has
    # started but has no report yet -- still running, or it failed -- is
    # refused rather than approved over: its findings are the ones the user
    # has not seen. Checked after the report is read, so a round that starts
    # in between is refused too, and one that starts after this makes the
    # recorded approval stale.
    design_data = ws.read_json(workspace.design_review().consolidated_json_path, {}) or {}
    latest = approval_mod.design_round(workspace)
    approval_round = _approval_round(design_data, latest)
    if approval_round.refused:
        _err(
            "not recording an approval: the latest design review round has no report yet -- "
            "it is still running or did not finish. Wait for it (or run `review run --design` "
            "again), present its findings, and ask again."
        )
        return 2
    round_id, unreviewed = approval_round.round_id, approval_round.unreviewed
    open_findings = [str(f.get("id")) for f in approval_mod.open_findings(design_data)]
    of_current_plan = approval_mod.findings_of_current_plan(workspace, plan_text) if open_findings else None

    previous = workspace.read_state().get(approval_mod.STAGE)
    already = (
        isinstance(previous, dict)
        and previous.get("sha256") == digest
        and previous.get("design_round") == round_id
    )
    if already:
        entry = cast(Dict[str, Any], previous)
    else:
        entry = approval_mod.record(workspace, digest, open_findings, of_current_plan, round_id)

    if args.json:
        payload = dict(entry)
        payload.update({"already_approved": already, "workflow": workspace.workflow})
        _emit_json(payload)
    else:
        _out(
            "%s %s (sha256 %s) for workflow %s"
            % ("already approved" if already else "approved", plan_relative, digest[:12], workspace.workflow)
        )
    require_approval = bool(loaded.design_settings().get("require_approval"))
    for note in _approval_notes(require_approval, unreviewed, open_findings, of_current_plan):
        _err(note)
    return 0


class _ApprovalRound(NamedTuple):
    """The design round an approval is given over, and whether it may be given at all."""

    round_id: Optional[str]
    #: The latest round ran to the end with no reviewer's review.
    unreviewed: bool
    #: The latest round has no report yet: nothing is recorded.
    refused: bool


def _approval_round(design_data: Dict[str, Any], latest: Optional[str]) -> _ApprovalRound:
    """The round to approve over, from the design report and the latest round id.

    Both are read by the caller, the report first; this only compares them.
    """
    round_id = approval_mod.reported_round(design_data)
    unreviewed = (
        latest is not None and latest != round_id and latest == approval_mod.unreviewed_round(design_data)
    )
    if unreviewed:
        # The round ran to the end and nobody reviewed it: there is nothing
        # left to wait for, and refusing would leave the user no way to go
        # ahead once the design review budget is spent. The approval is given
        # over that round, so it is not stale the moment it is recorded. Its
        # report has no findings -- nobody reviewed it -- which the note in
        # `_approval_notes` says, so an empty list does not read as a clean review.
        round_id = latest
    elif latest != round_id:
        return _ApprovalRound(round_id, False, True)
    return _ApprovalRound(round_id, unreviewed, False)


def _approval_notes(
    require_approval: bool, unreviewed: bool, open_findings: List[str], of_current_plan: Optional[bool]
) -> List[str]:
    """The notes `design approve` prints to stderr after recording, in this order."""
    notes: List[str] = []
    if not require_approval:
        notes.append("note: design.require_approval is false; recorded anyway")
    if unreviewed:
        notes.append(
            "note: the latest design review round ended with no reviewer's review (every reviewer "
            "failed), so this plan has no design review findings at all; the approval goes ahead "
            "without one -- say so in the report"
        )
    if open_findings and of_current_plan is False:
        notes.append(
            "note: %d design finding(s) open from a review of an earlier revision of this plan: %s "
            "-- the revision may already address them; say so in the report"
            % (len(open_findings), ", ".join(open_findings))
        )
    elif open_findings:
        notes.append(
            "note: %d design finding(s) still open: %s -- approving over them is the user's call; "
            "name them in the report" % (len(open_findings), ", ".join(open_findings))
        )
    return notes


def cmd_state_show(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    state = workspace.read_state()
    if args.json:
        _emit_json(state)
        return 0
    events = state.get("events", [])
    if not events:
        _out("No recorded stages yet.")
        return 0
    for event in events:
        _out(
            "%-14s %-8s %s %s"
            % (event.get("stage"), event.get("status"), event.get("at"), event.get("model") or "")
        )
    return 0


def cmd_state_record(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    detail: Dict[str, Any] = {}
    for item in args.detail or []:
        key, _, value = item.partition("=")
        detail[key] = config_mod.coerce_scalar(value)
    workspace.record_event(args.stage, args.status, detail)
    _out("recorded %s=%s" % (args.stage, args.status))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    loaded = _load_lenient(args.cwd)
    workspace = _workspace(args)
    payload = _summary_payload(args, loaded, workspace)
    if args.json:
        _emit_json(payload)
        return 0
    _out("\n".join(_summary_lines(payload)))
    return 0


def _shown(value: Any) -> Any:
    """``value`` as the text prints it with ``%s``: kept if a string or None, else ``str()``."""
    return value if value is None or isinstance(value, str) else str(value)


def _summary_payload(
    args: argparse.Namespace, loaded: config_mod.LoadedConfig, workspace: ws.Workspace
) -> Dict[str, Any]:
    """Everything `summary` reports, read once; the text and `--json` both come from it."""
    state = workspace.read_state()
    review_data = ws.read_json(workspace.consolidated_json_path, {}) or {}

    seen: Dict[str, str] = {}
    for event in state.get("events", []):
        seen[str(event.get("stage"))] = str(event.get("status"))
    design_counts = (ws.read_json(workspace.design_review().consolidated_json_path, {}) or {}).get("counts")
    design_tallies: Dict[str, Any] = {}
    if design_counts:
        design_tallies = {
            "reviewers_ok": design_counts.get("reviewers_ok"),
            "reviewers_total": design_counts.get("reviewers_total"),
        }
    counts = review_data.get("counts", {})
    models: Dict[str, Dict[str, Any]] = {}
    for key, _title in ROLE_TITLES:
        spec = loaded.data.get(key) or {}
        model = spec.get("model") or {}
        models[key] = {
            "provider": _shown(spec.get("provider")),
            "family": _shown(model.get("family", "default")),
            "version": _shown(model.get("version", "latest")),
        }
    # Prefer the models actually resolved during the run; fall back to config.
    # Either may be malformed: a recorded panel that is not a non-empty list
    # falls back to config, a configured one that is not a list lists
    # nothing, and a non-mapping entry is skipped.
    listed = review_data.get("reviewers")
    if not isinstance(listed, list) or not listed:
        listed = loaded.data.get("reviewers")
    reviewers: List[Dict[str, Any]] = []
    for reviewer in listed if isinstance(listed, list) else []:
        if not isinstance(reviewer, dict):
            continue
        model = reviewer.get("model")
        if isinstance(model, dict):
            model = model.get("family", "default")
        reviewers.append(
            {
                "id": _shown(reviewer.get("id")),
                "provider": _shown(reviewer.get("provider")),
                "model": _shown(model or "default"),
                "status": _shown(reviewer.get("status", "ok")),
            }
        )
    # A round the gate refused is recorded but ran nothing, so it appears in
    # no other part of this report -- and "what you skipped" is exactly what
    # the final report is required to name.
    decided = opt_report.summarise_rounds(state.get("events") or [])

    book = _ledger(args, workspace)
    return {
        "stages": seen,
        "counts": counts,
        "tokens": book.token_report(),
        "design_counts": design_tallies,
        "models": models,
        "reviewers": reviewers,
        "skipped": {
            "refused": decided["refused"],
            "refused_by": decided["refused_by"],
            "design_refused": decided["design_refused"],
            "panel_reduced": decided["panel_reduced"],
        },
    }


def _summary_lines(payload: Dict[str, Any]) -> List[str]:
    """The text of `summary`, read from the payload alone."""
    lines = ["Workflow:"]
    seen = payload["stages"]
    for stage in (
        "architect",
        "design_review",
        "design_approval",
        "implementer",
        "test",
        "review",
        "review_fixer",
        "re-test",
    ):
        if stage in seen:
            lines.append("  %-14s %s" % (stage, "OK" if seen[stage] == "ok" else seen[stage].upper()))
    design_counts = payload["design_counts"]
    if design_counts:
        lines.append(
            "  %-14s %s/%s ok"
            % ("design reviews", design_counts.get("reviewers_ok"), design_counts.get("reviewers_total"))
        )
    counts = payload["counts"]
    if counts:
        lines.append(
            "  %-14s %s/%s ok" % ("reviews", counts.get("reviewers_ok"), counts.get("reviewers_total"))
        )
    lines.append("")
    lines.append("Models:")
    for key, title in ROLE_TITLES:
        model = payload["models"][key]
        lines.append("  %-14s %s / %s / %s" % (title, model["provider"], model["family"], model["version"]))
    lines.append("Review:")
    for reviewer in payload["reviewers"]:
        lines.append(
            "  %-14s %s / %s%s"
            % (
                reviewer["id"],
                reviewer["provider"],
                reviewer["model"],
                "" if reviewer["status"] == "ok" else " (FAILED)",
            )
        )
    skipped = payload["skipped"]
    if skipped["refused"] or skipped["panel_reduced"] or skipped["design_refused"]:
        lines.append("")
        lines.append("Optimization:")
        # One line per reason, because "what you skipped" is only useful if it
        # says what would make the round run: fixing the tests, or narrowing
        # the change. A single total says neither.
        for reason, count in sorted(skipped["refused_by"].items()):
            cause = _REFUSAL_CAUSE.get(reason, reason)
            lines.append("  %-14s %d round(s) not run: %s" % (reason, count, cause))
        if skipped["design_refused"]:
            lines.append(
                "  %-14s %d design round(s) not run: plan over review.context.max_chars"
                % ("context", skipped["design_refused"])
            )
        if skipped["panel_reduced"]:
            lines.append(
                "  %-14s %d round(s) cut to one reviewer -- one opinion, not an independent second"
                % ("panel", skipped["panel_reduced"])
            )
        lines.append("  %-14s dev-orchestra optimization report" % "detail")

    report = payload["tokens"]
    if report["totals"]["runs"]:
        lines.append("")
        lines.append("Tokens:")
        for stage, account in sorted(report["by_stage"].items()):
            lines.append("  %-14s %s billed" % (stage, ws.fmt_int(int(account.get("billed_tokens") or 0))))
        total = report["totals"]
        cost = float(total.get("cost_usd") or 0.0)
        lines.append(
            "  %-14s %s billed over %d run(s)%s%s"
            % (
                "total",
                ws.fmt_int(int(total.get("billed_tokens") or 0)),
                total["runs"],
                (" -- %s" % ws.fmt_usd(cost)) if cost else "",
                "" if report["complete"] else " (partially reported: a floor)",
            )
        )
    return lines
