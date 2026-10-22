"""Reading a plan's text: its ``Added in this revision`` section and the tokens it names."""

from __future__ import annotations

import posixpath
import re
from typing import Iterable, Iterator, List, NamedTuple, Optional, Tuple

ADDED_HEADING_RE = re.compile(r"^\s{0,3}(#{2,3})\s+added in this revision\b", re.IGNORECASE)
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s")
NONE_ADDED_RE = re.compile(r"^[\s\-*_]*none(?![a-z0-9])", re.IGNORECASE)
FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

#: Where a line stands against the code fences, as ``fenced_lines`` says.
OUTSIDE = ""
OPEN = "open"
INSIDE = "inside"
CLOSE = "close"


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


def added_in_revision(plan_text: str) -> Optional[str]:
    """The body of the plan's ``## Added in this revision`` section.

    None when the plan has no such heading outside a fenced block; the body
    runs to the next heading of the same level or higher, stripped, so
    ``###`` items under a ``##`` heading stay inside it.
    """
    body: Optional[List[str]] = None
    level = 0
    for fenced in fenced_lines(plan_text.splitlines()):
        line = fenced.text
        if fenced.part:
            if body is not None:
                body.append(line)
            continue
        if body is not None:
            heading = HEADING_RE.match(line)
            if heading and len(heading.group(1)) <= level:
                break
        else:
            added = ADDED_HEADING_RE.match(line)
            if added:
                body = []
                level = len(added.group(1))
                continue
        if body is not None:
            body.append(line)
    return None if body is None else "\n".join(body).strip()


# No lazy group before an optional tail: ``(.*?)\s*#*\s*$`` backtracks
# polynomially on a ``#`` line padded with spaces. The closing hashes are
# stripped in ``heading_title`` instead.
HEADING_TEXT_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
FILES_HEADING_RE = re.compile(r"^(?:\d+[.)]\s*)?files to modify\b", re.IGNORECASE)
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_BRACE_RE = re.compile(r"^(.*?)\{([^{}]*)\}(.*)$")
_LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")
_SPAN_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9_]+$")
#: A plain word needs a letter after its dot, so ``0.14.0`` is not a file.
_PLAIN_EXTENSION_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9_]*$")
_ABBREVIATION_RE = re.compile(r"^(?:[A-Za-z]\.)+[A-Za-z]?$")
# Neither group can run past the next bracket, so a line of them stays linear.
_LINK_RE = re.compile(r"\[([^\[\]\n]*)\]\(([^()\[\]\s]*)\)")
_WORD_SPLIT_RE = re.compile(r"[\s|]+")
_EDGE_PUNCTUATION = "`\"'()<>,;:!?"


class PlanToken(NamedTuple):
    token: str
    section: str
    in_files: bool
    #: A plain word on a line whose entry is backticked -- the description
    #: beside it, so ``read/write`` there is not read as a directory.
    prose: bool = False


class PlanScan(NamedTuple):
    tokens: List[PlanToken]
    has_files_section: bool


def heading_title(text: str) -> str:
    """A heading's title from ``HEADING_TEXT_RE``'s second group, closing hashes dropped."""
    title = text.strip()
    head = title.rstrip("#")
    if head != title and (not head or head[-1].isspace()):
        title = head.rstrip()
    return title


def _expand_word(word: str) -> List[str]:
    brace = _BRACE_RE.match(word)
    if brace and "," in brace.group(2) and "{" not in brace.group(3) and "}" not in brace.group(3):
        members = [brace.group(1) + member + brace.group(3) for member in brace.group(2).split(",")]
    else:
        members = [word]
    return [_LINE_SUFFIX_RE.sub("", member) or member for member in members]


def _expand_token(span: str) -> List[str]:
    """The tokens one backticked span stands for.

    Its first word, and every later word shaped like a path (a ``/`` or a
    file extension), so an annotated ``scripts/auth.py (new)`` is the path
    and ``src/main.py, src/auth.py`` is both; a trailing ``,`` or ``;`` is
    dropped from each. One level of ``{a,b}`` is split into one token per
    member; a trailing ``:N`` or ``:N-M`` line reference is dropped, unless
    it is all there is. Nothing else is touched.
    """
    words = [word.rstrip(",;") or word for word in span.split()]
    if not words:
        return []
    chosen = words[:1] + [
        word for word in words[1:] if "/" in word or _SPAN_EXTENSION_RE.search(posixpath.basename(word))
    ]
    return [token for word in chosen for token in _expand_word(word)]


def _plain_words(text: str) -> List[str]:
    """The path-shaped words of text written without backticks.

    Split on whitespace and table pipes; a Markdown link counts by its text
    and its target; quotes, brackets, ``**`` and trailing sentence
    punctuation are dropped. A word is kept when it has a ``/`` or an
    extension starting with a letter, and is not an abbreviation such as
    ``e.g.``.
    """
    words: List[str] = []
    for raw in _WORD_SPLIT_RE.split(_LINK_RE.sub(r" \1 \2 ", text)):
        word = raw.strip(_EDGE_PUNCTUATION)
        if word.startswith("**"):
            word = word[2:]
        if word.endswith("**"):
            word = word[:-2]
        word = word.rstrip(".").strip(_EDGE_PUNCTUATION)
        # A bare "/" is the separator in "`a` / `b`", not a path.
        if not word or _ABBREVIATION_RE.match(word) or not word.strip("/"):
            continue
        if "/" in word or _PLAIN_EXTENSION_RE.search(posixpath.basename(word)):
            words.extend(_expand_word(word))
    return words


def plan_tokens(plan_text: str) -> PlanScan:
    """Every backticked token in the plan, with the heading it sits under.

    ``in_files`` is true under a ``Files to Modify`` heading and every
    subsection of it, which ends at the next heading of the same level or
    higher -- the boundary ``added_in_revision`` uses. That section is also
    read without backticks, because an entry written as plain text names a
    file all the same: every path-shaped word there (``_plain_words``) is a
    token too, marked ``prose`` when its line names something in backticks.

    Fenced lines are not read for the size, so a drafted CHANGELOG entry or
    doc row does not count as a file. Inside the section their path-shaped
    words still come back, with ``in_files`` false, so a file tree there is
    checked against the high-risk patterns; elsewhere they are skipped whole.
    """
    stack: List[Tuple[int, str]] = []
    tokens: List[PlanToken] = []
    has_files = False
    section = ""
    in_files = False
    for fenced in fenced_lines(plan_text.splitlines()):
        line = fenced.text
        if fenced.part == INSIDE and in_files:
            for word in _plain_words(line):
                tokens.append(PlanToken(word, section, False))
        if fenced.part:
            continue
        heading = HEADING_TEXT_RE.match(line)
        if heading:
            level = len(heading.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            title = heading_title(heading.group(2))
            stack.append((level, title))
            if FILES_HEADING_RE.match(title):
                has_files = True
        section = stack[-1][1] if stack else ""
        in_files = any(FILES_HEADING_RE.match(name) for _, name in stack)
        spans = BACKTICK_RE.findall(line)
        for span in spans:
            for token in _expand_token(span):
                tokens.append(PlanToken(token, section, in_files))
        if in_files:
            for word in _plain_words(BACKTICK_RE.sub(" ", line)):
                tokens.append(PlanToken(word, section, True, bool(spans)))
    return PlanScan(tokens, has_files)
