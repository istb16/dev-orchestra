"""Environment diagnostics.

Reports what is installed, what is configured, and whether each configured role
can actually be resolved to a model. Deliberately reports authentication as a
*state* ("available" / "unknown") and never echoes a credential value.
"""

from __future__ import annotations

import os
import platform
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import config as config_mod
from . import config_policy as policy_mod
from . import hosts, verified
from . import optimization as opt_mod
from . import presets as presets_mod
from . import workspace as ws
from .providers import (
    OFFLINE,
    REFUSED_ENFORCEMENT,
    USER_PROVIDERS_DISABLED_ENV,
    WARNED_ENFORCEMENT,
    ModelResolutionError,
    ResolvedModel,
    available_providers,
    describe_exception,
    describe_origin,
    get_provider,
    origin_payload,
    provider_origin,
    redact,
    user_provider_report,
    user_providers_disabled,
)
from .summary import DESIGN_PANEL_SOURCES

ROLE_LABELS = (
    ("orchestrator", "Orchestrator"),
    ("architect", "Architect"),
    ("implementer", "Implementer"),
    ("review_fixer", "Review fixer"),
)

#: The live check, next to this package: the same file from a plugin install.
SMOKE_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "smoke_live.py")

#: The directory two levels above skills/dev-orchestra/SKILL.md, which is what
#: Antigravity loads. realpath: launched through a link, this is the checkout.
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def user_home() -> str:
    """The home the Antigravity global install is under; tests replace it."""
    return os.path.expanduser("~")


def store_python() -> bool:
    """Whether this is the Microsoft Store package of Python; tests replace it."""
    return config_mod.on_windows() and _is_store_path((sys.executable, sys.base_prefix))


def _is_store_path(values: Iterable[Optional[str]]) -> bool:
    """Whether any of ``values`` is inside a WindowsApps folder.

    ``sys.executable`` is the Store package's ``...\\Microsoft\\WindowsApps\\...``
    alias, and ``sys.base_prefix`` is under ``C:\\Program Files\\WindowsApps``,
    which also holds for a venv made from it. A Python from any other package
    there counts too: Windows redirects every packaged app's AppData writes the
    same way, and the note still waits for a redirect ``stored_elsewhere`` finds.
    """
    return any("\\windowsapps\\" in (value or "").lower().replace("/", "\\") for value in values)


def _add_real(target: Dict[str, Any], key: str, path: Any) -> None:
    """``target[key]``: where ``path`` really is, only when Windows keeps it elsewhere."""
    if not isinstance(path, str) or not path:
        return
    real = config_mod.stored_elsewhere(path)
    if real is not None:
        target[key] = real


def _stored_elsewhere_report(report: Dict[str, Any]) -> None:
    """Where this Python's files really are, and the note that says so (#235).

    A file read only, before the config is loaded, so a parse error does not
    hide it.
    """
    store = store_python()
    if store:
        report["platform"]["store_python"] = True
    user = report["user_providers"]
    _add_real(user, "directory_real", user.get("directory"))
    for item in [*(user.get("loaded") or []), *(user.get("errors") or [])]:
        _add_real(item, "path_real", item.get("path"))
    directory = config_mod.global_config_dir()
    real = config_mod.stored_elsewhere(directory)
    if real is None:
        return
    if not store:
        report["notes"].append(
            "%s is stored at %s; programs other than this Python open the files there." % (directory, real)
        )
        return
    message = (
        "This Python is the Microsoft Store package (%s). Windows keeps the files it writes under your "
        "AppData in the package's own folder: %s is really at %s. Explorer, editors and other Pythons "
        "don't see those files. Adapters and records in the package folder take precedence over "
        "same-named files in the real %%APPDATA%%, so inspect %s to see the adapter code that runs. "
        "To fix this, either use a python.org Python (`py`), or set DEV_ORCHESTRA_HOME to a trusted "
        "folder you control, outside %%USERPROFILE%%\\AppData and outside any project checkout.%s "
        "Review providers\\*.py before moving them, because they are code that is imported at "
        "startup. Let scripts/smoke_live.py regenerate the verified\\ records instead of copying them."
    )
    # DEV_ORCHESTRA_CONFIG names the global file, which is then not in this folder.
    copy = "" if os.environ.get("DEV_ORCHESTRA_CONFIG") else " Then copy only config.yaml from %s." % real
    providers = os.path.join(real, "providers")
    report["notes"].append(message % (sys.executable, directory, real, providers, copy))


def collect(start: Optional[str] = None, probe_models: bool = True) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
        },
        "providers": {},
        "config": {},
        "roles": {},
        "reviewers": [],
        "problems": [],
        # Worth knowing, never wrong: they do not count towards exit_code.
        "notes": [],
    }
    report["user_providers"] = user_provider_report()
    for failure in report["user_providers"]["errors"]:
        report["problems"].append("user provider %s: %s" % (failure["path"], failure["error"]))
    _stored_elsewhere_report(report)

    detections = {}
    adapter_errors: Dict[str, str] = {}
    # A resume record inside this directory is not trusted; see verified.py.
    # The repository root, as `run` uses, or a subdirectory would trust a
    # record that `run` refuses.
    root = ws.repo_root(start or os.getcwd())
    # Before anything that can return early: a filesystem read only, so a
    # config error or --fast does not hide it.
    _antigravity_live(report, root)
    for name in available_providers():
        # One adapter at a time: a user adapter that raises is reported against
        # its file instead of taking the whole diagnosis down with it.
        try:
            provider = get_provider(name)
            detection = provider.detect()
            entry = detection.to_dict()
            entry["display_name"] = provider.display_name
            entry["model_selection"] = "supported"
            if type(provider).static_enforcement:
                # A constant, so it is known in both modes and whether or not
                # the CLI is there -- and the seats below are noted from it. A
                # user adapter's can raise; the rest of its block still stands.
                try:
                    entry["read_only_enforcement"] = dict(provider.read_only_enforcement())
                except Exception as exc:
                    message = describe_exception(exc)
                    entry["read_only_enforcement"] = {"status": "error", "detail": message}
                    report["problems"].append(
                        "provider %s (%s): read_only_enforcement() raised %s"
                        % (name, describe_origin(name), message)
                    )
            origin = provider_origin(name)
            if origin is not None and origin.kind == "user":
                entry["preset_fit"] = presets_mod.user_fit(provider)._asdict()
            if probe_models and detection.installed:
                candidates = provider.list_models()
                entry["models"] = [candidate.to_dict() for candidate in candidates]
                entry["model_discovery"] = candidates[0].source if candidates else "none"
                if not type(provider).static_enforcement:
                    # A static report was read above, and would be the same.
                    entry["read_only_enforcement"] = dict(provider.read_only_enforcement())
                entry["resume_support"] = dict(provider.resume_support(root))
                _add_real(entry["resume_support"], "record_real", entry["resume_support"].get("record"))
            elif not probe_models:
                # --fast skips this probe. It does not promise that no --help
                # is read: validating options.permission_mode reads one.
                entry.setdefault("read_only_enforcement", {"status": "not-checked"})
                entry["resume_support"] = {"status": "not-checked"}
            if detection.installed and name not in OFFLINE:
                if detection.version:
                    # A file read only, so --fast reports it too.
                    entry["live_check"] = verified.smoke_status(name, detection.version, root)
                    _live_check_note(name, detection.version, entry["live_check"], report)
                else:
                    # No version to look up or to ask a check of.
                    entry["live_check"] = {"status": "version-unavailable"}
            detections[name] = detection
        except Exception as exc:
            message = describe_exception(exc)
            adapter_errors[name] = message
            entry = {"installed": False, "display_name": name, "adapter_error": message}
            report["problems"].append(
                "provider %s (%s): adapter failed during diagnosis: %s"
                % (name, describe_origin(name), message)
            )
        entry["origin"] = origin_payload(name)
        report["providers"][name] = entry
    load_errors = len(report["user_providers"]["errors"])

    try:
        loaded = config_mod.load(start, validate_result=False)
    except config_mod.ConfigError as exc:
        global_path = config_mod.global_config_path()
        exists = os.path.isfile(global_path)
        # The error may be in either file; both are named, as when it loads.
        report["config"] = {
            "status": "error",
            "error": str(exc),
            "global": global_path if exists else "not found",
            "project_override": config_mod.find_project_config(start) or "none",
        }
        if exists:
            _add_real(report["config"], "global_real", global_path)
        report["problems"].append(str(exc))
        return report

    problems = config_mod.validate(
        loaded.data,
        project_layer=loaded.project_layer,
        global_layer=loaded.global_layer,
        origins=loaded.reviewer_origins,
        design_origins=loaded.design_reviewer_origins,
    )
    installed = presets_mod.installed_providers()
    report["config"] = {
        "status": "ok" if not problems else "invalid",
        "global": loaded.global_path or "not found",
        "project_override": loaded.project_path or "none",
        "using_builtin_defaults": loaded.used_defaults,
        "preset": {
            "name": loaded.preset,
            "source": loaded.preset_source,
            "fitted_to": installed,
            "notes": loaded.preset_notes,
        },
        "problems": problems,
        # Reported so a default that has since been improved is visible rather
        # than silently overridden by the file that recorded the old one. On
        # the files alone: a preset's own values are not something pinned.
        "pinned": config_mod.pinned_differences(loaded.files_data),
    }
    # A missing file has no real location to report; the note names its folder.
    _add_real(report["config"], "global_real", loaded.global_path)
    report["problems"].extend(problems)
    report["notes"].extend(loaded.preset_notes)
    # Problems for doctor, warnings for `config validate`: either way these
    # runs are refused, and --strict should say so before one is attempted.
    warnings = policy_mod.read_only_arg_warnings(loaded)
    report["config"]["warnings"] = warnings
    report["problems"].extend(warnings)
    # A seat refused for coming with the project file is a problem above, and
    # not also a note below.
    refused = policy_mod.project_raw_arg_refusals(loaded)
    reviewer_refused = policy_mod.reviewer_raw_arg_refusals(loaded)

    layers = (loaded.global_layer, loaded.project_layer)
    for key, label in ROLE_LABELS:
        spec = loaded.data.get(key)
        hint = ""
        # A role a file sets is not fitted, so a missing CLI there is the file's.
        if loaded.preset and any(config_mod.mentions(layer, key) for layer in layers):
            hint = (
                "set %s.provider to an installed CLI, or remove the role from the file so "
                "preset %s's fit applies" % (key, loaded.preset)
            )
        report["roles"][key] = _describe_role(
            label, spec, detections, report["problems"], adapter_errors, load_errors, missing_hint=hint
        )
        if key in config_mod.READ_ONLY_ROLES:
            _enforcement_report(label, spec, report, report["roles"][key], key in refused)
            # A tier can switch provider, and its runs are refused by that
            # provider's enforcement, not the base role's. One that keeps the
            # provider is refused for the same reason the role already was, so
            # it is not reported a second time.
            for seat in config_mod.role_seats(loaded.data, (key,)):
                if seat.kind != "tier" or seat.same_provider:
                    continue
                tier_label = "%s (tier %s)" % (label, seat.tier)
                _enforcement_report(tier_label, seat.spec, report, None, seat.label in refused)

    # A broken entry is skipped, but still counted, so each valid one keeps
    # the origin of its own position.
    panel = loaded.data.get("reviewers")
    for index, reviewer in enumerate(loaded.reviewers() if isinstance(panel, list) else []):
        if not isinstance(reviewer, dict):
            continue
        label = "Reviewer %s" % reviewer.get("id")
        entry = _reviewer_entry(label, reviewer, detections, report, adapter_errors, load_errors)
        entry["origin"] = loaded.reviewer_origin(index).label()
        report["reviewers"].append(entry)
        from_project = str(reviewer.get("id") or "") in reviewer_refused
        _enforcement_report(label, reviewer, report, entry, from_project)

    # The design panel: its own seats, when a file sets one, are diagnosed as
    # the code seats are; a copy of a code seat was diagnosed above.
    report["design_panel_source"] = loaded.design_panel_source
    report["design_reviewers"] = []
    design_refused = policy_mod.reviewer_raw_arg_refusals(loaded, "design")
    for index, reviewer in enumerate(loaded.design_reviewers()):
        if not isinstance(reviewer, dict):
            continue
        origin = loaded.design_reviewer_origin(index)
        if not origin.design:
            entry = {"id": reviewer.get("id"), "role": reviewer.get("role", "general")}
            entry["origin"] = origin.label()
            report["design_reviewers"].append(entry)
            continue
        label = "Design reviewer %s" % reviewer.get("id")
        entry = _reviewer_entry(label, reviewer, detections, report, adapter_errors, load_errors)
        entry["origin"] = origin.label()
        report["design_reviewers"].append(entry)
        from_project = str(reviewer.get("id") or "") in design_refused
        _enforcement_report(label, reviewer, report, entry, from_project)

    # A non-list or all-broken panel is already one of validate's problems
    # above, and `review run` refuses it rather than skipping the stage.
    if panel is None or panel == []:
        report["problems"].append("no reviewers configured: the independent-review stage will be skipped")
    _default_patterns_note(report, loaded.optimization_settings())
    seats = [*loaded.reviewers(), *loaded.design_reviewers()]
    _role_patterns_problems(report, loaded.optimization_settings(), seats)
    _frozen_panel_notes(report, loaded, installed)
    return report


def _reviewer_entry(
    label: str,
    reviewer: Dict[str, Any],
    detections: Dict[str, Any],
    report: Dict[str, Any],
    adapter_errors: Dict[str, str],
    load_errors: int,
) -> Dict[str, Any]:
    """One reviewer as doctor reports it, of either panel.

    Its model is resolved as a role's is, and so is its ``high_risk_model``,
    against the same provider: a family that will not resolve fails the
    high-risk rounds, which are the ones that matter most.
    """
    entry = _describe_role(label, reviewer, detections, report["problems"], adapter_errors, load_errors)
    entry["id"] = reviewer.get("id")
    entry["role"] = reviewer.get("role", "general")
    when = opt_mod.reviewer_condition(reviewer)
    if when != opt_mod.WHEN_ALWAYS:
        entry["when"] = when
        # The label is kept on the entry, so the renderer never has to
        # rebuild one from the bare kind.
        entry["condition"] = opt_mod.condition_label(reviewer)
        if when == opt_mod.WHEN_PATHS:
            entry["paths"] = [redact(pattern) for pattern in opt_mod.reviewer_paths(reviewer)]
    if reviewer.get("relevance") is not None:
        entry["relevance"] = reviewer.get("relevance")
    risk_model = reviewer.get("high_risk_model")
    if isinstance(risk_model, dict):
        shown: Dict[str, Any] = {"family": risk_model.get("family", "")}
        # Only once the seat itself resolved: a missing CLI or a broken
        # adapter is already a problem above, and would only be said twice.
        if entry.get("status") == "ok":
            switched = {"provider": reviewer.get("provider"), "model": risk_model}
            described = _describe_role(
                "%s (high-risk model)" % label,
                switched,
                detections,
                report["problems"],
                adapter_errors,
                load_errors,
            )
            shown.update({key: described[key] for key in ("status", "resolved") if key in described})
        entry["high_risk_model"] = shown
    return entry


def _role_patterns_problems(report: Dict[str, Any], settings: Dict[str, Any], seats: Sequence[Any]) -> None:
    """A role relevance rule with no pattern of its own to judge by.

    ``config validate`` accepts ``security_paths: []`` -- a list replaced on
    purpose may be empty -- but with the rule on, it leaves a security seat
    that opts in (``relevance: security``) out of every round no high-risk
    path reaches, which is worth a problem
    here as no high-risk pattern at all is for a ``when: high-risk`` seat.
    A rule no seat of either panel is judged by leaves nobody out, so its
    empty list is no problem.
    """
    if not opt_mod.skip_unneeded_roles(settings):
        return
    rules = {opt_mod.relevance_rule(seat) for seat in seats}
    for key, patterns in (
        ("security_paths", opt_mod.security_patterns(settings)),
        ("architecture_paths", opt_mod.architecture_patterns(settings)),
    ):
        if key.split("_")[0] in rules and not patterns:
            report["problems"].append(
                "optimization.%s: no pattern in force, so the %s rule finds nothing to keep its seat "
                "for; add patterns to %s or extra_%s, or set optimization.skip_unneeded_roles false"
                % (key, key.split("_")[0], key, key)
            )


def _seat(reviewer: Dict[str, Any]) -> Tuple[Any, ...]:
    """What a seat is, ids aside: provider, family, role and condition."""
    when = reviewer.get("when")
    return (
        reviewer.get("provider"),
        (reviewer.get("model") or {}).get("family"),
        reviewer.get("role", "general"),
        None if when in (None, opt_mod.WHEN_ALWAYS) else repr(when),
    )


def _frozen_panel_notes(
    report: Dict[str, Any], loaded: config_mod.LoadedConfig, installed: List[str]
) -> None:
    """A ``reviewers`` list that is the panel its file would inherit, plus more.

    The shape an older ``reviewer add`` left: the inherited panel copied in,
    then the addition. Moving the additions to ``reviewers_extra`` keeps them
    and lets the panel follow the fit again. A note, and never a rewrite.
    """
    if not loaded.preset:
        return
    files = (
        (loaded.global_path, loaded.global_layer, {"preset": loaded.preset}),
        (loaded.project_path, loaded.project_layer, loaded.global_layer),
    )
    for path, layer, below in files:
        listed = layer.get("reviewers")
        if not path or not isinstance(listed, list):
            continue
        # Dealt around the implementer the file sets, as its writers deal it.
        inherited = config_mod.compose(below, {}, installed, layer)[0].get("reviewers")
        if not isinstance(inherited, list):
            continue
        rest = [reviewer for reviewer in listed if isinstance(reviewer, dict)]
        matched = True
        for seat in inherited:
            if not isinstance(seat, dict):
                continue
            match = next((reviewer for reviewer in rest if _seat(reviewer) == _seat(seat)), None)
            if match is None:
                matched = False
                break
            rest.remove(match)
        if not matched or not rest:
            continue
        ids = ", ".join(str(reviewer.get("id")) for reviewer in rest)
        report["notes"].append(
            "reviewers in %s: holds the inherited panel plus %s; move %s to reviewers_extra and "
            "remove reviewers to keep following it" % (path, ids, ids)
        )


def _antigravity_live(
    report: Dict[str, Any],
    project_root: str,
    plugin_root: Optional[str] = None,
    home: Optional[str] = None,
) -> None:
    """An Antigravity install that is this checkout, and what else it would load.

    The installer refuses to link a checkout whose root holds something
    Antigravity loads on its own, but only at link time; a later checkout can
    add one, and it goes live on the next restart. A location is live when it
    resolves to this root: a link or junction to it, or the checkout itself
    sitting there. A copy elsewhere never does. A checkout registered through
    a plugins.json entry, or a copy staged by `agy plugin install`, is not
    checked: the docs say so.
    """
    plugin_root = PLUGIN_ROOT if plugin_root is None else plugin_root
    home = user_home() if home is None else home
    info: Dict[str, Any] = {"root": plugin_root, "live": [], "autoload": []}
    report["antigravity"] = info
    for _scope, location in hosts.antigravity_install_locations(project_root, home):
        if hosts.resolves_to(location, plugin_root):
            info["live"].append(location)
    if not info["live"]:
        return
    scan = hosts.antigravity_autoload(plugin_root)
    info["autoload"] = scan.entries
    report["problems"].extend(scan.errors)
    if not scan.entries:
        return
    shown = ", ".join("rules/" if entry == "rules" else entry for entry in scan.entries)
    for location in info["live"]:
        report["problems"].append(
            "Antigravity loads this checkout through %s and would also load %s on its next start; "
            "remove them, or replace it with a copy install (--copy, or -Copy in PowerShell)"
            % (location, shown)
        )


def _live_check_note(name: str, version: str, status: Dict[str, Any], report: Dict[str, Any]) -> None:
    """A CLI version that never went through scripts/smoke_live.py here.

    A CLI update is when its output or flags can drift from the adapter. Not
    for a failed check: whoever ran it has seen the failure. Not for a record
    inside the workspace either: the script would refuse to write it.
    """
    if status["status"] != "absent" or status.get("problem") == verified.SMOKE_INSIDE_WORKSPACE:
        return
    last = status.get("last_passed")
    script = '"%s"' % SMOKE_SCRIPT if " " in SMOKE_SCRIPT else SMOKE_SCRIPT
    report["notes"].append(
        "%s %s has not been live-checked on this machine (last passed: %s); "
        "run python %s --provider %s -- it spends a few real tokens"
        % (name, version, last["version"] if last else "never", script, name)
    )


def _default_patterns_note(report: Dict[str, Any], settings: Dict[str, Any]) -> None:
    """A high-risk reviewer judged by patterns nobody chose for this repository.

    A note, not a problem: the defaults can be exactly right. They fit the
    common names and miss a repository's own, and a reviewer that then almost
    never runs looks no different from one that has nothing to say. No pattern
    in force at all is a validation problem already.
    """
    conditional = [
        str(entry.get("id")) for entry in report["reviewers"] if entry.get("when") == opt_mod.WHEN_HIGH_RISK
    ]
    if not conditional or not opt_mod.default_patterns_only(settings):
        return
    report["notes"].append(
        "%s: when: high-risk, judged by the built-in high_risk_paths only. If this repository's "
        "sensitive paths have other names, add them to optimization.extra_high_risk_paths."
        % ", ".join(conditional)
    )


def _describe_role(
    label: str,
    spec: Any,
    detections: Dict[str, Any],
    problems: List[str],
    adapter_errors: Dict[str, str],
    load_errors: int,
    missing_hint: str = "",
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"label": label}
    if not isinstance(spec, dict):
        entry["status"] = "missing"
        problems.append("%s: not configured" % label)
        return entry
    provider_name = str(spec.get("provider") or "")
    entry["provider"] = provider_name
    model_spec = spec.get("model") or {}
    entry["family"] = model_spec.get("family", "")
    entry["version_policy"] = model_spec.get("version", "latest")

    # Reported before the CLI checks below: whether an option is going to be
    # ignored is a fact about the configuration, true whether or not the CLI
    # that would have honoured it happens to be installed.
    _describe_options(entry, spec, label, problems)

    detection = detections.get(provider_name)
    if detection is None and provider_name in adapter_errors:
        entry["status"] = "adapter-error"
        entry["error"] = adapter_errors[provider_name]
        problems.append(
            "%s: %s adapter failed during diagnosis (see the provider block above)" % (label, provider_name)
        )
        return entry
    if detection is None:
        entry["status"] = "unknown-provider"
        problem = "%s: unknown provider %r" % (label, provider_name)
        if load_errors:
            problem += "; %d user provider module(s) failed to load, see User providers" % load_errors
        if user_providers_disabled():
            problem += "; user adapters are disabled by %s" % USER_PROVIDERS_DISABLED_ENV
        problems.append(problem)
        return entry
    if not detection.installed:
        entry["status"] = "cli-missing"
        problem = "%s: %s CLI is not installed" % (label, provider_name)
        problems.append(problem + ("; %s" % missing_hint if missing_hint else ""))
        return entry

    try:
        resolved = get_provider(provider_name).resolve_model(model_spec)
        if not isinstance(resolved, ResolvedModel):
            raise TypeError("resolve_model() returned %s, not a ResolvedModel" % type(resolved).__name__)
    except ModelResolutionError as exc:
        entry["status"] = "unresolvable-model"
        entry["error"] = str(exc)
        problems.append("%s: %s" % (label, exc))
        return entry
    except Exception as exc:
        # A user adapter can raise anything from its resolution code; the
        # point of doctor is to say so rather than to fall over with it.
        entry["status"] = "adapter-error"
        entry["error"] = describe_exception(exc)
        problems.append("%s: %s adapter raised %s" % (label, provider_name, entry["error"]))
        return entry
    entry["status"] = "ok"
    entry["resolved"] = resolved.display
    entry["resolution_source"] = resolved.source
    return entry


def _describe_options(entry: Dict[str, Any], spec: Dict[str, Any], label: str, problems: List[str]) -> None:
    options = spec.get("options")
    if not isinstance(options, dict):
        return
    entry["options"] = {key: value for key, value in options.items() if key != "args"}
    ignored = sorted(set(options) & READ_ONLY_IGNORED_OPTIONS)
    if ignored and _is_read_only_role(label):
        entry["ignored_options"] = ignored
        problems.append(
            "%s: %s ignored -- planning and review stages always run read-only"
            % (label, ", ".join("options.%s" % key for key in ignored))
        )


#: Options that would loosen a sandbox. Harmless on the implementer and fixer,
#: silently overridden everywhere else -- so say so out loud instead.
READ_ONLY_IGNORED_OPTIONS = {"permission_mode", "sandbox", "approve", "skip_permissions"}


def _is_read_only_role(label: str) -> bool:
    return label.startswith(("Orchestrator", "Architect", "Reviewer"))


def _enforcement_report(
    label: str,
    spec: Any,
    report: Dict[str, Any],
    seat: Optional[Dict[str, Any]] = None,
    refused: bool = False,
) -> None:
    """A read-only seat on a provider whose read-only runs are refused, or warned about.

    Refused is a problem. Warned is a note -- the run goes ahead, from the
    global file or the global preset's fit -- unless the seat came with the
    project file, which ``read_only_arg_warnings`` has already made a problem
    of. Any other status, ``error`` included, is neither.
    """
    if not isinstance(spec, dict):
        return
    name = str(spec.get("provider") or "")
    entry = report["providers"].get(name) or {}
    enforcement = entry.get("read_only_enforcement") or {}
    status = enforcement.get("status")
    if status in REFUSED_ENFORCEMENT:
        report["problems"].append("%s: read-only runs are refused -- %s" % (label, enforcement.get("detail")))
    elif status in WARNED_ENFORCEMENT:
        if seat is not None:
            seat["read_only"] = status
        if not refused:
            report["notes"].append(
                "%s: read-only runs are NOT enforced by %s (allowed, warned) -- %s"
                % (label, name, enforcement.get("detail"))
            )


def _enforcement_line(enforcement: Dict[str, Any]) -> str:
    """How an enforcement status reads on the provider's line.

    ``partial`` carries its detail on the line itself: what it does not cover
    is the part a reader must not miss.
    """
    status = enforcement.get("status")
    mechanism = enforcement.get("mechanism") or ""
    detail = enforcement.get("detail") or ""
    if status == "verified":
        return "enforced by %s" % mechanism
    if status == "partial":
        return "enforced by %s; %s" % (mechanism, detail)
    if status == "unenforced":
        return "NOT ENFORCED (runs allowed, warned) -- %s" % detail
    if status == "unsupported":
        return "NOT ENFORCEABLE -- %s" % detail
    if status == "unverified":
        return "UNVERIFIED -- %s" % detail
    if status == "not-checked":
        return "not checked (--fast)"
    if status == "error":
        return "adapter error -- %s" % detail
    return "not reported by this adapter"


def _resume_line(name: str, support: Dict[str, Any]) -> str:
    """Whether ``run architect --resume`` continues a session on this CLI.

    Not a problem either way: a run that cannot resume runs fresh.
    """
    status = support.get("status")
    detail = support.get("detail") or ""
    if status == "verified":
        if not support.get("version"):
            return "verified (%s)" % detail
        where = "built-in" if support.get("source") == "built-in" else "record: %s" % _record(support)
        version, verified_at = support["version"], support.get("verified_at")
        return "verified for %s %s on %s (%s)" % (name, version, verified_at, where)
    if status == "trusted":
        where = "built-in" if support.get("source") == "built-in" else "record: %s" % _record(support)
        version, newer_than = support.get("version"), support.get("newer_than")
        smoke = "python scripts/smoke_live.py --provider %s" % name
        return "trusted for %s %s as newer than %s (verified on %s, %s); not verified itself -- run %s" % (
            name,
            version,
            newer_than,
            support.get("verified_at"),
            where,
            smoke,
        )
    if status == "unverified":
        return "UNVERIFIED -- %s; --resume runs fresh until then" % detail
    if status == "unsupported":
        return "NOT SUPPORTED -- %s" % detail
    if status == "not-checked":
        return "not checked (--fast)"
    return "not reported by this adapter"


def _record(support: Dict[str, Any]) -> str:
    return config_mod.describe_location(support.get("record"), support.get("record_real"))


def _live_check_line(version: Any, status: Dict[str, Any]) -> str:
    """Whether scripts/smoke_live.py has run this CLI version on this machine."""
    if status.get("status") == "version-unavailable":
        return "version unavailable"
    if status.get("problem"):
        return str(status["problem"])
    entry = status.get("entry") or {}
    day = str(entry.get("checked_at") or "")[:10]
    if status.get("status") == "passed":
        skipped = len(entry.get("skipped") or [])
        return "passed for %s on %s%s" % (version, day, ", %d skipped" % skipped if skipped else "")
    if status.get("status") == "failed":
        return "FAILED for %s on %s (%s)" % (version, day, ", ".join(entry.get("failed") or []))
    last = status.get("last_passed")
    if last:
        last_day = str(last.get("checked_at") or "")[:10]
        return "not run for %s (last passed: %s on %s)" % (version, last.get("version"), last_day)
    return "never run on this machine"


def render(report: Dict[str, Any]) -> str:
    lines = _environment_lines(report)
    for name, entry in report["providers"].items():
        lines += _provider_lines(name, entry)
    store = bool(report["platform"].get("store_python"))
    lines += _user_provider_lines(report.get("user_providers") or {}, store)
    lines.append("")
    lines += _config_lines(report["config"])
    lines += _roles_lines(report)
    lines += _closing_lines(report)
    return "\n".join(lines) + "\n"


def _environment_lines(report: Dict[str, Any]) -> List[str]:
    """The title and the platform, each followed by a blank line."""
    lines: List[str] = ["AI Development Orchestrator -- doctor", ""]
    platform_info = report["platform"]
    lines.append(
        "Environment: %s %s / Python %s%s"
        % (
            platform_info["system"],
            platform_info["release"],
            platform_info["python"],
            " (Microsoft Store package)" if platform_info.get("store_python") else "",
        )
    )
    lines.append("")
    return lines


def _provider_lines(name: str, entry: Dict[str, Any]) -> List[str]:
    """One provider's block, ending in a blank line."""
    lines = ["%s (%s)" % (entry.get("display_name", name), name)]
    if entry.get("adapter_error"):
        lines.append("  Installed: unknown (adapter failed)")
    else:
        lines.append("  Installed: %s" % ("yes" if entry.get("installed") else "no"))
    lines.append("  Source: %s" % describe_origin(name))
    if entry.get("adapter_error"):
        lines.append("  Adapter error: %s" % entry["adapter_error"])
    elif entry.get("installed"):
        lines.append("  Version: %s" % (entry.get("version") or "unknown"))
        lines.append(
            "  Authentication: %s (credential presence only, not verified)"
            % entry.get("authentication", "unknown")
        )
        lines.append("  Model selection: %s" % entry.get("model_selection", "unknown"))
        models = entry.get("models") or []
        if models:
            shown = ", ".join(m["label"] for m in models[:6])
            lines.append("  Models (%s): %s" % (entry.get("model_discovery", "?"), shown))
        if entry.get("read_only_enforcement"):
            lines.append("  Read-only runs: %s" % _enforcement_line(entry["read_only_enforcement"]))
        if entry.get("resume_support"):
            lines.append("  Resume: %s" % _resume_line(name, entry["resume_support"]))
        if entry.get("live_check"):
            live = _live_check_line(entry.get("version"), entry["live_check"])
            lines.append("  Live check: %s" % live)
    else:
        if entry.get("error"):
            lines.append("  Detail: %s" % entry["error"])
        # Only a static report reaches here: what an absent CLI's
        # enforcement would be is otherwise not known.
        enforcement = entry.get("read_only_enforcement") or {}
        if enforcement.get("status") not in (None, "not-checked"):
            lines.append("  Read-only runs: %s" % _enforcement_line(enforcement))
    # A user adapter's declaration, known whether or not its CLI is there.
    fit_note = (entry.get("preset_fit") or {}).get("note")
    if fit_note:
        lines.append("  Preset fitting: %s" % fit_note)
    lines.append("")
    return lines


def _config_lines(config_info: Dict[str, Any]) -> List[str]:
    """The config files, the preset in force and any pinned values, ending in a blank line."""
    lines = ["Config"]
    real = config_info.get("global_real")
    lines.append("  Global: %s" % config_mod.describe_location(config_info.get("global"), real))
    lines.append("  Project override: %s" % config_info.get("project_override"))
    preset = config_info.get("preset") or {}
    preset_name = preset.get("name")
    if preset:
        installed = preset.get("fitted_to") or []
        described = presets_mod.describe(preset_name, preset.get("source") or "", installed)
        lines.append("  Preset: %s" % described)
    if config_info.get("using_builtin_defaults"):
        source = "built-in defaults, fitted as preset %s" % (preset_name or presets_mod.DEFAULT)
        lines.append("  Source: %s (`config setup --preset <name>` saves one)" % source)
    pinned = config_info.get("pinned") or []
    if pinned:
        lines.append("  Pinned at a value the built-in default has moved off:")
        for entry in pinned:
            lines.append("    %-40s %s (default %s)" % (entry["setting"], entry["value"], entry["default"]))
        lines.append("    Deliberate choices look the same as values inherited from an")
        lines.append("    older default, so these are reported and never rewritten.")
        lines.append("    config prune drops the values equal to the current default, on request.")
    lines.append("")
    return lines


def _roles_lines(report: Dict[str, Any]) -> List[str]:
    """Each role, its options, and the reviewer panel."""
    lines = ["Roles"]
    for key, _label in ROLE_LABELS:
        entry = report["roles"].get(key, {})
        lines.append("  %-13s %s" % (entry.get("label", key) + ":", _role_line(entry)))
    for key, _label in ROLE_LABELS:
        entry = report["roles"].get(key, {})
        if entry.get("options"):
            lines.append("      %s options: %s" % (key, entry["options"]))
    reviewers = report["reviewers"]
    lines.append("  %-13s %d" % ("Reviewers:", len(reviewers)))
    for index, entry in enumerate(reviewers, 1):
        role = entry.get("role", "general")
        if entry.get("when"):
            role += " (when: %s)" % entry.get("condition", entry["when"])
        origin = str(entry.get("origin") or "")
        if origin.endswith(" extra"):
            role += " (extra: %s)" % origin[: -len(" extra")]
        lines.append("    %d. %s / %s / %s" % (index, entry.get("id"), _role_line(entry), role))
    # Only once a file sets a design panel: without one a design round runs
    # the panel listed above, and saying so on every report is noise. Read
    # with .get, as a report built before the design panel had neither key.
    design = report.get("design_reviewers") or []
    source = str(report.get("design_panel_source") or "code")
    if source != "code" or any("status" in entry for entry in design):
        lines.append("  %-13s %d (%s)" % ("Design:", len(design), DESIGN_PANEL_SOURCES.get(source, source)))
        for index, entry in enumerate(design, 1):
            # A seat copied from the code panel is that seat, diagnosed above.
            seat = _role_line(entry) if "status" in entry else "as in Reviewers"
            role = entry.get("role", "general")
            lines.append("    %d. %s / %s / %s" % (index, entry.get("id"), seat, role))
    return lines


def _closing_lines(report: Dict[str, Any]) -> List[str]:
    """The problems, or that there are none, then any notes; each after a blank line."""
    lines: List[str] = []
    if report["problems"]:
        lines += ["", "Problems"]
        for problem in report["problems"]:
            lines.append("  - %s" % problem)
    else:
        lines += ["", "No problems found."]
    if report.get("notes"):
        lines += ["", "Notes"]
        for note in report["notes"]:
            lines.append("  - %s" % note)
    return lines


def _user_provider_lines(info: Dict[str, Any], store: bool = False) -> List[str]:
    """Always shown, so the extension point and its path are never a secret.

    ``store``: a Microsoft Store Python, whose private copy is merged with the
    directory; any other redirect, such as a link, simply moves it.
    """
    lines = ["User providers"]
    directory = info.get("directory")
    real = info.get("directory_real")
    if not info.get("enabled", True):
        lines.append("  Directory: %s" % config_mod.describe_location(directory, real))
        lines.append("  Disabled by %s; nothing imported" % USER_PROVIDERS_DISABLED_ENV)
        return lines
    if not info.get("present"):
        shown = config_mod.describe_location(directory, real)
        lines.append("  Directory: %s (not present; nothing imported)" % shown)
        return lines
    if real is not None and store:
        merged = "merged with this Python's private copy at %s (the private copy wins for a name in both)"
        lines.append("  Directory: %s, %s" % (directory, merged % real))
    else:
        lines.append("  Directory: %s" % config_mod.describe_location(directory, real))
    lines.append("  Code in this directory is imported at startup, from outside the plugin.")
    loaded = info.get("loaded") or []
    if not loaded:
        lines.append("  Imported: none")
    for item in loaded:
        path = config_mod.describe_location(item["path"], item.get("path_real"))
        lines.append("  Imported: %s  <- %s" % (item["name"], path))
    for failure in info.get("errors") or []:
        path = config_mod.describe_location(failure["path"], failure.get("path_real"))
        lines.append("  Failed:   %s -- %s" % (path, failure["error"]))
    return lines


def _role_line(entry: Dict[str, Any]) -> str:
    if entry.get("status") == "ok":
        return "%s / %s / %s" % (
            entry.get("provider"),
            entry.get("family") or "default",
            entry.get("version_policy", "latest"),
        )
    return "%s / %s (%s)" % (
        entry.get("provider", "?"),
        entry.get("family") or "default",
        entry.get("status", "unknown"),
    )


def exit_code(report: Dict[str, Any]) -> int:
    return 1 if report.get("problems") else 0
