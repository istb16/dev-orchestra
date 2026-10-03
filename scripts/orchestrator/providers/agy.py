"""Antigravity CLI adapter.

Verified against ``agy`` 1.2.13 on Windows (``agy -p`` for non-interactive
runs, ``--output-format json`` for one JSON object at the end).

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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .. import execution
from .base import (
    MODE_IMPLEMENT,
    Launch,
    ModelCandidate,
    ModelResolutionError,
    Provider,
    ResolvedModel,
    RunResult,
    Usage,
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
    #: ``json`` prints once, at the end.
    streams_progress = False
    #: Left out on purpose. A resume check asks whether a resumed session
    #: keeps read-only, and agy's plan runs are ``unenforced``, so a version
    #: gate would protect nothing. ``--conversation <id>`` continues the
    #: original conversation with no fork, and was not measured. It would fit
    #: under verified.resume_trust() with ``required_resume_checks =
    #: ("reports a missing session",)``.
    supports_resume = False

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
        command = [self.executable, "--output-format", "json"]
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
        """The ``response`` field as stdout; ``AGY_ERROR`` lines and the
        ``error`` field onto stderr. Anything else passes through."""
        payload = _result_event(stdout_events(outcome))
        if payload is None:
            return outcome.stdout, outcome.stderr
        response = payload.get("response")
        stdout = response if isinstance(response, str) and response.strip() else outcome.stdout
        extra = [line.strip() for line in (outcome.stdout or "").splitlines() if "AGY_ERROR" in line]
        error = payload.get("error")
        if isinstance(error, str) and error.strip() and error.strip() not in (outcome.stderr or ""):
            extra.append(error.strip())
        stderr = "\n".join(part for part in [(outcome.stderr or "").rstrip("\n"), *extra] if part)
        return stdout, stderr

    def run_warnings(self, outcome, mode: str) -> List[str]:
        payload = _result_event(stdout_events(outcome))
        if payload is None:
            return []
        warnings: List[str] = []
        status = payload.get("status")
        if isinstance(status, str) and status and status != "SUCCESS":
            warnings.append("agy reported status %s" % status)
        denied = payload.get("denied_actions")
        if isinstance(denied, list) and denied:
            named = []
            for item in denied:
                if isinstance(item, dict):
                    named.append("%s (%s)" % (item.get("display_name") or "?", item.get("action") or "?"))
                else:
                    named.append(str(item))
            warnings.append(
                "agy denied %d action(s): %s; set options.skip_permissions: true in the global config, "
                "or pass --extra --dangerously-skip-permissions, for the implementer to run commands"
                % (len(denied), ", ".join(named))
            )
        return warnings

    def parse_usage(self, outcome, mode: str) -> Optional[Usage]:
        """``usage`` from the JSON result. ``output_tokens`` already counts
        ``thinking_tokens`` (measured), so thinking is not added; no cost is
        reported, and ``total_tokens`` is left for a CLI that reports only one."""
        payload = _result_event(stdout_events(outcome))
        usage = payload.get("usage") if payload else None
        if not isinstance(usage, dict):
            return None
        keys = ("input_tokens", "output_tokens", "cache_read_tokens")
        counts = {key: token_count(usage.get(key)) for key in keys}
        if all(value is None for value in counts.values()):
            return None
        return Usage(
            input_tokens=counts["input_tokens"],
            output_tokens=counts["output_tokens"],
            cache_read_tokens=counts["cache_read_tokens"],
            source="agy json result",
        )

    def parse_session(self, outcome) -> Dict[str, Any]:
        payload = _result_event(stdout_events(outcome))
        conversation = payload.get("conversation_id") if payload else None
        if isinstance(conversation, str) and conversation:
            return {"session_id": conversation}
        return {}


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


def _result_event(events: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The JSON result: the last object of stdout's JSON lines carrying
    ``status`` or ``response``."""
    for event in reversed(events):
        if "status" in event or "response" in event:
            return event
    return None


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
