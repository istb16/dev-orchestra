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

import os
import signal
import subprocess
import sys
import threading
import time
from typing import IO, Any, Callable, Dict, List, Optional, Sequence, cast

from . import clocks

#: Exit codes this module reports for its own decisions. Chosen to stay clear
#: of the 0-127 range a child is likely to use for its own reasons.
EXIT_TOTAL_TIMEOUT = 124  # conventional timeout(1) code
EXIT_IDLE_STALL = 125
EXIT_SPAWN_FAILED = 126

#: How long to let a killed group settle before abandoning its reader threads.
KILL_GRACE_SECONDS = 5.0
_POLL_SECONDS = 0.2

IS_WINDOWS = sys.platform.startswith("win")

#: The CLIs :func:`execute` is running now, for the SIGTERM handler a detached
#: worker installs. A worker runs one CLI at a time from its main thread, where
#: signal handlers also run, so the handler only ever runs between this
#: module's bytecodes, never alongside them. No lock: a handler waiting on a
#: lock held by the code it interrupted would deadlock.
_active: List[subprocess.Popen] = []
#: Set while a CLI is being started and not yet in ``_active``.
_spawning = False
_stop_requested = False
#: Where the handler says which CLI group it could not end: a file beside the
#: job record, which ``jobs cancel`` reads once the worker has gone. None
#: outside a worker.
_stop_note_path: Optional[str] = None
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
        #: The group did not fully exit after being killed.
        self.orphans_possible = orphans_possible

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
    """Reads one stream, remembering when the last byte arrived."""

    def __init__(self) -> None:
        self.chunks: List[str] = []
        self.last_output_at = time.monotonic()
        self._lock = threading.Lock()

    def pump(self, stream) -> None:
        try:
            for line in iter(stream.readline, ""):
                with self._lock:
                    self.chunks.append(line)
                    self.last_output_at = time.monotonic()
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

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


def end_children_on_sigterm(note_path: Optional[str] = None) -> None:
    """Make SIGTERM end the CLI :func:`execute` is running, and its tree,
    before this process exits.

    A detached worker installs this: its CLI runs in a session of its own, so
    the kill that reaches the worker's group never reaches the CLI. A no-op on
    Windows, where taskkill already ends the whole tree. A CLI group that is
    not confirmed gone is named in ``note_path``, since a worker's stderr goes
    nowhere.
    """
    global _stop_note_path
    if IS_WINDOWS:
        return
    _stop_note_path = note_path
    signal.signal(signal.SIGTERM, _on_sigterm)


def _on_sigterm(signum: int, frame: Any) -> None:
    global _stop_requested
    _stop_requested = True
    if _spawning:
        # execute() registers the new CLI, then honours the request.
        return
    _stop_now()


def _honour_stop() -> None:
    """Act on a SIGTERM that arrived while a CLI was being started."""
    if _stop_requested:
        _stop_now()


def _stop_now() -> None:
    for proc in _active[:]:
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
    if not _stop_note_path:
        return
    try:
        fd = os.open(_stop_note_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
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


def kill_tree(pid: int, grace: float = KILL_GRACE_SECONDS) -> bool:
    """:func:`terminate_tree` for a process known only by its pid, such as a
    detached worker. True once it is confirmed gone, not when it was signalled.

    On POSIX "gone" means its whole process group, not just the pid.

    The pid is only a number read back from a record, so on POSIX its group is
    signalled only while it still leads its own group, as a worker started by
    :func:`spawn_kwargs` does, or, once it has exited under this call, while
    the group it led still has members (checked immediately before each
    signal). Anything else is more likely a reused pid than our worker, and is
    left alone (False).
    """
    if not pid_alive(pid):
        return True
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
) -> ExecOutcome:
    """Run ``command``, feeding ``prompt`` on stdin, and always return.

    ``idle_timeout`` is only meaningful for a command that streams progress;
    pass ``None`` to disable it.
    """
    global _spawning
    # Only the charge reads this clock; the deadlines below stay on
    # ``time.monotonic()``.
    watch = clocks.Stopwatch()
    started = time.monotonic()
    watch.start()
    # A SIGTERM that arrives before the CLI is registered is deferred to
    # _honour_stop() below, so it cannot miss the CLI being started.
    _spawning = True
    try:
        proc = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            **spawn_kwargs(),
        )
        _active.append(proc)
    except OSError as exc:
        _spawning = False
        _honour_stop()
        return ExecOutcome(EXIT_SPAWN_FAILED, "", str(exc), time.monotonic() - started)
    finally:
        _spawning = False
    _honour_stop()

    try:
        out, err = _Drain(), _Drain()
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

        orphans = False
        if timed_out or stalled:
            orphans = not terminate_tree(proc)

        # Bounded join: if a survivor still holds a pipe, abandon the readers
        # rather than waiting on them. They are daemons and cannot outlive us.
        for thread in threads:
            thread.join(timeout=KILL_GRACE_SECONDS / len(threads))

        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe is not None and not pipe.closed:
                    pipe.close()
            except (OSError, ValueError):
                pass

        duration = time.monotonic() - started
        suspended = watch.read(duration)
        exit_code = proc.poll()
        if stalled:
            exit_code = EXIT_IDLE_STALL
        elif timed_out:
            exit_code = EXIT_TOTAL_TIMEOUT
        elif exit_code is None:
            exit_code = EXIT_SPAWN_FAILED

        stderr = err.text()
        if stalled:
            stderr = (
                "no output for %.0fs (idle limit %.0fs); treated as stalled\n" % (idle_for, idle_timeout)
            ) + stderr
        elif timed_out:
            stderr = ("timed out after %.0fs\n" % duration) + stderr
        if orphans:
            stderr += "\nwarning: the process group did not exit after being killed; check for orphans\n"

        return ExecOutcome(
            exit_code,
            out.text(),
            stderr,
            duration,
            timed_out=timed_out,
            stalled=stalled,
            idle_for=idle_for,
            orphans_possible=orphans,
            suspended=suspended,
        )
    finally:
        _active.remove(proc)


def _feed_stdin(proc: subprocess.Popen, prompt: str) -> None:
    # Every caller opens stdin as a text pipe.
    stdin = cast(IO[str], proc.stdin)
    try:
        if prompt:
            stdin.write(prompt)
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
