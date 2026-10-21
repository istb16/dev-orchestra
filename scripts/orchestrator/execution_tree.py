"""Stopping a child CLI's whole tree: process groups on POSIX, taskkill on
Windows, the SIGTERM handler a detached worker installs, and whether a
recorded pid is still the process it was.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any, Callable, List, Optional

from .execution_common import _POLL_SECONDS, IS_WINDOWS

#: How long to let a killed group settle before abandoning its reader threads.
KILL_GRACE_SECONDS = 5.0


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


def _group_running(pgid: int) -> bool:
    """Whether process group ``pgid`` is *confirmed* to have a member that is
    still running.

    The opposite leaning to :func:`_group_alive`: a group confirmed gone, one
    whose members are all zombies, and an answer that proves nothing (EPERM,
    which macOS gives for a group of zombies, or any other error) are all
    False. Where ``/proc`` lists the processes, a member counts only if it is
    not a zombie; elsewhere a group that may be signalled counts as running.
    """
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    try:
        names = os.listdir("/proc")
    except OSError:
        return True
    if not os.path.exists("/proc/self/stat"):
        return True
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "rb") as f:
                stat = f.read()
        except OSError:  # gone meanwhile, or not ours to read
            continue
        # "pid (comm) state ppid pgrp ...": comm may hold anything, ")" too.
        fields = stat[stat.rfind(b")") + 2 :].split()
        if len(fields) < 3 or not fields[2].isdigit() or int(fields[2]) != pgid:
            continue
        if fields[0] not in (b"Z", b"X"):
            return True
    return False


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
