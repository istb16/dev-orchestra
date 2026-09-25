"""Independent multi-model review: snapshot, fan-out, parse, consolidate, triage.

Design rules enforced here:

* every reviewer sees the *same frozen snapshot*, taken before any reviewer runs
* reviewers never see each other's output -- no cross-contamination
* reviewers run read-only; a reviewer that edits files is a configuration bug
* one reviewer failing does not fail the batch

The same fan-out reviews a plan before implementation (``create_design_snapshot``),
under those same rules and with its own artifacts and round counter.

Deduplication is deliberately split in two. Auto-merge only collapses findings
whose wording is near-identical, because collapsing two distinct bugs hides one.
Cross-model duplicates almost never look alike in prose -- measured on real
two-provider output, a confirmed duplicate pair scored 0.03 text similarity
while an unrelated pair scored 0.29 -- so they are surfaced as *candidates*,
matched on the code they quote, for the orchestrator to confirm during triage.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
import posixpath
import re
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import workspace as ws
from .optimization import DEFAULT_LEVEL, MAX_FINDINGS_BY_LEVEL
from .providers import MODE_REVIEW, ModelResolutionError, Usage, get_provider

SEVERITIES = ("critical", "high", "medium", "low")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}
TRIAGE_STATUSES = ("accepted", "rejected", "duplicate", "needs-investigation", "needs-triage")

#: Files whose diff is withheld from reviewers by default.
#:
#: A reviewer reads a diff to judge code somebody wrote. None of these were
#: written: they are generated, vendored, or recorded. Sending them costs the
#: same tokens as real code, once per reviewer and once per round -- a lockfile
#: touched by a routine dependency bump is regularly the largest single item in
#: a review, and no reviewer has ever had a useful thought about one.
#:
#: Withheld is not hidden. The file is still named to the reviewer, with how
#: many lines changed, so a review that genuinely turns on a dependency version
#: can go and read it. Anything ambiguous is deliberately absent from this
#: list: ``build/`` is conventionally output but is hand-written often enough
#: that excluding it by default would sometimes hide real work, and quietly
#: dropping a real change is a worse failure than paying for a lockfile.
DEFAULT_EXCLUDE = (
    # Lockfiles. The fact of the bump is in the manifest diff, which is kept.
    "*.lock",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "go.sum",
    # Vendored dependencies and build output.
    "dist/*",
    "*/dist/*",
    "vendor/*",
    "*/vendor/*",
    "node_modules/*",
    "*/node_modules/*",
    "*.min.js",
    "*.min.css",
    "*.map",
    # Recorded fixtures: regenerated, not authored.
    "*.snap",
)

ROLE_GUIDANCE: Dict[str, str] = {
    "general": (
        "Priority order: correctness bugs, regressions, missed edge cases, security, "
        "performance, data consistency, concurrency, error handling, thin tests. "
        "Flag needless complexity. Style-only: low severity at most, usually skip."
    ),
    "security": (
        "Focus: authn/authz gaps, injection (SQL/command/template), unsafe deserialisation, "
        "SSRF, path traversal, secret handling and leakage, unsafe defaults, missing input "
        "validation, access-control regressions in changed paths."
    ),
    "performance": (
        "Focus: algorithmic complexity, N+1 queries, missing indexes, needless I/O, unbounded "
        "memory, blocking calls on hot paths, cache invalidation. Quantify where the diff allows."
    ),
    "test": (
        "Focus: coverage of the changed behaviour -- missed edge cases, assertions that assert "
        "nothing, flaky patterns (time, ordering, network), untested error paths. "
        "Name the missing case, not 'add more tests'."
    ),
    "architecture": (
        "Focus: layering violations, misplaced responsibilities, leaky abstractions, coupling "
        "added by the change, public API shape, fit with conventions already in this codebase."
    ),
    "database": (
        "Focus: schema changes, migration safety (locking, backfills, reversibility), nullability "
        "and constraints, index coverage for new queries, transaction boundaries, "
        "data-consistency risk during deploy."
    ),
    "frontend": (
        "Focus: component state, rendering performance, accessibility (roles, labels, keyboard, "
        "focus), responsive layout, error and loading states, client-side validation not mirrored "
        "server-side."
    ),
    "backend": (
        "Focus: API contracts and compatibility, validation, error responses and status codes, "
        "idempotency, transactional integrity, background job semantics, observability of the "
        "changed paths."
    ),
}

#: The same roles, pointed at a plan instead of a diff.
#:
#: A design reviewer is not reading code somebody wrote; it is checking a
#: proposal against the codebase as it stands. So the question shifts from "is
#: this correct" to "would following this produce something correct" -- which
#: means checking that the files and symbols the plan names exist, that its
#: account of the current behaviour is true, and that what it leaves out is
#: not the hard part.
DESIGN_ROLE_GUIDANCE: Dict[str, str] = {
    "general": (
        "Does the plan meet the stated goal? Is the root cause right, or is it treating a "
        "symptom? Do the files and symbols it names exist, and does it cover every caller? "
        "Is it the minimal change? Does it break an existing contract? Is the Test Strategy "
        "enough to catch a regression? Name any assumption it leaves unstated."
    ),
    "security": (
        "Focus: what the proposal would let through -- authn/authz gaps it creates or fails to "
        "close, data exposed by a new path or payload, secret handling, unsafe defaults, "
        "validation the design assumes happens elsewhere."
    ),
    "performance": (
        "Focus: the cost of the proposed design at realistic scale -- added queries per request, "
        "work moved onto a hot path, unbounded growth, an abstraction that forces N calls where "
        "one would do. Say where the plan lacks the numbers to judge it."
    ),
    "test": (
        "Focus: the Test Strategy. Which behaviour in the plan has no test named for it, which "
        "regression would go unnoticed, which case is asserted so loosely it would pass either "
        "way. Name the missing case, not 'add more tests'."
    ),
    "architecture": (
        "Focus: where the plan puts each responsibility, the layers it crosses, the coupling it "
        "adds, the shape of any new API or module boundary, and whether it fits the conventions "
        "already in this codebase or invents a parallel one."
    ),
    "database": (
        "Focus: any proposed schema change -- migration safety (locking, backfills, "
        "reversibility), nullability and constraints, index coverage for the queries the plan "
        "implies, and what the data looks like mid-deploy."
    ),
    "frontend": (
        "Focus: the proposed component and state boundaries, rendering cost, accessibility, "
        "error and loading states the plan does not mention, and client-side validation it "
        "assumes without a server-side counterpart."
    ),
    "backend": (
        "Focus: the proposed API contracts and their compatibility, validation and error "
        "responses, idempotency, transaction boundaries, background job semantics, and whether "
        "the changed paths would be observable when they misbehave."
    ),
}

#: Caps on what a reviewer writes back.
#:
#: Output is the expensive direction -- per token it costs several times what
#: input does, and a reviewer's output is billed again as consolidation input
#: and again as the fixer's brief. An uncapped template invites a reviewer to
#: pad: twenty low findings, a screenful of quoted context per finding, a patch
#: where a sentence would do.
#:
#: The cap is on volume, not on judgement: a reviewer over the limit is asked
#: for its worst findings, not asked to stay quiet. Nothing is dropped on our
#: side either -- every finding that comes back is parsed and kept, and a
#: reviewer that overshoots is reported rather than trimmed, because deciding
#: which of its findings to discard is triage, and triage is not this layer's.
#: The library default, for a caller that does not pass one. The CLI always
#: does, from the level, so this is the same number by the same route.
DEFAULT_MAX_FINDINGS = MAX_FINDINGS_BY_LEVEL[DEFAULT_LEVEL]
MAX_EVIDENCE_LINES = 3
MAX_FIX_LINES = 2

REVIEW_PROMPT_TEMPLATE = """Independent code reviewer. Read-only.

Reviewer: {reviewer_id} | Role: {role} | Repo root: {root}

## Task

Review only the change below, on its own merits.
Read any file for context. Do not modify, create, or delete files. Do not run
commands that mutate the repository or the network.

{role_guidance}

## Change under review

{diff_section}

## Output

One block per issue, exactly this shape:

## Finding
- Severity: critical|high|medium|low
- File: <path from repo root>
- Line: <number, range, or n/a>
- Category: <one word, e.g. correctness, security, performance, tests>
- Problem: <what is wrong, 1-2 sentences>
- Impact: <what breaks, under what conditions>
- Evidence: <the code or hunk that shows it>
- Fix: <the concrete change>

{limits}
"""

#: The field names are deliberately the code template's, so ``parse_findings``,
#: the deduplication and the fix brief all work unchanged. ``File`` takes a
#: plan section or the repository path the plan misjudges, which is what makes
#: a design finding locatable at all.
DESIGN_REVIEW_PROMPT_TEMPLATE = """Independent design reviewer. Read-only.

Reviewer: {reviewer_id} | Role: {role} | Repo root: {root}

## Task

Review the implementation plan below before any code is written. Judge it
against the codebase as it is now: read the files it names and check every
claim it makes about them. Do not modify, create, or delete files. Do not run
commands that mutate the repository or the network.

{role_guidance}

## Design request

{request_section}

## Plan under review

{plan_section}

## Output

One block per issue, exactly this shape:

## Finding
- Severity: critical|high|medium|low
- File: <plan section, e.g. plan.md#Proposed Change, or the repo path the plan misjudges>
- Line: <line in that file, or n/a>
- Category: <one word, e.g. correctness, completeness, compatibility, risk, tests>
- Problem: <what is wrong or missing, 1-2 sentences>
- Impact: <what the implementation would get wrong if the plan were followed>
- Evidence: <the plan text, or the code that contradicts it>
- Fix: <the concrete change to the plan>

{limits}
"""


# --------------------------------------------------------------------------- snapshot


def create_snapshot(
    workspace: ws.Workspace,
    base: Optional[str] = None,
    include_untracked: bool = True,
    exclude: Optional[Sequence[str]] = None,
    incremental: bool = True,
) -> Dict[str, Any]:
    """Freeze the change under review into ``.ai/reviews/review-target.diff``.

    ``exclude`` withholds the *body* of a generated or vendored file's diff
    while still recording that it changed; pass ``()`` to take everything.

    ``incremental`` lets a second round diff against the previous round's
    snapshot instead of against ``HEAD``, so re-review sees the fix rather than
    the whole change again. It only applies when the previous snapshot was
    actually reviewed and something has changed since; a plain re-snapshot is
    always the full diff.
    """
    workspace.ensure()
    root = workspace.root
    if not ws.is_git_repo(root):
        raise ReviewError(
            "%s is not a git repository; review snapshots need git. "
            "Initialise a repo or review a specific set of files manually." % root
        )
    patterns = list(DEFAULT_EXCLUDE if exclude is None else exclude)

    head = _head(root)
    # Writing the tree means hashing every untracked-but-not-ignored file into
    # the object database, which on a repository with a large directory nobody
    # remembered to ignore is neither cheap nor invisible. Only
    # ``incremental: False`` says this round will not use one.
    #
    # ``--base`` used to switch this off too, and that was a mistake that cost
    # real money. The base decides what the *first* round covers; whether a
    # *later* round may narrow to the fix is a separate question. Reviewing a
    # branch against master -- which is what --base is for, and the ordinary
    # way to use this tool -- therefore re-sent the whole branch every round.
    # Measured on one real three-round review: the diff grew 1,867 -> 2,472 ->
    # 3,228 lines while the findings fell 11 -> 6 -> 5, and the cost per
    # finding went from $0.20 to $1.00.
    wanted = bool(incremental)
    tree = _write_tree(root) if wanted else ""
    previous_tree = _reviewed_tree(workspace, base) if wanted else ""
    if previous_tree and tree and previous_tree != tree:
        revisions = [previous_tree, tree]
        strategy = "git diff <previous round> <now>"
    else:
        revisions = [base or "HEAD"]
        strategy = "git diff %s" % revisions[0]
        previous_tree = ""

    # Which files changed, before asking for the diff itself: the exclusion is
    # decided from this cheap listing, so the expensive call is made once and
    # already filtered.
    tracked, listed_code, listed_err = _numstat(root, revisions, patterns)
    withheld = [entry for entry in tracked if entry.get("pattern")]
    # A tree-to-tree diff skips the untracked pass below, which is where the
    # rules about what is not part of the change at all normally get applied.
    # They have to be applied here instead, or they hold in round 1 and lapse
    # in round 2.
    suppressed = _not_under_review(tracked, workspace, root, include_untracked) if previous_tree else []
    if suppressed:
        withheld = [entry for entry in withheld if entry["path"] not in suppressed]

    if listed_code == 0:
        exclusions = _pathspecs(withheld) + [":(exclude,literal)%s" % path for path in suppressed]
        code, diff, err = _diff(root, revisions, exclusions)
        if code != 0 and exclusions:
            # Old git, or pathspec magic it does not accept. Taking the whole
            # diff costs tokens; dropping the change silently costs a review.
            code, diff, err = _diff(root, revisions, [])
            strategy += " (exclusions unsupported by this git)"
            withheld = []
            suppressed = []
    elif revisions == ["HEAD"] and not head:
        # A repository with no commits yet: everything is untracked. Only this
        # one case is benign -- any other failure is a revision git could not
        # resolve, and reporting that as "nothing to review" sends someone
        # hunting through their own change for something that was never there.
        code, diff, err, strategy = 0, "", "", "untracked-only (no HEAD commit)"
    else:
        raise ReviewError("git diff failed: %s" % (listed_err.strip() or listed_code))
    if code != 0:
        raise ReviewError("git diff failed: %s" % (err.strip() or code))

    untracked: List[str] = []
    # An incremental diff is tree-to-tree, and both trees already contain the
    # untracked files: adding them again would duplicate every hunk.
    if include_untracked and not previous_tree:
        ucode, uout, _ = ws.git(["ls-files", "--others", "--exclude-standard"], root)
        if ucode == 0:
            for name in [line.strip() for line in uout.splitlines() if line.strip()]:
                path = os.path.join(root, name)
                if not os.path.isfile(path) or os.path.getsize(path) > 512_000:
                    continue
                if _is_orchestrator_artifact(name, workspace):
                    continue
                pattern = withholds(name, patterns)
                if pattern:
                    withheld.append(_withheld_entry(name, pattern, added=_count_lines(path)))
                    continue
                dcode, dout, _ = ws.git(["diff", "--no-color", "--no-index", "--", os.devnull, name], root)
                # --no-index exits 1 when files differ, which is the normal case.
                if dcode in (0, 1) and dout.strip():
                    diff += dout
                    untracked.append(name)

    ws.write_text(workspace.snapshot_path, diff)
    full_diff_path = ""
    if previous_tree:
        # Leave the whole change somewhere the reviewer can look. This costs a
        # git call and no tokens: it is read only if a reviewer needs it.
        #
        # The exclusions are recomputed against *this* range: the incremental
        # round's withheld list only covers what the fix touched, and reusing
        # it would write the very lockfile round 1 withheld into the file the
        # prompt invites a reviewer to open.
        full_tracked, full_code, _ = _numstat(root, [base or "HEAD"], patterns)
        if full_code == 0:
            full_withheld = [entry for entry in full_tracked if entry.get("pattern")]
            full_suppressed = _not_under_review(full_tracked, workspace, root, include_untracked)
            fcode, full, _ = _diff(
                root,
                [base or "HEAD"],
                _pathspecs(full_withheld) + [":(exclude,literal)%s" % p for p in full_suppressed],
            )
            if fcode == 0 and full.strip():
                ws.write_text(workspace.full_snapshot_path, full)
                full_diff_path = workspace.relative(workspace.full_snapshot_path)
    added_lines, deleted_lines = _diff_line_counts(diff)
    reviewed = _reviewed_files(tracked, withheld, suppressed, untracked)
    # Two different lists, for two different questions.
    #
    # ``files`` is what a reviewer is being shown, and is what the size
    # threshold is measured against. ``changed_paths`` is everything git says
    # this change touches -- withheld files, and the name a rename came from,
    # neither of which appears in the diff. Risk is judged from the second:
    # a withheld ``.env`` is still a secret, and a file renamed away from
    # ``auth.py`` was still an auth file a moment ago.
    changed_paths = list(reviewed)
    for entry in tracked:
        for name in (entry.get("path"), entry.get("previous")):
            if name and name not in changed_paths:
                changed_paths.append(str(name))
    for entry in withheld:
        name = entry.get("path")
        if name and name not in changed_paths:
            changed_paths.append(str(name))
    for name in untracked:
        if name not in changed_paths:
            changed_paths.append(name)
    meta = {
        "generated_at": ws.utcnow(),
        "strategy": strategy,
        "base": base,
        "head": head,
        #: The working tree as a git tree object, so the next round can diff
        #: against exactly what this round reviewed.
        "tree": tree,
        "incremental_from": previous_tree,
        "full_diff": full_diff_path,
        "files": reviewed,
        "changed_paths": changed_paths,
        "untracked_included": untracked,
        "withheld": sorted(withheld, key=lambda entry: str(entry.get("path"))),
        "exclude_patterns": patterns,
        "bytes": len(diff.encode("utf-8")),
        "lines_added": added_lines,
        "lines_deleted": deleted_lines,
        "sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "empty": not diff.strip(),
    }
    ws.write_json(workspace.snapshot_meta_path, meta)
    return meta


def _diff(root: str, revisions: Sequence[str], pathspecs: Sequence[str]) -> "tuple[int, str, str]":
    """One or two revisions, diffed the same way either time.

    One revision compares it to the working tree; two compare them to each
    other, which is how an incremental round diffs against the tree the
    previous round reviewed.
    """
    return ws.git(
        [
            "diff",
            "--no-color",
            "-M",
            "--diff-algorithm=histogram",
            *revisions,
            "--",
            *pathspecs,
        ],
        root,
    )


def _write_tree(root: str) -> str:
    """Record the working tree as a git tree object, or "" if git cannot.

    Written through a throwaway index so the user's own index is untouched --
    the same trick ``git stash create`` uses. Untracked-but-not-ignored files
    are included, because a new module is usually the most important part of a
    change and the next round has to be able to diff against it.

    The objects this writes are unreferenced, which is fine for the lifetime of
    a workflow: git does not prune loose objects that recent.
    """
    handle, index = tempfile.mkstemp(prefix="dev-orchestra-index-")
    os.close(handle)
    # git wants to create the index itself; an existing empty file is not one.
    try:
        os.unlink(index)
    except OSError:
        return ""
    env = {"GIT_INDEX_FILE": index}
    try:
        code, _, _ = ws.git(["read-tree", "HEAD"], root, env=env)
        if code != 0:
            # No commits yet: an empty index is the right starting point.
            code, _, _ = ws.git(["read-tree", "--empty"], root, env=env)
            if code != 0:
                return ""
        code, _, _ = ws.git(["add", "-A", "--", "."], root, env=env)
        if code != 0:
            return ""
        code, out, _ = ws.git(["write-tree"], root, env=env)
        return out.strip() if code == 0 else ""
    finally:
        for leftover in (index, index + ".lock"):
            try:
                os.unlink(leftover)
            except OSError:
                pass


def _reviewed_tree(workspace: ws.Workspace, base: Optional[str] = None) -> str:
    """The tree of the previous round, if narrowing to it is safe.

    Three conditions, and all of them are about the reviewer rather than the
    cost.

    The previous snapshot has to have been reviewed. Diffing against one
    nobody reviewed would answer a question no round asked, and would turn an
    ordinary re-snapshot -- taken because a reviewer failed, say -- into an
    empty diff.

    And that review has to have produced something the fix was answering.
    Scope is only narrowed together with the premise that explains it, so a
    round following a clean review, or one whose findings were all rejected,
    takes the whole change: there is no fix to check, and a fragment with
    nothing to judge it against is the failure this feature exists to avoid.

    And the change has to still be the same change. A round asking for a
    different base is redefining what is under review, and narrowing to a fix
    for the previous definition would answer the old question quietly. Same
    base, including no base at all, means the same change.
    """
    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
    tree = str(meta.get("tree") or "")
    if not tree:
        return ""
    if (meta.get("base") or None) != (base or None):
        return ""
    consolidated = ws.read_json(workspace.consolidated_json_path, {}) or {}
    reviewed = str((consolidated.get("snapshot") or {}).get("sha256") or "")
    if not reviewed or reviewed != str(meta.get("sha256") or ""):
        return ""
    return tree if accepted_findings(consolidated) else ""


def _is_orchestrator_artifact(name: str, workspace: ws.Workspace) -> bool:
    """The skill's own files are not part of the change under review.

    Without this, the config the setup wizard just wrote and the artifacts in
    ``.ai/`` show up as untracked "changes" in every snapshot.
    """
    normalised = name.replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    workspace_prefix = workspace.relative(workspace.dir).rstrip("/") + "/"
    if normalised == workspace_prefix.rstrip("/") or normalised.startswith(workspace_prefix):
        return True
    from .config import PROJECT_CONFIG_NAMES

    return os.path.basename(normalised) in PROJECT_CONFIG_NAMES


def withholds(path: str, patterns: Sequence[str]) -> str:
    """The first pattern that withholds ``path``, or ``""``.

    Matching is fnmatch, case-sensitively on every platform so a snapshot taken
    on Windows contains the same thing as one taken on Linux, against two
    targets: the full repository-relative path, and -- for a pattern with no
    ``/`` in it -- the base name alone, so ``*.lock`` catches a lockfile at any
    depth. ``*`` crosses ``/``, which is why the defaults spell out both
    ``dist/*`` and ``*/dist/*`` instead of relying on a ``**`` this does not
    implement.
    """
    name = posixpath.basename(path)
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern:
            continue
        if fnmatch.fnmatchcase(path, pattern):
            return pattern
        if "/" not in pattern and fnmatch.fnmatchcase(name, pattern):
            return pattern
    return ""


def _withheld_entry(path: str, pattern: str, added: Optional[int], deleted: Optional[int] = 0):
    return {"path": path, "pattern": pattern, "added": added, "deleted": deleted}


def withheld_lines(withheld: Sequence[Dict[str, Any]]) -> str:
    """How many changed lines were withheld, or ``"?"`` where git said ``-``."""
    total = 0
    exact = True
    for entry in withheld:
        for key in ("added", "deleted"):
            value = entry.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                total += value
            elif value is None:
                exact = False  # a binary file, which git does not count
    return "{:,}".format(total) + ("" if exact else "+")


def render_round_context(workspace: ws.Workspace, meta: Dict[str, Any]) -> str:
    """What a re-review needs to know that the fix diff alone does not say.

    A reviewer is stateless and sees no other reviewer's output, so handing it
    only the fix would be handing it a change with no premise: "is this
    correct" is unanswerable without knowing what it was correcting. The
    premise is cheap to supply -- the findings were already structured during
    triage, and cost a line each against the thousands a second full diff
    costs.

    What is deliberately left out is who reported what. The findings arrive as
    the brief the fixer worked from, which is a fact about the change, rather
    than as another reviewer's opinion still in play.
    """
    if not meta.get("incremental_from"):
        return ""
    consolidated = ws.read_json(workspace.consolidated_json_path, {}) or {}
    lines = ["Re-review. The diff above is the fix only, not the whole change."]
    if meta.get("full_diff"):
        lines.append("Whole change frozen at %s -- read it if you need the context." % meta["full_diff"])
    accepted = accepted_findings(consolidated)
    if accepted:
        lines.append("")
        lines.append("The fix was meant to address:")
        for finding in accepted:
            lines.append(
                "- [%s] %s:%s -- %s"
                % (
                    finding.get("severity", "?"),
                    finding.get("file", "?"),
                    finding.get("line", "n/a"),
                    finding.get("problem", ""),
                )
            )
        lines.append("")
        lines.append(
            "For each: fixed or not. Plus any new problem the fix introduced. "
            "Do not assume a listed item was real."
        )
    return "\n".join(lines)


def render_design_round_context(workspace: ws.Workspace, meta: Dict[str, Any]) -> str:
    """What a re-reviewed plan needs to say that the plan itself does not.

    The design version of ``render_round_context``, and the same reasoning:
    the reviewer is stateless, so a revision handed over as a fresh plan
    invites the same objections again. Who reported what is left out here too
    -- the accepted findings arrive as the brief the revision worked from, not
    as another reviewer's opinion still in play.
    """
    if not meta.get("previous_sha"):
        return ""
    consolidated = ws.read_json(workspace.consolidated_json_path, {}) or {}
    accepted = accepted_findings(consolidated)
    if not accepted:
        return ""
    lines = ["This plan is a revision. It was meant to address:"]
    for finding in accepted:
        lines.append(
            "- [%s] %s -- %s"
            % (finding.get("severity", "?"), finding.get("file", "?"), finding.get("problem", ""))
        )
    lines += [
        "",
        "For each: addressed or not, plus any new problem the revision introduced. "
        "Do not assume a listed item was real.",
    ]
    return "\n".join(lines)


def render_withheld(withheld: Sequence[Dict[str, Any]]) -> str:
    """The note that tells a reviewer what it is not being shown.

    Terse on purpose: this rides along on every reviewer prompt in every round,
    so it is a list of names and sizes, not an explanation.
    """
    if not withheld:
        return ""
    lines = ["Changed, diff withheld (generated or vendored):"]
    for entry in withheld:
        added, deleted = entry.get("added"), entry.get("deleted")
        if added is None and deleted is None:
            size = "binary"
        else:
            size = "+%s -%s" % (added if added is not None else "?", deleted if deleted is not None else "?")
        lines.append("- %s (%s)" % (entry.get("path"), size))
    lines.append("Read one only if this change turns on its contents.")
    return "\n".join(lines)


def _numstat(
    root: str, revisions: Sequence[str], patterns: Sequence[str]
) -> "tuple[List[Dict[str, Any]], int, str]":
    """Every changed file with its line counts, plus git's own exit and error.

    The exit code is handed back rather than reduced to a boolean because the
    caller has to tell "there is no HEAD yet", which is an ordinary state, from
    "that revision does not exist", which is a mistake worth reporting.
    """
    code, out, err = ws.git(["diff", "--numstat", "-z", "-M", *revisions, "--"], root)
    if code != 0:
        return [], code, err
    entries: List[Dict[str, Any]] = []
    for added, deleted, path, previous in _parse_numstat(out):
        entry = _withheld_entry(path, withholds(path, patterns), added, deleted)
        if previous and previous != path:
            # Kept for the risk check, which has to see the name a file was
            # renamed *from*: the diff only carries where it landed.
            entry["previous"] = previous
        entries.append(entry)
    return entries, 0, ""


def _head(root: str) -> str:
    code, out, _ = ws.git(["rev-parse", "HEAD"], root)
    return out.strip() if code == 0 else ""


def _untracked_paths(root: str) -> "set[str]":
    code, out, _ = ws.git(["ls-files", "--others", "--exclude-standard"], root)
    if code != 0:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def _not_under_review(
    tracked: Sequence[Dict[str, Any]],
    workspace: ws.Workspace,
    root: str,
    include_untracked: bool,
) -> List[str]:
    """Changed paths that are not part of the change under review at all.

    Distinct from ``withheld``: a withheld file *is* part of the change and is
    reported to the reviewer by name. These are not, and are simply absent --
    the orchestrator's own configuration and workspace, and, when the caller
    asked for tracked changes only, everything git does not track.

    The other diff path applies both rules while walking untracked files. A
    tree-to-tree diff never walks them, so without this the rules would hold
    for a first round and lapse for a re-review.
    """
    untracked: "set[str]" = set()
    if not include_untracked:
        untracked = _untracked_paths(root)
    suppressed: List[str] = []
    for entry in tracked:
        path = str(entry.get("path") or "")
        if not path:
            continue
        if _is_orchestrator_artifact(path, workspace) or path in untracked:
            suppressed.append(path)
    return suppressed


def _parse_numstat(out: str) -> "List[tuple[Optional[int], Optional[int], str, str]]":
    """Parse ``git diff --numstat -z``.

    NUL-separated because a rename is reported as an empty path followed by the
    old and new names as their own records, and the readable form spells the
    same thing as ``src/{old => new}.txt`` -- which would have to be unpicked,
    and unpicked wrongly for any path containing a brace. Binary files carry
    ``-`` instead of a count, which is not zero and is not reported as zero.
    """
    fields = out.split("\0")
    records: "List[tuple[Optional[int], Optional[int], str, str]]" = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        parts = field.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[2]
        previous = ""
        if not path:
            # A rename or copy: the next two records are the old and new names.
            old = fields[index] if index < len(fields) else ""
            new = fields[index + 1] if index + 1 < len(fields) else ""
            index += 2
            path = new or old
            previous = old if new else ""
        if not path:
            continue
        records.append((_maybe_int(added), _maybe_int(deleted), path, previous))
    return records


def _maybe_int(value: str) -> Optional[int]:
    try:
        return int(value)
    except ValueError:
        return None  # "-", which git uses for a binary file


def _pathspecs(withheld: Sequence[Dict[str, Any]]) -> List[str]:
    """Exclusions as pathspecs, by literal path rather than by our pattern.

    The pattern already did its matching here, so git is handed the exact names
    to leave out. ``literal`` keeps a path containing glob characters from
    being read as a glob by git in turn.
    """
    return [":(exclude,literal)%s" % entry["path"] for entry in withheld]


def _count_lines(path: str) -> Optional[int]:
    try:
        with open(path, "rb") as handle:
            return handle.read().count(b"\n")
    except OSError:
        return None


def _diff_line_counts(diff: str) -> "tuple[int, int]":
    """Added and deleted lines in a unified diff.

    Counted from the diff rather than from ``--numstat`` so the number
    describes exactly what a reviewer will see: a withheld lockfile changed
    twelve thousand lines and contributes none of them, which is the point of
    withholding it.

    Counted by tracking hunk boundaries rather than by recognising headers,
    because a header cannot be told from content by its first characters. A
    content line carries its own ``+``/``-`` prefix and nothing between that
    and the text, so a deleted ``---`` -- Markdown front matter, a YAML
    document separator, an underline -- arrives as ``----``, and an added
    ``++count`` as ``+++count``. Matching on those undercounts the change,
    which is the one direction that matters: the count feeds the size
    threshold that decides whether a change is small enough for one reviewer.
    """
    added = deleted = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            # Only a real hunk header can start here: every line inside a hunk
            # carries a +, - or space first.
            in_hunk = True
            continue
        if line.startswith("diff --"):
            in_hunk = False
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return added, deleted


def _reviewed_files(
    tracked: Sequence[Dict[str, Any]],
    withheld: Sequence[Dict[str, Any]],
    suppressed: Sequence[str],
    untracked: Sequence[str],
) -> List[str]:
    """The files a reviewer is being shown, from git's list rather than ours.

    This was read out of the diff text, by looking for ``+++ b/`` headers.
    Git does not print those for every kind of change: a binary file gets
    "Binary files a/x and b/x differ" and a mode-only change gets nothing but
    ``old mode`` / ``new mode``, so both were missing from the list entirely
    -- which meant a change to three images and one source file counted as
    one file, and could be handed a reduced review panel for being small.

    ``git diff --numstat`` reports all of them, so the list comes from there
    now, less the two kinds of file a reviewer will not see: what was withheld
    as generated, and what is not part of the change at all. Untracked files
    are added back because they are diffed separately, by name, and git's
    listing of tracked changes cannot know about them.
    """
    hidden = {str(entry.get("path")) for entry in withheld}
    hidden.update(str(path) for path in suppressed)
    files: List[str] = []
    for entry in tracked:
        path = str(entry.get("path") or "")
        if path and path not in hidden and path not in files:
            files.append(path)
    for path in untracked:
        if path and path not in files:
            files.append(path)
    return files


class ReviewError(RuntimeError):
    """Raised for review-flow problems that should stop the current stage."""


def design_digest(
    workspace: ws.Workspace,
    plan_path: str,
    request_path: str = "",
) -> "tuple[str, str, str]":
    """The plan, the request it answers, and the sha that identifies the round.

    Reading and hashing are split from writing so a caller can derive the round
    -- and refuse it -- before anything on disk is replaced. A refused round has
    to leave the previous round's frozen plan and its sha exactly as they were:
    the reports already on disk are stamped with that sha, and swapping the plan
    out from under them makes every one of them stale, which discards the triage
    the refusal just told the caller to report.
    """
    plan_text = ws.read_text(plan_path)
    if not plan_text.strip():
        raise ReviewError(
            "no plan to review at %s -- run the architect first" % workspace.relative(plan_path)
        )
    request_text = ws.read_text(request_path) if request_path else ""
    digest = hashlib.sha256(("%s\n\0%s" % (plan_text, request_text)).encode("utf-8")).hexdigest()
    return plan_text, request_text, digest


def write_design_snapshot(
    workspace: ws.Workspace,
    plan_path: str,
    request_path: str,
    plan_text: str,
    digest: str,
) -> Dict[str, Any]:
    """Freeze the plan under review into the scoped ``review-target.md``.

    The plan is an artifact rather than a change, so there is no diff to take
    and git is not involved at all -- a design review works in a directory
    that was never a repository. What replaces the diff's sha is ``digest``,
    a hash of the plan *and* the request it answers: a plan rewritten to
    address findings is a new round, and so is the same plan against a
    different request.

    ``previous_sha`` records what the last round reviewed, and only when this
    round is reviewing something else. The re-review prompt is built from it,
    and a re-run of the identical plan must not claim to be a revision.

    ``round_id`` is new with every call, which is what the sha and the
    timestamp are not: the sha repeats when the same plan is reviewed again,
    and two rounds can start within one second. An approval of the plan is
    given over one round (``approval.design_round``), and only a later call
    here -- never a re-consolidation or a triage -- moves it on. The id is
    written here, before any reviewer runs, and copied into the consolidated
    report once every reviewer of the round has returned (see
    ``build_consolidation``); ``design approve`` only binds to a round
    whose report carries it.
    """
    workspace.ensure()
    ws.write_text(workspace.snapshot_path, plan_text)
    consolidated = ws.read_json(workspace.consolidated_json_path, {}) or {}
    reviewed = str((consolidated.get("snapshot") or {}).get("sha256") or "")
    meta = {
        "generated_at": ws.utcnow(),
        "round_id": uuid.uuid4().hex,
        "strategy": "plan",
        "plan": workspace.relative(plan_path),
        "request": workspace.relative(request_path) if request_path else "",
        # The same key a code snapshot uses, so everything downstream that
        # reports "what was reviewed" needs no second shape to understand.
        "files": [workspace.relative(plan_path)],
        "bytes": len(plan_text.encode("utf-8")),
        "sha256": digest,
        "previous_sha": reviewed if reviewed and reviewed != digest else "",
        # ``--base`` means nothing here, and the round counter keys on it.
        "base": None,
        "empty": False,
    }
    ws.write_json(workspace.snapshot_meta_path, meta)
    return meta


def create_design_snapshot(
    workspace: ws.Workspace,
    plan_path: str,
    request_path: str = "",
) -> Dict[str, Any]:
    """Hash the plan and freeze it in one step, for a caller with no gate."""
    plan_text, _, digest = design_digest(workspace, plan_path, request_path)
    return write_design_snapshot(workspace, plan_path, request_path, plan_text, digest)


# --------------------------------------------------------------------------- fan-out


def render_limits(max_findings: int = DEFAULT_MAX_FINDINGS) -> str:
    """The block that caps what a reviewer writes back.

    ``max_findings`` of 0 lifts the count cap; the rest of the block still
    applies, because a reviewer allowed to report everything is not thereby
    allowed to quote a whole file per finding or to open with a paragraph
    about how thorough it intends to be.

    Both forms open by saying that findings are what to produce. Only the
    capped form used to, and the one run observed returning a prose summary
    instead of finding blocks -- recorded ``unparsed``, and so counted as a
    failed review -- was uncapped. One run is not a cause, and this is not
    offered as the fix for it; but an instruction that is weaker in one mode
    than the other is worth levelling either way.
    """
    lines = ["Limits:"]
    if max_findings > 0:
        lines.append(
            "- Max %d findings. Over that, report the %d worst -- severity first, never padding."
            % (max_findings, max_findings)
        )
    else:
        lines.append("- Report every issue you can point at, one block each. No maximum.")
    lines += [
        "- Evidence: %d lines max. Fix: %d lines max." % (MAX_EVIDENCE_LINES, MAX_FIX_LINES),
        "- Only what you can point at in the code. No speculation. No duplicates.",
        "- Nothing worth fixing: reply with exactly NO_FINDINGS",
        "- Findings or NO_FINDINGS only. No preamble, no summary, no sign-off.",
    ]
    return "\n".join(lines)


class BuiltPrompt(NamedTuple):
    """A reviewer's prompt, and how much of the change it was able to carry.

    The delivery is decided here and recorded nowhere else. It depends on the
    change's length *and* on the settings and flags that shape the round, and
    none of those are known when ``review snapshot`` runs -- so writing it
    into the snapshot's metadata would create a second source of truth that
    disagrees with this one the moment anything changes between the snapshot
    and the run. What the reviewer was actually handed is what this says.
    """

    text: str
    #: "inline" -- the change body is in ``text``; "file" -- only its path is.
    #: "" is reserved for a run that fell over before a prompt was built, and
    #: is read as "not recorded" rather than as either answer.
    delivery: str
    change_chars: int


def default_inline_chars() -> int:
    """The shipped ``review.context.inline_chars``, read where it is written.

    Every default this tool has lives in ``default_config``, and this one is
    read from there rather than copied next to the code that applies it: two
    spellings of the same number are two answers the moment either moves.
    Imported inside the function because ``config`` reaches back into this
    module for ``DEFAULT_EXCLUDE``. A caller holding a configuration passes
    its own number and never comes here.
    """
    from .config import default_config

    return int(default_config()["review"]["context"]["inline_chars"])


def _inline_limit(inline_chars: Optional[int]) -> int:
    """``None`` means the shipped default, as an explicit ``null`` does.

    Every entry point below takes the limit as an optional argument and
    resolves it through here, so "left unset" has one meaning in one place.
    """
    return default_inline_chars() if inline_chars is None else inline_chars


def prompt_delivery(change_text: str, inline_chars: Optional[int] = None) -> str:
    """Whether a change body goes into the prompt or over as a path.

    One function and one number for both paths: the code review measures its
    diff and the design review its plan, and a round that decided delivery
    differently from the round beside it would record two things under one
    name. ``None`` means the shipped default, which is what an explicit
    ``null`` in the configuration means too.
    """
    return _delivery_of(len(change_text), inline_chars)


def _delivery_of(change_chars: int, inline_chars: Optional[int] = None) -> str:
    """The same rule, for a caller that holds a size rather than the text.

    Split out for the refusal below, which has to say what forcing the round
    would get without building a 400,000-character string to ask.
    """
    return "inline" if change_chars <= _inline_limit(inline_chars) else "file"


def over_context(change_chars: int, max_chars: int) -> bool:
    """Whether a change body is past the limit that refuses the round.

    Inclusive on the limit, like ``prompt_delivery``'s: the configured number
    is the largest change that still runs. ``max_chars`` of zero or less is no
    limit, which is what a caller reading a configuration that predates the
    setting ends up with.
    """
    return max_chars > 0 and change_chars > max_chars


def snapshot_chars(workspace: ws.Workspace) -> int:
    """The size the context budget measures: characters of the frozen diff.

    Characters, not the ``bytes`` already in the snapshot's metadata. The
    budget is about how much of a context window the change spends, which is
    what every other size in this module counts -- ``inline_chars``,
    ``Usage.prompt_chars``. A UTF-8 byte count is a different number, and it
    is largest exactly where the difference matters most.
    """
    return len(ws.read_text(workspace.snapshot_path))


def over_budget_note(
    budget_chars: int,
    max_chars: int,
    delivery_chars: int,
    inline_chars: Optional[int] = None,
    design: bool = False,
) -> List[str]:
    """Why a round was refused for size, and what the reader can do about it.

    Three things, in the order they are needed: the size against the limit,
    the ways to make the change smaller, and what ``--force`` would do. The
    middle one differs between the two paths because ``review snapshot`` has
    no ``--design`` form -- ``--base`` and ``review.exclude`` cannot be aimed
    at a plan, and pointing a reader at them would send them nowhere.

    Two sizes, and neither can stand in for the other. ``budget_chars`` is
    what this limit measures -- the plan *and* the request on the design path,
    because both go into every prompt unconditionally -- and it is the number
    the refusal states. ``delivery_chars`` is the body that would actually be
    handed over, the plan alone there, and only it can say what forcing would
    do. On the code path they are the same number and are still passed
    separately, so that every call site reads the same.
    """
    if design:
        narrow = (
            "Shorten it: the plan and the request it answers are measured together, and "
            "both go into every reviewer's prompt."
        )
    else:
        narrow = (
            "Narrow it: --base <rev>, review.exclude, or split the change and review the "
            "parts, then take the snapshot again."
        )
    return [
        "refusing to review a change body of %s chars: the limit is %s "
        "(review.context.max_chars)." % ("{:,}".format(budget_chars), "{:,}".format(max_chars)),
        narrow,
        "Nothing was reviewed, and in an automated workflow that is the answer to report: "
        "this change was not reviewed. Saying so is the point of the limit -- the "
        "alternative is calling an incomplete review complete.",
        "--force runs it anyway. It belongs to a human who has decided to pay for the "
        "round, not to the orchestrator. %s" % _forced_consequence(delivery_chars, inline_chars),
    ]


def _forced_consequence(delivery_chars: int, inline_chars: Optional[int] = None) -> str:
    """What forcing *this* round would record -- not what forcing records.

    Whether the reviewers come back ``partial`` is decided by
    ``prompt_delivery`` and ``review.context.inline_chars``, never by this
    budget. The shipped defaults make the two limits equal, so a change over
    ``max_chars`` is over the inline limit too and every reviewer is partial
    -- but that is a fact about *this* configuration, not about this code.
    ``inline_chars`` raised above ``max_chars``, or ``max_chars`` lowered
    below it, refuses rounds whose body would still have gone into the prompt
    whole, and promising partial there would be a promise this code does not
    keep. So it is computed, both ways, from the numbers actually in force.

    The size handed in is the one ``prompt_delivery`` decides on -- the plan
    alone on the design path, where the budget counted the request too. A
    plan that fits inline is delivered inline however far the pair went over.
    """
    limit = "{:,}".format(_inline_limit(inline_chars))
    if _delivery_of(delivery_chars, inline_chars) == "file":
        return (
            "The round is then recorded as over budget, and every reviewer comes back "
            "partial: the body is over review.context.inline_chars (%s), so it goes over "
            "as a file." % limit
        )
    return (
        "The round is then recorded as over budget. The body still fits in the prompt "
        "(review.context.inline_chars is %s), so it is not partial for that reason." % limit
    )


def _handover_note(change_chars: int, inline_chars: Optional[int] = None) -> str:
    """What a reviewer handed a path instead of a body is told.

    It states a fact and asks for nothing back. An earlier design had the
    prompt ask for a ``PARTIAL_REVIEW`` declaration, which fails three ways:
    a declaration cannot be tested for the case where it was *not* made,
    ``{limits}`` is the last thing in both templates and its "Findings or
    NO_FINDINGS only" overrides anything asked before it, and a reviewer's
    self-report is the one thing about a round this tool cannot check. The
    verdict comes from this tool's own inputs instead; this only makes sure
    the reviewer is not surprised by it.
    """
    return (
        "The change body is {:,} chars and is handed over as a file because it exceeds "
        "review.context.inline_chars ({:,}). This review is recorded as coverage-unverified "
        "whatever you answer. Read the whole file before judging; a paging tool needs "
        "more than one call.".format(change_chars, _inline_limit(inline_chars))
    )


def coverage_unverified_error(change_chars: int, inline_chars: Optional[int] = None) -> str:
    """The reason a ``partial`` run carries. Its findings are still real.

    It names the limit as well as the size: the limit is configuration now,
    so "over the inline limit" on its own no longer tells a reader which
    number this round was measured against.
    """
    return (
        "coverage unverified: the change body ({:,} chars) was handed over as a file, "
        "not inlined (over review.context.inline_chars, {:,}). Findings kept; "
        "not a clean review.".format(change_chars, _inline_limit(inline_chars))
    )


def build_review_prompt(
    reviewer: Dict[str, Any],
    workspace: ws.Workspace,
    diff_text: str,
    extra_context: str = "",
    template: Optional[str] = None,
    max_findings: int = DEFAULT_MAX_FINDINGS,
    inline_chars: Optional[int] = None,
) -> BuiltPrompt:
    role = str(reviewer.get("role") or "general")
    guidance = ROLE_GUIDANCE.get(
        role,
        "Review as a %s specialist. Concrete, evidence-backed issues in that perspective only." % role,
    )
    delivery = prompt_delivery(diff_text, inline_chars)
    if delivery == "inline":
        diff_section = "```diff\n%s\n```" % diff_text.rstrip()
    else:
        diff_section = (
            "Diff too large to inline. Read it from this file, frozen for this review:\n\n"
            "    %s\n\nReview only what that diff contains." % workspace.relative(workspace.snapshot_path)
        )
        diff_section += "\n\n" + _handover_note(len(diff_text), inline_chars)
    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
    for note in (render_withheld(meta.get("withheld") or []), render_round_context(workspace, meta)):
        if note:
            diff_section += "\n\n" + note
    prompt = (template or REVIEW_PROMPT_TEMPLATE).format(
        reviewer_id=reviewer.get("id", "reviewer"),
        role=role,
        role_guidance=guidance,
        root=workspace.root,
        diff_section=diff_section,
        limits=render_limits(max_findings),
    )
    if extra_context.strip():
        prompt += "\n## Additional context\n\n%s\n" % extra_context.strip()
    return BuiltPrompt(prompt, delivery, len(diff_text))


def build_design_review_prompt(
    reviewer: Dict[str, Any],
    workspace: ws.Workspace,
    plan_text: str,
    request_text: str = "",
    extra_context: str = "",
    max_findings: int = DEFAULT_MAX_FINDINGS,
    inline_chars: Optional[int] = None,
) -> BuiltPrompt:
    """The reviewer prompt for a plan, built from the frozen design snapshot.

    The change body here is the plan -- that is what the round is reviewing
    and what ``review-target.md`` freezes. The request it answers is context
    that rides along, and goes in whatever its size, exactly as it always has.
    """
    role = str(reviewer.get("role") or "general")
    guidance = DESIGN_ROLE_GUIDANCE.get(
        role,
        "Review the plan as a %s specialist. Concrete, evidence-backed issues in that "
        "perspective only." % role,
    )
    delivery = prompt_delivery(plan_text, inline_chars)
    if delivery == "inline":
        plan_section = "```markdown\n%s\n```" % plan_text.rstrip()
    else:
        plan_section = (
            "Plan too large to inline. Read it from this file, frozen for this review:\n\n"
            "    %s\n\nReview only what that plan contains." % workspace.relative(workspace.snapshot_path)
        )
        plan_section += "\n\n" + _handover_note(len(plan_text), inline_chars)
    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
    note = render_design_round_context(workspace, meta)
    if note:
        plan_section += "\n\n" + note
    request_section = (
        "```markdown\n%s\n```" % request_text.rstrip()
        if request_text.strip()
        else "Not recorded. Judge the plan against its own stated goal."
    )
    prompt = DESIGN_REVIEW_PROMPT_TEMPLATE.format(
        reviewer_id=reviewer.get("id", "reviewer"),
        role=role,
        role_guidance=guidance,
        root=workspace.root,
        request_section=request_section,
        plan_section=plan_section,
        limits=render_limits(max_findings),
    )
    if extra_context.strip():
        prompt += "\n## Additional context\n\n%s\n" % extra_context.strip()
    return BuiltPrompt(prompt, delivery, len(plan_text))


class ReviewerRun:
    def __init__(
        self,
        reviewer: Dict[str, Any],
        status: str,
        report_path: Optional[str] = None,
        error: str = "",
        model_display: str = "",
        duration: float = 0.0,
        findings: int = 0,
        usage: Optional[Usage] = None,
        invoked: bool = False,
        delivery: str = "",
        change_chars: int = 0,
        inline_chars: int = 0,
        snapshot: str = "",
        over_budget: bool = False,
        budget_chars: int = 0,
    ) -> None:
        self.reviewer = reviewer
        # ok | partial | failed | stalled | unparsed. Only "ok" counts as a
        # delivered review; "unparsed" means the CLI succeeded but its report
        # could not be read, and "partial" that the change body went over as a
        # file rather than in the prompt. Neither is ever a clean one.
        self.status = status
        self.report_path = report_path
        self.error = error
        self.model_display = model_display
        self.duration = duration
        self.findings = findings
        #: What this reviewer cost. Reviewers are the most duplicated stage in
        #: the pipeline -- the same diff, once per reviewer, once per round --
        #: so their cost is recorded per reviewer, not just per round.
        self.usage = usage or Usage()
        #: Whether a CLI was started. Defaults to False because the paths that
        #: construct a run without one -- an unknown provider, a model that
        #: will not resolve -- are exactly the ones that spent nothing.
        self.invoked = invoked
        #: How much of the change this reviewer was handed: see ``BuiltPrompt``.
        #: "" for a run that fell over before there was a prompt to build.
        self.delivery = delivery
        self.change_chars = change_chars
        #: What ``review.context.inline_chars`` was when ``delivery`` above was
        #: decided. Recorded for the reason ``budget_chars`` is: the limit is
        #: configuration and the shipped default is not the only answer, so a
        #: reader of a ``partial`` round who has only the size cannot tell
        #: whether it was a large change or a low limit that made it one.
        #: 0 for a run that fell over before there was a prompt to build.
        self.inline_chars = inline_chars
        #: Which snapshot this run was handed, stamped the way the reports are
        #: stamped and for the same reason: the reviewer table outlives the
        #: round it was written in, so an entry has to say what it answers for.
        #: "" for a run that fell over before there was a prompt to build.
        self.snapshot = snapshot
        #: Whether this round was sent past ``review.context.max_chars`` by
        #: ``--force``. Recorded here, and in the consolidation derived from
        #: here, for the reason ``BuiltPrompt`` gives about delivery: the
        #: limit and the flag belong to the run, not to the frozen file.
        self.over_budget = over_budget
        #: What ``review.context.max_chars`` measured for this round, which is
        #: not always ``change_chars``: on the design path the budget counts
        #: the plan and the request together while the body handed over is the
        #: plan alone. Recorded so the report can print the number the refusal
        #: would have used rather than the one delivery was decided by.
        self.budget_chars = budget_chars

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.reviewer.get("id"),
            "provider": self.reviewer.get("provider"),
            "role": self.reviewer.get("role", "general"),
            "model": self.model_display,
            "status": self.status,
            "error": self.error,
            "duration_seconds": round(self.duration, 2),
            "findings": self.findings,
            "report": self.report_path,
            "invoked": self.invoked,
            "delivery": self.delivery,
            "change_chars": self.change_chars,
            "inline_chars": self.inline_chars,
            "snapshot": self.snapshot,
            "over_budget": self.over_budget,
            "budget_chars": self.budget_chars,
            "usage": self.usage.to_dict(),
        }


def run_reviews(
    reviewers: Sequence[Dict[str, Any]],
    workspace: ws.Workspace,
    parallel: bool = True,
    timeout: int = 1800,
    extra_context: str = "",
    template: Optional[str] = None,
    idle_timeout: Optional[float] = None,
    max_findings: int = DEFAULT_MAX_FINDINGS,
    prompt_for: Optional[Callable[[Dict[str, Any]], BuiltPrompt]] = None,
    over_budget: bool = False,
    budget_chars: int = 0,
    inline_chars: Optional[int] = None,
) -> List[ReviewerRun]:
    """Run every configured reviewer against the frozen snapshot.

    ``prompt_for`` replaces the code-review prompt with one built per
    reviewer, which is how the design review reuses this whole function --
    the parallelism, the read-only mode, the tolerance of one failure and the
    ``unparsed`` verdict are properties of the fan-out, not of the diff. It
    returns a ``BuiltPrompt`` for the same reason the code path does: the
    round's coverage is decided from what each prompt actually carried.

    ``over_budget`` is the caller's answer to "was this round only running
    because a human forced it past ``review.context.max_chars``". It is asked
    for rather than worked out here because the limit is configuration and the
    flag is an argument, and neither is visible from the snapshot.
    ``budget_chars`` is the size that answer was decided on, for the same
    reason: on the design path the budget measured the plan and the request
    together, and only the caller knows the pair.

    ``inline_chars`` is ``review.context.inline_chars``. It builds the
    code-review prompt, is recorded on every entry, and words the ``partial``
    verdict -- and when ``prompt_for`` builds the prompt instead, it must be
    the number that callable was given, or the round would be measured
    against one limit and report another.
    """
    if not reviewers:
        return []
    diff_text = ws.read_text(workspace.snapshot_path)
    if not diff_text.strip():
        if prompt_for is not None:
            raise ReviewError("nothing to review at %s" % workspace.relative(workspace.snapshot_path))
        meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
        if meta.get("withheld"):
            raise ReviewError(
                "review snapshot is empty because every changed file was withheld as "
                "generated or vendored (%s). Re-snapshot with --no-exclude to review them."
                % ", ".join(str(entry.get("path")) for entry in meta["withheld"][:5])
            )
        raise ReviewError(
            "review snapshot is empty -- run `review snapshot` after making changes, "
            "or pass --base to compare against a different revision"
        )

    # Read once, before the fan-out: every run in this round answers for the
    # same snapshot, and its entry and its report are stamped with it alike.
    stamp = _snapshot_sha(workspace)
    # Resolved once too, and for the same reason: every entry of this round
    # records the limit it was measured against, and they must all record one.
    limit = _inline_limit(inline_chars)

    def run_one(reviewer: Dict[str, Any]) -> ReviewerRun:
        reviewer_id = str(reviewer.get("id") or "reviewer")
        try:
            provider = get_provider(str(reviewer.get("provider")))
        except Exception as exc:
            return ReviewerRun(reviewer, "failed", error=str(exc))
        if prompt_for is not None:
            built = prompt_for(reviewer)
        else:
            built = build_review_prompt(
                reviewer, workspace, diff_text, extra_context, template, max_findings, limit
            )
        # Every run from here on knows what it was handed, and which snapshot it
        # was handed, failures included: a round is judged on what it sent, not
        # on what came back, and the entry has to say which round that was.
        carried = {
            "delivery": built.delivery,
            "change_chars": built.change_chars,
            "inline_chars": limit,
            "snapshot": stamp,
            "over_budget": over_budget,
            "budget_chars": budget_chars,
        }
        try:
            result = provider.run(
                built.text,
                MODE_REVIEW,
                workspace.root,
                reviewer.get("model"),
                timeout=timeout,
                options=reviewer.get("options"),
                idle_timeout=idle_timeout,
            )
        except ModelResolutionError as exc:
            return ReviewerRun(reviewer, "failed", error=str(exc), **carried)
        except Exception as exc:
            # No duration, deliberately. Everything that reaches here was
            # raised before ``provider.run`` had a ``RunResult`` to hand back
            # -- ``detect``, a non-``ModelResolutionError`` from
            # ``resolve_model``, ``build_command``, ``_child_env`` -- so no
            # child was started, or none whose lifetime we were told. Reading
            # the output is no longer one of these: an adapter that raises
            # while parsing returns a measured failure instead, which lands in
            # the ``not result.ok`` branch below with its duration intact. What
            # cannot be known is not billed, and ``invoked`` stays False.
            return ReviewerRun(reviewer, "failed", error="%s: %s" % (type(exc).__name__, exc), **carried)

        model_display = result.resolved.display if result.resolved else ""
        if not result.ok:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            if result.stalled:
                status, error = "stalled", "no output for %.0fs; treated as wedged" % result.idle_for
            elif result.timed_out:
                status, error = "stalled", "hit its %.0fs deadline" % result.duration
            else:
                status = "failed"
                error = detail[-1] if detail else "exit code %s" % result.exit_code
            return ReviewerRun(
                reviewer,
                status,
                error=error,
                model_display=model_display,
                duration=result.duration,
                # A failed review is not a free one: whatever it burned before
                # falling over still has to appear in the account.
                usage=result.usage,
                invoked=result.invoked,
                **carried,
            )
        body = result.stdout.strip() or "NO_FINDINGS"
        header = (
            "# Review\n\n"
            "- Reviewer: %s\n- Provider: %s\n- Model: %s\n- Role: %s\n- Snapshot: %s\n\n---\n\n"
            % (
                reviewer_id,
                reviewer.get("provider"),
                model_display or "unknown",
                reviewer.get("role", "general"),
                stamp,
            )
        )
        path = workspace.reviewer_report_path(reviewer_id)
        ws.write_text(path, header + body + "\n")
        findings = parse_findings(body, reviewer_id)
        warning = unparsed_report_warning(body, findings)
        # "unparsed" wins: both are not-ok, but a report nobody can read is
        # the more specific fact about this run, and the one that says the
        # delegated cost bought nothing at all.
        if warning:
            status, error = "unparsed", warning
        elif built.delivery == "file":
            status, error = "partial", coverage_unverified_error(built.change_chars, limit)
        else:
            status, error = "ok", ""
        return ReviewerRun(
            reviewer,
            status,
            report_path=workspace.relative(path),
            error=error,
            model_display=model_display,
            duration=result.duration,
            findings=len(findings),
            usage=result.usage,
            invoked=result.invoked,
            **carried,
        )

    if parallel and len(reviewers) > 1:
        with ThreadPoolExecutor(max_workers=min(len(reviewers), 8)) as pool:
            return list(pool.map(run_one, reviewers))
    return [run_one(reviewer) for reviewer in reviewers]


def _snapshot_sha(workspace: ws.Workspace) -> str:
    return _stamp(ws.read_json(workspace.snapshot_meta_path, {}) or {})


def _stamp(meta: Dict[str, Any]) -> str:
    """A snapshot's stamp, in the one form everything that records one uses.

    Report headers, ``current_snapshot_stamp`` and the reviewer entries all go
    through here, so "this was written against that snapshot" is one comparison
    and not three spellings of it.
    """
    return str(meta.get("sha256", ""))[:12] or "unknown"


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
    for raw_line in lines:
        # Models bold the labels in several ways (``**Severity:** high`` and
        # ``**Severity**: high``); dropping the emphasis normalises all of them.
        line = raw_line.replace("**", "")
        match = _FIELD_RE.match(line)
        if match:
            raw_key = match.group(1).lower()
            key = _FIELD_ALIASES.get(raw_key, raw_key)
            fields.setdefault(key, []).append(match.group(2).strip())
            continue
        if key and line.strip() and not line.strip().startswith("#"):
            fields[key].append(line.strip())
    if not fields:
        return None
    finding: Dict[str, Any] = {
        "severity": _normalise_severity(_join(fields.get("severity"))),
        "file": _normalise_path(_join(fields.get("file"))),
        "line": _join(fields.get("line")) or "n/a",
        "category": (_join(fields.get("category")) or "general").lower(),
        "problem": _join(fields.get("problem")),
        "impact": _join(fields.get("impact")),
        "evidence": _join(fields.get("evidence")),
        "recommended_fix": _join(fields.get("recommended_fix")),
    }
    if not finding["problem"] and not finding["evidence"]:
        return None
    return finding


def _join(values: Optional[List[str]]) -> str:
    if not values:
        return ""
    return " ".join(part for part in (value.strip() for value in values) if part).strip()


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
    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
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
        current_sha = str((ws.read_json(workspace.snapshot_meta_path, {}) or {}).get("sha256") or "")
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
) -> Dict[str, Any]:
    """The round's report: consolidated findings, counts, and its coverage.

    ``completed_round`` is the design round whose reviewers have all returned,
    passed only by the caller that ran them. Without it the report keeps the
    round the previous report named: the snapshot's metadata names a round as
    soon as it starts, and a re-consolidation during it -- or after it failed
    -- must not claim findings nobody has reported yet.

    ``runs`` is the reviewer table, which by design holds entries that did not
    run this round -- see ``_merge_runs``. Coverage, ``snapshot.over_budget``
    and the ``snapshot_`` reviewer counts are all derived from the subset
    stamped with this snapshot, so that one report describes one snapshot; see
    ``_coverage`` for what the two coverage values mean, why one round's answer
    is not the whole change's, and ``coverage_lineage`` for the key the whole
    change's answer carries on.
    """
    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
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
    # The design round these findings belong to: the last one that got as far
    # as a report, which is what an approval is given over.
    round_id = completed_round or (last.get("snapshot") or {}).get("round_id")
    if round_id:
        snapshot["round_id"] = round_id
    return {
        "generated_at": ws.utcnow(),
        "iteration": iteration,
        #: Which review this round belongs to. Recorded so the next round can
        #: tell a re-review from an unrelated change that happens to share a
        #: project directory.
        "lineage": lineage,
        "snapshot": snapshot,
        "coverage": _coverage(runs, meta, iteration, lineage, last),
        "reviewers": list(runs),
        "counts": counts,
        "duplicate_candidates": candidates,
        "findings": consolidated,
    }


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
        size = "{:,}".format(chars) if chars else "size unrecorded"
        # The limit rides along when the round recorded one, because it is
        # configuration: "handed over as a file" says nothing on its own once
        # the reader cannot assume which number decided that.
        limit = coverage.get("inline_chars")
        against = ""
        if isinstance(limit, int) and limit:
            against = " (review.context.inline_chars %s)" % "{:,}".format(limit)
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
        "{:,}".format(chars) if chars else "size unrecorded"
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
            return data
    raise ReviewError("no finding with id %r" % finding_id)


def accepted_findings(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [f for f in data.get("findings", []) if f.get("triage") == "accepted"]


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
