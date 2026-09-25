"""Project workspace helpers: repo root discovery, ``.ai/`` layout, run state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(
    args: Sequence[str],
    cwd: str,
    timeout: int = 60,
    env: Optional[Dict[str, str]] = None,
) -> "tuple[int, str, str]":
    """Run git and return (exit code, stdout, stderr).

    ``env`` is merged over the caller's environment rather than replacing it,
    so a caller can point ``GIT_INDEX_FILE`` at a scratch index without losing
    the git configuration the user's environment supplies.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **env} if env else None,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def is_git_repo(path: str) -> bool:
    code, out, _ = git(["rev-parse", "--is-inside-work-tree"], path)
    return code == 0 and out.strip() == "true"


def repo_root(start: Optional[str] = None) -> str:
    start = os.path.abspath(start or os.getcwd())
    code, out, _ = git(["rev-parse", "--show-toplevel"], start)
    if code == 0 and out.strip():
        return os.path.abspath(out.strip())
    return start


#: Subdirectory of the container holding one directory per workflow. Defined
#: here rather than in ``workflow`` so that module can import this one without
#: the import going both ways.
WORKFLOWS = "workflows"

#: Sub-directory of ``reviews/`` that holds the design review. Its own
#: directory rather than a prefix on the file names, so it gets its own
#: consolidated report and therefore its own round counter -- a design round
#: must never advance, or be refused by, the code review's count.
DESIGN_REVIEW = "design"


class Workspace:
    """Where one workflow's artifacts live, under a project's ``.ai/``.

    ``container`` is the ``.ai/`` directory itself, shared by the project;
    ``dir`` is this workflow's own directory inside it. Constructed without a
    workflow the two are the same, which is the pre-0.4.0 layout and what a
    caller that has no workflow to name still gets.

    ``review_scope`` narrows only the review artifacts to a sub-directory. The
    plan, the execution prompts, the run state and the ledger stay where they
    are: there is one of each per workflow whichever review is being run.
    """

    def __init__(
        self,
        root: str,
        directory: Optional[str] = None,
        workflow: str = "",
        review_scope: str = "",
    ) -> None:
        self.root = os.path.abspath(root)
        self.container = os.path.abspath(directory or os.path.join(self.root, ".ai"))
        self.workflow = (workflow or "").strip()
        self.dir = os.path.join(self.container, WORKFLOWS, self.workflow) if self.workflow else self.container
        self.review_scope = (review_scope or "").strip()

    # -- paths -------------------------------------------------------------

    @property
    def plan_path(self) -> str:
        return os.path.join(self.dir, "plan.md")

    @property
    def execution_dir(self) -> str:
        return os.path.join(self.dir, "execution")

    @property
    def reviews_dir(self) -> str:
        base = os.path.join(self.dir, "reviews")
        return os.path.join(base, self.review_scope) if self.review_scope else base

    @property
    def snapshot_path(self) -> str:
        # A frozen plan is markdown, not a diff, and is named as what it is.
        name = "review-target.md" if self.review_scope == DESIGN_REVIEW else "review-target.diff"
        return os.path.join(self.reviews_dir, name)

    @property
    def full_snapshot_path(self) -> str:
        """The whole change, kept alongside an incremental round's diff.

        A round that only sends the fix has to leave the reviewer somewhere to
        look for the change the fix belongs to. Writing it costs a git call and
        no tokens: it is read only if a reviewer decides it needs the context.
        """
        return os.path.join(self.reviews_dir, "review-target-full.diff")

    @property
    def snapshot_meta_path(self) -> str:
        return os.path.join(self.reviews_dir, "review-target.json")

    @property
    def surrounding_path(self) -> str:
        """The enclosing symbols frozen with a code snapshot.

        Written only with ``review.context.surrounding: enclosing``, from the
        same git tree the diff was taken from, so a reviewer is shown the code
        the diff describes rather than whatever the working tree says later.
        """
        return os.path.join(self.reviews_dir, "review-surrounding.json")

    @property
    def consolidated_md_path(self) -> str:
        return os.path.join(self.reviews_dir, "consolidated.md")

    @property
    def consolidated_json_path(self) -> str:
        return os.path.join(self.reviews_dir, "consolidated.json")

    @property
    def state_path(self) -> str:
        return os.path.join(self.dir, "state.json")

    def reviewer_report_path(self, reviewer_id: str) -> str:
        return os.path.join(self.reviews_dir, "%s.md" % reviewer_id)

    def design_review(self) -> "Workspace":
        """The same workflow, with the review artifacts scoped to the design."""
        return Workspace(self.root, self.container, self.workflow, review_scope=DESIGN_REVIEW)

    def relative(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.root).replace(os.sep, "/")
        except ValueError:  # pragma: no cover - different drives on Windows
            return path

    # -- lifecycle ---------------------------------------------------------

    def ensure(self) -> "Workspace":
        for directory in (self.container, self.dir, self.execution_dir, self.reviews_dir):
            os.makedirs(directory, exist_ok=True)
        # One ignore file for the container, so every workflow under it is
        # covered by the file the project has already seen.
        gitignore = os.path.join(self.container, ".gitignore")
        if not os.path.exists(gitignore):
            with open(gitignore, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    "# Working artifacts produced by dev-orchestra.\n"
                    "# Delete this file if you would rather commit them for team visibility.\n"
                    "*\n"
                )
        return self

    # -- state -------------------------------------------------------------

    def read_state(self) -> Dict[str, Any]:
        data = read_json(self.state_path, None)
        if not isinstance(data, dict):
            return {"version": 1, "runs": []}
        data.setdefault("runs", [])
        return data

    def write_state(self, state: Dict[str, Any]) -> None:
        self.ensure()
        write_json(self.state_path, state)

    def last_status(self, stage: str) -> str:
        """The most recent recorded status for one stage, or "".

        Read by the optimization gate, which has to tell "the tests failed"
        from "nobody said". The empty string is the second of those and is
        never treated as the first.
        """
        events = self.read_state().get("events") or []
        if not isinstance(events, list):
            return ""
        for event in reversed(events):
            if isinstance(event, dict) and event.get("stage") == stage:
                return str(event.get("status") or "")
        return ""

    def record_event(
        self, stage: str, status: str, detail: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Append one stage outcome to the run log (models included, secrets not).

        Locked, because this rewrites the whole state file to append one line
        and the ledger lives in that file too. Unlocked it read the state,
        another process committed a charge, and this wrote its own copy back
        over it: measured with eight concurrent charges, one of them vanished.
        The lock is not re-entrant, so a caller already holding it appends with
        :func:`new_event` instead of calling this.
        """
        event = new_event(stage, status, detail)
        with file_lock(self.state_path):
            state = self.read_state()
            append_event(state, event)
            self.write_state(state)
        return event


def new_event(stage: str, status: str, detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One run-log entry, built without touching the disk.

    Split out so a caller that already holds the state lock can append without
    taking it again -- :meth:`Workspace.record_event` is the same thing plus
    the read, the append and the write.
    """
    event = {"stage": stage, "status": status, "at": utcnow()}
    if detail:
        event.update(detail)
    return event


def append_event(state: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    """Add ``event`` to ``state``'s run log, in place."""
    state.setdefault("events", []).append(event)
    state["updated_at"] = utcnow()
    return state


def write_json(path: str, data: Any) -> None:
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


#: Reader patience for a file another process may be rewriting.
_READ_ATTEMPTS = 6

_IS_WINDOWS = sys.platform.startswith("win")


def _read_shared(path: str) -> str:
    """Read a file without blocking a concurrent rename over it.

    On Windows a handle opened by ``open()`` does not grant delete sharing, so
    a reader holding one makes ``os.replace`` fail -- a reader breaking a
    *writer*, which is worse than the torn read atomic writes were added to
    prevent. Asking ``CreateFileW`` for FILE_SHARE_DELETE fixes it at the
    source: renames succeed while we read, and every read sees one whole
    version of the file.

    Everywhere else a rename is already invisible to an open reader.
    """
    if not _IS_WINDOWS:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    import ctypes
    import ctypes.wintypes
    import msvcrt

    GENERIC_READ = 0x80000000
    FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004  # read | write | delete
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.restype = ctypes.wintypes.HANDLE
    handle = create_file(
        ctypes.c_wchar_p(path),
        GENERIC_READ,
        FILE_SHARE_ALL,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if not handle or handle == INVALID_HANDLE:
        raise OSError(ctypes.get_last_error(), "could not open %s for shared reading" % path)
    # open_osfhandle transfers ownership: closing the fd closes the handle.
    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        return stream.read()


def read_json(path: str, default: Any = None) -> Any:
    if not os.path.isfile(path):
        return default
    for attempt in range(_READ_ATTEMPTS):
        try:
            return json.loads(_read_shared(path))
        except (ValueError, OSError):
            # Writes are atomic, so this is rare -- but silently returning the
            # default turns "unreadable" into "absent", which is how a live job
            # once looked like a missing one. Retrying costs milliseconds.
            # On Windows a reader that lands in the instant a rename replaces
            # the file is refused outright, an OSError just as passing.
            if attempt == _READ_ATTEMPTS - 1 or not os.path.isfile(path):
                return default
            time.sleep(0.02 * (attempt + 1))
    return default


def write_text(path: str, text: str) -> None:
    _atomic_write(path, text)


def _atomic_write(path: str, text: str) -> None:
    """Write via a sibling temp file and one rename.

    ``os.replace`` is atomic on POSIX and Windows alike, so a reader sees either
    the old content or the new one, never a partial write. Detached workers and
    their parent share these files, and a reader landing in a truncate window
    used to get a parse error indistinguishable from "the file is not there".
    """
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=parent or None, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        _replace_with_retry(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _replace_with_retry(temporary: str, target: str, attempts: int = 25) -> None:
    """Rename over ``target``, working around Windows file sharing.

    On Windows ``os.replace`` is denied while another handle has the target
    open, and Python's ``open()`` does not grant delete sharing -- so a
    concurrent *reader* can make a *write* fail, which was worse than the torn
    read this was meant to prevent. Readers hold the file only momentarily, so
    a short backoff wins in practice.
    """
    last: Optional[OSError] = None
    for attempt in range(attempts):
        try:
            os.replace(temporary, target)
            return
        except PermissionError as exc:  # Windows only
            last = exc
            time.sleep(min(0.01 * (attempt + 1), 0.1))

    # Out of retries. Writing in place gives up atomicity, but losing the write
    # would be worse, and readers already retry on a parse error.
    try:
        with open(temporary, "r", encoding="utf-8") as source:
            content = source.read()
        with open(target, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.unlink(temporary)
        return
    except OSError:
        pass
    if last is not None:
        raise last


class file_lock:  # lowercase: it is used as a context manager, not a value
    """A best-effort exclusive lock for read-modify-write on a shared file.

    Atomic writes stop a reader seeing half a file; they do not stop two writers
    from each reading, editing and writing back, losing one of the edits. That
    became reachable once detached workers started sharing the run state with
    their parent.

    Deliberately simple: an O_EXCL lock file, a bounded wait, and a stale-lock
    break. Failing to acquire is not fatal -- proceeding unlocked keeps the tool
    working at the cost of the guarantee, which beats deadlocking.

    **The wait is bounded per holder, not per caller.** It used to be five
    seconds in total, which silently made the guarantee depend on how many
    writers were queued ahead: measured at twelve contenders holding for half a
    second each, two of them gave up and clobbered each other's edit. A
    deadline is there to survive a holder that died, and a queue moving along
    is not that -- so every time the lock visibly changes hands the deadline is
    pushed out again, and only a lock that sits unchanged runs it down.
    ``max_wait`` is the backstop for the case that is neither: a holder alive
    enough to keep the file fresh and stuck enough never to release it.

    Each acquisition writes a token nobody else will repeat, because "the lock
    changed hands" cannot be read from the pid when the contenders are threads
    of one process -- which is the ordinary case here.
    """

    def __init__(
        self,
        path: str,
        timeout: float = 5.0,
        stale_after: float = 30.0,
        max_wait: float = 60.0,
    ) -> None:
        self.path = os.path.abspath(path) + ".lock"
        self.timeout = timeout
        self.stale_after = stale_after
        self.max_wait = max_wait
        self.acquired = False

    def _holder(self) -> str:
        """Who holds the lock, or "" if nobody does.

        Through ``_read_shared``, which is the whole reason that function
        exists: a plain ``open`` on Windows does not grant delete sharing, so
        reading the lock file to see whether it moved stopped its holder from
        unlinking it. The holder then failed to release, every waiter ran its
        deadline down against a lock that would never move, and a poll meant
        to detect progress prevented it. Measured at twelve contenders: ten
        proceeded unlocked.
        """
        try:
            return _read_shared(self.path)[:64]
        except (OSError, UnicodeDecodeError):
            return ""

    def __enter__(self) -> "file_lock":
        started = time.monotonic()
        deadline = started + self.timeout
        seen = ""
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        token = "%d:%s" % (os.getpid(), uuid.uuid4().hex)
        while True:
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(handle, token.encode("ascii"))
                os.close(handle)
                self.acquired = True
                return self
            except FileExistsError:
                if self._break_if_stale():
                    continue
                now = time.monotonic()
                holder = self._holder()
                if holder and holder != seen:
                    # It changed hands, so waiting is getting somewhere.
                    seen = holder
                    deadline = now + self.timeout
                if now >= deadline or now - started >= self.max_wait:
                    return self
                time.sleep(0.05)
            except OSError:
                # On Windows an O_EXCL open can fail with a sharing or
                # permission error while another thread is unlinking the same
                # file. That is contention, not a broken filesystem, so it
                # waits like contention rather than giving up the guarantee.
                now = time.monotonic()
                if now >= deadline or now - started >= self.max_wait:
                    return self
                time.sleep(0.05)

    def _break_if_stale(self) -> bool:
        try:
            age = time.time() - os.path.getmtime(self.path)
        except OSError:
            return True
        if age <= self.stale_after:
            return False
        try:
            os.unlink(self.path)
        except OSError:
            return False
        return True

    def __exit__(self, *exc_info) -> None:
        if not self.acquired:
            return
        try:
            os.unlink(self.path)
        except OSError:
            pass


def read_text(path: str, default: str = "") -> str:
    if not os.path.isfile(path):
        return default
    try:
        return _read_shared(path)
    except (OSError, UnicodeDecodeError):
        # Fall back to a tolerant read: report files may contain anything.
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return default


def list_files(directory: str, suffix: str = "") -> List[str]:
    if not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.endswith(suffix) and os.path.isfile(os.path.join(directory, name))
    )
