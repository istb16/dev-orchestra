"""Refuse to start a real AI CLI from the tests, or from anything they start.

``tests/helpers.py`` installs this in every test process and puts this
directory on ``PYTHONPATH``, so a Python process a test starts -- a detached
job's worker, say -- loads it too, as its ``sitecustomize``.

Starting ``claude``, ``codex`` or ``agy`` (with ``.exe``, ``.cmd`` and the like)
then fails with ``FileNotFoundError``, just as it does on CI, where none of
them is installed. The one exception is a fake a test wrote itself and named,
by its path, with ``IsolatedCase.allow_cli``. Standard library only.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from importlib.machinery import PathFinder
from typing import Any, List, Optional

#: The executables of the built-in adapters.
PROVIDER_EXECUTABLES = ("agy", "claude", "codex")

#: Fakes a test has allowed, as paths joined with ``os.pathsep``. Kept in the
#: environment so the processes a test starts allow the same ones.
ALLOWED_ENV = "DEV_ORCHESTRA_TEST_ALLOWED_CLIS"

#: When set, every refused start is also appended to this file as a JSON line,
#: from whichever process it was refused in.
REFUSED_LOG_ENV = "DEV_ORCHESTRA_TEST_REFUSED_CLI_LOG"

_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")

#: Every start refused in this process: the command it would have run.
refused: List[List[str]] = []


def normalised(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def allowed() -> List[str]:
    return [normalised(path) for path in os.environ.get(ALLOWED_ENV, "").split(os.pathsep) if path]


def is_provider_cli(program: str) -> bool:
    name = os.path.basename(program.strip().strip('"')).lower()
    stem, suffix = os.path.splitext(name)
    return (stem if suffix in _SUFFIXES else name) in PROVIDER_EXECUTABLES


def refuses(program: str) -> bool:
    if not is_provider_cli(program):
        return False
    # A bare name is refused even when a fake comes first on PATH: Windows
    # looks for `name.exe` by itself and may well find the real one.
    if not os.path.dirname(program):
        return True
    return normalised(program) not in allowed()


def program_of(args: Any, shell: bool) -> Optional[str]:
    """The program ``Popen(args, shell=shell)`` would start, as given."""
    if isinstance(args, (str, bytes, os.PathLike)):
        line = os.fsdecode(args)
        # Without a shell, POSIX runs the string as the program itself; a
        # shell, or Windows, reads it as a command line.
        split = shell or os.name == "nt"
    elif args:
        line = os.fsdecode(args[0])
        split = shell
    else:
        return None
    if not split:
        return line
    try:
        words = shlex.split(line, posix=os.name != "nt")
    except ValueError:
        words = line.split()
    return words[0].strip('"') if words else None


def _record(command: List[str]) -> None:
    refused.append(command)
    log = os.environ.get(REFUSED_LOG_ENV)
    if not log:
        return
    try:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "command": command}) + "\n")
    except OSError:
        pass


def install() -> None:
    """Guard ``subprocess.Popen``, which ``subprocess.run`` goes through; once per process."""
    original = subprocess.Popen.__init__
    if getattr(original, "refuses_provider_clis", False):
        return

    def guarded(self: Any, args: Any, *rest: Any, **kwargs: Any) -> None:
        if not isinstance(args, (str, bytes, os.PathLike)):
            args = list(args)
        # Popen(args, bufsize, executable, stdin, stdout, stderr, preexec_fn, close_fds, shell, ...)
        shell = bool(kwargs.get("shell", rest[7] if len(rest) > 7 else False))
        executable = kwargs.get("executable", rest[1] if len(rest) > 1 else None)
        programs = [program_of(args, shell)]
        if executable is not None and not shell:
            programs.append(os.fsdecode(executable))
        for program in programs:
            if program and refuses(program):
                command = (
                    [os.fsdecode(arg) for arg in args] if isinstance(args, list) else [os.fsdecode(args)]
                )
                _record(command)
                raise FileNotFoundError(
                    errno.ENOENT,
                    "the tests never start a real AI CLI (refused by tests/cli_guard); a test "
                    "that wants a fake one names it by path and passes it to IsolatedCase.allow_cli()",
                    program,
                )
        original(self, args, *rest, **kwargs)

    setattr(guarded, "refuses_provider_clis", True)
    setattr(subprocess.Popen, "__init__", guarded)


def _load_the_next_sitecustomize() -> None:
    """Loaded as ``sitecustomize``, this hides any other one; run that too."""
    here = normalised(os.path.dirname(os.path.abspath(__file__)))
    path = [entry for entry in sys.path if normalised(entry or os.curdir) != here]
    spec = PathFinder.find_spec("sitecustomize", path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


install()

if __name__ == "sitecustomize":
    _load_the_next_sitecustomize()
