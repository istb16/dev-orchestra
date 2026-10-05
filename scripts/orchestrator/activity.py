"""What a running CLI is doing, as short lines a person can be shown.

A provider's :meth:`~orchestrator.providers.base.Provider.activity_of` picks
the tool uses out of one line of its CLI's output, and a :class:`Sink` keeps
them: in a file beside a detached job (``jobs wait`` reads it back with
:func:`read`), or echoed to stderr for ``review run --progress``.

What a line may say is an allowlist (:func:`tool_line`): a tool's name, a
path, a program and, for a known few, its subcommand, a URL's scheme and
host. The model's own
text and a tool's free-text arguments are never shown. Every line goes
through :func:`clip` before it is written or echoed, and again when it is
read back, so neither an adapter's hook nor a tampered file gets raw text
past it. ``redact`` is only the last line of defence.

The sink a run reports to is found through a thread-local
(:func:`install`, :func:`recording`, :func:`current`): ``Provider.run``
takes no keyword for it, so a third-party adapter keeps its signature. A
``provider.run`` called from a thread that installed no sink records
nothing, which is the safe way to fail.

Imports only the standard library and :mod:`orchestrator.workspace`, so it
stays free of the provider registry.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import threading
import time
import unicodedata
from typing import Any, Callable, Dict, Iterator, List, NamedTuple, Optional, Sequence
from urllib.parse import urlsplit

from .workspace import redact

#: Entries one activity file holds before it is rotated to ``<path>.1``.
ENTRY_LIMIT = 500
#: The longest line shown, ellipsis included.
LINE_LIMIT = 100
#: Lines echoed per reviewer before the rest are only counted.
ECHO_LIMIT = 300
#: Bounds of ``jobs wait/show --since`` and ``--activity``.
SINCE_MAX = 10**9
LATEST_MAX = 100

ELLIPSIS = "\u2026"
OUTSIDE = "<outside>"
ECHO_CUT = ELLIPSIS + " further tool uses not shown"


class Activity(NamedTuple):
    """What one line of a CLI's output showed: tool lines, and the context
    size if the line said."""

    lines: Sequence[str]
    context_tokens: Optional[int]


NOTHING = Activity((), None)

# -- cleaning ------------------------------------------------------------------

#: OSC (to BEL or ST), CSI, and the other two-character escapes, in their
#: 7-bit and C1 spellings. An OSC left unterminated runs to the end.
_ESCAPES = re.compile(
    r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|\x1b[@-Z\\-_]"
)


def clip(text: Any) -> str:
    """``text`` as one short, printable, redacted line; "" for anything else.

    In order: escape sequences removed, cut at the first line break, every
    other control or format character dropped (bidi overrides included),
    redacted, stripped, and cut to :data:`LINE_LIMIT` characters.
    """
    if not isinstance(text, str):
        return ""
    text = _ESCAPES.sub("", text)
    lines = text.splitlines()
    text = lines[0] if lines else ""
    text = "".join(char for char in text if not unicodedata.category(char).startswith("C"))
    text = redact(text).strip()
    if len(text) > LINE_LIMIT:
        text = text[: LINE_LIMIT - 1] + ELLIPSIS
    return text


# -- the allowlist -------------------------------------------------------------


def show_path(path: Any, cwd: str, pathmod: Any = os.path) -> str:
    """``path`` relative to ``cwd`` when inside it, else ``<outside>/<basename>``.

    ``pathmod`` is ``ntpath`` or ``posixpath`` for a test; ``os.path`` otherwise.
    A path on another drive counts as outside. "" for no path at all.
    """
    if not isinstance(path, str) or not path.strip():
        return ""
    try:
        full = pathmod.normpath(pathmod.join(cwd, path))
        base = pathmod.normpath(cwd)
        common = pathmod.commonpath([pathmod.normcase(full), pathmod.normcase(base)])
        if common == pathmod.normcase(base):
            shown = pathmod.relpath(full, base)
            return shown.replace("\\", "/") if pathmod.sep == "\\" else shown
    except ValueError:
        pass
    name = pathmod.basename(pathmod.normpath(path))
    return "%s/%s" % (OUTSIDE, name) if name else OUTSIDE


#: Shells whose ``-c``-style argument is the command actually run.
_SHELLS = ("pwsh", "powershell", "bash", "sh", "zsh", "cmd")
_SHELL_FLAGS = ("-command", "-c", "-lc", "/c")
#: Programs whose second word is a subcommand, not free text. Any other
#: program is shown alone: ``echo password`` must not show ``password``.
_SUBCOMMAND_PROGRAMS = frozenset(
    "git gh npm pnpm yarn npx cargo go docker kubectl pip uv poetry make dotnet terraform".split()
)
_SUBCOMMAND = re.compile(r"^[a-z][a-z0-9-]*$")
#: What a program's name may be once its directory and ``.exe`` are gone.
_PROGRAM = re.compile(r"^[A-Za-z0-9._+-]+$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def show_command(command: Any) -> str:
    """``Bash: <program>``, or ``Bash: <program> <subcommand>`` for a program
    known to take one; ``Bash`` when nothing can be read. Nothing else of the
    command is shown.

    A command wrapped in a shell (``pwsh -Command '...'``, ``bash -lc '...'``)
    is unwrapped once first, as Codex runs every command that way.
    """
    if not isinstance(command, str):
        return "Bash"
    try:
        inner = _unwrap_shell(command)
        return _program_line(inner if inner is not None else command)
    except Exception:
        return "Bash"


def _unwrap_shell(command: str) -> Optional[str]:
    """The command a shell was given to run, or None if ``command`` is not
    ``<shell> [options] <flag> <command>``.

    Raises ValueError for a command that starts like a wrapped one and whose
    quoting cannot be read.
    """
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        words = command.split()
        if words and (words[0][:1] in "\"'" or _shell_name(words[0]) in _SHELLS):
            raise
        return None
    if len(tokens) < 3 or _shell_name(tokens[0]) not in _SHELLS:
        return None
    for index, token in enumerate(tokens[1:-1], start=1):
        if token.lower() in _SHELL_FLAGS:
            return _unquoted(tokens[index + 1])
    return None


def _program_line(command: str) -> str:
    lines = command.splitlines()
    words = _without_assignments(lines[0].split() if lines else [])
    if not words:
        return "Bash"
    program = _basename(_unquoted(words[0]))
    if program.lower().endswith(".exe"):
        program = program[: -len(".exe")]
    if not _PROGRAM.match(program):
        return "Bash"
    shown = [program]
    if program.lower() in _SUBCOMMAND_PROGRAMS and len(words) > 1 and _SUBCOMMAND.match(words[1]):
        shown.append(words[1])
    return "Bash: " + " ".join(shown)


def _without_assignments(words: List[str]) -> List[str]:
    """``words`` without the variable assignments that lead them.

    A POSIX ``NAME=value`` may have a quoted value with spaces in it, so its
    words run to the closing quote. A PowerShell statement that starts with a
    variable (``$env:NAME = 'value';``) runs to the word that ends it with
    ``;`` outside quotes, so a quoted ``'a; b'`` does not end it early. One
    holding a backtick, ``(`` or ``{`` (an escape, a subexpression, a block)
    cannot be read that way, and neither can one that never ends: either
    leaves nothing.
    """
    words = list(words)
    while words:
        word = words[0]
        if word.startswith("$"):
            words = _after_statement(words)
        elif _ASSIGNMENT.match(word):
            value = words.pop(0).split("=", 1)[1]
            quote = value[:1]
            if quote in ("'", '"') and (len(value) < 2 or not value.endswith(quote)):
                while words and not words.pop(0).endswith(quote):
                    pass
        else:
            break
    return words


def _after_statement(words: List[str]) -> List[str]:
    """The words after the PowerShell statement that ``words`` starts with;
    see :func:`_without_assignments`."""
    quote = ""
    for index, word in enumerate(words):
        for char in word:
            if char in "`({":
                return []
            if quote:
                if char == quote:
                    quote = ""
            elif char in "'\"":
                quote = char
        if not quote and word.endswith(";"):
            return words[index + 1 :]
    return []


def _shell_name(token: str) -> str:
    """A program's basename, lowercased and without ``.exe``."""
    name = _basename(_unquoted(token)).lower()
    return name[: -len(".exe")] if name.endswith(".exe") else name


def _unquoted(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token.strip("\"'")


def _basename(token: str) -> str:
    return re.split(r"[\\/]", token)[-1]


def show_url(url: Any) -> str:
    """``WebFetch scheme://host[:port]``: no user, path, query or fragment,
    since many APIs carry a token in the path."""
    if not isinstance(url, str):
        return "WebFetch"
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "WebFetch"
    if not parts.scheme or not host:
        return "WebFetch"
    if ":" in host:
        host = "[%s]" % host
    if port is not None:
        host = "%s:%d" % (host, port)
    return "WebFetch %s://%s" % (parts.scheme.lower(), host)


def show_glob(pattern: Any, path: Any, cwd: str) -> str:
    """``Glob <pattern>``, ``Glob <path>`` or ``Glob <pattern> in <path>``.

    The pattern is shown only when it is relative and has no ``..`` segment,
    so it cannot name a place outside the project; the path goes through
    :func:`show_path`.
    """
    shown_path = show_path(path, cwd)
    shown = ""
    if isinstance(pattern, str) and pattern.strip() and _relative_inside(pattern.strip()):
        shown = pattern.strip()
    if shown and shown_path:
        return "Glob %s in %s" % (shown, shown_path)
    if shown or shown_path:
        return "Glob %s" % (shown or shown_path)
    return "Glob"


def _relative_inside(pattern: str) -> bool:
    if pattern[:1] in ("/", "\\", "~") or re.match(r"^[A-Za-z]:", pattern):
        return False
    return ".." not in re.split(r"[\\/]", pattern)


#: Tools whose one shown argument is a file path, and its input key.
_PATH_TOOLS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}
#: The shell tool by its POSIX and its Windows name; both are shown as ``Bash``.
_SHELL_TOOLS = ("Bash", "PowerShell")


def tool_line(name: Any, arguments: Any, cwd: str) -> str:
    """The one line a tool use may show; see the table in ``references/cli.md``.

    Only the arguments named here are read, each through its own rule. Any
    other tool, or arguments that are not a mapping, give the name alone.
    """
    if not isinstance(name, str) or not name:
        return ""
    if name.startswith("mcp__"):
        server, _, tool = name[len("mcp__") :].partition("__")
        return "%s.%s" % (server, tool) if server and tool else name
    if not isinstance(arguments, dict):
        return name
    if name in _PATH_TOOLS:
        path = show_path(arguments.get(_PATH_TOOLS[name]), cwd)
        return "%s %s" % (name, path) if path else name
    if name == "Glob":
        return show_glob(arguments.get("pattern"), arguments.get("path"), cwd)
    if name == "Grep":
        path = show_path(arguments.get("path"), cwd)
        return "Grep %s" % path if path else name
    if name in _SHELL_TOOLS:
        return show_command(arguments.get("command"))
    if name == "WebFetch":
        return show_url(arguments.get("url"))
    return name


# -- the sink ------------------------------------------------------------------

#: Held while echoing, so lines from parallel reviewers never interleave.
_ECHO_LOCK = threading.Lock()


class Sink:
    """Where one run's activity goes: a file, an echo, or both.

    Only the one thread handing on the CLI's stdout lines adds to it, so the
    file is opened, appended to and closed per entry, without a lock.
    """

    def __init__(self, path: Optional[str] = None, echo: Optional[Callable[[str], None]] = None) -> None:
        self.path = path
        self.echo = echo
        #: Tool lines seen, written or not; a gap in the file's ``n`` is how a
        #: reader knows entries were dropped.
        self.n = 0
        self.started = time.monotonic()
        self.context_tokens: Optional[int] = None
        self._written_tokens: Optional[int] = None
        self._in_file = 0
        self._echoed = 0

    def add(self, act: Any) -> None:
        """Keep what ``act`` showed. Never raises: activity must not fail a run."""
        try:
            lines = list(act.lines)
            tokens = act.context_tokens
        except Exception:
            return
        if _count(tokens):
            self.context_tokens = tokens
        for raw in lines:
            line = clip(raw)
            if not line:
                continue
            self.n += 1
            if self.path and self._write(
                {"n": self.n, "s": self.seconds(), "line": line, "tokens": self.context_tokens}
            ):
                self._written_tokens = self.context_tokens
            if self.echo is not None:
                self._echo_line(line)
        if self.path and self.context_tokens is not None and self.context_tokens != self._written_tokens:
            # A message with no tool use still says how big the context is
            # now; an entry without ``n`` carries it and is not a tool use.
            if self._write({"s": self.seconds(), "tokens": self.context_tokens}):
                self._written_tokens = self.context_tokens

    def seconds(self) -> float:
        """Seconds since the sink was made."""
        return round(time.monotonic() - self.started, 1)

    def say(self, text: str) -> None:
        """Echo ``text`` outside the tool lines and their limit, such as ``done: ok``."""
        line = clip(text)
        if line and self.echo is not None:
            self._echo(line)

    def _echo_line(self, line: str) -> None:
        self._echoed += 1
        # Read at call time, so a test can lower it.
        if self._echoed <= ECHO_LIMIT:
            self._echo(line)
        elif self._echoed == ECHO_LIMIT + 1:
            self._echo(ECHO_CUT)

    def _echo(self, line: str) -> None:
        echo = self.echo
        if echo is None:
            return
        try:
            with _ECHO_LOCK:
                echo(line)
        except Exception:
            pass

    def _write(self, entry: Dict[str, Any]) -> bool:
        """Append ``entry``; whether it was written."""
        path = str(self.path)
        if self._in_file >= ENTRY_LIMIT:
            try:
                os.replace(path, path + ".1")
            except OSError:
                # A reader may hold the file open (Windows). The entry is
                # dropped rather than growing the file past the cap, and the
                # rename is tried again on the next one.
                return False
            self._in_file = 0
        try:
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError:
            return False
        self._in_file += 1
        return True


_local = threading.local()


def install(sink: Optional[Sink]) -> None:
    """Make ``sink`` the one this thread's provider runs report to."""
    _local.sink = sink


def current() -> Optional[Sink]:
    """The sink this thread's provider runs report to, if any."""
    return getattr(_local, "sink", None)


@contextlib.contextmanager
def recording(sink: Optional[Sink]) -> Iterator[Optional[Sink]]:
    """Install ``sink`` for the block, restoring the previous one afterwards,
    even when the block raises."""
    previous = current()
    install(sink)
    try:
        yield sink
    finally:
        install(previous)


# -- reading it back -----------------------------------------------------------


def _count(value: Any) -> bool:
    """Whether ``value`` is a token count: a non-negative int, not a bool."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _bounded(value: Any, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(max(value, 0), high)


def _seconds(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0 or value == float("inf"):
        return None
    return float(value)


def read(path: str, since: int = 0, latest: int = 10) -> Optional[Dict[str, Any]]:
    """A job's activity, or None when it has no activity file.

    The file is untrusted: lines that do not decode, are not objects, or whose
    ``n`` is not a positive int or whose ``line`` is not a string are skipped,
    and every line is clipped again. An entry with only ``tokens`` is no tool
    use, but the latest count in the file, from either kind of entry, is the
    ``context_tokens`` reported. ``since`` keeps entries after that ``n``;
    ``latest`` keeps the newest that many of those.

    Returns ``{count, context_tokens, lines, dropped, omitted}``: ``count`` is
    the highest ``n`` kept, ``lines`` are ``{n, s, line}``, ``dropped`` says an
    ``n`` in ``(since, count]`` is missing, and ``omitted`` how many matching
    entries ``latest`` left out.
    """
    since = _bounded(since, SINCE_MAX)
    latest = _bounded(latest, LATEST_MAX)
    texts: List[str] = []
    for name in (path + ".1", path):
        try:
            with open(name, "r", encoding="utf-8", errors="replace") as handle:
                texts.append(handle.read())
        except OSError:
            continue
    if not texts:
        return None
    entries: Dict[int, Dict[str, Any]] = {}
    context_tokens = None
    for text in texts:
        for raw in text.splitlines():
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            # The last count written wins, whether a tool line carried it or
            # an entry of its own did.
            if _count(entry.get("tokens")):
                context_tokens = entry["tokens"]
            n = entry.get("n")
            line = entry.get("line")
            if isinstance(n, bool) or not isinstance(n, int) or n <= 0 or not isinstance(line, str):
                continue
            entries.setdefault(n, entry)
    ordered = sorted(entries)
    count = ordered[-1] if ordered else 0
    matching = [n for n in ordered if n > since]
    kept = matching[-latest:] if latest else []
    lines = []
    for n in kept:
        lines.append({"n": n, "s": _seconds(entries[n].get("s")), "line": clip(entries[n]["line"])})
    return {
        "count": count,
        "context_tokens": context_tokens,
        "lines": lines,
        "dropped": count > since and len(matching) < count - since,
        "omitted": len(matching) - len(kept),
    }
