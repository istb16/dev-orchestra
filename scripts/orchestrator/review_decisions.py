"""What a code reviewer is told was already decided: the "Decisions already made" section."""

from __future__ import annotations

import re
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from . import context as context_mod
from . import questions as questions_mod
from . import workspace as ws
from .review_common import (
    CODE_DECISIONS_PLAN_LABEL,
    CODE_DECISIONS_REJECTED_LABEL,
    CODE_DECISIONS_SECTION,
    CODE_DECISIONS_USER_LABEL,
)
from .review_consolidation import finding_key, recorded_rounds
from .review_fanout import fenced
from .review_plan import plan_section
from .review_round_context import one_line

#: The most one reviewer prompt carries of this section, the note on a cut aside.
DECISIONS_CAP_CHARS = 6000

#: The plan sections handed over, in this order. Test Strategy and the rest
#: are the plan's own business, not a premise for the code.
PLAN_SECTIONS = ("Risks", "Decisions")

#: The longest problem text one rejected finding's line carries.
PROBLEM_CHARS = 160

_NONE_RE = re.compile(r"^[\s\-*_]*none[\s.*_]*$", re.IGNORECASE)


class _Part(NamedTuple):
    label: str
    lines: List[str]
    #: The file the full text is in, for the cut note; "" for the rejections.
    source: str
    info: str


def render_code_decisions(
    workspace: ws.Workspace,
    lineage: str,
    meta: Dict[str, Any],
    cap: int = DECISIONS_CAP_CHARS,
) -> str:
    """The section a code review prompt ends with; "" when there is nothing to put in it.

    Its parts, in this order, which is also the order a cut drops them from
    the end: the user's answers to the design questions, the plan's Risks
    and Decisions sections, and the findings rejected in earlier rounds of
    this review (``_rejected_lines``). Each is fenced under its own label.
    The section is cut at a line boundary at ``cap`` chars, and a note after
    it names what was cut.
    """
    parts: List[_Part] = []
    user = questions_mod.decisions_text(workspace.read_state())
    if user.strip():
        source = workspace.relative(workspace.decisions_path)
        parts.append(_Part(CODE_DECISIONS_USER_LABEL, user.strip().splitlines(), source, "markdown"))
    plan_text = ws.read_text(workspace.plan_path, "")
    found: List[Tuple[str, str]] = []
    for name in PLAN_SECTIONS:
        body = plan_section(plan_text, name)
        if body and not _NONE_RE.match(body):
            found.append((name, body))
    if found:
        plan_relative = workspace.relative(workspace.plan_path)
        lines: List[str] = []
        for name, body in found:
            if lines:
                lines.append("")
            lines += ["## %s" % name, "", *body.splitlines()]
        sections = ", ".join("plan.md#%s" % name for name, _ in found)
        label = CODE_DECISIONS_PLAN_LABEL.format(sections=sections)
        parts.append(_Part(label, lines, plan_relative, "markdown"))
    rejected = _rejected_lines(workspace, lineage, meta)
    if rejected:
        parts.append(_Part(CODE_DECISIONS_REJECTED_LABEL, rejected, "", "text"))
    if not parts:
        return ""
    return _cut(parts, cap)


def _rejected_lines(workspace: ws.Workspace, lineage: str, meta: Dict[str, Any]) -> List[str]:
    """One line per finding rejected in an earlier round of this review.

    Each finding is taken as the latest round of this ``lineage`` that holds
    it left it -- this snapshot's rounds included, so one rejected and later
    accepted, here or on an earlier snapshot, is not listed. A finding
    rejected and then not reported again keeps its rejection. Only one whose
    latest occurrence is on another snapshot is listed, so a rerun of this
    snapshot is not told its own rejections.

    Rounds are ordered by the moment ``generated_at`` names, then by snapshot
    and round id, so rounds with the same or no timestamp give the same answer
    every time. A round whose ``findings`` is not a list holds none.

    Who reported it is never said: a rejection is a decision, not one
    reviewer's opinion against another's.
    """
    if not lineage:
        return []
    sha = str(meta.get("sha256") or "")
    rounds = [
        (_round_sha(data), data)
        for data in recorded_rounds(workspace)
        if str(data.get("lineage") or "") == lineage
    ]
    rounds.sort(key=lambda pair: (_round_time(pair[1]), pair[0], _round_id(pair[1])))
    latest: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for round_sha, data in rounds:
        findings = data.get("findings")
        for finding in findings if isinstance(findings, list) else []:
            if isinstance(finding, dict):
                key = finding_key(finding)
                # Re-inserted so the order is the order of the latest decisions.
                latest.pop(key, None)
                latest[key] = (round_sha, finding)
    lines: List[str] = []
    for round_sha, finding in latest.values():
        if finding.get("triage") != "rejected" or round_sha == sha:
            continue
        problem = one_line(str(finding.get("problem") or ""))
        if len(problem) > PROBLEM_CHARS:
            problem = problem[: PROBLEM_CHARS - 3] + "..."
        note = one_line(str(finding.get("triage_note") or "").strip()) or "no reason recorded"
        where = "%s:%s" % (str(finding.get("file") or "?"), str(finding.get("line") or "n/a"))
        lines.append("- %s -- %s -- %s" % (one_line(where), problem, note))
    return lines


def _snapshot_of(data: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = data.get("snapshot")
    return snapshot if isinstance(snapshot, dict) else {}


def _round_sha(data: Dict[str, Any]) -> str:
    return str(_snapshot_of(data).get("sha256") or "")


def _round_id(data: Dict[str, Any]) -> str:
    return str(_snapshot_of(data).get("round_id") or "")


def _round_time(data: Dict[str, Any]) -> float:
    """``generated_at`` as seconds since the epoch; a missing or unreadable one sorts first.

    Compared as a moment, not as text: an offset or fractional seconds would
    otherwise order a round by how its time is written.
    """
    stamp = ws.parse_stamp(data.get("generated_at"))
    return float("-inf") if stamp is None else stamp.timestamp()


def _cut(parts: List[_Part], cap: int) -> str:
    """The section with ``parts`` in it, cut at a line boundary at ``cap`` chars.

    Parts are taken whole while they fit, the first that does not is taken
    up to its last line that fits, and the rest are left out. A cut is
    followed by a note naming the files the full text is in and how many
    rejected findings were left out -- only those that were actually cut.
    """
    head = CODE_DECISIONS_SECTION.format(parts="")
    size = len(head)
    rendered: List[str] = []
    cut_sources: List[str] = []
    rejections_left = 0
    cutting = False
    for part in parts:
        kept: List[str] = []
        if not cutting:
            # The fence for the whole part is at least as long as the one for
            # any of its lines, so measuring with it never runs past the cap.
            fence = context_mod.fence_for("\n".join(part.lines))
            separator = 2 if rendered else 0
            used = size + separator + len(part.label) + 2 + 2 * len(fence) + len(part.info) + 2
            for line in part.lines:
                if used + len(line) + 1 > cap:
                    cutting = True
                    break
                kept.append(line)
                used += len(line) + 1
            if kept and "\n".join(kept).strip():
                block = part.label + "\n\n" + fenced("\n".join(kept), part.info)
                rendered.append(block)
                size += separator + len(block)
        if len(kept) < len(part.lines):
            if part.source:
                cut_sources.append(part.source)
            else:
                rejections_left = len(part.lines) - len(kept)
    section = CODE_DECISIONS_SECTION.format(parts="\n\n".join(rendered)).rstrip()
    note = _cut_note(cap, cut_sources, rejections_left)
    return section + ("\n\n" + note if note else "")


def _cut_note(cap: int, sources: List[str], rejections_left: int) -> Optional[str]:
    """What a cut left out; None when nothing was."""
    if not sources and not rejections_left:
        return None
    clauses: List[str] = []
    if sources:
        clauses.append("Full text: %s" % ", ".join("`%s`" % source for source in sources))
    if rejections_left:
        clauses.append("%d earlier rejected finding(s) left out" % rejections_left)
    return "This section was cut at {:,} chars. {}.".format(cap, "; ".join(clauses))
