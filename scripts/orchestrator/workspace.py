"""Project workspace helpers: repo root discovery, ``.ai/`` layout, run state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(args: Sequence[str], cwd: str, timeout: int = 60) -> "tuple[int, str, str]":
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


class Workspace:
    """The ``.ai/`` directory for one project."""

    def __init__(self, root: str, directory: Optional[str] = None) -> None:
        self.root = os.path.abspath(root)
        self.dir = os.path.abspath(directory or os.path.join(self.root, ".ai"))

    # -- paths -------------------------------------------------------------

    @property
    def plan_path(self) -> str:
        return os.path.join(self.dir, "plan.md")

    @property
    def execution_dir(self) -> str:
        return os.path.join(self.dir, "execution")

    @property
    def reviews_dir(self) -> str:
        return os.path.join(self.dir, "reviews")

    @property
    def snapshot_path(self) -> str:
        return os.path.join(self.reviews_dir, "review-target.diff")

    @property
    def snapshot_meta_path(self) -> str:
        return os.path.join(self.reviews_dir, "review-target.json")

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

    def relative(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.root).replace(os.sep, "/")
        except ValueError:  # pragma: no cover - different drives on Windows
            return path

    # -- lifecycle ---------------------------------------------------------

    def ensure(self) -> "Workspace":
        for directory in (self.dir, self.execution_dir, self.reviews_dir):
            os.makedirs(directory, exist_ok=True)
        gitignore = os.path.join(self.dir, ".gitignore")
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

    def record_event(
        self, stage: str, status: str, detail: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Append one stage outcome to the run log (models included, secrets not)."""
        state = self.read_state()
        event = {"stage": stage, "status": status, "at": utcnow()}
        if detail:
            event.update(detail)
        state.setdefault("events", []).append(event)
        state["updated_at"] = utcnow()
        self.write_state(state)
        return event


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
        except ValueError:
            # Writes are atomic, so this is rare -- but silently returning the
            # default turns "unreadable" into "absent", which is how a live job
            # once looked like a missing one. Retrying costs milliseconds.
            if attempt == _READ_ATTEMPTS - 1:
                return default
            time.sleep(0.02 * (attempt + 1))
        except OSError:
            return default
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
    """

    def __init__(self, path: str, timeout: float = 5.0, stale_after: float = 30.0) -> None:
        self.path = os.path.abspath(path) + ".lock"
        self.timeout = timeout
        self.stale_after = stale_after
        self.acquired = False

    def __enter__(self) -> "file_lock":
        deadline = time.monotonic() + self.timeout
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        while True:
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(handle, str(os.getpid()).encode("ascii"))
                os.close(handle)
                self.acquired = True
                return self
            except FileExistsError:
                if self._break_if_stale():
                    continue
                if time.monotonic() >= deadline:
                    return self
                time.sleep(0.05)
            except OSError:
                return self

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
