"""The config, model, reviewer and doctor commands."""

from __future__ import annotations

import argparse
import copy
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from . import config as config_mod
from . import doctor as doctor_mod
from . import miniyaml
from . import optimization as opt_mod
from . import presets as presets_mod
from . import suggest as suggest_mod
from . import wizard as wizard_mod
from .cli_common import (
    _compose_preview,
    _emit_json,
    _err,
    _fitted_base,
    _global_file,
    _layer_path,
    _out,
    _panel_write_problems,
    _prune_base,
    _read_layer,
    _resolve_scope,
    _seed_list,
    _seed_panel,
)
from .providers import (
    ModelResolutionError,
    UnknownProviderError,
    adapter_failure,
    available_providers,
    describe_exception,
    describe_origin,
    get_provider,
    origin_payload,
    provider_origin,
    redact,
)
from .summary import render_summary

# --------------------------------------------------------------------------- config


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
    path = ""
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
    preset = {"name": loaded.preset, "source": loaded.preset_source, "notes": loaded.preset_notes}
    if args.json:
        providers_payload = {name: origin_payload(name) for name in referenced}
        payload: Dict[str, Any] = {
            "source": source,
            "config": data,
            "providers": providers_payload,
            "preset": preset,
        }
        if not scoped:
            payload["reviewer_origins"] = [origin.label() for origin in loaded.reviewer_origins]
        _emit_json(payload)
        return 0
    _out("Source: %s" % source)
    if not scoped:
        installed = presets_mod.installed_providers()
        _out("Preset: %s" % presets_mod.describe(loaded.preset, loaded.preset_source, installed))
        for note in loaded.preset_notes:
            _out("  note: %s" % note)
    if loaded.used_defaults and not scoped:
        _out("No config file found yet -- showing preset %s fitted to the installed CLIs." % loaded.preset)
    _out("")
    if scoped:
        _out(_render_layer(path, data, exists))
    else:
        summary = render_summary(data, loaded.reviewer_origins)
        _out(summary if "orchestrator" in data else "(empty layer)")
    if referenced:
        _out("Providers: %s" % ", ".join(_describe_referenced_provider(name) for name in referenced))
    problems = config_mod.validate(
        loaded.data,
        project_layer=loaded.project_layer,
        global_layer=loaded.global_layer,
        origins=loaded.reviewer_origins,
    )
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

    if args.preset:
        # The file names the preset and keeps what it held beside the keys a
        # preset governs; the values are fitted to this machine at load time.
        data = presets_mod.with_preset(existing, args.preset)
        save = True
    elif args.defaults:
        # The recommended configuration *is* the built-in defaults, so the
        # honest way to record a choice of it is to override nothing. Writing
        # the values out would freeze today's copy of them into the file and
        # shadow every later improvement -- the whole of what this file is for.
        data = {"version": config_mod.CONFIG_VERSION}
        save = True
    else:
        if not sys.stdin.isatty() and not args.force:
            _err(
                "config setup needs an interactive terminal; use --preset quality|standard|fast, "
                "or --defaults for the recommended setup."
            )
            return 2
        try:
            data, save = wizard_mod.run(
                wizard_mod.Prompter(), existing, _fitted_base(scope, existing), scope=scope
            )
        except EOFError:
            _err("input ended before setup finished; nothing was saved. Try --defaults instead.")
            return 2

    if not save:
        _out("Not saved.")
        return 1
    # The layer on its own names no roles at all, so it is the configuration it
    # resolves to that has to be valid -- the same dict the wizard summarised.
    preview, fit, _preset, _source = _compose_preview(scope, data)
    problems = config_mod.validate(
        preview,
        project_layer=data if scope == "project" else None,
        global_layer=data if scope == "global" else _global_file(),
        origins=fit.origins,
    )
    if problems:
        # Writing this would leave every workflow command failing with
        # "invalid configuration" straight after a successful-looking setup.
        _err("not saved -- the configuration is invalid:")
        for problem in problems:
            _err("  - %s" % problem)
        return 2
    config_mod.write_config_file(path, data, scope)
    if args.preset:
        _out(render_summary(preview, fit.origins))
        notes = presets_mod.render_notes(fit)
        if notes:
            _out(notes)
    _out("Saved %s configuration to %s" % (scope, path))
    _out(
        "It records only what you chose; everything else follows %s (config show)."
        % config_mod.layer_below(scope)
    )
    return 0


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
    # The global file keeps the preset it names: that is the choice the reset
    # returns to, and the overrides are what it clears. An unknown name is one
    # of those overrides, or the reset would leave the file as invalid as before.
    cleared: Dict[str, Any] = {"version": config_mod.CONFIG_VERSION}
    unknown = None
    previous: Dict[str, Any] = {}
    if os.path.isfile(path):
        try:
            previous = config_mod.read_config_file(path)
        except config_mod.ConfigError:
            pass  # a file that does not parse is what a reset is for
    if scope == "global":
        preset = previous.get("preset")
        if isinstance(preset, str) and preset in presets_mod.PRESETS:
            cleared["preset"] = preset
        elif preset is not None:
            unknown = preset
    config_mod.write_config_file(path, cleared, scope)
    if unknown is not None:
        _out("note: dropped the unknown preset %r; the global file now names none" % (unknown,))
    extras = previous.get("reviewers_extra")
    if isinstance(extras, list) and extras:
        _out("removed %d extra reviewer(s) (reviewers_extra)" % len(extras))
    if scope == "global":
        name = cleared.get("preset") or presets_mod.DEFAULT
        following = "every value now follows preset %s and the built-in defaults" % name
    else:
        following = "this project now follows the global layer"
    _out("Reset %s configuration: overrides cleared, %s (%s)" % (scope, following, path))
    try:
        loaded = config_mod.load(args.cwd, validate_result=False)
    except config_mod.ConfigError as exc:
        _err(str(exc))  # the other layer does not parse; the reset itself is done
        return 0
    _out(render_summary(loaded.data, loaded.reviewer_origins))
    for note in loaded.preset_notes:
        _out("note: %s" % note)
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    role_key = args.path.split(".")[0].split("[")[0]
    if role_key == "preset":
        # Only the global file can name one, so that is where it goes unless a
        # scope says otherwise -- and a project scope is refused, not written.
        if args.scope == "project":
            _err("preset: only the global file can name a preset for now (use --scope global)")
            return 2
        scope = "global"
    else:
        scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    before = config_mod.load(args.cwd, validate_result=False) if role_key in config_mod.KNOWN_ROLES else None
    # A panel path is checked as the panel it leaves, before anything is written.
    panel_before = copy.deepcopy(layer) if role_key in ("reviewers", "reviewers_extra") else None
    frozen = left_out = None
    if "[" in args.path:
        list_path = args.path.split("[", 1)[0]
        base = _fitted_base(scope, layer)
        if list_path == "reviewers":
            frozen, left_out = _seed_panel(scope, layer, base, path, args.cwd)
        else:
            _seed_list(layer, list_path, base)
    value = args.value if args.raw else config_mod.coerce_scalar(args.value)
    if scope == "project":
        # From the arguments alone, before anything is written: the same
        # refusal a run of that seat would meet.
        refusal = _project_seat_write_refusal(args.path, value, layer, path)
        if refusal:
            _err(refusal)
            return 2
    renamed = re.fullmatch(r"reviewers_extra\[(\d+)\]\.id", args.path)
    if renamed:
        # As `reviewer set --id` refuses it: the extra would run under another name.
        preview, fit, _preset, _source = _compose_preview(scope, layer)
        own = config_mod.ReviewerOrigin(scope, "reviewers_extra", int(renamed.group(1)))
        index = next((i for i, origin in enumerate(fit.origins) if origin == own), None)
        if index is not None and _id_taken(preview, index, value):
            _err("reviewer id %r already exists" % (value,))
            return 2
    try:
        config_mod.set_path(layer, args.path, value)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        if left_out:
            _err(left_out)
        return 2
    except IndexError:
        # `set_path` assigns straight into the list, so an index past its end
        # arrives as a bare IndexError and `main` catches only the config
        # errors. This is the one entry point that takes an index at all, so it
        # is the one that owes the user a message instead of a traceback.
        _err("%s: index out of range" % args.path)
        if left_out:
            _err(left_out)
        return 2
    if panel_before is not None:
        introduced = _panel_write_problems(scope, panel_before, layer)
        if introduced:
            for problem in introduced:
                _err(problem)
            return 2
    config_mod.write_config_file(path, layer, scope)
    _out("%s = %r  (%s: %s)" % (args.path, value, scope, path))
    if frozen:
        _out(frozen)
    reloaded = config_mod.load(args.cwd, validate_result=False)
    effective = reloaded.data
    if before is not None:
        _left_the_fit_note(before, reloaded, role_key, path)
    problems = config_mod.validate(
        effective,
        project_layer=reloaded.project_layer,
        global_layer=reloaded.global_layer,
        origins=reloaded.reviewer_origins,
    )
    for problem in problems:
        _err("warning: %s" % problem)
    for warning in config_mod.read_only_arg_warnings(reloaded):
        _err("warning: %s" % warning)
    _warn_unenforced(reloaded)
    _warn_unresolvable(effective, args.path.split(".")[0])
    return 0


#: The keys that name a read-only seat's provider.
_SEAT_PROVIDER_PATHS = (
    re.compile(r"^(?P<role>orchestrator|architect)\.provider$"),
    re.compile(r"^(?P<role>orchestrator|architect)\.model_tiers\.(?P<tier>.+)\.provider$"),
    re.compile(r"^reviewers\[(?P<index>\d+)\]\.provider$"),
    re.compile(r"^reviewers_extra\[(?P<index>\d+)\]\.provider$"),
)


def _project_seat_write_refusal(dotted: str, value: Any, layer: Dict[str, Any], path: str) -> str:
    """Why ``dotted = value`` is not written to the project file, or ""."""
    provider = str(value) if isinstance(value, str) else ""
    if not provider or not config_mod.warned_provider(provider):
        return ""
    name = os.path.basename(path)
    for pattern in _SEAT_PROVIDER_PATHS:
        match = pattern.match(dotted)
        if not match:
            continue
        found = match.groupdict()
        if found.get("index") is not None:
            index = int(found["index"])
            reviewers = layer.get(dotted.split("[", 1)[0])
            entry = reviewers[index] if isinstance(reviewers, list) and index < len(reviewers) else {}
            reviewer_id = entry.get("id") if isinstance(entry, dict) else None
            display = "reviewer %s" % (reviewer_id or index + 1)
            return config_mod.project_reviewer_refusal(display, provider, name)
        role, tier = found["role"], found.get("tier")
        display = "%s (tier %s)" % (role, tier) if tier else role
        return config_mod.project_seat_refusal(display, provider, name, dotted)
    return ""


def _warn_unenforced(loaded: config_mod.LoadedConfig) -> None:
    """One ``warning:`` line per read-only seat that cannot be held to reading."""
    refused = list(config_mod.project_raw_arg_refusals(loaded))
    for line in config_mod.read_only_enforcement_warnings(loaded.data, refused):
        _err("warning: %s" % line)


def _left_the_fit_note(
    before: config_mod.LoadedConfig, after: config_mod.LoadedConfig, role: str, path: str
) -> None:
    """A role edit that took the role out of the preset's fit and moved its provider.

    A role any file sets is not fitted at all, so the first edit to one can
    move it back to the default provider -- which is worth a line.
    """
    layers = (before.global_layer, before.project_layer)
    if not after.preset or any(config_mod.mentions(layer, role) for layer in layers):
        return
    old = (before.data.get(role) or {}).get("provider")
    new = (after.data.get(role) or {}).get("provider")
    if old == new:
        return
    message = "note: %s is now set by %s (provider %s); preset %s no longer fits it"
    _out(message % (role, path, new, after.preset))


def cmd_config_prune(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path = _layer_path(scope, args.cwd)
    if not os.path.isfile(path):
        _err("no %s configuration file at %s" % (scope, path))
        return 2
    layer = config_mod.read_config_file(path)
    pruned, dropped = config_mod.prune_layer(layer, _prune_base(scope, args.cwd, layer))
    if _compose_preview(scope, pruned)[0] != _compose_preview(scope, layer)[0]:
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
    _out("Dropped %d from %s; they now follow %s." % (len(dropped), path, config_mod.layer_below(scope)))
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
    problems = config_mod.validate(
        loaded.data,
        project_layer=loaded.project_layer,
        global_layer=loaded.global_layer,
        origins=loaded.reviewer_origins,
    )
    # Warnings, not problems: they refuse one role's runs, not the file.
    warnings = config_mod.read_only_arg_warnings(loaded)
    refused = list(config_mod.project_raw_arg_refusals(loaded))
    warnings += config_mod.read_only_enforcement_warnings(loaded.data, refused)
    if args.json:
        _emit_json({"valid": not problems, "problems": problems, "warnings": warnings})
    else:
        if problems:
            _out("Invalid configuration:")
            for problem in problems:
                _out("  - %s" % problem)
        else:
            _out("Configuration is valid.")
        if warnings:
            _out("Warnings:")
            for warning in warnings:
                _out("  - %s" % warning)
    return 1 if problems else 0


def _preview_inputs(preview: Any, notes: List[str]) -> Tuple[List[str], str, Optional[List[Any]]]:
    """``(exclude, workspace dir, panel)`` read from an unvalidated preview.

    Each falls back to its default with a note when it is not the type it
    should be; the panel to None, which a write refuses.
    """
    review = preview.get("review") if isinstance(preview, dict) else None
    exclude = review.get("exclude") if isinstance(review, dict) else None
    if not (isinstance(exclude, list) and all(isinstance(p, str) for p in exclude)):
        notes.append("review.exclude is not a list of strings; the default list was used")
        exclude = list(config_mod.default_config()["review"]["exclude"])
    workspace = preview.get("workspace") if isinstance(preview, dict) else None
    directory = workspace.get("dir") if isinstance(workspace, dict) else None
    if not (isinstance(directory, str) and directory):
        notes.append("workspace.dir is not a non-empty string; .ai was used")
        directory = ".ai"
    panel = preview.get("reviewers") if isinstance(preview, dict) else None
    if not isinstance(panel, list):
        notes.append("reviewers is not a list; the panel was treated as empty")
        return exclude, directory, None
    return exclude, directory, panel


def cmd_config_suggest_roles(args: argparse.Namespace) -> int:
    cwd = os.path.abspath(args.cwd or os.getcwd())
    root, in_git = suggest_mod.find_listing_root(cwd)
    # The file `load()` uses from here, which is the one a write must change.
    found = config_mod.find_project_config(cwd)
    target = found or os.path.join(root, config_mod.PROJECT_CONFIG_NAMES[0])
    layer = config_mod.read_config_file(found) if found else {}
    layer.setdefault("version", config_mod.CONFIG_VERSION)
    outside = found is not None and os.path.normcase(os.path.dirname(found)) != os.path.normcase(root)

    # Every refusal comes before git runs.
    if args.provider and config_mod.warned_provider(args.provider):
        display = "suggested reviewers"
        _err(config_mod.project_reviewer_refusal(display, args.provider, os.path.basename(target)))
        return 2
    installed = presets_mod.installed_providers()
    provider = args.provider or presets_mod.suggestion_provider(installed)
    no_provider = "no CLI a project reviewer can run on is installed (claude, codex); --provider names one"
    if args.write:
        if outside:
            _err(
                "the project file in force here is %s, but the files are listed from %s; run where the "
                "project file sits at the listing root, or move it there" % (found, root)
            )
            return 2
        extras = layer.get("reviewers_extra")
        if extras is not None and not isinstance(extras, list):
            _err("%s: reviewers_extra: must be a list; nothing was written" % target)
            return 2
        if provider is None:
            _err(no_provider)
            return 2
    notes: List[str] = []
    preview, fit, preset, _source = _compose_preview("project", layer)
    exclude, workspace_dir, panel = _preview_inputs(preview, notes)
    if panel is None and args.write:
        _err("reviewers: the panel in force is not a list; nothing was written")
        return 2

    try:
        if in_git:
            paths, walked_short = suggest_mod.git_paths(root), False
        else:
            paths, walked_short = suggest_mod.walk_paths(root)
    except suggest_mod.ListingError as exc:
        _err(str(exc))
        return 2
    source = "git" if in_git else "walk"
    listing = suggest_mod.build_listing(root, source, paths, exclude, workspace_dir, walked_short, notes)
    deps, note = suggest_mod.read_package_deps(root, listing)
    if note:
        listing.notes.append(note)
    if provider is None:
        listing.notes.append(no_provider)
        provider = presets_mod.CLAUDE  # for display only; a write was refused above
    family = args.model or presets_mod.cheap_family(provider)
    panel = panel or []
    labels = [origin.label() for origin in fit.origins]
    suggestions, skipped = suggest_mod.suggest(listing, panel, deps, labels)
    # An id this file's extras write is taken too, even when it runs as another.
    own_extras = layer.get("reviewers_extra")
    own: List[Any] = own_extras if isinstance(own_extras, list) else []
    reviewers = suggest_mod.reviewers_for(suggestions, [*panel, *own], provider, family)
    result = suggest_mod.Result(listing, suggestions, reviewers, skipped)

    written = None
    if args.write and reviewers:
        before = copy.deepcopy(layer)
        try:
            for reviewer in reviewers:
                config_mod.add_extra_reviewer(layer, reviewer)
        except config_mod.ConfigError as exc:
            _err(str(exc))
            return 2
        problems = _panel_write_problems("project", before, layer)
        if problems:
            for problem in problems:
                _err(problem)
            return 2
        config_mod.write_config_file(target, layer, "project")
        written = target

    if args.json:
        for line in listing.notes:
            _err("note: %s" % line)
        _emit_json(suggest_mod.payload(result, written))
    else:
        _out(suggest_mod.render(result))
        ids = ", ".join(reviewer["id"] for reviewer in reviewers)
        if not reviewers:
            _out("Nothing to add.")
        elif written and layer.get("reviewers") is not None:
            _out("Added %s to %s as extras, after the reviewers it lists" % (ids, target))
        elif written:
            _out(
                "Added %s to %s as extras; the panel still follows %s"
                % (ids, target, _inherited_panel("project", preset))
            )
        else:
            message = "Nothing was written; --write adds them to %s's reviewers_extra" % target
            if outside:
                message += " (refused from here: %s is not at %s)" % (found, root)
            _out(message)
    if written:
        _warn_unenforced_after_write(cwd)
    return 0


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
            "families": [],
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
                entry["families"] = [
                    {"family": family, "resolves_to": target} for family, target in provider.config_families()
                ]
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
        if entry.get("families"):
            _out("  families to put in a config (each follows the newest listed id):")
            for item in entry["families"]:
                _out("    family=%-26s now %s" % (item["family"], item["resolves_to"]))
    return status


# --------------------------------------------------------------------------- reviewers


def _redacted_reviewer(reviewer: Any) -> Any:
    """A copy of ``reviewer`` with its ``when.paths`` patterns redacted.

    The text listing redacts them through ``condition_label``; the JSON one
    prints the entry itself, so it is redacted here instead.
    """
    if not isinstance(reviewer, dict):
        return reviewer
    shown = copy.deepcopy(reviewer)
    when = shown.get("when")
    if isinstance(when, dict) and isinstance(when.get("paths"), list):
        when["paths"] = [redact(p) if isinstance(p, str) else p for p in when["paths"]]
    return shown


def cmd_reviewer_list(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    reviewers = loaded.reviewers()
    if args.json:
        listed: List[Any] = []
        for index, reviewer in enumerate(reviewers):
            shown = _redacted_reviewer(reviewer)
            if isinstance(shown, dict):
                shown["origin"] = loaded.reviewer_origin(index).label()
            listed.append(shown)
        _emit_json(listed)
        return 0
    if not reviewers:
        _out("No reviewers configured.")
        return 0
    for index, reviewer in enumerate(reviewers, 1):
        model = reviewer.get("model") or {}
        when = opt_mod.condition_label(reviewer)
        origin = loaded.reviewer_origin(index - 1)
        _out(
            "%d. %-18s %-8s %-18s %-8s %s%s%s"
            % (
                index,
                reviewer.get("id"),
                reviewer.get("provider"),
                model.get("family", "default"),
                model.get("version", "latest"),
                reviewer.get("role", "general"),
                "" if when == opt_mod.WHEN_ALWAYS else " (when: %s)" % when,
                " (extra: %s)" % origin.layer if origin.key == "reviewers_extra" else "",
            )
        )
    return 0


def _both_conditions(args: argparse.Namespace) -> bool:
    """``--when`` and ``--when-paths`` together, which name two conditions for one reviewer."""
    if args.when and args.when_paths:
        _err("give --when or --when-paths, not both")
        return True
    return False


def cmd_reviewer_add(args: argparse.Namespace) -> int:
    if _both_conditions(args):
        return 2
    scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    before = copy.deepcopy(layer)
    preview, _fit, preset, _source = _compose_preview(scope, layer)
    role = args.role or "general"
    reviewer_id = args.id or config_mod.suggest_reviewer_id(preview, args.provider, role)
    if scope == "project" and config_mod.warned_provider(args.provider):
        # Nothing written: the same refusal a run of this reviewer would meet.
        name = os.path.basename(path)
        _err(config_mod.project_reviewer_refusal("reviewer %s" % reviewer_id, args.provider, name))
        return 2
    panel = preview.get("reviewers")
    if args.id and any(isinstance(r, dict) and r.get("id") == args.id for r in panel or []):
        # Any id of the panel in force: an extra never takes one, it is renamed.
        _err("reviewer id %r already exists" % args.id)
        return 2
    family = args.model
    if family is None:
        family = config_mod.default_reviewer_family(args.provider)
    reviewer = config_mod.make_reviewer(
        reviewer_id,
        args.provider,
        family,
        role,
        version="pinned" if args.pin else "latest",
        model_id=args.pin,
        when=args.when,
        paths=args.when_paths,
    )
    # A file that lists the panel owns it, so the reviewer joins that list;
    # otherwise it goes beside the panel the file inherits, which keeps
    # following the fit or the global file's list.
    listed = layer.get("reviewers") is not None
    try:
        if listed:
            config_mod.add_reviewer(layer, reviewer)
        else:
            config_mod.add_extra_reviewer(layer, reviewer)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    blocking = _panel_write_problems(scope, before, layer)
    if blocking:
        for problem in blocking:
            _err(problem)
        return 2
    config_mod.write_config_file(path, layer, scope)
    added = "Added reviewer %s (%s / %s / %s) to %s" % (reviewer_id, args.provider, family, role, path)
    if listed:
        _out(added)
    else:
        _out("%s as an extra; the panel still follows %s" % (added, _inherited_panel(scope, preset)))
    _warn_unenforced_after_write(args.cwd)
    return 0


def _inherited_panel(scope: str, preset: Any) -> str:
    """What a file that lists no reviewers takes its panel from, the way a message says it."""
    if scope == "project" and _global_file().get("reviewers") is not None:
        return "the global file's reviewers"
    return "preset %s's fit" % preset if preset else "the built-in defaults"


def _own_extra(fit: Any, index: int, scope: str) -> Any:
    """The origin of reviewer ``index`` when it is an extra of the file being edited, else None."""
    origins = fit.origins
    origin = origins[index] if index < len(origins) else None
    if origin is not None and origin.key == "reviewers_extra" and origin.layer == scope:
        return origin
    return None


def _renamed_own_extra(
    layer: Dict[str, Any], preview: Dict[str, Any], fit: Any, scope: str, selector: str
) -> Optional[Tuple[int, str]]:
    """``(j, id)`` when ``reviewers_extra[j]`` of this file is written as ``selector``
    but runs under another id, since a seat in force took it; else None.

    The selector would otherwise find that seat, and editing it would seed
    the panel and change or drop a reviewer nobody named.
    """
    extras = layer.get("reviewers_extra")
    if not isinstance(extras, list):
        return None
    panel = preview.get("reviewers") or []
    for position, extra in enumerate(extras):
        if not isinstance(extra, dict) or extra.get("id") != selector:
            continue
        origin = config_mod.ReviewerOrigin(scope, "reviewers_extra", position)
        for index, seat_origin in enumerate(fit.origins):
            if seat_origin != origin or index >= len(panel) or not isinstance(panel[index], dict):
                continue
            runs_as = panel[index].get("id")
            if isinstance(runs_as, str) and runs_as != selector:
                return position, runs_as
    return None


def _refuse_renamed_own_extra(
    layer: Dict[str, Any], preview: Dict[str, Any], fit: Any, scope: str, selector: str
) -> bool:
    """Print the refusal ``_renamed_own_extra`` calls for; True when it did."""
    renamed = _renamed_own_extra(layer, preview, fit, scope, selector)
    if renamed is None:
        return False
    position, runs_as = renamed
    message = (
        "reviewer %s: reviewers_extra[%d] in the %s file runs as %s, since %s is taken; use %s "
        "(select the other seat by its position in reviewer list)"
    )
    _err(message % (selector, position, scope, runs_as, selector, runs_as))
    return True


def _id_taken(preview: Dict[str, Any], index: Optional[int], reviewer_id: Any) -> bool:
    """Whether a reviewer other than ``index`` of the panel in force holds ``reviewer_id``.

    The check ``reviewer add`` makes, for a write that renames a seat: an
    extra never takes an id, it is renamed, so the name written would not be
    the one it runs under.
    """
    return any(
        position != index and isinstance(reviewer, dict) and reviewer.get("id") == reviewer_id
        for position, reviewer in enumerate(preview.get("reviewers") or [])
    )


def _seat_selector(reviewer: Any, selector: str) -> str:
    """How a seat resolved over the panel in force is found again in the seeded list."""
    reviewer_id = reviewer.get("id") if isinstance(reviewer, dict) else None
    return reviewer_id if isinstance(reviewer_id, str) and reviewer_id else selector


def _warn_unenforced_after_write(cwd: Any) -> None:
    """The enforcement warnings of the configuration a write left in force."""
    try:
        loaded = config_mod.load(cwd, validate_result=False)
    except config_mod.ConfigError:
        return  # the other layer does not parse; the write itself is done
    # The refusals too, as `config set` prints them: a project panel copied
    # from the global one can hold a seat the project file may not set.
    for warning in config_mod.read_only_arg_warnings(loaded):
        _err("warning: %s" % warning)
    _warn_unenforced(loaded)


def cmd_reviewer_remove(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    before = copy.deepcopy(layer)
    # Resolved over the panel in force with this file, which in project scope
    # is what `reviewer list` shows, so its positions are the list's.
    preview, fit, _preset, _source = _compose_preview(scope, layer)
    if _refuse_renamed_own_extra(layer, preview, fit, scope, args.selector):
        return 2
    try:
        index, found = config_mod.find_reviewer(preview, args.selector)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    frozen = None
    origin = _own_extra(fit, index, scope)
    if origin is not None:
        # The file's own extra goes in place, and the panel stays inherited.
        removed = layer["reviewers_extra"].pop(origin.position)
    else:
        base = _fitted_base(scope, layer)
        frozen, left_out = _seed_panel(scope, layer, base, path, args.cwd)
        try:
            _, removed = config_mod.remove_reviewer(layer, _seat_selector(found, args.selector))
        except config_mod.ConfigError as exc:
            _err(str(exc))
            if left_out:
                _err(left_out)
            return 2
    # The same check add and set make: removing the last reviewer that always
    # runs would leave a panel that a quiet round could not be reviewed by.
    # Only what the removal itself introduced is refused, so removing a broken
    # reviewer stays a way out of a panel that has another one.
    problems = _panel_write_problems(scope, before, layer, removed=origin)
    if problems:
        for problem in problems:
            _err(problem)
        return 2
    config_mod.write_config_file(path, layer, scope)
    _out("Removed reviewer %s from %s" % (removed.get("id"), path))
    if frozen:
        _out(frozen)
    _warn_unenforced_after_write(args.cwd)
    return 0


def cmd_reviewer_set(args: argparse.Namespace) -> int:
    if _both_conditions(args):
        return 2
    scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    before = copy.deepcopy(layer)
    preview, fit, _preset, _source = _compose_preview(scope, layer)
    if _refuse_renamed_own_extra(layer, preview, fit, scope, args.selector):
        return 2
    try:
        index, found = config_mod.find_reviewer(preview, args.selector)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    # Before either edit path: an extra of the project file is held to it too.
    if args.provider and scope == "project" and config_mod.warned_provider(args.provider):
        display = "reviewer %s" % (args.id or found.get("id") or index + 1)
        _err(config_mod.project_reviewer_refusal(display, args.provider, os.path.basename(path)))
        return 2
    if args.id and _id_taken(preview, index, args.id):
        _err("reviewer id %r already exists" % args.id)
        return 2
    frozen = None
    origin = _own_extra(fit, index, scope)
    if origin is not None:
        # The entry as the file holds it: a renamed extra keeps its written id.
        reviewer = layer["reviewers_extra"][origin.position]
    else:
        base = _fitted_base(scope, layer)
        frozen, left_out = _seed_panel(scope, layer, base, path, args.cwd)
        try:
            _, reviewer = config_mod.find_reviewer(layer, _seat_selector(found, args.selector))
        except config_mod.ConfigError as exc:
            _err(str(exc))
            if left_out:
                _err(left_out)
            return 2
    family_note = ""
    if args.provider:
        previous = reviewer.get("provider")
        reviewer["provider"] = args.provider
        if args.provider != previous and not (args.model or args.pin):
            # A family is the old CLI's word for a model; the new one would
            # not resolve it, so it gets its own default instead.
            old_family = (reviewer.get("model") or {}).get("family")
            new_family = config_mod.default_reviewer_family(args.provider)
            reviewer["model"] = {"family": new_family, "version": "latest"}
            message = "note: model family reset from %r to %r for provider %s (--model picks another)"
            family_note = message % (old_family, new_family, args.provider)
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
    # Each replaces the condition whole, as --model replaces the model block:
    # a list of patterns is never merged into the one already there.
    if args.when_paths:
        reviewer["when"] = {"paths": list(args.when_paths)}
    elif args.when == opt_mod.WHEN_ALWAYS:
        reviewer.pop("when", None)
    elif args.when:
        reviewer["when"] = args.when
    problems = _panel_write_problems(scope, before, layer)
    if problems:
        for problem in problems:
            _err(problem)
        return 2
    config_mod.write_config_file(path, layer, scope)
    _out("Updated reviewer %s in %s" % (reviewer.get("id"), path))
    if family_note:
        _out(family_note)
    if frozen:
        _out(frozen)
    _warn_unenforced_after_write(args.cwd)
    return 0


# --------------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor_mod.collect(args.cwd, probe_models=not args.fast)
    if args.json:
        _emit_json(report)
    else:
        _out(doctor_mod.render(report))
    return doctor_mod.exit_code(report) if args.strict else 0
