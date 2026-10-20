"""The shape of a reviewer panel, and the edits a command makes to one.

``config`` re-exports its public names.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import optimization as opt_mod
from .config_common import ConfigError
from .config_keys import get_path, set_path


class PanelKeys(NamedTuple):
    """Where one reviewer panel is written in a file."""

    #: The dotted key of the panel's list.
    reviewers: str
    #: The dotted key of the reviewers added to whatever panel is inherited.
    extras: str


#: The code review panel, and the design review's own.
CODE_PANEL = PanelKeys("reviewers", "reviewers_extra")
DESIGN_PANEL = PanelKeys("review.design.reviewers", "review.design.reviewers_extra")


class ReviewerOrigin(NamedTuple):
    """Where one reviewer of the effective panel was written."""

    #: ``default`` (the fit, or the built-in panel), ``global`` or ``project``.
    layer: str
    #: ``reviewers`` or ``reviewers_extra``, or the design panel's
    #: ``review.design.reviewers`` or ``review.design.reviewers_extra``.
    key: str
    #: The index in that file's list. Not ``index``, which ``tuple`` owns.
    position: int

    @property
    def extra(self) -> bool:
        """Whether this reviewer is one of a file's extras, of either panel."""
        return self.key in (CODE_PANEL.extras, DESIGN_PANEL.extras)

    @property
    def design(self) -> bool:
        """Whether this reviewer was written under ``review.design``."""
        return self.key in DESIGN_PANEL

    def label(self) -> str:
        """``fit``, ``global``, ``project``, ``global extra`` or ``project extra``.

        A design panel's own entries read ``global design``, ``project design
        extra`` and so on; a seat it copied from the code panel keeps that
        seat's label.
        """
        if self.design:
            if self.extra:
                return "%s design extra" % self.layer
            return "%s design" % ("fit" if self.layer == "default" else self.layer)
        if self.extra:
            return "%s extra" % self.layer
        return "fit" if self.layer == "default" else self.layer


def default_reviewer_family(provider: str) -> str:
    """The family a new reviewer on ``provider`` gets when none is named."""
    return {"claude": "opus", "agy": "default"}.get(provider, "recommended-coding")


def make_reviewer(
    reviewer_id: str,
    provider: str,
    family: Optional[str],
    role: str = "general",
    version: str = "latest",
    model_id: Optional[str] = None,
    when: Optional[str] = None,
    paths: Optional[Sequence[str]] = None,
    high_risk_family: Optional[str] = None,
    relevance: Optional[str] = None,
) -> Dict[str, Any]:
    reviewer: Dict[str, Any] = {"id": reviewer_id, "provider": provider}
    model: Dict[str, Any] = {}
    if family:
        model["family"] = family
    model["version"] = version
    if model_id:
        model["id"] = model_id
    reviewer["model"] = model
    # Each written only when it says something, so a panel with no
    # conditional reviewer stays byte-identical to the one the defaults describe.
    if high_risk_family:
        reviewer["high_risk_model"] = {"family": high_risk_family, "version": "latest"}
    reviewer["role"] = role
    if relevance:
        reviewer["relevance"] = relevance
    if paths:
        reviewer["when"] = {"paths": list(paths)}
    elif when and when != opt_mod.WHEN_ALWAYS:
        reviewer["when"] = when
    return reviewer


def add_reviewer(
    data: Dict[str, Any], reviewer: Dict[str, Any], panel_keys: PanelKeys = CODE_PANEL
) -> Dict[str, Any]:
    reviewers = get_path(data, panel_keys.reviewers)
    if reviewers is None:
        reviewers = []
        set_path(data, panel_keys.reviewers, reviewers)
    if not isinstance(reviewers, list):
        raise ConfigError("%s: must be a list" % panel_keys.reviewers)
    if any(isinstance(item, dict) and item.get("id") == reviewer.get("id") for item in reviewers):
        raise ConfigError("reviewer id %r already exists" % reviewer.get("id"))
    reviewers.append(reviewer)
    return data


def add_extra_reviewer(
    data: Dict[str, Any], reviewer: Dict[str, Any], panel_keys: PanelKeys = CODE_PANEL
) -> Dict[str, Any]:
    """``add_reviewer`` for a panel's extras: beside the inherited panel, not instead of it."""
    extras = get_path(data, panel_keys.extras)
    if extras is None:
        extras = []
        set_path(data, panel_keys.extras, extras)
    if not isinstance(extras, list):
        raise ConfigError("%s: must be a list" % panel_keys.extras)
    if any(isinstance(item, dict) and item.get("id") == reviewer.get("id") for item in extras):
        raise ConfigError("reviewer id %r already exists" % reviewer.get("id"))
    extras.append(reviewer)
    return data


def remove_reviewer(
    data: Dict[str, Any], selector: str, panel_keys: PanelKeys = CODE_PANEL
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Remove a reviewer by id, by ``role`` name, or by 1-based position."""
    reviewers = get_path(data, panel_keys.reviewers) or []
    if not isinstance(reviewers, list):
        raise ConfigError("%s: must be a list" % panel_keys.reviewers)

    index = _find_reviewer_index(reviewers, selector)
    removed = reviewers.pop(index)
    set_path(data, panel_keys.reviewers, reviewers)
    return data, removed


def find_reviewer(
    data: Dict[str, Any], selector: str, panel_keys: PanelKeys = CODE_PANEL
) -> Tuple[int, Dict[str, Any]]:
    reviewers = get_path(data, panel_keys.reviewers) or []
    index = _find_reviewer_index(reviewers, selector)
    return index, reviewers[index]


def _find_reviewer_index(reviewers: List[Any], selector: str) -> int:
    for index, reviewer in enumerate(reviewers):
        if isinstance(reviewer, dict) and reviewer.get("id") == selector:
            return index
    if selector.isdigit():
        position = int(selector)
        if 1 <= position <= len(reviewers):
            return position - 1
        raise ConfigError("reviewer position %s is out of range (1-%d)" % (selector, len(reviewers)))
    matches = [
        index
        for index, reviewer in enumerate(reviewers)
        if isinstance(reviewer, dict) and reviewer.get("role") == selector
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ConfigError(
            "%d reviewers have role %r; remove by id instead (%s)"
            % (len(matches), selector, ", ".join(str(reviewers[i].get("id")) for i in matches))
        )
    raise ConfigError("no reviewer matches %r" % selector)


def suggest_reviewer_id(data: Dict[str, Any], provider: str, role: str) -> str:
    """Build a unique, readable reviewer id such as ``codex-security-2``."""
    base = "%s-%s" % (provider, re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-") or "general")
    existing = {r.get("id") for r in (data.get("reviewers") or []) if isinstance(r, dict)}
    if base not in existing:
        return base
    counter = 2
    while "%s-%d" % (base, counter) in existing:
        counter += 1
    return "%s-%d" % (base, counter)
