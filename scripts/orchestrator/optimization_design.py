"""Whether the design review runs, and who sits on a design round.

The design-round half of ``optimization``. ``decide_design`` answers whether
the stage runs under ``review.design.enabled``, and ``decide_design_round``
who sits on a round once it does. The plan-reading helpers both are built on
(``plan_files``, ``design_risk``) stay in ``optimization``, because the code
round's relevance rules read the plan through them as well.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from .optimization import (
    DESIGN_LARGE_PLAN_FILES,
    PlanFiles,
    condition_reviewers,
    conditional_notes,
    design_evidence,
    design_risk,
    join_relevance,
    normalise_level,
    plan_files,
    relevance_records,
    risk_reason,
)
from .workspace import redact


class DesignDecision:
    """Whether the design review runs for this plan, and why."""

    def __init__(
        self,
        mode: str,
        run: bool,
        reason: str = "",
        files: int = 0,
        high_risk: Sequence[Tuple[str, str]] = (),
    ) -> None:
        #: "on", "off" or "auto".
        self.mode = mode
        self.run = run
        #: Empty under on and off. Redacted: it carries file names.
        self.reason = redact(reason) if reason else ""
        #: Code files counted in Files to Modify, when the size was judged.
        self.files = files
        #: ``(token, pattern)`` for every token that matched a high-risk pattern.
        self.high_risk = list(high_risk)

    def label(self) -> str:
        if self.mode != "auto":
            return self.mode
        return "auto -> %s (%s)" % ("run" if self.run else "skip", self.reason)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "run": self.run,
            "reason": self.reason or None,
            "files": self.files,
            "high_risk": [{"path": redact(p), "pattern": redact(q)} for p, q in self.high_risk],
        }


def decide_design(
    mode: str,
    settings: Dict[str, Any],
    scan: Any,
    *,
    plan_state: str,
    round_ran: bool,
    literal_paths: Sequence[str] = (),
) -> DesignDecision:
    """Whether the design review runs, from the plan the architect wrote.

    ``scan`` is ``review.plan_tokens()`` of the plan when ``plan_state`` is
    ``"ok"``; ``"missing"`` and ``"unreadable"`` come with none. Under
    ``auto`` every doubt answers run: once a round has run, so a revision
    cannot switch the loop off half way; for a plan not yet written or not
    readable; for any token matching a high-risk pattern, in any case; and
    for a Files to Modify section that is absent, names no file, names a
    glob, a directory or a path through ``..``, or names
    ``DESIGN_LARGE_PLAN_FILES`` code files or more. Only file-shaped tokens
    (a ``/`` or an extension) are judged as globs or directories, so
    ``payload["mode"]`` there is code, not a pattern. Nothing here reads the
    disk. ``literal_paths`` are high-risk too, each matched verbatim.
    """
    if mode != "auto":
        return DesignDecision(mode, mode == "on")
    if round_ran:
        return DesignDecision(mode, True, "a design round already ran")
    if plan_state == "missing":
        # Run once there is a plan to review: with no design stage there is none.
        return DesignDecision(mode, True, "once a plan is written")
    if plan_state != "ok" or scan is None:
        return DesignDecision(mode, True, "plan could not be read")

    hits = design_risk(settings, scan, literal_paths)
    if hits:
        token, section, _ = hits[0]
        where = " (%s)" % section if section else ""
        more = " and %d more" % (len(hits) - 1) if len(hits) > 1 else ""
        return DesignDecision(
            mode, True, "touches %s%s%s" % (token, where, more), high_risk=[(t, p) for t, _, p in hits]
        )

    files = plan_files(scan)
    if files.doubt:
        return DesignDecision(mode, True, files.doubt)
    count = len(files.code)
    if count >= DESIGN_LARGE_PLAN_FILES:
        return DesignDecision(mode, True, "%d code files" % count, files=count)
    noun = "code file" if count == 1 else "code files"
    return DesignDecision(mode, False, "%d %s, none high-risk" % (count, noun), files=count)


class DesignPlan:
    """Who sits on a design round, and what it was decided from.

    A design round has no gate, no panel cut and no escalation: the level is
    the configured one, recorded for the report only. What it does have is
    the plan's high-risk hits -- a ``when: high-risk`` seat joins on them and
    a seat with a ``high_risk_model`` switches to it -- and the conditional
    records of ``condition_reviewers`` and ``relevance_records``.
    """

    def __init__(
        self,
        level: str,
        high_risk: Sequence[Tuple[str, str]] = (),
        conditional: Sequence[Dict[str, Any]] = (),
        declared: bool = False,
        files: int = 0,
    ) -> None:
        self.level = level
        #: ``(token, pattern)`` for every plan token matching a high-risk pattern.
        self.high_risk = list(high_risk)
        self.conditional = [dict(record) for record in conditional]
        self.declared = declared
        #: Code files counted in Files to Modify.
        self.files = files

    @property
    def is_high_risk(self) -> bool:
        return bool(self.high_risk) or self.declared

    def risk_reason(self) -> str:
        """Why the round is high-risk, for the model-switch note; "" when it is not."""
        return risk_reason(self.high_risk, self.declared)

    def conditional_notes(self) -> List[str]:
        return conditional_notes(self.conditional)

    def left_out(self) -> List[Dict[str, Any]]:
        """The records of the seats this round leaves out."""
        return [record for record in self.conditional if not record.get("runs")]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "high_risk": [{"path": redact(p), "pattern": redact(q)} for p, q in self.high_risk],
            "declared": self.declared,
            "conditional": [dict(record) for record in self.conditional],
            "files": self.files,
        }


def decide_design_round(
    settings: Dict[str, Any],
    scan: Any,
    plan_state: str,
    panel: Sequence[Dict[str, Any]],
    *,
    declared: bool = False,
    carried: Sequence[Dict[str, Any]] = (),
    only: bool = False,
    literal_paths: Sequence[str] = (),
) -> DesignPlan:
    """Who sits on this design round, from the plan the architect wrote.

    Whether the stage runs at all is ``decide_design``'s; this is asked once
    it does. ``panel`` is the design panel in force (``LoadedConfig.
    design_reviewers``), ``scan`` the plan's ``review.plan_tokens()`` when
    ``plan_state`` is ``"ok"``. ``carried`` is the accepted findings the
    design review still has open. ``literal_paths`` is as ``decide_design``'s.
    """
    hits = design_risk(settings, scan, literal_paths) if plan_state == "ok" else []
    pairs = [(token, pattern) for token, _section, pattern in hits]
    conditional = condition_reviewers(panel, pairs, declared=declared, carried=carried, only=only)
    relevance = relevance_records(
        "design",
        panel,
        design_evidence(scan, plan_state),
        settings,
        hits=pairs,
        declared=declared,
        carried=carried,
        only=only,
        literal_paths=literal_paths,
    )
    conditional = join_relevance(panel, conditional, relevance)
    files = plan_files(scan) if plan_state == "ok" else PlanFiles([], [], "")
    return DesignPlan(
        normalise_level(settings.get("level")),
        pairs,
        conditional,
        declared=bool(declared),
        files=len(files.code),
    )
