"""Antigravity CLI adapter.

Verified against ``agy`` 1.2.13 on Windows (``agy -p`` for non-interactive
runs, ``--output-format json`` for one JSON object at the end), and its
output reading against ``agy`` 1.2.16 (``--output-format stream-json``).

What ``stream-json`` prints, one JSON object per line, each with an ``event``
key and its body under the key of the same name (``tests/fixtures/agy/``):

* ``init`` once; then ``step_update`` lines: a ``user_input`` step, each
  ``agent_response`` step's ``DONE`` line carrying that step's ``usage``, and
  each ``tool`` step once ``ACTIVE`` (``tool_name``, ``tool_info.parameters``)
  and once ``DONE``. The final answer streams as ``text_delta`` first.
* ``result`` last: the object ``json`` prints (``status``, ``response``,
  ``usage``, ``conversation_id``, ``denied_actions`` when any), nested.
* The result's ``usage`` equals the sum of the steps' (5822 input, 36491
  cache read in the recording), so tokens come from the result alone. A
  step's ``input_tokens`` is the uncached part: ``input_tokens +
  cache_read_tokens`` grew 13671, 14125, 14517 over one run, the context size.
* A denied shell command (headless, without the bypass) ends ``SUCCESS``
  with an empty ``response`` and ``denied_actions``; recorded over stdin,
  assumed the same under ``-p``.

The run fails closed: stdout is ``result.response`` and never agy's raw
output. Every case but the first is a failed run:

* a result with status ``SUCCESS`` and a response: the response;
* ``SUCCESS`` with an empty response (the denied run): "";
* any other status, or none: the response, if any, as partial output;
* a stream with no usable result: the last streamed answer, as partial output;
* neither a stream nor a result (plain text, a renamed shape): "".

Partial output leaves stdout for ``RunResult.partial_output`` once the run
fails, so it reaches only ``run --output``'s ``.rejected`` file. Lines that
are not JSON go to stderr, clipped; JSON lines never do, as model text and
tool output live there. Denied actions alone are a warning: an implementer
may finish its task despite one.

What was measured, and what this adapter rests on:

* ``-p`` takes the prompt as its value: ``-p`` with nothing after it exits 2
  (``flag needs an argument: -p``). It goes last, so no flag is read as the
  prompt.
* stdin is not read: ``-p -`` sends the literal ``-``, and ``-p ""`` exits 1
  with ``{"status": "ERROR", "error": "Error: empty prompt. ..."}``. So the
  prompt goes in a file inside the workspace's ``.ai/`` that ``-p`` names;
  never on the command line, where any local process can read it.
* ``usage.output_tokens`` already includes ``thinking_tokens``: a thinking
  model's run reported input 12527, output 215, thinking 212, total 12742
  (= input + output). Thinking is not added on top.
* There is no read-only mode. ``--mode plan``, ``--mode plan --sandbox`` and
  ``--agent research`` each wrote a file and read outside the workspace, so a
  plan or review run is ``unenforced``: allowed from the global config with a
  warning, refused from the project file.
* Without ``--dangerously-skip-permissions`` file edits ran and shell commands
  were refused in headless mode; with it, a command ran. The flag is
  ``options.skip_permissions``, taken only from the global config or --extra.
* A trivial prompt costs about 12k-25k input tokens.

Model handling: families are names, never ids. ``default`` omits ``--model``;
``gemini-flash`` and ``gemini-pro`` pick the newest id ``agy models`` lists on
this machine. Nothing here enumerates model ids from memory.
"""

from __future__ import annotations

import glob
import os
import re
import tempfile
import time
from typing import Any, Callable, ClassVar, Dict, Iterator, List, NamedTuple, Optional, Sequence, Tuple

from .. import activity, execution
from .base import (
    MODE_IMPLEMENT,
    TOOL_ACTIVITY_CALLS,
    Launch,
    ModelCandidate,
    ModelResolutionError,
    Provider,
    ResolvedModel,
    RunResult,
    Usage,
    event_of_type,
    stdout_events,
    token_count,
)

DEFAULT_FAMILIES = ("default", "", "recommended", "auto")

#: The shared detail of every warning about a read-only seat on agy.
AGY_UNENFORCED = (
    "agy cannot be held to reading only: on agy 1.2.13, `--mode plan`, `--mode plan --sandbox` and "
    "`--agent research` each wrote a file and read outside the workspace. A plan or review run on "
    "agy can modify the working tree, `.ai/` (including the approval record), `.git/` and files "
    "outside the repository, and nothing checks afterwards. Allowed from the global config, or "
    "from the preset's fit when agy is the only CLI installed; set the role or list reviewers in "
    "the global config to keep it off agy."
)

#: How the denied-actions warning starts; :meth:`AgyProvider.denied_action_items`
#: reads it back.
DENIED_PREFIX = "agy denied "
_DENIED_RE = re.compile(r"^%s\d+ action\(s\): (.*?); set " % re.escape(DENIED_PREFIX))
#: One ``<display name> (<action>)`` item of that warning's list.
_DENIED_ITEM_RE = re.compile(r"(.+?) \(([^()]*)\)(?:, |$)")

#: An id ``agy models`` lists: ``gemini-<major>.<minor>-<kind>[-<effort>]``.
_MODEL_ID_RE = re.compile(r"^gemini-(\d+)\.(\d+)-(flash|pro)(?:-(low|medium|high))?$")

#: The families that pick an id from that list.
_FAMILY_RE = re.compile(r"^gemini-(?:(flash)(?:-(low|medium|high))?|(pro)(?:-(low|high))?)$")

#: The families ``model list`` offers for a config, each shown only when it
#: resolves on this machine.
CONFIG_FAMILIES = (
    "default",
    "gemini-flash",
    "gemini-flash-high",
    "gemini-flash-medium",
    "gemini-flash-low",
    "gemini-pro",
    "gemini-pro-high",
    "gemini-pro-low",
)

#: Unsuffixed families prefer, within one version, high over no suffix over
#: medium over low.
_EFFORT_ORDER = {"high": 0, None: 1, "medium": 2, "low": 3}

#: Where the prompt goes, inside the workspace, and what ``-p`` says instead.
#: A file is named ``agy-prompt-<pid>-<random>.md`` after the process that
#: wrote it, so a run removes only files whose process is gone.
PROMPT_DIR = ".ai"
PROMPT_PREFIX = "agy-prompt-"
PROMPT_SUFFIX = ".md"
PROMPT_FILE_INSTRUCTION = (
    "Read the file .ai/%s in the current directory and carry out the instructions in it exactly; "
    "it is your whole task."
)

#: What ``--print-command`` shows for the file, which only a run creates.
PROMPT_FILE_PLACEHOLDER = PROMPT_PREFIX + "<pid>-<random>" + PROMPT_SUFFIX

_PROMPT_FILE_RE = re.compile(r"^%s(\d+)-.*%s$" % (re.escape(PROMPT_PREFIX), re.escape(PROMPT_SUFFIX)))

#: The prefix of every warning that fails a run. Warnings carry the verdict
#: from ``run_warnings`` to ``around_launch``; nothing else recomputes it.
NO_ANSWER = "agy: no answer: "
NO_RESULT_STREAM = "no result event; streamed text is kept only as partial output"
NO_RESULT = "no result in agy's output"
EMPTY_RESPONSE = "the result's response was empty"
BAD_STATUS = "status %s"
UNREADABLE = "the output could not be read"

#: Stdout lines that are not JSON copied to stderr before the rest are counted.
DIAGNOSTIC_LIMIT = 20

#: A tool name shown or counted as it is; any other is ``tool``.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


class PromptFileError(OSError):
    """The prompt file cannot be written inside the workspace."""


class AgyProvider(Provider):
    name = "agy"
    display_name = "Antigravity CLI"
    executable = "agy"

    fallback_models = (ModelCandidate("", "default", "CLI default", "builtin-fallback"),)
    fallback_updated = "2026-09-30"
    option_keys = ("args", "skip_permissions")
    local_only_options = ("skip_permissions",)
    static_enforcement = True
    #: ``stream-json`` prints tool steps and the answer as they happen, but
    #: nothing while the model thinks. A non-final model step prints a single
    #: ``DONE`` line when it ends, so generating a tool call's arguments is
    #: silent, a large ``write_to_file`` included. Only short runs were
    #: measured (gaps of 6 s or less), so no idle deadline is claimed and the
    #: total deadline is the only one. Activity is reported whatever this says.
    streams_progress = False
    #: Left out on purpose. A resume check asks whether a resumed session
    #: keeps read-only, and agy's plan runs are ``unenforced``, so a version
    #: gate would protect nothing. ``--conversation <id>`` continues the
    #: original conversation with no fork, and was not measured. It would fit
    #: under verified.resume_trust() with ``required_resume_checks =
    #: ("reports a missing session",)``.
    supports_resume = False

    # What the live check asks (base.py, "live check"). The stream counts
    # tool steps but not what they printed; a headless run is denied shell
    # commands, so the tool check asks for the file-viewing tool by name, and
    # ``skip_permissions`` is the bypass whose counterpart is a named denial.
    tool_activity_reported = TOOL_ACTIVITY_CALLS
    file_read_tool = "view_file"
    implement_write_checked = True
    permission_bypass_options: ClassVar[Optional[Dict[str, Any]]] = {"skip_permissions": True}

    def denied_action_items(self, warning: str) -> Optional[List[Tuple[str, str]]]:
        """The items of a warning :func:`_denied_warning` wrote; the whole list
        as one item of no kind when no item in it parses.

        An entry agy gave as something other than an object has no
        ``(action)``. Where one follows the last item that parses, each of its
        ``", "``-separated pieces is kept as an item of no kind; one before an
        item that parses joins that item's name, since nothing in the text
        tells the two apart.
        """
        match = _DENIED_RE.match(str(warning))
        if not match:
            return None
        listed = match.group(1)
        items: List[Tuple[str, str]] = []
        position = 0
        while position < len(listed):
            item = _DENIED_ITEM_RE.match(listed, position)
            if not item:
                if not items:
                    return [(listed, "")]
                # Nothing after this point parses at all: the match is lazy
                # across ", ", so any later item would have matched here.
                items.extend((piece, "") for piece in listed[position:].split(", ") if piece)
                break
            items.append((item.group(1), item.group(2)))
            position = item.end()
        return items or [(listed, "")]

    def validate_options(self, options: Optional[Dict[str, Any]]) -> List[str]:
        problems = super().validate_options(options)
        if not isinstance(options, dict):
            return problems
        skip = options.get("skip_permissions")
        if skip is not None and not isinstance(skip, bool):
            problems.append("options.skip_permissions must be true or false")
        return problems

    def auth_status(self) -> "tuple[str, str]":
        return "unknown", "not detected for agy; run `agy` once to sign in if runs fail"

    # -- models ------------------------------------------------------------

    def _discover_models(self) -> List[ModelCandidate]:
        if not self.which():
            return list(self.fallback_models)
        completed = self._capture([self.executable, "models"], timeout=45)
        if completed is None or completed.returncode != 0:
            return list(self.fallback_models)
        listed = parse_models(completed.stdout or "")
        if not listed:
            return list(self.fallback_models)
        return [ModelCandidate("", "default", "CLI default", "cli-default"), *listed]

    def _resolve_latest(self, family: str) -> ResolvedModel:
        lowered = (family or "").strip().lower()
        if lowered in DEFAULT_FAMILIES:
            return ResolvedModel(
                self.name,
                family or "default",
                "latest",
                None,
                "agy default",
                "cli-default",
                "no --model flag passed; agy selects its default model",
            )
        listed = [candidate for candidate in self.list_models() if candidate.value]
        for candidate in listed:
            if candidate.value.lower() == lowered:
                return ResolvedModel(
                    self.name,
                    family,
                    "latest",
                    candidate.value,
                    candidate.value,
                    candidate.source,
                    "listed by `agy models` on this machine",
                )
        match = _FAMILY_RE.match(lowered)
        if match:
            kind = match.group(1) or match.group(3)
            effort = match.group(2) or match.group(4)
            chosen = _newest(listed, kind, effort)
            if chosen is not None:
                return ResolvedModel(
                    self.name,
                    family,
                    "latest",
                    chosen,
                    chosen,
                    "cli-catalog",
                    "newest %s listed by `agy models` on this machine" % lowered,
                )
        raise ModelResolutionError(
            "agy: the installed Antigravity CLI does not list %r. Run `dev-orchestra model list "
            "--provider agy`, use family `default`, `gemini-flash` or `gemini-pro`, or set "
            "model.version: pinned with model.id." % family
        )

    def config_families(self) -> List[Tuple[str, str]]:
        """The families this adapter resolves, with the id each picks now.

        ``agy models`` lists dated ids; a config that stores one goes stale,
        one that stores a family follows the newest.
        """
        found: List[Tuple[str, str]] = []
        for family in CONFIG_FAMILIES:
            try:
                resolved = self._resolve_latest(family)
            except ModelResolutionError:
                continue
            found.append((family, resolved.argument or resolved.display))
        return found

    # -- command -----------------------------------------------------------

    def build_command(
        self,
        mode: str,
        resolved: ResolvedModel,
        cwd: str,
        extra_args: Sequence[str] = (),
        options: Optional[Dict[str, Any]] = None,
        prompt_file: Optional[str] = None,
    ) -> List[str]:
        """``-p`` last, naming ``prompt_file`` once ``around_launch`` has written
        it, and a placeholder for it before (``--print-command``).

        No ``--mode``: plan mode was measured to write all the same, and to
        move the answer out of stdout. A read-only run gets no raw arguments
        (the base gate accepts none) and never the permission bypass.
        """
        options = options or {}
        command = [self.executable, "--output-format", "stream-json"]
        if resolved.argument:
            command += ["--model", resolved.argument]
        if mode == MODE_IMPLEMENT:
            if options.get("skip_permissions") is True:
                command.append("--dangerously-skip-permissions")
            command += self.option_args(options)
            command += list(extra_args)
        command += ["-p", PROMPT_FILE_INSTRUCTION % (prompt_file or PROMPT_FILE_PLACEHOLDER)]
        return command

    def command_line(
        self,
        mode: str,
        resolved: ResolvedModel,
        cwd: str,
        extra_args: Sequence[str] = (),
        options: Optional[Dict[str, Any]] = None,
        resume_session: Optional[str] = None,
        prompt_file: Optional[str] = None,
    ) -> List[str]:
        """The base command, naming the prompt file one run wrote. No resume:
        ``supports_resume`` is False, so the base never asks for one."""
        if resume_session is not None:
            return super().command_line(mode, resolved, cwd, extra_args, options, resume_session)
        return self.build_command(mode, resolved, cwd, extra_args, options, prompt_file=prompt_file)

    def read_only_enforcement(self) -> Dict[str, Any]:
        """Static: measured, and nothing needs asking."""
        return {
            "status": "unenforced",
            "mechanism": "none (the CLI has no read-only mode; --mode plan was measured to write)",
            "detail": AGY_UNENFORCED,
        }

    def around_launch(self, launch: Launch, proceed: Callable[[Launch], RunResult]) -> RunResult:
        """Put the prompt where agy reads it: in a file ``-p`` names.

        agy does not read stdin, so none is sent, and the prompt is never an
        argument: any local process can read a command line. The file is
        written inside the workspace's ``.ai/``, which needs no read outside
        it, readable only by its owner, and removed when the run ends; one
        left by a process that is gone is removed before the next one starts.
        """
        path: Optional[str] = None
        try:
            # Only for a CLI that is there: a missing one is reported by the
            # base as missing, and nothing is written for a run never started.
            if self.detect().installed:
                try:
                    path = _write_prompt_file(launch.cwd, launch.prompt)
                except OSError as exc:
                    return RunResult(False, 2, "", "agy: %s" % exc, [self.executable], 0.0, invoked=False)
            # Passed with the call, not kept on the instance: reviewers run
            # in parallel on one adapter, and each names its own file.
            result = super().around_launch(
                launch._replace(
                    prompt="",
                    command_kwargs={
                        **(launch.command_kwargs or {}),
                        "prompt_file": os.path.basename(path) if path is not None else None,
                    },
                ),
                proceed,
            )
            # A run never started, or one that already failed, is left as it is.
            if result.invoked and result.ok:
                if any(warning.startswith(NO_ANSWER) for warning in result.warnings):
                    result.ok = False
                elif not result.stdout.strip():
                    result.ok = False
                    result.stderr = "agy printed no answer\n" + result.stderr
            if not result.ok:
                # Text from a failed run is partial output, not the answer.
                result.partial_output, result.stdout = result.stdout, ""
            if result.usage.prompt_chars is not None:
                # The base measures what went on stdin, and that was nothing.
                result.usage.prompt_chars = len(launch.prompt)
            return result
        finally:
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    # -- output ------------------------------------------------------------

    def postprocess(self, outcome, mode: str) -> "tuple[str, str]":
        """The result's ``response`` as stdout, or partial output, or "";
        never agy's raw output, even when reading it fails."""
        try:
            return _answer(outcome)
        except Exception as exc:
            note = "%s%s (%s: %s)" % (NO_ANSWER, UNREADABLE, type(exc).__name__, exc)
            return "", "\n".join(part for part in [(outcome.stderr or "").rstrip("\n"), note] if part)

    def run_warnings(self, outcome, mode: str) -> List[str]:
        """A missing result, a status other than ``SUCCESS`` or an empty
        response, each prefixed ``NO_ANSWER`` (``around_launch`` fails the run
        on that prefix); denied actions and unreadable lines, unprefixed."""
        try:
            return _warnings(outcome)
        except Exception:
            return [NO_ANSWER + UNREADABLE]

    def parse_usage(self, outcome, mode: str) -> Optional[Usage]:
        """Tokens from the result's ``usage`` alone, which the steps sum to.
        ``output_tokens`` already counts ``thinking_tokens`` (measured), so
        thinking is not added; no cost is reported, and ``total_tokens`` is
        left for a CLI that reports only one.

        ``tool_uses`` only from a stream with ``step_update`` lines; otherwise
        None, not a measured 0. No ``tool_output_chars``: a tool step's
        ``output`` is a display summary (``4 lines, 17 bytes``), not what the
        tool returned."""
        events = stdout_events(outcome)
        parsed = _parse(events)
        tool_uses: Optional[int] = None
        by_name: Optional[Dict[str, int]] = None
        if parsed.stream and parsed.step_updates > 0:
            tool_uses, by_name = _tool_counts(events)
        usage = parsed.result.get("usage") if parsed.result is not None else None
        if isinstance(usage, dict):
            keys = ("input_tokens", "output_tokens", "cache_read_tokens")
            counts = {key: token_count(usage.get(key)) for key in keys}
            if any(value is not None for value in counts.values()):
                return Usage(
                    input_tokens=counts["input_tokens"],
                    output_tokens=counts["output_tokens"],
                    cache_read_tokens=counts["cache_read_tokens"],
                    source="agy stream-json result" if parsed.stream else "agy json result",
                    tool_uses=tool_uses,
                    tool_uses_by_name=by_name,
                )
        if tool_uses is not None:
            return Usage(source="agy stream events", tool_uses=tool_uses, tool_uses_by_name=by_name)
        return None

    def parse_session(self, outcome) -> Dict[str, Any]:
        """The result's ``conversation_id``, and the context of the last
        model step that reported one. No ``init``: agy does not resume."""
        events = stdout_events(outcome)
        parsed = _parse(events)
        conversation = parsed.result.get("conversation_id") if parsed.result is not None else None
        context_tokens: Optional[int] = None
        for step in _steps(events):
            context = _context_of(step)
            if context is not None:
                context_tokens = context
        return {
            "session_id": conversation if isinstance(conversation, str) and conversation else None,
            "context_tokens": context_tokens,
        }

    def activity_of(self, line: str, cwd: str) -> activity.Activity:
        """The tool an ``ACTIVE`` tool step starts, and the context a model
        step's ``DONE`` line reports. The ``DONE`` line of a tool repeats it
        and carries its output, so it shows nothing. Never ``text_delta`` nor
        a tool's output."""
        step = _body(event_of_type(line, "step_update", key="event"), "step_update")
        if step is None:
            return activity.NOTHING
        context = _context_of(step)
        if step.get("step_type") == "tool" and step.get("state") == "ACTIVE":
            name = _tool_name(step)
            if name:
                return activity.Activity([_tool_activity(name, step, cwd)], context)
        if context is None:
            return activity.NOTHING
        return activity.Activity((), context)


class _Parsed(NamedTuple):
    """What stdout holds: whether it is a stream, its result, and how many
    ``step_update`` lines it has."""

    stream: bool
    result: Optional[Dict[str, Any]]
    step_updates: int


def _is_stream(events: Sequence[Dict[str, Any]]) -> bool:
    """Whether any decoded line names its ``event``, whatever the name."""
    return any(isinstance(event.get("event"), str) for event in events)


def _body(event: Optional[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    """``event[name]`` when ``event`` is a ``name`` event and the body an object."""
    if event is None or event.get("event") != name:
        return None
    body = event.get(name)
    return body if isinstance(body, dict) else None


def _steps(events: Sequence[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    """The bodies of the ``step_update`` lines, in order."""
    for event in events:
        step = _body(event, "step_update")
        if step is not None:
            yield step


def _parse(events: Sequence[Dict[str, Any]]) -> _Parsed:
    """In a stream, the result is the body of the last ``result`` line, if an
    object; flat objects and ``step_update`` lines never count. Otherwise it
    is the last flat object with ``status`` or ``response``: an error agy
    prints before any stream, or a run forced to ``json``."""
    if not _is_stream(events):
        for event in reversed(events):
            if "status" in event or "response" in event:
                return _Parsed(False, event, 0)
        return _Parsed(False, None, 0)
    result: Optional[Dict[str, Any]] = None
    for event in reversed(events):
        if event.get("event") == "result":
            result = _body(event, "result")
            break
    return _Parsed(True, result, sum(1 for _ in _steps(events)))


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _diagnostics(stdout: Optional[str]) -> List[str]:
    """The stdout lines that are not JSON, clipped, at most ``DIAGNOSTIC_LIMIT``.

    A line that starts with ``{``, whole or torn, is never copied: model text
    and tool output live there.
    """
    lines = []
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("{"):
            continue
        clipped = activity.clip(stripped)
        if clipped:
            lines.append(clipped)
    if len(lines) > DIAGNOSTIC_LIMIT:
        hidden = len(lines) - DIAGNOSTIC_LIMIT
        lines = [*lines[:DIAGNOSTIC_LIMIT], "agy: %d more stdout line(s) not shown" % hidden]
    return lines


def _torn(stdout: Optional[str], events: Sequence[Dict[str, Any]]) -> int:
    """How many stdout lines start with ``{`` but are not a JSON object.

    ``events`` are those lines decoded (:func:`stdout_events` keeps only
    objects from lines starting with ``{``), so nothing is decoded twice.
    """
    braced = sum(1 for line in (stdout or "").splitlines() if line.strip().startswith("{"))
    return braced - len(events)


def _index(value: Any) -> bool:
    """Whether ``value`` is a step index: an int, not a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def _reconstructed(events: Sequence[Dict[str, Any]]) -> str:
    """The text of the last ``agent_response`` step that streamed any: its
    non-empty ``text_delta`` values, joined in order."""
    texts: Dict[int, List[str]] = {}
    for step in _steps(events):
        delta = step.get("text_delta")
        index = step.get("step_index")
        if step.get("step_type") != "agent_response" or not isinstance(delta, str) or not delta:
            continue
        if isinstance(index, int) and _index(index):
            texts.setdefault(index, []).append(delta)
    return "".join(texts[max(texts)]) if texts else ""


def _answer(outcome: Any) -> Tuple[str, str]:
    """stdout and stderr by the table in the module docstring. A result
    always wins over the streamed text."""
    events = stdout_events(outcome)
    parsed = _parse(events)
    extra = _diagnostics(outcome.stdout)
    if parsed.result is None:
        stdout = _reconstructed(events) if parsed.stream else ""
    else:
        response = parsed.result.get("response")
        stdout = response if isinstance(response, str) and _has_text(response) else ""
        error = parsed.result.get("error")
        if isinstance(error, str) and _has_text(error) and error.strip() not in (outcome.stderr or ""):
            extra.append(error.strip())
    stderr = "\n".join(part for part in [(outcome.stderr or "").rstrip("\n"), *extra] if part)
    return stdout, stderr


def _warnings(outcome: Any) -> List[str]:
    events = stdout_events(outcome)
    parsed = _parse(events)
    warnings: List[str] = []
    result = parsed.result
    if result is None:
        warnings.append(NO_ANSWER + (NO_RESULT_STREAM if parsed.stream else NO_RESULT))
    else:
        status = result.get("status")
        if status != "SUCCESS":
            shown = status if isinstance(status, str) and status else "missing"
            warnings.append(NO_ANSWER + BAD_STATUS % shown)
        elif not _has_text(result.get("response")):
            warnings.append(NO_ANSWER + EMPTY_RESPONSE)
        denied = _denied_warning(result)
        if denied is not None:
            warnings.append(denied)
    torn = _torn(outcome.stdout, events)
    if torn:
        warnings.append("agy: %d stdout line(s) could not be read as JSON" % torn)
    return warnings


def _denied_warning(result: Dict[str, Any]) -> Optional[str]:
    denied = result.get("denied_actions")
    if not isinstance(denied, list) or not denied:
        return None
    named = []
    for item in denied:
        if isinstance(item, dict):
            named.append("%s (%s)" % (item.get("display_name") or "?", item.get("action") or "?"))
        else:
            named.append(str(item))
    return DENIED_PREFIX + (
        "%d action(s): %s; set options.skip_permissions: true in the global config, "
        "or pass --extra --dangerously-skip-permissions, for the implementer to run commands"
        % (len(denied), ", ".join(named))
    )


def _tool_name(step: Dict[str, Any]) -> str:
    """``tool_name``, else ``tool_info.name``; "" when neither is a name."""
    name = step.get("tool_name")
    if not isinstance(name, str) or not name:
        info = step.get("tool_info")
        name = info.get("name") if isinstance(info, dict) else None
    return name if isinstance(name, str) else ""


def _counted_name(step: Dict[str, Any]) -> str:
    name = _tool_name(step)
    if not name:
        return "unknown"
    return name if _TOOL_NAME_RE.match(name) else "tool"


def _tool_counts(events: Sequence[Dict[str, Any]]) -> Tuple[int, Dict[str, int]]:
    """Tool steps: each distinct index once, whether seen ``ACTIVE``,
    ``DONE`` or both; a line without one counts once if ``ACTIVE``. Named by
    the first line seen for the step."""
    seen = set()
    by_name: Dict[str, int] = {}
    for step in _steps(events):
        if step.get("step_type") != "tool":
            continue
        index = step.get("step_index")
        if _index(index):
            if index in seen:
                continue
            seen.add(index)
        elif step.get("state") != "ACTIVE":
            continue
        name = _counted_name(step)
        by_name[name] = by_name.get(name, 0) + 1
    return sum(by_name.values()), by_name


def _context_of(step: Dict[str, Any]) -> Optional[int]:
    """The context a model step saw: ``input_tokens + cache_read_tokens`` of
    an ``agent_response`` step's ``DONE`` line, when both are counts."""
    if step.get("step_type") != "agent_response" or step.get("state") != "DONE":
        return None
    usage = step.get("usage")
    if not isinstance(usage, dict):
        return None
    uncached = token_count(usage.get("input_tokens"))
    cached = token_count(usage.get("cache_read_tokens"))
    if uncached is None or cached is None:
        return None
    return uncached + cached


def _tool_activity(name: str, step: Dict[str, Any], cwd: str) -> str:
    """The line one tool step shows; only the parameters named here are read."""
    info = step.get("tool_info")
    parameters = info.get("parameters") if isinstance(info, dict) else None
    if not isinstance(parameters, dict):
        parameters = {}
    if name == "view_file":
        return activity.tool_line("Read", {"file_path": parameters.get("AbsolutePath")}, cwd)
    if name == "write_to_file":
        return activity.tool_line("Write", {"file_path": parameters.get("TargetFile")}, cwd)
    if name == "run_command":
        return activity.tool_line("Bash", {"command": parameters.get("CommandLine")}, cwd)
    if not _TOOL_NAME_RE.match(name):
        return "tool"
    return activity.tool_line(name, None, cwd)


def parse_models(text: str) -> List[ModelCandidate]:
    """``id<TAB>display`` lines of ``agy models``; every other line is ignored."""
    candidates: List[ModelCandidate] = []
    seen = set()
    for line in text.splitlines():
        if "\t" not in line:
            continue
        model_id, label = (part.strip() for part in line.split("\t", 1))
        if not model_id or model_id.lower() in seen:
            continue
        seen.add(model_id.lower())
        candidates.append(ModelCandidate(model_id, model_id, label or model_id, "cli-catalog"))
    return candidates


def _newest(listed: Sequence[ModelCandidate], kind: str, effort: Optional[str]) -> Optional[str]:
    """The newest listed id of ``kind``; with ``effort``, only ids carrying it."""
    ranked: List[Tuple[Tuple[int, int, int], str]] = []
    for candidate in listed:
        match = _MODEL_ID_RE.match(candidate.value.lower())
        if not match or match.group(3) != kind:
            continue
        found = match.group(4)
        if effort is not None and found != effort:
            continue
        key = (-int(match.group(1)), -int(match.group(2)), _EFFORT_ORDER.get(found, 9))
        ranked.append((key, candidate.value))
    return min(ranked)[1] if ranked else None


def _prompt_directory(cwd: str) -> str:
    """``<cwd>/.ai``, made if missing, refused unless it is really there.

    A ``.ai`` that is a link, or resolves anywhere else, would have the
    prompt written, and files removed, outside the workspace.
    """
    root = os.path.realpath(cwd)
    directory = os.path.join(root, PROMPT_DIR)
    if os.path.islink(directory):
        raise PromptFileError(
            "%s is a link; the prompt file is written only inside the workspace" % directory
        )
    os.makedirs(directory, exist_ok=True)
    # A junction is not a link to ``islink`` before Python 3.12; resolving
    # catches it, and anything else that leads out.
    if os.path.normcase(os.path.realpath(directory)) != os.path.normcase(directory):
        raise PromptFileError(
            "%s resolves elsewhere; the prompt file is written only inside the workspace" % directory
        )
    return directory


#: How old a prompt file of a gone process must be before it is removed. A pid
#: that cannot be opened (another user's, an elevated one, one on another host
#: sharing the workspace) reads as gone, so age is the second condition: no run
#: lasts this long.
ORPHAN_MIN_AGE_SECONDS = 24 * 3600


def _remove_orphaned_prompt_files(directory: str) -> None:
    """Remove the prompt files whose writing process is gone.

    Never this process's (another reviewer thread may be using one) nor a
    live process's, nor one whose name carries no process id, nor one younger
    than ``ORPHAN_MIN_AGE_SECONDS``.
    """
    now = time.time()
    for stale in glob.glob(os.path.join(glob.escape(directory), PROMPT_PREFIX + "*" + PROMPT_SUFFIX)):
        match = _PROMPT_FILE_RE.match(os.path.basename(stale))
        if not match:
            continue
        pid = int(match.group(1))
        if pid == os.getpid() or execution.pid_alive(pid):
            continue
        try:
            if now - os.path.getmtime(stale) < ORPHAN_MIN_AGE_SECONDS:
                continue
            os.unlink(stale)
        except OSError:
            pass


def _write_prompt_file(cwd: str, prompt: str) -> str:
    """Write ``prompt`` to a new file in ``<cwd>/.ai/``, after removing any a
    process that is gone left there."""
    directory = _prompt_directory(cwd)
    _remove_orphaned_prompt_files(directory)
    prefix = "%s%d-" % (PROMPT_PREFIX, os.getpid())
    handle, path = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=PROMPT_SUFFIX)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(prompt)
    except BaseException:
        # The caller never gets the path, so a partial prompt would stay.
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def build_provider(executable: Optional[str] = None) -> AgyProvider:
    return AgyProvider(executable)
