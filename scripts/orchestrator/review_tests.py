"""Reviewing alongside the tests (``review.start_before_tests``).

A code review round normally waits for the tests: the orchestrator runs them,
records the result, and only then takes the snapshot. Measured here, the tests
took a median 381s from the end of an implement or fix run to their record,
and a round 117s, while only 3 of 219 recorded runs failed. Starting the round
first hides it behind the tests, and a failure is cheap because it is rare.

What makes that safe is tying a test result to the tree it was run on. With
the option on, ``state record test`` keeps the working tree as a git tree
object -- the same thing a snapshot keeps -- and a round counts only when the
last result recorded on its snapshot's tree passed. A lint fix made while the
tests ran changes the tree, so its round no longer counts: it has to be taken
again, rather than triaged as though the reviewers had seen the fix.
"""

from __future__ import annotations

from typing import Any, Dict, List

from . import workspace as ws

#: The stages ``state record`` treats as a test result.
TEST_STAGES = ("test", "re-test")

#: Where a round stands against the tests, as :func:`round_verdict` says.
USABLE = "usable"
PENDING = "pending"
FAILED = "failed"
CHANGED = "changed"
UNCHECKED = "unchecked"


def tree_detail(root: str) -> Dict[str, Any]:
    """What ``state record test`` adds to its event: the tree the tests ran on."""
    tree = ws.write_tree(root)
    return {"tree": tree} if tree else {}


def _tests_on(workspace: ws.Workspace, tree: str) -> List[Dict[str, Any]]:
    events = workspace.read_state().get("events") or []
    if not isinstance(events, list):
        return []
    return [
        event
        for event in events
        if isinstance(event, dict) and event.get("stage") in TEST_STAGES and event.get("tree") == tree
    ]


def tested_status(workspace: ws.Workspace, tree: str) -> str:
    """The last test result recorded on ``tree``, or "" when there is none.

    What the gate of a round started before the tests reads, instead of the
    last result of all: that one was recorded on an earlier tree, and a failure
    there says nothing about this one.
    """
    if not tree:
        return ""
    on_tree = _tests_on(workspace, tree)
    return str(on_tree[-1].get("status") or "") if on_tree else ""


def round_verdict(workspace: ws.Workspace, meta: Dict[str, Any]) -> Dict[str, str]:
    """Whether the code round on the snapshot ``meta`` may be triaged and fixed.

    ``usable`` when the last test result recorded on the snapshot's tree
    passed; ``failed`` when it failed; ``pending`` when no test result has
    been recorded since the snapshot; ``changed`` when results were recorded
    since, but none on this tree. ``unchecked`` when the snapshot kept no
    tree (git could not write one), where nothing can be told and nothing is
    held back.
    """
    tree = str(meta.get("tree") or "")
    if not tree:
        return {"state": UNCHECKED, "detail": "the snapshot has no tree to match the tests against"}
    status = tested_status(workspace, tree)
    if status == "ok":
        return {"state": USABLE, "detail": "the tests passed on the reviewed tree"}
    if status:
        return {"state": FAILED, "detail": "the tests failed on the reviewed tree"}
    taken = str(meta.get("generated_at") or "")
    events = workspace.read_state().get("events") or []
    since = [
        event
        for event in (events if isinstance(events, list) else [])
        if isinstance(event, dict)
        and event.get("stage") in TEST_STAGES
        and str(event.get("at") or "") >= taken
    ]
    if since:
        return {
            "state": CHANGED,
            "detail": "the tests recorded since the snapshot ran on a different tree",
        }
    return {"state": PENDING, "detail": "no test result recorded since the snapshot"}


def refusal(verdict: Dict[str, str]) -> str:
    """Why triage and the fix brief refuse a round, or "" when they may go on."""
    state = verdict.get("state")
    if state in (USABLE, UNCHECKED):
        return ""
    if state == PENDING:
        return (
            "refusing: this round started before the tests (review.start_before_tests) and no "
            "test result has been recorded on its tree yet. Record it (state record test "
            "ok|failed) and run this again."
        )
    return (
        "refusing: %s (review.start_before_tests), so this round is not used. Fix, re-test, "
        "take a new snapshot (review snapshot) and review again." % verdict.get("detail")
    )


def next_step(verdict: Dict[str, str]) -> str:
    """The ``next:`` line ``review status`` prints instead of its own, or ""."""
    state = verdict.get("state")
    if state == PENDING:
        return "record the tests on this tree (state record test ok|failed), then review status again"
    if state in (FAILED, CHANGED):
        return "this round is not used: fix, re-test, then take a new snapshot and review again"
    return ""
