"""One review round, code or design: budgets, the panel run, consolidation, the report."""

from __future__ import annotations

import argparse
import contextlib
import threading
import time
from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

from . import activity, instructions
from . import config as config_mod
from . import config_policy as policy_mod
from . import context as context_mod
from . import ledger as ledger_mod
from . import optimization as opt_mod
from . import review as review_mod
from . import workspace as ws
from .cli_common import (
    _emit_json,
    _err,
    _ledger,
    _out,
)
from .providers import MODE_REVIEW, WARNED_ENFORCEMENT, get_provider


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


def _warn_unenforced(
    loaded: config_mod.LoadedConfig, reviewers: List[Dict[str, Any]], panel: str = "code"
) -> Dict[str, str]:
    """One line per reviewer about to run on a provider that cannot be held to reading.

    A reviewer refused for coming with the project file is not warned about:
    it does not run. Returns the lines printed, by reviewer id. ``panel`` is
    the panel the round runs, since an id can name a seat in each.
    """
    refused = list(policy_mod.project_raw_arg_refusals(loaded))
    lines = policy_mod.reviewer_enforcement_warnings(
        loaded.data, refused, panel, design_origins=loaded.design_reviewer_origins
    )
    printed: Dict[str, str] = {}
    for reviewer in reviewers:
        reviewer_id = str(reviewer.get("id") or "")
        line = lines.get(reviewer_id)
        if line:
            _err("warning: %s" % line)
            printed[reviewer_id] = line
    return printed


def _reviewer_refusals(
    loaded: config_mod.LoadedConfig, reviewers: List[Dict[str, Any]], panel: str = "code"
) -> Dict[str, str]:
    """Why each reviewer about to run is not run, by reviewer id, in ``panel``.

    The configuration's refusals, and one decided on the live report: a
    reviewer from the project file whose adapter reports, when asked now, that
    it cannot be held to reading -- one whose report is not static, which the
    configuration's refusals never ask.
    """
    refusals = dict(policy_mod.reviewer_raw_arg_refusals(loaded, panel))
    from_project = policy_mod.reviewer_provider_refusals(loaded, panel)
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
    *,
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


class _ReviewKind(NamedTuple):
    """What a code round and a design round say and record differently."""

    #: The ledger and run-log stage.
    stage: str
    #: What the refusals call the round.
    label: str
    #: The setting the round budget is read from.
    budget_setting: str
    #: What the round that reached the limit still gets.
    leftover: str
    #: What the identical-findings note calls a round, and what came between two.
    round_word: str
    change_word: str
    #: What a stale report has no report for.
    subject: str
    #: Put before a reviewer's id in its usage label.
    usage_prefix: str
    #: The reviewer panel the round runs: ``code`` or ``design``.
    panel: str


_CODE = _ReviewKind(
    stage="review",
    label="review",
    budget_setting="review.max_review_iterations",
    leftover="fix and re-test",
    round_word="round",
    change_word="fix",
    subject="snapshot",
    usage_prefix="",
    panel="code",
)

_DESIGN = _ReviewKind(
    stage="design_review",
    label="design review",
    budget_setting="review.design.max_iterations",
    leftover="revision",
    round_word="design round",
    change_word="revision",
    subject="plan",
    usage_prefix="design:",
    panel="design",
)


#: What both paths print when there is no panel to run.
_NO_REVIEWERS = "No reviewers configured -- skipping the independent-review stage."


def _save_skipped_round(
    workspace: ws.Workspace, iteration: int, lineage: str, round_id: Optional[str]
) -> int:
    """Record a round with no panel against the freeze it occupies. Exits 0."""

    def skipped(_: Dict[str, Any]) -> Dict[str, Any]:
        return review_mod.build_consolidation(workspace, [], [], iteration, lineage, completed_round=round_id)

    review_mod.update_consolidation(workspace, skipped)
    return 0


def _only_reviewers(configured: List[Dict[str, Any]], only: List[str]) -> List[Dict[str, Any]]:
    """The configured reviewers ``--only`` names, by id or by role."""
    wanted = set(only)
    return [r for r in configured if r.get("id") in wanted or r.get("role") in wanted]


def _refuse_unmatched_only(only: List[str]) -> int:
    _err("--only %s matched no configured reviewer" % " ".join(only))
    return 2


def _refuse_if_rounds_spent(
    kind: _ReviewKind, iteration: int, max_iterations: int, force: bool
) -> Optional[int]:
    """The round budget, refused in ``kind``'s words. Each caller reads its own setting."""
    if iteration <= max_iterations or force:
        return None
    _err(
        "refusing to run %s round %d: the budget is %d rounds (%s)."
        % (kind.label, iteration, max_iterations, kind.budget_setting)
    )
    _err(
        "The round that reached the limit still gets its %s; only the re-review is refused. "
        "Report what is still open, or pass --force to override." % kind.leftover
    )
    return ledger_mod.EXIT_BUDGET_EXHAUSTED


def _open_ledger(args: argparse.Namespace, workspace: ws.Workspace) -> ledger_mod.Ledger:
    """The ledger a round is charged to, with its stalls cleared for the runtime refusal to read."""
    book = _ledger(args, workspace)
    # Before the refusal decides: a stage whose process is gone has no charge
    # to answer for, and its absence is part of what the runtime budget says.
    book.clear_stalls()
    return book


class _RoundContext(NamedTuple):
    """What every step of one review round reads, once it is allowed to run."""

    args: argparse.Namespace
    loaded: config_mod.LoadedConfig
    settings: Dict[str, Any]
    workspace: ws.Workspace
    book: ledger_mod.Ledger
    kind: _ReviewKind
    iteration: int


class _PanelRun(NamedTuple):
    """A panel that ran: its in-flight token, its runs, and the warnings already printed."""

    token: str
    runs: List[review_mod.ReviewerRun]
    warned: Dict[str, str]


def _run_panel(
    ctx: _RoundContext,
    reviewers: List[Dict[str, Any]],
    *,
    max_findings: int,
    over_budget: bool,
    budget_chars: int,
    inline_chars: int,
    extra_context: str = "",
    prompt_for: Optional[Callable[[Dict[str, Any]], review_mod.BuiltPrompt]] = None,
    surrounding: Optional[context_mod.Adoption] = None,
) -> Union[int, _PanelRun]:
    """Open the round's ledger entry and run the panel, or the exit code of a round that could not."""
    args, settings, book = ctx.args, ctx.settings, ctx.book
    batch_timeout = args.timeout or config_mod.review_timeout(ctx.loaded).seconds
    token = book.begin(
        ctx.kind.stage,
        {"iteration": ctx.iteration, "reviewers": [str(r.get("id")) for r in reviewers]},
        deadline=batch_timeout,
    )
    idle_timeout = args.idle_timeout
    if idle_timeout is None:
        idle_timeout = config_mod.idle_timeout(ctx.loaded, MODE_REVIEW).seconds
    warned = _warn_unenforced(ctx.loaded, reviewers, ctx.kind.panel)
    design = ctx.kind is _DESIGN
    _note_unread_instructions(ctx.loaded, reviewers, design)

    def code_instructions(reviewer: Dict[str, Any]) -> str:
        return instructions.review_block(instructions.for_seat(ctx.loaded, reviewer, design=False))

    # The code path's alone: the design path's `prompt_for` carries its own.
    instructions_for = None if design else code_instructions
    progress = ctx.loaded.progress_settings()
    echo = _ProgressEcho(progress.quiet_notice_seconds) if getattr(args, "progress", False) else None
    watch: ContextManager[None] = contextlib.nullcontext()
    if echo is not None and progress.quiet_notice_seconds > 0:
        watch = echo.watching(progress.interval_seconds)
    try:
        with watch:
            runs = review_mod.run_reviews(
                reviewers,
                ctx.workspace,
                review_mod.FanoutOptions(
                    parallel=not args.sequential and bool(settings.get("parallel", True)),
                    timeout=batch_timeout,
                    extra_context=extra_context,
                    idle_timeout=idle_timeout,
                    max_findings=max_findings,
                    over_budget=over_budget,
                    budget_chars=budget_chars,
                    # The same number to both: `prompt_for` decides the delivery and
                    # `run_reviews` records and words it, and a round that took them
                    # from two places would sooner or later take two different ones.
                    inline_chars=inline_chars,
                    prompt_for=prompt_for,
                    surrounding=surrounding,
                    refusals=_reviewer_refusals(ctx.loaded, reviewers, ctx.kind.panel),
                    activity_for=echo.sink_for if echo is not None else None,
                    instructions_for=instructions_for,
                ),
            )
    except review_mod.ReviewError as exc:
        book.end(token, "failed", {"error": str(exc)})
        _err(str(exc))
        return 2
    return _PanelRun(token, runs, warned)


def _note_unread_instructions(
    loaded: config_mod.LoadedConfig, reviewers: List[Dict[str, Any]], design: bool
) -> None:
    """A ``note:`` per instructions file the panel's seats could not read, each once.

    The reviewers run without its text, as a role run does.
    """
    resolved = [instructions.for_seat(loaded, reviewer, design=design) for reviewer in reviewers]
    for line in instructions.problem_notes(resolved, "reviewing"):
        _err(line)


class _ProgressEcho:
    """``review run --progress``: a sink per reviewer that echoes its tool uses
    to stderr as ``[<reviewer> +mm:ss] <line>``. Nothing is written to a file.

    With ``quiet_notice_seconds`` above 0, :meth:`flag_quiet` adds the stuck
    notice for each reviewer still running that has been silent that long,
    and :meth:`watching` calls it once an interval for the round.
    """

    def __init__(self, quiet_notice_seconds: int = 0) -> None:
        self.quiet_notice_seconds = quiet_notice_seconds
        self._sinks: List[activity.Sink] = []
        self._lock = threading.Lock()

    def sink_for(self, reviewer_id: str) -> activity.Sink:
        tag = activity.clip(reviewer_id) or "reviewer"
        started = time.monotonic()

        def echo(line: str) -> None:
            minutes, seconds = divmod(int(time.monotonic() - started), 60)
            _err("[%s +%02d:%02d] %s" % (tag, minutes, seconds, line))

        sink = activity.Sink(echo=echo)
        with self._lock:
            self._sinks.append(sink)
        return sink

    def flag_quiet(self) -> None:
        """Echo the stuck notice for each running reviewer silent for the threshold or longer."""
        if self.quiet_notice_seconds <= 0:
            return
        with self._lock:
            sinks = list(self._sinks)
        for sink in sinks:
            if sink.ended:
                continue
            quiet = sink.quiet_seconds()
            if quiet >= self.quiet_notice_seconds:
                # Checked again under the echo lock: a reviewer that ended
                # since the check above must not be flagged after its `done:`.
                sink.say_if_running(activity.quiet_note(quiet))

    @contextlib.contextmanager
    def watching(self, interval: float) -> Iterator[None]:
        """Call :meth:`flag_quiet` every ``interval`` seconds while the block runs.

        A daemon thread, stopped and joined on the way out however the block
        ends, so it never outlives the round or holds up the exit. A check that
        raises is skipped, not fatal: the next interval still runs.
        """
        stop = threading.Event()

        def watch() -> None:
            while not stop.wait(interval):
                try:
                    self.flag_quiet()
                except Exception:
                    pass

        thread = threading.Thread(target=watch, name="review-progress-watch", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()


def _account_runs(ctx: _RoundContext, panel: _PanelRun, switched: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """Record what each run used and say what its adapter said; the runs as recorded.

    ``switched`` holds the ids that ran on their ``high_risk_model``: their
    entries say so with ``model_slot``. An entry without it is the usual slot.
    """
    for run in panel.runs:
        if run.invoked:
            # Prefixed, so a reviewer's design cost never merges into its code
            # cost in `tokens show` -- the same reasoning as `role:tier`.
            label = ctx.kind.usage_prefix + str(run.reviewer.get("id") or "reviewer")
            ctx.book.record_usage(ctx.kind.stage, run.usage.to_dict(), label=label)
    _report_run_warnings(panel.runs, panel.warned)
    entries = [run.to_dict() for run in panel.runs]
    for entry in entries:
        if str(entry.get("id")) in switched:
            entry["model_slot"] = opt_mod.HIGH_RISK_SLOT
    return entries


def _risk_models(
    reviewers: List[Dict[str, Any]], high_risk: bool, why: str
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Each seat on its ``high_risk_model`` on a high-risk round: ``(reviewers, switched ids)``.

    One note per seat that switched, saying why and from what to what.
    """
    chosen: List[Dict[str, Any]] = []
    switched: List[str] = []
    for reviewer in reviewers:
        run, moved = opt_mod.risk_model(reviewer, high_risk)
        if moved:
            switched.append(str(reviewer.get("id")))
            _err("note: %s" % opt_mod.risk_model_note(reviewer, run, why))
        chosen.append(run)
    return chosen, switched


def _consolidate_round(
    workspace: ws.Workspace,
    panel: Sequence[Dict[str, Any]],
    excluded: set,
    run_dicts: Optional[List[Dict[str, Any]]],
    iteration: int,
    lineage: str,
    *,
    completed_round: Optional[str] = None,
    unreviewed_round: Optional[str] = None,
    amend: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Build the round's report from the panel's reports and save it.

    The one place a code round, a design round and ``review consolidate``
    decide which reports and which reviewer entries a report is built from.
    Returns ``(data, stale)``: the saved report and the reviewers whose
    reports were written against another snapshot.

    Every reviewer of ``panel`` is read, not only the ones that just ran:
    with --only that would otherwise overwrite the report with a subset and
    discard the other reviewers' findings and triage. A reviewer in
    ``excluded`` sat the round out and is left out of the findings and of the
    reviewer table alike, or it would be listed and counted again.

    ``run_dicts`` is the runs this round recorded, merged into the last
    report's table (``_merge_runs``); None rebuilds without a run, and keeps
    that table as it stands. ``amend`` sees the report before it is saved.
    """
    ids = [str(r.get("id")) for r in panel if str(r.get("id")) not in excluded]
    findings, stale = review_mod.read_reports(workspace, ids, review_mod.current_snapshot_stamp(workspace))

    # The last report is read for its table and its triage, both inside the
    # lock ``review triage`` takes, so a decision made while the round was
    # being built is carried rather than written over.
    def rebuilt(previous: Dict[str, Any]) -> Dict[str, Any]:
        if run_dicts is None:
            entries = previous.get("reviewers", [])
        else:
            entries = _merge_runs(workspace, run_dicts)
        data = review_mod.build_consolidation(
            workspace,
            [entry for entry in entries if str(entry.get("id")) not in excluded],
            findings,
            iteration,
            lineage,
            completed_round=completed_round,
            unreviewed_round=unreviewed_round,
        )
        if amend is not None:
            amend(data)
        return data

    return cast(Dict[str, Any], review_mod.update_consolidation(workspace, rebuilt)), stale


def _round_detail(
    ctx: _RoundContext,
    meta: Dict[str, Any],
    data: Dict[str, Any],
    run_dicts: List[Dict[str, Any]],
    optimization: Dict[str, Any],
    rerun: bool = False,
) -> Dict[str, Any]:
    """The event detail a round that ran closes its ledger entry with."""
    return {
        "iteration": ctx.iteration,
        # Which freeze this event paid for: the sha repeats when the same plan
        # or tree is frozen again, this does not, and it names the archived
        # report the findings of this round are in. See ``review_mod.round_key``.
        "round_id": meta.get("round_id") or "",
        "reviewers": run_dicts,
        "findings": data["counts"].get("findings_total"),
        "identical_rounds": _round_repeats(ctx, data, rerun),
        # Who sat on the round and why, read back by the report and by a
        # re-consolidation.
        "optimization": optimization,
    }


def _round_repeats(ctx: _RoundContext, data: Dict[str, Any], rerun: bool = False) -> int:
    """How many rounds in a row have produced these findings."""
    # The second run of a pair is the same snapshot reviewed again on purpose,
    # not a fix that changed nothing, so it registers no signature: the pair's
    # signature is its first run's.
    if rerun:
        return ctx.book.repeats(ctx.kind.stage)
    return ctx.book.register_signature(ctx.kind.stage, review_mod.findings_signature(data))


def _end_round(
    ctx: _RoundContext, token: str, detail: Dict[str, Any], runs: List[review_mod.ReviewerRun]
) -> None:
    """Close the round's ledger entry with ``detail``, charged for the panel."""
    ctx.book.end(
        token,
        "ok",
        detail,
        # Summed over the panel, not the wall clock of the batch: three
        # reviewers running in parallel for 25 minutes delegated 75 minutes of
        # execution, and the budget is on delegated execution. Less what each
        # run spent with the machine asleep. The panel is the unit that was
        # delegated.
        charged_seconds=sum(run.duration - run.suspended for run in runs),
        suspended_seconds=sum(run.suspended for run in runs),
    )


def _round_notes(ctx: _RoundContext, repeats: int, stale: List[str], rerun: bool = False) -> None:
    """What the round found out about itself: nothing changed, or a report was left out."""
    kind = ctx.kind
    if repeats > 1 and not rerun:
        _err(
            "note: %s %d produced the same findings as the previous round -- "
            "the last %s changed nothing that the reviewers can see."
            % (kind.round_word, ctx.iteration, kind.change_word)
        )
    for reviewer_id in stale:
        _err(
            "note: %s has no report for this %s; its earlier report was ignored" % (reviewer_id, kind.subject)
        )


def _print_round(
    ctx: _RoundContext,
    runs: List[review_mod.ReviewerRun],
    run_dicts: List[Dict[str, Any]],
    data: Dict[str, Any],
    payload_extra: Dict[str, Any],
    extra_lines: Sequence[str] = (),
) -> int:
    """The round's report, as JSON or as text, and the exit code it ends with.

    ``payload_extra`` follows the keys every round has, in its own order, and
    ``extra_lines`` goes between the tally and the report's path.
    """
    ok, failed, partial = review_mod.summarise_runs(runs)
    if ctx.args.json:
        payload: Dict[str, Any] = {
            "ok": ok,
            "failed": failed,
            "partial": partial,
            "reviewers": run_dicts,
            "counts": data["counts"],
        }
        payload.update(payload_extra)
        _emit_json(payload)
    else:
        for run in runs:
            _out(_reviewer_line(run))
        _out("")
        _out(_panel_summary(ok, failed, partial))
        for line in extra_lines:
            _out(line)
        _out("Consolidated: %s" % ctx.workspace.relative(ctx.workspace.consolidated_md_path))
    if ok == 0 and (failed or partial):
        return 1
    return 0
