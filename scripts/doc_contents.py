#!/usr/bin/env python3
"""Write the table of contents at the top of a reference document.

    python scripts/doc_contents.py references/reviews.md docs/ja/references/reviews.md
    python scripts/doc_contents.py --check references/reviews.md docs/ja/references/reviews.md

The documents that have contents are the long references -- reviews, cli,
configuration, limits, workflow and providers -- and their translations;
``tests/test_docs.py`` names them.

The contents list the document's ``##`` and ``###`` headings, each linked to
its section, between two marker comments right under the ``#`` title. Run it
after adding, renaming or removing a heading; ``--check`` (and
``tests/test_docs.py``) fails while a document's contents are out of date.

A translation links to the English anchors it already carries: every heading
there is preceded by ``<a id="...">`` naming the English section, so a link
into the translation reads the same as one into the English.
"""

from __future__ import annotations

import glob
import pathlib
import re
import sys
from typing import List, Optional, Tuple

START = "<!-- contents: start -->"
END = "<!-- contents: end -->"
TITLES = {"en": "**Contents**", "ja": "**目次**"}

_HEADING = re.compile(r"^(#{1,3}) (.+?)\s*$")
_ANCHOR = re.compile(r'^<a id="([^"]+)"></a>\s*$')
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def slug(text: str) -> str:
    """The anchor GitHub gives a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = text.strip().lower().replace("`", "")
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def headings(text: str) -> List[Tuple[int, str, str]]:
    """``(level, title, anchor)`` for each ``##`` and ``###`` outside code fences."""
    found: List[Tuple[int, str, str]] = []
    seen: dict = {}
    fence = ""
    previous_anchor: Optional[str] = None
    for line in text.split("\n"):
        marker = _FENCE.match(line)
        if fence:
            # Closed only by the character that opened it, at least as many times.
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = ""
            continue
        if marker:
            fence = marker.group(1)
            continue
        anchor = _ANCHOR.match(line)
        if anchor:
            previous_anchor = anchor.group(1)
            continue
        match = _HEADING.match(line)
        if match:
            level, title = len(match.group(1)), match.group(2)
            # GitHub numbers a repeated name over every heading, the title included.
            base = slug(title)
            count = seen.get(base, 0)
            seen[base] = count + 1
            if level > 1:
                name = previous_anchor or (base if not count else "%s-%d" % (base, count))
                found.append((level, title, name))
            previous_anchor = None
        elif line.strip():
            previous_anchor = None
    return found


def contents(text: str, language: str) -> str:
    lines = [START, "", TITLES[language], ""]
    for level, title, anchor in headings(text):
        lines.append("%s- [%s](#%s)" % ("  " * (level - 2), title, anchor))
    lines += ["", END]
    return "\n".join(lines)


def with_contents(text: str, language: str) -> str:
    """``text`` with its contents block written, or rewritten, under the title."""
    if START in text:
        before, rest = text.split(START, 1)
        if END not in rest:
            raise ValueError("%s without %s" % (START, END))
        after = rest.split(END, 1)[1].lstrip("\n")
        return before + contents(before + after, language) + "\n\n" + after
    lines = text.split("\n")
    title = next(i for i, line in enumerate(lines) if line.startswith("# "))
    head = "\n".join(lines[: title + 1])
    body = "\n".join(lines[title + 1 :]).lstrip("\n")
    return head + "\n\n" + contents(text, language) + "\n\n" + body


def language_of(path: pathlib.Path) -> str:
    return "ja" if "ja" in path.parts else "en"


def main(argv: List[str]) -> int:
    check = "--check" in argv
    # Expanded here as well: PowerShell and cmd pass a `*.md` through as written.
    paths = [
        pathlib.Path(name)
        for arg in argv
        if arg != "--check"
        for name in (sorted(glob.glob(arg)) if glob.has_magic(arg) else [arg])
    ]
    if not paths:
        print(str(__doc__).strip(), file=sys.stderr)
        return 2
    stale = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        updated = with_contents(text, language_of(path))
        if updated == text:
            continue
        if check:
            stale.append(str(path))
        else:
            path.write_text(updated, encoding="utf-8", newline="\n")
            print("wrote contents: %s" % path)
    for path in stale:
        print(
            "contents out of date: %s (run python scripts/doc_contents.py %s)" % (path, path), file=sys.stderr
        )
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
