"""The ``units`` commands: a plan's work units, checked and reported."""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Sequence

from . import approval as approval_mod
from . import atomic_file, units_merge, units_run
from . import units as units_mod
from . import workspace as ws
from . import worktrees as worktrees_mod
from .cli_args import _add_command_group, _add_json_flag
from .cli_common import _emit_json, _err, _out, _workspace, _workspace_and_config


def _read_units(workspace: ws.Workspace) -> "tuple[str, List[units_mod.Unit]]":
    """(plan_text, units); raises ``UnitsError`` for a block that cannot be read."""
    plan_text = ws.read_text(workspace.plan_path)
    return plan_text, units_mod.parse(plan_text)


def cmd_units_check(args: argparse.Namespace) -> int:
    """Refuse a plan whose units cannot be run; print the schedule of one that can."""
    workspace, loaded = _workspace_and_config(args)
    plan_relative = workspace.relative(workspace.plan_path)
    plan_text = ws.read_text(workspace.plan_path)
    if not plan_text.strip():
        _err("no plan at %s" % plan_relative)
        return 2
    settings = loaded.implementation_settings()
    try:
        found, problems, warnings = units_mod.check(
            plan_text, settings, units_mod.known_files(workspace.root)
        )
    except units_mod.UnitsError as exc:
        _err("%s: %s" % (plan_relative, exc))
        return 2
    if not found:
        _out("%s has no ## Units: a single run." % plan_relative)
        return 0
    if problems:
        _err("%s: the units cannot be run:" % plan_relative)
        for problem in problems:
            _err("  - %s" % problem)
        return 2
    for warning in warnings:
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
    # A unit whose run is gone is failed, as units clean and run --unit find it.
    units_run.reconcile(workspace)
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
    """``units merge NAME --resolved``: the conflicts are resolved, so the unit is merged.

    A conflict that wrote no markers is resolved once its file differs from
    the version the merge kept, or when ``--keep`` names it: keeping that
    version is then the resolution.
    """
    status = str(record.get("status") or units_mod.PENDING)
    if status == units_mod.MERGED:
        _merge_report(args, {"unit": name, "status": status}, ["%s is already merged." % name])
        return 0
    if status != units_mod.CONFLICTED:
        _err("%s has no conflicts to resolve: it is %s" % (name, status))
        return 2
    conflicts = [str(path) for path in (record.get("conflicts") or [])]
    keep = [units_mod.normalise(path) for path in (args.keep or [])]
    strays = [path for path in keep if path not in conflicts]
    if strays:
        _err("--keep %s: not among %s's conflicts (%s)" % (", ".join(strays), name, ", ".join(conflicts)))
        return 2
    left = units_merge.markers_left(workspace.root, name, conflicts)
    if left:
        _err("%s: conflict markers are still in %s" % (name, ", ".join(left)))
        return 2
    untouched = units_merge.untouched(workspace.root, record.get("conflict_files"))
    kept = [path for path in untouched if path not in keep]
    if kept:
        _err(
            "%s: %s still hold the working version the merge kept; resolve them, or keep that "
            "version with --keep PATH" % (name, ", ".join(kept))
        )
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
        if args.keep:
            _err("--keep goes with --resolved")
            return 2
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
                {
                    "status": units_mod.CONFLICTED,
                    "conflicts": paths,
                    "conflict_files": units_merge.conflict_files(workspace.root, conflicts),
                    "outside_files": outside,
                },
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
    workspace, loaded = _workspace_and_config(args)
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
        found, problems, warnings = units_mod.check(
            plan_text, loaded.implementation_settings(), units_mod.known_files(workspace.root)
        )
    except units_mod.UnitsError as exc:
        _err("could not join the plans: %s" % exc)
        return 2
    if problems:
        _err("the joined units cannot be run:")
        for problem in problems:
            _err("  - %s" % problem)
        return 2
    for warning in warnings:
        _err("warning: %s" % warning)
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    ws.write_text(args.output, plan_text)
    _out("joined %d unit plans into %s" % (len(found), args.output))
    return 0


def cmd_units_clean(args: argparse.Namespace) -> int:
    """Remove the worktrees units left behind: every unit's, or NAME's (``units_run.clean``)."""
    workspace = _workspace(args)
    try:
        outcomes = units_run.clean(workspace, args.name or None)
    except units_run.NoSuchUnit as exc:
        _err(str(exc))
        return 2
    removed = kept = 0
    for outcome in outcomes:
        if outcome.action == units_run.STILL_RUNNING:
            _out("%s is running; its worktree stays: %s" % (outcome.name, outcome.worktree))
        elif outcome.action == units_run.REMOVED:
            _out("removed %s's worktree: %s" % (outcome.name, outcome.worktree))
            removed += 1
        else:
            _err("could not remove %s's worktree: %s" % (outcome.name, outcome.worktree))
            kept += 1
    if not removed and not kept:
        _out("No worktrees to remove.")
    return 1 if kept else 0


# --------------------------------------------------------------------------- parser


def _add_units_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    units_sub = _add_command_group(subparsers, "units", "the plan's work units, run apart or side by side")
    units_check = units_sub.add_parser("check", help="refuse units that cannot run, print the schedule")
    units_check.set_defaults(func=cmd_units_check)
    units_status = units_sub.add_parser("status", help="each unit's state, and which can start now")
    _add_json_flag(units_status)
    units_status.set_defaults(func=cmd_units_status)
    units_merge = units_sub.add_parser("merge", help="merge a finished unit into the working tree")
    units_merge.add_argument("name", help="the unit")
    units_merge.add_argument(
        "--resolved",
        action="store_true",
        help="its conflicts are resolved: check no marker is left, mark it merged",
    )
    units_merge.add_argument(
        "--keep",
        action="append",
        default=[],
        metavar="PATH",
        help="with --resolved: a conflict without markers whose working version is kept as the resolution",
    )
    _add_json_flag(units_merge)
    units_merge.set_defaults(func=cmd_units_merge)
    units_clean = units_sub.add_parser("clean", help="remove the worktrees units left behind")
    units_clean.add_argument("name", nargs="?", default=None, help="only this unit's")
    units_clean.set_defaults(func=cmd_units_clean)
    units_join = units_sub.add_parser("join", help="join per-unit plans into one plan with ## Units")
    units_join.add_argument(
        "--part",
        action="append",
        required=True,
        metavar="NAME=PATH",
        help="a unit's plan; repeat it, in the order the units go in the plan",
    )
    units_join.add_argument("--output", required=True, help="where the joined plan is written")
    units_join.set_defaults(func=cmd_units_join)
