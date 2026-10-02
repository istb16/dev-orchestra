"""Offline provider used by the test suite and by ``--dry-run``.

Never touches the network or a real CLI. Responses come from, in order:

1. a file named ``<role-or-mode>.txt`` inside ``$DEV_ORCHESTRA_MOCK_DIR``
2. ``$DEV_ORCHESTRA_MOCK_RESPONSE``
3. a built-in stub that echoes what was requested

``$DEV_ORCHESTRA_MOCK_FAIL`` makes a run fail and ``$DEV_ORCHESTRA_MOCK_DELAY``
makes it take a measurable amount of time. ``$DEV_ORCHESTRA_MOCK_SUSPENDED``
makes it report that many seconds of sleep on top of what it measured.

Resuming, for the tests of an architect continuing its session:
``$DEV_ORCHESTRA_MOCK_SESSION`` is the session id every run reports (a new
UUID otherwise), ``$DEV_ORCHESTRA_MOCK_CONTEXT_TOKENS`` its context size, and
``$DEV_ORCHESTRA_MOCK_RESUME`` makes a resumed run fail the way the real CLI
does for a missing session (``reject``) or stall (``stall``).
``$DEV_ORCHESTRA_MOCK_TRACE`` names a file each run appends one JSON line to:
its mode, command, the session it resumed and the prompt's length.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence

from ..clocks import Stopwatch
from ..execution import EXIT_IDLE_STALL
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
    supports_resume = True

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

    def read_only_enforcement(self) -> Dict[str, Any]:
        return {
            "status": "verified",
            "mechanism": "runs nothing (offline mock)",
            "detail": "no CLI is started",
        }

    def resume_support(self, root: str) -> Dict[str, Any]:
        return {"status": "verified", "detail": "runs nothing (offline mock)"}

    def resume_args(self, session_id: str) -> List[str]:
        return ["--resume=%s" % session_id]

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
        command_kwargs: Optional[Dict[str, Any]] = None,
    ) -> RunResult:
        watch = Stopwatch()
        started = time.monotonic()
        watch.start()

        resolved = self.resolve_model(model_spec)
        command = self.command_line(
            mode, resolved, cwd, extra_args, options, resume_session, **(command_kwargs or {})
        )
        _trace(mode, command, resume_session, prompt)
        time.sleep(_mock_delay())
        # Measured the way ``execution.execute`` measures a real child, with a
        # simulated sleep added to the duration and excluded from the charge.
        elapsed = time.monotonic() - started
        simulated = _mock_suspended()
        duration, suspended = elapsed + simulated, watch.read(elapsed) + simulated
        resume = os.environ.get("DEV_ORCHESTRA_MOCK_RESUME") if resume_session is not None else None
        if resume == "reject":
            # The shape the real CLI gives a session that does not exist: no
            # output, a zero usage it still reports, and a new id it discards.
            return RunResult(
                False,
                1,
                "",
                "No conversation found with session ID: %s" % resume_session,
                command,
                duration,
                resolved,
                usage=Usage(
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=0.0,
                    source="mock",
                    prompt_chars=len(prompt),
                ),
                session_id=str(uuid.uuid4()),
                resume_rejected=True,
                suspended=suspended,
            )
        session = {"session_id": _mock_session(), "context_tokens": _mock_context(prompt)}
        if resume == "stall":
            return RunResult(
                False,
                EXIT_IDLE_STALL,
                "",
                "no output (mock stall)",
                command,
                duration,
                resolved,
                stalled=True,
                usage=_mock_usage(prompt, ""),
                suspended=suspended,
                **session,
            )
        if self.fail or _should_fail(prompt):
            return RunResult(
                False, 1, "", "mock failure", command, duration, resolved, suspended=suspended, **session
            )
        response = _canned_response(mode)
        return RunResult(
            True,
            0,
            response,
            "",
            command,
            duration,
            resolved,
            usage=_mock_usage(prompt, response),
            suspended=suspended,
            **session,
        )


def _mock_session() -> str:
    return os.environ.get("DEV_ORCHESTRA_MOCK_SESSION") or str(uuid.uuid4())


def _mock_context(prompt: str) -> Optional[int]:
    raw = os.environ.get("DEV_ORCHESTRA_MOCK_CONTEXT_TOKENS")
    if raw:
        try:
            return int(raw)
        except ValueError:
            return None
    return len(prompt) // 4


def _trace(mode: str, command: Sequence[str], resume_session: Optional[str], prompt: str) -> None:
    path = os.environ.get("DEV_ORCHESTRA_MOCK_TRACE")
    if not path:
        return
    line = {
        "mode": mode,
        "command": list(command),
        "resume_session": resume_session,
        "prompt_chars": len(prompt),
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _mock_usage(prompt: str, response: str) -> Usage:
    """Deterministic counts derived from the text, so totals are assertable.

    Four characters per token is only a rule of thumb, which is exactly why no
    real adapter estimates: here the number just has to be reproducible.

    The tool counts are a *measured zero*, which is what this provider honestly
    observed: it started no CLI, so nothing called a tool. That is deliberately
    not the same as the ``None`` a provider that cannot say reports, and the
    difference is what the CLI tests read.
    """
    return Usage(
        input_tokens=max(len(prompt) // 4, 1),
        output_tokens=max(len(response) // 4, 1),
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.0,
        source="mock",
        prompt_chars=len(prompt),
        tool_uses=0,
        tool_uses_by_name={},
        tool_output_chars=0,
    )


def _mock_delay() -> float:
    """Seconds to stay alive for, from ``DEV_ORCHESTRA_MOCK_DELAY``.

    The only reason this exists: a test that wants to catch a detached worker
    *while it is running* -- to reset the budgets under it, or cancel it --
    needs the worker to still be there when the parent looks. A mock run is
    otherwise over before the job file has settled. An unreadable or negative
    value means no delay rather than an error, since this is a test knob and
    failing the run would say nothing useful about the run.
    """
    raw = os.environ.get("DEV_ORCHESTRA_MOCK_DELAY")
    if not raw:
        return 0.0
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return 0.0


def _mock_suspended() -> float:
    """Seconds of sleep to simulate, from ``DEV_ORCHESTRA_MOCK_SUSPENDED``.

    Added to the run's duration and reported as suspended, so the charge stays
    what was measured: the only way to show the runtime budget excluding sleep
    without sleeping a machine. Anything but a finite, non-negative number
    means no simulated sleep, as for ``_mock_delay``.
    """
    raw = os.environ.get("DEV_ORCHESTRA_MOCK_SUSPENDED")
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    if not math.isfinite(value) or value < 0:
        return 0.0
    return value


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
