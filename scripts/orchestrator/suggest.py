"""Path-scoped specialist reviewers, proposed from what a repository holds.

``config suggest-roles`` reads the project's file names -- and the root
``package.json``, nothing else -- and proposes the built-in specialists a
path can speak for: ``database``, ``frontend`` and ``backend``. No model is
asked and no token is spent. The only subprocess is one ``git ls-files -z``,
and only inside a git repository; outside one the directory is walked, within
bounds.

Two sets of paths come out of a listing. ``listed`` is every file but the
orchestrator's own, and is what a proposal's match count and share are taken
over: withheld files are in it, since a withheld file still brings a
path-scoped reviewer into a round. ``evidence`` is ``listed`` minus what says
nothing about the code people write -- dot-directories, vendored and
generated trees, tests, and what ``review.exclude`` withholds -- and is the
only set the rules look at.

Uncertain cases go toward not proposing. Every role that is not proposed is
named with the reason.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from . import config as config_mod
from . import optimization as opt_mod
from . import workspace as ws
from .review_snapshot import withholds

#: ``git ls-files`` output past this many UTF-8 bytes is an error, not a partial
#: listing. It is checked after the output is captured, so it bounds the work,
#: not git.
MAX_LISTING_BYTES = 16 * 1024 * 1024

#: Seconds ``git ls-files`` may take; a timeout is an error too.
GIT_TIMEOUT = 30

#: How many evidence paths the rules read, in listing order.
EVIDENCE_LIMIT = 20000

#: The walk outside a git repository: files, seconds, directory depth.
WALK_FILE_LIMIT = 20000
WALK_SECONDS = 10.0
WALK_DEPTH = 8

#: Bytes of ``package.json`` read; one more than this means it is too large.
PACKAGE_JSON_LIMIT = 1024 * 1024

#: Directories that hold vendored, built or generated files: pruned from the
#: walk, and never evidence.
SKIP_DIRS = frozenset(
    (
        "node_modules",
        "vendor",
        "third_party",
        "dist",
        "build",
        "target",
        "venv",
        ".venv",
        "__pycache__",
        "generated",
        "__generated__",
        "gen",
    )
)

#: Directories whose files test the code rather than being it.
TEST_DIRS = frozenset(("test", "tests", "__tests__", "spec", "testdata", "fixtures"))

#: The roles this command proposes, in the order it reports them.
SUGGESTED_ROLES = ("database", "frontend", "backend")

#: A role whose patterns would need more than this many is not proposed.
MAX_PATTERNS = 12

#: A role whose patterns match more than this share of ``listed`` would join
#: most rounds, which keeps the panel whole.
MAX_SHARE = 0.5

#: How many files a directory name, or an extension, needs to count.
MIN_FILES = 2

DATABASE_DIRS = ("migrations", "migrate", "alembic", "prisma")
FRONTEND_EXTS = ("tsx", "jsx", "vue", "svelte")
BACKEND_DIRS = ("api", "server", "backend", "handlers", "routes", "controllers")

#: ``package.json`` dependencies that are evidence of a frontend, never a signal.
FRONTEND_DEPS = ("react", "vue", "svelte", "@angular/core", "next", "@sveltejs/kit")
REMIX_PREFIX = "@remix-run/"

#: Root files named in the backend evidence, never a signal.
BACKEND_ROOT_FILES = ("go.mod", "pyproject.toml", "package.json")

#: The keys of ``package.json`` whose names are read.
_DEPENDENCY_KEYS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")

#: How many example files one line of evidence names.
_EXAMPLES = 3

_NOT_A_SPECIALIST = "not a path specialist; suggest-roles proposes only database, frontend and backend"

FOOTER = "Each joins only code rounds touching its paths, and every design review round."


class ListingError(Exception):
    """``git ls-files`` failed or printed too much; nothing is walked instead."""


class Listing(NamedTuple):
    #: The directory paths are relative to.
    root: str
    #: ``git`` or ``walk``.
    source: str
    #: Every file but the orchestrator's own, repository-relative POSIX.
    listed: List[str]
    #: What the rules read: ``listed`` less what says nothing about the code.
    evidence: List[str]
    #: The walk stopped early, or the evidence was capped.
    truncated: bool
    #: ``review.exclude`` as in force.
    exclude: Tuple[str, ...]
    notes: List[str]


class Suggestion(NamedTuple):
    role: str
    #: The ``when.paths`` patterns.
    paths: List[str]
    #: What led to it, one line each.
    evidence: List[str]
    #: Files of ``listed`` its patterns match.
    matches: int
    #: Of those, the ones ``review.exclude`` withholds.
    withheld_matches: int


class Skipped(NamedTuple):
    role: str
    reason: str


class Result(NamedTuple):
    listing: Listing
    suggestions: List[Suggestion]
    #: Parallel to ``suggestions``: the reviewer each one would add.
    reviewers: List[Dict[str, Any]]
    skipped: List[Skipped]


# --------------------------------------------------------------------------- listing


def find_listing_root(cwd: str) -> Tuple[str, bool]:
    """``(root, in_git)``: the nearest directory holding a ``.git`` entry, or ``cwd``.

    ``lexists`` rather than ``isdir``, so a worktree's or a submodule's
    ``.git`` file counts. Only the filesystem is read.
    """
    start = os.path.abspath(cwd)
    current = start
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            return current, True
        parent = os.path.dirname(current)
        if parent == current:
            return start, False
        current = parent


def _describe_size(count: int) -> str:
    mib = 1024 * 1024
    return "%d MiB" % (count // mib) if count >= mib and count % mib == 0 else "%d bytes" % count


def git_paths(root: str) -> List[str]:
    """Every path ``git ls-files`` lists under ``root``; raises ``ListingError``."""
    code, out, err = ws.git(["ls-files", "-z"], root, timeout=GIT_TIMEOUT)
    if code != 0:
        reason = err.strip() or "exit status %d" % code
        raise ListingError("git ls-files failed in %s: %s" % (root, reason))
    if len(out.encode("utf-8")) > MAX_LISTING_BYTES:
        raise ListingError(
            "git ls-files printed more than %s; suggest-roles does not scan a repository this large"
            % _describe_size(MAX_LISTING_BYTES)
        )
    return [path for path in out.split("\0") if path]


def walk_paths(root: str) -> Tuple[List[str], bool]:
    """``(paths, truncated)`` from walking ``root``, which is not a git repository.

    Dot-directories, ``SKIP_DIRS`` and anything deeper than ``WALK_DEPTH``
    are pruned, links are not followed, and the walk stops at
    ``WALK_FILE_LIMIT`` files or after ``WALK_SECONDS``.
    """
    deadline = time.monotonic() + WALK_SECONDS
    paths: List[str] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        # Checked here too, so a tree of directories with no files is bounded.
        if time.monotonic() >= deadline:
            return paths, True
        relative = os.path.relpath(current, root)
        parts = [] if relative == os.curdir else relative.replace(os.sep, "/").split("/")
        dirs[:] = sorted(
            name
            for name in dirs
            if len(parts) < WALK_DEPTH and not name.startswith(".") and name not in SKIP_DIRS
        )
        for name in sorted(files):
            if len(paths) >= WALK_FILE_LIMIT or time.monotonic() >= deadline:
                return paths, True
            paths.append("/".join([*parts, name]))
    return paths, False


def _workspace_prefix(root: str, workspace_dir: str) -> str:
    """``workspace.dir`` as a repository-relative prefix, or "" when it is outside."""
    target = workspace_dir if os.path.isabs(workspace_dir) else os.path.join(root, workspace_dir)
    try:
        relative = os.path.relpath(os.path.abspath(target), root)
    except ValueError:  # pragma: no cover - different drives on Windows
        return ""
    relative = relative.replace(os.sep, "/")
    if relative == "." or relative.startswith("../") or relative == "..":
        return ""
    return relative.rstrip("/") + "/"


def _orchestrators_own(path: str, workspace_prefix: str) -> bool:
    if workspace_prefix and path.startswith(workspace_prefix):
        return True
    return path.rsplit("/", 1)[-1] in config_mod.PROJECT_CONFIG_NAMES


def _says_nothing(path: str, exclude: Sequence[str]) -> bool:
    """True for a path that is not evidence of the code people write."""
    for directory in path.split("/")[:-1]:
        lowered = directory.lower()
        if directory.startswith(".") or lowered in SKIP_DIRS or lowered in TEST_DIRS:
            return True
    return bool(withholds(path, exclude))


def build_listing(
    root: str,
    source: str,
    paths: Sequence[str],
    exclude: Sequence[str],
    workspace_dir: str,
    walk_truncated: bool = False,
    notes: Sequence[str] = (),
) -> Listing:
    """The two path sets of ``paths``, with the evidence capped at ``EVIDENCE_LIMIT``."""
    prefix = _workspace_prefix(root, workspace_dir)
    listed = [path for path in paths if not _orchestrators_own(path, prefix)]
    evidence = [path for path in listed if not _says_nothing(path, exclude)]
    noted = list(notes)
    if walk_truncated:
        noted.append(
            "%s is not a git repository, and the walk stopped at %d files or %g seconds; "
            "the shares are approximate" % (root, WALK_FILE_LIMIT, WALK_SECONDS)
        )
    elif source == "walk":
        noted.append("%s is not a git repository, so it was walked; the shares are approximate" % root)
    capped = len(evidence) > EVIDENCE_LIMIT
    if capped:
        evidence = evidence[:EVIDENCE_LIMIT]
        noted.append("evidence read from the first {:,} files".format(EVIDENCE_LIMIT))
    return Listing(root, source, listed, evidence, walk_truncated or capped, tuple(exclude), noted)


# --------------------------------------------------------------------------- package.json


def read_package_deps(root: str, listing: Listing) -> Tuple[Optional[Set[str]], str]:
    """``(dependencies, note)`` from the root ``package.json``.

    ``set()`` when there is none, and None when there is one that was not
    read: untracked in a git repository, not a regular file, swapped between
    the check and the open, larger than ``PACKAGE_JSON_LIMIT``, or not a JSON
    object. A link is never followed and a FIFO never opened for reading.
    """
    path = os.path.join(root, "package.json")
    if not os.path.lexists(path):
        return set(), ""
    if listing.source == "git" and "package.json" not in listing.listed:
        return None, "package.json is not tracked by git, so its dependencies were not read"
    ignored = "package.json is not a regular file, so its dependencies were not read"
    try:
        before = os.lstat(path)
    except OSError as exc:
        return None, "package.json could not be read (%s)" % exc
    if not stat.S_ISREG(before.st_mode):
        return None, ignored
    flags = os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_BINARY"):
        flags |= getattr(os, name, 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        return None, "package.json could not be read (%s)" % exc
    chunks: List[bytes] = []
    size = 0
    try:
        after = os.fstat(fd)
        same = (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        if not (stat.S_ISREG(after.st_mode) and same):
            return None, ignored
        while size <= PACKAGE_JSON_LIMIT:
            chunk = os.read(fd, PACKAGE_JSON_LIMIT + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        return None, "package.json could not be read (%s)" % exc
    finally:
        os.close(fd)
    if size > PACKAGE_JSON_LIMIT:
        limit = _describe_size(PACKAGE_JSON_LIMIT)
        return None, "package.json is larger than %s, so its dependencies were not read" % limit
    try:
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except ValueError:  # UnicodeDecodeError is one
        return None, "package.json is not valid JSON, so its dependencies were not read"
    if not isinstance(data, dict):
        return None, "package.json is not a JSON object, so its dependencies were not read"
    deps: Set[str] = set()
    for key in _DEPENDENCY_KEYS:
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(name for name in section if isinstance(name, str))
    return deps, ""


# --------------------------------------------------------------------------- rules


def _extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1] if "." in name.lstrip(".") else ""


def _is_frontend_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return _extension(path).lower() in FRONTEND_EXTS or name.startswith("+")


def _examples(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:_EXAMPLES])
    return shown + (", ..." if len(paths) > _EXAMPLES else "")


def _files(count: int) -> str:
    return "%d file%s" % (count, "" if count == 1 else "s")


def _dir_pairs(names: Iterable[str]) -> List[str]:
    patterns: List[str] = []
    for name in names:
        patterns += ["%s/*" % name, "*/%s/*" % name]
    return patterns


def _dedupe(items: Iterable[str]) -> List[str]:
    return list(OrderedDict.fromkeys(items))


def _named_dirs(evidence: Sequence[str], names: Sequence[str]) -> "OrderedDict[str, List[str]]":
    """Each concrete directory whose name is one of ``names`` (any case) -> its evidence files."""
    found: "OrderedDict[str, List[str]]" = OrderedDict()
    for path in evidence:
        parts = path.split("/")
        for depth, directory in enumerate(parts[:-1]):
            if directory.lower() in names:
                found.setdefault("/".join(parts[: depth + 1]), []).append(path)
    return found


def _by_name(dirs: "OrderedDict[str, List[str]]") -> "OrderedDict[str, Tuple[List[str], List[str]]]":
    """Lower-cased name -> (the casings found, the files under any of them).

    Only a directory holding ``MIN_FILES`` files counts; two smaller ones of
    the same name do not add up to one.
    """
    grouped: "OrderedDict[str, Tuple[List[str], List[str]]]" = OrderedDict()
    seen: Dict[str, Set[str]] = {}
    for directory, files in dirs.items():
        if len(files) < MIN_FILES:
            continue
        name = directory.rsplit("/", 1)[-1]
        casings, members = grouped.setdefault(name.lower(), ([], []))
        if name not in casings:
            casings.append(name)
        known = seen.setdefault(name.lower(), set())
        for path in files:
            if path not in known:
                known.add(path)
                members.append(path)
    return grouped


def _by_extension(evidence: Sequence[str], extensions: Sequence[str]) -> "OrderedDict[str, List[str]]":
    """Each casing of an extension in ``extensions`` -> its evidence files."""
    found: "OrderedDict[str, List[str]]" = OrderedDict()
    for path in evidence:
        extension = _extension(path)
        if extension.lower() in extensions:
            found.setdefault(extension, []).append(path)
    return found


def _dir_line(casings: Sequence[str], files: Sequence[str]) -> str:
    """``migrations/, Migrations/: 14 files, e.g. ...``"""
    return "%s/: %s, e.g. %s" % ("/, ".join(casings), _files(len(files)), _examples(files))


def _or_list(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else "%s or %s" % (", ".join(items[:-1]), items[-1])


class _Rule(NamedTuple):
    patterns: List[str]
    evidence: List[str]
    #: Why there is no signal; empty when there is one.
    missing: str


def _database(evidence: Sequence[str]) -> _Rule:
    patterns: List[str] = []
    lines: List[str] = []
    for casings, files in _by_name(_named_dirs(evidence, DATABASE_DIRS)).values():
        patterns += _dir_pairs(casings)
        lines.append(_dir_line(casings, files))
    sql = _by_extension(evidence, ("sql",))
    sql_files = [path for files in sql.values() for path in files]
    if len(sql_files) >= MIN_FILES:
        patterns += ["*.%s" % extension for extension in sql]
        lines.append("*.sql: %s, e.g. %s" % (_files(len(sql_files)), _examples(sql_files)))
    prisma = [path for path in evidence if path.rsplit("/", 1)[-1].lower() == "schema.prisma"]
    if prisma:
        patterns += _dedupe(path.rsplit("/", 1)[-1] for path in prisma)
        lines.append("schema.prisma: %s" % _examples(prisma))
    missing = ""
    if not patterns:
        dirs = _or_list(["%s/" % name for name in DATABASE_DIRS])
        missing = "no signal (no %s directory; fewer than %d *.sql files; no schema.prisma)"
        missing %= (dirs, MIN_FILES)
    return _Rule(_dedupe(patterns), lines, missing)


def _frontend_deps(deps: Optional[Set[str]]) -> List[str]:
    if not deps:
        return []
    return sorted(dep for dep in deps if dep in FRONTEND_DEPS or dep.startswith(REMIX_PREFIX))


def _frontend(evidence: Sequence[str], deps: Optional[Set[str]]) -> _Rule:
    found = _by_extension(evidence, FRONTEND_EXTS)
    files = [path for paths in found.values() for path in paths]
    listed = _frontend_deps(deps)
    exts = _or_list(["*.%s" % extension for extension in FRONTEND_EXTS])
    if len(files) < MIN_FILES:
        if listed:
            missing = "package.json lists %s, but fewer than %d %s files hold components"
            missing %= (", ".join(listed), MIN_FILES, exts)
        else:
            missing = "no signal (fewer than %d %s files)" % (MIN_FILES, exts)
        return _Rule([], [], missing)
    patterns = ["*.%s" % extension for extension in found]
    lines = ["%s: %s, e.g. %s" % (", ".join(patterns), _files(len(files)), _examples(files))]
    if listed:
        lines.append("package.json lists %s" % ", ".join(listed))
    return _Rule(patterns, lines, "")


def _routes_excluded(deps: Optional[Set[str]], frontend_signal: bool) -> bool:
    """SvelteKit and Remix keep pages under ``routes/``, so it is not backend evidence there.

    An unread ``package.json`` with a frontend signal is treated the same way.
    """
    if deps is None:
        return frontend_signal
    return any(dep == "@sveltejs/kit" or dep.startswith(REMIX_PREFIX) for dep in deps)


def _backend(
    evidence: Sequence[str], listed: Sequence[str], deps: Optional[Set[str]], frontend: bool
) -> _Rule:
    dirs = _named_dirs(evidence, BACKEND_DIRS)
    routes_out = _routes_excluded(deps, frontend)
    kept: "OrderedDict[str, List[str]]" = OrderedDict()
    dropped: Set[str] = set()
    for directory, files in dirs.items():
        name = directory.rsplit("/", 1)[-1].lower()
        # SvelteKit's and Remix's routes/, and a directory mostly of components
        # or SvelteKit route files, are a frontend's.
        frontend_files = sum(1 for path in files if _is_frontend_file(path))
        if (name == "routes" and routes_out) or 2 * frontend_files >= len(files):
            dropped.add(name)
            continue
        kept[directory] = files
    patterns: List[str] = []
    lines: List[str] = []
    for name, (casings, files) in _by_name(kept).items():
        if name not in dropped:
            patterns += _dir_pairs(casings)
            lines.append(_dir_line(casings, files))
            continue
        # The name-wide pair would match the dropped directories too, so each
        # kept one is named by its full path.
        for directory, held in kept.items():
            if directory.rsplit("/", 1)[-1].lower() == name and len(held) >= MIN_FILES:
                patterns.append("%s/*" % directory)
                lines.append(_dir_line([directory], held))
    if not patterns:
        names = _or_list(["%s/" % name for name in BACKEND_DIRS])
        missing = "no signal (no %s directory holding %d files outside a frontend)" % (names, MIN_FILES)
        return _Rule([], [], missing)
    roots = [name for name in BACKEND_ROOT_FILES if name in listed]
    if roots:
        lines.append("at the root: %s" % ", ".join(roots))
    return _Rule(_dedupe(patterns), lines, "")


def _on_panel(role: str, panel: Sequence[Any], labels: Sequence[str]) -> List[str]:
    """How each reviewer of ``role`` on the panel is named: ``claude-database (project extra)``."""
    named: List[str] = []
    for index, reviewer in enumerate(panel):
        if not isinstance(reviewer, dict) or reviewer.get("role") != role:
            continue
        label = labels[index] if index < len(labels) else ""
        name = str(reviewer.get("id") or "reviewer %d" % (index + 1))
        named.append("%s (%s)" % (name, label) if label else name)
    return named


def suggest(
    listing: Listing,
    panel: Sequence[Any],
    package_deps: Optional[Set[str]],
    labels: Sequence[str] = (),
) -> Tuple[List[Suggestion], List[Skipped]]:
    """The proposals for ``listing``, and every role not proposed with the reason.

    ``panel`` is the composed panel, ``labels`` the origin label of each of its
    reviewers, and ``package_deps`` the root ``package.json``'s dependencies
    (None when it was there and not read).
    """
    evidence, listed = listing.evidence, listing.listed
    frontend = _frontend(evidence, package_deps)
    rules = {
        "database": _database(evidence),
        "frontend": frontend,
        "backend": _backend(evidence, listed, package_deps, bool(frontend.patterns)),
    }
    suggestions: List[Suggestion] = []
    skipped: List[Skipped] = []
    for role in SUGGESTED_ROLES:
        rule = rules[role]
        present = _on_panel(role, panel, labels)
        if present:
            skipped.append(Skipped(role, "already on the panel: %s" % ", ".join(present)))
            continue
        if rule.missing:
            skipped.append(Skipped(role, rule.missing))
            continue
        if len(rule.patterns) > MAX_PATTERNS:
            skipped.append(Skipped(role, "too many name variants (%d patterns)" % len(rule.patterns)))
            continue
        hits = [path for path, _pattern in opt_mod.high_risk_matches(listed, rule.patterns)]
        share = len(hits) / len(listed) if listed else 0.0
        if share > MAX_SHARE:
            reason = "would join most rounds (%d%% of files match), which keeps the panel whole"
            skipped.append(Skipped(role, reason % round(share * 100)))
            continue
        withheld = sum(1 for path in hits if withholds(path, listing.exclude))
        suggestions.append(Suggestion(role, rule.patterns, rule.evidence, len(hits), withheld))
    for role in config_mod.BUILTIN_ROLES:
        if role not in SUGGESTED_ROLES:
            skipped.append(Skipped(role, _NOT_A_SPECIALIST))
    return suggestions, skipped


def reviewers_for(
    suggestions: Sequence[Suggestion], panel: Sequence[Any], provider: str, family: str
) -> List[Dict[str, Any]]:
    """The reviewer each suggestion adds, each with an id nothing on ``panel`` holds."""
    made: List[Dict[str, Any]] = []
    for suggestion in suggestions:
        taken = {"reviewers": [*panel, *made]}
        reviewer_id = config_mod.suggest_reviewer_id(taken, provider, suggestion.role)
        role, paths = suggestion.role, suggestion.paths
        made.append(config_mod.make_reviewer(reviewer_id, provider, family, role, paths=paths))
    return made


# --------------------------------------------------------------------------- output


def _count(number: int) -> str:
    return "{:,}".format(number)


def render(result: Result) -> str:
    """The text report: what was listed, each proposal, and every role skipped."""
    listing = result.listing
    how = "git ls-files" if listing.source == "git" else "a walk"
    lines = ["Listed %s files in %s (%s)." % (_count(len(listing.listed)), listing.root, how)]
    lines += ["note: %s" % note for note in listing.notes]
    total = _count(len(listing.listed))
    if result.suggestions:
        lines += ["", "Proposed:"]
        for suggestion, reviewer in zip(result.suggestions, result.reviewers, strict=True):
            family = (reviewer.get("model") or {}).get("family", "default")
            seat = (reviewer["id"], reviewer["provider"], family, suggestion.role)
            lines.append("  %s  %s / %s  role %s" % seat)
            lines.append("    when.paths: %s" % ", ".join(suggestion.paths))
            for line in suggestion.evidence:
                lines.append("    evidence: %s" % line)
            matched = "    matches %s of %s files" % (_count(suggestion.matches), total)
            withheld = suggestion.withheld_matches
            if withheld:
                verb = "is" if withheld == 1 else "are"
                matched += "; %s of them %s withheld by review.exclude" % (_count(withheld), verb)
                matched += ", and those still bring it in"
            lines.append(matched)
        lines.append(FOOTER)
    if result.skipped:
        lines += ["", "Not proposed:"]
        lines += ["  %s: %s" % (skipped.role, skipped.reason) for skipped in result.skipped]
    return "\n".join(lines)


def payload(result: Result, written: Optional[str]) -> Dict[str, Any]:
    """The ``--json`` object."""
    listing = result.listing
    return {
        "root": listing.root,
        "source": listing.source,
        "files": len(listing.listed),
        "truncated": listing.truncated,
        "notes": list(listing.notes),
        "suggestions": [
            {
                "role": suggestion.role,
                "id": reviewer["id"],
                "provider": reviewer["provider"],
                "family": (reviewer.get("model") or {}).get("family"),
                "paths": list(suggestion.paths),
                "evidence": list(suggestion.evidence),
                "matches": suggestion.matches,
                "withheld_matches": suggestion.withheld_matches,
            }
            for suggestion, reviewer in zip(result.suggestions, result.reviewers, strict=True)
        ],
        "skipped": [{"role": skipped.role, "reason": skipped.reason} for skipped in result.skipped],
        "written": written,
    }
