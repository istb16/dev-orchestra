"""Codex CLI adapter.

Verified against ``codex`` 0.156.x (``codex exec`` for non-interactive runs).

Model handling: this adapter never enumerates OpenAI model ids from memory. The
``recommended-coding`` family resolves by *omitting* ``-m`` entirely, which
makes the CLI use whatever model it currently recommends (its ``config.toml``
default). A named family is accepted only when the *installed* CLI vouches for
it -- either it is the model the CLI is configured with, or it appears in the
catalogue ``codex debug models`` prints (0.154+). Older CLIs print nothing
there, and then a named family must be pinned by the user with
``model.version: pinned`` + ``model.id``.

Plan runs (the architect) add ``--json``, which prints JSONL events: the
session id is the first ``thread.started`` event's ``thread_id``, and usage
comes from ``turn.completed``. The prose usage footer is not printed under
``--json``. The final answer is still read from ``-o``. Measured on 0.156.1;
the recordings are in ``tests/fixtures/codex/``.

Resuming a session (an architect revising its own plan) forks it:
``codex exec fork <id> - --skip-git-repo-check --ignore-user-config -c
sandbox_mode="read-only" -m <model> --json``. The ``-`` is the prompt read
from stdin, which a fork given ``-o`` needs. ``--ignore-user-config`` keeps
the user's ``config.toml`` -- a profile, a ``sandbox_mode`` -- from loosening
the fork, and drops its model too, so ``-m`` is always passed: the resolved
model, or the one ``config.toml`` names, which the parent ran under. That is
the prevention. After the run, the fork's rollout under
``$CODEX_HOME/sessions`` has to name the parent and state a ``read-only``
sandbox for every turn, or the run fails and its answer is not used: that is
detection, after the fact, and cannot undo a write made during the run.

What this covers is the filesystem, as for a fresh read-only run (``partial``):
MCP servers and external side effects are not examined. ``--ignore-user-config``
probably leaves the MCP servers in the user's ``config.toml`` unloaded, but that
was not measured and is not claimed.

The adapter refuses to fork, as a rejected resume, when it has no model to
pass or when the parent's rollout does not place it in this workspace.
"""

from __future__ import annotations

import datetime
import fnmatch
import glob
import json
import os
import re
import tempfile
from typing import Any, Dict, List, Optional, Sequence

from .. import verified
from ..execution import ExecOutcome
from .base import (
    MODE_IMPLEMENT,
    MODE_PLAN,
    READ_ONLY_MODES,
    ModelCandidate,
    ModelResolutionError,
    Provider,
    ResolvedModel,
    RunResult,
    Usage,
)

RECOMMENDED_FAMILIES = ("recommended-coding", "recommended", "default", "auto", "latest", "")

#: How the CLI states what a run cost. Unlike everything else in this adapter
#: these are read off human-readable output rather than a documented schema, so
#: they are patterns to *try*, in order, and a miss reports nothing rather than
#: guessing. ``codex exec`` prints a single total, not an input/output split.
#: The accounting footer ``codex exec`` prints, which is a label on its own
#: line followed by one number:
#:
#:     tokens used
#:     3,877
#:
#: Anchored to the start of a line, and the number must start with a digit.
#: Neither is fussiness. This is matched against the CLI's own output, and
#: that output contains the agent's *answer* -- which, for a reviewer reading
#: this repository, is liable to discuss token accounting in prose. An
#: unanchored pattern read a finding's evidence as the report: a review that
#: quoted ``input_tokens: 12`` recorded twelve billed tokens and threw the
#: real total away. The number is what this exists to know; a sentence about
#: a number is not it.
_USAGE_TOTAL_RES = (
    re.compile(r"^[ \t]*tokens?[ \t]+used[ \t]*:?[ \t]*\n?[ \t]*(\d[\d,_]*)", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*total[ \t]+tokens?[ \t]*:?[ \t]*\n?[ \t]*(\d[\d,_]*)", re.IGNORECASE | re.MULTILINE),
)

#: Sandbox policies `codex exec -s` accepts, per its own --help.
SANDBOX_POLICIES = ("read-only", "workspace-write", "danger-full-access")

_TOML_MODEL_RE = re.compile(r"^\s*model\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)

#: Only a UUID goes on the command line after ``fork``, or into a glob: the
#: id is read from the run log or from the CLI's own output.
_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
#: What ``codex exec fork --help`` has to list before a session is resumed.
_FORK_FLAGS = ("-c", "-m", "--json", "-o", "--skip-git-repo-check", "--ignore-user-config")
#: The ``-c`` override a fork runs under, quotes included: a TOML string.
_FORK_SANDBOX = 'sandbox_mode="read-only"'
#: What a resume record vouches for. Filesystem only: MCP servers and external
#: side effects are not examined, as for a fresh read-only run.
_FORK_MECHANISM = (
    "--ignore-user-config -c %s (fork; filesystem sandbox confirmed from the fork's rollout)" % _FORK_SANDBOX
)
#: How the CLI says the thread asked for has no rollout, before the id.
_MISSING_THREAD = "thread/fork failed: no rollout found for thread id "

#: Versions whose forked sessions were measured to stay read-only, by the
#: checks smoke_live.py runs. Keyed by the first line of `codex --version`.
#: Filled from the entry smoke_live.py prints.
VERIFIED_RESUME: Dict[str, Dict[str, Any]] = {
    "codex-cli 0.156.1": {
        "verified_at": "2026-09-30T14:55:24Z",
        "read_only_mechanism": (
            '--ignore-user-config -c sandbox_mode="read-only" '
            "(fork; filesystem sandbox confirmed from the fork's rollout)"
        ),
        "checks": [
            "resolves a model",
            "answers a review prompt",
            "reports what it spent",
            "reports its tool activity",
            "stays read-only",
            "resumes read-only",
            "forks the session",
            "reports a missing session",
            "ignores repository config on resume",
        ],
        "model": "gpt-6-sol",
        "dev_orchestra": "0.15.0",
    },
}


def codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


class CodexProvider(Provider):
    name = "codex"
    display_name = "Codex CLI"
    executable = "codex"

    fallback_models = (
        ModelCandidate(
            "", "recommended-coding", "CLI default (recommended coding model)", "builtin-fallback"
        ),
    )
    fallback_updated = "2026-09-10"
    option_keys = ("args", "sandbox", "approve")
    #: Turned on once ``python scripts/smoke_live.py --provider codex`` passed
    #: every check in :attr:`required_resume_checks` and its entry went into
    #: :data:`VERIFIED_RESUME`.
    supports_resume = True
    #: No confinement check (what a Codex run may read was not measured) and
    #: no hooks (Codex reads no ``.claude/settings.json``); a repository
    #: ``.codex/config.toml`` instead.
    required_resume_checks = (
        "stays read-only",
        "resumes read-only",
        "forks the session",
        "reports a missing session",
        "ignores repository config on resume",
    )

    def validate_options(self, options: Optional[Dict[str, Any]]) -> List[str]:
        problems = super().validate_options(options)
        if not isinstance(options, dict):
            return problems
        sandbox = options.get("sandbox")
        if sandbox is not None and sandbox not in SANDBOX_POLICIES:
            problems.append("options.sandbox %r is not one of %s" % (sandbox, ", ".join(SANDBOX_POLICIES)))
        approve = options.get("approve")
        if approve is not None and not isinstance(approve, bool):
            problems.append("options.approve must be true or false")
        return problems

    def auth_status(self) -> "tuple[str, str]":
        if os.environ.get("OPENAI_API_KEY"):
            return "present", "OPENAI_API_KEY is set in the environment"
        auth_file = os.path.join(codex_home(), "auth.json")
        if os.path.isfile(auth_file):
            return "present", "CLI credential store found"
        return "unknown", "no credential file found; run `codex login` if runs fail"

    def configured_model(self) -> Optional[str]:
        """The model the installed CLI is configured to use, read from its config."""
        config_path = os.path.join(codex_home(), "config.toml")
        if not os.path.isfile(config_path):
            return None
        try:
            with open(config_path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            return None
        match = _TOML_MODEL_RE.search(text)
        return match.group(1) if match else None

    def _catalog_models(self) -> List[ModelCandidate]:
        """Models the installed CLI publishes via ``codex debug models``.

        The command renders the CLI's own catalogue as JSON, so the ids come
        from the CLI rather than from this adapter's memory. Anything it does
        not print -- an older CLI without the command, a hidden internal model
        -- is simply not offered.
        """
        if not self.which():
            return []
        completed = self._capture([self.executable, "debug", "models"], timeout=45)
        if completed is None or completed.returncode != 0:
            return []
        try:
            payload = json.loads(completed.stdout or "")
        except ValueError:
            return []
        entries = payload.get("models") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return []
        candidates: List[ModelCandidate] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            slug = str(entry.get("slug") or "").strip()
            # "hide" marks models the CLI does not offer for selection.
            if not slug or entry.get("visibility") not in (None, "list"):
                continue
            label = str(entry.get("display_name") or slug)
            candidates.append(ModelCandidate(slug, slug, label, "cli-catalog"))
        return candidates

    def _discover_models(self) -> List[ModelCandidate]:
        candidates = [
            ModelCandidate("", "recommended-coding", "CLI default (recommended coding model)", "cli-default")
        ]
        configured = self.configured_model()
        if configured:
            candidates.append(
                ModelCandidate(
                    configured, configured, "%s (this CLI's configured model)" % configured, "cli-config"
                )
            )
        known = {candidate.value.lower() for candidate in candidates}
        for candidate in self._catalog_models():
            if candidate.value.lower() not in known:
                candidates.append(candidate)
        return candidates

    def _resolve_latest(self, family: str) -> ResolvedModel:
        lowered = (family or "").strip().lower()
        if lowered in RECOMMENDED_FAMILIES:
            configured = self.configured_model()
            display = configured or "codex default"
            return ResolvedModel(
                self.name,
                family or "recommended-coding",
                "latest",
                None,
                display,
                "cli-default",
                "no -m flag passed; the Codex CLI selects its current recommended model",
            )
        configured = self.configured_model()
        if configured and configured.lower() == lowered:
            return ResolvedModel(self.name, family, "latest", configured, configured, "cli-config")
        # list_models() is memoised for the process; calling _catalog_models()
        # here would re-spawn the CLI for every role that has to be resolved,
        # including on the `doctor --fast` path that skips model discovery.
        for candidate in self.list_models():
            if candidate.value and candidate.value.lower() == lowered:
                return ResolvedModel(
                    self.name,
                    family,
                    "latest",
                    candidate.value,
                    candidate.value,
                    candidate.source,
                    "listed by `codex debug models` on this machine",
                )
        raise ModelResolutionError(
            "codex: the installed Codex CLI does not vouch for %r, so it cannot be verified. "
            "Run `dev-orchestra model list` to see what it offers, use family "
            "'recommended-coding' to let the CLI choose, or set model.version: pinned "
            "with an explicit model.id." % family
        )

    def build_command(
        self,
        mode: str,
        resolved: ResolvedModel,
        cwd: str,
        extra_args: Sequence[str] = (),
        options: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        options = options or {}
        if mode in READ_ONLY_MODES:
            # Configured sandbox/approval settings are deliberately ignored for
            # planning and review: those stages stay read-only regardless.
            sandbox = "read-only"
        else:
            sandbox = str(options.get("sandbox") or "workspace-write")
        command = [
            self.executable,
            "exec",
            "--skip-git-repo-check",
            "--color",
            "never",
            "-C",
            cwd,
            "-s",
            sandbox,
        ]
        if resolved.argument:
            command += ["-m", resolved.argument]
        if mode == MODE_PLAN:
            # The session id is printed only in the JSONL events; the answer
            # still comes from ``-o``.
            command += ["--json"]
        if mode == MODE_IMPLEMENT and options.get("approve", True):
            # Route approvals through Codex's own automatic review instead of
            # blocking on a prompt that nobody can answer in a headless run.
            command += ["--approve-for-me"]
        command += self.option_args(options)
        command += list(extra_args)
        return command

    def parse_usage(self, outcome, mode: str) -> Optional[Usage]:
        """Whatever the CLI said a run cost, if it said anything.

        A plan run prints ``--json`` events, and its ``turn.completed`` event
        comes first; a review or implement run's output is a transcript, and
        only its footer is read, as before. Its
        ``input_tokens`` includes ``cached_input_tokens`` (measured: 15427
        with 11904 cached for a one-word prompt), so the cache reads are taken
        out and kept apart, as ``billed_tokens`` expects. ``output_tokens`` is
        as printed: whether ``reasoning_output_tokens`` is inside it was not
        shown, so it is not added.

        Otherwise best-effort by necessity: Codex reports usage in prose, so
        a wording change turns this into "unreported" rather than into a
        wrong number. The totals it prints cover the whole session, so they
        are recorded as ``total_tokens`` and never split into an input/output
        pair we would be inventing.
        """
        usage = _usage_from_events(_json_events(outcome.stdout)) if mode == MODE_PLAN else None
        if usage is not None:
            return usage
        return parse_usage_text(outcome.stdout, outcome.stderr)

    def parse_session(self, outcome: ExecOutcome) -> Dict[str, Any]:
        """The session a ``--json`` run ended in: the first ``thread.started``
        event's ``thread_id``, and only a UUID. Reads stdout only: a fresh run
        never touches the sessions tree."""
        for event in _json_events(outcome.stdout):
            if event.get("type") != "thread.started":
                continue
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and _SESSION_ID_RE.match(thread_id):
                return {"session_id": thread_id}
            return {}
        return {}

    # -- resuming a session ------------------------------------------------

    def fork_help_text(self) -> Optional[str]:
        """``codex exec fork --help``, read once per process; None if it could not be."""
        return self._cached("fork_help_text", self._read_fork_help)

    def _read_fork_help(self) -> Optional[str]:
        completed = self._capture([self.executable, "exec", "fork", "--help"], timeout=45)
        if completed is None or completed.returncode != 0:
            return None
        return completed.stdout or ""

    def resume_mechanism(self) -> str:
        return _FORK_MECHANISM

    def resume_support(self, root: str) -> Dict[str, Any]:
        """Whether this version's forked sessions are known to stay read-only.

        The same rule as Claude's (:func:`verified.resume_trust`), over this
        adapter's own checks and mechanism. Not memoised, so a record
        smoke_live.py just wrote is read.
        """
        if not self.supports_resume:
            return super().resume_support(root)
        report: Dict[str, Any] = {
            "status": "unverified",
            "detail": "",
            "version": None,
            "source": None,
            "record": verified.record_path(self.name),
            "verified_at": None,
            "newer_than": None,
            "missing": [],
        }
        text = self.fork_help_text()
        if text is None:
            report["detail"] = "could not read 'codex exec fork --help', so forking a session is unverified"
            return report
        missing = [flag for flag in _FORK_FLAGS if not _advertises(text, flag)]
        if missing:
            report["status"] = "unsupported"
            report["missing"] = missing
            report["detail"] = "codex exec fork does not advertise %s" % ", ".join(missing)
            return report
        version = self.version()[0]
        if not version:
            report["detail"] = "could not read 'codex --version'"
            return report
        trust = verified.resume_trust(
            self.name, version, self.resume_mechanism(), root, VERIFIED_RESUME, self.required_resume_checks
        )
        return self.resume_report(version, trust)

    def resume_args(self, session_id: str) -> List[str]:
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
            raise ValueError("a session to resume must be named by a UUID")
        return ["fork", session_id, "-"]

    def resume_command(
        self,
        mode: str,
        resolved: ResolvedModel,
        cwd: str,
        extra_args: Sequence[str],
        options: Optional[Dict[str, Any]],
        session_id: str,
    ) -> List[str]:
        """The fork, held read-only before it starts.

        No ``-s``, ``-C`` or ``--color``: ``fork`` does not take them, and
        the process runs in ``cwd``. With no model to pass, ``-m`` is left
        out here and :meth:`_launch` refuses the run.
        """
        command = [
            self.executable,
            "exec",
            *self.resume_args(session_id),
            "--skip-git-repo-check",
            "--ignore-user-config",
            "-c",
            _FORK_SANDBOX,
        ]
        model = resolved.argument or self.configured_model()
        if model:
            command += ["-m", model]
        command += ["--json"]
        command += self.option_args(options)
        command += list(extra_args)
        return command

    def resume_rejected(
        self,
        outcome: ExecOutcome,
        mode: str,
        options: Optional[Dict[str, Any]],
        session_id: str,
    ) -> bool:
        """True only for the CLI's own "no rollout for this thread" error.

        Matched to what 0.156.1 printed (``tests/fixtures/codex/``): a
        non-zero exit, no thread started, and a stderr line naming the id
        that was asked for. Stderr is read because it is the only place the
        CLI says so.
        """
        if outcome.exit_code == 0 or outcome.timed_out or outcome.stalled:
            return False
        if any(event.get("type") == "thread.started" for event in _json_events(outcome.stdout)):
            return False
        expected = _MISSING_THREAD + session_id
        return any(expected in line for line in (outcome.stderr or "").splitlines())

    # ``refused_read_only_args`` is the base allowlist, which accepts nothing:
    # everything a read-only run needs is built above, and every spelling that
    # could loosen it -- ``-s``, ``-sVALUE``, ``-c sandbox_mode=...``, a
    # ``--profile`` -- is a raw argument.

    def read_only_enforcement(self) -> Dict[str, Any]:
        """Static: ``-s read-only`` is always passed, and nothing needs asking."""
        return {
            "status": "partial",
            "mechanism": "-s read-only (CLI sandbox)",
            "detail": (
                "filesystem writes are blocked by the sandbox (measured); MCP servers were not "
                "examined, so external side effects are not covered"
            ),
        }

    def _launch(
        self,
        prompt: str,
        mode: str,
        cwd: str,
        model_spec: Optional[Dict[str, str]] = None,
        timeout: int = 1800,
        extra_args: Sequence[str] = (),
        env: Optional[Dict[str, str]] = None,
        options: Optional[Dict[str, Any]] = None,
        idle_timeout: Optional[float] = None,
        resume_session: Optional[str] = None,
    ) -> RunResult:
        """Capture the agent's final message via ``-o`` instead of scraping logs.

        Overrides ``_launch`` rather than ``run`` so that ``-o`` is added after
        the read-only gate has looked at the caller's arguments; it is this
        adapter's own argument, not a raw one.

        Every keyword the base method takes has to be named here *and* passed
        on. An override that quietly drops one is worse than no override: the
        caller's request disappears with nothing raised, or -- for a keyword
        this signature never learned about -- the call fails with a TypeError
        that looks like a bug in the caller.

        A resumed run is refused before it starts when there is no model to
        pass or the parent is not this workspace's, and failed after it ends
        unless the fork's rollout confirms a read-only sandbox. A CLI that is
        not installed is reported as missing by the base method, not as a
        refused session.
        """
        if resume_session is not None and self.detect().installed:
            refusal = self._fork_refusal(resume_session, cwd, model_spec)
            if refusal:
                return RunResult(
                    False, 2, "", refusal, [self.executable], 0.0, invoked=False, resume_rejected=True
                )
        handle, last_message_path = tempfile.mkstemp(prefix="codex-last-", suffix=".txt")
        os.close(handle)
        try:
            result = super()._launch(
                prompt,
                mode,
                cwd,
                model_spec=model_spec,
                timeout=timeout,
                extra_args=[*list(extra_args), "-o", last_message_path],
                env=env,
                options=options,
                idle_timeout=idle_timeout,
                resume_session=resume_session,
            )
            final = _read_text(last_message_path)
            if final.strip():
                result.stdout = _redacted(final)
            elif mode == MODE_PLAN and result.invoked:
                # Under --json stdout is the event stream, never the answer.
                result.stdout = ""
                result.ok = False
                result.stderr = _above("codex printed no final message (-o was empty)", result.stderr)
            if resume_session is not None and result.ok:
                self._confirm_fork(result, resume_session)
            return result
        finally:
            try:
                os.unlink(last_message_path)
            except OSError:
                pass

    def _fork_refusal(self, parent: str, cwd: str, model_spec: Optional[Dict[str, str]]) -> str:
        """Why the fork of ``parent`` must not start, or "".

        The workspace is read from the parent's rollout, the one place Codex
        records it: without it any conversation on this machine could be
        continued here. The path is compared, never stored or printed.
        """
        if not (self.resolve_model(model_spec).argument or self.configured_model()):
            return (
                "codex: a forked session runs under --ignore-user-config and needs a model; "
                "set model.family for the architect or model in config.toml"
            )
        path = _find_rollout(parent, walk=True)
        meta = _session_meta(_rollout_records(path)) if path else None
        if meta is None or meta.get("id") != parent or not _same_path(meta.get("cwd"), cwd):
            return "codex: the session to resume was not started in this workspace"
        return ""

    def _confirm_fork(self, result: RunResult, parent: str) -> None:
        """Fail ``result`` unless the fork's rollout says it ran read-only.

        Detection after the run: a write made during it is not undone, only
        reported, and the answer is not used. The flags in
        :meth:`resume_command` are what prevents one.
        """
        problem = "no rollout for the fork"
        thread_id = result.session_id
        path = _find_rollout(thread_id, walk=False) if thread_id else None
        records = _rollout_records(path) if path else []
        meta = _session_meta(records)
        contexts = [record.get("payload") for record in records if record.get("type") == "turn_context"]
        if path and (meta is None or meta.get("id") != thread_id or meta.get("forked_from_id") != parent):
            problem = "rollout does not name the parent"
        elif path and not contexts:
            problem = "no turn_context in the rollout"
        elif path:
            policies = [_sandbox_type(context) for context in contexts]
            loose = [policy for policy in policies if policy != "read-only"]
            if not loose:
                last = contexts[-1] if isinstance(contexts[-1], dict) else {}
                result.session_init = {
                    "sandbox_policy": "read-only",
                    "approval_policy": last.get("approval_policy"),
                }
                return
            problem = loose[0] or "no sandbox policy"
        verdict = (
            "the forked session's filesystem sandbox could not be confirmed read-only (%s); "
            "its output is not used" % problem
        )
        result.ok = False
        result.stdout = ""
        result.session_init = None
        result.stderr = _above(verdict, result.stderr)


def parse_usage_text(*streams: str) -> Optional[Usage]:
    """Scan the CLI's own output for its accounting footer. None if there is none.

    Only a total is read, because a total is all this CLI prints. Patterns for
    an input/output split used to be here too, speculatively, against a format
    the CLI has never emitted -- and since the text being scanned includes the
    agent's answer, the only thing they ever matched was a reviewer writing
    about token accounting. Splitting a number we were told into two we were
    not is not worth a parser that can be fed by the code under review.
    """
    for text in streams:
        if not text:
            continue
        for pattern in _USAGE_TOTAL_RES:
            matches = pattern.findall(text)
            if not matches:
                continue
            # The last match, because the footer is printed after the answer:
            # anything earlier that looks like one came from the answer.
            total = _digits(matches[-1])
            if total is not None:
                return Usage(total_tokens=total, source="codex output")
    return None


def _json_events(stdout: str) -> List[Dict[str, Any]]:
    """Every JSON object on its own line, skipping anything unparseable."""
    events: List[Dict[str, Any]] = []
    for line in (stdout or "").splitlines():
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


def _usage_from_events(events: List[Dict[str, Any]]) -> Optional[Usage]:
    """The last ``turn.completed`` event's usage, cache reads kept apart."""
    for event in reversed(events):
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            return None
        total_input = _count(usage.get("input_tokens"))
        cached = _count(usage.get("cached_input_tokens"))
        parsed = Usage(
            input_tokens=max(0, total_input - (cached or 0)) if total_input is not None else None,
            output_tokens=_count(usage.get("output_tokens")),
            cache_read_tokens=cached,
            source="codex json events",
        )
        return parsed if parsed.measured else None
    return None


def _count(value: Any) -> Optional[int]:
    """A token count, or None. A bool is not a count; neither is a string."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _advertises(help_text: str, option: str) -> bool:
    """Whether ``option`` appears in ``help_text`` as a whole token."""
    pattern = r"(?<![\w-])%s(?![\w-])" % re.escape(option)
    return re.search(pattern, help_text) is not None


def _above(note: str, stderr: str) -> str:
    """``stderr`` with the adapter's own verdict named above it."""
    return "\n".join([note, stderr]) if stderr else note


def _recent_days() -> List[datetime.date]:
    """Today and yesterday, local and UTC: the dated directories a rollout
    written in the last day can be in, whichever clock Codex names them by."""
    days: List[datetime.date] = []
    for now in (datetime.datetime.now(), datetime.datetime.now(datetime.timezone.utc)):
        for back in (0, 1):
            day = (now - datetime.timedelta(days=back)).date()
            if day not in days:
                days.append(day)
    return days


def _find_rollout(thread_id: str, walk: bool) -> Optional[str]:
    """The rollout file of ``thread_id``, or None.

    Looked for in the last day's dated directories; ``walk`` goes through
    the whole sessions tree after that. The id is a UUID and is escaped
    before it becomes part of a pattern.
    """
    if not isinstance(thread_id, str) or not _SESSION_ID_RE.match(thread_id):
        return None
    sessions = os.path.join(codex_home(), "sessions")
    name = "rollout-*-%s.jsonl" % glob.escape(thread_id)
    for day in _recent_days():
        directory = os.path.join(sessions, "%04d" % day.year, "%02d" % day.month, "%02d" % day.day)
        found = sorted(glob.glob(os.path.join(glob.escape(directory), name)))
        if found:
            return found[0]
    if not walk:
        return None
    for directory, _, files in os.walk(sessions):
        for filename in sorted(files):
            if fnmatch.fnmatchcase(filename, name):
                return os.path.join(directory, filename)
    return None


def _rollout_records(path: str) -> List[Dict[str, Any]]:
    """Every JSON object in a rollout; nothing when it cannot be read."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return _json_events(handle.read())
    except OSError:
        return []


def _session_meta(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The first ``session_meta`` payload: the rollout's own thread."""
    for record in records:
        if record.get("type") == "session_meta":
            payload = record.get("payload")
            return payload if isinstance(payload, dict) else None
    return None


def _same_path(recorded: Any, cwd: str) -> bool:
    if not isinstance(recorded, str) or not recorded:
        return False

    def real(path: str) -> str:
        return os.path.normcase(os.path.realpath(path))

    return real(recorded) == real(cwd)


def _sandbox_type(context: Any) -> str:
    """``turn_context.payload.sandbox_policy.type``, or "" when absent."""
    policy = context.get("sandbox_policy") if isinstance(context, dict) else None
    kind = policy.get("type") if isinstance(policy, dict) else None
    return kind if isinstance(kind, str) else ""


def _digits(raw: str) -> Optional[int]:
    try:
        return int(raw.replace(",", "").replace("_", ""))
    except ValueError:
        return None


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _redacted(text: str) -> str:
    from .base import redact

    return redact(text)


def build_provider(executable: Optional[str] = None) -> CodexProvider:
    return CodexProvider(executable)
