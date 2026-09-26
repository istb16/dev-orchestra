"""``dev-orchestra`` command line interface.

Only the parts that benefit from being deterministic and reusable live here:
configuration, environment diagnosis, provider invocation, and the mechanical
half of the review pipeline (snapshot, fan-out, parse, dedupe). Judgement calls
-- which stages to run, whether a finding is real -- stay with the orchestrating
agent, which drives these commands.
"""

from __future__ import annotations

import argparse
import codecs
import copy
import hashlib
import json
import os
import sys
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import approval as approval_mod
from . import config as config_mod
from . import context as context_mod
from . import doctor as doctor_mod
from . import jobs as jobs_mod
from . import ledger as ledger_mod
from . import miniyaml
from . import optimization as opt_mod
from . import review as review_mod
from . import wizard as wizard_mod
from . import workflow as workflow_mod
from . import workspace as ws
from .providers import (
    MODE_IMPLEMENT,
    MODE_PLAN,
    MODE_REVIEW,
    MODES,
    ModelResolutionError,
    UnknownProviderError,
    adapter_failure,
    available_providers,
    describe_exception,
    describe_origin,
    get_provider,
    origin_payload,
    provider_origin,
)

__version__ = "0.10.0"

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
    to be added in both places.
    """
    defaults = config_mod.default_config()
    if scope == "global":
        return defaults
    global_path = config_mod.global_config_path()
    if not os.path.isfile(global_path):
        return defaults
    return config_mod.deep_merge(defaults, config_mod.read_config_file(global_path))


#: Sentinel for "the layer holds nothing here at all", which ``get_path`` cannot
#: otherwise distinguish from a value that happens to be falsy.
_UNSET = object()


def _seed_list(layer: Dict[str, Any], list_path: str, base: Dict[str, Any]) -> None:
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
    """
    if config_mod.get_path(layer, list_path, _UNSET) is not _UNSET:
        return
    inherited = config_mod.get_path(base, list_path)
    if isinstance(inherited, list):
        config_mod.set_path(layer, list_path, copy.deepcopy(inherited))


def _effective_preview(scope: str, layer: Dict[str, Any], start: Optional[str] = None) -> Dict[str, Any]:
    """What ``load()`` will resolve once this layer is saved."""
    return config_mod.deep_merge(_layer_base(scope, start), layer)


def _container(args: argparse.Namespace) -> "tuple[str, str]":
    """The project root and its ``.ai/``, before a workflow is chosen."""
    root = ws.repo_root(getattr(args, "cwd", None) or os.getcwd())
    loaded = config_mod.load(root, validate_result=False)
    return root, loaded.workspace_dir(root)


def _workspace(args: argparse.Namespace) -> ws.Workspace:
    """The workspace for the workflow this invocation belongs to.

    Every command goes through here, which is why the layout change is one
    function: resolve the id, adopt any pre-0.4.0 artifacts into it, and hand
    back a workspace pointed at that workflow's directory.
    """
    root, container = _container(args)
    workflow = workflow_mod.ensure(container, getattr(args, "workflow", "") or "")
    moved = workflow_mod.migrate(container, workflow)
    if moved:
        _err(
            "Adopted the previous %s into workflow %s: %s"
            % (os.path.basename(container), workflow, ", ".join(moved))
        )
    return ws.Workspace(root, container, workflow).ensure()


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
        return config_mod.load(start)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        _err("Run `dev-orchestra config setup` to rebuild the configuration.")
        raise SystemExit(2) from exc


# --------------------------------------------------------------------------- config


def _describe_spec(spec: Dict[str, Any]) -> str:
    """provider / family / version, the way `config show` says it."""
    model = spec.get("model") or {}
    version = model.get("version", "latest")
    family = model.get("id") if version == "pinned" else model.get("family", "default")
    return "%s / %s / %s" % (spec.get("provider", "?"), family or "default", version)


def _render_layer(path: str, data: Dict[str, Any], exists: bool) -> str:
    """One layer, as it is on disk rather than as a configuration.

    Summarised through ``render_summary`` it would read as a whole setup,
    filling in from the built-in defaults exactly the values this view exists
    to tell apart from what the file overrides.

    ``exists`` rather than emptiness decides which of the two "nothing is
    overridden" sentences this is: an empty file reads as ``{}`` too, and
    reporting it as missing would contradict the ``Source:`` line printed
    directly above.
    """
    if not exists:
        return "No file at %s -- nothing overridden." % path
    if set(data) <= {"version"}:
        return "Nothing overridden -- every value follows the layer below."
    note = "Everything not listed is inherited (config show for the effective configuration)."
    return "%s\n%s" % (miniyaml.dumps(data), note)


def cmd_config_show(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    scoped = args.scope in ("global", "project")
    exists = False
    if args.scope == "global":
        # Deliberately not `_read_layer`: that one supplies what a writer needs
        # a saved file to hold, and reporting it here would show a `version`
        # that is not on disk as though it were.
        path = config_mod.global_config_path()
        exists = os.path.isfile(path)
        data = config_mod.read_config_file(path) if exists else {}
        source = path if exists else "%s (not created yet)" % path
    elif args.scope == "project":
        path = config_mod.project_config_path(args.cwd)
        exists = os.path.isfile(path)
        data = config_mod.read_config_file(path) if exists else {}
        source = path if exists else "no project override"
    else:
        data = loaded.data
        source = "effective (project: %s, global: %s)" % (
            loaded.project_path or "none",
            loaded.global_path or "none",
        )

    referenced = config_mod.referenced_providers(data)
    if args.json:
        providers_payload = {name: origin_payload(name) for name in referenced}
        _emit_json({"source": source, "config": data, "providers": providers_payload})
        return 0
    _out("Source: %s" % source)
    if loaded.used_defaults and not scoped:
        _out("No config file found yet -- showing built-in defaults.")
    _out("")
    if scoped:
        _out(_render_layer(path, data, exists))
    else:
        _out(wizard_mod.render_summary(data) if "orchestrator" in data else "(empty layer)")
    if referenced:
        _out("Providers: %s" % ", ".join(_describe_referenced_provider(name) for name in referenced))
    problems = config_mod.validate(loaded.data)
    if problems:
        _out("Problems:")
        for problem in problems:
            _out("  - %s" % problem)
    return 0


def _describe_referenced_provider(name: str) -> str:
    if provider_origin(name) is None:
        return "%s (no adapter; %s)" % (name, config_mod.user_providers_hint())
    return "%s (%s)" % (name, describe_origin(name))


def cmd_config_path(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    _out("global:  %s%s" % (config_mod.global_config_path(), "" if loaded.global_path else "  (not created)"))
    _out(
        "project: %s"
        % (loaded.project_path or "none (would be %s)" % config_mod.project_config_path(args.cwd))
    )
    return 0


def cmd_config_setup(args: argparse.Namespace) -> int:
    scope = args.scope or "global"
    path = _layer_path(scope, args.cwd)
    existing = config_mod.read_config_file(path) if os.path.isfile(path) else None

    if args.defaults:
        # The recommended configuration *is* the built-in defaults, so the
        # honest way to record a choice of it is to override nothing. Writing
        # the values out would freeze today's copy of them into the file and
        # shadow every later improvement -- the whole of what this file is for.
        data = {"version": config_mod.CONFIG_VERSION}
        save = True
    else:
        if not sys.stdin.isatty() and not args.force:
            _err("config setup needs an interactive terminal; use --defaults for the recommended setup.")
            return 2
        try:
            data, save = wizard_mod.run(wizard_mod.Prompter(), existing, _layer_base(scope, args.cwd))
        except EOFError:
            _err("input ended before setup finished; nothing was saved. Try --defaults instead.")
            return 2

    if not save:
        _out("Not saved.")
        return 1
    # The layer on its own names no roles at all, so it is the configuration it
    # resolves to that has to be valid -- the same dict the wizard summarised.
    problems = config_mod.validate(_effective_preview(scope, data, args.cwd))
    if problems:
        # Writing this would leave every workflow command failing with
        # "invalid configuration" straight after a successful-looking setup.
        _err("not saved -- the configuration is invalid:")
        for problem in problems:
            _err("  - %s" % problem)
        return 2
    config_mod.write_config_file(path, data, scope)
    _out("Saved %s configuration to %s" % (scope, path))
    _out("It records only what you chose; everything else follows %s (config show)." % _below(scope))
    return 0


def _below(scope: str) -> str:
    """What a layer inherits from, named the way a message can use it."""
    return config_mod.layer_below(scope)


def cmd_config_reset(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path = _layer_path(scope, args.cwd)
    if args.delete:
        if os.path.isfile(path):
            os.remove(path)
            _out("Removed %s" % path)
        else:
            _out("Nothing to remove at %s" % path)
        return 0
    # Clearing the overrides, not restoring the defaults: for the global layer
    # those are the same thing, and for a project layer the difference matters
    # -- writing the defaults there would pin them over whatever the global
    # layer says, in a file that is usually committed and read by the team.
    config_mod.write_config_file(path, {"version": config_mod.CONFIG_VERSION}, scope)
    if scope == "global":
        following = "every value now follows the built-in defaults"
    else:
        following = "this project now follows the global layer"
    _out("Reset %s configuration: overrides cleared, %s (%s)" % (scope, following, path))
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    if "[" in args.path:
        _seed_list(layer, args.path.split("[", 1)[0], _layer_base(scope, args.cwd))
    value = args.value if args.raw else config_mod.coerce_scalar(args.value)
    try:
        config_mod.set_path(layer, args.path, value)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    except IndexError:
        # `set_path` assigns straight into the list, so an index past its end
        # arrives as a bare IndexError and `main` catches only the config
        # errors. This is the one entry point that takes an index at all, so it
        # is the one that owes the user a message instead of a traceback.
        _err("%s: index out of range" % args.path)
        return 2
    config_mod.write_config_file(path, layer, scope)
    _out("%s = %r  (%s: %s)" % (args.path, value, scope, path))
    effective = config_mod.load(args.cwd, validate_result=False).data
    for problem in config_mod.validate(effective):
        _err("warning: %s" % problem)
    _warn_unresolvable(effective, args.path.split(".")[0])
    return 0


def cmd_config_prune(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path = _layer_path(scope, args.cwd)
    if not os.path.isfile(path):
        _err("no %s configuration file at %s" % (scope, path))
        return 2
    layer = config_mod.read_config_file(path)
    base = _layer_base(scope, args.cwd)
    pruned, dropped = config_mod.prune_layer(layer, base)
    if config_mod.deep_merge(base, pruned) != config_mod.deep_merge(base, layer):
        # Nothing should be able to get here. It is checked anyway because the
        # failure would be a configuration quietly changing underneath someone
        # who asked for it not to.
        _err("%s: pruning would change the effective configuration, so nothing was written." % path)
        return 2

    if not dropped:
        _out("Nothing to drop from %s: it already holds only its own decisions." % path)
        # Dropping values is not the only thing pruning does: `prune_layer` also
        # supplies the format version a file written before it existed never
        # had. Returning on an empty `dropped` left such a file unnormalised
        # whenever it happened to hold nothing redundant.
        if pruned == layer:
            return 0
        if args.dry_run:
            _out("The configuration format version would be recorded (dry run, nothing written).")
            return 0
        config_mod.write_config_file(path, pruned, scope)
        _out("Recorded the configuration format version in %s." % path)
        return 0
    for entry in dropped:
        _out("  %-40s %s" % (entry["setting"], _prune_value(entry["value"])))
    _out("Values equal to the current default were assumed to be inherited.")
    if args.dry_run:
        _out("%d would be dropped from %s (dry run, nothing written)." % (len(dropped), path))
        return 0
    config_mod.write_config_file(path, pruned, scope)
    _out("Dropped %d from %s; they now follow %s." % (len(dropped), path, _below(scope)))
    return 0


def _prune_value(value: Any) -> str:
    """A list is named by its length, for the reason ``pinned_differences`` gives."""
    if isinstance(value, list):
        return "%d entries" % len(value)
    return repr(value)


def _warn_unresolvable(effective: Dict[str, Any], role_key: str) -> None:
    """Changing a provider can strand a model family that only the old CLI knew."""
    spec = effective.get(role_key)
    if not isinstance(spec, dict) or not spec.get("provider"):
        return
    name = str(spec["provider"])
    try:
        get_provider(name).resolve_model(spec.get("model"))
    except ModelResolutionError as exc:
        _err("warning: %s" % exc)
    except UnknownProviderError:
        pass
    except Exception as exc:
        _err("warning: %s" % adapter_failure(name, exc))


def cmd_config_validate(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    problems = config_mod.validate(loaded.data)
    if args.json:
        _emit_json({"valid": not problems, "problems": problems})
    elif problems:
        _out("Invalid configuration:")
        for problem in problems:
            _out("  - %s" % problem)
    else:
        _out("Configuration is valid.")
    return 1 if problems else 0


# --------------------------------------------------------------------------- models


def cmd_model_list(args: argparse.Namespace) -> int:
    names = [args.provider] if args.provider else available_providers()
    if args.provider and provider_origin(args.provider) is None:
        _err("unknown provider %r (known: %s)" % (args.provider, ", ".join(available_providers())))
        return 2
    payload: Dict[str, Any] = {}
    for name in names:
        # One adapter at a time, so a user adapter that raises is reported
        # against its file and the others are still listed.
        entry: Dict[str, Any] = {
            "installed": False,
            "version": None,
            "models": [],
            "fallback_updated": None,
            "adapter_error": None,
            "origin": origin_payload(name),
        }
        try:
            provider = get_provider(name)
            detection = provider.detect()
            models: List[Dict[str, Any]] = []
            if detection.installed:
                models = [candidate.to_dict() for candidate in provider.list_models()]
            entry.update(
                installed=detection.installed,
                version=detection.version,
                models=models,
                fallback_updated=provider.fallback_updated,
            )
        except Exception as exc:
            entry["adapter_error"] = describe_exception(exc)
        payload[name] = entry

    # Discovery that failed is a failure, even with the other results printed.
    status = 1 if any(entry["adapter_error"] for entry in payload.values()) else 0
    if args.json:
        _emit_json(payload)
        return status
    for name, entry in payload.items():
        if entry.get("adapter_error"):
            _out("%s: adapter failed (%s): %s" % (name, describe_origin(name), entry["adapter_error"]))
            continue
        _out("%s: %s" % (name, "installed" if entry["installed"] else "not installed"))
        if not entry["installed"]:
            continue
        for model in entry["models"]:
            _out("  %-28s family=%-20s source=%s" % (model["label"], model["family"], model["source"]))
        if all(m["source"] == "builtin-fallback" for m in entry["models"]) and entry["models"]:
            _out("  (built-in fallback list, last reviewed %s)" % entry["fallback_updated"])
    return status


# --------------------------------------------------------------------------- reviewers


def cmd_reviewer_list(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    reviewers = loaded.reviewers()
    if args.json:
        _emit_json(reviewers)
        return 0
    if not reviewers:
        _out("No reviewers configured.")
        return 0
    for index, reviewer in enumerate(reviewers, 1):
        model = reviewer.get("model") or {}
        _out(
            "%d. %-18s %-8s %-18s %-8s %s"
            % (
                index,
                reviewer.get("id"),
                reviewer.get("provider"),
                model.get("family", "default"),
                model.get("version", "latest"),
                reviewer.get("role", "general"),
            )
        )
    return 0


def cmd_reviewer_add(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    _seed_list(layer, "reviewers", _layer_base(scope, args.cwd))
    role = args.role or "general"
    reviewer_id = args.id or config_mod.suggest_reviewer_id(layer, args.provider, role)
    family = args.model
    if family is None:
        family = "opus" if args.provider == "claude" else "recommended-coding"
    reviewer = config_mod.make_reviewer(
        reviewer_id,
        args.provider,
        family,
        role,
        version="pinned" if args.pin else "latest",
        model_id=args.pin,
    )
    try:
        config_mod.add_reviewer(layer, reviewer)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    problems = config_mod.validate(config_mod.deep_merge(config_mod.default_config(), layer))
    blocking = [p for p in problems if p.startswith("reviewers")]
    if blocking:
        for problem in blocking:
            _err(problem)
        return 2
    config_mod.write_config_file(path, layer, scope)
    _out("Added reviewer %s (%s / %s / %s) to %s" % (reviewer_id, args.provider, family, role, path))
    return 0


def cmd_reviewer_remove(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    _seed_list(layer, "reviewers", _layer_base(scope, args.cwd))
    try:
        _, removed = config_mod.remove_reviewer(layer, args.selector)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    config_mod.write_config_file(path, layer, scope)
    _out("Removed reviewer %s from %s" % (removed.get("id"), path))
    return 0


def cmd_reviewer_set(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    _seed_list(layer, "reviewers", _layer_base(scope, args.cwd))
    try:
        index, reviewer = config_mod.find_reviewer(layer, args.selector)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    if args.provider:
        reviewer["provider"] = args.provider
    if args.role:
        reviewer["role"] = args.role
    if args.model or args.pin:
        model: Dict[str, Any] = {"family": args.model or reviewer.get("model", {}).get("family")}
        model["version"] = "pinned" if args.pin else "latest"
        if args.pin:
            model["id"] = args.pin
        reviewer["model"] = model
    if args.id:
        reviewer["id"] = args.id
    layer["reviewers"][index] = reviewer
    problems = [
        p
        for p in config_mod.validate(config_mod.deep_merge(config_mod.default_config(), layer))
        if p.startswith("reviewers")
    ]
    if problems:
        for problem in problems:
            _err(problem)
        return 2
    config_mod.write_config_file(path, layer, scope)
    _out("Updated reviewer %s in %s" % (reviewer.get("id"), path))
    return 0


# --------------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor_mod.collect(args.cwd, probe_models=not args.fast)
    if args.json:
        _emit_json(report)
    else:
        _out(doctor_mod.render(report))
    return doctor_mod.exit_code(report) if args.strict else 0


# --------------------------------------------------------------------------- run


def _require_prompt(text: str, source: str) -> str:
    """Refuse a prompt that arrived empty, whichever way it arrived.

    An empty prompt is delegated like any other, and the provider then refuses
    it in its own words -- about its stdin, naming neither the source the
    caller used nor the mistake they made. It also costs an attempt to hear it.
    """
    if not text.strip():
        raise SystemExit("empty prompt: %s; nothing was delegated" % source)
    return text


def _both_paths(written: str, resolved: Optional[str]) -> str:
    """Name a path both ways when ``_in_workflow`` rewrote it.

    The resolved path alone is one the caller never typed, and a caller looking
    for their own typo needs to see what they wrote.
    """
    if not resolved or resolved == written:
        return written
    return "%s (resolved to %s)" % (written, resolved)


def _read_prompt(args: argparse.Namespace, workspace: Optional[ws.Workspace] = None) -> str:
    if args.prompt_file:
        if args.prompt_file == "-":
            return _require_prompt(sys.stdin.read(), "stdin carried nothing")
        path = _in_workflow(workspace, args.prompt_file) if workspace else args.prompt_file
        named = _both_paths(args.prompt_file, path)
        # Deliberately not ws.read_text: its default is right for a report that
        # may legitimately be absent, and turns a mistyped --prompt-file into an
        # empty prompt that runs. Read it so the failure is the caller's to see,
        # and so "no such file" stays distinct from "there and empty" -- they
        # are different mistakes.
        if not os.path.isfile(path):
            trouble = "does not exist" if not os.path.exists(path) else "is not a file"
            raise SystemExit("prompt file %s: %s" % (trouble, named))
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as exc:
            raise SystemExit("prompt file cannot be read: %s (%s)" % (named, exc)) from exc
        return _require_prompt(text, "%s is empty" % named)
    if args.prompt is not None:
        # Tested against None, not truthiness: `--prompt ""` used to fall
        # through to the stdin branch, where a pipe made it someone else's
        # empty prompt and a terminal blamed a missing one.
        return _require_prompt(args.prompt, "--prompt was empty")
    if not sys.stdin.isatty():
        return _require_prompt(sys.stdin.read(), "the piped stdin was empty")
    raise SystemExit("no prompt supplied: use --prompt, --prompt-file, or pipe one in")


class _Refused(NamedTuple):
    """Why ``--output`` was not written, and what became of the stdout."""

    message: str
    rejected_file: Optional[str] = None
    #: Set only when the sidecar could not be written, so the caller can print
    #: what would otherwise be lost.
    unsaved_output: str = ""


def _remove_if_present(path: str) -> None:
    """Remove ``path``. Gone is the state being asked for, so gone already is not a failure."""
    try:
        os.remove(path)
    except OSError:
        pass


def _answered(result: Any) -> bool:
    """Whether a run produced a result: it ended ``ok`` and printed something.

    One function because ``--output`` and the bare print ask the same question,
    and which of them the caller used says nothing about whether the run
    answered. "Printed something" is ``strip()``-empty or not; see
    ``_save_output`` for why nothing finer.
    """
    return bool(result.ok and result.stdout.strip())


def _save_output(role: str, path: str, result: Any) -> Optional[_Refused]:
    """Save a run's stdout to ``path``; return the complaint if it was refused.

    The file named by ``--output`` is usually the one the run was asked to
    revise, so writing a bad result destroys the input: a stalled Architect
    replaced a 50,088-byte plan with the 150 bytes it had emitted before going
    quiet. Only an ``ok`` run that produced something may overwrite, and
    "produced something" is ``strip()``-empty or not. Nothing finer: a size
    threshold is a number wrong for some role, and a short-but-real result is
    the caller's to judge, not this function's.

    The refused stdout goes to a sidecar rather than nowhere. It used to
    survive by landing in the target, and it is often the only account of what
    the run did instead of the work. The same ``strip()`` decides there is
    anything to keep -- a sidecar holding two newlines is a file to delete.
    """
    sidecar = path + ".rejected"
    # An earlier attempt's sidecar is not this run's account of itself, and the
    # operator is told to read one before spending the next attempt. Cleared
    # first so no branch below can leave one behind by forgetting to.
    _remove_if_present(sidecar)
    if _answered(result):
        ws.write_text(path, result.stdout)
        return None
    message = "%s produced nothing usable; %s is unchanged." % (role, path)
    if not result.stdout.strip():
        return _Refused(message)
    try:
        ws.write_text(sidecar, result.stdout)
    except OSError as exc:
        # Keeping the output is the convenience; failing at it must cost only
        # the convenience, not the output and not the report of the run itself.
        return _Refused(
            message + " It could not be kept in %s (%s), so it follows here." % (sidecar, exc),
            unsaved_output=result.stdout,
        )
    return _Refused(message + " Its partial output is in %s." % sidecar, rejected_file=sidecar)


def cmd_run(args: argparse.Namespace) -> int:
    loaded = _load_or_die(args.cwd)
    role = args.role
    tier = args.tier
    try:
        if role in config_mod.KNOWN_ROLES:
            spec = loaded.role(role, tier)
        elif tier:
            # Reviewers are already one model each; the panel is the routing.
            _err("--tier applies to %s, not to a reviewer" % ", ".join(config_mod.KNOWN_ROLES))
            return 2
        else:
            spec = _reviewer_spec(loaded, role)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2

    mode = args.mode or DEFAULT_MODES.get(role, MODE_REVIEW)
    workspace = _workspace(args)
    provider_name = str(spec.get("provider"))
    try:
        provider = get_provider(provider_name)
    except ValueError as exc:
        _err(str(exc))
        return 2

    if args.print_command:
        try:
            resolved = provider.resolve_model(spec.get("model"))
        except ModelResolutionError as exc:
            _err(str(exc))
            return 2
        _out(
            " ".join(
                provider.build_command(mode, resolved, workspace.root, args.extra or [], spec.get("options"))
            )
        )
        return 0

    try:
        prompt = _read_prompt(args, workspace)
    except SystemExit as exc:
        # A worker's stderr is DEVNULL, so a reason left there reaches nobody:
        # every exit this function can take before the outcome is written has
        # to put its message in the job record, or the run is reported only as
        # a worker that vanished having recorded no outcome -- which is also
        # what is said about one that was killed. The same goes for the
        # ModelResolutionError exit below. The foreground call keeps the
        # message on stderr, where its caller is watching.
        if args.job_file:
            jobs_mod.finish(args.job_file, "failed", error=str(exc))
        raise
    # The approval gate. Before the ledger is opened and before the budget is
    # consumed: a refused run must cost nothing, and before --detach: a worker
    # must never be the process that finds out first. The worker checks again
    # all the same -- --job-file is a flag on a public parser, and a worker
    # that trusted its parent would be the way round the gate; its refusal
    # goes into the job record, for the reason given above about stderr. That
    # refusal costs the attempt the parent consumed before handing over: it
    # is not given back.
    # --force is deliberately not honoured here. It overrides a budget, which
    # is a resource; approval is the user's consent, and the human who would
    # force past it is the human who can say yes, which `design approve`
    # records.
    if role == "implementer":
        refusal = _refuse_unless_approved(loaded, workspace, args, role)
        if refusal is not None:
            return refusal
    settings = loaded.review_settings()
    timeout = args.timeout or int(settings.get("timeout_seconds", 1800))
    idle_timeout = args.idle_timeout
    if idle_timeout is None:
        idle_timeout = settings.get("idle_timeout_seconds")

    book = _ledger(args, workspace)
    book.clear_stalls()
    if tier:
        _err("note: running %s on its %r tier (%s)" % (role, tier, _describe_spec(spec)))
    refusal = _refuse_if_exhausted(book, role, args.force)
    if refusal is not None:
        return refusal
    try:
        if not args.job_file:
            book.consume(role, force=args.force)
    except ledger_mod.BudgetExhausted as exc:
        _err(str(exc))
        return ledger_mod.EXIT_BUDGET_EXHAUSTED

    if args.detach:
        # Hand the work to a detached worker so this call cannot block. The
        # budget was already consumed above, so the worker must not do it again.
        passthrough = _detached_argv(args, role)
        job = jobs_mod.start(workspace, role, passthrough, prompt=prompt, timeout=timeout)
        if args.json:
            _emit_json(job)
        else:
            _out("started %s as job %s" % (role, job["id"]))
            _out("follow it with: dev-orchestra jobs wait %s" % job["id"])
        return 0 if job.get("status") != "failed" else 1

    # Written down before the call, so a stall is visible from outside this
    # process and survives it dying.
    if args.job_file:
        jobs_mod.claim(args.job_file)
    token = book.begin(
        role,
        {
            "mode": mode,
            "provider": provider_name,
            "command": provider.executable,
            "job": args.job_file,
            "tier": tier or None,
        },
        deadline=timeout,
    )
    try:
        result = provider.run(
            prompt,
            mode,
            workspace.root,
            spec.get("model"),
            timeout=timeout,
            extra_args=args.extra or [],
            options=spec.get("options"),
            idle_timeout=idle_timeout,
        )
    except ModelResolutionError as exc:
        book.end(token, "failed", {"error": str(exc)})
        if args.job_file:
            jobs_mod.finish(args.job_file, "failed", error=str(exc))
        _err(str(exc))
        return 2

    if args.job_file:
        jobs_mod.finish(
            args.job_file,
            "succeeded" if result.ok else "failed",
            output=result.stdout,
            error="" if result.ok else (result.stderr or "").strip()[:2000],
            detail={
                "exit_code": result.exit_code,
                "stalled": result.stalled,
                "timed_out": result.timed_out,
                "duration_seconds": round(result.duration, 2),
                "model": result.resolved.display if result.resolved else None,
            },
        )
    # Recorded whatever the outcome -- a failed run still spent what it spent.
    # A run that never started one is a different thing, and counting it as
    # unreported would make the account call itself incomplete over a run with
    # nothing to report.
    if result.invoked:
        # Labelled by tier when there is one, so `tokens show` can answer the
        # question a tier exists to raise: did the cheaper one cost less.
        book.record_usage(role, result.usage.to_dict(), label="%s:%s" % (role, tier) if tier else "")
    book.end(
        token,
        "ok" if result.ok else ("stalled" if result.stalled else "failed"),
        {
            "mode": mode,
            "provider": provider_name,
            # Recorded on the *end* event, not only on the start: the start
            # entry is dropped from the ledger when the stage finishes, and
            # the run log is what a report is written from.
            "tier": tier or None,
            "model": result.resolved.display if result.resolved else None,
            "model_source": result.resolved.source if result.resolved else None,
            "duration_seconds": round(result.duration, 2),
            "timed_out": result.timed_out,
            "stalled": result.stalled,
            "output": args.output,
            "billed_tokens": result.usage.billed_tokens,
            # Whether the run left a usable result: an ``ok`` over silence
            # saved nothing, and `status` must not count it as the revision
            # or fix that was asked for. Pure, so the order below stands.
            "answered": _answered(result),
        },
        # What the child was measured to take, whatever it exited with. A run
        # killed at its deadline spent the time it spent; so did a failed one.
        charged_seconds=result.duration,
    )

    # Printed last, and after the books are closed. Showing the output used to
    # come first, so a console that could not encode one character of it took
    # the accounting and the in-flight entry down with it: the tokens went
    # unrecorded and the next command reported this finished run as abandoned.
    # Nothing below this line is allowed to decide whether the run happened.
    refused = None
    answered_nothing = False
    target = ""
    if args.output:
        target = _in_workflow(workspace, args.output)
        refused = _save_output(role, target, result)
    elif not args.job_file:
        _out(result.stdout)
        # The same judgement `--output` refuses a write on. A run with nowhere
        # to write has no refusal to report, so an `ok` run that printed
        # nothing used to be reported as a success by printing that nothing.
        # The --job-file branch is left out: a worker's stdout is in the job,
        # and `jobs wait` reports the outcome.
        answered_nothing = result.ok and not _answered(result)
    if result.stalled:
        _err(
            "%s produced no output for %.0fs and was treated as stalled (not merely slow)."
            % (role, result.idle_for)
        )
    elif result.timed_out:
        _err("%s hit its %ss deadline and was killed." % (role, timeout))
    elif not result.ok:
        _err("%s failed (exit %s): %s" % (role, result.exit_code, result.stderr.strip()[:500]))
    elif answered_nothing:
        # Reported with the outcomes rather than beside the print: an exit 0
        # over silence is a diagnosis, the same one the other branches make.
        # The raw stderr comes along because a CLI that refused the prompt says
        # why there and nowhere else.
        complaint = "%s exited 0 but produced no output." % role
        first_words = result.stderr.strip()[:500]
        if first_words:
            complaint += " Its stderr began: %s" % first_words
        _err(complaint)
    # Said here rather than beside the write, so a refusal and the outcome that
    # caused it read as one report instead of two lines that look at odds.
    if refused:
        _err(refused.message)
        if refused.unsaved_output:
            _err(refused.unsaved_output)
        if args.job_file:
            # A detached worker's stderr goes to DEVNULL, so the job record is
            # the only reader this refusal has. Written as a second update
            # rather than folded into the first: the accounting above must not
            # wait on the save to decide that the run happened.
            jobs_mod.finish(
                args.job_file,
                "succeeded" if result.ok else "failed",
                detail={
                    "output_written": False,
                    "output_target": target,
                    "rejected_file": refused.rejected_file,
                },
            )
    if result.orphans_possible:
        _err("warning: %s's process group may have left orphans; check for stray processes." % role)
    # A refused write exits non-zero even when the run itself was fine: the
    # promise `--output` makes is that the named file holds this run's result,
    # and exiting 0 over an untouched one lets the next command in a chain read
    # the stale file as if it were new. A run with nothing to show exits the
    # same way for the same reason -- an empty answer is not a result.
    return 0 if result.ok and not refused and not answered_nothing else 1


def _refuse_unless_approved(
    loaded: config_mod.LoadedConfig, workspace: ws.Workspace, args: argparse.Namespace, role: str
) -> Optional[int]:
    """Refuse the implementer a plan the user has not approved as it is now."""
    info = approval_mod.current(workspace, bool(loaded.design_settings().get("require_approval")))
    if info["state"] not in approval_mod.REFUSED:
        return None
    lines = approval_mod.refusal_lines(info, workspace.relative(workspace.plan_path))
    # The whole refusal, not its first line: the instruction to ask the user
    # is the part a worker's reader most needs, and the job is all it has.
    if args.job_file:
        _record_worker_refusal(args, workspace, role, "\n".join(lines))
    for line in lines:
        _err(line)
    return approval_mod.EXIT_APPROVAL_REQUIRED


def _record_worker_refusal(args: argparse.Namespace, workspace: ws.Workspace, role: str, error: str) -> None:
    """Record a worker's refusal in its job record, the only place it can say so."""
    jobs_mod.finish(args.job_file, "failed", error=error)


def _reviewer_spec(loaded: config_mod.LoadedConfig, selector: str) -> Dict[str, Any]:
    _, reviewer = config_mod.find_reviewer(loaded.data, selector)
    return reviewer


# --------------------------------------------------------------------------- review


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


def _refuse_if_runtime_spent(book: ledger_mod.Ledger, stage: str, force: bool) -> Optional[int]:
    """The runtime budget, applied to the stages that do not consume attempts.

    Review is the largest consumer of delegated runtime -- a round is a run per
    panel member -- and until now it was the one consumer that never asked. A
    budget the biggest spender does not consult is the "guard in name only"
    this module's ledger exists to stop being.

    Only the runtime reason, not ``check()``: that would also apply the
    attempt, total-run and no-progress rules to review, which are governed by
    ``review.max_review_iterations`` and decided above this point.
    """
    if force:
        return None
    reason = book.runtime_refusal()
    if not reason:
        return None
    _err("refusing to run %s:" % stage)
    _err("  - %s" % reason)
    _err("Report what is unresolved instead of retrying, or pass --force to override.")
    return ledger_mod.EXIT_BUDGET_EXHAUSTED


def _refuse_if_over_context(
    workspace: ws.Workspace,
    stage: str,
    budget_chars: int,
    max_chars: int,
    delivery_chars: int,
    inline_chars: int,
    force: bool,
    iteration: int,
    detail: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Stop a round whose change body is too large to review at all.

    Written down even though nothing ran, and *because* nothing ran: this is
    the largest thing the limit ever saves, and a saving that leaves no trace
    cannot be counted. The code path passes its ``optimization`` block in
    ``detail`` for the same reason the gate refusal carries one --
    ``summarise_rounds`` selects rounds on that key, so a refusal without it
    is a refusal no report can see.

    Nothing is spent by getting here: no budget consumed, no ledger entry
    opened, and on the design path the previous round's plan is left frozen
    where it was, so the triage this refusal asks the reader to report on is
    still there to report.

    ``budget_chars`` is what the limit measures and what the refusal records;
    ``delivery_chars`` is the body that would go into the prompt, and decides
    only what forcing would do -- against ``inline_chars``, which is the other
    limit and not this one. They differ on the design path -- see
    ``over_budget_note`` -- and all three are passed on both paths.
    """
    if force or not review_mod.over_context(budget_chars, max_chars):
        return None
    event = {"iteration": iteration, "refused_by": "context", "reviewers": []}
    event.update(detail or {})
    # The inline limit is not recorded here. It decided nothing about this
    # round -- nothing was delivered -- and it only shapes the last line of the
    # message below, which is about a round that does not exist yet.
    event["context"] = {"chars": budget_chars, "max_chars": max_chars}
    workspace.record_event(stage, opt_mod.REFUSED, event)
    design = stage == "design_review"
    for line in review_mod.over_budget_note(
        budget_chars, max_chars, delivery_chars, inline_chars, design=design
    ):
        _err(line)
    return ledger_mod.EXIT_BUDGET_EXHAUSTED


def _lineage(args: argparse.Namespace, workspace: ws.Workspace) -> str:
    """Which review the next round belongs to.

    Keyed partly on the ledger's workflow, so ``budget reset`` clears the
    round counter along with everything else it claims to clear. It used to
    say "this is now a fresh workflow" and leave the one counter that refuses
    work untouched.
    """
    return review_mod.review_lineage(workspace, _ledger(args, workspace).workflow_id())


def _iteration(
    args: argparse.Namespace,
    workspace: ws.Workspace,
    lineage: str = "",
    current_sha: Optional[str] = None,
) -> int:
    """An explicit --iteration wins; otherwise derive it from what is on disk.

    ``current_sha`` is the snapshot a round would take but has not written yet,
    which is how a round can be costed before it is allowed to replace one.
    """
    given = getattr(args, "iteration", None)
    if given is not None:
        return int(given)
    return review_mod.next_iteration(workspace, lineage or _lineage(args, workspace), current_sha)


def _merge_runs(workspace: ws.Workspace, run_dicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep reviewers that did not run this time in the reviewer table."""
    previous = (ws.read_json(workspace.consolidated_json_path, {}) or {}).get("reviewers", [])
    fresh = {entry.get("id") for entry in run_dicts}
    kept = [
        dict(entry, status=entry.get("status", "ok")) for entry in previous if entry.get("id") not in fresh
    ]
    return run_dicts + kept


def _same_snapshot_report(workspace: ws.Workspace, meta: Dict[str, Any]) -> Dict[str, Any]:
    """The consolidated report when its last run reviewed this snapshot, else empty.

    Whatever the lineage: ``build_consolidation`` carries triage over by
    finding key across a lineage change too, so a rebuild after ``budget
    reset`` can lose a decision just as one without it can.
    """
    previous = ws.read_json(workspace.consolidated_json_path, {}) or {}
    sha = str(meta.get("sha256") or "")
    if not previous or not sha or str((previous.get("snapshot") or {}).get("sha256") or "") != sha:
        return {}
    return previous


def _measurement_rerun(workspace: ws.Workspace, meta: Dict[str, Any], lineage: str, iteration: int) -> bool:
    """Whether this run reviews again the snapshot the last run of this review did.

    The condition ``next_iteration`` keeps the current round on, and the round
    this run records has to be that round: an explicit ``--iteration`` that
    names another is a round of its own, and registers its signature.
    """
    previous = _same_snapshot_report(workspace, meta)
    if not previous or str(previous.get("lineage") or "") != lineage:
        return False
    return iteration == int(previous.get("iteration") or 0)


def _triage_record(entry: Dict[str, Any]) -> Dict[str, str]:
    """What triage keeps on a finding: the decision and its note."""
    return {
        "triage": str(entry.get("triage") or "needs-triage"),
        "triage_note": str(entry.get("triage_note") or ""),
    }


def _triage_changed_since_build(consolidated: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The findings whose triage or note was set after the last run built them.

    Measured against the triage that run recorded, so a decision carried in
    from an earlier round is not one made on this snapshot. A report without
    that record -- built without the flag, or by ``review consolidate`` --
    counts every finding with a decision or a note.
    """
    findings = [entry for entry in consolidated.get("findings") or [] if isinstance(entry, dict)]
    baseline = (consolidated.get("measurement") or {}).get("triage_at_build")
    untouched = {"triage": "needs-triage", "triage_note": ""}
    if not isinstance(baseline, dict):
        baseline = {}
    return [entry for entry in findings if _triage_record(entry) != baseline.get(entry.get("key"), untouched)]


def _measurement_inputs(
    args: argparse.Namespace, plan: opt_mod.Plan, inline_chars: int, max_chars: int
) -> Dict[str, Any]:
    """The prompt inputs other than the context, which a pair has to hold equal.

    The extra context is kept as a digest: the record is for telling two runs
    apart, not for keeping what a caller passed.
    """
    extra = args.context or ""
    return {
        "context_sha256": hashlib.sha256(extra.encode("utf-8")).hexdigest() if extra else "",
        "max_findings": plan.max_findings,
        "inline_chars": inline_chars,
        "max_chars": max_chars,
        "force": bool(args.force),
    }


def _measurement_block(
    meta: Dict[str, Any],
    workspace: ws.Workspace,
    book: ledger_mod.Ledger,
    override: str,
    rerun: bool,
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    """What ``optimization report`` pairs a ``--surrounding`` run on.

    The whole snapshot identity rather than the reviewer entries' short sha,
    and the workflow directory apart from the budget epoch: a pair is keyed on
    the first, and ``budget reset`` between the two runs changes only the second.
    """
    frozen = meta.get("surrounding") if isinstance(meta.get("surrounding"), dict) else {}
    return {
        "surrounding": override,
        "snapshot": str(meta.get("sha256") or ""),
        "tree": str(frozen.get("tree") or meta.get("tree") or ""),
        "head": meta.get("head"),
        "base": meta.get("base"),
        "workflow": workspace.workflow,
        "epoch": book.workflow_id(),
        "rerun": rerun,
        "inputs": inputs,
    }


def _adoption_line(adoption: context_mod.Adoption) -> str:
    """``review run``'s one line on the surrounding context it handed over."""
    record = adoption.record()
    if adoption.adopted:
        line = "%d symbol(s), %s chars adopted" % (
            len(adoption.adopted),
            "{:,}".format(adoption.adopted_chars),
        )
    else:
        line = "nothing adopted -- %s" % adoption.reason
    if adoption.trimmed:
        line += "; %d left out (%s)" % (len(adoption.trimmed), context_mod.trim_reasons(record))
    return line


def _reviewer_line(run: review_mod.ReviewerRun) -> str:
    mark = "ok" if run.status == "ok" else ("PARTIAL" if run.status == "partial" else "FAILED")
    return "%-7s %-18s %-8s %-18s %s" % (
        mark,
        run.reviewer.get("id"),
        run.reviewer.get("provider"),
        run.model_display or "?",
        run.error or "%d finding(s)" % run.findings,
    )


def _panel_summary(ok: int, failed: int, partial: int) -> str:
    """The round's tally.

    The first two counts keep their wording: the orchestrator is told to
    report `N successful, M failed` verbatim, and SKILL.md and workflow.md
    both say so. Partial is appended, and only when there is one, so a round
    that never touched the inline limit reads exactly as it always did.
    """
    line = "%d successful, %d failed" % (ok, failed)
    if partial:
        line += ", %d partial (change handed over as a file)" % partial
    return line


def _risk_paths(meta: Dict[str, Any]) -> List[str]:
    """Every path a snapshot says the change touches.

    ``changed_paths`` is the whole set, including withheld files and the name
    a rename came from. A snapshot written by an earlier version does not have
    it, so the old pair is reconstructed instead -- one round judged from a
    slightly narrower set is better than a crash, and the next snapshot has
    the key.
    """
    paths = meta.get("changed_paths")
    if isinstance(paths, list) and paths:
        return [str(path) for path in paths]
    return list(meta.get("files") or []) + [str(entry.get("path")) for entry in (meta.get("withheld") or [])]


def cmd_review_snapshot(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    workspace = _workspace(args)
    settings = loaded.review_settings()
    exclude = () if args.no_exclude else settings.get("exclude")
    incremental = bool(settings.get("incremental_rounds", True)) and not args.full
    # Read before the snapshot, because the surrounding context is frozen with
    # it: what a reviewer is shown has to come from the tree the diff did.
    context_settings = loaded.context_settings()
    surrounding = args.surrounding or context_mod.surrounding_mode(context_settings.get("surrounding"))
    try:
        meta = review_mod.create_snapshot(
            workspace,
            args.base,
            include_untracked=not args.no_untracked,
            exclude=exclude,
            incremental=incremental,
            surrounding=surrounding,
        )
    except review_mod.ReviewError as exc:
        _err(str(exc))
        return 2
    # Warned about, never refused: taking a snapshot spends nothing, and the
    # command that would spend something says so itself. Refusing here would
    # also leave no snapshot to narrow from.
    #
    # Measured before the JSON branch, because --json is the form a wrapper
    # reads and a warning only a human sees is not one it can act on. The
    # three keys go in the payload, not in the snapshot's metadata file: what
    # the limit was and whether this change is over it are facts about the
    # commands that read the snapshot, not about the frozen diff.
    max_chars = int(context_settings.get("max_chars") or 0)
    change_chars = review_mod.snapshot_chars(workspace)
    over_context = review_mod.over_context(change_chars, max_chars)
    if args.json:
        payload = dict(meta)
        payload["change_chars"] = change_chars
        payload["max_chars"] = max_chars
        payload["over_context"] = over_context
        _emit_json(payload)
        return 0
    _out("Snapshot: %s" % workspace.relative(workspace.snapshot_path))
    _out("  strategy: %s" % meta["strategy"])
    if meta.get("incremental_from"):
        _out("  scope:    what changed since the last reviewed round, not the whole change")
        if meta.get("full_diff"):
            _out("            whole change kept at %s" % meta["full_diff"])
        _out("            reviewers also get the findings the fix was meant to address")
    _out("  files:    %d" % len(meta["files"]))
    _out("  size:     %d bytes (sha256 %s)" % (meta["bytes"], meta["sha256"][:12]))
    for line in _surrounding_snapshot_lines(workspace, meta, context_settings):
        _out(line)
    if over_context:
        _out(
            "  WARNING:  %s chars is over review.context.max_chars (%s) -- review run will "
            "refuse this change." % ("{:,}".format(change_chars), "{:,}".format(max_chars))
        )
        _out("            Narrow it with --base or review.exclude, or split the change.")
    withheld = meta.get("withheld") or []
    if withheld:
        # Named, not merely counted: an exclusion nobody can see is an
        # exclusion nobody can correct.
        lines = review_mod.withheld_lines(withheld)
        _out("  withheld: %d file(s), %s changed line(s) not sent to reviewers" % (len(withheld), lines))
        for entry in withheld:
            _out("    %s (%s)" % (entry["path"], entry["pattern"]))
        _out("    reviewers are told these changed; --no-exclude sends them in full")
    if meta["empty"]:
        if withheld:
            _out("  WARNING: every changed file was withheld -- re-run with --no-exclude to review them.")
        else:
            _out("  WARNING: the snapshot is empty -- there is nothing to review.")
        return 1
    return 0


def _surrounding_snapshot_lines(
    workspace: ws.Workspace, meta: Dict[str, Any], context_settings: Dict[str, Any]
) -> List[str]:
    """What was frozen as surrounding context, and what could not be. Nothing when off."""
    block = meta.get("surrounding")
    if not isinstance(block, dict):
        return []
    tree = str(block.get("tree") or "")
    frozen = "  context:  enclosing -- %d symbol(s), %s chars frozen from tree %s at %s" % (
        int(block.get("candidates") or 0),
        "{:,}".format(int(block.get("chars") or 0)),
        (tree[:7] + "...") if tree else "(none)",
        block.get("path"),
    )
    cap = context_settings.get("surrounding_chars")
    if isinstance(cap, int) and not isinstance(cap, bool):
        cap = "{:,}".format(cap)
    later = "            adopted at review run within review.context.surrounding_chars (%s)" % cap
    skipped = (ws.read_json(workspace.surrounding_path, {}) or {}).get("skipped") or []
    note = context_mod.not_extracted(skipped)
    if note:
        later += "; " + note[0].lower() + note[1:].rstrip(".")
    return [frozen, later]


def _design_request_path(args: argparse.Namespace, workspace: ws.Workspace) -> str:
    """Where the request the plan answers is expected to be."""
    if getattr(args, "request", None):
        return str(_in_workflow(workspace, args.request))
    return os.path.join(workspace.execution_dir, "design-request.md")


def _run_design_review(args: argparse.Namespace, loaded: config_mod.LoadedConfig) -> int:
    """Run the panel against `.ai/plan.md` instead of against a diff.

    Same reviewers, same fan-out, same read-only mode. What differs is the
    input, the prompt, and where the results land -- and the last of those is
    what keeps a design round from advancing or exhausting the code review's
    counter.
    """
    workspace = _workspace(args).design_review().ensure()
    settings = loaded.review_settings()
    design = loaded.design_review_settings()
    if not design.get("enabled"):
        _err("note: review.design.enabled is false; running because you asked")

    configured = loaded.reviewers()
    reviewers = configured
    if args.only:
        wanted = set(args.only)
        reviewers = [r for r in configured if r.get("id") in wanted or r.get("role") in wanted]
        if not reviewers:
            _err("--only %s matched no configured reviewer" % " ".join(args.only))
            return 2

    request_path = _design_request_path(args, workspace)
    if not os.path.isfile(request_path):
        _err(
            "note: no design request at %s; reviewers judge the plan against its own "
            "stated goal" % workspace.relative(request_path)
        )
        request_path = ""
    # The plan is read and hashed here, and frozen only once this round is
    # allowed to run. Writing the snapshot first would replace the plan the
    # previous round's reports are stamped against, so a round refused for
    # budget -- or skipped for having no panel -- would strand its own triage.
    try:
        plan_text, request_text, digest = review_mod.design_digest(
            workspace, workspace.plan_path, request_path
        )
    except review_mod.ReviewError as exc:
        _err(str(exc))
        return 2

    lineage = _lineage(args, workspace)
    iteration = _iteration(args, workspace, lineage, digest)
    max_iterations = int(design.get("max_iterations", 2))
    if iteration > max_iterations and not args.force:
        _err(
            "refusing to run design review round %d: the budget is %d rounds "
            "(review.design.max_iterations)." % (iteration, max_iterations)
        )
        _err(
            "The round that reached the limit still gets its revision; only the re-review "
            "is refused. Report what is still open, or pass --force to override."
        )
        return ledger_mod.EXIT_BUDGET_EXHAUSTED

    book = _ledger(args, workspace)
    # Before the refusal decides: a stage whose process is gone has no charge
    # to answer for, and its absence is part of what the runtime budget says.
    book.clear_stalls()
    refusal = _refuse_if_runtime_spent(book, "design review", args.force)
    if refusal is not None:
        return refusal

    # The plan *and* the request: both go into every reviewer's prompt, whole
    # and unconditionally, so measuring the plan alone would let a round past
    # the limit on a technicality. Refused here, before the plan is frozen,
    # for the reason the digest above is read early.
    #
    # The plan alone is a second, separate size: it is the only part ever
    # handed over as a file, so it -- and not the pair -- decides delivery and
    # what forcing this round may promise.
    context_settings = loaded.context_settings()
    max_chars = int(context_settings.get("max_chars") or 0)
    inline_chars = int(context_settings.get("inline_chars") or 0)
    budget_chars = len(plan_text) + len(request_text)
    refusal = _refuse_if_over_context(
        workspace,
        "design_review",
        budget_chars,
        max_chars,
        len(plan_text),
        inline_chars,
        args.force,
        iteration,
        {"plan_chars": len(plan_text), "request_chars": len(request_text)},
    )
    if refusal is not None:
        return refusal
    over_budget = review_mod.over_context(budget_chars, max_chars)

    meta = review_mod.write_design_snapshot(workspace, workspace.plan_path, request_path, plan_text, digest)

    if not reviewers:
        # Recorded against the plan that was actually frozen, so the round this
        # skip occupies is the one the next real round continues from rather
        # than a phantom that quietly spends the budget.
        _out("No reviewers configured -- skipping the independent-review stage.")
        data = review_mod.build_consolidation(
            workspace, [], [], iteration, lineage, completed_round=meta.get("round_id")
        )
        ws.write_json(workspace.consolidated_json_path, data)
        ws.write_text(workspace.consolidated_md_path, review_mod.render_consolidation(data))
        return 0

    # No gate and no panel reduction. There is no test result to judge a plan
    # by and no diff to measure, and a design decision is precisely where
    # cross-model disagreement earns its cost -- so the whole panel runs.
    max_findings = opt_mod.findings_cap(loaded.optimization_settings(), settings)

    batch_timeout = args.timeout or int(settings.get("timeout_seconds", 1800))
    token = book.begin(
        "design_review",
        {"iteration": iteration, "reviewers": [str(r.get("id")) for r in reviewers]},
        deadline=batch_timeout,
    )
    idle_timeout = args.idle_timeout
    if idle_timeout is None:
        idle_timeout = settings.get("idle_timeout_seconds")
    try:
        runs = review_mod.run_reviews(
            reviewers,
            workspace,
            parallel=not args.sequential and bool(settings.get("parallel", True)),
            timeout=batch_timeout,
            idle_timeout=idle_timeout,
            max_findings=max_findings,
            over_budget=over_budget,
            budget_chars=budget_chars,
            # The same number to both: `prompt_for` decides the delivery and
            # `run_reviews` records and words it, and a round that took them
            # from two places would sooner or later take two different ones.
            inline_chars=inline_chars,
            prompt_for=lambda reviewer: review_mod.build_design_review_prompt(
                reviewer,
                workspace,
                plan_text,
                request_text,
                args.context or "",
                max_findings,
                inline_chars,
            ),
        )
    except review_mod.ReviewError as exc:
        book.end(token, "failed", {"error": str(exc)})
        _err(str(exc))
        return 2

    for run in runs:
        if run.invoked:
            # Prefixed, so a reviewer's design cost never merges into its code
            # cost in `tokens show` -- the same reasoning as `role:tier`.
            label = "design:%s" % (run.reviewer.get("id") or "reviewer")
            book.record_usage("design_review", run.usage.to_dict(), label=label)
    run_dicts = [run.to_dict() for run in runs]
    stamp = review_mod.current_snapshot_stamp(workspace)
    findings, stale = review_mod.read_reports(workspace, [str(r.get("id")) for r in configured], stamp)
    # Every reviewer of this round has returned, so this is the report the
    # round's id may be published in -- and the only place that says so. Not
    # when none of them came back with a review: a round nobody reviewed has
    # no findings to show, and approving over it would look clean.
    reviewed = any(run.status in ("ok", "partial") for run in runs)
    data = review_mod.build_consolidation(
        workspace,
        _merge_runs(workspace, run_dicts),
        findings,
        iteration,
        lineage,
        completed_round=meta.get("round_id") if reviewed else None,
        unreviewed_round=None if reviewed else meta.get("round_id"),
    )
    ws.write_json(workspace.consolidated_json_path, data)
    ws.write_text(workspace.consolidated_md_path, review_mod.render_consolidation(data))
    repeats = book.register_signature("design_review", review_mod.findings_signature(data))
    book.end(
        token,
        "ok",
        {
            "iteration": iteration,
            "reviewers": run_dicts,
            "findings": data["counts"].get("findings_total"),
            "identical_rounds": repeats,
        },
        # Summed over the panel, not the wall clock of the batch: three
        # reviewers running in parallel for 25 minutes delegated 75 minutes of
        # execution, and the budget is on delegated execution.
        charged_seconds=sum(run.duration for run in runs),
    )
    if repeats > 1:
        _err(
            "note: design round %d produced the same findings as the previous round -- "
            "the last revision changed nothing that the reviewers can see." % iteration
        )
    for reviewer_id in stale:
        _err("note: %s has no report for this plan; its earlier report was ignored" % reviewer_id)

    ok, failed, partial = review_mod.summarise_runs(runs)
    if args.json:
        _emit_json(
            {
                "ok": ok,
                "failed": failed,
                "partial": partial,
                "reviewers": run_dicts,
                "counts": data["counts"],
                "plan": meta["plan"],
            }
        )
    else:
        for run in runs:
            _out(_reviewer_line(run))
        _out("")
        _out(_panel_summary(ok, failed, partial))
        _out("Consolidated: %s" % workspace.relative(workspace.consolidated_md_path))
    if ok == 0 and (failed or partial):
        return 1
    return 0


def cmd_review_run(args: argparse.Namespace) -> int:
    loaded = _load_or_die(args.cwd)
    # One run's override of review.context.surrounding, for measuring what the
    # context does on one snapshot. None when not given, and then nothing below
    # differs from a run without the flag.
    override = getattr(args, "surrounding", None)
    if args.design and override:
        _err("--surrounding applies to the code review only: a design round carries no surrounding context.")
        return 2
    if args.design:
        return _run_design_review(args, loaded)
    workspace = _workspace(args)
    configured = loaded.reviewers()
    reviewers = configured
    if args.only:
        wanted = set(args.only)
        reviewers = [r for r in configured if r.get("id") in wanted or r.get("role") in wanted]
    if not reviewers and not args.only:
        _out("No reviewers configured -- skipping the independent-review stage.")
        lineage = _lineage(args, workspace)
        data = review_mod.build_consolidation(
            workspace, [], [], _iteration(args, workspace, lineage), lineage
        )
        ws.write_json(workspace.consolidated_json_path, data)
        ws.write_text(workspace.consolidated_md_path, review_mod.render_consolidation(data))
        return 0
    if not reviewers:
        _err("--only %s matched no configured reviewer" % " ".join(args.only))
        return 2

    settings = loaded.review_settings()
    context_settings = dict(loaded.context_settings())
    if override:
        context_settings["surrounding"] = override
    if not os.path.isfile(workspace.snapshot_path):
        try:
            review_mod.create_snapshot(
                workspace,
                args.base,
                exclude=settings.get("exclude"),
                incremental=bool(settings.get("incremental_rounds", True)),
                surrounding=context_mod.surrounding_mode(context_settings.get("surrounding")),
            )
        except review_mod.ReviewError as exc:
            _err(str(exc))
            return 2
    lineage = _lineage(args, workspace)
    iteration = _iteration(args, workspace, lineage)
    max_iterations = int(settings.get("max_review_iterations", 2))
    if iteration > max_iterations and not args.force:
        _err(
            "refusing to run review round %d: the budget is %d rounds "
            "(review.max_review_iterations)." % (iteration, max_iterations)
        )
        _err(
            "The round that reached the limit still gets its fix and re-test; only the "
            "re-review is refused. Report what is still open, or pass --force to override."
        )
        return ledger_mod.EXIT_BUDGET_EXHAUSTED

    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
    if override and meta.get("incremental_from"):
        # Refused on the first run as well as the second: the premise section
        # is built from the consolidated report of the moment a run starts, and
        # the first run rewrites it, so no pair on this snapshot would differ
        # in the context alone.
        _err(
            "refusing to run: --surrounding %s on an incremental round (fix diff since %s). The prompt of "
            "a re-review carries the accepted findings of the moment it runs, so two runs on it would "
            "differ in more than the surrounding context. Measure on a whole-change snapshot: the first "
            "round of a change, or a round after a clean or all-rejected review."
            % (override, meta.get("incremental_from"))
        )
        return 2
    plan = opt_mod.decide(
        loaded.optimization_settings(),
        settings,
        _risk_paths(meta),
        int(meta.get("lines_added") or 0) + int(meta.get("lines_deleted") or 0),
        workspace.last_status("test"),
        len(reviewers),
        reviewed_files=len(meta.get("files") or []),
    )
    if plan.escalated:
        _err("note: %s" % plan.escalation_note())
    if plan.gate == opt_mod.GATE_REFUSE and not args.force:
        # Written down even though nothing ran, and *because* nothing ran: a
        # skipped round is the largest thing this level ever saves, and a
        # saving that leaves no trace cannot be counted. No budget is consumed
        # and no ledger entry opened -- there was no attempt to account for.
        workspace.record_event(
            "review",
            opt_mod.REFUSED,
            {
                "iteration": iteration,
                "optimization": plan.to_dict(),
                # Which of the two refusals this was. Both are recorded the
                # same way, and a report that cannot tell them apart prices
                # them the same -- see ``summarise_rounds``.
                "refused_by": "gate",
                "reviewers": [],
            },
        )
        _err(plan.gate_note())
        return ledger_mod.EXIT_BUDGET_EXHAUSTED

    # After the gate, because a round the gate already refused has no reason to
    # be measured, and before anything is charged or run.
    max_chars = int(context_settings.get("max_chars") or 0)
    inline_chars = int(context_settings.get("inline_chars") or 0)
    change_chars = review_mod.snapshot_chars(workspace)
    # Chosen before the limit is checked, because the limit measures what the
    # prompt carries: the diff and the context adopted beside it. The context
    # is capped at what the diff leaves under both limits, so this never
    # refuses a round the diff alone would have run.
    adoption = context_mod.adopt(
        workspace,
        meta,
        context_settings.get("surrounding"),
        context_settings.get("surrounding_chars"),
        change_chars,
        max_chars,
        inline_chars,
        review_mod.delivery_of(change_chars, inline_chars),
    )
    # The delivered body is the diff alone -- context never decides delivery --
    # so it is passed apart from the budget, as the design path passes its own.
    budget_chars = change_chars + adoption.context_chars
    refusal = _refuse_if_over_context(
        workspace,
        "review",
        budget_chars,
        max_chars,
        change_chars,
        inline_chars,
        args.force,
        iteration,
        {"optimization": plan.to_dict()},
    )
    if refusal is not None:
        return refusal
    # Before anything is charged: an asked-for measurement that would measure
    # nothing, or that would throw away a decision made on this snapshot, is
    # refused rather than billed. Without the flag neither check runs.
    rerun = override is not None and _measurement_rerun(workspace, meta, lineage, iteration)
    if override == "enclosing" and not adoption.adopted:
        _err(
            "refusing to run: --surrounding enclosing was asked for but nothing would be adopted (%s); "
            "the run would measure nothing and still bill the whole panel." % adoption.reason
        )
        if adoption.reason == context_mod.NOT_FROZEN:
            _err("Run `review snapshot --surrounding enclosing` first.")
        else:
            _err('See references/limits.md, "Measuring what surrounding context does".')
        return 2
    # On the snapshot alone, not on ``rerun``: a lineage change between the two
    # runs makes the second no rerun, but it still rebuilds the same findings.
    previous = _same_snapshot_report(workspace, meta) if override is not None else {}
    if previous:
        changed = _triage_changed_since_build(previous)
        if changed:
            _err(
                "refusing to run: --surrounding %s would rebuild the findings of a snapshot that has been "
                "triaged since its last run (%d of %d with a new decision or note), and a decision on a "
                "finding that does not come back would be lost. Take the pair before triage, or take a new "
                "snapshot." % (override, len(changed), len(previous.get("findings") or []))
            )
            return 2
    # Forced past it: the round runs, and every record of it says so.
    over_budget = review_mod.over_context(budget_chars, max_chars)

    if plan.gate == opt_mod.GATE_WARN:
        _err(plan.gate_note())
    if plan.reviewer_limit is not None and not args.only:
        reviewers = opt_mod.choose_reviewers(reviewers, plan.reviewer_limit)
        _err(
            "note: %s (%s). Cross-model disagreement is what a second reviewer buys; "
            "raise optimization.level or the low_risk thresholds to keep it."
            % (plan.reviewer_note(), ", ".join(str(r.get("id")) for r in reviewers))
        )

    book = _ledger(args, workspace)
    book.clear_stalls()
    refusal = _refuse_if_runtime_spent(book, "review", args.force)
    if refusal is not None:
        return refusal
    batch_timeout = args.timeout or int(settings.get("timeout_seconds", 1800))
    token = book.begin(
        "review",
        {"iteration": iteration, "reviewers": [str(r.get("id")) for r in reviewers]},
        deadline=batch_timeout,
    )

    idle_timeout = args.idle_timeout
    if idle_timeout is None:
        idle_timeout = settings.get("idle_timeout_seconds")
    max_findings = plan.max_findings
    try:
        runs = review_mod.run_reviews(
            reviewers,
            workspace,
            parallel=not args.sequential and bool(settings.get("parallel", True)),
            timeout=batch_timeout,
            extra_context=args.context or "",
            idle_timeout=idle_timeout,
            max_findings=max_findings,
            over_budget=over_budget,
            budget_chars=budget_chars,
            inline_chars=inline_chars,
            surrounding=adoption,
        )
    except review_mod.ReviewError as exc:
        book.end(token, "failed", {"error": str(exc)})
        _err(str(exc))
        return 2
    context_on = adoption.mode != "none"

    for run in runs:
        if run.invoked:
            book.record_usage("review", run.usage.to_dict(), label=str(run.reviewer.get("id") or "reviewer"))
    run_dicts = [run.to_dict() for run in runs]
    # Consolidate from every configured reviewer's report, not only the ones
    # that just ran: with --only that would otherwise overwrite the report with
    # a subset and discard the other reviewers' findings and triage. Reports
    # from an earlier snapshot are skipped rather than mixed in.
    stamp = review_mod.current_snapshot_stamp(workspace)
    findings, stale = review_mod.read_reports(workspace, [str(r.get("id")) for r in configured], stamp)
    data = review_mod.build_consolidation(
        workspace, _merge_runs(workspace, run_dicts), findings, iteration, lineage
    )
    if override:
        # The triage as this run built it, so the second run of a pair can tell
        # a decision made on this snapshot from one carried in with a finding.
        data["measurement"] = {
            "surrounding": override,
            "rerun": rerun,
            "triage_at_build": {entry["key"]: _triage_record(entry) for entry in data["findings"]},
        }
    ws.write_json(workspace.consolidated_json_path, data)
    ws.write_text(workspace.consolidated_md_path, review_mod.render_consolidation(data))
    # The second run of a pair is the same snapshot reviewed again on purpose,
    # not a fix that changed nothing, so it registers no signature: the pair's
    # signature is its first run's.
    if rerun:
        repeats = book.repeats("review")
    else:
        repeats = book.register_signature("review", review_mod.findings_signature(data))
    detail = {
        "iteration": iteration,
        "reviewers": run_dicts,
        "findings": data["counts"].get("findings_total"),
        "identical_rounds": repeats,
        "optimization": plan.to_dict(),
    }
    measurement = None
    if override:
        measurement = _measurement_block(
            meta, workspace, book, override, rerun, _measurement_inputs(args, plan, inline_chars, max_chars)
        )
        detail["measurement"] = measurement
    if context_on and any("surrounding" in run for run in run_dicts):
        # Sizes and counts only: ``optimization report`` splits rounds on it,
        # and the names are on every reviewer entry beside it. Only when a
        # reviewer's prompt was built with it: a round whose every reviewer
        # fell over first showed no one any context, and must not be counted
        # as a round with it.
        detail["surrounding"] = adoption.summary()
    book.end(
        token,
        "ok",
        detail,
        # Per reviewer, as above: the panel is the unit that was delegated.
        charged_seconds=sum(run.duration for run in runs),
    )
    if repeats > 1 and not rerun:
        _err(
            "note: round %d produced the same findings as the previous round -- "
            "the last fix changed nothing that the reviewers can see." % iteration
        )
    for reviewer_id in stale:
        _err("note: %s has no report for this snapshot; its earlier report was ignored" % reviewer_id)
    if max_findings:
        for run in runs:
            if run.findings > max_findings:
                _err(
                    "note: %s returned %d findings against a cap of %d; all are kept, "
                    "but its output cost more than it needed to"
                    % (run.reviewer.get("id"), run.findings, max_findings)
                )

    ok, failed, partial = review_mod.summarise_runs(runs)
    if args.json:
        payload = {
            "ok": ok,
            "failed": failed,
            "partial": partial,
            "reviewers": run_dicts,
            "counts": data["counts"],
            "optimization": plan.to_dict(),
        }
        if context_on:
            payload["surrounding"] = adoption.record()
        if measurement is not None:
            payload["measurement"] = measurement
        _emit_json(payload)
    else:
        for run in runs:
            _out(_reviewer_line(run))
        _out("")
        _out(_panel_summary(ok, failed, partial))
        if context_on:
            line = "Surrounding context: %s" % _adoption_line(adoption)
            if override:
                line += " (--surrounding enclosing for this run)"
            _out(line)
        elif override:
            _out(
                "Surrounding context: none (--surrounding none for this run; "
                "review.context.surrounding unchanged)"
            )
        _out("Consolidated: %s" % workspace.relative(workspace.consolidated_md_path))
    if ok == 0 and (failed or partial):
        return 1
    return 0


def cmd_review_consolidate(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    workspace = _review_workspace(args)
    reviewer_ids = [str(r.get("id")) for r in loaded.reviewers()]
    stamp = review_mod.current_snapshot_stamp(workspace)
    findings, stale = review_mod.read_reports(workspace, reviewer_ids, stamp)
    previous = ws.read_json(workspace.consolidated_json_path, {}) or {}
    lineage = _lineage(args, workspace)
    data = review_mod.build_consolidation(
        workspace, previous.get("reviewers", []), findings, _iteration(args, workspace, lineage), lineage
    )
    for reviewer_id in stale:
        _err("note: %s's report predates the current snapshot and was ignored" % reviewer_id)
    ws.write_json(workspace.consolidated_json_path, data)
    ws.write_text(workspace.consolidated_md_path, review_mod.render_consolidation(data))
    if args.json:
        _emit_json(data)
    else:
        _out(review_mod.render_consolidation(data))
    return 0


def _no_review_yet(args: argparse.Namespace) -> None:
    _err("no consolidated review found -- run `review run%s` first" % (" --design" if args.design else ""))


def cmd_review_show(args: argparse.Namespace) -> int:
    workspace = _review_workspace(args)
    data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    if not data:
        _no_review_yet(args)
        return 2
    if args.accepted:
        data = dict(data, findings=review_mod.accepted_findings(data))
    if args.json:
        _emit_json(data)
    else:
        _out("Source: %s" % workspace.relative(workspace.consolidated_json_path))
        _out(review_mod.render_consolidation(data))
    return 0


def cmd_review_triage(args: argparse.Namespace) -> int:
    workspace = _review_workspace(args)
    data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    if not data:
        _no_review_yet(args)
        return 2
    try:
        for finding_id in args.ids:
            review_mod.set_triage(data, finding_id, args.status, args.note or "")
    except review_mod.ReviewError as exc:
        _err(str(exc))
        return 2
    ws.write_json(workspace.consolidated_json_path, data)
    ws.write_text(workspace.consolidated_md_path, review_mod.render_consolidation(data))
    _out("Triaged %s as %s" % (", ".join(args.ids), args.status))
    return 0


def cmd_review_fix_brief(args: argparse.Namespace) -> int:
    workspace = _review_workspace(args)
    data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    if args.design:
        brief = review_mod.render_fix_brief(data, "Revise the plan to address these accepted findings")
    else:
        brief = review_mod.render_fix_brief(data)
    if args.output:
        path = _in_workflow(workspace, args.output)
        ws.write_text(path, brief)
        _out("Wrote %s" % workspace.relative(path))
    else:
        _out(brief)
    return 0


def cmd_review_status(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    workspace = _review_workspace(args)
    # The ledger, the run log and the plan belong to the workflow, not to the
    # review directory `--design` points at.
    base = _workspace(args)
    data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    settings = loaded.review_settings()
    severities = tuple(settings.get("re_review_severities") or ("critical", "high"))
    blocking = review_mod.unresolved_blocking(data, severities)
    accepted = bool(review_mod.accepted_findings(data))
    iteration = int(data.get("iteration", 0) or 0)
    # Read only: stalls are `status`'s to clear.
    book = _ledger(args, base)
    events = [event for event in (base.read_state().get("events") or []) if isinstance(event, dict)]
    # Each budget is reported under the name of the setting it came from, so a
    # consumer holding both payloads can tell which one it was handed. The code
    # review's key is what it always was; only --design carries the other name.
    if args.design:
        budget_key = "max_iterations"
        max_iterations = int(loaded.design_review_settings().get("max_iterations", 2))
        final = _design_final_pass(
            blocking,
            iteration,
            max_iterations,
            approval_mod.findings_of_current_plan(base, approval_mod.read_plan(base)[0]),
            _ran_since_last_round(events, "design_review", "architect", counts=_wrote_plan(base))[0],
            book.remaining("architect"),
            _approved_as_recorded(
                approval_mod.current(base, bool(loaded.design_settings().get("require_approval")))
            ),
            approval_mod.implemented_since_plan(base, events),
            accepted,
        )
        repeats = book.repeats("design_review")
    else:
        budget_key = "max_review_iterations"
        max_iterations = int(settings.get("max_review_iterations", 2))
        fixed, retested = _ran_since_last_round(events, "review", "review_fixer", ("test", "re-test"))
        final = _code_final_pass(
            blocking, iteration, max_iterations, fixed, retested, book.remaining("review_fixer"), accepted
        )
        repeats = book.repeats("review")
    counts = data.get("counts", {})
    # No consolidated report at all is the only real "none": nothing has been
    # reviewed. A report with no `coverage` block is a round that happened
    # before coverage was recorded, and calling that `round none, change none`
    # would claim it had been measured and found empty. `render_consolidation`
    # leaves its line out for exactly that reason, and the two commands read
    # the same file -- they must not describe it differently.
    coverage = data.get("coverage")
    if not data:
        coverage = {
            "round": "none",
            "change": "none",
            "unverified_since": None,
            "change_chars": None,
            "inline_chars": None,
        }
    elif not isinstance(coverage, dict):
        coverage = None
    payload = {
        "iteration": iteration,
        budget_key: max_iterations,
        "blocking": [f["id"] for f in blocking],
        "blocking_count": len(blocking),
        "accepted_count": len(review_mod.accepted_findings(data)),
        "re_review_recommended": bool(blocking) and iteration < max_iterations,
        "iteration_budget_exhausted": iteration >= max_iterations,
        "coverage": coverage,
        # Counted over the snapshot the `coverage` beside it describes, because
        # this is the number printed in the same breath as that value. The
        # table-wide column is still in `counts`, under its own name.
        "reviewers_partial": review_mod.snapshot_reviewers(counts, "partial"),
        # A round that only ran because a human forced it past
        # review.context.max_chars. False for every round that was not, and
        # for every report written before the limit existed -- which is the
        # true answer for all of them: none of them was forced past it.
        "over_budget": bool((data.get("snapshot") or {}).get("over_budget")),
        "counts": counts,
    }
    # Only when the report recorded one, as `consolidated.md` does.
    surrounding = data.get("surrounding")
    if isinstance(surrounding, dict):
        payload["surrounding"] = surrounding
    if args.design:
        payload["final_revision"] = final["state"]
        payload["final_revision_pending"] = final["pending"]
    else:
        payload["final_fix"] = final["state"]
        payload["final_fix_pending"] = final["pending"]
    if args.json:
        _emit_json(payload)
    else:
        _out("iteration %d/%d" % (iteration, max_iterations))
        _out("accepted findings: %d" % payload["accepted_count"])
        _out("blocking (%s): %d %s" % ("/".join(severities), len(blocking), ", ".join(payload["blocking"])))
        _out("re-review recommended: %s" % ("yes" if payload["re_review_recommended"] else "no"))
        if coverage is None:
            _out("coverage: not recorded -- this report predates it")
        else:
            _out("coverage: round %s, change %s" % (coverage.get("round"), coverage.get("change")))
        if payload["over_budget"]:
            _out(
                "over budget: this round was sent past review.context.max_chars by --force; "
                "say so when you report it"
            )
        if isinstance(surrounding, dict):
            for line in _surrounding_status_lines(surrounding):
                _out(line)
        for line in _coverage_advice(
            coverage or {},
            counts,
            payload["iteration_budget_exhausted"],
            _configured_inline_chars(loaded),
            args.design,
        ):
            _out(line)
        if payload["iteration_budget_exhausted"] and blocking:
            _out("iteration budget exhausted -- %s" % _final_pass_advice(args.design, final, repeats))
    return 0


def _surrounding_status_lines(block: Dict[str, Any]) -> List[str]:
    """The context this snapshot's reviewers were shown, and what was left out by name."""
    if block.get("shared") is False:
        records = list((block.get("by_reviewer") or {}).items())
    else:
        records = [("", block)]
    lines = []
    for reviewer_id, record in records:
        label = "surrounding context (%s)" % reviewer_id if reviewer_id else "surrounding context"
        if not isinstance(record, dict):
            lines.append("%s: none (ran with review.context.surrounding none)" % label)
            continue
        lines.append("%s: %s" % (label, context_mod.status_line(record)))
        trimmed = [c for c in record.get("trimmed") or [] if isinstance(c, dict)]
        for candidate in trimmed[:5]:
            lines.append(
                "  left out: %s:%s-%s %s"
                % (
                    candidate.get("path"),
                    candidate.get("start"),
                    candidate.get("end"),
                    candidate.get("symbol"),
                )
            )
        if len(trimmed) > 5:
            lines.append("  and %d more" % (len(trimmed) - 5))
        skipped = [entry for entry in record.get("skipped") or [] if isinstance(entry, dict)]
        for entry in skipped[:5]:
            lines.append("  not extracted: %s -- %s" % (entry.get("path"), entry.get("reason") or "?"))
        if len(skipped) > 5:
            lines.append("  and %d more not extracted" % (len(skipped) - 5))
    return lines


def _final_pass_advice(design: bool, final: Dict[str, Any], repeats: int) -> str:
    """What `review status` says to do once the round budget is spent."""
    state = final["state"]
    if design and state == "pending":
        advice = (
            "fold the accepted findings into the plan once more (review fix-brief --design, then "
            "run architect) and do not re-review it; then present the plan and the findings to the "
            "user and ask"
        )
    elif design and state == "done" and final.get("plan_changed") is True:
        advice = (
            "the plan was revised after this round and is not re-reviewed; present it with the "
            "findings still open from the earlier revision and ask"
        )
    elif design and state == "done" and final.get("plan_changed") is False:
        advice = (
            "the architect ran after this round and left the plan unchanged; present the plan and "
            "the open findings and ask whether to approve over them or to triage them again"
        )
    elif design and state == "approved":
        advice = "the plan is approved over the open findings; do not revise or re-review it"
    elif design and state == "blocked":
        advice = (
            "the accepted findings are not folded in and no architect attempt is left "
            "(budgets.architect); report them and ask whether to approve over them (`design approve`) "
            "or to free an attempt (`budget reset`) and revise"
        )
    elif not design and state == "pending":
        advice = (
            "fix the accepted findings once more (review fix-brief, then run review_fixer), re-test "
            "and record it (state record test ok|failed), and do not re-review; then report what is "
            "still open"
        )
    elif not design and state == "retest":
        advice = (
            "the fix after this round is not re-reviewed; re-run the tests, record them "
            "(state record test ok|failed), then report the remaining findings"
        )
    elif not design and state == "done":
        advice = "fixed and re-tested after this round, not re-reviewed; report the remaining findings"
    elif not design and state == "blocked":
        advice = (
            "the accepted findings are not fixed and no review_fixer attempt is left "
            "(budgets.review_fixer); report them, or free an attempt (`budget reset`) and fix"
        )
    else:
        # `implemented`, `unaccepted` (nothing to fold in or fix), and a
        # design round with no frozen plan to compare.
        advice = "report the remaining findings instead of looping"
    # The repeat does not stop a revision or fix still owed; it is said so the
    # report can.
    if repeats > 1 and state in ("pending", "retest"):
        advice += " (this round repeated the previous round's findings)"
    return advice


def _configured_inline_chars(loaded: config_mod.LoadedConfig) -> int:
    """``review.context.inline_chars``, resolved without trusting the file.

    ``cmd_review_status`` loads with ``validate_result=False`` on purpose: it
    is the one review command written to answer while the configuration is
    broken. So the value here can be whatever a hand edit left behind --
    ``400k``, a list, ``true`` -- and ``int()`` on it raises ``ValueError``,
    which ``main`` does not catch. That is the command written to survive a
    broken config dying on one, while every other command reports "invalid
    configuration" and exits 2. Anything that is not a plain integer falls
    back to the shipped default, which is what an unreadable setting is worth.
    Thread any further setting into this command the same way.
    """
    value = loaded.context_settings().get("inline_chars")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return review_mod.default_inline_chars()


def _coverage_advice(
    coverage: Dict[str, Any],
    counts: Dict[str, Any],
    budget_spent: bool,
    inline_chars: int,
    design: bool = False,
) -> List[str]:
    """What to do about an unverified coverage, if anything.

    Each line names the one action that changes the answer. Another round is
    never it: the snapshot is frozen, so re-running it sends the same prompt
    and gets the same verdict. Narrowing what the round *shows* is never it
    either, which is why ``--base`` is not named here: a smaller diff is a
    smaller round, not a reviewed change, and pointing at the flag that moves
    the mark without moving the change is pointing at the way around it.

    The state is ``review_mod.coverage_state``, the same classification
    ``consolidated.md`` words: the two commands read one file and must not
    describe it differently -- an advice line telling the reader to re-snapshot
    with ``--full`` when what is missing is a reviewer sends them to redo the
    thing they just did.

    A design round is judged on ``.ai/plan.md`` itself, and none of the
    code-review remedies can be aimed at it -- ``review snapshot`` writes the
    code snapshot and there is no ``--design`` form of it. The only thing that
    makes an oversize plan reviewable is a shorter plan.

    ``inline_chars`` is the *configured* limit, not the one the round in the
    report was measured against, because these lines are about the next run
    and not the last one. It is also the second way out, and the one the
    report itself cannot name: a limit is a setting, and a reader who decides
    the prompt is worth paying for raises it rather than cutting the change up.

    The recorded limit is read too, through the size beside it. A body handed
    over as a file was over the limit of its own round, so a recorded size
    that fits the configured one says the limit has been raised since -- and
    then "the same snapshot gives the same answer" is false, splitting a
    change that already fits is wasted work, and raising a limit the reader
    has just raised is worse than saying nothing. That case gets its own line.
    """
    lines = []
    chars = coverage.get("change_chars")
    size = "{:,}".format(chars) if chars else "size unrecorded"
    limit = "{:,}".format(inline_chars)
    # Counted over this snapshot, like the coverage value it is quoted beside.
    partial = review_mod.snapshot_reviewers(counts, "partial")
    state = review_mod.coverage_state(coverage, counts)
    mark = review_mod.unverified_phrase(coverage)
    if state == "round_unverified":
        # ``coverage.change_chars`` is recorded for precisely this comparison.
        # No check that the recorded limit differs is needed: the body went
        # over as a file, so it was over the limit of its round, and a size at
        # or under the configured one says that limit is not this one.
        raised = isinstance(chars, int) and bool(chars) and inline_chars > 0 and chars <= inline_chars
        if raised and design:
            lines.append(
                "not a clean review: %d reviewer(s) partial, the plan (%s chars) was handed over as "
                "a file under a lower review.context.inline_chars. The limit is %s chars now, so "
                "the plan fits inline: run the design round again." % (partial, size, limit)
            )
        elif raised:
            lines.append(
                "not a clean review: %d reviewer(s) partial, the change body (%s chars) was handed "
                "over as a file under a lower review.context.inline_chars. The limit is %s chars now, "
                "so this snapshot fits inline: run review run against it again." % (partial, size, limit)
            )
        elif design:
            lines.append(
                "not a clean review: %d reviewer(s) partial, the plan (%s chars) was handed over "
                "as a file. Re-running the same plan gives the same answer: shorten .ai/plan.md "
                "to fit inline (<= %s chars), or raise review.context.inline_chars, then run the "
                "design round again." % (partial, size, limit)
            )
        else:
            lines.append(
                "not a clean review: %d reviewer(s) partial, the change body (%s chars) was handed "
                "over as a file. Re-running the same snapshot gives the same answer: split the "
                "change and review the parts, so each part fits inline (<= %s chars), or raise "
                "review.context.inline_chars, then snapshot again." % (partial, size, limit)
            )
    elif state == "no_reviewer":
        if design:
            lines.append("%s -- no reviewer has run against this plan; run the design round." % mark)
        else:
            lines.append(
                "%s -- no reviewer has run against this snapshot; run the reviewers against it "
                "with review run." % mark
            )
    elif state == "none_ok":
        if design:
            lines.append(
                "%s -- no reviewer came back ok for this plan; re-run the reviewers that did not." % mark
            )
        else:
            lines.append(
                "%s -- no reviewer came back ok for this snapshot; re-run the reviewers that did not." % mark
            )
    elif state == "fix_only":
        if design:
            lines.append(
                "%s -- no round has shown a reviewer the whole plan; shorten .ai/plan.md until it "
                "fits inline, then run the design round again." % mark
            )
        else:
            lines.append(
                "%s -- re-snapshot with --full once the whole change fits inline, then run again." % mark
            )
    if budget_spent and coverage.get("change") == "unverified":
        lines.append("iteration budget exhausted -- report the change as not reviewed in full")
    return lines


# --------------------------------------------------------------------------- state


def _detached_argv(args: argparse.Namespace, role: str) -> List[str]:
    """Rebuild this invocation for the worker, prompt now coming from a file.

    The prompt's text is deliberately absent: ``jobs.start`` writes it to a
    file of its own, because only it knows the job id the path is built from.
    What goes here is the placeholder it substitutes -- placed, not appended,
    so it lands where this command line has room for it. This used to say
    ``--prompt-file -`` while the worker's stdin was ``DEVNULL``, so every
    detached run delegated an empty prompt -- invisible under the mock
    provider, which does not read one.
    """
    argv = ["run", role, "--force"]
    if args.tier:
        # Left out, the worker ran the role's default model: a more expensive
        # run than the one asked for, recorded without the label a tier exists
        # to be read by.
        argv += ["--tier", args.tier]
    if args.mode:
        argv += ["--mode", args.mode]
    if args.output:
        argv += ["--output", os.path.abspath(args.output)]
    if args.timeout:
        argv += ["--timeout", str(args.timeout)]
    if args.idle_timeout is not None:
        argv += ["--idle-timeout", str(args.idle_timeout)]
    # Ahead of --extra, which is nargs=REMAINDER and takes everything after it.
    # Both used to be appended by `jobs.start`, past the end of a command whose
    # shape only this function knows, and a detached run carrying --extra
    # reached the provider with the two paths as provider arguments.
    argv += ["--prompt-file", jobs_mod.PROMPT_FILE, "--job-file", jobs_mod.JOB_FILE]
    if args.extra:
        argv += ["--extra", *args.extra]
    return argv


def cmd_jobs_list(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    found = jobs_mod.list_jobs(workspace)
    if args.json:
        _emit_json(found)
        return 0
    if not found:
        _out("No jobs recorded.")
        return 0
    for job in found:
        _out(
            "%-34s %-10s %-14s %s"
            % (job.get("id"), job.get("status"), job.get("stage"), job.get("started_at"))
        )
    return 0


def cmd_jobs_show(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    job = jobs_mod.read_job(workspace, args.job_id)
    if job is None:
        _err("no such job: %s" % args.job_id)
        return 2
    if args.json:
        _emit_json(job)
        return 0
    _out(jobs_mod.render(job))
    if args.output and job.get("output_file"):
        _out("")
        _out(ws.read_text(str(job["output_file"])))
    return 0


def cmd_jobs_wait(args: argparse.Namespace) -> int:
    """Bounded wait: returning while the job runs is an outcome, not an error."""
    workspace = _workspace(args)
    if jobs_mod.read_job(workspace, args.job_id) is None:
        _err("no such job: %s" % args.job_id)
        return 2
    job = jobs_mod.wait(workspace, args.job_id, timeout=args.timeout, poll=args.poll)
    if args.json:
        _emit_json(job)
    else:
        _out(jobs_mod.render(job))
        if job.get("status") == "succeeded" and job.get("output_file"):
            _out("")
            _out(ws.read_text(str(job["output_file"])))
    if job.get("waited_out"):
        return 4
    # A refused `--output` write exits non-zero in the foreground, and waiting
    # on the job is the same caller asking the same question about the same
    # file. The run may well have succeeded; the file it was told to fill did
    # not get filled, and that is what the next command in the chain reads.
    if job.get("output_written") is False:
        return 1
    return 0 if job.get("status") == "succeeded" else 1


def cmd_jobs_cancel(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    try:
        job = jobs_mod.cancel(workspace, args.job_id)
    except KeyError:
        _err("no such job: %s" % args.job_id)
        return 2
    _out(jobs_mod.render(job))
    return 0


def cmd_budget_show(args: argparse.Namespace) -> int:
    book = _ledger(args)
    summary = book.summary()
    if args.json:
        _emit_json(summary)
        return 0
    _out("Workflow started: %s" % summary["started_at"])
    for stage, entry in summary["budgets"].items():
        _out("  %-14s %d/%d used" % (stage, entry["used"], entry["limit"]))
    total = summary["total_delegated_runs"]
    _out("  %-14s %s/%s used" % ("delegated runs", total["used"], total["limit"]))
    runtime = summary["runtime"]
    if runtime["remaining"] is not None:
        # Shown as used/limit like every other budget above it, and labelled:
        # this counts delegated execution, not how long the workflow has been
        # open, so a figure far below the wall clock is not a bug.
        _out("  %-14s %.0f/%ss used (delegated execution)" % ("runtime", runtime["used"], runtime["limit"]))
    for stage, repeats in (summary["signatures"] or {}).items():
        if repeats and int(repeats) > 1:
            _out("  %-14s same outcome %s times in a row" % (stage, repeats))
    return 0


def cmd_budget_consume(args: argparse.Namespace) -> int:
    """Claim an attempt at a stage the orchestrator runs itself, such as tests."""
    book = _ledger(args)
    try:
        book.consume(args.stage, force=args.force)
    except ledger_mod.BudgetExhausted as exc:
        _err("refusing another %s attempt: %s" % (args.stage, exc))
        _err("Report what is still failing instead of retrying.")
        return ledger_mod.EXIT_BUDGET_EXHAUSTED
    remaining = book.remaining(args.stage)
    suffix = "" if remaining is None else " (%d left)" % remaining
    _out("%s attempt recorded%s" % (args.stage, suffix))
    return 0


def cmd_budget_reset(args: argparse.Namespace) -> int:
    _ledger(args).reset()
    # It used to claim a fresh workflow, which was wrong twice over: the work
    # done so far is still the same workflow, and the token account survives a
    # reset. Saying otherwise invited reading `tokens show` as a contradiction.
    _out("Budgets reset; the token account is kept.")
    return 0


_TOKEN_ROW = "  %-18s %5s %9s %9s %9s %9s %9s %7s %9s"


def _token_row(name: str, account: Dict[str, Any]) -> str:
    def num(field: str) -> str:
        value = int(account.get(field) or 0)
        return "{:,}".format(value) if value else "-"

    def tool(field: str) -> str:
        """Unreported prints ``-``; a measured zero prints ``0``.

        ``num`` renders both as ``-``, which is right for tokens -- a run that
        billed nothing did not happen -- and wrong here: a reviewer that opened
        no files is a result, and the finding this whole count exists to
        produce. Only ``tool_reported_runs`` separates the two.
        """
        if not int(account.get("tool_reported_runs") or 0):
            return "-"
        return "{:,}".format(int(account.get(field) or 0))

    runs = "%s/%s" % (account.get("measured_runs") or 0, account.get("runs") or 0)
    cost = float(account.get("cost_usd") or 0.0)
    return _TOKEN_ROW % (
        name,
        runs,
        num("input_tokens"),
        num("output_tokens"),
        num("total_tokens"),
        num("billed_tokens"),
        ("$%.4f" % cost) if cost else "-",
        tool("tool_uses"),
        tool("tool_output_chars"),
    )


def cmd_tokens_show(args: argparse.Namespace) -> int:
    """What the workflow has spent. Accounting only -- it refuses nothing."""
    book = _ledger(args)
    report = book.token_report()
    if args.json:
        _emit_json(report)
        return 0

    totals = report["totals"]
    if not totals["runs"]:
        _out("No delegated runs recorded yet.")
        return 0

    header = ("stage", "meas.", "input", "output", "total", "billed", "cost", "tools", "tool out")
    _out(_TOKEN_ROW % header)
    for stage, account in sorted(report["by_stage"].items()):
        _out(_token_row(stage, account))
    _out(_token_row("ALL", totals))
    if report["by_label"]:
        _out("")
        _out("Per reviewer and tier:")
        for label, account in sorted(report["by_label"].items()):
            _out(_token_row(label, account))

    _out("")
    chars = int(totals.get("prompt_chars") or 0)
    if chars:
        _out(
            "Prompt text this repo composed: %s chars over %d run(s). "
            "That is the part it can shorten." % ("{:,}".format(chars), totals["runs"])
        )
    reported_tools = int(totals.get("tool_reported_runs") or 0)
    if reported_tools:
        _out(
            "`tools` counts every tool call, whatever it was called. `tool out` is what "
            "those tools printed back -- not source read: `wc -l` returns 3 characters "
            "for a 200-line file and `cat` returns the file. How much of this repository "
            "a delegated run actually read is not knowable from here."
        )
    unknown_tools = int(totals.get("tool_unknown_runs") or 0)
    silent_tools = max(int(totals["runs"]) - reported_tools - unknown_tools, 0)
    if unknown_tools:
        # A run recorded before this was counted cannot say whether it used
        # tools, and "it reported no tool activity" would be a claim about it.
        # Said beside the count below rather than instead of it, now that
        # ``silent_tools`` subtracts these out: a panel with a legacy stage and
        # a Codex stage has both kinds, and the two numbers plus the reported
        # ones account for every run.
        _out(
            "%d of %d run(s) predate tool counting and cannot say whether they used "
            "tools, so the tool columns leave them out. That is not the same as "
            "having used none." % (unknown_tools, totals["runs"])
        )
    if silent_tools:
        _out(
            "%d of %d run(s) reported no tool activity (Codex does not); the tool "
            "columns cover only the runs that did." % (silent_tools, totals["runs"])
        )
    if not report["complete"]:
        silent = int(totals["runs"]) - int(totals["measured_runs"])
        _out(
            "%d of %d run(s) reported no usage, so every total above is a floor, "
            "not a total." % (silent, totals["runs"])
        )
    if not report.get("priced", True):
        unpriced = int(totals["runs"]) - int(totals.get("priced_runs") or 0)
        _out(
            "%d of %d run(s) reported tokens but no cost, so the cost column is a "
            "floor even where the token counts are not. Comparing providers on it "
            "understates the ones that price nothing." % (unpriced, totals["runs"])
        )
    return 0


_OPT_ROW = "  %-22s %s"

#: What each ``refused_by`` means to somebody reading the final report, since
#: the two ask for different things: fix the tests, or make the change smaller.
_REFUSAL_CAUSE = {
    "gate": "tests recorded as failing",
    "context": "change over review.context.max_chars",
}


def _rounds_recorded(args: argparse.Namespace, workspace: ws.Workspace) -> "tuple[List[Dict[str, Any]], str]":
    """Every review round recorded in this project, and what was read.

    A level's effect is a rate -- how often it refused a round, how often it
    cut the panel -- and a rate needs rounds. Reading the run log rather than
    the ledger was what supplied them, because `budget reset` starts a fresh
    ledger while the event log keeps accumulating. Splitting the run log per
    workflow took that away again: one workflow is a handful of rounds, which
    is not a rate. So the default reads every workflow here.

    `--workflow` narrows it to one, which is the question "what did the level
    do *in this piece of work*" rather than "in this repository".
    """
    if getattr(args, "workflow", ""):
        return list(workspace.read_state().get("events") or []), workspace.relative(workspace.state_path)

    events: List[Dict[str, Any]] = []
    for entry in workflow_mod.listing(workspace.container):
        state = ws.read_json(os.path.join(entry["dir"], "state.json"), {}) or {}
        found = state.get("events")
        if isinstance(found, list):
            events.extend(item for item in found if isinstance(item, dict))
    if not events:
        # A flat `.ai/` that has not been adopted yet, or nothing recorded.
        events = [item for item in (workspace.read_state().get("events") or []) if isinstance(item, dict)]
    # One sequence out of several logs. The counts do not depend on the order,
    # but "what happened over time" reads wrong when it is per directory.
    events.sort(key=lambda item: str(item.get("at") or ""))
    return events, workspace.relative(workspace.container)


def cmd_optimization_report(args: argparse.Namespace) -> int:
    """What the level decided, over every round this project has recorded."""
    workspace = _workspace(args)
    events, source = _rounds_recorded(args, workspace)
    report = opt_mod.summarise_rounds(events)
    if args.json:
        _emit_json(report)
        return 0

    if not report["rounds"] and not report["design_rounds"] and not report["design_refused"]:
        _out("No review rounds recorded in %s." % source)
        _out("Run a review, then ask again -- this reads what happened, not what would.")
        return 0

    # Every row in this block describes a decision the level made, and no level
    # decides anything for a design round. So the block is code review's alone,
    # and is skipped rather than filled with zeroes when only design rounds
    # were recorded -- a `levels in force` of `-` invites the reader to conclude
    # the dial did nothing, when it was never asked.
    if report["rounds"]:
        _out(
            "Review rounds recorded: %d (%d ran, %d refused)"
            % (report["rounds"], report["ran"], report["refused"])
        )
        if report["refused"]:
            # Which of them refused, because the two mean different things to
            # act on: the gate says fix the tests, the limit says the change
            # is too big to review at all.
            _out(_OPT_ROW % ("refused by", _counts(report["refused_by"])))
        _out(_OPT_ROW % ("levels in force", _counts(report["levels"])))
        _out(_OPT_ROW % ("gate verdicts", _counts(report["gates"])))
        _out(_OPT_ROW % ("panel reduced", report["panel_reduced"]))
        _out(_OPT_ROW % ("escalated (high risk)", report["escalated"]))
        if report["escalation_patterns"]:
            _out(_OPT_ROW % ("  caused by", _counts(report["escalation_patterns"])))
        if report["always_escalated"]:
            _out(
                "  every round escalated, so the level you configured never applied. "
                "Narrow optimization.high_risk_paths, or accept that this repository "
                "reviews at quality."
            )
        _out("")
    total_runs = report["reviewer_runs"] + report["design_reviewer_runs"]
    total_reported = report["measured_runs"] + report["design_measured_runs"]
    total_billed = report["billed_tokens"] + report["design_billed_tokens"]
    _out(
        "Reviewer runs: %d (%d reported usage), %s billed"
        % (total_runs, total_reported, "{:,}".format(total_billed))
    )
    if report["design_rounds"]:
        # The total above was code review only, so the one command asked what
        # review cost answered with half of it: measured on one workflow,
        # `Reviewer runs: 8` beside four design runs and 350,429 billed tokens
        # that appeared nowhere. Split into rows rather than merged, because a
        # round against a plan and a round against a diff are not the same unit
        # of work and a per-round figure spanning both describes neither.
        if report["rounds"]:
            code_row = _runs_row(
                report["reviewer_runs"],
                report["measured_runs"],
                report["billed_tokens"],
                report["ran"],
                report["billed_per_round"],
            )
            _out(_OPT_ROW % ("code review", code_row))
        design_row = _runs_row(
            report["design_reviewer_runs"],
            report["design_measured_runs"],
            report["design_billed_tokens"],
            report["design_rounds"],
            report["design_billed_per_round"],
        )
        _out(_OPT_ROW % ("design review", design_row))
    elif report["billed_per_round"]:
        _out("  %s billed per round that ran" % "{:,}".format(report["billed_per_round"]))
    if report["tool_reported_runs"] or report["design_tool_reported_runs"]:
        _out("")
        _out("Tool activity, per run and only over the runs that reported it:")
        if report["tool_reported_runs"]:
            row = _tools_row(report, "", report["reviewer_runs"])
            _out(_OPT_ROW % ("code review", row))
        if report["design_tool_reported_runs"]:
            row = _tools_row(report, "design_", report["design_reviewer_runs"])
            _out(_OPT_ROW % ("design review", row))
        _out("  Divided by the runs that reported it, never by every reviewer run:")
        _out("  Codex reports none, and a mixed panel would otherwise halve the figure")
        _out("  for no reason but its composition.")
        _out("  Observed output is what the tools printed back, not source read: `wc -l`")
        _out("  returns 3 characters for a 200-line file and `cat` returns the file.")
    by_context = report.get("by_context") or {}
    # Only once a round has actually carried context: before that there is
    # nothing to compare, and a block of dashes would read as a finding.
    if (by_context.get("with") or {}).get("rounds"):
        _out("")
        _out("Surrounding context (review.context.surrounding), code review rounds only:")
        for name, label in (("with", "with context"), ("without", "without context")):
            group = by_context.get(name) or {}
            _out("  %-22s %s" % (label, _context_row(group, name == "with")))
            _out("  %-22s %s" % ("", _context_per_run_row(group)))
        _out("  The raw figures move with the size of each change and with the number of reviewers in the")
        _out("  panel; compare the per-run lines. Codex reports no tool activity, by design: its runs are in")
        _out("  the billed figures and out of the tool ones, which is why each tool figure names the runs it")
        _out("  was divided by.")
    paired = report.get("paired") or {}
    if paired.get("pairs_listed"):
        _out("")
        for line in _paired_rows(paired):
            _out(line)
    if report["design_refused"]:
        _out("")
        _out(
            "Design review rounds refused for size: %d -- nothing ran, and the plan and its"
            % report["design_refused"]
        )
        _out("request were over review.context.max_chars.")
    if report["estimated_saving"]:
        _out("")
        _out(
            "Estimated saving from %d gate-refused round(s): ~%s billed tokens."
            % (report["refused_by"].get("gate", 0), "{:,}".format(report["estimated_saving"]))
        )
        _out("An estimate: what a round that did not happen would have cost is")
        _out("unknowable, so this is the mean of the %d that did." % report["ran"])
    refused_for_size = report["refused_by"].get("context")
    if refused_for_size:
        _out("")
        _out("%d round(s) refused for size are not priced above: a change over" % refused_for_size)
        _out("review.context.max_chars was going to cost more than the mean, so")
        _out("charging it the mean would understate what was not spent.")
    if report["rounds_without_a_test_result"]:
        _out("")
        _out(
            "%d of %d round(s) ran with no test result recorded, so the gate had"
            % (report["rounds_without_a_test_result"], report["rounds"])
        )
        _out("nothing to act on and cannot have fired. Record one before `review run`:")
        _out("  dev-orchestra state record test ok|failed")
    return 0


def _counts(counter: Dict[str, int]) -> str:
    return ", ".join("%s x%d" % item for item in sorted(counter.items())) or "-"


def _figure(value: Any) -> str:
    """A per-run figure, or ``-`` where nobody reported one."""
    if value is None:
        return "-"
    return "{:,.1f}".format(value) if isinstance(value, float) else "{:,}".format(value)


def _paired_rows(paired: Dict[str, Any]) -> List[str]:
    """``optimization report``'s block of runs paired on one snapshot."""
    lines = [
        "Paired on one snapshot (--surrounding none vs enclosing: the same frozen diff, tree, panel "
        "and prompt inputs):"
    ]
    for pair in paired.get("pairs") or []:
        head = "  %s" % str(pair.get("snapshot") or "")[:12]
        if pair.get("workflow"):
            head += " in %s" % pair["workflow"]
        head += "   change %s chars; panel %s; %s context chars adopted (%s as carried), %s left out" % (
            _figure(pair.get("change_chars")),
            _panel_names(pair.get("panel") or []),
            "{:,}".format(int(pair.get("adopted_chars") or 0)),
            "{:,}".format(int(pair.get("context_chars") or 0)),
            "{:,}".format(int(pair.get("trimmed_chars") or 0)),
        )
        for reason in _pair_exclusions(pair):
            head += "; " + reason
        lines.append(head)
        for name in ("with", "without"):
            side = pair.get(name) or {}
            lines.append(
                "      %-8s %d run(s), %s billed, %s per run; %s use(s)/run, %s observed output chars/run "
                "(%d of %d run(s) reported)"
                % (
                    name + ":",
                    int(side.get("reviewer_runs") or 0),
                    "{:,}".format(int(side.get("billed_tokens") or 0)),
                    _figure(side.get("billed_per_run")),
                    _figure(side.get("tool_uses_per_run")),
                    _figure(side.get("tool_output_chars_per_run")),
                    int(side.get("tool_reported_runs") or 0),
                    int(side.get("reviewer_runs") or 0),
                )
            )
        delta = pair.get("delta") or {}
        lines.append(
            "      delta:   %s billed/run, %s use(s)/run, %s observed output chars/run"
            % (
                _figure(delta.get("billed_per_run")),
                _figure(delta.get("tool_uses_per_run")),
                _figure(delta.get("tool_output_chars_per_run")),
            )
        )
    counted = int(paired.get("pairs_total") or 0)
    ours, theirs, delta = paired.get("with") or {}, paired.get("without") or {}, paired.get("delta") or {}
    fields = ("billed_per_run", "tool_uses_per_run", "tool_output_chars_per_run")
    lines.append(
        "  total, %d pair(s) counted   with: %s billed/run, %s use(s)/run, %s observed output chars/run; "
        "without: %s; delta: %s"
        % (
            counted,
            _figure(ours.get(fields[0])),
            _figure(ours.get(fields[1])),
            _figure(ours.get(fields[2])),
            ", ".join(_figure(theirs.get(field)) for field in fields),
            ", ".join(_figure(delta.get(field)) for field in fields),
        )
    )
    lines.extend(
        [
            "  %d pair(s) is a small-sample observation, not a statistical result. A counted" % counted,
            "  pair holds the change, the tree, the panel, the delivered reviews and the other prompt",
            "  inputs equal; what it does not hold equal is the reviewers' own run-to-run variation, so",
            "  one pair says what happened once. Codex reports no tool activity, so the tool figures",
            "  are over the runs that reported them. A pair is listed but left out of the total when",
            '  its panels differ ("panels differ"), when a reviewer run on either side did not deliver',
            '  ("not delivered"), when the two runs had different prompt inputs ("inputs differ"), or',
            '  when the enclosing run adopted nothing ("nothing adopted").',
        ]
    )
    return lines


def _panel_names(panel: List[Dict[str, Any]]) -> str:
    names = []
    for entry in panel:
        details = (entry.get("provider"), entry.get("model") or "?", entry.get("role") or "?")
        names.append("%s (%s, %s, %s)" % (entry.get("id"), *details))
    return ", ".join(names)


def _pair_exclusions(pair: Dict[str, Any]) -> List[str]:
    """Why a listed pair is left out of the total, one clause per reason."""
    reasons = []
    if not pair.get("same_panel"):
        reasons.append("panels differ (without: %s)" % _panel_names(pair.get("without_panel") or []))
    if not pair.get("delivered"):
        undelivered = pair.get("undelivered") or []
        names = ["%s: %s %s" % (run.get("side"), run.get("id"), run.get("status")) for run in undelivered]
        reasons.append("not delivered (%s)" % ", ".join(names))
    if not pair.get("same_inputs"):
        reasons.append("inputs differ (%s)" % ", ".join(pair.get("inputs_differ") or []))
    if pair.get("nothing_adopted"):
        reasons.append("nothing adopted")
    return reasons


def _context_row(group: Dict[str, Any], adopted: bool) -> str:
    """One group's raw figures: rounds, runs, billed and tool activity."""
    row = "%d round(s), %d run(s), %s billed, %s per round" % (
        int(group.get("rounds") or 0),
        int(group.get("reviewer_runs") or 0),
        "{:,}".format(int(group.get("billed_tokens") or 0)),
        _figure(group.get("billed_per_round")),
    )
    row += "; %s use(s)/run, %s observed output chars/run (%d of %d run(s) reported)" % (
        _figure(group.get("tool_uses_per_run")),
        _figure(group.get("tool_output_chars_per_run")),
        int(group.get("tool_reported_runs") or 0),
        int(group.get("reviewer_runs") or 0),
    )
    if adopted:
        row += "; %s context chars adopted, %s left out" % (
            "{:,}".format(int(group.get("adopted_chars") or 0)),
            "{:,}".format(int(group.get("trimmed_chars") or 0)),
        )
    return row


def _context_per_run_row(group: Dict[str, Any]) -> str:
    """The same group per run and per 1k chars of change -- the line to compare."""
    return (
        "per run and 1k chars of change (%d sized round(s), %s chars): %s billed over %d billed run(s), "
        "%s observed output chars over %d reporting run(s)"
        % (
            int(group.get("sized_rounds") or 0),
            "{:,}".format(int(group.get("change_chars") or 0)),
            _figure(group.get("billed_per_run_per_1k_change_chars")),
            int(group.get("sized_billed_runs") or 0),
            _figure(group.get("tool_output_chars_per_run_per_1k_change_chars")),
            int(group.get("sized_tool_runs") or 0),
        )
    )


def _runs_row(runs: int, reported: int, billed: int, rounds: int, per_round: Optional[int]) -> str:
    """One stage's share of the reviewer spend, for a row under the total."""
    row = "%d (%d reported usage), %s billed over %d round(s)" % (
        runs,
        reported,
        "{:,}".format(billed),
        rounds,
    )
    if per_round:
        row += ", %s each" % "{:,}".format(per_round)
    return row


def _tools_row(report: Dict[str, Any], prefix: str, runs: int) -> str:
    """One stage's tool activity, with the denominator it was divided by.

    The denominator is printed because it is the part that can mislead: "6.5
    uses/run" over half a panel is a different claim from the same figure over
    all of it, and only the count says which.
    """
    return "%s use(s)/run, %s observed output chars/run (%d of %d run(s) reported)" % (
        report["%stool_uses_per_run" % prefix],
        "{:,.1f}".format(report["%stool_output_chars_per_run" % prefix]),
        report["%stool_reported_runs" % prefix],
        runs,
    )


def cmd_progress_record(args: argparse.Namespace) -> int:
    """Record a stage outcome so a loop that achieves nothing can be stopped."""
    book = _ledger(args)
    repeats = book.register_signature(args.stage, args.signature)
    allowed = int(book.settings.get("max_repeats_without_progress") or 0)
    stop = bool(allowed and repeats >= allowed)
    if args.json:
        _emit_json({"stage": args.stage, "repeats": repeats, "stop": stop})
    elif stop:
        _out(
            "%s has produced the same outcome %d times: stop and report, since "
            "retrying is not making progress." % (args.stage, repeats)
        )
    else:
        _out("%s outcome recorded (seen %d time(s))." % (args.stage, repeats))
    return 0


# --------------------------------------------------------------------------- workflow


def _workflow_warning(workspace: ws.Workspace) -> List[str]:
    """Other workflows that look live in this same working tree.

    Separate directories separate the reports, not the files being reported
    on: there is one working tree here and `git diff` reads all of it. Saying
    so is the whole point -- the layout would otherwise suggest an isolation it
    cannot provide.
    """
    if not workspace.workflow:
        return []
    others = workflow_mod.active_elsewhere(workspace.container, workspace.workflow)
    if not others:
        return []
    return [
        "Another workflow is active in this working tree: %s" % ", ".join(others),
        "Artifacts are separate; the files under review are not. For work that"
        " really runs in parallel, give each workflow its own worktree"
        " (git worktree add ../name branch).",
    ]


def cmd_workflow_list(args: argparse.Namespace) -> int:
    _, container = _container(args)
    entries = workflow_mod.listing(container)
    current, _ = workflow_mod.resolve(container, getattr(args, "workflow", "") or "")
    if args.json:
        _emit_json({"current": current, "workflows": entries})
        return 0
    if not entries:
        _out("No workflows recorded yet in %s" % container)
        return 0
    for entry in entries:
        marks = []
        if entry["workflow"] == current:
            marks.append("current")
        if entry["in_flight"]:
            marks.append("in flight: %s" % ", ".join(entry["in_flight"]))
        _out(
            "%s  %s  runs=%d  %s%s"
            % (
                entry["workflow"],
                entry["updated_at"] or entry["started_at"] or "-",
                entry["runs"],
                ("%s/%s" % (entry["last_stage"], entry["last_status"])) if entry["last_stage"] else "-",
                ("  [%s]" % "; ".join(marks)) if marks else "",
            )
        )
    return 0


def cmd_workflow_show(args: argparse.Namespace) -> int:
    root, container = _container(args)
    try:
        workflow, origin = workflow_mod.resolve(container, getattr(args, "workflow", "") or "")
    except workflow_mod.WorkflowError as exc:
        _err(str(exc))
        return 2
    others = workflow_mod.active_elsewhere(container, workflow)
    payload = {
        "workflow": workflow,
        "origin": origin,
        "dir": workflow_mod.workflow_dir(container, workflow),
        "container": container,
        "root": root,
        "also_active": others,
    }
    if args.json:
        _emit_json(payload)
        return 0
    _out("Workflow: %s (from: %s)" % (workflow, origin))
    _out("Artifacts: %s" % payload["dir"])
    if others:
        for line in _workflow_warning(ws.Workspace(root, container, workflow)):
            _out(line)
    return 0


def cmd_workflow_use(args: argparse.Namespace) -> int:
    """Pin an id for callers whose host exports no session of its own."""
    _, container = _container(args)
    try:
        workflow = workflow_mod.normalise(args.id)
    except workflow_mod.WorkflowError as exc:
        _err(str(exc))
        return 2
    workflow_mod.write_pointer(container, workflow, "requested")
    _out("This directory now defaults to workflow %s" % workflow)
    if workflow_mod.session_id():
        _err(
            "Note: this session exports a session id, which wins over the"
            " pointer. Pass --workflow %s, or set %s, to override it." % (workflow, workflow_mod.WORKFLOW_ENV)
        )
    return 0


def cmd_workflow_remove(args: argparse.Namespace) -> int:
    """Delete one workflow's artifacts. The reports are work; ask first."""
    import shutil

    _, container = _container(args)
    try:
        workflow = workflow_mod.normalise(args.id)
    except workflow_mod.WorkflowError as exc:
        _err(str(exc))
        return 2
    directory = workflow_mod.workflow_dir(container, workflow)
    if not os.path.isdir(directory):
        _err("No such workflow: %s" % workflow)
        return 1
    if not args.yes:
        _err("Refusing to delete %s without --yes" % directory)
        return 2
    current, _ = workflow_mod.resolve(container, getattr(args, "workflow", "") or "")
    if workflow == current:
        _err("Refusing to delete the workflow this session is in (%s)" % workflow)
        return 2
    shutil.rmtree(directory)
    _out("Removed %s" % directory)
    return 0


def _reviewed_something(event: Dict[str, Any]) -> bool:
    """Whether this event is a round that put the change in front of a panel.

    The one positive mark such a round leaves is a reviewer that actually
    started: an entry with ``invoked`` true. Nothing else on the stage leaves
    one -- a refusal of either kind records an empty list, and ``clear_stalls``
    records a reason. The entries alone are not the mark: a reviewer that dies
    on provider startup or model resolution is recorded too, with ``invoked``
    false, and a round where every entry is one of those put the change in
    front of nobody. Asked this way round rather than by naming the statuses
    that do not count, so the next bookkeeping status added to the ledger
    cannot quietly become one that clears a refusal.
    """
    reviewers = event.get("reviewers")
    if not isinstance(reviewers, list):
        return False
    return any(isinstance(run, dict) and run.get("invoked") for run in reviewers)


def _ran_since_last_round(
    events: List[Dict[str, Any]],
    review_stage: str,
    run_stage: str,
    then_stages: Tuple[str, ...] = (),
    counts: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Tuple[bool, bool]:
    """Whether ``run_stage`` answered after the last round that reviewed something.

    The first value: an ``ok`` run of ``run_stage`` with a usable result since
    that round, and one ``counts`` accepts when it is given. An event without
    ``answered`` predates the key and counts; an ``ok`` over silence saved
    nothing and does not. The second: whether any ``then_stages`` event,
    whatever its status, follows that run. Rounds that reviewed nothing --
    refusals, abandoned entries -- are not the round.
    """
    ran = False
    then = False
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        stage = event.get("stage")
        if stage == review_stage and _reviewed_something(event):
            break
        if (
            stage == run_stage
            and event.get("status") == "ok"
            and event.get("answered", True) is not False
            and (counts is None or counts(event))
        ):
            ran = True
            break
        if stage in then_stages:
            then = True
    return ran, ran and then


def _wrote_plan(workspace: ws.Workspace) -> Callable[[Dict[str, Any]], bool]:
    """Whether an architect run event wrote its answer to this workflow's plan.

    Only such a run is a revision of the plan: an architect run asked
    something else, or left on stdout, is not the revision a round is owed.
    """
    plan = os.path.normcase(os.path.abspath(workspace.plan_path))

    def counts(event: Dict[str, Any]) -> bool:
        output = event.get("output")
        if not isinstance(output, str) or not output or output == "-":
            return False
        return os.path.normcase(os.path.abspath(_in_workflow(workspace, output) or output)) == plan

    return counts


def _approved_as_recorded(info: Dict[str, Any]) -> bool:
    """Whether the recorded approval covers this plan and this design round.

    Read from the record rather than from ``state``: with
    ``design.require_approval`` off the state is ``not-required`` whatever was
    recorded, and a plan the user did approve is still one they approved.
    """
    return info.get("matches_current_plan") is True and info.get("reviewed_since_approval") is False


def _design_final_pass(
    blocking: List[Dict[str, Any]],
    iteration: int,
    max_iterations: int,
    of_current_plan: Optional[bool],
    ran_since: bool,
    architect_left: Optional[int],
    approved: bool,
    implemented: bool,
    accepted: bool,
) -> Dict[str, Any]:
    """Where the one revision the round that reached the limit still gets stands.

    The limit counts reviews, not revisions: the last round's findings are
    folded in once more, and only the re-review of that revision is refused.
    ``status`` and ``review status --design`` both read this, so they cannot
    disagree about it. With none of the findings accepted there is nothing to
    fold in, and the spent budget is the stop it always was (``unaccepted``).
    """
    plan_changed = None
    if not blocking or iteration < max_iterations:
        state = None
    elif approved:
        state = "approved"
    elif implemented:
        # A plan already built on is not asked to change under the code.
        state = "implemented"
    elif of_current_plan is False or ran_since or of_current_plan is None:
        state = "done"
        plan_changed = True if of_current_plan is False else (None if of_current_plan is None else False)
    elif not accepted:
        state = "unaccepted"
    elif architect_left == 0:
        state = "blocked"
    else:
        state = "pending"
    return {"state": state, "pending": state == "pending", "plan_changed": plan_changed}


def _code_final_pass(
    blocking: List[Dict[str, Any]],
    iteration: int,
    max_iterations: int,
    fixed_since: bool,
    retested_since: bool,
    fixer_left: Optional[int],
    accepted: bool,
) -> Dict[str, Any]:
    """The code review's counterpart: the last round gets its fix and re-test.

    Only accepted findings are fixed, so with none accepted the spent budget
    stops as it always did (``unaccepted``).
    """
    if not blocking or iteration < max_iterations:
        state = None
    elif fixed_since:
        state = "done" if retested_since else "retest"
    elif not accepted:
        state = "unaccepted"
    elif fixer_left == 0:
        state = "blocked"
    else:
        state = "pending"
    return {"state": state, "pending": state == "pending"}


def _context_refusal(events: List[Dict[str, Any]], stage: str) -> Optional[Dict[str, Any]]:
    """The context refusal ``stage`` is still sitting on, if it is.

    Outstanding means: refused for size, and nothing has reviewed the change
    since. A refusal deliberately leaves the previous round's consolidation in
    place -- that is how its triage survives -- so without this, an oversized
    change refused today still answers ``continue`` from a clean review of
    something else, which was the hole this closes.

    Only a round that actually reviewed something supersedes it, because only
    that round's own coverage can say where the change stands afterwards. It
    is emphatically not "the last thing that happened to this stage": a stage
    collects entries nobody reviewed anything for. ``status`` itself writes
    one, calling ``clear_stalls`` before it reads these events, so a killed
    round left in flight would otherwise clear the very refusal this call is
    looking for.

    Nor does a refusal supersede a refusal. Two refused rounds mean nothing
    was reviewed twice -- a gate refusal after a size refusal, then a passing
    test run, still leaves the oversized change unread -- which is more reason
    to stop and report, not less.
    """
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("stage") != stage:
            continue
        if _reviewed_something(event):
            return None
        if event.get("status") == opt_mod.REFUSED and event.get("refused_by") == "context":
            return event
    return None


def _refusal_reason(event: Dict[str, Any], design: bool = False) -> str:
    """One refusal, in the words the final report has to use.

    The size and the limit come from the event because a refused round writes
    no consolidation to read them from -- that is the whole point of it.
    """
    context = event.get("context") or {}
    chars = context.get("chars")
    limit = context.get("max_chars")
    return (
        "the last %s round was refused: the change body (%s chars) is over "
        "review.context.max_chars (%s), so nothing was reviewed"
        % (
            "design review" if design else "review",
            "{:,}".format(chars) if isinstance(chars, int) else "size unrecorded",
            "{:,}".format(limit) if isinstance(limit, int) else "the limit",
        )
    )


def _approval_line(
    info: Dict[str, Any],
    plan_relative: str,
    design_pass: Optional[Dict[str, Any]] = None,
    architect_left: Optional[int] = None,
) -> str:
    """The `Plan approval:` line of `status`, worded as what to do next."""
    line = _approval_advice(info, plan_relative, design_pass or {})
    # Not a stop: nothing needs the architect until the user asks for a
    # change, and then `run architect` refuses and says so.
    if info["pending"] and architect_left == 0 and (design_pass or {}).get("state") != "blocked":
        line += " (no architect attempt is left for changes; `budget reset architect` first)"
    return line


def _approval_advice(info: Dict[str, Any], plan_relative: str, design_pass: Dict[str, Any]) -> str:
    state = info["state"]
    final = design_pass.get("state")
    open_findings = ", ".join(info.get("open_findings") or [])
    if info["pending"] and info.get("design_review_exhausted") and final == "pending":
        return (
            "%s -- the design review budget is spent; fold the accepted findings (%s) into %s first "
            "(review fix-brief --design, then run architect) without re-reviewing, then present the "
            "plan and ask" % (state, open_findings, plan_relative)
        )
    if info["pending"] and final == "done" and design_pass.get("plan_changed") is True:
        return (
            "%s -- present %s to the user with %s still open from a review of an earlier revision "
            "(the design review budget is spent, so this revision is not re-reviewed) and ask; a yes "
            "is recorded with `design approve`, never without one" % (state, plan_relative, open_findings)
        )
    if info["pending"] and final == "done" and design_pass.get("plan_changed") is False:
        return (
            "%s -- the architect left %s unchanged over %s (the design review budget is spent); "
            "present the plan and the findings and ask whether to approve over them (`design approve`) "
            "or to triage them again" % (state, plan_relative, open_findings)
        )
    if info["pending"] and final == "blocked":
        return (
            "%s -- the design review budget is spent with %s open and no architect attempt is left to "
            "fold them in; report them and ask the user whether to approve over them (`design approve`) "
            "or to free an attempt (`budget reset`) and revise" % (state, open_findings)
        )
    if info["pending"] and info.get("design_review_exhausted"):
        # A spent design review is a stop-and-report, and the report has to
        # end in the question or the orchestrator reads the stop as the end.
        return (
            "%s -- the design review budget is spent with %s open; report them and ask the user "
            "whether to approve over them (`design approve`) or to revise"
            % (state, ", ".join(info.get("open_findings") or []))
        )
    if state == "pending":
        return (
            "pending -- present %s to the user and ask; a yes is recorded with `design approve`, "
            "never without one" % plan_relative
        )
    if state == "stale" and info.get("stale_reason") == "plan-changed":
        return "stale -- the plan changed after it was approved (%s -> %s); present it again and ask" % (
            str(info.get("approved_sha256") or "")[:12],
            str(info.get("plan_sha256") or "")[:12],
        )
    if state == "stale":
        return "stale -- a design review ran after the plan was approved; present its findings and ask again"
    if state == "approved":
        return "approved (%s)" % str(info.get("approved_sha256") or "")[:12]
    if state == "not-required":
        return "not required (design.require_approval: false)"
    if state == "implemented-unapproved":
        return (
            "not recorded; the implementer already ran on this plan (this gate is newer than the "
            "workflow) -- nothing to ask unless it is to run again, which needs `design approve`"
        )
    return "no plan"


def cmd_status(args: argparse.Namespace) -> int:
    """One verdict the orchestrator can act on: continue, or stop and report."""
    loaded = config_mod.load(args.cwd, validate_result=False)
    workspace = _workspace(args)
    book = _ledger(args, workspace)
    abandoned = book.clear_stalls()

    review_data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    settings = loaded.review_settings()
    severities = tuple(settings.get("re_review_severities") or ("critical", "high"))
    blocking = review_mod.unresolved_blocking(review_data, severities)
    iteration = int(review_data.get("iteration", 0) or 0)
    max_iterations = int(settings.get("max_review_iterations", 2))
    summary = book.summary()

    # The design review is read whether or not it is enabled: a round that was
    # run by hand, or left open when the setting was turned back off, is still
    # an open finding about the plan the implementation would follow.
    design_data = ws.read_json(workspace.design_review().consolidated_json_path, {}) or {}
    design_settings = loaded.design_review_settings()
    design_blocking = review_mod.unresolved_blocking(design_data, severities)
    design_iteration = int(design_data.get("iteration", 0) or 0)
    design_max = int(design_settings.get("max_iterations", 2))
    design_exhausted = bool(design_blocking) and design_iteration >= design_max
    approval_info = approval_mod.current(workspace, bool(loaded.design_settings().get("require_approval")))
    # The same list `design approve` names, whatever the severity: the user is
    # asked about what the approval will record, not only what blocks.
    design_open = [str(f.get("id")) for f in approval_mod.open_findings(design_data)]
    plan_text, plan_digest = approval_mod.read_plan(workspace)
    of_current_plan = approval_mod.findings_of_current_plan(workspace, plan_text)
    approval_info["open_findings"] = design_open
    approval_info["open_findings_of_current_plan"] = of_current_plan if design_open else None
    approval_info["design_review_exhausted"] = design_exhausted

    events = [event for event in (workspace.read_state().get("events") or []) if isinstance(event, dict)]
    refused_for_size = _context_refusal(events, "review")
    design_refused_for_size = _context_refusal(events, "design_review")

    # The round that reached the limit still gets its revision and its fix;
    # the limit refuses only the re-review. Until those are done the spent
    # budget is not a stop.
    architect_left = summary["budgets"].get("architect", {}).get("remaining")
    plan_approved = _approved_as_recorded(approval_info)
    design_accepted = bool(review_mod.accepted_findings(design_data))
    design_pass = _design_final_pass(
        design_blocking,
        design_iteration,
        design_max,
        of_current_plan,
        _ran_since_last_round(events, "design_review", "architect", counts=_wrote_plan(workspace))[0],
        architect_left,
        plan_approved,
        approval_mod.implemented_since_plan(workspace, events),
        design_accepted,
    )
    fixed, retested = _ran_since_last_round(events, "review", "review_fixer", ("test", "re-test"))
    review_pass = _code_final_pass(
        blocking,
        iteration,
        max_iterations,
        fixed,
        retested,
        summary["budgets"].get("review_fixer", {}).get("remaining"),
        bool(review_mod.accepted_findings(review_data)),
    )
    review_repeats = int((summary["signatures"] or {}).get("review") or 0)
    design_repeats = int((summary["signatures"] or {}).get("design_review") or 0)

    reasons: List[str] = []
    if refused_for_size:
        reasons.append(_refusal_reason(refused_for_size))
    if design_refused_for_size:
        reasons.append(_refusal_reason(design_refused_for_size, design=True))
    if review_pass["state"] in ("done", "blocked", "unaccepted"):
        suffix = ""
        if review_pass["state"] == "blocked":
            suffix = " and no review_fixer attempt left"
        elif review_pass["state"] == "done":
            suffix = "; fixed and re-tested after the last round, not re-reviewed -- report"
        reasons.append(
            "review budget spent (%d/%d rounds) with %d finding(s) still open%s"
            % (iteration, max_iterations, len(blocking), suffix)
        )
    # A repeat does not stop the fix still owed to the last round.
    if review_repeats > 1 and review_pass["state"] not in ("pending", "retest"):
        reasons.append("the last review round found exactly what the previous one found")
    # Left out once the user has approved this plan (`approved`): they were
    # shown the open findings and decided to go ahead over them, which is what
    # the stop was waiting for. Kept, it would say stop at every stage that
    # follows. Left out too while the last round's revision is still owed.
    if design_pass["state"] in ("done", "blocked", "implemented", "unaccepted"):
        suffix = ""
        if design_pass["state"] == "blocked":
            suffix = " and no architect attempt left to fold them in"
        elif design_pass["state"] == "done" and design_pass["plan_changed"] is True:
            suffix = "; revised after the last round, not re-reviewed -- present the plan and ask"
        elif design_pass["state"] == "done" and design_pass["plan_changed"] is False:
            suffix = (
                "; the architect left the plan unchanged after the last round -- present the plan "
                "and the findings and ask"
            )
        reasons.append(
            "design review budget spent (%d/%d rounds) with %d finding(s) still open%s"
            % (design_iteration, design_max, len(design_blocking), suffix)
        )
    if design_repeats > 1 and design_pass["state"] != "pending":
        reasons.append("the last design review round found exactly what the previous one found")
    for stage, entry in summary["budgets"].items():
        if entry["remaining"] != 0:
            continue
        # With a plan written, the architect is needed again only for a
        # revision a design round still owes before its limit: at the limit
        # the revision says so in its own reason above, an approved plan is
        # not revised, and a change asked for at approval is refused by
        # `run architect`.
        if (
            stage == "architect"
            and plan_digest
            and (design_pass["state"] is not None or not design_blocking or plan_approved)
        ):
            continue
        # A fix already made needs its re-test or its report, not the fixer.
        if stage == "review_fixer" and review_pass["state"] in ("retest", "done"):
            continue
        reasons.append("%s has no attempts left" % stage)
    total = summary["total_delegated_runs"]
    if total["limit"] and total["used"] >= int(total["limit"]):
        reasons.append("no delegated runs left in this workflow")
    # Asked of the ledger rather than of the rounded summary key: advice that
    # says stop while `run` still goes is the mismatch this budget exists to
    # remove, and half a second of remaining runtime is enough to cause it.
    if book.runtime_refusal():
        reasons.append("the delegated runtime budget is spent")

    # What the next `review run` would decide, so the orchestrator finds out
    # here rather than by being refused. Cheap: the snapshot meta is already
    # on disk, and nothing is delegated to work it out.
    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
    plan = opt_mod.decide(
        loaded.optimization_settings(),
        settings,
        _risk_paths(meta),
        int(meta.get("lines_added") or 0) + int(meta.get("lines_deleted") or 0),
        workspace.last_status("test"),
        len(loaded.reviewers()),
        reviewed_files=len(meta.get("files") or []),
    )
    if plan.gate == opt_mod.GATE_REFUSE:
        reasons.append("the last recorded test run failed; fix it before reviewing")

    payload = {
        "verdict": "stop-and-report" if reasons else "continue",
        "reasons": reasons,
        "stalls": summary["stalls"],
        "abandoned_stages": abandoned,
        "in_flight": summary["in_flight"],
        "review": {
            "iteration": iteration,
            "max_review_iterations": max_iterations,
            "blocking": [f["id"] for f in blocking],
            "accepted": len(review_mod.accepted_findings(review_data)),
            # The refusal itself, not a flag: the size and the limit are what
            # the report has to name, and they are not in the consolidation --
            # a refused round writes no consolidation at all.
            "refused_for_size": (refused_for_size or {}).get("context") or None,
            # The ledger's repeat count, named as the run-log event names it.
            "identical_rounds": review_repeats,
            "final_fix": review_pass["state"],
            "final_fix_pending": review_pass["pending"],
        },
        "design_review": {
            "enabled": bool(design_settings.get("enabled")),
            "iteration": design_iteration,
            "max_iterations": design_max,
            "blocking": [f["id"] for f in design_blocking],
            "accepted": len(review_mod.accepted_findings(design_data)),
            "refused_for_size": (design_refused_for_size or {}).get("context") or None,
            "identical_rounds": design_repeats,
            "final_revision": design_pass["state"],
            "final_revision_pending": design_pass["pending"],
        },
        # Not a reason and not a verdict: `run implementer` enforces it, and a
        # stop-and-report here would read as "give up" where the answer is to
        # ask the user.
        "design_approval": approval_info,
        "budgets": summary["budgets"],
        "total_delegated_runs": total,
        "runtime_remaining_seconds": summary["runtime_remaining_seconds"],
        "runtime": summary["runtime"],
        # Reported, never enforced: no verdict here turns on what a run cost.
        "tokens": summary["tokens"],
        "optimization": plan.to_dict(),
        "workflow": workspace.workflow,
        # Named here because status is the command the orchestrator consults
        # before every stage: if another session is working in this tree, that
        # is the moment to know, not after two reviews disagree about what the
        # change even is.
        "also_active": workflow_mod.active_elsewhere(workspace.container, workspace.workflow)
        if workspace.workflow
        else [],
    }
    if args.json:
        _emit_json(payload)
        return 0

    _out("Verdict: %s" % payload["verdict"].upper())
    for reason in reasons:
        _out("  - %s" % reason)
    for line in _workflow_warning(workspace):
        _out("  ! %s" % line)
    if summary["stalls"]:
        _out("")
        _out("Stalled stages:")
        for stall in summary["stalls"]:
            _out(
                "  %s started %s (%.0fs ago) -- %s"
                % (stall["stage"], stall["started_at"], stall["elapsed_seconds"], stall["reason"])
            )
    if abandoned:
        _out("")
        _out("Cleared %d stage(s) whose process is gone: %s" % (len(abandoned), ", ".join(abandoned)))
    if summary["in_flight"]:
        _out("")
        _out("In flight:")
        for token, entry in summary["in_flight"].items():
            _out("  %s (%s) since %s" % (entry.get("stage"), token, entry.get("started_at")))
    _out("")
    line = "Review: round %d/%d, %d accepted, %d blocking" % (
        iteration,
        max_iterations,
        payload["review"]["accepted"],
        len(blocking),
    )
    if review_pass["state"] == "pending":
        line += " -- final fix pending (fix, re-test, do not re-review)"
    elif review_pass["state"] == "retest":
        line += " -- final fix done, re-test pending (record it, do not re-review)"
    if review_pass["state"] in ("pending", "retest") and review_repeats > 1:
        line += "; identical to the previous round"
    _out(line)
    line = "Design review: %s, round %d/%d, %d accepted, %d blocking" % (
        "on" if payload["design_review"]["enabled"] else "off",
        design_iteration,
        design_max,
        payload["design_review"]["accepted"],
        len(design_blocking),
    )
    if design_pass["pending"]:
        line += " -- final revision pending (fold the findings in, do not re-review)"
        if design_repeats > 1:
            line += "; identical to the previous round"
    _out(line)
    _out(
        "Plan approval: %s"
        % _approval_line(approval_info, workspace.relative(workspace.plan_path), design_pass, architect_left)
    )
    line = "Optimization: %s" % plan.level
    if plan.escalated:
        line += " (escalated from %s -- %s)" % (plan.requested, plan.escalation_note().split(": ", 1)[-1])
    line += ", tests %s" % (plan.test_status or "not recorded")
    if plan.reviewer_limit is not None:
        line += ", %d reviewer" % plan.reviewer_limit
    _out(line)
    tokens = summary["tokens"]["totals"]
    if tokens["runs"]:
        _out(
            "Tokens: %s billed over %d run(s)%s (dev-orchestra tokens show)"
            % (
                "{:,}".format(int(tokens["billed_tokens"] or 0)) or "0",
                tokens["runs"],
                "" if summary["tokens"]["complete"] else ", partially reported",
            )
        )
    return 0


def cmd_design_approve(args: argparse.Namespace) -> int:
    """Record the user's yes to the plan as it is now.

    Open design findings are printed rather than refused: going ahead over
    them is the user's decision, and this command is only ever the record of
    one. What it adds is whether the findings are about this plan or about an
    earlier revision of it, which the report has to say.
    """
    loaded = config_mod.load(args.cwd, validate_result=False)
    workspace = _workspace(args)
    plan_relative = workspace.relative(workspace.plan_path)
    plan_text, digest = approval_mod.read_plan(workspace)
    if not digest:
        _err("no plan to approve at %s -- run the architect first" % plan_relative)
        return 2

    # The findings and the round they belong to come out of one read of one
    # report, so they cannot describe two different rounds. A round that has
    # started but has no report yet -- still running, or it failed -- is
    # refused rather than approved over: its findings are the ones the user
    # has not seen. Checked after the report is read, so a round that starts
    # in between is refused too, and one that starts after this makes the
    # recorded approval stale.
    design_data = ws.read_json(workspace.design_review().consolidated_json_path, {}) or {}
    round_id = approval_mod.reported_round(design_data)
    latest = approval_mod.design_round(workspace)
    unreviewed = (
        latest is not None and latest != round_id and latest == approval_mod.unreviewed_round(design_data)
    )
    if unreviewed:
        # The round ran to the end and nobody reviewed it: there is nothing
        # left to wait for, and refusing would leave the user no way to go
        # ahead once the design review budget is spent. The approval is given
        # over that round, so it is not stale the moment it is recorded. Its
        # report has no findings -- nobody reviewed it -- which the note below
        # says, so an empty list does not read as a clean review.
        round_id = latest
    elif latest != round_id:
        _err(
            "not recording an approval: the latest design review round has no report yet -- "
            "it is still running or did not finish. Wait for it (or run `review run --design` "
            "again), present its findings, and ask again."
        )
        return 2
    open_findings = [str(f.get("id")) for f in approval_mod.open_findings(design_data)]
    of_current_plan = approval_mod.findings_of_current_plan(workspace, plan_text) if open_findings else None

    previous = workspace.read_state().get(approval_mod.STAGE)
    already = (
        isinstance(previous, dict)
        and previous.get("sha256") == digest
        and previous.get("design_round") == round_id
    )
    if already:
        entry = previous
    else:
        entry = approval_mod.record(workspace, digest, open_findings, of_current_plan, round_id)

    if args.json:
        payload = dict(entry)
        payload.update({"already_approved": already, "workflow": workspace.workflow})
        _emit_json(payload)
    else:
        _out(
            "%s %s (sha256 %s) for workflow %s"
            % ("already approved" if already else "approved", plan_relative, digest[:12], workspace.workflow)
        )
    if not loaded.design_settings().get("require_approval"):
        _err("note: design.require_approval is false; recorded anyway")
    if unreviewed:
        _err(
            "note: the latest design review round ended with no reviewer's review (every reviewer "
            "failed), so this plan has no design review findings at all; the approval goes ahead "
            "without one -- say so in the report"
        )
    if open_findings and of_current_plan is False:
        _err(
            "note: %d design finding(s) open from a review of an earlier revision of this plan: %s "
            "-- the revision may already address them; say so in the report"
            % (len(open_findings), ", ".join(open_findings))
        )
    elif open_findings:
        _err(
            "note: %d design finding(s) still open: %s -- approving over them is the user's call; "
            "name them in the report" % (len(open_findings), ", ".join(open_findings))
        )
    return 0


def cmd_state_show(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    state = workspace.read_state()
    if args.json:
        _emit_json(state)
        return 0
    events = state.get("events", [])
    if not events:
        _out("No recorded stages yet.")
        return 0
    for event in events:
        _out(
            "%-14s %-8s %s %s"
            % (event.get("stage"), event.get("status"), event.get("at"), event.get("model") or "")
        )
    return 0


def cmd_state_record(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    detail: Dict[str, Any] = {}
    for item in args.detail or []:
        key, _, value = item.partition("=")
        detail[key] = config_mod.coerce_scalar(value)
    workspace.record_event(args.stage, args.status, detail)
    _out("recorded %s=%s" % (args.stage, args.status))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    workspace = _workspace(args)
    state = workspace.read_state()
    review_data = ws.read_json(workspace.consolidated_json_path, {}) or {}

    lines = ["Workflow:"]
    seen: Dict[str, str] = {}
    for event in state.get("events", []):
        seen[str(event.get("stage"))] = str(event.get("status"))
    for stage in (
        "architect",
        "design_review",
        "design_approval",
        "implementer",
        "test",
        "review",
        "review_fixer",
        "re-test",
    ):
        if stage in seen:
            lines.append("  %-14s %s" % (stage, "OK" if seen[stage] == "ok" else seen[stage].upper()))
    design_counts = (ws.read_json(workspace.design_review().consolidated_json_path, {}) or {}).get("counts")
    if design_counts:
        lines.append(
            "  %-14s %s/%s ok"
            % ("design reviews", design_counts.get("reviewers_ok"), design_counts.get("reviewers_total"))
        )
    counts = review_data.get("counts", {})
    if counts:
        lines.append(
            "  %-14s %s/%s ok" % ("reviews", counts.get("reviewers_ok"), counts.get("reviewers_total"))
        )
    lines.append("")
    lines.append("Models:")
    for key, title in wizard_mod.ROLE_TITLES:
        spec = loaded.data.get(key) or {}
        model = spec.get("model") or {}
        lines.append(
            "  %-14s %s / %s / %s"
            % (title, spec.get("provider"), model.get("family", "default"), model.get("version", "latest"))
        )
    lines.append("Review:")
    # Prefer the models actually resolved during the run; fall back to config.
    for reviewer in review_data.get("reviewers") or loaded.reviewers():
        model = reviewer.get("model")
        if isinstance(model, dict):
            model = model.get("family", "default")
        lines.append(
            "  %-14s %s / %s%s"
            % (
                reviewer.get("id"),
                reviewer.get("provider"),
                model or "default",
                "" if reviewer.get("status", "ok") == "ok" else " (FAILED)",
            )
        )
    # A round the gate refused is recorded but ran nothing, so it appears in
    # no other part of this report -- and "what you skipped" is exactly what
    # the final report is required to name.
    decided = opt_mod.summarise_rounds(state.get("events") or [])
    if decided["refused"] or decided["panel_reduced"] or decided["design_refused"]:
        lines.append("")
        lines.append("Optimization:")
        # One line per reason, because "what you skipped" is only useful if it
        # says what would make the round run: fixing the tests, or narrowing
        # the change. A single total says neither.
        for reason, count in sorted(decided["refused_by"].items()):
            cause = _REFUSAL_CAUSE.get(reason, reason)
            lines.append("  %-14s %d round(s) not run: %s" % (reason, count, cause))
        if decided["design_refused"]:
            lines.append(
                "  %-14s %d design round(s) not run: plan over review.context.max_chars"
                % ("context", decided["design_refused"])
            )
        if decided["panel_reduced"]:
            lines.append(
                "  %-14s %d round(s) cut to one reviewer -- one opinion, not an independent second"
                % ("panel", decided["panel_reduced"])
            )
        lines.append("  %-14s dev-orchestra optimization report" % "detail")

    book = _ledger(args, workspace)
    report = book.token_report()
    if report["totals"]["runs"]:
        lines.append("")
        lines.append("Tokens:")
        for stage, account in sorted(report["by_stage"].items()):
            lines.append("  %-14s %s billed" % (stage, "{:,}".format(int(account.get("billed_tokens") or 0))))
        total = report["totals"]
        cost = float(total.get("cost_usd") or 0.0)
        lines.append(
            "  %-14s %s billed over %d run(s)%s%s"
            % (
                "total",
                "{:,}".format(int(total.get("billed_tokens") or 0)),
                total["runs"],
                (" -- $%.4f" % cost) if cost else "",
                "" if report["complete"] else " (partially reported: a floor)",
            )
        )
    if args.json:
        _emit_json({"stages": seen, "counts": counts, "tokens": report})
        return 0
    _out("\n".join(lines))
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dev-orchestra",
        description="Configuration, diagnostics and review plumbing for the"
        " AI Development Orchestrator skill.",
    )
    parser.add_argument("--version", action="version", version="dev-orchestra %s" % __version__)
    parser.add_argument("--cwd", default=None, help="operate as if run from this directory")
    parser.add_argument(
        "--workflow",
        default="",
        help="the workflow these artifacts belong to (default: this session's)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # config -----------------------------------------------------------------
    config_parser = subparsers.add_parser("config", help="show and edit configuration")
    config_sub = config_parser.add_subparsers(dest="subcommand", required=True)

    show = config_sub.add_parser("show", help="show the effective configuration")
    show.add_argument("--scope", choices=["global", "project", "effective"], default="effective")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_config_show)

    path_parser = config_sub.add_parser("path", help="print config file locations")
    path_parser.set_defaults(func=cmd_config_path)

    setup = config_sub.add_parser("setup", help="run the setup wizard")
    setup.add_argument("--scope", choices=["global", "project"], default="global")
    setup.add_argument(
        "--defaults", action="store_true", help="write the recommended config without prompting"
    )
    setup.add_argument("--force", action="store_true", help="prompt even without a TTY")
    setup.set_defaults(func=cmd_config_setup)

    reset = config_sub.add_parser(
        "reset", help="clear this layer's overrides (the file keeps only version); --delete removes it"
    )
    reset.add_argument("--scope", choices=["global", "project"], default=None)
    reset.add_argument("--delete", action="store_true", help="delete the config file instead of clearing it")
    reset.set_defaults(func=cmd_config_reset)

    prune = config_sub.add_parser("prune", help="drop values equal to what the layer inherits")
    prune.add_argument("--scope", choices=["global", "project"], default=None)
    prune.add_argument("--dry-run", action="store_true", help="list what would be dropped, write nothing")
    prune.set_defaults(func=cmd_config_prune)

    set_parser = config_sub.add_parser("set", help="set one value, e.g. implementer.model.family opus")
    set_parser.add_argument("path")
    set_parser.add_argument("value")
    set_parser.add_argument("--scope", choices=["global", "project"], default=None)
    set_parser.add_argument("--raw", action="store_true", help="keep the value as a string")
    set_parser.set_defaults(func=cmd_config_set)

    validate = config_sub.add_parser("validate", help="validate the effective configuration")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=cmd_config_validate)

    # model ------------------------------------------------------------------
    model_parser = subparsers.add_parser("model", help="inspect available models")
    model_sub = model_parser.add_subparsers(dest="subcommand", required=True)
    model_list = model_sub.add_parser("list", help="list models the installed CLIs advertise")
    model_list.add_argument("--provider", choices=available_providers(), default=None)
    model_list.add_argument("--json", action="store_true")
    model_list.set_defaults(func=cmd_model_list)

    # reviewer ---------------------------------------------------------------
    reviewer_parser = subparsers.add_parser("reviewer", help="manage the review panel")
    reviewer_sub = reviewer_parser.add_subparsers(dest="subcommand", required=True)

    r_list = reviewer_sub.add_parser("list", help="list configured reviewers")
    r_list.add_argument("--json", action="store_true")
    r_list.set_defaults(func=cmd_reviewer_list)

    r_add = reviewer_sub.add_parser("add", help="add a reviewer")
    r_add.add_argument("--provider", required=True, choices=available_providers())
    r_add.add_argument("--model", default=None, help="model family (default: provider's recommended)")
    r_add.add_argument("--role", default="general")
    r_add.add_argument("--id", default=None)
    r_add.add_argument("--pin", default=None, help="pin an exact model id instead of tracking latest")
    r_add.add_argument("--scope", choices=["global", "project"], default=None)
    r_add.set_defaults(func=cmd_reviewer_add)

    r_remove = reviewer_sub.add_parser("remove", help="remove a reviewer by id, role, or position")
    r_remove.add_argument("selector")
    r_remove.add_argument("--scope", choices=["global", "project"], default=None)
    r_remove.set_defaults(func=cmd_reviewer_remove)

    r_set = reviewer_sub.add_parser("set", help="change an existing reviewer")
    r_set.add_argument("selector")
    r_set.add_argument("--provider", choices=available_providers(), default=None)
    r_set.add_argument("--model", default=None)
    r_set.add_argument("--role", default=None)
    r_set.add_argument("--id", default=None)
    r_set.add_argument("--pin", default=None)
    r_set.add_argument("--scope", choices=["global", "project"], default=None)
    r_set.set_defaults(func=cmd_reviewer_set)

    # doctor -----------------------------------------------------------------
    doctor_parser = subparsers.add_parser("doctor", help="diagnose CLIs, auth and configuration")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument("--fast", action="store_true", help="skip model discovery")
    doctor_parser.add_argument("--strict", action="store_true", help="exit non-zero when problems are found")
    doctor_parser.set_defaults(func=cmd_doctor)

    # run --------------------------------------------------------------------
    run_parser = subparsers.add_parser("run", help="run one configured role against a prompt")
    run_parser.add_argument(
        "role", help="orchestrator | architect | implementer | review_fixer | <reviewer id>"
    )
    run_parser.add_argument(
        "--tier",
        default=None,
        help="run this role on one of its configured model_tiers (e.g. light)",
    )
    run_parser.add_argument("--prompt", default=None)
    run_parser.add_argument("--prompt-file", default=None, help="path, or - for stdin")
    run_parser.add_argument("--mode", choices=list(MODES), default=None)
    run_parser.add_argument(
        "--output",
        default=None,
        help="write the response here instead of stdout; kept as it is when the run produced none",
    )
    run_parser.add_argument("--timeout", type=int, default=None, help="total deadline in seconds")
    run_parser.add_argument(
        "--idle-timeout",
        type=float,
        default=None,
        help="treat as stalled after this long with no output (streaming providers only)",
    )
    run_parser.add_argument("--force", action="store_true", help="run even though a budget is spent")
    run_parser.add_argument(
        "--detach",
        action="store_true",
        help="start the run in its own process and return a job id immediately",
    )
    run_parser.add_argument("--json", action="store_true", help="machine-readable output")
    run_parser.add_argument("--job-file", default=None, help=argparse.SUPPRESS)
    run_parser.add_argument("--print-command", action="store_true", help="print the CLI invocation and exit")
    run_parser.add_argument("--extra", nargs=argparse.REMAINDER, help="extra args passed to the provider CLI")
    run_parser.set_defaults(func=cmd_run)

    # review -----------------------------------------------------------------
    review_parser = subparsers.add_parser("review", help="independent multi-model review pipeline")
    review_sub = review_parser.add_subparsers(dest="subcommand", required=True)

    snapshot = review_sub.add_parser("snapshot", help="freeze the change under review")
    snapshot.add_argument("--base", default=None, help="revision to diff against (default: HEAD)")
    snapshot.add_argument("--no-untracked", action="store_true")
    snapshot.add_argument(
        "--no-exclude",
        action="store_true",
        help="send every changed file, including generated and vendored ones",
    )
    snapshot.add_argument(
        "--full",
        action="store_true",
        help="diff the whole change even on a re-review, not just what the fix changed",
    )
    snapshot.add_argument(
        "--surrounding",
        choices=context_mod.SURROUNDING_MODES,
        default=None,
        help="override review.context.surrounding for this snapshot only",
    )
    snapshot.add_argument("--json", action="store_true")
    snapshot.set_defaults(func=cmd_review_snapshot)

    def _add_design_flag(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--design",
            action="store_true",
            help="the design review of .ai/plan.md instead of the code review",
        )

    review_run = review_sub.add_parser("run", help="run every reviewer against the frozen snapshot")
    review_run.add_argument(
        "--request",
        default=None,
        help="the design request the plan answers (--design; default .ai/execution/design-request.md)",
    )
    review_run.add_argument(
        "--iteration",
        type=int,
        default=None,
        help="review round; derived from the snapshot when omitted",
    )
    review_run.add_argument("--sequential", action="store_true")
    review_run.add_argument("--only", nargs="*", default=None, help="reviewer ids or roles to run")
    review_run.add_argument("--context", default=None, help="extra context for reviewers")
    review_run.add_argument("--base", default=None)
    review_run.add_argument("--timeout", type=int, default=None)
    review_run.add_argument("--idle-timeout", type=float, default=None)
    review_run.add_argument(
        "--force",
        action="store_true",
        help="run past the review iteration budget, or with the tests recorded as failing",
    )
    review_run.add_argument(
        "--surrounding",
        choices=context_mod.SURROUNDING_MODES,
        default=None,
        help="override review.context.surrounding for this run only",
    )
    review_run.add_argument("--json", action="store_true")
    review_run.set_defaults(func=cmd_review_run)

    consolidate = review_sub.add_parser("consolidate", help="re-parse reports and dedupe findings")
    consolidate.add_argument("--iteration", type=int, default=None)
    consolidate.add_argument("--json", action="store_true")
    consolidate.set_defaults(func=cmd_review_consolidate)

    review_show = review_sub.add_parser("show", help="show the consolidated review")
    review_show.add_argument("--accepted", action="store_true", help="only accepted findings")
    review_show.add_argument("--json", action="store_true")
    review_show.set_defaults(func=cmd_review_show)

    triage = review_sub.add_parser("triage", help="record a triage decision for findings")
    triage.add_argument("ids", nargs="+")
    triage.add_argument("--status", required=True, choices=list(review_mod.TRIAGE_STATUSES))
    triage.add_argument("--note", default=None)
    triage.set_defaults(func=cmd_review_triage)

    fix_brief = review_sub.add_parser("fix-brief", help="emit the accepted-findings brief for the fixer")
    fix_brief.add_argument("--output", default=None)
    fix_brief.set_defaults(func=cmd_review_fix_brief)

    status = review_sub.add_parser("status", help="report whether a re-review is warranted")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_review_status)

    for parser_with_scope in (review_run, consolidate, review_show, triage, fix_brief, status):
        _add_design_flag(parser_with_scope)

    # design -----------------------------------------------------------------
    design_parser = subparsers.add_parser("design", help="the user's approval of the plan")
    design_sub = design_parser.add_subparsers(dest="subcommand", required=True)
    design_approve = design_sub.add_parser(
        "approve", help="record the user's approval of the plan as it is now (only after their yes)"
    )
    design_approve.add_argument("--json", action="store_true")
    design_approve.set_defaults(func=cmd_design_approve)

    # state ------------------------------------------------------------------
    state_parser = subparsers.add_parser("state", help="inspect or append run state")
    state_sub = state_parser.add_subparsers(dest="subcommand", required=True)
    state_show = state_sub.add_parser("show")
    state_show.add_argument("--json", action="store_true")
    state_show.set_defaults(func=cmd_state_show)
    state_record = state_sub.add_parser("record")
    state_record.add_argument("stage")
    state_record.add_argument("status")
    state_record.add_argument("--detail", nargs="*", default=None, help="key=value pairs")
    state_record.set_defaults(func=cmd_state_record)

    jobs_parser = subparsers.add_parser("jobs", help="detached runs, so no call blocks forever")
    jobs_sub = jobs_parser.add_subparsers(dest="subcommand", required=True)
    jobs_list = jobs_sub.add_parser("list", help="every recorded job, newest first")
    jobs_list.add_argument("--json", action="store_true")
    jobs_list.set_defaults(func=cmd_jobs_list)
    jobs_show = jobs_sub.add_parser("show", help="one job")
    jobs_show.add_argument("job_id")
    jobs_show.add_argument("--output", action="store_true", help="also print its output")
    jobs_show.add_argument("--json", action="store_true")
    jobs_show.set_defaults(func=cmd_jobs_show)
    jobs_wait = jobs_sub.add_parser("wait", help="wait for a job, with a deadline of your own")
    jobs_wait.add_argument("job_id")
    jobs_wait.add_argument("--timeout", type=float, default=60.0)
    jobs_wait.add_argument("--poll", type=float, default=1.0)
    jobs_wait.add_argument("--json", action="store_true")
    jobs_wait.set_defaults(func=cmd_jobs_wait)
    jobs_cancel = jobs_sub.add_parser("cancel", help="stop a running job")
    jobs_cancel.add_argument("job_id")
    jobs_cancel.set_defaults(func=cmd_jobs_cancel)

    budget_parser = subparsers.add_parser("budget", help="attempt budgets that stop runaway loops")
    budget_sub = budget_parser.add_subparsers(dest="subcommand", required=True)
    budget_show = budget_sub.add_parser("show", help="what has been spent")
    budget_show.add_argument("--json", action="store_true")
    budget_show.set_defaults(func=cmd_budget_show)
    budget_consume = budget_sub.add_parser("consume", help="claim an attempt at a stage")
    budget_consume.add_argument("stage", help="a stage the orchestrator runs itself, e.g. test")
    budget_consume.add_argument("--force", action="store_true")
    budget_consume.set_defaults(func=cmd_budget_consume)
    budget_reset = budget_sub.add_parser("reset", help="start the budgets again, keeping the token account")
    budget_reset.set_defaults(func=cmd_budget_reset)

    # Separate from `budget` on purpose: attempts are enforced, tokens are only
    # counted, and putting them under one command invites reading one as the
    # other.
    tokens_parser = subparsers.add_parser("tokens", help="what the workflow has spent, per stage")
    tokens_sub = tokens_parser.add_subparsers(dest="subcommand", required=True)
    tokens_show = tokens_sub.add_parser("show", help="the token account (reported, never enforced)")
    tokens_show.add_argument("--json", action="store_true")
    tokens_show.set_defaults(func=cmd_tokens_show)

    optimization_parser = subparsers.add_parser(
        "optimization", help="what optimization.level has decided, over time"
    )
    optimization_sub = optimization_parser.add_subparsers(dest="optimization_command", required=True)
    optimization_report = optimization_sub.add_parser(
        "report", help="rounds refused, panels cut, and what that came to"
    )
    optimization_report.add_argument("--json", action="store_true")
    optimization_report.set_defaults(func=cmd_optimization_report)

    progress_parser = subparsers.add_parser("progress", help="detect a loop that is going nowhere")
    progress_sub = progress_parser.add_subparsers(dest="subcommand", required=True)
    progress_record = progress_sub.add_parser("record", help="record a stage outcome signature")
    progress_record.add_argument("stage")
    progress_record.add_argument("--signature", required=True, help="e.g. the failing test summary")
    progress_record.add_argument("--json", action="store_true")
    progress_record.set_defaults(func=cmd_progress_record)

    workflow_parser = subparsers.add_parser(
        "workflow", help="the workflows in this project and which one is yours"
    )
    workflow_sub = workflow_parser.add_subparsers(dest="subcommand", required=True)
    workflow_list = workflow_sub.add_parser("list", help="every workflow here, most recent first")
    workflow_list.add_argument("--json", action="store_true")
    workflow_list.set_defaults(func=cmd_workflow_list)
    workflow_show = workflow_sub.add_parser("show", help="which workflow this command is in, and why")
    workflow_show.add_argument("--json", action="store_true")
    workflow_show.set_defaults(func=cmd_workflow_show)
    workflow_use = workflow_sub.add_parser("use", help="remember an id for this directory")
    workflow_use.add_argument("id")
    workflow_use.set_defaults(func=cmd_workflow_use)
    workflow_remove = workflow_sub.add_parser("remove", help="delete one finished workflow's artifacts")
    workflow_remove.add_argument("id")
    workflow_remove.add_argument("--yes", action="store_true", help="do not ask")
    workflow_remove.set_defaults(func=cmd_workflow_remove)

    status_parser = subparsers.add_parser(
        "status", help="continue or stop: budgets, stalls and open findings in one verdict"
    )
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=cmd_status)

    summary = subparsers.add_parser("summary", help="print the end-of-run summary")
    summary.add_argument("--json", action="store_true")
    summary.set_defaults(func=cmd_summary)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    tolerate_console_encoding()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.cwd:
        # Resolve before chdir and keep the absolute form: commands also use
        # args.cwd as a search-start path, and a relative value would otherwise
        # be applied a second time against the directory we just moved into.
        args.cwd = os.path.abspath(args.cwd)
        os.chdir(args.cwd)
    try:
        return int(args.func(args) or 0)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    except review_mod.ReviewError as exc:
        _err(str(exc))
        return 2
    except workflow_mod.WorkflowError as exc:
        _err(str(exc))
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        _err("interrupted")
        return 130
