"""Consolidating the reports: dedup, triage, the fix brief, the round records."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, cast

from . import context as context_mod
from . import workspace as ws
from .review_common import SEVERITIES, SEVERITY_RANK, TRIAGE_STATUSES, accepted_findings
from .review_fanout import ReviewerRun, _snapshot_sha, _stamp
from .review_parsing import _HEADER_MARKER, parse_findings
from .review_snapshot import ReviewError

# --------------------------------------------------------------------------- consolidation

_WORD_RE = re.compile(r"[a-z0-9]+")


def _fingerprint(text: str) -> str:
    return " ".join(_WORD_RE.findall((text or "").lower()))


def finding_key(finding: Dict[str, Any]) -> str:
    """A stable identity for one finding, independent of its position."""
    return "%s|%s" % (finding.get("file", ""), _fingerprint(finding.get("problem", ""))[:200])


def _line_number(value: str) -> Optional[int]:
    match = re.search(r"\d+", value or "")
    return int(match.group(0)) if match else None


def _same_locus(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    if left["file"] != right["file"]:
        return False
    lnum, rnum = _line_number(left.get("line", "")), _line_number(right.get("line", ""))
    if lnum is None or rnum is None:
        return True
    return abs(lnum - rnum) <= 10


def are_duplicates(left: Dict[str, Any], right: Dict[str, Any], threshold: float = 0.72) -> bool:
    """Two findings are the same issue when they sit at the same place and say the same thing."""
    if not _same_locus(left, right):
        return False
    left_text = _fingerprint("%s %s" % (left.get("problem", ""), left.get("recommended_fix", "")))
    right_text = _fingerprint("%s %s" % (right.get("problem", ""), right.get("recommended_fix", "")))
    if not left_text or not right_text:
        return False
    if left.get("category") == right.get("category") and left_text == right_text:
        return True
    return difflib.SequenceMatcher(None, left_text, right_text).ratio() >= threshold


_BACKTICK_RE = re.compile(r"`([^`]{2,120})`")
_CODEISH_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+|[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+"
)
_STOPWORDS = frozenset(
    "the a an and or of to in on for with without any is are be it this that not no".split()
)


def code_tokens(finding: Dict[str, Any]) -> set:
    """Identifier-ish tokens a finding points at.

    Two reviewers describing the same bug rarely use the same prose, but they
    almost always quote the same code. That makes the quoted code a much better
    similarity signal than the wording -- good enough to *suggest* a duplicate,
    not good enough to merge on, because distinct bugs in one expression quote
    it identically too.
    """
    text = " ".join(str(finding.get(field, "")) for field in ("evidence", "problem", "recommended_fix"))
    tokens = set()
    for span in _BACKTICK_RE.findall(text):
        for token in _CODEISH_RE.findall(span):
            tokens.add(token.lower())
        bare = span.strip().lower()
        if bare and " " not in bare and len(bare) > 2:
            tokens.add(bare)
    for token in _CODEISH_RE.findall(text):
        tokens.add(token.lower())
    return {token for token in tokens if token not in _STOPWORDS}


def duplicate_candidates(findings: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cross-reviewer findings that may be the same issue, for the orchestrator.

    Only pairs from *different* reviewers are considered: reviewers are told not
    to report the same issue twice, so two findings from one reviewer are two
    issues by construction.
    """
    token_sets = [code_tokens(finding) for finding in findings]
    frequency: Dict[str, int] = {}
    for tokens in token_sets:
        for token in tokens:
            frequency[token] = frequency.get(token, 0) + 1

    pairs: List[Dict[str, Any]] = []
    for index, left in enumerate(findings):
        for offset, right in enumerate(findings[index + 1 :], start=index + 1):
            if left.get("file") != right.get("file"):
                continue
            if set(left.get("reported_by", [])) & set(right.get("reported_by", [])):
                continue
            shared = token_sets[index] & token_sets[offset]
            if not shared:
                continue
            # A token only these two findings mention is far stronger evidence
            # than one every finding in the file quotes, so rank by the rarest
            # match and let the orchestrator work down the list.
            pairs.append(
                {
                    "ids": [left["id"], right["id"]],
                    "file": left.get("file"),
                    "shared_code": sorted(shared, key=lambda token: (frequency[token], token))[:6],
                    "specificity": min(frequency[token] for token in shared),
                }
            )
    pairs.sort(key=lambda pair: (pair["specificity"], pair["ids"]))
    return pairs


def consolidate_findings(findings: Sequence[Dict[str, Any]], threshold: float = 0.72) -> List[Dict[str, Any]]:
    """Merge duplicate findings across reviewers, keeping the strongest wording."""
    merged: List[Dict[str, Any]] = []
    for finding in sorted(findings, key=lambda f: SEVERITY_RANK.get(f.get("severity", "medium"), 2)):
        for existing in merged:
            if are_duplicates(existing, finding, threshold):
                _absorb(existing, finding)
                break
        else:
            entry = dict(finding)
            entry["reported_by"] = [finding.get("reviewer", "unknown")]
            entry["duplicate_count"] = 1
            entry.pop("reviewer", None)
            merged.append(entry)

    merged.sort(key=lambda f: (SEVERITY_RANK.get(f.get("severity", "medium"), 2), f.get("file", "")))
    for index, entry in enumerate(merged, 1):
        entry["id"] = "F%d" % index
        entry.setdefault("triage", "needs-triage")
        entry.setdefault("triage_note", "")
    return merged


def _absorb(target: Dict[str, Any], other: Dict[str, Any]) -> None:
    reviewer = other.get("reviewer", "unknown")
    if reviewer not in target["reported_by"]:
        target["reported_by"].append(reviewer)
    target["duplicate_count"] = target.get("duplicate_count", 1) + 1
    if SEVERITY_RANK.get(other.get("severity", "medium"), 2) < SEVERITY_RANK.get(
        target.get("severity", "medium"), 2
    ):
        target["severity"] = other["severity"]
    for field in ("problem", "impact", "evidence", "recommended_fix"):
        if len(other.get(field, "")) > len(target.get(field, "")):
            target[field] = other[field]
    if target.get("line") in ("", "n/a") and other.get("line") not in ("", "n/a"):
        target["line"] = other["line"]


_REPORT_SNAPSHOT_RE = re.compile(r"^-\s*Snapshot:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE)


def report_snapshot(text: str) -> str:
    """The snapshot stamp ``run_reviews`` wrote into a report header."""
    match = _REPORT_SNAPSHOT_RE.search(text.partition(_HEADER_MARKER)[0])
    return match.group(1) if match else ""


def read_reports(
    workspace: ws.Workspace,
    reviewer_ids: Sequence[str],
    expected_snapshot: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Parse reviewer reports, skipping any written against another snapshot.

    Returns ``(findings, stale_reviewer_ids)``. Without the staleness check a
    re-consolidation silently mixes a previous round's findings into the current
    one, handing the fixer issues that were already fixed.
    """
    findings: List[Dict[str, Any]] = []
    stale: List[str] = []
    for reviewer_id in reviewer_ids:
        path = workspace.reviewer_report_path(reviewer_id)
        if not os.path.isfile(path):
            continue
        text = ws.read_text(path)
        if expected_snapshot:
            stamped = report_snapshot(text)
            if stamped and stamped != expected_snapshot:
                stale.append(reviewer_id)
                continue
        findings.extend(parse_findings(text, reviewer_id))
    return findings, stale


def collect_reports(
    workspace: ws.Workspace,
    reviewer_ids: Sequence[str],
    expected_snapshot: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return read_reports(workspace, reviewer_ids, expected_snapshot)[0]


def current_snapshot_stamp(workspace: ws.Workspace) -> str:
    """The stamp reports are compared against -- same form as the header."""
    return _snapshot_sha(workspace)


def review_lineage(workspace: ws.Workspace, workflow: str = "") -> str:
    """What makes a round a continuation of the last one rather than a new one.

    The round counter lives in the consolidated report, which is per project
    and outlives any one change. On its own that made the counter count
    *snapshots ever taken here*: a second branch, with an unrelated change,
    opened at round 3 and was refused -- and ``budget reset``, which says in
    so many words that this is now a fresh workflow, did not help, because the
    one counter that stopped the work was not in the ledger it resets.

    Three things identify the review, and a change in any of them starts the
    count again:

    * the workflow, so ``budget reset`` and the idle reset mean what they say
    * the branch, because another branch is another change
    * the base, because ``--base`` is the other way of saying which change

    The coverage mark is carried on the first two alone -- the same key is not
    right for both questions, and ``coverage_lineage`` says why.
    """
    meta = workspace.read_snapshot_meta()
    return "|".join([workflow or "", _branch(workspace.root), str(meta.get("base") or "")])


def coverage_lineage(lineage: str) -> str:
    """The part of ``review_lineage`` the unverified mark carries on.

    Everything but the base: the workflow and the branch. The base is "the
    other way of saying which change", which is exactly why it cannot gate the
    mark. Narrowing the diff with ``--base`` makes the *round* smaller; it does
    not make the change reviewed, so keying the carry on it would let the mark
    be dropped by the first thing a reader reaches for when they see one.

    The round counter still keys on the base -- a review of a different base is
    a different review to count -- so this takes the full key apart rather than
    being built beside it: there is one construction of the key, and this is a
    view of it.

    What that costs, and is accepted: a second, unrelated change on the same
    branch inherits the mark until a non-incremental snapshot is reviewed
    inline. That is the cautious direction for this to fail in, and
    ``review snapshot --full`` is the way out of it.
    """
    return "|".join(lineage.split("|")[:2])


def _branch(root: str) -> str:
    """The current branch, or "" when git cannot name one.

    A detached HEAD has no name to key on, so it keys on nothing and the
    counter behaves as it did before: carrying on is the cautious direction
    for a loop guard to fail in.
    """
    code, out, _ = ws.git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    name = out.strip() if code == 0 else ""
    return "" if name in ("", "HEAD") else name


def next_iteration(workspace: ws.Workspace, lineage: str = "", current_sha: Optional[str] = None) -> int:
    """Derive the review round from what is on disk.

    The iteration budget only stops a review->fix->re-review loop if the counter
    actually advances, so it must not depend on the caller passing a number: a
    new snapshot is a new round, and re-running against the same snapshot (after
    a reviewer failed, say) stays in the current one.

    ``current_sha`` names the snapshot this round *would* take, for a caller
    that has to know the round before it is allowed to write one. Left out, the
    snapshot on disk is the one being asked about.

    A round belonging to a different review starts at one. See
    ``review_lineage`` for what "different" means and why it has to.
    """
    previous = ws.read_json(workspace.consolidated_json_path, {}) or {}
    if not previous:
        return 1
    if lineage and str(previous.get("lineage") or "") != lineage:
        return 1
    recorded = int(previous.get("iteration", 0) or 0)
    previous_sha = str((previous.get("snapshot") or {}).get("sha256") or "")
    if current_sha is None:
        current_sha = str(workspace.read_snapshot_meta().get("sha256") or "")
    if previous_sha and current_sha and previous_sha == current_sha:
        return max(recorded, 1)
    return recorded + 1


def build_consolidation(
    workspace: ws.Workspace,
    runs: Sequence[Dict[str, Any]],
    findings: Sequence[Dict[str, Any]],
    iteration: int = 1,
    lineage: str = "",
    completed_round: Optional[str] = None,
    unreviewed_round: Optional[str] = None,
) -> Dict[str, Any]:
    """The round's report: consolidated findings, counts, and its coverage.

    ``completed_round`` is the round whose reviewers have all returned, passed
    only by the caller that ran them -- for a design round only once one of
    them returned a review, for a code round whenever it ran, since nothing
    is approved over a code round. Without it the report keeps the
    round the previous report named: the snapshot's metadata names a round as
    soon as it starts, and a re-consolidation during it -- or after it failed
    -- must not claim findings nobody has reported yet.

    ``unreviewed_round`` is the design round that ran to the end with no
    reviewer's review in it. It is not a round the findings belong to, but it
    is not one still running either: ``design approve`` may go ahead over it,
    where a running round has to be waited for.

    ``runs`` is the reviewer table, which by design holds entries that did not
    run this round -- see ``_merge_runs``. Coverage, ``snapshot.over_budget``
    and the ``snapshot_`` reviewer counts are all derived from the subset
    stamped with this snapshot, so that one report describes one snapshot; see
    ``_coverage`` for what the two coverage values mean, why one round's answer
    is not the whole change's, and ``coverage_lineage`` for the key the whole
    change's answer carries on.
    """
    meta = workspace.read_snapshot_meta()
    last = ws.read_json(workspace.consolidated_json_path, {}) or {}
    # Keyed by content, never by id: ids are positional (F1..Fn, severity
    # ordered) and get reassigned every round, so an id-keyed lookup silently
    # drops a decision -- or applies it to a different finding -- as soon as the
    # finding set changes, which is exactly what fixing things does.
    previous = {finding_key(entry): entry for entry in last.get("findings", [])}
    consolidated = consolidate_findings(findings)
    for entry in consolidated:
        entry["key"] = finding_key(entry)
        old = previous.get(entry["key"])
        if old:
            entry["triage"] = old.get("triage", entry["triage"])
            entry["triage_note"] = old.get("triage_note", "")
            # Carried with the decision it marks, and only where there is one:
            # its absence is what tells a rebuilt default from a decision.
            if old.get("triage_set_at"):
                entry["triage_set_at"] = old.get("triage_set_at")
    candidates = duplicate_candidates(consolidated)
    by_id = {entry["id"]: entry for entry in consolidated}
    for pair in candidates:
        for finding_id in pair["ids"]:
            other = [other_id for other_id in pair["ids"] if other_id != finding_id]
            by_id[finding_id].setdefault("possible_duplicates", []).extend(other)
    current = _current_runs(runs, meta)
    counts = _counts(consolidated, runs, current)
    counts["duplicate_candidates"] = len(candidates)
    snapshot = {
        "sha256": meta.get("sha256"),
        "files": meta.get("files", []),
        # Read off this snapshot's reviewer entries rather than off the
        # snapshot's own metadata: which limit was in force and whether
        # --force was given are facts about the run. See ``BuiltPrompt``.
        "over_budget": any(run.get("over_budget") for run in current),
        "budget_chars": _budget_chars(current),
    }
    # The round these findings belong to: the last one that got as far as a
    # report, which is what a design approval is given over and what the
    # archived copy of this report is filed under.
    round_id = completed_round or (last.get("snapshot") or {}).get("round_id")
    if round_id:
        snapshot["round_id"] = round_id
    # Kept across a re-consolidation of that same round, and only of it: the
    # snapshot moves on the moment the next round starts.
    ended = unreviewed_round
    if ended is None and completed_round is None:
        ended = (last.get("snapshot") or {}).get("unreviewed_round")
    if ended and ended == meta.get("round_id"):
        snapshot["unreviewed_round"] = ended
    data = {
        "generated_at": ws.utcnow(),
        "iteration": iteration,
        #: Which review this round belongs to. Recorded so the next round can
        #: tell a re-review from an unrelated change that happens to share a
        #: project directory.
        "lineage": lineage,
        "snapshot": snapshot,
        "coverage": _coverage(runs, meta, iteration, lineage, last),
    }
    surrounding = _surrounding(current)
    if surrounding is not None:
        data["surrounding"] = surrounding
    data.update(
        {
            "reviewers": list(runs),
            "counts": counts,
            "duplicate_candidates": candidates,
            "findings": consolidated,
        }
    )
    return data


def _surrounding(current: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The context this snapshot's reviewers were shown, if any of them were.

    ``shared`` only when every current entry carries the same record: after
    ``--only`` with a different setting, "shown to every reviewer" would be a
    claim about reviewers who were shown something else. A ``null`` under
    ``by_reviewer`` is an entry that built its prompt with the setting off --
    an entry that fell over before building one has no stamp, is not
    current, and so is not here at all. None when no entry carries one: a
    round with the setting off, a design round, a report from before it.
    """
    records = {str(run.get("id")): run.get("surrounding") for run in current}
    if not any(isinstance(record, dict) for record in records.values()):
        return None
    values = list(records.values())
    if all(isinstance(record, dict) for record in values) and all(record == values[0] for record in values):
        return {"shared": True, **cast(Dict[str, Any], values[0])}
    by_reviewer = {key: (record if isinstance(record, dict) else None) for key, record in records.items()}
    return {"shared": False, "by_reviewer": by_reviewer}


def _coverage(
    runs: Sequence[Dict[str, Any]],
    meta: Dict[str, Any],
    iteration: int,
    lineage: str,
    last: Dict[str, Any],
) -> Dict[str, Any]:
    """What was actually put in front of a reviewer -- this round, and overall.

    The definitions below are the ones in the Coverage section of
    ``references/reviews.md``, in the same words, with the same keys and the
    same values.

    ``coverage.round`` -- whether **this round's change body**
    (``review-target.diff``, or ``review-target.md`` for a design round; on an
    incremental round that diff is the fix alone) was inlined whole into every
    reviewer's prompt this round.

    * ``complete``: inlined for every reviewer. **Says nothing about the whole
      change.** On an incremental round it means "the fix was shown in full".
    * ``unverified``: handed to one or more reviewers as a file.
    * ``none``: no reviewer ran against this snapshot (none configured, none
      run since it was taken, or every entry predates this version).

    ``coverage.change`` -- **the whole change under review**.

    * ``unverified``: this workflow and branch (``coverage_lineage``) has a
      round whose ``round`` was ``unverified``, and since then no
      non-incremental snapshot (empty ``incremental_from``) has been reviewed
      with ``round: complete`` and at least one reviewer of *that* snapshot
      ``ok``.
    * ``complete``: that has happened, or no round was ever unverified.
    * ``none``: nothing on this workflow and branch has been reviewed yet.

    ``coverage.unverified_since`` -- the round number ``change: unverified``
    started at, when that number is one of *this* count's. ``null`` otherwise,
    which includes an ``unverified`` change whose mark was carried across a
    lineage change: the mark is what the carry is for and it survives, the
    number is not and does not. ``next_iteration`` restarts the count at 1 on a
    lineage change, so a number from the previous lineage would name a round
    the current count does not have -- ``iteration 1/2`` beside "since round
    3". Read the mark from ``change``, never from this being set.

    ``coverage.change_chars`` -- how many characters this round's change body
    was, as the runs recorded it (``null`` when no run did).

    The two are separate because an incremental round inlines only the fix.
    Judging the whole change by what *this* round inlined would let a fix-only
    round launder a partial one: accept the finding, fix it, inline the fix,
    NO_FINDINGS, clean -- with four fifths of the change still unread by
    anyone. So the unverified mark carries forward until a round shows the
    whole change to somebody.

    Every value above is derived from the entries stamped with *this*
    snapshot. The reviewer table is not that set: ``_merge_runs`` keeps a
    reviewer that did not run this time so the table stays complete, and
    ``review consolidate`` starts from the previous round's table -- so
    deriving coverage from all of it would answer for a snapshot nobody has
    read. ``read_reports`` already discards a report by its stamp; this is the
    same check on the entry that report came with.
    """
    current = _current_runs(runs, meta)
    # Entries with no delivery are left out rather than guessed at: a run that
    # fell over before its prompt was built, and every round recorded before
    # this version, genuinely have no answer to give.
    deliveries = [str(run.get("delivery") or "") for run in current if run.get("delivery")]
    if "file" in deliveries:
        this_round = "unverified"
    elif deliveries:
        this_round = "complete"
    else:
        this_round = "none"

    # Carried on the lineage minus its base -- see ``coverage_lineage`` for why
    # the base is the one part of the key that must not gate it. The mark and
    # the round number it started at are carried separately: the mark is the
    # safety property and survives a change of base, the round number belongs
    # to a count that a lineage change restarts and is dropped with it.
    carry_key = coverage_lineage(lineage)
    carried_mark = False
    carried_since = None
    previous = last.get("coverage")
    if isinstance(previous, dict) and (
        not carry_key or coverage_lineage(str(last.get("lineage") or "")) == carry_key
    ):
        since = previous.get("unverified_since")
        numbered = isinstance(since, int) and not isinstance(since, bool)
        # Read from ``change``, so a mark already carried without a number
        # carries on; ``unverified_since`` alone would drop it at the next
        # round.
        carried_mark = numbered or str(previous.get("change") or "") == "unverified"
        # The condition ``next_iteration`` restarts the count on, and the only
        # one under which the carried number still names a round of it. An
        # empty lineage says nothing, there as here, and continues the count.
        if numbered and not (lineage and str(last.get("lineage") or "") != lineage):
            carried_since = since

    # This snapshot's reviewers, not the table's: an entry from an earlier
    # round coming back ``ok`` is not somebody reading this change.
    reviewers_ok = sum(1 for run in current if run.get("status") == "ok")
    if this_round == "unverified":
        unverified = True
        unverified_since = iteration if carried_since is None else carried_since
    elif this_round == "complete" and not meta.get("incremental_from") and reviewers_ok >= 1:
        # Somebody read the whole change in full. Nothing else clears this --
        # an incremental round, a round every reviewer failed, and a round
        # with no reviewers all leave the mark where it was.
        unverified = False
        unverified_since = None
    else:
        unverified = carried_mark
        unverified_since = carried_since if carried_mark else None

    if unverified:
        change = "unverified"
    elif this_round == "none" and not carried_mark:
        change = "none"
    else:
        change = "complete"

    measured = [run for run in current if run.get("delivery")]
    # Both numbers come off the entry that decided the mark -- the first one
    # handed a file when there is one, any measured entry otherwise. The limit
    # is no longer one number per round: ``--only`` re-runs a reviewer and
    # ``_merge_runs`` puts the fresh entry ahead of the retained ones, so one
    # snapshot's entries can carry limits from two configurations. Reading the
    # pair off one entry, and that the one whose delivery made the round
    # unverified, keeps it one round's two numbers and never one round's size
    # against another's limit -- a line naming a limit the size beside it does
    # not exceed contradicts itself.
    decided = next(
        (run for run in measured if str(run.get("delivery")) == "file"),
        measured[0] if measured else None,
    )
    return {
        "round": this_round,
        "change": change,
        "unverified_since": unverified_since,
        "change_chars": int(decided.get("change_chars") or 0) if decided else None,
        # The limit the size beside it was measured against. ``null`` where
        # ``change_chars`` is, and for a round recorded before the limit was
        # configurable -- the answer was 120,000 then, but nothing wrote it
        # down and this will not invent it.
        "inline_chars": (int(decided.get("inline_chars") or 0) or None) if decided else None,
    }


def _budget_chars(current: Sequence[Dict[str, Any]]) -> Optional[int]:
    """What ``review.context.max_chars`` measured this round, per the runs.

    Not ``coverage.change_chars``, which is the body a reviewer was handed: on
    the design path the budget counted the plan and the request together and
    the body handed over was the plan alone. A report that printed the second
    number beside "over review.context.max_chars" would contradict itself.
    ``None`` when no run recorded one -- the same answer coverage gives.
    """
    for run in current:
        chars = run.get("budget_chars")
        if isinstance(chars, int) and not isinstance(chars, bool) and chars:
            return chars
    return None


def _current_runs(runs: Sequence[Dict[str, Any]], meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The entries stamped with this snapshot -- the only ones it is judged on.

    The reviewer table is not that set: ``_merge_runs`` keeps a reviewer that
    did not run this time so the table stays complete. Coverage and the
    ``snapshot_`` counts both read this subset, so that the coverage line and
    the tally printed beside it describe the same snapshot.
    """
    return [run for run in runs if str(run.get("snapshot") or "") == _stamp(meta)]


def _counts(
    findings: Sequence[Dict[str, Any]],
    runs: Sequence[Dict[str, Any]],
    current: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Finding and reviewer tallies -- the reviewers counted twice, by design.

    ``reviewers_*`` is the whole table, which deliberately outlives the round;
    ``snapshot_reviewers_*`` is the subset stamped with the snapshot this
    report is about, the set ``_coverage`` answers from. They differ after
    ``--only``, and on a snapshot nobody has run against, and quoting one where
    the other is meant makes one report describe two snapshots. Both are named
    so neither can be read as the other; ``snapshot_reviewers`` reads the one
    that goes with a coverage value.
    """
    by_severity = dict.fromkeys(SEVERITIES, 0)
    for finding in findings:
        by_severity[finding.get("severity", "medium")] = (
            by_severity.get(finding.get("severity", "medium"), 0) + 1
        )
    counts = {
        "findings_total": len(findings),
        "by_severity": by_severity,
        "duplicates_merged": sum(max(0, f.get("duplicate_count", 1) - 1) for f in findings),
    }
    for prefix, entries in (("", runs), ("snapshot_", current)):
        counts[prefix + "reviewers_total"] = len(entries)
        counts[prefix + "reviewers_ok"] = sum(1 for run in entries if run.get("status") == "ok")
        # Each reviewer lands in exactly one of the three, so the columns still
        # sum to the total. Counting "not ok" as failed would have put a
        # partial reviewer in two of them at once.
        counts[prefix + "reviewers_partial"] = sum(1 for run in entries if run.get("status") == "partial")
        counts[prefix + "reviewers_failed"] = sum(
            1 for run in entries if run.get("status") not in ("ok", "partial")
        )
    return counts


def snapshot_reviewers(counts: Dict[str, Any], column: str) -> int:
    """One reviewer column, counted over this snapshot's entries.

    Falls back to the table's column for a report written before the scoped
    counts existed: it is the better of the two answers available there, and
    the alternative is reading every such report as a round nobody ran.
    """
    scoped = counts.get("snapshot_reviewers_%s" % column)
    return int((counts.get("reviewers_%s" % column) if scoped is None else scoped) or 0)


def coverage_state(coverage: Dict[str, Any], counts: Dict[str, Any]) -> str:
    """Which unclean state a report's coverage is in, or ``""`` for none.

    ``consolidated.md`` and ``review status`` word the answer differently --
    one is a line in a report, the other names the action that changes it --
    but they read the same file and must not disagree about which state it
    describes. So the state is decided once, here, and each of them words the
    answer it is given.

    * ``round_unverified``: this round handed the change body over as a file.
    * ``no_reviewer``: the mark is carried and no reviewer ran against this
      snapshot. Nothing was inlined this round, because nothing ran.
    * ``none_ok``: reviewers ran against this snapshot and none came back
      ``ok``, so the round cleared nothing and neither would re-snapshotting.
    * ``fix_only``: the round's body was inlined and read, and the mark is
      still carried -- which, since ``_coverage`` clears it for exactly a
      non-incremental round with a reviewer ``ok``, means the body was the fix
      alone.
    """
    this_round = str(coverage.get("round") or "none")
    if this_round == "unverified":
        return "round_unverified"
    if str(coverage.get("change") or "none") != "unverified":
        return ""
    if this_round == "none":
        return "no_reviewer"
    if snapshot_reviewers(counts, "ok") == 0:
        return "none_ok"
    return "fix_only"


def unverified_phrase(coverage: Dict[str, Any]) -> str:
    """The mark in words, with the round it started at when there is one.

    The mark carries across a change of base and the round number cannot --
    see ``coverage.unverified_since`` in ``_coverage``. Both readers state the
    mark either way rather than print a round number that is not in the count
    beside it.
    """
    since = coverage.get("unverified_since")
    if isinstance(since, int) and not isinstance(since, bool):
        return "change unverified since round %d" % since
    return "change unverified"


def _coverage_line(coverage: Dict[str, Any], counts: Dict[str, Any]) -> str:
    """The headline form of the two coverage values.

    An unverified round is the more urgent of the two and is stated on its
    own; a clean round carrying an older mark says what is missing, because
    the answer is not "run it again" -- the same snapshot gives the same
    answer. Each state names the one thing that is missing and nothing else:
    sending a reader to re-snapshot a snapshot that already holds the whole
    change inline points them away from the reviewer they actually lack.
    """
    state = coverage_state(coverage, counts)
    if state == "round_unverified":
        chars = coverage.get("change_chars")
        size = ws.fmt_size(chars)
        # The limit rides along when the round recorded one, because it is
        # configuration: "handed over as a file" says nothing on its own once
        # the reader cannot assume which number decided that.
        limit = coverage.get("inline_chars")
        against = ""
        if isinstance(limit, int) and limit:
            against = " (review.context.inline_chars %s)" % ws.fmt_int(limit)
        return (
            "- Coverage: round unverified -- the change body (%s chars) was handed over "
            "as a file%s; not a clean review" % (size, against)
        )
    if state == "no_reviewer":
        return (
            "- Coverage: %s -- no reviewer has run against this snapshot; run the reviewers "
            "against it with review run" % unverified_phrase(coverage)
        )
    if state == "none_ok":
        return (
            "- Coverage: %s -- no reviewer came back ok for this snapshot; re-run the reviewers "
            "that did not" % unverified_phrase(coverage)
        )
    if state == "fix_only":
        return (
            "- Coverage: %s -- this round inlined the fix only; snapshot --full once the whole "
            "change fits inline" % unverified_phrase(coverage)
        )
    return "- Coverage: round %s, change %s" % (
        str(coverage.get("round") or "none"),
        str(coverage.get("change") or "none"),
    )


def _over_budget_line(snapshot: Dict[str, Any]) -> str:
    """That this round only ran because a human said so.

    It says who, because that is the part a reader of the report cannot see
    anywhere else: the orchestrator is told not to reach for ``--force``, so a
    round carrying this mark was a person's decision and the report has to
    name it as one. The size is the one the budget measured, recorded beside
    the flag it decided -- not ``coverage.change_chars``, which answers the
    other question this line is not about. See ``_budget_chars``.
    """
    chars = snapshot.get("budget_chars")
    return "- Change: %s chars, over review.context.max_chars -- reviewed only because --force was given" % (
        ws.fmt_size(chars)
    )


def _reviewer_lines(counts: Dict[str, Any]) -> List[str]:
    """The table's tally, and this snapshot's when the two differ.

    A single unnamed "Reviewers: 2 ok / 2 total" beside a coverage line about
    this snapshot is the report describing two snapshots at once -- the table
    outlives the round on purpose (``_merge_runs``), so after ``--only``, or on
    a snapshot nobody has run against, its tally is not this snapshot's. When
    the totals match, so do the sets: the scoped entries are a subset of the
    table, so one line says both. A report from before the scoped counts has
    only the one tally, and gets the line it always had.
    """
    lines = ["- Reviewers: %s" % _tally(counts, "")]
    scoped = counts.get("snapshot_reviewers_total")
    if scoped is not None and scoped != counts.get("reviewers_total"):
        lines.append("- Reviewers of this snapshot: %s" % _tally(counts, "snapshot_"))
    return lines


def _tally(counts: Dict[str, Any], prefix: str) -> str:
    line = "%s ok / %s total" % (
        counts.get(prefix + "reviewers_ok"),
        counts.get(prefix + "reviewers_total"),
    )
    partial = int(counts.get(prefix + "reviewers_partial") or 0)
    return line + (", %d partial" % partial if partial else "")


def render_consolidation(data: Dict[str, Any]) -> str:
    counts = data.get("counts", {})
    lines = [
        "# Consolidated review",
        "",
        "- Generated: %s" % data.get("generated_at"),
        "- Iteration: %s" % data.get("iteration"),
        "- Snapshot: %s" % str(data.get("snapshot", {}).get("sha256", ""))[:12],
    ]
    lines += _reviewer_lines(counts)
    # Left out rather than reported as "none": a report written before this
    # version recorded no coverage at all, and a line saying so would claim it
    # had been measured and found empty.
    coverage = data.get("coverage")
    if isinstance(coverage, dict):
        lines.append(_coverage_line(coverage, counts))
    # Only when recorded, for the reason coverage is: a report from before the
    # setting, or with it off, measured nothing about context.
    surrounding = data.get("surrounding")
    if isinstance(surrounding, dict):
        lines.append(_surrounding_line(surrounding))
    # Only when it happened. A line on every report saying a round was *not*
    # over budget would bury the one round that was, and every report written
    # before this existed would be claiming something nobody measured.
    snapshot = data.get("snapshot") or {}
    if snapshot.get("over_budget"):
        lines.append(_over_budget_line(snapshot))
    lines += [
        "- Findings: %s (%s duplicate report(s) merged)"
        % (counts.get("findings_total"), counts.get("duplicates_merged")),
        "",
        "## Reviewers",
        "",
    ]
    for run in data.get("reviewers", []):
        status = run.get("status")
        mark = "ok" if status == "ok" else ("PARTIAL" if status == "partial" else "FAILED")
        lines.append(
            "- %s [%s] %s / %s / %s -- %s"
            % (
                mark,
                run.get("role"),
                run.get("id"),
                run.get("provider"),
                run.get("model") or "unknown model",
                (run.get("error") or "%s finding(s)" % run.get("findings", 0)),
            )
        )
    if isinstance(surrounding, dict):
        lines += _surrounding_section(surrounding)
    candidates = data.get("duplicate_candidates") or []
    if candidates:
        lines += [
            "",
            "## Possible duplicates (confirm during triage)",
            "",
            "Different reviewers quoting the same code. Auto-merge stays conservative on",
            "purpose -- collapsing two distinct bugs hides one -- so these are suggestions.",
            "Mark a confirmed one with `review triage <id> --status duplicate`.",
            "",
        ]
        for pair in candidates:
            lines.append(
                "- %s  (`%s`, shared: %s)"
                % (
                    " ~ ".join(pair["ids"]),
                    pair.get("file", "?"),
                    ", ".join("`%s`" % c for c in pair["shared_code"]),
                )
            )
    lines += ["", "## Findings", ""]
    if not data.get("findings"):
        lines.append("No findings were reported.")
    for finding in data.get("findings", []):
        lines += [
            "### %s [%s] %s"
            % (finding["id"], finding["severity"].upper(), finding.get("category", "general")),
            "",
            "- Location: `%s`:%s" % (finding.get("file", "?"), finding.get("line", "n/a")),
            "- Reported by: %s" % ", ".join(finding.get("reported_by", [])),
            "- Possible duplicate of: %s"
            % (", ".join(dict.fromkeys(finding.get("possible_duplicates", []))) or "none"),
            "- Triage: %s%s"
            % (finding.get("triage", "needs-triage"), _note_suffix(finding.get("triage_note", ""))),
            "- Problem: %s" % finding.get("problem", ""),
            "- Impact: %s" % finding.get("impact", ""),
            "- Evidence: %s" % finding.get("evidence", ""),
            "- Recommended fix: %s" % finding.get("recommended_fix", ""),
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def _surrounding_line(block: Dict[str, Any]) -> str:
    """The headline form of the context this snapshot's reviewers were shown."""
    if block.get("shared") is False:
        parts = []
        for reviewer_id, record in (block.get("by_reviewer") or {}).items():
            if isinstance(record, dict):
                parts.append("%s: %s" % (reviewer_id, context_mod.brief(record)))
            else:
                parts.append("%s: none" % reviewer_id)
        return "- Surrounding context: differs by reviewer -- %s" % "; ".join(parts)
    return "- Surrounding context: enclosing -- %s" % context_mod.summary(block)


def _surrounding_section(block: Dict[str, Any]) -> List[str]:
    """Every symbol shown, every one left out, and every file not extracted, by name.

    Only when there is a name to give. A reviewer that fell over before its
    prompt was built is not here: the Reviewers table already says it failed.
    """
    if block.get("shared") is False:
        records = block.get("by_reviewer") or {}
        if not any(
            isinstance(record, dict)
            and (record.get("adopted") or record.get("trimmed") or record.get("skipped"))
            for record in records.values()
        ):
            return []
        lines = ["", "## Surrounding context"]
        for reviewer_id, record in records.items():
            lines += ["", "### %s" % reviewer_id, ""]
            if not isinstance(record, dict):
                lines.append("None (ran with review.context.surrounding none).")
            else:
                lines += _surrounding_names(record, "Shown")
        return lines
    if not (block.get("adopted") or block.get("trimmed") or block.get("skipped")):
        return []
    return ["", "## Surrounding context", "", *_surrounding_names(block, "Shown to every reviewer")]


def _surrounding_names(record: Dict[str, Any], shown: str) -> List[str]:
    lines: List[str] = []
    adopted = [c for c in record.get("adopted") or [] if isinstance(c, dict)]
    trimmed = [c for c in record.get("trimmed") or [] if isinstance(c, dict)]
    if adopted:
        lines.append(
            "%s (%d symbol(s), %s chars):"
            % (shown, len(adopted), ws.fmt_int(int(record.get("adopted_chars") or 0)))
        )
        lines += ["- %s" % context_mod.describe(c) for c in adopted]
    else:
        lines.append("Nothing adopted (%s)." % (record.get("reason") or "no budget"))
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in trimmed:
        groups.setdefault(str(candidate.get("reason") or "budget"), []).append(candidate)
    for reason, members in groups.items():
        lines += ["", "Left out (%s):" % reason]
        lines += ["- %s" % context_mod.describe(c) for c in members]
    skipped = [entry for entry in record.get("skipped") or [] if isinstance(entry, dict)]
    if skipped:
        lines += ["", "Not extracted (%d file(s)):" % len(skipped)]
        lines += ["- `%s` -- %s" % (entry.get("path"), entry.get("reason") or "?") for entry in skipped]
    return lines


def _note_suffix(note: str) -> str:
    return " (%s)" % note if note else ""


def findings_signature(data: Dict[str, Any]) -> str:
    """A stable fingerprint of a round's outcome.

    Two rounds with the same signature mean the fix changed nothing the
    reviewers can see, which is a reason to stop that arrives sooner and more
    accurately than an iteration budget.
    """
    keys = sorted(
        finding.get("key") or finding_key(finding)
        for finding in data.get("findings", [])
        if finding.get("triage") not in ("rejected", "duplicate")
    )
    if not keys:
        return "no-findings"
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]


def set_triage(data: Dict[str, Any], finding_id: str, status: str, note: str = "") -> Dict[str, Any]:
    if status not in TRIAGE_STATUSES:
        raise ReviewError(
            "unknown triage status %r (expected one of %s)" % (status, ", ".join(TRIAGE_STATUSES))
        )
    for finding in data.get("findings", []):
        if finding.get("id") == finding_id:
            finding["triage"] = status
            finding["triage_note"] = note
            # Written with every decision, ``needs-triage`` included: a rebuilt
            # report defaults a finding to ``needs-triage`` too, and only this
            # says the owner put it back there rather than never deciding.
            finding["triage_set_at"] = ws.utcnow()
            return data
    raise ReviewError("no finding with id %r" % finding_id)


def round_key(data: Dict[str, Any]) -> Tuple[str, str]:
    """The round a consolidated report belongs to: ``(sha12, round_id)``.

    The sha alone does not name a round. Freezing the same plan or the same
    tree again repeats it, and the round id is what tells the two rounds
    apart. The surrounding-context side of a measured pair is deliberately
    not part of it: the pair reads one freeze twice and is one round.

    Taken from the report's content, never its file name, so the archive and
    the live report of one round compare equal. ``("", "")`` for a report
    written before anything was frozen.
    """
    snapshot = data.get("snapshot") or {}
    sha = str(snapshot.get("sha256") or "")
    if not sha:
        return "", ""
    return _stamp(snapshot), str(snapshot.get("round_id") or "")


def save_consolidation(workspace: ws.Workspace, data: Dict[str, Any]) -> str:
    """Write the live report and its round's archived copy; return the copy's path.

    The live report is what everything downstream reads, and the next round
    replaces it. The copy under ``rounds/`` is the same record kept after
    that, overwritten only by a later write to the same round -- a re-run,
    a re-consolidation, a triage. Only the json is archived: the markdown is
    a rendering of it.

    A report with no snapshot sha gets no copy and ``""`` back. It was written
    before anything was frozen, so there is no round it could belong to, and
    no reviewer event will ever ask for it. Nor does one built for another
    round than its own -- see ``_built_for_another_round``.
    """
    ws.write_json(workspace.consolidated_json_path, data)
    ws.write_text(workspace.consolidated_md_path, render_consolidation(data))
    if not (data.get("snapshot") or {}).get("sha256"):
        return ""
    if _built_for_another_round(workspace, data):
        return ""
    path = workspace.round_report_path(round_key(data))
    ws.write_json(path, data)
    return path


def _built_for_another_round(workspace: ws.Workspace, data: Dict[str, Any]) -> bool:
    """Whether a report was built after the current freeze under an older round's id.

    A design round no reviewer returned a review for, and a re-consolidation
    before the round's reviewers are back, keep the round id of the last
    report (see ``build_consolidation``) over the current freeze's content.
    When the same plan or tree was frozen again, that is the older round's
    key, and filing the report under it would replace that round's findings
    and triage. A report built before the freeze -- the previous round's,
    triaged since -- is still its own round's.
    """
    meta = workspace.read_snapshot_meta()
    current = str(meta.get("round_id") or "")
    if not current or str((data.get("snapshot") or {}).get("round_id") or "") == current:
        return False
    return str(data.get("generated_at") or "") >= str(meta.get("generated_at") or "")


def recorded_rounds(workspace: ws.Workspace) -> List[Dict[str, Any]]:
    """Every round's report this workspace still has, one per round.

    The archived copies and the live report, which wins where both name one
    round: it is the same record, and the live one is what ``review status``
    shows. A workflow from before the archive existed has only the live
    report, and is read as one round. A live report built for another round
    than its own is no round's, and is left out rather than standing in for
    the round whose id it carries. Each entry is a copy of the report with
    ``live`` added.
    """
    rounds: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for path in ws.list_files(workspace.rounds_dir, ".json"):
        data = ws.read_json(path, None)
        if isinstance(data, dict) and round_key(data)[0]:
            rounds[round_key(data)] = dict(data, live=False)
    live = ws.read_json(workspace.consolidated_json_path, None)
    if isinstance(live, dict) and live and not _built_for_another_round(workspace, live):
        rounds[round_key(live)] = dict(live, live=True)
    return list(rounds.values())


def unresolved_blocking(
    data: Dict[str, Any], severities: Sequence[str] = ("critical", "high")
) -> List[Dict[str, Any]]:
    """Findings that should block completion: accepted or untriaged and severe."""
    blocking = []
    for finding in data.get("findings", []):
        if finding.get("severity") not in severities:
            continue
        if finding.get("triage") in ("rejected", "duplicate"):
            continue
        blocking.append(finding)
    return blocking


def render_fix_brief(data: Dict[str, Any], title: str = "Fix these accepted findings") -> str:
    """The prompt payload handed to the Review Fixer: accepted findings only.

    Every line here is billed twice: once as the reviewer's output and again
    as the fixer's input. So a field that is empty is omitted rather than sent
    as a label with nothing after it, and who reported a finding is dropped --
    the fixer's job is the same whoever noticed.

    ``title`` is the one thing a design brief needs to say differently: the
    findings are about a plan, so what is being asked for is a revision rather
    than a fix, and every item below the heading reads the same either way.
    """
    findings = accepted_findings(data)
    if not findings:
        return "No accepted findings. Nothing to fix.\n"
    lines = ["# %s" % title, ""]
    for finding in findings:
        lines += [
            "## %s [%s] %s:%s"
            % (
                finding["id"],
                finding["severity"].upper(),
                finding.get("file", "?"),
                finding.get("line", "n/a"),
            ),
            "",
        ]
        for label, key in (
            ("Category", "category"),
            ("Problem", "problem"),
            ("Impact", "impact"),
            ("Evidence", "evidence"),
            ("Fix", "recommended_fix"),
        ):
            value = str(finding.get(key) or "").strip()
            if value:
                lines.append("- %s: %s" % (label, value))
        lines.append("")
    return "\n".join(lines)


def summarise_runs(runs: Sequence[ReviewerRun]) -> Tuple[int, int, int]:
    """``(ok, failed, partial)`` -- three columns that sum to ``len(runs)``.

    Partial is its own column rather than a kind of failure. The reviewer ran,
    and its findings are real; what is missing is the guarantee that it saw
    the whole change. Folding it into ``failed`` would have counted it twice
    against a total that has not changed.
    """
    ok = sum(1 for run in runs if run.status == "ok")
    partial = sum(1 for run in runs if run.status == "partial")
    return ok, len(runs) - ok - partial, partial
