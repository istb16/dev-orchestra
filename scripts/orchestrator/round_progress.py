"""Where the review rounds of a workflow stand, for `review run`, `review status` and `status`.

What a code or design round on the current snapshot or plan would decide, and
whether the round that reached the iteration limit has had the one revision
or fix it is still owed. `review run` decides its round here, and both status
commands preview it and read the final pass here, so none of them can
disagree; each status command passes in its own run log and its own reading
of the remaining budget, because those are what the two read differently.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import approval as approval_mod
from . import config as config_mod
from . import ledger as ledger_mod
from . import optimization as opt_mod
from . import optimization_design as opt_design
from . import review as review_mod
from . import workspace as ws


def _risk_paths(meta: Dict[str, Any]) -> List[str]:
    """Every path a snapshot says the change touches.

    ``changed_paths`` is the whole set, including withheld files and the name
    a rename came from. A snapshot without the list reads as one with no
    paths, as a missing or unreadable snapshot does; `review run` refuses a
    snapshot that has ``files`` but not the list (``missing_path_lists``), so
    only a preview reads it that way.
    """
    paths = meta.get("changed_paths")
    return [str(path) for path in paths] if isinstance(paths, list) else []


def _condition_paths(meta: Dict[str, Any]) -> List[str]:
    """The paths a path-scoped reviewer is matched against.

    ``condition_paths`` is the change as a reviewer sees it: ``changed_paths``
    less what is in neither the diff nor the withheld notice. Missing, it
    reads as no paths, like ``_risk_paths``.
    """
    paths = meta.get("condition_paths")
    return [str(path) for path in paths] if isinstance(paths, list) else []


def missing_path_lists(meta: Any) -> List[str]:
    """The path lists a snapshot with reviewed files lacks, in the order named.

    Read as no paths, high-risk patterns would match nothing and path-scoped
    reviewers would not qualify, so the panel could be cut without a word.
    Not rebuilt from ``files`` and ``withheld`` either: they leave out the
    name a rename came from. A snapshot written before the lists were, or
    edited by hand, is retaken instead. ``{}``, no snapshot, lacks nothing.
    """
    if not isinstance(meta, dict) or "files" not in meta:
        return []
    return [key for key in ("changed_paths", "condition_paths") if not isinstance(meta.get(key), list)]


def round_plan(
    loaded: config_mod.LoadedConfig,
    settings: Dict[str, Any],
    workspace: ws.Workspace,
    meta: Dict[str, Any],
    panel: List[Dict[str, Any]],
    declared: bool = False,
    only: bool = False,
) -> opt_mod.Plan:
    """What a review round on the snapshot ``meta`` decides.

    Built here for both `review run` and the `status` preview, so the
    preview cannot drift from the run. The preview passes the whole panel,
    and neither ``--only`` nor ``--high-risk``. The reviewed files go in by
    name, for the role relevance rules; a snapshot without the list (or no
    snapshot yet) has nothing for them to judge.
    """
    files = meta.get("files")
    return opt_mod.decide(
        loaded.optimization_settings(),
        settings,
        _risk_paths(meta),
        int(meta.get("lines_added") or 0) + int(meta.get("lines_deleted") or 0),
        workspace.last_status("test"),
        len(panel),
        reviewed_files=len(meta.get("files") or []),
        panel=panel,
        declared=declared,
        carried=review_mod.carried_findings(workspace, meta),
        only=only,
        condition_paths=_condition_paths(meta),
        reviewed=[str(path) for path in files] if isinstance(files, list) else None,
    )


def design_decision(loaded: config_mod.LoadedConfig, workspace: ws.Workspace) -> opt_design.DesignDecision:
    """Whether the design review runs for this workflow's plan, and why.

    One answer for ``status``, ``review status --design`` and ``review run
    --design``. Once a design round exists ``auto`` keeps answering run, so a
    revision cannot switch the loop off half way. The plan is read strictly:
    a decision made from a guessed file name would be worse than none.
    """
    mode = config_mod.design_review_mode(loaded.design_review_settings().get("enabled"))
    design_data = ws.read_json(workspace.design_review().consolidated_json_path, {}) or {}
    round_ran = (
        int(design_data.get("iteration", 0) or 0) > 0 or approval_mod.design_round(workspace) is not None
    )
    plan_state, scan = _plan_scan(workspace)
    return opt_design.decide_design(
        mode, loaded.optimization_settings(), scan, plan_state=plan_state, round_ran=round_ran
    )


def _plan_scan(workspace: ws.Workspace) -> Tuple[str, Any]:
    """``(plan_state, scan)`` of the workflow's plan, read strictly.

    ``ok`` with ``review.plan_tokens()`` of it; ``missing`` for no plan or a
    blank one, as approval.read_plan reads it; ``unreadable`` otherwise.
    """
    text = ws.read_text_strict(workspace.plan_path)
    if text is None:
        return "unreadable", None
    if not text.strip():
        return "missing", None
    return "ok", review_mod.plan_tokens(text)


def _design_carried(workspace: ws.Workspace, lineage: str) -> List[Dict[str, Any]]:
    """The accepted findings of the live design report, unless its lineage was reset.

    The design round's analogue of ``review.carried_findings``: a seat that
    reported one of these is kept to re-check it. Whatever plan the report
    reviewed -- a revision is meant to address them, so a revised plan is
    exactly the round that must re-check them. Empty when no design round
    exists, or when ``lineage`` is given and the report belongs to another.
    """
    consolidated = ws.read_json(workspace.design_review().consolidated_json_path, {}) or {}
    if not consolidated:
        return []
    if lineage and str(consolidated.get("lineage") or "") != lineage:
        return []
    return review_mod.accepted_findings(consolidated)


def design_lineage(workspace: ws.Workspace, book: ledger_mod.Ledger) -> str:
    """The lineage the next design round of this workflow would record."""
    return review_mod.review_lineage(workspace.design_review(), book.workflow_id())


def design_round_plan(
    loaded: config_mod.LoadedConfig,
    workspace: ws.Workspace,
    panel: List[Dict[str, Any]],
    *,
    lineage: str,
    declared: bool = False,
    only: bool = False,
) -> opt_design.DesignPlan:
    """What a design round on this workflow's plan decides about its panel.

    Built here for `review run --design`, `review status --design` and the
    `status` preview, so the previews cannot drift from the run. A preview
    passes the whole design panel and neither ``--only`` nor ``--high-risk``.
    """
    plan_state, scan = _plan_scan(workspace)
    return opt_design.decide_design_round(
        loaded.optimization_settings(),
        scan,
        plan_state,
        panel,
        declared=declared,
        carried=_design_carried(workspace, lineage),
        only=only,
    )


def left_out_suffix(records: Sequence[Dict[str, Any]]) -> str:
    """``; <id> left out (<reason>)`` for each record of a seat a round would leave out."""
    return "".join(
        "; %s left out (%s)" % (record.get("id"), record.get("reason"))
        for record in records
        if isinstance(record, dict) and not record.get("runs")
    )


def reviewed_something(event: Dict[str, Any]) -> bool:
    """Whether this event is a round that put the change in front of a panel.

    The one positive mark such a round leaves is a reviewer that actually
    started: an entry with ``invoked`` true. Nothing else on the stage leaves
    one -- a refusal of either kind records an empty list, and ``clear_stalls``
    records a reason. The entries alone are not the mark: a reviewer that dies
    on provider startup or model resolution is recorded too, with ``invoked``
    false, and a round where every entry is one of those put the change in
    front of nobody. Asked this way round rather than by naming the statuses
    that do not count, so the next bookkeeping status added to the ledger
    cannot quietly become one that clears a refusal.
    """
    reviewers = event.get("reviewers")
    if not isinstance(reviewers, list):
        return False
    return any(isinstance(run, dict) and run.get("invoked") for run in reviewers)


def _ran_since_last_round(
    events: List[Dict[str, Any]],
    review_stage: str,
    run_stage: str,
    then_stages: Tuple[str, ...] = (),
    counts: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Tuple[bool, bool]:
    """Whether ``run_stage`` answered after the last round that reviewed something.

    The first value: an ``ok`` run of ``run_stage`` with a usable result since
    that round, and one ``counts`` accepts when it is given. An event without
    ``answered`` predates the key and counts; an ``ok`` over silence saved
    nothing and does not. The second: whether any ``then_stages`` event,
    whatever its status, follows that run. Rounds that reviewed nothing --
    refusals, abandoned entries -- are not the round.
    """
    ran = False
    then = False
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        stage = event.get("stage")
        if stage == review_stage and reviewed_something(event):
            break
        if (
            stage == run_stage
            and event.get("status") == "ok"
            and event.get("answered", True) is not False
            and (counts is None or counts(event))
        ):
            ran = True
            break
        if stage in then_stages:
            then = True
    return ran, ran and then


def approved_as_recorded(info: Dict[str, Any]) -> bool:
    """Whether the recorded approval covers this plan and this design round.

    Read from the record rather than from ``state``: with
    ``design.require_approval`` off the state is ``not-required`` whatever was
    recorded, and a plan the user did approve is still one they approved.
    """
    return info.get("matches_current_plan") is True and info.get("reviewed_since_approval") is False


def _design_final_pass(
    blocking: List[Dict[str, Any]],
    *,
    iteration: int,
    max_iterations: int,
    of_current_plan: Optional[bool],
    ran_since: bool,
    architect_left: Optional[int],
    approved: bool,
    implemented: bool,
    accepted: bool,
) -> Dict[str, Any]:
    """Where the one revision the round that reached the limit still gets stands.

    The limit counts reviews, not revisions: the last round's findings are
    folded in once more, and only the re-review of that revision is refused.
    ``status`` and ``review status --design`` both read this, so they cannot
    disagree about it. With none of the findings accepted there is nothing to
    fold in, and the spent budget is the stop it always was (``unaccepted``).
    """
    plan_changed = None
    if not blocking or iteration < max_iterations:
        state = None
    elif approved:
        state = "approved"
    elif implemented:
        # A plan already built on is not asked to change under the code.
        state = "implemented"
    elif of_current_plan is False or ran_since or of_current_plan is None:
        state = "done"
        plan_changed = True if of_current_plan is False else (None if of_current_plan is None else False)
    elif not accepted:
        state = "unaccepted"
    elif architect_left == 0:
        state = "blocked"
    else:
        state = "pending"
    return {"state": state, "pending": state == "pending", "plan_changed": plan_changed}


def design_final_pass_from(
    workspace: ws.Workspace,
    events: List[Dict[str, Any]],
    blocking: List[Dict[str, Any]],
    *,
    iteration: int,
    max_iterations: int,
    of_current_plan: Optional[bool],
    approval: Dict[str, Any],
    architect_left: Optional[int],
    accepted: bool,
    wrote_plan: Callable[[Dict[str, Any]], bool],
) -> Dict[str, Any]:
    """``_design_final_pass`` from the run log and the approval record.

    ``events`` and ``architect_left`` are the caller's: ``status`` reads them
    after clearing stalls and from its budget summary, ``review status`` from
    the run log as it is and from the ledger. ``wrote_plan`` says which
    architect runs revised the plan.
    """
    return _design_final_pass(
        blocking,
        iteration=iteration,
        max_iterations=max_iterations,
        of_current_plan=of_current_plan,
        ran_since=_ran_since_last_round(events, "design_review", "architect", counts=wrote_plan)[0],
        architect_left=architect_left,
        approved=approved_as_recorded(approval),
        implemented=approval_mod.implemented_since_plan(workspace, events),
        accepted=accepted,
    )


def _code_final_pass(
    blocking: List[Dict[str, Any]],
    *,
    iteration: int,
    max_iterations: int,
    fixed_since: bool,
    retested_since: bool,
    fixer_left: Optional[int],
    accepted: bool,
) -> Dict[str, Any]:
    """The code review's counterpart: the last round gets its fix and re-test.

    Only accepted findings are fixed, so with none accepted the spent budget
    stops as it always did (``unaccepted``).
    """
    if not blocking or iteration < max_iterations:
        state = None
    elif fixed_since:
        state = "done" if retested_since else "retest"
    elif not accepted:
        state = "unaccepted"
    elif fixer_left == 0:
        state = "blocked"
    else:
        state = "pending"
    return {"state": state, "pending": state == "pending"}


def code_final_pass_from(
    events: List[Dict[str, Any]],
    blocking: List[Dict[str, Any]],
    *,
    iteration: int,
    max_iterations: int,
    fixer_left: Optional[int],
    accepted: bool,
) -> Dict[str, Any]:
    """``_code_final_pass`` from the run log; ``events`` and ``fixer_left`` are the caller's."""
    fixed, retested = _ran_since_last_round(events, "review", "review_fixer", ("test", "re-test"))
    return _code_final_pass(
        blocking,
        iteration=iteration,
        max_iterations=max_iterations,
        fixed_since=fixed,
        retested_since=retested,
        fixer_left=fixer_left,
        accepted=accepted,
    )
