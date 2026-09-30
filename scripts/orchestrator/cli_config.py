"""The config, model, reviewer and doctor commands."""

from __future__ import annotations

import argparse
import copy
import os
import re
import sys
from typing import Any, Dict, List

from . import config as config_mod
from . import doctor as doctor_mod
from . import miniyaml
from . import optimization as opt_mod
from . import presets as presets_mod
from . import wizard as wizard_mod
from .cli_common import (
    _compose_preview,
    _emit_json,
    _err,
    _fitted_base,
    _frozen_panel_note,
    _layer_path,
    _out,
    _prune_base,
    _read_layer,
    _resolve_scope,
    _seed_list,
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
        _emit_json({"source": source, "config": data, "providers": providers_payload, "preset": preset})
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
        _out(wizard_mod.render_summary(data) if "orchestrator" in data else "(empty layer)")
    if referenced:
        _out("Providers: %s" % ", ".join(_describe_referenced_provider(name) for name in referenced))
    problems = config_mod.validate(loaded.data, project_layer=loaded.project_layer)
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
                wizard_mod.Prompter(), existing, _fitted_base(scope, args.cwd, existing), scope=scope
            )
        except EOFError:
            _err("input ended before setup finished; nothing was saved. Try --defaults instead.")
            return 2

    if not save:
        _out("Not saved.")
        return 1
    # The layer on its own names no roles at all, so it is the configuration it
    # resolves to that has to be valid -- the same dict the wizard summarised.
    preview, fit, _preset, _source = _compose_preview(scope, data, args.cwd)
    problems = config_mod.validate(preview, project_layer=data if scope == "project" else None)
    if problems:
        # Writing this would leave every workflow command failing with
        # "invalid configuration" straight after a successful-looking setup.
        _err("not saved -- the configuration is invalid:")
        for problem in problems:
            _err("  - %s" % problem)
        return 2
    config_mod.write_config_file(path, data, scope)
    if args.preset:
        _out(wizard_mod.render_summary(preview))
        notes = presets_mod.render_notes(fit)
        if notes:
            _out(notes)
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
    # The global file keeps the preset it names: that is the choice the reset
    # returns to, and the overrides are what it clears. An unknown name is one
    # of those overrides, or the reset would leave the file as invalid as before.
    cleared: Dict[str, Any] = {"version": config_mod.CONFIG_VERSION}
    unknown = None
    if scope == "global" and os.path.isfile(path):
        try:
            preset = config_mod.read_config_file(path).get("preset")
        except config_mod.ConfigError:
            preset = None  # a file that does not parse is what a reset is for
        if isinstance(preset, str) and preset in presets_mod.PRESETS:
            cleared["preset"] = preset
        elif preset is not None:
            unknown = preset
    config_mod.write_config_file(path, cleared, scope)
    if unknown is not None:
        _out("note: dropped the unknown preset %r; the global file now names none" % (unknown,))
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
    _out(wizard_mod.render_summary(loaded.data))
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
    frozen = None
    if "[" in args.path:
        list_path = args.path.split("[", 1)[0]
        base = _fitted_base(scope, args.cwd, layer)
        seeded = _seed_list(layer, list_path, base)
        if list_path == "reviewers":
            frozen = _frozen_panel_note(seeded, path, base, scope, args.cwd)
    value = args.value if args.raw else config_mod.coerce_scalar(args.value)
    if scope == "project":
        # From the arguments alone, before anything is written: the same
        # refusal a run of that seat would meet.
        refusal = _project_seat_write_refusal(args.path, value, layer, path)
        if refusal:
            _err(refusal)
            return 2
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
    if frozen:
        _out(frozen)
    reloaded = config_mod.load(args.cwd, validate_result=False)
    effective = reloaded.data
    if before is not None:
        _left_the_fit_note(before, reloaded, role_key, path)
    for problem in config_mod.validate(effective, project_layer=reloaded.project_layer):
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
            reviewers = layer.get("reviewers")
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
    if _compose_preview(scope, pruned, args.cwd)[0] != _compose_preview(scope, layer, args.cwd)[0]:
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
    problems = config_mod.validate(loaded.data, project_layer=loaded.project_layer)
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
        _emit_json([_redacted_reviewer(reviewer) for reviewer in reviewers])
        return 0
    if not reviewers:
        _out("No reviewers configured.")
        return 0
    for index, reviewer in enumerate(reviewers, 1):
        model = reviewer.get("model") or {}
        when = opt_mod.condition_label(reviewer)
        _out(
            "%d. %-18s %-8s %-18s %-8s %s%s"
            % (
                index,
                reviewer.get("id"),
                reviewer.get("provider"),
                model.get("family", "default"),
                model.get("version", "latest"),
                reviewer.get("role", "general"),
                "" if when == opt_mod.WHEN_ALWAYS else " (when: %s)" % when,
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
    base = _fitted_base(scope, args.cwd, layer)
    frozen = _frozen_panel_note(_seed_list(layer, "reviewers", base), path, base, scope, args.cwd)
    role = args.role or "general"
    reviewer_id = args.id or config_mod.suggest_reviewer_id(layer, args.provider, role)
    if scope == "project" and config_mod.warned_provider(args.provider):
        # Nothing written: the same refusal a run of this reviewer would meet.
        name = os.path.basename(path)
        _err(config_mod.project_reviewer_refusal("reviewer %s" % reviewer_id, args.provider, name))
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
    if frozen:
        _out(frozen)
    _warn_unenforced_after_write(args.cwd)
    return 0


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


def _reviewer_problems(layer: Dict[str, Any]) -> List[str]:
    """The ``reviewers`` problems of a config layer, merged over the defaults."""
    problems = config_mod.validate(config_mod.deep_merge(config_mod.default_config(), layer))
    return [p for p in problems if p.startswith("reviewers")]


def _unindexed(problem: str) -> str:
    """A problem without its list indices, which a removal shifts."""
    return re.sub(r"\[\d+\]", "[]", problem)


def cmd_reviewer_remove(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    base = _fitted_base(scope, args.cwd, layer)
    frozen = _frozen_panel_note(_seed_list(layer, "reviewers", base), path, base, scope, args.cwd)
    before = {_unindexed(p) for p in _reviewer_problems(layer)}
    try:
        _, removed = config_mod.remove_reviewer(layer, args.selector)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    # The same check add and set make: removing the last reviewer that always
    # runs would leave a panel that a quiet round could not be reviewed by.
    # Only what the removal itself introduced is refused, so removing a broken
    # reviewer stays a way out of a panel that has another one.
    problems = [p for p in _reviewer_problems(layer) if _unindexed(p) not in before]
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
    base = _fitted_base(scope, args.cwd, layer)
    frozen = _frozen_panel_note(_seed_list(layer, "reviewers", base), path, base, scope, args.cwd)
    try:
        index, reviewer = config_mod.find_reviewer(layer, args.selector)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    if args.provider and scope == "project" and config_mod.warned_provider(args.provider):
        display = "reviewer %s" % (args.id or reviewer.get("id") or index + 1)
        _err(config_mod.project_reviewer_refusal(display, args.provider, os.path.basename(path)))
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
