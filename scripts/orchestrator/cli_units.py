"""The ``units`` commands: a plan's work units, checked and reported."""

from __future__ import annotations

import argparse
from typing import Any, Dict, List

from . import approval as approval_mod
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
