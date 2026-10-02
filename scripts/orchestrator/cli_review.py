"""The `review` commands: snapshot, run, triage, consolidate, fix brief."""

from __future__ import annotations

import argparse
import hashlib
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import approval as approval_mod
from . import config as config_mod
from . import context as context_mod
from . import ledger as ledger_mod
from . import optimization as opt_mod
from . import review as review_mod
from . import workspace as ws
from .cli_common import (
    _emit_json,
    _err,
    _in_workflow,
    _ledger,
    _load_or_die,
    _out,
    _review_workspace,
    _workspace,
    _wrote_plan,
)
from .providers import WARNED_ENFORCEMENT, get_provider

# --------------------------------------------------------------------------- review


def _refuse_if_runtime_spent(book: ledger_mod.Ledger, stage: str, force: bool) -> Optional[int]:
    """The runtime budget, applied to the stages that do not consume attempts.

    Review is the largest consumer of delegated runtime -- a round is a run per
    panel member -- and until now it was the one consumer that never asked. A
    budget the biggest spender does not consult is the "guard in name only"
    this module's ledger exists to stop being.

    Only the runtime reason, not ``check()``: that would also apply the
    attempt, total-run and no-progress rules to review, which are governed by
    ``review.max_review_iterations`` and decided above this point.
    """
    if force:
        return None
    reason = book.runtime_refusal()
    if not reason:
        return None
    _err("refusing to run %s:" % stage)
    _err("  - %s" % reason)
    _err("Report what is unresolved instead of retrying, or pass --force to override.")
    return ledger_mod.EXIT_BUDGET_EXHAUSTED


def _warn_unenforced(loaded: config_mod.LoadedConfig, reviewers: List[Dict[str, Any]]) -> Dict[str, str]:
    """One line per reviewer about to run on a provider that cannot be held to reading.

    A reviewer refused for coming with the project file is not warned about:
    it does not run. Returns the lines printed, by reviewer id.
    """
    refused = list(config_mod.project_raw_arg_refusals(loaded))
    lines = config_mod.reviewer_enforcement_warnings(loaded.data, refused)
    printed: Dict[str, str] = {}
    for reviewer in reviewers:
        reviewer_id = str(reviewer.get("id") or "")
        line = lines.get(reviewer_id)
        if line:
            _err("warning: %s" % line)
            printed[reviewer_id] = line
    return printed


def _reviewer_refusals(loaded: config_mod.LoadedConfig, reviewers: List[Dict[str, Any]]) -> Dict[str, str]:
    """Why each reviewer about to run is not run, by reviewer id.

    The configuration's refusals, and one decided on the live report: a
    reviewer from the project file whose adapter reports, when asked now, that
    it cannot be held to reading -- one whose report is not static, which the
    configuration's refusals never ask.
    """
    refusals = dict(config_mod.reviewer_raw_arg_refusals(loaded))
    from_project = config_mod.reviewer_provider_refusals(loaded)
    for reviewer in reviewers:
        reviewer_id = str(reviewer.get("id") or "")
        if reviewer_id in refusals or reviewer_id not in from_project:
            continue
        try:
            provider = get_provider(str(reviewer.get("provider")))
            if type(provider).static_enforcement:
                continue  # reviewer_raw_arg_refusals already decided it
            if not provider.detect().installed:
                continue
            status = provider.read_only_enforcement().get("status")
        except Exception:
            continue  # the run reports it
        if status in WARNED_ENFORCEMENT:
            refusals[reviewer_id] = from_project[reviewer_id]
    return refusals


def _report_run_warnings(runs: List[Any], warned: Optional[Dict[str, str]] = None) -> None:
    """What each reviewer's adapter said about its run, whatever the outcome,
    less the enforcement warning ``_warn_unenforced`` already printed."""
    for run in runs:
        reviewer_id = str(run.reviewer.get("id") or "")
        before = (warned or {}).get(reviewer_id, "")
        for warning in run.warnings:
            if before and before.endswith(warning):
                continue
            _err("warning: reviewer %s: %s" % (reviewer_id or "reviewer", warning))


def _refuse_if_over_context(
    workspace: ws.Workspace,
    stage: str,
    budget_chars: int,
    max_chars: int,
    delivery_chars: int,
    inline_chars: int,
    force: bool,
    iteration: int,
    detail: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Stop a round whose change body is too large to review at all.

    Written down even though nothing ran, and *because* nothing ran: this is
    the largest thing the limit ever saves, and a saving that leaves no trace
    cannot be counted. The code path passes its ``optimization`` block in
    ``detail`` for the same reason the gate refusal carries one --
    ``summarise_rounds`` selects rounds on that key, so a refusal without it
    is a refusal no report can see.

    Nothing is spent by getting here: no budget consumed, no ledger entry
    opened, and on the design path the previous round's plan is left frozen
    where it was, so the triage this refusal asks the reader to report on is
    still there to report.

    ``budget_chars`` is what the limit measures and what the refusal records;
    ``delivery_chars`` is the body that would go into the prompt, and decides
    only what forcing would do -- against ``inline_chars``, which is the other
    limit and not this one. They differ on the design path -- see
    ``over_budget_note`` -- and all three are passed on both paths.
    """
    if force or not review_mod.over_context(budget_chars, max_chars):
        return None
    event = {"iteration": iteration, "refused_by": "context", "reviewers": []}
    event.update(detail or {})
    # The inline limit is not recorded here. It decided nothing about this
    # round -- nothing was delivered -- and it only shapes the last line of the
    # message below, which is about a round that does not exist yet.
    event["context"] = {"chars": budget_chars, "max_chars": max_chars}
    workspace.record_event(stage, opt_mod.REFUSED, event)
    design = stage == "design_review"
    for line in review_mod.over_budget_note(
        budget_chars, max_chars, delivery_chars, inline_chars, design=design
    ):
        _err(line)
    return ledger_mod.EXIT_BUDGET_EXHAUSTED


def _lineage(args: argparse.Namespace, workspace: ws.Workspace) -> str:
    """Which review the next round belongs to.

    Keyed partly on the ledger's workflow, so ``budget reset`` clears the
    round counter along with everything else it claims to clear. It used to
    say "this is now a fresh workflow" and leave the one counter that refuses
    work untouched.
    """
    return review_mod.review_lineage(workspace, _ledger(args, workspace).workflow_id())


def _iteration(
    args: argparse.Namespace,
    workspace: ws.Workspace,
    lineage: str = "",
    current_sha: Optional[str] = None,
) -> int:
    """An explicit --iteration wins; otherwise derive it from what is on disk.

    ``current_sha`` is the snapshot a round would take but has not written yet,
    which is how a round can be costed before it is allowed to replace one.
    """
    given = getattr(args, "iteration", None)
    if given is not None:
        return int(given)
    return review_mod.next_iteration(workspace, lineage or _lineage(args, workspace), current_sha)


def _merge_runs(workspace: ws.Workspace, run_dicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep reviewers that did not run this time in the reviewer table."""
    previous = (ws.read_json(workspace.consolidated_json_path, {}) or {}).get("reviewers", [])
    fresh = {entry.get("id") for entry in run_dicts}
    kept = [
        dict(entry, status=entry.get("status", "ok")) for entry in previous if entry.get("id") not in fresh
    ]
    return run_dicts + kept


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


def _adoption_line(adoption: context_mod.Adoption) -> str:
    """``review run``'s one line on the surrounding context it handed over."""
    record = adoption.record()
    if adoption.adopted:
        line = "%d symbol(s), %s chars adopted" % (
            len(adoption.adopted),
            "{:,}".format(adoption.adopted_chars),
        )
    else:
        line = "nothing adopted -- %s" % adoption.reason
    if adoption.trimmed:
        line += "; %d left out (%s)" % (len(adoption.trimmed), context_mod.trim_reasons(record))
    return line


def _reviewer_line(run: review_mod.ReviewerRun) -> str:
    mark = "ok" if run.status == "ok" else ("PARTIAL" if run.status == "partial" else "FAILED")
    return "%-7s %-18s %-8s %-18s %s" % (
        mark,
        run.reviewer.get("id"),
        run.reviewer.get("provider"),
        run.model_display or "?",
        run.error or "%d finding(s)" % run.findings,
    )


def _panel_summary(ok: int, failed: int, partial: int) -> str:
    """The round's tally.

    The first two counts keep their wording: the orchestrator is told to
    report `N successful, M failed` verbatim, and SKILL.md and workflow.md
    both say so. Partial is appended, and only when there is one, so a round
    that never touched the inline limit reads exactly as it always did.
    """
    line = "%d successful, %d failed" % (ok, failed)
    if partial:
        line += ", %d partial (change handed over as a file)" % partial
    return line


def _risk_paths(meta: Dict[str, Any]) -> List[str]:
    """Every path a snapshot says the change touches.

    ``changed_paths`` is the whole set, including withheld files and the name
    a rename came from. A snapshot written by an earlier version does not have
    it, so the old pair is reconstructed instead -- one round judged from a
    slightly narrower set is better than a crash, and the next snapshot has
    the key.
    """
    paths = meta.get("changed_paths")
    if isinstance(paths, list) and paths:
        return [str(path) for path in paths]
    return list(meta.get("files") or []) + [str(entry.get("path")) for entry in (meta.get("withheld") or [])]


def _condition_paths(meta: Dict[str, Any]) -> List[str]:
    """The paths a path-scoped reviewer is matched against.

    ``condition_paths`` is the change as a reviewer sees it: ``changed_paths``
    less what is in neither the diff nor the withheld notice. A snapshot
    written before it existed falls back to ``_risk_paths`` on a first round,
    where nothing is suppressed and the two lists are the same, so rename
    sources still count. An incremental one may have suppressed files in
    ``changed_paths``, so it gets the reviewed and withheld files only.
    """
    paths = meta.get("condition_paths")
    if isinstance(paths, list) and paths:
        return [str(path) for path in paths]
    if not meta.get("incremental_from"):
        return _risk_paths(meta)
    return list(meta.get("files") or []) + [str(entry.get("path")) for entry in (meta.get("withheld") or [])]


def _round_plan(
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
    and neither ``--only`` nor ``--high-risk``.
    """
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
    )


def cmd_review_snapshot(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    workspace = _workspace(args)
    settings = loaded.review_settings()
    exclude = () if args.no_exclude else settings.get("exclude")
    incremental = bool(settings.get("incremental_rounds", True)) and not args.full
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
    _out("  files:    %d" % len(meta["files"]))
    _out("  size:     %d bytes (sha256 %s)" % (meta["bytes"], meta["sha256"][:12]))
    for line in _surrounding_snapshot_lines(workspace, meta, context_settings):
        _out(line)
    if over_context:
        _out(
            "  WARNING:  %s chars is over review.context.max_chars (%s) -- review run will "
            "refuse this change." % ("{:,}".format(change_chars), "{:,}".format(max_chars))
        )
        _out("            Narrow it with --base or review.exclude, or split the change.")
    withheld = meta.get("withheld") or []
    if withheld:
        # Named, not merely counted: an exclusion nobody can see is an
        # exclusion nobody can correct.
        lines = review_mod.withheld_lines(withheld)
        _out("  withheld: %d file(s), %s changed line(s) not sent to reviewers" % (len(withheld), lines))
        for entry in withheld:
            _out("    %s (%s)" % (entry["path"], entry["pattern"]))
        _out("    reviewers are told these changed; --no-exclude sends them in full")
    if meta["empty"]:
        if withheld:
            _out("  WARNING: every changed file was withheld -- re-run with --no-exclude to review them.")
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
        "{:,}".format(int(block.get("chars") or 0)),
        (tree[:7] + "...") if tree else "(none)",
        block.get("path"),
    )
    cap = context_settings.get("surrounding_chars")
    if isinstance(cap, int) and not isinstance(cap, bool):
        cap = "{:,}".format(cap)
    later = "            adopted at review run within review.context.surrounding_chars (%s)" % cap
    skipped = (ws.read_json(workspace.surrounding_path, {}) or {}).get("skipped") or []
    note = context_mod.not_extracted(skipped)
    if note:
        later += "; " + note[0].lower() + note[1:].rstrip(".")
    return [frozen, later]


def _design_request_path(args: argparse.Namespace, workspace: ws.Workspace) -> str:
    """Where the request the plan answers is expected to be."""
    if getattr(args, "request", None):
        return str(_in_workflow(workspace, args.request))
    return os.path.join(workspace.execution_dir, "design-request.md")


def _design_decision(loaded: config_mod.LoadedConfig, workspace: ws.Workspace) -> opt_mod.DesignDecision:
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
    text = ws.read_text_strict(workspace.plan_path)
    if text is None:
        plan_state, scan = "unreadable", None
    elif not text.strip():
        # Blank is no plan, as approval.read_plan reads it.
        plan_state, scan = "missing", None
    else:
        plan_state, scan = "ok", review_mod.plan_tokens(text)
    return opt_mod.decide_design(
        mode, loaded.optimization_settings(), scan, plan_state=plan_state, round_ran=round_ran
    )


def _run_design_review(args: argparse.Namespace, loaded: config_mod.LoadedConfig) -> int:
    """Run the panel against `.ai/plan.md` instead of against a diff.

    Same reviewers, same fan-out, same read-only mode. What differs is the
    input, the prompt, and where the results land -- and the last of those is
    what keeps a design round from advancing or exhausting the code review's
    counter.
    """
    workspace = _workspace(args).design_review().ensure()
    settings = loaded.review_settings()
    design = loaded.design_review_settings()
    decision = _design_decision(loaded, workspace)
    if decision.mode == "off":
        _err("note: review.design.enabled is false; running because you asked")
    elif not decision.run:
        _err(
            "note: review.design.enabled is auto and this plan would be skipped (%s); "
            "running because you asked" % decision.reason
        )

    configured = loaded.reviewers()
    reviewers = configured
    if args.only:
        wanted = set(args.only)
        reviewers = [r for r in configured if r.get("id") in wanted or r.get("role") in wanted]
        if not reviewers:
            _err("--only %s matched no configured reviewer" % " ".join(args.only))
            return 2

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
    try:
        plan_text, request_text, digest = review_mod.design_digest(
            workspace, workspace.plan_path, request_path
        )
    except review_mod.ReviewError as exc:
        _err(str(exc))
        return 2

    lineage = _lineage(args, workspace)
    iteration = _iteration(args, workspace, lineage, digest)
    max_iterations = int(design.get("max_iterations", 2))
    if iteration > max_iterations and not args.force:
        _err(
            "refusing to run design review round %d: the budget is %d rounds "
            "(review.design.max_iterations)." % (iteration, max_iterations)
        )
        _err(
            "The round that reached the limit still gets its revision; only the re-review "
            "is refused. Report what is still open, or pass --force to override."
        )
        return ledger_mod.EXIT_BUDGET_EXHAUSTED

    book = _ledger(args, workspace)
    # Before the refusal decides: a stage whose process is gone has no charge
    # to answer for, and its absence is part of what the runtime budget says.
    book.clear_stalls()
    refusal = _refuse_if_runtime_spent(book, "design review", args.force)
    if refusal is not None:
        return refusal

    # The plan *and* the request: both go into every reviewer's prompt, whole
    # and unconditionally, so measuring the plan alone would let a round past
    # the limit on a technicality. Refused here, before the plan is frozen,
    # for the reason the digest above is read early.
    #
    # The plan alone is a second, separate size: it is the only part ever
    # handed over as a file, so it -- and not the pair -- decides delivery and
    # what forcing this round may promise.
    context_settings = loaded.context_settings()
    max_chars = int(context_settings.get("max_chars") or 0)
    inline_chars = int(context_settings.get("inline_chars") or 0)
    budget_chars = len(plan_text) + len(request_text)
    refusal = _refuse_if_over_context(
        workspace,
        "design_review",
        budget_chars,
        max_chars,
        len(plan_text),
        inline_chars,
        args.force,
        iteration,
        {"plan_chars": len(plan_text), "request_chars": len(request_text)},
    )
    if refusal is not None:
        return refusal
    over_budget = review_mod.over_context(budget_chars, max_chars)

    meta = review_mod.write_design_snapshot(workspace, workspace.plan_path, request_path, plan_text, digest)

    if not reviewers:
        # Recorded against the plan that was actually frozen, so the round this
        # skip occupies is the one the next real round continues from rather
        # than a phantom that quietly spends the budget.
        _out("No reviewers configured -- skipping the independent-review stage.")
        data = review_mod.build_consolidation(
            workspace, [], [], iteration, lineage, completed_round=meta.get("round_id")
        )
        review_mod.save_consolidation(workspace, data)
        return 0

    # No gate and no panel reduction. There is no test result to judge a plan
    # by and no diff to measure, and a design decision is precisely where
    # cross-model disagreement earns its cost -- so the whole panel runs. A
    # reviewer's `when` is ignored here too: a plan has no paths to judge, and
    # the design stage is where the specialists were measured to pay off.
    max_findings = opt_mod.findings_cap(loaded.optimization_settings(), settings)

    batch_timeout = args.timeout or int(settings.get("timeout_seconds", 1800))
    token = book.begin(
        "design_review",
        {"iteration": iteration, "reviewers": [str(r.get("id")) for r in reviewers]},
        deadline=batch_timeout,
    )
    idle_timeout = args.idle_timeout
    if idle_timeout is None:
        idle_timeout = settings.get("idle_timeout_seconds")
    warned = _warn_unenforced(loaded, reviewers)
    try:
        runs = review_mod.run_reviews(
            reviewers,
            workspace,
            parallel=not args.sequential and bool(settings.get("parallel", True)),
            timeout=batch_timeout,
            idle_timeout=idle_timeout,
            max_findings=max_findings,
            over_budget=over_budget,
            budget_chars=budget_chars,
            # The same number to both: `prompt_for` decides the delivery and
            # `run_reviews` records and words it, and a round that took them
            # from two places would sooner or later take two different ones.
            inline_chars=inline_chars,
            prompt_for=lambda reviewer: review_mod.build_design_review_prompt(
                reviewer,
                workspace,
                plan_text,
                request_text,
                args.context or "",
                max_findings,
                inline_chars,
            ),
            refusals=_reviewer_refusals(loaded, reviewers),
        )
    except review_mod.ReviewError as exc:
        book.end(token, "failed", {"error": str(exc)})
        _err(str(exc))
        return 2

    for run in runs:
        if run.invoked:
            # Prefixed, so a reviewer's design cost never merges into its code
            # cost in `tokens show` -- the same reasoning as `role:tier`.
            label = "design:%s" % (run.reviewer.get("id") or "reviewer")
            book.record_usage("design_review", run.usage.to_dict(), label=label)
    _report_run_warnings(runs, warned)
    run_dicts = [run.to_dict() for run in runs]
    stamp = review_mod.current_snapshot_stamp(workspace)
    findings, stale = review_mod.read_reports(workspace, [str(r.get("id")) for r in configured], stamp)
    # Every reviewer of this round has returned, so this is the report the
    # round's id may be published in -- and the only place that says so. Not
    # when none of them came back with a review: a round nobody reviewed has
    # no findings to show, and approving over it would look clean.
    reviewed = any(run.status in ("ok", "partial") for run in runs)
    data = review_mod.build_consolidation(
        workspace,
        _merge_runs(workspace, run_dicts),
        findings,
        iteration,
        lineage,
        completed_round=meta.get("round_id") if reviewed else None,
        unreviewed_round=None if reviewed else meta.get("round_id"),
    )
    review_mod.save_consolidation(workspace, data)
    repeats = book.register_signature("design_review", review_mod.findings_signature(data))
    book.end(
        token,
        "ok",
        {
            "iteration": iteration,
            # Which freeze this event paid for. The sha repeats when a plan is
            # reviewed again; this does not, and it names the archived report
            # the findings of this round are in.
            "round_id": meta.get("round_id") or "",
            "reviewers": run_dicts,
            "findings": data["counts"].get("findings_total"),
            "identical_rounds": repeats,
        },
        # Summed over the panel, not the wall clock of the batch: three
        # reviewers running in parallel for 25 minutes delegated 75 minutes of
        # execution, and the budget is on delegated execution. Less what each
        # run spent with the machine asleep.
        charged_seconds=sum(run.duration - run.suspended for run in runs),
        suspended_seconds=sum(run.suspended for run in runs),
    )
    if repeats > 1:
        _err(
            "note: design round %d produced the same findings as the previous round -- "
            "the last revision changed nothing that the reviewers can see." % iteration
        )
    for reviewer_id in stale:
        _err("note: %s has no report for this plan; its earlier report was ignored" % reviewer_id)

    ok, failed, partial = review_mod.summarise_runs(runs)
    if args.json:
        _emit_json(
            {
                "ok": ok,
                "failed": failed,
                "partial": partial,
                "reviewers": run_dicts,
                "counts": data["counts"],
                "plan": meta["plan"],
            }
        )
    else:
        for run in runs:
            _out(_reviewer_line(run))
        _out("")
        _out(_panel_summary(ok, failed, partial))
        _out("Consolidated: %s" % workspace.relative(workspace.consolidated_md_path))
    if ok == 0 and (failed or partial):
        return 1
    return 0


def cmd_review_run(args: argparse.Namespace) -> int:
    loaded = _load_or_die(args.cwd)
    # One run's override of review.context.surrounding, for measuring what the
    # context does on one snapshot. None when not given, and then nothing below
    # differs from a run without the flag.
    override = getattr(args, "surrounding", None)
    if args.design and override:
        _err("--surrounding applies to the code review only: a design round carries no surrounding context.")
        return 2
    declared = bool(getattr(args, "high_risk", False))
    if args.design and declared:
        _err("--high-risk applies to the code review only: a design round runs every configured reviewer.")
        return 2
    if args.design:
        return _run_design_review(args, loaded)
    workspace = _workspace(args)
    configured = loaded.reviewers()
    reviewers = configured
    if args.only:
        wanted = set(args.only)
        reviewers = [r for r in configured if r.get("id") in wanted or r.get("role") in wanted]
    if not reviewers and not args.only:
        _out("No reviewers configured -- skipping the independent-review stage.")
        lineage = _lineage(args, workspace)
        meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
        data = review_mod.build_consolidation(
            workspace,
            [],
            [],
            _iteration(args, workspace, lineage),
            lineage,
            completed_round=meta.get("round_id"),
        )
        review_mod.save_consolidation(workspace, data)
        return 0
    if not reviewers:
        _err("--only %s matched no configured reviewer" % " ".join(args.only))
        return 2

    settings = loaded.review_settings()
    context_settings = dict(loaded.context_settings())
    if override:
        context_settings["surrounding"] = override
    if not os.path.isfile(workspace.snapshot_path):
        try:
            review_mod.create_snapshot(
                workspace,
                args.base,
                exclude=settings.get("exclude"),
                incremental=bool(settings.get("incremental_rounds", True)),
                surrounding=context_mod.surrounding_mode(context_settings.get("surrounding")),
            )
        except review_mod.ReviewError as exc:
            _err(str(exc))
            return 2
    lineage = _lineage(args, workspace)
    iteration = _iteration(args, workspace, lineage)
    max_iterations = int(settings.get("max_review_iterations", 2))
    if iteration > max_iterations and not args.force:
        _err(
            "refusing to run review round %d: the budget is %d rounds "
            "(review.max_review_iterations)." % (iteration, max_iterations)
        )
        _err(
            "The round that reached the limit still gets its fix and re-test; only the "
            "re-review is refused. Report what is still open, or pass --force to override."
        )
        return ledger_mod.EXIT_BUDGET_EXHAUSTED

    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
    if override and meta.get("incremental_from"):
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
    plan = _round_plan(loaded, settings, workspace, meta, reviewers, declared, bool(args.only))
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

    # After the gate, because a round the gate already refused has no reason to
    # be measured, and before anything is charged or run.
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
        budget_chars,
        max_chars,
        change_chars,
        inline_chars,
        args.force,
        iteration,
        {"optimization": plan.to_dict()},
    )
    if refusal is not None:
        return refusal
    # Before anything is charged: an asked-for measurement that would measure
    # nothing, or that would throw away a decision made on this snapshot, is
    # refused rather than billed. Without the flag neither check runs.
    rerun = override is not None and _measurement_rerun(workspace, meta, lineage, iteration)
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
    # Forced past it: the round runs, and every record of it says so.
    over_budget = review_mod.over_context(budget_chars, max_chars)

    if plan.gate == opt_mod.GATE_WARN:
        _err(plan.gate_note())
    if plan.reviewer_limit is not None and not args.only:
        reviewers = opt_mod.choose_reviewers(reviewers, plan.reviewer_limit)
        _err(
            "note: %s (%s). Cross-model disagreement is what a second reviewer buys; "
            "raise optimization.level or the low_risk thresholds to keep it."
            % (plan.reviewer_note(), ", ".join(str(r.get("id")) for r in reviewers))
        )

    book = _ledger(args, workspace)
    book.clear_stalls()
    refusal = _refuse_if_runtime_spent(book, "review", args.force)
    if refusal is not None:
        return refusal
    batch_timeout = args.timeout or int(settings.get("timeout_seconds", 1800))
    token = book.begin(
        "review",
        {"iteration": iteration, "reviewers": [str(r.get("id")) for r in reviewers]},
        deadline=batch_timeout,
    )

    idle_timeout = args.idle_timeout
    if idle_timeout is None:
        idle_timeout = settings.get("idle_timeout_seconds")
    max_findings = plan.max_findings
    warned = _warn_unenforced(loaded, reviewers)
    try:
        runs = review_mod.run_reviews(
            reviewers,
            workspace,
            parallel=not args.sequential and bool(settings.get("parallel", True)),
            timeout=batch_timeout,
            extra_context=args.context or "",
            idle_timeout=idle_timeout,
            max_findings=max_findings,
            over_budget=over_budget,
            budget_chars=budget_chars,
            inline_chars=inline_chars,
            surrounding=adoption,
            refusals=_reviewer_refusals(loaded, reviewers),
        )
    except review_mod.ReviewError as exc:
        book.end(token, "failed", {"error": str(exc)})
        _err(str(exc))
        return 2
    context_on = adoption.mode != "none"

    for run in runs:
        if run.invoked:
            book.record_usage("review", run.usage.to_dict(), label=str(run.reviewer.get("id") or "reviewer"))
    _report_run_warnings(runs, warned)
    run_dicts = [run.to_dict() for run in runs]
    # Consolidate from every configured reviewer's report, not only the ones
    # that just ran: with --only that would otherwise overwrite the report with
    # a subset and discard the other reviewers' findings and triage. Reports
    # from an earlier snapshot are skipped rather than mixed in, and so are
    # those of a conditional reviewer this round left out.
    stamp = review_mod.current_snapshot_stamp(workspace)
    findings, stale = review_mod.read_reports(
        workspace, [str(r.get("id")) for r in configured if str(r.get("id")) not in excluded], stamp
    )
    # Passed whether or not anyone returned: nothing is approved over a code
    # round, so the id only has to name the round this report was built for.
    data = review_mod.build_consolidation(
        workspace,
        [entry for entry in _merge_runs(workspace, run_dicts) if str(entry.get("id")) not in excluded],
        findings,
        iteration,
        lineage,
        completed_round=meta.get("round_id"),
    )
    if override:
        # The triage as this run built it, so the second run of a pair can tell
        # a decision made on this snapshot from one carried in with a finding.
        data["measurement"] = {
            "surrounding": override,
            "rerun": rerun,
            "triage_at_build": {entry["key"]: _triage_record(entry) for entry in data["findings"]},
        }
    review_mod.save_consolidation(workspace, data)
    # The second run of a pair is the same snapshot reviewed again on purpose,
    # not a fix that changed nothing, so it registers no signature: the pair's
    # signature is its first run's.
    if rerun:
        repeats = book.repeats("review")
    else:
        repeats = book.register_signature("review", review_mod.findings_signature(data))
    detail = {
        "iteration": iteration,
        # Which freeze this event paid for: the sha repeats when the same tree
        # is frozen again, this does not. See ``review_mod.round_key``.
        "round_id": meta.get("round_id") or "",
        "reviewers": run_dicts,
        "findings": data["counts"].get("findings_total"),
        "identical_rounds": repeats,
        "optimization": plan.to_dict(),
    }
    measurement = None
    if override:
        measurement = _measurement_block(
            meta, workspace, book, override, rerun, _measurement_inputs(args, plan, inline_chars, max_chars)
        )
        detail["measurement"] = measurement
    if context_on and any("surrounding" in run for run in run_dicts):
        # Sizes and counts only: ``optimization report`` splits rounds on it,
        # and the names are on every reviewer entry beside it. Only when a
        # reviewer's prompt was built with it: a round whose every reviewer
        # fell over first showed no one any context, and must not be counted
        # as a round with it.
        detail["surrounding"] = adoption.summary()
    book.end(
        token,
        "ok",
        detail,
        # Per reviewer, as above: the panel is the unit that was delegated.
        charged_seconds=sum(run.duration - run.suspended for run in runs),
        suspended_seconds=sum(run.suspended for run in runs),
    )
    if repeats > 1 and not rerun:
        _err(
            "note: round %d produced the same findings as the previous round -- "
            "the last fix changed nothing that the reviewers can see." % iteration
        )
    for reviewer_id in stale:
        _err("note: %s has no report for this snapshot; its earlier report was ignored" % reviewer_id)
    if max_findings:
        for run in runs:
            if run.findings > max_findings:
                _err(
                    "note: %s returned %d findings against a cap of %d; all are kept, "
                    "but its output cost more than it needed to"
                    % (run.reviewer.get("id"), run.findings, max_findings)
                )

    ok, failed, partial = review_mod.summarise_runs(runs)
    if args.json:
        payload = {
            "ok": ok,
            "failed": failed,
            "partial": partial,
            "reviewers": run_dicts,
            "counts": data["counts"],
            "optimization": plan.to_dict(),
        }
        if context_on:
            payload["surrounding"] = adoption.record()
        if measurement is not None:
            payload["measurement"] = measurement
        _emit_json(payload)
    else:
        for run in runs:
            _out(_reviewer_line(run))
        _out("")
        _out(_panel_summary(ok, failed, partial))
        if context_on:
            line = "Surrounding context: %s" % _adoption_line(adoption)
            if override:
                line += " (--surrounding enclosing for this run)"
            _out(line)
        elif override:
            _out(
                "Surrounding context: none (--surrounding none for this run; "
                "review.context.surrounding unchanged)"
            )
        _out("Consolidated: %s" % workspace.relative(workspace.consolidated_md_path))
    if ok == 0 and (failed or partial):
        return 1
    return 0


def _condition_excluded(workspace: ws.Workspace) -> set:
    """The conditional reviewers the last code round on this snapshot left out.

    Read off that round's own event, so a re-consolidation reads the reports
    the round did and no others: a left-out reviewer's report from an earlier
    run of the same snapshot is not brought back by rebuilding.
    """
    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
    round_id = str(meta.get("round_id") or "")
    if not round_id:
        return set()
    events = workspace.read_state().get("events") or []
    for event in reversed(events if isinstance(events, list) else []):
        if not isinstance(event, dict) or event.get("stage") != "review" or event.get("status") != "ok":
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


def cmd_review_consolidate(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    workspace = _review_workspace(args)
    reviewer_ids = [str(r.get("id")) for r in loaded.reviewers()]
    excluded: set = set()
    if not getattr(args, "design", False):
        excluded = _condition_excluded(workspace)
        reviewer_ids = [name for name in reviewer_ids if name not in excluded]
    stamp = review_mod.current_snapshot_stamp(workspace)
    findings, stale = review_mod.read_reports(workspace, reviewer_ids, stamp)
    previous = ws.read_json(workspace.consolidated_json_path, {}) or {}
    # The same reviewers left out of the table as out of the findings, or one
    # the round sat out would be listed and counted again.
    entries = [entry for entry in previous.get("reviewers", []) if str(entry.get("id")) not in excluded]
    lineage = _lineage(args, workspace)
    data = review_mod.build_consolidation(
        workspace, entries, findings, _iteration(args, workspace, lineage), lineage
    )
    for reviewer_id in stale:
        _err("note: %s's report predates the current snapshot and was ignored" % reviewer_id)
    review_mod.save_consolidation(workspace, data)
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


def cmd_review_triage(args: argparse.Namespace) -> int:
    workspace = _review_workspace(args)
    data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    if not data:
        _no_review_yet(args)
        return 2
    try:
        for finding_id in args.ids:
            review_mod.set_triage(data, finding_id, args.status, args.note or "")
    except review_mod.ReviewError as exc:
        _err(str(exc))
        return 2
    review_mod.save_consolidation(workspace, data)
    _out("Triaged %s as %s" % (", ".join(args.ids), args.status))
    return 0


def cmd_review_fix_brief(args: argparse.Namespace) -> int:
    workspace = _review_workspace(args)
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


def cmd_review_status(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    workspace = _review_workspace(args)
    # The ledger, the run log and the plan belong to the workflow, not to the
    # review directory `--design` points at.
    base = _workspace(args)
    data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    settings = loaded.review_settings()
    severities = tuple(settings.get("re_review_severities") or ("critical", "high"))
    blocking = review_mod.unresolved_blocking(data, severities)
    accepted = bool(review_mod.accepted_findings(data))
    iteration = int(data.get("iteration", 0) or 0)
    # Read only: stalls are `status`'s to clear.
    book = _ledger(args, base)
    events = [event for event in (base.read_state().get("events") or []) if isinstance(event, dict)]
    # Each budget is reported under the name of the setting it came from, so a
    # consumer holding both payloads can tell which one it was handed. The code
    # review's key is what it always was; only --design carries the other name.
    if args.design:
        budget_key = "max_iterations"
        max_iterations = int(loaded.design_review_settings().get("max_iterations", 2))
        final = _design_final_pass(
            blocking,
            iteration,
            max_iterations,
            approval_mod.findings_of_current_plan(base, approval_mod.read_plan(base)[0]),
            _ran_since_last_round(events, "design_review", "architect", counts=_wrote_plan(base))[0],
            book.remaining("architect"),
            _approved_as_recorded(
                approval_mod.current(base, bool(loaded.design_settings().get("require_approval")))
            ),
            approval_mod.implemented_since_plan(base, events),
            accepted,
        )
        repeats = book.repeats("design_review")
    else:
        budget_key = "max_review_iterations"
        max_iterations = int(settings.get("max_review_iterations", 2))
        fixed, retested = _ran_since_last_round(events, "review", "review_fixer", ("test", "re-test"))
        final = _code_final_pass(
            blocking, iteration, max_iterations, fixed, retested, book.remaining("review_fixer"), accepted
        )
        repeats = book.repeats("review")
    counts = data.get("counts", {})
    # No consolidated report at all is the only real "none": nothing has been
    # reviewed. A report with no `coverage` block is a round that happened
    # before coverage was recorded, and calling that `round none, change none`
    # would claim it had been measured and found empty. `render_consolidation`
    # leaves its line out for exactly that reason, and the two commands read
    # the same file -- they must not describe it differently.
    coverage = data.get("coverage")
    if not data:
        coverage = {
            "round": "none",
            "change": "none",
            "unverified_since": None,
            "change_chars": None,
            "inline_chars": None,
        }
    elif not isinstance(coverage, dict):
        coverage = None
    payload = {
        "iteration": iteration,
        budget_key: max_iterations,
        "blocking": [f["id"] for f in blocking],
        "blocking_count": len(blocking),
        "accepted_count": len(review_mod.accepted_findings(data)),
        "re_review_recommended": bool(blocking) and iteration < max_iterations,
        "iteration_budget_exhausted": iteration >= max_iterations,
        "coverage": coverage,
        # Counted over the snapshot the `coverage` beside it describes, because
        # this is the number printed in the same breath as that value. The
        # table-wide column is still in `counts`, under its own name.
        "reviewers_partial": review_mod.snapshot_reviewers(counts, "partial"),
        # A round that only ran because a human forced it past
        # review.context.max_chars. False for every round that was not, and
        # for every report written before the limit existed -- which is the
        # true answer for all of them: none of them was forced past it.
        "over_budget": bool((data.get("snapshot") or {}).get("over_budget")),
        "counts": counts,
    }
    # Only when the report recorded one, as `consolidated.md` does.
    surrounding = data.get("surrounding")
    if isinstance(surrounding, dict):
        payload["surrounding"] = surrounding
    decision = None
    if args.design:
        payload["final_revision"] = final["state"]
        payload["final_revision_pending"] = final["pending"]
        decision = _design_decision(loaded, base)
        payload["enabled"] = decision.run
        payload["mode"] = decision.mode
        payload["reason"] = decision.reason or None
    else:
        payload["final_fix"] = final["state"]
        payload["final_fix_pending"] = final["pending"]
    if args.json:
        _emit_json(payload)
    else:
        if decision is not None:
            _out("design review: %s" % decision.label())
        _out("iteration %d/%d" % (iteration, max_iterations))
        _out("accepted findings: %d" % payload["accepted_count"])
        _out("blocking (%s): %d %s" % ("/".join(severities), len(blocking), ", ".join(payload["blocking"])))
        _out("re-review recommended: %s" % ("yes" if payload["re_review_recommended"] else "no"))
        if coverage is None:
            _out("coverage: not recorded -- this report predates it")
        else:
            _out("coverage: round %s, change %s" % (coverage.get("round"), coverage.get("change")))
        if payload["over_budget"]:
            _out(
                "over budget: this round was sent past review.context.max_chars by --force; "
                "say so when you report it"
            )
        if isinstance(surrounding, dict):
            for line in _surrounding_status_lines(surrounding):
                _out(line)
        for line in _coverage_advice(
            coverage or {},
            counts,
            payload["iteration_budget_exhausted"],
            _configured_inline_chars(loaded),
            args.design,
        ):
            _out(line)
        if payload["iteration_budget_exhausted"] and blocking:
            _out("iteration budget exhausted -- %s" % _final_pass_advice(args.design, final, repeats))
    return 0


def _surrounding_status_lines(block: Dict[str, Any]) -> List[str]:
    """The context this snapshot's reviewers were shown, and what was left out by name."""
    if block.get("shared") is False:
        records = list((block.get("by_reviewer") or {}).items())
    else:
        records = [("", block)]
    lines = []
    for reviewer_id, record in records:
        label = "surrounding context (%s)" % reviewer_id if reviewer_id else "surrounding context"
        if not isinstance(record, dict):
            lines.append("%s: none (ran with review.context.surrounding none)" % label)
            continue
        lines.append("%s: %s" % (label, context_mod.status_line(record)))
        trimmed = [c for c in record.get("trimmed") or [] if isinstance(c, dict)]
        for candidate in trimmed[:5]:
            lines.append(
                "  left out: %s:%s-%s %s"
                % (
                    candidate.get("path"),
                    candidate.get("start"),
                    candidate.get("end"),
                    candidate.get("symbol"),
                )
            )
        if len(trimmed) > 5:
            lines.append("  and %d more" % (len(trimmed) - 5))
        skipped = [entry for entry in record.get("skipped") or [] if isinstance(entry, dict)]
        for entry in skipped[:5]:
            lines.append("  not extracted: %s -- %s" % (entry.get("path"), entry.get("reason") or "?"))
        if len(skipped) > 5:
            lines.append("  and %d more not extracted" % (len(skipped) - 5))
    return lines


def _final_pass_advice(design: bool, final: Dict[str, Any], repeats: int) -> str:
    """What `review status` says to do once the round budget is spent."""
    state = final["state"]
    if design and state == "pending":
        advice = (
            "fold the accepted findings into the plan once more (review fix-brief --design, then "
            "run architect) and do not re-review it; then present the plan and the findings to the "
            "user and ask"
        )
    elif design and state == "done" and final.get("plan_changed") is True:
        advice = (
            "the plan was revised after this round and is not re-reviewed; present it with the "
            "findings still open from the earlier revision and ask"
        )
    elif design and state == "done" and final.get("plan_changed") is False:
        advice = (
            "the architect ran after this round and left the plan unchanged; present the plan and "
            "the open findings and ask whether to approve over them or to triage them again"
        )
    elif design and state == "approved":
        advice = "the plan is approved over the open findings; do not revise or re-review it"
    elif design and state == "blocked":
        advice = (
            "the accepted findings are not folded in and no architect attempt is left "
            "(budgets.architect); report them and ask whether to approve over them (`design approve`) "
            "or to free an attempt (`budget reset`) and revise"
        )
    elif not design and state == "pending":
        advice = (
            "fix the accepted findings once more (review fix-brief, then run review_fixer), re-test "
            "and record it (state record test ok|failed), and do not re-review; then report what is "
            "still open"
        )
    elif not design and state == "retest":
        advice = (
            "the fix after this round is not re-reviewed; re-run the tests, record them "
            "(state record test ok|failed), then report the remaining findings"
        )
    elif not design and state == "done":
        advice = "fixed and re-tested after this round, not re-reviewed; report the remaining findings"
    elif not design and state == "blocked":
        advice = (
            "the accepted findings are not fixed and no review_fixer attempt is left "
            "(budgets.review_fixer); report them, or free an attempt (`budget reset`) and fix"
        )
    else:
        # `implemented`, `unaccepted` (nothing to fold in or fix), and a
        # design round with no frozen plan to compare.
        advice = "report the remaining findings instead of looping"
    # The repeat does not stop a revision or fix still owed; it is said so the
    # report can.
    if repeats > 1 and state in ("pending", "retest"):
        advice += " (this round repeated the previous round's findings)"
    return advice


def _configured_inline_chars(loaded: config_mod.LoadedConfig) -> int:
    """``review.context.inline_chars``, resolved without trusting the file.

    ``cmd_review_status`` loads with ``validate_result=False`` on purpose: it
    is the one review command written to answer while the configuration is
    broken. So the value here can be whatever a hand edit left behind --
    ``400k``, a list, ``true`` -- and ``int()`` on it raises ``ValueError``,
    which ``main`` does not catch. That is the command written to survive a
    broken config dying on one, while every other command reports "invalid
    configuration" and exits 2. Anything that is not a plain integer falls
    back to the shipped default, which is what an unreadable setting is worth.
    Thread any further setting into this command the same way.
    """
    value = loaded.context_settings().get("inline_chars")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return review_mod.default_inline_chars()


def _coverage_advice(
    coverage: Dict[str, Any],
    counts: Dict[str, Any],
    budget_spent: bool,
    inline_chars: int,
    design: bool = False,
) -> List[str]:
    """What to do about an unverified coverage, if anything.

    Each line names the one action that changes the answer. Another round is
    never it: the snapshot is frozen, so re-running it sends the same prompt
    and gets the same verdict. Narrowing what the round *shows* is never it
    either, which is why ``--base`` is not named here: a smaller diff is a
    smaller round, not a reviewed change, and pointing at the flag that moves
    the mark without moving the change is pointing at the way around it.

    The state is ``review_mod.coverage_state``, the same classification
    ``consolidated.md`` words: the two commands read one file and must not
    describe it differently -- an advice line telling the reader to re-snapshot
    with ``--full`` when what is missing is a reviewer sends them to redo the
    thing they just did.

    A design round is judged on ``.ai/plan.md`` itself, and none of the
    code-review remedies can be aimed at it -- ``review snapshot`` writes the
    code snapshot and there is no ``--design`` form of it. The only thing that
    makes an oversize plan reviewable is a shorter plan.

    ``inline_chars`` is the *configured* limit, not the one the round in the
    report was measured against, because these lines are about the next run
    and not the last one. It is also the second way out, and the one the
    report itself cannot name: a limit is a setting, and a reader who decides
    the prompt is worth paying for raises it rather than cutting the change up.

    The recorded limit is read too, through the size beside it. A body handed
    over as a file was over the limit of its own round, so a recorded size
    that fits the configured one says the limit has been raised since -- and
    then "the same snapshot gives the same answer" is false, splitting a
    change that already fits is wasted work, and raising a limit the reader
    has just raised is worse than saying nothing. That case gets its own line.
    """
    lines = []
    chars = coverage.get("change_chars")
    size = "{:,}".format(chars) if chars else "size unrecorded"
    limit = "{:,}".format(inline_chars)
    # Counted over this snapshot, like the coverage value it is quoted beside.
    partial = review_mod.snapshot_reviewers(counts, "partial")
    state = review_mod.coverage_state(coverage, counts)
    mark = review_mod.unverified_phrase(coverage)
    if state == "round_unverified":
        # ``coverage.change_chars`` is recorded for precisely this comparison.
        # No check that the recorded limit differs is needed: the body went
        # over as a file, so it was over the limit of its round, and a size at
        # or under the configured one says that limit is not this one.
        raised = isinstance(chars, int) and bool(chars) and inline_chars > 0 and chars <= inline_chars
        if raised and design:
            lines.append(
                "not a clean review: %d reviewer(s) partial, the plan (%s chars) was handed over as "
                "a file under a lower review.context.inline_chars. The limit is %s chars now, so "
                "the plan fits inline: run the design round again." % (partial, size, limit)
            )
        elif raised:
            lines.append(
                "not a clean review: %d reviewer(s) partial, the change body (%s chars) was handed "
                "over as a file under a lower review.context.inline_chars. The limit is %s chars now, "
                "so this snapshot fits inline: run review run against it again." % (partial, size, limit)
            )
        elif design:
            lines.append(
                "not a clean review: %d reviewer(s) partial, the plan (%s chars) was handed over "
                "as a file. Re-running the same plan gives the same answer: shorten .ai/plan.md "
                "to fit inline (<= %s chars), or raise review.context.inline_chars, then run the "
                "design round again." % (partial, size, limit)
            )
        else:
            lines.append(
                "not a clean review: %d reviewer(s) partial, the change body (%s chars) was handed "
                "over as a file. Re-running the same snapshot gives the same answer: split the "
                "change and review the parts, so each part fits inline (<= %s chars), or raise "
                "review.context.inline_chars, then snapshot again." % (partial, size, limit)
            )
    elif state == "no_reviewer":
        if design:
            lines.append("%s -- no reviewer has run against this plan; run the design round." % mark)
        else:
            lines.append(
                "%s -- no reviewer has run against this snapshot; run the reviewers against it "
                "with review run." % mark
            )
    elif state == "none_ok":
        if design:
            lines.append(
                "%s -- no reviewer came back ok for this plan; re-run the reviewers that did not." % mark
            )
        else:
            lines.append(
                "%s -- no reviewer came back ok for this snapshot; re-run the reviewers that did not." % mark
            )
    elif state == "fix_only":
        if design:
            lines.append(
                "%s -- no round has shown a reviewer the whole plan; shorten .ai/plan.md until it "
                "fits inline, then run the design round again." % mark
            )
        else:
            lines.append(
                "%s -- re-snapshot with --full once the whole change fits inline, then run again." % mark
            )
    if budget_spent and coverage.get("change") == "unverified":
        lines.append("iteration budget exhausted -- report the change as not reviewed in full")
    return lines


def _reviewed_something(event: Dict[str, Any]) -> bool:
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
        if stage == review_stage and _reviewed_something(event):
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


def _approved_as_recorded(info: Dict[str, Any]) -> bool:
    """Whether the recorded approval covers this plan and this design round.

    Read from the record rather than from ``state``: with
    ``design.require_approval`` off the state is ``not-required`` whatever was
    recorded, and a plan the user did approve is still one they approved.
    """
    return info.get("matches_current_plan") is True and info.get("reviewed_since_approval") is False


def _design_final_pass(
    blocking: List[Dict[str, Any]],
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


def _code_final_pass(
    blocking: List[Dict[str, Any]],
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
