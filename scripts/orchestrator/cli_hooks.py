"""The ``hooks`` commands: the reply-language hooks in Claude Code's user settings."""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List

from . import claude_hooks, hosts
from .cli_common import _emit_json, _err, _out

RESTART_NOTE = "Claude Code picks up hooks at session start; start a new session (or review them in /hooks)."
SILENT_NOTE = "the hooks stay silent until language.reply is set"
OTHER_PROJECTS_NOTE = (
    "other projects whose .dev-orchestra.yaml sets language.reply lose the check; "
    "`%s` restores it" % claude_hooks.FIX_COMMAND
)
LEFT_OUT_NOTE = "the reply-language hooks are not installed; `%s` adds them" % claude_hooks.FIX_COMMAND


def _hooks_disabled_note() -> None:
    try:
        settings = claude_hooks.read_settings(claude_hooks.settings_path())
    except claude_hooks.HooksError:
        return
    if settings.get("disableAllHooks") is True:
        _err(
            "warning: %s sets disableAllHooks, so Claude Code runs none of these hooks until it is "
            "turned off" % claude_hooks.settings_path()
        )


def _relay_lines(change: claude_hooks.Change, verb: str) -> None:
    for path in change.relay_files:
        _out("  %s %s" % (("would be " + verb) if change.dry_run else verb, path))


def report_install(change: claude_hooks.Change) -> None:
    """What ``install`` changed, as ``hooks install`` and ``config set`` print it."""
    if not change.changed:
        _out("Claude Code hooks already installed in %s" % change.settings_path)
    else:
        lead = "Would install" if change.dry_run else "Installed"
        _out("%s the reply-language hooks in %s" % (lead, change.settings_path))
    for entry in change.added:
        _out("  + %s" % entry)
    for entry in change.removed:
        _out("  - %s" % entry)
    if change.added and claude_hooks.in_virtual_environment():
        _out("  (the Python this virtual environment was made from, not the environment's own)")
    _relay_lines(change, "written:")
    _backup_line(change)
    if change.changed and not change.dry_run:
        _out(RESTART_NOTE)
    _hooks_disabled_note()


def report_uninstall(change: claude_hooks.Change) -> None:
    if not change.changed:
        _out("No reply-language hooks to remove in %s" % change.settings_path)
    else:
        lead = "Would remove" if change.dry_run else "Removed"
        _out("%s the reply-language hooks from %s" % (lead, change.settings_path))
    for entry in change.removed:
        _out("  - %s" % entry)
    _relay_lines(change, "deleted:")
    _backup_line(change)


def _backup_line(change: claude_hooks.Change) -> None:
    if change.backup:
        _out("  previous file kept as %s" % change.backup)
    elif change.dry_run and change.changed and os.path.isfile(change.settings_path):
        _out("  the previous file would be kept as %s" % (change.settings_path + claude_hooks.BACKUP_SUFFIX))


def cmd_hooks_install(args: argparse.Namespace) -> int:
    try:
        change = claude_hooks.install(hosts.PLUGIN_ROOT, dry_run=args.dry_run)
    except claude_hooks.HooksError as exc:
        _err(str(exc))
        return 2
    report_install(change)
    readable, tag = claude_hooks.configured_reply(args.cwd)
    if readable and not tag:
        _out("note: %s" % SILENT_NOTE)
    return 0


def cmd_hooks_uninstall(args: argparse.Namespace) -> int:
    try:
        change = claude_hooks.uninstall(dry_run=args.dry_run)
    except claude_hooks.HooksError as exc:
        _err(str(exc))
        return 2
    report_uninstall(change)
    return 0


def status_lines(report: Dict[str, Any]) -> List[str]:
    """``hooks status`` as text, without the advice."""
    state = report["status"]
    if report.get("reasons"):
        state += " (%s)" % ", ".join(report["reasons"])
    lines = ["Claude Code settings: %s" % report["settings_path"], "Status: %s" % state]
    if report.get("error"):
        lines.append("  %s" % report["error"])
    if report.get("events"):
        lines.append("Events: %s" % ", ".join(report["events"]))
    if report.get("python"):
        lines.append("Python: %s" % report["python"])
    lines.append("Relay: %s" % report["relay"])
    if report.get("plugin_root"):
        lines.append("Plugin checkout: %s" % report["plugin_root"])
    if report.get("hooks_disabled"):
        lines.append("note: disableAllHooks is set; Claude Code runs no hooks")
    return lines


def cmd_hooks_status(args: argparse.Namespace) -> int:
    report = claude_hooks.status(hosts.PLUGIN_ROOT)
    if args.json:
        _emit_json(report)
        return 0
    for line in status_lines(report):
        _out(line)
    _readable, tag = claude_hooks.configured_reply(args.cwd)
    state = report["status"]
    if tag and state in (claude_hooks.STATUS_STALE, claude_hooks.STATUS_NOT_INSTALLED):
        _out("language.reply is %s; run: %s" % (tag, claude_hooks.FIX_COMMAND))
    elif not tag and claude_hooks.installed(report):
        command = claude_hooks.UNINSTALL_COMMAND
        _out("note: no language.reply is set, so they stay silent; `%s` removes them" % command)
    return 0
