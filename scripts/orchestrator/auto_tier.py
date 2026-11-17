"""Whether `run implementer` or `run review_fixer` goes to the role's ``light`` tier.

Pure functions: the caller reads the plan, the live consolidated report and
the run log, and this module only judges them. With no ``light`` tier, or
with ``optimization.auto_tier`` false, :func:`choose` returns None and the
run goes exactly as it did before the CLI chose tiers.

What a choice records, and how a failed light run is recognised, is
``tier_record``'s, which readers of the run log import on its own.
"""

from __future__ import annotations

import posixpath
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import optimization as opt_mod
from . import units as units_mod
from .review_common import accepted_findings
from .review_plan import files_entries, plan_tokens
from .tier_record import (
    CAUSE_DOUBT,
    CAUSE_LIGHT,
    CAUSE_RETRY,
    CAUSE_RISK,
    CAUSE_SIZE,
    LIGHT,
    RETRY_REASON,
    ROLES,
    SOURCE_AUTO,
    TierChoice,
    last_light_failure,
)

__all__ = ["AUTO_TIER_MAX_FILES", "LIGHT_SEVERITIES", "choose", "enabled"]

#: The most files an implementer run may name and still go to ``light``.
AUTO_TIER_MAX_FILES = 3
#: Finding severities a ``light`` review fixer may be handed.
LIGHT_SEVERITIES = ("medium", "low")
#: What a finding's ``file`` says when it names no file.
_NO_PATH = ("", "n/a", "na", "none", "unknown", "-", "?", "null")


def enabled(settings: Dict[str, Any]) -> bool:
    """``optimization.auto_tier``: on unless it is ``false``."""
    return settings.get("auto_tier") is not False


def _plural(count: int, word: str) -> str:
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def _own(cause: str, reason: str) -> TierChoice:
    return TierChoice(None, SOURCE_AUTO, "own model: %s" % reason, cause)


def _unit_files(plan_text: str, unit: str) -> Tuple[List[str], str]:
    """The files of ``unit`` in the plan, and why they cannot be judged, or ""."""
    try:
        found = units_mod.parse(plan_text)
    except units_mod.UnitsError as exc:
        return [], "the plan's Units cannot be read (%s)" % exc
    for entry in found:
        if entry.name == unit:
            judged = opt_mod.judge_files(entry.files)
            return judged.shaped, ("unit %s %s" % (unit, judged.doubt) if judged.doubt else "")
    return [], "the plan has no unit %s" % unit


def _implementer(
    settings: Dict[str, Any], plan_text: Optional[str], unit: Optional[str], literal_paths: Sequence[str]
) -> TierChoice:
    """``light`` for at most three files, none of them high-risk; the role's model on any doubt."""
    if not plan_text or not plan_text.strip():
        return _own(CAUSE_DOUBT, "the plan could not be read")
    if unit:
        files, doubt = _unit_files(plan_text, unit)
        checked = files
        where = "unit %s" % unit
    else:
        scan = plan_tokens(plan_text)
        found = opt_mod.plan_files(scan)
        # A list entry with no ``/`` and no extension (``Makefile``) is no
        # path ``plan_files`` counts, but it is a file all the same: counted
        # and checked. A prose bullet's first word counts too, toward the
        # role's own model.
        bare = [
            word for word in files_entries(plan_text) if "/" not in word and not posixpath.splitext(word)[1]
        ]
        files = list(dict.fromkeys(found.shaped + bare))
        doubt = found.doubt
        checked = [item.token for item in scan.tokens if item.in_files] + bare
        where = "Files to Modify"
    if doubt:
        return _own(CAUSE_DOUBT, doubt)
    if len(files) > AUTO_TIER_MAX_FILES:
        return _own(
            CAUSE_SIZE, "%s in %s (more than %d)" % (_plural(len(files), "file"), where, AUTO_TIER_MAX_FILES)
        )
    hits = opt_mod.token_matches(checked, opt_mod.risk_patterns(settings, literal_paths))
    if hits:
        return _own(CAUSE_RISK, "%s matches %s" % hits[0])
    reason = "light: %s in %s, none high-risk" % (_plural(len(files), "file"), where)
    return TierChoice(LIGHT, SOURCE_AUTO, reason, CAUSE_LIGHT)


def _fixer(settings: Dict[str, Any], consolidated: Any, literal_paths: Sequence[str]) -> TierChoice:
    """``light`` when every accepted finding is medium or low, on a file that is not high-risk."""
    findings = accepted_findings(consolidated) if isinstance(consolidated, dict) else []
    if not findings:
        return _own(CAUSE_DOUBT, "no accepted findings")
    patterns = opt_mod.risk_patterns(settings, literal_paths)
    for finding in findings:
        name = str(finding.get("id") or "?")
        severity = str(finding.get("severity") or "").strip().lower()
        if severity not in LIGHT_SEVERITIES:
            return _own(CAUSE_RISK, "%s is %s" % (name, severity or "of no severity"))
        path = str(finding.get("file") or "").strip()
        if path.lower() in _NO_PATH:
            return _own(CAUSE_DOUBT, "%s names no file" % name)
        doubt = opt_mod.judge_files([path]).doubt
        if doubt:
            return _own(CAUSE_DOUBT, "%s %s" % (name, doubt))
        hits = opt_mod.token_matches([path], patterns)
        if hits:
            return _own(CAUSE_RISK, "%s is on %s, which matches %s" % (name, hits[0][0], hits[0][1]))
    return TierChoice(
        LIGHT,
        SOURCE_AUTO,
        "light: %d accepted finding(s), all medium or lower, none on a high-risk path" % len(findings),
        CAUSE_LIGHT,
    )


def choose(
    role: str,
    settings: Dict[str, Any],
    tiers: Dict[str, Any],
    plan_text: Optional[str],
    unit: Optional[str],
    consolidated: Any,
    events: Sequence[Any],
    literal_paths: Sequence[str] = (),
) -> Optional[TierChoice]:
    """The tier the CLI chooses for ``role``; None when it chooses none.

    None for a role other than the implementer and the review fixer, for a
    role with no ``light`` tier, and with ``optimization.auto_tier`` false.
    ``settings`` is the optimization settings; ``tiers`` the role's
    ``model_tiers``; ``plan_text`` the plan as read, None when it could not
    be; ``consolidated`` the live code review report; ``events`` the run log.
    """
    if role not in ROLES or LIGHT not in tiers or not enabled(settings):
        return None
    if last_light_failure(events, role, unit) is not None:
        return TierChoice(None, SOURCE_AUTO, RETRY_REASON, CAUSE_RETRY)
    if role == "implementer":
        return _implementer(settings, plan_text, unit, literal_paths)
    return _fixer(settings, consolidated, literal_paths)
