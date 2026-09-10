"""Environment diagnostics.

Reports what is installed, what is configured, and whether each configured role
can actually be resolved to a model. Deliberately reports authentication as a
*state* ("available" / "unknown") and never echoes a credential value.
"""

from __future__ import annotations

import platform
from typing import Any, Dict, List, Optional

from . import config as config_mod
from .providers import ModelResolutionError, available_providers, get_provider

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
    }

    detections = {}
    for name in available_providers():
        provider = get_provider(name)
        detection = provider.detect()
        detections[name] = detection
        entry = detection.to_dict()
        entry["display_name"] = provider.display_name
        entry["model_selection"] = "supported"
        if probe_models and detection.installed:
            candidates = provider.list_models()
            entry["models"] = [candidate.to_dict() for candidate in candidates]
            entry["model_discovery"] = candidates[0].source if candidates else "none"
        report["providers"][name] = entry

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
    }
    report["problems"].extend(problems)

    for key, label in ROLE_LABELS:
        spec = loaded.data.get(key)
        report["roles"][key] = _describe_role(label, spec, detections, report["problems"])

    for reviewer in loaded.reviewers():
        entry = _describe_role("Reviewer %s" % reviewer.get("id"), reviewer, detections, report["problems"])
        entry["id"] = reviewer.get("id")
        entry["role"] = reviewer.get("role", "general")
        report["reviewers"].append(entry)

    if not report["reviewers"]:
        report["problems"].append("no reviewers configured: the independent-review stage will be skipped")
    return report


def _describe_role(label: str, spec: Any, detections: Dict[str, Any], problems: List[str]) -> Dict[str, Any]:
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

    detection = detections.get(provider_name)
    if detection is None:
        entry["status"] = "unknown-provider"
        problems.append("%s: unknown provider %r" % (label, provider_name))
        return entry
    if not detection.installed:
        entry["status"] = "cli-missing"
        problems.append("%s: %s CLI is not installed" % (label, provider_name))
        return entry

    try:
        resolved = get_provider(provider_name).resolve_model(model_spec)
    except ModelResolutionError as exc:
        entry["status"] = "unresolvable-model"
        entry["error"] = str(exc)
        problems.append("%s: %s" % (label, exc))
        return entry
    entry["status"] = "ok"
    entry["resolved"] = resolved.display
    entry["resolution_source"] = resolved.source
    return entry


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
        lines.append("  Installed: %s" % ("yes" if entry.get("installed") else "no"))
        if entry.get("installed"):
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
        elif entry.get("error"):
            lines.append("  Detail: %s" % entry["error"])
        lines.append("")

    config_info = report["config"]
    lines.append("Config")
    lines.append("  Global: %s" % config_info.get("global"))
    lines.append("  Project override: %s" % config_info.get("project_override"))
    if config_info.get("using_builtin_defaults"):
        lines.append("  Source: built-in defaults (run `config setup` to save your own)")
    lines.append("")

    lines.append("Roles")
    for key, _label in ROLE_LABELS:
        entry = report["roles"].get(key, {})
        lines.append("  %-13s %s" % (entry.get("label", key) + ":", _role_line(entry)))
    reviewers = report["reviewers"]
    lines.append("  %-13s %d" % ("Reviewers:", len(reviewers)))
    for index, entry in enumerate(reviewers, 1):
        lines.append(
            "    %d. %s / %s / %s" % (index, entry.get("id"), _role_line(entry), entry.get("role", "general"))
        )

    if report["problems"]:
        lines += ["", "Problems"]
        for problem in report["problems"]:
            lines.append("  - %s" % problem)
    else:
        lines += ["", "No problems found."]
    return "\n".join(lines) + "\n"


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
