"""Per-machine record of CLI versions whose resumed sessions stay read-only.

Whether a resumed session keeps the read-only restrictions is a property of
the installed CLI's version, not of a project, so the record lives in the
user's config directory and nowhere a branch under review can supply:
``.ai/`` is written per workflow, and ``.dev-orchestra.yaml`` and
``DEV_ORCHESTRA_CONFIG`` can come from the checkout. ``DEV_ORCHESTRA_HOME``
can point into the checkout too, so a record whose real path lies inside the
workspace is neither read nor written.

Only ``scripts/smoke_live.py`` writes here, after running the checks named in
:data:`REQUIRED_RESUME_CHECKS` against the real CLI. This module knows no CLI
syntax.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Sequence, Tuple

from . import __version__, config
from . import workspace as ws

SCHEMA = 1

#: Every check a version has to pass before its resumed sessions are trusted.
REQUIRED_RESUME_CHECKS = (
    "stays read-only",
    "resumes read-only",
    "forks the session",
    "reports a missing session",
    "resumes confined (absolute)",
    "ignores repository hooks",
    "ignores repository hooks on resume",
)

INSIDE_WORKSPACE = (
    "the verification record is inside the workspace; point DEV_ORCHESTRA_HOME outside the checkout"
)


class VerifiedRecordError(RuntimeError):
    """Raised instead of writing a record that would land inside the workspace."""


def record_path(provider: str) -> str:
    """Where ``provider``'s record lives. Nothing else builds this path."""
    return os.path.join(config.global_config_dir(), "verified", "%s-resume.json" % provider)


def _real(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def outside(path: str, root: str) -> bool:
    """True when ``path`` does not resolve to ``root`` or anything under it."""
    record, base = _real(path), _real(root)
    try:
        return os.path.commonpath([record, base]) != base
    except ValueError:
        # Different drives: nothing on one is inside the other.
        return True


def read(provider: str, root: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(data, problem)``. ``(None, None)`` when there is no record yet."""
    path = record_path(provider)
    if not outside(path, root):
        return None, INSIDE_WORKSPACE
    if not os.path.isfile(path):
        return None, None
    data = ws.read_json(path)
    if not isinstance(data, dict):
        return None, "the verification record is not a JSON object"
    if data.get("schema") != SCHEMA:
        return None, "the verification record has an unknown schema"
    return data, None


def lookup(provider: str, version: str, mechanism: str, root: str) -> Dict[str, Any]:
    """``status`` is ``passed``, ``failed`` or ``absent`` for this version.

    ``passed`` needs the same read-only mechanism the adapter uses now and
    every required check: changing the flags makes an old pass stale.
    """
    data, problem = read(provider, root)
    if data is None:
        return {"status": "absent", "entry": None, "problem": problem}
    failed = data.get("failed")
    if isinstance(failed, dict) and isinstance(failed.get(version), dict):
        return {"status": "failed", "entry": failed[version], "problem": None}
    versions = data.get("versions")
    entry = versions.get(version) if isinstance(versions, dict) else None
    if isinstance(entry, dict) and entry_is_complete(entry, mechanism):
        return {"status": "passed", "entry": entry, "problem": None}
    return {"status": "absent", "entry": None, "problem": None}


def entry_is_complete(entry: Dict[str, Any], mechanism: str) -> bool:
    """Whether a recorded or built-in entry vouches for the current flags."""
    checks = entry.get("checks")
    if entry.get("read_only_mechanism") != mechanism or not isinstance(checks, list):
        return False
    return all(name in checks for name in REQUIRED_RESUME_CHECKS)


def _update(provider: str, root: str, change) -> str:
    path = record_path(provider)
    if not outside(path, root):
        raise VerifiedRecordError(INSIDE_WORKSPACE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with ws.file_lock(path):
        data = ws.read_json(path)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            data = {}
        data["schema"] = SCHEMA
        data["provider"] = provider
        versions = data.get("versions")
        failed = data.get("failed")
        data["versions"] = versions if isinstance(versions, dict) else {}
        data["failed"] = failed if isinstance(failed, dict) else {}
        change(data)
        ws.write_json(path, data)
    return path


def record_pass(
    provider: str,
    version: str,
    mechanism: str,
    checks: Sequence[str],
    model: str,
    root: str,
) -> str:
    """Record that ``version`` passed; returns the record's path."""

    def change(data: Dict[str, Any]) -> None:
        data["failed"].pop(version, None)
        data["versions"][version] = {
            "verified_at": ws.utcnow(),
            "read_only_mechanism": mechanism,
            "checks": list(checks),
            "model": model,
            "dev_orchestra": __version__,
        }

    return _update(provider, root, change)


def record_fail(provider: str, version: str, failed_checks: Sequence[str], root: str) -> str:
    """Record that ``version`` failed; a local failure outranks a built-in pass."""

    def change(data: Dict[str, Any]) -> None:
        data["versions"].pop(version, None)
        data["failed"][version] = {"failed_at": ws.utcnow(), "checks": list(failed_checks)}

    return _update(provider, root, change)
