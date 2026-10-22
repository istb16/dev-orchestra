"""Bringing a work unit's result back into the user's working tree.

A unit that ran in a worktree left two trees: ``base_tree``, the user's tree
as it was when the unit started, and ``result_tree``, the worktree as the
unit left it. Each file the unit changed is merged three ways against the
file as it is in the user's tree now, one file at a time with
``git merge-file`` on temporary copies, so only the working tree is touched:
never the index, HEAD or a branch. ``git apply --3way`` writes the index, and
``git merge-tree --write-tree`` needs git 2.38.

Every file's result is worked out before any is written, and a write that
fails puts back the files written before it, so a merge that fails part way
leaves the tree as it was.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from typing import Dict, List, NamedTuple, Optional, Sequence

from . import atomic_file
from . import units as units_mod
from . import workspace as ws
from . import worktrees as worktrees_mod

#: What a file's merge does to the working tree.
WRITE = "write"  # the unit's version, or a clean three-way merge
DELETE = "delete"  # the unit deleted it, and the user's tree had not changed it
SAME = "same"  # the user's tree already holds the unit's version
CONFLICT = "conflict"

#: The labels ``git merge-file`` puts on the conflict markers.
MAIN_LABEL = "main"
UNIT_LABEL = "unit:%s"

_ABSENT_MODE = "000000"
_LINK_MODE = "120000"
_SUBMODULE_MODE = "160000"
_EXECUTABLE_MODE = "100755"

#: How much of a file is looked at for a NUL byte, as git does.
_BINARY_PROBE = 8000

_IS_WINDOWS = sys.platform.startswith("win")


class MergeError(RuntimeError):
    """A merge git could not work out; nothing was written."""


class FileMerge(NamedTuple):
    """One file's merge, worked out before anything is written."""

    #: As git names it, ``/``-separated and relative to the root.
    path: str
    action: str
    #: What the working file becomes: for a conflict, the text with markers,
    #: or None when the user's version is kept as it is.
    content: Optional[bytes]
    #: The working file as it was read, None for no file: checked again before writing.
    current: Optional[bytes]
    #: The unit's git mode for the file, ``000000`` when it deleted it.
    mode: str
    #: The unit's version, saved under ``theirs/`` when the user's is kept.
    theirs: Optional[bytes]
    #: Why the file conflicts; "" when it does not.
    reason: str


def _raw_changes(
    root: str, base: str, result: str, excludes: Sequence[str]
) -> List["tuple[str, str, str, str, str]"]:
    """(path, base mode, unit mode, base blob, unit blob) for each path the unit changed."""
    pathspecs = [":(exclude)%s" % path for path in excludes]
    code, out, err = ws.git_bytes(
        ["diff", "--raw", "-z", "--no-renames", "--no-abbrev", base, result, "--", ".", *pathspecs], root
    )
    if code != 0:
        raise MergeError("git diff %s %s failed: %s" % (base[:12], result[:12], err.strip()))
    fields = out.split(b"\0")
    changes = []
    index = 0
    while index + 1 < len(fields) and fields[index].startswith(b":"):
        old_mode, new_mode, old_blob, new_blob, _ = fields[index][1:].decode("ascii").split(" ")
        path = os.fsdecode(fields[index + 1])
        changes.append((path, old_mode, new_mode, old_blob, new_blob))
        index += 2
    return changes


def changed_paths(root: str, base: str, result: str, excludes: Sequence[str] = ()) -> List[str]:
    """The paths a unit changed between its two trees."""
    return [change[0] for change in _raw_changes(root, base, result, excludes)]


def outside_files(files: Sequence[str], paths: Sequence[str]) -> List[str]:
    """The ``paths`` none of a unit's ``files`` entries names."""
    return [path for path in paths if not any(units_mod.overlaps(path, entry) for entry in files)]


def _blob(root: str, path: str, blob: str, checkout: bool = False) -> bytes:
    """A blob as git stores it, or with ``checkout`` as it is checked out at ``path``.

    Checked out, its filters and line endings are applied: what a file the
    unit's version replaces should hold.
    """
    args = ["cat-file", "--filters", "--path=%s" % path, blob] if checkout else ["cat-file", "blob", blob]
    code, out, err = ws.git_bytes(args, root)
    if code != 0:
        raise MergeError("git cat-file %s (%s) failed: %s" % (blob[:12], path, err.strip()))
    return out


def _stored(root: str, path: str) -> str:
    """The blob the working file at ``path`` is stored as, written so it can be read back.

    Compared by blob, the user's file and the unit's trees agree on line
    endings: under ``core.autocrlf`` a file checked out with CRLF is stored
    with LF, as the trees hold it.
    """
    code, out, err = ws.git_bytes(["hash-object", "-w", "--", path], root)
    if code != 0:
        raise MergeError("git hash-object %s failed: %s" % (path, err.strip()))
    return out.decode("ascii", errors="replace").strip()


def _is_binary(*versions: Optional[bytes]) -> bool:
    return any(version is not None and b"\0" in version[:_BINARY_PROBE] for version in versions)


def _merge_text(root: str, name: str, current: bytes, base: bytes, unit: bytes) -> "tuple[bytes, bool]":
    """(merged text, whether it holds conflict markers) from ``git merge-file -p``."""
    with tempfile.TemporaryDirectory(prefix="dev-orchestra-merge-") as scratch:
        paths = []
        for label, content in (("main", current), ("base", base), ("unit", unit)):
            path = os.path.join(scratch, label)
            with open(path, "wb") as handle:
                handle.write(content)
            paths.append(path)
        labels = ["-L", MAIN_LABEL, "-L", "base", "-L", UNIT_LABEL % name]
        code, out, err = ws.git_bytes(["merge-file", "-p", *labels, *paths], root)
    # The number of conflicts, at most 127; anything else is git failing, and
    # so is a conflict with no output, which is how a git that never started looks.
    if code < 0 or code > 127 or (code and not out):
        raise MergeError("git merge-file failed: %s" % err.strip())
    return out, code > 0


def _unit_copy(root: str, path: str, mode: str, blob: str) -> Optional[bytes]:
    """The unit's version of a file whose working copy is kept; None when it has none to save."""
    if mode in (_ABSENT_MODE, _SUBMODULE_MODE):
        return None
    return _blob(root, path, blob, checkout=mode != _LINK_MODE)


def _linked_parent(root: str, path: str) -> bool:
    """Whether a directory above ``path`` is a link, or the path leads out of ``root``.

    A write through such a directory would land wherever it points, outside
    the repository.
    """
    parts = path.split("/")
    for depth in range(1, len(parts)):
        if worktrees_mod.is_link(os.path.join(root, *parts[:depth])):
            return True
    inside = os.path.realpath(os.path.join(root, *parts))
    return not inside.startswith(os.path.join(os.path.realpath(root), ""))


def _merge_file(root: str, name: str, change: "tuple[str, str, str, str, str]") -> FileMerge:
    path, base_mode, unit_mode, base_blob, unit_blob = change
    full = os.path.join(root, path)
    if _linked_parent(root, path):
        return FileMerge(
            path,
            CONFLICT,
            None,
            None,
            unit_mode,
            _unit_copy(root, path, unit_mode, unit_blob),
            "a directory above it is a link in the working tree",
        )
    if os.path.islink(full) or (os.path.lexists(full) and not os.path.isfile(full)):
        return FileMerge(
            path,
            CONFLICT,
            None,
            None,
            unit_mode,
            _unit_copy(root, path, unit_mode, unit_blob),
            "not a regular file in the working tree",
        )
    current = atomic_file.current_bytes(full)
    for mode in (base_mode, unit_mode):
        if mode in (_LINK_MODE, _SUBMODULE_MODE):
            kind = "a symlink" if mode == _LINK_MODE else "a submodule"
            theirs = _blob(root, path, unit_blob) if unit_mode == _LINK_MODE else None
            return FileMerge(path, CONFLICT, None, current, unit_mode, theirs, kind)
    base_id = None if base_mode == _ABSENT_MODE else base_blob
    unit_id = None if unit_mode == _ABSENT_MODE else unit_blob
    current_id = None if current is None else _stored(root, path)
    if current_id == base_id:
        unit = None if unit_id is None else _blob(root, path, unit_id, checkout=True)
        return FileMerge(path, WRITE if unit is not None else DELETE, unit, current, unit_mode, None, "")
    if current_id == unit_id:
        return FileMerge(path, SAME, None, current, unit_mode, None, "")
    if unit_id is None:
        return FileMerge(
            path, CONFLICT, None, current, unit_mode, None, "the unit deleted it, the working tree changed it"
        )
    theirs = _blob(root, path, unit_id, checkout=True)
    if current is None:
        return FileMerge(
            path,
            CONFLICT,
            None,
            current,
            unit_mode,
            theirs,
            "the working tree deleted it, the unit changed it",
        )
    # All three as checked out, like the working file, so the merged text is
    # written as it is: in the same line endings as a file the unit's version replaces.
    mine = current
    base = b"" if base_id is None else _blob(root, path, base_id, checkout=True)
    unit = theirs
    if _is_binary(mine, base, unit):
        return FileMerge(path, CONFLICT, None, current, unit_mode, theirs, "a binary file")
    merged, conflicted = _merge_text(root, name, mine, base, unit)
    if conflicted:
        return FileMerge(path, CONFLICT, merged, current, unit_mode, None, "conflict markers in the file")
    return FileMerge(path, WRITE, merged, current, unit_mode, None, "")


def plan_merge(root: str, name: str, base: str, result: str, excludes: Sequence[str] = ()) -> List[FileMerge]:
    """Each changed file's merge into the working tree at ``root``, nothing written.

    Against the file as it is now: the same as the base, it takes the unit's
    version; the same as the unit's, nothing; both changed text, a three-way
    merge; anything else -- a binary, a delete against a change, a symlink or
    a submodule -- a conflict that keeps the user's version. Raises
    ``MergeError`` when git cannot say.
    """
    return [_merge_file(root, name, change) for change in _raw_changes(root, base, result, excludes)]


def _set_mode(path: str, mode: str) -> None:
    """The unit's executable bit, on POSIX; Windows has none to set."""
    if _IS_WINDOWS:
        return
    mask = os.umask(0)
    os.umask(mask)
    os.chmod(path, (0o777 if mode == _EXECUTABLE_MODE else 0o666) & ~mask)


def theirs_path(workspace: ws.Workspace, name: str, path: str) -> str:
    """Where a conflicting file's unit version is kept: ``units/<name>/theirs/<path>``."""
    return os.path.join(workspace.units_dir, name, "theirs", *path.split("/"))


def _mode_of(path: str) -> Optional[int]:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def _undo(done: Sequence["tuple[str, Optional[bytes], Optional[int]]"]) -> None:
    """Put back each working file ``apply`` changed, last first, as far as it can."""
    for full, previous, mode in reversed(done):
        try:
            if previous is None:
                if os.path.lexists(full):
                    os.unlink(full)
                continue
            atomic_file.write(full, previous)
            if mode is not None:
                os.chmod(full, mode)
        except OSError:
            pass


def apply(workspace: ws.Workspace, name: str, results: Sequence[FileMerge]) -> None:
    """Write ``plan_merge``'s results into the working tree, and the kept unit versions beside it.

    Raises ``atomic_file.Raced`` when a file changed after it was read, or
    ``OSError``; the working files already written are put back first, so
    the tree is left as it was.
    """
    for result in results:
        if result.theirs is not None:
            atomic_file.write(theirs_path(workspace, name, result.path), result.theirs)
    done: List["tuple[str, Optional[bytes], Optional[int]]"] = []
    try:
        for result in results:
            full = os.path.join(workspace.root, result.path)
            if result.action == DELETE:
                if atomic_file.current_bytes(full) != result.current:
                    raise atomic_file.Raced(full)
                mode = _mode_of(full)
                os.unlink(full)
                done.append((full, result.current, mode))
            elif result.content is not None:
                mode = _mode_of(full)
                atomic_file.write(full, result.content, keep_mode=True, expected=result.current)
                done.append((full, result.current, mode))
                if result.action == WRITE:
                    _set_mode(full, result.mode)
    except BaseException:
        _undo(done)
        raise


def conflict_markers(name: str) -> "tuple[bytes, bytes]":
    """The opening and closing marker lines ``git merge-file`` writes for unit ``name``."""
    return (b"<<<<<<< " + MAIN_LABEL.encode("utf-8"), b">>>>>>> " + (UNIT_LABEL % name).encode("utf-8"))


def markers_left(root: str, name: str, paths: Sequence[str]) -> List[str]:
    """The ``paths`` that still hold a conflict marker line of unit ``name``."""
    opening, closing = conflict_markers(name)
    found = []
    for path in paths:
        content = atomic_file.current_bytes(os.path.join(root, path)) or b""
        lines = content.splitlines()
        if any(line.startswith(opening) or line.startswith(closing) for line in lines):
            found.append(path)
    return found


def write_brief(
    workspace: ws.Workspace,
    name: str,
    conflicts: Sequence[FileMerge],
    plan_text: str,
    touched_by: Dict[str, List[str]],
) -> str:
    """Write ``units/<name>-conflicts.md`` for the agent that resolves the conflicts; its path.

    ``touched_by`` maps each earlier-merged unit that changed one of the
    conflicting files to those files. The brief holds the files, the plan's
    goal, the plan sections of this unit and of each of those, and where the
    unit's version of a file whose user version was kept is.
    """
    lines = [
        "# Merge conflicts: unit %s" % name,
        "",
        "Unit %s was merged into the working tree, and these files conflict with what" % name,
        "the working tree already holds -- the user's edits, or units merged earlier.",
        "Resolve each one so both sides' intent is kept, and change nothing else.",
        "Remove every `<<<<<<< %s` / `=======` / `>>>>>>> %s` marker." % (MAIN_LABEL, UNIT_LABEL % name),
        "",
        "## Conflicting files",
    ]
    for result in conflicts:
        line = "- `%s`: %s" % (result.path, result.reason)
        if result.theirs is not None:
            saved = workspace.relative(theirs_path(workspace, name, result.path))
            line += "; the working tree's version is kept, the unit's is at `%s`" % saved
        lines.append(line)
    goal = units_mod.goal(plan_text)
    if goal:
        lines += ["", "## Goal of the plan", "", goal.rstrip()]
    lines += ["", "## This unit", "", (units_mod.section(plan_text, name) or "(no section)\n").rstrip()]
    for other, files in touched_by.items():
        lines += [
            "",
            "## Unit %s, merged earlier (also changed %s)" % (other, ", ".join("`%s`" % f for f in files)),
            "",
            (units_mod.section(plan_text, other) or "(no section)\n").rstrip(),
        ]
    path = os.path.join(workspace.units_dir, "%s-conflicts.md" % name)
    ws.write_text(path, "\n".join(lines) + "\n")
    return path
