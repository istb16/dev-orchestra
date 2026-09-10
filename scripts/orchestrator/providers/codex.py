"""Codex CLI adapter.

Verified against ``codex`` 0.154.x (``codex exec`` for non-interactive runs).

Model handling: the Codex CLI does not expose a "list models" command, so this
adapter never enumerates OpenAI model ids from memory. The ``recommended-coding``
family resolves by *omitting* ``-m`` entirely, which makes the CLI use whatever
model it currently recommends (its ``config.toml`` default). Anything else must
either match the model the CLI is configured with, or be pinned explicitly by
the user with ``model.version: pinned`` + ``model.id``.
"""

from __future__ import annotations

import os
import re
import tempfile
from typing import Dict, List, Optional, Sequence

from .base import (
    MODE_IMPLEMENT,
    READ_ONLY_MODES,
    ModelCandidate,
    ModelResolutionError,
    Provider,
    ResolvedModel,
    RunResult,
)

RECOMMENDED_FAMILIES = ("recommended-coding", "recommended", "default", "auto", "latest", "")

_TOML_MODEL_RE = re.compile(r"^\s*model\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)


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
        raise ModelResolutionError(
            "codex: the Codex CLI does not publish a model list, so %r cannot be verified. "
            "Use family 'recommended-coding' to let the CLI choose, or set "
            "model.version: pinned with an explicit model.id." % family
        )

    def build_command(
        self, mode: str, resolved: ResolvedModel, cwd: str, extra_args: Sequence[str] = ()
    ) -> List[str]:
        sandbox = "read-only" if mode in READ_ONLY_MODES else "workspace-write"
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
        if mode == MODE_IMPLEMENT:
            # Route approvals through Codex's own automatic review instead of
            # blocking on a prompt that nobody can answer in a headless run.
            command += ["--approve-for-me"]
        command += list(extra_args)
        return command

    def run(
        self,
        prompt: str,
        mode: str,
        cwd: str,
        model_spec: Optional[Dict[str, str]] = None,
        timeout: int = 1800,
        extra_args: Sequence[str] = (),
        env: Optional[Dict[str, str]] = None,
    ) -> RunResult:
        """Capture the agent's final message via ``-o`` instead of scraping logs."""
        handle, last_message_path = tempfile.mkstemp(prefix="codex-last-", suffix=".txt")
        os.close(handle)
        try:
            result = super().run(
                prompt,
                mode,
                cwd,
                model_spec=model_spec,
                timeout=timeout,
                extra_args=[*list(extra_args), "-o", last_message_path],
                env=env,
            )
            final = _read_text(last_message_path)
            if final.strip():
                result.stdout = _redacted(final)
            return result
        finally:
            try:
                os.unlink(last_message_path)
            except OSError:
                pass


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
