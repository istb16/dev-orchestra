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

A ``security``, ``test`` or ``architecture`` reviewer that always runs is
asked one more question, in both stages and at every level: whether this round
has anything for it (``relevance_records``). Only evidence of absence leaves
one out -- no security-relevant path, a docs-only change, one small module
with no contract path -- and a high-risk hit, a declaration, a carried finding
or ``--only`` keeps it before the question is asked. Doubt about the evidence
means run.
"""

from __future__ import annotations

import copy
import fnmatch
import posixpath
import re
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

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
#: is not in ``REVIEWER_CONDITIONS``. A configured design panel honours
#: ``high-risk`` against the plan and may not use ``paths``; a design round
#: that falls back to the code panel ignores ``when`` altogether.
WHEN_ALWAYS = "always"
WHEN_HIGH_RISK = "high-risk"
WHEN_PATHS = "paths"
REVIEWER_CONDITIONS = (WHEN_ALWAYS, WHEN_HIGH_RISK)

#: The kind of a conditional record written by ``relevance_records``. Not a
#: ``when`` value anyone writes: it is how a round says it judged a seat.
WHEN_RELEVANCE = "relevance"

#: The rules a seat's ``relevance`` may name, one per built-in role they are
#: for; ``always`` keeps the seat out of the judgement.
RELEVANCE_RULES = ("security", "test", "architecture")
RELEVANCE_ALWAYS = "always"
#: The roles whose own rule is never applied unless the seat names it in
#: ``relevance``: the security rule judges by path names alone.
OPT_IN_RELEVANCE = ("security",)

#: The model slot a run took its model from when the round was high-risk and
#: the seat has a ``high_risk_model``. A run record without one is ``usual``.
HIGH_RISK_SLOT = "high-risk"
USUAL_SLOT = "usual"

#: Paths a security reviewer has something to read in. Deliberately broad:
#: the rule only ever leaves the seat out when nothing changed matches, and a
#: miss there is a missed finding rather than a saving. Same two-form
#: directory convention as ``DEFAULT_HIGH_RISK_PATHS``, every one of which is
#: included, so replacing ``high_risk_paths`` cannot drop security coverage.
DEFAULT_SECURITY_PATHS = (
    # Request handling and input.
    "*api*",
    "*route*",
    "*router*",
    "*controller*",
    "*handler*",
    "*endpoint*",
    "*middleware*",
    "*http*",
    "*request*",
    "*cookie*",
    "*cors*",
    "*header*",
    "*token*",
    "*jwt*",
    "*oauth*",
    "*upload*",
    "*download*",
    "*input*",
    "*valid*",
    "*sanitiz*",
    "*escape*",
    "*template*",
    # Files, network and data access.
    "*file*",
    "*path*",
    "*url*",
    "*fetch*",
    "*client*",
    "*query*",
    "*sql*",
    "*db*",
    # Execution and deserialisation.
    "*exec*",
    "*shell*",
    "*subprocess*",
    "*command*",
    "*serial*",
    "*pickle*",
    "*yaml*",
    "*xml*",
    "*eval*",
    # Configuration and supply chain.
    "*config*",
    "*setting*",
    "*.env*",
    "*.ini",
    "*.toml",
    "*.cfg",
    "requirements*.txt",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile*",
    "*.lock",
    "composer.json",
    "*.csproj",
    "*.gradle",
    "pom.xml",
    *DEFAULT_HIGH_RISK_PATHS,
)

#: Paths that are a contract, a module surface, a configuration or record
#: format, the CLI, or the build: what an architecture reviewer reads for.
#: Conservative too: in a repository whose names match these on most changes,
#: the seat sits out only single-module changes that touch none of them.
DEFAULT_ARCHITECTURE_PATHS = (
    # Contracts and schemas.
    "*schema*",
    "*.proto",
    "*openapi*",
    "*swagger*",
    "*.graphql",
    "*api*",
    "*interface*",
    "*contract*",
    "*types*",
    "*migration*/*",
    "*/migration*/*",
    "*.sql",
    # Module surface.
    "__init__.py",
    "index.ts",
    "index.js",
    "mod.rs",
    "lib.rs",
    "*public*",
    "*export*",
    # Configuration and formats.
    "*config*",
    "*setting*",
    "*format*",
    "*record*",
    "*state*",
    "*ledger*",
    "*.json",
    "*.yaml",
    "*.yml",
    "*.toml",
    # CLI surface.
    "*cli*",
    "*command*",
    "*arg*",
    "*option*",
    # Build.
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "package.json",
    "Makefile",
    "*.gradle",
    "CMakeLists.txt",
    "Dockerfile*",
)

#: A reviewed file with one of these basename suffixes is documentation, as is
#: one under ``DESIGN_DOCS_PREFIXES``.
DOC_SUFFIXES = (".md", ".rst", ".txt")


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
    return _patterns(settings, "high_risk_paths", DEFAULT_HIGH_RISK_PATHS)


def security_patterns(settings: Dict[str, Any]) -> List[str]:
    """``security_paths`` or the defaults, plus ``extra_security_paths``."""
    return _patterns(settings, "security_paths", DEFAULT_SECURITY_PATHS)


def architecture_patterns(settings: Dict[str, Any]) -> List[str]:
    """``architecture_paths`` or the defaults, plus ``extra_architecture_paths``."""
    return _patterns(settings, "architecture_paths", DEFAULT_ARCHITECTURE_PATHS)


def _patterns(settings: Dict[str, Any], key: str, defaults: Sequence[str]) -> List[str]:
    """``settings[key]`` when a list, else ``defaults``; plus ``extra_<key>``; usable entries only."""
    patterns = settings.get(key)
    if not isinstance(patterns, (list, tuple)):
        patterns = defaults
    extra = settings.get("extra_" + key)
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
    """Whether a reviewer runs this round, by its conditional records.

    A conditional reviewer runs only on a record that says so. One that
    always runs does unless a record -- ``relevance_records`` writes the only
    kind it can have -- says it sits this round out.
    """
    name = str(reviewer.get("id") or "")
    mine = [record for record in records if record.get("id") == name]
    if reviewer_condition(reviewer) == WHEN_ALWAYS:
        return not mine or any(record.get("runs") for record in mine)
    return any(record.get("runs") for record in mine)


def risk_model(reviewer: Dict[str, Any], high_risk: bool) -> Tuple[Dict[str, Any], bool]:
    """``(reviewer, switched)``: the seat on its ``high_risk_model`` on a high-risk round.

    A deep copy with ``model`` replaced, when the round is high-risk and the
    seat has one; the reviewer itself, unchanged, otherwise. Only the model
    moves: the provider and ``options`` stay, so the read-only and refusal
    rules that hold for the seat hold for the switched run too.
    """
    model = reviewer.get("high_risk_model") if isinstance(reviewer, dict) else None
    if not high_risk or not isinstance(model, dict):
        return reviewer, False
    switched = copy.deepcopy(reviewer)
    switched["model"] = copy.deepcopy(model)
    return switched, True


def model_label(model: Any) -> str:
    """A model block the way a note names it: its pinned id, else its family."""
    if not isinstance(model, dict):
        return "default"
    if model.get("version") == "pinned" and model.get("id"):
        return str(model["id"])
    return str(model.get("family") or "default")


def risk_model_note(reviewer: Dict[str, Any], switched: Dict[str, Any], why: str) -> str:
    """``high-risk round (<why>): <id> runs <model> instead of <model>``, redacted."""
    return redact(
        "high-risk round (%s): %s runs %s instead of %s"
        % (why, reviewer.get("id"), model_label(switched.get("model")), model_label(reviewer.get("model")))
    )


# --------------------------------------------------------------------------- role relevance


class Evidence(NamedTuple):
    """What a stage knows about its change, for ``relevance_records`` to judge from."""

    #: What the security patterns are matched against: a code round's changed
    #: paths, withheld ones included; every token of a plan.
    touched: Sequence[str]
    #: What size and directories are read from: a code round's reviewed files;
    #: a plan's code files in Files to Modify.
    files: Sequence[str]
    #: What the architecture patterns are matched against: a code round's
    #: changed paths; every file-shaped entry in a plan's Files to Modify.
    named: Sequence[str]
    #: A plan's Files to Modify entries under ``DESIGN_TESTS_PREFIXES``.
    tests: Sequence[str] = ()
    #: Why the evidence cannot be judged at all, or "". Doubt means run.
    doubt: str = ""


def relevance_rule(reviewer: Any) -> Optional[str]:
    """The rule a seat is judged by, or None for one that is never judged.

    A ``general`` seat is never judged. Otherwise ``relevance`` names the
    rule, or ``always`` keeps the seat out; unset, a ``test`` or
    ``architecture`` seat is judged by its own role's rule and any other
    role is never judged. A ``security`` seat is judged only when it names
    ``relevance: security`` itself: the rule reads path names, not what the
    change does, so a vulnerability in an ordinary file would miss it, and
    leaving the security reviewer out on that is a choice to make by hand.
    A value validation would refuse is read as ``always``: a seat that runs
    is the careful answer.
    """
    if not isinstance(reviewer, dict):
        return None
    role = str(reviewer.get("role") or "general").strip().lower()
    if role == "general":
        return None
    value = reviewer.get("relevance")
    if value is not None:
        named = value.strip().lower() if isinstance(value, str) else ""
        return named if named in RELEVANCE_RULES else None
    return role if role in RELEVANCE_RULES and role not in OPT_IN_RELEVANCE else None


def skip_unneeded_roles(settings: Dict[str, Any]) -> bool:
    """``optimization.skip_unneeded_roles``: on unless it is ``false``."""
    return settings.get("skip_unneeded_roles") is not False


def is_doc(path: str) -> bool:
    """A ``.md``, ``.rst`` or ``.txt`` file, or one under ``DESIGN_DOCS_PREFIXES``."""
    normal = _plan_path(str(path))
    base = posixpath.basename(normal).lower()
    return normal.startswith(DESIGN_DOCS_PREFIXES) or base.endswith(DOC_SUFFIXES)


def _top_dirs(paths: Sequence[str]) -> List[str]:
    """The first path component of each path, ``.`` for a root file; once each, in order."""
    dirs: List[str] = []
    for path in paths:
        normal = _plan_path(str(path))
        top = normal.split("/", 1)[0] if "/" in normal else "."
        if top not in dirs:
            dirs.append(top)
    return dirs


def _matches(stage: str, paths: Sequence[str], patterns: Sequence[str]) -> List[Tuple[str, str]]:
    """``high_risk_matches`` on a diff's paths; case-insensitive on a plan's tokens."""
    if stage == "design":
        return _token_matches(paths, patterns)
    return high_risk_matches(paths, patterns)


def _hit_reason(hits: Sequence[Tuple[str, str]]) -> str:
    return "%s matches %s%s" % (hits[0][0], hits[0][1], _and_more(len(hits) - 1))


def _security_needed(stage: str, evidence: Evidence, settings: Dict[str, Any]) -> Tuple[bool, str]:
    hits = _matches(stage, evidence.touched, risk_patterns(settings) + security_patterns(settings))
    if hits:
        return True, _hit_reason(hits)
    if stage == "design":
        return False, "no security-relevant token in the plan (optimization.security_paths)"
    return False, "no security-relevant path changed (optimization.security_paths)"


def _test_needed(stage: str, evidence: Evidence) -> Tuple[bool, str]:
    if stage == "design":
        if evidence.files:
            return True, "Files to Modify names code: %s" % evidence.files[0]
        if evidence.tests:
            return True, "Files to Modify names a test: %s" % evidence.tests[0]
        return False, "docs-only plan: Files to Modify names no code or test file"
    code = [path for path in evidence.files if not is_doc(path)]
    if code:
        return True, "%s is not documentation" % code[0]
    return False, "docs-only change: every reviewed file is documentation"


def _architecture_needed(stage: str, evidence: Evidence, settings: Dict[str, Any]) -> Tuple[bool, str]:
    count = len(evidence.files)
    design = stage == "design"
    if count >= DESIGN_LARGE_PLAN_FILES:
        return True, "%d %s" % (count, "code files in Files to Modify" if design else "files reviewed")
    dirs = _top_dirs([path for path in evidence.files if not is_doc(path)])
    if len(dirs) >= 2:
        return True, "spans %d directories (%s)" % (len(dirs), ", ".join(dirs))
    hits = _matches(stage, evidence.named, architecture_patterns(settings))
    if hits:
        return True, _hit_reason(hits)
    where = "%d director%s" % (len(dirs), "y" if len(dirs) == 1 else "ies")
    reason = "%s, %d %s, no contract, schema, config or CLI path" % (
        where,
        count,
        "code file(s)" if design else "file(s)",
    )
    return False, reason + " (optimization.architecture_paths)"


def _rule_answer(rule: str, stage: str, evidence: Evidence, settings: Dict[str, Any]) -> Tuple[bool, str]:
    if rule == "security":
        return _security_needed(stage, evidence, settings)
    if rule == "test":
        return _test_needed(stage, evidence)
    return _architecture_needed(stage, evidence, settings)


#: The reason a carried finding keeps a seat; it alone of the keep reasons
#: also keeps a code panel whole against the low-risk cut.
_CARRIED = "has open accepted finding"


def relevance_records(
    stage: str,
    panel: Sequence[Dict[str, Any]],
    evidence: Evidence,
    settings: Dict[str, Any],
    *,
    hits: Sequence[Tuple[str, str]] = (),
    declared: bool = False,
    carried: Sequence[Dict[str, Any]] = (),
    only: bool = False,
) -> List[Dict[str, Any]]:
    """One ``relevance`` record per seat judged this round: whether it runs, and why.

    ``stage`` is ``code`` or ``design``. Judged: a seat that always runs
    (``when`` seats already have a record), is not ``general``, and has a
    rule (``relevance_rule``). Nothing is judged when
    ``optimization.skip_unneeded_roles`` is false or the evidence is in doubt.
    The level is not asked: it never changes the answer.

    Before its rule, a seat is kept, in this order, by a high-risk hit, a
    declaration, an open accepted finding it reported, or ``--only``. A kept
    seat gets a record that says so, which the report counts as a round the
    rule was asked.

    Every reason is the rule's bare answer, redacted; how to include a seat
    left out is ``conditional_notes``'s to say. The empty-panel guard is not
    applied here: ``join_relevance`` applies it once a round's records are
    joined.
    """
    if not skip_unneeded_roles(settings) or evidence.doubt:
        return []
    records: List[Dict[str, Any]] = []
    for reviewer in panel:
        if not isinstance(reviewer, dict) or reviewer_condition(reviewer) != WHEN_ALWAYS:
            continue
        rule = relevance_rule(reviewer)
        if rule is None:
            continue
        name = str(reviewer.get("id") or "")
        own = [
            finding
            for finding in carried
            if isinstance(finding, dict) and name in [str(r) for r in finding.get("reported_by") or []]
        ]
        own.sort(key=_finding_order)
        if hits:
            runs, reason = True, _hit_reason(hits)
        elif declared:
            runs, reason = True, "declared with --high-risk"
        elif own:
            runs, reason = True, "%s %s%s" % (_CARRIED, own[0].get("id"), _and_more(len(own) - 1))
        elif only:
            runs, reason = True, "named by --only"
        else:
            runs, reason = _rule_answer(rule, stage, evidence, settings)
        records.append({"id": name, "when": WHEN_RELEVANCE, "runs": runs, "reason": redact(reason)})
    return records


def join_relevance(
    panel: Sequence[Dict[str, Any]],
    conditional: Sequence[Dict[str, Any]],
    relevance: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """A round's ``when`` records and its ``relevance`` records, as one new list.

    Copies of both, so neither input is changed. When relevance judged a
    seat and no seat would run, the one ``choose_reviewers`` keeps does, its
    record rewritten to say why (``_keep_one``).
    """
    joined = [dict(record) for record in (*conditional, *relevance)]
    if relevance:
        _keep_one(panel, joined)
    return joined


def _keep_one(panel: Sequence[Dict[str, Any]], records: Sequence[Dict[str, Any]]) -> None:
    """The empty-panel guard: when no seat would run, the one ``choose_reviewers`` keeps does.

    Its record in ``records`` -- a list ``join_relevance`` owns -- is
    rewritten in place, whatever kind it is.
    """
    members = [reviewer for reviewer in panel if isinstance(reviewer, dict)]
    if not members or any(qualifies(reviewer, records) for reviewer in members):
        return
    name = str(choose_reviewers(members, 1)[0].get("id") or "")
    for record in records:
        if record.get("id") == name:
            record["runs"] = True
            record["reason"] = "kept: no other reviewer would run"


def keeps_whole(records: Sequence[Dict[str, Any]]) -> bool:
    """Whether a record that ran should keep a code panel whole against the low-risk cut.

    A ``when`` seat that qualified does; a relevance answer does only when a
    carried finding kept the seat. A rule finding its evidence is not a
    reason to pay for two reviewers to agree that a small change is small.
    """
    for record in records:
        if not record.get("runs"):
            continue
        if record.get("when") != WHEN_RELEVANCE or str(record.get("reason") or "").startswith(_CARRIED):
            return True
    return False


def code_evidence(paths: Sequence[str], reviewed: Optional[Sequence[str]]) -> Evidence:
    """A code round's evidence: ``paths`` the whole change, ``reviewed`` the
    snapshot's ``files`` (None when it has none)."""
    if reviewed is None:
        return Evidence((), (), (), doubt="the snapshot lists no reviewed files")
    if not reviewed:
        return Evidence((), (), (), doubt="no file was reviewed")
    touched = [str(path) for path in paths]
    return Evidence(touched, [str(path) for path in reviewed], touched)


def design_evidence(scan: Any, plan_state: str) -> Evidence:
    """A plan's evidence, from ``review.plan_tokens()``; doubt when the plan
    was not read or its Files to Modify cannot be judged."""
    if plan_state != "ok" or scan is None:
        return Evidence((), (), (), doubt="plan could not be read")
    files = plan_files(scan)
    if files.doubt:
        return Evidence((), (), (), doubt=files.doubt)
    tests = [path for path in files.shaped if path.startswith(DESIGN_TESTS_PREFIXES)]
    return Evidence([item.token for item in scan.tokens], files.code, files.shaped, tests)


def conditional_notes(records: Sequence[Dict[str, Any]]) -> List[str]:
    """One line per conditional record, saying whether its reviewer runs and why.

    A seat a relevance rule left out is told how to include it.
    """
    notes: List[str] = []
    for record in records:
        verdict = "added" if record["runs"] else "left out"
        line = "%s (when: %s) %s: %s" % (record["id"], record["when"], verdict, record["reason"])
        if record["when"] == WHEN_RELEVANCE and not record["runs"]:
            line += "; --only %s to include it" % redact(str(record["id"]))
        notes.append(line)
    return notes


def risk_reason(high_risk: Sequence[Tuple[str, str]], declared: bool) -> str:
    """Why a round is high-risk, for the model-switch note; "" when it is not."""
    if high_risk:
        return redact(_hit_reason(high_risk))
    return "declared with --high-risk" if declared else ""


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
        return conditional_notes(self.conditional)

    def risk_reason(self) -> str:
        """Why the round is high-risk, for the model-switch note; "" when it is not.

        A high-risk path hit, or ``--high-risk``: the two that switch a seat
        to its ``high_risk_model``.
        """
        return risk_reason(self.high_risk, self.declared)

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
    reviewed: Optional[Sequence[str]] = None,
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

    ``reviewed`` is the snapshot's list of reviewed files, for the role
    relevance rules (``relevance_records``), which judge the panel at every
    level. Without it nothing is judged: a caller that has no list has no
    evidence of absence either.
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
        # At every level, `quality` included: the level never keeps a role
        # with nothing to read. A high-risk hit does, as it escalates.
        relevance = relevance_records(
            "code",
            panel,
            code_evidence(paths, reviewed),
            settings,
            hits=hits,
            declared=declared,
            carried=carried,
            only=only,
        )
        conditional = join_relevance(panel, conditional, relevance)
        members = [reviewer for reviewer in panel if isinstance(reviewer, dict)]
        reviewers = sum(1 for reviewer in members if qualifies(reviewer, conditional))
    # A conditional reviewer that qualified keeps the panel whole. A high-risk
    # path hit already does, through `quality`; a declaration, a carried
    # finding or a reviewer's own path does not move the level, and the cut
    # would drop the very reviewer that just qualified. A role relevance rule
    # that found its evidence does not: see `keeps_whole`.
    keep_whole = declared or keeps_whole(conditional)
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


def _folded_patterns(patterns: Sequence[str]) -> Dict[str, str]:
    """Each pattern lower-cased, mapped back to the first one written that way."""
    folded: Dict[str, str] = {}
    for pattern in patterns:
        folded.setdefault(pattern.lower(), pattern)
    return folded


def _token_matches(tokens: Sequence[str], patterns: Sequence[str]) -> List[Tuple[str, str]]:
    """``(token, pattern)`` for each plan token one of ``patterns`` matches.

    Case-insensitive, through ``_risk_candidates``: the plan's spelling of a
    path says nothing about what it is. Each token is matched once.
    """
    folded = _folded_patterns(patterns)
    seen = set()
    hits: List[Tuple[str, str]] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        matched = high_risk_matches(_risk_candidates(token), list(folded))
        if matched:
            hits.append((token, folded[matched[0][1]]))
    return hits


def design_risk(settings: Dict[str, Any], scan: Any) -> List[Tuple[str, str, str]]:
    """``(token, section, pattern)`` for every plan token matching a high-risk pattern.

    The whole plan, not only Files to Modify, in any case. Apart from
    ``decide_design`` so that a design round can ask it whatever
    ``review.design.enabled`` is: it decides a seat's ``high_risk_model`` and
    who sits on the panel as well as whether the stage runs. ``scan`` is
    ``review.plan_tokens()`` of the plan, or None for a plan not read.
    """
    if scan is None:
        return []
    sections: Dict[str, str] = {}
    for item in scan.tokens:
        sections.setdefault(item.token, item.section)
    return [
        (token, sections.get(token, ""), pattern)
        for token, pattern in _token_matches([item.token for item in scan.tokens], risk_patterns(settings))
    ]


class PlanFiles(NamedTuple):
    """What a plan's Files to Modify names, shaped as ``decide_design`` reads it."""

    #: Every file-shaped entry, normalised, in order and once each.
    shaped: List[str]
    #: The entries that are neither docs nor tests.
    code: List[str]
    #: Why the section cannot be judged (no section, a glob, a directory, a
    #: path through ``..``, a name that may be a directory, no file), or "".
    doubt: str


def plan_files(scan: Any) -> PlanFiles:
    """The Files to Modify section of ``scan``, and whether it can be judged at all.

    Only file-shaped tokens (a ``/`` or an extension) are judged as globs or
    directories, so ``payload["mode"]`` there is code, not a pattern. The
    first doubt found is the one reported.
    """
    if scan is None:
        return PlanFiles([], [], "plan could not be read")
    if not scan.has_files_section:
        return PlanFiles([], [], "no Files to Modify section")
    shaped: List[str] = []
    for item in scan.tokens:
        if not item.in_files:
            continue
        path = _plan_path(item.token)
        extension = _has_extension(path)
        if "/" not in path and not extension:
            continue
        if any(ch in path for ch in _GLOB_CHARS):
            return PlanFiles(shaped, [], "names a glob: %s" % item.token)
        if path.endswith("/"):
            return PlanFiles(shaped, [], "names a directory: %s" % item.token)
        if ".." in path.split("/"):
            return PlanFiles(shaped, [], "names a path through ..: %s" % item.token)
        if not extension:
            if item.prose:
                continue
            return PlanFiles(shaped, [], "may name a directory: %s" % item.token)
        if path not in shaped:
            shaped.append(path)
    if not shaped:
        return PlanFiles(shaped, [], "Files to Modify names no file")
    code = [path for path in shaped if not path.startswith(DESIGN_TESTS_PREFIXES) and not is_doc(path)]
    return PlanFiles(shaped, code, "")


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

    hits = design_risk(settings, scan)
    if hits:
        token, section, _ = hits[0]
        where = " (%s)" % section if section else ""
        more = " and %d more" % (len(hits) - 1) if len(hits) > 1 else ""
        return DesignDecision(
            mode, True, "touches %s%s%s" % (token, where, more), high_risk=[(t, p) for t, _, p in hits]
        )

    files = plan_files(scan)
    if files.doubt:
        return DesignDecision(mode, True, files.doubt)
    count = len(files.code)
    if count >= DESIGN_LARGE_PLAN_FILES:
        return DesignDecision(mode, True, "%d code files" % count, files=count)
    noun = "code file" if count == 1 else "code files"
    return DesignDecision(mode, False, "%d %s, none high-risk" % (count, noun), files=count)


class DesignPlan:
    """Who sits on a design round, and what it was decided from.

    A design round has no gate, no panel cut and no escalation: the level is
    the configured one, recorded for the report only. What it does have is
    the plan's high-risk hits -- a ``when: high-risk`` seat joins on them and
    a seat with a ``high_risk_model`` switches to it -- and the conditional
    records of ``condition_reviewers`` and ``relevance_records``.
    """

    def __init__(
        self,
        level: str,
        high_risk: Sequence[Tuple[str, str]] = (),
        conditional: Sequence[Dict[str, Any]] = (),
        declared: bool = False,
        files: int = 0,
    ) -> None:
        self.level = level
        #: ``(token, pattern)`` for every plan token matching a high-risk pattern.
        self.high_risk = list(high_risk)
        self.conditional = [dict(record) for record in conditional]
        self.declared = declared
        #: Code files counted in Files to Modify.
        self.files = files

    @property
    def is_high_risk(self) -> bool:
        return bool(self.high_risk) or self.declared

    def risk_reason(self) -> str:
        """Why the round is high-risk, for the model-switch note; "" when it is not."""
        return risk_reason(self.high_risk, self.declared)

    def conditional_notes(self) -> List[str]:
        return conditional_notes(self.conditional)

    def left_out(self) -> List[Dict[str, Any]]:
        """The records of the seats this round leaves out."""
        return [record for record in self.conditional if not record.get("runs")]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "high_risk": [{"path": redact(p), "pattern": redact(q)} for p, q in self.high_risk],
            "declared": self.declared,
            "conditional": [dict(record) for record in self.conditional],
            "files": self.files,
        }


def decide_design_round(
    settings: Dict[str, Any],
    scan: Any,
    plan_state: str,
    panel: Sequence[Dict[str, Any]],
    *,
    declared: bool = False,
    carried: Sequence[Dict[str, Any]] = (),
    only: bool = False,
) -> DesignPlan:
    """Who sits on this design round, from the plan the architect wrote.

    Whether the stage runs at all is ``decide_design``'s; this is asked once
    it does. ``panel`` is the design panel in force (``LoadedConfig.
    design_reviewers``), ``scan`` the plan's ``review.plan_tokens()`` when
    ``plan_state`` is ``"ok"``. ``carried`` is the accepted findings the
    design review still has open.
    """
    hits = design_risk(settings, scan) if plan_state == "ok" else []
    pairs = [(token, pattern) for token, _section, pattern in hits]
    conditional = condition_reviewers(panel, pairs, declared=declared, carried=carried, only=only)
    relevance = relevance_records(
        "design",
        panel,
        design_evidence(scan, plan_state),
        settings,
        hits=pairs,
        declared=declared,
        carried=carried,
        only=only,
    )
    conditional = join_relevance(panel, conditional, relevance)
    files = plan_files(scan) if plan_state == "ok" else PlanFiles([], [], "")
    return DesignPlan(
        normalise_level(settings.get("level")),
        pairs,
        conditional,
        declared=bool(declared),
        files=len(files.code),
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
