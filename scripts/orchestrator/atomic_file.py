"""Replacing a file through a sibling temporary file and one rename.

Shared by the ``.ai/`` files (``workspace``) and the user's Claude Code
settings (``claude_hooks``); each caller picks the options it needs, and the
temporary file is removed whatever goes wrong. Standard library only.
"""

from __future__ import annotations

import functools
import os
import stat
import sys
import tempfile
import time
from typing import Any, Callable, Optional, Set, Union

_IS_WINDOWS = sys.platform.startswith("win")

#: Volumes (``st_dev`` of the target's directory) that refused a
#: POSIX-semantics rename as unsupported; ``rename_over`` goes straight to
#: ``os.replace`` there.
_NO_POSIX_RENAME: Set[int] = set()


class Raced(Exception):
    """The file changed between being read and being replaced."""


#: ``write``'s ``expected`` when the file is not compared before the move.
UNCHECKED: Any = object()


def current_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def write_target(path: str) -> str:
    """Where writing ``path`` lands: a linked file's target, so the link stays a link."""
    return os.path.realpath(path) if os.path.islink(path) else path


def write(
    path: str,
    content: Union[bytes, str],
    *,
    prefix: Optional[str] = None,
    suffix: str = ".tmp",
    durable: bool = False,
    keep_mode: bool = False,
    follow_link: bool = False,
    expected: object = UNCHECKED,
    replace: Optional[Callable[[str, str], None]] = None,
) -> None:
    """Write ``content`` beside ``path`` and move it into place.

    The move is one rename (``rename_over``), so a reader sees either the old
    content or the new one, never a partial write. Detached workers and
    their parent share these files, and a reader landing in a truncate window
    used to get a parse error indistinguishable from "the file is not there".

    A ``str`` is written as UTF-8, its newlines as they are. The temporary file
    is ``prefix`` + random + ``suffix`` in ``path``'s directory; without a
    ``prefix`` it is named after the file, ``.settings.json.<random>.tmp``.
    ``durable`` flushes it to disk before the move, and ``keep_mode`` gives it
    the mode of the file it replaces. With ``follow_link`` a ``path`` that is a
    link -- a dotfile manager's, say -- has its target replaced, and the
    temporary file is made beside that. With ``expected`` (the bytes read
    earlier, or None for no file) the file is read once more just before the
    move, and ``Raced`` raised if it changed in between. ``replace`` moves the
    temporary file over the target; by default one ``rename_over``.
    """
    if follow_link:
        path = write_target(path)
    directory = os.path.dirname(path) or "."  # a bare name is in the current directory
    os.makedirs(directory, exist_ok=True)
    if prefix is None:
        prefix = "." + os.path.basename(path) + "."
    handle, temporary = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
            if durable:
                stream.flush()
                os.fsync(stream.fileno())
        if keep_mode:
            try:
                os.chmod(temporary, stat.S_IMODE(os.stat(path).st_mode))
            except OSError:
                pass  # a new file keeps what mkstemp gave it
        if expected is not UNCHECKED and current_bytes(path) != expected:
            raise Raced(path)
        # Looked up here, not bound as a default, so a patched rename_over is the one called.
        if replace is None:
            rename_over(temporary, path)
        else:
            replace(temporary, path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def replace_with_retry(temporary: str, target: str, attempts: int = 25, fallback: bool = True) -> None:
    """Rename over ``target``, working around Windows file sharing.

    On Windows a rename is denied while another handle has the target open
    without delete sharing, and Python's ``open()`` does not grant it -- so a
    concurrent *reader* can make a *write* fail, which was worse than the torn
    read this was meant to prevent. Readers that grant delete sharing no longer
    block ``rename_over``; plain ``open()`` readers, and antivirus, still do.
    They hold the file only momentarily, so a short backoff wins in practice.
    With ``fallback`` false the last refusal is raised instead of writing in
    place, so the target is never left half-written.
    """
    if not fallback and attempts < 1:
        raise ValueError("replace_with_retry without fallback needs at least one attempt")
    last: Optional[OSError] = None
    for attempt in range(attempts):
        try:
            rename_over(temporary, target)
            return
        except PermissionError as exc:  # Windows only
            last = exc
            time.sleep(min(0.01 * (attempt + 1), 0.1))

    if not fallback:
        # For a file the user owns: a half-written file is worse than a failed write.
        assert last is not None  # at least one attempt, checked above
        raise last
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


def rename_over(temporary: str, target: str) -> None:
    """Move ``temporary`` over ``target`` in one step, as every opener sees it.

    On Windows ``os.replace`` is ``MoveFileExW``, and while it runs the target's
    name is briefly missing: a concurrent reader finds no file, which is how a
    live state file read as absent. A rename with POSIX semantics swaps the
    directory entry at once. Where the volume does not support one (Windows
    before 1709, FAT, some shares) it falls back to ``os.replace`` and
    remembers that for the volume. Elsewhere ``os.replace`` already does this.
    """
    if not _IS_WINDOWS:
        os.replace(temporary, target)
        return
    volume = _volume_of(target)
    if volume not in _NO_POSIX_RENAME:
        if _rename_by_handle(temporary, target):
            return
        if volume is not None:
            _NO_POSIX_RENAME.add(volume)
    os.replace(temporary, target)


def _volume_of(target: str) -> Optional[int]:
    """The volume holding ``target``'s directory, or None if it cannot be told.

    The volume itself, not the drive letter: a FAT stick or a share mounted
    on a folder under ``C:`` must not mark all of ``C:`` as unsupported.
    """
    try:
        return os.stat(os.path.dirname(os.path.abspath(target))).st_dev
    except OSError:
        return None


@functools.lru_cache(maxsize=1)
def _kernel32() -> Any:
    """A private kernel32 for ``_rename_by_handle`` and workspace's shared reads, built once.

    A private kernel32 with use_last_error keeps the error code safe from
    later calls, and leaves the shared windll object unchanged.
    """
    # Windows only: WinDLL exists nowhere else.
    import ctypes
    import ctypes.wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.LPVOID,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
    kernel32.SetFileInformationByHandle.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.c_int,
        ctypes.wintypes.LPVOID,
        ctypes.wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = ctypes.wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    kernel32.GetFileType.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.GetFileType.restype = ctypes.wintypes.DWORD
    return kernel32


def _rename_by_handle(temporary: str, target: str) -> bool:
    """Rename ``temporary`` over ``target`` with ``FileRenameInfoEx``; False if unsupported.

    REPLACE_IF_EXISTS | POSIX_SEMANTICS, as Rust's ``fs::rename`` does. Other
    failures raise an ``OSError`` carrying the Windows error, so Python picks
    the subclass: PermissionError for 5 and 32, which ``replace_with_retry``
    waits out.
    """
    # Windows only: the call and its error codes exist nowhere else.
    import ctypes

    DELETE = 0x00010000
    FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004  # read | write | delete
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE = ctypes.c_void_p(-1).value
    FILE_RENAME_INFO_EX = 22
    REPLACE_IF_EXISTS_AND_POSIX = 0x1 | 0x2
    UNSUPPORTED = (1, 50, 87)  # invalid function, not supported, invalid parameter

    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(
        temporary, DELETE, FILE_SHARE_ALL, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
    )
    if not handle or handle == INVALID_HANDLE:
        raise OSError(None, "could not open %s to rename it" % temporary, temporary, ctypes.get_last_error())
    try:
        name = os.path.abspath(target)
        units = len(name.encode("utf-16-le")) // 2  # a character past U+FFFF takes two

        # FILE_RENAME_INFO, sized for this name: a union of ReplaceIfExists and
        # Flags, the root, the name's length in bytes, then the name itself.
        class FileRenameInfo(ctypes.Structure):
            _fields_ = [
                ("Flags", ctypes.c_uint32),
                ("RootDirectory", ctypes.c_void_p),
                ("FileNameLength", ctypes.c_uint32),
                ("FileName", ctypes.c_wchar * (units + 1)),
            ]

        info = FileRenameInfo(REPLACE_IF_EXISTS_AND_POSIX, None, units * 2, name)
        if kernel32.SetFileInformationByHandle(
            handle, FILE_RENAME_INFO_EX, ctypes.byref(info), ctypes.sizeof(info)
        ):
            return True
        code = ctypes.get_last_error()
        if code in UNSUPPORTED:
            return False
        raise OSError(None, "could not rename %s over %s" % (temporary, target), target, code)
    finally:
        kernel32.CloseHandle(handle)
