"""Helpers every command module shares: output, loading, the workspace."""

from __future__ import annotations

import argparse
import codecs
import copy
import json
import os
import sys
from typing import Any, Dict, Optional, Tuple, overload

from . import config as config_mod
from . import workflow as workflow_mod
from . import workspace as ws
from .providers import MODE_IMPLEMENT, MODE_PLAN

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


def _resolve_scope(requested: Optional[str], start: Optional[str] = None) -> str:
    if requested in ("global", "project"):
        return requested
    return "project" if config_mod.find_project_config(start) else "global"


def _layer_path(scope: str, start: Optional[str] = None) -> str:
    return config_mod.global_config_path() if scope == "global" else config_mod.project_config_path(start)


def _read_layer(scope: str, start: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """The layer a writer is about to edit: what is on disk, and nothing else.

    The global layer used to be seeded with the whole of ``default_config()``,
    so the first ``config set`` froze every default beside the one value that
    was asked for. ``version`` identifies the file format rather than
    configuring anything, so it is the one key a writer supplies -- including
    into a file that predates it, which is the only way an edit to such a file
    can leave a file this loader's contract describes.
    """
    path = _layer_path(scope, start)
    layer = config_mod.read_config_file(path) if os.path.isfile(path) else {}
    layer.setdefault("version", config_mod.CONFIG_VERSION)
    return path, layer


def _layer_base(scope: str, start: Optional[str] = None) -> Dict[str, Any]:
    """What would be in force if this layer did not exist.

    The global file is shared by every project on the machine, so no project's
    values may flow into it: its base is the built-in defaults alone. A project
    file sits on top of the global one, so its base is the defaults plus that.
    ``load()`` is the wrong answer for either -- it includes the layer being
    edited, and for the global layer it includes whichever project happens to
    be the working directory.

    This mirrors the layer order in ``config.load``; a third layer would have
    to be added in both places.
    """
    defaults = config_mod.default_config()
    if scope == "global":
        return defaults
    global_path = config_mod.global_config_path()
    if not os.path.isfile(global_path):
        return defaults
    return config_mod.deep_merge(defaults, config_mod.read_config_file(global_path))


#: Sentinel for "the layer holds nothing here at all", which ``get_path`` cannot
#: otherwise distinguish from a value that happens to be falsy.
_UNSET = object()


def _seed_list(layer: Dict[str, Any], list_path: str, base: Dict[str, Any]) -> None:
    """Give the layer the whole list before one entry of it is edited.

    A list replaces the one below it wholesale (``deep_merge``), so changing
    one entry is also a decision about the others: they have to be copied from
    what this layer was inheriting, which is ``base`` and never the effective
    configuration -- a global file must not end up holding the panel of
    whichever project the command was run in.

    A path neither side knows is left alone, for ``set_path`` to reject as it
    always has -- and so is one this layer already holds something else at. A
    scalar where a list is expected is the same kind of mistake, and seeding
    over it would replace a value the user wrote instead of refusing.
    """
    if config_mod.get_path(layer, list_path, _UNSET) is not _UNSET:
        return
    inherited = config_mod.get_path(base, list_path)
    if isinstance(inherited, list):
        config_mod.set_path(layer, list_path, copy.deepcopy(inherited))


def _effective_preview(scope: str, layer: Dict[str, Any], start: Optional[str] = None) -> Dict[str, Any]:
    """What ``load()`` will resolve once this layer is saved."""
    return config_mod.deep_merge(_layer_base(scope, start), layer)


def _container(args: argparse.Namespace) -> "tuple[str, str]":
    """The project root and its ``.ai/``, before a workflow is chosen."""
    root = ws.repo_root(getattr(args, "cwd", None) or os.getcwd())
    loaded = config_mod.load(root, validate_result=False)
    return root, loaded.workspace_dir(root)


def _workspace(args: argparse.Namespace) -> ws.Workspace:
    """The workspace for the workflow this invocation belongs to.

    Every command goes through here, which is why the layout change is one
    function: resolve the id, adopt any pre-0.4.0 artifacts into it, and hand
    back a workspace pointed at that workflow's directory.
    """
    root, container = _container(args)
    workflow = workflow_mod.ensure(container, getattr(args, "workflow", "") or "")
    moved = workflow_mod.migrate(container, workflow)
    if moved:
        _err(
            "Adopted the previous %s into workflow %s: %s"
            % (os.path.basename(container), workflow, ", ".join(moved))
        )
    return ws.Workspace(root, container, workflow).ensure()


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
        return config_mod.load(start)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        _err("Run `dev-orchestra config setup` to rebuild the configuration.")
        raise SystemExit(2) from exc
