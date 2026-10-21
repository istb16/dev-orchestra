"""Replacing a file through a sibling temporary file and one rename.

Shared by the ``.ai/`` files (``workspace``) and the user's Claude Code
settings (``claude_hooks``); each caller picks the options it needs, and the
temporary file is removed whatever goes wrong. Standard library only.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
from typing import Any, Callable, Optional, Union


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

    ``os.replace`` is atomic on POSIX and Windows alike, so a reader sees either
    the old content or the new one, never a partial write. Detached workers and
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
    temporary file over the target; by default one ``os.replace``.
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
        # Looked up here, not bound as a default, so a patched os.replace is the one called.
        if replace is None:
            os.replace(temporary, path)
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

    On Windows ``os.replace`` is denied while another handle has the target
    open, and Python's ``open()`` does not grant delete sharing -- so a
    concurrent *reader* can make a *write* fail, which was worse than the torn
    read this was meant to prevent. Readers hold the file only momentarily, so
    a short backoff wins in practice. With ``fallback`` false the last refusal is
    raised instead of writing in place, so the target is never left half-written.
    """
    last: Optional[OSError] = None
    for attempt in range(attempts):
        try:
            os.replace(temporary, target)
            return
        except PermissionError as exc:  # Windows only
            last = exc
            time.sleep(min(0.01 * (attempt + 1), 0.1))

    if not fallback:
        # For a file the user owns: a half-written file is worse than a failed write.
        if last is not None:
            raise last
        return
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
