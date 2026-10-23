"""`review run --design`: the design panel against the plan instead of a diff."""

from __future__ import annotations

import argparse
import os

from . import config as config_mod
from . import optimization as opt_mod
from . import questions as questions_mod
from . import review as review_mod
from . import workspace as ws
from .cli_common import (
    _err,
    _in_workflow,
    _out,
    _workspace,
)
from .cli_review_round import (
    _DESIGN,
    _NO_REVIEWERS,
    _account_runs,
    _consolidate_round,
    _end_round,
    _iteration,
    _lineage,
    _only_reviewers,
    _open_ledger,
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
from .round_progress import (
    design_decision,
    design_round_plan,
)


def _design_request_path(args: argparse.Namespace, workspace: ws.Workspace) -> str:
    """Where the request the plan answers is expected to be."""
    if getattr(args, "request", None):
        return str(_in_workflow(workspace, args.request))
    return os.path.join(workspace.execution_dir, "design-request.md")


def _run_design_review(args: argparse.Namespace, loaded: config_mod.LoadedConfig, declared: bool) -> int:
    """Run the design panel against `.ai/plan.md` instead of against a diff.

    The design panel (``review.design.reviewers`` when a file sets one, else
    the code panel with ``when`` ignored), the same fan-out, the same
    read-only mode. What differs is the input, the prompt, who sits on the
    panel, and where the results land -- and the last of those is what keeps
    a design round from advancing or exhausting the code review's counter.
    """
    workspace = _workspace(args).design_review().ensure()
    settings = loaded.review_settings()
    decision = design_decision(loaded, workspace)
    if decision.mode == "off":
        _err("note: review.design.enabled is false; running because you asked")
    elif not decision.run:
        _err(
            "note: review.design.enabled is auto and this plan would be skipped (%s); "
            "running because you asked" % decision.reason
        )

    configured = loaded.design_reviewers()
    reviewers = configured
    if args.only:
        reviewers = _only_reviewers(configured, args.only)
        if not reviewers:
            return _refuse_unmatched_only(args.only)

    request_path = _design_request_path(args, workspace)
    if not os.path.isfile(request_path):
        _err(
            "note: no design request at %s; reviewers judge the plan against its own "
            "stated goal" % workspace.relative(request_path)
        )
        request_path = ""
    # The plan is read and hashed here, and frozen only once this round is
    # allowed to run. Writing the snapshot first would replace the plan the
    # previous round's reports are stamped against, so a round refused for
    # budget -- or skipped for having no panel -- would strand its own triage.
    # Read once, so the prompt, the sha and review-target.json describe the
    # same decisions; "" without design questions, and then none of the three
    # changes.
    decisions_text = questions_mod.decisions_text(workspace.read_state())
    try:
        plan_text, request_text, digest = review_mod.design_digest(
            workspace, workspace.plan_path, request_path, decisions_text
        )
    except review_mod.ReviewError as exc:
        _err(str(exc))
        return 2

    lineage = _lineage(args, workspace)
    iteration = _iteration(args, workspace, lineage, digest)
    max_iterations = loaded.design_max_iterations()
    refusal = _refuse_if_rounds_spent(_DESIGN, iteration, max_iterations, args.force)
    if refusal is not None:
        return refusal

    book = _open_ledger(args, workspace)
    refusal = _refuse_if_runtime_spent(book, "design review", args.force)
    if refusal is not None:
        return refusal

    # The plan, the request and the decisions: all go into every reviewer's
    # prompt, whole and unconditionally, so measuring the plan alone would let
    # a round past the limit on a technicality. Refused here, before the plan
    # is frozen, for the reason the digest above is read early.
    #
    # The plan alone is a second, separate size: it is the only part ever
    # handed over as a file, so it -- and not the pair -- decides delivery and
    # what forcing this round may promise.
    context_settings = loaded.context_settings()
    max_chars = int(context_settings.get("max_chars") or 0)
    inline_chars = int(context_settings.get("inline_chars") or 0)
    budget_chars = len(plan_text) + len(request_text) + len(decisions_text)
    detail = {"plan_chars": len(plan_text), "request_chars": len(request_text)}
    if decisions_text:
        detail["decisions_chars"] = len(decisions_text)
    refusal = _refuse_if_over_context(
        workspace,
        "design_review",
        budget_chars=budget_chars,
        max_chars=max_chars,
        delivery_chars=len(plan_text),
        inline_chars=inline_chars,
        force=args.force,
        iteration=iteration,
        detail=detail,
    )
    if refusal is not None:
        return refusal
    over_budget = review_mod.over_context(budget_chars, max_chars)

    # Decided before the round is saved: the carried findings are read off
    # the live report of this lineage.
    design_plan = design_round_plan(
        loaded, workspace, reviewers, lineage=lineage, declared=declared, only=bool(args.only)
    )
    meta = review_mod.write_design_snapshot(
        workspace, workspace.plan_path, request_path, plan_text, digest, decisions_text
    )

    if not reviewers:
        # Recorded against the plan that was actually frozen, so the round this
        # skip occupies is the one the next real round continues from rather
        # than a phantom that quietly spends the budget.
        _out(_NO_REVIEWERS)
        return _save_skipped_round(workspace, iteration, lineage, meta.get("round_id"))

    # No gate and no panel reduction. There is no test result to judge a plan
    # by and no diff to measure, and a design decision is precisely where
    # cross-model disagreement earns its cost. What does decide who sits:
    # a `when: high-risk` seat of a configured design panel joins on a plan
    # that names a high-risk path, and a specialist with nothing in the plan
    # to read sits out (`optimization.relevance_records`). Under --only every
    # named reviewer runs, and its record says so.
    for line in design_plan.conditional_notes():
        _err("note: %s" % line)
    excluded = {str(record["id"]) for record in design_plan.left_out()}
    if not args.only:
        reviewers = [r for r in reviewers if opt_mod.qualifies(r, design_plan.conditional)]
    reviewers, switched = _risk_models(reviewers, design_plan.is_high_risk, design_plan.risk_reason())
    max_findings = opt_mod.findings_cap(loaded.optimization_settings(), settings)

    ctx = _RoundContext(args, loaded, settings, workspace, book, _DESIGN, iteration)
    panel = _run_panel(
        ctx,
        reviewers,
        max_findings=max_findings,
        over_budget=over_budget,
        budget_chars=budget_chars,
        inline_chars=inline_chars,
        prompt_for=lambda reviewer: review_mod.build_design_review_prompt(
            reviewer,
            workspace,
            plan_text,
            request_text,
            args.context or "",
            max_findings,
            inline_chars,
            decisions_text,
        ),
    )
    if isinstance(panel, int):
        return panel
    runs = panel.runs

    run_dicts = _account_runs(ctx, panel, switched)
    # Every reviewer of this round has returned, so this is the report the
    # round's id may be published in -- and the only place that says so. Not
    # when none of them came back with a review: a round nobody reviewed has
    # no findings to show, and approving over it would look clean. A seat this
    # round left out is left out of its consolidation too, as on the code
    # path: its report from an earlier run of this plan is not news.
    reviewed = any(run.status in ("ok", "partial") for run in runs)
    data, stale = _consolidate_round(
        workspace,
        configured,
        excluded,
        run_dicts,
        iteration,
        lineage,
        completed_round=meta.get("round_id") if reviewed else None,
        unreviewed_round=None if reviewed else meta.get("round_id"),
    )
    detail = _round_detail(ctx, meta, data, run_dicts, design_plan.to_dict())
    _end_round(ctx, panel.token, detail, runs)
    _round_notes(ctx, detail["identical_rounds"], stale)
    return _print_round(
        ctx, runs, run_dicts, data, {"plan": meta["plan"], "optimization": design_plan.to_dict()}
    )
