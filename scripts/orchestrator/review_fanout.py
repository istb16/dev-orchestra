"""Running every reviewer on the frozen change, in parallel and read-only."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import activity
from . import config as config_mod
from . import context as context_mod
from . import workspace as ws
from .providers import MODE_REVIEW, ModelResolutionError, RunResult, Usage, get_provider
from .review_common import (
    DEFAULT_MAX_FINDINGS,
    DESIGN_REVIEW_PROMPT_TEMPLATE,
    DESIGN_ROLE_GUIDANCE,
    MAX_EVIDENCE_LINES,
    MAX_FIX_LINES,
    REVIEW_PROMPT_TEMPLATE,
    ROLE_GUIDANCE,
)
from .review_parsing import parse_findings, unparsed_report_warning
from .review_snapshot import (
    ReviewError,
    current_snapshot_stamp,
    render_design_round_context,
    render_round_context,
    render_withheld,
)

# --------------------------------------------------------------------------- fan-out


def render_limits(max_findings: int = DEFAULT_MAX_FINDINGS) -> str:
    """The block that caps what a reviewer writes back.

    ``max_findings`` of 0 lifts the count cap; the rest of the block still
    applies, because a reviewer allowed to report everything is not thereby
    allowed to quote a whole file per finding or to open with a paragraph
    about how thorough it intends to be.

    Both forms open by saying that findings are what to produce. Only the
    capped form used to, and the one run observed returning a prose summary
    instead of finding blocks -- recorded ``unparsed``, and so counted as a
    failed review -- was uncapped. One run is not a cause, and this is not
    offered as the fix for it; but an instruction that is weaker in one mode
    than the other is worth levelling either way.
    """
    lines = ["Limits:"]
    if max_findings > 0:
        lines.append(
            "- Max %d findings. Over that, report the %d worst -- severity first, never padding."
            % (max_findings, max_findings)
        )
    else:
        lines.append("- Report every issue you can point at, one block each. No maximum.")
    lines += [
        "- Evidence: %d lines max. Fix: %d lines max." % (MAX_EVIDENCE_LINES, MAX_FIX_LINES),
        "- Only what you can point at in the code. No speculation. No duplicates.",
        "- Nothing worth fixing: reply with exactly NO_FINDINGS",
        "- Findings or NO_FINDINGS only. No preamble, no summary, no sign-off.",
    ]
    return "\n".join(lines)


class BuiltPrompt(NamedTuple):
    """A reviewer's prompt, and how much of the change it was able to carry.

    The delivery is decided here and recorded nowhere else. It depends on the
    change's length *and* on the settings and flags that shape the round, and
    none of those are known when ``review snapshot`` runs -- so writing it
    into the snapshot's metadata would create a second source of truth that
    disagrees with this one the moment anything changes between the snapshot
    and the run. What the reviewer was actually handed is what this says.
    """

    text: str
    #: "inline" -- the change body is in ``text``; "file" -- only its path is.
    #: "" is reserved for a run that fell over before a prompt was built, and
    #: is read as "not recorded" rather than as either answer.
    delivery: str
    change_chars: int


def default_inline_chars() -> int:
    """The shipped ``review.context.inline_chars``, read where it is written.

    Every default this tool has lives in ``default_config``, and this one is
    read from there rather than copied next to the code that applies it: two
    spellings of the same number are two answers the moment either moves.
    A caller holding a configuration passes its own number and never comes
    here.
    """
    return int(config_mod.default_config()["review"]["context"]["inline_chars"])


def _inline_limit(inline_chars: Optional[int]) -> int:
    """``None`` means the shipped default, as an explicit ``null`` does.

    Every entry point below takes the limit as an optional argument and
    resolves it through here, so "left unset" has one meaning in one place.
    """
    return default_inline_chars() if inline_chars is None else inline_chars


def prompt_delivery(change_text: str, inline_chars: Optional[int] = None) -> str:
    """Whether a change body goes into the prompt or over as a path.

    One function and one number for both paths: the code review measures its
    diff and the design review its plan, and a round that decided delivery
    differently from the round beside it would record two things under one
    name. ``None`` means the shipped default, which is what an explicit
    ``null`` in the configuration means too.
    """
    return delivery_of(len(change_text), inline_chars)


def delivery_of(change_chars: int, inline_chars: Optional[int] = None) -> str:
    """The same rule, for a caller that holds a size rather than the text.

    Split out for the refusal below, which has to say what forcing the round
    would get without building a 400,000-character string to ask. Public for
    ``review run``, which has to know the delivery before any prompt is
    built: the surrounding context is budgeted against it.
    """
    return "inline" if change_chars <= _inline_limit(inline_chars) else "file"


def over_context(change_chars: int, max_chars: int) -> bool:
    """Whether a change body is past the limit that refuses the round.

    Inclusive on the limit, like ``prompt_delivery``'s: the configured number
    is the largest change that still runs. ``max_chars`` of zero or less is no
    limit, which is what a caller reading a configuration that predates the
    setting ends up with.
    """
    return max_chars > 0 and change_chars > max_chars


def snapshot_chars(workspace: ws.Workspace) -> int:
    """The size the context budget measures: characters of the frozen diff.

    Characters, not the ``bytes`` already in the snapshot's metadata. The
    budget is about how much of a context window the change spends, which is
    what every other size in this module counts -- ``inline_chars``,
    ``Usage.prompt_chars``. A UTF-8 byte count is a different number, and it
    is largest exactly where the difference matters most.
    """
    return len(ws.read_text(workspace.snapshot_path))


def over_budget_note(
    budget_chars: int,
    max_chars: int,
    delivery_chars: int,
    inline_chars: Optional[int] = None,
    design: bool = False,
) -> List[str]:
    """Why a round was refused for size, and what the reader can do about it.

    Three things, in the order they are needed: the size against the limit,
    the ways to make the change smaller, and what ``--force`` would do. The
    middle one differs between the two paths because ``review snapshot`` has
    no ``--design`` form -- ``--base`` and ``review.exclude`` cannot be aimed
    at a plan, and pointing a reader at them would send them nowhere.

    Two sizes, and neither can stand in for the other. ``budget_chars`` is
    what this limit measures -- the plan *and* the request on the design path,
    because both go into every prompt unconditionally -- and it is the number
    the refusal states. ``delivery_chars`` is the body that would actually be
    handed over, the plan alone there, and only it can say what forcing would
    do. On the code path they are the same number and are still passed
    separately, so that every call site reads the same.
    """
    if design:
        narrow = (
            "Shorten it: the plan and the request it answers are measured together, and "
            "both go into every reviewer's prompt."
        )
    else:
        narrow = (
            "Narrow it: --base <rev>, review.exclude, or split the change and review the "
            "parts, then take the snapshot again."
        )
    return [
        "refusing to review a change body of %s chars: the limit is %s "
        "(review.context.max_chars)." % (ws.fmt_int(budget_chars), ws.fmt_int(max_chars)),
        narrow,
        "Nothing was reviewed, and in an automated workflow that is the answer to report: "
        "this change was not reviewed. Saying so is the point of the limit -- the "
        "alternative is calling an incomplete review complete.",
        "--force runs it anyway. It belongs to a human who has decided to pay for the "
        "round, not to the orchestrator. %s" % _forced_consequence(delivery_chars, inline_chars),
    ]


def _forced_consequence(delivery_chars: int, inline_chars: Optional[int] = None) -> str:
    """What forcing *this* round would record -- not what forcing records.

    Whether the reviewers come back ``partial`` is decided by
    ``prompt_delivery`` and ``review.context.inline_chars``, never by this
    budget. The shipped defaults make the two limits equal, so a change over
    ``max_chars`` is over the inline limit too and every reviewer is partial
    -- but that is a fact about *this* configuration, not about this code.
    ``inline_chars`` raised above ``max_chars``, or ``max_chars`` lowered
    below it, refuses rounds whose body would still have gone into the prompt
    whole, and promising partial there would be a promise this code does not
    keep. So it is computed, both ways, from the numbers actually in force.

    The size handed in is the one ``prompt_delivery`` decides on -- the plan
    alone on the design path, where the budget counted the request too. A
    plan that fits inline is delivered inline however far the pair went over.
    """
    limit = ws.fmt_int(_inline_limit(inline_chars))
    if delivery_of(delivery_chars, inline_chars) == "file":
        return (
            "The round is then recorded as over budget, and every reviewer comes back "
            "partial: the body is over review.context.inline_chars (%s), so it goes over "
            "as a file." % limit
        )
    return (
        "The round is then recorded as over budget. The body still fits in the prompt "
        "(review.context.inline_chars is %s), so it is not partial for that reason." % limit
    )


def _handover_note(change_chars: int, inline_chars: Optional[int] = None) -> str:
    """What a reviewer handed a path instead of a body is told.

    It states a fact and asks for nothing back. An earlier design had the
    prompt ask for a ``PARTIAL_REVIEW`` declaration, which fails three ways:
    a declaration cannot be tested for the case where it was *not* made,
    ``{limits}`` is the last thing in both templates and its "Findings or
    NO_FINDINGS only" overrides anything asked before it, and a reviewer's
    self-report is the one thing about a round this tool cannot check. The
    verdict comes from this tool's own inputs instead; this only makes sure
    the reviewer is not surprised by it.
    """
    return (
        "The change body is {:,} chars and is handed over as a file because it exceeds "
        "review.context.inline_chars ({:,}). This review is recorded as coverage-unverified "
        "whatever you answer. Read the whole file before judging; a paging tool needs "
        "more than one call.".format(change_chars, _inline_limit(inline_chars))
    )


def coverage_unverified_error(change_chars: int, inline_chars: Optional[int] = None) -> str:
    """The reason a ``partial`` run carries. Its findings are still real.

    It names the limit as well as the size: the limit is configuration now,
    so "over the inline limit" on its own no longer tells a reader which
    number this round was measured against.
    """
    return (
        "coverage unverified: the change body ({:,} chars) was handed over as a file, "
        "not inlined (over review.context.inline_chars, {:,}). Findings kept; "
        "not a clean review.".format(change_chars, _inline_limit(inline_chars))
    )


def _fenced(text: str, info: str) -> str:
    """``text`` in a fence it cannot close, so none of it reads as the prompt's own."""
    body = text.rstrip()
    fence = context_mod.fence_for(body)
    return "%s%s\n%s\n%s" % (fence, info, body, fence)


def build_review_prompt(
    reviewer: Dict[str, Any],
    workspace: ws.Workspace,
    diff_text: str,
    extra_context: str = "",
    template: Optional[str] = None,
    max_findings: int = DEFAULT_MAX_FINDINGS,
    inline_chars: Optional[int] = None,
    surrounding: Optional[context_mod.Adoption] = None,
) -> BuiltPrompt:
    """The code reviewer's prompt, built from the frozen snapshot.

    ``surrounding`` is the round's adopted context. It goes after every other
    note, and is nothing at all when the setting is off, so that prompt is
    the one this function always built.
    """
    role = str(reviewer.get("role") or "general")
    guidance = ROLE_GUIDANCE.get(
        role,
        "Review as a %s specialist. Concrete, evidence-backed issues in that perspective only." % role,
    )
    delivery = prompt_delivery(diff_text, inline_chars)
    if delivery == "inline":
        diff_section = _fenced(diff_text, "diff")
    else:
        diff_section = (
            "Diff too large to inline. Read it from this file, frozen for this review:\n\n"
            "    %s\n\nReview only what that diff contains." % workspace.relative(workspace.snapshot_path)
        )
        diff_section += "\n\n" + _handover_note(len(diff_text), inline_chars)
    meta = workspace.read_snapshot_meta()
    for note in (
        render_withheld(meta.get("withheld") or []),
        render_round_context(workspace, meta),
        context_mod.render_surrounding(surrounding),
    ):
        if note:
            diff_section += "\n\n" + note
    prompt = (template or REVIEW_PROMPT_TEMPLATE).format(
        reviewer_id=reviewer.get("id", "reviewer"),
        role=role,
        role_guidance=guidance,
        root=workspace.root,
        diff_section=diff_section,
        limits=render_limits(max_findings),
    )
    if extra_context.strip():
        prompt += "\n## Additional context\n\n%s\n" % extra_context.strip()
    return BuiltPrompt(prompt, delivery, len(diff_text))


def build_design_review_prompt(
    reviewer: Dict[str, Any],
    workspace: ws.Workspace,
    plan_text: str,
    request_text: str = "",
    extra_context: str = "",
    max_findings: int = DEFAULT_MAX_FINDINGS,
    inline_chars: Optional[int] = None,
) -> BuiltPrompt:
    """The reviewer prompt for a plan, built from the frozen design snapshot.

    The change body here is the plan -- that is what the round is reviewing
    and what ``review-target.md`` freezes. The request it answers is context
    that rides along, and goes in whatever its size, exactly as it always has.
    """
    role = str(reviewer.get("role") or "general")
    guidance = DESIGN_ROLE_GUIDANCE.get(
        role,
        "Review the plan as a %s specialist. Concrete, evidence-backed issues in that "
        "perspective only." % role,
    )
    delivery = prompt_delivery(plan_text, inline_chars)
    if delivery == "inline":
        plan_section = _fenced(plan_text, "markdown")
    else:
        plan_section = (
            "Plan too large to inline. Read it from this file, frozen for this review:\n\n"
            "    %s\n\nReview only what that plan contains." % workspace.relative(workspace.snapshot_path)
        )
        plan_section += "\n\n" + _handover_note(len(plan_text), inline_chars)
    meta = workspace.read_snapshot_meta()
    note = render_design_round_context(workspace, meta, plan_text)
    if note:
        plan_section += "\n\n" + note
    request_section = (
        _fenced(request_text, "markdown")
        if request_text.strip()
        else "Not recorded. Judge the plan against its own stated goal."
    )
    prompt = DESIGN_REVIEW_PROMPT_TEMPLATE.format(
        reviewer_id=reviewer.get("id", "reviewer"),
        role=role,
        role_guidance=guidance,
        root=workspace.root,
        request_section=request_section,
        plan_section=plan_section,
        limits=render_limits(max_findings),
    )
    if extra_context.strip():
        prompt += "\n## Additional context\n\n%s\n" % extra_context.strip()
    return BuiltPrompt(prompt, delivery, len(plan_text))


class RoundStamp(NamedTuple):
    """What a run was handed and which round it answers for, as its entry records it.

    Every run that got as far as a built prompt carries one; ``ReviewerRun``
    describes each field.
    """

    delivery: str
    change_chars: int
    inline_chars: int
    snapshot: str
    over_budget: bool
    budget_chars: int
    surrounding: Optional[Dict[str, Any]] = None

    @classmethod
    def empty(cls) -> RoundStamp:
        """The stamp of a run that fell over before there was a prompt to build."""
        return cls("", 0, 0, "", False, 0, None)


class ReviewerRun:
    def __init__(
        self,
        reviewer: Dict[str, Any],
        status: str,
        *,
        stamp: Optional[RoundStamp] = None,
        report_path: Optional[str] = None,
        error: str = "",
        model_display: str = "",
        duration: float = 0.0,
        findings: int = 0,
        usage: Optional[Usage] = None,
        invoked: bool = False,
        warnings: Optional[Sequence[str]] = None,
        suspended: float = 0.0,
    ) -> None:
        if stamp is None:
            stamp = RoundStamp.empty()
        self.reviewer = reviewer
        # ok | partial | failed | stalled | unparsed. Only "ok" counts as a
        # delivered review; "unparsed" means the CLI succeeded but its report
        # could not be read, and "partial" that the change body went over as a
        # file rather than in the prompt. Neither is ever a clean one.
        self.status = status
        self.report_path = report_path
        self.error = error
        self.model_display = model_display
        self.duration = duration
        #: How much of ``duration`` the machine spent asleep, as
        #: ``RunResult.suspended`` carries it. Not charged to the runtime budget.
        self.suspended = suspended
        self.findings = findings
        #: What this reviewer cost. Reviewers are the most duplicated stage in
        #: the pipeline -- the same diff, once per reviewer, once per round --
        #: so their cost is recorded per reviewer, not just per round.
        self.usage = usage or Usage()
        #: Whether a CLI was started. Defaults to False because the paths that
        #: construct a run without one -- an unknown provider, a model that
        #: will not resolve -- are exactly the ones that spent nothing.
        self.invoked = invoked
        #: How much of the change this reviewer was handed: see ``BuiltPrompt``.
        #: "" for a run that fell over before there was a prompt to build.
        self.delivery = stamp.delivery
        self.change_chars = stamp.change_chars
        #: What ``review.context.inline_chars`` was when ``delivery`` above was
        #: decided. Recorded for the reason ``budget_chars`` is: the limit is
        #: configuration and the shipped default is not the only answer, so a
        #: reader of a ``partial`` round who has only the size cannot tell
        #: whether it was a large change or a low limit that made it one.
        #: 0 for a run that fell over before there was a prompt to build.
        self.inline_chars = stamp.inline_chars
        #: Which snapshot this run was handed, stamped the way the reports are
        #: stamped and for the same reason: the reviewer table outlives the
        #: round it was written in, so an entry has to say what it answers for.
        #: "" for a run that fell over before there was a prompt to build.
        self.snapshot = stamp.snapshot
        #: Whether this round was sent past ``review.context.max_chars`` by
        #: ``--force``. Recorded here, and in the consolidation derived from
        #: here, for the reason ``BuiltPrompt`` gives about delivery: the
        #: limit and the flag belong to the run, not to the frozen file.
        self.over_budget = stamp.over_budget
        #: What ``review.context.max_chars`` measured for this round, which is
        #: not always ``change_chars``: on the design path the budget counts
        #: the plan and the request together while the body handed over is the
        #: plan alone. Recorded so the report can print the number the refusal
        #: would have used rather than the one delivery was decided by.
        self.budget_chars = stamp.budget_chars
        #: The surrounding context this reviewer was handed and what was left
        #: out of it, as ``Adoption.record`` gives it. None when the setting
        #: was off, and for a run that fell over before there was a prompt:
        #: only a built prompt is ever recorded as carrying context.
        self.surrounding = stamp.surrounding
        #: What the adapter said about the run whatever its outcome, as
        #: ``RunResult.warnings`` carries it.
        self.warnings = list(warnings or ())

    def to_dict(self) -> Dict[str, Any]:
        entry = {
            "id": self.reviewer.get("id"),
            "provider": self.reviewer.get("provider"),
            "role": self.reviewer.get("role", "general"),
            "model": self.model_display,
            "status": self.status,
            "error": self.error,
            "duration_seconds": round(self.duration, 2),
            "findings": self.findings,
            "report": self.report_path,
            "invoked": self.invoked,
            "delivery": self.delivery,
            "change_chars": self.change_chars,
            "inline_chars": self.inline_chars,
            "snapshot": self.snapshot,
            "over_budget": self.over_budget,
            "budget_chars": self.budget_chars,
            "usage": self.usage.to_dict(),
        }
        # Absent rather than null when off, so an entry is what it always was.
        if self.surrounding is not None:
            entry["surrounding"] = self.surrounding
        if self.warnings:
            entry["warnings"] = list(self.warnings)
        if self.suspended:
            entry["suspended_seconds"] = round(self.suspended, 2)
        return entry


class FanoutOptions(NamedTuple):
    """How ``run_reviews`` runs a round; each option is described there."""

    parallel: bool = True
    timeout: int = 1800
    extra_context: str = ""
    idle_timeout: Optional[float] = None
    max_findings: int = DEFAULT_MAX_FINDINGS
    prompt_for: Optional[Callable[[Dict[str, Any]], BuiltPrompt]] = None
    over_budget: bool = False
    budget_chars: int = 0
    inline_chars: Optional[int] = None
    surrounding: Optional[context_mod.Adoption] = None
    refusals: Optional[Dict[str, str]] = None
    activity_for: Optional[Callable[[str], Optional[activity.Sink]]] = None


def run_reviews(
    reviewers: Sequence[Dict[str, Any]],
    workspace: ws.Workspace,
    options: Optional[FanoutOptions] = None,
) -> List[ReviewerRun]:
    """Run every configured reviewer against the frozen snapshot.

    ``options`` is a ``FanoutOptions``, every default when None.

    ``prompt_for`` replaces the code-review prompt with one built per
    reviewer, which is how the design review reuses this whole function --
    the parallelism, the read-only mode, the tolerance of one failure and the
    ``unparsed`` verdict are properties of the fan-out, not of the diff. It
    returns a ``BuiltPrompt`` for the same reason the code path does: the
    round's coverage is decided from what each prompt actually carried.

    ``over_budget`` is the caller's answer to "was this round only running
    because a human forced it past ``review.context.max_chars``". It is asked
    for rather than worked out here because the limit is configuration and the
    flag is an argument, and neither is visible from the snapshot.
    ``budget_chars`` is the size that answer was decided on, for the same
    reason: on the design path the budget measured the plan and the request
    together, and only the caller knows the pair.

    ``inline_chars`` is ``review.context.inline_chars``. It builds the
    code-review prompt, is recorded on every entry, and words the ``partial``
    verdict -- and when ``prompt_for`` builds the prompt instead, it must be
    the number that callable was given, or the round would be measured
    against one limit and report another.

    ``surrounding`` is the round's adopted context, code review only. It is
    recorded on the entry beside the snapshot stamp, by the same dict: an
    entry carries it exactly when a prompt was built with it.

    ``refusals`` maps a reviewer id to why that reviewer is not run at all --
    its raw arguments came from the project file. The reviewer fails and the
    round goes on, as it does for any one reviewer that fails.

    ``activity_for`` maps a reviewer id to the sink its run reports its tool
    uses to (``review run --progress``), or None. The sink is installed in the
    thread that runs the reviewer, and told ``done: <status>`` however the
    run ends.
    """
    if not reviewers:
        return []
    if options is None:
        options = FanoutOptions()
    diff_text = ws.read_text(workspace.snapshot_path)
    if not diff_text.strip():
        if options.prompt_for is not None:
            raise ReviewError("nothing to review at %s" % workspace.relative(workspace.snapshot_path))
        meta = workspace.read_snapshot_meta()
        withheld = meta.get("withheld") or []
        excluded = [str(entry.get("path")) for entry in withheld if not entry.get("reason")]
        unread = [str(entry.get("path")) for entry in withheld if entry.get("reason")]
        if withheld:
            parts = []
            if excluded:
                parts.append(
                    "as generated or vendored (%s) -- re-snapshot with --no-exclude to review them"
                    % ", ".join(excluded[:5])
                )
            if unread:
                parts.append(
                    "unread (%s), too large, unreadable, or not a regular file -- review them by hand"
                    % ", ".join(unread[:5])
                )
            raise ReviewError(
                "review snapshot is empty because every changed file was withheld: %s." % "; ".join(parts)
            )
        raise ReviewError(
            "review snapshot is empty -- run `review snapshot` after making changes, "
            "or pass --base to compare against a different revision"
        )

    # Read once, before the fan-out: every run in this round answers for the
    # same snapshot, and its entry and its report are stamped with it alike.
    stamp = current_snapshot_stamp(workspace)
    # Resolved once too, and for the same reason: every entry of this round
    # records the limit it was measured against, and they must all record one.
    limit = _inline_limit(options.inline_chars)
    prompt_for, surrounding = options.prompt_for, options.surrounding
    # The design path builds its own prompt and never carries context.
    context = surrounding if (prompt_for is None and surrounding and surrounding.mode != "none") else None
    fan = _Fanout(
        workspace=workspace,
        diff_text=diff_text,
        extra_context=options.extra_context,
        max_findings=options.max_findings,
        prompt_for=prompt_for,
        limit=limit,
        context=context,
        stamp=stamp,
        over_budget=options.over_budget,
        budget_chars=options.budget_chars,
        refusals=options.refusals,
        timeout=options.timeout,
        idle_timeout=options.idle_timeout,
    )
    activity_for = options.activity_for

    def run_one(reviewer: Dict[str, Any]) -> ReviewerRun:
        sink = activity_for(str(reviewer.get("id") or "reviewer")) if activity_for is not None else None
        if sink is None:
            return _attempt(fan, reviewer, None)
        status = "failed"
        try:
            outcome = _attempt(fan, reviewer, sink)
            status = outcome.status
            return outcome
        finally:
            # However the run ended, a raise included.
            sink.say("done: %s" % status)

    if options.parallel and len(reviewers) > 1:
        with ThreadPoolExecutor(max_workers=min(len(reviewers), 8)) as pool:
            return list(pool.map(run_one, reviewers))
    return [run_one(reviewer) for reviewer in reviewers]


class _Fanout(NamedTuple):
    """What every run of one round reads, resolved once before the fan-out."""

    workspace: ws.Workspace
    diff_text: str
    extra_context: str
    max_findings: int
    prompt_for: Optional[Callable[[Dict[str, Any]], BuiltPrompt]]
    #: ``review.context.inline_chars``, resolved.
    limit: int
    #: The round's adopted context, or None when no prompt carries one.
    context: Optional[context_mod.Adoption]
    #: The snapshot stamp every entry and report of the round carries.
    stamp: str
    over_budget: bool
    budget_chars: int
    refusals: Optional[Dict[str, str]]
    timeout: int
    idle_timeout: Optional[float]


def _attempt(fan: _Fanout, reviewer: Dict[str, Any], sink: Optional[activity.Sink]) -> ReviewerRun:
    """One reviewer's run, whatever becomes of it."""
    reviewer_id = str(reviewer.get("id") or "reviewer")
    try:
        provider = get_provider(str(reviewer.get("provider")))
    except Exception as exc:
        return ReviewerRun(reviewer, "failed", error=str(exc))
    if fan.prompt_for is not None:
        built = fan.prompt_for(reviewer)
    else:
        built = build_review_prompt(
            reviewer,
            fan.workspace,
            fan.diff_text,
            fan.extra_context,
            None,
            fan.max_findings,
            fan.limit,
            fan.context,
        )
    # Every run from here on knows what it was handed, and which snapshot it
    # was handed, failures included: a round is judged on what it sent, not
    # on what came back, and the entry has to say which round that was.
    carried = RoundStamp(
        delivery=built.delivery,
        change_chars=built.change_chars,
        inline_chars=fan.limit,
        snapshot=fan.stamp,
        over_budget=fan.over_budget,
        budget_chars=fan.budget_chars,
        surrounding=fan.context.record() if fan.context is not None else None,
    )
    if fan.refusals and reviewer_id in fan.refusals:
        return ReviewerRun(reviewer, "failed", stamp=carried, error=fan.refusals[reviewer_id])
    try:
        with activity.recording(sink):
            result = provider.run(
                built.text,
                MODE_REVIEW,
                fan.workspace.root,
                reviewer.get("model"),
                timeout=fan.timeout,
                options=reviewer.get("options"),
                idle_timeout=fan.idle_timeout,
            )
    except ModelResolutionError as exc:
        return ReviewerRun(reviewer, "failed", stamp=carried, error=str(exc))
    except Exception as exc:
        # No duration, deliberately. Everything that reaches here was
        # raised before ``provider.run`` had a ``RunResult`` to hand back
        # -- ``detect``, a non-``ModelResolutionError`` from
        # ``resolve_model``, ``build_command``, ``_child_env`` -- so no
        # child was started, or none whose lifetime we were told. Reading
        # the output is no longer one of these: an adapter that raises
        # while parsing returns a measured failure instead, which lands in
        # the ``not result.ok`` branch below with its duration intact. What
        # cannot be known is not billed, and ``invoked`` stays False.
        return ReviewerRun(reviewer, "failed", stamp=carried, error="%s: %s" % (type(exc).__name__, exc))

    if not result.ok:
        status, error = _failure(result)
        return _finished(reviewer, status, error, result, carried)
    model_display = result.resolved.display if result.resolved else ""
    # An empty reply is left empty: it is a report nobody can read, and
    # ``_verdict`` records it ``unparsed``, never as a clean review.
    body = result.stdout.strip()
    header = _report_header(reviewer_id, reviewer, model_display, fan.stamp)
    path = fan.workspace.reviewer_report_path(reviewer_id)
    ws.write_text(path, header + body + "\n")
    findings = parse_findings(body, reviewer_id)
    status, error = _verdict(body, findings, built, fan.limit)
    return _finished(
        reviewer,
        status,
        error,
        result,
        carried,
        report_path=fan.workspace.relative(path),
        findings=len(findings),
    )


def _finished(
    reviewer: Dict[str, Any],
    status: str,
    error: str,
    result: RunResult,
    stamp: RoundStamp,
    **fields: Any,
) -> ReviewerRun:
    """The entry of a run that has a ``RunResult``, read off it; ``fields`` adds the report."""
    return ReviewerRun(
        reviewer,
        status,
        stamp=stamp,
        error=error,
        model_display=result.resolved.display if result.resolved else "",
        duration=result.duration,
        suspended=result.suspended,
        # A failed review is not a free one: whatever it burned before
        # falling over still has to appear in the account.
        usage=result.usage,
        invoked=result.invoked,
        warnings=result.warnings,
        **fields,
    )


def _failure(result: RunResult) -> Tuple[str, str]:
    """``(status, error)`` for a run whose CLI did not come back ok."""
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    if result.stalled:
        return "stalled", "no output for %.0fs; treated as wedged" % result.idle_for
    if result.timed_out:
        return "stalled", "hit its %.0fs deadline" % result.duration
    error = detail[-1] if detail else "exit code %s" % result.exit_code
    return "failed", error


def _report_header(reviewer_id: str, reviewer: Dict[str, Any], model_display: str, stamp: str) -> str:
    """What a reviewer's report opens with, the snapshot stamp included."""
    return (
        "# Review\n\n"
        "- Reviewer: %s\n- Provider: %s\n- Model: %s\n- Role: %s\n- Snapshot: %s\n\n---\n\n"
        % (
            reviewer_id,
            reviewer.get("provider"),
            model_display or "unknown",
            reviewer.get("role", "general"),
            stamp,
        )
    )


def _verdict(
    body: str, findings: Sequence[Dict[str, Any]], built: BuiltPrompt, limit: int
) -> Tuple[str, str]:
    """``(status, error)`` for a run that came back with a report."""
    warning = unparsed_report_warning(body, findings)
    # "unparsed" wins: both are not-ok, but a report nobody can read is
    # the more specific fact about this run, and the one that says the
    # delegated cost bought nothing at all.
    if warning:
        return "unparsed", warning
    if built.delivery == "file":
        return "partial", coverage_unverified_error(built.change_chars, limit)
    return "ok", ""
