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

The same judgement decides who sits on a code review panel. A reviewer
configured ``when: high-risk`` joins a round only when that round matched a
high-risk path, was declared high-risk with ``review run --high-risk``, or is
re-checking an accepted finding the reviewer itself reported. That is a
membership lever and nothing more: a declaration is a claim, not evidence, so
it never moves the level or the gate, and it is not an escalation. A panel
must keep one reviewer that always runs, or a quiet round would have nobody.

A reviewer configured ``when: {paths: [...]}`` is judged by its own patterns
instead: it joins a round when a changed path matches one of them, when it is
re-checking an accepted finding it reported, or when ``--only`` names it, and
nothing else adds it -- not a high-risk hit, not a declaration. Its patterns
say what the reviewer knows about, not how dangerous the change is, so a match
never moves the level or the gate either.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .workspace import redact

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
#: names differ, or add to it with ``extra_high_risk_paths``; that is the part
#: that is configurable.
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

#: When a reviewer runs on a code review round. ``always`` is the default and
#: what a reviewer with no ``when`` means; ``high-risk`` joins only the rounds
#: ``condition_reviewers`` says qualify. ``paths`` is not a string value but
#: the kind of a ``when`` mapping holding the reviewer's own patterns, so it
#: is not in ``REVIEWER_CONDITIONS``. The design review ignores all three.
WHEN_ALWAYS = "always"
WHEN_HIGH_RISK = "high-risk"
WHEN_PATHS = "paths"
REVIEWER_CONDITIONS = (WHEN_ALWAYS, WHEN_HIGH_RISK)


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


def is_paths_condition(value: Any) -> bool:
    """Whether ``value`` is a ``when`` mapping that validation accepts.

    The key ``paths`` alone, holding a non-empty list whose every entry is a
    string that is not blank. ``config.validate`` refuses exactly the
    mappings this rejects, and ``reviewer_condition`` reads exactly those as
    ``always``, so the two cannot drift apart.
    """
    if not isinstance(value, dict) or set(value) != {"paths"}:
        return False
    patterns = value.get("paths")
    return (
        isinstance(patterns, list)
        and bool(patterns)
        and all(isinstance(pattern, str) and pattern.strip() for pattern in patterns)
    )


def reviewer_condition(reviewer: Any) -> str:
    """A reviewer's ``when``, normalised; ``always`` when unset or unrecognised.

    Validation reports an unknown value at ``config validate``, and
    ``review run`` refuses a config with one, so reading it as ``always``
    here only ever affects a caller that loaded without validation. A
    malformed ``when`` mapping is read the same way: a reviewer that always
    runs is the careful answer, one that never can is a broken one.
    """
    value = reviewer.get("when") if isinstance(reviewer, dict) else None
    if isinstance(value, str) and value.strip().lower() in REVIEWER_CONDITIONS:
        return value.strip().lower()
    if is_paths_condition(value):
        return WHEN_PATHS
    return WHEN_ALWAYS


def reviewer_paths(reviewer: Any) -> List[str]:
    """A path-scoped reviewer's own patterns, stripped; ``[]`` for any other."""
    if reviewer_condition(reviewer) != WHEN_PATHS:
        return []
    return [pattern.strip() for pattern in reviewer["when"]["paths"]]


def condition_label(reviewer: Any) -> str:
    """A reviewer's condition for display: ``paths`` followed by its patterns."""
    when = reviewer_condition(reviewer)
    if when == WHEN_PATHS:
        return redact("%s %s" % (WHEN_PATHS, ", ".join(reviewer_paths(reviewer))))
    return when


def risk_patterns(settings: Dict[str, Any]) -> List[str]:
    """Every high-risk path pattern in force.

    ``high_risk_paths`` when configured as a list, the defaults otherwise,
    plus ``extra_high_risk_paths`` -- so a repository can add the one path
    the defaults miss without copying the thirty they already cover.
    """
    patterns = settings.get("high_risk_paths")
    if not isinstance(patterns, (list, tuple)):
        patterns = DEFAULT_HIGH_RISK_PATHS
    extra = settings.get("extra_high_risk_paths")
    if not isinstance(extra, (list, tuple)):
        extra = ()
    combined = list(patterns) + list(extra)
    return [pattern for pattern in combined if isinstance(pattern, str) and pattern.strip()]


def default_patterns_only(settings: Dict[str, Any]) -> bool:
    """Whether the patterns in force come from the built-in defaults alone.

    ``high_risk_paths`` unset, or a list drawn only from the defaults in any
    order, and no ``extra_high_risk_paths``. A subset counts because configs
    written before 0.6.0 copied the whole default config into the file, so
    they hold an older default list nobody chose. A pattern outside the
    defaults means someone chose the patterns, so it is not this case; nor is
    a list with no usable pattern, which validation already reports. Entries
    are read the way ``risk_patterns`` reads them, so a blank or non-string
    one changes nothing.
    """
    patterns = settings.get("high_risk_paths")
    if isinstance(patterns, (list, tuple)):
        chosen = {p for p in patterns if isinstance(p, str) and p.strip()}
        if not chosen or not chosen <= set(DEFAULT_HIGH_RISK_PATHS):
            return False
    extra = settings.get("extra_high_risk_paths")
    if not isinstance(extra, (list, tuple)):
        return True
    return not [pattern for pattern in extra if isinstance(pattern, str) and pattern.strip()]


def _and_more(more: int) -> str:
    return " (and %d more)" % more if more > 0 else ""


def _finding_order(finding: Dict[str, Any]) -> Tuple[int, str]:
    """``F3`` before ``F12``: ids are positions, so order them as numbers."""
    name = str(finding.get("id") or "")
    digits = "".join(ch for ch in name if ch.isdigit())
    return (int(digits) if digits else 0, name)


def condition_reviewers(
    reviewers: Sequence[Dict[str, Any]],
    hits: Sequence[Tuple[str, str]],
    declared: bool = False,
    carried: Sequence[Dict[str, Any]] = (),
    only: bool = False,
    paths: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """One record per conditional reviewer: whether it runs this round, and why.

    Reasons are tried in order and the first that applies is kept: a
    high-risk path matched, the round was declared with ``--high-risk``, the
    round carries an accepted finding this reviewer reported (``carried``,
    from ``review.carried_findings``), or ``--only`` named it. An
    unconditional reviewer gets no record: it runs whatever the round is.

    A path-scoped reviewer is judged by its own patterns against ``paths``
    instead, and ``hits`` and ``declared`` never add it; when either is set
    and it is left out, the reason says so and names ``--only`` as the way
    in. Every reason is redacted here, because it carries filenames and
    patterns to notes, JSON and the event log alike.
    """
    records: List[Dict[str, Any]] = []
    for reviewer in reviewers:
        if not isinstance(reviewer, dict):
            continue
        when = reviewer_condition(reviewer)
        if when == WHEN_ALWAYS:
            continue
        name = str(reviewer.get("id") or "")
        own = [
            finding
            for finding in carried
            if isinstance(finding, dict) and name in [str(r) for r in finding.get("reported_by") or []]
        ]
        own.sort(key=_finding_order)
        matched = hits if when == WHEN_HIGH_RISK else high_risk_matches(paths, reviewer_paths(reviewer))
        if matched:
            reason = "%s matches %s%s" % (matched[0][0], matched[0][1], _and_more(len(matched) - 1))
        elif declared and when == WHEN_HIGH_RISK:
            reason = "declared with --high-risk"
        elif own:
            reason = "has open accepted finding %s%s" % (own[0].get("id"), _and_more(len(own) - 1))
        elif only:
            reason = "named by --only"
        else:
            reason = redact(_left_out(reviewer, hits, declared))
            records.append({"id": name, "when": when, "runs": False, "reason": reason})
            continue
        records.append({"id": name, "when": when, "runs": True, "reason": redact(reason)})
    return records


def _left_out(reviewer: Dict[str, Any], hits: Sequence[Tuple[str, str]], declared: bool) -> str:
    """Why a conditional reviewer sits this round out.

    A path-scoped one names its own patterns, so a change someone thought
    relevant shows at once why it did not qualify. On a round that would
    have added a high-risk reviewer, it also says that this one ignores
    that, and how to bring it in anyway. The hits win over a declaration:
    they are the evidence, and a declaration is only a claim.
    """
    if reviewer_condition(reviewer) != WHEN_PATHS:
        return "no high-risk path matched"
    reason = "no path matches %s" % ", ".join(reviewer_paths(reviewer))
    if hits:
        reason += " (round is high-risk; when: paths ignores that; --only <ids> to include it)"
    elif declared:
        reason += " (declared with --high-risk; when: paths ignores that; --only <ids> to include it)"
    return reason


def qualifies(reviewer: Dict[str, Any], records: Sequence[Dict[str, Any]]) -> bool:
    """Whether a reviewer runs this round, by its ``condition_reviewers`` record."""
    if reviewer_condition(reviewer) == WHEN_ALWAYS:
        return True
    name = str(reviewer.get("id") or "")
    return any(record.get("id") == name and record.get("runs") for record in records)


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
        conditional: Sequence[Dict[str, Any]] = (),
        declared: bool = False,
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
        #: One ``condition_reviewers`` record per conditional reviewer.
        self.conditional = [dict(record) for record in conditional]
        #: Whether the round was declared high-risk with ``--high-risk``.
        self.declared = declared

    @property
    def escalated(self) -> bool:
        return self.level != self.requested

    def escalation_note(self) -> str:
        if not self.escalated:
            return ""
        first = self.high_risk[0] if self.high_risk else ("", "")
        more = len(self.high_risk) - 1
        # Redacted like every reason: it carries a filename and a pattern.
        return redact(
            "%s → %s: %s matches %s%s"
            % (
                self.requested,
                self.level,
                first[0],
                first[1],
                " (and %d more)" % more if more > 0 else "",
            )
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

    def conditional_notes(self) -> List[str]:
        """One line per conditional reviewer, saying whether it runs and why."""
        notes: List[str] = []
        for record in self.conditional:
            verdict = "added" if record["runs"] else "left out"
            notes.append("%s (when: %s) %s: %s" % (record["id"], record["when"], verdict, record["reason"]))
        return notes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_level": self.requested,
            "level": self.level,
            "escalated": self.escalated,
            "high_risk": [{"path": redact(p), "pattern": redact(q)} for p, q in self.high_risk],
            "gate": self.gate,
            "test_status": self.test_status,
            "max_findings": self.max_findings,
            "reviewer_limit": self.reviewer_limit,
            "files": self.files,
            "lines": self.lines,
            "conditional": [dict(record) for record in self.conditional],
            "declared": self.declared,
        }


def decide(
    settings: Dict[str, Any],
    review_settings: Dict[str, Any],
    paths: Sequence[str],
    lines: int,
    test_status: str,
    reviewers: int,
    reviewed_files: Optional[int] = None,
    *,
    panel: Optional[Sequence[Dict[str, Any]]] = None,
    declared: bool = False,
    carried: Sequence[Dict[str, Any]] = (),
    only: bool = False,
    condition_paths: Optional[Sequence[str]] = None,
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

    ``panel`` is the reviewers themselves, for a caller that has them. It
    decides each conditional reviewer (see ``condition_reviewers``), and
    ``reviewers`` is then the number that would run rather than the count
    passed in. ``declared`` is ``review run --high-risk``: it adds the
    conditional reviewers and keeps the panel whole, and touches nothing
    else -- not the level, the gate, the findings cap or ``high_risk``.

    ``condition_paths`` is what a path-scoped reviewer's patterns are matched
    against: the change as a reviewer sees it, without the files that are in
    neither the diff nor the withheld notice. It defaults to ``paths``. It
    decides membership only; risk is still judged from ``paths``.
    """
    requested = normalise_level(settings.get("level"))
    hits = high_risk_matches(paths, risk_patterns(settings))
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
    conditional: List[Dict[str, Any]] = []
    if panel is not None:
        conditional = condition_reviewers(
            panel,
            hits,
            declared=declared,
            carried=carried,
            only=only,
            paths=paths if condition_paths is None else condition_paths,
        )
        members = [reviewer for reviewer in panel if isinstance(reviewer, dict)]
        reviewers = sum(1 for reviewer in members if qualifies(reviewer, conditional))
    # A conditional reviewer that qualified keeps the panel whole. A high-risk
    # path hit already does, through `quality`; a declaration, a carried
    # finding or a reviewer's own path does not move the level, and the cut
    # would drop the very reviewer that just qualified.
    keep_whole = declared or any(record["runs"] for record in conditional)
    limit = None
    # Any level short of `quality`, which is the level that means "spend what
    # it takes". Restricting this to `aggressive` made it unreachable in the
    # repositories that most need it: a high-risk hit escalates to `quality`,
    # and `quality` is not `aggressive`, so in an infrastructure repository
    # where `*.tf` matches on most rounds the dial could not fire at all.
    # `hits` is still what stops it -- a small change to an auth file gets the
    # full panel -- but a small change to nothing risky no longer pays for two
    # independent reviewers to agree it is small.
    if level != "quality" and reviewers > 1 and not keep_whole:
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
        conditional=conditional,
        declared=bool(declared),
    )


#: Under ``review.design.enabled: auto``, a plan whose Files to Modify names
#: this many code files or more gets a design round. Drawn from six measured
#: plans: the one low-risk plan counted 5, every other 7 or more.
DESIGN_LARGE_PLAN_FILES = 6

#: Files to Modify entries under these, or with a ``.md`` basename, are docs
#: or tests and do not count toward the size of a plan.
DESIGN_DOCS_PREFIXES = ("docs/", "references/")
DESIGN_TESTS_PREFIXES = ("tests/",)

_GLOB_CHARS = ("*", "?", "[", "{", "}")
#: Underscores included, so a config key such as ``workspace.stale_notice_days``
#: counts like the file names beside it; that over-count only moves toward run.
_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9_]+$")


class DesignDecision:
    """Whether the design review runs for this plan, and why."""

    def __init__(
        self,
        mode: str,
        run: bool,
        reason: str = "",
        files: int = 0,
        high_risk: Sequence[Tuple[str, str]] = (),
    ) -> None:
        #: "on", "off" or "auto".
        self.mode = mode
        self.run = run
        #: Empty under on and off. Redacted: it carries file names.
        self.reason = redact(reason) if reason else ""
        #: Code files counted in Files to Modify, when the size was judged.
        self.files = files
        #: ``(token, pattern)`` for every token that matched a high-risk pattern.
        self.high_risk = list(high_risk)

    def label(self) -> str:
        if self.mode != "auto":
            return self.mode
        return "auto -> %s (%s)" % ("run" if self.run else "skip", self.reason)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "run": self.run,
            "reason": self.reason or None,
            "files": self.files,
            "high_risk": [{"path": redact(p), "pattern": redact(q)} for p, q in self.high_risk],
        }


def _plan_path(token: str) -> str:
    path = token.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _has_extension(path: str) -> bool:
    return bool(_EXTENSION_RE.search(posixpath.basename(path.rstrip("/"))))


def _risk_candidates(token: str) -> List[str]:
    """What a plan token is matched as against the high-risk patterns.

    Lower-cased, since the plan's spelling of ``src/Auth.py`` says nothing
    about the risk. ``normpath`` would drop the trailing slash the directory
    patterns need, so a directory gets a child; so does a name with a ``/``
    and no extension, which may be one (``db/migrate``).
    """
    path = _plan_path(token).lower()
    if path.endswith("/"):
        return [path + "_"]
    if "/" in path and not _has_extension(path):
        return [path, path + "/_"]
    return [path]


def decide_design(
    mode: str,
    settings: Dict[str, Any],
    scan: Any,
    *,
    plan_state: str,
    round_ran: bool,
) -> DesignDecision:
    """Whether the design review runs, from the plan the architect wrote.

    ``scan`` is ``review.plan_tokens()`` of the plan when ``plan_state`` is
    ``"ok"``; ``"missing"`` and ``"unreadable"`` come with none. Under
    ``auto`` every doubt answers run: once a round has run, so a revision
    cannot switch the loop off half way; for a plan not yet written or not
    readable; for any token matching a high-risk pattern, in any case; and
    for a Files to Modify section that is absent, names no file, names a
    glob, a directory or a path through ``..``, or names
    ``DESIGN_LARGE_PLAN_FILES`` code files or more. Only file-shaped tokens
    (a ``/`` or an extension) are judged as globs or directories, so
    ``payload["mode"]`` there is code, not a pattern. Nothing here reads the
    disk.
    """
    if mode != "auto":
        return DesignDecision(mode, mode == "on")
    if round_ran:
        return DesignDecision(mode, True, "a design round already ran")
    if plan_state == "missing":
        # Run once there is a plan to review: with no design stage there is none.
        return DesignDecision(mode, True, "once a plan is written")
    if plan_state != "ok" or scan is None:
        return DesignDecision(mode, True, "plan could not be read")

    folded: Dict[str, str] = {}
    for pattern in risk_patterns(settings):
        folded.setdefault(pattern.lower(), pattern)
    seen = set()
    hits: List[Tuple[str, str, str]] = []
    for item in scan.tokens:
        if item.token in seen:
            continue
        seen.add(item.token)
        matched = high_risk_matches(_risk_candidates(item.token), list(folded))
        if matched:
            hits.append((item.token, item.section, folded[matched[0][1]]))
    if hits:
        token, section, _ = hits[0]
        where = " (%s)" % section if section else ""
        more = " and %d more" % (len(hits) - 1) if len(hits) > 1 else ""
        return DesignDecision(
            mode, True, "touches %s%s%s" % (token, where, more), high_risk=[(t, p) for t, _, p in hits]
        )

    if not scan.has_files_section:
        return DesignDecision(mode, True, "no Files to Modify section")
    listed = [item for item in scan.tokens if item.in_files]
    shaped: List[str] = []
    for item in listed:
        path = _plan_path(item.token)
        extension = _has_extension(path)
        if "/" not in path and not extension:
            continue
        if any(ch in path for ch in _GLOB_CHARS):
            return DesignDecision(mode, True, "names a glob: %s" % item.token)
        if path.endswith("/"):
            return DesignDecision(mode, True, "names a directory: %s" % item.token)
        if ".." in path.split("/"):
            return DesignDecision(mode, True, "names a path through ..: %s" % item.token)
        if not extension:
            if item.prose:
                continue
            return DesignDecision(mode, True, "may name a directory: %s" % item.token)
        if path not in shaped:
            shaped.append(path)
    if not shaped:
        return DesignDecision(mode, True, "Files to Modify names no file")
    code = [
        path
        for path in shaped
        if not path.startswith(DESIGN_DOCS_PREFIXES + DESIGN_TESTS_PREFIXES)
        and not posixpath.basename(path).lower().endswith(".md")
    ]
    count = len(code)
    if count >= DESIGN_LARGE_PLAN_FILES:
        return DesignDecision(mode, True, "%d code files" % count, files=count)
    noun = "code file" if count == 1 else "code files"
    return DesignDecision(mode, False, "%d %s, none high-risk" % (count, noun), files=count)


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
    by_level = MAX_FINDINGS_BY_LEVEL[normalise_level(level if level is not None else settings.get("level"))]
    return _positive(review_settings.get("max_findings"), by_level)


def _positive(value: Any, fallback: int) -> int:
    """``value`` when it is a non-negative int (a bool is not one), else ``fallback``."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


#: The status a refused round is recorded under. It is not a failure: nothing
#: was attempted. It is the one outcome that has to be written down even
#: though nothing ran, because a saving that leaves no trace cannot be counted
#: and a feature whose effect cannot be counted gets argued about instead.
REFUSED = "refused"


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
