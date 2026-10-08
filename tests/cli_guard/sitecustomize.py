"""Refuse to start a real AI CLI from the tests, or from anything they start.

``tests/helpers.py`` installs this in every test process and puts this
directory on ``PYTHONPATH``, so a Python process a test starts -- a detached
job's worker, say -- loads it too, as its ``sitecustomize``.

Starting ``claude``, ``codex`` or ``agy`` then fails with ``FileNotFoundError``,
just as it does on CI, where none of them is installed. What counts as
starting one:

- a program named ``claude``, ``codex`` or ``agy`` (``.exe``, ``.cmd`` and the
  like included), by path or by bare name;
- any argument that is one of the installed CLIs, found once at start-up on
  ``PATH`` with every ``PATHEXT`` extension: the file, where its links lead,
  and for an npm shim the script it runs and that script's package;
- ``node``, ``bun`` or ``deno`` running a script of one of those packages, or
  of ``node_modules/@anthropic-ai/claude-code`` or ``node_modules/@openai/codex``;
- any of the above behind ``cmd /c``, ``sh -c``, ``pwsh -Command``, ``env``,
  ``npx`` and the like, in any command of a ``&&``, ``||``, ``;``, ``|`` or
  ``&`` list, after ``VAR=value`` words, and on Windows behind an unquoted path
  with spaces.

The one exception is a fake a test wrote itself and named, by its path, with
``IsolatedCase.allow_cli``. ``subprocess.Popen`` (which ``subprocess.run``
goes through), ``os.system``, ``os.spawn*``, ``os.posix_spawn*``, ``os.exec*``
and ``os.startfile`` are guarded. Not covered: ``_winapi.CreateProcess`` and
other direct system calls, and a Python process started with ``-I``, ``-E``
or ``-S``, or with an environment that drops ``PYTHONPATH``, which never loads
this file. Standard library only.
"""

from __future__ import annotations

import base64
import binascii
import errno
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from importlib.machinery import PathFinder
from typing import Any, Callable, Iterable, List, Optional, Sequence, Set, Tuple

#: The executables of the built-in adapters.
PROVIDER_EXECUTABLES = ("agy", "claude", "codex")

#: Fakes a test has allowed, as paths joined with ``os.pathsep``. Kept in the
#: environment so the processes a test starts allow the same ones.
ALLOWED_ENV = "DEV_ORCHESTRA_TEST_ALLOWED_CLIS"

#: When set, every refused start is also appended to this file as a JSON line,
#: from whichever process it was refused in.
REFUSED_LOG_ENV = "DEV_ORCHESTRA_TEST_REFUSED_CLI_LOG"

#: Where the installed CLIs are, as JSON, handed down to the processes a test
#: starts: a test may change PATH before it starts one.
LOCATIONS_ENV = "DEV_ORCHESTRA_TEST_REAL_CLIS"

_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")
_SCRIPT_RUNNERS = ("node", "nodejs", "bun", "deno")
_PACKAGE_RUNNERS = ("npx", "bunx", "pnpx")
_PREFIX_COMMANDS = ("env", "exec", "command", "nohup", "time", "nice")
_SHELLS = ("sh", "bash", "dash", "zsh", "ksh", "ash", "fish")
_POWERSHELLS = ("pwsh", "powershell")
_PROVIDER_PACKAGE = re.compile(
    r"(?:^|/)node_modules/(?:@anthropic-ai/claude-code|@openai/codex"
    r"|(?:@[^/]+/)?(?:claude|codex|agy|antigravity)(?:-[^/]*)?)(?:/|$)"
)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# The script an npm shim runs: `"%dp0%\node_modules\...\cli.js"` in a .cmd,
# `"$basedir/node_modules/.../cli.js"` in the sh and .ps1 ones.
_SHIM_TARGET = re.compile(r"%~?dp0%?\\([^\"%\r\n*]+)|\$basedir[/\\]([^\"\r\n$`]+)")
_MAX_DEPTH = 8

#: Every start refused in this process: the command it would have run.
refused: List[List[str]] = []

_inside = threading.local()


def normalised(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def allowed() -> List[str]:
    return [normalised(path) for path in os.environ.get(ALLOWED_ENV, "").split(os.pathsep) if path]


def _stem(program: str) -> str:
    # Either separator, whatever this platform's is: a Windows path is judged anywhere.
    name = re.split(r"[\\/]", program.strip().strip('"').rstrip("\\/"))[-1].lower()
    stem, suffix = os.path.splitext(name)
    return stem if suffix in (*_SUFFIXES, ".js", ".mjs") else name


def is_provider_cli(program: str) -> bool:
    return _stem(program) in PROVIDER_EXECUTABLES


def _has_directory(token: str) -> bool:
    return "/" in token or "\\" in token


def _is_allowed(token: str) -> bool:
    token = token.strip().strip('"')
    return bool(token) and _has_directory(token) and normalised(token) in allowed()


# -- where the installed CLIs are ---------------------------------------------


class Locations:
    """The installed CLIs' files, and the packages an npm install runs them from."""

    def __init__(self, files: Iterable[str] = (), packages: Iterable[str] = ()) -> None:
        self.files: Set[str] = set(files)
        self.packages: Set[str] = set(packages)

    def merge(self, other: "Locations") -> None:
        self.files |= other.files
        self.packages |= other.packages

    def to_json(self) -> str:
        return json.dumps({"files": sorted(self.files), "packages": sorted(self.packages)})

    @classmethod
    def from_json(cls, text: str) -> "Locations":
        try:
            data = json.loads(text)
            return cls(data.get("files") or (), data.get("packages") or ())
        except (ValueError, AttributeError, TypeError):
            return cls()

    def holds(self, token: str) -> bool:
        """Whether ``token`` names one of the files, or anything inside a package."""
        token = token.strip().strip('"')
        if not token or len(token) > 4096 or not _has_directory(token):
            return False
        candidates = {normalised(token)}
        try:
            candidates.add(normalised(os.path.realpath(token)))
        except (OSError, ValueError):
            pass
        for candidate in candidates:
            if candidate in self.files:
                return True
            for package in self.packages:
                if candidate == package or candidate.startswith(package + os.sep):
                    return True
        return False


def _package_of(path: str) -> Optional[str]:
    """The npm package directory ``path`` is inside, if it is inside one."""
    current = os.path.dirname(path)
    for _ in range(8):
        parent = os.path.dirname(current)
        if parent == current:
            return None
        if os.path.basename(parent).lower() == "node_modules":
            return current
        if os.path.basename(parent).startswith("@") and os.path.basename(os.path.dirname(parent)).lower() == (
            "node_modules"
        ):
            return current
        current = parent
    return None


def shim_targets(shim: str) -> List[str]:
    """The scripts an npm shim at ``shim`` runs, as absolute paths."""
    try:
        with open(shim, "rb") as handle:
            text = handle.read(64 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return []
    base = os.path.dirname(os.path.abspath(shim))
    targets = []
    for match in _SHIM_TARGET.finditer(text):
        relative = (match.group(1) or match.group(2) or "").strip()
        if relative:
            targets.append(os.path.normpath(os.path.join(base, *re.split(r"[\\/]", relative))))
    return targets


def discover(path_value: str, pathext: str = "") -> Locations:
    """Every claude, codex and agy on ``path_value``, as :class:`Locations`."""
    exts = [""]
    if os.name == "nt" or pathext:
        exts += [ext.lower() for ext in (pathext or ".COM;.EXE;.BAT;.CMD").split(";") if ext]
        exts += [".ps1"]
    locations = Locations()
    for entry in path_value.split(os.pathsep):
        directory = entry.strip().strip('"')
        if not directory:
            continue
        for name in PROVIDER_EXECUTABLES:
            for ext in dict.fromkeys(exts):
                found = os.path.join(directory, name + ext)
                if os.path.isfile(found):
                    _add(locations, found)
    return locations


def _add(locations: Locations, found: str) -> None:
    files = [found]
    try:
        files.append(os.path.realpath(found))
    except (OSError, ValueError):
        pass
    for path in list(files):
        files += shim_targets(path)
    for path in files:
        locations.files.add(normalised(path))
        package = _package_of(os.path.abspath(path))
        if package:
            locations.packages.add(normalised(package))


_locations = Locations()


def locations() -> Locations:
    return _locations


# -- judging a command ---------------------------------------------------------


def split_windows(line: str) -> List[Tuple[str, int, int]]:
    """``line`` split as ``CommandLineToArgvW`` does: (token, start, end) each."""
    tokens: List[Tuple[str, int, int]] = []
    i, n = 0, len(line)
    while i < n:
        while i < n and line[i] in " \t":
            i += 1
        if i >= n:
            break
        start, chars, quoted = i, [], False
        while i < n and (quoted or line[i] not in " \t"):
            if line[i] == "\\":
                run = i
                while i < n and line[i] == "\\":
                    i += 1
                count = i - run
                if i < n and line[i] == '"':
                    chars.append("\\" * (count // 2))
                    if count % 2:
                        chars.append('"')
                        i += 1
                else:
                    chars.append("\\" * count)
            elif line[i] == '"':
                if quoted and i + 1 < n and line[i + 1] == '"':
                    chars.append('"')
                    i += 2
                else:
                    quoted = not quoted
                    i += 1
            else:
                chars.append(line[i])
                i += 1
        tokens.append(("".join(chars), start, i))
    return tokens


def _split_list(line: str, separators: str, escape: str) -> List[str]:
    """``line`` cut at each run of ``separators`` outside double quotes."""
    parts, current, quoted, i = [], [], False, 0
    while i < len(line):
        char = line[i]
        if escape and char == escape and not quoted and i + 1 < len(line):
            current.append(line[i + 1])
            i += 2
            continue
        if char == '"':
            quoted = not quoted
        if not quoted and char in separators:
            parts.append("".join(current))
            current = []
            while i < len(line) and line[i] in separators:
                i += 1
            continue
        current.append(char)
        i += 1
    parts.append("".join(current))
    return [part for part in parts if part.strip()]


def _judge_windows_line(line: str, depth: int) -> Optional[str]:
    """A command line as ``CreateProcess`` reads it."""
    tokens = split_windows(line)
    if not tokens:
        return None
    if _stem(tokens[0][0]) == "cmd" or _is_comspec(tokens[0][0]):
        return _judge_cmd_line(line, tokens, depth)
    verdict = _judge([token for token, _, _ in tokens], depth)
    if verdict:
        return verdict
    stripped = line.strip()
    if not stripped.startswith('"'):
        # An unquoted path with spaces: CreateProcess tries each longer prefix.
        words = stripped.split(" ")
        for end in range(2, len(words) + 1):
            candidate = " ".join(words[:end])
            if (is_provider_cli(candidate) and not _is_allowed(candidate)) or (
                _locations.holds(candidate) and not _is_allowed(candidate)
            ):
                return candidate
    return None


def _is_comspec(program: str) -> bool:
    comspec = os.environ.get("COMSPEC")
    return bool(comspec) and normalised(program.strip('"')) == normalised(comspec or "")


def _judge_cmd_line(line: str, tokens: Sequence[Tuple[str, int, int]], depth: int) -> Optional[str]:
    """``cmd.exe ... /c <command>``: the command, with cmd.exe's outer quotes taken off too."""
    for token, start, end in tokens[1:]:
        switch = token.lower()
        if switch[:2] not in ("/c", "/k", "/r"):
            continue
        rest = (line[start + 2 :] if len(switch) > 2 else line[end:]).strip()
        variants = [rest]
        if rest.startswith('"'):
            inner = rest[1:]
            last = inner.rfind('"')
            variants.append(inner[:last] + inner[last + 1 :] if last >= 0 else inner)
        for variant in variants:
            verdict = _judge_cmd_script(variant, depth + 1)
            if verdict:
                return verdict
        return None
    return None


def _judge_cmd_script(script: str, depth: int) -> Optional[str]:
    for segment in _split_list(script, "&|", "^"):
        segment = segment.strip().lstrip("@(").strip()
        if segment:
            verdict = _judge_windows_line(segment, depth + 1)
            if verdict:
                return verdict
    return None


def _judge_posix_script(script: str, depth: int) -> Optional[str]:
    for line in script.splitlines() or [script]:
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            words = list(lexer)
        except ValueError:
            words = line.split()
        segment: List[str] = []
        for word in [*words, ";"]:
            if word and all(char in "();<>|&" for char in word):
                if segment:
                    verdict = _judge(segment, depth + 1)
                    if verdict:
                        return verdict
                segment = []
                if word in ("<", ">", ">>"):
                    segment = ["<redirect>"]
            elif segment == ["<redirect>"]:
                segment = []
            else:
                segment.append(word)
    return None


def _judge_powershell_script(script: str, depth: int) -> Optional[str]:
    for segment in _split_list(script.replace("&&", ";").replace("||", ";"), ";|", "`"):
        segment = segment.strip()
        if segment[:1] in ("&", "."):
            segment = segment[1:].strip()
        try:
            words = shlex.split(segment, posix=True)
        except ValueError:
            words = segment.split()
        words = [word.strip("'\"") for word in words]
        if words:
            verdict = _judge(words, depth + 1)
            if verdict:
                return verdict
    return None


def _first_operand(argv: Sequence[str], skip: Sequence[str] = ()) -> int:
    """Index of the first word in ``argv[1:]`` that is not an option or in ``skip``; len if none."""
    for index in range(1, len(argv)):
        word = argv[index]
        if word.startswith("-") or word.lower() in skip or _ASSIGNMENT.match(word):
            continue
        return index
    return len(argv)


def _is_provider_script(script: str) -> bool:
    script = script.strip().strip('"')
    if _is_allowed(script):
        return False
    if is_provider_cli(script) or _locations.holds(script):
        return True
    return bool(_PROVIDER_PACKAGE.search(normalised(script).replace("\\", "/").lower()))


def _judge(argv: Sequence[str], depth: int) -> Optional[str]:
    """The offending word if ``argv`` would start a provider CLI, else None."""
    if depth > _MAX_DEPTH:
        return None
    words = list(argv)
    while words and _ASSIGNMENT.match(words[0]):
        words = words[1:]
    if not words:
        return None
    program = words[0].strip().strip('"')
    for word in words:
        if _locations.holds(word) and not _is_allowed(word):
            return word
    if is_provider_cli(program) and not _is_allowed(program):
        return program
    if not _has_directory(program):
        found = shutil.which(program) if program else None
        if found and _locations.holds(found):
            return program
    stem = _stem(program)
    if stem in _SCRIPT_RUNNERS:
        index = _first_operand(words, ("run", "x", "exec"))
        if index < len(words) and _is_provider_script(words[index]):
            return words[index]
    elif stem in _PACKAGE_RUNNERS or (
        stem in ("pnpm", "yarn", "npm") and len(words) > 1 and words[1].lower() in ("dlx", "exec", "x")
    ):
        index = _first_operand(words, ("dlx", "exec", "x"))
        if index < len(words):
            # `@scope/name@version` or `name@version`, without the version.
            spec = words[index].lower()
            name = "@" + spec[1:].split("@", 1)[0] if spec.startswith("@") else spec.split("@", 1)[0]
            if is_provider_cli(name) or _PROVIDER_PACKAGE.search("node_modules/" + name):
                return words[index]
            return _judge(words[index:], depth + 1)
    elif stem in _PREFIX_COMMANDS:
        index = _first_operand(words)
        return _judge(words[index:], depth + 1) if index < len(words) else None
    elif stem in _SHELLS:
        for index in range(1, len(words) - 1):
            flags = words[index]
            if flags.startswith("-") and not flags.startswith("--") and "c" in flags[1:]:
                return _judge_posix_script(words[index + 1], depth + 1)
        index = _first_operand(words)
        if index < len(words) and is_provider_cli(words[index]) and not _is_allowed(words[index]):
            return words[index]
    elif stem in _POWERSHELLS:
        return _judge_powershell_args(words, depth)
    elif stem == "cmd" or _is_comspec(program):
        line = subprocess.list2cmdline(words)
        return _judge_cmd_line(line, split_windows(line), depth)
    return None


def _judge_powershell_args(words: Sequence[str], depth: int) -> Optional[str]:
    for index in range(1, len(words)):
        flag = words[index].lower()
        if not flag.startswith("-") or len(flag) < 2:
            continue
        rest = words[index + 1 :]
        if "-command".startswith(flag):
            return _judge_powershell_script(" ".join(rest), depth + 1)
        if "-file".startswith(flag) and rest:
            return _judge(rest, depth + 1)
        if flag == "-ec" or (len(flag) >= 2 and "-encodedcommand".startswith(flag)):
            if rest:
                try:
                    script = base64.b64decode(rest[0]).decode("utf-16-le", errors="replace")
                except (binascii.Error, ValueError):
                    return None
                return _judge_powershell_script(script, depth + 1)
    return None


def refusal(args: Any, shell: bool = False, executable: Any = None) -> Optional[str]:
    """What ``Popen(args, shell=shell, executable=executable)`` would start that is a
    provider CLI, or None when it starts none."""
    if isinstance(args, (str, bytes, os.PathLike)):
        line = os.fsdecode(args)
        if shell:
            verdict = _judge_cmd_script(line, 0) if os.name == "nt" else _judge_posix_script(line, 0)
        elif os.name == "nt":
            verdict = _judge_windows_line(line, 0)
        else:
            verdict = _judge([line], 0)
        argv = [line]
    else:
        argv = [os.fsdecode(arg) for arg in args]
        if shell and argv:
            if os.name == "nt":
                verdict = _judge_cmd_script(subprocess.list2cmdline(argv), 0)
            else:
                verdict = _judge_posix_script(argv[0], 0)
        else:
            verdict = _judge(argv, 0)
    if verdict is None and executable is not None and not shell:
        verdict = _judge([os.fsdecode(executable), *argv[1:]], 0)
    return verdict


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


def _refuse(program: str, command: List[str]) -> None:
    _record(command)
    raise FileNotFoundError(
        errno.ENOENT,
        "the tests never start a real AI CLI (refused by tests/cli_guard); a test "
        "that wants a fake one names it by path and passes it to IsolatedCase.allow_cli()",
        program,
    )


def _as_command(args: Any) -> List[str]:
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)]
    return [os.fsdecode(arg) for arg in args]


# -- installing -----------------------------------------------------------------


def install() -> None:
    """Guard ``subprocess.Popen`` and the ``os`` launchers; once per process."""
    global _locations
    original = subprocess.Popen.__init__
    if getattr(original, "refuses_provider_clis", False):
        return
    found = discover(os.environ.get("PATH", ""), os.environ.get("PATHEXT", ""))
    found.merge(Locations.from_json(os.environ.get(LOCATIONS_ENV, "")))
    _locations = found
    os.environ[LOCATIONS_ENV] = found.to_json()

    def guarded(self: Any, args: Any, *rest: Any, **kwargs: Any) -> None:
        if not isinstance(args, (str, bytes, os.PathLike)):
            args = list(args)
        # Popen(args, bufsize, executable, stdin, stdout, stderr, preexec_fn, close_fds, shell, ...)
        shell = bool(kwargs.get("shell", rest[7] if len(rest) > 7 else False))
        executable = kwargs.get("executable", rest[1] if len(rest) > 1 else None)
        verdict = refusal(args, shell, executable)
        if verdict:
            _refuse(verdict, _as_command(args))
        _inside.active = getattr(_inside, "active", 0) + 1
        try:
            original(self, args, *rest, **kwargs)
        finally:
            _inside.active -= 1

    setattr(guarded, "refuses_provider_clis", True)
    setattr(guarded, "refused", refused)
    setattr(subprocess.Popen, "__init__", guarded)
    _guard_os()


def _wrap(name: str, judge: Callable[..., Optional[Tuple[str, List[str]]]]) -> None:
    original = getattr(os, name, None)
    if original is None or getattr(original, "refuses_provider_clis", False):
        return

    def guarded(*args: Any, **kwargs: Any) -> Any:
        # subprocess may use os.posix_spawn itself, for a command already judged.
        if not getattr(_inside, "active", 0):
            verdict = judge(*args, **kwargs)
            if verdict:
                _refuse(*verdict)
        return original(*args, **kwargs)

    setattr(guarded, "refuses_provider_clis", True)
    setattr(os, name, guarded)


def _judge_path_and_argv(path: Any, argv: Any) -> Optional[Tuple[str, List[str]]]:
    command = _as_command(argv) if argv is not None else []
    for candidate in ([os.fsdecode(path), *command[1:]], command):
        verdict = refusal(candidate) if candidate else None
        if verdict:
            return verdict, command or [os.fsdecode(path)]
    return None


def _guard_os() -> None:
    def system(command: Any, *_: Any, **__: Any) -> Optional[Tuple[str, List[str]]]:
        verdict = refusal(command, shell=True)
        return (verdict, _as_command(command)) if verdict else None

    def spawn(mode: Any, path: Any, args: Any, *_: Any, **__: Any) -> Optional[Tuple[str, List[str]]]:
        return _judge_path_and_argv(path, args)

    def posix_spawn(path: Any, argv: Any, *_: Any, **__: Any) -> Optional[Tuple[str, List[str]]]:
        return _judge_path_and_argv(path, argv)

    def execute(path: Any, args: Any, *_: Any, **__: Any) -> Optional[Tuple[str, List[str]]]:
        return _judge_path_and_argv(path, args)

    def startfile(path: Any, *rest: Any, **kwargs: Any) -> Optional[Tuple[str, List[str]]]:
        arguments = kwargs.get("arguments", rest[1] if len(rest) > 1 else "")
        line = subprocess.list2cmdline([os.fsdecode(path)]) + (" " + arguments if arguments else "")
        verdict = refusal(line)
        return (verdict, [line]) if verdict else None

    _wrap("system", system)
    for name in ("spawnv", "spawnve", "spawnvp", "spawnvpe"):
        _wrap(name, spawn)
    for name in ("posix_spawn", "posix_spawnp"):
        _wrap(name, posix_spawn)
    for name in ("execv", "execve", "execvp", "execvpe"):
        _wrap(name, execute)
    _wrap("startfile", startfile)


def refused_launches() -> List[List[str]]:
    """The refusals of whichever guard is installed, which may be another copy of this one."""
    return list(getattr(subprocess.Popen.__init__, "refused", refused))


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return normalised(os.path.realpath(a)) == normalised(os.path.realpath(b))


def _load_the_next_sitecustomize() -> None:
    """Loaded as ``sitecustomize``, this hides any other one; run that too, once."""
    if getattr(sys, "_dev_orchestra_cli_guard_chained", False):
        return
    setattr(sys, "_dev_orchestra_cli_guard_chained", True)
    path = list(sys.path)
    for _ in range(len(path) + 1):
        spec = PathFinder.find_spec("sitecustomize", path)
        if spec is None or spec.loader is None or not spec.origin:
            return
        if not _same_file(spec.origin, __file__):
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return
        # This file again, under another spelling of its directory.
        found = os.path.dirname(spec.origin)
        path = [entry for entry in path if not _same_file(entry or os.curdir, found)]


install()

if __name__ == "sitecustomize":
    _load_the_next_sitecustomize()
