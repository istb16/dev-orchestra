"""Helpers every command module shares: output, loading, the workspace."""

from __future__ import annotations

import argparse
import codecs
import copy
import json
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple, overload

from . import config as config_mod
from . import ledger as ledger_mod
from . import presets as presets_mod
from . import workflow as workflow_mod
from . import workspace as ws
from .providers import MODE_IMPLEMENT, MODE_PLAN, warned_provider
from .summary import render_summary

DEFAULT_MODES = {
    "orchestrator": MODE_PLAN,
    "architect": MODE_PLAN,
    "implementer": MODE_IMPLEMENT,
    "review_fixer": MODE_IMPLEMENT,
}


# --------------------------------------------------------------------------- helpers


#: Error handlers that already guarantee ``write`` cannot raise. Anything else
#: is either strict or one of CPython's own stdio defaults, which sound
#: forgiving and are not: ``surrogateescape`` and ``surrogatepass`` only rescue
#: lone surrogates, so a plain em dash still kills a cp932 console.
_TOLERANT_ERRORS = frozenset({"ignore", "replace", "backslashreplace", "xmlcharrefreplace", "namereplace"})


def _encodes_everything(stream: Any) -> bool:
    """True for a UTF stream, where no character can fail to encode."""
    try:
        return codecs.lookup(getattr(stream, "encoding", "") or "").name.startswith("utf")
    except (LookupError, TypeError):
        return False


def tolerate_console_encoding() -> None:
    """Stop an unencodable character from killing a finished run.

    A Japanese Windows console is cp932, and a delegated agent writes prose:
    one em dash in a summary and ``sys.stdout.write`` raises
    ``UnicodeEncodeError``. Every edit had already been applied, so the work
    was done and only the report of it was lost -- reported from real use.

    The first version of this only relaxed a ``strict`` stream, which is the
    handler Windows never actually uses: CPython gives ``sys.stdout`` the
    ``surrogateescape`` handler there. That is not a choice anybody made and it
    does not help with an em dash, so the guard skipped the one console the
    function existed for.

    ``backslashreplace`` rather than ``replace``: the output is read by the
    orchestrating agent as well as by a person, and ``\u2014`` says which
    character could not be shown while ``?`` throws it away. A stream that can
    encode anything is left as it is, and so is one whose handler someone chose
    deliberately -- those already cannot raise.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # a StringIO under test, or a plain wrapper
            continue
        if _encodes_everything(stream):
            continue
        if (getattr(stream, "errors", "") or "") in _TOLERANT_ERRORS:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):  # pragma: no cover - platform dependent
            pass


def _write(stream: Any, text: str) -> None:
    """Write ``text``, degrading characters rather than dropping the message.

    ``tolerate_console_encoding`` handles this for every stream that can be
    reconfigured. This is for the ones that cannot -- a wrapper someone else
    installed, a pipe already handed to us -- where the alternative is losing
    a whole report to one character in it.
    """
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        stream.write(text.encode(encoding, "backslashreplace").decode(encoding))


def _out(text: str = "") -> None:
    _write(sys.stdout, text + ("\n" if not text.endswith("\n") else ""))


def _err(text: str) -> None:
    _write(sys.stderr, text.rstrip() + "\n")


def _emit_json(data: Any) -> None:
    """``--json`` promises machine-readable output, so it must stay parseable.

    ``ensure_ascii=False`` reads better wherever the console can show the
    character, and on a console that cannot it hands the character to
    ``backslashreplace`` -- which spells it ``\\xe9``, an escape JSON does not
    define. That turns a loud ``UnicodeEncodeError`` into output that parses as
    nothing, or worse, parses wrong. Let JSON do the escaping instead: its own
    ``\\uXXXX`` is ASCII, survives any console, and reads back as the same
    string.
    """
    _out(json.dumps(data, indent=2, ensure_ascii=not _encodes_everything(sys.stdout)))


def _resolve_scope(requested: Optional[str], start: Optional[str] = None) -> str:
    if requested in ("global", "project"):
        return requested
    return "project" if config_mod.find_project_config(start) else "global"


def _layer_path(scope: str, start: Optional[str] = None) -> str:
    return config_mod.global_config_path() if scope == "global" else config_mod.project_config_path(start)


def _read_layer(scope: str, start: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """The layer a writer is about to edit: what is on disk, and nothing else.

    The global layer used to be seeded with the whole of ``default_config()``,
    so the first ``config set`` froze every default beside the one value that
    was asked for. ``version`` identifies the file format rather than
    configuring anything, so it is the one key a writer supplies -- including
    into a file that predates it, which is the only way an edit to such a file
    can leave a file this loader's contract describes.
    """
    path = _layer_path(scope, start)
    layer = config_mod.read_config_file(path) if os.path.isfile(path) else {}
    layer.setdefault("version", config_mod.CONFIG_VERSION)
    return path, layer


def _layer_base(scope: str, start: Optional[str] = None) -> Dict[str, Any]:
    """What would be in force if this layer did not exist.

    The global file is shared by every project on the machine, so no project's
    values may flow into it: its base is the built-in defaults alone. A project
    file sits on top of the global one, so its base is the defaults plus that.
    ``load()`` is the wrong answer for either -- it includes the layer being
    edited, and for the global layer it includes whichever project happens to
    be the working directory.

    This mirrors the layer order in ``config.load``; a third layer would have
    to be added in both places. It leaves out the preset's expansion:
    ``_fitted_base`` is the base with it, through ``config.compose``, and
    ``_prune_base`` is where the two agree.
    """
    defaults = config_mod.default_config()
    if scope == "global":
        return defaults
    global_path = config_mod.global_config_path()
    if not os.path.isfile(global_path):
        return defaults
    return config_mod.deep_merge(defaults, config_mod.read_config_file(global_path))


def _global_file() -> Dict[str, Any]:
    path = config_mod.global_config_path()
    return config_mod.read_config_file(path) if os.path.isfile(path) else {}


def _fitted_base(scope: str, layer: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What ``load()`` gives without this layer, the preset's fit included.

    The global layer's base is the defaults plus the expansion of its own
    preset, read as ``config.compose`` reads it (``standard`` when it names
    none, nothing for a name it does not know); a project layer's is
    ``load()`` without the project file. The panel is still dealt around the
    implementer ``layer`` -- the one being edited -- sets, as it is in force.
    """
    installed = presets_mod.installed_providers()
    if scope == "global":
        return config_mod.compose({"preset": _global_file().get("preset")}, {}, installed, layer)[0]
    return config_mod.compose(_global_file(), {}, installed, layer)[0]


def _agreed(first: Dict[str, Any], second: Dict[str, Any], whole: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """The values ``first`` and ``second`` both hold; the keys in ``whole`` only when equal whole."""
    agreed: Dict[str, Any] = {}
    for key, value in first.items():
        if key not in second:
            continue
        other = second[key]
        if isinstance(value, dict) and isinstance(other, dict) and key not in whole:
            agreed[key] = _agreed(value, other)
        elif value == other:
            agreed[key] = copy.deepcopy(value)
    return agreed


def _prune_base(scope: str, start: Optional[str], layer: Dict[str, Any]) -> Dict[str, Any]:
    """What ``config prune`` compares ``layer`` against.

    A value may go only when the built-in defaults and the preset's fit both
    say it: a governed key the file stops setting falls through to the fit,
    and nothing may be pruned for equalling one machine's fit either. A role
    counts only whole, since dropping its last field hands it to the fit.

    A project file that sets the implementer deals the panel around it, so
    for the global layer the fit that project sees has to agree as well.
    """
    roles = config_mod.KNOWN_ROLES
    base = _agreed(_layer_base(scope, start), _fitted_base(scope, layer), roles)
    if scope == "global":
        project_path = config_mod.find_project_config(start)
        project = config_mod.read_config_file(project_path) if project_path else {}
        if config_mod.mentions(project, "implementer"):
            with_project = config_mod.deep_merge(layer, project)
            base = _agreed(base, _fitted_base(scope, with_project), roles)
    return base


#: Sentinel for "the layer holds nothing here at all", which ``get_path`` cannot
#: otherwise distinguish from a value that happens to be falsy.
_UNSET = object()


def _seed_list(layer: Dict[str, Any], list_path: str, base: Dict[str, Any]) -> bool:
    """Give the layer the whole list before one entry of it is edited.

    A list replaces the one below it wholesale (``deep_merge``), so changing
    one entry is also a decision about the others: they have to be copied from
    what this layer was inheriting, which is ``base`` and never the effective
    configuration -- a global file must not end up holding the panel of
    whichever project the command was run in.

    A path neither side knows is left alone, for ``set_path`` to reject as it
    always has -- and so is one this layer already holds something else at. A
    scalar where a list is expected is the same kind of mistake, and seeding
    over it would replace a value the user wrote instead of refusing.

    Returns True when it seeded, so a writer can say it froze the panel.
    """
    if config_mod.get_path(layer, list_path, _UNSET) is not _UNSET:
        return False
    inherited = config_mod.get_path(base, list_path)
    if isinstance(inherited, list):
        config_mod.set_path(layer, list_path, copy.deepcopy(inherited))
        return True
    return False


def _compose_preview(scope: str, layer: Dict[str, Any]) -> Tuple[Any, ...]:
    """``config.compose`` of this layer as it would be saved: ``(data, fit, preset, source)``."""
    installed = presets_mod.installed_providers()
    if scope == "global":
        return config_mod.compose(layer, {}, installed)
    return config_mod.compose(_global_file(), layer, installed)


#: The one panel-wide problem that names an entry only as an example: which
#: high-risk reviewer it names says nothing about whether a write caused it.
_NO_RISK_PATTERN = "optimization.high_risk_paths: no pattern in force"

#: How a problem names a panel entry: an extra by its file and position, any
#: other reviewer by its place in the panel.
_ENTRY_LABEL = re.compile(r"reviewers_extra\[(\d+)\] in the (global|project) file|reviewers\[(\d+)\]")


def _panel_problems(scope: str, layer: Dict[str, Any]) -> Tuple[List[str], List[Optional[str]]]:
    """The panel problems ``load()`` would raise with this layer saved, and the panel's ids."""
    data, fit, _preset, _source = _compose_preview(scope, layer)
    global_layer, project_layer = (layer, None) if scope == "global" else (_global_file(), layer)
    problems = config_mod.validate(
        data, project_layer=project_layer, global_layer=global_layer, origins=fit.origins
    )
    panel = data.get("reviewers")
    ids = [
        reviewer.get("id") if isinstance(reviewer, dict) and isinstance(reviewer.get("id"), str) else None
        for reviewer in (panel if isinstance(panel, list) else [])
    ]
    return [problem for problem in problems if problem.startswith(("reviewers", _NO_RISK_PATTERN))], ids


def _problem_key(problem: str, ids: List[Optional[str]], removed: Any = None) -> Optional[str]:
    """A problem with each entry it names said by what stays the same across a write.

    A reviewer of the panel by its id, which a removal or a seeded list does
    not move; an extra by its file and position, less one past an extra the
    write ``removed`` -- and None for a problem of that extra itself, which
    the write took away.
    """
    if problem.startswith(_NO_RISK_PATTERN):
        return _NO_RISK_PATTERN
    gone = False

    def entry(match: "re.Match[str]") -> str:
        nonlocal gone
        if match.group(3) is not None:
            index = int(match.group(3))
            name = ids[index] if index < len(ids) else None
            return "reviewers{%s}" % (name if name is not None else "#%d" % index)
        position, layer = int(match.group(1)), match.group(2)
        if removed is not None and removed.key == "reviewers_extra" and removed.layer == layer:
            if position == removed.position:
                gone = True
            elif position > removed.position:
                position -= 1
        return "reviewers_extra[%d] in the %s file" % (position, layer)

    key = _ENTRY_LABEL.sub(entry, problem)
    return None if gone else key


def _panel_write_problems(
    scope: str,
    before: Dict[str, Any],
    after: Dict[str, Any],
    removed: Any = None,
) -> List[str]:
    """The panel problems a write from ``before`` to ``after`` would introduce.

    The one check every writer of a panel path makes before writing: the
    configuration is composed as it would be saved, both files and the
    extras included, so nothing is written that ``load()`` then refuses.
    Only what the write itself introduced is refused, so an already broken
    file stays fixable -- a problem counts as already there only for the
    same entry, so the same mistake made on another one is still refused.
    ``removed`` is the origin of an extra the write took out of its file.
    """
    old_problems, old_ids = _panel_problems(scope, before)
    new_problems, new_ids = _panel_problems(scope, after)
    # A reviewer renamed in place is still the one it was.
    kept = set(new_ids)
    names = [
        old_ids[index]
        if name not in old_ids and index < len(old_ids) and old_ids[index] not in kept
        else name
        for index, name in enumerate(new_ids)
    ]
    known = {_problem_key(problem, old_ids, removed) for problem in old_problems}
    return [problem for problem in new_problems if _problem_key(problem, names) not in known]


def _frozen_panel_note(
    seeded: bool, path: str, base: Dict[str, Any], scope: str, start: Optional[str] = None
) -> Optional[str]:
    """What a writer says when seeding ``reviewers`` took the panel off the fit.

    Only when no file under the written one listed the reviewers before:
    copying a list another file already chose freezes nothing that was
    following a preset. A project file is not under the global one.
    """
    if not seeded:
        return None
    loaded = config_mod.load(start, validate_result=False)
    layers = (loaded.global_layer, loaded.project_layer) if scope == "project" else (loaded.global_layer,)
    if not loaded.preset or any(layer.get("reviewers") is not None for layer in layers):
        return None
    recorded = ", ".join(
        "%s %s" % (reviewer.get("id"), (reviewer.get("model") or {}).get("family", "default"))
        for reviewer in base.get("reviewers") or []
        if isinstance(reviewer, dict)
    )
    message = "note: %s now lists the reviewers; the panel no longer follows preset %s's fit (recorded %s)"
    return message % (path, loaded.preset, recorded or "none")


def _seed_panel(
    scope: str, layer: Dict[str, Any], base: Dict[str, Any], path: str, start: Optional[str] = None
) -> Tuple[Optional[str], Optional[str]]:
    """``_seed_list`` for ``reviewers``, and what the writer says about it.

    A project file may not hold a reviewer on a warned provider, so when the
    panel copied into one is the global preset's fit -- the global file lists
    no reviewers -- those seats are left out of the copy and named. A panel
    the global file lists is copied whole: the user chose those seats, and the
    write says which of them the project file will have refused.

    Returns the note for a write that succeeds, and the note for one that
    fails -- a selector or an index that named a seat left out would
    otherwise read as a plain "not found".
    """
    dropped: List[Tuple[str, str]] = []
    reviewers = base.get("reviewers")
    if scope == "project" and _global_file().get("reviewers") is None and isinstance(reviewers, list):
        kept: List[Any] = []
        for reviewer in reviewers:
            provider = str(reviewer.get("provider") or "") if isinstance(reviewer, dict) else ""
            if warned_provider(provider):
                dropped.append((str(reviewer.get("id")), provider))
            else:
                kept.append(reviewer)
        base = dict(base, reviewers=kept)
    seeded = _seed_list(layer, "reviewers", base)
    frozen = _frozen_panel_note(seeded, path, base, scope, start)
    if not (seeded and dropped):
        return frozen, None
    message = "not copied into %s: %s -- a reviewer on %s is taken only from the global config"
    ids = ", ".join(reviewer_id for reviewer_id, _provider in dropped)
    providers = ", ".join(sorted({provider for _id, provider in dropped}))
    left_out = message % (os.path.basename(path), ids, providers)
    if frozen:
        frozen += "; " + left_out
    return frozen, "note: " + left_out


def _container_and_config(args: argparse.Namespace) -> "tuple[str, str, config_mod.LoadedConfig]":
    """The project root, its ``.ai/``, and the configuration they came from."""
    root = ws.repo_root(getattr(args, "cwd", None) or os.getcwd())
    loaded = config_mod.load(root, validate_result=False)
    return root, loaded.workspace_dir(root), loaded


def _container(args: argparse.Namespace) -> "tuple[str, str]":
    """The project root and its ``.ai/``, before a workflow is chosen."""
    root, container, _ = _container_and_config(args)
    return root, container


#: How many quiet workflows the note names before it says "...".
_STALE_LISTED = 5


def _stale_notice(container: str, workflow: str, days: int) -> None:
    """Advisory only: whatever happens in here, the command goes on unchanged."""
    if days <= 0:
        return
    try:
        stale = workflow_mod.stale_elsewhere(container, workflow, days)
    except Exception:  # a sibling's file is not this command's problem
        return
    if not stale:
        return
    count = len(stale)
    names = ", ".join(stale[:_STALE_LISTED]) + (", ..." if count > _STALE_LISTED else "")
    subject = "workflow has" if count == 1 else "workflows have"
    unit = "day" if days == 1 else "days"
    _err(
        'note: %d %s not been active for %d %s or more (%s). See them with "workflow list"; '
        'remove one with "workflow remove <id> --yes".' % (count, subject, days, unit, names)
    )


def _workspace(args: argparse.Namespace) -> ws.Workspace:
    """The workspace for the workflow this invocation belongs to.

    Every command goes through here, which is why the layout change is one
    function: resolve the id, adopt any pre-0.4.0 artifacts into it, and hand
    back a workspace pointed at that workflow's directory. The call that
    creates the directory is the start of a new workflow, and the one place the
    others that went quiet are noted.
    """
    root, container, loaded = _container_and_config(args)
    requested = getattr(args, "workflow", "") or ""
    if getattr(args, "job_file", None):
        # A detached worker is told its parent's workflow; it does not get to
        # move the pointer, which may have moved on since the parent returned.
        workflow, _ = workflow_mod.resolve(container, requested)
    else:
        workflow = workflow_mod.ensure(container, requested)
    # Before migrate(), which would otherwise create the directory itself.
    fresh = workflow_mod.create_dir(container, workflow)
    moved = workflow_mod.migrate(container, workflow)
    if moved:
        _err(
            "Adopted the previous %s into workflow %s: %s"
            % (os.path.basename(container), workflow, ", ".join(moved))
        )
    workspace = ws.Workspace(root, container, workflow).ensure()
    if fresh:
        _stale_notice(container, workflow, loaded.stale_notice_days())
        if loaded.used_defaults:
            _first_run_summary(loaded)
    return workspace


def _first_run_summary(loaded: config_mod.LoadedConfig) -> None:
    """The whole configuration, once, when a workflow starts with no config file.

    Nothing is saved and nothing is asked: these commands run under an agent
    with no terminal. The choice belongs to ``config setup``.
    """
    _err(render_summary(loaded.data))
    for note in loaded.preset_notes:
        _err("note: %s" % note)
    _err(
        "Save it with config setup --preset %s, or choose another with config setup."
        % (loaded.preset or presets_mod.DEFAULT)
    )


def _review_workspace(args: argparse.Namespace) -> ws.Workspace:
    """The workspace a ``review`` subcommand reads and writes.

    ``--design`` points it at ``reviews/design/``, which is the whole of what
    makes the design review a separate review: its own reports, its own
    consolidated file, and therefore its own round counter and triage.
    """
    workspace = _workspace(args)
    if getattr(args, "design", False):
        return workspace.design_review().ensure()
    return workspace


@overload
def _in_workflow(workspace: ws.Workspace, path: str) -> str: ...
@overload
def _in_workflow(workspace: ws.Workspace, path: Optional[str]) -> Optional[str]: ...
def _in_workflow(workspace: ws.Workspace, path: Optional[str]) -> Optional[str]:
    """Resolve a path written as ``.ai/...`` inside this workflow's directory.

    Every documented command names artifacts by their container-relative path
    -- ``--output .ai/plan.md`` -- which was unambiguous while there was one
    directory per project. It now means "the plan *of this workflow*", so the
    container prefix is rewritten to the workflow's own directory.

    Left exactly as written: anything outside the container, and anything that
    already names a workflow. The second is what keeps a detached worker from
    mapping a path its parent has already mapped.
    """
    if not path or path == "-" or not workspace.workflow:
        return path
    try:
        relative = os.path.relpath(os.path.abspath(path), workspace.container)
    except ValueError:  # pragma: no cover - different drives on Windows
        return path
    parts = relative.replace("\\", "/").split("/")
    if parts[0] in ("..", ws.WORKFLOWS):
        return path
    return os.path.join(workspace.dir, *parts)


def _load_or_die(start: Optional[str] = None) -> config_mod.LoadedConfig:
    try:
        loaded = config_mod.load(start)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        _err("Run `dev-orchestra config setup` to rebuild the configuration.")
        raise SystemExit(2) from exc
    if loaded.used_defaults:
        # One line: every `run` is its own process, and a workflow has many.
        _err(
            "note: no config file; running preset %s fitted to %s (config setup --preset <name> saves one)"
            % (loaded.preset, presets_mod.describe_installed(presets_mod.installed_providers()))
        )
    return loaded


def _load_lenient(start: Optional[str] = None) -> config_mod.LoadedConfig:
    """The configuration as its files say, *not validated*: for reporting and display only.

    ``validate_result=False`` skips ``config.validate`` and with it the
    refusals a project file is held to. Anything that starts a provider run
    loads through :func:`_load_or_die`.
    """
    return config_mod.load(start, validate_result=False)


def _ledger(args: argparse.Namespace, workspace: Optional[ws.Workspace] = None) -> ledger_mod.Ledger:
    workspace = workspace or _workspace(args)
    loaded = config_mod.load(getattr(args, "cwd", None), validate_result=False)
    return ledger_mod.Ledger(workspace, ledger_mod.budget_settings(loaded.data))


def _refuse_if_exhausted(book: ledger_mod.Ledger, stage: str, force: bool) -> Optional[int]:
    """Stop a loop at the action, not with advice from a query command."""
    if force:
        return None
    reasons = book.check(stage)
    if not reasons:
        return None
    _err("refusing to run %s:" % stage)
    for reason in reasons:
        _err("  - %s" % reason)
    _err("Report what is unresolved instead of retrying, or pass --force to override.")
    return ledger_mod.EXIT_BUDGET_EXHAUSTED


def _wrote_plan(workspace: ws.Workspace) -> Callable[[Dict[str, Any]], bool]:
    """Whether an architect run event wrote its answer to this workflow's plan.

    Only such a run is a revision of the plan: an architect run asked
    something else, or left on stdout, is not the revision a round is owed.
    """
    # Resolved, not just made absolute: one plan has more than one spelling
    # when a directory on its path is a link -- macOS's temporary directories
    # live under /var, which is /private/var -- and a correct --output must not
    # be refused for the spelling it was given in.
    plan = os.path.normcase(os.path.realpath(workspace.plan_path))

    def counts(event: Dict[str, Any]) -> bool:
        output = event.get("output")
        if not isinstance(output, str) or not output or output == "-":
            return False
        return os.path.normcase(os.path.realpath(_in_workflow(workspace, output) or output)) == plan

    return counts
