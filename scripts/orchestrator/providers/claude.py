"""Claude Code CLI adapter.

Verified against ``claude`` 2.1.x. Everything CLI-specific lives here; the rest
of the skill only speaks the :mod:`orchestrator.providers.base` interface.

Model handling: the CLI's own ``--model`` help text advertises the aliases that
track the latest snapshot of each family (``opus``, ``sonnet``, ``fable``, ...),
so ``version: latest`` simply passes the alias through and lets the CLI resolve
it. No dated snapshot id is ever synthesised here.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional, Sequence

from .base import (
    MODE_IMPLEMENT,
    READ_ONLY_MODES,
    ModelCandidate,
    ModelResolutionError,
    Provider,
    ResolvedModel,
)

_ALIAS_RE = re.compile(r"'([a-z][a-z0-9.\-]*)'")
_READ_ONLY_DENY = "Edit,Write,NotebookEdit"


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

    def auth_status(self) -> "tuple[str, str]":
        for variable in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
            if os.environ.get(variable):
                return "present", "%s is set in the environment" % variable
        store = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
        if os.path.isfile(store):
            return "present", "CLI credential store found"
        return "unknown", "no credential file found; the CLI may use an OS keychain"

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
        self, mode: str, resolved: ResolvedModel, cwd: str, extra_args: Sequence[str] = ()
    ) -> List[str]:
        command = [self.executable, "-p", "--output-format", "text"]
        if resolved.argument:
            command += ["--model", resolved.argument]
        command += _permission_args(mode)
        # extra_args come from the role's `options.args` and win by position:
        # a later --permission-mode overrides the default chosen above.
        command += list(extra_args)
        return command


def _permission_args(mode: str) -> List[str]:
    if mode in READ_ONLY_MODES:
        return ["--permission-mode", "plan", "--disallowed-tools", _READ_ONLY_DENY]
    if mode == MODE_IMPLEMENT:
        return ["--permission-mode", "acceptEdits"]
    return []


def _parse_model_aliases(help_text: str) -> List[str]:
    """Extract the aliases advertised by ``--model`` in ``claude --help``."""
    lines = help_text.splitlines()
    block: List[str] = []
    capturing = False
    for line in lines:
        if "--model <model>" in line:
            capturing = True
            block.append(line)
            continue
        if capturing:
            # The option's description is the indented continuation block.
            if line.strip() and not re.match(r"^\s{0,4}(-{1,2}\w|[A-Z][a-z]+:)", line):
                block.append(line)
                continue
            break
    if not block:
        return []
    text = " ".join(block)
    aliases: List[str] = []
    for match in _ALIAS_RE.findall(text):
        if match.startswith("claude-"):
            continue  # full snapshot-style name shown as an example, not an alias
        if match not in aliases:
            aliases.append(match)
    return aliases


def build_provider(executable: Optional[str] = None) -> ClaudeProvider:
    return ClaudeProvider(executable)
