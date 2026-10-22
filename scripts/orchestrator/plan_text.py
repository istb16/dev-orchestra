"""A plan's Markdown read around its code fences: which lines are fenced, and its headings.

Shared by the review modules and the units modules, so neither reads a plan
through the other.
"""

from __future__ import annotations

import re
from typing import Iterable, Iterator, NamedTuple

FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

#: Where a line stands against the code fences, as ``fenced_lines`` says.
OUTSIDE = ""
OPEN = "open"
INSIDE = "inside"
CLOSE = "close"

# No lazy group before an optional tail: ``(.*?)\s*#*\s*$`` backtracks
# polynomially on a ``#`` line padded with spaces. The closing hashes are
# stripped in ``heading_title`` instead.
HEADING_TEXT_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")


class FencedLine(NamedTuple):
    #: The line's index in the lines given, from 0.
    number: int
    text: str
    #: ``OUTSIDE``, or the fence's ``OPEN`` line, a line ``INSIDE`` it, or its ``CLOSE`` line.
    part: str
    #: The info string of the fence the line belongs to, ``yaml`` for ```` ```yaml ````; "" outside.
    info: str


def fenced_lines(lines: Iterable[str]) -> Iterator[FencedLine]:
    """Each line with where it stands against the code fences.

    A fence opens on three or more backticks or tildes and closes on a run
    of the same character at least as long; one left open runs to the end.
    """
    fence = ""
    info = ""
    for index, line in enumerate(lines):
        marker = FENCE_RE.match(line)
        if fence:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = ""
                yield FencedLine(index, line, CLOSE, info)
            else:
                yield FencedLine(index, line, INSIDE, info)
        elif marker:
            fence = marker.group(1)
            info = line[marker.end() :].strip()
            yield FencedLine(index, line, OPEN, info)
        else:
            yield FencedLine(index, line, OUTSIDE, "")


def heading_title(text: str) -> str:
    """A heading's title from ``HEADING_TEXT_RE``'s second group, closing hashes dropped."""
    title = text.strip()
    head = title.rstrip("#")
    if head != title and (not head or head[-1].isspace()):
        title = head.rstrip()
    return title
