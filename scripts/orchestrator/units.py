"""Work units: a plan split into pieces the implementer runs one at a time, or side by side.

A plan opts in with a ``## Units`` heading holding a fenced ``yaml`` block::

    units:
      - name: parser
        kind: feature
        files: [scripts/orchestrator/cli.py]
        depends_on: []

and a ``### Unit: <name>`` section per unit with its steps. A plan without the
heading is a single run, exactly as before units existed. The serial parts of
a long plan are written the same way, as units chained by ``depends_on``.

Units that may run at the same time -- neither depends on the other, however
indirectly -- must not list overlapping files, so their results can be merged
back without one undoing the other. ``units check`` holds a plan to that, and
to the rest of ``validate``, before anything runs.

The state of each unit is kept in ``execution/units.json``. Every
read-modify-write of it holds ``locked(workspace)``.
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Set

from . import approval as approval_mod
from . import miniyaml, review_plan
from . import workspace as ws
from .config_common import MAX_PARALLEL_UNITS, default_config

#: What a unit may be. ``integration`` is the one unit that comes last.
KINDS = ("feature", "fix", "refactor", "docs", "test", "ci", "integration")
INTEGRATION = "integration"

#: A unit's name goes into a directory and file names, so it is kept to a slug.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_UNITS_HEADING = "Units"
_UNIT_HEADING_RE = re.compile(r"^Unit:\s*(\S+)\s*$")
_FILES_HEADING = "Files to Modify"
_GOAL_HEADING = "Goal"
_GLOB_CHARS = "*?["

#: Statuses a unit's record takes in ``units.json``.
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
MERGED = "merged"
FAILED = "failed"
CANCELLED = "cancelled"
CONFLICTED = "conflicted"

#: ``units merge`` left conflicts for an agent to resolve. The next free code
#: after ``questions.EXIT_QUESTIONS_PENDING``.
EXIT_CONFLICTS = 7


class UnitsError(ValueError):
    """A ``## Units`` block that cannot be read as a list of units."""


class Unit(NamedTuple):
    """One entry of the ``## Units`` block."""

    name: str
    kind: str
    files: List[str]
    depends_on: List[str]


class Settings(NamedTuple):
    """The ``implementation`` block as ``settings_of`` reads it."""

    max_parallel: int
    hub_paths: List[str]


def settings_of(data: Dict[str, Any]) -> Settings:
    """The ``implementation`` block of ``data``, each value the default where it is absent or unusable.

    Read leniently, as ``progress_settings_of`` is: commands load unvalidated.
    """
    defaults = default_config()["implementation"]
    configured = data.get("implementation")
    block = configured if isinstance(configured, dict) else {}
    cap = block.get("max_parallel")
    if not (isinstance(cap, int) and not isinstance(cap, bool) and 1 <= cap <= MAX_PARALLEL_UNITS):
        cap = defaults["max_parallel"]
    hubs = block.get("hub_paths")
    if not (isinstance(hubs, list) and all(isinstance(p, str) and p.strip() for p in hubs)):
        hubs = defaults["hub_paths"]
    return Settings(cap, list(hubs))


# --------------------------------------------------------------------------- the plan


def _headings(lines: List[str]) -> List["tuple[int, str, int]"]:
    """Each heading outside a code fence as (level, title, line), read as ``review_plan`` reads them."""
    found: List["tuple[int, str, int]"] = []
    for fenced in review_plan.fenced_lines(lines):
        match = None if fenced.part else review_plan.HEADING_TEXT_RE.match(fenced.text)
        if match:
            found.append((len(match.group(1)), review_plan.heading_title(match.group(2)), fenced.number))
    return found


def _sections(plan_text: str) -> List["tuple[int, str, int, int]"]:
    """Each heading outside a code fence as (level, title, start line, end line).

    The end is the line of the next heading of the same level or higher, or
    the end of the text.
    """
    lines = plan_text.splitlines()
    found = _headings(lines)
    sections = []
    for position, (level, title, start) in enumerate(found):
        end = len(lines)
        for later_level, _, later_start in found[position + 1 :]:
            if later_level <= level:
                end = later_start
                break
        sections.append((level, title, start, end))
    return sections


def _body(plan_text: str, level: int, title_matches: Callable[[str], bool]) -> Optional[List[str]]:
    """The lines under the first heading of ``level`` whose title passes ``title_matches``."""
    lines = plan_text.splitlines()
    for found_level, title, start, end in _sections(plan_text):
        if found_level == level and title_matches(title):
            return lines[start + 1 : end]
    return None


def _yaml_block(lines: List[str]) -> Optional[str]:
    """The first fenced ``yaml`` block in ``lines``, without its fences."""
    inside: Optional[List[str]] = None
    for fenced in review_plan.fenced_lines(lines):
        if inside is None:
            if fenced.part == review_plan.OPEN and fenced.info.lower().split()[:1] in (["yaml"], ["yml"]):
                inside = []
            continue
        if fenced.part == review_plan.CLOSE:
            return "\n".join(inside)
        inside.append(fenced.text)
    return None


def valid_name(name: str) -> bool:
    """Whether ``name`` is a unit name: a slug, ``[a-z0-9][a-z0-9_-]*``."""
    return bool(_NAME_RE.match(name))


def parse(plan_text: str) -> List[Unit]:
    """The units of a plan, in the order it lists them; [] when it has no ``## Units``.

    Raises ``UnitsError`` for a block that is not a list of units: each entry
    needs a slug ``name``, a known ``kind`` and a non-empty list of ``files``.
    """
    body = _body(plan_text, 2, lambda title: title.strip() == _UNITS_HEADING)
    if body is None:
        return []
    block = _yaml_block(body)
    if block is None:
        raise UnitsError("## Units has no fenced yaml block")
    try:
        data = miniyaml.loads(block)
    except ValueError as exc:
        raise UnitsError("## Units: %s" % exc) from None
    entries = data.get("units") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise UnitsError("## Units: the block must hold units: with at least one entry")
    units: List[Unit] = []
    for index, entry in enumerate(entries):
        label = "units[%d]" % index
        if not isinstance(entry, dict):
            raise UnitsError("%s: must be a mapping" % label)
        name = entry.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise UnitsError("%s: name must match [a-z0-9][a-z0-9_-]* (got %r)" % (label, name))
        kind = entry.get("kind")
        if kind not in KINDS:
            raise UnitsError("%s: kind must be one of %s (got %r)" % (name, "|".join(KINDS), kind))
        files = entry.get("files")
        if not (isinstance(files, list) and files and all(isinstance(f, str) and f.strip() for f in files)):
            raise UnitsError("%s: files must be a non-empty list of paths or globs" % name)
        depends = entry.get("depends_on")
        if depends is None:
            depends = []
        if not isinstance(depends, list) or not all(isinstance(d, str) for d in depends):
            raise UnitsError("%s: depends_on must be a list of unit names" % name)
        units.append(Unit(name, kind, [_normalise(f) for f in files], list(depends)))
    return units


def section(plan_text: str, name: str) -> str:
    """The text of ``### Unit: <name>``, heading included; "" when there is none."""
    lines = plan_text.splitlines()
    for level, title, start, end in _sections(plan_text):
        match = _UNIT_HEADING_RE.match(title.strip()) if level == 3 else None
        if match and match.group(1) == name:
            return "\n".join(lines[start:end]).strip() + "\n"
    return ""


def goal(plan_text: str) -> str:
    """The text under the plan's ``## Goal``; "" when there is none."""
    body = _body(plan_text, 2, lambda title: title.strip() == _GOAL_HEADING) or []
    return "\n".join(body).strip()


def header(unit: Unit, plan_text: str, checks_skipped: bool) -> str:
    """What the CLI puts in front of a unit's prompt: the unit, its files and its plan section."""
    if checks_skipped:
        # A link failed: run in this worktree, the checks would miss the environment.
        checks = "Do not run checks; they run after the merge."
    else:
        checks = "Run your checks before you finish."
    lines = [
        "# Work unit: %s (%s)" % (unit.name, unit.kind),
        "",
        "This run implements one unit of the plan, and only that unit.",
        "Its files: %s" % ", ".join(unit.files),
        checks,
        "",
        section(plan_text, unit.name).rstrip(),
        "",
        "---",
        "",
    ]
    return "\n".join(lines) + "\n"


def _demoted(plan_text: str) -> str:
    """A unit's own plan without its ``## Units``, every heading below ``### Unit:``.

    Moved down by as many levels as put its top heading at level 4, so no
    heading of it ends the ``### Unit:`` section it goes under.
    """
    lines = plan_text.splitlines()
    units_at = [
        (start, end)
        for level, title, start, end in _sections(plan_text)
        if level == 2 and title.strip() == _UNITS_HEADING
    ]
    if units_at:
        start, end = units_at[0]
        lines = lines[:start] + lines[end:]
    kept = "\n".join(lines)
    levels = [level for level, _, _, _ in _sections(kept)]
    shift = max(0, 4 - min(levels)) if levels else 0
    out: List[str] = []
    headings = {index for _, _, index in _headings(lines)}
    for index, line in enumerate(lines):
        match = review_plan.HEADING_TEXT_RE.match(line) if index in headings else None
        if match:
            hashes = match.group(1)
            line = line[: match.start(1)] + "#" * min(len(hashes) + shift, 6) + line[match.end(1) :]
        out.append(line)
    return "\n".join(out).strip() + "\n"


def join(parts: Sequence["tuple[str, str]"]) -> str:
    """One plan from per-unit plans, given as (name, plan text) in the order they go in.

    Each part's ``## Units`` must hold just its own unit. The joined plan's
    ``## Goal`` and ``## Files to Modify`` hold each part's, under the part's
    name, so ``goal`` and ``warnings`` read them as they read any plan; its
    ``## Units`` lists them all, and each part follows under ``### Unit: <name>``.
    Raises ``UnitsError`` for a part that cannot be read so.
    """
    entries: List[Dict[str, Any]] = []
    bodies: List[str] = []
    goals: List[str] = []
    file_lists: List[str] = []
    for name, plan_text in parts:
        found = parse(plan_text)
        if [unit.name for unit in found] != [name]:
            listed = ", ".join(unit.name for unit in found) or "no unit"
            raise UnitsError(
                "%s: its plan's ## Units must hold the one unit %s, not %s" % (name, name, listed)
            )
        unit = found[0]
        entries.append(
            {"name": unit.name, "kind": unit.kind, "files": unit.files, "depends_on": unit.depends_on}
        )
        bodies.append("### Unit: %s\n\n%s" % (name, _demoted(plan_text)))
        # Moved down as the part's body is, so no heading of it is taken for a unit's.
        part_goal = goal(plan_text)
        if part_goal:
            goals.append("### %s\n\n%s" % (name, _demoted(part_goal)))
        part_files = "\n".join(_files_body(plan_text)).strip()
        if part_files:
            file_lists.append("### %s\n\n%s" % (name, _demoted(part_files)))
    shared = ""
    if goals:
        shared += "## %s\n\n%s\n" % (_GOAL_HEADING, "\n".join(goals))
    if file_lists:
        shared += "## %s\n\n%s\n" % (_FILES_HEADING, "\n".join(file_lists))
    block = miniyaml.dumps({"units": entries})
    return "# Plan\n\n%s## %s\n\n```yaml\n%s```\n\n%s" % (shared, _UNITS_HEADING, block, "\n".join(bodies))


def _files_body(plan_text: str) -> List[str]:
    """The lines under ``## Files to Modify``."""
    return _body(plan_text, 2, lambda title: title.strip() == _FILES_HEADING) or []


def _listed_files(plan_text: str) -> List[str]:
    """The backticked paths under ``## Files to Modify``."""
    return [_normalise(path) for line in _files_body(plan_text) for path in re.findall(r"`([^`\s]+)`", line)]


# --------------------------------------------------------------------------- checks


def _normalise(path: str) -> str:
    """A ``files`` entry with forward slashes and no leading ``./``."""
    text = path.strip().replace("\\", "/")
    return text[2:] if text.startswith("./") else text


def _is_glob(pattern: str) -> bool:
    return any(char in pattern for char in _GLOB_CHARS)


def _literal_prefix(pattern: str) -> str:
    """What a glob's matches all start with: its text up to the first wildcard."""
    positions = [pattern.index(char) for char in _GLOB_CHARS if char in pattern]
    return pattern[: min(positions)] if positions else pattern


def overlaps(first: str, second: str) -> bool:
    """Whether two entries of ``files`` may name the same file.

    Equal paths, a glob matching the other path, or two globs one of whose
    fixed prefixes starts the other's. ``fnmatch``'s ``*`` crosses ``/``, so a
    prefix compared as text is what decides. A path may name a directory, so
    a path under it, or a glob whose fixed prefix is under it, may name the
    same file.
    """
    if first == second:
        return True
    first_glob, second_glob = _is_glob(first), _is_glob(second)
    if first_glob and fnmatch.fnmatchcase(second, first):
        return True
    if second_glob and fnmatch.fnmatchcase(first, second):
        return True
    if first_glob and second_glob:
        a, b = _literal_prefix(first), _literal_prefix(second)
        return a.startswith(b) or b.startswith(a)
    if not first_glob and not second_glob:
        a, b = first.rstrip("/") + "/", second.rstrip("/") + "/"
        return a.startswith(b) or b.startswith(a)
    pattern, path = (first, second) if first_glob else (second, first)
    return _literal_prefix(pattern).startswith(path.rstrip("/") + "/")


def known_files(root: str) -> Optional[List[str]]:
    """The repository's files, tracked and untracked-not-ignored, ``/``-separated; None when git failed.

    A directory git lists on its own, with a trailing ``/``, is a nested
    repository; it is left out.
    """
    code, out, _ = ws.git_bytes(["ls-files", "--cached", "--others", "--exclude-standard", "-z"], root)
    if code != 0:
        return None
    return sorted({os.fsdecode(raw) for raw in out.split(b"\0") if raw and not raw.endswith(b"/")})


def _named_files(entry: str, files: Sequence[str]) -> Set[str]:
    """The ``files`` an entry of ``files`` names: those a glob matches, or a path and the files under it.

    Text alone cannot tell a directory from a file; the files that exist can.
    ``.`` names the whole tree.
    """
    if _is_glob(entry):
        return {path for path in files if fnmatch.fnmatchcase(path, entry)}
    directory = entry.rstrip("/")
    prefix = "" if directory in ("", ".") else directory + "/"
    return {path for path in files if path == entry or path.startswith(prefix)}


def _cycle(units: Sequence[Unit]) -> List[str]:
    """One dependency cycle as the names along it, first name repeated at the end; [] when none."""
    depends = {unit.name: list(unit.depends_on) for unit in units}
    state: Dict[str, int] = {}  # 1 = on the path, 2 = done
    path: List[str] = []

    def visit(name: str) -> List[str]:
        state[name] = 1
        path.append(name)
        for dep in depends.get(name, []):
            if dep not in depends:
                continue
            if state.get(dep) == 1:
                return [*path[path.index(dep) :], dep]
            if not state.get(dep):
                found = visit(dep)
                if found:
                    return found
        path.pop()
        state[name] = 2
        return []

    for unit in units:
        if not state.get(unit.name):
            found = visit(unit.name)
            if found:
                return found
    return []


def ancestors(units: Sequence[Unit]) -> Dict[str, Set[str]]:
    """Every unit's transitive dependencies. Assumes no cycle and no unknown name."""
    depends = {unit.name: unit.depends_on for unit in units}
    found: Dict[str, Set[str]] = {}

    def of(name: str) -> Set[str]:
        if name not in found:
            found[name] = set()
            for dep in depends.get(name, []):
                found[name] |= {dep} | of(dep)
        return found[name]

    for unit in units:
        of(unit.name)
    return found


def validate(
    units: Sequence[Unit],
    hub_paths: Sequence[str],
    plan_text: Optional[str] = None,
    known_files: Optional[Sequence[str]] = None,
) -> List[str]:
    """Why ``units`` cannot be run; empty means they can.

    With ``plan_text``, each unit also needs its ``### Unit:`` section. With
    ``known_files``, the repository's files, an entry also stands for the
    files it names (``_named_files``): two entries naming a common file
    overlap, and so does an entry naming a hub file. The text rules still
    apply, for files that do not exist yet.
    """
    problems: List[str] = []
    names = [unit.name for unit in units]
    seen: Set[str] = set()
    for name in names:
        if name in seen:
            problems.append("duplicate unit name %r" % name)
        seen.add(name)
    for unit in units:
        for dep in unit.depends_on:
            if dep not in seen:
                problems.append("%s: depends on unknown unit %r" % (unit.name, dep))
            elif dep == unit.name:
                problems.append("%s: depends on itself" % unit.name)
        if plan_text is not None and not section(plan_text, unit.name):
            problems.append("%s: the plan has no ### Unit: %s section" % (unit.name, unit.name))
    if problems:
        return problems
    cycle = _cycle(units)
    if cycle:
        return ["dependency cycle: %s" % " -> ".join(cycle)]

    named: Dict["tuple[str, str]", Set[str]] = {}
    if known_files is not None:
        named = {(unit.name, path): _named_files(path, known_files) for unit in units for path in unit.files}

    def clashes(first: Unit, a: str, second: Unit, b: str) -> bool:
        if overlaps(a, b):
            return True
        return bool(named) and bool(named[(first.name, a)] & named[(second.name, b)])

    above = ancestors(units)
    for index, first in enumerate(units):
        for second in units[index + 1 :]:
            if first.name in above[second.name] or second.name in above[first.name]:
                continue
            for a in first.files:
                clash = [b for b in second.files if clashes(first, a, second, b)]
                if clash:
                    problems.append(
                        "%s and %s may run in parallel but both list %s%s"
                        % (first.name, second.name, a, "" if clash == [a] else " (%s)" % ", ".join(clash))
                    )

    integration = [unit for unit in units if unit.kind == INTEGRATION]
    if len(integration) > 1:
        problems.append("more than one integration unit: %s" % ", ".join(unit.name for unit in integration))
    elif integration:
        last = integration[0]
        missing = [name for name in names if name != last.name and name not in above[last.name]]
        if missing:
            problems.append(
                "%s: an integration unit must depend on every other unit, and does not on %s"
                % (last.name, ", ".join(missing))
            )

    if len(units) >= 2:
        for unit in units:
            if unit.kind == INTEGRATION:
                continue
            for path in unit.files:
                hubs = [hub for hub in hub_paths if overlaps(path, hub)]
                if hubs:
                    problems.append(
                        "%s: %s is a hub file (implementation.hub_paths: %s); "
                        "only the integration unit may list it" % (unit.name, path, ", ".join(hubs))
                    )
                    continue
                held = sorted(
                    found
                    for found in named.get((unit.name, path), set())
                    if any(overlaps(found, hub) for hub in hub_paths)
                )
                if held:
                    problems.append(
                        "%s: %s holds hub file %s (implementation.hub_paths); "
                        "only the integration unit may list it" % (unit.name, path, ", ".join(held))
                    )
    return problems


def warnings(units: Sequence[Unit], plan_text: str) -> List[str]:
    """Unit files the plan's Files to Modify does not list. Only said, never refused."""
    listed = _listed_files(plan_text)
    found: List[str] = []
    for unit in units:
        for path in unit.files:
            if not any(overlaps(path, entry) for entry in listed):
                found.append("%s: %s is not in Files to Modify" % (unit.name, path))
    return found


def schedule(units: Sequence[Unit], max_parallel: int) -> List[List[str]]:
    """The units as waves: each wave's units depend only on earlier waves, at most ``max_parallel`` a wave.

    Taken in the plan's order. Assumes ``validate`` found nothing.
    """
    done: Set[str] = set()
    left = list(units)
    waves: List[List[str]] = []
    width = max(int(max_parallel), 1)
    while left:
        ready = [unit for unit in left if all(dep in done for dep in unit.depends_on)][:width]
        if not ready:  # a cycle: validate refuses it, so this only stops a loop
            break
        waves.append([unit.name for unit in ready])
        done.update(unit.name for unit in ready)
        left = [unit for unit in left if unit.name not in done]
    return waves


# --------------------------------------------------------------------------- state


def locked(workspace: ws.Workspace) -> ws.file_lock:
    """Hold the units lock across a read-modify-write of ``units.json``."""
    return ws.file_lock(workspace.units_path)


def load_state(workspace: ws.Workspace) -> Dict[str, Any]:
    """``units.json``: the plan hash it was written for and a record per unit."""
    data = ws.read_json(workspace.units_path, None)
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("units"), dict):
        data["units"] = {}
    data.setdefault("plan_sha256", "")
    return data


def save_state(workspace: ws.Workspace, state: Dict[str, Any]) -> None:
    workspace.ensure()
    ws.write_json(workspace.units_path, state)


def update(workspace: ws.Workspace, name: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``fields`` into unit ``name``'s record under the lock; the record as written."""
    with locked(workspace):
        state = load_state(workspace)
        record = state["units"].setdefault(name, {})
        record.update(fields)
        save_state(workspace, state)
    return dict(record)


def records(state: Dict[str, Any], plan_sha256: str) -> Dict[str, Dict[str, Any]]:
    """The unit records of ``state`` when it was written for this plan, else none."""
    if not plan_sha256 or state.get("plan_sha256") != plan_sha256:
        return {}
    return {name: entry for name, entry in (state.get("units") or {}).items() if isinstance(entry, dict)}


def status_of(entries: Dict[str, Dict[str, Any]], name: str) -> str:
    return str((entries.get(name) or {}).get("status") or PENDING)


def ready(units: Sequence[Unit], entries: Dict[str, Dict[str, Any]]) -> List[str]:
    """The units not yet started whose dependencies are all merged."""
    return [
        unit.name
        for unit in units
        if status_of(entries, unit.name) == PENDING
        and all(status_of(entries, dep) == MERGED for dep in unit.depends_on)
    ]


def runs_in_place(units: Sequence[Unit], state: Dict[str, Any], name: str) -> bool:
    """Whether unit ``name`` runs in the user's tree, with no worktree and no merge.

    So when nothing could run beside it: no other unit is running, and every
    other unit not yet merged depends on it, however indirectly. Not "every
    dependency merged": under that rule the first of two independent units
    would run in place and keep the other from starting.
    """
    entries = {k: v for k, v in (state.get("units") or {}).items() if isinstance(v, dict)}
    above = ancestors(units)
    for unit in units:
        if unit.name == name:
            continue
        status = status_of(entries, unit.name)
        if status == RUNNING:
            return False
        if status != MERGED and name not in above.get(unit.name, set()):
            return False
    return True


def pass_key(workspace: ws.Workspace, stage: str) -> str:
    """What a pass of ``stage`` is keyed on in the ledger (``Ledger.consume``).

    For the implementer, the plan's sha256: under ``design.require_approval``
    the approved plan's, so a new approval is a new pass. For the architect,
    the sha256 of the last approved plan, or "" before any approval: there is
    no plan yet when the per-unit architects run.
    """
    if stage == "architect":
        approved = workspace.read_state().get(approval_mod.STAGE)
        return str(approved.get("sha256") or "") if isinstance(approved, dict) else ""
    return approval_mod.read_plan(workspace)[1]
