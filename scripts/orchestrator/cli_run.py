"""The `run` command: delegating one stage to a configured CLI."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Union, cast

from . import activity, execution
from . import approval as approval_mod
from . import config as config_mod
from . import config_policy as policy_mod
from . import jobs as jobs_mod
from . import ledger as ledger_mod
from . import workspace as ws
from .cli_common import (
    DEFAULT_MODES,
    _emit_json,
    _err,
    _in_workflow,
    _ledger,
    _load_or_die,
    _out,
    _refuse_if_exhausted,
    _workspace,
    _wrote_plan,
)
from .providers import (
    MODE_IMPLEMENT,
    MODE_PLAN,
    MODE_REVIEW,
    READ_ONLY_MODES,
    REFUSED_ENFORCEMENT,
    SESSION_ID_RE,
    WARNED_ENFORCEMENT,
    ModelResolutionError,
    Provider,
    get_provider,
    redact,
    unenforced_warning,
)
from .summary import _describe as _describe_spec

# --------------------------------------------------------------------------- run


def _require_prompt(text: str, source: str) -> str:
    """Refuse a prompt that arrived empty, whichever way it arrived.

    An empty prompt is delegated like any other, and the provider then refuses
    it in its own words -- about its stdin, naming neither the source the
    caller used nor the mistake they made. It also costs an attempt to hear it.
    """
    if not text.strip():
        raise SystemExit("empty prompt: %s; nothing was delegated" % source)
    return text


def _both_paths(written: str, resolved: Optional[str]) -> str:
    """Name a path both ways when ``_in_workflow`` rewrote it.

    The resolved path alone is one the caller never typed, and a caller looking
    for their own typo needs to see what they wrote.
    """
    if not resolved or resolved == written:
        return written
    return "%s (resolved to %s)" % (written, resolved)


def _quoted(token: str) -> str:
    """``token`` as ``--print-command`` shows it: quoted when it has spaces,
    so an argument such as agy's ``-p`` value stays one argument."""
    if not re.search(r"\s", token):
        return token
    return subprocess.list2cmdline([token]) if os.name == "nt" else shlex.quote(token)


def _read_prompt(args: argparse.Namespace, workspace: Optional[ws.Workspace] = None) -> str:
    if args.prompt_file:
        return _read_prompt_file(args.prompt_file, workspace)
    if args.prompt is not None:
        # Tested against None, not truthiness: `--prompt ""` used to fall
        # through to the stdin branch, where a pipe made it someone else's
        # empty prompt and a terminal blamed a missing one.
        return _require_prompt(args.prompt, "--prompt was empty")
    if not sys.stdin.isatty():
        return _require_prompt(sys.stdin.read(), "the piped stdin was empty")
    raise SystemExit("no prompt supplied: use --prompt, --prompt-file, or pipe one in")


def _read_prompt_file(prompt_file: str, workspace: Optional[ws.Workspace] = None) -> str:
    """A prompt from a file (or ``-`` for stdin); SystemExit if there is none."""
    if prompt_file == "-":
        return _require_prompt(sys.stdin.read(), "stdin carried nothing")
    path = (_in_workflow(workspace, prompt_file) if workspace else prompt_file) or prompt_file
    named = _both_paths(prompt_file, path)
    # Deliberately not ws.read_text: its default is right for a report that
    # may legitimately be absent, and turns a mistyped --prompt-file into an
    # empty prompt that runs. Read it so the failure is the caller's to see,
    # and so "no such file" stays distinct from "there and empty" -- they
    # are different mistakes.
    if not os.path.isfile(path):
        trouble = "does not exist" if not os.path.exists(path) else "is not a file"
        raise SystemExit("prompt file %s: %s" % (trouble, named))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise SystemExit("prompt file cannot be read: %s (%s)" % (named, exc)) from exc
    return _require_prompt(text, "%s is empty" % named)


class _Refused(NamedTuple):
    """Why ``--output`` was not written, and what became of the stdout."""

    message: str
    rejected_file: Optional[str] = None
    #: Set only when the sidecar could not be written, so the caller can print
    #: what would otherwise be lost.
    unsaved_output: str = ""


def _remove_if_present(path: str) -> None:
    """Remove ``path``. Gone is the state being asked for, so gone already is not a failure."""
    try:
        os.remove(path)
    except OSError:
        pass


def _answered(result: Any) -> bool:
    """Whether a run produced a result: it ended ``ok`` and printed something.

    One function because ``--output`` and the bare print ask the same question,
    and which of them the caller used says nothing about whether the run
    answered. "Printed something" is ``strip()``-empty or not; see
    ``_save_output`` for why nothing finer.
    """
    return bool(result.ok and result.stdout.strip())


def _save_output(role: str, path: str, result: Any) -> Optional[_Refused]:
    """Save a run's stdout to ``path``; return the complaint if it was refused.

    The file named by ``--output`` is usually the one the run was asked to
    revise, so writing a bad result destroys the input: a stalled Architect
    replaced a 50,088-byte plan with the 150 bytes it had emitted before going
    quiet. Only an ``ok`` run that produced something may overwrite, and
    "produced something" is ``strip()``-empty or not. Nothing finer: a size
    threshold is a number wrong for some role, and a short-but-real result is
    the caller's to judge, not this function's.

    The refused stdout goes to a sidecar rather than nowhere. It used to
    survive by landing in the target, and it is often the only account of what
    the run did instead of the work. The same ``strip()`` decides there is
    anything to keep -- a sidecar holding two newlines is a file to delete.
    A failed run's ``partial_output``, which its adapter kept out of stdout,
    goes there too, and only there.
    """
    sidecar = path + ".rejected"
    # An earlier attempt's sidecar is not this run's account of itself, and the
    # operator is told to read one before spending the next attempt. Cleared
    # first so no branch below can leave one behind by forgetting to.
    _remove_if_present(sidecar)
    if _answered(result):
        ws.write_text(path, result.stdout)
        return None
    message = "%s produced nothing usable; %s is unchanged." % (role, path)
    kept = result.stdout if result.stdout.strip() else result.partial_output
    if not kept.strip():
        return _Refused(message)
    try:
        ws.write_text(sidecar, kept)
    except OSError as exc:
        # Keeping the output is the convenience; failing at it must cost only
        # the convenience, not the output and not the report of the run itself.
        return _Refused(
            message + " It could not be kept in %s (%s), so it follows here." % (sidecar, exc),
            unsaved_output=kept,
        )
    return _Refused(message + " Its partial output is in %s." % sidecar, rejected_file=sidecar)


class _Seat(NamedTuple):
    """The configured seat a `run` delegates to, before anything is opened."""

    role: str
    tier: Optional[str]
    reviewer_index: Optional[int]
    spec: Dict[str, Any]
    mode: str
    provider_name: str
    #: How the project-file checks name the seat: "reviewers[N]",
    #: "<role>.model_tiers.<tier>" or the role.
    label: str


class _Run(NamedTuple):
    """What every launch of one `run` shares, once the books are open."""

    args: argparse.Namespace
    seat: _Seat
    provider: Provider
    workspace: ws.Workspace
    book: ledger_mod.Ledger
    timeout: int
    idle_timeout: Optional[float]
    #: Where ``timeout`` came from, as the kill message names it.
    timeout_origin: str


class _Attempt(NamedTuple):
    """One launch: its in-flight token and result, and what it was asked to continue."""

    token: str
    result: Any
    #: The ``resume_session`` passed to ``_run_once``: None for a fresh run and
    #: for the retry. Never ``result.session_id`` -- this is what the retry
    #: condition and the ``:resumed`` usage label read.
    session_id: Optional[str]
    resume_detail: Optional[Dict[str, Any]]


def cmd_run(args: argparse.Namespace) -> int:
    loaded = _load_or_die(args.cwd)
    seat = _resolve_seat(args, loaded)
    if seat is None:
        return 2
    # Refused before anything is printed, spent or started, and into the job
    # record as well as onto stderr, for the reason `_read_prompts` gives.
    # None of these messages carries an argument's value.
    problem = _mode_refusal(args, seat)
    if problem:
        return _refuse_run(args, [problem])
    workspace = _workspace(args)
    # Before the provider is known: these depend on the arguments alone.
    problem = _resume_refusal(args, seat.role, seat.mode, workspace)
    if problem:
        return _refuse_run(args, [problem])
    try:
        provider = get_provider(seat.provider_name)
    except ValueError as exc:
        _err(str(exc))
        return 2
    refused = _preflight_refusals(args, loaded, seat, provider)
    if refused:
        return _refuse_run(args, refused)
    if args.print_command:
        return _print_command(args, loaded, seat, provider, workspace)
    refused, warned_before = _enforcement_refusal(loaded, seat, provider)
    if refused:
        return _refuse_run(args, refused)
    if warned_before:
        _err("warning: %s: %s" % (seat.role, warned_before))
    prompt, resume_prompt = _read_prompts(args, workspace)
    # The approval gate. Before the ledger is opened and before the budget is
    # consumed: a refused run must cost nothing, and before --detach: a worker
    # must never be the process that finds out first. The worker checks again
    # all the same -- --job-file is a flag on a public parser, and a worker
    # that trusted its parent would be the way round the gate; its refusal
    # goes into the job record, for the reason `_read_prompts` gives about stderr. That
    # refusal costs the attempt the parent consumed before handing over: it
    # is not given back.
    # --force is deliberately not honoured here. It overrides a budget, which
    # is a resource; approval is the user's consent, and the human who would
    # force past it is the human who can say yes, which `design approve`
    # records.
    if seat.role == "implementer":
        refusal = _refuse_unless_approved(loaded, workspace, args, seat.role)
        if refusal is not None:
            return refusal
    settings = loaded.review_settings()
    timeout, timeout_origin = _deadline(args, loaded, seat)
    idle_timeout = args.idle_timeout
    if idle_timeout is None:
        idle_timeout = settings.get("idle_timeout_seconds")
    book = _ledger(args, workspace)
    refusal = _spend_attempt(args, book, seat)
    if refusal is not None:
        return refusal
    if args.detach:
        return _detach(args, workspace, seat.role, prompt, resume_prompt, timeout)
    if args.job_file:
        # A detached worker: `jobs cancel` must reach the CLI it runs, which is
        # in a session of its own.
        execution.end_children_on_sigterm(jobs_mod.stop_note_path(args.job_file))
    session_id: Optional[str] = None
    resume_detail: Optional[Dict[str, Any]] = None
    if args.resume:
        session_id, resume_detail = _choose_resume(loaded, workspace, provider, seat.provider_name)
    # Written down before the call, so a stall is visible from outside this
    # process and survives it dying. The record is kept: whether the user
    # forced this run is in it, and the worker's own --force says nothing.
    job = jobs_mod.claim(args.job_file) if args.job_file else None
    run = _Run(args, seat, provider, workspace, book, timeout, idle_timeout, timeout_origin)
    # Every run a worker makes reports its tool uses beside the job, the fresh
    # retry after a rejected resume included, so `n` carries on. Installed for
    # this call only: a worker run in-process, as the tests do, must not leave
    # its sink on the thread.
    sink = activity.Sink(jobs_mod.activity_path(args.job_file)) if args.job_file else None
    with activity.recording(sink):
        # `resume_prompt` is read whenever --resume is given, and a session id
        # is only ever found under --resume.
        attempt = _run_once(
            run, cast(str, resume_prompt) if session_id is not None else prompt, session_id, resume_detail
        )
        if attempt is None:
            return 2
        if (
            attempt.session_id is not None
            and attempt.resume_detail is not None
            and attempt.result.resume_rejected
        ):
            retried = _retry_fresh_after_rejection(run, job, attempt, prompt)
            if isinstance(retried, int):
                return retried
            attempt = retried
    _record_outcome(run, attempt)
    # Printed last, and after the books are closed. Showing the output used to
    # come first, so a console that could not encode one character of it took
    # the accounting and the in-flight entry down with it: the tokens went
    # unrecorded and the next command reported this finished run as abandoned.
    # Nothing below this line is allowed to decide whether the run happened.
    return _report_outcome(run, attempt, warned_before)


def _resolve_seat(args: argparse.Namespace, loaded: config_mod.LoadedConfig) -> Optional[_Seat]:
    """The seat ``args.role`` names; None, with the reason on stderr, if there is none."""
    role = args.role
    tier = args.tier
    reviewer_index = None
    try:
        if role in config_mod.KNOWN_ROLES:
            spec = loaded.role(role, tier)
        elif tier:
            # Reviewers are already one model each; the panel is the routing.
            _err("--tier applies to %s, not to a reviewer" % ", ".join(config_mod.KNOWN_ROLES))
            return None
        else:
            reviewer_index, spec = config_mod.find_reviewer(loaded.data, role)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return None

    mode = args.mode or DEFAULT_MODES.get(role, MODE_REVIEW)
    provider_name = str(spec.get("provider"))
    if reviewer_index is not None:
        label = "reviewers[%d]" % reviewer_index
    elif tier:
        label = "%s.model_tiers.%s" % (role, tier)
    else:
        label = role
    return _Seat(role, tier, reviewer_index, spec, mode, provider_name, label)


def _deadline(args: argparse.Namespace, loaded: config_mod.LoadedConfig, seat: _Seat) -> Tuple[int, str]:
    """The run's total deadline in seconds, and where it came from.

    ``--timeout`` first; then ``run.timeout_seconds.<role>`` for a role, or
    ``review.timeout_seconds`` for a reviewer, as ``review run`` reads it. A
    detached worker comes back here, so parent and worker agree.
    """
    if args.timeout:
        return args.timeout, "--timeout"
    if seat.reviewer_index is not None:
        review = config_mod.review_timeout(loaded)
        return review.seconds, "review.timeout_seconds, %s" % review.source
    deadline = config_mod.run_timeout(loaded, seat.role)
    return deadline.seconds, "run.timeout_seconds.%s, %s" % (seat.role, deadline.source)


def _timeout_message(role: str, seconds: int, origin: str) -> str:
    """What stderr says about a run killed at its deadline: the number, and the key that raises it."""
    if origin == "--timeout":
        return "%s hit its %ss deadline (--timeout) and was killed." % (role, seconds)
    message = "%s hit its %ss deadline (%s) and was killed; --timeout raises it for one run."
    return message % (role, seconds, origin)


def _mode_refusal(args: argparse.Namespace, seat: _Seat) -> Optional[str]:
    """Why ``--mode`` cannot go on this seat, as a fixed sentence."""
    read_only_role = seat.role not in config_mod.KNOWN_ROLES or DEFAULT_MODES[seat.role] in READ_ONLY_MODES
    if read_only_role and args.mode == MODE_IMPLEMENT:
        return (
            "%s: refused -- %s runs are read-only (plan or review); --mode implement is not "
            "accepted for this role" % (seat.role, seat.role)
        )
    return None


def _preflight_refusals(
    args: argparse.Namespace, loaded: config_mod.LoadedConfig, seat: _Seat, provider: Provider
) -> List[str]:
    """The lines of the first check that refuses this seat its arguments; none if all pass."""
    if seat.mode == MODE_IMPLEMENT:
        # The same reasoning for a write role on a provider whose permission
        # bypass is local-only: nothing of it is taken from the project file.
        from_project = policy_mod.project_write_refusals(loaded).get(seat.label)
        if from_project:
            return ["refused -- %s" % from_project]
    if seat.mode in READ_ONLY_MODES:
        # Raw arguments, and a seat on a provider that cannot be held to
        # reading, that came with the project file.
        from_project = policy_mod.project_raw_arg_refusals(loaded).get(seat.label)
        if from_project:
            return ["refused -- %s" % from_project]
        problems = provider.read_only_arg_problems(seat.mode, args.extra or [], seat.spec.get("options"))
        return ["%s: refused -- %s" % (seat.role, problem) for problem in problems]
    return []


def _print_command(
    args: argparse.Namespace,
    loaded: config_mod.LoadedConfig,
    seat: _Seat,
    provider: Provider,
    workspace: ws.Workspace,
) -> int:
    """``--print-command``: print the command the run would start, and start nothing."""
    try:
        resolved = provider.resolve_model(seat.spec.get("model"))
    except ModelResolutionError as exc:
        _err(str(exc))
        return 2
    # Looked up without opening the ledger, so printing spends nothing.
    session_id = None
    if args.resume:
        session_id, _ = _choose_resume(loaded, workspace, provider, seat.provider_name)
    command = provider.command_line(
        seat.mode,
        resolved,
        workspace.root,
        args.extra or [],
        seat.spec.get("options"),
        resume_session=session_id,
    )
    _out(" ".join(_quoted(token) for token in command))
    return 0


def _enforcement_refusal(
    loaded: config_mod.LoadedConfig, seat: _Seat, provider: Provider
) -> Tuple[List[str], str]:
    """The provider's read-only enforcement, judged before anything is spent.

    Returns ``(refusal lines, warning)``: the lines when the seat is refused,
    otherwise the warning to print once, if any.
    """
    # Only for a CLI that is there: a missing one is reported by the run as
    # missing (127), not as one whose enforcement could not be read.
    if seat.mode in READ_ONLY_MODES and provider.detect().installed:
        enforcement = provider.read_only_enforcement()
        if enforcement.get("status") in REFUSED_ENFORCEMENT:
            return ["%s: refused -- %s" % (seat.role, enforcement.get("detail"))], ""
        if enforcement.get("status") in WARNED_ENFORCEMENT:
            # The live report, for an adapter whose report is not static and
            # so was not asked above: a project file does not choose it either.
            from_project = policy_mod.project_provider_refusals(loaded).get(seat.label)
            if from_project:
                return ["refused -- %s" % from_project], ""
            return [], unenforced_warning(seat.provider_name, enforcement)
    return [], ""


def _read_prompts(args: argparse.Namespace, workspace: ws.Workspace) -> Tuple[str, Optional[str]]:
    """The fresh prompt and, under ``--resume``, the resumed one; SystemExit if either is missing."""
    try:
        prompt = _read_prompt(args, workspace)
        # Both prompts are read before anything is spent: which one is sent
        # is only known once `cmd_run` has read the run log.
        resume_prompt = _read_prompt_file(args.resume_prompt_file, workspace) if args.resume else None
    except SystemExit as exc:
        # A worker's stderr is DEVNULL, so a reason left there reaches nobody:
        # every exit `run` can take before the outcome is written has
        # to put its message in the job record, or the run is reported only as
        # a worker that vanished having recorded no outcome -- which is also
        # what is said about one that was killed. The same goes for the
        # ModelResolutionError exit in `_run_once`. The foreground call keeps the
        # message on stderr, where its caller is watching.
        _fail_job(args, str(exc))
        raise
    return prompt, resume_prompt


def _spend_attempt(args: argparse.Namespace, book: ledger_mod.Ledger, seat: _Seat) -> Optional[int]:
    """Take this run's attempt from the budget; the exit code if it is refused one."""
    book.clear_stalls()
    if seat.tier:
        _err("note: running %s on its %r tier (%s)" % (seat.role, seat.tier, _describe_spec(seat.spec)))
    refusal = _refuse_if_exhausted(book, seat.role, args.force)
    if refusal is not None:
        return refusal
    try:
        if not args.job_file:
            book.consume(seat.role, force=args.force)
    except ledger_mod.BudgetExhausted as exc:
        _err(str(exc))
        return ledger_mod.EXIT_BUDGET_EXHAUSTED
    return None


def _detach(
    args: argparse.Namespace,
    workspace: ws.Workspace,
    role: str,
    prompt: str,
    resume_prompt: Optional[str],
    timeout: int,
) -> int:
    """``--detach``: start a worker for the run and return at once."""
    # Hand the work to a detached worker so this call cannot block. The
    # budget was already consumed above, so the worker must not do it again.
    passthrough = _detached_argv(args, role, workspace.workflow)
    job = jobs_mod.start(
        workspace,
        role,
        passthrough,
        prompt=prompt,
        timeout=timeout,
        resume_prompt=resume_prompt,
        force=bool(args.force),
    )
    if args.json:
        _emit_json(job)
    else:
        _out("started %s as job %s" % (role, job["id"]))
        _out("follow it with: dev-orchestra jobs wait %s" % job["id"])
    return 0 if job.get("status") != "failed" else 1


def _run_once(
    run: _Run, text: str, resume_session: Optional[str], resume_detail: Optional[Dict[str, Any]]
) -> Optional[_Attempt]:
    """Start one in-flight entry and run the provider on ``text``.

    None when the model cannot be resolved: that entry is ended, the job is
    failed and the reason is on stderr.
    """
    args, seat, book = run.args, run.seat, run.book
    begun: Dict[str, Any] = {
        "mode": seat.mode,
        "provider": seat.provider_name,
        "command": run.provider.executable,
        "job": args.job_file,
        "tier": seat.tier or None,
    }
    if resume_detail is not None:
        begun["resume"] = dict(resume_detail)
    token = book.begin(seat.role, begun, deadline=run.timeout)
    # The keyword only when there is a session: an adapter that cannot
    # resume is called exactly as it was before resuming existed.
    resuming: Dict[str, Any] = {"resume_session": resume_session} if resume_session is not None else {}
    try:
        result = run.provider.run(
            text,
            seat.mode,
            run.workspace.root,
            seat.spec.get("model"),
            timeout=run.timeout,
            extra_args=args.extra or [],
            options=seat.spec.get("options"),
            idle_timeout=run.idle_timeout,
            **resuming,
        )
    except ModelResolutionError as exc:
        book.end(token, "failed", {"error": str(exc)})
        _fail_job(args, str(exc))
        _err(str(exc))
        return None
    return _Attempt(token, result, resume_session, resume_detail)


def _end_detail(run: _Run, result: Any, resume_detail: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The detail of a run's end event."""
    seat = run.seat
    detail: Dict[str, Any] = {
        "mode": seat.mode,
        "provider": seat.provider_name,
        # Recorded on the *end* event, not only on the start: the start
        # entry is dropped from the ledger when the stage finishes, and
        # the run log is what a report is written from.
        "tier": seat.tier or None,
        "model": result.resolved.display if result.resolved else None,
        "model_source": result.resolved.source if result.resolved else None,
        "duration_seconds": round(result.duration, 2),
        "timed_out": result.timed_out,
        "stalled": result.stalled,
        "output": run.args.output,
        "billed_tokens": result.usage.billed_tokens,
        # Whether the run left a usable result: an ``ok`` over silence
        # saved nothing, and `status` must not count it as the revision
        # or fix that was asked for. Pure, so the order below stands.
        "answered": _answered(result),
        # The session this run ended in is what the next --resume
        # continues; the rest is what comparing resumed and fresh
        # revisions needs.
        "session_id": result.session_id,
        "context_tokens": result.context_tokens,
        "cost_usd": result.usage.cost_usd,
        "cache_read_tokens": result.usage.cache_read_tokens,
    }
    if resume_detail is not None:
        detail["resume"] = dict(resume_detail, outcome="ok" if result.ok else None)
    # Only when there are some, so a run's event is what it always was.
    if result.warnings:
        detail["warnings"] = list(result.warnings)
    return detail


def _retry_fresh_after_rejection(
    run: _Run, job: Optional[Dict[str, Any]], rejected: _Attempt, prompt: str
) -> Union[_Attempt, int]:
    """Record the rejected resumed run and run fresh once; the exit code if that cannot happen."""
    args, role, book = run.args, run.seat.role, run.book
    result = rejected.result
    # The session is gone (the CLI said so, naming it). That run is
    # recorded as the failure it was, without its stderr, and not in the
    # job: the job's outcome is the fresh run's.
    if result.invoked:
        book.record_usage(role, result.usage.to_dict())
    rejected_detail = _end_detail(run, result, rejected.resume_detail)
    rejected_detail["context_tokens"] = None
    rejected_detail["resume"] = dict(cast(Dict[str, Any], rejected.resume_detail), outcome="rejected")
    book.end(
        rejected.token,
        "failed",
        rejected_detail,
        charged_seconds=result.duration - result.suspended,
        suspended_seconds=result.suspended,
    )
    _err("note: %s" % _RESUME_REJECTED)
    if not result.invoked:
        # The adapter refused to start it, so nothing came from the CLI:
        # its reason is said here, and still not recorded.
        refused = (result.stderr or "").strip().splitlines()
        if refused:
            _err("note: %s" % refused[-1])
    # Running fresh is another attempt, so it asks the budget as any run
    # does -- as the user asked it, not as the worker was started. A
    # refusal ran nothing, so the attempt already taken is the fresh one's.
    user_force = bool(job.get("force", False)) if job is not None else bool(args.force)
    if result.invoked and not user_force and book.check(role):
        _err(_RESUME_NO_BUDGET)
        _refuse_if_exhausted(book, role, user_force)
        _fail_job(args, _RESUME_NO_BUDGET)
        return ledger_mod.EXIT_BUDGET_EXHAUSTED
    try:
        if result.invoked:
            book.consume(role, force=user_force)
    except ledger_mod.BudgetExhausted as exc:
        _err(_RESUME_NO_BUDGET)
        _err(str(exc))
        _fail_job(args, _RESUME_NO_BUDGET)
        return ledger_mod.EXIT_BUDGET_EXHAUSTED
    retried = _run_once(run, prompt, None, _fresh_resume_detail(_RESUME_REJECTED))
    if retried is None:
        return 2
    return retried


def _record_outcome(run: _Run, attempt: _Attempt) -> None:
    """Close the books on the run, then say in the job how it finished."""
    # The books are closed before the job says it finished. `jobs wait`
    # returns on that status, and a caller that reads `tokens show` next used
    # to find the run's usage not yet written: the worker recorded "succeeded"
    # first and the account a moment later. A failure in the accounting still
    # finishes the job -- as failed, naming it -- rather than leaving a worker
    # that vanished, or a success whose usage is nowhere.
    args, seat, book = run.args, run.seat, run.book
    result = attempt.result
    finished = {
        "exit_code": result.exit_code,
        "stalled": result.stalled,
        "timed_out": result.timed_out,
        "duration_seconds": round(result.duration, 2),
        "model": result.resolved.display if result.resolved else None,
        "session_id": result.session_id,
    }
    if attempt.resume_detail is not None:
        finished["resume"] = dict(attempt.resume_detail, outcome="ok" if result.ok else None)
    if result.warnings:
        finished["warnings"] = list(result.warnings)
    if result.suspended:
        finished["suspended_seconds"] = round(result.suspended, 2)

    try:
        # Recorded whatever the outcome -- a failed run still spent what it spent.
        # A run that never started one is a different thing, and counting it as
        # unreported would make the account call itself incomplete over a run with
        # nothing to report.
        if result.invoked:
            # Labelled by tier when there is one, so `tokens show` can answer the
            # question a tier exists to raise: did the cheaper one cost less. A
            # continued session is labelled too, to be read against fresh runs.
            label = ""
            if seat.tier:
                label = "%s:%s" % (seat.role, seat.tier)
            elif attempt.session_id is not None:
                label = "%s:resumed" % seat.role
            book.record_usage(seat.role, result.usage.to_dict(), label=label)
        book.end(
            attempt.token,
            "ok" if result.ok else ("stalled" if result.stalled else "failed"),
            _end_detail(run, result, attempt.resume_detail),
            # What the child was measured to take, whatever it exited with. A run
            # killed at its deadline spent the time it spent; so did a failed one.
            # The time the machine slept through is not execution.
            charged_seconds=result.duration - result.suspended,
            suspended_seconds=result.suspended,
        )
    except BaseException as exc:
        if args.job_file:
            # Its own guard, so a job file that cannot be written does not
            # take the place of the failure that brought us here.
            try:
                _fail_job(
                    args,
                    output=result.stdout,
                    error=redact(
                        "the run finished (exit %s) but recording it failed: %s" % (result.exit_code, exc)
                    )[:2000],
                    detail=finished,
                )
            except Exception as finish_exc:
                _err("error: could not record the job's outcome: %s" % redact(str(finish_exc)))
        raise
    if args.job_file:
        jobs_mod.finish(
            args.job_file,
            "succeeded" if result.ok else "failed",
            output=result.stdout,
            error="" if result.ok else (result.stderr or "").strip()[:2000],
            detail=finished,
        )


def _report_outcome(run: _Run, attempt: _Attempt, warned_before: str) -> int:
    """Save or print the run's output, say how it went, and return the exit code."""
    args, role = run.args, run.seat.role
    result = attempt.result
    refused = None
    answered_nothing = False
    target = ""
    if args.output:
        target = _in_workflow(run.workspace, str(args.output))
        refused = _save_output(role, target, result)
    elif not args.job_file:
        _out(result.stdout)
        # The same judgement `--output` refuses a write on. A run with nowhere
        # to write has no refusal to report, so an `ok` run that printed
        # nothing used to be reported as a success by printing that nothing.
        # The --job-file branch is left out: a worker's stdout is in the job,
        # and `jobs wait` reports the outcome.
        answered_nothing = result.ok and not _answered(result)
    # Whatever the outcome: on success nothing else shows stderr. The
    # enforcement warning was already printed before the run.
    for warning in result.warnings:
        if warning != warned_before:
            _err("warning: %s: %s" % (role, warning))
    if result.stalled:
        _err(
            "%s produced no output for %.0fs and was treated as stalled (not merely slow)."
            % (role, result.idle_for)
        )
    elif result.timed_out:
        _err(_timeout_message(role, run.timeout, run.timeout_origin))
    elif not result.ok:
        _err("%s failed (exit %s): %s" % (role, result.exit_code, result.stderr.strip()[:500]))
    elif answered_nothing:
        # Reported with the outcomes rather than beside the print: an exit 0
        # over silence is a diagnosis, the same one the other branches make.
        # The raw stderr comes along because a CLI that refused the prompt says
        # why there and nowhere else.
        complaint = "%s exited 0 but produced no output." % role
        first_words = result.stderr.strip()[:500]
        if first_words:
            complaint += " Its stderr began: %s" % first_words
        _err(complaint)
    # Said here rather than beside the write, so a refusal and the outcome that
    # caused it read as one report instead of two lines that look at odds.
    if refused:
        _err(refused.message)
        if refused.unsaved_output:
            _err(refused.unsaved_output)
        if args.job_file:
            # A detached worker's stderr goes to DEVNULL, so the job record is
            # the only reader this refusal has. Written as a second update
            # rather than folded into the first: the accounting above must not
            # wait on the save to decide that the run happened.
            jobs_mod.finish(
                args.job_file,
                "succeeded" if result.ok else "failed",
                detail={
                    "output_written": False,
                    "output_target": target,
                    "rejected_file": refused.rejected_file,
                },
            )
    if result.orphans_possible:
        _err("warning: %s's process group may have left orphans; check for stray processes." % role)
    # A refused write exits non-zero even when the run itself was fine: the
    # promise `--output` makes is that the named file holds this run's result,
    # and exiting 0 over an untouched one lets the next command in a chain read
    # the stale file as if it were new. A run with nothing to show exits the
    # same way for the same reason -- an empty answer is not a result.
    return 0 if result.ok and not refused and not answered_nothing else 1


def _refuse_unless_approved(
    loaded: config_mod.LoadedConfig, workspace: ws.Workspace, args: argparse.Namespace, role: str
) -> Optional[int]:
    """Refuse the implementer a plan the user has not approved as it is now."""
    info = approval_mod.current(workspace, bool(loaded.design_settings().get("require_approval")))
    if info["state"] not in approval_mod.REFUSED:
        return None
    lines = approval_mod.refusal_lines(info, workspace.relative(workspace.plan_path))
    # The whole refusal, not its first line: the instruction to ask the user
    # is the part a worker's reader most needs, and the job is all it has.
    _fail_job(args, "\n".join(lines))
    for line in lines:
        _err(line)
    return approval_mod.EXIT_APPROVAL_REQUIRED


def _fail_job(args: argparse.Namespace, error: str, **fields: Any) -> None:
    """Record a worker's failure in its job record, the only place it can say so.

    ``error`` goes in as given: a caller whose message may carry a secret
    redacts it first.
    """
    if args.job_file:
        jobs_mod.finish(args.job_file, "failed", error=error, **fields)


def _refuse_run(args: argparse.Namespace, lines: List[str]) -> int:
    """Refuse a run before it starts: exit 2, said on stderr and in the job."""
    _fail_job(args, "\n".join(lines))
    for line in lines:
        _err(line)
    return 2


#: Why a ``--resume`` run ran fresh. Fixed phrases only: every value they could
#: name (a mode, a provider, a path, a session id, an age, a CLI version) is
#: already in the event's own keys or came from ``state.json`` or the CLI, and
#: `optimization report` counts these strings as they are.
_RESUME_NOT_SUPPORTED = "the provider cannot resume a session"
_RESUME_REJECTED = "the CLI rejected the session it was asked to resume"
_RESUME_REASONS = (
    _RESUME_NOT_SUPPORTED,
    _RESUME_NOT_SUPPORTED + " (unsupported)",
    _RESUME_NOT_SUPPORTED + " (unverified)",
    _RESUME_NOT_SUPPORTED + " (unspecified)",
    "no earlier architect run in this workflow",
    "the last architect run is not resumable: it did not succeed",
    "the last architect run is not resumable: it did not answer",
    "the last architect run is not resumable: it has no session id",
    "the last architect run is not resumable: its session id is not a UUID",
    "the last architect run is not resumable: its recorded mode is not plan",
    "the last architect run is not resumable: its output is not this workflow's plan",
    "the last architect run is not resumable: its recorded provider differs",
    "the last architect run is older than design.resume.max_age_seconds",
    "the last architect run's context exceeds design.resume.max_context_tokens",
    "the last architect run's context is unknown and design.resume.max_context_tokens is set",
    _RESUME_REJECTED,
)
_RESUME_NO_BUDGET = _RESUME_REJECTED + "; running fresh would spend an attempt"


def _resume_refusal(args: argparse.Namespace, role: str, mode: str, workspace: ws.Workspace) -> Optional[str]:
    """Why ``--resume`` cannot go on this command line, as a fixed sentence.

    Decided by the arguments alone, so it is checked on every path: the
    foreground run, ``--print-command``, the parent of ``--detach`` and the
    worker, which must not trust its parent -- ``--job-file`` is a public flag.
    """
    if not getattr(args, "resume", False):
        return "--resume-prompt-file needs --resume" if getattr(args, "resume_prompt_file", None) else None
    if role != "architect":
        return "--resume applies to architect"
    if mode != MODE_PLAN:
        return (
            "--resume applies to architect in plan mode: the session it would continue was "
            "read-only and must stay so"
        )
    if not args.resume_prompt_file:
        return "--resume needs --resume-prompt-file"
    # Both prompts are read before the run: stdin would give the second one
    # nothing, and the run would blame a prompt the user did pipe.
    fresh_from_stdin = args.prompt_file == "-" or (not args.prompt_file and args.prompt is None)
    if args.resume_prompt_file == "-" and fresh_from_stdin:
        return "--resume-prompt-file - needs the fresh prompt from --prompt or a file: stdin is read once"
    # The session continued is one that wrote the plan, so the run continuing
    # it must write the plan too, or an earlier design's context would carry
    # into an unrelated answer.
    if not args.output:
        return "--resume revises this workflow's plan: --output is missing"
    if not _wrote_plan(workspace)({"output": args.output}):
        return "--resume revises this workflow's plan: --output must be .ai/plan.md"
    return None


def _resume_candidate(
    workspace: ws.Workspace,
    events: List[Dict[str, Any]],
    provider: Any,
    provider_name: str,
    design_settings: Dict[str, Any],
) -> Tuple[Optional[str], Dict[str, Any], str]:
    """The session ``--resume`` continues, or why the run goes fresh.

    Returns ``(session_id, resume_detail, provider_note)``. ``provider_note``
    is the adapter's own explanation, for stderr only: it names the CLI
    version and never goes into an event or a job record.

    ``state.json`` is trusted to name the session, as it is trusted with the
    approval and the budget; its values are checked, never repeated.
    """

    def fresh(reason: str, note: str = "") -> Tuple[None, Dict[str, Any], str]:
        return None, _fresh_resume_detail(reason), note

    if not getattr(provider, "supports_resume", False):
        return fresh(_RESUME_NOT_SUPPORTED)
    support = provider.resume_support(workspace.root) or {}
    status = support.get("status")
    if status not in ("verified", "trusted"):
        suffix = status if status in ("unsupported", "unverified") else "unspecified"
        return fresh("%s (%s)" % (_RESUME_NOT_SUPPORTED, suffix), str(support.get("detail") or ""))

    runs = [event for event in events if isinstance(event, dict) and event.get("stage") == "architect"]
    last = runs[-1] if runs else None
    if last is None:
        return fresh("no earlier architect run in this workflow")
    if last.get("status") != "ok":
        return fresh("the last architect run is not resumable: it did not succeed")
    if last.get("answered") is False:
        return fresh("the last architect run is not resumable: it did not answer")
    session_id = last.get("session_id")
    if not session_id:
        return fresh("the last architect run is not resumable: it has no session id")
    # A session id goes on a command line; only a UUID may.
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        return fresh("the last architect run is not resumable: its session id is not a UUID")
    if last.get("mode") != MODE_PLAN:
        return fresh("the last architect run is not resumable: its recorded mode is not plan")
    if not _wrote_plan(workspace)(last):
        return fresh("the last architect run is not resumable: its output is not this workflow's plan")
    if last.get("provider") != provider_name:
        return fresh("the last architect run is not resumable: its recorded provider differs")

    limits = design_settings.get("resume") or {}
    max_age = limits.get("max_age_seconds")
    if not isinstance(max_age, int) or isinstance(max_age, bool):
        max_age = config_mod.default_config()["design"]["resume"]["max_age_seconds"]
    ended = approval_mod.epoch_of(last.get("at"))
    if ended is None or time.time() - ended >= max_age:
        return fresh("the last architect run is older than design.resume.max_age_seconds")
    cap = limits.get("max_context_tokens")
    context = last.get("context_tokens")
    if isinstance(cap, int) and not isinstance(cap, bool):
        # A run that reported no usable size could be over the cap: only a
        # size shown to be within it resumes.
        if not isinstance(context, int) or isinstance(context, bool):
            return fresh(
                "the last architect run's context is unknown and design.resume.max_context_tokens is set"
            )
        if context > cap:
            return fresh("the last architect run's context exceeds design.resume.max_context_tokens")
    detail = {
        "requested": True,
        "mode": "resumed",
        "resumed_from": session_id,
        "reason": None,
        "outcome": None,
    }
    if status == "trusted":
        # Trusted as newer than a version that passed, not checked itself:
        # the adapter's sentence says so on stderr, and only the word goes
        # into the record, never the versions.
        detail["trust"] = "newer"
        return session_id, detail, str(support.get("detail") or "")
    return session_id, detail, ""


def _fresh_resume_detail(reason: str) -> Dict[str, Any]:
    """The ``resume`` detail of a ``--resume`` run that runs fresh, and why."""
    return {"requested": True, "mode": "fresh", "resumed_from": None, "reason": reason, "outcome": None}


def _choose_resume(
    loaded: config_mod.LoadedConfig, workspace: ws.Workspace, provider: Any, provider_name: str
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Look up the session ``--resume`` continues and say which it is on stderr."""
    session_id, detail, note = _resume_candidate(
        workspace,
        list(workspace.read_state().get("events") or []),
        provider,
        provider_name,
        loaded.design_settings(),
    )
    _announce_resume(session_id, detail, note)
    return session_id, detail


def _announce_resume(session_id: Optional[str], detail: Dict[str, Any], note: str) -> None:
    if session_id is not None:
        _err("note: resuming the last architect session")
        if note:
            _err("note: %s" % note)
        return
    _err("note: --resume requested, running fresh: %s" % detail["reason"])
    if note:
        _err("note: %s" % note)


def _detached_argv(args: argparse.Namespace, role: str, workflow: str) -> List[str]:
    """Rebuild this invocation for the worker, prompt now coming from a file.

    The prompt's text is deliberately absent: ``jobs.start`` writes it to a
    file of its own, because only it knows the job id the path is built from.
    What goes here is the placeholder it substitutes -- placed, not appended,
    so it lands where this command line has room for it. This used to say
    ``--prompt-file -`` while the worker's stdin was ``DEVNULL``, so every
    detached run delegated an empty prompt -- invisible under the mock
    provider, which does not read one.

    ``workflow`` is the one the parent resolved. The worker resolves its own
    otherwise, from the environment or the session, and its ledger, its run
    log and its own approval check then belong to a workflow the parent was
    never in.
    """
    argv = ["--workflow", workflow, "run", role, "--force"]
    if args.tier:
        # Left out, the worker ran the role's default model: a more expensive
        # run than the one asked for, recorded without the label a tier exists
        # to be read by.
        argv += ["--tier", args.tier]
    if args.mode:
        argv += ["--mode", args.mode]
    if args.output:
        argv += ["--output", os.path.abspath(args.output)]
    if args.timeout:
        argv += ["--timeout", str(args.timeout)]
    if args.idle_timeout is not None:
        argv += ["--idle-timeout", str(args.idle_timeout)]
    # Ahead of --extra, which is nargs=REMAINDER and takes everything after it.
    # Both used to be appended by `jobs.start`, past the end of a command whose
    # shape only this function knows, and a detached run carrying --extra
    # reached the provider with the two paths as provider arguments.
    argv += ["--prompt-file", jobs_mod.PROMPT_FILE, "--job-file", jobs_mod.JOB_FILE]
    if getattr(args, "resume", False):
        # The worker reads the copy `jobs.start` makes, as it does the fresh
        # prompt, so an edit to the file after this parent checked it does not
        # reach the continued session.
        argv += ["--resume", "--resume-prompt-file", jobs_mod.RESUME_PROMPT_FILE]
    if args.extra:
        argv += ["--extra", *args.extra]
    return argv
