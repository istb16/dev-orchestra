"""Provider adapter interface.

A provider wraps one coding CLI. Adapters must never invent model identifiers:
``resolve_model`` either returns something the installed CLI told us about, or
returns a resolution that lets the CLI pick its own default.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence

from .. import execution

# Execution modes shared by every adapter.
MODE_PLAN = "plan"  # investigate / design, must not modify files
MODE_IMPLEMENT = "implement"  # allowed to modify the working tree
MODE_REVIEW = "review"  # read-only, produces a review report

MODES = (MODE_PLAN, MODE_IMPLEMENT, MODE_REVIEW)
READ_ONLY_MODES = (MODE_PLAN, MODE_REVIEW)


_SECRET_PATTERNS = (
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{12,})"),
    re.compile(r"\b(sk-ant-[A-Za-z0-9_\-]{12,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,})"),
    re.compile(r"\b(ey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"),
    re.compile(r"(?i)\bbearer\s+([A-Za-z0-9_\-\.]{12,})"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|secret)"
        r"\s*[:=]\s*[\"']?([A-Za-z0-9_\-\.]{12,})"
    ),
)


#: Discovery results are stable for the lifetime of a process, and both the
#: doctor and the wizard ask for them repeatedly. Keyed by (adapter, executable).
_DISCOVERY_CACHE: Dict[tuple, Any] = {}


def clear_discovery_cache() -> None:
    """Forget cached CLI discovery (used by tests)."""
    _DISCOVERY_CACHE.clear()


def redact(text: str) -> str:
    """Strip credential-shaped substrings from anything we log or persist."""
    if not text:
        return text
    cleaned = text
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub(lambda m: m.group(0).replace(m.group(1), "[redacted]"), cleaned)
    return cleaned


class Detection:
    """Result of looking for a CLI on this machine."""

    def __init__(
        self,
        installed: bool,
        path: Optional[str] = None,
        version: Optional[str] = None,
        error: Optional[str] = None,
        authenticated: str = "unknown",
        auth_detail: str = "",
    ) -> None:
        self.installed = installed
        self.path = path
        self.version = version
        self.error = error
        # "present" | "missing" | "unknown" -- a state, never the credential.
        self.authenticated = authenticated
        self.auth_detail = auth_detail

    def to_dict(self) -> Dict[str, Any]:
        return {
            "installed": self.installed,
            "path": self.path,
            "version": self.version,
            "error": self.error,
            "authentication": self.authenticated,
            "authentication_detail": self.auth_detail,
        }


class ModelCandidate:
    """A model the CLI actually told us about (or a documented fallback)."""

    def __init__(self, value: str, family: str = "", label: str = "", source: str = "cli") -> None:
        self.value = value  # what gets passed to the CLI ("" = let the CLI decide)
        self.family = family or value
        self.label = label or value or "CLI default"
        # cli | cli-config | cli-help | builtin-fallback
        self.source = source

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "family": self.family, "label": self.label, "source": self.source}


class ResolvedModel:
    """How a configured (family, version policy) maps onto a CLI invocation."""

    def __init__(
        self,
        provider: str,
        family: str,
        version_policy: str,
        argument: Optional[str],
        display: str,
        source: str,
        note: str = "",
    ) -> None:
        self.provider = provider
        self.family = family
        self.version_policy = version_policy
        # None => omit the model flag entirely and let the CLI choose.
        self.argument = argument
        self.display = display
        self.source = source
        self.note = note

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "family": self.family,
            "version_policy": self.version_policy,
            "argument": self.argument,
            "display": self.display,
            "source": self.source,
            "note": self.note,
        }


class ModelResolutionError(RuntimeError):
    """Raised when a configured model cannot be resolved without guessing."""


class Usage:
    """What one delegated run cost, as the CLI itself reported it.

    Missing numbers stay missing. Estimating them from the prompt we sent would
    ignore the child CLI's own system prompt, tool schemas and the files it
    chose to read -- which is most of the input -- so the estimate would be
    wrong by more than it is right, and it would be wrong in the direction that
    makes a run look cheap. A number that cannot be trusted is worse than no
    number when the point of measuring is to decide what to cut.

    ``prompt_chars`` is the one figure this repository knows first-hand: the
    size of the prompt it composed. That is also the only part of the input it
    can shorten, so it is worth tracking separately from the total.
    """

    def __init__(
        self,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        cache_read_tokens: Optional[int] = None,
        cache_write_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
        source: str = "",
        prompt_chars: Optional[int] = None,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        #: Set only by a CLI that reports one figure and does not split it.
        #: Never derived by adding the two above: a CLI that reports both is
        #: also the CLI whose definition of "total" we would be guessing at.
        self.total_tokens = total_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_write_tokens = cache_write_tokens
        self.cost_usd = cost_usd
        #: Where the numbers came from: the CLI's own name for its report, or
        #: ``unreported`` when it told us nothing.
        self.source = source or "unreported"
        self.prompt_chars = prompt_chars

    @property
    def measured(self) -> bool:
        """True when the CLI reported at least one token count."""
        return any(value is not None for value in (self.input_tokens, self.output_tokens, self.total_tokens))

    @property
    def billed_tokens(self) -> Optional[int]:
        """One comparable figure per run, for ranking stages by size.

        Cache reads are deliberately left out: they are an order of magnitude
        cheaper, and folding them in would rank a well-cached stage above an
        expensive one. Use ``cost_usd`` when the question is money.
        """
        parts = [self.input_tokens, self.output_tokens, self.cache_write_tokens]
        known = [value for value in parts if value is not None]
        if known:
            return sum(known)
        return self.total_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
            "billed_tokens": self.billed_tokens,
            "prompt_chars": self.prompt_chars,
            "source": self.source,
            "measured": self.measured,
        }


class RunResult:
    def __init__(
        self,
        ok: bool,
        exit_code: int,
        stdout: str,
        stderr: str,
        command: Sequence[str],
        duration: float,
        resolved: Optional[ResolvedModel] = None,
        timed_out: bool = False,
        stalled: bool = False,
        idle_for: float = 0.0,
        orphans_possible: bool = False,
        usage: Optional[Usage] = None,
    ) -> None:
        self.ok = ok
        self.exit_code = exit_code
        self.stdout = redact(stdout)
        self.stderr = redact(stderr)
        self.command = list(command)
        self.duration = duration
        self.resolved = resolved
        #: The total deadline was reached.
        self.timed_out = timed_out
        #: No output for the idle deadline -- the agent looks wedged, which is
        #: worth saying differently from "it took too long".
        self.stalled = stalled
        self.idle_for = idle_for
        self.orphans_possible = orphans_possible
        #: What the run cost. Never None, so callers need not guard; the
        #: numbers inside it are None when the CLI reported nothing.
        self.usage = usage or Usage()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stalled": self.stalled,
            "idle_for_seconds": round(self.idle_for, 2),
            "orphans_possible": self.orphans_possible,
            "duration_seconds": round(self.duration, 2),
            "command": self.command,
            "model": self.resolved.to_dict() if self.resolved else None,
            "usage": self.usage.to_dict(),
        }


class Provider:
    """Base adapter. Subclasses override the CLI-specific parts."""

    name = "base"
    display_name = "Base"
    executable = ""

    #: Documented fallbacks used only when the CLI tells us nothing. Keep these
    #: as families/aliases -- never dated snapshot ids.
    fallback_models: Sequence[ModelCandidate] = ()
    fallback_updated = ""

    #: True only when a healthy run of the command this adapter builds emits
    #: output *while working*. Measured, not assumed: it decides whether an
    #: idle-output deadline can distinguish a wedged agent from a busy one.
    streams_progress = False

    def __init__(self, executable: Optional[str] = None) -> None:
        if executable:
            self.executable = executable

    # -- discovery ---------------------------------------------------------

    def which(self) -> Optional[str]:
        return shutil.which(self.executable)

    def _cached(self, kind: str, compute):
        key = (type(self).__name__, self.executable, kind)
        if key not in _DISCOVERY_CACHE:
            _DISCOVERY_CACHE[key] = compute()
        return _DISCOVERY_CACHE[key]

    def detect(self) -> Detection:
        return self._cached("detect", self._detect)

    def _detect(self) -> Detection:
        path = self.which()
        if not path:
            return Detection(False, error="%s not found on PATH" % self.executable)
        version, error = self.version()
        detection = Detection(True, path=path, version=version, error=error)
        detection.authenticated, detection.auth_detail = self.auth_status()
        return detection

    def version(self) -> "tuple[Optional[str], Optional[str]]":
        return self._cached("version", self._version)

    def _version(self) -> "tuple[Optional[str], Optional[str]]":
        completed = self._capture([self.executable, "--version"], timeout=30)
        if completed is None:
            return None, "could not execute %s --version" % self.executable
        if completed.returncode != 0:
            return None, (completed.stderr or completed.stdout or "").strip()[:200] or "non-zero exit"
        return (completed.stdout or completed.stderr or "").strip().splitlines()[0], None

    def auth_status(self) -> "tuple[str, str]":
        """Report whether credentials appear to exist -- never their values.

        This is a *presence* check only: it does not prove the session is still
        valid, so a run can still fail with an auth error afterwards.
        """
        return "unknown", ""

    def list_models(self) -> List[ModelCandidate]:
        """Model candidates discovered from the installed CLI, best effort."""
        return list(self._cached("models", self._discover_models))

    def _discover_models(self) -> List[ModelCandidate]:
        return list(self.fallback_models)

    def resolve_model(self, spec: Optional[Dict[str, Any]]) -> ResolvedModel:
        spec = spec or {}
        family = str(spec.get("family") or "").strip()
        policy = str(spec.get("version") or "latest")
        pinned = spec.get("id")
        if policy == "pinned":
            if not pinned:
                raise ModelResolutionError(
                    "%s: version policy is 'pinned' but no model id is set" % self.name
                )
            return ResolvedModel(
                self.name, family or str(pinned), policy, str(pinned), str(pinned), "config-pinned"
            )
        return self._resolve_latest(family)

    def _resolve_latest(self, family: str) -> ResolvedModel:
        raise NotImplementedError

    # -- execution ---------------------------------------------------------

    def build_command(
        self,
        mode: str,
        resolved: ResolvedModel,
        cwd: str,
        extra_args: Sequence[str] = (),
        options: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        raise NotImplementedError

    # -- role options ------------------------------------------------------

    #: Provider-specific keys a role may set under ``options``. Subclasses add
    #: their own; ``args`` is understood by every adapter.
    option_keys: Sequence[str] = ("args",)

    def validate_options(self, options: Optional[Dict[str, Any]]) -> List[str]:
        """Return human-readable problems with a role's ``options`` mapping."""
        if options is None:
            return []
        if not isinstance(options, dict):
            return ["options must be a mapping"]
        problems: List[str] = []
        for key, value in options.items():
            if key not in self.option_keys:
                problems.append(
                    "options.%s is not understood by the %s provider (known: %s)"
                    % (key, self.name, ", ".join(self.option_keys))
                )
            elif key == "args" and not (
                isinstance(value, list) and all(isinstance(item, str) for item in value)
            ):
                problems.append("options.args must be a list of strings")
        return problems

    @staticmethod
    def option_args(options: Optional[Dict[str, Any]]) -> List[str]:
        """The raw extra CLI arguments a role configured, if any."""
        if not isinstance(options, dict):
            return []
        args = options.get("args")
        return [str(item) for item in args] if isinstance(args, list) else []

    def run(
        self,
        prompt: str,
        mode: str,
        cwd: str,
        model_spec: Optional[Dict[str, Any]] = None,
        timeout: int = 1800,
        extra_args: Sequence[str] = (),
        env: Optional[Dict[str, str]] = None,
        options: Optional[Dict[str, Any]] = None,
        idle_timeout: Optional[float] = None,
    ) -> RunResult:
        if mode not in MODES:
            raise ValueError("unknown mode %r" % mode)
        detection = self.detect()
        if not detection.installed:
            return RunResult(False, 127, "", detection.error or "CLI not installed", [self.executable], 0.0)

        resolved = self.resolve_model(model_spec)
        command = self.build_command(mode, resolved, cwd, extra_args, options)
        outcome = execution.execute(
            command,
            cwd=cwd,
            prompt=prompt,
            timeout=timeout,
            idle_timeout=self.idle_timeout(options, idle_timeout),
            env=self._child_env(env),
        )
        stdout, stderr = self.postprocess(outcome, mode)
        # Read the cost from the raw output, before postprocess narrows it to
        # the final answer: the accounting the CLI prints is not part of it.
        usage = self.parse_usage(outcome, mode) or Usage()
        usage.prompt_chars = len(prompt)
        return RunResult(
            outcome.ok,
            outcome.exit_code,
            stdout,
            stderr,
            command,
            outcome.duration,
            resolved,
            timed_out=outcome.timed_out,
            stalled=outcome.stalled,
            idle_for=outcome.idle_for,
            orphans_possible=outcome.orphans_possible,
            usage=usage,
        )

    def idle_timeout(
        self, options: Optional[Dict[str, Any]] = None, requested: Optional[float] = None
    ) -> Optional[float]:
        """The no-output deadline, or None where this CLI cannot support one.

        An adapter must only return a number when a healthy run of the command
        it builds actually emits progress. Claiming otherwise turns a slow but
        working agent into a killed one.
        """
        if not self.streams_progress:
            return None
        if isinstance(options, dict) and options.get("idle_timeout") is not None:
            return float(options["idle_timeout"])
        return requested

    def postprocess(self, outcome: "execution.ExecOutcome", mode: str) -> "tuple[str, str]":
        """Turn raw child output into (stdout, stderr) for the caller."""
        return outcome.stdout, outcome.stderr

    def parse_usage(self, outcome: "execution.ExecOutcome", mode: str) -> Optional[Usage]:
        """What the run cost, if this CLI says so. None means it does not.

        Adapters must only return numbers the CLI actually printed. Inventing
        one here would silently corrupt every total downstream.
        """
        return None

    def _child_env(self, overrides: Optional[Dict[str, str]]) -> Dict[str, str]:
        """Inherit the user's environment so existing CLI auth keeps working."""
        env = dict(os.environ)
        if overrides:
            env.update(overrides)
        return env

    # -- helpers -----------------------------------------------------------

    def _capture(self, command: Sequence[str], timeout: int = 30) -> Optional[subprocess.CompletedProcess]:
        try:
            return subprocess.run(
                list(command),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return None
