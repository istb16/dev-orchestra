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

"Its tree" allows for one difference: files added since the snapshot that it
did not have, such as a test report the run wrote, and anything under the
workspace (``.ai/``, where the review itself writes). A test run that leaves
an unignored file behind would otherwise make every round ``changed``. A new
source file made while the tests ran is missed for the same reason.
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
    """Whether the tests ran on ``reviewed``, give or take files added since.

    Additions only, and anything under the workspace or in a project config
    file whatever happened to it; every other difference -- a file changed or
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
    for status, path in zip(fields[0::2], fields[1::2], strict=True):
        if status == "A" or review_snapshot.is_orchestrator_artifact(path, workspace):
            continue
        return False
    return True


def _last_on(workspace: ws.Workspace, events: List[Dict[str, Any]], tree: str) -> Optional[Dict[str, Any]]:
    # Newest first: the last result on the tree decides, and a long run log
    # stops at the first match instead of asking git about every entry.
    for event in reversed(events):
        if event.get("tree") and same_tree(workspace, tree, str(event["tree"])):
            return event
    return None


def tested_status(workspace: ws.Workspace, tree: str) -> str:
    """The last test result recorded on ``tree``, or "" when there is none.

    What the gate of a round started before the tests reads, instead of the
    last result of all: that one was recorded on an earlier tree, and a failure
    there says nothing about this one.
    """
    if not tree:
        return ""
    last = _last_on(workspace, _test_events(workspace), tree)
    return str(last.get("status") or "") if last else ""


def round_verdict(workspace: ws.Workspace, snapshot: Dict[str, Any]) -> Dict[str, str]:
    """Whether the code round a report holds may be triaged and fixed.

    ``snapshot`` is the report's own ``snapshot`` block: the round its
    findings come from, not whichever snapshot is on disk now. ``usable`` when
    the last test result recorded on its tree passed; ``failed`` when it
    failed; ``pending`` when no test result has been recorded since the
    snapshot was taken; ``changed`` when results were recorded since, but none
    on its tree. ``unchecked`` when the round kept no tree (it was taken with
    the option off, or git could not write one), where nothing can be told and
    nothing is held back.
    """
    tree = str(snapshot.get("tested_tree") or "")
    if not tree:
        return {"state": UNCHECKED, "detail": "the round kept no tree to match the tests against"}
    events = _test_events(workspace)
    last = _last_on(workspace, events, tree)
    if last is not None and last.get("status") == "ok":
        return {"state": USABLE, "detail": "the tests passed on the reviewed tree"}
    if last is not None:
        return {"state": FAILED, "detail": "the tests failed on the reviewed tree"}
    # Without the time it was taken, every earlier result would read as
    # recorded since, and a round still waiting would be thrown away. The
    # times are to the second, so one in the same second counts as since: a
    # result recorded just before the snapshot was on its tree, and matched
    # above, unless the tree changed within that second.
    taken = str(snapshot.get("taken_at") or "")
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
    return round_verdict(workspace, snapshot if isinstance(snapshot, dict) else {})


def usable(verdict: Optional[Dict[str, str]]) -> bool:
    return verdict is None or verdict.get("state") in (USABLE, UNCHECKED)


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
