"""Claude Code CLI adapter.

Verified against ``claude`` 2.1.x. Everything CLI-specific lives here; the rest
of the skill only speaks the :mod:`orchestrator.providers.base` interface.

Model handling: the CLI's own ``--model`` help text advertises the aliases that
track the latest snapshot of each family (``opus``, ``sonnet``, ``fable``, ...),
so ``version: latest`` simply passes the alias through and lets the CLI resolve
it. No dated snapshot id is ever synthesised here.

Output format: ``stream-json``, not ``text``. Measured, ``--output-format text``
prints nothing until the run is nearly over (first output 8.1s into an 8.9s
run), so there is no way to tell a wedged agent from a busy one. The streaming
format emits ``system``/``thinking_tokens`` events throughout, which gives the
idle deadline something real to watch. The final answer is read from the
``result`` event, with fallbacks so a schema change degrades instead of losing
the output: assistant text blocks, then raw stdout. ``options.output_format:
text`` opts back out, at the cost of stall detection.

The ``thinking_tokens`` events stop once the answer starts, and the answer
arrives as one ``assistant`` event when it is finished: measured on 2.1.283, a
17k-character answer left 141s with no line at all. So the adapter also asks
for ``--include-partial-messages`` when ``--help`` lists it, which streams the
answer as ``stream_event`` chunks (largest gap 1.7s on the same prompt, stdout
about 8x larger). The readers below ignore those lines, except to keep the
text of a message the run was killed in the middle of.

Read-only runs (plan and review) are held to reading by the CLI, not by the
prompt: ``--permission-mode plan --disallowed-tools Edit,Write,NotebookEdit
--tools Read,Grep,Glob --strict-mcp-config --restricted``. Measured on 2.1.283:

- Plan mode and the three denied tools alone did not stop writes. A run
  refused ``Write`` wrote the same file with ``Bash``, and MCP tools such as
  sending a Slack message were reachable through ``ToolSearch``.
- With ``--tools Read,Grep,Glob --strict-mcp-config`` the session starts with
  those three tools and no MCP servers, and a resumed session still answers
  ``Write`` and ``Bash`` with "No such tool available".
- Command hooks in the repository's ``.claude/settings.json`` still ran with
  ``--tools``. ``--restricted`` stopped them: it ignores user, project and
  local settings files, so the branch under review cannot bring its own.
- ``--restricted`` also confines Read, Grep and Glob to the working directory
  and ``--add-dir``: all three failed on an absolute path outside it, and
  succeeded without the flag. Symlinks were not tested.

A CLI whose ``--help`` does not list all three flags, or whose ``--help``
cannot be read, gets its read-only runs refused rather than run with less.

Resuming a session (an architect revising its own plan) adds only
``--resume=<id> --fork-session`` to the read-only command. The ``=`` form,
because ``--resume`` takes an optional value and a value starting with ``-``
would otherwise be read as the next flag. Measured on 2.1.283 with the flags
above: the resumed, forked session started with the tools ``Glob``, ``Grep``
and ``Read``, no MCP servers and permission mode ``plan`` (its init event),
under a new session id; asked to write a file, it called no tool and wrote
nothing. A session that does not exist exits 1 with a single ``result``
event: no turn, zero usage, and an ``errors`` sentence naming the id that was
asked for. The redacted recordings are in ``tests/fixtures/claude/``.

Whether a resumed session keeps these restrictions is a property of the CLI
version, so it is checked per version, in two layers: :data:`VERIFIED_RESUME`
ships with the adapter, and ``scripts/smoke_live.py`` records the versions it
checked on this machine (:mod:`orchestrator.verified`). A version in neither
is not resumed.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence

from .. import verified
from ..execution import ExecOutcome
from .base import (
    MODE_IMPLEMENT,
    READ_ONLY_MODES,
    ModelCandidate,
    ModelResolutionError,
    Provider,
    ResolvedModel,
    Usage,
)

_ALIAS_RE = re.compile(r"'([a-z][a-z0-9.\-]*)'")
_READ_ONLY_DENY = "Edit,Write,NotebookEdit"
#: The only tools a read-only session has. ``--disallowed-tools`` above is
#: redundant with it and kept anyway: this pair is the combination measured.
_READ_ONLY_TOOLS = "Read,Grep,Glob"
#: What ``--help`` has to list before a read-only run is started.
_READ_ONLY_FLAGS = ("--tools", "--strict-mcp-config", "--restricted")
READ_ONLY_MECHANISM = (
    "--permission-mode plan --disallowed-tools %s --tools %s --strict-mcp-config --restricted"
    % (_READ_ONLY_DENY, _READ_ONLY_TOOLS)
)
#: What ``--help`` has to list before a session is resumed.
_RESUME_FLAGS = ("--resume", "--fork-session")
#: Only a UUID goes on the command line after ``--resume=``: the id is read
#: from the run log, and anything else could smuggle in a flag.
_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
#: How the CLI says a resumed session does not exist, before the id.
_MISSING_SESSION = "No conversation found with session ID: "
#: Streams the answer while it is written; asked for only when advertised.
_PARTIAL_MESSAGES_FLAG = "--include-partial-messages"
_CHOICES_RE = re.compile(r'"([A-Za-z]+)"')
_NO_PATH = "has no path after it"

#: Versions whose resumed sessions were measured to stay read-only, by the
#: checks smoke_live.py runs. Keyed by the first line of `claude --version`.
#: The mechanism is a literal, not READ_ONLY_MECHANISM: changing the adapter's
#: flags must make every entry stale until the check is run again.
#: An entry is added only when a release runs every check in
#: verified.REQUIRED_RESUME_CHECKS against that version; any other version
#: resumes only on a machine that ran them itself. The symlink checks were
#: skipped for 2.1.283 (the Windows machine it was verified on lacks the
#: privilege to create a symlink) and are not required.
VERIFIED_RESUME: Dict[str, Dict[str, Any]] = {
    "2.1.283 (Claude Code)": {
        "verified_at": "2026-09-27T05:58:04Z",
        "read_only_mechanism": (
            "--permission-mode plan --disallowed-tools Edit,Write,NotebookEdit "
            "--tools Read,Grep,Glob --strict-mcp-config --restricted"
        ),
        "checks": [
            "installed",
            "resolves a model",
            "answers a review prompt",
            "reports what it spent",
            "reports its tool activity",
            "stays read-only",
            "stays confined (absolute)",
            "--add-dir widens",
            "resumes read-only",
            "forks the session",
            "reports a missing session",
            "resumes confined (absolute)",
            "ignores repository hooks",
            "ignores repository hooks on resume",
        ],
        "model": "CLI default",
        "dev_orchestra": "0.11.0",
    },
}

#: Used only when ``claude --help`` cannot be read. Same rule as models: this is
#: a fallback, not a source of truth.
FALLBACK_PERMISSION_MODES = ("acceptEdits", "bypassPermissions", "plan")


class ClaudeProvider(Provider):
    name = "claude"
    display_name = "Claude Code"
    executable = "claude"

    # Families known to exist when this adapter was last reviewed. Used only if
    # the installed CLI advertises nothing; still aliases, never snapshots.
    fallback_models = (
        ModelCandidate("opus", "opus", "opus (latest Opus)", "builtin-fallback"),
        ModelCandidate("sonnet", "sonnet", "sonnet (latest Sonnet)", "builtin-fallback"),
        ModelCandidate("fable", "fable", "fable (latest Fable)", "builtin-fallback"),
        ModelCandidate("haiku", "haiku", "haiku (latest Haiku)", "builtin-fallback"),
    )
    fallback_updated = "2026-09-10"
    option_keys = ("args", "permission_mode", "output_format", "idle_timeout")
    # Measured: the streaming format emits thinking_tokens events while the
    # model works, so a no-output deadline can tell wedged from busy. The text
    # format cannot -- see the module docstring.
    streams_progress = True
    #: What the adapter asks for unless a role overrides it.
    default_output_format = "stream-json"
    supports_resume = True

    def auth_status(self) -> "tuple[str, str]":
        for variable in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
            if os.environ.get(variable):
                return "present", "%s is set in the environment" % variable
        store = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
        if os.path.isfile(store):
            return "present", "CLI credential store found"
        return "unknown", "no credential file found; the CLI may use an OS keychain"

    def permission_modes(self) -> List[str]:
        """The modes the installed CLI advertises for ``--permission-mode``."""
        return list(self._cached("permission_modes", self._discover_permission_modes))

    def help_text(self) -> Optional[str]:
        """``claude --help``, read once per process; None if it could not be.

        Permission modes, model aliases and read-only support are all read
        from it, and a read-only run may ask for all three.
        """
        return self._cached("help_text", self._read_help)

    def _read_help(self) -> Optional[str]:
        completed = self._capture([self.executable, "--help"], timeout=45)
        if completed is None or completed.returncode != 0:
            return None
        return completed.stdout or ""

    def _discover_permission_modes(self) -> List[str]:
        text = self.help_text()
        if text is None:
            return list(FALLBACK_PERMISSION_MODES)
        block = _help_block(text, "--permission-mode <mode>")
        modes = _CHOICES_RE.findall(block)
        return modes or list(FALLBACK_PERMISSION_MODES)

    # -- read-only enforcement ---------------------------------------------

    #: ``--restricted`` confines the file tools to the working directories, so
    #: widening that set is all ``--add-dir`` can do. Anything else could hand
    #: back what the adapter's own flags take away: ``--tools default``, a
    #: ``--settings`` file with hooks, ``--agents``, ``--plugin-dir``.
    read_only_args_accepted = "only --add-dir <path>"

    def refused_read_only_args(self, raw_args: Sequence[str], source: str) -> List[str]:
        raw = list(raw_args)
        problems: List[str] = []
        index = 0
        while index < len(raw):
            token = raw[index]
            if token == "--add-dir":
                following = raw[index + 1] if index + 1 < len(raw) else ""
                if following and not following.startswith("-"):
                    index += 2
                    continue
                problems.append(self._raw_argument_problem(token, index, len(raw), source, _NO_PATH))
            elif token == "--add-dir=":
                problems.append(self._raw_argument_problem(token, index, len(raw), source, _NO_PATH))
            elif not token.startswith("--add-dir="):
                problems.append(self._raw_argument_problem(token, index, len(raw), source))
            index += 1
        return problems

    def read_only_enforcement(self) -> Dict[str, Any]:
        support = self._cached("read_only_support", self._discover_read_only_support)
        status = support["status"]
        report: Dict[str, Any] = {"status": status, "mechanism": READ_ONLY_MECHANISM}
        if status == "verified":
            report["detail"] = (
                "tool allowlist %s, no MCP servers, --restricted (settings files ignored; "
                "Read/Grep/Glob confined to the working directory and --add-dir)" % _READ_ONLY_TOOLS
            )
        elif status == "unsupported":
            version = self.version()[0]
            report["missing"] = list(support["missing"])
            report["detail"] = (
                "%s does not advertise --tools / --strict-mcp-config / --restricted (missing: %s); "
                "plan and review runs are refused rather than run without enforcement. Upgrade the CLI."
                % ("claude %s" % version if version else "this claude", ", ".join(support["missing"]))
            )
        else:
            report["detail"] = (
                "could not read 'claude --help', so read-only enforcement (--tools / "
                "--strict-mcp-config / --restricted) is unverified; plan and review runs are "
                "refused rather than run unverified."
            )
        return report

    def _discover_read_only_support(self) -> Dict[str, Any]:
        text = self.help_text()
        if text is None:
            return {"status": "unverified", "missing": []}
        missing = [flag for flag in _READ_ONLY_FLAGS if not _advertises(text, flag)]
        return {"status": "unsupported" if missing else "verified", "missing": missing}

    # -- resuming a session ------------------------------------------------

    def resume_support(self, root: str) -> Dict[str, Any]:
        """Whether this version's resumed sessions are known to stay read-only.

        A failure recorded on this machine outranks the built-in table: a
        regression seen here is not overruled by a release that saw none.
        Not memoised, so a record smoke_live.py just wrote is read.
        """
        report: Dict[str, Any] = {
            "status": "unverified",
            "detail": "",
            "version": None,
            "source": None,
            "record": verified.record_path(self.name),
            "verified_at": None,
            "missing": [],
        }
        text = self.help_text()
        if text is None:
            report["detail"] = "could not read 'claude --help', so --resume / --fork-session are unverified"
            return report
        missing = [flag for flag in _RESUME_FLAGS if not _advertises(text, flag)]
        if missing:
            report["status"] = "unsupported"
            report["missing"] = missing
            report["detail"] = "%s not advertised" % ", ".join(missing)
            return report
        version = self.version()[0]
        if not version:
            report["detail"] = "could not read 'claude --version'"
            return report
        report["version"] = version
        found = verified.lookup(self.name, version, READ_ONLY_MECHANISM, root)
        if found["status"] == "failed":
            report["detail"] = (
                "claude %s failed the resume check on this machine; run python scripts/smoke_live.py "
                "--provider claude again after fixing it" % version
            )
            return report
        if found["status"] == "passed":
            return self._resume_verified(report, version, found["entry"], "record")
        entry = VERIFIED_RESUME.get(version)
        if isinstance(entry, dict) and verified.entry_is_complete(entry, READ_ONLY_MECHANISM):
            return self._resume_verified(report, version, entry, "built-in")
        detail = (
            "claude %s has not been verified to keep a resumed session read-only; "
            "run python scripts/smoke_live.py --provider claude" % version
        )
        if found["problem"]:
            detail += "; %s" % found["problem"]
        report["detail"] = detail
        return report

    @staticmethod
    def _resume_verified(
        report: Dict[str, Any], version: str, entry: Dict[str, Any], source: str
    ) -> Dict[str, Any]:
        verified_at = str(entry.get("verified_at") or "")
        report["status"] = "verified"
        report["source"] = source
        report["verified_at"] = verified_at
        report["detail"] = "resume verified for claude %s on %s (%s)" % (version, verified_at, source)
        return report

    def resume_args(self, session_id: str) -> List[str]:
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
            raise ValueError("a session to resume must be named by a UUID")
        return ["--resume=%s" % session_id, "--fork-session"]

    def resume_rejected(
        self,
        outcome: ExecOutcome,
        mode: str,
        options: Optional[Dict[str, Any]],
        session_id: str,
    ) -> bool:
        """True only for the CLI's own "no such session" result.

        Matched to what 2.1.283 printed (``tests/fixtures/claude/``): a
        non-zero exit, no turn at all, and one ``result`` event whose
        ``errors`` names the id that was asked for. Stderr is not read. If
        the wording changes, a rejection is reported as an ordinary failure
        and not retried -- never the other way round.
        """
        output_format = str((options or {}).get("output_format") or self.default_output_format)
        if output_format != "stream-json":
            return False
        if outcome.exit_code == 0 or outcome.timed_out or outcome.stalled:
            return False
        events = _stream_events(outcome.stdout)
        # A ``stream_event`` is a message being written, so a turn happened.
        if any(event.get("type") in (*_STREAM_EVENT_TYPES, "stream_event") for event in events):
            return False
        result = next((event for event in reversed(events) if event.get("type") == "result"), None)
        if result is None:
            return False
        turns = result.get("num_turns")
        if (
            result.get("is_error") is not True
            or isinstance(turns, bool)
            or turns != 0
            or result.get("subtype") != "error_during_execution"
        ):
            return False
        errors = result.get("errors")
        if not isinstance(errors, list):
            return False
        expected = _MISSING_SESSION + session_id
        return any(isinstance(error, str) and error.strip() == expected for error in errors)

    def parse_session(self, outcome: ExecOutcome) -> Dict[str, Any]:
        """The session a run ended in, the context it last had, and its init.

        The context is the last ``assistant`` event's input: the ``result``
        usage adds up every turn, so it is the run's cost, not its size.
        """
        events = _stream_events(outcome.stdout)
        session_id = None
        for kind in ("result", "system"):
            event = next((item for item in reversed(events) if item.get("type") == kind), None)
            if event is not None and isinstance(event.get("session_id"), str):
                session_id = event["session_id"]
                break
        context_tokens = None
        last = next((item for item in reversed(events) if item.get("type") == "assistant"), None)
        message = last.get("message") if last is not None else None
        usage = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict):
            parts = [
                _count(usage.get(key))
                for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            ]
            if all(part is not None for part in parts):
                context_tokens = sum(parts)
        init = None
        start = next(
            (item for item in events if item.get("type") == "system" and item.get("subtype") == "init"),
            None,
        )
        if start is not None:
            tools = start.get("tools")
            if not (isinstance(tools, list) and all(isinstance(tool, str) for tool in tools)):
                tools = None
            servers = start.get("mcp_servers")
            permission = start.get("permissionMode")
            version = start.get("claude_code_version")
            init = {
                "tools": tools,
                "mcp_servers": servers if isinstance(servers, list) else None,
                "permission_mode": permission if isinstance(permission, str) else None,
                "version": version if isinstance(version, str) else None,
            }
        return {"session_id": session_id, "context_tokens": context_tokens, "init": init}

    def validate_options(self, options: Optional[Dict[str, Any]]) -> List[str]:
        problems = super().validate_options(options)
        if not isinstance(options, dict):
            return problems
        output_format = options.get("output_format")
        if output_format is not None and output_format not in ("stream-json", "text", "json"):
            problems.append("options.output_format %r is not one of stream-json, text, json" % output_format)
        requested = options.get("permission_mode")
        if requested is None:
            return problems
        if not isinstance(requested, str):
            problems.append("options.permission_mode must be a string")
            return problems
        known = self.permission_modes()
        if known and requested not in known:
            problems.append(
                "options.permission_mode %r is not one of the modes this CLI accepts (%s)"
                % (requested, ", ".join(known))
            )
        return problems

    def _discover_models(self) -> List[ModelCandidate]:
        """Read the aliases the installed CLI advertises in its own help."""
        text = self.help_text()
        if text is None:
            return list(self.fallback_models)
        aliases = _parse_model_aliases(text)
        if not aliases:
            return list(self.fallback_models)
        return [ModelCandidate(alias, alias, alias, "cli-help") for alias in aliases]

    def _resolve_latest(self, family: str) -> ResolvedModel:
        if not family or family in ("default", "recommended", "auto"):
            return ResolvedModel(
                self.name,
                family or "default",
                "latest",
                None,
                "CLI default",
                "cli-default",
                "no --model flag passed; the CLI picks its configured default",
            )
        candidates = self.list_models()
        lowered = family.strip().lower()
        for candidate in candidates:
            if candidate.value.lower() == lowered or candidate.family.lower() == lowered:
                return ResolvedModel(
                    self.name,
                    family,
                    "latest",
                    candidate.value,
                    candidate.value,
                    candidate.source,
                    "alias resolved to the latest snapshot by the CLI",
                )
        # A full model name (e.g. "claude-opus-5") is a legitimate family too:
        # accept it only when it is clearly a Claude model id, never a guess.
        if lowered.startswith("claude-"):
            return ResolvedModel(
                self.name,
                family,
                "latest",
                family,
                family,
                "config-explicit",
                "explicit model name passed through to the CLI",
            )
        raise ModelResolutionError(
            "claude: cannot resolve model family %r. Known aliases: %s. "
            "Set model.version to 'pinned' with an explicit model.id to bypass discovery."
            % (family, ", ".join(c.value for c in candidates) or "none discovered")
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
        output_format = str(options.get("output_format") or self.default_output_format)
        command = [self.executable, "-p", "--output-format", output_format]
        if output_format == "stream-json":
            # The CLI requires --verbose with the streaming format.
            command.append("--verbose")
            # Without it a long answer is silent until it is finished, and
            # the idle deadline takes that for a stall. Never guessed.
            text = self.help_text()
            if text is not None and _advertises(text, _PARTIAL_MESSAGES_FLAG):
                command.append(_PARTIAL_MESSAGES_FLAG)
        if resolved.argument:
            command += ["--model", resolved.argument]
        requested = options.get("permission_mode")
        command += _permission_args(mode, requested if isinstance(requested, str) else None)
        # Raw arguments go last. On a read-only run they have already been
        # held to the allowlist by ``run``; nothing here depends on the order
        # to keep that run read-only.
        command += self.option_args(options)
        command += list(extra_args)
        return command

    def idle_timeout(
        self, options: Optional[Dict[str, Any]] = None, requested: Optional[float] = None
    ) -> Optional[float]:
        """No idle deadline unless the format actually streams progress."""
        output_format = str((options or {}).get("output_format") or self.default_output_format)
        if output_format != "stream-json":
            return None
        return super().idle_timeout(options, requested)

    def postprocess(self, outcome: ExecOutcome, mode: str) -> "tuple[str, str]":
        """Reduce a stream-json run to its final answer."""
        text, note = parse_stream_json(outcome.stdout)
        if text is None:
            return outcome.stdout, outcome.stderr
        stderr = outcome.stderr
        if note:
            stderr = (stderr + "\n" + note).strip()
        return text, stderr

    def parse_usage(self, outcome: ExecOutcome, mode: str) -> Optional[Usage]:
        """The token counts the CLI puts on its own ``result`` event.

        The tool counts come from the same stream, and from different events,
        so they are read apart and folded in here: a run can stream tool calls
        and end without a usable ``result`` event, and that run still used the
        tools it used.

        Decoded once, and both readers take the events. Reading the invoice and
        reading the tool calls from the same string meant decoding a review
        stream twice -- and this exists to measure the runs whose streams are
        largest, where that is the one place the cost is worth avoiding.

        There is no per-name character breakdown: nothing aggregates one, and
        a field serialised into every run's event that can never be read back
        is a cost with no reader. The characters are one total.
        """
        events = _stream_events(outcome.stdout)
        usage = _usage_from_events(events)
        tools = _tools_from_events(events)
        if tools is None:
            return usage
        if usage is None:
            usage = Usage(source="claude stream events")
        usage.tool_uses = tools["tool_uses"]
        usage.tool_uses_by_name = tools["tool_uses_by_name"]
        usage.tool_output_chars = tools["tool_output_chars"]
        return usage


def _stream_events(stdout: str) -> List[Dict[str, Any]]:
    """Every JSON object on its own line, skipping anything unparseable."""
    events: List[Dict[str, Any]] = []
    for line in stdout.splitlines():
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


def parse_stream_json(stdout: str) -> "tuple[Optional[str], str]":
    """Extract the final answer from a stream-json run.

    Returns ``(text, note)``, or ``(None, "")`` when this does not look like a
    stream at all -- in which case the caller keeps the raw output rather than
    discarding it, so an unrecognised format degrades instead of losing work.
    """
    events = _stream_events(stdout)
    if not events:
        return None, ""

    for event in reversed(events):
        if event.get("type") == "result":
            result = event.get("result")
            if isinstance(result, str) and result.strip():
                note = ""
                if event.get("is_error"):
                    note = "the CLI reported is_error on its result event"
                return result, note
            break

    # No usable result event: fall back to the assistant's own text blocks,
    # then to the text a run killed mid-message had streamed so far. The CLI
    # sends each finished block as an ``assistant`` event before the message
    # stops, so only the chunks after the last one with text are unfinished.
    collected: List[str] = []
    pending: List[str] = []
    for event in events:
        if event.get("type") == "stream_event":
            inner = event.get("event")
            if not isinstance(inner, dict):
                continue
            if inner.get("type") == "message_start":
                pending = []
            delta = inner.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                piece = delta.get("text")
                if isinstance(piece, str):
                    pending.append(piece)
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                piece = block.get("text")
                if isinstance(piece, str) and piece.strip():
                    collected.append(piece)
                    pending = []
    unfinished = "".join(pending)
    if not unfinished.strip():
        if collected:
            return "\n".join(collected), "no result event; reconstructed from assistant messages"
        return None, ""
    if not collected:
        return unfinished, "no result event; reconstructed from an unfinished message"
    note = "no result event; reconstructed from assistant messages and an unfinished message"
    return "\n".join([*collected, unfinished]), note


def parse_stream_usage(stdout: str) -> Optional[Usage]:
    """Read the cost of a stream-json run from its ``result`` event.

    The event carries four token counts, not one, and they are not
    interchangeable: cached input is billed at a fraction of fresh input, and
    writing the cache costs more than either. They are kept apart rather than
    summed into a single "input" figure, which would misstate the cost in
    whichever direction the cache happened to fall. For money, the CLI's own
    ``total_cost_usd`` is the number to trust.

    Returns None when this was not a stream, or was a stream carrying no usage
    -- absent is reported as absent, never as zero.

    Takes the raw output; :func:`_usage_from_events` is the same reader over an
    already-decoded stream, for the caller that needs both readers at once.
    """
    return _usage_from_events(_stream_events(stdout))


def _usage_from_events(events: List[Dict[str, Any]]) -> Optional[Usage]:
    """:func:`parse_stream_usage` over decoded events."""
    for event in reversed(events):
        if event.get("type") != "result":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        cost = event.get("total_cost_usd")
        measured_cost = isinstance(cost, (int, float)) and not isinstance(cost, bool)
        parsed = Usage(
            input_tokens=_count(usage.get("input_tokens")),
            output_tokens=_count(usage.get("output_tokens")),
            cache_read_tokens=_count(usage.get("cache_read_input_tokens")),
            cache_write_tokens=_count(usage.get("cache_creation_input_tokens")),
            cost_usd=float(cost) if measured_cost else None,
            source="claude result event",
        )
        return parsed if parsed.measured or parsed.cost_usd is not None else None
    return None


#: Event types only a real stream emits. ``result`` is left out on purpose:
#: ``output_format: json`` prints one of those and nothing else, and it is a
#: format that never reports a tool either way.
_STREAM_EVENT_TYPES = ("system", "assistant", "user")


def parse_stream_tools(stdout: str) -> Optional[Dict[str, Any]]:
    """Count what a stream-json run did with its tools.

    Every ``tool_use`` block is counted, whatever it is called. Counting only
    ``Read`` would undercount: a read-only run has ``Grep`` and ``Glob`` as
    well, and an implement run has every tool -- the run measured while
    designing this, from before read-only runs were narrowed to ``Read``,
    ``Grep`` and ``Glob``, read ``CONTRIBUTING.md`` with ``Bash`` and no
    ``Read`` at all. The breakdown by name is kept because the total alone
    cannot say which.

    The character count is of what the tools printed back, one total over every
    ``tool_result`` in the stream -- including one whose ``tool_use`` the stream
    never showed, which is counted rather than dropped. **It is not how much
    source the agent read**: ``wc -l`` on a two-hundred-line file returns three
    characters, ``cat`` on the same file returns all of it, and the two look
    identical from here.

    Returns None when this was not a stream at all -- absent is reported as
    absent, never as a measured zero.

    Takes the raw output; :func:`_tools_from_events` is the same reader over an
    already-decoded stream, for the caller that needs both readers at once.
    """
    return _tools_from_events(_stream_events(stdout))


def _tools_from_events(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """:func:`parse_stream_tools` over decoded events."""
    # What was actually seen, not "did anything parse". ``output_format: json``
    # is a documented, validated option, and it prints one compact ``result``
    # object -- parseable, and no stream. Reporting zeros for it would record
    # the one thing these counts must never claim: a measured zero for a format
    # that never emits a tool event. A stream-json run always emits ``system``
    # and ``assistant`` events, so a stream that genuinely used no tools still
    # reports its zero.
    #
    # Asked as "was one of these seen", not "was anything other than a result
    # seen": an object with no ``type`` at all answers the second question yes,
    # so an unrecognised format got counted as a stream that used no tools.
    if not any(event.get("type") in _STREAM_EVENT_TYPES for event in events):
        return None

    uses = 0
    uses_by_name: Dict[str, int] = {}
    chars = 0

    for event in events:
        for block in _content_blocks(event):
            kind = block.get("type")
            if kind == "tool_use":
                name = block.get("name")
                name = name if isinstance(name, str) and name else "unknown"
                uses += 1
                uses_by_name[name] = uses_by_name.get(name, 0) + 1
            elif kind == "tool_result":
                # Totalled, not attributed to the call that produced it: the
                # per-name character breakdown was bookkeeping on every block
                # of every stream that no caller ever read.
                chars += _text_length(block.get("content"))

    return {
        "tool_uses": uses,
        "tool_uses_by_name": uses_by_name,
        "tool_output_chars": chars,
    }


def _content_blocks(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The content blocks of an event, from whichever level carries them.

    ``assistant`` and ``user`` events wrap theirs in ``message``; the envelope
    is read as well so that a stream putting them one level up is not silently
    counted as no tool activity.
    """
    blocks: List[Dict[str, Any]] = []
    for holder in (event.get("message"), event):
        content = holder.get("content") if isinstance(holder, dict) else None
        if isinstance(content, list):
            blocks.extend(block for block in content if isinstance(block, dict))
            break
    return blocks


def _text_length(content: Any) -> int:
    """How many characters of text a ``tool_result`` carried.

    ``content`` is a plain string on some results and a list of blocks on
    others, so both are measured. A block with no text -- an image, say -- is
    counted as the nothing it adds to what the agent read.
    """
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if isinstance(block, str):
            total += len(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                total += len(text)
    return total


def _count(value: Any) -> Optional[int]:
    """A token count, or None. A bool is not a count; neither is a string."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _permission_args(mode: str, requested: Optional[str] = None) -> List[str]:
    if mode in READ_ONLY_MODES:
        # A configured permission_mode is deliberately ignored here: planning
        # and review stages stay read-only whatever the config says. Loosening
        # them is not a preference, it is a broken invariant.
        #
        # The order is part of it. ``--tools`` takes a variable number of
        # values, so a boolean flag has to follow it to close the list; see the
        # module docstring for what each flag was measured to stop.
        return [
            "--permission-mode",
            "plan",
            "--disallowed-tools",
            _READ_ONLY_DENY,
            "--tools",
            _READ_ONLY_TOOLS,
            "--strict-mcp-config",
            "--restricted",
        ]
    if mode == MODE_IMPLEMENT:
        return ["--permission-mode", requested or "acceptEdits"]
    return []


def _advertises(help_text: str, option: str) -> bool:
    """Whether ``claude --help`` has a line for ``option`` itself.

    Anchored to the option column, because descriptions mention other
    options: the ``--restricted`` entry says "unless --tools names them" on a
    continuation line, and a CLI without ``--tools`` still prints that.
    """
    pattern = r"^[ \t]{2,4}(?:-\w,[ \t]+)?%s(?=[ \t]|$)" % re.escape(option)
    return re.search(pattern, help_text, re.MULTILINE) is not None


def _help_block(help_text: str, option: str) -> str:
    """The description block for one option in ``claude --help``."""
    block: List[str] = []
    capturing = False
    for line in help_text.splitlines():
        if option in line:
            capturing = True
            block.append(line)
            continue
        if capturing:
            if line.strip() and not re.match(r"^\s{0,4}(-{1,2}\w|[A-Z][a-z]+:)", line):
                block.append(line)
                continue
            break
    return " ".join(block)


def _parse_model_aliases(help_text: str) -> List[str]:
    """Extract the aliases advertised by ``--model`` in ``claude --help``."""
    text = _help_block(help_text, "--model <model>")
    if not text:
        return []
    aliases: List[str] = []
    for match in _ALIAS_RE.findall(text):
        if match.startswith("claude-"):
            continue  # full snapshot-style name shown as an example, not an alias
        if match not in aliases:
            aliases.append(match)
    return aliases


def build_provider(executable: Optional[str] = None) -> ClaudeProvider:
    return ClaudeProvider(executable)
