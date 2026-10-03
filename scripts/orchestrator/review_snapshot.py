"""Freezing the change under review: the diff, its exclusions, and the round's context."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import posixpath
import re
import tempfile
import uuid
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from . import context as context_mod
from . import workspace as ws
from .config import PROJECT_CONFIG_NAMES
from .review_common import DEFAULT_EXCLUDE, accepted_findings

# --------------------------------------------------------------------------- snapshot


def create_snapshot(
    workspace: ws.Workspace,
    base: Optional[str] = None,
    include_untracked: bool = True,
    exclude: Optional[Sequence[str]] = None,
    incremental: bool = True,
    surrounding: str = "none",
) -> Dict[str, Any]:
    """Freeze the change under review into ``.ai/reviews/review-target.diff``.

    ``exclude`` withholds the *body* of a generated or vendored file's diff
    while still recording that it changed; pass ``()`` to take everything.

    ``incremental`` lets a second round diff against the previous round's
    snapshot instead of against ``HEAD``, so re-review sees the fix rather than
    the whole change again. It only applies when the previous snapshot was
    actually reviewed and something has changed since; a plain re-snapshot is
    always the full diff.

    ``surrounding`` is ``review.context.surrounding``. With ``enclosing`` the
    symbol around every hunk is extracted from the tree written before the
    diff and frozen beside it in ``review-surrounding.json``.
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
    context_on = context_mod.surrounding_mode(surrounding) == "enclosing"
    # Written before the diff whenever context is on, incremental or not: only
    # then does "the working tree still matches this tree" imply "and the diff
    # taken after it". ``meta["tree"]`` keeps its meaning either way -- it is
    # what the next round may narrow from, and ``incremental: False`` says none.
    frozen_tree = _write_tree(root) if (wanted or context_on) else ""
    tree = frozen_tree if wanted else ""
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
    changed_paths = _touched_paths(reviewed, tracked, withheld, untracked, skip=set())
    # A third list, for who joins the panel: a path-scoped reviewer is matched
    # against the change as a reviewer sees it, the reviewed and withheld
    # files and rename sources, but not what is suppressed. A suppressed file
    # is in neither the diff nor the withheld notice, so a reviewer added for
    # it would be handed a diff with no trace of what summoned it. Suppression
    # happens only on an incremental round; on a first round the two lists
    # are the same.
    condition_paths = _touched_paths(reviewed, tracked, withheld, untracked, skip=set(suppressed))
    meta = {
        "generated_at": ws.utcnow(),
        # New with every freeze, as for a design round: the sha repeats when
        # the same tree is frozen again, and the archived report of each round
        # is filed under this so the second does not overwrite the first.
        "round_id": uuid.uuid4().hex,
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
        "condition_paths": condition_paths,
        "untracked_included": untracked,
        "withheld": sorted(withheld, key=lambda entry: str(entry.get("path"))),
        "exclude_patterns": patterns,
        "bytes": len(diff.encode("utf-8")),
        "lines_added": added_lines,
        "lines_deleted": deleted_lines,
        "sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "empty": not diff.strip(),
    }
    if context_on:
        frozen = context_mod.extract(root, frozen_tree, diff, reviewed, working_tree_diff=not previous_tree)
        frozen["sha256"] = meta["sha256"]
        frozen["generated_at"] = ws.utcnow()
        ws.write_json(workspace.surrounding_path, frozen)
        meta["surrounding"] = {
            "mode": "enclosing",
            "path": workspace.relative(workspace.surrounding_path),
            "tree": frozen_tree,
            "candidates": len(frozen["candidates"]),
            "chars": sum(int(c["chars"]) for c in frozen["candidates"]),
            "skipped": len(frozen["skipped"]),
        }
    elif os.path.isfile(workspace.surrounding_path):
        # A snapshot taken with the setting off has no context, and an older
        # file left beside it would describe a diff that is no longer there.
        try:
            os.unlink(workspace.surrounding_path)
        except OSError:
            pass
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
    meta = workspace.read_snapshot_meta()
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
    return ws.fmt_int(total) + ("" if exact else "+")


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
    lines = ["Re-review. The diff above is the fix only, not the whole change."]
    if meta.get("full_diff"):
        lines.append("Whole change frozen at %s -- read it if you need the context." % meta["full_diff"])
    accepted = carried_findings(workspace, meta)
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


def carried_findings(workspace: ws.Workspace, meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The accepted findings a round still has open; empty when it has none.

    The accepted findings of the live consolidated report, on a snapshot with
    ``incremental_from`` set -- the set ``render_round_context`` hands every
    reviewer -- or on a rerun of the snapshot that report was built for,
    where nothing has changed that could have fixed them. A conditional
    reviewer whose id is in one of these findings' ``reported_by`` joins the
    round to re-check its own finding, so a rerun that would otherwise leave
    it out cannot drop the finding from the report unfixed.
    """
    consolidated = ws.read_json(workspace.consolidated_json_path, {}) or {}
    if not meta.get("incremental_from"):
        sha = str(meta.get("sha256") or "")
        if not sha or str((consolidated.get("snapshot") or {}).get("sha256") or "") != sha:
            return []
    return accepted_findings(consolidated)


ADDED_HEADING_RE = re.compile(r"^\s{0,3}(#{2,3})\s+added in this revision\b", re.IGNORECASE)
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s")
NONE_ADDED_RE = re.compile(r"^[\s\-*_]*none(?![a-z0-9])", re.IGNORECASE)
FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def added_in_revision(plan_text: str) -> Optional[str]:
    """The body of the plan's ``## Added in this revision`` section.

    None when the plan has no such heading outside a fenced block; the body
    runs to the next heading of the same level or higher, stripped, so
    ``###`` items under a ``##`` heading stay inside it.
    """
    fence = ""
    body: Optional[List[str]] = None
    level = 0
    for line in plan_text.splitlines():
        marker = FENCE_RE.match(line)
        if fence:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = ""
            if body is not None:
                body.append(line)
            continue
        if marker:
            fence = marker.group(1)
        elif body is not None:
            heading = HEADING_RE.match(line)
            if heading and len(heading.group(1)) <= level:
                break
        else:
            added = ADDED_HEADING_RE.match(line)
            if added:
                body = []
                level = len(added.group(1))
                continue
        if body is not None:
            body.append(line)
    return None if body is None else "\n".join(body).strip()


# No lazy group before an optional tail: ``(.*?)\s*#*\s*$`` backtracks
# polynomially on a ``#`` line padded with spaces. The closing hashes are
# stripped in ``_heading_title`` instead.
HEADING_TEXT_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
FILES_HEADING_RE = re.compile(r"^(?:\d+[.)]\s*)?files to modify\b", re.IGNORECASE)
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_BRACE_RE = re.compile(r"^(.*?)\{([^{}]*)\}(.*)$")
_LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")
_SPAN_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9_]+$")
#: A plain word needs a letter after its dot, so ``0.14.0`` is not a file.
_PLAIN_EXTENSION_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9_]*$")
_ABBREVIATION_RE = re.compile(r"^(?:[A-Za-z]\.)+[A-Za-z]?$")
# Neither group can run past the next bracket, so a line of them stays linear.
_LINK_RE = re.compile(r"\[([^\[\]\n]*)\]\(([^()\[\]\s]*)\)")
_WORD_SPLIT_RE = re.compile(r"[\s|]+")
_EDGE_PUNCTUATION = "`\"'()<>,;:!?"


class PlanToken(NamedTuple):
    token: str
    section: str
    in_files: bool
    #: A plain word on a line whose entry is backticked -- the description
    #: beside it, so ``read/write`` there is not read as a directory.
    prose: bool = False


class PlanScan(NamedTuple):
    tokens: List[PlanToken]
    has_files_section: bool


def _heading_title(text: str) -> str:
    title = text.strip()
    head = title.rstrip("#")
    if head != title and (not head or head[-1].isspace()):
        title = head.rstrip()
    return title


def _expand_word(word: str) -> List[str]:
    brace = _BRACE_RE.match(word)
    if brace and "," in brace.group(2) and "{" not in brace.group(3) and "}" not in brace.group(3):
        members = [brace.group(1) + member + brace.group(3) for member in brace.group(2).split(",")]
    else:
        members = [word]
    return [_LINE_SUFFIX_RE.sub("", member) or member for member in members]


def _expand_token(span: str) -> List[str]:
    """The tokens one backticked span stands for.

    Its first word, and every later word shaped like a path (a ``/`` or a
    file extension), so an annotated ``scripts/auth.py (new)`` is the path
    and ``src/main.py, src/auth.py`` is both; a trailing ``,`` or ``;`` is
    dropped from each. One level of ``{a,b}`` is split into one token per
    member; a trailing ``:N`` or ``:N-M`` line reference is dropped, unless
    it is all there is. Nothing else is touched.
    """
    words = [word.rstrip(",;") or word for word in span.split()]
    if not words:
        return []
    chosen = words[:1] + [
        word for word in words[1:] if "/" in word or _SPAN_EXTENSION_RE.search(posixpath.basename(word))
    ]
    return [token for word in chosen for token in _expand_word(word)]


def _plain_words(text: str) -> List[str]:
    """The path-shaped words of text written without backticks.

    Split on whitespace and table pipes; a Markdown link counts by its text
    and its target; quotes, brackets, ``**`` and trailing sentence
    punctuation are dropped. A word is kept when it has a ``/`` or an
    extension starting with a letter, and is not an abbreviation such as
    ``e.g.``.
    """
    words: List[str] = []
    for raw in _WORD_SPLIT_RE.split(_LINK_RE.sub(r" \1 \2 ", text)):
        word = raw.strip(_EDGE_PUNCTUATION)
        if word.startswith("**"):
            word = word[2:]
        if word.endswith("**"):
            word = word[:-2]
        word = word.rstrip(".").strip(_EDGE_PUNCTUATION)
        # A bare "/" is the separator in "`a` / `b`", not a path.
        if not word or _ABBREVIATION_RE.match(word) or not word.strip("/"):
            continue
        if "/" in word or _PLAIN_EXTENSION_RE.search(posixpath.basename(word)):
            words.extend(_expand_word(word))
    return words


def plan_tokens(plan_text: str) -> PlanScan:
    """Every backticked token in the plan, with the heading it sits under.

    ``in_files`` is true under a ``Files to Modify`` heading and every
    subsection of it, which ends at the next heading of the same level or
    higher -- the boundary ``added_in_revision`` uses. That section is also
    read without backticks, because an entry written as plain text names a
    file all the same: every path-shaped word there (``_plain_words``) is a
    token too, marked ``prose`` when its line names something in backticks.

    Fenced lines are not read for the size, so a drafted CHANGELOG entry or
    doc row does not count as a file. Inside the section their path-shaped
    words still come back, with ``in_files`` false, so a file tree there is
    checked against the high-risk patterns; elsewhere they are skipped whole.
    """
    fence = ""
    stack: List[Tuple[int, str]] = []
    tokens: List[PlanToken] = []
    has_files = False
    section = ""
    in_files = False
    for line in plan_text.splitlines():
        marker = FENCE_RE.match(line)
        if fence:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = ""
            elif in_files:
                for word in _plain_words(line):
                    tokens.append(PlanToken(word, section, False))
            continue
        if marker:
            fence = marker.group(1)
            continue
        heading = HEADING_TEXT_RE.match(line)
        if heading:
            level = len(heading.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            title = _heading_title(heading.group(2))
            stack.append((level, title))
            if FILES_HEADING_RE.match(title):
                has_files = True
        section = stack[-1][1] if stack else ""
        in_files = any(FILES_HEADING_RE.match(name) for _, name in stack)
        spans = BACKTICK_RE.findall(line)
        for span in spans:
            for token in _expand_token(span):
                tokens.append(PlanToken(token, section, in_files))
        if in_files:
            for word in _plain_words(BACKTICK_RE.sub(" ", line)):
                tokens.append(PlanToken(word, section, True, bool(spans)))
    return PlanScan(tokens, has_files)


def render_design_round_context(
    workspace: ws.Workspace, meta: Dict[str, Any], plan_text: Optional[str] = None
) -> str:
    """What a re-reviewed plan needs to say that the plan itself does not.

    The design version of ``render_round_context``, and the same reasoning:
    the reviewer is stateless, so a revision handed over as a fresh plan
    invites the same objections again. Who reported what is left out here too
    -- the accepted findings arrive as the brief the revision worked from, not
    as another reviewer's opinion still in play.

    Most new findings on a recorded re-review came from what the revision
    added to answer the last round, so the reviewer is pointed at the plan's
    ``## Added in this revision`` list -- or asked to look for an unlisted
    addition when the list is empty or missing. That pointer goes out on every
    recorded re-review, including one the owner asked for with no accepted
    findings behind it; only the findings block needs them. ``plan_text`` is
    the frozen plan the round reviews; it is read from the snapshot when not
    given.
    """
    if not meta.get("previous_sha"):
        return ""
    consolidated = ws.read_json(workspace.consolidated_json_path, {}) or {}
    accepted = accepted_findings(consolidated)
    lines: List[str] = []
    if accepted:
        lines.append("This plan is a revision. It was meant to address:")
        for finding in accepted:
            lines.append(
                "- [%s] %s -- %s"
                % (finding.get("severity", "?"), finding.get("file", "?"), finding.get("problem", ""))
            )
        lines += [
            "",
            "For each: addressed or not, plus any new problem the revision introduced. "
            "Do not assume a listed item was real.",
            "",
        ]
    if plan_text is None:
        plan_text = ws.read_text(workspace.snapshot_path, "")
    added = added_in_revision(plan_text)
    if added and not NONE_ADDED_RE.match(added):
        lines.append(
            "New problems on a re-review mostly come from what a revision adds. Examine each item "
            'under "Added in this revision" first -- its failure paths, what it does on older or '
            "malformed input, how it interacts with existing behaviour and with the other items -- "
            "then check that nothing else changed without being listed."
        )
    else:
        lines.append("Check whether the revision added a mechanism without saying so.")
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


def _touched_paths(
    reviewed: Sequence[str],
    tracked: Sequence[Dict[str, Any]],
    withheld: Sequence[Dict[str, Any]],
    untracked: Sequence[str],
    skip: Set[str],
) -> List[str]:
    """``reviewed`` plus every other path the change touches, less ``skip``.

    Rename sources and withheld files are added after the reviewed ones. A
    tracked entry whose path is in ``skip`` contributes neither its path nor
    the name it was renamed from. ``changed_paths`` and ``condition_paths``
    are both built here, so the two cannot drift apart.
    """
    paths = list(reviewed)
    for entry in tracked:
        if str(entry.get("path") or "") in skip:
            continue
        for name in (entry.get("path"), entry.get("previous")):
            if name and name not in paths:
                paths.append(str(name))
    for entry in withheld:
        name = entry.get("path")
        if name and name not in paths:
            paths.append(str(name))
    for name in untracked:
        if name not in paths:
            paths.append(name)
    return paths


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
