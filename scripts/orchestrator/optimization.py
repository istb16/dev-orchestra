"""How hard to try to be cheap, and when not to try at all.

Three levers, one dial. ``optimization.level`` decides whether a review round
runs at all when the tests are known to be red, how many reviewers a small
change gets, and how many findings each one is asked for.

Two things shape everything here.

The first is that this tool cannot run the project's tests. It does not know
the command -- the orchestrator discovers that from the repository and runs it
directly. So the gate reads a *recorded* result (``state record test ok``,
which ``references/workflow.md`` has always told the orchestrator to write)
rather than a result it produced itself. That leaves three states, not two:
failed, passed, and never recorded. Only the first is worth refusing on.
Refusing on the third would break every existing workflow the day it shipped,
to punish people for not having written down something that was previously
optional.

The second is that cheap is not the goal; cheap *for the same outcome* is. A
change that touches authentication, a migration, a payment path or a deploy
config gets the full treatment whatever the dial says, because the saving on
one skipped reviewer is worth a fraction of one missed authorisation bug. That
escalation is not configurable down to nothing: the patterns are, the fact
that a match escalates is not.
"""

from __future__ import annotations

import fnmatch
import posixpath
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Ordered least to most careful, so a comparison can ask "at least as careful
#: as", and so an escalation can be expressed as a maximum rather than a jump.
LEVELS = ("aggressive", "balanced", "quality")
DEFAULT_LEVEL = "balanced"

#: What each level asks a reviewer for. ``review.max_findings`` set to an
#: explicit number overrides this; left unset, the level decides.
MAX_FINDINGS_BY_LEVEL = {"aggressive": 4, "balanced": 6, "quality": 10}

#: Below both of these, and touching nothing high-risk, a change is small
#: enough that one reviewer is a reasonable trade. The saving is one delegated
#: run and the cost of being wrong is the cross-model disagreement that makes
#: this tool worth running, so the line is drawn low -- but not so low that it
#: never applies. Measured over eleven real rounds at 2 files / 50 lines: the
#: panel was reduced zero times, and no round came close.
DEFAULT_LOW_RISK_MAX_FILES = 5
DEFAULT_LOW_RISK_MAX_LINES = 150

#: Paths where a small diff is not a small change.
#:
#: Every entry here was chosen because the damage is invisible in the diff: a
#: deleted permission check, a migration that locks a table, a secret moved
#: into a file that ships. Line count says nothing about any of them -- the
#: authorisation bug is routinely the one-line change -- so size alone can
#: never be the test.
#:
#: Matched against the whole path and, for a pattern with no slash, against the
#: basename too. Replace the list wholesale in config to suit a codebase whose
#: names differ; that is the part that is configurable.
DEFAULT_HIGH_RISK_PATHS = (
    # Authentication and authorisation.
    "*auth*",
    "*login*",
    "*session*",
    "*permission*",
    "*policy*",
    "*acl*",
    # Secrets.
    "*secret*",
    "*credential*",
    "*password*",
    ".env",
    ".env.*",
    # Money.
    "*payment*",
    "*billing*",
    "*checkout*",
    "*invoice*",
    # Data shape and data loss.
    "*.sql",
    "*migration*/*",
    "*/migration*/*",
    "*migrate*/*",
    "*/migrate*/*",
    "*schema*",
    # Cryptography.
    "*crypto*",
    "*cipher*",
    # What runs in production.
    "Dockerfile",
    "Dockerfile.*",
    "*.tf",
    # Both forms of every directory pattern. ``fnmatch`` has no ``**``, and a
    # pattern containing a slash is matched against the whole path, so
    # ``*/k8s/*`` needs something before the directory and misses it at the
    # repository root -- which is where it usually lives.
    "k8s/*",
    "*/k8s/*",
    "deploy/*",
    "*/deploy/*",
    ".github/workflows/*",
    "*/.github/workflows/*",
)

#: What the gate does with each of the three recorded test states.
GATE_REFUSE = "refuse"
GATE_WARN = "warn"
GATE_ALLOW = "allow"


def normalise_level(value: Any) -> str:
    """A level we recognise, or the default. Never raises.

    Validation reports a bad level at ``config validate``; a run that reaches
    this point with one should be careful rather than broken, and the default
    is the careful-enough middle.
    """
    if isinstance(value, str) and value.strip().lower() in LEVELS:
        return value.strip().lower()
    return DEFAULT_LEVEL


def at_least(level: str, floor: str) -> str:
    """``level``, raised to ``floor`` if it is less careful than that."""
    return floor if LEVELS.index(level) < LEVELS.index(floor) else level


def high_risk_matches(paths: Sequence[str], patterns: Sequence[str]) -> List[Tuple[str, str]]:
    """Every ``(path, pattern)`` pair that makes this change high-risk.

    Pairs rather than a boolean because a refusal to optimise has to be able
    to say which file caused it. "Running the full panel because this change
    touches `db/migrate/003_drop_orders.rb`" is a sentence someone can check;
    "running the full panel" is one they can only obey.
    """
    hits: List[Tuple[str, str]] = []
    for raw in paths:
        path = posixpath.normpath(str(raw).replace("\\", "/"))
        while path.startswith("./"):
            path = path[2:]
        if not path:
            continue
        base = posixpath.basename(path)
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            if fnmatch.fnmatchcase(path, pattern) or (
                "/" not in pattern and fnmatch.fnmatchcase(base, pattern)
            ):
                hits.append((path, pattern))
                break
    return hits


class Plan:
    """What this level decided, and what it decided it from."""

    def __init__(
        self,
        requested: str,
        level: str,
        gate: str,
        test_status: str,
        max_findings: int,
        reviewer_limit: Optional[int],
        high_risk: Sequence[Tuple[str, str]] = (),
        files: int = 0,
        lines: int = 0,
    ) -> None:
        #: The configured level, before any escalation.
        self.requested = requested
        #: The level actually in force.
        self.level = level
        #: One of GATE_REFUSE / GATE_WARN / GATE_ALLOW.
        self.gate = gate
        #: "ok", "failed", or "" when nothing was ever recorded.
        self.test_status = test_status
        self.max_findings = max_findings
        #: None means every configured reviewer runs.
        self.reviewer_limit = reviewer_limit
        self.high_risk = list(high_risk)
        self.files = files
        self.lines = lines

    @property
    def escalated(self) -> bool:
        return self.level != self.requested

    def escalation_note(self) -> str:
        if not self.escalated:
            return ""
        first = self.high_risk[0] if self.high_risk else ("", "")
        more = len(self.high_risk) - 1
        return "%s → %s: %s matches %s%s" % (
            self.requested,
            self.level,
            first[0],
            first[1],
            " (and %d more)" % more if more > 0 else "",
        )

    def gate_note(self) -> str:
        if self.gate == GATE_REFUSE:
            return (
                "refusing to review: the last recorded test run failed. Reviewing code "
                "that does not pass its own tests spends a reviewer on a problem you "
                "already know about. Fix the tests, record the result, and run again "
                "-- or pass --force."
            )
        if self.gate == GATE_WARN:
            return (
                "note: no test result recorded for this change, so the review is "
                "running unchecked. `state record test ok` before `review run` lets "
                "this stop a review of a red tree."
            )
        return ""

    def reviewer_note(self) -> str:
        if self.reviewer_limit is None:
            return ""
        return "low-risk change (%d file(s), %d line(s)): %d reviewer instead of the full panel" % (
            self.files,
            self.lines,
            self.reviewer_limit,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_level": self.requested,
            "level": self.level,
            "escalated": self.escalated,
            "high_risk": [{"path": p, "pattern": q} for p, q in self.high_risk],
            "gate": self.gate,
            "test_status": self.test_status,
            "max_findings": self.max_findings,
            "reviewer_limit": self.reviewer_limit,
            "files": self.files,
            "lines": self.lines,
        }


def decide(
    settings: Dict[str, Any],
    review_settings: Dict[str, Any],
    paths: Sequence[str],
    lines: int,
    test_status: str,
    reviewers: int,
    reviewed_files: Optional[int] = None,
) -> Plan:
    """Work out what this round should cost, from the change and the config.

    ``paths`` is every path the change touches, and is what risk is judged
    from -- a withheld ``.env`` is still a secret, a deleted auth file is
    still an auth file. ``reviewed_files`` is how many files a reviewer will
    actually be shown, and is what the size threshold is measured against,
    for the same reason the line count already excludes withheld files:
    otherwise a one-line fix next to a lockfile bump stops counting as small
    while the diff a reviewer sees is two lines long. It defaults to
    ``len(paths)`` for a caller that has only one number.
    """
    requested = normalise_level(settings.get("level"))
    patterns = settings.get("high_risk_paths")
    if not isinstance(patterns, (list, tuple)):
        patterns = DEFAULT_HIGH_RISK_PATHS
    hits = high_risk_matches(paths, patterns)
    level = at_least(requested, "quality") if hits else requested

    status = (test_status or "").strip().lower()
    if status in ("failed", "fail", "error", "red"):
        gate = GATE_ALLOW if level == "quality" else GATE_REFUSE
    elif status:
        gate = GATE_ALLOW
    else:
        gate = GATE_ALLOW if level == "quality" else GATE_WARN

    max_findings = findings_cap(settings, review_settings, level)

    files = len(paths) if reviewed_files is None else max(int(reviewed_files), 0)
    limit = None
    # Any level short of `quality`, which is the level that means "spend what
    # it takes". Restricting this to `aggressive` made it unreachable in the
    # repositories that most need it: a high-risk hit escalates to `quality`,
    # and `quality` is not `aggressive`, so in an infrastructure repository
    # where `*.tf` matches on most rounds the dial could not fire at all.
    # `hits` is still what stops it -- a small change to an auth file gets the
    # full panel -- but a small change to nothing risky no longer pays for two
    # independent reviewers to agree it is small.
    if level != "quality" and reviewers > 1:
        max_files = _positive(settings.get("low_risk_max_files"), DEFAULT_LOW_RISK_MAX_FILES)
        max_lines = _positive(settings.get("low_risk_max_lines"), DEFAULT_LOW_RISK_MAX_LINES)
        if files <= max_files and lines <= max_lines:
            limit = 1

    return Plan(
        requested,
        level,
        gate,
        status,
        max_findings,
        limit,
        hits,
        files=files,
        lines=lines,
    )


def findings_cap(
    settings: Dict[str, Any], review_settings: Dict[str, Any], level: Optional[str] = None
) -> int:
    """How many findings a reviewer is asked for.

    Apart from ``decide`` because a review with no diff to measure -- the
    design review -- still needs the cap, and must not have to fabricate a
    size and a test result to get one. ``level`` is passed in by ``decide``
    after any escalation has been settled; a caller with no change to judge
    leaves it out and gets the configured level.
    """
    configured = review_settings.get("max_findings")
    if isinstance(configured, int) and not isinstance(configured, bool) and configured >= 0:
        return configured
    return MAX_FINDINGS_BY_LEVEL[normalise_level(level if level is not None else settings.get("level"))]


def _positive(value: Any, fallback: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


#: The status a refused round is recorded under. It is not a failure: nothing
#: was attempted. It is the one outcome that has to be written down even
#: though nothing ran, because a saving that leaves no trace cannot be counted
#: and a feature whose effect cannot be counted gets argued about instead.
REFUSED = "refused"


def summarise_rounds(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """What the level decided, over every review round in a run log.

    Read from ``.ai/state.json`` rather than from the ledger, because the
    ledger is one workflow and the question is about many: a level's effect is
    a rate, not a number. ``budget reset`` starts a fresh ledger; the event log
    keeps accumulating.

    The saving is reported as an estimate and labelled as one. What a refused
    round *would* have cost is unknowable -- it did not happen -- so the
    figure is the mean of the rounds that did run, in the same repository,
    which is the closest honest stand-in.

    Design rounds are counted under their own keys and nowhere else. A plan has
    no diff to measure and no test result to gate on, so a design round carries
    no ``optimization`` block and no level decided anything for it -- which is
    why it cannot join the figures above. Counting it nowhere at all is what
    shipped, and it hid a real cost from the one command whose job is to say
    what review cost: measured on one workflow, two design rounds and 350,429
    billed tokens sat in ``tokens show`` and appeared in this report as zero.
    ``design_rounds`` counts the rounds that ran, as ``ran`` does for code,
    and ``design_refused`` the ones that were refused before they could --
    counting those nowhere would hide the same cost from the other end.

    A refusal is broken down by what refused it, because the two are not worth
    the same. ``estimated_saving`` prices only the gate's: a round refused for
    the size of its change was always going to be dearer than the mean, so
    charging it the mean understates it, and a figure that understates by an
    unknown amount is worse than one that is explicitly not being claimed. An
    event recorded before ``refused_by`` existed is read as the gate's, which
    is what it was -- nothing else refused a round then.
    """
    rounds = [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("stage") == "review"
        and isinstance(event.get("optimization"), dict)
    ]
    # Only rounds that finished, and matched on what a finished round *is*
    # rather than on a list of the ways one ends badly: ``end`` writes whatever
    # status it is handed and ``clear_stalls`` adds its own, so an exclusion
    # list is a thing to keep in sync with every status ever added. A round that
    # raised or was abandoned after a kill billed nothing, and counting it in
    # the divisor halves the per-round figure of the round that did run.
    design = [
        event
        for event in events
        if isinstance(event, dict) and event.get("stage") == "design_review" and event.get("status") == "ok"
    ]
    design_refused = sum(
        1
        for event in events
        if isinstance(event, dict)
        and event.get("stage") == "design_review"
        and event.get("status") == REFUSED
    )
    by_context = {"with": _context_group(), "without": _context_group()}
    levels: Dict[str, int] = {}
    gates: Dict[str, int] = {}
    patterns: Dict[str, int] = {}
    refused_by: Dict[str, int] = {}
    escalated = reduced = refused = unrecorded = 0
    reviewer_runs = measured_runs = billed = 0
    tool_runs = tool_uses = tool_chars = 0
    measured: List[Dict[str, Any]] = []

    for event in rounds:
        plan = event["optimization"]
        _bump(levels, str(plan.get("level") or "?"))
        _bump(gates, str(plan.get("gate") or "?"))
        if plan.get("escalated"):
            escalated += 1
            # Which pattern, not just how many. A dial that is escalated out of
            # existence on every round looks identical in a count to one that
            # never fires, and only the pattern says which -- and whether it is
            # the one to replace.
            for hit in plan.get("high_risk") or []:
                if isinstance(hit, dict) and hit.get("pattern"):
                    _bump(patterns, str(hit["pattern"]))
        if plan.get("reviewer_limit") is not None:
            reduced += 1
        if not str(plan.get("test_status") or ""):
            unrecorded += 1
        if event.get("status") == REFUSED:
            refused += 1
            _bump(refused_by, str(event.get("refused_by") or "gate"))
            continue
        runs, reported, spent = _reviewer_spend(event)
        reviewer_runs += runs
        measured_runs += reported
        billed += spent
        said, called, printed = _reviewer_tools(event)
        tool_runs += said
        tool_uses += called
        tool_chars += printed
        _add_to_context_group(
            by_context["with" if _with_context(event) else "without"],
            event,
            (runs, reported, spent),
            (said, called, printed),
        )
        if isinstance(event.get("measurement"), dict):
            measured.append(event)

    design_runs = design_measured = design_billed = 0
    design_tool_runs = design_tool_uses = design_tool_chars = 0
    for event in design:
        runs, reported, spent = _reviewer_spend(event)
        design_runs += runs
        design_measured += reported
        design_billed += spent
        said, called, printed = _reviewer_tools(event)
        design_tool_runs += said
        design_tool_uses += called
        design_tool_chars += printed

    ran = len(rounds) - refused
    per_round = billed // ran if (ran and billed) else None
    # Its own number, never averaged with the code figure above: a plan and a
    # diff are not the same unit of work, and a mean of the two sizes neither.
    design_per_round = design_billed // len(design) if (design and design_billed) else None
    return {
        "rounds": len(rounds),
        "ran": ran,
        "refused": refused,
        "refused_by": refused_by,
        "levels": levels,
        "gates": gates,
        "escalated": escalated,
        "escalation_patterns": patterns,
        "always_escalated": bool(rounds) and escalated == len(rounds),
        "panel_reduced": reduced,
        "rounds_without_a_test_result": unrecorded,
        "reviewer_runs": reviewer_runs,
        "measured_runs": measured_runs,
        "billed_tokens": billed,
        "billed_per_round": per_round,
        # The gate's refusals alone -- see the docstring for why a round
        # refused for size is left out rather than priced at the mean.
        "estimated_saving": (per_round * refused_by.get("gate", 0)) if per_round else 0,
        "design_rounds": len(design),
        "design_refused": design_refused,
        "design_reviewer_runs": design_runs,
        "design_measured_runs": design_measured,
        "design_billed_tokens": design_billed,
        "design_billed_per_round": design_per_round,
        # Per *run*, over the runs that reported -- never over ``reviewer_runs``.
        # None rather than zero where nothing reported: a panel of CLIs that do
        # not count tools has not measured no tool use.
        "tool_reported_runs": tool_runs,
        "tool_uses": tool_uses,
        "tool_output_chars": tool_chars,
        "tool_uses_per_run": _per_run(tool_uses, tool_runs),
        "tool_output_chars_per_run": _per_run(tool_chars, tool_runs),
        "design_tool_reported_runs": design_tool_runs,
        "design_tool_uses": design_tool_uses,
        "design_tool_output_chars": design_tool_chars,
        "design_tool_uses_per_run": _per_run(design_tool_uses, design_tool_runs),
        "design_tool_output_chars_per_run": _per_run(design_tool_chars, design_tool_runs),
        # Code rounds that ran, split on whether a reviewer was handed
        # surrounding context -- see ``_with_context``.
        "by_context": {name: _finish_context_group(group) for name, group in by_context.items()},
        # Runs of one snapshot with and without it -- see ``_pair_rounds``.
        "paired": _pair_rounds(measured),
    }


#: What ``inputs_differ`` calls each prompt input, where the key is not a name.
_INPUT_NAMES = {"context_sha256": "context"}

PAIRED_NOTE = "small-sample observation over pairs of runs on one snapshot, not a statistical result"


def _pair_rounds(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Code rounds run twice on one snapshot, with and without surrounding context.

    Only rounds run with ``review run --surrounding``, which record the whole
    snapshot identity; the side is the flag's value, never whether context
    was adopted. The latest run of each side makes the pair. A pair is listed
    whatever it holds, and counted toward the totals only when everything but
    the context was held equal: the panel, a delivered review from every run,
    the other prompt inputs -- and when the context side adopted anything.
    """
    sides: Dict[Tuple[Any, ...], Dict[str, Dict[str, Any]]] = {}
    for event in events:
        side = _pair_side(event)
        if side is None:
            continue
        slot = sides.setdefault(_pair_key(event), {})
        if side not in slot or str(event.get("at") or "") >= str(slot[side].get("at") or ""):
            slot[side] = event
    pairs = []
    totals = {"with": _pair_group(), "without": _pair_group()}
    for key, slot in sides.items():
        if "with" not in slot or "without" not in slot:
            continue
        pair = _pair(key, slot["with"], slot["without"])
        pairs.append(pair)
        if pair["counted"]:
            for name in ("with", "without"):
                for field in totals[name]:
                    totals[name][field] += pair[name][field]
    pairs.sort(key=lambda pair: str(pair["with"].get("at") or ""))
    finished = {name: _finish_pair_group(group) for name, group in totals.items()}
    return {
        "pairs": pairs,
        "pairs_total": sum(1 for pair in pairs if pair["counted"]),
        "pairs_listed": len(pairs),
        "with": finished["with"],
        "without": finished["without"],
        "delta": _pair_delta(finished["with"], finished["without"]),
        "note": PAIRED_NOTE,
    }


def _pair_key(event: Dict[str, Any]) -> Tuple[Any, ...]:
    """The workflow directory and the frozen snapshot -- never the budget epoch."""
    block = event["measurement"]
    return (
        str(block.get("workflow") or ""),
        str(block.get("snapshot") or ""),
        str(block.get("tree") or ""),
        block.get("head"),
        block.get("base"),
    )


def _pair_side(event: Dict[str, Any]) -> Optional[str]:
    mode = event["measurement"].get("surrounding")
    return {"none": "without", "enclosing": "with"}.get(str(mode or ""))


def _panel(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Who reviewed, and in what role: the role changes the prompt a reviewer gets."""
    return [
        {
            "id": run.get("id"),
            "provider": run.get("provider"),
            "model": run.get("model"),
            "role": run.get("role"),
        }
        for run in event.get("reviewers") or []
        if isinstance(run, dict)
    ]


def _undelivered(event: Dict[str, Any], side: str) -> List[Dict[str, Any]]:
    """The runs of one side that did not deliver a review."""
    return [
        {"side": side, "id": run.get("id"), "status": run.get("status")}
        for run in event.get("reviewers") or []
        if isinstance(run, dict) and run.get("status") != "ok"
    ]


def _inputs_differ(with_event: Dict[str, Any], without_event: Dict[str, Any]) -> List[str]:
    """The prompt inputs the two runs did not share, by name.

    A run that recorded none differs in every one the other recorded, and in
    ``inputs`` itself when neither did: nothing says they were equal.
    """
    ours = with_event["measurement"].get("inputs")
    theirs = without_event["measurement"].get("inputs")
    if not isinstance(ours, dict) or not isinstance(theirs, dict):
        return ["inputs"]
    keys = list(ours) + [key for key in theirs if key not in ours]
    return [_INPUT_NAMES.get(key, key) for key in keys if ours.get(key) != theirs.get(key)]


def _pair(key: Tuple[Any, ...], with_event: Dict[str, Any], without_event: Dict[str, Any]) -> Dict[str, Any]:
    panel, without_panel = _panel(with_event), _panel(without_event)

    def members(entries: List[Dict[str, Any]]) -> set:
        return {(entry["id"], entry["provider"], entry["model"], entry["role"]) for entry in entries}

    same_panel = members(panel) == members(without_panel)
    undelivered = _undelivered(with_event, "with") + _undelivered(without_event, "without")
    inputs_differ = _inputs_differ(with_event, without_event)
    nothing_adopted = not _with_context(with_event)
    adopted, trimmed = _round_context_chars(with_event)
    context_chars = 0
    for run in with_event.get("reviewers") or []:
        record = run.get("surrounding") if isinstance(run, dict) else None
        if isinstance(record, dict):
            context_chars = max(context_chars, _int(record.get("context_chars")))
    sides = {"with": _pair_side_figures(with_event), "without": _pair_side_figures(without_event)}
    return {
        "workflow": key[0],
        "epoch": {
            "with": with_event["measurement"].get("epoch"),
            "without": without_event["measurement"].get("epoch"),
        },
        "snapshot": key[1],
        "tree": key[2],
        "same_panel": same_panel,
        "delivered": not undelivered,
        "same_inputs": not inputs_differ,
        "nothing_adopted": nothing_adopted,
        "counted": same_panel and not undelivered and not inputs_differ and not nothing_adopted,
        "panel": panel,
        "without_panel": None if same_panel else without_panel,
        "undelivered": undelivered,
        "inputs_differ": inputs_differ,
        "change_chars": _round_change_chars(with_event),
        "adopted_chars": adopted,
        "context_chars": context_chars,
        "trimmed_chars": trimmed,
        "with": sides["with"],
        "without": sides["without"],
        "delta": _pair_delta(sides["with"], sides["without"]),
    }


def _pair_group() -> Dict[str, int]:
    keys = "reviewer_runs measured_runs billed_tokens tool_reported_runs tool_uses tool_output_chars"
    return dict.fromkeys(keys.split(), 0)


def _finish_pair_group(group: Dict[str, int]) -> Dict[str, Any]:
    finished: Dict[str, Any] = dict(group)
    finished["billed_per_run"] = _per_run(group["billed_tokens"], group["measured_runs"])
    finished["tool_uses_per_run"] = _per_run(group["tool_uses"], group["tool_reported_runs"])
    finished["tool_output_chars_per_run"] = _per_run(group["tool_output_chars"], group["tool_reported_runs"])
    return finished


def _pair_side_figures(event: Dict[str, Any]) -> Dict[str, Any]:
    runs, reported, billed = _reviewer_spend(event)
    said, called, printed = _reviewer_tools(event)
    group = {
        "reviewer_runs": runs,
        "measured_runs": reported,
        "billed_tokens": billed,
        "tool_reported_runs": said,
        "tool_uses": called,
        "tool_output_chars": printed,
    }
    return dict(_finish_pair_group(group), at=event.get("at"))


def _pair_delta(with_side: Dict[str, Any], without_side: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """With minus without, per run; None where either side has nothing to divide."""
    delta: Dict[str, Optional[float]] = {}
    for field in ("billed_per_run", "tool_uses_per_run", "tool_output_chars_per_run"):
        ours, theirs = with_side.get(field), without_side.get(field)
        delta[field] = None if ours is None or theirs is None else round(ours - theirs, 1)
    return delta


def _with_context(event: Dict[str, Any]) -> bool:
    """Whether a round handed any reviewer surrounding context.

    Read off the round's own summary, and off the reviewer entries for a
    round without one. A round that adopted nothing -- the body went over as
    a file, the budget was spent -- showed no context and is not a round
    with it, whatever the setting said.
    """
    block = event.get("surrounding")
    if isinstance(block, dict):
        return _int(block.get("adopted_chars")) > 0
    return any(
        _int((run.get("surrounding") or {}).get("adopted_chars")) > 0
        for run in event.get("reviewers") or []
        if isinstance(run, dict) and isinstance(run.get("surrounding"), dict)
    )


def _round_change_chars(event: Dict[str, Any]) -> Optional[int]:
    """The size of the diff a code round reviewed, or None where unrecorded.

    Every reviewer of a code round is handed the same diff, so the first
    entry that recorded a size answers for the round.
    """
    for run in event.get("reviewers") or []:
        if not isinstance(run, dict):
            continue
        chars = run.get("change_chars")
        if isinstance(chars, int) and not isinstance(chars, bool) and chars > 0:
            return chars
    return None


def _round_context_chars(event: Dict[str, Any]) -> Tuple[int, int]:
    """Adopted and left-out context chars of one round, counted once per round."""
    block = event.get("surrounding")
    if isinstance(block, dict):
        return _int(block.get("adopted_chars")), _int(block.get("trimmed_chars"))
    adopted = trimmed = 0
    for run in event.get("reviewers") or []:
        record = run.get("surrounding") if isinstance(run, dict) else None
        if isinstance(record, dict):
            adopted = max(adopted, _int(record.get("adopted_chars")))
            trimmed = max(trimmed, _int(record.get("trimmed_chars")))
    return adopted, trimmed


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _context_group() -> Dict[str, int]:
    keys = (
        "rounds reviewer_runs measured_runs billed_tokens tool_reported_runs tool_uses "
        "tool_output_chars sized_rounds change_chars sized_billed_tokens billed_run_change_chars "
        "sized_tool_output_chars tool_run_change_chars sized_billed_runs sized_tool_runs "
        "adopted_chars trimmed_chars"
    )
    return dict.fromkeys(keys.split(), 0)


def _add_to_context_group(
    group: Dict[str, int],
    event: Dict[str, Any],
    spend: Tuple[int, int, int],
    tools: Tuple[int, int, int],
) -> None:
    """Add one round to a group, weighting its size by the runs that reported.

    The raw totals move with the size of each change and with the number of
    reviewers in the panel, and a reduced panel is an ordinary thing for a
    small change to get. Dividing by "size times reporting runs" instead
    gives what one run spent per 1k chars of change, which neither moves.
    Only rounds that recorded their size count toward the normalised
    figures, numerator and denominator alike.
    """
    runs, reported, billed = spend
    said, called, printed = tools
    group["rounds"] += 1
    group["reviewer_runs"] += runs
    group["measured_runs"] += reported
    group["billed_tokens"] += billed
    group["tool_reported_runs"] += said
    group["tool_uses"] += called
    group["tool_output_chars"] += printed
    adopted, trimmed = _round_context_chars(event)
    group["adopted_chars"] += adopted
    group["trimmed_chars"] += trimmed
    size = _round_change_chars(event)
    if size is None:
        return
    group["sized_rounds"] += 1
    group["change_chars"] += size
    group["sized_billed_tokens"] += billed
    group["billed_run_change_chars"] += size * reported
    group["sized_billed_runs"] += reported
    group["sized_tool_output_chars"] += printed
    group["tool_run_change_chars"] += size * said
    group["sized_tool_runs"] += said


def _finish_context_group(group: Dict[str, int]) -> Dict[str, Any]:
    finished: Dict[str, Any] = dict(group)
    rounds, billed = group["rounds"], group["billed_tokens"]
    finished["billed_per_round"] = billed // rounds if (rounds and billed) else None
    finished["tool_uses_per_run"] = _per_run(group["tool_uses"], group["tool_reported_runs"])
    finished["tool_output_chars_per_run"] = _per_run(group["tool_output_chars"], group["tool_reported_runs"])
    finished["billed_per_run_per_1k_change_chars"] = _per_1k(
        group["sized_billed_tokens"], group["billed_run_change_chars"]
    )
    finished["tool_output_chars_per_run_per_1k_change_chars"] = _per_1k(
        group["sized_tool_output_chars"], group["tool_run_change_chars"]
    )
    return finished


def _per_1k(total: int, weighted_chars: int) -> Optional[float]:
    """``total`` per 1,000 run-weighted chars of change, or None with nothing to divide by."""
    if not weighted_chars:
        return None
    return round(total / (weighted_chars / 1000.0), 1)


def _per_run(total: int, runs: int) -> Optional[float]:
    """A per-run average, or None when nobody reported one to average."""
    if not runs:
        return None
    return round(total / float(runs), 1)


def _reviewer_tools(event: Dict[str, Any]) -> Tuple[int, int, int]:
    """One round's reported tool activity: runs that said, calls, output chars.

    The first figure is the denominator, and it is not ``reviewer_runs``.
    Codex reports no tool activity at all, so dividing a Claude reviewer's
    tool calls by a mixed panel halves the figure for no reason but the
    panel's composition -- and the comparison this exists for would then move
    whenever a reviewer is added or dropped.

    An ``int`` is a report, zero included: a reviewer that opened nothing is
    the result this measurement is looking for.
    """
    reported = uses = chars = 0
    for run in event.get("reviewers") or []:
        if not isinstance(run, dict):
            continue
        usage = run.get("usage") or {}
        called = usage.get("tool_uses")
        if isinstance(called, bool) or not isinstance(called, int):
            continue
        reported += 1
        uses += called
        output = usage.get("tool_output_chars")
        if isinstance(output, int) and not isinstance(output, bool):
            chars += output
    return reported, uses, chars


def _reviewer_spend(event: Dict[str, Any]) -> Tuple[int, int, int]:
    """One round's reviewer runs, how many reported usage, and what they billed.

    A run that reported nothing still ran, so it is counted and contributes
    nothing to the total -- which is why the two counts are reported side by
    side and the billed figure is a floor.
    """
    runs = reported = billed = 0
    for run in event.get("reviewers") or []:
        if not isinstance(run, dict):
            continue
        runs += 1
        spent = (run.get("usage") or {}).get("billed_tokens")
        if spent:
            reported += 1
            billed += int(spent)
    return runs, reported, billed


def _bump(counter: Dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


#: Below this many decided findings no rate is printed, and below this many
#: accepted ones no per-accepted figure. Under ten, one finding moves a rate by
#: ten points or more, and that is noise, not a measurement. The counts are
#: always printed; only the division is withheld.
SCORECARD_MIN_DECIDED = 10

#: A reviewer run that returned a review, whole or in part. Anything else is a
#: run that was paid for, or at least attempted, and produced nothing to triage.
_REVIEWED = ("ok", "partial")

#: A triage value ``consolidate_findings`` never writes as its default, so a
#: record holding one is a decision even in a report from before decisions
#: were stamped with ``triage_set_at``.
_EXPLICIT_TRIAGE = ("accepted", "rejected", "duplicate", "needs-investigation")

_SCORE_SPEND = ("runs", "failed_runs", "measured_runs", "priced_runs", "billed_tokens", "cost_usd")
_SCORE_FINDINGS = ("reported", "accepted", "rejected", "duplicate", "open")
_SCORE_ROUNDS = ("rounds_recorded", "rounds_read", "rounds_unreviewed", "rerun_rounds")


def reviewer_scorecard(inputs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """What each reviewer found, what the owner kept of it, and what it cost.

    ``inputs`` is one entry per workflow and stage: ``stage`` (``code`` or
    ``design``), ``events`` (that stage's finished events) and ``rounds``
    (``review.recorded_rounds``). Cost and runs come from the events, findings
    and triage from the rounds' reports, and the two are matched round by
    round -- see ``_match_rounds``. A round with events and no report to read
    is a hole, and its cost is left out with its findings: a per-accepted
    figure has to divide what one set of rounds cost by what the same set
    found.

    Findings are counted once per workflow and stage, by ``key``: a finding
    nobody fixed comes back in every later round, carrying its triage, and
    counting it per round would make the most persistent finding the most
    accepted one.
    """
    stages: Dict[str, Dict[str, Any]] = {}
    for stage in ("code", "design"):
        stages[stage] = dict.fromkeys(_SCORE_ROUNDS, 0)
        stages[stage].update(workflows_read=0, reviewers={}, panel=_score_group(), findings=0)
    for item in inputs:
        stage = stages["design" if item.get("stage") == "design" else "code"]
        events = [
            event
            for event in item.get("events") or []
            if isinstance(event, dict) and event.get("status") == "ok"
        ]
        rounds = [entry for entry in item.get("rounds") or [] if isinstance(entry, dict)]
        groups = _match_rounds(events, rounds)
        read = [group for group in groups if group["round"] is not None]
        read.sort(key=lambda group: group["order"])
        counted = [group for group in groups if group["round"] is not None or group["unreviewed"]]
        stage["rounds_recorded"] += len(groups)
        stage["rounds_read"] += len(read)
        stage["rounds_unreviewed"] += sum(1 for group in groups if group["unreviewed"])
        # Among the rounds whose cost is in: the note that goes with it says
        # both runs of the pair were paid for, which is not so of a hole.
        stage["rerun_rounds"] += sum(1 for group in counted if group["rerun"])
        if read:
            stage["workflows_read"] += 1
        for group in counted:
            for event in group["events"]:
                _add_spend(stage, event)
        for finding in _unique_findings([group["round"] for group in read]):
            _score_finding(stage, finding)
    for stage in stages.values():
        stage["findings"] = stage["panel"]["reported"]
        stage["reviewers"] = {name: _finish_score(group, True) for name, group in stage["reviewers"].items()}
        stage["panel"] = _finish_score(stage["panel"], False)
    total = _score_group()
    for field in _SCORE_SPEND + _SCORE_FINDINGS:
        total[field] = stages["code"]["panel"][field] + stages["design"]["panel"][field]
    total = _finish_score(total, False)
    for field in _SCORE_ROUNDS:
        total[field] = stages["code"][field] + stages["design"][field]
    return {"code": stages["code"], "design": stages["design"], "total": total}


def _round_key_of_event(event: Dict[str, Any]) -> Tuple[Any, ...]:
    """The round an event paid for, as far as the event can say.

    ``("sha", sha12, round_id)`` when its reviewer entries agree on one
    snapshot stamp, ``("iteration", n)`` when they carry none or disagree. A
    re-run with ``--only`` and both runs of a ``--surrounding`` pair share the
    key, and are one round.
    """
    runs = [run for run in event.get("reviewers") or [] if isinstance(run, dict)]
    stamps = {str(run.get("snapshot") or "") for run in runs} - {"", "unknown"}
    if len(stamps) == 1:
        return ("sha", stamps.pop(), str(event.get("round_id") or ""))
    return ("iteration", _int(event.get("iteration")))


def _match_rounds(events: Sequence[Dict[str, Any]], rounds: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One entry per round the events paid for, with the report it matched.

    In order of certainty, and no report is matched twice:

    1. an event with a round id matches the report of exactly that round;
    2. one without (recorded before events carried it) matches the report of
       its sha, only when there is exactly one. Two reports of one sha are two
       rounds of the same content, and taking the newer would pin the old
       round's cost on the new round's findings -- so it is a hole instead;
    3. one with no snapshot stamp at all matches the live report, when their
       iterations agree and nothing more certain has taken it.

    A round where no reviewer returned a review is not a hole, and is not read
    even when a report matched it: whatever that report holds, no reviewer of
    the round put it there. Its runs and cost still count.

    ``order`` is the position of the round's latest event. Events arrive in
    the order they were written, which a report's ``generated_at`` -- to the
    second -- cannot always tell.
    """
    # Here rather than at the top: the review module imports this one.
    from .review import round_key

    groups: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    order: Dict[Tuple[Any, ...], int] = {}
    for position, event in enumerate(events):
        key = _round_key_of_event(event)
        groups.setdefault(key, []).append(event)
        order[key] = position
    keys = [round_key(entry) for entry in rounds]
    live = next((index for index, entry in enumerate(rounds) if entry.get("live")), None)
    matched: Dict[Tuple[Any, ...], int] = {}
    taken: set = set()

    def take(key: Tuple[Any, ...], index: int) -> None:
        matched[key] = index
        taken.add(index)

    for key in groups:
        if key[0] == "sha" and key[2]:
            for index, found in enumerate(keys):
                if found == (key[1], key[2]) and index not in taken:
                    take(key, index)
                    break
    for key in groups:
        if key[0] == "sha" and not key[2]:
            candidates = [index for index, found in enumerate(keys) if found[0] == key[1]]
            if len(candidates) == 1 and candidates[0] not in taken:
                take(key, candidates[0])
    for key in groups:
        if key[0] == "iteration" and live is not None and live not in taken:
            if _int(rounds[live].get("iteration")) == key[1]:
                take(key, live)

    result = []
    for key, members in groups.items():
        reviewed = any(
            isinstance(run, dict) and run.get("status") in _REVIEWED
            for event in members
            for run in event.get("reviewers") or []
        )
        found = rounds[matched[key]] if key in matched and reviewed else None
        result.append(
            {
                "key": key,
                "events": members,
                "order": order[key],
                "round": found,
                "unreviewed": not reviewed,
                "rerun": any(
                    isinstance(event.get("measurement"), dict) and event["measurement"].get("rerun") is True
                    for event in members
                ),
            }
        )
    return result


def _explicit_triage(finding: Dict[str, Any]) -> bool:
    """Whether a finding's triage is a decision rather than a rebuilt default.

    ``triage_set_at`` is written by every ``review triage``, so a finding put
    back to ``needs-triage`` by its owner is a decision too: it withdraws an
    earlier acceptance. Without the stamp, only a value the rebuild never
    writes says so.
    """
    return bool(finding.get("triage_set_at")) or finding.get("triage") in _EXPLICIT_TRIAGE


def _unique_findings(rounds: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One record per finding ``key`` over one workflow's rounds of one stage.

    ``rounds`` in the order they ran, oldest first. The last *explicit* triage
    wins: a finding the carry missed comes back as ``needs-triage`` by
    default, and that default must not undo an acceptance made in the round
    before. A candidate duplicate link is resolved inside the round that made
    it, since ids are positions renumbered every round.
    """
    unique: Dict[str, Dict[str, Any]] = {}
    for position, report in enumerate(rounds):
        findings = [finding for finding in report.get("findings") or [] if isinstance(finding, dict)]
        by_id = {str(finding.get("id") or ""): finding for finding in findings}
        for finding in findings:
            key = str(finding.get("key") or "") or "%d:%s" % (position, finding.get("id"))
            record = unique.setdefault(key, {"reported_by": [], "triage": None, "paired": False})
            ours = [str(name) for name in finding.get("reported_by") or []]
            for name in ours:
                if name not in record["reported_by"]:
                    record["reported_by"].append(name)
            if _explicit_triage(finding):
                record["triage"] = finding.get("triage")
            if _alone_pairs(finding, by_id, ours):
                record["paired"] = True
    return list(unique.values())


def _alone_pairs(finding: Dict[str, Any], by_id: Dict[str, Dict[str, Any]], ours: List[str]) -> bool:
    """Whether this round linked the finding to another reviewer's as a duplicate.

    Either side triaged ``duplicate`` will do: a finding judged a copy of
    someone else's was not found alone, and neither was the one it copied.
    A link between two findings of one reviewer says nothing about the panel.
    """
    for other_id in finding.get("possible_duplicates") or []:
        other = by_id.get(str(other_id))
        if not other:
            continue
        theirs = [str(name) for name in other.get("reported_by") or []]
        if not any(name not in ours for name in theirs):
            continue
        if "duplicate" in (finding.get("triage"), other.get("triage")):
            return True
    return False


def _score_group() -> Dict[str, Any]:
    group: Dict[str, Any] = dict.fromkeys(_SCORE_SPEND + _SCORE_FINDINGS, 0)
    group["cost_usd"] = 0.0
    return group


def _score_reviewer(stage: Dict[str, Any], name: str) -> Dict[str, Any]:
    group = stage["reviewers"].get(name)
    if group is None:
        group = stage["reviewers"][name] = _score_group()
        group.update(alone=0, alone_accepted=0)
    return group


def _add_spend(stage: Dict[str, Any], event: Dict[str, Any]) -> None:
    """One event's runs, added to each reviewer and to the panel.

    A run that reported nothing still ran, and adds nothing to the totals --
    which is why they are floors.
    """
    for run in event.get("reviewers") or []:
        if not isinstance(run, dict):
            continue
        usage = run.get("usage") if isinstance(run.get("usage"), dict) else {}
        billed = _int(usage.get("billed_tokens"))
        cost = usage.get("cost_usd")
        priced = isinstance(cost, (int, float)) and not isinstance(cost, bool)
        for group in (_score_reviewer(stage, str(run.get("id") or "?")), stage["panel"]):
            group["runs"] += 1
            if run.get("status") not in _REVIEWED:
                group["failed_runs"] += 1
            if usage.get("measured") is True or billed > 0:
                group["measured_runs"] += 1
            group["billed_tokens"] += billed
            if priced:
                group["priced_runs"] += 1
                group["cost_usd"] += float(cost)


def _final_triage(record: Dict[str, Any]) -> str:
    """``accepted``, ``rejected``, ``duplicate``, or ``open`` for anything undecided."""
    triage = record.get("triage")
    return triage if triage in ("accepted", "rejected", "duplicate") else "open"


def _score_finding(stage: Dict[str, Any], record: Dict[str, Any]) -> None:
    """One finding, once to every reviewer who reported it and once to the panel."""
    outcome = _final_triage(record)
    reporters = record["reported_by"]
    # Found alone only when one reviewer reported it and no round linked it to
    # another reviewer's as a duplicate. A duplicate nobody linked is still
    # counted here, which is what makes this an upper bound.
    alone = len(reporters) == 1 and not record["paired"]
    for name in reporters:
        group = _score_reviewer(stage, name)
        group["reported"] += 1
        group[outcome] += 1
        if alone:
            group["alone"] += 1
            if outcome == "accepted":
                group["alone_accepted"] += 1
    stage["panel"]["reported"] += 1
    stage["panel"][outcome] += 1


def _finish_score(group: Dict[str, Any], alone: bool) -> Dict[str, Any]:
    """The counts, plus the rates they support -- None where they support none.

    ``cost_per_accepted`` is None as well where no run was priced: a panel of
    reviewers that report no price has not found findings for free.
    """
    finished: Dict[str, Any] = {field: group[field] for field in _SCORE_SPEND + _SCORE_FINDINGS}
    finished["cost_usd"] = round(float(group["cost_usd"]), 4)
    if alone:
        finished["alone"] = group.get("alone", 0)
        finished["alone_accepted"] = group.get("alone_accepted", 0)
    decided = group["accepted"] + group["rejected"] + group["duplicate"]
    accepted = group["accepted"]
    finished["rejection_rate"] = (
        round(group["rejected"] / float(decided), 3) if decided >= SCORECARD_MIN_DECIDED else None
    )
    enough = accepted >= SCORECARD_MIN_DECIDED
    finished["billed_per_accepted"] = group["billed_tokens"] // accepted if enough else None
    finished["cost_per_accepted"] = (
        round(group["cost_usd"] / accepted, 4) if enough and group["priced_runs"] else None
    )
    return finished


def choose_reviewers(reviewers: Sequence[Dict[str, Any]], limit: Optional[int]) -> List[Dict[str, Any]]:
    """Which reviewers survive a reduced panel.

    A ``general`` reviewer first, then configuration order. Taking the first
    configured one outright would leave a panel of exactly one specialist --
    a security reviewer alone reports no correctness bugs, because it was
    told not to look for them.
    """
    chosen = list(reviewers)
    if limit is None or limit >= len(chosen):
        return chosen
    ranked = sorted(
        range(len(chosen)),
        key=lambda index: (0 if str(chosen[index].get("role") or "") == "general" else 1, index),
    )
    keep = sorted(ranked[:limit])
    return [chosen[index] for index in keep]
