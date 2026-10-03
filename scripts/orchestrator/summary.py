"""The configuration summary the wizard and ``config show`` print."""

from __future__ import annotations

from typing import Any, Dict, Sequence, cast

from . import config as config_mod
from .optimization import WHEN_ALWAYS, condition_label

ROLE_TITLES = (
    ("orchestrator", "Orchestrator"),
    ("architect", "Architect"),
    ("implementer", "Implementer"),
    ("review_fixer", "Review Fixer"),
)


def render_summary(data: Dict[str, Any], origins: Sequence[config_mod.ReviewerOrigin] = ()) -> str:
    """``origins``, parallel to the panel, marks each extra with the file it came from."""
    lines = ["Configuration", ""]
    for key, title in ROLE_TITLES:
        spec = data.get(key) or {}
        lines.append("  %s" % title)
        lines.append("    %s" % _describe(spec))
        # Shown because a tier is invisible until somebody routes work to it,
        # and an unused tier is usually one nobody remembered was there.
        tiers = spec.get("model_tiers") if isinstance(spec, dict) else None
        for name in sorted(tiers) if isinstance(tiers, dict) else []:
            entry = cast(Dict[str, Any], tiers)[name]
            described = _describe(merged(spec, entry)) if isinstance(entry, dict) else "(invalid)"
            lines.append("      --tier %-10s %s" % (name, described))
    # A panel that is not a list, or holds no mapping, shows as none: the
    # problems `config validate` reports name what is wrong with it.
    reviewers = data.get("reviewers")
    if not isinstance(reviewers, list):
        reviewers = []
    lines.append("  Reviews")
    if not any(isinstance(reviewer, dict) for reviewer in reviewers):
        lines.append("    (none configured)")
    for index, reviewer in enumerate(reviewers, 1):
        # Skipped but counted, so the numbers and origins stay aligned.
        if not isinstance(reviewer, dict):
            continue
        when = condition_label(reviewer)
        origin = origins[index - 1] if index <= len(origins) else None
        mark = ""
        if origin is not None and origin.key == "reviewers_extra":
            mark = " (extra, %s file)" % origin.layer
        lines.append(
            "    %d. %s / %s / %s%s%s"
            % (
                index,
                _describe(reviewer),
                reviewer.get("role", "general"),
                reviewer.get("id"),
                "" if when == WHEN_ALWAYS else " (when: %s)" % when,
                mark,
            )
        )
    # Shown rather than asked: the wizard settles who does which job, and this
    # is a behaviour knob like `max_review_iterations`. But it decides whether
    # a whole stage runs, so leaving it out of the summary entirely would make
    # it the one stage nobody can see the state of.
    lines.append("    design review: %s  (review.design.enabled)" % _design_review_mode(data))
    lines.append("    optimization level: %s  (optimization.level)" % _optimization_level(data))
    lines.append(
        "    plan approval: %s  (design.require_approval)"
        % ("required" if _approval_required(data) else "not required")
    )
    for warning in config_mod.read_only_enforcement_warnings(data):
        lines.append("  Warning: %s" % warning)
    lines.append("")
    return "\n".join(lines)


def _design_review_mode(data: Dict[str, Any]) -> str:
    """``on``, ``off`` or ``auto``; falls back to the built-in default, since a
    layer may name no `review` at all."""
    review = data.get("review")
    design = review.get("design") if isinstance(review, dict) else None
    if isinstance(design, dict) and "enabled" in design:
        return config_mod.design_review_mode(design["enabled"])
    return config_mod.design_review_mode(config_mod.default_config()["review"]["design"]["enabled"])


def _optimization_level(data: Dict[str, Any]) -> str:
    """Falls back to the built-in default: a layer may name no `optimization`."""
    optimization = data.get("optimization")
    level = optimization.get("level") if isinstance(optimization, dict) else None
    return str(level or config_mod.default_config()["optimization"]["level"])


def _approval_required(data: Dict[str, Any]) -> bool:
    """Falls back to the built-in default: a layer may name no `design` at all,
    or name the key with no value, which means the default as it does in
    ``LoadedConfig.design_settings``."""
    design = data.get("design")
    if isinstance(design, dict) and design.get("require_approval") is not None:
        return bool(design["require_approval"])
    return bool(config_mod.default_config()["design"]["require_approval"])


def merged(spec: Dict[str, Any], tier: Dict[str, Any]) -> Dict[str, Any]:
    """The role ``spec`` with ``tier`` merged over it, as ``config.merge_tier`` resolves a tier."""
    return config_mod.merge_tier(spec, tier)


def _describe(spec: Dict[str, Any]) -> str:
    """provider / family / version, the way `config show` says it."""
    model = spec.get("model") or {}
    version = model.get("version", "latest")
    family = model.get("id") if version == "pinned" else model.get("family", "default")
    return "%s / %s / %s" % (spec.get("provider", "?"), family or "default", version)
