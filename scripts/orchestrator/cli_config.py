"""The config, model, reviewer and doctor commands."""

from __future__ import annotations

import argparse
import copy
import os
import re
import sys
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple, Union

from . import claude_hooks, hosts, miniyaml
from . import config as config_mod
from . import config_policy as policy_mod
from . import doctor as doctor_mod
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
    _global_lists,
    _layer_path,
    _load_lenient,
    _not_copied,
    _out,
    _panel_write_problems,
    _prune_base,
    _read_layer,
    _recorded,
    _resolve_scope,
    _seed_list,
    _seed_panel,
    _without_warned_seats,
)
from .cli_hooks import LEFT_OUT_NOTE, OTHER_PROJECTS_NOTE, report_install, report_uninstall
from .execution import DELEGATED_ENV
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
    warned_provider,
)
from .summary import DESIGN_PANEL_SOURCES, render_summary, seat_notes

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


def _effective_source(loaded: config_mod.LoadedConfig, show: Callable[[str], str]) -> str:
    """The effective scope's source; ``show`` prints the global file's path."""
    return "effective (project: %s, global: %s)" % (
        loaded.project_path or "none",
        show(loaded.global_path) if loaded.global_path else "none",
    )


def cmd_config_show(args: argparse.Namespace) -> int:
    loaded = _load_lenient(args.cwd)
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
        source = _effective_source(loaded, str)

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
        # Present only where Windows keeps the file somewhere else (#235).
        if args.scope == "global" and exists:
            real = config_mod.stored_elsewhere(path)
            if real is not None:
                payload["source_real"] = real
        if not scoped:
            # The files ``source`` names, so ``global_real`` sits beside its path.
            payload["project"] = loaded.project_path
            payload["global"] = loaded.global_path
            if loaded.global_path:
                real = config_mod.stored_elsewhere(loaded.global_path)
                if real is not None:
                    payload["global_real"] = real
            payload["reviewer_origins"] = [origin.label() for origin in loaded.reviewer_origins]
            if loaded.has_design_panel():
                payload["design_reviewer_origins"] = [
                    origin.label() for origin in loaded.design_reviewer_origins
                ]
        _emit_json(payload)
        return 0
    # The JSON keeps ``source`` as it was and adds ``source_real`` or
    # ``global_real`` instead.
    if args.scope == "global" and exists:
        source = config_mod.shown_location(path)
    elif not scoped:
        source = _effective_source(loaded, config_mod.shown_location)
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
        summary = render_summary(data, loaded.reviewer_origins, loaded.design_reviewer_origins)
        _out(summary if "orchestrator" in data else "(empty layer)")
    if referenced:
        _out("Providers: %s" % ", ".join(_describe_referenced_provider(name) for name in referenced))
    problems = config_mod.validate(
        loaded.data,
        project_layer=loaded.project_layer,
        global_layer=loaded.global_layer,
        origins=loaded.reviewer_origins,
        design_origins=loaded.design_reviewer_origins,
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
    loaded = _load_lenient(args.cwd)
    global_path = config_mod.global_config_path()
    if loaded.global_path:
        _out("global:  %s" % config_mod.shown_location(global_path))
    else:
        _out("global:  %s  (not created)" % global_path)
    _out(
        "project: %s"
        % (loaded.project_path or "none (would be %s)" % config_mod.project_config_path(args.cwd))
    )
    return 0


def cmd_config_setup(args: argparse.Namespace) -> int:
    scope = args.scope or "global"
    path = _layer_path(scope, args.cwd)
    existing = config_mod.read_config_file(path) if os.path.isfile(path) else None
    language = None
    if args.language is not None:
        language = config_mod.normalise_language_tag(args.language)
        if language is None:
            _err("--language: %r is not a language tag such as ja, zh-TW, ko or en" % args.language)
            return 2
    before = claude_hooks.configured_layers(args.cwd)

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
                wizard_mod.Prompter(), existing, _fitted_base(scope, existing), scope=scope, reply=language
            )
        except EOFError:
            _err("input ended before setup finished; nothing was saved. Try --defaults instead.")
            return 2
    if language is not None and (args.preset or args.defaults):
        config_mod.set_path(data, "language.reply", language)

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
        design_origins=fit.design_origins,
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
        _out(render_summary(preview, fit.origins, fit.design_origins))
        notes = presets_mod.render_notes(fit)
        if notes:
            _out(notes)
    _out("Saved %s configuration to %s" % (scope, config_mod.shown_location(path)))
    _out(
        "It records only what you chose; everything else follows %s (config show)."
        % config_mod.layer_below(scope)
    )
    _sync_hooks(args, scope, before)
    return 0


def _sync_hooks(args: argparse.Namespace, scope: str, before: claude_hooks.Layers) -> None:
    """Add or remove the Claude Code hooks after ``scope``'s file was written.

    ``before`` is ``claude_hooks.configured_layers`` from before the write;
    ``claude_hooks.sync`` decides, and this says what it did. Never from a
    delegated run, and never with ``--no-hooks``. A refusal is a warning: the
    configuration is saved either way.
    """
    if getattr(args, "no_hooks", False) or os.environ.get(DELEGATED_ENV):
        return
    after = claude_hooks.configured_layers(args.cwd)
    command = claude_hooks.FIX_COMMAND if after.tag(scope) else claude_hooks.UNINSTALL_COMMAND
    try:
        synced = claude_hooks.sync(scope, before, after, hosts.PLUGIN_ROOT)
    except claude_hooks.HooksError as exc:
        _err("warning: %s" % exc)
        _err("warning: the configuration is saved; run `%s` once that is fixed" % command)
        return
    if synced.change is not None and synced.action == claude_hooks.SYNC_INSTALLED:
        report_install(synced.change)
    elif synced.change is not None and synced.action == claude_hooks.SYNC_UNINSTALLED:
        report_uninstall(synced.change)
        _out("note: %s" % OTHER_PROJECTS_NOTE)
    elif synced.action == claude_hooks.SYNC_NO_SETTINGS:
        _out("note: Claude Code settings not found; `%s` creates them" % claude_hooks.FIX_COMMAND)
    elif synced.action == claude_hooks.SYNC_LEFT_OUT:
        _out("note: %s" % LEFT_OUT_NOTE)


def cmd_config_reset(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path = _layer_path(scope, args.cwd)
    language_before = claude_hooks.configured_layers(args.cwd)
    if args.delete:
        if os.path.isfile(path):
            os.remove(path)
            _out("Removed %s" % config_mod.shown_location(path))
        else:
            _out("Nothing to remove at %s" % config_mod.shown_location(path))
        _sync_hooks(args, scope, language_before)
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
    shown = config_mod.shown_location(path)
    _out("Reset %s configuration: overrides cleared, %s (%s)" % (scope, following, shown))
    _sync_hooks(args, scope, language_before)
    try:
        loaded = _load_lenient(args.cwd)
    except config_mod.ConfigError as exc:
        _err(str(exc))  # the other layer does not parse; the reset itself is done
        return 0
    _out(render_summary(loaded.data, loaded.reviewer_origins, loaded.design_reviewer_origins))
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
    before = _load_lenient(args.cwd) if role_key in config_mod.KNOWN_ROLES else None
    language_before = (
        claude_hooks.configured_layers(args.cwd) if args.path in ("language", "language.reply") else None
    )
    # A panel path is checked as the panel it leaves, before anything is written.
    design_path = args.path.startswith(config_mod.DESIGN_PANEL.reviewers)
    panel_path = role_key in ("reviewers", "reviewers_extra") or design_path
    panel_before = copy.deepcopy(layer) if panel_path else None
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
        if index is not None and _id_taken(preview.get("reviewers") or [], index, value):
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
    _out("%s = %r  (%s: %s)" % (args.path, value, scope, config_mod.shown_location(path)))
    if frozen:
        _out(frozen)
    reloaded = _load_lenient(args.cwd)
    effective = reloaded.data
    if before is not None:
        _left_the_fit_note(before, reloaded, role_key, path)
    problems = config_mod.validate(
        effective,
        project_layer=reloaded.project_layer,
        global_layer=reloaded.global_layer,
        origins=reloaded.reviewer_origins,
        design_origins=reloaded.design_reviewer_origins,
    )
    for problem in problems:
        _err("warning: %s" % problem)
    for warning in policy_mod.read_only_arg_warnings(reloaded):
        _err("warning: %s" % warning)
    _warn_unenforced(reloaded)
    _warn_unresolvable(effective, args.path.split(".")[0])
    if language_before is not None:
        _sync_hooks(args, scope, language_before)
    return 0


#: The keys that name a read-only seat's provider.
_SEAT_PROVIDER_PATHS = (
    re.compile(r"^(?P<role>orchestrator|architect)\.provider$"),
    re.compile(r"^(?P<role>orchestrator|architect)\.model_tiers\.(?P<tier>.+)\.provider$"),
    re.compile(r"^reviewers\[(?P<index>\d+)\]\.provider$"),
    re.compile(r"^reviewers_extra\[(?P<index>\d+)\]\.provider$"),
    re.compile(r"^review\.design\.reviewers\[(?P<index>\d+)\]\.provider$"),
    re.compile(r"^review\.design\.reviewers_extra\[(?P<index>\d+)\]\.provider$"),
)


def _project_seat_write_refusal(dotted: str, value: Any, layer: Dict[str, Any], path: str) -> str:
    """Why ``dotted = value`` is not written to the project file, or ""."""
    provider = str(value) if isinstance(value, str) else ""
    if not provider or not warned_provider(provider):
        return ""
    name = os.path.basename(path)
    for pattern in _SEAT_PROVIDER_PATHS:
        match = pattern.match(dotted)
        if not match:
            continue
        found = match.groupdict()
        if found.get("index") is not None:
            index = int(found["index"])
            reviewers = config_mod.get_path(layer, dotted.split("[", 1)[0])
            entry = reviewers[index] if isinstance(reviewers, list) and index < len(reviewers) else {}
            reviewer_id = entry.get("id") if isinstance(entry, dict) else None
            display = "reviewer %s" % (reviewer_id or index + 1)
            return policy_mod.project_reviewer_refusal(display, provider, name)
        role, tier = found["role"], found.get("tier")
        display = "%s (tier %s)" % (role, tier) if tier else role
        return policy_mod.project_seat_refusal(display, provider, name, dotted)
    return ""


def _warn_unenforced(loaded: config_mod.LoadedConfig) -> None:
    """One ``warning:`` line per read-only seat that cannot be held to reading."""
    refused = list(policy_mod.project_raw_arg_refusals(loaded))
    origins = loaded.design_reviewer_origins
    for line in policy_mod.read_only_enforcement_warnings(loaded.data, refused, origins):
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
    _out(message % (role, config_mod.shown_location(path), new, after.preset))


def cmd_config_prune(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path = _layer_path(scope, args.cwd)
    if not os.path.isfile(path):
        _err("no %s configuration file at %s" % (scope, path))
        return 2
    layer = config_mod.read_config_file(path)
    base, held = _prune_base(scope, args.cwd, layer)
    pruned, dropped = config_mod.prune_layer(layer, base)
    if _compose_preview(scope, pruned)[0] != _compose_preview(scope, layer)[0]:
        # Nothing should be able to get here. It is checked anyway because the
        # failure would be a configuration quietly changing underneath someone
        # who asked for it not to.
        _err("%s: pruning would change the effective configuration, so nothing was written." % path)
        return 2

    shown = config_mod.shown_location(path)
    if "reviewers" in held:
        _out(
            "Kept reviewers in %s: dropping it would move design reviews to the preset's design panel."
            % shown
        )
    if not dropped:
        _out("Nothing to drop from %s: it already holds only its own decisions." % shown)
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
        _out("Recorded the configuration format version in %s." % shown)
        return 0
    for entry in dropped:
        _out("  %-40s %s" % (entry["setting"], _prune_value(entry["value"])))
    _out("Values equal to the current default were assumed to be inherited.")
    if args.dry_run:
        _out("%d would be dropped from %s (dry run, nothing written)." % (len(dropped), shown))
        return 0
    config_mod.write_config_file(path, pruned, scope)
    _out("Dropped %d from %s; they now follow %s." % (len(dropped), shown, config_mod.layer_below(scope)))
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
    loaded = _load_lenient(args.cwd)
    problems = config_mod.validate(
        loaded.data,
        project_layer=loaded.project_layer,
        global_layer=loaded.global_layer,
        origins=loaded.reviewer_origins,
        design_origins=loaded.design_reviewer_origins,
    )
    # Warnings, not problems: they refuse one role's runs, not the file.
    warnings = policy_mod.read_only_arg_warnings(loaded)
    refused = list(policy_mod.project_raw_arg_refusals(loaded))
    origins = loaded.design_reviewer_origins
    warnings += policy_mod.read_only_enforcement_warnings(loaded.data, refused, origins)
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
    if args.provider and warned_provider(args.provider):
        display = "suggested reviewers"
        _err(policy_mod.project_reviewer_refusal(display, args.provider, os.path.basename(target)))
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
        # A broken entry keeps its place, but never its contents: it may be a
        # secret pasted in the wrong place, in a shape `redact` does not know.
        return "[invalid entry]"
    shown = copy.deepcopy(reviewer)
    when = shown.get("when")
    if isinstance(when, dict) and isinstance(when.get("paths"), list):
        when["paths"] = [redact(p) if isinstance(p, str) else p for p in when["paths"]]
    return shown


def cmd_reviewer_list(args: argparse.Namespace) -> int:
    loaded = _load_lenient(args.cwd)
    design = bool(getattr(args, "design", False))
    # A panel that is not a list is listed as none; `config validate` names it.
    if design:
        reviewers = loaded.design_reviewers()
        origin_of = loaded.design_reviewer_origin
    else:
        panel = loaded.data.get("reviewers")
        reviewers = loaded.reviewers() if isinstance(panel, list) else []
        origin_of = loaded.reviewer_origin
    if args.json:
        listed: List[Any] = []
        for index, reviewer in enumerate(reviewers):
            shown = _redacted_reviewer(reviewer)
            if isinstance(shown, dict):
                shown["origin"] = origin_of(index).label()
            listed.append(shown)
        _emit_json(listed)
        return 0
    if design:
        _out("(design panel: %s)" % DESIGN_PANEL_SOURCES[loaded.design_panel_source])
    if not any(isinstance(reviewer, dict) for reviewer in reviewers):
        _out("No reviewers configured.")
        return 0
    # A broken entry is skipped but keeps its number, so each valid one is
    # numbered, and its origin read, by its position in the panel.
    for index, reviewer in enumerate(reviewers, 1):
        if not isinstance(reviewer, dict):
            continue
        model = reviewer.get("model") or {}
        when = opt_mod.condition_label(reviewer)
        origin = origin_of(index - 1)
        _out(
            "%d. %-18s %-8s %-18s %-8s %s%s%s%s"
            % (
                index,
                reviewer.get("id"),
                reviewer.get("provider"),
                model.get("family", "default"),
                model.get("version", "latest"),
                reviewer.get("role", "general"),
                "" if when == opt_mod.WHEN_ALWAYS else " (when: %s)" % when,
                seat_notes(reviewer),
                " (extra: %s)" % origin.layer if origin.extra else "",
            )
        )
    return 0


def _design_flags(args: argparse.Namespace) -> Tuple[bool, Optional[int]]:
    """``(design, refusal)``: whether ``--design`` was given, and the exit code of a flag it refuses."""
    design = bool(getattr(args, "design", False))
    if design and getattr(args, "when_paths", None):
        _err(
            "--when-paths applies to the code review only: a plan has no changed paths (use --when high-risk)"
        )
        return design, 2
    return design, None


def _panel_in_force(preview: Dict[str, Any], fit: Any, design: bool) -> Tuple[List[Any], List[Any]]:
    """``(panel, origins)`` a composed preview runs on one stage.

    The design panel is the preview's own when a file or the fit sets one,
    else the code panel without ``when``, each seat keeping its code origin.
    """
    if not design:
        panel = preview.get("reviewers")
        return (panel if isinstance(panel, list) else []), list(fit.origins)
    own = config_mod.get_path(preview, config_mod.DESIGN_PANEL.reviewers)
    if own is not None:
        return (own if isinstance(own, list) else []), list(fit.design_origins)
    code = preview.get("reviewers")
    copies = [config_mod.without_when(reviewer) for reviewer in code] if isinstance(code, list) else []
    return copies, list(fit.origins)


def _fit_design_preset(preset: Any, origins: Sequence[Any]) -> Optional[str]:
    """The preset whose fit the design panel follows, or None when it follows a file or the code panel."""
    if not preset or not config_mod.design_panel_is_fit(origins):
        return None
    return str(preset)


def _inherited_design_panel(scope: str, preset: Any = None, origins: Sequence[Any] = ()) -> str:
    """What a file that lists no design reviewers takes its design panel from.

    ``origins`` are the design panel's in force, which say whether it is the fit's.
    """
    below = _global_file() if scope == "project" else {}
    if config_mod.get_path(below, config_mod.DESIGN_PANEL.reviewers) is not None:
        return "the global file's design reviewers"
    fit_preset = _fit_design_preset(preset, origins)
    if fit_preset:
        return "preset %s's fit" % fit_preset
    return "the code panel"


def _both_conditions(args: argparse.Namespace) -> bool:
    """``--when`` and ``--when-paths`` together, which name two conditions for one reviewer."""
    if args.when and args.when_paths:
        _err("give --when or --when-paths, not both")
        return True
    return False


def cmd_reviewer_add(args: argparse.Namespace) -> int:
    if _both_conditions(args):
        return 2
    design, refusal = _design_flags(args)
    if refusal is not None:
        return refusal
    keys = config_mod.DESIGN_PANEL if design else config_mod.CODE_PANEL
    scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    before = copy.deepcopy(layer)
    preview, fit, preset, _source = _compose_preview(scope, layer)
    panel, origins = _panel_in_force(preview, fit, design)
    role = args.role or "general"
    reviewer_id = args.id or config_mod.suggest_reviewer_id({"reviewers": panel}, args.provider, role)
    if scope == "project" and warned_provider(args.provider):
        # Nothing written: the same refusal a run of this reviewer would meet.
        name = os.path.basename(path)
        _err(policy_mod.project_reviewer_refusal("reviewer %s" % reviewer_id, args.provider, name))
        return 2
    if args.id and any(isinstance(r, dict) and r.get("id") == args.id for r in panel):
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
        high_risk_family=getattr(args, "high_risk_model", None),
        relevance=getattr(args, "relevance", None),
    )
    # A file that lists the panel owns it, so the reviewer joins that list;
    # otherwise it goes beside the panel the file inherits, which keeps
    # following the fit or the global file's list.
    listed = config_mod.get_path(layer, keys.reviewers) is not None
    try:
        if listed:
            config_mod.add_reviewer(layer, reviewer, keys)
        else:
            config_mod.add_extra_reviewer(layer, reviewer, keys)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    blocking = _panel_write_problems(scope, before, layer)
    if blocking:
        for problem in blocking:
            _err(problem)
        return 2
    config_mod.write_config_file(path, layer, scope)
    shown = config_mod.shown_location(path)
    noun = "design reviewer" if design else "reviewer"
    added = "Added %s %s (%s / %s / %s) to %s" % (noun, reviewer_id, args.provider, family, role, shown)
    if listed:
        _out(added)
    elif design:
        follows = _inherited_design_panel(scope, preset, origins)
        _out("%s as a design extra; the design panel still follows %s" % (added, follows))
    else:
        _out("%s as an extra; the panel still follows %s" % (added, _inherited_panel(scope, preset)))
    _warn_unenforced_after_write(args.cwd)
    return 0


def _inherited_panel(scope: str, preset: Any) -> str:
    """What a file that lists no reviewers takes its panel from, the way a message says it."""
    if scope == "project" and _global_file().get("reviewers") is not None:
        return "the global file's reviewers"
    return "preset %s's fit" % preset if preset else "the built-in defaults"


def _own_extra(origins: List[Any], index: int, scope: str, keys: Any = config_mod.CODE_PANEL) -> Any:
    """The origin of reviewer ``index`` when it is an extra of the file being edited, else None."""
    origin = origins[index] if index < len(origins) else None
    if origin is not None and origin.key == keys.extras and origin.layer == scope:
        return origin
    return None


def _renamed_own_extra(
    layer: Dict[str, Any],
    panel: List[Any],
    origins: List[Any],
    scope: str,
    selector: str,
    keys: Any = config_mod.CODE_PANEL,
) -> Optional[Tuple[int, str]]:
    """``(j, id)`` when the extras' entry ``j`` of this file is written as ``selector``
    but runs under another id, since a seat in force took it; else None.

    The selector would otherwise find that seat, and editing it would seed
    the panel and change or drop a reviewer nobody named.
    """
    extras = config_mod.get_path(layer, keys.extras)
    if not isinstance(extras, list):
        return None
    for position, extra in enumerate(extras):
        if not isinstance(extra, dict) or extra.get("id") != selector:
            continue
        origin = config_mod.ReviewerOrigin(scope, keys.extras, position)
        for index, seat_origin in enumerate(origins):
            if seat_origin != origin or index >= len(panel) or not isinstance(panel[index], dict):
                continue
            runs_as = panel[index].get("id")
            if isinstance(runs_as, str) and runs_as != selector:
                return position, runs_as
    return None


def _refuse_renamed_own_extra(
    layer: Dict[str, Any],
    panel: List[Any],
    origins: List[Any],
    scope: str,
    selector: str,
    keys: Any = config_mod.CODE_PANEL,
) -> bool:
    """Print the refusal ``_renamed_own_extra`` calls for; True when it did."""
    renamed = _renamed_own_extra(layer, panel, origins, scope, selector, keys)
    if renamed is None:
        return False
    position, runs_as = renamed
    message = (
        "reviewer %s: %s[%d] in the %s file runs as %s, since %s is taken; use %s "
        "(select the other seat by its position in reviewer list)"
    )
    _err(message % (selector, keys.extras, position, scope, runs_as, selector, runs_as))
    return True


def _id_taken(panel: List[Any], index: Optional[int], reviewer_id: Any) -> bool:
    """Whether a reviewer other than ``index`` of the panel in force holds ``reviewer_id``.

    The check ``reviewer add`` makes, for a write that renames a seat: an
    extra never takes an id, it is renamed, so the name written would not be
    the one it runs under.
    """
    return any(
        position != index and isinstance(reviewer, dict) and reviewer.get("id") == reviewer_id
        for position, reviewer in enumerate(panel)
    )


def _seed_design_panel(
    scope: str,
    layer: Dict[str, Any],
    panel: List[Any],
    origins: List[Any],
    from_code: bool,
    path: str,
    fit_preset: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Copy the design panel in force into the file before one seat of it is edited.

    ``_seed_panel`` for ``review.design.reviewers``: a list replaces the one
    below it whole, so changing one seat is a decision about the others. The
    file's own design extras stay extras. A seat copied from the code panel
    loses its ``when``, as it never had one on a design round, and the note
    says so. ``fit_preset`` names the preset whose fit is copied; a file
    that takes it off the fit says what it recorded, as
    ``_frozen_panel_note`` does for the code panel. In a project file a seat
    on a warned provider is left out by the rule ``_seed_panel`` follows
    (``_without_warned_seats``): unless the global file lists the panel the
    seat came from -- the design panel for a design seat, the code panel for
    one copied from it -- or it is the project file's own. Returns the notes
    for a write that succeeds and for one that fails.
    """
    if config_mod.get_path(layer, config_mod.DESIGN_PANEL.reviewers) is not None:
        return None, None
    seats: List[Tuple[Any, Any]] = []
    for index, reviewer in enumerate(panel):
        origin = origins[index] if index < len(origins) else None
        if origin is not None and origin.layer == scope and origin.key == config_mod.DESIGN_PANEL.extras:
            continue
        seats.append((reviewer, origin))
    listed = {
        True: scope == "project" and _global_lists(config_mod.DESIGN_PANEL.reviewers),
        False: scope == "project" and _global_lists(config_mod.CODE_PANEL.reviewers),
    }

    def chosen(index: int) -> bool:
        origin = seats[index][1]
        if origin is None:
            return False
        return origin.layer == "project" or listed[origin.design]

    copied, dropped = _without_warned_seats([reviewer for reviewer, _origin in seats], scope, chosen)
    config_mod.set_path(layer, config_mod.DESIGN_PANEL.reviewers, copy.deepcopy(copied))
    if from_code:
        source = "the code panel, without its when conditions"
    elif fit_preset:
        source = "preset %s's fit" % fit_preset
    else:
        source = "the design panel in force"
    frozen = "note: %s now lists the design reviewers (review.design.reviewers), copied from %s" % (
        config_mod.shown_location(path),
        source,
    )
    if fit_preset:
        # The fit's design panel is in force only where no file lists one, so
        # a project file copying it takes it off the fit too.
        frozen += "; the design panel no longer follows preset %s's fit (recorded %s)" % (
            fit_preset,
            _recorded(copied),
        )
    if not dropped:
        return frozen, None
    left_out = _not_copied(path, dropped)
    return frozen + "; " + left_out, "note: " + left_out


def _seat_selector(reviewer: Any, selector: str) -> str:
    """How a seat resolved over the panel in force is found again in the seeded list."""
    reviewer_id = reviewer.get("id") if isinstance(reviewer, dict) else None
    return reviewer_id if isinstance(reviewer_id, str) and reviewer_id else selector


def _warn_unenforced_after_write(cwd: Any) -> None:
    """The enforcement warnings of the configuration a write left in force."""
    try:
        loaded = _load_lenient(cwd)
    except config_mod.ConfigError:
        return  # the other layer does not parse; the write itself is done
    # The refusals too, as `config set` prints them: a project panel copied
    # from the global one can hold a seat the project file may not set.
    for warning in policy_mod.read_only_arg_warnings(loaded):
        _err("warning: %s" % warning)
    _warn_unenforced(loaded)


class _FoundSeat(NamedTuple):
    """A seat a `reviewer set` or `remove` selector named, and the panel it is in."""

    #: Whether it is the design panel's (``--design``).
    design: bool
    keys: config_mod.PanelKeys
    #: The panel in force with the file being edited, and where each was written.
    panel: List[Any]
    origins: List[Any]
    #: Its place in ``panel``. Not ``index``, which ``tuple`` owns.
    position: int
    reviewer: Dict[str, Any]
    #: A design panel that is the code panel without ``when``: no file lists one.
    from_code: bool
    #: The preset whose fit the design panel is, or None.
    fit_preset: Optional[str] = None


def _seed_for_edit(
    args: argparse.Namespace,
    scope: str,
    layer: Dict[str, Any],
    path: str,
    found: _FoundSeat,
) -> Tuple[Optional[str], Optional[str]]:
    """Copy the panel the found seat is in into the file, as an edit of an inherited seat needs."""
    if found.design:
        return _seed_design_panel(
            scope, layer, found.panel, found.origins, found.from_code, path, found.fit_preset
        )
    return _seed_panel(scope, layer, _fitted_base(scope, layer), path, args.cwd)


def _find_seat(args: argparse.Namespace, scope: str, layer: Dict[str, Any]) -> Union[int, _FoundSeat]:
    """The seat ``args.selector`` names in the panel in force with this file, or an exit code.

    Resolved over the panel in force, which in project scope is what
    `reviewer list` shows, so its positions are the list's. ``--design``
    resolves it over the design panel.
    """
    design, refusal = _design_flags(args)
    if refusal is not None:
        return refusal
    keys = config_mod.DESIGN_PANEL if design else config_mod.CODE_PANEL
    preview, fit, preset, _source = _compose_preview(scope, layer)
    panel, origins = _panel_in_force(preview, fit, design)
    if _refuse_renamed_own_extra(layer, panel, origins, scope, args.selector, keys):
        return 2
    try:
        index, reviewer = config_mod.find_reviewer({"reviewers": panel}, args.selector)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    from_code = design and config_mod.get_path(preview, keys.reviewers) is None
    fit_preset = _fit_design_preset(preset, origins) if design else None
    return _FoundSeat(design, keys, panel, origins, index, reviewer, from_code, fit_preset)


def cmd_reviewer_remove(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    before = copy.deepcopy(layer)
    found = _find_seat(args, scope, layer)
    if isinstance(found, int):
        return found
    keys = found.keys
    frozen = None
    origin = _own_extra(found.origins, found.position, scope, keys)
    if origin is not None:
        # The file's own extra goes in place, and the panel stays inherited.
        removed = config_mod.get_path(layer, keys.extras).pop(origin.position)
    else:
        frozen, left_out = _seed_for_edit(args, scope, layer, path, found)
        selector = _seat_selector(found.reviewer, args.selector)
        try:
            _, removed = config_mod.remove_reviewer(layer, selector, keys)
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
    _out("Removed reviewer %s from %s" % (removed.get("id"), config_mod.shown_location(path)))
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
    found = _find_seat(args, scope, layer)
    if isinstance(found, int):
        return found
    keys, index = found.keys, found.position
    # Before either edit path: an extra of the project file is held to it too.
    if args.provider and scope == "project" and warned_provider(args.provider):
        display = "reviewer %s" % (args.id or found.reviewer.get("id") or index + 1)
        _err(policy_mod.project_reviewer_refusal(display, args.provider, os.path.basename(path)))
        return 2
    if args.id and _id_taken(found.panel, index, args.id):
        _err("reviewer id %r already exists" % args.id)
        return 2
    frozen = None
    origin = _own_extra(found.origins, index, scope, keys)
    if origin is not None:
        # The entry as the file holds it: a renamed extra keeps its written id.
        reviewer = config_mod.get_path(layer, keys.extras)[origin.position]
    else:
        frozen, left_out = _seed_for_edit(args, scope, layer, path, found)
        selector = _seat_selector(found.reviewer, args.selector)
        try:
            _, reviewer = config_mod.find_reviewer(layer, selector, keys)
        except config_mod.ConfigError as exc:
            _err(str(exc))
            if left_out:
                _err(left_out)
            return 2
    family_note = ""
    if args.provider:
        changed = args.provider != reviewer.get("provider")
        keep_high_risk = bool(getattr(args, "high_risk_model", None))
        risk_removed = config_mod.move_reviewer_provider(reviewer, args.provider, keep_high_risk)
        if changed and not (args.model or args.pin):
            # A family is the old CLI's word for a model; the new one would
            # not resolve it, so it gets its own default instead.
            old_family = (reviewer.get("model") or {}).get("family")
            new_family = config_mod.default_reviewer_family(args.provider)
            reviewer["model"] = {"family": new_family, "version": "latest"}
            message = "note: model family reset from %r to %r for provider %s (--model picks another)"
            family_note = message % (old_family, new_family, args.provider)
        if risk_removed:
            # Whatever --model says: the high-risk model has no default to reset to.
            removed = "%s (--high-risk-model sets another)" % config_mod.HIGH_RISK_MODEL_REMOVED
            family_note = (
                family_note + "; " + removed
                if family_note
                else "note: provider is now %s; %s" % (args.provider, removed)
            )
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
    _set_seat_options(args, reviewer)
    problems = _panel_write_problems(scope, before, layer)
    if problems:
        for problem in problems:
            _err(problem)
        return 2
    config_mod.write_config_file(path, layer, scope)
    _out("Updated reviewer %s in %s" % (reviewer.get("id"), config_mod.shown_location(path)))
    if family_note:
        _out(family_note)
    if frozen:
        _out(frozen)
    _warn_unenforced_after_write(args.cwd)
    return 0


def _set_seat_options(args: argparse.Namespace, reviewer: Dict[str, Any]) -> None:
    """``--high-risk-model``, ``--clear-high-risk-model`` and ``--relevance`` on one seat.

    Each replaces what was there whole; ``--relevance default`` removes the
    key, so the seat is judged by its role's own rule again.
    """
    high_risk_model = getattr(args, "high_risk_model", None)
    if high_risk_model:
        reviewer["high_risk_model"] = {"family": high_risk_model, "version": "latest"}
    elif getattr(args, "clear_high_risk_model", False):
        reviewer.pop("high_risk_model", None)
    relevance = getattr(args, "relevance", None)
    if relevance == "default":
        reviewer.pop("relevance", None)
    elif relevance:
        reviewer["relevance"] = relevance


# --------------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor_mod.collect(args.cwd, probe_models=not args.fast)
    if args.json:
        _emit_json(report)
    else:
        _out(doctor_mod.render(report))
    return doctor_mod.exit_code(report) if args.strict else 0
