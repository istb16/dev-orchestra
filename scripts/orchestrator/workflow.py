"""Which workflow a command belongs to, and where its artifacts live.

`.ai/` used to be one directory per project, so two sessions working in the
same checkout shared `plan.md`, the review reports, the budgets and the round
counter. Reported from real use: a second session does not announce itself, so
the first one's plan is simply overwritten and its budget spent by someone
else. Artifacts now live in `.ai/workflows/<id>/`, one directory per workflow.

**This separates the bookkeeping, not the work.** The implementer edits the
working tree and the reviewers read `git diff` of that same working tree, and
there is only one of those per checkout. Two workflows running *at the same
time* here still see each other's half-finished edits, whatever directory their
reports are written to. The unit that actually isolates is a worktree:

    git worktree add ../feature-x feature-x

which gives a different repository root, and therefore a different `.ai/`, for
free. `active_elsewhere` exists to say so out loud rather than let the
separated directories imply a safety they do not provide.

The identity is resolved, in order, from an explicit `--workflow`, the
`DEV_ORCHESTRA_WORKFLOW` environment variable, the host's session id, the
pointer this directory last wrote, and finally a new id. A session id is
hashed rather than used as the name: it keeps another tool's internal
identifier out of our paths, it is short, and -- the reason it is preferred
over the pointer -- it is *deterministic*, so every command from one session
resolves to the same workflow without two sessions having to agree on a file.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import workspace as ws

#: Environment variable a caller sets to name the workflow itself.
WORKFLOW_ENV = "DEV_ORCHESTRA_WORKFLOW"

#: Session identifiers the known hosts export. First one set wins. These are
#: other tools' internals, not an interface they promise us, so a missing one
#: is ordinary: resolution simply falls through to the pointer file.
SESSION_ENV = (
    "DEV_ORCHESTRA_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_SESSION_ID",
)

#: Where the last resolved id is remembered, for hosts that export no session.
POINTER = "current.json"

#: What a workflow may be called. Explicit ids are allowed to be readable
#: (`--workflow auth-fix`); nothing that could escape the container is.
_VALID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Artifacts of the pre-0.4.0 layout, moved into a workflow directory once.
LEGACY_ENTRIES = ("plan.md", "state.json", "execution", "reviews", "jobs")


class WorkflowError(ValueError):
    """An id that cannot be used as a directory name."""


def normalise(value: str) -> str:
    """Validate an id supplied from outside, or raise."""
    value = (value or "").strip()
    if not value:
        raise WorkflowError("workflow id is empty")
    if not _VALID.match(value) or value in (".", ".."):
        raise WorkflowError("invalid workflow id %r: use letters, digits, dot, dash or underscore" % value)
    return value


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def from_session(session: str) -> str:
    """A stable short id for a host session, without exposing the session."""
    return hashlib.sha256(session.encode("utf-8")).hexdigest()[:12]


def session_id(env: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if env is None else env
    for name in SESSION_ENV:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def workflows_dir(container: str) -> str:
    return os.path.join(container, ws.WORKFLOWS)


def workflow_dir(container: str, workflow: str) -> str:
    return os.path.join(workflows_dir(container), normalise(workflow))


def pointer_path(container: str) -> str:
    return os.path.join(container, POINTER)


def read_pointer(container: str) -> str:
    data = ws.read_json(pointer_path(container), {}) or {}
    value = str(data.get("workflow") or "")
    try:
        return normalise(value)
    except WorkflowError:
        return ""


def write_pointer(container: str, workflow: str, origin: str = "") -> None:
    os.makedirs(container, exist_ok=True)
    ws.write_json(
        pointer_path(container),
        {"workflow": workflow, "origin": origin, "updated_at": ws.utcnow()},
    )


def resolve(
    container: str,
    requested: str = "",
    env: Optional[Dict[str, str]] = None,
) -> Tuple[str, str]:
    """Return ``(id, origin)`` for the workflow this command belongs to.

    ``origin`` names which rule answered, which is what `workflow show` and
    the status line report: "which workflow am I in, and why that one" is the
    question someone asks when two sessions disagree.
    """
    env = os.environ if env is None else env

    if requested:
        return normalise(requested), "requested"

    named = (env.get(WORKFLOW_ENV) or "").strip()
    if named:
        return normalise(named), "environment"

    session = session_id(env)
    if session:
        return from_session(session), "session"

    remembered = read_pointer(container)
    if remembered:
        return remembered, "pointer"

    return new_id(), "new"


def ensure(container: str, requested: str = "", env: Optional[Dict[str, str]] = None) -> str:
    """Resolve the workflow and record it as the one this directory is on."""
    workflow, origin = resolve(container, requested, env)
    if origin != "pointer" and read_pointer(container) != workflow:
        write_pointer(container, workflow, origin)
    return workflow


# --------------------------------------------------------------------------- listing


def _meta(container: str, workflow: str) -> Dict[str, Any]:
    """What is known about one workflow, read from what it has written."""
    directory = workflow_dir(container, workflow)
    state = ws.read_json(os.path.join(directory, "state.json"), {}) or {}
    ledger = state.get("ledger") if isinstance(state.get("ledger"), dict) else {}
    events = state.get("events") if isinstance(state.get("events"), list) else []
    last = events[-1] if events else {}
    in_flight = ledger.get("in_flight") if isinstance(ledger.get("in_flight"), dict) else {}
    return {
        "workflow": workflow,
        "dir": directory,
        "started_at": str(ledger.get("started_at") or ""),
        "updated_at": str(state.get("updated_at") or ""),
        "last_activity_monotonic": float(ledger.get("last_activity_monotonic") or 0),
        "last_stage": str(last.get("stage") or "") if isinstance(last, dict) else "",
        "last_status": str(last.get("status") or "") if isinstance(last, dict) else "",
        "in_flight": sorted(str(key) for key in in_flight),
        "runs": int(ledger.get("total_delegated_runs") or 0),
    }


def listing(container: str) -> List[Dict[str, Any]]:
    """Every workflow in this container, most recently active first."""
    root = workflows_dir(container)
    if not os.path.isdir(root):
        return []
    entries = []
    for name in sorted(os.listdir(root)):
        if not os.path.isdir(os.path.join(root, name)):
            continue
        try:
            normalise(name)
        except WorkflowError:
            continue  # not ours; leave it alone rather than report it
        entries.append(_meta(container, name))
    entries.sort(key=lambda entry: entry["last_activity_monotonic"], reverse=True)
    return entries


def active_elsewhere(container: str, workflow: str, idle_seconds: float = 900.0) -> List[str]:
    """Other workflows here that look like they are still running.

    The working tree is shared whatever the directories say, so this is the
    warning that separated artifacts would otherwise suppress. "Looks like":
    a stage recorded as in flight, or activity within ``idle_seconds``.
    """
    import time

    now = time.time()
    names = []
    for entry in listing(container):
        if entry["workflow"] == workflow:
            continue
        last = entry["last_activity_monotonic"]
        if entry["in_flight"] or (last and now - last < idle_seconds):
            names.append(entry["workflow"])
    return names


# --------------------------------------------------------------------------- migration


def legacy_artifacts(container: str) -> List[str]:
    """Pre-0.4.0 artifacts sitting directly in the container."""
    if not os.path.isdir(container):
        return []
    return [name for name in LEGACY_ENTRIES if os.path.exists(os.path.join(container, name))]


def migrate(container: str, workflow: str) -> List[str]:
    """Move a flat `.ai/` into `.ai/workflows/<id>/`, once.

    Adopting the old layout rather than ignoring it: a workflow interrupted by
    an upgrade keeps its plan, its review reports and its budget. Nothing is
    deleted and nothing is merged -- if the destination already holds a file of
    that name, the old one is left where it is for someone to look at.
    """
    moved: List[str] = []
    names = legacy_artifacts(container)
    if not names:
        return moved
    destination = workflow_dir(container, workflow)
    os.makedirs(destination, exist_ok=True)
    for name in names:
        source = os.path.join(container, name)
        target = os.path.join(destination, name)
        if os.path.exists(target):
            continue
        try:
            os.replace(source, target)
        except OSError:
            continue
        moved.append(name)
    return moved
