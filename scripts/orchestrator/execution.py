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


def terminate_tree(proc: subprocess.Popen, grace: float = KILL_GRACE_SECONDS) -> bool:
    """Kill the child *and its descendants*. True if everything exited."""
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

    The pid is only a number read back from a record, so on POSIX it is
    signalled only while it still leads its own group, as a worker started by
    :func:`spawn_kwargs` does; anything else is more likely a reused pid than
    our worker, and is left alone (False).
    """
    if not pid_alive(pid):
        return True
    if not IS_WINDOWS:
        try:
            if os.getpgid(pid) != pid:
                return False
        except ProcessLookupError:
            return True
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
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                break
            if exited(grace / 2):
                return True
    # The tree may already be gone (taskkill done, or the group no longer
    # there to signal); then the pid may be someone else's by now.
    if exited(0):
        return True
    kill()
    return exited(grace)


def _reap(pid: int) -> None:
    """Collect ``pid`` if it is a child of this process that has exited: until
    then it is a zombie, which still answers as alive."""
    if IS_WINDOWS:
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:  # not our child, or already collected
        pass


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
    # Only the charge reads this clock; the deadlines below stay on
    # ``time.monotonic()``.
    watch = clocks.Stopwatch()
    started = time.monotonic()
    watch.start()
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
    except OSError as exc:
        return ExecOutcome(EXIT_SPAWN_FAILED, "", str(exc), time.monotonic() - started)

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
