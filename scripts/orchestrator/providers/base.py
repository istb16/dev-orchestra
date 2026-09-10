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
import time
from typing import Any, Dict, List, Optional, Sequence

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
    ) -> None:
        self.ok = ok
        self.exit_code = exit_code
        self.stdout = redact(stdout)
        self.stderr = redact(stderr)
        self.command = list(command)
        self.duration = duration
        self.resolved = resolved
        self.timed_out = timed_out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_seconds": round(self.duration, 2),
            "command": self.command,
            "model": self.resolved.to_dict() if self.resolved else None,
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
    ) -> RunResult:
        if mode not in MODES:
            raise ValueError("unknown mode %r" % mode)
        detection = self.detect()
        if not detection.installed:
            return RunResult(False, 127, "", detection.error or "CLI not installed", [self.executable], 0.0)

        resolved = self.resolve_model(model_spec)
        command = self.build_command(mode, resolved, cwd, extra_args, options)
        started = time.time()
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=self._child_env(env),
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                False,
                124,
                "",
                "timed out after %ss" % timeout,
                command,
                time.time() - started,
                resolved,
                True,
            )
        except OSError as exc:
            return RunResult(False, 126, "", str(exc), command, time.time() - started, resolved)
        return RunResult(
            completed.returncode == 0,
            completed.returncode,
            completed.stdout or "",
            completed.stderr or "",
            command,
            time.time() - started,
            resolved,
        )

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
