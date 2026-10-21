"""Reading a child CLI's output: what arrived, when it last arrived, and each
line handed to a callback that can never hold the run back.
"""

from __future__ import annotations

import codecs
import io
import queue
import threading
import time
from typing import Callable, List, Optional, Tuple, cast

from .execution_common import _POLL_SECONDS

#: Stdout lines waiting for ``on_line`` before further ones are dropped.
_LINE_QUEUE_SIZE = 1000
#: The most one read of a pipe takes; a read returns whatever has arrived.
_READ_CHUNK = 65536


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
