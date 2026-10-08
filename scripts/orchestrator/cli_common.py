"""Helpers every command module shares: output, loading, the workspace."""

from __future__ import annotations

import argparse
import codecs
import json
import os
import sys
from typing import Any, Callable, Dict, Optional, overload

from . import config as config_mod
from . import ledger as ledger_mod
from . import presets as presets_mod
from . import workflow as workflow_mod
from . import workspace as ws
from .providers import MODE_IMPLEMENT, MODE_PLAN
from .summary import render_summary

DEFAULT_MODES = {
    "orchestrator": MODE_PLAN,
    "architect": MODE_PLAN,
    "implementer": MODE_IMPLEMENT,
    "review_fixer": MODE_IMPLEMENT,
}


# --------------------------------------------------------------------------- helpers


#: Error handlers that already guarantee ``write`` cannot raise. Anything else
#: is either strict or one of CPython's own stdio defaults, which sound
#: forgiving and are not: ``surrogateescape`` and ``surrogatepass`` only rescue
#: lone surrogates, so a plain em dash still kills a cp932 console.
_TOLERANT_ERRORS = frozenset({"ignore", "replace", "backslashreplace", "xmlcharrefreplace", "namereplace"})


def _encodes_everything(stream: Any) -> bool:
    """True for a UTF stream, where no character can fail to encode."""
    try:
        return codecs.lookup(getattr(stream, "encoding", "") or "").name.startswith("utf")
    except (LookupError, TypeError):
        return False


def tolerate_console_encoding() -> None:
    """Stop an unencodable character from killing a finished run.

    A Japanese Windows console is cp932, and a delegated agent writes prose:
    one em dash in a summary and ``sys.stdout.write`` raises
    ``UnicodeEncodeError``. Every edit had already been applied, so the work
    was done and only the report of it was lost -- reported from real use.

    The first version of this only relaxed a ``strict`` stream, which is the
    handler Windows never actually uses: CPython gives ``sys.stdout`` the
    ``surrogateescape`` handler there. That is not a choice anybody made and it
    does not help with an em dash, so the guard skipped the one console the
    function existed for.

    ``backslashreplace`` rather than ``replace``: the output is read by the
    orchestrating agent as well as by a person, and ``\u2014`` says which
    character could not be shown while ``?`` throws it away. A stream that can
    encode anything is left as it is, and so is one whose handler someone chose
    deliberately -- those already cannot raise.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # a StringIO under test, or a plain wrapper
            continue
        if _encodes_everything(stream):
            continue
        if (getattr(stream, "errors", "") or "") in _TOLERANT_ERRORS:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):  # pragma: no cover - platform dependent
            pass


def _write(stream: Any, text: str) -> None:
    """Write ``text``, degrading characters rather than dropping the message.

    ``tolerate_console_encoding`` handles this for every stream that can be
    reconfigured. This is for the ones that cannot -- a wrapper someone else
    installed, a pipe already handed to us -- where the alternative is losing
    a whole report to one character in it.
    """
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        stream.write(text.encode(encoding, "backslashreplace").decode(encoding))


def _out(text: str = "") -> None:
    _write(sys.stdout, text + ("\n" if not text.endswith("\n") else ""))


def _err(text: str) -> None:
    _write(sys.stderr, text.rstrip() + "\n")


def _emit_json(data: Any) -> None:
    """``--json`` promises machine-readable output, so it must stay parseable.

    ``ensure_ascii=False`` reads better wherever the console can show the
    character, and on a console that cannot it hands the character to
    ``backslashreplace`` -- which spells it ``\\xe9``, an escape JSON does not
    define. That turns a loud ``UnicodeEncodeError`` into output that parses as
    nothing, or worse, parses wrong. Let JSON do the escaping instead: its own
    ``\\uXXXX`` is ASCII, survives any console, and reads back as the same
    string.
    """
    _out(json.dumps(data, indent=2, ensure_ascii=not _encodes_everything(sys.stdout)))


def _container_and_config(args: argparse.Namespace) -> "tuple[str, str, config_mod.LoadedConfig]":
    """The project root, its ``.ai/``, and the configuration they came from.

    The configuration is read from the working directory, as every other
    command reads it, not from ``root``: run from a subdirectory holding its
    own project file, ``.ai/`` used to follow the root's file while the run
    and its budgets followed the subdirectory's. The root is the one the
    configuration resolved its workspace in (``LoadedConfig.root``), which is
    also where what the project file may not set is reported against.
    """
    start = getattr(args, "cwd", None) or os.getcwd()
    loaded = config_mod.load(start, validate_result=False)
    return loaded.root, loaded.workspace_dir(), loaded


def _container(args: argparse.Namespace) -> "tuple[str, str]":
    """The project root and its ``.ai/``, before a workflow is chosen."""
    root, container, _ = _container_and_config(args)
    return root, container


#: How many quiet workflows the note names before it says "...".
_STALE_LISTED = 5


def _stale_notice(container: str, workflow: str, days: int) -> None:
    """Advisory only: whatever happens in here, the command goes on unchanged."""
    if days <= 0:
        return
    try:
        stale = workflow_mod.stale_elsewhere(container, workflow, days)
    except Exception:  # a sibling's file is not this command's problem
        return
    if not stale:
        return
    count = len(stale)
    names = ", ".join(stale[:_STALE_LISTED]) + (", ..." if count > _STALE_LISTED else "")
    subject = "workflow has" if count == 1 else "workflows have"
    unit = "day" if days == 1 else "days"
    _err(
        'note: %d %s not been active for %d %s or more (%s). See them with "workflow list"; '
        'remove one with "workflow remove <id> --yes".' % (count, subject, days, unit, names)
    )


def _workspace(args: argparse.Namespace) -> ws.Workspace:
    """The workspace for the workflow this invocation belongs to.

    Every command that uses a workflow goes through here: refuse a container
    still in the pre-0.4.0 layout before anything is written, resolve the id,
    and hand back a workspace pointed at that workflow's directory. The call
    that creates the directory is the start of a new workflow, and the one
    place the others that went quiet are noted.
    """
    root, container, loaded = _container_and_config(args)
    workflow_mod.refuse_legacy(container)
    requested = getattr(args, "workflow", "") or ""
    if getattr(args, "job_file", None):
        # A detached worker is told its parent's workflow; it does not get to
        # move the pointer, which may have moved on since the parent returned.
        workflow, _ = workflow_mod.resolve(container, requested)
    else:
        workflow = workflow_mod.ensure(container, requested)
    fresh = workflow_mod.create_dir(container, workflow)
    workspace = ws.Workspace(root, container, workflow).ensure()
    if fresh:
        _stale_notice(container, workflow, loaded.stale_notice_days())
        if loaded.used_defaults:
            _first_run_summary(loaded)
    return workspace


def _first_run_summary(loaded: config_mod.LoadedConfig) -> None:
    """The whole configuration, once, when a workflow starts with no config file.

    Nothing is saved and nothing is asked: these commands run under an agent
    with no terminal. The choice belongs to ``config setup``.
    """
    _err(render_summary(loaded.data, loaded=loaded))
    for note in loaded.preset_notes:
        _err("note: %s" % note)
    _err(
        "Save it with config setup --preset %s, or choose another with config setup."
        % (loaded.preset or presets_mod.DEFAULT)
    )


def _review_workspace(args: argparse.Namespace) -> ws.Workspace:
    """The workspace a ``review`` subcommand reads and writes.

    ``--design`` points it at ``reviews/design/``, which is the whole of what
    makes the design review a separate review: its own reports, its own
    consolidated file, and therefore its own round counter and triage.
    """
    workspace = _workspace(args)
    if getattr(args, "design", False):
        return workspace.design_review().ensure()
    return workspace


@overload
def _in_workflow(workspace: ws.Workspace, path: str) -> str: ...
@overload
def _in_workflow(workspace: ws.Workspace, path: Optional[str]) -> Optional[str]: ...
def _in_workflow(workspace: ws.Workspace, path: Optional[str]) -> Optional[str]:
    """Resolve a path written as ``.ai/...`` inside this workflow's directory.

    Every documented command names artifacts by their container-relative path
    -- ``--output .ai/plan.md`` -- which was unambiguous while there was one
    directory per project. It now means "the plan *of this workflow*", so the
    container prefix is rewritten to the workflow's own directory.

    Left exactly as written: anything outside the container, and anything that
    already names a workflow. The second is what keeps a detached worker from
    mapping a path its parent has already mapped.
    """
    if not path or path == "-" or not workspace.workflow:
        return path
    try:
        relative = os.path.relpath(os.path.abspath(path), workspace.container)
    except ValueError:  # pragma: no cover - different drives on Windows
        return path
    parts = relative.replace("\\", "/").split("/")
    if parts[0] in ("..", ws.WORKFLOWS):
        return path
    return os.path.join(workspace.dir, *parts)


def _load_or_die(start: Optional[str] = None) -> config_mod.LoadedConfig:
    try:
        loaded = config_mod.load(start)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        _err("Run `dev-orchestra config setup` to rebuild the configuration.")
        raise SystemExit(2) from exc
    if loaded.used_defaults:
        # One line: every `run` is its own process, and a workflow has many.
        _err(
            "note: no config file; running preset %s fitted to %s (config setup --preset <name> saves one)"
            % (loaded.preset, presets_mod.describe_installed(presets_mod.installed_providers()))
        )
    return loaded


def _load_lenient(start: Optional[str] = None) -> config_mod.LoadedConfig:
    """The configuration as its files say, *not validated*: for reporting and display only.

    ``validate_result=False`` skips ``config.validate`` and with it the
    refusals a project file is held to. Anything that starts a provider run
    loads through :func:`_load_or_die`.
    """
    return config_mod.load(start, validate_result=False)


def _ledger(args: argparse.Namespace, workspace: Optional[ws.Workspace] = None) -> ledger_mod.Ledger:
    workspace = workspace or _workspace(args)
    loaded = config_mod.load(getattr(args, "cwd", None), validate_result=False)
    return ledger_mod.Ledger(workspace, ledger_mod.budget_settings(loaded.data))


def _refuse_if_exhausted(book: ledger_mod.Ledger, stage: str, force: bool) -> Optional[int]:
    """Stop a loop at the action, not with advice from a query command."""
    if force:
        return None
    reasons = book.check(stage)
    if not reasons:
        return None
    _err("refusing to run %s:" % stage)
    for reason in reasons:
        _err("  - %s" % reason)
    _err("Report what is unresolved instead of retrying, or pass --force to override.")
    return ledger_mod.EXIT_BUDGET_EXHAUSTED


def _wrote_plan(workspace: ws.Workspace) -> Callable[[Dict[str, Any]], bool]:
    """Whether an architect run event wrote its answer to this workflow's plan.

    Only such a run is a revision of the plan: an architect run asked
    something else, or left on stdout, is not the revision a round is owed.
    """
    # Resolved, not just made absolute: one plan has more than one spelling
    # when a directory on its path is a link -- macOS's temporary directories
    # live under /var, which is /private/var -- and a correct --output must not
    # be refused for the spelling it was given in.
    plan = os.path.normcase(os.path.realpath(workspace.plan_path))

    def counts(event: Dict[str, Any]) -> bool:
        output = event.get("output")
        if not isinstance(output, str) or not output or output == "-":
            return False
        return os.path.normcase(os.path.realpath(_in_workflow(workspace, output) or output)) == plan

    return counts
