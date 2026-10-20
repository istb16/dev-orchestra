"""Every run a configuration would make: each role, each of its tiers and each reviewer.

``config`` re-exports its public names.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

from .config_common import READ_ONLY_ROLES
from .config_keys import get_path
from .config_reviewers import DESIGN_PANEL, ReviewerOrigin


def merge_tier(spec: Dict[str, Any], tier: Dict[str, Any]) -> Dict[str, Any]:
    """A role spec with one tier applied.

    Each key the tier sets replaces the role's, whole. It is not a deep
    merge: a tier naming a family over a base pinned to an id would otherwise
    inherit the pin and run a model nobody asked for, which is exactly the
    mistake this feature exists to avoid making by hand.

    Changing provider drops what belonged to the old one -- its options are
    not portable, and its model family usually is not either. A tier that
    wants a specific model on the new provider says so.
    """
    merged = {key: copy.deepcopy(value) for key, value in spec.items() if key != "model_tiers"}
    switched = bool(tier.get("provider")) and tier.get("provider") != spec.get("provider")
    if switched:
        merged.pop("model", None)
        merged.pop("options", None)
    for key in ("provider", "model", "options"):
        if key in tier:
            merged[key] = copy.deepcopy(tier[key])
    return merged


class SeatRun(NamedTuple):
    """One run a role, one of its tiers or a reviewer would make."""

    #: ``role``, ``tier`` or ``reviewer``: what every consumer dispatches on.
    kind: str
    #: ``architect``, ``architect.model_tiers.light`` or ``reviewers[0]``.
    label: str
    #: ``architect``, ``architect (tier light)`` or ``reviewer <id>``.
    display: str
    #: Set for a reviewer only.
    reviewer_id: str
    #: The role's spec, the tier merged onto it, or the reviewer.
    spec: Dict[str, Any]
    #: For a role or a tier.
    role: str = ""
    #: A tier's key as written: not ``str()``'d, as ``layer_of`` takes it literally.
    tier: Any = None
    #: A tier's own entry.
    entry: Optional[Dict[str, Any]] = None
    #: A reviewer's place in the panel, non-mappings counted.
    position: int = -1
    #: A tier that keeps its role's provider.
    same_provider: bool = False
    #: ``code`` or ``design``: the reviewer panel a reviewer seat is in.
    panel: str = "code"


def role_seats(data: Dict[str, Any], roles: Sequence[str]) -> List[SeatRun]:
    """For each role in ``roles``: the role, then its mapping tiers in written order.

    Broken entries are skipped: ``validate`` reports those already.
    """
    seats: List[SeatRun] = []
    for role in roles:
        spec = data.get(role)
        if not isinstance(spec, dict):
            continue
        seats.append(SeatRun("role", role, role, "", spec, role=role))
        tiers = spec.get("model_tiers")
        for tier, entry in tiers.items() if isinstance(tiers, dict) else ():
            if not isinstance(entry, dict):
                continue
            merged = merge_tier(spec, entry)
            seats.append(
                SeatRun(
                    "tier",
                    "%s.model_tiers.%s" % (role, tier),
                    "%s (tier %s)" % (role, tier),
                    "",
                    merged,
                    role=role,
                    tier=tier,
                    entry=entry,
                    same_provider=merged.get("provider") == spec.get("provider"),
                )
            )
    return seats


def reviewer_seats(
    data: Dict[str, Any], design_origins: Optional[Sequence[ReviewerOrigin]] = None
) -> List[SeatRun]:
    """Each mapping reviewer, at its place in the panel: a non-mapping before it still counts.

    The code panel, then the design panel when a file sets one. A design seat
    copied from the code panel that runs as the code seat of its id does --
    the same provider and options -- is that seat's run and is not listed
    twice; it is refused and warned about as that seat. A seat a file wrote
    under ``review.design`` is always listed, so it meets its own layer's
    rules whatever code seat it resembles. ``design_origins`` says which is
    which; without them (data never composed by ``load``) a design seat that
    runs as its code seat is taken for a copy.
    """
    seats: List[SeatRun] = []
    reviewers = data.get("reviewers")
    code: Dict[str, Dict[str, Any]] = {}
    for index, reviewer in enumerate(reviewers if isinstance(reviewers, list) else []):
        if not isinstance(reviewer, dict):
            continue
        reviewer_id = str(reviewer.get("id") or "")
        code.setdefault(reviewer_id, reviewer)
        display = "reviewer %s" % (reviewer_id or index + 1)
        label = "reviewers[%d]" % index
        seats.append(SeatRun("reviewer", label, display, reviewer_id, reviewer, position=index))
    design = get_path(data, DESIGN_PANEL.reviewers)
    for index, reviewer in enumerate(design if isinstance(design, list) else []):
        if not isinstance(reviewer, dict):
            continue
        reviewer_id = str(reviewer.get("id") or "")
        if design_origins is None:
            copied = True
        else:
            origin = design_origins[index] if index < len(design_origins) else None
            copied = origin is not None and not origin.design
        if copied and same_run(reviewer, code.get(reviewer_id)):
            continue
        display = "design reviewer %s" % (reviewer_id or index + 1)
        label = "%s[%d]" % (DESIGN_PANEL.reviewers, index)
        seat = SeatRun("reviewer", label, display, reviewer_id, reviewer, position=index, panel="design")
        seats.append(seat)
    return seats


def same_run(reviewer: Any, other: Any) -> bool:
    """Whether two reviewer entries run on the same provider with the same options."""
    if not isinstance(reviewer, dict) or not isinstance(other, dict):
        return False
    same_provider = reviewer.get("provider") == other.get("provider")
    return same_provider and reviewer.get("options") == other.get("options")


def read_only_seats(
    data: Dict[str, Any], design_origins: Optional[Sequence[ReviewerOrigin]] = None
) -> List[SeatRun]:
    """The read-only roles and their tiers, then every reviewer (``reviewer_seats``)."""
    return role_seats(data, READ_ONLY_ROLES) + reviewer_seats(data, design_origins)
