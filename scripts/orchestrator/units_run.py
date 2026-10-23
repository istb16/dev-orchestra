"""A work unit's run, from its claim to its clean: the state each step leaves in ``units.json``.

The library side of ``run implementer --unit``, ``jobs cancel`` and the
``units`` commands. It returns facts and raises refusals; the CLI prints them,
as ``jobs.cancel`` and ``jobs cancel`` share the work. A refusal's reason is a
sentence all the same: the CLI writes it into the job record too.

``ws.write_tree`` and ``worktrees_mod.create`` are called through their
modules, never imported by name, so a test can stand in for either.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import execution_tree, units_merge
from . import jobs as jobs_mod
from . import units as units_mod
from . import workspace as ws
from . import worktrees as worktrees_mod

#: What ``clean`` did with a unit: left a running unit's worktree, removed
#: a worktree, or could not remove one.
STILL_RUNNING = "running"
REMOVED = "removed"
NOT_REMOVED = "not_removed"


class Refused(ValueError):
    """A unit that may not start; the message is what follows ``--unit NAME: ``."""


class NoSuchUnit(ValueError):
    """A unit ``clean`` was named that has no record; the message says so."""


class Claimed(NamedTuple):
    """A unit ``claim`` recorded as running, with where it runs."""

    name: str
    #: Run in the user's tree, with no worktree and no merge.
    in_place: bool
    #: Where the agent runs: the unit's worktree, or the user's tree.
    cwd: str
    #: The unit's record before it was claimed, put back if the run is refused.
    previous: Optional[Dict[str, Any]]
    #: A link failed: the unit's own checks would run without its environment.
    checks_skipped: bool
    #: The ``implementation.worktree_files`` not copied in, each ``{"path", "reason"}``.
    copies_skipped: Tuple[Dict[str, str], ...] = ()


class Finished(NamedTuple):
    """How ``finish`` recorded a unit's end."""

    #: The status written; "" when the unit was not running.
    status: str
    #: Set when the result tree could not be written: the worktree kept with
    #: the unit's edits, "" when the record named none. None otherwise.
    kept_worktree: Optional[str] = None


class Cancelled(NamedTuple):
    """What ``cancel`` left behind."""

    #: The unit's worktree could not be removed; ``units clean`` tries again.
    worktree_kept: bool
    #: The files a unit that ran in place changed in the working tree.
    left_in_tree: List[str]


class Cleaned(NamedTuple):
    """What ``clean`` did with one unit's worktree."""

    name: str
    #: ``STILL_RUNNING``, ``REMOVED`` or ``NOT_REMOVED``.
    action: str
    worktree: str


# --------------------------------------------------------------------------- liveness


def may_still_run(workspace: ws.Workspace, record: Dict[str, Any]) -> bool:
    """Whether a running unit's run may still be going; False for a unit not running.

    By its job's worker when a detached run named one whose record is there;
    otherwise by the record itself, as ``jobs.may_still_run`` reads one: the
    pid of the process that claimed it, or for a record with none, its start.
    """
    if record.get("status") != units_mod.RUNNING:
        return False
    if record.get("job"):
        job = jobs_mod.read_job(workspace, str(record["job"]))
        if job is not None:
            return jobs_mod.may_still_run(job)
    return jobs_mod.may_still_run(record)


def _fail_gone_run(workspace: ws.Workspace, name: str, record: Dict[str, Any]) -> bool:
    """Record a unit whose run is gone as failed; False when its record has changed since it was read.

    Under the units lock, and only while the record is still that run's: a
    worker that finished in between has written how it ended.
    """
    with units_mod.locked(workspace):
        state = units_mod.load_state(workspace)
        current = state["units"].get(name)
        if not isinstance(current, dict) or current.get("status") != units_mod.RUNNING:
            return False
        if (current.get("job"), current.get("pid")) != (record.get("job"), record.get("pid")):
            return False
        current.update(status=units_mod.FAILED, finished_at=ws.utcnow())
        units_mod.save_state(workspace, state)
    return True


def reconcile(workspace: ws.Workspace) -> List[str]:
    """Record every running unit whose run is gone as failed, so it can be run again; their names.

    As ``jobs`` records a dead worker's job ``abandoned`` when it reads it.
    The job files are read outside the units lock, which is taken only to
    write.
    """
    entries = units_mod.load_state(workspace)["units"]
    failed: List[str] = []
    for name in sorted(entries):
        record = entries[name]
        if not isinstance(record, dict) or record.get("status") != units_mod.RUNNING:
            continue
        if may_still_run(workspace, record):
            continue
        if _fail_gone_run(workspace, name, record):
            failed.append(name)
    return failed


# --------------------------------------------------------------------------- claim


def refusal(
    found: Sequence[units_mod.Unit], entries: Dict[str, Dict[str, Any]], unit: units_mod.Unit, cap: int
) -> str:
    """Why ``unit`` cannot start now; "" when it can. Read under the units lock."""
    status = units_mod.status_of(entries, unit.name)
    if status == units_mod.RUNNING:
        return "it is already running"
    if status == units_mod.MERGED:
        return "it is already merged"
    if status == units_mod.CONFLICTED:
        return "it has conflicts to resolve: resolve them, then run units merge %s --resolved" % unit.name
    if status == units_mod.SUCCEEDED:
        return "its result is not merged yet: run units merge %s first" % unit.name
    waiting = [dep for dep in unit.depends_on if units_mod.status_of(entries, dep) != units_mod.MERGED]
    if waiting:
        return "it depends on %s, not merged yet" % ", ".join(
            "%s (%s)" % (dep, units_mod.status_of(entries, dep)) for dep in waiting
        )
    running = [other.name for other in found if units_mod.status_of(entries, other.name) == units_mod.RUNNING]
    in_place = [name for name in running if (entries.get(name) or {}).get("in_place")]
    if in_place:
        return "%s is running in place, in the user's tree, and nothing may start beside it" % in_place[0]
    if len(running) >= cap:
        return "implementation.max_parallel is %d, and %s %s running" % (
            cap,
            ", ".join(running),
            "is" if len(running) == 1 else "are",
        )
    return ""


def claim(
    workspace: ws.Workspace,
    found: Sequence[units_mod.Unit],
    unit: units_mod.Unit,
    plan_sha256: str,
    max_parallel: int,
    copies: Sequence[str] = (),
    checked_copies: Sequence[str] = (),
) -> Claimed:
    """Claim ``unit`` and make its worktree, given ``copies`` and ``checked_copies`` (``worktrees.create``).

    Raises ``Refused`` when it may not start. The checks and the claim are
    made under the units lock, so two units starting together cannot both
    pass the parallelism cap. Runs that are gone are recorded failed first
    (``reconcile``), so the checks see what ``units status`` shows. The git
    work is done after the lock is let go, with the unit already recorded as
    running.
    """
    name = unit.name
    reconcile(workspace)
    with units_mod.locked(workspace):
        state = units_mod.load_state(workspace)
        if state.get("plan_sha256") != plan_sha256:
            held = sorted(
                other
                for other, entry in state["units"].items()
                if isinstance(entry, dict)
                and (entry.get("worktree") or entry.get("status") == units_mod.RUNNING)
            )
            if held:
                raise Refused(
                    "units of an earlier plan are running or hold a worktree (%s); "
                    "cancel their jobs or run units clean first" % ", ".join(held)
                )
            state = {"plan_sha256": plan_sha256, "units": {}}
        entries = {key: value for key, value in state["units"].items() if isinstance(value, dict)}
        reason = refusal(found, entries, unit, max_parallel)
        if reason:
            raise Refused("refused -- %s" % reason)
        in_place = units_mod.runs_in_place(found, state, name)
        previous = entries.get(name)
        if previous is not None and previous.get("worktree"):
            # Claimed again, the record would no longer name it, and nothing would remove it.
            raise Refused(
                "refused -- an earlier run's worktree is still there (%s); "
                "run units clean %s first" % (previous["worktree"], name)
            )
        # This process, until a detached run names its job: a foreground run
        # killed before its `finally` leaves no job, and `units clean` finds
        # it gone by the pid.
        state["units"][name] = {
            "status": units_mod.RUNNING,
            "in_place": in_place,
            "job": None,
            "pid": os.getpid(),
            "pid_started": execution_tree.process_started(os.getpid()),
            "started_at": ws.utcnow(),
        }
        units_mod.save_state(workspace, state)
    if in_place:
        # An earlier in-place run that was cancelled or failed left its edits in
        # the tree: keep its base, so this run's diff covers both.
        earlier = previous if isinstance(previous, dict) else {}
        kept = earlier.get("in_place") and earlier.get("status") in (units_mod.CANCELLED, units_mod.FAILED)
        base_tree = earlier.get("base_tree") if kept else None
        if not base_tree:
            base_tree = ws.write_tree(workspace.root, worktrees_mod.excludes(workspace)) or None
        fields: Dict[str, Any] = {
            "base_tree": base_tree,
            "base_commit": None,
            "worktree": None,
            "links": [],
            "checks_skipped": False,
        }
    else:
        try:
            fields = worktrees_mod.create(workspace, name, copies, checked_copies)
        except (worktrees_mod.WorktreeError, OSError) as exc:
            restore(workspace, name, previous)
            raise Refused("no worktree could be made: %s" % exc) from None
    units_mod.update(workspace, name, fields)
    return Claimed(
        name,
        in_place,
        workspace.root if in_place else str(fields["worktree"]),
        previous,
        bool(fields["checks_skipped"]),
        tuple(fields.get("copies_skipped") or ()),
    )


def restore(workspace: ws.Workspace, name: str, previous: Optional[Dict[str, Any]]) -> None:
    """Put back the record a unit had before it was claimed."""
    with units_mod.locked(workspace):
        state = units_mod.load_state(workspace)
        if previous is None:
            state["units"].pop(name, None)
        else:
            state["units"][name] = previous
        units_mod.save_state(workspace, state)


def release(workspace: ws.Workspace, name: str, previous: Optional[Dict[str, Any]]) -> None:
    """Undo a claim whose run was refused its attempt: the worktree goes, the record is put back."""
    record = units_mod.load_state(workspace)["units"].get(name)
    if isinstance(record, dict):
        worktrees_mod.remove(workspace.root, record, name)
    restore(workspace, name, previous)


# --------------------------------------------------------------------------- the end of a run


def finish(workspace: ws.Workspace, name: str, ok: bool) -> Finished:
    """Close a unit's run: write its tree and diff, record how it ended, remove its worktree.

    A unit that ran in place and succeeded is ``merged``: its changes are
    already in the user's tree. One that ran in a worktree whose result tree
    could not be written is ``failed``, so it can be run again, and its
    worktree, which holds its edits, is kept (``kept_worktree``). Nothing is
    done for a unit not running.
    """
    record = units_mod.load_state(workspace)["units"].get(name)
    if not isinstance(record, dict) or record.get("status") != units_mod.RUNNING:
        return Finished("")
    in_place = bool(record.get("in_place"))
    worktree = None if in_place else record.get("worktree")
    tree_root = str(worktree) if worktree and os.path.isdir(str(worktree)) else workspace.root
    links = [str(link) for link in (record.get("links") or [])]
    result_tree = ""
    if in_place or tree_root != workspace.root:
        result_tree = ws.write_tree(tree_root, worktrees_mod.excludes(workspace, links))
    fields: Dict[str, Any] = {"result_tree": result_tree or None, "finished_at": ws.utcnow()}
    if record.get("error"):
        fields["error"] = None  # an earlier run's
    base_tree = record.get("base_tree")
    if base_tree and result_tree:
        code, diff, _ = ws.git(
            ["diff", "--binary", "--no-color", str(base_tree), result_tree], workspace.root
        )
        if code == 0:
            ws.write_text(os.path.join(workspace.units_dir, "%s.diff" % name), diff)
    kept_worktree: Optional[str] = None
    if not ok:
        fields["status"] = units_mod.FAILED
    elif in_place:
        fields.update(status=units_mod.MERGED, merged_at=ws.utcnow())
        if base_tree and result_tree:
            fields["outside_files"] = _outside_files(workspace, name, str(base_tree), result_tree, links)
    elif not result_tree:
        # Recorded succeeded, it could be neither merged nor run again. Its
        # edits are only in the worktree, so that stays for `units clean`.
        fields.update(status=units_mod.FAILED, error="its result tree could not be written")
        kept_worktree = str(worktree or "")
        worktree = None
    else:
        fields["status"] = units_mod.SUCCEEDED
    if worktree:
        fields["worktree"] = None if worktrees_mod.remove(workspace.root, record, name) else worktree
    units_mod.update(workspace, name, fields)
    return Finished(str(fields["status"]), kept_worktree)


def _outside_files(workspace: ws.Workspace, name: str, base: str, result: str, links: List[str]) -> List[str]:
    """The files unit ``name`` changed outside its ``files``, as ``units merge`` records them."""
    try:
        found = units_mod.parse(ws.read_text(workspace.plan_path))
        changed = units_merge.changed_paths(
            workspace.root, base, result, worktrees_mod.excludes(workspace, links)
        )
    except (units_mod.UnitsError, units_merge.MergeError):
        return []
    unit = next((entry for entry in found if entry.name == name), None)
    return units_merge.outside_files(unit.files, changed) if unit else []


def cancel(workspace: ws.Workspace, name: str, job_id: Optional[str]) -> Cancelled:
    """Remove unit ``name``'s worktree, its job ``job_id`` cancelled; a running unit is ``cancelled``.

    Only the unit this job ran: a record that has since gone to another job is
    left alone. A failed one keeps its status, which says why it ended.
    """
    record = units_mod.load_state(workspace)["units"].get(name)
    if not isinstance(record, dict) or record.get("job") not in (None, job_id):
        return Cancelled(False, [])
    if record.get("status") not in (units_mod.RUNNING, units_mod.FAILED):
        return Cancelled(False, [])
    fields: Dict[str, Any] = {}
    if record.get("status") == units_mod.RUNNING:
        fields.update(status=units_mod.CANCELLED, finished_at=ws.utcnow())
    worktree_kept = False
    if record.get("worktree"):
        gone = worktrees_mod.remove(workspace.root, record, name)
        fields["worktree"] = None if gone else record["worktree"]
        worktree_kept = not gone
    left_in_tree: List[str] = []
    if record.get("in_place") and record.get("base_tree"):
        left_in_tree = _changed_in_place(workspace, str(record["base_tree"]))
    units_mod.update(workspace, name, fields)
    return Cancelled(worktree_kept, left_in_tree)


def _changed_in_place(workspace: ws.Workspace, base_tree: str) -> List[str]:
    """The files changed in the working tree since ``base_tree``; [] when git cannot say."""
    excludes = worktrees_mod.excludes(workspace)
    now = ws.write_tree(workspace.root, excludes)
    if not now:
        return []
    try:
        return units_merge.changed_paths(workspace.root, base_tree, now, excludes)
    except units_merge.MergeError:
        return []


def clean(workspace: ws.Workspace, name: Optional[str] = None) -> List[Cleaned]:
    """Remove the worktrees units left behind: every unit's, or ``name``'s; what was done with each.

    A unit still running keeps its worktree. One whose worker is gone without
    finishing it is recorded as failed, so it can be run again. Each record is
    read again when its turn comes: removing a worktree before it can take
    seconds, and a unit can finish meanwhile. Raises ``NoSuchUnit`` for a
    ``name`` with no record.
    """
    state = units_mod.load_state(workspace)
    entries = {key: entry for key, entry in state["units"].items() if isinstance(entry, dict)}
    if name and name not in entries:
        raise NoSuchUnit("no unit named %s has a record" % name)
    names = [name] if name else sorted(entries)
    done: List[Cleaned] = []
    for each in names:
        record = units_mod.load_state(workspace)["units"].get(each)
        if not isinstance(record, dict):
            continue
        if record.get("status") == units_mod.RUNNING:
            if may_still_run(workspace, record):
                if record.get("worktree"):
                    done.append(Cleaned(each, STILL_RUNNING, str(record["worktree"])))
                continue
            if not _fail_gone_run(workspace, each, record):
                continue
        if not record.get("worktree"):
            continue
        if worktrees_mod.remove(workspace.root, record, each):
            units_mod.update(workspace, each, {"worktree": None})
            done.append(Cleaned(each, REMOVED, str(record["worktree"])))
        else:
            done.append(Cleaned(each, NOT_REMOVED, str(record["worktree"])))
    return done
