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

import subprocess
import threading
import time
from typing import IO, Any, Callable, Dict, NamedTuple, Optional, Sequence, Tuple, cast

from . import clocks, execution_drain, execution_launch, execution_tree
from .execution_common import _POLL_SECONDS, IS_WINDOWS
from .execution_launch import find_program  # noqa: F401 - re-exported
from .execution_tree import end_children_on_sigterm, pid_alive  # noqa: F401 - re-exported

#: Exit codes this module reports for its own decisions. Chosen to stay clear
#: of the 0-127 range a child is likely to use for its own reasons.
EXIT_TOTAL_TIMEOUT = 124  # conventional timeout(1) code
EXIT_IDLE_STALL = 125
EXIT_SPAWN_FAILED = 126

#: After the child exits, how long its readers get to take in what it wrote
#: last, and how long a quiet pipe must stay quiet to count as read.
_SETTLE_SECONDS = 1.0
_SETTLE_QUIET_SECONDS = 0.2
#: How long a finished run waits for ``on_line`` to catch up.
_ON_LINE_GRACE_SECONDS = 2.0

#: Set by ``Provider._child_env`` for every process this tool starts, so a
#: delegated Claude run that loads the user's settings keeps dev-orchestra's
#: own hooks silent (``reply_language``, ``claude_hooks.refresh``).
DELEGATED_ENV = "DEV_ORCHESTRA_DELEGATED"


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
        out, err = execution_drain._Drain(on_line), execution_drain._Drain()
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
        if killed:
            orphans = not execution_tree.terminate_tree(proc)
        else:
            # Where the CLI's own output ends, should something it started
            # still be writing: what that writes later is not the run's.
            _settle(threads[:2], out, err)
            marks = out.mark(), err.mark()

        # Bounded join: if a survivor still holds a pipe, abandon the readers
        # rather than waiting on them. They are daemons and cannot outlive us.
        _join(threads, execution_tree.KILL_GRACE_SECONDS)
        readers = threads[:2]
        lingering = not killed and any(t.is_alive() for t in readers)
        # On POSIX the group outlives its leader: a member still running once
        # the readers are done is a process the CLI started, whatever it does
        # with the pipes. Asked only now, so a member that was merely on its
        # way out when the CLI exited does not cost the CLI's own last output.
        left_behind = (
            not killed and not lingering and not IS_WINDOWS and execution_tree._group_running(proc.pid)
        )
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
                _join(readers, execution_tree.KILL_GRACE_SECONDS)
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
        execution_tree._stop.active.remove(proc)


def _spawn(command: Sequence[str], cwd: str, env: Optional[Dict[str, str]]) -> subprocess.Popen:
    """Start ``command`` in a group of its own and register it for the SIGTERM
    handler. An ``OSError`` from ``Popen`` is raised once a pending stop has
    been honoured."""
    # A SIGTERM that arrives before the CLI is registered is deferred to
    # _honour_stop() below, so it cannot miss the CLI being started.
    execution_tree._stop.spawning = True
    try:
        proc = subprocess.Popen(
            execution_launch.launchable(command),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **execution_launch.spawn_kwargs(),
        )
        execution_tree._stop.active.append(proc)
    except OSError:
        execution_tree._stop.spawning = False
        execution_tree._honour_stop()
        raise
    finally:
        execution_tree._stop.spawning = False
    execution_tree._honour_stop()
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
    out: execution_drain._Drain,
    err: execution_drain._Drain,
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


def _settle(
    readers: Sequence[threading.Thread], out: execution_drain._Drain, err: execution_drain._Drain
) -> None:
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
    if IS_WINDOWS or not execution_tree._group_alive(proc.pid):
        return False
    return execution_tree._stop_group(proc.pid, execution_tree.KILL_GRACE_SECONDS)


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
