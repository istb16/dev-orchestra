"""The settings a project file is not trusted with.

The project file can be committed, and it is read without asking, so it can
come with the branch under review. Two settings would let such a branch wave
its own plan through: ``design.require_approval``, the user's say before
anything is implemented, and a ``workspace.dir`` outside the repository,
which moves the approval record -- ``state.json`` and the plan beside it -- to
wherever the file points. Both are taken from the global config alone:
``config.trusted_project_layer`` merges the project layer without them, and
``config_policy`` says what was left out. A ``workspace.dir`` that leaves
only through a link in the repository needs the repository to tell, so it
is dropped only where one is given (``ignored``'s ``root``).

Nothing from this package is imported here: ``config`` asks while composing,
and the reply-language hook on every prompt.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
from typing import Any, Dict, List, Optional, Tuple

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
    # By Windows' rules on every system: the file may be read on either.
    drive, rest = ntpath.splitdrive(directory)
    if drive or os.path.isabs(directory) or rest.startswith(("/", "\\")):
        return True
    parts = posixpath.normpath(directory.replace("\\", "/")).split("/")
    return parts[0] == ".."


def linked_outside(root: str, directory: Any) -> bool:
    """Whether a relative ``workspace.dir`` leaves ``root`` once links are followed.

    ``outside_repository`` reads the text; this follows a symlink or a
    junction on the way, which a branch can commit as easily as the file
    naming it: ``dir: inner`` with ``inner`` a link to ``/srv/ai`` is the same
    move. Only for a value whose text stays inside, and only for ``root``,
    the repository a command runs in.
    """
    if not isinstance(directory, str) or not directory.strip() or outside_repository(directory):
        return False
    real_root = os.path.normcase(os.path.realpath(root))
    real = os.path.normcase(os.path.realpath(os.path.join(root, directory)))
    try:
        return os.path.commonpath([real_root, real]) != real_root
    except ValueError:  # another drive
        return True


def ignored(project_layer: Dict[str, Any], root: Optional[str] = None) -> List[Tuple[str, Any]]:
    """``(dotted key, value)`` for each setting the project layer may not make.

    A ``null`` approval is not one: it means the default, which is required,
    and cannot turn the gate off. A ``workspace.dir`` inside the repository
    stays the project's to choose. With ``root``, the repository a command
    runs in, a ``workspace.dir`` a link takes out of it is one too
    (``linked_outside``); without, only the text is read.
    """
    found: List[Tuple[str, Any]] = []
    design = project_layer.get("design")
    if isinstance(design, dict) and design.get("require_approval") is not None:
        found.append((APPROVAL, design["require_approval"]))
    workspace = project_layer.get("workspace")
    directory = workspace.get("dir") if isinstance(workspace, dict) else None
    if outside_repository(directory) or (root is not None and linked_outside(root, directory)):
        found.append((WORKSPACE_DIR, directory))
    return found


def without_ignored(project_layer: Dict[str, Any], root: Optional[str] = None) -> Dict[str, Any]:
    """``project_layer`` less what ``ignored`` names; the same object when that is nothing."""
    keys = {key for key, _value in ignored(project_layer, root)}
    if not keys:
        return project_layer
    kept = dict(project_layer)
    for key in keys:
        block, _, name = key.partition(".")
        kept[block] = {k: v for k, v in kept[block].items() if k != name}
    return kept


def written_ignored(dotted: str, value: Any) -> List[Tuple[str, Any]]:
    """What ``ignored`` would drop of ``config set --scope project <dotted> <value>``.

    Read from the write alone, so a whole block counts as well as one key:
    ``design`` set to ``{require_approval: false}`` is the same write as
    ``design.require_approval`` set to ``false``. A path into a list holds
    neither setting.
    """
    if "[" in dotted:
        return []
    layer: Dict[str, Any] = {}
    *blocks, last = dotted.split(".")
    node = layer
    for block in blocks:
        node = node.setdefault(block, {})
    node[last] = value
    return ignored(layer)
