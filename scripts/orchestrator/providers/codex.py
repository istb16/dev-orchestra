"""Codex CLI adapter.

Verified against ``codex`` 0.154.x (``codex exec`` for non-interactive runs).

Model handling: this adapter never enumerates OpenAI model ids from memory. The
``recommended-coding`` family resolves by *omitting* ``-m`` entirely, which
makes the CLI use whatever model it currently recommends (its ``config.toml``
default). A named family is accepted only when the *installed* CLI vouches for
it -- either it is the model the CLI is configured with, or it appears in the
catalogue ``codex debug models`` prints (0.154+). Older CLIs print nothing
there, and then a named family must be pinned by the user with
``model.version: pinned`` + ``model.id``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any, Dict, List, Optional, Sequence

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

#: Sandbox policies `codex exec -s` accepts, per its own --help.
SANDBOX_POLICIES = ("read-only", "workspace-write", "danger-full-access")

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
    option_keys = ("args", "sandbox", "approve")

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
        if mode == MODE_IMPLEMENT and options.get("approve", True):
            # Route approvals through Codex's own automatic review instead of
            # blocking on a prompt that nobody can answer in a headless run.
            command += ["--approve-for-me"]
        command += self.option_args(options)
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
        options: Optional[Dict[str, Any]] = None,
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
