"""The configuration summary the wizard and ``config show`` print."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, cast

from . import config as config_mod
from .optimization import WHEN_ALWAYS, condition_label, model_label, skip_unneeded_roles

ROLE_TITLES = (
    ("orchestrator", "Orchestrator"),
    ("architect", "Architect"),
    ("implementer", "Implementer"),
    ("review_fixer", "Review Fixer"),
)


#: How a listing says where the design panel's base comes from, by
#: ``LoadedConfig.design_panel_source``.
DESIGN_PANEL_SOURCES = {
    "code": "the code panel; when conditions ignored",
    "fit": "the preset's fit",
    "global": "global file",
    "project": "project file",
}


def seat_notes(reviewer: Dict[str, Any]) -> str:
    """`` (opus when high-risk)`` and `` (relevance: always)`` where a seat sets them."""
    notes = ""
    risk_model = reviewer.get("high_risk_model")
    if isinstance(risk_model, dict):
        notes += " (%s when high-risk)" % model_label(risk_model)
    relevance = reviewer.get("relevance")
    if relevance is not None:
        notes += " (relevance: %s)" % relevance
    return notes


def _panel_lines(reviewers: Any, origins: Sequence[config_mod.ReviewerOrigin]) -> List[str]:
    """One line per mapping reviewer, numbered by its place in the panel."""
    # A panel that is not a list, or holds no mapping, shows as none: the
    # problems `config validate` reports name what is wrong with it.
    if not isinstance(reviewers, list):
        reviewers = []
    lines: List[str] = []
    if not any(isinstance(reviewer, dict) for reviewer in reviewers):
        lines.append("    (none configured)")
    for index, reviewer in enumerate(reviewers, 1):
        # Skipped but counted, so the numbers and origins stay aligned.
        if not isinstance(reviewer, dict):
            continue
        when = condition_label(reviewer)
        origin = origins[index - 1] if index <= len(origins) else None
        mark = ""
        if origin is not None and origin.extra:
            mark = " (extra, %s file)" % origin.layer
        lines.append(
            "    %d. %s / %s / %s%s%s%s"
            % (
                index,
                _describe(reviewer),
                reviewer.get("role", "general"),
                reviewer.get("id"),
                "" if when == WHEN_ALWAYS else " (when: %s)" % when,
                seat_notes(reviewer),
                mark,
            )
        )
    return lines


def render_summary(
    data: Dict[str, Any],
    origins: Sequence[config_mod.ReviewerOrigin] = (),
    design_origins: Sequence[config_mod.ReviewerOrigin] = (),
    loaded: Optional[config_mod.LoadedConfig] = None,
) -> str:
    """``origins``, parallel to the panel, marks each extra with the file it came from;
    ``design_origins`` does the same for the design panel, when a file sets one.
    ``loaded``, the configuration ``data`` was loaded as, adds where each
    deadline was set; a preview has no files to name."""
    # lazy: config_policy loads the provider registry, which runs the user adapters;
    # summary must import without it
    from .config_policy import read_only_enforcement_warnings

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
    lines.append("  Reviews")
    lines.extend(_panel_lines(data.get("reviewers"), origins))
    # Shown rather than asked: the wizard settles who does which job, and this
    # is a behaviour knob like `max_review_iterations`. But it decides whether
    # a whole stage runs, so leaving it out of the summary entirely would make
    # it the one stage nobody can see the state of.
    lines.append("    design review: %s  (review.design.enabled)" % _design_review_mode(data))
    lines.append("    optimization level: %s  (optimization.level)" % _optimization_level(data))
    lines.append(
        "    skip unneeded roles: %s  (optimization.skip_unneeded_roles)"
        % ("on" if _skip_unneeded_roles(data) else "off")
    )
    lines.append(
        "    plan approval: %s  (design.require_approval)"
        % ("required" if _approval_required(data) else "not required")
    )
    # The design panel, under its own heading: which seats a design round
    # runs is as much a part of the setup as who reviews the code.
    design = config_mod.get_path(data, config_mod.DESIGN_PANEL.reviewers)
    lines.append("  Design reviews")
    if design is None:
        lines.append("    (the code panel; when conditions ignored)  (review.design.reviewers)")
    else:
        lines.extend(_panel_lines(design, design_origins))
    lines.extend(_deadline_lines(data, loaded))
    lines.append("  Reply language: %s  (language.reply)" % _reply_language(data))
    # With no origins (data never composed by ``load``), a design seat that
    # runs as its code seat is taken for a copy, as ``_reviewer_seats`` says.
    for warning in read_only_enforcement_warnings(data, design_origins=list(design_origins) or None):
        lines.append("  Warning: %s" % warning)
    lines.append("")
    return "\n".join(lines)


def _deadline_lines(data: Dict[str, Any], loaded: Optional[config_mod.LoadedConfig]) -> List[str]:
    """Each role's run deadline, then the reviewers', with the layer that set each when it is known.

    Shown because the two keys split: a reviewer's deadline no longer says
    anything about a run's, and a cap somebody set on one is not on the other.
    """
    known = loaded if loaded is not None else config_mod.LoadedConfig(data, None, None, False)
    lines = ["  Deadlines"]
    for key, title in ROLE_TITLES:
        deadline = config_mod.run_timeout(known, key)
        source = " (%s)" % deadline.source if loaded is not None else ""
        lines.append("    %s: %ds%s  (run.timeout_seconds.%s)" % (title, deadline.seconds, source, key))
    review = config_mod.review_timeout(known)
    source = " (%s)" % review.source if loaded is not None else ""
    lines.append("    Reviewers: %ds%s  (review.timeout_seconds)" % (review.seconds, source))
    return lines


def _skip_unneeded_roles(data: Dict[str, Any]) -> bool:
    """Falls back to the built-in default: a layer may name no `optimization`."""
    optimization = data.get("optimization")
    return skip_unneeded_roles(optimization if isinstance(optimization, dict) else {})


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


def _reply_language(data: Dict[str, Any]) -> str:
    """The tag, with the Stop-hook check's state when it is off; ``not set`` without one."""
    settings = config_mod.language_settings_of(data)
    if not settings["reply"]:
        return "not set"
    return settings["reply"] + ("" if settings["rewrite"] else " (no rewrite: language.rewrite false)")


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
