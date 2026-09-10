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
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence

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
_CHOICES_RE = re.compile(r'"([A-Za-z]+)"')

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

    def _discover_permission_modes(self) -> List[str]:
        completed = self._capture([self.executable, "--help"], timeout=45)
        if completed is None or completed.returncode != 0:
            return list(FALLBACK_PERMISSION_MODES)
        block = _help_block(completed.stdout or "", "--permission-mode <mode>")
        modes = _CHOICES_RE.findall(block)
        return modes or list(FALLBACK_PERMISSION_MODES)

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
        completed = self._capture([self.executable, "--help"], timeout=45)
        if completed is None or completed.returncode != 0:
            return list(self.fallback_models)
        aliases = _parse_model_aliases(completed.stdout or "")
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
        if resolved.argument:
            command += ["--model", resolved.argument]
        requested = options.get("permission_mode")
        command += _permission_args(mode, requested if isinstance(requested, str) else None)
        command += self.option_args(options)
        # Anything passed at the call site wins by position: for repeated flags
        # the CLI takes the last occurrence.
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
        """The token counts the CLI puts on its own ``result`` event."""
        return parse_stream_usage(outcome.stdout)


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

    # No usable result event: fall back to the assistant's own text blocks.
    collected: List[str] = []
    for event in events:
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
    if collected:
        return "\n".join(collected), "no result event; reconstructed from assistant messages"
    return None, ""


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
    """
    for event in reversed(_stream_events(stdout)):
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
        return ["--permission-mode", "plan", "--disallowed-tools", _READ_ONLY_DENY]
    if mode == MODE_IMPLEMENT:
        return ["--permission-mode", requested or "acceptEdits"]
    return []


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
