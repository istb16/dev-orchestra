"""How hard to try to be cheap, and when not to try at all.

Three levers, one dial. ``optimization.level`` decides whether a review round
runs at all when the tests are known to be red, how many reviewers a small
change gets, and how many findings each one is asked for.

Two things shape everything here.

The first is that this tool cannot run the project's tests. It does not know
the command -- the orchestrator discovers that from the repository and runs it
directly. So the gate reads a *recorded* result (``state record test ok``,
which ``references/workflow.md`` has always told the orchestrator to write)
rather than a result it produced itself. That leaves three states, not two:
failed, passed, and never recorded. Only the first is worth refusing on.
Refusing on the third would break every existing workflow the day it shipped,
to punish people for not having written down something that was previously
optional.

The second is that cheap is not the goal; cheap *for the same outcome* is. A
change that touches authentication, a migration, a payment path or a deploy
config gets the full treatment whatever the dial says, because the saving on
one skipped reviewer is worth a fraction of one missed authorisation bug. That
escalation is not configurable down to nothing: the patterns are, the fact
that a match escalates is not.
"""

from __future__ import annotations

import fnmatch
import posixpath
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Ordered least to most careful, so a comparison can ask "at least as careful
#: as", and so an escalation can be expressed as a maximum rather than a jump.
LEVELS = ("aggressive", "balanced", "quality")
DEFAULT_LEVEL = "balanced"

#: What each level asks a reviewer for. ``review.max_findings`` set to an
#: explicit number overrides this; left unset, the level decides.
MAX_FINDINGS_BY_LEVEL = {"aggressive": 4, "balanced": 6, "quality": 10}

#: Below both of these, and touching nothing high-risk, a change is small
#: enough that one reviewer is a reasonable trade. Deliberately low: the
#: saving is one delegated run, and the cost of being wrong is the
#: cross-model disagreement that makes this tool worth running.
DEFAULT_LOW_RISK_MAX_FILES = 2
DEFAULT_LOW_RISK_MAX_LINES = 50

#: Paths where a small diff is not a small change.
#:
#: Every entry here was chosen because the damage is invisible in the diff: a
#: deleted permission check, a migration that locks a table, a secret moved
#: into a file that ships. Line count says nothing about any of them -- the
#: authorisation bug is routinely the one-line change -- so size alone can
#: never be the test.
#:
#: Matched against the whole path and, for a pattern with no slash, against the
#: basename too. Replace the list wholesale in config to suit a codebase whose
#: names differ; that is the part that is configurable.
DEFAULT_HIGH_RISK_PATHS = (
    # Authentication and authorisation.
    "*auth*",
    "*login*",
    "*session*",
    "*permission*",
    "*policy*",
    "*acl*",
    # Secrets.
    "*secret*",
    "*credential*",
    "*password*",
    ".env",
    ".env.*",
    # Money.
    "*payment*",
    "*billing*",
    "*checkout*",
    "*invoice*",
    # Data shape and data loss.
    "*.sql",
    "*migration*/*",
    "*/migration*/*",
    "*migrate*/*",
    "*/migrate*/*",
    "*schema*",
    # Cryptography.
    "*crypto*",
    "*cipher*",
    # What runs in production.
    "Dockerfile",
    "Dockerfile.*",
    "*.tf",
    # Both forms of every directory pattern. ``fnmatch`` has no ``**``, and a
    # pattern containing a slash is matched against the whole path, so
    # ``*/k8s/*`` needs something before the directory and misses it at the
    # repository root -- which is where it usually lives.
    "k8s/*",
    "*/k8s/*",
    "deploy/*",
    "*/deploy/*",
    ".github/workflows/*",
    "*/.github/workflows/*",
)

#: What the gate does with each of the three recorded test states.
GATE_REFUSE = "refuse"
GATE_WARN = "warn"
GATE_ALLOW = "allow"


def normalise_level(value: Any) -> str:
    """A level we recognise, or the default. Never raises.

    Validation reports a bad level at ``config validate``; a run that reaches
    this point with one should be careful rather than broken, and the default
    is the careful-enough middle.
    """
    if isinstance(value, str) and value.strip().lower() in LEVELS:
        return value.strip().lower()
    return DEFAULT_LEVEL


def at_least(level: str, floor: str) -> str:
    """``level``, raised to ``floor`` if it is less careful than that."""
    return floor if LEVELS.index(level) < LEVELS.index(floor) else level


def high_risk_matches(paths: Sequence[str], patterns: Sequence[str]) -> List[Tuple[str, str]]:
    """Every ``(path, pattern)`` pair that makes this change high-risk.

    Pairs rather than a boolean because a refusal to optimise has to be able
    to say which file caused it. "Running the full panel because this change
    touches `db/migrate/003_drop_orders.rb`" is a sentence someone can check;
    "running the full panel" is one they can only obey.
    """
    hits: List[Tuple[str, str]] = []
    for raw in paths:
        path = posixpath.normpath(str(raw).replace("\\", "/"))
        while path.startswith("./"):
            path = path[2:]
        if not path:
            continue
        base = posixpath.basename(path)
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            if fnmatch.fnmatchcase(path, pattern) or (
                "/" not in pattern and fnmatch.fnmatchcase(base, pattern)
            ):
                hits.append((path, pattern))
                break
    return hits


class Plan:
    """What this level decided, and what it decided it from."""

    def __init__(
        self,
        requested: str,
        level: str,
        gate: str,
        test_status: str,
        max_findings: int,
        reviewer_limit: Optional[int],
        high_risk: Sequence[Tuple[str, str]] = (),
        files: int = 0,
        lines: int = 0,
    ) -> None:
        #: The configured level, before any escalation.
        self.requested = requested
        #: The level actually in force.
        self.level = level
        #: One of GATE_REFUSE / GATE_WARN / GATE_ALLOW.
        self.gate = gate
        #: "ok", "failed", or "" when nothing was ever recorded.
        self.test_status = test_status
        self.max_findings = max_findings
        #: None means every configured reviewer runs.
        self.reviewer_limit = reviewer_limit
        self.high_risk = list(high_risk)
        self.files = files
        self.lines = lines

    @property
    def escalated(self) -> bool:
        return self.level != self.requested

    def escalation_note(self) -> str:
        if not self.escalated:
            return ""
        first = self.high_risk[0] if self.high_risk else ("", "")
        more = len(self.high_risk) - 1
        return "%s → %s: %s matches %s%s" % (
            self.requested,
            self.level,
            first[0],
            first[1],
            " (and %d more)" % more if more > 0 else "",
        )

    def gate_note(self) -> str:
        if self.gate == GATE_REFUSE:
            return (
                "refusing to review: the last recorded test run failed. Reviewing code "
                "that does not pass its own tests spends a reviewer on a problem you "
                "already know about. Fix the tests, record the result, and run again "
                "-- or pass --force."
            )
        if self.gate == GATE_WARN:
            return (
                "note: no test result recorded for this change, so the review is "
                "running unchecked. `state record test ok` before `review run` lets "
                "this stop a review of a red tree."
            )
        return ""

    def reviewer_note(self) -> str:
        if self.reviewer_limit is None:
            return ""
        return "low-risk change (%d file(s), %d line(s)): %d reviewer instead of the full panel" % (
            self.files,
            self.lines,
            self.reviewer_limit,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_level": self.requested,
            "level": self.level,
            "escalated": self.escalated,
            "high_risk": [{"path": p, "pattern": q} for p, q in self.high_risk],
            "gate": self.gate,
            "test_status": self.test_status,
            "max_findings": self.max_findings,
            "reviewer_limit": self.reviewer_limit,
            "files": self.files,
            "lines": self.lines,
        }


def decide(
    settings: Dict[str, Any],
    review_settings: Dict[str, Any],
    paths: Sequence[str],
    lines: int,
    test_status: str,
    reviewers: int,
    reviewed_files: Optional[int] = None,
) -> Plan:
    """Work out what this round should cost, from the change and the config.

    ``paths`` is every path the change touches, and is what risk is judged
    from -- a withheld ``.env`` is still a secret, a deleted auth file is
    still an auth file. ``reviewed_files`` is how many files a reviewer will
    actually be shown, and is what the size threshold is measured against,
    for the same reason the line count already excludes withheld files:
    otherwise a one-line fix next to a lockfile bump stops counting as small
    while the diff a reviewer sees is two lines long. It defaults to
    ``len(paths)`` for a caller that has only one number.
    """
    requested = normalise_level(settings.get("level"))
    patterns = settings.get("high_risk_paths")
    if not isinstance(patterns, (list, tuple)):
        patterns = DEFAULT_HIGH_RISK_PATHS
    hits = high_risk_matches(paths, patterns)
    level = at_least(requested, "quality") if hits else requested

    status = (test_status or "").strip().lower()
    if status in ("failed", "fail", "error", "red"):
        gate = GATE_ALLOW if level == "quality" else GATE_REFUSE
    elif status:
        gate = GATE_ALLOW
    else:
        gate = GATE_ALLOW if level == "quality" else GATE_WARN

    configured_cap = review_settings.get("max_findings")
    if isinstance(configured_cap, int) and not isinstance(configured_cap, bool) and configured_cap >= 0:
        max_findings = configured_cap
    else:
        max_findings = MAX_FINDINGS_BY_LEVEL[level]

    files = len(paths) if reviewed_files is None else max(int(reviewed_files), 0)
    limit = None
    if level == "aggressive" and reviewers > 1:
        max_files = _positive(settings.get("low_risk_max_files"), DEFAULT_LOW_RISK_MAX_FILES)
        max_lines = _positive(settings.get("low_risk_max_lines"), DEFAULT_LOW_RISK_MAX_LINES)
        if files <= max_files and lines <= max_lines:
            limit = 1

    return Plan(
        requested,
        level,
        gate,
        status,
        max_findings,
        limit,
        hits,
        files=files,
        lines=lines,
    )


def _positive(value: Any, fallback: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


def choose_reviewers(reviewers: Sequence[Dict[str, Any]], limit: Optional[int]) -> List[Dict[str, Any]]:
    """Which reviewers survive a reduced panel.

    A ``general`` reviewer first, then configuration order. Taking the first
    configured one outright would leave a panel of exactly one specialist --
    a security reviewer alone reports no correctness bugs, because it was
    told not to look for them.
    """
    chosen = list(reviewers)
    if limit is None or limit >= len(chosen):
        return chosen
    ranked = sorted(
        range(len(chosen)),
        key=lambda index: (0 if str(chosen[index].get("role") or "") == "general" else 1, index),
    )
    keep = sorted(ranked[:limit])
    return [chosen[index] for index in keep]
