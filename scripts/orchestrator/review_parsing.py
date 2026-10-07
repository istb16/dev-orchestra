"""Reading a reviewer's report into findings."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from .review_common import SEVERITIES

# --------------------------------------------------------------------------- parsing

_FIELD_RE = re.compile(
    r"^\s*(?:[-*+]\s*)?\*{0,2}(severity|file|line|lines|category|problem|impact|evidence|"
    r"recommended fix|recommendation|fix)\*{0,2}\s*:\s*(.*)$",
    re.IGNORECASE,
)
#: A finding header. Models reach for every emphasis style there is, so accept
#: ``## Finding``, ``**Finding 2**``, ``Finding 3:`` and bare ``Finding``. Being
#: strict here used to mean a report full of findings parsed as zero, which is
#: indistinguishable from a clean review -- the worst way for this to fail.
_HEADER_RE = re.compile(r"^\s{0,3}(?:#{1,6}\s*)?\*{0,2}finding\b[^:]{0,40}?\*{0,2}\s*:?\s*$", re.IGNORECASE)
_SEVERITY_LINE_RE = re.compile(r"^\s*(?:[-*+]\s*)?\*{0,2}severity\*{0,2}\s*:", re.IGNORECASE)
_NO_FINDINGS_RE = re.compile(r"^\s*\*{0,2}NO_FINDINGS\*{0,2}\s*$", re.MULTILINE)
_FIELD_ALIASES = {
    "lines": "line",
    "recommendation": "recommended_fix",
    "fix": "recommended_fix",
    "recommended fix": "recommended_fix",
}


def parse_findings(text: str, reviewer_id: str) -> List[Dict[str, Any]]:
    """Parse a reviewer report into structured findings. Tolerant by design."""
    if not text:
        return []
    body = _strip_report_header(text)
    if not _HEADER_RE.search(body) and _NO_FINDINGS_RE.search(body):
        return []

    blocks = _split_blocks(body)
    findings: List[Dict[str, Any]] = []
    for block in blocks:
        finding = _parse_block(block)
        if not finding:
            continue
        finding["reviewer"] = reviewer_id
        findings.append(finding)
    return findings


def _split_blocks(body: str) -> List[List[str]]:
    """Split a report body into per-finding line blocks.

    Headers are preferred, but a report that lost its headers entirely is still
    recoverable: each ``Severity:`` line starts a finding, since the required
    schema puts exactly one at the top of every block.
    """
    blocks: List[List[str]] = []
    current: Optional[List[str]] = None
    for line in body.splitlines():
        if _HEADER_RE.match(line):
            current = []
            blocks.append(current)
            continue
        if current is not None:
            current.append(line)
    if blocks:
        return blocks

    for line in body.splitlines():
        if _SEVERITY_LINE_RE.match(line):
            current = []
            blocks.append(current)
        if current is not None:
            current.append(line)
    return blocks


def unparsed_report_warning(text: str, parsed: Sequence[Dict[str, Any]]) -> str:
    """Describe a report that produced nothing but does not claim to be clean.

    A reviewer must either report findings in the required shape or say
    ``NO_FINDINGS``. Anything else means the report could not be read, and that
    must never be reported to the orchestrator as "no problems found".
    """
    if parsed:
        return ""
    body = _strip_report_header(text or "")
    if _NO_FINDINGS_RE.search(body):
        return ""
    if not body.strip():
        return "report was empty (no findings and no NO_FINDINGS)"
    return (
        "report could not be parsed: no findings in the required format and no "
        "NO_FINDINGS -- treat this reviewer as failed, not clean"
    )


#: Separates the header this skill writes from the reviewer's own body.
_HEADER_MARKER = "\n---\n"


def _strip_report_header(text: str) -> str:
    head, sep, tail = text.partition(_HEADER_MARKER)
    if sep and head.lstrip().startswith("# Review"):
        return tail
    return text


def _parse_block(lines: List[str]) -> Optional[Dict[str, Any]]:
    fields: Dict[str, List[str]] = {}
    key: Optional[str] = None
    # The fence being read through, the index of the line that closes it, and
    # how far its lines are indented. Inside a fence nothing is a label and
    # nothing is a heading: ``# comment`` and ``fix: ...`` are code there.
    fence = ""
    closer = -1
    indent = 0
    for index, raw_line in enumerate(lines):
        if key and fence:
            if index == closer:
                fields[key].append(raw_line.strip())
                fence = ""
            else:
                fields[key].append(_dedent(raw_line.rstrip(), indent))
            continue
        # Models bold the labels in several ways (``**Severity:** high`` and
        # ``**Severity**: high``); dropping the emphasis normalises all of them.
        line = raw_line.replace("**", "")
        match = _FIELD_RE.match(line)
        if match:
            raw_key = match.group(1).lower()
            key = _FIELD_ALIASES.get(raw_key, raw_key)
            text = match.group(2).strip()
            fields.setdefault(key, [])
        elif key and line.strip() and not line.strip().startswith("#"):
            text = line.strip()
        else:
            continue
        if text:
            fields[key].append(text)
        opener = _fence_opener(text)
        if opener:
            # A fence nobody closes is read as ordinary lines: swallowing the
            # rest of the finding would lose more than the fence protects.
            closer = _closing_line(lines, index + 1, opener)
            if closer >= 0:
                fence, indent = opener, _indent_of(lines[closer])
    if not fields:
        return None
    finding: Dict[str, Any] = {
        "severity": _normalise_severity(_join(fields.get("severity"))),
        "file": _normalise_path(_join(fields.get("file"))),
        "line": _join(fields.get("line")) or "n/a",
        "category": (_join(fields.get("category")) or "general").lower(),
        "problem": _join(fields.get("problem")),
        "impact": _join(fields.get("impact")),
        "evidence": _join_lines(fields.get("evidence")),
        "recommended_fix": _join_lines(fields.get("recommended_fix")),
    }
    if not finding["problem"] and not finding["evidence"]:
        return None
    return finding


#: The opening line of a fenced code block: three or more backticks or tildes,
#: then an optional info string such as ``python``.
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def _fence_opener(text: str) -> str:
    """The fence ``text`` opens, or "" when it opens none."""
    match = _FENCE_RE.match(text)
    if not match:
        return ""
    fence, info = match.groups()
    # A backtick fence's info string cannot hold a backtick, so a line that
    # opens and closes inline code with three backticks opens no fence.
    if fence[0] == "`" and "`" in info:
        return ""
    return fence


def _closing_line(lines: List[str], start: int, fence: str) -> int:
    """The index of the line that closes ``fence``, or -1.

    Only the same character closes a fence, at least as many of it, and with
    nothing else on the line, so a ``~~~`` inside a backtick fence is code.
    """
    for index in range(start, len(lines)):
        stripped = lines[index].strip()
        if len(stripped) >= len(fence) and stripped == fence[0] * len(stripped):
            return index
    return -1


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _dedent(line: str, indent: int) -> str:
    """``line`` without the indentation the whole fence shares."""
    return line[min(indent, _indent_of(line)) :]


def _join(values: Optional[List[str]]) -> str:
    if not values:
        return ""
    return " ".join(part for part in (value.strip() for value in values) if part).strip()


def _join_lines(values: Optional[List[str]]) -> str:
    """A field that may hold code, with its line breaks kept.

    Evidence and a fix quote code, and the fix brief hands that code to the
    fixer: joined into one line, its shape -- and with it its meaning -- is gone.
    """
    if not values:
        return ""
    return "\n".join(value.rstrip() for value in values).strip("\n")


def _normalise_severity(value: str) -> str:
    lowered = (value or "").strip().lower()
    for severity in SEVERITIES:
        if lowered.startswith(severity):
            return severity
    if lowered in ("blocker", "critical/high"):
        return "critical"
    if lowered in ("major",):
        return "high"
    if lowered in ("minor", "nit", "nitpick", "info", "style"):
        return "low"
    return "medium"


def _normalise_path(value: str) -> str:
    path = (value or "").strip().strip("`").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path
