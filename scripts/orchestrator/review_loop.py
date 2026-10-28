"""Where the code review loop stands after a round, and what it does next.

Pure: every input is read by the caller, so ``review status``, ``status`` and
``review snapshot`` cannot disagree about the same report.
"""

from __future__ import annotations

from typing import Any, Dict, List, NamedTuple, Optional, Sequence

from .review_common import SEVERITIES, accepted_findings
from .review_consolidation import unresolved_blocking

#: What "above Low" means for ``review.whole_change_recheck``.
_ABOVE_LOW = ("critical", "high", "medium")
_SCOPES = ("fix", "whole")


class LoopDecision(NamedTuple):
    """What a round's report calls for."""

    #: The unresolved findings that call for another round.
    wanted: List[Dict[str, Any]]
    #: ``"fix"``, ``"whole"`` or ``None``: the scope of the round recommended.
    next_review: Optional[str]
    #: The next snapshot takes the whole change because the loop asked for it.
    recheck_next: bool
    budget_left: bool
    #: Fix the accepted findings, re-test and stop: nothing to re-review.
    finish: bool

    @property
    def recommended(self) -> bool:
        """Another round: something is wanted, or the whole change is, and a round is left."""
        return (bool(self.wanted) or self.recheck_next) and self.budget_left

    @property
    def exhausted(self) -> bool:
        """Out of rounds with work left; a finished review is not exhausted."""
        return not self.budget_left and not self.finish


def above_low_severities(blocking: Sequence[str]) -> List[str]:
    """Medium and up, plus whatever ``review.re_review_severities`` blocks on.

    The union keeps a project that made Low blocking blocking on Low.
    """
    wanted = set(_ABOVE_LOW) | set(blocking)
    return [severity for severity in SEVERITIES if severity in wanted]


def round_scope(data: Dict[str, Any], meta: Dict[str, Any]) -> str:
    """Whether the reported round reviewed the fix only or the whole change.

    A report written before the scope was recorded falls back to the snapshot
    it was taken from, when that is still the one on disk; otherwise it counts
    as fix-only, which errs toward one more whole-change round rather than one
    fewer.
    """
    snapshot = data.get("snapshot") or {}
    scope = snapshot.get("scope")
    if scope in _SCOPES:
        return str(scope)
    sha = snapshot.get("sha256")
    if sha and meta.get("sha256") == sha:
        if meta.get("scope") in _SCOPES:
            return str(meta["scope"])
        if "incremental_from" in meta:
            return "fix" if meta.get("incremental_from") else "whole"
    return "fix"


def decide(
    enabled: bool,
    data: Dict[str, Any],
    meta: Dict[str, Any],
    blocking_severities: Sequence[str],
    iteration: int,
    max_iterations: int,
) -> LoopDecision:
    """The loop's next step from the consolidated report ``data``.

    Off, this is the rule as it always was: another round while blocking
    findings remain and the budget lasts. On, the cut-off is anything above
    Low; a clean fix-only round is followed by one whole-change round, which
    may be the spare one past ``max_iterations``, and a clean whole-change
    round finishes the review.
    """
    if not enabled or not data:
        return LoopDecision(
            wanted=unresolved_blocking(data, blocking_severities),
            next_review=None,
            recheck_next=False,
            budget_left=iteration < max_iterations,
            finish=False,
        )
    wanted = unresolved_blocking(data, above_low_severities(blocking_severities))
    scope = round_scope(data, meta)
    if wanted:
        budget_left = iteration < max_iterations
        next_review: Optional[str] = None
        if budget_left:
            # What ``review snapshot`` will take: it narrows to the fix only
            # when the report has accepted findings.
            next_review = "fix" if accepted_findings(data) else "whole"
        return LoopDecision(
            wanted=wanted, next_review=next_review, recheck_next=False, budget_left=budget_left, finish=False
        )
    if scope == "whole":
        return LoopDecision(
            wanted=wanted,
            next_review=None,
            recheck_next=False,
            budget_left=iteration < max_iterations,
            finish=True,
        )
    # The one spare round: a clean fix-only round at the cap still gets its
    # whole-change review.
    budget_left = iteration <= max_iterations
    return LoopDecision(
        wanted=wanted,
        next_review="whole" if budget_left else None,
        recheck_next=budget_left,
        budget_left=budget_left,
        finish=False,
    )


def round_cap(enabled: bool, meta: Dict[str, Any], max_iterations: int) -> int:
    """The highest round ``review run`` may run on the snapshot ``meta`` describes."""
    if enabled and meta.get("recheck"):
        return max_iterations + 1
    return max_iterations
