"""Git worktrees for the work units that run side by side.

A unit that could run beside another gets a worktree of its own, made from
the user's working tree as it is -- uncommitted edits and untracked files
included -- so the agent sees what the user sees, and nothing it does reaches
the user's tree until ``units merge`` brings it back.

The worktree is made without touching the user's index, HEAD or branches: the
tree is written through a throwaway index (``ws.write_tree``), wrapped in an
unreferenced commit, and checked out detached at
``<parent>/<repo>.units/<id8>-<name>``.

Removing one is careful about links. The environment directories linked into
it (``.venv`` and the like) point into the user's tree, and a removal that
followed one would delete the user's environment. Every link is unlinked
first, without following it, and any directory left after
``git worktree remove`` is deleted by a walker that never descends a link.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import time
from typing import Any, Dict, List, Sequence

from . import workspace as ws

#: The ignored environment directories a worktree borrows from the user's tree.
LINKED_NAMES = (".venv", "venv", "node_modules")

#: Who the base commit is by: fixed, so it never depends on the user's identity.
_IDENTITY = {
    "GIT_AUTHOR_NAME": "dev-orchestra",
    "GIT_AUTHOR_EMAIL": "dev-orchestra@localhost",
    "GIT_COMMITTER_NAME": "dev-orchestra",
    "GIT_COMMITTER_EMAIL": "dev-orchestra@localhost",
}

#: ``git worktree remove`` is tried this many times, backing off between tries:
#: an editor indexer or a virus scanner can hold a file open for a moment.
_REMOVE_TRIES = 5
_REMOVE_BACKOFF = (0.2, 2.0)

_IS_WINDOWS = sys.platform.startswith("win")


class WorktreeError(RuntimeError):
    """A worktree that could not be made."""


def excludes(workspace: ws.Workspace, links: Sequence[str] = ()) -> List[str]:
    """What a unit's trees leave out: the workspace, and the names linked into its worktree."""
    found: List[str] = []
    container = workspace.relative(workspace.container)
    if container and container != "." and not container.startswith(".."):
        found.append(container)
    return found + [name for name in links if name not in found]


def location(root: str, base_commit: str, name: str) -> str:
    """``<parent>/<repo>.units/<id8>-<name>``: beside the repository, never inside it."""
    parent, repo = os.path.split(os.path.abspath(root))
    return os.path.join(parent, "%s.units" % repo, "%s-%s" % (base_commit[:8], name))


def _ignored(root: str, name: str) -> bool:
    for path in (name, name + "/"):
        code, _, _ = ws.git(["check-ignore", "-q", "--", path], root)
        if code == 0:
            return True
    return False


def _link(target: str, link: str) -> None:
    """A directory link at ``link`` to ``target``: a junction on Windows, a symlink elsewhere.

    Raises ``OSError`` when neither can be made. No ``cmd /c mklink``
    fallback: the paths come from the repository, and ``cmd.exe`` would read
    a ``&`` in one as another command.
    """
    if not _IS_WINDOWS:
        os.symlink(target, link, target_is_directory=True)
        return
    try:
        # Windows only: _winapi exists nowhere else.
        import _winapi

        _winapi.CreateJunction(target, link)
    except (ImportError, AttributeError) as exc:
        raise OSError("could not link %s to %s: %s" % (link, target, exc)) from None


def _copy_missing(source: str, target: str) -> None:
    if os.path.isfile(source) and not os.path.lexists(target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # With the source's mode: settings.local.json can hold tokens.
        shutil.copy2(source, target)


def _units_dir(path: str) -> None:
    """Make ``<repo>.units``, the parent of worktree ``path``, for this user alone.

    Raises ``WorktreeError`` when it is a link, or on POSIX another user's:
    what is copied into a worktree must not be readable by someone else. One
    that already exists open to others is closed to them.
    """
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if ws.is_link(parent):
        raise WorktreeError("%s is a link; refusing to make a worktree in it" % parent)
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        info = os.stat(parent)
        if info.st_uid != getuid():
            raise WorktreeError("%s belongs to another user; refusing to make a worktree in it" % parent)
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(parent, 0o700)


def create(workspace: ws.Workspace, name: str, copies: Sequence[str] = ()) -> Dict[str, Any]:
    """Make unit ``name``'s worktree; its fields for the unit's record.

    ``worktree``, ``base_tree``, ``base_commit``, ``links`` (the names linked
    in) and ``checks_skipped`` (a link failed, so the unit's own checks would
    run without its environment). Raises ``WorktreeError`` when git cannot
    make it; nothing is left behind then.

    ``copies`` are repository-relative files it is given a copy of when the
    user's tree has them, beside the plan and the decisions; an absolute one,
    or one outside the repository, is skipped.
    """
    root = workspace.root
    wanted = [
        entry for entry in LINKED_NAMES if os.path.isdir(os.path.join(root, entry)) and _ignored(root, entry)
    ]
    base_tree = ws.write_tree(root, excludes(workspace, wanted))
    if not base_tree:
        raise WorktreeError("git could not record the working tree")
    code, head, _ = ws.git(["rev-parse", "--verify", "-q", "HEAD"], root)
    parents = ["-p", head.strip()] if code == 0 and head.strip() else []
    code, out, err = ws.git(
        ["commit-tree", "--no-gpg-sign", base_tree, *parents, "-m", _commit_message(name)],
        root,
        env=_IDENTITY,
    )
    base_commit = out.strip()
    if code != 0 or not base_commit:
        raise WorktreeError("git commit-tree failed: %s" % err.strip())
    path = location(root, base_commit, name)
    _units_dir(path)
    code, _, err = ws.git(["worktree", "add", "--detach", path, base_commit], root, timeout=600)
    if code != 0:
        ws.git(["worktree", "prune"], root)
        raise WorktreeError("git worktree add failed: %s" % err.strip())

    links: List[str] = []
    checks_skipped = False
    for entry in wanted:
        try:
            _link(os.path.join(root, entry), os.path.join(path, entry))
            links.append(entry)
        except OSError:
            checks_skipped = True
    wanted_copies: List[str] = list(copies)
    for relative in (workspace.plan_path, workspace.decisions_path):
        wanted_copies.append(os.path.relpath(relative, root))
    record = {
        "worktree": path,
        "base_tree": base_tree,
        "base_commit": base_commit,
        "links": links,
        "checks_skipped": checks_skipped,
    }
    try:
        for relative in wanted_copies:
            if not os.path.isabs(relative) and not os.path.normpath(relative).startswith(".."):
                _copy_missing(os.path.join(root, relative), os.path.join(path, relative))
    except OSError as exc:
        remove(root, record, name)
        raise WorktreeError("could not copy into the worktree: %s" % exc) from None
    return record


# --------------------------------------------------------------------------- removal


def _safe_rmtree(path: str) -> None:
    """Delete ``path`` without ever descending a link: each link met is unlinked.

    ``shutil.rmtree`` is not used: before Python 3.12 it follows a junction.
    """
    if ws.is_link(path):
        ws.unlink_link(path)
        return
    with os.scandir(path) as entries:
        children = [entry.path for entry in entries]
    for child in children:
        if ws.is_link(child):
            ws.unlink_link(child)
        elif os.path.isdir(child):
            _safe_rmtree(child)
        else:
            try:
                os.unlink(child)
            except PermissionError:
                os.chmod(child, stat.S_IWRITE)
                os.unlink(child)
    os.rmdir(path)


def _git_remove(root: str, path: str) -> None:
    """One ``git worktree remove --force``; ``PermissionError`` when the directory stays."""
    code, _, err = ws.git(["worktree", "remove", "--force", path], root, timeout=600)
    if code != 0 and os.path.exists(path):
        raise PermissionError("git worktree remove failed: %s" % err.strip())


def _same_path(one: str, other: str) -> bool:
    return os.path.normcase(os.path.realpath(one)) == os.path.normcase(os.path.realpath(other))


def _commit_message(name: str) -> str:
    return "dev-orchestra unit %s" % name


def _unit_commit(root: str, base_commit: Any, name: str) -> bool:
    """Whether ``base_commit`` is the full id of a commit ``create`` made for unit ``name``."""
    if not isinstance(base_commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_commit):
        return False
    code, out, _ = ws.git(["cat-file", "commit", base_commit], root)
    if code != 0:
        return False
    _, _, message = out.partition("\n\n")
    return message.strip() == _commit_message(name)


def _is_unit_worktree(root: str, path: str, base_commit: Any, name: str) -> bool:
    """Whether ``path`` is unit ``name``'s worktree, made from ``base_commit``.

    Not a link, in a ``<repo>.units`` that is not one, and exactly where
    ``location`` puts it, for a ``base_commit`` that is this repository's
    commit for that unit. Listed by ``git worktree list`` or not: a removal
    that failed part way can leave it unlisted. The path and the commit come
    from ``units.json``, which an agent could have rewritten, and whatever
    they name is deleted -- so neither another unit's worktree nor any other
    directory passes.
    """
    if ws.is_link(path) or not _unit_commit(root, base_commit, name):
        return False
    expected = location(root, str(base_commit), name)
    if ws.is_link(os.path.dirname(expected)):
        return False
    return _same_path(path, expected)


def remove(root: str, record: Dict[str, Any], name: str) -> bool:
    """Remove the worktree unit ``name``'s ``record`` names; whether it is gone.

    Nothing is touched unless the path is that unit's worktree
    (``_is_unit_worktree``); a path already gone has only ``git
    worktree prune`` run. The ``LINKED_NAMES`` links first, each checked
    with ``lstat`` and unlinked without being followed; then ``git worktree
    remove``, retried while something holds a file; then whatever is left,
    by ``_safe_rmtree``; then ``git worktree prune``.
    """
    path = record.get("worktree")
    if not isinstance(path, str) or not path:
        return True
    if not os.path.lexists(path):
        ws.git(["worktree", "prune"], root)
        return True
    if not _is_unit_worktree(root, path, record.get("base_commit"), name):
        return False
    # Only these names, never the record's own list: units.json could name any path.
    for linked in LINKED_NAMES:
        link = os.path.join(path, linked)
        if ws.is_link(link):
            try:
                ws.unlink_link(link)
            except OSError:
                pass
    if os.path.lexists(path):
        delay, cap = _REMOVE_BACKOFF
        for attempt in range(_REMOVE_TRIES):
            try:
                _git_remove(root, path)
                break
            except OSError:
                if attempt + 1 < _REMOVE_TRIES:
                    time.sleep(delay)
                    delay = min(delay * 2, cap)
    if os.path.lexists(path):
        try:
            _safe_rmtree(path)
        except OSError:
            pass
    ws.git(["worktree", "prune"], root)
    return not os.path.lexists(path)
