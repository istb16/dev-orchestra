"""Environment diagnostics.

Reports what is installed, what is configured, and whether each configured role
can actually be resolved to a model. Deliberately reports authentication as a
*state* ("available" / "unknown") and never echoes a credential value.
"""

from __future__ import annotations

import os
import platform
from typing import Any, Dict, List, Optional

from . import config as config_mod
from . import optimization as opt_mod
from . import workspace as ws
from .providers import (
    REFUSED_ENFORCEMENT,
    USER_PROVIDERS_DISABLED_ENV,
    ModelResolutionError,
    ResolvedModel,
    available_providers,
    describe_exception,
    describe_origin,
    get_provider,
    origin_payload,
    redact,
    user_provider_report,
    user_providers_disabled,
)

ROLE_LABELS = (
    ("orchestrator", "Orchestrator"),
    ("architect", "Architect"),
    ("implementer", "Implementer"),
    ("review_fixer", "Review fixer"),
)


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

    detections = {}
    adapter_errors: Dict[str, str] = {}
    # A resume record inside this directory is not trusted; see verified.py.
    # The repository root, as `run` uses, or a subdirectory would trust a
    # record that `run` refuses.
    root = ws.repo_root(start or os.getcwd())
    for name in available_providers():
        # One adapter at a time: a user adapter that raises is reported against
        # its file instead of taking the whole diagnosis down with it.
        try:
            provider = get_provider(name)
            detection = provider.detect()
            entry = detection.to_dict()
            entry["display_name"] = provider.display_name
            entry["model_selection"] = "supported"
            if probe_models and detection.installed:
                candidates = provider.list_models()
                entry["models"] = [candidate.to_dict() for candidate in candidates]
                entry["model_discovery"] = candidates[0].source if candidates else "none"
                entry["read_only_enforcement"] = dict(provider.read_only_enforcement())
                entry["resume_support"] = dict(provider.resume_support(root))
            elif not probe_models:
                # --fast skips this probe. It does not promise that no --help
                # is read: validating options.permission_mode reads one.
                entry["read_only_enforcement"] = {"status": "not-checked"}
                entry["resume_support"] = {"status": "not-checked"}
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
        report["config"] = {"status": "error", "error": str(exc)}
        report["problems"].append(str(exc))
        return report

    problems = config_mod.validate(loaded.data)
    report["config"] = {
        "status": "ok" if not problems else "invalid",
        "global": loaded.global_path or "not found",
        "project_override": loaded.project_path or "none",
        "using_builtin_defaults": loaded.used_defaults,
        "problems": problems,
        # Reported so a default that has since been improved is visible rather
        # than silently overridden by the file that recorded the old one.
        "pinned": config_mod.pinned_differences(loaded.data),
    }
    report["problems"].extend(problems)
    # Problems for doctor, warnings for `config validate`: either way these
    # runs are refused, and --strict should say so before one is attempted.
    warnings = config_mod.read_only_arg_warnings(loaded)
    report["config"]["warnings"] = warnings
    report["problems"].extend(warnings)

    for key, label in ROLE_LABELS:
        spec = loaded.data.get(key)
        report["roles"][key] = _describe_role(
            label, spec, detections, report["problems"], adapter_errors, load_errors
        )
        if key in config_mod.READ_ONLY_ROLES:
            _refused_enforcement(label, spec, report)
            # A tier can switch provider, and its runs are refused by that
            # provider's enforcement, not the base role's. One that keeps the
            # provider is refused for the same reason the role already was, so
            # it is not reported a second time.
            tiers = spec.get("model_tiers") if isinstance(spec, dict) else None
            tier_items = tiers.items() if isinstance(tiers, dict) else ()
            for tier, tier_entry in tier_items:
                if isinstance(tier_entry, dict):
                    merged = config_mod.merge_tier(spec, tier_entry)
                    if merged.get("provider") == spec.get("provider"):
                        continue
                    tier_label = "%s (tier %s)" % (label, tier)
                    _refused_enforcement(tier_label, merged, report)

    for reviewer in loaded.reviewers():
        label = "Reviewer %s" % reviewer.get("id")
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
        report["reviewers"].append(entry)
        _refused_enforcement(label, reviewer, report)

    if not report["reviewers"]:
        report["problems"].append("no reviewers configured: the independent-review stage will be skipped")
    _default_patterns_note(report, loaded.optimization_settings())
    return report


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
        problems.append("%s: %s CLI is not installed" % (label, provider_name))
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
READ_ONLY_IGNORED_OPTIONS = {"permission_mode", "sandbox", "approve"}


def _is_read_only_role(label: str) -> bool:
    return label.startswith(("Orchestrator", "Architect", "Reviewer"))


def _refused_enforcement(label: str, spec: Any, report: Dict[str, Any]) -> None:
    """A read-only role on a provider whose read-only runs will be refused."""
    if not isinstance(spec, dict):
        return
    entry = report["providers"].get(str(spec.get("provider") or "")) or {}
    enforcement = entry.get("read_only_enforcement") or {}
    if enforcement.get("status") in REFUSED_ENFORCEMENT:
        report["problems"].append("%s: read-only runs are refused -- %s" % (label, enforcement.get("detail")))


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
    if status == "unsupported":
        return "NOT ENFORCEABLE -- %s" % detail
    if status == "unverified":
        return "UNVERIFIED -- %s" % detail
    if status == "not-checked":
        return "not checked (--fast)"
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
        where = "built-in" if support.get("source") == "built-in" else "record: %s" % support.get("record")
        version, verified_at = support["version"], support.get("verified_at")
        return "verified for %s %s on %s (%s)" % (name, version, verified_at, where)
    if status == "unverified":
        return "UNVERIFIED -- %s; --resume runs fresh until then" % detail
    if status == "unsupported":
        return "NOT SUPPORTED -- %s" % detail
    if status == "not-checked":
        return "not checked (--fast)"
    return "not reported by this adapter"


def render(report: Dict[str, Any]) -> str:
    lines: List[str] = ["AI Development Orchestrator -- doctor", ""]
    platform_info = report["platform"]
    lines.append(
        "Environment: %s %s / Python %s"
        % (platform_info["system"], platform_info["release"], platform_info["python"])
    )
    lines.append("")

    for name, entry in report["providers"].items():
        lines.append("%s (%s)" % (entry.get("display_name", name), name))
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
        elif entry.get("error"):
            lines.append("  Detail: %s" % entry["error"])
        lines.append("")

    lines += _user_provider_lines(report.get("user_providers") or {})
    lines.append("")

    config_info = report["config"]
    lines.append("Config")
    lines.append("  Global: %s" % config_info.get("global"))
    lines.append("  Project override: %s" % config_info.get("project_override"))
    if config_info.get("using_builtin_defaults"):
        lines.append("  Source: built-in defaults (run `config setup` to save your own)")
    pinned = config_info.get("pinned") or []
    if pinned:
        lines.append("  Pinned at a value the built-in default has moved off:")
        for entry in pinned:
            lines.append("    %-40s %s (default %s)" % (entry["setting"], entry["value"], entry["default"]))
        lines.append("    Deliberate choices look the same as values inherited from an")
        lines.append("    older default, so these are reported and never rewritten.")
        lines.append("    config prune drops the values equal to the current default, on request.")
    lines.append("")

    lines.append("Roles")
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
        lines.append("    %d. %s / %s / %s" % (index, entry.get("id"), _role_line(entry), role))

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
    return "\n".join(lines) + "\n"


def _user_provider_lines(info: Dict[str, Any]) -> List[str]:
    """Always shown, so the extension point and its path are never a secret."""
    lines = ["User providers"]
    directory = info.get("directory")
    if not info.get("enabled", True):
        lines.append("  Directory: %s" % directory)
        lines.append("  Disabled by %s; nothing imported" % USER_PROVIDERS_DISABLED_ENV)
        return lines
    if not info.get("present"):
        lines.append("  Directory: %s (not present; nothing imported)" % directory)
        return lines
    lines.append("  Directory: %s" % directory)
    lines.append("  Code in this directory is imported at startup, from outside the plugin.")
    loaded = info.get("loaded") or []
    if not loaded:
        lines.append("  Imported: none")
    for item in loaded:
        lines.append("  Imported: %s  <- %s" % (item["name"], item["path"]))
    for failure in info.get("errors") or []:
        lines.append("  Failed:   %s -- %s" % (failure["path"], failure["error"]))
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
