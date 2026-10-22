"""The ``units`` commands: a plan's work units, checked and reported."""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Sequence

from . import approval as approval_mod
from . import atomic_file, units_merge
from . import jobs as jobs_mod
from . import units as units_mod
from . import workspace as ws
from . import worktrees as worktrees_mod
from .cli_common import _emit_json, _err, _out, _workspace, _workspace_and_config


def _read_units(workspace: ws.Workspace) -> "tuple[str, List[units_mod.Unit]]":
    """(plan_text, units); raises ``UnitsError`` for a block that cannot be read."""
    plan_text = ws.read_text(workspace.plan_path)
    return plan_text, units_mod.parse(plan_text)


def cmd_units_check(args: argparse.Namespace) -> int:
    """Refuse a plan whose units cannot be run; print the schedule of one that can."""
    workspace, loaded = _workspace_and_config(args)
    plan_relative = workspace.relative(workspace.plan_path)
    try:
        plan_text, found = _read_units(workspace)
    except units_mod.UnitsError as exc:
        _err("%s: %s" % (plan_relative, exc))
        return 2
    if not plan_text.strip():
        _err("no plan at %s" % plan_relative)
        return 2
    if not found:
        _out("%s has no ## Units: a single run." % plan_relative)
        return 0
    settings = units_mod.settings_of(loaded.data)
    problems = units_mod.validate(found, settings.hub_paths, plan_text)
    if problems:
        _err("%s: the units cannot be run:" % plan_relative)
        for problem in problems:
            _err("  - %s" % problem)
        return 2
    for warning in units_mod.warnings(found, plan_text):
        _err("warning: %s" % warning)
    waves = units_mod.schedule(found, settings.max_parallel)
    cap = settings.max_parallel
    _out("%d units in %d waves (implementation.max_parallel: %d):" % (len(found), len(waves), cap))
    for number, wave in enumerate(waves, 1):
        _out("  wave %d: %s" % (number, ", ".join(wave)))
    return 0


def cmd_units_status(args: argparse.Namespace) -> int:
    """Each unit's state, which can start now, and whether the implement stage failed."""
    workspace, _ = _workspace_and_config(args)
    plan_relative = workspace.relative(workspace.plan_path)
    try:
        plan_text, found = _read_units(workspace)
    except units_mod.UnitsError as exc:
        _err("%s: %s" % (plan_relative, exc))
        return 2
    digest = approval_mod.plan_digest(plan_text) if plan_text.strip() else ""
    entries = units_mod.records(units_mod.load_state(workspace), digest)
    rows: List[Dict[str, Any]] = []
    for unit in found:
        entry = entries.get(unit.name) or {}
        rows.append(
            {
                "name": unit.name,
                "kind": unit.kind,
                "status": units_mod.status_of(entries, unit.name),
                "job": entry.get("job"),
                "base_commit": entry.get("base_commit"),
                "in_place": bool(entry.get("in_place")),
                "worktree": entry.get("worktree"),
            }
        )
    ready = units_mod.ready(found, entries)
    failed = any(row["status"] == units_mod.FAILED for row in rows)
    if args.json:
        _emit_json({"plan_sha256": digest, "units": rows, "ready": ready, "failed": failed})
        return 0
    if not found:
        _out("%s has no ## Units: a single run." % plan_relative)
        return 0
    for row in rows:
        place = "in place" if row["in_place"] else (row["worktree"] or "-")
        _out(
            "  %-20s %-10s %-11s job %s  base %s  %s"
            % (
                row["name"],
                row["kind"],
                row["status"],
                row["job"] or "-",
                str(row["base_commit"] or "-")[:12],
                place,
            )
        )
    _out("Ready to start: %s" % (", ".join(ready) or "none"))
    if failed:
        _out("The implement stage failed: a unit failed, and the units that depend on it cannot start.")
    return 0


def _touched_by(
    workspace: ws.Workspace,
    found: Sequence[units_mod.Unit],
    entries: Dict[str, Dict[str, Any]],
    name: str,
    paths: Sequence[str],
) -> Dict[str, List[str]]:
    """Each earlier-merged unit that changed one of ``paths``, with those of them it changed."""
    touched: Dict[str, List[str]] = {}
    for unit in found:
        entry = entries.get(unit.name) or {}
        base, result = entry.get("base_tree"), entry.get("result_tree")
        if unit.name == name or entry.get("status") != units_mod.MERGED or not base or not result:
            continue
        excludes = worktrees_mod.excludes(workspace, [str(link) for link in (entry.get("links") or [])])
        try:
            changed = units_merge.changed_paths(workspace.root, str(base), str(result), excludes)
        except units_merge.MergeError:
            continue
        shared = [path for path in paths if path in changed]
        if shared:
            touched[unit.name] = shared
    return touched


def _merge_report(args: argparse.Namespace, report: Dict[str, Any], lines: Sequence[str]) -> None:
    if args.json:
        _emit_json(report)
        return
    for line in lines:
        _out(line)


def _resolve_unit(
    args: argparse.Namespace, workspace: ws.Workspace, name: str, record: Dict[str, Any]
) -> int:
    """``units merge NAME --resolved``: the conflicts are resolved, so the unit is merged."""
    status = str(record.get("status") or units_mod.PENDING)
    if status == units_mod.MERGED:
        _merge_report(args, {"unit": name, "status": status}, ["%s is already merged." % name])
        return 0
    if status != units_mod.CONFLICTED:
        _err("%s has no conflicts to resolve: it is %s" % (name, status))
        return 2
    conflicts = [str(path) for path in (record.get("conflicts") or [])]
    left = units_merge.markers_left(workspace.root, name, conflicts)
    if left:
        _err("%s: conflict markers are still in %s" % (name, ", ".join(left)))
        return 2
    units_mod.update(
        workspace,
        name,
        {"status": units_mod.MERGED, "merged_at": ws.utcnow(), "resolved_files": conflicts},
    )
    workspace.record_event("units", "resolved", {"unit": name, "resolved_files": conflicts})
    _merge_report(
        args,
        {"unit": name, "status": units_mod.MERGED, "resolved_files": conflicts},
        ["merged %s: its conflicts in %s are resolved" % (name, ", ".join(conflicts) or "no file")],
    )
    return 0


def cmd_units_merge(args: argparse.Namespace) -> int:
    """Merge a finished unit's result into the working tree, file by file; only the working tree.

    One merge at a time, under a lock of its own: the units lock is held only
    to rewrite ``units.json``, so the git work does not keep other units from
    recording how they ended.
    """
    workspace = _workspace(args)
    plan_relative = workspace.relative(workspace.plan_path)
    try:
        plan_text, found = _read_units(workspace)
    except units_mod.UnitsError as exc:
        _err("%s: %s" % (plan_relative, exc))
        return 2
    unit = next((entry for entry in found if entry.name == args.name), None)
    if unit is None:
        _err("%s has no unit named %s" % (plan_relative, args.name))
        return 2
    name = unit.name
    with ws.file_lock(os.path.join(workspace.units_dir, "merge")):
        entries = units_mod.records(units_mod.load_state(workspace), approval_mod.plan_digest(plan_text))
        record = entries.get(name) or {}
        if args.resolved:
            return _resolve_unit(args, workspace, name, record)
        status = units_mod.status_of(entries, name)
        if status == units_mod.MERGED:
            # A unit that ran in place is merged when it ends: its changes are in the tree already.
            _merge_report(args, {"unit": name, "status": status}, ["%s is already merged." % name])
            return 0
        brief = workspace.relative(os.path.join(workspace.units_dir, "%s-conflicts.md" % name))
        if status == units_mod.CONFLICTED:
            _err(
                "%s has conflicts to resolve (%s); then run: units merge %s --resolved" % (name, brief, name)
            )
            return units_mod.EXIT_CONFLICTS
        base, result = record.get("base_tree"), record.get("result_tree")
        if status != units_mod.SUCCEEDED or not base or not result:
            _err("%s cannot be merged: it is %s, not succeeded with its result recorded" % (name, status))
            return 2
        excludes = worktrees_mod.excludes(workspace, [str(link) for link in (record.get("links") or [])])
        try:
            results = units_merge.plan_merge(workspace.root, name, str(base), str(result), excludes)
            units_merge.apply(workspace, name, results)
        except (units_merge.MergeError, atomic_file.Raced, OSError) as exc:
            _err("could not merge %s: %s" % (name, exc))
            return 1
        outside = units_merge.outside_files(unit.files, [entry.path for entry in results])
        conflicts = [entry for entry in results if entry.action == units_merge.CONFLICT]
        notes = ["  outside its files: %s" % path for path in outside]
        if conflicts:
            paths = [entry.path for entry in conflicts]
            touched = _touched_by(workspace, found, entries, name, paths)
            units_merge.write_brief(workspace, name, conflicts, plan_text, touched)
            units_mod.update(
                workspace,
                name,
                {"status": units_mod.CONFLICTED, "conflicts": paths, "outside_files": outside},
            )
            workspace.record_event("units", "conflicted", {"unit": name, "conflicts": paths})
            _merge_report(
                args,
                {
                    "unit": name,
                    "status": units_mod.CONFLICTED,
                    "conflicts": paths,
                    "outside_files": outside,
                    "brief": brief,
                },
                [
                    "%s conflicts in %d file(s): %s" % (name, len(paths), ", ".join(paths)),
                    *notes,
                    "Resolve them with the brief %s, then run: units merge %s --resolved" % (brief, name),
                ],
            )
            return units_mod.EXIT_CONFLICTS
        units_mod.update(
            workspace,
            name,
            {"status": units_mod.MERGED, "merged_at": ws.utcnow(), "conflicts": [], "outside_files": outside},
        )
        workspace.record_event("units", "merged", {"unit": name, "files": len(results)})
        _merge_report(
            args,
            {"unit": name, "status": units_mod.MERGED, "files": len(results), "outside_files": outside},
            ["merged %s: %d file(s)" % (name, len(results)), *notes],
        )
        return 0


def cmd_units_join(args: argparse.Namespace) -> int:
    """Join the per-unit architects' plans into one plan; nothing is written when its units cannot run."""
    _, loaded = _workspace_and_config(args)
    parts: List["tuple[str, str]"] = []
    for spec in args.part:
        name, sep, path = spec.partition("=")
        if not sep or not name or not path:
            _err("--part %s: expected NAME=PATH" % spec)
            return 2
        text = ws.read_text(path)
        if not text.strip():
            _err("--part %s: no plan at %s" % (name, path))
            return 2
        parts.append((name, text))
    try:
        plan_text = units_mod.join(parts)
        found = units_mod.parse(plan_text)
    except units_mod.UnitsError as exc:
        _err("could not join the plans: %s" % exc)
        return 2
    problems = units_mod.validate(found, units_mod.settings_of(loaded.data).hub_paths, plan_text)
    if problems:
        _err("the joined units cannot be run:")
        for problem in problems:
            _err("  - %s" % problem)
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    ws.write_text(args.output, plan_text)
    _out("joined %d unit plans into %s" % (len(found), args.output))
    return 0


def cmd_units_clean(args: argparse.Namespace) -> int:
    """Remove the worktrees units left behind: every unit's, or NAME's.

    A unit still running keeps its worktree. One whose worker is gone without
    finishing it is recorded as failed, so it can be run again.
    """
    workspace = _workspace(args)
    state = units_mod.load_state(workspace)
    entries = {name: entry for name, entry in state["units"].items() if isinstance(entry, dict)}
    if args.name and args.name not in entries:
        _err("no unit named %s has a record" % args.name)
        return 2
    names = [args.name] if args.name else sorted(entries)
    removed = kept = 0
    for name in names:
        record = entries[name]
        if record.get("status") == units_mod.RUNNING:
            job = jobs_mod.read_job(workspace, str(record.get("job"))) if record.get("job") else None
            if job is None or jobs_mod.may_still_run(job):
                if record.get("worktree"):
                    _out("%s is running; its worktree stays: %s" % (name, record["worktree"]))
                continue
        if not record.get("worktree") and record.get("status") != units_mod.RUNNING:
            continue
        fields: Dict[str, Any] = {}
        if record.get("status") == units_mod.RUNNING:
            fields.update(status=units_mod.FAILED, finished_at=ws.utcnow())
        if record.get("worktree"):
            if worktrees_mod.remove(workspace.root, record):
                _out("removed %s's worktree: %s" % (name, record["worktree"]))
                fields["worktree"] = None
                removed += 1
            else:
                _err("could not remove %s's worktree: %s" % (name, record["worktree"]))
                kept += 1
        units_mod.update(workspace, name, fields)
    if not removed and not kept:
        _out("No worktrees to remove.")
    return 1 if kept else 0
