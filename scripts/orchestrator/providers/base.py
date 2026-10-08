"""Provider adapter interface.

A provider wraps one coding CLI. Adapters must never invent model identifiers:
``resolve_model`` either returns something the installed CLI told us about, or
returns a resolution that lets the CLI pick its own default.
"""

from __future__ import annotations

import copy
import functools
import json
import math
import os
import re
import subprocess
from typing import Any, Callable, ClassVar, Dict, List, NamedTuple, Optional, Sequence, Tuple, TypeVar

from .. import activity, clocks, execution, verified
from ..workspace import redact

# Execution modes shared by every adapter.
MODE_PLAN = "plan"  # investigate / design, must not modify files
MODE_IMPLEMENT = "implement"  # allowed to modify the working tree
MODE_REVIEW = "review"  # read-only, produces a review report

MODES = (MODE_PLAN, MODE_IMPLEMENT, MODE_REVIEW)
READ_ONLY_MODES = (MODE_PLAN, MODE_REVIEW)

#: What ``Provider.read_only_enforcement`` may report. ``verified``: the CLI
#: itself stops writes and external side effects. ``partial``: it stops
#: filesystem writes, and external side effects were not examined.
#: ``unenforced``: the CLI was measured to have no read-only mode, and a run
#: goes ahead with a warning. ``unsupported``: the CLI does not advertise what
#: enforcement needs. ``unverified``: that could not be checked.
#: ``unspecified``: the adapter says nothing.
ENFORCEMENT_STATUSES = ("verified", "partial", "unenforced", "unsupported", "unverified", "unspecified")

#: The statuses a read-only run is refused under. Falling back to a weaker
#: command would call a run read-only that nothing is holding to it.
REFUSED_ENFORCEMENT = ("unsupported", "unverified")

#: The statuses a read-only run goes ahead under, warned about wherever the
#: seat is configured or run. Nothing checks afterwards what such a run did.
WARNED_ENFORCEMENT = ("unenforced",)

#: What ``Provider.tool_activity_reported`` may say ``parse_usage`` fills in:
#: nothing, the tool calls only, or the calls and what the tools printed back.
TOOL_ACTIVITY_NONE = "none"
TOOL_ACTIVITY_CALLS = "calls"
TOOL_ACTIVITY_OUTPUT = "calls and output"

#: A raw argument is named in a refusal by its flag, never by its value: the
#: value of ``--settings`` or ``-c`` can be a credential, and a refusal is
#: printed, persisted in job records and shown to the orchestrator session.
_RAW_ARGUMENT_NAME_LIMIT = 40


#: Discovery results are stable for the lifetime of a process, and both the
#: doctor and the wizard ask for them repeatedly. Keyed by (adapter, executable).
_DISCOVERY_CACHE: Dict[tuple, Any] = {}


def clear_discovery_cache() -> None:
    """Forget cached CLI discovery (used by tests)."""
    _DISCOVERY_CACHE.clear()


def unenforced_warning(name: str, enforcement: Dict[str, Any]) -> str:
    """The one wording for a read-only seat on a provider that cannot be held to reading."""
    return "read-only is NOT enforced by %s -- %s" % (name, enforcement.get("detail") or "")


def describe_exception(exc: BaseException) -> str:
    return "%s: %s" % (type(exc).__name__, exc)


def _prefixed(notes: Sequence[str], stderr: str) -> str:
    """``stderr`` with an adapter's own failures named above it."""
    if not notes:
        return stderr
    return "\n".join([*notes, stderr])


#: Only a UUID names a session on a command line or in a glob: the id is read
#: from a run log or from the CLI's own output, and anything else could smuggle
#: in a flag.
SESSION_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def json_lines(text: str) -> List[Dict[str, Any]]:
    """Every JSON object on its own line, skipping anything unparseable."""
    events: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


#: Where :func:`stdout_events` keeps its decoded copy in ``outcome.cache``.
_EVENTS_KEY = "stdout_events"


def stdout_events(outcome: Any) -> Tuple[Dict[str, Any], ...]:
    """:func:`json_lines` of ``outcome.stdout``, decoded once per run and
    shared by every hook that reads it.

    A tuple, so that no hook can reorder or drop what the next one reads.
    The events themselves are shared as well: a hook must treat them as
    read-only. Copying them for each caller would cost more than the
    decoding it saves. The decoded copy is kept in ``outcome.cache``; an
    outcome that cannot have one (one with ``__slots__``, a NamedTuple) is
    decoded again on each call.
    """
    stdout = outcome.stdout
    cache = getattr(outcome, "cache", None)
    if not isinstance(cache, dict):
        try:
            outcome.cache = cache = {}
        except AttributeError:
            return tuple(json_lines(stdout))
    cached = cache.get(_EVENTS_KEY)
    if cached is not None and cached[0] is stdout:
        return cached[1]
    events = tuple(json_lines(stdout))
    cache[_EVENTS_KEY] = (stdout, events)
    return events


def event_of_type(line: str, kind: str, key: str = "type") -> Optional[Dict[str, Any]]:
    """The JSON object on one stdout ``line``, if its ``key`` (``type`` unless
    named) is ``kind``.

    For an ``activity_of`` hook, which sees every line while the CLI runs: a
    line that does not name ``kind`` in quotes is never decoded.
    """
    if '"%s"' % kind not in line:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get(key) != kind:
        return None
    return event


def token_count(value: Any) -> Optional[int]:
    """A token count, or None. A bool is not a count; neither is a string or
    a negative number."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


class Detection:
    """Result of looking for a CLI on this machine."""

    def __init__(
        self,
        installed: bool,
        path: Optional[str] = None,
        version: Optional[str] = None,
        error: Optional[str] = None,
        authenticated: str = "unknown",
        auth_detail: str = "",
    ) -> None:
        self.installed = installed
        self.path = path
        self.version = version
        self.error = error
        # "present" | "missing" | "unknown" -- a state, never the credential.
        self.authenticated = authenticated
        self.auth_detail = auth_detail

    def to_dict(self) -> Dict[str, Any]:
        return {
            "installed": self.installed,
            "path": self.path,
            "version": self.version,
            "error": self.error,
            "authentication": self.authenticated,
            "authentication_detail": self.auth_detail,
        }


class ModelCandidate:
    """A model the CLI actually told us about (or a documented fallback)."""

    def __init__(self, value: str, family: str = "", label: str = "", source: str = "cli") -> None:
        self.value = value  # what gets passed to the CLI ("" = let the CLI decide)
        self.family = family or value
        self.label = label or value or "CLI default"
        # cli | cli-config | cli-help | builtin-fallback
        self.source = source

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "family": self.family, "label": self.label, "source": self.source}


class ResolvedModel:
    """How a configured (family, version policy) maps onto a CLI invocation."""

    def __init__(
        self,
        provider: str,
        family: str,
        version_policy: str,
        argument: Optional[str],
        display: str,
        source: str,
        note: str = "",
    ) -> None:
        self.provider = provider
        self.family = family
        self.version_policy = version_policy
        # None => omit the model flag entirely and let the CLI choose.
        self.argument = argument
        self.display = display
        self.source = source
        self.note = note

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "family": self.family,
            "version_policy": self.version_policy,
            "argument": self.argument,
            "display": self.display,
            "source": self.source,
            "note": self.note,
        }


class ModelResolutionError(RuntimeError):
    """Raised when a configured model cannot be resolved without guessing."""


class Usage:
    """What one delegated run cost, as the CLI itself reported it.

    Missing numbers stay missing. Estimating them from the prompt we sent would
    ignore the child CLI's own system prompt, tool schemas and the files it
    chose to read -- which is most of the input -- so the estimate would be
    wrong by more than it is right, and it would be wrong in the direction that
    makes a run look cheap. A number that cannot be trusted is worse than no
    number when the point of measuring is to decide what to cut.

    ``prompt_chars`` is the one figure this repository knows first-hand: the
    size of the prompt it composed. That is also the only part of the input it
    can shorten, so it is worth tracking separately from the total.

    ``tool_output_chars`` is named for what it is: the characters a tool
    *printed back to the agent*. It is not how much source the agent read. A
    reviewer that runs ``wc -l`` on a two-hundred-line file is billed three
    characters here, and one that runs ``cat`` on the same file is billed the
    whole of it -- indistinguishable from outside the CLI. How much of the
    repository a delegated run actually read is not knowable from here at all.
    """

    def __init__(
        self,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        cache_read_tokens: Optional[int] = None,
        cache_write_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
        source: str = "",
        prompt_chars: Optional[int] = None,
        tool_uses: Optional[int] = None,
        tool_uses_by_name: Optional[Dict[str, int]] = None,
        tool_output_chars: Optional[int] = None,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        #: Set only by a CLI that reports one figure and does not split it.
        #: Never derived by adding the two above: a CLI that reports both is
        #: also the CLI whose definition of "total" we would be guessing at.
        self.total_tokens = total_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_write_tokens = cache_write_tokens
        self.cost_usd = cost_usd
        #: Where the numbers came from: the CLI's own name for its report, or
        #: ``unreported`` when it told us nothing.
        self.source = source or "unreported"
        self.prompt_chars = prompt_chars
        #: How many times the agent called a tool, whatever the tool was.
        #: Counting only ``Read`` undercounts: ``Grep`` and ``Glob`` read files
        #: too, and so does a shell wherever a read-only run still has one --
        #: Codex does, and the one Claude run measured while designing this
        #: read with ``Bash`` (``wc -l``) before Claude's read-only runs were
        #: narrowed to ``Read``, ``Grep`` and ``Glob``.
        self.tool_uses = tool_uses
        self.tool_uses_by_name = tool_uses_by_name
        #: Observed tool output, not source read -- see the class docstring.
        #: Only the total: nothing aggregates a per-name breakdown of it, and
        #: one serialised into every run's event that can never be read back
        #: through the account is a cost with no reader. ``tool_uses_by_name``
        #: already answers which tools a run called.
        self.tool_output_chars = tool_output_chars

    @property
    def measured(self) -> bool:
        """True when the CLI reported at least one token count."""
        return any(value is not None for value in (self.input_tokens, self.output_tokens, self.total_tokens))

    @property
    def billed_tokens(self) -> Optional[int]:
        """One comparable figure per run, for ranking stages by size.

        Cache reads are deliberately left out: they are an order of magnitude
        cheaper, and folding them in would rank a well-cached stage above an
        expensive one. Use ``cost_usd`` when the question is money.
        """
        parts = [self.input_tokens, self.output_tokens, self.cache_write_tokens]
        known = [value for value in parts if value is not None]
        if known:
            return sum(known)
        return self.total_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
            "billed_tokens": self.billed_tokens,
            "prompt_chars": self.prompt_chars,
            "tool_uses": self.tool_uses,
            "tool_uses_by_name": self.tool_uses_by_name,
            "tool_output_chars": self.tool_output_chars,
            "source": self.source,
            "measured": self.measured,
        }


class RunResult:
    def __init__(
        self,
        ok: bool,
        exit_code: int,
        stdout: str,
        stderr: str,
        command: Sequence[str],
        duration: float,
        resolved: Optional[ResolvedModel] = None,
        timed_out: bool = False,
        stalled: bool = False,
        idle_for: float = 0.0,
        orphans_possible: bool = False,
        usage: Optional[Usage] = None,
        invoked: bool = True,
        session_id: Optional[str] = None,
        context_tokens: Optional[int] = None,
        session_init: Optional[Dict[str, Any]] = None,
        resume_rejected: bool = False,
        warnings: Optional[Sequence[str]] = None,
        suspended: float = 0.0,
        partial_output: str = "",
    ) -> None:
        self.ok = ok
        self.exit_code = exit_code
        self.stdout = redact(stdout)
        #: Text a failed run produced that is not its answer, which an adapter
        #: keeps out of ``stdout``: only ``run --output``'s ``.rejected`` file
        #: receives it, never the print, the job or a review report.
        self.partial_output = redact(partial_output)
        self.stderr = redact(stderr)
        self.command = list(command)
        self.duration = duration
        #: How much of ``duration`` the machine spent asleep. The runtime
        #: budget is charged ``duration - suspended``; 0 when nothing measured it.
        self.suspended = suspended
        self.resolved = resolved
        #: The total deadline was reached.
        self.timed_out = timed_out
        #: No output for the idle deadline -- the agent looks wedged, which is
        #: worth saying differently from "it took too long".
        self.stalled = stalled
        self.idle_for = idle_for
        self.orphans_possible = orphans_possible
        #: What the run cost. Never None, so callers need not guard; the
        #: numbers inside it are None when the CLI reported nothing.
        self.usage = usage or Usage()
        #: False when no CLI was started at all -- it was not installed, or a
        #: model could not be resolved. Such a run spent nothing, so counting
        #: it as one that failed to report would make the token account
        #: declare itself incomplete over a run that had nothing to report.
        self.invoked = invoked
        #: The session the run ended in, as the CLI reported it -- after a
        #: fork, not the session it continued.
        self.session_id = session_id
        #: The context the model last saw, in tokens, if the CLI said.
        self.context_tokens = context_tokens
        #: What the CLI reported when the session started: the facts a check
        #: of a resumed session's restrictions has to rest on.
        self.session_init = session_init
        #: True only when the CLI positively said the session asked for does
        #: not exist; any other failure is an ordinary one.
        self.resume_rejected = resume_rejected
        #: What the adapter wants said about this run whatever its outcome --
        #: stderr alone is dropped on success.
        self.warnings = [redact(str(warning)) for warning in warnings or ()]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stalled": self.stalled,
            "idle_for_seconds": round(self.idle_for, 2),
            "orphans_possible": self.orphans_possible,
            "duration_seconds": round(self.duration, 2),
            "suspended_seconds": round(self.suspended, 2),
            "command": self.command,
            "model": self.resolved.to_dict() if self.resolved else None,
            "invoked": self.invoked,
            "usage": self.usage.to_dict(),
            "session_id": self.session_id,
            "context_tokens": self.context_tokens,
            "session_init": self.session_init,
            "resume_rejected": self.resume_rejected,
            "warnings": list(self.warnings),
        }


class Launch(NamedTuple):
    """One run as :meth:`Provider._launch` was called for it, which
    :meth:`Provider.around_launch` may change before it starts."""

    prompt: str
    mode: str
    cwd: str
    model_spec: Optional[Dict[str, Any]]
    timeout: int
    extra_args: Sequence[str]
    env: Optional[Dict[str, str]]
    options: Optional[Dict[str, Any]]
    idle_timeout: Optional[float]
    resume_session: Optional[str]
    command_kwargs: Optional[Dict[str, Any]]
    #: The adapter's own arguments, after the caller's; never gated.
    own_args: Tuple[str, ...] = ()


#: What :meth:`Provider.run` gated before ``around_launch`` saw the run.
#: ``options`` carries ``args``, which the gate checked too.
_GATED_FIELDS = ("mode", "resume_session", "extra_args", "options")


def _gated(launch: Launch, name: str) -> Any:
    """A gated field of ``launch``, comparable: ``extra_args`` as a list."""
    value = getattr(launch, name)
    return list(value) if name == "extra_args" else value


class Provider:
    """Base adapter. Subclasses override the CLI-specific parts."""

    name = "base"
    display_name = "Base"
    executable = ""

    #: Documented fallbacks used only when the CLI tells us nothing. Keep these
    #: as families/aliases -- never dated snapshot ids.
    fallback_models: Sequence[ModelCandidate] = ()
    fallback_updated = ""

    #: True only when a healthy run of the command this adapter builds emits
    #: output *while working*. Measured, not assumed: it decides whether an
    #: idle-output deadline can distinguish a wedged agent from a busy one.
    streams_progress = False

    #: True when this adapter can continue an earlier session of a read-only
    #: run. The orchestrator still only asks when :meth:`resume_support`
    #: reports ``verified`` or ``trusted``.
    supports_resume = False

    #: The checks a version must pass before this adapter's resumed sessions
    #: are trusted. The strictest set unless an adapter names its own. That
    #: set includes the confinement and hooks checks, which the live check
    #: asks only of an adapter that sets ``confines_read_only`` and
    #: ``repository_hooks_file``; one that sets neither names its own, or no
    #: pass is ever recorded (the live check says which it never asked).
    required_resume_checks: Sequence[str] = verified.REQUIRED_RESUME_CHECKS

    #: True when :meth:`read_only_enforcement` is a constant that needs no
    #: subprocess, so ``doctor`` reports it whether or not the CLI is there.
    #: Read from the class everywhere: an instance value is ignored.
    static_enforcement = False

    #: The model family a preset gives this adapter in every slot, vouched for
    #: without running the CLI (like Codex's ``recommended-coding``). Set to a
    #: non-empty string to let presets fit this adapter; None keeps it out.
    #: Read from the class, like ``static_enforcement``: an instance value is ignored.
    preset_family: Optional[str] = None

    #: Options a write role takes only from the global config or ``--extra``.
    #: For an adapter that names any, no project-file option of a write role
    #: is honoured, raw arguments included.
    local_only_options: Sequence[str] = ()

    def __init__(self, executable: Optional[str] = None) -> None:
        if executable:
            self.executable = executable

    # -- discovery ---------------------------------------------------------

    def which(self) -> Optional[str]:
        # Where a run will start it from: on Windows never the current
        # directory, which may be the repository under review.
        return execution.find_program(self.executable)

    def _cached(self, kind: str, compute):
        key = (type(self).__module__, type(self).__name__, self.executable, kind)
        if key not in _DISCOVERY_CACHE:
            _DISCOVERY_CACHE[key] = compute()
        return _DISCOVERY_CACHE[key]

    def detect(self) -> Detection:
        return self._cached("detect", self._detect)

    def _detect(self) -> Detection:
        path = self.which()
        if not path:
            return Detection(False, error="%s not found on PATH" % self.executable)
        version, error = self.version()
        detection = Detection(True, path=path, version=version, error=error)
        detection.authenticated, detection.auth_detail = self.auth_status()
        return detection

    def version(self) -> "tuple[Optional[str], Optional[str]]":
        return self._cached("version", self._version)

    def _version(self) -> "tuple[Optional[str], Optional[str]]":
        completed = self._capture([self.executable, "--version"], timeout=30)
        if completed is None:
            return None, "could not execute %s --version" % self.executable
        if completed.returncode != 0:
            return None, (completed.stderr or completed.stdout or "").strip()[:200] or "non-zero exit"
        return (completed.stdout or completed.stderr or "").strip().splitlines()[0], None

    def auth_status(self) -> "tuple[str, str]":
        """Report whether credentials appear to exist -- never their values.

        This is a *presence* check only: it does not prove the session is still
        valid, so a run can still fail with an auth error afterwards.
        """
        return "unknown", ""

    def list_models(self) -> List[ModelCandidate]:
        """Model candidates discovered from the installed CLI, best effort."""
        return list(self._cached("models", self._discover_models))

    def _discover_models(self) -> List[ModelCandidate]:
        return list(self.fallback_models)

    def resolve_model(self, spec: Optional[Dict[str, Any]]) -> ResolvedModel:
        spec = spec or {}
        family = str(spec.get("family") or "").strip()
        policy = str(spec.get("version") or "latest")
        pinned = spec.get("id")
        if policy == "pinned":
            if not pinned:
                raise ModelResolutionError(
                    "%s: version policy is 'pinned' but no model id is set" % self.name
                )
            return ResolvedModel(
                self.name, family or str(pinned), policy, str(pinned), str(pinned), "config-pinned"
            )
        return self._resolve_latest(family)

    def _resolve_latest(self, family: str) -> ResolvedModel:
        raise NotImplementedError

    def config_families(self) -> List["tuple[str, str]"]:
        """``(family, what it resolves to now)`` for the families to put in a
        config, when the listed models are dated ids that go stale. Empty
        when the listed families are those; ``model list`` shows these apart."""
        return []

    # -- execution ---------------------------------------------------------

    def build_command(
        self,
        mode: str,
        resolved: ResolvedModel,
        cwd: str,
        extra_args: Sequence[str] = (),
        options: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        raise NotImplementedError

    # -- role options ------------------------------------------------------

    #: Provider-specific keys a role may set under ``options``. Subclasses add
    #: their own; ``args`` is understood by every adapter.
    option_keys: Sequence[str] = ("args",)

    def validate_options(self, options: Optional[Dict[str, Any]]) -> List[str]:
        """Return human-readable problems with a role's ``options`` mapping."""
        if options is None:
            return []
        if not isinstance(options, dict):
            return ["options must be a mapping"]
        problems: List[str] = []
        for key, value in options.items():
            if key not in self.option_keys:
                problems.append(
                    "options.%s is not understood by the %s provider (known: %s)"
                    % (key, self.name, ", ".join(self.option_keys))
                )
            elif key == "args" and not (
                isinstance(value, list) and all(isinstance(item, str) for item in value)
            ):
                problems.append("options.args must be a list of strings")
            elif key == "idle_timeout" and value is not None and not clocks.is_seconds(value):
                # Checked for every adapter that takes it, because `run` and
                # `review run` hand it on as it is: 0 or a negative stalled
                # every run at once, `true` was one second, and a string ended
                # `run` on a traceback.
                problems.append(
                    "options.idle_timeout must be a number of seconds above 0 and at most %d, or null"
                    % clocks.MAX_SECONDS
                )
        return problems

    @staticmethod
    def option_args(options: Optional[Dict[str, Any]]) -> List[str]:
        """The raw extra CLI arguments a role configured, if any."""
        if not isinstance(options, dict):
            return []
        args = options.get("args")
        return [str(item) for item in args] if isinstance(args, list) else []

    # -- read-only enforcement ---------------------------------------------

    #: How a refusal says what a read-only run of this adapter does accept.
    read_only_args_accepted = "no raw arguments"

    def refused_read_only_args(self, raw_args: Sequence[str], source: str) -> List[str]:
        """Problems with raw arguments a caller wants on a read-only run.

        An allowlist, and empty unless an adapter names something: a raw
        argument can reopen what the adapter's own flags close -- a later
        sandbox flag, a profile, a settings file -- and a denylist cannot know
        the flags a CLI adds next release. ``source`` is where the arguments
        came from (``options.args`` or ``--extra``).
        """
        raw = list(raw_args)
        return [self._raw_argument_problem(token, index, len(raw), source) for index, token in enumerate(raw)]

    def _raw_argument_problem(
        self, token: str, index: int, total: int, source: str, reason: str = "is not accepted"
    ) -> str:
        return "read-only run: %s (token %d of %d in %s) %s; read-only %s runs accept %s" % (
            self.describe_raw_argument(token),
            index + 1,
            total,
            source,
            reason,
            self.name,
            self.read_only_args_accepted,
        )

    @staticmethod
    def describe_raw_argument(token: str) -> str:
        """Name a raw argument without its value.

        ``--flag=value`` is named up to the ``=``, and a short option by its
        first two characters, because a CLI may read ``-sVALUE`` as ``-s
        VALUE``. Anything else is a value, and is only called one.
        """
        if token.startswith("--"):
            name = token.split("=", 1)[0]
        elif token.startswith("-") and len(token) >= 2:
            name = token[:2]
        else:
            return "a bare value"
        return "'%s'" % name[:_RAW_ARGUMENT_NAME_LIMIT]

    def read_only_arg_problems(
        self, mode: str, extra_args: Sequence[str] = (), options: Optional[Dict[str, Any]] = None
    ) -> List[str]:
        """Every problem with the caller's raw arguments on a ``mode`` run:
        ``options.args`` first, then ``--extra``; none outside a read-only mode,
        where the allowlist does not apply. The one list of what is checked;
        each caller says it in its own words."""
        if mode not in READ_ONLY_MODES:
            return []
        problems = self.refused_read_only_args(self.option_args(options), "options.args")
        problems += self.refused_read_only_args(list(extra_args), "--extra")
        return problems

    def read_only_refusal(
        self, mode: str, extra_args: Sequence[str] = (), options: Optional[Dict[str, Any]] = None
    ) -> Optional[RunResult]:
        """A refused run, if the caller's raw arguments cannot go on this one.

        Only the caller's arguments are looked at, before an adapter adds its
        own: those are the ones a config file or a command line put there.
        """
        problems = self.read_only_arg_problems(mode, extra_args, options)
        if not problems:
            return None
        # One line: the review path reports the last line of stderr as the
        # reviewer's error, and the command carries none of the arguments.
        return RunResult(False, 2, "", "; ".join(problems), [self.executable], 0.0, invoked=False)

    def read_only_enforcement(self) -> Dict[str, Any]:
        """How this CLI holds a plan or review run to reading, as far as known.

        ``status`` is one of :data:`ENFORCEMENT_STATUSES`; ``mechanism`` names
        the flags or sandbox that do it and ``detail`` says what that covers.
        Only what the CLI itself stops counts: an instruction in the prompt is
        not enforcement.
        """
        return {
            "status": "unspecified",
            "mechanism": "",
            "detail": "this adapter does not report how it enforces read-only runs",
        }

    # -- resuming a session ------------------------------------------------

    #: Flags :meth:`resume_help_text` must list before a session is resumed
    #: on the shared rule.
    resume_flags: Sequence[str] = ()
    #: ``detail`` when :meth:`resume_help_text` cannot be read.
    resume_help_unread = "could not read the help that lists the resume flags, so resuming is unverified"
    #: ``detail`` when flags are missing; ``%s`` is the missing ones, comma-joined.
    resume_flags_missing = "%s not advertised"
    #: ``detail`` when :meth:`version` gives nothing.
    resume_version_unread = "could not read the CLI's --version"

    def verified_resume(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """The adapter's module-level ``VERIFIED_RESUME`` (smoke_live.py finds
        it by that name); None, the default, keeps the adapter off the shared
        rule. A method, so a table rebound on the module is read at call time."""
        return None

    def resume_help_text(self) -> Optional[str]:
        """The help that has to list :attr:`resume_flags`; None if it could
        not be read, which leaves resuming unverified."""
        return None

    def resume_advertises(self, help_text: str, flag: str) -> bool:
        """Whether ``help_text`` lists ``flag`` as an option of its own. False
        here: what counts depends on the CLI's help layout (descriptions name
        other options), so an adapter on the shared rule must supply its matcher."""
        return False

    def resume_support(self, root: str) -> Dict[str, Any]:
        """Whether a resumed session of this CLI is known to stay read-only.

        ``status`` is ``verified``, ``trusted``, ``unverified``,
        ``unsupported`` or ``unspecified``; ``verified`` or ``trusted`` lets
        the orchestrator resume. ``trusted`` is a version newer than one that
        passed (``newer_than``), not checked itself. ``root`` is the
        workspace, which a per-machine record must lie outside of to be
        trusted. Not memoised: a record written a moment ago has to be read.

        An adapter that names a table (:meth:`verified_resume`) is judged by
        :func:`verified.resume_trust` once its help lists every one of
        :attr:`resume_flags` and its version can be read. A failure recorded
        on this machine outranks the built-in table: a regression seen here
        is not overruled by a release that saw none.
        """
        if not self.supports_resume:
            return {"status": "unsupported", "detail": "%s does not resume sessions" % self.name}
        table = self.verified_resume()
        # An empty table is still a table: every version is then unverified.
        if table is None:
            return {
                "status": "unspecified",
                "detail": "this adapter does not report whether a resumed session stays read-only",
            }
        report = self._resume_skeleton(None)
        if not self.resume_flags:
            report["detail"] = (
                "this adapter names no resume flags to look for in its help, so resuming is unverified"
            )
            return report
        text = self.resume_help_text()
        if text is None:
            report["detail"] = self.resume_help_unread
            return report
        missing = [flag for flag in self.resume_flags if not self.resume_advertises(text, flag)]
        if missing:
            report["status"] = "unsupported"
            report["missing"] = missing
            # Plain replacement, not %: an adapter's wording may hold a literal
            # % or no %s at all, and must not break doctor over it.
            report["detail"] = str(self.resume_flags_missing).replace("%s", ", ".join(missing))
            return report
        version = self.version()[0]
        if not version:
            report["detail"] = self.resume_version_unread
            return report
        trust = verified.resume_trust(
            self.name, version, self.resume_mechanism(), root, table, self.required_resume_checks
        )
        return self.resume_report(version, trust)

    def resume_mechanism(self) -> str:
        """The flags a resume record vouches for. A record made under other
        flags is stale. The fresh read-only mechanism unless an adapter's
        resumed command differs from its fresh one."""
        return str(self.read_only_enforcement().get("mechanism") or "")

    def resume_report(self, version: str, trust: Dict[str, Any]) -> Dict[str, Any]:
        """A :func:`verified.resume_trust` result as a :meth:`resume_support`
        report, in the same sentences for every adapter."""
        smoke = "python scripts/smoke_live.py --provider %s" % self.name
        entry = trust.get("entry") or {}
        report = self._resume_skeleton(version)
        status = trust.get("status")
        if status in ("passed", "newer"):
            verified_at = str(entry.get("verified_at") or "")
            source = trust.get("source")
            report["source"] = source
            report["verified_at"] = verified_at
            if status == "passed":
                report["status"] = "verified"
                report["detail"] = "resume verified for %s %s on %s (%s)" % (
                    self.name,
                    version,
                    verified_at,
                    source,
                )
                return report
            newer_than = trust.get("newer_than")
            report["status"] = "trusted"
            report["newer_than"] = newer_than
            words = (self.name, version, newer_than, verified_at, source, version, smoke)
            report["detail"] = (
                "%s %s is trusted to resume as newer than %s, verified on %s (%s); %s itself has not "
                "been checked -- run %s to check it" % words
            )
            return report
        blocked_by = trust.get("blocked_by")
        if status == "failed" and trust.get("unordered"):
            report["detail"] = (
                "%s %s cannot be ordered against the versions checked here, and a failure of the resume "
                "check is recorded on this machine; run %s" % (self.name, version, smoke)
            )
        elif status == "failed" and blocked_by:
            report["detail"] = (
                "%s %s, newer than the last pass this machine trusts, failed the resume check here; "
                "run %s again after fixing it" % (self.name, blocked_by, smoke)
            )
        elif status == "failed":
            detail = "%s %s failed the resume check on this machine; run %s again after fixing it" % (
                self.name,
                version,
                smoke,
            )
            report["detail"] = detail
        else:
            detail = "%s %s has not been verified to keep a resumed session read-only; run %s" % (
                self.name,
                version,
                smoke,
            )
            if trust.get("problem"):
                unread = "; the %s could not be read (%s), so only versions in the built-in table resume"
                detail += unread % (verified.RECORD, trust["problem"])
            report["detail"] = detail
        return report

    def _resume_skeleton(self, version: Optional[str]) -> Dict[str, Any]:
        """A full :meth:`resume_support` report that has decided nothing yet:
        ``unverified``, with every key in the order every adapter gives it."""
        return {
            "status": "unverified",
            "detail": "",
            "version": version,
            "source": None,
            "record": verified.record_path(self.name),
            "verified_at": None,
            "newer_than": None,
            "missing": [],
        }

    def resume_args(self, session_id: str) -> List[str]:
        """The arguments that continue ``session_id``; appended after the
        command :meth:`build_command` returns."""
        raise NotImplementedError("%s does not resume sessions" % self.name)

    def resume_command(
        self,
        mode: str,
        resolved: ResolvedModel,
        cwd: str,
        extra_args: Sequence[str],
        options: Optional[Dict[str, Any]],
        session_id: str,
    ) -> List[str]:
        """The command that continues ``session_id``: the fresh command with
        :meth:`resume_args` after it, unless an adapter's CLI resumes with a
        command of another shape."""
        command = list(self.build_command(mode, resolved, cwd, extra_args, options))
        return command + self.resume_args(session_id)

    def resume_rejected(
        self,
        outcome: "execution.ExecOutcome",
        mode: str,
        options: Optional[Dict[str, Any]],
        session_id: str,
    ) -> bool:
        """True only when the CLI positively said ``session_id`` does not exist.

        The orchestrator runs fresh once after a rejection, so a False here
        costs a retry and a wrong True spends an attempt on a run that had
        already failed for another reason.

        An adapter may also refuse to start a resumed run itself, returning a
        result with ``resume_rejected=True`` and ``invoked=False`` from
        :meth:`around_launch`; the orchestrator runs fresh once, as for a rejection
        by the CLI.
        """
        return False

    def parse_session(self, outcome: "execution.ExecOutcome") -> Dict[str, Any]:
        """``session_id``, ``context_tokens`` and ``init`` of a finished run,
        as far as the CLI printed them. Empty when it says nothing."""
        return {}

    # -- live check (scripts/smoke_live.py) --------------------------------
    #
    # What the live check may ask of this CLI, as measured. Each default is
    # "not asked": an adapter that sets none is checked as before. Only the
    # live check reads these; no run, and nothing the orchestrator decides,
    # depends on them, so ``confines_read_only`` is a claim to be checked,
    # not a guarantee anything enforces.

    #: True when the CLI confines a read-only run to its working directory.
    #: Asks ``stays confined (absolute/symlink)``, ``--add-dir widens`` and,
    #: for a resuming adapter, ``resumes confined (...)``.
    confines_read_only = False

    #: A repository-relative path the CLI reads command hooks from (Claude's
    #: ``settings.json`` hooks format). Non-empty asks ``ignores repository
    #: hooks`` and ``... on resume``.
    repository_hooks_file = ""

    #: A repository-relative path the CLI reads a sandbox setting from
    #: (Codex's ``config.toml`` format). Non-empty asks ``ignores repository
    #: config on resume``.
    repository_sandbox_config_file = ""

    #: What :meth:`parse_usage` fills in about tools: one of
    #: :data:`TOOL_ACTIVITY_NONE`, :data:`TOOL_ACTIVITY_CALLS` and
    #: :data:`TOOL_ACTIVITY_OUTPUT`.
    tool_activity_reported = TOOL_ACTIVITY_NONE

    #: The tool the CLI reads a file with, when a read-only run may also reach
    #: a shell; the tool check then asks for that tool by name.
    file_read_tool = ""

    #: True when implement mode is checked to write a file.
    implement_write_checked = False

    #: The role options that let a write run run a shell command without
    #: asking; non-None asks ``names a denied command`` and ``runs a command
    #: with skip_permissions``.
    permission_bypass_options: ClassVar[Optional[Dict[str, Any]]] = None

    def read_only_widening_args(self, directory: str) -> List[str]:
        """The raw arguments that let a read-only run also read ``directory``;
        they must pass this adapter's own read-only gate. None by default."""
        return []

    def denied_action_items(self, warning: str) -> Optional[List[Tuple[str, str]]]:
        """``(display name, kind)`` of each action a warning from
        :meth:`run_warnings` names as denied; None for any other warning.
        ``kind`` is the CLI's own word, and ``"command"`` is a shell command."""
        return None

    def resumed_session_problem(self, result: RunResult) -> Optional[str]:
        """The first way a resumed session falls short of read-only, or None.

        Read only from restrictions the CLI itself reported (an init event, a
        rollout), never from a run that simply did not write: a model that
        chose not to write proves nothing about a CLI that dropped a flag.
        """
        return "no reading of a resumed session's restrictions is known for %s" % self.name

    def command_line(
        self,
        mode: str,
        resolved: ResolvedModel,
        cwd: str,
        extra_args: Sequence[str] = (),
        options: Optional[Dict[str, Any]] = None,
        resume_session: Optional[str] = None,
    ) -> List[str]:
        """The command a run starts. Adapters override :meth:`build_command`,
        :meth:`resume_args` and :meth:`resume_command`, not this -- unless a
        run passes a keyword of its own through ``_launch(command_kwargs=)``.

        The resume arguments are the adapter's own, so they never pass
        through the caller's raw-argument gate, which :meth:`run` applies
        before any command is built; a ``build_command`` written before
        resuming existed is called as it always was.
        """
        if resume_session is not None:
            return list(self.resume_command(mode, resolved, cwd, extra_args, options, resume_session))
        return list(self.build_command(mode, resolved, cwd, extra_args, options))

    # -- running -----------------------------------------------------------

    def run(
        self,
        prompt: str,
        mode: str,
        cwd: str,
        model_spec: Optional[Dict[str, Any]] = None,
        timeout: int = 1800,
        extra_args: Sequence[str] = (),
        env: Optional[Dict[str, str]] = None,
        options: Optional[Dict[str, Any]] = None,
        idle_timeout: Optional[float] = None,
        resume_session: Optional[str] = None,
    ) -> RunResult:
        """Run the CLI once. Adapters override :meth:`around_launch`, not this.

        The raw-argument gate lives here so that every adapter passes through
        it, including one whose ``build_command`` never calls the base, and so
        that it sees the caller's arguments before an adapter appends its own.
        An adapter that overrides ``run`` has to carry the gate itself.

        ``resume_session`` is never a raw argument: it travels as a keyword to
        :meth:`command_line`, which appends the adapter's own
        :meth:`resume_args`. Only a read-only run may continue a session.
        """
        if mode not in MODES:
            raise ValueError("unknown mode %r" % mode)
        if resume_session is not None and mode not in READ_ONLY_MODES:
            raise ValueError("only a read-only run may resume a session")
        refusal = self.read_only_refusal(mode, extra_args, options)
        if refusal is not None:
            return refusal
        kwargs: Dict[str, Any] = {
            "model_spec": model_spec,
            "timeout": timeout,
            "extra_args": extra_args,
            "env": env,
            "options": options,
            "idle_timeout": idle_timeout,
        }
        # Passed only when there is one: an adapter that overrides
        # ``_launch`` with the signature it had before resuming existed has
        # to keep running fresh runs unchanged.
        if resume_session is not None:
            kwargs["resume_session"] = resume_session
        return self._launch(prompt, mode, cwd, **kwargs)

    def _launch(
        self,
        prompt: str,
        mode: str,
        cwd: str,
        model_spec: Optional[Dict[str, Any]] = None,
        timeout: int = 1800,
        extra_args: Sequence[str] = (),
        env: Optional[Dict[str, str]] = None,
        options: Optional[Dict[str, Any]] = None,
        idle_timeout: Optional[float] = None,
        resume_session: Optional[str] = None,
        command_kwargs: Optional[Dict[str, Any]] = None,
    ) -> RunResult:
        """``command_kwargs`` go to :meth:`command_line` as they are: what an
        adapter's override decides for one run before its command is built
        travels with the call, never on the instance, which parallel runs share.

        The run goes through :meth:`around_launch`, which an adapter overrides
        for work before or after the CLI runs.
        """
        called = Launch(
            prompt,
            mode,
            cwd,
            model_spec,
            timeout,
            extra_args,
            env,
            options,
            idle_timeout,
            resume_session,
            command_kwargs,
        )
        # A copy of what the gate saw: ``launch.options["args"].append(...)``
        # inside around_launch changes the caller's own objects, so comparing
        # against those would find nothing changed.
        checked = called._replace(extra_args=list(extra_args), options=copy.deepcopy(options))
        return self.around_launch(called, functools.partial(self._start, checked))

    def around_launch(self, launch: Launch, proceed: Callable[[Launch], RunResult]) -> RunResult:
        """Start the run ``launch`` describes by calling ``proceed``.

        An adapter overrides this for work before or after the CLI runs (a
        temporary file, a check of the output), passing ``proceed`` a
        ``launch._replace(...)``. ``prompt``, ``cwd``, ``model_spec``,
        ``timeout``, ``env``, ``idle_timeout``, ``command_kwargs`` and
        ``own_args`` may change; ``mode``, ``resume_session``, ``extra_args``
        and ``options`` may not, since :meth:`run` has already gated them, and
        ``proceed`` raises ``ValueError`` if they did. A subclass of an adapter
        that overrides this calls ``super().around_launch(launch, proceed)``.
        """
        return proceed(launch)

    def _start(self, checked: Launch, launch: Launch) -> RunResult:
        """The run itself: preflight, command, CLI, and its output read.

        ``checked`` holds copies of the gated fields as :meth:`run` gated them.
        """
        # around_launch runs after run()'s gate, so it may not change what the
        # gate decided on, whether by _replace or in place.
        changed = [name for name in _GATED_FIELDS if _gated(launch, name) != _gated(checked, name)]
        if changed:
            raise ValueError("around_launch may not change %s" % ", ".join(changed))
        # From here on the command is built from the copies that were checked.
        launch = launch._replace(**{name: getattr(checked, name) for name in _GATED_FIELDS})
        mode = launch.mode
        refusal, warnings = self._preflight(mode)
        if refusal is not None:
            return refusal

        resolved = self.resolve_model(launch.model_spec)
        extra_args = [*launch.extra_args, *launch.own_args]
        command = self.command_line(
            mode,
            resolved,
            launch.cwd,
            extra_args,
            launch.options,
            launch.resume_session,
            **(launch.command_kwargs or {}),
        )
        outcome = execution.execute(
            command,
            cwd=launch.cwd,
            prompt=launch.prompt,
            timeout=launch.timeout,
            idle_timeout=self.idle_timeout(launch.options, launch.idle_timeout),
            env=self._child_env(launch.env),
            **self._streaming(launch.cwd),
        )
        ran = _Ran(command, resolved, outcome)
        # Read before postprocess, which keeps only the final answer, and on
        # both paths below: a rejected resume is also a run whose output an
        # adapter may fail to read.
        session_fields, notes = self._read_session(outcome, launch, warnings)
        try:
            stdout, stderr = self.postprocess(outcome, mode)
        except Exception as exc:
            # The child ran and was measured; only the reading of its output
            # failed. A failed run is not a free one, so the measurement
            # travels with the failure instead of dying with the exception --
            # an adapter raising here used to reach the caller as a run of
            # unknown length, and the review path recorded it as taking 0s.
            # ``ok`` is False whatever the child exited with: what we cannot
            # read, we cannot use.
            stderr = _prefixed(notes, "%s\n%s" % (describe_exception(exc), outcome.stderr))
            # Nothing was parsed, so the account reports this run as one it
            # could not measure rather than as one that cost nothing.
            return _finished(ran, False, outcome.stdout, stderr, Usage(), session_fields)
        # Read from the raw output, before postprocess narrows it to the final
        # answer: the accounting the CLI prints is not part of it. Apart from
        # postprocess, because the two failures are not the same failure -- an
        # unreadable answer is a failed run, an unreadable invoice is a run
        # whose cost is unknown, and the account already has a word for that.
        # Some adapters read the invoice out of prose, so it is the fragile one.
        try:
            usage = self.parse_usage(outcome, mode) or Usage()
        except Exception as exc:
            # An invoice this adapter can no longer read looks exactly like a
            # CLI that reports none, so name it where the run's output is kept:
            # otherwise a parser broken by a CLI's output drifting degrades
            # every run to "unmeasured" with nothing to diagnose it from.
            usage = Usage()
            stderr = "%s\n%s" % (describe_exception(exc), stderr)
        usage.prompt_chars = len(launch.prompt)
        return _finished(ran, outcome.ok, stdout, _prefixed(notes, stderr), usage, session_fields)

    def _preflight(self, mode: str) -> Tuple[Optional[RunResult], List[str]]:
        """A run refused before anything is built, or None and the warnings
        it starts with."""
        detection = self.detect()
        if not detection.installed:
            return (
                RunResult(
                    False,
                    127,
                    "",
                    detection.error or "CLI not installed",
                    [self.executable],
                    0.0,
                    invoked=False,
                ),
                [],
            )
        # After detection: a CLI that is not there is reported as missing, not
        # as one whose enforcement could not be read.
        warnings: List[str] = []
        if mode in READ_ONLY_MODES:
            enforcement = self.read_only_enforcement()
            status = enforcement.get("status")
            if status in REFUSED_ENFORCEMENT:
                detail = enforcement.get("detail") or "read-only enforcement is %s" % status
                return RunResult(False, 2, "", str(detail), [self.executable], 0.0, invoked=False), []
            if status in WARNED_ENFORCEMENT:
                warnings.append(unenforced_warning(self.name, enforcement))
        return None, warnings

    def _streaming(self, cwd: str) -> Dict[str, Any]:
        """The keywords :func:`execution.execute` is given for the activity sink."""
        # ``on_line`` only when someone is listening: a run with no sink calls
        # ``execute`` exactly as it always did, stand-ins in tests included.
        streaming: Dict[str, Any] = {}
        sink = activity.current()
        if sink is not None:

            def on_line(line: str) -> None:
                # A faulty hook, ours or a third party's, loses its activity
                # and nothing else: the run's result must not depend on it.
                try:
                    act = self.activity_of(line, cwd)
                except Exception:
                    return
                sink.add(act)

            streaming["on_line"] = on_line
        return streaming

    def _read_session(
        self, outcome: "execution.ExecOutcome", launch: Launch, warnings: List[str]
    ) -> Tuple[Dict[str, Any], List[str]]:
        """The session fields of a finished run, and the notes to put above
        its stderr. ``warnings`` gains what :meth:`run_warnings` says."""
        mode, options = launch.mode, launch.options
        notes: List[str] = []
        rejected = False
        if launch.resume_session is not None:
            session_id = launch.resume_session
            rejected = _guarded(
                notes, lambda: bool(self.resume_rejected(outcome, mode, options, session_id)), False
            )
        session = _guarded(notes, lambda: self.parse_session(outcome) or {}, {})
        _guarded(
            notes,
            lambda: warnings.extend(str(warning) for warning in self.run_warnings(outcome, mode) or ()),
            None,
        )
        # Said above the adapter's own failures, as those are: on success the
        # caller shows ``warnings`` and drops stderr.
        notes = [*warnings, *notes]
        session_fields = {
            "session_id": session.get("session_id"),
            "context_tokens": session.get("context_tokens"),
            "session_init": session.get("init"),
            "resume_rejected": rejected,
            "warnings": warnings,
        }
        return session_fields, notes

    def idle_timeout(
        self, options: Optional[Dict[str, Any]] = None, requested: Optional[float] = None
    ) -> Optional[float]:
        """The no-output deadline, or None where this CLI cannot support one.

        An adapter must only return a number when a healthy run of the command
        it builds actually emits progress. Claiming otherwise turns a slow but
        working agent into a killed one.
        """
        if not self.streams_progress:
            return None
        configured = options.get("idle_timeout") if isinstance(options, dict) else None
        # A value `validate_options` refuses is ignored rather than trusted:
        # an adapter can be called with options nothing validated.
        if configured is not None and clocks.is_seconds(configured):
            return float(configured)
        return requested

    def activity_of(self, line: str, cwd: str) -> activity.Activity:
        """Tool uses one stdout line shows, and the context size if it says. Never model text.

        Called for each line while the CLI runs, only when a sink is
        installed (:mod:`orchestrator.activity`). Pick fields through
        :func:`activity.tool_line` rather than building lines from the
        input: every line is clipped and redacted afterwards, but only the
        allowlist keeps free text out. Nothing, by default.
        """
        return activity.NOTHING

    def postprocess(self, outcome: "execution.ExecOutcome", mode: str) -> "tuple[str, str]":
        """Turn raw child output into (stdout, stderr) for the caller."""
        return outcome.stdout, outcome.stderr

    def run_warnings(self, outcome: "execution.ExecOutcome", mode: str) -> List[str]:
        """What a finished run should say whatever its outcome, read from the
        raw output. Kept in ``RunResult.warnings`` and put above stderr."""
        return []

    def parse_usage(self, outcome: "execution.ExecOutcome", mode: str) -> Optional[Usage]:
        """What the run cost, if this CLI says so. None means it does not.

        Adapters must only return numbers the CLI actually printed. Inventing
        one here would silently corrupt every total downstream.
        """
        return None

    def _child_env(self, overrides: Optional[Dict[str, str]]) -> Dict[str, str]:
        """Inherit the user's environment so existing CLI auth keeps working.

        Every child is marked as delegated, so dev-orchestra's own hooks stay
        silent inside a run that loads the user's settings. The marker is set
        last, so no override can clear it.
        """
        env = dict(os.environ)
        if overrides:
            env.update(overrides)
        env[execution.DELEGATED_ENV] = "1"
        return env

    # -- helpers -----------------------------------------------------------

    def _help_output(self, *args: str) -> Optional[str]:
        """What ``<executable> <args>`` prints, or None if it could not be read."""
        completed = self._capture([self.executable, *args], timeout=45)
        if completed is None or completed.returncode != 0:
            return None
        return completed.stdout or ""

    def _capture(self, command: Sequence[str], timeout: int = 30) -> Optional[subprocess.CompletedProcess]:
        """What a short query of the CLI (``--version``, ``--help``, a model
        list) printed, or None when it could not be started or did not finish
        within ``timeout``.

        Run through :func:`execution.execute`, as a delegated run is, and not
        ``subprocess.run``: on timeout that kills only the CLI and then waits
        for its pipes, which a helper the CLI started can hold open forever
        (#274). Nothing is written to stdin.
        """
        try:
            cwd = os.getcwd()
        except OSError:
            return None
        outcome = execution.execute(list(command), cwd=cwd, timeout=timeout)
        if not outcome.started or outcome.timed_out or outcome.stalled:
            return None
        return subprocess.CompletedProcess(list(command), outcome.exit_code, outcome.stdout, outcome.stderr)


def _suspended_of(outcome: Any) -> float:
    """``outcome.suspended``, or 0 for an outcome built without one.

    An adapter or a test may hand back its own stand-in for
    ``execution.ExecOutcome``; one written before the field existed measured
    no sleep, so it is charged its whole duration, as it always was.

    Clamped to ``[0, outcome.duration]``, and 0 unless finite: callers charge
    ``duration - suspended``, so a NaN or an overlarge value would otherwise
    wipe out the run's whole charge rather than leave what was measured.
    """
    try:
        suspended = float(getattr(outcome, "suspended", 0.0) or 0.0)
        duration = float(getattr(outcome, "duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not (math.isfinite(suspended) and math.isfinite(duration)):
        return 0.0
    return min(max(suspended, 0.0), max(duration, 0.0))


class _Ran(NamedTuple):
    """A CLI that was started: the command, the model it ran under, and what
    became of it."""

    command: List[str]
    resolved: ResolvedModel
    outcome: "execution.ExecOutcome"


_T = TypeVar("_T")


def _guarded(notes: List[str], read: Callable[[], _T], default: _T) -> _T:
    """``read()``, or ``default`` once what it raised is added to ``notes``."""
    try:
        return read()
    except Exception as exc:
        notes.append(describe_exception(exc))
        return default


def _finished(
    ran: _Ran,
    ok: bool,
    stdout: str,
    stderr: str,
    usage: Usage,
    session_fields: Dict[str, Any],
) -> RunResult:
    """The result of a run that was started, carrying its measurement
    whatever could be read of its output."""
    outcome = ran.outcome
    return RunResult(
        ok,
        outcome.exit_code,
        stdout,
        stderr,
        ran.command,
        outcome.duration,
        ran.resolved,
        timed_out=outcome.timed_out,
        stalled=outcome.stalled,
        idle_for=outcome.idle_for,
        orphans_possible=outcome.orphans_possible,
        usage=usage,
        invoked=True,
        suspended=_suspended_of(outcome),
        **session_fields,
    )
