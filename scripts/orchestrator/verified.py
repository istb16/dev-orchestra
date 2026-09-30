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

A second record per provider (``<provider>-smoke.json``) says which versions
went through that script at all, and how they fared: a CLI update is when its
output or flags can drift, and ``doctor`` notes a version never checked here.
It holds check names only, never a check's detail.
"""

from __future__ import annotations

import os
import re
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

#: How a problem names each record: ``doctor`` reports both, on separate lines.
RECORD = "verification record"
SMOKE_RECORD = "live-check record"


def _inside(label: str) -> str:
    return "the %s is inside the workspace; point DEV_ORCHESTRA_HOME outside the checkout" % label


INSIDE_WORKSPACE = _inside(RECORD)
SMOKE_INSIDE_WORKSPACE = _inside(SMOKE_RECORD)


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
    return _load(record_path(provider), root)


def _load(path: str, root: str, label: str = RECORD) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not outside(path, root):
        return None, _inside(label)
    if not os.path.isfile(path):
        return None, None
    data = ws.read_json(path)
    if not isinstance(data, dict):
        return None, "the %s is not a JSON object" % label
    if data.get("schema") != SCHEMA:
        return None, "the %s has an unknown schema" % label
    return data, None


def lookup(
    provider: str,
    version: str,
    mechanism: str,
    root: str,
    required: Sequence[str] = REQUIRED_RESUME_CHECKS,
) -> Dict[str, Any]:
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
    if isinstance(entry, dict) and entry_is_complete(entry, mechanism, required):
        return {"status": "passed", "entry": entry, "problem": None}
    return {"status": "absent", "entry": None, "problem": None}


def entry_is_complete(
    entry: Dict[str, Any], mechanism: str, required: Sequence[str] = REQUIRED_RESUME_CHECKS
) -> bool:
    """Whether a recorded or built-in entry vouches for the current flags."""
    checks = entry.get("checks")
    if entry.get("read_only_mechanism") != mechanism or not isinstance(checks, list):
        return False
    return all(name in checks for name in required)


#: The first standalone dotted run of integers in a ``--version`` line:
#: ``2.1.285 (Claude Code)``, ``codex-cli 0.156.1``, ``1.2.14``.
_VERSION_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)+")


def parse_version(text: Any) -> Optional[Tuple[int, ...]]:
    """The version numbers in ``text``, or None when there is no clean run.

    Only the first run is read: a suffix such as ``-beta.1`` on it makes the
    whole string unparsable, so a pre-release cannot be read off a later
    number in the same line (``2.1.286-beta.1 (build 2026.09.30)``).
    """
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    after = text[match.end() : match.end() + 1]
    if after and (after.isalnum() or after in "._-"):
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _compare(left: Tuple[int, ...], right: Tuple[int, ...]) -> int:
    """-1, 0 or 1, the shorter version padded with zeros."""
    width = max(len(left), len(right))
    a = left + (0,) * (width - len(left))
    b = right + (0,) * (width - len(right))
    return (a > b) - (a < b)


def resume_trust(
    provider: str,
    version: str,
    mechanism: str,
    root: str,
    built_in: Dict[str, Dict[str, Any]],
    required: Sequence[str] = REQUIRED_RESUME_CHECKS,
) -> Dict[str, Any]:
    """How far ``version`` is trusted to keep a resumed session read-only.

    ``status`` is ``failed``, ``passed`` (this exact version), ``newer``
    (newer than ``newer_than``, which passed, and of the same major version)
    or ``absent``. A failure recorded on this machine is overruled only by a
    pass recorded here above it, never by the built-in table, and blocks
    every version up to the next such pass (``blocked_by``); a version that
    cannot be ordered is blocked by any (``unordered``). Nothing newer is
    trusted without this machine's failures to hand: with ``problem`` set --
    a failure the record holds but not as an object included -- only an
    exact built-in entry counts.
    """
    data, problem = read(provider, root)
    recorded_failures = (data or {}).get("failed", {})
    if data is not None and not (
        isinstance(recorded_failures, dict) and all(isinstance(e, dict) for e in recorded_failures.values())
    ):
        data, problem = None, "the %s has a malformed failure entry" % RECORD
    trust: Dict[str, Any] = {
        "status": "absent",
        "entry": None,
        "source": None,
        "newer_than": None,
        "blocked_by": None,
        "unordered": False,
        "problem": problem,
    }
    recorded_passes = (data or {}).get("versions")
    failed: Dict[str, Any] = dict(recorded_failures) if data is not None else {}
    passes: Dict[str, Any] = recorded_passes if isinstance(recorded_passes, dict) else {}

    def complete(entry: Any) -> bool:
        return isinstance(entry, dict) and entry_is_complete(entry, mechanism, required)

    if version in failed:
        return dict(trust, status="failed", entry=failed[version])
    if complete(passes.get(version)):
        return dict(trust, status="passed", entry=passes[version], source="record")
    exact = built_in.get(version)
    if problem:
        if complete(exact):
            return dict(trust, status="passed", entry=exact, source="built-in")
        return trust

    current = parse_version(version)
    if failed:
        if current is None:
            # Not ordered, so no failure can be said to lie below it.
            return dict(trust, status="failed", unordered=True)
        last_pass: Optional[Tuple[int, ...]] = None
        for name, entry in passes.items():
            parsed = parse_version(name)
            if parsed is None or not complete(entry) or _compare(parsed, current) >= 0:
                continue
            if last_pass is None or _compare(parsed, last_pass) > 0:
                last_pass = parsed
        for name in sorted(failed):
            parsed = parse_version(name)
            if parsed is None or (
                _compare(parsed, current) <= 0 and (last_pass is None or _compare(parsed, last_pass) > 0)
            ):
                return dict(trust, status="failed", entry=failed[name], blocked_by=name)

    if complete(exact):
        return dict(trust, status="passed", entry=exact, source="built-in")
    if current is None:
        return trust

    best: Optional[Tuple[Tuple[int, ...], str, Dict[str, Any], str]] = None
    # The record first, so that it wins a tie with the table. A pass vouches
    # for its own major version only: a major release is where the flags a
    # pass measured are most likely to change.
    for source, table in (("record", passes), ("built-in", built_in)):
        for name, entry in table.items():
            parsed = parse_version(name)
            if parsed is None or name in failed or not complete(entry) or _compare(parsed, current) >= 0:
                continue
            if parsed[0] != current[0]:
                continue
            if best is None or _compare(parsed, best[0]) > 0:
                best = (parsed, name, entry, source)
    if best is None:
        return trust
    return dict(trust, status="newer", entry=best[2], source=best[3], newer_than=best[1])


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


def smoke_record_path(provider: str) -> str:
    """Where ``provider``'s live-check record lives. Nothing else builds this path."""
    return os.path.join(config.global_config_dir(), "verified", "%s-smoke.json" % provider)


def record_smoke(
    provider: str,
    version: str,
    failed: Sequence[str],
    skipped: Sequence[str],
    root: str,
    observed: Optional[Dict[str, Any]] = None,
) -> str:
    """Record how ``version`` fared in a live check; returns the record's path.

    ``observed`` is what a check saw rather than whether it passed -- whether
    a review-mode run wrote, for one -- kept as a baseline for the next run.
    """
    path = smoke_record_path(provider)
    if not outside(path, root):
        raise VerifiedRecordError(SMOKE_INSIDE_WORKSPACE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with ws.file_lock(path):
        data = ws.read_json(path)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            data = {}
        versions = data.get("versions")
        data = {
            "schema": SCHEMA,
            "provider": provider,
            "versions": versions if isinstance(versions, dict) else {},
        }
        data["versions"][version] = {
            "checked_at": ws.utcnow(),
            "ok": not failed,
            "failed": list(failed),
            "skipped": list(skipped),
            "dev_orchestra": __version__,
        }
        if observed:
            data["versions"][version]["observed"] = dict(observed)
        ws.write_json(path, data)
    return path


def smoke_status(provider: str, version: str, root: str) -> Dict[str, Any]:
    """``status`` is ``passed``, ``failed`` or ``absent`` for this version.

    ``passed`` allows skipped checks: nothing was shown to fail. ``last_passed``
    is the most recent passing entry of any version, or None.
    """
    data, problem = _load(smoke_record_path(provider), root, SMOKE_RECORD)
    versions = (data or {}).get("versions")
    versions = versions if isinstance(versions, dict) else {}
    last_passed: Optional[Dict[str, Any]] = None
    for name, entry in versions.items():
        if not isinstance(entry, dict) or entry.get("ok") is not True:
            continue
        checked_at = entry.get("checked_at")
        if isinstance(checked_at, str) and (last_passed is None or checked_at > last_passed["checked_at"]):
            last_passed = {"version": name, "checked_at": checked_at}
    entry = versions.get(version)
    if not isinstance(entry, dict):
        status, entry = "absent", None
    else:
        status = "passed" if entry.get("ok") is True else "failed"
    return {"status": status, "entry": entry, "last_passed": last_passed, "problem": problem}
