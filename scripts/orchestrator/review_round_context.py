"""What a re-review is told beside the change: the findings it was meant to address."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import workspace as ws
from .review_common import accepted_findings
from .review_plan import NONE_ADDED_RE, added_in_revision


def render_round_context(workspace: ws.Workspace, meta: Dict[str, Any]) -> str:
    """What a re-review needs to know that the fix diff alone does not say.

    A reviewer is stateless and sees no other reviewer's output, so handing it
    only the fix would be handing it a change with no premise: "is this
    correct" is unanswerable without knowing what it was correcting. The
    premise is cheap to supply -- the findings were already structured during
    triage, and cost a line each against the thousands a second full diff
    costs.

    What is deliberately left out is who reported what. The findings arrive as
    the brief the fixer worked from, which is a fact about the change, rather
    than as another reviewer's opinion still in play.
    """
    if not meta.get("incremental_from"):
        return ""
    lines = ["Re-review. The diff above is the fix only, not the whole change."]
    if meta.get("full_diff"):
        lines.append("Whole change frozen at %s -- read it if you need the context." % meta["full_diff"])
    accepted = carried_findings(workspace, meta)
    if accepted:
        lines.append("")
        lines.append("The fix was meant to address:")
        for finding in accepted:
            lines.append(
                "- [%s] %s:%s -- %s"
                % (
                    finding.get("severity", "?"),
                    finding.get("file", "?"),
                    finding.get("line", "n/a"),
                    finding.get("problem", ""),
                )
            )
        lines.append("")
        lines.append(
            "For each: fixed or not. Plus any new problem the fix introduced. "
            "Do not assume a listed item was real."
        )
    return "\n".join(lines)


def carried_findings(workspace: ws.Workspace, meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The accepted findings a round still has open; empty when it has none.

    The accepted findings of the live consolidated report, on a snapshot with
    ``incremental_from`` set -- the set ``render_round_context`` hands every
    reviewer -- or on a rerun of the snapshot that report was built for,
    where nothing has changed that could have fixed them. A conditional
    reviewer whose id is in one of these findings' ``reported_by`` joins the
    round to re-check its own finding, so a rerun that would otherwise leave
    it out cannot drop the finding from the report unfixed.
    """
    consolidated = ws.read_json(workspace.consolidated_json_path, {}) or {}
    if not meta.get("incremental_from"):
        sha = str(meta.get("sha256") or "")
        if not sha or str((consolidated.get("snapshot") or {}).get("sha256") or "") != sha:
            return []
    return accepted_findings(consolidated)


def render_design_round_context(
    workspace: ws.Workspace, meta: Dict[str, Any], plan_text: Optional[str] = None
) -> str:
    """What a re-reviewed plan needs to say that the plan itself does not.

    The design version of ``render_round_context``, and the same reasoning:
    the reviewer is stateless, so a revision handed over as a fresh plan
    invites the same objections again. Who reported what is left out here too
    -- the accepted findings arrive as the brief the revision worked from, not
    as another reviewer's opinion still in play.

    Most new findings on a recorded re-review came from what the revision
    added to answer the last round, so the reviewer is pointed at the plan's
    ``## Added in this revision`` list -- or asked to look for an unlisted
    addition when the list is empty or missing. That pointer goes out on every
    recorded re-review, including one the owner asked for with no accepted
    findings behind it; only the findings block needs them. ``plan_text`` is
    the frozen plan the round reviews; it is read from the snapshot when not
    given.
    """
    if not meta.get("previous_sha"):
        return ""
    consolidated = ws.read_json(workspace.consolidated_json_path, {}) or {}
    accepted = accepted_findings(consolidated)
    lines: List[str] = []
    if accepted:
        lines.append("This plan is a revision. It was meant to address:")
        for finding in accepted:
            lines.append(
                "- [%s] %s -- %s"
                % (finding.get("severity", "?"), finding.get("file", "?"), finding.get("problem", ""))
            )
        lines += [
            "",
            "For each: addressed or not, plus any new problem the revision introduced. "
            "Do not assume a listed item was real.",
            "",
        ]
    if plan_text is None:
        plan_text = ws.read_text(workspace.snapshot_path, "")
    added = added_in_revision(plan_text)
    if added and not NONE_ADDED_RE.match(added):
        lines.append(
            "New problems on a re-review mostly come from what a revision adds. Examine each item "
            'under "Added in this revision" first -- its failure paths, what it does on older or '
            "malformed input, how it interacts with existing behaviour and with the other items -- "
            "then check that nothing else changed without being listed."
        )
    else:
        lines.append("Check whether the revision added a mechanism without saying so.")
    return "\n".join(lines)
