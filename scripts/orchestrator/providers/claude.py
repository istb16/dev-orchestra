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
from typing import Any, Dict, List, Optional, Sequence

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
    option_keys = ("args", "permission_mode")

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
        command = [self.executable, "-p", "--output-format", "text"]
        if resolved.argument:
            command += ["--model", resolved.argument]
        requested = (options or {}).get("permission_mode")
        command += _permission_args(mode, requested if isinstance(requested, str) else None)
        command += self.option_args(options)
        # Anything passed at the call site wins by position: for repeated flags
        # the CLI takes the last occurrence.
        command += list(extra_args)
        return command


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
