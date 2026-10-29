"""The user's approval of the plan, and whether the current plan still has it.

``run implementer`` refuses a plan nobody approved while
``design.require_approval`` asks for it (``approval_auto`` decides ``auto``).
SKILL.md tells the orchestrator to ask before implementing, but prose can be
skipped; this is the part that cannot.
The approval is recorded by ``design approve`` only, under its own key in
``state.json`` -- ``state record`` can write any event, so an event alone would
be an approval anyone could fake by accident.

An approval is of one plan as it was reviewed: the sha256 of the plan text and
the design review round it was given over -- the last one with a report, whose
findings the user was shown. A revised plan, or a design review started
afterwards, needs approving again.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import questions as questions_mod
from . import workspace as ws

#: The state key the approval is kept under, and the stage of its run-log event.
STAGE = "design_approval"

#: ``run implementer`` refused: the plan is not approved. Not 3: that one means
#: a budget is spent and the orchestrator is told to report and stop, where
#: this one means ask the user.
EXIT_APPROVAL_REQUIRED = 5

#: States in which ``run implementer`` is refused.
REFUSED = ("pending", "stale", "implemented-unapproved")


def plan_digest(plan_text: str) -> str:
    """sha256 of the plan alone. Not the request: approval is of the words
    the implementer will follow, and a rewritten request over an unchanged
    plan is not a new plan to approve."""
    return hashlib.sha256(plan_text.encode("utf-8")).hexdigest()


def read_plan(workspace: ws.Workspace) -> "tuple[str, str]":
    """(plan_text, digest); ("", "") when there is no plan or it is blank."""
    plan_text = ws.read_text(workspace.plan_path)
    if not plan_text.strip():
        return "", ""
    return plan_text, plan_digest(plan_text)


def design_round(workspace: ws.Workspace) -> Optional[str]:
    """The id of the latest design review round, or None.

    Written by ``review run --design`` alone, new every time, so re-running
    the same plan is a new round and neither ``review consolidate --design``
    nor a triage is one. None covers both "no design review ever ran" and a
    round written before rounds had ids: an approval over either records None,
    and the first round that does carry an id makes it stale -- the safe side
    of not knowing whether the review came after the approval.
    """
    meta = workspace.design_review().read_snapshot_meta()
    round_id = meta.get("round_id") if isinstance(meta, dict) else None
    return str(round_id) if round_id else None


def reported_round(design_data: Dict[str, Any]) -> Optional[str]:
    """The design round a consolidated report describes, or None.

    ``design_round`` moves on as soon as ``review run --design`` starts, before
    any reviewer has answered; this one only once the round has a report. An
    approval is bound to this one, read from the same report as the findings
    it names, so it can never be given over a round whose findings nobody has
    seen yet.
    """
    snapshot = design_data.get("snapshot") if isinstance(design_data, dict) else None
    round_id = snapshot.get("round_id") if isinstance(snapshot, dict) else None
    return str(round_id) if round_id else None


def unreviewed_round(design_data: Dict[str, Any]) -> Optional[str]:
    """The design round that ran to the end with no reviewer's review, or None.

    Unlike a round still running, there is nothing to wait for: ``design
    approve`` goes ahead over it and says the plan went unreviewed -- the
    round's report has no findings, and none from before it are carried.
    """
    snapshot = design_data.get("snapshot") if isinstance(design_data, dict) else None
    round_id = snapshot.get("unreviewed_round") if isinstance(snapshot, dict) else None
    return str(round_id) if round_id else None


def open_findings(design_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every design finding not rejected or merged as a duplicate, whatever its severity.

    Not ``review.unresolved_blocking``: that one decides what blocks and so
    leaves out what is below ``re_review_severities``, where approving is the
    user's decision over every finding still open, not only the severe ones.
    """
    findings = design_data.get("findings") if isinstance(design_data, dict) else None
    return [
        f
        for f in (findings or [])
        if isinstance(f, dict) and f.get("triage") not in ("rejected", "duplicate")
    ]


def findings_of_current_plan(
    workspace: ws.Workspace, plan_text: str, state: Optional[Dict[str, Any]] = None
) -> Optional[bool]:
    """Whether the latest design round reviewed this plan text, or None if none did.

    Compared with the frozen plan in ``review-target.md`` rather than through
    ``design_digest``: that one hashes the request as it is on disk now, so a
    rewritten request would make findings about this very plan look old. The
    decisions are compared with their frozen copy the same way: a round that
    reviewed this plan under answers since revised reviewed an earlier one.
    ``state`` is ``state.json`` as the caller read it (``None``: read it here).
    """
    review = workspace.design_review()
    # Read, not os.path.isfile: that can miss the snapshot mid-rename (#358).
    snapshot = ws.read_text(review.snapshot_path, None)
    if snapshot is None:
        return None
    if snapshot != plan_text:
        return False
    decisions = questions_mod.decisions_text(workspace.read_state() if state is None else state)
    return ws.read_text(review.snapshot_decisions_path) == decisions


def epoch_of(stamp: Any) -> Optional[float]:
    """An event's ``at`` as seconds since the epoch, or None if it is not one."""
    try:
        return datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def implemented_since_plan(workspace: ws.Workspace, events: List[Dict[str, Any]]) -> bool:
    """Whether the last successful implementer run is newer than the plan file.

    Measured against the plan's mtime rather than the last architect event: a
    plan the orchestrator revised by hand leaves no event behind. Event times
    are whole seconds, so a run in the same second as the plan's last write
    counts as older -- the side that asks. A failed run never counts.
    """
    finished = [e for e in events if e.get("stage") == "implementer" and e.get("status") == "ok"]
    if not finished:
        return False
    at = epoch_of(finished[-1].get("at"))
    try:
        written = os.path.getmtime(workspace.plan_path)
    except OSError:
        return False
    return at is not None and at > written


def current(
    workspace: ws.Workspace,
    required: bool,
    asking: bool = True,
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The approval state `status` prints and `run implementer` gates on.

    ``matches_current_plan`` and ``reviewed_since_approval`` are worked out
    whether or not approval is required, so turning the setting off does not
    hide what the record says about the plan. ``asking`` is
    ``questions.asking``: with ``design.ask_questions`` off the recorded
    answers are not enforced, so a revised one does not stale the approval.
    ``state`` is ``state.json`` as the caller read it (``None``: read it
    here). ``approval_auto.current`` adds the keys of the requirement in force.
    """
    _, digest = read_plan(workspace)
    if state is None:
        state = workspace.read_state()
    approved = state.get(STAGE)
    if not isinstance(approved, dict):
        approved = None
    events = [e for e in (state.get("events") or []) if isinstance(e, dict)]
    round_id = design_round(workspace)

    matches = None
    reviewed_since = None
    decisions_changed = False
    if approved is not None:
        reviewed_since = approved.get("design_round") != round_id
        if digest:
            matches = approved.get("sha256") == digest
        # Only an entry that recorded the answers can find them changed: one
        # from before the design questions has no key and never goes stale by it.
        if asking and "decisions_sha256" in approved:
            decisions_changed = approved.get("decisions_sha256") != questions_mod.decisions_digest(state)

    stale_reason = None
    if not required:
        name = "not-required"
    elif not digest:
        name = "no-plan"
    elif approved is None:
        # Informational only: the gate refuses this state like `pending`. It
        # keeps a workflow finished before the gate existed from being told to
        # ask at every later stage.
        name = "implemented-unapproved" if implemented_since_plan(workspace, events) else "pending"
    elif not matches:
        name, stale_reason = "stale", "plan-changed"
    elif reviewed_since:
        name, stale_reason = "stale", "reviewed-since"
    elif decisions_changed:
        name, stale_reason = "stale", "decisions-changed"
    else:
        name = "approved"

    return {
        "required": bool(required),
        "state": name,
        "pending": name in ("pending", "stale"),
        "stale_reason": stale_reason,
        "plan_sha256": digest or None,
        "approved_sha256": (approved or {}).get("sha256"),
        "approved_at": (approved or {}).get("approved_at"),
        "matches_current_plan": matches,
        "reviewed_since_approval": reviewed_since,
    }


def record(
    workspace: ws.Workspace,
    digest: str,
    open_findings: List[str],
    of_current_plan: Optional[bool],
    round_id: Optional[str],
    asking: bool,
) -> Dict[str, Any]:
    """Write state["design_approval"] and append the run-log event under one lock.

    The entry keeps ``decisions_sha256``, ``questions.decisions_digest`` as
    the answers are, or None when no question was asked: an answer revised
    afterwards makes the approval stale even over an unchanged plan. With
    ``asking`` (``questions.asking``), an open question raises
    ``questions.QuestionsOpen`` and nothing is written. Both are read under
    the lock the entry is written under, so a question recorded meanwhile can
    neither slip past the check nor into the digest of an approval.
    """
    entry = {
        "sha256": digest,
        "plan": workspace.relative(workspace.plan_path),
        "approved_at": ws.utcnow(),
        "open_findings": list(open_findings),
        "open_findings_of_current_plan": of_current_plan,
        "design_round": round_id,
    }
    event = ws.new_event(
        STAGE,
        "approved",
        {"sha256": digest, "open_findings": list(open_findings), "design_round": round_id},
    )
    with ws.file_lock(workspace.state_path):
        state = workspace.read_state()
        still_open = questions_mod.open_ids(questions_mod.read(state))
        if asking and still_open:
            raise questions_mod.QuestionsOpen(still_open)
        entry["decisions_sha256"] = questions_mod.decisions_digest(state)
        state[STAGE] = entry
        ws.append_event(state, event)
        workspace.write_state(state)
    return entry
