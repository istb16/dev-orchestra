"""Offline provider used by the test suite and by ``--dry-run``.

Never touches the network or a real CLI. Responses come from, in order:

1. a file named ``<role-or-mode>.txt`` inside ``$DEV_ORCHESTRA_MOCK_DIR``
2. ``$DEV_ORCHESTRA_MOCK_RESPONSE``
3. a built-in stub that echoes what was requested
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Sequence

from .base import ModelCandidate, ModelResolutionError, Provider, ResolvedModel, RunResult, Usage

#: Asking for this family raises, so tests can exercise the resolution-failure
#: path on a machine with no provider CLI installed at all.
UNRESOLVABLE_FAMILY = "unresolvable"


class MockProvider(Provider):
    name = "mock"
    display_name = "Mock (offline)"
    executable = "mock"

    fallback_models = (
        ModelCandidate("mock-small", "small", "mock-small", "builtin-fallback"),
        ModelCandidate("mock-large", "large", "mock-large", "builtin-fallback"),
    )
    fallback_updated = "2026-09-10"

    #: Set by tests to force a failure without touching the environment.
    fail = False

    def which(self) -> Optional[str]:
        return "mock"

    def version(self) -> "tuple[Optional[str], Optional[str]]":
        return "mock 0", None

    def auth_status(self) -> "tuple[str, str]":
        return "present", "no credentials required"

    def _discover_models(self) -> List[ModelCandidate]:
        return list(self.fallback_models)

    def _resolve_latest(self, family: str) -> ResolvedModel:
        if family == UNRESOLVABLE_FAMILY:
            raise ModelResolutionError("mock: %r cannot be resolved (by design)" % family)
        value = family or "mock-small"
        return ResolvedModel(self.name, value, "latest", value, value, "builtin-fallback")

    def build_command(
        self,
        mode: str,
        resolved: ResolvedModel,
        cwd: str,
        extra_args: Sequence[str] = (),
        options: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        return ["mock", mode, resolved.argument or "default", *self.option_args(options), *extra_args]

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
        idle_timeout: Optional[float] = None,
    ) -> RunResult:
        started = time.time()
        resolved = self.resolve_model(model_spec)
        command = self.build_command(mode, resolved, cwd, extra_args, options)
        if self.fail or _should_fail(prompt):
            return RunResult(False, 1, "", "mock failure", command, time.time() - started, resolved)
        response = _canned_response(mode)
        return RunResult(
            True,
            0,
            response,
            "",
            command,
            time.time() - started,
            resolved,
            usage=_mock_usage(prompt, response),
        )


def _mock_usage(prompt: str, response: str) -> Usage:
    """Deterministic counts derived from the text, so totals are assertable.

    Four characters per token is only a rule of thumb, which is exactly why no
    real adapter estimates: here the number just has to be reproducible.
    """
    return Usage(
        input_tokens=max(len(prompt) // 4, 1),
        output_tokens=max(len(response) // 4, 1),
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.0,
        source="mock",
        prompt_chars=len(prompt),
    )


def _should_fail(prompt: str) -> bool:
    """``DEV_ORCHESTRA_MOCK_FAIL=1`` fails everything; any other value fails
    only runs whose prompt contains it (e.g. one reviewer id)."""
    marker = os.environ.get("DEV_ORCHESTRA_MOCK_FAIL")
    if not marker:
        return False
    if marker in ("1", "true", "all"):
        return True
    return marker in prompt


def _canned_response(mode: str) -> str:
    directory = os.environ.get("DEV_ORCHESTRA_MOCK_DIR")
    if directory:
        candidate = os.path.join(directory, "%s.txt" % mode)
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
    inline = os.environ.get("DEV_ORCHESTRA_MOCK_RESPONSE")
    if inline:
        return inline
    if mode == "review":
        return "NO_FINDINGS\n"
    return "[mock %s response]\n" % mode


def build_provider(executable: Optional[str] = None) -> MockProvider:
    return MockProvider(executable)
