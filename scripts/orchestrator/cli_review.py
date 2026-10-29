"""The `review` commands: snapshot, run, triage, consolidate, fix brief."""

from __future__ import annotations

import argparse
import hashlib
import os
from typing import (
    Any,
    Dict,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from . import config as config_mod
from . import config_policy as policy_mod
from . import context as context_mod
from . import instructions, review_tests
from . import ledger as ledger_mod
from . import optimization as opt_mod
from . import review as review_mod
from . import workspace as ws
from .cli_args import _add_command_group, _add_json_flag, _seconds
from .cli_common import (
    _emit_json,
    _err,
    _in_workflow,
    _load_lenient,
    _load_or_die,
    _out,
    _review_workspace,
    _workspace,
)
from .cli_review_design import (
    _run_design_review,
)
from .cli_review_round import (
    _CODE,
    _DESIGN,
    _NO_REVIEWERS,
    _account_runs,
    _consolidate_round,
    _end_round,
    _iteration,
    _lineage,
    _only_reviewers,
    _open_ledger,
    _PanelRun,
    _print_round,
    _refuse_if_over_context,
    _refuse_if_rounds_spent,
    _refuse_if_runtime_spent,
    _refuse_unmatched_only,
    _risk_models,
    _round_detail,
    _round_notes,
    _RoundContext,
    _run_panel,
    _save_skipped_round,
)
from .cli_review_status import cmd_review_status
from .round_progress import (
    code_round_cap,
    missing_path_lists,
    recheck_next,
    round_plan,
)

# --------------------------------------------------------------------------- review


def _same_snapshot_report(workspace: ws.Workspace, meta: Dict[str, Any]) -> Dict[str, Any]:
    """The consolidated report when its last run reviewed this snapshot, else empty.

    Whatever the lineage: ``build_consolidation`` carries triage over by
    finding key across a lineage change too, so a rebuild after ``budget
    reset`` can lose a decision just as one without it can.
    """
    previous = ws.read_json(workspace.consolidated_json_path, {}) or {}
    sha = str(meta.get("sha256") or "")
    if not previous or not sha or str((previous.get("snapshot") or {}).get("sha256") or "") != sha:
        return {}
    return previous


def _measurement_rerun(workspace: ws.Workspace, meta: Dict[str, Any], lineage: str, iteration: int) -> bool:
    """Whether this run reviews again the snapshot the last run of this review did.

    The condition ``next_iteration`` keeps the current round on, and the round
    this run records has to be that round: an explicit ``--iteration`` that
    names another is a round of its own, and registers its signature.
    """
    previous = _same_snapshot_report(workspace, meta)
    if not previous or str(previous.get("lineage") or "") != lineage:
        return False
    return iteration == int(previous.get("iteration") or 0)


def _triage_record(entry: Dict[str, Any]) -> Dict[str, str]:
    """What triage keeps on a finding: the decision and its note."""
    return {
        "triage": str(entry.get("triage") or "needs-triage"),
        "triage_note": str(entry.get("triage_note") or ""),
    }


def _triage_changed_since_build(consolidated: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The findings whose triage or note was set after the last run built them.

    Measured against the triage that run recorded, so a decision carried in
    from an earlier round is not one made on this snapshot. A report without
    that record -- built without the flag, or by ``review consolidate`` --
    counts every finding with a decision or a note.
    """
    findings = [entry for entry in consolidated.get("findings") or [] if isinstance(entry, dict)]
    baseline = (consolidated.get("measurement") or {}).get("triage_at_build")
    untouched = {"triage": "needs-triage", "triage_note": ""}
    if not isinstance(baseline, dict):
        baseline = {}
    return [entry for entry in findings if _triage_record(entry) != baseline.get(entry.get("key"), untouched)]


def _measurement_inputs(
    args: argparse.Namespace, plan: opt_mod.Plan, inline_chars: int, max_chars: int
) -> Dict[str, Any]:
    """The prompt inputs other than the context, which a pair has to hold equal.

    The extra context is kept as a digest: the record is for telling two runs
    apart, not for keeping what a caller passed.
    """
    extra = args.context or ""
    return {
        "context_sha256": hashlib.sha256(extra.encode("utf-8")).hexdigest() if extra else "",
        "max_findings": plan.max_findings,
        "inline_chars": inline_chars,
        "max_chars": max_chars,
        "force": bool(args.force),
    }


def _measurement_block(
    meta: Dict[str, Any],
    workspace: ws.Workspace,
    book: ledger_mod.Ledger,
    override: str,
    rerun: bool,
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    """What ``optimization report`` pairs a ``--surrounding`` run on.

    The whole snapshot identity rather than the reviewer entries' short sha,
    and the workflow directory apart from the budget epoch: a pair is keyed on
    the first, and ``budget reset`` between the two runs changes only the second.
    """
    raw_frozen = meta.get("surrounding")
    frozen = raw_frozen if isinstance(raw_frozen, dict) else {}
    return {
        "surrounding": override,
        "snapshot": str(meta.get("sha256") or ""),
        "tree": str(frozen.get("tree") or meta.get("tree") or ""),
        "head": meta.get("head"),
        "base": meta.get("base"),
        "workflow": workspace.workflow,
        "epoch": book.workflow_id(),
        "rerun": rerun,
        "inputs": inputs,
    }


def _refuse_incremental_measurement(override: Optional[str], meta: Dict[str, Any]) -> Optional[int]:
    """``--surrounding`` on an incremental round. Nothing without the flag."""
    if not (override and meta.get("incremental_from")):
        return None
    # Refused on the first run as well as the second: the premise section
    # is built from the consolidated report of the moment a run starts, and
    # the first run rewrites it, so no pair on this snapshot would differ
    # in the context alone.
    _err(
        "refusing to run: --surrounding %s on an incremental round (fix diff since %s). The prompt of "
        "a re-review carries the accepted findings of the moment it runs, so two runs on it would "
        "differ in more than the surrounding context. Measure on a whole-change snapshot: the first "
        "round of a change, or a round after a clean or all-rejected review."
        % (override, meta.get("incremental_from"))
    )
    return 2


def _refuse_unmeasurable(
    override: Optional[str], adoption: context_mod.Adoption, workspace: ws.Workspace, meta: Dict[str, Any]
) -> Optional[int]:
    """A ``--surrounding`` run that would measure nothing or lose a decision. Nothing without it."""
    # Before anything is charged: an asked-for measurement that would measure
    # nothing, or that would throw away a decision made on this snapshot, is
    # refused rather than billed. Without the flag neither check runs.
    if override == "enclosing" and not adoption.adopted:
        _err(
            "refusing to run: --surrounding enclosing was asked for but nothing would be adopted (%s); "
            "the run would measure nothing and still bill the whole panel." % adoption.reason
        )
        if adoption.reason == context_mod.NOT_FROZEN:
            _err("Run `review snapshot --surrounding enclosing` first.")
        else:
            _err('See references/limits.md, "Measuring what surrounding context does".')
        return 2
    # On the snapshot alone, not on ``rerun``: a lineage change between the two
    # runs makes the second no rerun, but it still rebuilds the same findings.
    previous = _same_snapshot_report(workspace, meta) if override is not None else {}
    if previous:
        changed = _triage_changed_since_build(previous)
        if changed:
            _err(
                "refusing to run: --surrounding %s would rebuild the findings of a snapshot that has been "
                "triaged since its last run (%d of %d with a new decision or note), and a decision on a "
                "finding that does not come back would be lost. Take the pair before triage, or take a new "
                "snapshot." % (override, len(changed), len(previous.get("findings") or []))
            )
            return 2
    return None


def _record_triage_at_build(data: Dict[str, Any], override: Optional[str], rerun: bool) -> None:
    """Put the ``measurement`` record on a ``--surrounding`` run's report. Nothing without the flag."""
    if not override:
        return
    # The triage as this run built it, so the second run of a pair can tell
    # a decision made on this snapshot from one carried in with a finding.
    data["measurement"] = {
        "surrounding": override,
        "rerun": rerun,
        "triage_at_build": {entry["key"]: _triage_record(entry) for entry in data["findings"]},
    }


def _adoption_line(adoption: context_mod.Adoption) -> str:
    """``review run``'s one line on the surrounding context it handed over."""
    record = adoption.record()
    if adoption.adopted:
        line = "%d symbol(s), %s chars adopted" % (
            len(adoption.adopted),
            ws.fmt_int(adoption.adopted_chars),
        )
    else:
        line = "nothing adopted -- %s" % adoption.reason
    if adoption.trimmed:
        line += "; %d left out (%s)" % (len(adoption.trimmed), context_mod.trim_reasons(record))
    return line


def _surrounding_run_lines(
    adoption: context_mod.Adoption, override: Optional[str], context_on: bool
) -> List[str]:
    """``review run``'s Surrounding line: when the context was on, or when the flag turned it off."""
    if context_on:
        line = "Surrounding context: %s" % _adoption_line(adoption)
        if override:
            line += " (--surrounding enclosing for this run)"
        return [line]
    if override:
        return [
            "Surrounding context: none (--surrounding none for this run; "
            "review.context.surrounding unchanged)"
        ]
    return []


def _refuse_snapshot_without_paths(workspace: ws.Workspace, meta: Any) -> Optional[int]:
    """Refuse a round on a snapshot ``round_progress.missing_path_lists`` finds wanting."""
    missing = missing_path_lists(meta)
    if not missing:
        return None
    _err(
        "refusing to run: the snapshot %s has no %s list, so which paths the change touches cannot be "
        "judged and the high-risk and path-scoped reviewers could be left out. Take a fresh one with "
        "`dev-orchestra review snapshot`."
        % (workspace.relative(workspace.snapshot_meta_path), " or ".join(missing))
    )
    return 2


_RECHECK_SCOPE = "whole change (review.whole_change_recheck)"


def _recheck(
    args: argparse.Namespace, loaded: config_mod.LoadedConfig, workspace: ws.Workspace, incremental: bool
) -> bool:
    """``recheck_next``, asked only of a snapshot that could otherwise narrow to the fix.

    One that takes the whole change anyway -- ``--full``, or
    ``review.incremental_rounds`` off -- is not the loop's whole-change round
    and gets no spare round. Asked before the snapshot replaces the one on
    disk: the orchestrator need not remember ``--full``.
    """
    return incremental and recheck_next(loaded, workspace, args.base, _lineage(args, workspace))


def cmd_review_snapshot(args: argparse.Namespace) -> int:
    loaded = _load_lenient(args.cwd)
    workspace = _workspace(args)
    settings = loaded.review_settings()
    exclude = () if args.no_exclude else settings.get("exclude")
    incremental = bool(settings.get("incremental_rounds", True)) and not args.full
    recheck = _recheck(args, loaded, workspace, incremental)
    # Read before the snapshot, because the surrounding context is frozen with
    # it: what a reviewer is shown has to come from the tree the diff did.
    context_settings = loaded.context_settings()
    surrounding = args.surrounding or context_mod.surrounding_mode(context_settings.get("surrounding"))
    try:
        meta = review_mod.create_snapshot(
            workspace,
            args.base,
            include_untracked=not args.no_untracked,
            exclude=exclude,
            incremental=incremental,
            surrounding=surrounding,
            watched=instructions.watched_paths(loaded),
            recheck=recheck,
            tested=loaded.start_before_tests(),
        )
    except review_mod.ReviewError as exc:
        _err(str(exc))
        return 2
    # Warned about, never refused: taking a snapshot spends nothing, and the
    # command that would spend something says so itself. Refusing here would
    # also leave no snapshot to narrow from.
    #
    # Measured before the JSON branch, because --json is the form a wrapper
    # reads and a warning only a human sees is not one it can act on. The
    # three keys go in the payload, not in the snapshot's metadata file: what
    # the limit was and whether this change is over it are facts about the
    # commands that read the snapshot, not about the frozen diff.
    max_chars = int(context_settings.get("max_chars") or 0)
    change_chars = review_mod.snapshot_chars(workspace)
    over_context = review_mod.over_context(change_chars, max_chars)
    if args.json:
        payload = dict(meta)
        payload["change_chars"] = change_chars
        payload["max_chars"] = max_chars
        payload["over_context"] = over_context
        _emit_json(payload)
        return 0
    _out("Snapshot: %s" % workspace.relative(workspace.snapshot_path))
    _out("  strategy: %s" % meta["strategy"])
    if meta.get("incremental_from"):
        _out("  scope:    what changed since the last reviewed round, not the whole change")
        if meta.get("full_diff"):
            _out("            whole change kept at %s" % meta["full_diff"])
        _out("            reviewers also get the findings the fix was meant to address")
    if meta.get("recheck"):
        _out("  scope:    %s" % _RECHECK_SCOPE)
    _out("  files:    %d" % len(meta["files"]))
    _out("  size:     %d bytes (sha256 %s)" % (meta["bytes"], meta["sha256"][:12]))
    for line in _surrounding_snapshot_lines(workspace, meta, context_settings):
        _out(line)
    if over_context:
        _out(
            "  WARNING:  %s chars is over review.context.max_chars (%s) -- review run will "
            "refuse this change." % (ws.fmt_int(change_chars), ws.fmt_int(max_chars))
        )
        _out("            Narrow it with --base or review.exclude, or split the change.")
    withheld = meta.get("withheld") or []
    excluded, unread = review_mod.partition_withheld(withheld)
    if withheld:
        # Named, not merely counted: an exclusion nobody can see is an
        # exclusion nobody can correct.
        if excluded:
            lines = review_mod.withheld_lines(excluded)
            summary = "%d file(s), %s changed line(s) not sent to reviewers" % (len(withheld), lines)
            if unread:
                summary += " (%d not read, lines not counted)" % len(unread)
        else:
            summary = "%d file(s) not read, lines not counted" % len(unread)
        _out("  withheld: " + summary)
        for entry in withheld:
            _out("    %s (%s)" % (entry["path"], entry.get("pattern") or entry.get("reason") or "?"))
        if excluded and unread:
            _out("    reviewers are told these changed; --no-exclude sends the pattern-matched ones in full,")
            _out("    the ones not read stay withheld")
        elif excluded:
            _out("    reviewers are told these changed; --no-exclude sends them in full")
        else:
            _out("    reviewers are told these changed")
    if meta["empty"]:
        if excluded and not unread:
            _out("  WARNING: every changed file was withheld -- re-run with --no-exclude to review them.")
        elif withheld:
            _out("  WARNING: every changed file was withheld --")
            if excluded:
                _out("           re-run with --no-exclude to review the pattern-matched ones;")
            _out("           the %d file(s) not read have to be reviewed by hand." % len(unread))
        else:
            _out("  WARNING: the snapshot is empty -- there is nothing to review.")
        return 1
    return 0


def _surrounding_snapshot_lines(
    workspace: ws.Workspace, meta: Dict[str, Any], context_settings: Dict[str, Any]
) -> List[str]:
    """What was frozen as surrounding context, and what could not be. Nothing when off."""
    block = meta.get("surrounding")
    if not isinstance(block, dict):
        return []
    tree = str(block.get("tree") or "")
    frozen = "  context:  enclosing -- %d symbol(s), %s chars frozen from tree %s at %s" % (
        int(block.get("candidates") or 0),
        ws.fmt_int(int(block.get("chars") or 0)),
        (tree[:7] + "...") if tree else "(none)",
        block.get("path"),
    )
    cap = context_settings.get("surrounding_chars")
    if ws.is_int(cap):
        cap = ws.fmt_int(cap)
    later = "            adopted at review run within review.context.surrounding_chars (%s)" % cap
    skipped = (ws.read_json(workspace.surrounding_path, {}) or {}).get("skipped") or []
    note = context_mod.not_extracted(skipped)
    if note:
        later += "; " + note[0].lower() + note[1:].rstrip(".")
    return [frozen, later]


def _refuse_design_only_flags(args: argparse.Namespace, override: Optional[str]) -> Optional[int]:
    """The code review's flag on a design round, which has no use for it."""
    if args.design and override:
        _err("--surrounding applies to the code review only: a design round carries no surrounding context.")
        return 2
    return None


def _code_panel(
    args: argparse.Namespace, loaded: config_mod.LoadedConfig, workspace: ws.Workspace
) -> Union[int, Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """``(configured, reviewers)`` for a code round, or the exit code of one with no panel to run."""
    configured = loaded.reviewers()
    reviewers = configured
    if args.only:
        reviewers = _only_reviewers(configured, args.only)
    if not reviewers and not args.only:
        _out(_NO_REVIEWERS)
        lineage = _lineage(args, workspace)
        meta = workspace.read_snapshot_meta()
        return _save_skipped_round(
            workspace, _iteration(args, workspace, lineage), lineage, meta.get("round_id")
        )
    if not reviewers:
        return _refuse_unmatched_only(args.only)
    return configured, reviewers


def _ensure_snapshot(
    args: argparse.Namespace,
    loaded: config_mod.LoadedConfig,
    workspace: ws.Workspace,
    settings: Dict[str, Any],
    context_settings: Dict[str, Any],
    watched: Sequence[str] = (),
) -> Optional[int]:
    """Take the snapshot a round reviews, or the exit code of a failed one.

    Taken when none is on disk, and again when ``--base`` names another base
    than the one on disk was taken against. It used to be honoured only in the
    first case, so a snapshot left from earlier -- an empty one included --
    was reviewed instead and the flag was dropped without a word. A different
    base is a different change (see ``review_lineage``), so the round that
    follows counts from one, as it does after ``review snapshot --base``.
    Without ``--base`` the snapshot on disk is kept, whatever it was taken
    against, and so is one taken against the commit ``--base`` names (see
    ``review_mod.same_base``): a snapshot taken with no base was taken against
    the commit HEAD was at, and ``--base HEAD`` retaking it restarted the
    round count for the same change.

    A snapshot taken here asks ``_recheck`` first, and takes the whole change
    when it says so, as ``review snapshot`` would; one kept is not asked.
    """
    retake = False
    if os.path.isfile(workspace.snapshot_path) and args.base:
        meta = workspace.read_snapshot_meta()
        retake = not review_mod.same_base(workspace.root, meta, args.base)
        if retake:
            # With the commit: `HEAD` then and `HEAD` now can be two bases.
            taken = str(meta.get("base") or "HEAD")
            commit = str(meta.get("base_commit") or "")
            if commit:
                taken += " at %s" % commit[:12]
            _err(
                "note: retaking the snapshot against --base %s (the one on disk was taken against %s); "
                "`review snapshot --base %s` takes it with other options" % (args.base, taken, args.base)
            )
    if retake or not os.path.isfile(workspace.snapshot_path):
        recheck = _recheck(args, loaded, workspace, bool(settings.get("incremental_rounds", True)))
        try:
            review_mod.create_snapshot(
                workspace,
                args.base,
                exclude=settings.get("exclude"),
                incremental=bool(settings.get("incremental_rounds", True)),
                surrounding=context_mod.surrounding_mode(context_settings.get("surrounding")),
                watched=watched,
                recheck=recheck,
                tested=loaded.start_before_tests(),
            )
        except review_mod.ReviewError as exc:
            _err(str(exc))
            return 2
        if recheck:
            _err("note: snapshot scope: %s" % _RECHECK_SCOPE)
    return None


class _GatedPanel(NamedTuple):
    """A code round the gate let through: its plan, who runs, and who sat it out."""

    plan: opt_mod.Plan
    reviewers: List[Dict[str, Any]]
    excluded: set


def _plan_and_gate(
    args: argparse.Namespace,
    loaded: config_mod.LoadedConfig,
    workspace: ws.Workspace,
    meta: Dict[str, Any],
    reviewers: List[Dict[str, Any]],
    declared: bool,
    iteration: int,
) -> Union[int, _GatedPanel]:
    """Decide the round and say what it decided, or the exit code of the gate's refusal.

    The refusal is recorded before any ledger entry exists.
    """
    plan = round_plan(loaded, workspace, meta, reviewers, declared, bool(args.only))
    if plan.escalated:
        _err("note: %s" % plan.escalation_note())
    for line in plan.conditional_notes():
        _err("note: %s" % line)
    # Under --only every named reviewer runs, and its record says so.
    # Otherwise a conditional reviewer that did not qualify sits the round
    # out, and is left out of its consolidation too: a report it wrote on an
    # earlier run of this snapshot must not be rebuilt into a round its own
    # event says it sat out.
    excluded = {str(record["id"]) for record in plan.conditional if not record["runs"]}
    if not args.only:
        reviewers = [r for r in reviewers if opt_mod.qualifies(r, plan.conditional)]
    if plan.gate == opt_mod.GATE_REFUSE and not args.force:
        # Written down even though nothing ran, and *because* nothing ran: a
        # skipped round is the largest thing this level ever saves, and a
        # saving that leaves no trace cannot be counted. No budget is consumed
        # and no ledger entry opened -- there was no attempt to account for.
        workspace.record_event(
            "review",
            opt_mod.REFUSED,
            {
                "iteration": iteration,
                "optimization": plan.to_dict(),
                # Which of the two refusals this was. Both are recorded the
                # same way, and a report that cannot tell them apart prices
                # them the same -- see ``summarise_rounds``.
                "refused_by": "gate",
                "reviewers": [],
            },
        )
        _err(plan.gate_note())
        return ledger_mod.EXIT_BUDGET_EXHAUSTED
    return _GatedPanel(plan, reviewers, excluded)


class _RoundSize(NamedTuple):
    """What a code round measured before it was allowed to run."""

    max_chars: int
    inline_chars: int
    adoption: context_mod.Adoption
    budget_chars: int
    #: Whether ``--force`` sent the round past ``review.context.max_chars``.
    over_budget: bool
    #: Whether this ``--surrounding`` run is the second of a pair.
    rerun: bool


def _size_round(
    args: argparse.Namespace,
    workspace: ws.Workspace,
    meta: Dict[str, Any],
    context_settings: Dict[str, Any],
    plan: opt_mod.Plan,
    override: Optional[str],
    lineage: str,
    iteration: int,
) -> Union[int, _RoundSize]:
    """Adopt the context and measure the round, or the exit code of a refusal for its size.

    The context refusal is recorded before any ledger entry exists.
    """
    max_chars = int(context_settings.get("max_chars") or 0)
    inline_chars = int(context_settings.get("inline_chars") or 0)
    change_chars = review_mod.snapshot_chars(workspace)
    # Chosen before the limit is checked, because the limit measures what the
    # prompt carries: the diff and the context adopted beside it. The context
    # is capped at what the diff leaves under both limits, so this never
    # refuses a round the diff alone would have run.
    adoption = context_mod.adopt(
        workspace,
        meta,
        context_settings.get("surrounding"),
        context_settings.get("surrounding_chars"),
        change_chars,
        max_chars,
        inline_chars,
        review_mod.delivery_of(change_chars, inline_chars),
    )
    # The delivered body is the diff alone -- context never decides delivery --
    # so it is passed apart from the budget, as the design path passes its own.
    budget_chars = change_chars + adoption.context_chars
    refusal = _refuse_if_over_context(
        workspace,
        "review",
        budget_chars=budget_chars,
        max_chars=max_chars,
        delivery_chars=change_chars,
        inline_chars=inline_chars,
        force=args.force,
        iteration=iteration,
        detail={"optimization": plan.to_dict()},
    )
    if refusal is not None:
        return refusal
    rerun = override is not None and _measurement_rerun(workspace, meta, lineage, iteration)
    refusal = _refuse_unmeasurable(override, adoption, workspace, meta)
    if refusal is not None:
        return refusal
    # Forced past it: the round runs, and every record of it says so.
    over_budget = review_mod.over_context(budget_chars, max_chars)
    return _RoundSize(max_chars, inline_chars, adoption, budget_chars, over_budget, rerun)


def _warn_project_gates(loaded: config_mod.LoadedConfig) -> None:
    """What the project file asked of the gates: what was ignored, and what it loosened.

    Here as well as in ``doctor`` and ``config validate``: the project file can
    come with the branch this round reviews, and this is the run that says so
    while the round is still the user's to stop.
    """
    for line in policy_mod.project_ignored_warnings(loaded):
        _err("warning: %s" % line)
    for line in policy_mod.project_loosening_notices(loaded):
        _err("warning: %s" % line)


def cmd_review_run(args: argparse.Namespace) -> int:
    loaded = _load_or_die(args.cwd)
    _warn_project_gates(loaded)
    # One run's override of review.context.surrounding, for measuring what the
    # context does on one snapshot. None when not given, and then nothing below
    # differs from a run without the flag.
    override = getattr(args, "surrounding", None)
    declared = bool(getattr(args, "high_risk", False))
    refusal = _refuse_design_only_flags(args, override)
    if refusal is not None:
        return refusal
    if args.design:
        return _run_design_review(args, loaded, declared)
    workspace = _workspace(args)
    found = _code_panel(args, loaded, workspace)
    if isinstance(found, int):
        return found
    configured, reviewers = found

    settings = loaded.review_settings()
    context_settings = dict(loaded.context_settings())
    if override:
        context_settings["surrounding"] = override
    watched = instructions.watched_paths(loaded)
    refusal = _ensure_snapshot(args, loaded, workspace, settings, context_settings, watched)
    if refusal is not None:
        return refusal
    lineage = _lineage(args, workspace)
    iteration = _iteration(args, workspace, lineage)
    max_iterations = loaded.max_review_iterations()
    meta = workspace.read_snapshot_meta()
    # One round past review.max_review_iterations, only on the whole-change
    # snapshot review.whole_change_recheck asked for.
    spare = code_round_cap(loaded, meta) > max_iterations
    refusal = _refuse_if_rounds_spent(_CODE, iteration, max_iterations, args.force, spare)
    if refusal is not None:
        return refusal

    refusal = _refuse_snapshot_without_paths(workspace, meta)
    if refusal is not None:
        return refusal
    refusal = _refuse_incremental_measurement(override, meta)
    if refusal is not None:
        return refusal
    gated = _plan_and_gate(args, loaded, workspace, meta, reviewers, declared, iteration)
    if isinstance(gated, int):
        return gated
    plan, reviewers, excluded = gated

    # After the gate, because a round the gate already refused has no reason to
    # be measured, and before anything is charged or run.
    size = _size_round(args, workspace, meta, context_settings, plan, override, lineage, iteration)
    if isinstance(size, int):
        return size
    reviewers = _warn_and_limit(args, plan, reviewers)
    reviewers, switched = _risk_models(reviewers, bool(plan.risk_reason()), plan.risk_reason())

    book = _open_ledger(args, workspace)
    refusal = _refuse_if_runtime_spent(book, "review", args.force)
    if refusal is not None:
        return refusal
    ctx = _RoundContext(args, loaded, settings, workspace, book, _CODE, iteration)
    panel = _run_panel(
        ctx,
        reviewers,
        max_findings=plan.max_findings,
        over_budget=size.over_budget,
        budget_chars=size.budget_chars,
        inline_chars=size.inline_chars,
        extra_context=args.context or "",
        surrounding=size.adoption,
    )
    if isinstance(panel, int):
        return panel
    runs = panel.runs
    context_on = size.adoption.mode != "none"

    data, detail, measurement, stale = _consolidate_code_round(
        ctx, panel, meta, plan, size, excluded, configured, override, lineage, switched
    )
    run_dicts = detail["reviewers"]
    _end_round(ctx, panel.token, detail, runs)
    _round_notes(ctx, detail["identical_rounds"], stale, size.rerun)
    _cap_notes(runs, plan.max_findings)

    return _print_round(
        ctx,
        runs,
        run_dicts,
        data,
        _code_payload_extra(plan, size.adoption, context_on, measurement),
        _surrounding_run_lines(size.adoption, override, context_on),
    )


def _warn_and_limit(
    args: argparse.Namespace, plan: opt_mod.Plan, reviewers: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Print the gate's warning and apply the plan's reviewer limit, returning who runs."""
    if plan.gate == opt_mod.GATE_WARN:
        _err(plan.gate_note())
    if plan.reviewer_limit is not None and not args.only:
        reviewers = opt_mod.choose_reviewers(reviewers, plan.reviewer_limit)
        _err(
            "note: %s (%s). Cross-model disagreement is what a second reviewer buys; "
            "raise optimization.level or the low_risk thresholds to keep it."
            % (plan.reviewer_note(), ", ".join(str(r.get("id")) for r in reviewers))
        )
    return reviewers


def _consolidate_code_round(
    ctx: _RoundContext,
    panel: _PanelRun,
    meta: Dict[str, Any],
    plan: opt_mod.Plan,
    size: _RoundSize,
    excluded: set,
    configured: List[Dict[str, Any]],
    override: Optional[str],
    lineage: str,
    switched: Sequence[str] = (),
) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]], List[str]]:
    """Account, consolidate and save a code round that ran.

    Returns ``(data, detail, measurement, stale)``: the saved consolidation,
    the round's event detail, its measurement block (None without
    ``--surrounding``), and the reviewers whose reports were stale.
    ``switched`` holds the ids that ran on their ``high_risk_model``.
    """
    workspace = ctx.workspace
    adoption, rerun = size.adoption, size.rerun
    context_on = adoption.mode != "none"
    run_dicts = _account_runs(ctx, panel, switched)
    # Passed whether or not anyone returned: nothing is approved over a code
    # round, so the id only has to name the round this report was built for.
    data, stale = _consolidate_round(
        workspace,
        configured,
        excluded,
        run_dicts,
        ctx.iteration,
        lineage,
        completed_round=meta.get("round_id"),
        amend=lambda built: _record_triage_at_build(built, override, rerun),
    )
    detail = _round_detail(ctx, meta, data, run_dicts, plan.to_dict(), rerun)
    measurement = None
    if override:
        measurement = _measurement_block(
            meta,
            workspace,
            ctx.book,
            override,
            rerun,
            _measurement_inputs(ctx.args, plan, size.inline_chars, size.max_chars),
        )
        detail["measurement"] = measurement
    if context_on and any("surrounding" in run for run in run_dicts):
        # Sizes and counts only: ``optimization report`` splits rounds on it,
        # and the names are on every reviewer entry beside it. Only when a
        # reviewer's prompt was built with it: a round whose every reviewer
        # fell over first showed no one any context, and must not be counted
        # as a round with it.
        detail["surrounding"] = adoption.summary()
    return data, detail, measurement, stale


def _cap_notes(runs: List[review_mod.ReviewerRun], max_findings: int) -> None:
    """Note each reviewer that returned more findings than the plan's cap."""
    if max_findings:
        for run in runs:
            if run.findings > max_findings:
                _err(
                    "note: %s returned %d findings against a cap of %d; all are kept, "
                    "but its output cost more than it needed to"
                    % (run.reviewer.get("id"), run.findings, max_findings)
                )


def _code_payload_extra(
    plan: opt_mod.Plan,
    adoption: context_mod.Adoption,
    context_on: bool,
    measurement: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """The code round's keys beside the common ones in its printed payload."""
    payload_extra: Dict[str, Any] = {"optimization": plan.to_dict()}
    if context_on:
        payload_extra["surrounding"] = adoption.record()
    if measurement is not None:
        payload_extra["measurement"] = measurement
    return payload_extra


def _condition_excluded(workspace: ws.Workspace, stage: str = "review") -> set:
    """The reviewers the last round of ``stage`` on this snapshot left out.

    ``review`` or ``design_review``: a conditional reviewer that did not
    qualify, or a role the round had nothing for. Read off that round's own
    event, so a re-consolidation reads the reports the round did and no
    others: a left-out reviewer's report from an earlier run of the same
    snapshot is not brought back by rebuilding. A design event from before
    design rounds recorded who sat out carries no block, and leaves no one out.
    """
    meta = workspace.read_snapshot_meta()
    round_id = str(meta.get("round_id") or "")
    if not round_id:
        return set()
    events = workspace.read_state().get("events") or []
    for event in reversed(events if isinstance(events, list) else []):
        if not isinstance(event, dict) or event.get("stage") != stage or event.get("status") != "ok":
            continue
        if str(event.get("round_id") or "") != round_id:
            continue
        raw_plan = event.get("optimization")
        plan = raw_plan if isinstance(raw_plan, dict) else {}
        return {
            str(record.get("id"))
            for record in plan.get("conditional") or []
            if isinstance(record, dict) and not record.get("runs")
        }
    return set()


def _broken_panel(loaded: config_mod.LoadedConfig, design: bool = False) -> List[str]:
    """validate's problems with the panel, when it is not a list of mappings.

    `review run` refuses such a config. Consolidating it would read no
    reports and save a finding-free round over the last one, which
    `review status` would then pass. An absent panel is not broken.
    ``design`` asks of the design panel, when a file sets one.
    """
    key = config_mod.DESIGN_PANEL.reviewers if design and loaded.has_design_panel() else "reviewers"
    panel = config_mod.get_path(loaded.data, key)
    if panel is None or (isinstance(panel, list) and all(isinstance(r, dict) for r in panel)):
        return []
    problems = config_mod.validate_loaded(loaded)
    return [problem for problem in problems if problem.startswith((key + ":", key + "["))]


def cmd_review_consolidate(args: argparse.Namespace) -> int:
    loaded = _load_lenient(args.cwd)
    design = bool(getattr(args, "design", False))
    broken = _broken_panel(loaded, design)
    if broken:
        _err("refusing to consolidate: the reviewers panel is invalid:")
        for problem in broken:
            _err("  - %s" % problem)
        return 2
    workspace = _review_workspace(args)
    panel = loaded.design_reviewers() if design else loaded.reviewers()
    excluded = _condition_excluded(workspace, _DESIGN.stage if design else _CODE.stage)
    lineage = _lineage(args, workspace)
    data, stale = _consolidate_round(
        workspace, panel, excluded, None, _iteration(args, workspace, lineage), lineage
    )
    for reviewer_id in stale:
        _err("note: %s's report predates the current snapshot and was ignored" % reviewer_id)
    if args.json:
        _emit_json(data)
    else:
        _out(review_mod.render_consolidation(data))
    return 0


def _no_review_yet(args: argparse.Namespace) -> None:
    _err("no consolidated review found -- run `review run%s` first" % (" --design" if args.design else ""))


def cmd_review_show(args: argparse.Namespace) -> int:
    workspace = _review_workspace(args)
    data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    if not data:
        _no_review_yet(args)
        return 2
    if args.accepted:
        data = dict(data, findings=review_mod.accepted_findings(data))
    if args.json:
        _emit_json(data)
    else:
        _out("Source: %s" % workspace.relative(workspace.consolidated_json_path))
        _out(review_mod.render_consolidation(data))
    return 0


def _tests_refusal(args: argparse.Namespace, workspace: ws.Workspace, action: str) -> str:
    """Why a code round started before the tests may not go on to ``action`` yet, or "".

    Judged on the round the report holds, not on the snapshot on disk, which
    the next round may already have replaced. Only with
    ``review.start_before_tests`` on: off, the tests were recorded before the
    snapshot was taken, as they always were.
    """
    if args.design or not _load_lenient(args.cwd).start_before_tests():
        return ""
    data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    return review_tests.refusal(review_tests.report_verdict(_workspace(args), data), action)


def cmd_review_triage(args: argparse.Namespace) -> int:
    workspace = _review_workspace(args)
    # Accepting is what hands a finding to the fixer, and what narrows the
    # next snapshot to its fix; rejecting or merging one can go on while the
    # tests run.
    refusal = _tests_refusal(args, workspace, "accept") if args.status == "accepted" else ""
    if refusal:
        _err(refusal)
        return 2

    # Read inside the lock: two triage calls run side by side each read the
    # report, set their own decision and wrote it back, and the second write
    # dropped the first decision while both printed success.
    def triaged(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not data:
            return None
        for finding_id in args.ids:
            review_mod.set_triage(data, finding_id, args.status, args.note or "")
        return data

    try:
        saved = review_mod.update_consolidation(workspace, triaged)
    except review_mod.ReviewError as exc:
        _err(str(exc))
        return 2
    if saved is None:
        _no_review_yet(args)
        return 2
    _out("Triaged %s as %s" % (", ".join(args.ids), args.status))
    return 0


def cmd_review_fix_brief(args: argparse.Namespace) -> int:
    workspace = _review_workspace(args)
    refusal = _tests_refusal(args, workspace, "write the fix brief")
    if refusal:
        _err(refusal)
        return 2
    data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    if args.design:
        brief = review_mod.render_fix_brief(data, "Revise the plan to address these accepted findings")
    else:
        brief = review_mod.render_fix_brief(data)
    if args.output:
        path = _in_workflow(workspace, str(args.output))
        ws.write_text(path, brief)
        _out("Wrote %s" % workspace.relative(path))
    else:
        _out(brief)
    return 0


# --------------------------------------------------------------------------- parser


def _add_design_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--design",
        action="store_true",
        help="the design review of .ai/plan.md instead of the code review",
    )


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
