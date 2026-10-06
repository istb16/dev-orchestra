"""The settings a project file is not trusted with.

The project file can be committed, and it is read without asking, so it can
come with the branch under review. Two settings would let such a branch wave
its own plan through: ``design.require_approval``, the user's say before
anything is implemented, and a ``workspace.dir`` outside the repository,
which moves the approval record -- ``state.json`` and the plan beside it -- to
wherever the file points. Both are taken from the global config alone:
``config.compose`` merges the project layer without them, and
``config_policy`` says what was left out.

Nothing from this package is imported here: ``config`` asks while composing,
and the reply-language hook on every prompt.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

APPROVAL = "design.require_approval"
WORKSPACE_DIR = "workspace.dir"


def outside_repository(directory: Any) -> bool:
    """Whether a ``workspace.dir`` would put the workspace outside the repository.

    Absolute, rooted (``\\x``, which Windows resolves on the current drive),
    drive-relative (``C:x``), or climbing out with ``..``. Read from the text
    alone: the value is joined to whichever repository a command runs in, so
    the same value is inside or outside of every one of them alike. Anything
    but a non-empty string is ``validate``'s to report.
    """
    if not isinstance(directory, str) or not directory:
        return False
    drive, rest = os.path.splitdrive(directory)
    if drive or os.path.isabs(directory) or rest.startswith(("/", "\\")):
        return True
    parts = os.path.normpath(directory).replace("\\", "/").split("/")
    return parts[0] == ".."


def ignored(project_layer: Dict[str, Any]) -> List[Tuple[str, Any]]:
    """``(dotted key, value)`` for each setting the project layer may not make.

    A ``null`` approval is not one: it means the default, which is required,
    and cannot turn the gate off. A ``workspace.dir`` inside the repository
    stays the project's to choose.
    """
    found: List[Tuple[str, Any]] = []
    design = project_layer.get("design")
    if isinstance(design, dict) and design.get("require_approval") is not None:
        found.append((APPROVAL, design["require_approval"]))
    workspace = project_layer.get("workspace")
    if isinstance(workspace, dict) and outside_repository(workspace.get("dir")):
        found.append((WORKSPACE_DIR, workspace["dir"]))
    return found


def without_ignored(project_layer: Dict[str, Any]) -> Dict[str, Any]:
    """``project_layer`` less what ``ignored`` names; the same object when that is nothing."""
    keys = {key for key, _value in ignored(project_layer)}
    if not keys:
        return project_layer
    kept = dict(project_layer)
    for key in keys:
        block, _, name = key.partition(".")
        kept[block] = {k: v for k, v in kept[block].items() if k != name}
    return kept


def refused_write(dotted: str, value: Any) -> bool:
    """Whether ``config set --scope project <dotted> <value>`` would write a value ``ignored`` drops."""
    if dotted == APPROVAL:
        return value is not None
    return dotted == WORKSPACE_DIR and outside_repository(value)
