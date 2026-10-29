"""Reviewing alongside the tests (``review.start_before_tests``).

A code review round normally waits for the tests: the orchestrator runs them,
records the result, and only then takes the snapshot. Measured here, the tests
took a median 381s from the end of an implement or fix run to their record,
and a round 117s, while only 3 of 219 recorded runs failed. Starting the round
first hides it behind the tests, and a failure is cheap because it is rare.

What makes that safe is tying a test result to the tree it was run on. With
the option on, the snapshot always keeps the working tree as ``tested_tree``
(whatever ``review.incremental_rounds`` says), the report keeps it with the
round's findings, and ``state record test`` keeps the tree the tests ran on.
A round counts only when the last result recorded on its tree passed. A lint
fix made while the tests ran changes the tree, so its round no longer counts:
it has to be taken again, rather than triaged as though the reviewers had
seen the fix.

"Its tree" allows for one difference: anything under the workspace
(``.ai/``, where the review itself writes) and the project config files. Any
other difference, a file added included, is one the reviewers did not see, so
a test run that leaves an unignored file behind (a report it wrote) makes
every round ``changed``: its outputs have to be in ``.gitignore``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import review_snapshot
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


def _test_events(workspace: ws.Workspace) -> List[Dict[str, Any]]:
    events = workspace.read_state().get("events") or []
    if not isinstance(events, list):
        return []
    return [event for event in events if isinstance(event, dict) and event.get("stage") in TEST_STAGES]


def same_tree(workspace: ws.Workspace, reviewed: str, tested: str) -> bool:
    """Whether the tests ran on ``reviewed``, give or take the orchestrator's own files.

    Anything under the workspace or in a project config file, whatever
    happened to it; every other difference -- a file added, changed or
    removed -- is a change the reviewers did not see. Git failing is not the
    same tree.
    """
    if not reviewed or not tested:
        return False
    if reviewed == tested:
        return True
    code, out, _ = ws.git(
        ["diff-tree", "-r", "--no-renames", "--name-status", "-z", reviewed, tested], workspace.root
    )
    if code != 0:
        return False
    # ``-z`` gives "status NUL path NUL" for each entry.
    entries = out.strip("\0")
    fields = entries.split("\0") if entries else []
    if len(fields) % 2:
        return False
    return all(review_snapshot.is_orchestrator_artifact(path, workspace) for path in fields[1::2])


def _last_on(
    workspace: ws.Workspace, events: List[Dict[str, Any]], tree: str, since: str
) -> Optional[Dict[str, Any]]:
    """The newest test event recorded on ``tree``, or None.

    Newest first: the last result on the tree decides. Git is asked only about
    results recorded at or after ``since`` (the snapshot's time) and the
    newest one before it, as status polls this while the tests run and an
    older result was on an older tree; an exact match needs no git at all.
    Without ``since``, only that newest one is asked about.
    """
    before_asked = False
    for event in reversed(events):
        recorded = str(event.get("tree") or "")
        if not recorded:
            continue
        if recorded == tree:
            return event
        if since and str(event.get("at") or "") >= since:
            asked = True
        else:
            asked = not before_asked
            before_asked = True
        if asked and same_tree(workspace, tree, recorded):
            return event
    return None


def tested_status(workspace: ws.Workspace, tree: str, since: str = "") -> str:
    """The last test result recorded on ``tree``, or "" when there is none.

    What the gate of a round started before the tests reads, instead of the
    last result of all: that one was recorded on an earlier tree, and a failure
    there says nothing about this one. ``since`` is when the snapshot was
    taken; see :func:`_last_on`.
    """
    if not tree:
        return ""
    last = _last_on(workspace, _test_events(workspace), tree, since)
    return str(last.get("status") or "") if last else ""


def round_verdict(workspace: ws.Workspace, snapshot: Dict[str, Any]) -> Dict[str, str]:
    """Whether the code round a report holds may be triaged and fixed.

    ``snapshot`` is the report's own ``snapshot`` block: the round its
    findings come from, not whichever snapshot is on disk now. ``usable`` when
    the last test result recorded on its tree passed; ``failed`` when it
    failed; ``pending`` when no test result has been recorded since the
    snapshot was taken; ``changed`` when results were recorded since, but none
    on its tree, or when the round has an empty tree. ``unchecked`` only when
    the round has no tree at all (it was taken with the option off), where
    nothing can be told and nothing is held back.
    """
    if "tested_tree" not in snapshot:
        return {"state": UNCHECKED, "detail": "the round kept no tree to match the tests against"}
    tree = str(snapshot.get("tested_tree") or "")
    if not tree:
        # The snapshot refuses this now; a report from before, or edited by
        # hand, must not slip through as one taken with the option off.
        return {
            "state": CHANGED,
            "detail": "git could not record the reviewed tree, so no test result matches it",
        }
    events = _test_events(workspace)
    taken = str(snapshot.get("taken_at") or "")
    last = _last_on(workspace, events, tree, taken)
    if last is not None and last.get("status") == "ok":
        return {"state": USABLE, "detail": "the tests passed on the reviewed tree"}
    if last is not None:
        return {"state": FAILED, "detail": "the tests failed on the reviewed tree"}
    # Without the time it was taken, every earlier result would read as
    # recorded since, and a round still waiting would be thrown away. The
    # times are to the second, so one in the same second counts as since: a
    # result recorded just before the snapshot was on its tree, and matched
    # above, unless the tree changed within that second.
    if taken and any(str(event.get("at") or "") >= taken for event in events):
        return {
            "state": CHANGED,
            "detail": "the tests recorded since the snapshot ran on a different tree",
        }
    return {"state": PENDING, "detail": "no test result recorded on the reviewed tree yet"}


def report_verdict(workspace: ws.Workspace, data: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """:func:`round_verdict` for the round a code report holds; None with no report."""
    if not data:
        return None
    snapshot = data.get("snapshot")
    if snapshot is not None and not isinstance(snapshot, dict):
        # Not a report from before the option, which has a block without the
        # tree, but one nothing can be read from: held back, not waved through.
        return {"state": CHANGED, "detail": "the report's snapshot block cannot be read"}
    return round_verdict(workspace, snapshot or {})


def usable(verdict: Optional[Dict[str, str]]) -> bool:
    return verdict is None or verdict.get("state") in (USABLE, UNCHECKED)


def needs_new_round(verdict: Optional[Dict[str, str]]) -> bool:
    """Whether the round is thrown away, so the loop has to review the whole change again."""
    return verdict is not None and verdict.get("state") in (FAILED, CHANGED)


def refusal(verdict: Optional[Dict[str, str]], action: str) -> str:
    """Why ``action`` is refused on a round, or "" when it may go on."""
    if verdict is None or usable(verdict):
        return ""
    if verdict.get("state") == PENDING:
        return (
            "refusing to %s: this round started before the tests (review.start_before_tests) "
            "and no test result has been recorded on its tree yet. Record it (state record test "
            "ok|failed) and run this again." % action
        )
    return (
        "refusing to %s: %s (review.start_before_tests), so this round is not used. Fix, "
        "re-test, take a new snapshot (review snapshot) and review again." % (action, verdict.get("detail"))
    )


def next_step(verdict: Optional[Dict[str, str]]) -> str:
    """The next step ``review status`` and ``status`` print instead of their own, or ""."""
    if verdict is None or usable(verdict):
        return ""
    if verdict.get("state") == PENDING:
        return "record the tests on this tree (state record test ok|failed), then review status again"
    return "this round is not used: fix, re-test, then take a new snapshot and review again"
