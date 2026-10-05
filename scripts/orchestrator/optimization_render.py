"""The text of ``optimization report``, built as lines from the figures.

``optimization_report`` works the figures out and says nothing about how they
read; this module words them and reads nothing. The command loads the run
logs, picks JSON or text, and prints what :func:`report_lines` returns.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import optimization_report as opt_report
from . import workspace as ws

_OPT_ROW = "  %-22s %s"


def report_lines(report: Dict[str, Any], source: str) -> List[str]:
    """The whole report, as ``cmd_optimization_report`` prints it line by line.

    ``report`` is ``summarise_rounds`` with ``scorecard`` and
    ``architect_revisions`` attached; ``source`` names what was read.
    """
    revisions = report["architect_revisions"]
    if (
        not report["rounds"]
        and not report["design_rounds"]
        and not report["design_refused"]
        and not revisions["attempts"]
    ):
        return [
            "No review rounds recorded in %s." % source,
            "Run a review, then ask again -- this reads what happened, not what would.",
        ]
    if not report["rounds"] and not report["design_rounds"] and not report["design_refused"]:
        # Revisions alone: the review blocks below would all be zeroes.
        return _revision_rows(revisions)

    lines = _level_lines(report) + _spend_lines(report) + _tool_lines(report) + _context_lines(report)
    paired = report.get("paired") or {}
    if paired.get("pairs_listed"):
        lines.append("")
        lines.extend(_paired_rows(paired))
    lines.extend(_scorecard_rows(report["scorecard"]))
    if revisions["attempts"]:
        lines.append("")
        lines.extend(_revision_rows(revisions))
    return lines + _footer_lines(report)


def _level_lines(report: Dict[str, Any]) -> List[str]:
    """``Review rounds recorded``: what the level decided, round by round."""
    # Every row in this block describes a decision the level made, and no level
    # decides anything for a design round. So the block is code review's alone,
    # and is skipped rather than filled with zeroes when only design rounds
    # were recorded -- a `levels in force` of `-` invites the reader to conclude
    # the dial did nothing, when it was never asked.
    if not report["rounds"]:
        return []
    lines = [
        "Review rounds recorded: %d (%d ran, %d refused)"
        % (report["rounds"], report["ran"], report["refused"])
    ]
    if report["refused"]:
        # Which of them refused, because the two mean different things to
        # act on: the gate says fix the tests, the limit says the change
        # is too big to review at all.
        lines.append(_OPT_ROW % ("refused by", _counts(report["refused_by"])))
    lines.append(_OPT_ROW % ("levels in force", _counts(report["levels"])))
    lines.append(_OPT_ROW % ("gate verdicts", _counts(report["gates"])))
    lines.append(_OPT_ROW % ("panel reduced", report["panel_reduced"]))
    conditional = report.get("conditional") or {}
    if any(conditional.values()):
        # Only once a log holds a conditional decision: a panel without
        # one would print a row of zeroes about a setting it never used.
        row = "added x%d, left out x%d" % (conditional.get("added", 0), conditional.get("left_out", 0))
        if conditional.get("declared_rounds"):
            row += ", declared with --high-risk x%d" % conditional["declared_rounds"]
        lines.append(_OPT_ROW % ("conditional reviewers", row))
    relevance = report.get("relevance") or {}
    if relevance.get("judged"):
        lines.append(_OPT_ROW % ("roles skipped", _relevance_row(relevance)))
    lines.append(_OPT_ROW % ("escalated (high risk)", report["escalated"]))
    if report["escalation_patterns"]:
        lines.append(_OPT_ROW % ("  caused by", _counts(report["escalation_patterns"])))
    if report["always_escalated"]:
        lines.append(
            "  every round escalated, so the level you configured never applied. "
            "Narrow optimization.high_risk_paths and optimization.extra_high_risk_paths, "
            "or accept that this repository reviews at quality."
        )
    lines.append("")
    return lines


def _spend_lines(report: Dict[str, Any]) -> List[str]:
    """``Reviewer runs``: the total, then each stage's share of it."""
    total_runs = report["reviewer_runs"] + report["design_reviewer_runs"]
    total_reported = report["measured_runs"] + report["design_measured_runs"]
    total_billed = report["billed_tokens"] + report["design_billed_tokens"]
    lines = [
        "Reviewer runs: %d (%d reported usage), %s billed"
        % (total_runs, total_reported, ws.fmt_int(total_billed))
    ]
    if report["design_rounds"]:
        # The total above was code review only, so the one command asked what
        # review cost answered with half of it: measured on one workflow,
        # `Reviewer runs: 8` beside four design runs and 350,429 billed tokens
        # that appeared nowhere. Split into rows rather than merged, because a
        # round against a plan and a round against a diff are not the same unit
        # of work and a per-round figure spanning both describes neither.
        if report["rounds"]:
            code_row = _runs_row(
                report["reviewer_runs"],
                report["measured_runs"],
                report["billed_tokens"],
                report["ran"],
                report["billed_per_round"],
            )
            lines.append(_OPT_ROW % ("code review", code_row))
        design_row = _runs_row(
            report["design_reviewer_runs"],
            report["design_measured_runs"],
            report["design_billed_tokens"],
            report["design_rounds"],
            report["design_billed_per_round"],
        )
        lines.append(_OPT_ROW % ("design review", design_row))
        design_relevance = report.get("design_relevance") or {}
        if design_relevance.get("judged"):
            lines.append(_OPT_ROW % ("  roles skipped", _relevance_row(design_relevance)))
    elif report["billed_per_round"]:
        lines.append("  %s billed per round that ran" % ws.fmt_int(report["billed_per_round"]))
    return lines


def _relevance_row(relevance: Dict[str, Any]) -> str:
    """``left out x12 of 40 judged (quality x5), est. 1,200,000 tokens``."""
    row = "left out x%d of %d judged" % (relevance.get("left_out", 0), relevance.get("judged", 0))
    by_level = relevance.get("left_out_by_level") or {}
    if by_level:
        row += " (%s)" % _counts(by_level)
    if relevance.get("estimated_saving"):
        row += ", est. %s tokens" % ws.fmt_int(relevance["estimated_saving"])
    return row


def _tool_lines(report: Dict[str, Any]) -> List[str]:
    """Tool activity per run, for each stage some run reported it for."""
    if not (report["tool_reported_runs"] or report["design_tool_reported_runs"]):
        return []
    lines = ["", "Tool activity, per run and only over the runs that reported it:"]
    if report["tool_reported_runs"]:
        row = _tools_row(report, "", report["reviewer_runs"])
        lines.append(_OPT_ROW % ("code review", row))
    if report["design_tool_reported_runs"]:
        row = _tools_row(report, "design_", report["design_reviewer_runs"])
        lines.append(_OPT_ROW % ("design review", row))
    lines.extend(
        [
            "  Divided by the runs that reported it, never by every reviewer run:",
            "  Codex reports none, and a mixed panel would otherwise halve the figure",
            "  for no reason but its composition.",
            "  Observed output is what the tools printed back, not source read: `wc -l`",
            "  returns 3 characters for a 200-line file and `cat` returns the file.",
        ]
    )
    return lines


def _context_lines(report: Dict[str, Any]) -> List[str]:
    """Code review rounds with surrounding context beside the rounds without."""
    by_context = report.get("by_context") or {}
    # Only once a round has actually carried context: before that there is
    # nothing to compare, and a block of dashes would read as a finding.
    if not (by_context.get("with") or {}).get("rounds"):
        return []
    lines = ["", "Surrounding context (review.context.surrounding), code review rounds only:"]
    for name, label in (("with", "with context"), ("without", "without context")):
        group = by_context.get(name) or {}
        lines.append("  %-22s %s" % (label, _context_row(group, name == "with")))
        lines.append("  %-22s %s" % ("", _context_per_run_row(group)))
    lines.extend(
        [
            "  The raw figures move with the size of each change and with the number of reviewers in the",
            "  panel; compare the per-run lines. Codex reports no tool activity, by design: its runs are in",
            "  the billed figures and out of the tool ones, which is why each tool figure names the runs it",
            "  was divided by.",
        ]
    )
    return lines


def _footer_lines(report: Dict[str, Any]) -> List[str]:
    """What the figures above leave out: refusals, the saving, rounds with no test result."""
    lines: List[str] = []
    if report["design_refused"]:
        lines.append("")
        lines.append(
            "Design review rounds refused for size: %d -- nothing ran, and the plan and its"
            % report["design_refused"]
        )
        lines.append("request were over review.context.max_chars.")
    if report["estimated_saving"]:
        lines.append("")
        lines.append(
            "Estimated saving from %d gate-refused round(s): ~%s billed tokens."
            % (report["refused_by"].get("gate", 0), ws.fmt_int(report["estimated_saving"]))
        )
        lines.append("An estimate: what a round that did not happen would have cost is")
        lines.append("unknowable, so this is the mean of the %d that did." % report["ran"])
    refused_for_size = report["refused_by"].get("context")
    if refused_for_size:
        lines.append("")
        lines.append("%d round(s) refused for size are not priced above: a change over" % refused_for_size)
        lines.append("review.context.max_chars was going to cost more than the mean, so")
        lines.append("charging it the mean would understate what was not spent.")
    if report["rounds_without_a_test_result"]:
        lines.append("")
        lines.append(
            "%d of %d round(s) ran with no test result recorded, so the gate had"
            % (report["rounds_without_a_test_result"], report["rounds"])
        )
        lines.append("nothing to act on and cannot have fired. Record one before `review run`:")
        lines.append("  dev-orchestra state record test ok|failed")
    return lines


def _revision_rows(revisions: Dict[str, Any]) -> List[str]:
    """The ``Architect revisions`` block: one line per group, then why runs went fresh."""
    lines = ["Architect revisions (cost against each workflow's initial design run):"]
    for name in ("resumed", "fresh"):
        group = revisions[name]
        failed = group["failed_attempts"]
        head = "%s: %d revisions (%d priced, %d with ratio)" % (
            name,
            group["runs"],
            group["priced_runs"],
            group["ratio_runs"],
        )
        if group["cost_ratio_mean"] is None:
            # The attempts still cost something even with no ratio to show.
            lines.append(
                "  %s cost ratio to initial n/a (initial run has no usable cost); %d stalled + %d rejected "
                "+ %d failed attempts (%d priced)"
                % (head, failed["stalled"], failed["rejected"], failed["failed"], failed["priced"])
            )
            continue
        per_completed = group["cost_per_completed_ratio"]
        lines.append(
            "  %s cost ratio to initial %.2f mean, %s per completed incl. %d stalled + %d rejected "
            "+ %d failed attempts (%d priced)"
            % (
                head,
                group["cost_ratio_mean"],
                "n/a" if per_completed is None else "%.2f" % per_completed,
                failed["stalled"],
                failed["rejected"],
                failed["failed"],
                failed["priced"],
            )
        )
    reasons = revisions["fallbacks"]["reasons"]
    if reasons:
        lines.append("  --resume ran fresh because:")
        for reason, count in sorted(reasons.items()):
            lines.append("    %s x%d" % (reason, count))
    return lines


def _counts(counter: Dict[str, int]) -> str:
    return ", ".join("%s x%d" % item for item in sorted(counter.items())) or "-"


def _figure(value: Any) -> str:
    """A per-run figure, or ``-`` where nobody reported one."""
    if value is None:
        return "-"
    return "{:,.1f}".format(value) if isinstance(value, float) else ws.fmt_int(value)


def _usd(value: Optional[float]) -> str:
    # Two places where `tokens show` prints four: a per-accepted figure is read
    # as a price, and a fraction of a cent on it is noise to the reader.
    return "-" if value is None else "$%.2f" % value


_SCORECARD_OUTCOMES = ("reported", "accepted", "rejected", "duplicate", "open")

#: Under a stage whose recorded rounds were not all readable. The rounds that
#: were lost are not a random few: they are the early rounds, before the fixes,
#: so no correction is possible and the most that can be said is that the
#: figures lean -- not by how much, and not which way.
_SCORECARD_BIAS = (
    "  Reported and accepted are counted over the rounds whose report could be read; a round\n"
    "  with none is left out of every figure, its cost included. Before 0.11.0 only a workflow's\n"
    "  last round was kept, and the lost rounds reviewed the %s before its findings were fixed,\n"
    "  so what survives is not a sample of the whole: a rejection rate over it is biased, and the\n"
    "  direction of the bias is not known. Per-accepted figures are taken over the readable\n"
    "  rounds only; the unread rounds could move them either way."
)

_SCORECARD_FLOORS = (
    "  Cost totals are floors: a run that reported nothing adds nothing, and one that reported\n"
    "  tokens but no price (Codex) adds no dollars."
)

_SCORECARD_PAIRS = (
    "  %d round(s) %s a --surrounding pair: both runs' cost is in, and its findings and triage\n"
    "  are the second run's. See the paired block for what it measured."
)

_SCORECARD_ALONE = (
    "  Found alone is an upper bound on what dropping the reviewer would lose: a duplicate no\n"
    "  candidate link joined is counted as found alone.\n"
    "  Rates are printed from %d decided findings; below that the counts stand alone."
)

_SCORECARD_EFFORT = (
    "Review effort, code and design together: %d accepted over %d of %d recorded round(s); %s billed,"
)

#: Under the figure for both stages together. Added although the per-round
#: figures are not: those divide by a round, which is a different unit of work
#: for a plan and for a diff, and this divides by an accepted finding, which is
#: one defect the owner decided to fix whichever stage found it.
_SCORECARD_TOTAL = (
    "  Added here because both stages count the same thing, a finding the owner accepted; the\n"
    "  per-round figures above are not added, because a round of each is a different unit of\n"
    "  work. Read it beside the two stage figures: the mix of stages moves it, and it is taken\n"
    "  over the readable rounds only."
)


def _scorecard_counts(group: Dict[str, Any], alone: bool) -> str:
    counts = tuple(int(group.get(field) or 0) for field in _SCORECARD_OUTCOMES)
    row = "%d reported: %d accepted, %d rejected, %d duplicate, %d open" % counts
    if alone:
        row += "; %d found alone (%d accepted)" % (
            int(group.get("alone") or 0),
            int(group.get("alone_accepted") or 0),
        )
    return row


def _scorecard_spend(group: Dict[str, Any], panel: bool) -> str:
    """Runs and what they cost; the panel says how many of its runs were priced."""
    runs = int(group.get("runs") or 0)
    row = "%d run(s)" % runs
    if group.get("failed_runs"):
        row += " (%d failed)" % int(group["failed_runs"])
    billed = int(group.get("billed_tokens") or 0)
    if not group.get("measured_runs") and not billed:
        return row + ", nothing reported"
    row += ", %s billed" % ws.fmt_int(billed)
    priced = int(group.get("priced_runs") or 0)
    if not priced:
        return row + ", no cost reported"
    if panel:
        return row + ", %s over %d of %d run(s)" % (_usd(group.get("cost_usd")), priced, runs)
    return row + ", %s over %d priced run(s)" % (_usd(group.get("cost_usd")), priced)


def _scorecard_per_accepted(group: Dict[str, Any]) -> str:
    per = group.get("billed_per_accepted")
    if per is None:
        return "per accepted withheld under %d accepted" % opt_report.SCORECARD_MIN_DECIDED
    cost = group.get("cost_per_accepted")
    if cost is None:
        # Said, not left out: a reviewer that prices nothing did not find its
        # findings for free, and a missing column reads as if it had.
        return "%s billed per accepted, $ -" % ws.fmt_int(per)
    return "%s billed / %s per accepted" % (ws.fmt_int(per), _usd(cost))


def _scorecard_rates(group: Dict[str, Any]) -> str:
    rate = group.get("rejection_rate")
    if rate is None:
        # Accepted is a part of decided, so under this threshold both are.
        return "rates withheld under %d decided" % opt_report.SCORECARD_MIN_DECIDED
    return "%.0f%% rejected, %s" % (rate * 100, _scorecard_per_accepted(group))


def _scorecard_cost_row(group: Dict[str, Any], panel: bool) -> str:
    """The second line of a scorecard row: what the runs cost, and the rates."""
    return "%s; %s" % (_scorecard_spend(group, panel), _scorecard_rates(group))


def _scorecard_rows(scorecard: Dict[str, Any]) -> List[str]:
    """``optimization report``'s scorecard: one block per stage, then both together.

    A stage with no report to read is left out rather than printed as zeroes,
    which would read as a panel that found nothing.
    """
    lines: List[str] = []
    for stage, label, subject in (("code", "code review", "code"), ("design", "design review", "plan")):
        block = scorecard.get(stage) or {}
        read = int(block.get("rounds_read") or 0)
        if not read:
            continue
        recorded = int(block.get("rounds_recorded") or 0)
        unreviewed = int(block.get("rounds_unreviewed") or 0)
        head = "Reviewer scorecard, %s: %d of %d recorded round(s)" % (label, read, recorded)
        head += " had a report to read"
        if unreviewed:
            head += "; %d round(s) no reviewer reviewed" % unreviewed
        lines.extend(["", head + "."])
        for name, group in sorted((block.get("reviewers") or {}).items()):
            lines.append(_OPT_ROW % (name, _scorecard_counts(group, True)))
            cost_row = _scorecard_cost_row(group, False)
            if group.get("when"):
                cost_row += " (when: %s; left out of %d round(s))" % (
                    group["when"],
                    int(group.get("left_out_rounds") or 0),
                )
            lines.append(_OPT_ROW % ("", cost_row))
        panel = block.get("panel") or {}
        lines.append(_OPT_ROW % ("panel", _scorecard_counts(panel, False)))
        lines.append(_OPT_ROW % ("", _scorecard_cost_row(panel, True)))
        if recorded - read - unreviewed > 0:
            lines.extend((_SCORECARD_BIAS % subject).splitlines())
        lines.extend(_SCORECARD_FLOORS.splitlines())
        rerun = int(block.get("rerun_rounds") or 0)
        if rerun:
            lines.extend((_SCORECARD_PAIRS % (rerun, "was" if rerun == 1 else "were")).splitlines())
        lines.extend((_SCORECARD_ALONE % opt_report.SCORECARD_MIN_DECIDED).splitlines())
    if not lines:
        return lines
    total = scorecard.get("total") or {}
    priced = int(total.get("priced_runs") or 0)
    cost = "no cost reported"
    if priced:
        cost = "%s over %d priced run(s)" % (_usd(total.get("cost_usd")), priced)
    counts = (
        int(total.get("accepted") or 0),
        int(total.get("rounds_read") or 0),
        int(total.get("rounds_recorded") or 0),
        ws.fmt_int(int(total.get("billed_tokens") or 0)),
    )
    lines.append("")
    lines.append(_SCORECARD_EFFORT % counts)
    lines.append("  %s; %s" % (cost, _scorecard_per_accepted(total)))
    lines.extend(_SCORECARD_TOTAL.splitlines())
    return lines


def _paired_rows(paired: Dict[str, Any]) -> List[str]:
    """``optimization report``'s block of runs paired on one snapshot."""
    lines = [
        "Paired on one snapshot (--surrounding none vs enclosing: the same frozen diff, tree, panel "
        "and prompt inputs):"
    ]
    for pair in paired.get("pairs") or []:
        head = "  %s" % str(pair.get("snapshot") or "")[:12]
        if pair.get("workflow"):
            head += " in %s" % pair["workflow"]
        head += "   change %s chars; panel %s; %s context chars adopted (%s as carried), %s left out" % (
            _figure(pair.get("change_chars")),
            _panel_names(pair.get("panel") or []),
            ws.fmt_int(int(pair.get("adopted_chars") or 0)),
            ws.fmt_int(int(pair.get("context_chars") or 0)),
            ws.fmt_int(int(pair.get("trimmed_chars") or 0)),
        )
        for reason in _pair_exclusions(pair):
            head += "; " + reason
        lines.append(head)
        for name in ("with", "without"):
            side = pair.get(name) or {}
            lines.append(
                "      %-8s %d run(s), %s billed, %s per run; %s use(s)/run, %s observed output chars/run "
                "(%d of %d run(s) reported%s)"
                % (
                    name + ":",
                    int(side.get("reviewer_runs") or 0),
                    ws.fmt_int(int(side.get("billed_tokens") or 0)),
                    _figure(side.get("billed_per_run")),
                    _figure(side.get("tool_uses_per_run")),
                    _figure(side.get("tool_output_chars_per_run")),
                    int(side.get("tool_reported_runs") or 0),
                    int(side.get("reviewer_runs") or 0),
                    _output_runs(side.get("tool_output_reported_runs"), side.get("tool_reported_runs")),
                )
            )
        delta = pair.get("delta") or {}
        lines.append(
            "      delta:   %s billed/run, %s use(s)/run, %s observed output chars/run"
            % (
                _figure(delta.get("billed_per_run")),
                _figure(delta.get("tool_uses_per_run")),
                _figure(delta.get("tool_output_chars_per_run")),
            )
        )
    counted = int(paired.get("pairs_total") or 0)
    ours, theirs, delta = paired.get("with") or {}, paired.get("without") or {}, paired.get("delta") or {}
    fields = ("billed_per_run", "tool_uses_per_run", "tool_output_chars_per_run")
    lines.append(
        "  total, %d pair(s) counted   with: %s billed/run, %s use(s)/run, %s observed output chars/run; "
        "without: %s; delta: %s"
        % (
            counted,
            _figure(ours.get(fields[0])),
            _figure(ours.get(fields[1])),
            _figure(ours.get(fields[2])),
            ", ".join(_figure(theirs.get(field)) for field in fields),
            ", ".join(_figure(delta.get(field)) for field in fields),
        )
    )
    lines.extend(
        [
            "  %d pair(s) is a small-sample observation, not a statistical result. A counted" % counted,
            "  pair holds the change, the tree, the panel, the delivered reviews and the other prompt",
            "  inputs equal; what it does not hold equal is the reviewers' own run-to-run variation, so",
            "  one pair says what happened once. Codex reports no tool activity, so the tool figures",
            "  are over the runs that reported them. A pair is listed but left out of the total when",
            '  its panels differ ("panels differ"), when a reviewer run on either side did not deliver',
            '  ("not delivered"), when the two runs had different prompt inputs ("inputs differ"), or',
            '  when the enclosing run adopted nothing ("nothing adopted").',
        ]
    )
    return lines


def _panel_names(panel: List[Dict[str, Any]]) -> str:
    names = []
    for entry in panel:
        details = (entry.get("provider"), entry.get("model") or "?", entry.get("role") or "?")
        names.append("%s (%s, %s, %s)" % (entry.get("id"), *details))
    return ", ".join(names)


def _pair_exclusions(pair: Dict[str, Any]) -> List[str]:
    """Why a listed pair is left out of the total, one clause per reason."""
    reasons = []
    if not pair.get("same_panel"):
        reasons.append("panels differ (without: %s)" % _panel_names(pair.get("without_panel") or []))
    if not pair.get("delivered"):
        undelivered = pair.get("undelivered") or []
        names = ["%s: %s %s" % (run.get("side"), run.get("id"), run.get("status")) for run in undelivered]
        reasons.append("not delivered (%s)" % ", ".join(names))
    if not pair.get("same_inputs"):
        reasons.append("inputs differ (%s)" % ", ".join(pair.get("inputs_differ") or []))
    if pair.get("nothing_adopted"):
        reasons.append("nothing adopted")
    return reasons


def _context_row(group: Dict[str, Any], adopted: bool) -> str:
    """One group's raw figures: rounds, runs, billed and tool activity."""
    row = "%d round(s), %d run(s), %s billed, %s per round" % (
        int(group.get("rounds") or 0),
        int(group.get("reviewer_runs") or 0),
        ws.fmt_int(int(group.get("billed_tokens") or 0)),
        _figure(group.get("billed_per_round")),
    )
    row += "; %s use(s)/run, %s observed output chars/run (%d of %d run(s) reported%s)" % (
        _figure(group.get("tool_uses_per_run")),
        _figure(group.get("tool_output_chars_per_run")),
        int(group.get("tool_reported_runs") or 0),
        int(group.get("reviewer_runs") or 0),
        _output_runs(group.get("tool_output_reported_runs"), group.get("tool_reported_runs")),
    )
    if adopted:
        row += "; %s context chars adopted, %s left out" % (
            ws.fmt_int(int(group.get("adopted_chars") or 0)),
            ws.fmt_int(int(group.get("trimmed_chars") or 0)),
        )
    return row


def _context_per_run_row(group: Dict[str, Any]) -> str:
    """The same group per run and per 1k chars of change -- the line to compare."""
    return (
        "per run and 1k chars of change (%d sized round(s), %s chars): %s billed over %d billed run(s), "
        "%s observed output chars over %d reporting run(s)"
        % (
            int(group.get("sized_rounds") or 0),
            ws.fmt_int(int(group.get("change_chars") or 0)),
            _figure(group.get("billed_per_run_per_1k_change_chars")),
            int(group.get("sized_billed_runs") or 0),
            _figure(group.get("tool_output_chars_per_run_per_1k_change_chars")),
            int(group.get("sized_output_runs") or 0),
        )
    )


def _runs_row(runs: int, reported: int, billed: int, rounds: int, per_round: Optional[int]) -> str:
    """One stage's share of the reviewer spend, for a row under the total."""
    row = "%d (%d reported usage), %s billed over %d round(s)" % (
        runs,
        reported,
        ws.fmt_int(billed),
        rounds,
    )
    if per_round:
        row += ", %s each" % ws.fmt_int(per_round)
    return row


def _tools_row(report: Dict[str, Any], prefix: str, runs: int) -> str:
    """One stage's tool activity, with the denominator it was divided by.

    The denominator is printed because it is the part that can mislead: "6.5
    uses/run" over half a panel is a different claim from the same figure over
    all of it, and only the count says which.
    """
    tool_runs = report["%stool_reported_runs" % prefix]
    return "%s use(s)/run, %s observed output chars/run (%d of %d run(s) reported%s)" % (
        report["%stool_uses_per_run" % prefix],
        _figure(report["%stool_output_chars_per_run" % prefix]),
        tool_runs,
        runs,
        _output_runs(report.get("%stool_output_reported_runs" % prefix), tool_runs),
    )


def _output_runs(printed_runs: Any, tool_runs: Any) -> str:
    """``, output chars over K run(s)`` when fewer runs reported output than
    reported tools (agy counts calls, not their output); "" otherwise, and
    where none did, as the figure then shows ``-``."""
    printed = int(printed_runs or 0)
    if printed and printed != int(tool_runs or 0):
        return ", output chars over %d run(s)" % printed
    return ""
