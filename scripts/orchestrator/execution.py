"""Running a child CLI without ever hanging ourselves.

``subprocess.run(..., timeout=...)`` is not enough for this job:

* On timeout it kills only the direct child. ``claude`` and ``codex`` spawn
  their own children (ripgrep, node, git), which survive as orphans.
* Worse, after killing the child it calls ``communicate()``, which waits for
  EOF on the pipes. A surviving grandchild still holds the write end, so the
  call can block indefinitely -- the timeout machinery hangs on the very
  failure it exists to handle.

So this module drives ``Popen`` directly: the child gets its own process group,
a breach kills the whole group, output is drained by daemon threads that can be
abandoned, and stdin is written by a thread because a prompt with an inlined
diff is far larger than a pipe buffer.

It also tracks *when output last arrived*, which is what makes an idle deadline
possible: a wedged agent stops producing anything, while a slow one keeps
ticking. Whether that signal exists at all depends on the CLI and the output
format it was asked for -- see ``references/providers.md``.
"""

from __future__ import annotations

import codecs
import errno
import io
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import IO, Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple, Union, cast

from . import clocks

#: Exit codes this module reports for its own decisions. Chosen to stay clear
#: of the 0-127 range a child is likely to use for its own reasons.
EXIT_TOTAL_TIMEOUT = 124  # conventional timeout(1) code
EXIT_IDLE_STALL = 125
EXIT_SPAWN_FAILED = 126

#: How long to let a killed group settle before abandoning its reader threads.
KILL_GRACE_SECONDS = 5.0
_POLL_SECONDS = 0.2
#: Stdout lines waiting for ``on_line`` before further ones are dropped.
_LINE_QUEUE_SIZE = 1000
#: The most one read of a pipe takes; a read returns whatever has arrived.
_READ_CHUNK = 65536
#: After the child exits, how long its readers get to take in what it wrote
#: last, and how long a quiet pipe must stay quiet to count as read.
_SETTLE_SECONDS = 1.0
_SETTLE_QUIET_SECONDS = 0.2
#: How long a finished run waits for ``on_line`` to catch up.
_ON_LINE_GRACE_SECONDS = 2.0

IS_WINDOWS = sys.platform.startswith("win")

#: Set by ``Provider._child_env`` for every process this tool starts, so a
#: delegated Claude run that loads the user's settings keeps dev-orchestra's
#: own hooks silent (``reply_language``, ``claude_hooks.refresh``).
DELEGATED_ENV = "DEV_ORCHESTRA_DELEGATED"


class _StopState:
    """What the SIGTERM handler a detached worker installs needs to know."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        #: The CLIs :func:`execute` is running now. A worker runs one CLI at a
        #: time from its main thread, where
        #: signal handlers also run, so the handler only ever runs between this
        #: module's bytecodes, never alongside them. No lock: a handler waiting on a
        #: lock held by the code it interrupted would deadlock.
        self.active: List[subprocess.Popen] = []
        #: Set while a CLI is being started and not yet in ``active``.
        self.spawning = False
        self.requested = False
        #: Where the handler says which CLI group it could not end: a file beside the
        #: job record, which ``jobs cancel`` reads once the worker has gone. None
        #: outside a worker.
        self.note_path: Optional[str] = None


_stop = _StopState()
#: The worker's own budget for its CLI. ``_stop_group`` spends at most this
#: plus one poll interval for each of its two steps, about 1s for the default
#: 5s grace: well inside kill_tree's SIGTERM window (grace/2, 2.5s), so the
#: worker exits on SIGTERM, not SIGKILL.
_STOP_GRACE_SECONDS = KILL_GRACE_SECONDS / 8


class ExecOutcome:
    """What happened to one child process."""

    def __init__(
        self,
        exit_code: int,
        stdout: str,
        stderr: str,
        duration: float,
        timed_out: bool = False,
        stalled: bool = False,
        idle_for: float = 0.0,
        orphans_possible: bool = False,
        suspended: float = 0.0,
        started: bool = True,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.duration = duration
        #: How much of ``duration`` the machine spent asleep: see ``clocks``.
        self.suspended = suspended
        #: The total deadline was reached.
        self.timed_out = timed_out
        #: No output arrived for the idle deadline: the agent looks wedged.
        self.stalled = stalled
        self.idle_for = idle_for
        #: The group did not fully exit after being killed, or something the
        #: child started still held its output after it exited and could not
        #: be confirmed stopped.
        self.orphans_possible = orphans_possible
        #: False when the child could not be started at all. For the caller in
        #: this process only: ``to_dict`` leaves it out, and a recorded outcome
        #: that never started says so by its exit code, ``EXIT_SPAWN_FAILED``.
        self.started = started
        #: Free for readers of this outcome to keep what they derive from it.
        #: Never serialised.
        self.cache: Dict[str, Any] = {}

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.stalled

    def to_dict(self) -> Dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration, 2),
            "suspended_seconds": round(self.suspended, 2),
            "timed_out": self.timed_out,
            "stalled": self.stalled,
            "idle_for_seconds": round(self.idle_for, 2),
            "orphans_possible": self.orphans_possible,
        }


class _Drain:
    """Reads one stream, remembering when the last byte arrived.

    ``on_line`` sees each line after it is recorded, on a thread of its own
    fed through a bounded queue: the reader never waits on it, so a slow or
    failing callback can neither block the child on a full pipe, nor hold
    back the idle deadline, nor fail the run. A line that finds the queue
    full is not handed on, only counted in ``dropped``. Once :meth:`mark`
    is taken, a line that ends past it waits until :meth:`detach` drops it or
    :meth:`keep` lets it through.
    """

    def __init__(self, on_line: Optional[Callable[[str], None]] = None) -> None:
        self.chunks: List[str] = []
        self.last_output_at = time.monotonic()
        #: Lines ``on_line`` never saw because its queue was full.
        self.dropped = 0
        self._lock = threading.Lock()
        self._on_line = on_line
        #: Each line, with how many chunks were recorded once it had ended.
        self._lines: "queue.Queue[Tuple[int, str]]" = queue.Queue(maxsize=_LINE_QUEUE_SIZE)
        #: The last :meth:`mark`: a line that ends past it is not handed on.
        self._limit: Optional[int] = None
        self._closed = threading.Event()
        self._abandoned = False
        #: Set by ``detach``: what is read from now on is read and dropped.
        self._detached = False
        #: Cleared by :meth:`mark` until :meth:`detach` or :meth:`keep` says
        #: whether the lines past the mark are the run's.
        self._settled = threading.Event()
        self._settled.set()
        self._handler: Optional[threading.Thread] = None
        if on_line is not None:
            self._handler = threading.Thread(target=self._hand_on, daemon=True)
            self._handler.start()

    def pump(self, stream: io.BufferedIOBase) -> None:
        """Read ``stream`` to its end, a chunk at a time.

        Whatever arrives counts as output, a newline or not: a CLI printing
        dots, or a progress bar redrawn with ``\\r``, is alive. Read a line at
        a time, it was taken for silent until it ended a line (#273). Bytes
        are decoded as UTF-8 and newlines read as a text pipe would read them
        (``\\r\\n`` and ``\\r`` become ``\\n``).
        """
        utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        decoder = io.IncrementalNewlineDecoder(utf8, translate=True)
        # The pieces of a line not yet ended, joined once it is.
        partial: List[str] = []
        try:
            while True:
                data = stream.read1(_READ_CHUNK)
                if self._detached:
                    # Keep the pipe drained, so its writer never blocks on
                    # it, but keep nothing: no one will read it.
                    if not data:
                        break
                    continue
                text = decoder.decode(data, final=not data)
                with self._lock:
                    if self._detached:
                        continue
                    if text:
                        self.chunks.append(text)
                    if data:
                        self.last_output_at = time.monotonic()
                    recorded = len(self.chunks)
                if self._handler is not None:
                    *complete, rest = text.split("\n")
                    for line in complete:
                        partial.append(line)
                        self._hand(recorded, "".join(partial) + "\n")
                        partial = []
                    if rest:
                        partial.append(rest)
                    if not data and partial:
                        # The last line, which never ended.
                        self._hand(recorded, "".join(partial))
                if not data:
                    break
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def mark(self) -> int:
        """Where the output recorded so far ends, for :meth:`detach`.

        From now on ``on_line`` is handed a line that ends past here only
        once :meth:`keep` says it is the run's, so it never sees what
        :meth:`detach` is about to drop.
        """
        with self._lock:
            self._limit = len(self.chunks)
            self._settled.clear()
            return self._limit

    def keep(self) -> None:
        """Undo :meth:`mark`: everything read is the run's, past it or not."""
        with self._lock:
            self._limit = None
        self._settled.set()

    def detach(self, mark: int) -> int:
        """Stop keeping output: drop what was recorded after ``mark`` and
        everything still to come. The reader goes on reading, so whatever
        still writes to the pipe never blocks on it. Lines ``on_line`` has
        not seen yet are still handed on up to ``mark``, by :meth:`finish`.
        Returns how many characters were dropped.

        For output that is no longer the run's -- what a process the CLI left
        behind writes after the CLI exited -- which would otherwise collect
        here for as long as that process lives.
        """
        with self._lock:
            self._detached = True
            self._limit = mark if self._limit is None else min(self._limit, mark)
            dropped = sum(len(chunk) for chunk in self.chunks[mark:])
            del self.chunks[mark:]
        self._settled.set()
        return dropped

    def _hand(self, recorded: int, line: str) -> None:
        """Queue ``line``, which had ended once ``recorded`` chunks were kept,
        for ``on_line``, or count it dropped if the queue is full."""
        try:
            self._lines.put_nowait((recorded, line))
        except queue.Full:
            self.dropped += 1

    def _hand_on(self) -> None:
        on_line = cast(Callable[[str], None], self._on_line)
        while not self._abandoned:
            try:
                recorded, line = self._lines.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                # ``_closed`` is set only after the last put, so a closed
                # drain with an empty queue has nothing more coming.
                if self._closed.is_set() and self._lines.empty():
                    return
                continue
            if self._abandoned:
                return
            with self._lock:
                limit = self._limit
            if limit is not None and recorded > limit:
                # Past the mark: whether it is the run's is not known yet.
                while not self._settled.wait(_POLL_SECONDS):
                    if self._abandoned:
                        return
                with self._lock:
                    limit = self._limit
                if limit is not None and recorded > limit:
                    continue  # not the run's output
            try:
                on_line(line)
            except Exception:
                pass

    def finish(self, timeout: float) -> None:
        """Wait up to ``timeout`` for ``on_line`` to see the lines queued so
        far, then stop handing any on: a callback still behind by then loses
        the rest rather than running after the run is over."""
        if self._handler is None:
            return
        self._closed.set()
        self._handler.join(timeout=timeout)
        self._abandoned = True

    def text(self) -> str:
        with self._lock:
            return "".join(self.chunks)

    def last_seen(self) -> float:
        with self._lock:
            return self.last_output_at


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


def end_children_on_sigterm(note_path: Optional[str] = None) -> None:
    """Make SIGTERM end the CLI :func:`execute` is running, and its tree,
    before this process exits.

    A detached worker installs this: its CLI runs in a session of its own, so
    the kill that reaches the worker's group never reaches the CLI. A no-op on
    Windows, where taskkill already ends the whole tree. A CLI group that is
    not confirmed gone is named in ``note_path``, since a worker's stderr goes
    nowhere.
    """
    if IS_WINDOWS:
        return
    _stop.note_path = note_path
    signal.signal(signal.SIGTERM, _on_sigterm)


def _on_sigterm(signum: int, frame: Any) -> None:
    _stop.requested = True
    if _stop.spawning:
        # execute() registers the new CLI, then honours the request.
        return
    _stop_now()


def _honour_stop() -> None:
    """Act on a SIGTERM that arrived while a CLI was being started."""
    if _stop.requested:
        _stop_now()


def _stop_now() -> None:
    for proc in _stop.active[:]:
        # Already reaped: taken as gone, as terminate_tree does.
        if proc.returncode is None and not _stop_group(proc.pid, _STOP_GRACE_SECONDS):
            _note("warning: the CLI's process group (pid %d) may still be running\n" % proc.pid)
    # As the default action would: no cleanup, no record written.
    os._exit(128 + signal.SIGTERM)


def _stop_group(pgid: int, grace: float) -> bool:
    """End group ``pgid`` from the SIGTERM handler. True once it is confirmed
    gone.

    The steps of :func:`_end_tree`, but without ``Popen.wait``: the handler
    may have interrupted the main thread inside ``proc.wait``, holding the
    lock a nested wait would need. The leader is reaped directly instead,
    since an unreaped one keeps its group present.
    """
    if pgid <= 1:
        # killpg(0) is our own group and killpg(1) is init's.
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        # An emptied id may be reused: signal it only while it has members.
        if sig == signal.SIGKILL and not _group_alive(pgid):
            return True
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        if _group_ends(pgid, time.monotonic() + grace / 2, reap=True):
            return True
    return False


def _note(text: str) -> None:
    """Best-effort line from the signal handler, to stderr and to the note
    file: raw writes, since ``sys.stderr`` may be mid-write in the code it
    interrupted, and no lock, since the code it interrupted may hold it."""
    data = text.encode("utf-8", "replace")
    try:
        os.write(2, data)
    except OSError:
        pass
    if not _stop.note_path:
        return
    try:
        fd = os.open(_stop.note_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError:
        pass


def terminate_tree(proc: subprocess.Popen, grace: float = KILL_GRACE_SECONDS) -> bool:
    """Kill the child *and its descendants*. True if everything exited: on
    POSIX, its whole process group.

    A child that has already been reaped is taken as gone without looking at
    its group: it may have been reaped long ago, and its id may now be someone
    else's group.
    """
    if proc.poll() is not None:
        return True

    def exited(timeout: float) -> bool:
        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def kill() -> None:
        try:
            proc.kill()
        except OSError:
            pass

    return _end_tree(proc.pid, grace, exited, kill)


def kill_tree(pid: int, grace: float = KILL_GRACE_SECONDS, verified: bool = True) -> bool:
    """:func:`terminate_tree` for a process known only by its pid, such as a
    detached worker. True once it is confirmed gone, not when it was signalled.

    On POSIX "gone" means its whole process group, not just the pid.

    The pid is only a number read back from a record, so on POSIX its group is
    signalled only while it still leads its own group, as a worker started by
    :func:`spawn_kwargs` does, or, once it has exited under this call, while
    the group it led still has members (checked immediately before each
    signal). Anything else is more likely a reused pid than our worker, and is
    left alone (False).

    ``verified`` is whether the caller confirmed the pid is still the process
    it means. An unverified pid is signalled only where the check above can
    stand in for that: on Windows it is left alone (False), as taskkill /T /F
    would end whatever tree has the pid by now.
    """
    if not pid_alive(pid):
        return True
    if IS_WINDOWS and not verified:
        return False
    if not IS_WINDOWS:
        try:
            if os.getpgid(pid) != pid:
                return False
        except ProcessLookupError:
            # It exited after the check above. A group with that id cannot be
            # reused while it has members, so ESRCH proves it gone; one that is
            # still there was never seen being led by our worker, so it is not
            # signalled.
            return not _group_alive(pid)
        except OSError:
            return False

    def exited(timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            _reap(pid)
            if not pid_alive(pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_SECONDS)

    def kill() -> None:
        try:
            os.kill(pid, signal.SIGTERM if IS_WINDOWS else signal.SIGKILL)
        except OSError:
            pass

    return _end_tree(pid, grace, exited, kill)


def _end_tree(pid: int, grace: float, exited: Callable[[float], bool], kill: Callable[[], None]) -> bool:
    """Signal ``pid``'s whole tree, escalating, until ``exited`` says it is gone.

    ``exited(timeout)`` waits up to ``timeout`` for the process to exit and
    says whether it did; ``kill`` is the last resort, for the process alone.
    On POSIX "gone" means the whole process group ``pid`` leads: a member that
    ignores SIGTERM after its leader exited still gets SIGKILL.
    """
    if IS_WINDOWS:
        # taskkill is the only reliable way to reach grandchildren on Windows.
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=grace,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        if pid <= 1:
            # killpg(0) is our own group and killpg(1) is init's.
            return False
        # Both callers start or verify a session leader, so its pid is the
        # group id. Looking it up through getpgid would fail once the leader
        # has been reaped, while its group may still have members.
        leader_gone = False
        for sig in (signal.SIGTERM, signal.SIGKILL):
            # Once the leader has exited, signal its group only while that
            # group is still there: an emptied id may be reused.
            if leader_gone and not _group_alive(pid):
                return True
            try:
                os.killpg(pid, sig)
            except OSError:
                break
            deadline = time.monotonic() + grace / 2
            leader_gone = exited(grace / 2)
            if leader_gone and _group_ends(pid, deadline):
                return True
    # The tree may already be gone (taskkill done, or the group no longer
    # there to signal); then the pid may be someone else's by now.
    if exited(0):
        return _tree_gone(pid)
    kill()
    return exited(grace) and _tree_gone(pid)


def _tree_gone(pid: int) -> bool:
    """Whether the group ``pid`` led is confirmed gone; taskkill already
    waited for the tree on Windows."""
    return IS_WINDOWS or not _group_alive(pid)


def _reap(pid: int) -> None:
    """Collect ``pid`` if it is a child of this process that has exited: until
    then it is a zombie, which still answers as alive."""
    if IS_WINDOWS:
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:  # not our child, or already collected
        pass


def _group_alive(pgid: int) -> bool:
    """Whether process group ``pgid`` is *not confirmed gone*.

    Only ESRCH proves the group has no members. A zombie member counts as
    present until it is reaped, and EPERM (which macOS can return for a group
    of zombies) or any other error proves nothing, so all of these are True.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _group_ends(pgid: int, deadline: float, reap: bool = False) -> bool:
    """Wait until ``deadline`` for group ``pgid`` to be confirmed gone.

    It probes at least once, so a leader that used the whole window to exit
    still gets its emptied group noticed. ``reap`` collects the leader, our
    child, before each probe.
    """
    while True:
        if reap:
            _reap(pgid)
        if not _group_alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SECONDS)


def execute(
    command: Sequence[str],
    cwd: str,
    prompt: str = "",
    timeout: Optional[float] = 1800.0,
    idle_timeout: Optional[float] = None,
    env: Optional[Dict[str, str]] = None,
    on_line: Optional[Callable[[str], None]] = None,
) -> ExecOutcome:
    """Run ``command``, feeding ``prompt`` on stdin, and always return.

    ``idle_timeout`` is only meaningful for a command that streams progress;
    pass ``None`` to disable it. ``on_line`` is called with each line of
    stdout, never stderr, on a thread of its own; what it raises is ignored,
    and a callback that falls far behind misses lines rather than slowing the
    run (see :class:`_Drain`).
    """
    # Only the charge reads this clock; the deadlines below stay on
    # ``time.monotonic()``.
    watch = clocks.Stopwatch()
    started = time.monotonic()
    watch.start()
    try:
        proc = _spawn(command, cwd, env)
    except OSError as exc:
        return ExecOutcome(EXIT_SPAWN_FAILED, "", str(exc), time.monotonic() - started, started=False)

    try:
        out, err = _Drain(on_line), _Drain()
        threads = [
            threading.Thread(target=out.pump, args=(proc.stdout,), daemon=True),
            threading.Thread(target=err.pump, args=(proc.stderr,), daemon=True),
            # stdin gets its own thread: a review prompt carries an inlined diff,
            # which is much larger than a pipe buffer, so a child that has not
            # started reading yet would otherwise block us here.
            threading.Thread(target=_feed_stdin, args=(proc, prompt), daemon=True),
        ]
        for thread in threads:
            thread.start()

        watched = _watch(proc, started, timeout, idle_timeout, out, err)

        orphans = False
        killed = watched.timed_out or watched.stalled
        marks = (0, 0)
        left_behind = False
        if killed:
            orphans = not terminate_tree(proc)
        else:
            # Where the CLI's own output ends, should something it started
            # still be writing: what that writes later is not the run's.
            _settle(threads[:2], out, err)
            marks = out.mark(), err.mark()
            # On POSIX the group outlives its leader: a member left now is a
            # process the CLI started, whatever it does with the pipes.
            left_behind = not IS_WINDOWS and _group_alive(proc.pid)

        # Bounded join: if a survivor still holds a pipe, abandon the readers
        # rather than waiting on them. They are daemons and cannot outlive us.
        _join(threads, KILL_GRACE_SECONDS)
        readers = threads[:2]
        lingering = not killed and any(t.is_alive() for t in readers)
        discarded = 0
        if lingering or left_behind:
            # Whatever came after the marks was written after the CLI exited,
            # by something it started, and is not the run's: it is cut off
            # whether that writer still holds the pipe or closed it meanwhile.
            discarded = out.detach(marks[0]) + err.detach(marks[1])
        elif not killed:
            # Nothing is known to have outlived the CLI: what the readers took
            # in after the marks is the CLI's own, read late by a slow reader.
            out.keep()
            err.keep()
        if lingering:
            # The CLI exited, but something it started still holds its output:
            # a dev server left running in the background, say. Nothing would
            # ever end it.
            orphans = True
            if _end_leftovers(proc):
                _join(readers, KILL_GRACE_SECONDS)
                orphans = any(t.is_alive() for t in readers)
        # So whoever reads the sink next sees every line it is going to see.
        out.finish(_ON_LINE_GRACE_SECONDS)

        _close_pipes(proc, threads)

        duration = time.monotonic() - started
        suspended = watch.read(duration)
        exit_code, stderr = _verdict(
            proc,
            watched,
            err.text(),
            duration,
            idle_timeout,
            orphans,
            lingering=lingering,
            discarded=discarded,
        )

        return ExecOutcome(
            exit_code,
            out.text(),
            stderr,
            duration,
            timed_out=watched.timed_out,
            stalled=watched.stalled,
            idle_for=watched.idle_for,
            orphans_possible=orphans,
            suspended=suspended,
        )
    finally:
        _stop.active.remove(proc)


def _spawn(command: Sequence[str], cwd: str, env: Optional[Dict[str, str]]) -> subprocess.Popen:
    """Start ``command`` in a group of its own and register it for the SIGTERM
    handler. An ``OSError`` from ``Popen`` is raised once a pending stop has
    been honoured."""
    # A SIGTERM that arrives before the CLI is registered is deferred to
    # _honour_stop() below, so it cannot miss the CLI being started.
    _stop.spawning = True
    try:
        proc = subprocess.Popen(
            launchable(command),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **spawn_kwargs(),
        )
        _stop.active.append(proc)
    except OSError:
        _stop.spawning = False
        _honour_stop()
        raise
    finally:
        _stop.spawning = False
    _honour_stop()
    return proc


class _Watched(NamedTuple):
    """How waiting for a child ended."""

    timed_out: bool
    stalled: bool
    idle_for: float


def _watch(
    proc: subprocess.Popen,
    started: float,
    timeout: Optional[float],
    idle_timeout: Optional[float],
    out: _Drain,
    err: _Drain,
) -> _Watched:
    """Wait for ``proc`` to exit, or for one of its deadlines to pass."""
    timed_out = stalled = False
    idle_for = 0.0
    while True:
        if proc.poll() is not None:
            break
        now = time.monotonic()
        if timeout is not None and now - started >= timeout:
            timed_out = True
            break
        if idle_timeout is not None:
            idle_for = now - max(out.last_seen(), err.last_seen())
            if idle_for >= idle_timeout:
                stalled = True
                break
        time.sleep(_POLL_SECONDS)
    return _Watched(timed_out, stalled, idle_for)


def _settle(readers: Sequence[threading.Thread], out: _Drain, err: _Drain) -> None:
    """After the child exits, let the readers take in what it wrote last.

    Up to a pipe buffer of it may not have been read yet. Returns once both
    readers are done -- at once, in the usual case -- or once nothing new has
    arrived for a moment, or after ``_SETTLE_SECONDS``. The moment is
    counted from no earlier than now, so a reader not yet scheduled when the
    child exited still gets it.
    """
    settling = time.monotonic()
    deadline = settling + _SETTLE_SECONDS
    while any(thread.is_alive() for thread in readers):
        now = time.monotonic()
        quiet = now - max(out.last_seen(), err.last_seen(), settling)
        if now >= deadline or quiet >= _SETTLE_QUIET_SECONDS:
            return
        time.sleep(_SETTLE_QUIET_SECONDS / 4)


def _join(threads: Sequence[threading.Thread], budget: float) -> None:
    """Wait for ``threads`` to end, spending at most ``budget`` seconds on them."""
    for thread in threads:
        thread.join(timeout=budget / len(threads))


def _end_leftovers(proc: subprocess.Popen) -> bool:
    """End what is left of the group of ``proc``, which has exited. True once
    it is confirmed gone.

    On POSIX the group outlives its leader and can still be signalled: its id
    cannot be reused while it has members, and ``_stop_group`` checks that it
    still has some before its SIGKILL. A holder that left the group (setsid)
    is out of reach, and so is everything on Windows, where the tree hangs off
    a process that no longer exists for ``taskkill /T`` to start from: both
    answer False.
    """
    if IS_WINDOWS or not _group_alive(proc.pid):
        return False
    return _stop_group(proc.pid, KILL_GRACE_SECONDS)


def _close_pipes(proc: subprocess.Popen, threads: Sequence[threading.Thread]) -> None:
    """Close each pipe whose thread (stdout, stderr, stdin, in that order)
    has finished with it.

    A pipe whose thread is still blocked on it is left alone: ``close()``
    waits for the stream's lock, which the blocked read or write holds until
    the far end goes away -- for a process the CLI left running, perhaps
    never. The daemon thread keeps it until we exit.
    """
    for pipe, thread in zip((proc.stdout, proc.stderr, proc.stdin), threads, strict=True):
        if thread.is_alive():
            continue
        try:
            if pipe is not None and not pipe.closed:
                pipe.close()
        except (OSError, ValueError):
            pass


def _verdict(
    proc: subprocess.Popen,
    watched: _Watched,
    stderr: str,
    duration: float,
    idle_timeout: Optional[float],
    orphans: bool,
    lingering: bool = False,
    discarded: int = 0,
) -> Tuple[int, str]:
    """The exit code to report, and ``stderr`` with what this module decided
    put around it. ``lingering``: the child exited, but something it started
    still held its output; ``discarded`` characters it wrote after the child
    exited were dropped."""
    timed_out, stalled, idle_for = watched
    exit_code = proc.poll()
    if stalled:
        exit_code = EXIT_IDLE_STALL
    elif timed_out:
        exit_code = EXIT_TOTAL_TIMEOUT
    elif exit_code is None:
        exit_code = EXIT_SPAWN_FAILED

    if stalled:
        stderr = (
            "no output for %.0fs (idle limit %.0fs); treated as stalled\n" % (idle_for, idle_timeout)
        ) + stderr
    elif timed_out:
        stderr = ("timed out after %.0fs\n" % duration) + stderr
    if lingering:
        if orphans:
            stderr += (
                "\nwarning: the CLI exited but a process it started still holds its output and may"
                " still be running; check for orphans\n"
            )
        else:
            stderr += (
                "\nwarning: the CLI exited but a process it started still held its output; it was stopped\n"
            )
        if discarded:
            stderr += "warning: %d characters it wrote after the CLI exited were dropped\n" % discarded
    elif discarded:
        stderr += (
            "\nwarning: a process the CLI started wrote %d characters after the CLI exited;"
            " they were dropped\n" % discarded
        )
    elif orphans:
        stderr += "\nwarning: the process group did not exit after being killed; check for orphans\n"
    return exit_code, stderr


def _feed_stdin(proc: subprocess.Popen, prompt: str) -> None:
    """Write ``prompt`` to the child's stdin as UTF-8, its newlines as they are.

    The pipes are binary: on Windows a text pipe turns every ``\\n`` into
    ``\\r\\n``, so a diff of a CRLF file reached a reviewer as ``\\r\\r\\n``
    (#270). agy's prompt file is written the same way.
    """
    # _spawn opens every pipe in binary mode.
    stdin = cast(IO[bytes], proc.stdin)
    try:
        if prompt:
            stdin.write(prompt.encode("utf-8", "replace"))
        stdin.close()
    except (OSError, ValueError):
        pass


def pid_alive(pid: int) -> bool:
    """Best-effort liveness check for a recorded pid.

    Used to notice a stage whose process died without recording an outcome. PIDs
    are recycled, so a true answer is not proof that it is *our* process.
    """
    if pid <= 0:
        return False
    if IS_WINDOWS:
        # Windows only: ctypes.windll exists nowhere else.
        import ctypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        # Opening a process is not the same as it running. A terminated process
        # keeps its object, and its pid, for as long as anyone holds a handle to
        # it, so OpenProcess alone answered "alive" for a worker taskkill had
        # already reported dead -- measured: taskkill returned 0, tasklist no
        # longer listed the pid, and this said True for as long as it was asked.
        # Nothing ever cleared such a stage, because the wait that SYNCHRONIZE
        # is requested for was never done.
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_started(pid: int) -> Optional[str]:
    """When the process ``pid`` started, as an opaque token, or None where that
    cannot be read.

    A pid alone is someone else's once it is reused; the pid with this token
    is not. Tokens are only compared for equality, and only with one read on
    the same machine: Windows gives the creation time, Linux the start tick
    since boot together with the boot's id. Elsewhere (macOS) there is nothing
    to read without spawning ``ps``, and the answer is None.
    """
    if pid <= 0:
        return None
    if IS_WINDOWS:
        # Windows only: ctypes.windll exists nowhere else.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            # FILETIMEs, each read whole as a 64-bit count of 100ns.
            created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
            if not ctypes.windll.kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
            ):
                return None
            return str(created.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        with open("/proc/%d/stat" % pid, encoding="ascii", errors="replace") as handle:
            stat = handle.read()
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii", errors="replace") as handle:
            boot = handle.read().strip()
    except OSError:
        return None
    # The command name is in parentheses and may itself hold spaces or ")";
    # the fields after its last ")" begin with the third, so the start time
    # (the 22nd) is the 20th of them.
    fields = stat.rpartition(")")[2].split()
    if len(fields) < 20:
        return None
    return "%s/%s" % (boot, fields[19])
