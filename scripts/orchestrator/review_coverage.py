"""A report's coverage in words: which unclean state it is in, and what each reader says about it."""

from __future__ import annotations

from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from . import workspace as ws


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


class _Wording(NamedTuple):
    """What the two readers say about one state, as ``%(name)s`` templates.

    ``headline`` is ``consolidated.md``'s ``- Coverage:`` line, and None
    where that report has no line of its own for the state. ``advice`` is the
    line ``review status`` prints.
    """

    headline: Optional[str]
    advice: str


#: Every state's wording, keyed by ``(state, design)``. ``round_raised`` is not
#: a state ``coverage_state`` returns: it is ``round_unverified`` whose recorded
#: size fits the limit configured now, which only the advice tells apart.
_WORDING: Dict[Tuple[str, bool], _Wording] = {
    ("round_unverified", False): _Wording(
        "- Coverage: round unverified -- the change body (%(size)s chars) was handed over as a "
        "file%(against)s; not a clean review",
        "not a clean review: %(partial)d reviewer(s) partial, the change body (%(size)s chars) was "
        "handed over as a file. Re-running the same snapshot gives the same answer: split the change "
        "and review the parts, so each part fits inline (<= %(limit)s chars), or raise "
        "review.context.inline_chars, then snapshot again.",
    ),
    ("round_unverified", True): _Wording(
        None,
        "not a clean review: %(partial)d reviewer(s) partial, the plan (%(size)s chars) was handed "
        "over as a file. Re-running the same plan gives the same answer: shorten .ai/plan.md to fit "
        "inline (<= %(limit)s chars), or raise review.context.inline_chars, then run the design round "
        "again.",
    ),
    ("round_raised", False): _Wording(
        None,
        "not a clean review: %(partial)d reviewer(s) partial, the change body (%(size)s chars) was "
        "handed over as a file under a lower review.context.inline_chars. The limit is %(limit)s chars "
        "now, so this snapshot fits inline: run review run against it again.",
    ),
    ("round_raised", True): _Wording(
        None,
        "not a clean review: %(partial)d reviewer(s) partial, the plan (%(size)s chars) was handed "
        "over as a file under a lower review.context.inline_chars. The limit is %(limit)s chars now, "
        "so the plan fits inline: run the design round again.",
    ),
    ("no_reviewer", False): _Wording(
        "- Coverage: %(mark)s -- no reviewer has run against this snapshot; run the reviewers "
        "against it with review run",
        "%(mark)s -- no reviewer has run against this snapshot; run the reviewers against it with "
        "review run.",
    ),
    ("no_reviewer", True): _Wording(
        None,
        "%(mark)s -- no reviewer has run against this plan; run the design round.",
    ),
    ("none_ok", False): _Wording(
        "- Coverage: %(mark)s -- no reviewer came back ok for this snapshot; re-run the reviewers "
        "that did not",
        "%(mark)s -- no reviewer came back ok for this snapshot; re-run the reviewers that did not.",
    ),
    ("none_ok", True): _Wording(
        None,
        "%(mark)s -- no reviewer came back ok for this plan; re-run the reviewers that did not.",
    ),
    ("fix_only", False): _Wording(
        "- Coverage: %(mark)s -- this round inlined the fix only; snapshot --full once the whole "
        "change fits inline",
        "%(mark)s -- re-snapshot with --full once the whole change fits inline, then run again.",
    ),
    ("fix_only", True): _Wording(
        None,
        "%(mark)s -- no round has shown a reviewer the whole plan; shorten .ai/plan.md until it fits "
        "inline, then run the design round again.",
    ),
}


def coverage_headline(coverage: Dict[str, Any], counts: Dict[str, Any]) -> str:
    """The headline form of the two coverage values.

    An unverified round is the more urgent of the two and is stated on its
    own; a clean round carrying an older mark says what is missing, because
    the answer is not "run it again" -- the same snapshot gives the same
    answer. Each state names the one thing that is missing and nothing else:
    sending a reader to re-snapshot a snapshot that already holds the whole
    change inline points them away from the reviewer they actually lack.
    """
    state = coverage_state(coverage, counts)
    if not state:
        return "- Coverage: round %s, change %s" % (
            str(coverage.get("round") or "none"),
            str(coverage.get("change") or "none"),
        )
    values: Dict[str, Any] = {"mark": unverified_phrase(coverage)}
    if state == "round_unverified":
        chars = coverage.get("change_chars")
        values["size"] = ws.fmt_size(chars)
        # The limit rides along when the round recorded one, because it is
        # configuration: "handed over as a file" says nothing on its own once
        # the reader cannot assume which number decided that.
        limit = coverage.get("inline_chars")
        values["against"] = ""
        if isinstance(limit, int) and limit:
            values["against"] = " (review.context.inline_chars %s)" % ws.fmt_int(limit)
    # The code review's wording, for a design round too: the report does not
    # record which kind of round it is, so ``render_consolidation`` has no
    # design flag to pass, and the design rows hold no headline.
    return (_WORDING[(state, False)].headline or "") % values


def coverage_advice(
    coverage: Dict[str, Any],
    counts: Dict[str, Any],
    budget_spent: bool,
    inline_chars: int,
    design: bool = False,
) -> List[str]:
    """What to do about an unverified coverage, if anything.

    Each line names the one action that changes the answer. Another round is
    never it: the snapshot is frozen, so re-running it sends the same prompt
    and gets the same verdict. Narrowing what the round *shows* is never it
    either, which is why ``--base`` is not named here: a smaller diff is a
    smaller round, not a reviewed change, and pointing at the flag that moves
    the mark without moving the change is pointing at the way around it.

    The state is ``coverage_state``, the same classification
    ``consolidated.md`` words: the two commands read one file and must not
    describe it differently -- an advice line telling the reader to re-snapshot
    with ``--full`` when what is missing is a reviewer sends them to redo the
    thing they just did.

    A design round is judged on ``.ai/plan.md`` itself, and none of the
    code-review remedies can be aimed at it -- ``review snapshot`` writes the
    code snapshot and there is no ``--design`` form of it. The only thing that
    makes an oversize plan reviewable is a shorter plan.

    ``inline_chars`` is the *configured* limit, not the one the round in the
    report was measured against, because these lines are about the next run
    and not the last one. It is also the second way out, and the one the
    report itself cannot name: a limit is a setting, and a reader who decides
    the prompt is worth paying for raises it rather than cutting the change up.

    The recorded limit is read too, through the size beside it. A body handed
    over as a file was over the limit of its own round, so a recorded size
    that fits the configured one says the limit has been raised since -- and
    then "the same snapshot gives the same answer" is false, splitting a
    change that already fits is wasted work, and raising a limit the reader
    has just raised is worse than saying nothing. That case gets its own line.
    """
    lines = []
    chars = coverage.get("change_chars")
    values: Dict[str, Any] = {
        "size": ws.fmt_size(chars),
        "limit": ws.fmt_int(inline_chars),
        # Counted over this snapshot, like the coverage value it is quoted beside.
        "partial": snapshot_reviewers(counts, "partial"),
    }
    state = coverage_state(coverage, counts)
    values["mark"] = unverified_phrase(coverage)
    if state == "round_unverified":
        # ``coverage.change_chars`` is recorded for precisely this comparison.
        # No check that the recorded limit differs is needed: the body went
        # over as a file, so it was over the limit of its round, and a size at
        # or under the configured one says that limit is not this one. A bool
        # is not a size, whatever ``isinstance`` says about it.
        raised = (
            isinstance(chars, int)
            and not isinstance(chars, bool)
            and chars > 0
            and inline_chars > 0
            and chars <= inline_chars
        )
        if raised:
            state = "round_raised"
    if state:
        lines.append(_WORDING[(state, design)].advice % values)
    if budget_spent and coverage.get("change") == "unverified":
        lines.append("iteration budget exhausted -- report the change as not reviewed in full")
    return lines
