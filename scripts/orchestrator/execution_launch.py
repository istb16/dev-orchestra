"""Starting a child CLI: its own process group, and on Windows the command
``Popen`` has to be given to run what ``which`` found.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Union

from .execution_common import IS_WINDOWS


def spawn_kwargs() -> Dict[str, Any]:
    """Put the child in its own group so the whole tree can be signalled."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


#: The name the tests have always used.
_spawn_kwargs = spawn_kwargs


# --------------------------------------------------------------------------- launching on Windows

#: What Windows starts through cmd.exe rather than as a program of its own.
BATCH_SUFFIXES = (".bat", ".cmd")

#: Characters cmd.exe acts on in an argument even inside double quotes, or
#: that cannot be quoted for it at all: ``"`` ends the quoting, ``%`` and
#: ``!`` expand variables, and a line break ends the command. Python passes a
#: batch file's arguments through as it would a program's (the behaviour behind
#: CVE-2024-24576 elsewhere), so an argument holding one of them could run a
#: command of its own; a launch carrying one is refused instead.
BATCH_UNSAFE = frozenset('"%!\r\n\x00')

#: The program and script an npm ``.cmd`` shim hands its arguments to:
#: ``"%dp0%\node_modules\...\cli.js" %*`` (``%~dp0\`` in older shims).
_SHIM_TARGET = re.compile(r'"%(?:~dp0|dp0%)\\([^"%\r\n]+)"[ \t]*%\*')
_NODE_SCRIPTS = (".js", ".cjs", ".mjs")


class LaunchRefused(OSError):
    """A command that cannot be started safely as it stands."""


def launchable(command: Sequence[str]) -> Union[str, List[str]]:
    """``command`` as ``Popen`` has to be given it to run what ``which`` found.

    POSIX resolves a bare name on ``PATH`` the way ``shutil.which`` does, so
    nothing changes there. Windows does not: ``CreateProcess`` only appends
    ``.exe``, so the ``claude.cmd`` and ``codex.cmd`` npm installs were found
    by ``doctor`` and then could not be started (#269). A bare name is
    replaced with the absolute path :func:`find_program` finds on ``PATH``
    -- never one in the current directory, which may be a repository under
    review -- and one it does not find is not started at all, since
    ``CreateProcess`` would look in the current directory for it. A name
    with a directory is resolved by :func:`find_program` too, against this
    process's directory, where ``doctor`` looked, not the child's, and with
    the same ``PATHEXT`` candidates, so what starts is the file ``doctor``
    reported. A batch file that is an
    npm shim is bypassed for the program it would run (``node`` and its
    script, or an ``.exe``), so no argument passes through cmd.exe; any other
    batch file runs under cmd.exe with every argument quoted, and refuses an
    argument cmd.exe would read as its own syntax (:data:`BATCH_UNSAFE`).
    """
    argv = [str(token) for token in command]
    if not IS_WINDOWS or not argv:
        return argv
    found = find_program(argv[0])
    if found is None:
        where = "" if _has_directory(argv[0]) else " on PATH"
        raise FileNotFoundError(errno.ENOENT, "%s not found%s" % (argv[0], where), argv[0])
    return windows_launch(argv, found)


def find_program(name: str) -> Optional[str]:
    """``shutil.which(name)``, except that on Windows the current directory is never searched.

    ``shutil.which`` on Windows looks in the current directory before
    ``PATH`` (and returns a relative ``.\\name``), so a ``claude.cmd``
    committed at the root of a repository would be found and run in place
    of the installed CLI. Only the absolute directories on ``PATH`` are
    searched here, with ``PATHEXT``, and the path returned is absolute. A
    name with a directory in it is not searched for: it is looked up where
    it says, made absolute, with the same ``PATHEXT`` candidates.
    """
    if not IS_WINDOWS:
        return shutil.which(name)
    return search_path(name, os.environ.get("PATH", ""), os.environ.get("PATHEXT", ""))


def search_path(name: str, path: str, pathext: str) -> Optional[str]:
    """:func:`find_program` on Windows, with ``PATH`` and ``PATHEXT`` given; testable anywhere."""
    exts = [ext for ext in (pathext or ".COM;.EXE;.BAT;.CMD").split(";") if ext]
    if os.path.splitext(name)[1].lower() in (ext.lower() for ext in exts):
        candidates = [name]
    else:
        candidates = [name + ext for ext in exts]
    if _has_directory(name):
        # Where it says, as :func:`launchable` starts it; never on PATH.
        for candidate in candidates:
            found = os.path.abspath(candidate)
            if os.path.isfile(found):
                return found
        return None
    for entry in path.split(";"):
        directory = entry.strip().strip('"')
        # A relative entry (".", or the empty one) is the current directory
        # by another name.
        if not directory or not _is_absolute(directory):
            continue
        for candidate in candidates:
            found = os.path.join(directory, candidate)
            if os.path.isfile(found):
                return found
    return None


def _is_absolute(directory: str) -> bool:
    """A Windows path from a drive or share root, on any platform."""
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|[\\/]{2})", directory)) or os.path.isabs(directory)


def windows_launch(argv: List[str], found: Optional[str]) -> Union[str, List[str]]:
    """:func:`launchable` on Windows, with the ``which`` result given; testable anywhere."""
    program = found or argv[0]
    args = argv[1:]
    if os.path.splitext(program)[1].lower() not in BATCH_SUFFIXES:
        return [program, *args]
    shim = npm_shim_target(program)
    if shim is not None:
        return [*shim, *args]
    return batch_command_line(program, args)


def _has_directory(program: str) -> bool:
    return bool(os.path.dirname(program)) or "/" in program or "\\" in program


def npm_shim_target(batch: str) -> Optional[List[str]]:
    """The program an npm ``.cmd`` shim runs, as a command; None if it is not one.

    Only a target that exists is taken: a ``.js`` script with the ``node.exe``
    beside the shim or else on ``PATH`` (the shim's own order), or an
    ``.exe``. Anything else, including a shim for a shell script, is left to
    cmd.exe.
    """
    try:
        with open(batch, "rb") as handle:
            text = handle.read(64 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return None
    targets = _SHIM_TARGET.findall(text)
    if not targets:
        return None
    base = os.path.dirname(os.path.abspath(batch))
    # The separator spelled out, so this reads the same on any platform.
    target = os.path.normpath(os.path.join(base, *targets[-1].split("\\")))
    if not os.path.isfile(target):
        return None
    suffix = os.path.splitext(target)[1].lower()
    if suffix == ".exe":
        return [target]
    if suffix not in _NODE_SCRIPTS:
        return None
    node = os.path.join(base, "node.exe")
    if not os.path.isfile(node):
        node = find_program("node") or ""
    # Only a program: a `node.cmd` (a version manager's shim) would be the
    # batch file this is here to avoid, started without cmd.exe's quoting.
    if os.path.splitext(node)[1].lower() != ".exe":
        return None
    return [node, target]


def batch_command_line(batch: str, args: Sequence[str]) -> str:
    """The cmd.exe command line that runs ``batch`` with ``args`` and nothing else.

    ``/d`` skips AutoRun, ``/v:off`` leaves ``!`` alone, and ``/s`` makes
    cmd.exe drop exactly the outer pair of quotes. Every token is quoted, so
    ``&``, ``|``, ``<``, ``>``, ``^`` and parentheses stay text; backslashes
    before a closing quote are doubled for the program the batch file runs.
    """
    for token in (batch, *args):
        unsafe = sorted(BATCH_UNSAFE.intersection(token))
        if unsafe:
            # The characters are named, never the argument: it may be a prompt
            # or carry a secret.
            raise LaunchRefused(
                "refused to start %s: it is a batch file, run by cmd.exe, and an argument holds %s, "
                "which cmd.exe would read as its own syntax. Install the CLI as an .exe, or pass the "
                "value another way" % (os.path.basename(batch), ", ".join(repr(c) for c in unsafe))
            )
    comspec = os.environ.get("COMSPEC") or os.path.join(
        os.environ.get("SystemRoot") or r"C:\Windows", "System32", "cmd.exe"
    )
    inner = " ".join(_batch_quoted(token) for token in (batch, *args))
    return '"%s" /d /v:off /s /c "%s"' % (comspec, inner)


def _batch_quoted(token: str) -> str:
    trailing = len(token) - len(token.rstrip("\\"))
    return '"%s%s"' % (token, "\\" * trailing)
