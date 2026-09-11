"""Independent multi-model review: snapshot, fan-out, parse, consolidate, triage.

Design rules enforced here:

* every reviewer sees the *same frozen snapshot*, taken before any reviewer runs
* reviewers never see each other's output -- no cross-contamination
* reviewers run read-only; a reviewer that edits files is a configuration bug
* one reviewer failing does not fail the batch

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
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import workspace as ws
from .providers import MODE_REVIEW, ModelResolutionError, Usage, get_provider

SEVERITIES = ("critical", "high", "medium", "low")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}
TRIAGE_STATUSES = ("accepted", "rejected", "duplicate", "needs-investigation", "needs-triage")

#: Inline the diff up to this size; beyond it, reviewers read the file instead.
MAX_INLINE_DIFF_CHARS = 120_000

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
        "Prioritise, in this order: correctness bugs, regressions, missing edge cases, "
        "security problems, performance problems, data consistency, concurrency issues, "
        "error handling, and insufficient tests. Also flag unnecessary complexity. "
        "Style-only observations are low severity at most, and usually not worth reporting."
    ),
    "security": (
        "Focus on authentication and authorisation gaps, injection (SQL/command/template), "
        "unsafe deserialisation, SSRF, path traversal, secret handling and leakage, unsafe "
        "defaults, missing input validation, and access-control regressions in changed code paths."
    ),
    "performance": (
        "Focus on algorithmic complexity, N+1 queries, missing indexes, unnecessary I/O, "
        "unbounded memory growth, blocking calls on hot paths, and cache invalidation mistakes. "
        "Quantify the cost where the diff gives you enough information."
    ),
    "test": (
        "Focus on test coverage of the changed behaviour: missing edge cases, tests that assert "
        "nothing meaningful, flaky patterns (time, ordering, network), and untested error paths. "
        "Name the specific case that is missing, not just 'add more tests'."
    ),
    "architecture": (
        "Focus on layering violations, misplaced responsibilities, leaky abstractions, coupling "
        "introduced by the change, public API/contract shape, and whether the change fits the "
        "conventions already present in this codebase."
    ),
    "database": (
        "Focus on schema changes, migration safety (locking, backfills, reversibility), "
        "nullability and constraint changes, index coverage for new queries, transaction "
        "boundaries, and data-consistency risk during deploy."
    ),
    "frontend": (
        "Focus on component state handling, rendering performance, accessibility (roles, labels, "
        "keyboard and focus behaviour), responsive layout, error and loading states, and "
        "client-side validation that is not mirrored server-side."
    ),
    "backend": (
        "Focus on API contracts and compatibility, validation, error responses and status codes, "
        "idempotency, transactional integrity, background job semantics, and observability of "
        "the changed paths."
    ),
}

REVIEW_PROMPT_TEMPLATE = """You are an independent code reviewer.

Reviewer id: {reviewer_id}
Review role: {role}
Repository root: {root}

## Your task

Review ONLY the change described by the snapshot below. Judge it on its merits.
You are read-only: do not modify, create, or delete any file. Do not run
commands that mutate the repository or the network.

You may read any file in the repository to understand context.

{role_guidance}

## Change under review

{diff_section}

## Required output format

Report every issue as a block in exactly this format:

## Finding
- Severity: critical | high | medium | low
- File: <path relative to the repository root>
- Line: <line number or range, or "n/a">
- Category: <short category, e.g. correctness, security, performance, tests>
- Problem: <what is wrong, one or two sentences>
- Impact: <what breaks, and under what conditions>
- Evidence: <the specific code or diff hunk that shows it>
- Recommended fix: <concrete change you would make>

Rules:
- Report only issues you can point at in the code. Do not speculate.
- Do not report the same issue twice.
- If the change is sound and you find nothing worth fixing, reply with exactly:

NO_FINDINGS

Output nothing except findings (or NO_FINDINGS). No preamble, no summary.
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
    # remembered to ignore is neither cheap nor invisible. ``--base`` and
    # ``incremental: False`` both say this round is not going to use it, so it
    # is not written; the round after finds no tree and takes the whole change,
    # which is the safe direction to fall back in.
    wanted = incremental and not base
    tree = _write_tree(root) if wanted else ""
    previous_tree = _reviewed_tree(workspace) if wanted else ""
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
        "files": _changed_files(diff),
        "untracked_included": untracked,
        "withheld": sorted(withheld, key=lambda entry: str(entry.get("path"))),
        "exclude_patterns": patterns,
        "bytes": len(diff.encode("utf-8")),
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


def _reviewed_tree(workspace: ws.Workspace) -> str:
    """The tree of the previous round, if narrowing to it is safe.

    Two conditions, and both are about the reviewer rather than the cost.

    The previous snapshot has to have been reviewed. Diffing against one
    nobody reviewed would answer a question no round asked, and would turn an
    ordinary re-snapshot -- taken because a reviewer failed, say -- into an
    empty diff.

    And that review has to have produced something the fix was answering.
    Scope is only narrowed together with the premise that explains it, so a
    round following a clean review, or one whose findings were all rejected,
    takes the whole change: there is no fix to check, and a fragment with
    nothing to judge it against is the failure this feature exists to avoid.
    """
    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
    tree = str(meta.get("tree") or "")
    if not tree:
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
    lines = [
        "This is a re-review. The diff above is only what changed since the previous "
        "round -- the fix, not the whole change."
    ]
    if meta.get("full_diff"):
        lines.append("The whole change is frozen at %s; read it if you need the context." % meta["full_diff"])
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
            "Say whether each is actually fixed, and report any new problem the fix "
            "introduced. Do not assume a listed item was real."
        )
    return "\n".join(lines)


def render_withheld(withheld: Sequence[Dict[str, Any]]) -> str:
    """The note that tells a reviewer what it is not being shown.

    Terse on purpose: this rides along on every reviewer prompt in every round,
    so it is a list of names and sizes, not an explanation.
    """
    if not withheld:
        return ""
    lines = ["Changed but withheld as generated or vendored -- diffs not shown:"]
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
    for added, deleted, path in _parse_numstat(out):
        entries.append(_withheld_entry(path, withholds(path, patterns), added, deleted))
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


def _parse_numstat(out: str) -> "List[tuple[Optional[int], Optional[int], str]]":
    """Parse ``git diff --numstat -z``.

    NUL-separated because a rename is reported as an empty path followed by the
    old and new names as their own records, and the readable form spells the
    same thing as ``src/{old => new}.txt`` -- which would have to be unpicked,
    and unpicked wrongly for any path containing a brace. Binary files carry
    ``-`` instead of a count, which is not zero and is not reported as zero.
    """
    fields = out.split("\0")
    records: "List[tuple[Optional[int], Optional[int], str]]" = []
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
        if not path:
            # A rename or copy: the next two records are the old and new names.
            old = fields[index] if index < len(fields) else ""
            new = fields[index + 1] if index + 1 < len(fields) else ""
            index += 2
            path = new or old
        if not path:
            continue
        records.append((_maybe_int(added), _maybe_int(deleted), path))
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


def _changed_files(diff: str) -> List[str]:
    files: List[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            name = line[6:].strip()
            if name and name != "/dev/null" and name not in files:
                files.append(name)
    return files


class ReviewError(RuntimeError):
    """Raised for review-flow problems that should stop the current stage."""


# --------------------------------------------------------------------------- fan-out


def build_review_prompt(
    reviewer: Dict[str, Any],
    workspace: ws.Workspace,
    diff_text: str,
    extra_context: str = "",
    template: Optional[str] = None,
) -> str:
    role = str(reviewer.get("role") or "general")
    guidance = ROLE_GUIDANCE.get(
        role,
        "Review the change from the perspective of a %s specialist. Report only concrete, "
        "evidence-backed issues within that perspective." % role,
    )
    if len(diff_text) <= MAX_INLINE_DIFF_CHARS:
        diff_section = "```diff\n%s\n```" % diff_text.rstrip()
    else:
        diff_section = (
            "The diff is too large to inline. Read it from this file, which is frozen "
            "for the duration of this review:\n\n    %s\n\nReview only what that diff contains."
            % workspace.relative(workspace.snapshot_path)
        )
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
    )
    if extra_context.strip():
        prompt += "\n## Additional context from the orchestrator\n\n%s\n" % extra_context.strip()
    return prompt


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
    ) -> None:
        self.reviewer = reviewer
        # ok | failed | stalled | unparsed. Only "ok" counts as a delivered
        # review; "unparsed" means the CLI succeeded but its report could not
        # be read, which is a failed review and never a clean one.
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
) -> List[ReviewerRun]:
    """Run every configured reviewer against the frozen snapshot."""
    if not reviewers:
        return []
    diff_text = ws.read_text(workspace.snapshot_path)
    if not diff_text.strip():
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

    def run_one(reviewer: Dict[str, Any]) -> ReviewerRun:
        reviewer_id = str(reviewer.get("id") or "reviewer")
        try:
            provider = get_provider(str(reviewer.get("provider")))
        except Exception as exc:
            return ReviewerRun(reviewer, "failed", error=str(exc))
        prompt = build_review_prompt(reviewer, workspace, diff_text, extra_context, template)
        try:
            result = provider.run(
                prompt,
                MODE_REVIEW,
                workspace.root,
                reviewer.get("model"),
                timeout=timeout,
                options=reviewer.get("options"),
                idle_timeout=idle_timeout,
            )
        except ModelResolutionError as exc:
            return ReviewerRun(reviewer, "failed", error=str(exc))
        except Exception as exc:
            return ReviewerRun(reviewer, "failed", error="%s: %s" % (type(exc).__name__, exc))

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
                _snapshot_sha(workspace),
            )
        )
        path = workspace.reviewer_report_path(reviewer_id)
        ws.write_text(path, header + body + "\n")
        findings = parse_findings(body, reviewer_id)
        warning = unparsed_report_warning(body, findings)
        return ReviewerRun(
            reviewer,
            "unparsed" if warning else "ok",
            report_path=workspace.relative(path),
            error=warning,
            model_display=model_display,
            duration=result.duration,
            findings=len(findings),
            usage=result.usage,
            invoked=result.invoked,
        )

    if parallel and len(reviewers) > 1:
        with ThreadPoolExecutor(max_workers=min(len(reviewers), 8)) as pool:
            return list(pool.map(run_one, reviewers))
    return [run_one(reviewer) for reviewer in reviewers]


def _snapshot_sha(workspace: ws.Workspace) -> str:
    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
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


def next_iteration(workspace: ws.Workspace) -> int:
    """Derive the review round from what is on disk.

    The iteration budget only stops a review->fix->re-review loop if the counter
    actually advances, so it must not depend on the caller passing a number: a
    new snapshot is a new round, and re-running against the same snapshot (after
    a reviewer failed, say) stays in the current one.
    """
    previous = ws.read_json(workspace.consolidated_json_path, {}) or {}
    if not previous:
        return 1
    recorded = int(previous.get("iteration", 0) or 0)
    previous_sha = str((previous.get("snapshot") or {}).get("sha256") or "")
    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
    current_sha = str(meta.get("sha256") or "")
    if previous_sha and current_sha and previous_sha == current_sha:
        return max(recorded, 1)
    return recorded + 1


def build_consolidation(
    workspace: ws.Workspace,
    runs: Sequence[Dict[str, Any]],
    findings: Sequence[Dict[str, Any]],
    iteration: int = 1,
) -> Dict[str, Any]:
    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
    # Keyed by content, never by id: ids are positional (F1..Fn, severity
    # ordered) and get reassigned every round, so an id-keyed lookup silently
    # drops a decision -- or applies it to a different finding -- as soon as the
    # finding set changes, which is exactly what fixing things does.
    previous = {
        finding_key(entry): entry
        for entry in (ws.read_json(workspace.consolidated_json_path, {}) or {}).get("findings", [])
    }
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
    counts = _counts(consolidated, runs)
    counts["duplicate_candidates"] = len(candidates)
    return {
        "generated_at": ws.utcnow(),
        "iteration": iteration,
        "snapshot": {"sha256": meta.get("sha256"), "files": meta.get("files", [])},
        "reviewers": list(runs),
        "counts": counts,
        "duplicate_candidates": candidates,
        "findings": consolidated,
    }


def _counts(findings: Sequence[Dict[str, Any]], runs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    by_severity = dict.fromkeys(SEVERITIES, 0)
    for finding in findings:
        by_severity[finding.get("severity", "medium")] = (
            by_severity.get(finding.get("severity", "medium"), 0) + 1
        )
    return {
        "reviewers_total": len(runs),
        "reviewers_ok": sum(1 for run in runs if run.get("status") == "ok"),
        "reviewers_failed": sum(1 for run in runs if run.get("status") != "ok"),
        "findings_total": len(findings),
        "by_severity": by_severity,
        "duplicates_merged": sum(max(0, f.get("duplicate_count", 1) - 1) for f in findings),
    }


def render_consolidation(data: Dict[str, Any]) -> str:
    counts = data.get("counts", {})
    lines = [
        "# Consolidated review",
        "",
        "- Generated: %s" % data.get("generated_at"),
        "- Iteration: %s" % data.get("iteration"),
        "- Snapshot: %s" % str(data.get("snapshot", {}).get("sha256", ""))[:12],
        "- Reviewers: %s ok / %s total" % (counts.get("reviewers_ok"), counts.get("reviewers_total")),
        "- Findings: %s (%s duplicate report(s) merged)"
        % (counts.get("findings_total"), counts.get("duplicates_merged")),
        "",
        "## Reviewers",
        "",
    ]
    for run in data.get("reviewers", []):
        mark = "ok" if run.get("status") == "ok" else "FAILED"
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


def render_fix_brief(data: Dict[str, Any]) -> str:
    """The prompt payload handed to the Review Fixer: accepted findings only."""
    findings = accepted_findings(data)
    if not findings:
        return "No accepted findings. Nothing to fix.\n"
    lines = ["# Accepted review findings to fix", ""]
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
            "- Category: %s" % finding.get("category", "general"),
            "- Reported by: %s" % ", ".join(finding.get("reported_by", [])),
            "- Problem: %s" % finding.get("problem", ""),
            "- Impact: %s" % finding.get("impact", ""),
            "- Evidence: %s" % finding.get("evidence", ""),
            "- Recommended fix: %s" % finding.get("recommended_fix", ""),
            "",
        ]
    return "\n".join(lines)


def summarise_runs(runs: Sequence[ReviewerRun]) -> Tuple[int, int]:
    ok = sum(1 for run in runs if run.status == "ok")
    return ok, len(runs) - ok
