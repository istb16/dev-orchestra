"""Setup presets, fitted to the CLIs installed on this machine.

A preset is one choice on the spend axis -- ``quality``, ``standard``,
``fast`` -- expressed as values of keys that already exist: the four roles,
the reviewer panel, ``review.design.enabled`` and ``optimization.level``
(``GOVERNED``). The global file stores only ``preset: <name>``; the values are
worked out at load time against what is on PATH, so a machine without Codex
never carries a Codex reviewer that fails every round.

Only the built-in adapters take part. A user adapter runs when a file names
it, as it always has: nobody chose it for a seat, and its read-only
enforcement may be ``unspecified``. ``which()`` is the whole of the detection:
a PATH lookup, no subprocess, so it is cheap enough for every ``load()``.

Codex gets ``recommended-coding`` in every slot, the one family its adapter
vouches for without running the CLI, so an expansion never needs the network
or a subprocess to be valid.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import config as config_mod

CLAUDE = "claude"
CODEX = "codex"

#: The providers a preset is dealt across, in the order roles prefer them.
FITTED_PROVIDERS = (CLAUDE, CODEX)

#: The family each non-Claude provider gets, whatever the preset's tier.
_OFFLINE_FAMILY = {CODEX: "recommended-coding"}

#: The keys a preset sets. ``config setup --preset`` and the wizard's preset
#: path replace these in an existing file and keep everything else.
GOVERNED = (
    "orchestrator",
    "architect",
    "implementer",
    "review_fixer",
    "reviewers",
    "review.design.enabled",
    "optimization.level",
)

#: The preset in force when the global file names none.
DEFAULT = "standard"

_ALWAYS = "always"
_HIGH_RISK = "high-risk"


class Preset(NamedTuple):
    #: role -> Claude model family.
    roles: Dict[str, str]
    #: (review role, Claude model family, condition), dealt in this order.
    seats: Tuple[Tuple[str, str, str], ...]
    #: ``review.design.enabled``, or None to leave it unset.
    design_review: Optional[bool]
    #: ``optimization.level``, or None to leave it unset.
    level: Optional[str]


class Fit(NamedTuple):
    """An expansion: the values it sets, and what was refitted and why."""

    values: Dict[str, Any]
    notes: List[str]
    #: Parallel to ``notes``: the key each one is about (a role, or
    #: ``reviewers``), so a caller can drop the notes for what a file sets.
    subjects: List[str]


def _standard() -> Preset:
    """``default_config()`` read back as a preset, so the two cannot drift.

    A default seat held by another vendor takes ``sonnet`` when it lands on
    Claude: a Claude-only machine then gets two models, not two copies of one.
    """
    defaults = config_mod.default_config()
    seats = []
    for reviewer in defaults["reviewers"]:
        family = reviewer["model"]["family"] if reviewer["provider"] == CLAUDE else "sonnet"
        seats.append((reviewer.get("role", "general"), family, str(reviewer.get("when") or _ALWAYS)))
    return Preset(
        {role: defaults[role]["model"]["family"] for role in config_mod.KNOWN_ROLES},
        tuple(seats),
        None,
        None,
    )


PRESETS: Dict[str, Preset] = {
    "quality": Preset(
        {"orchestrator": "opus", "architect": "fable", "implementer": "fable", "review_fixer": "fable"},
        (("general", "fable", _ALWAYS), ("security", "opus", _ALWAYS), ("architecture", "opus", _ALWAYS)),
        True,
        "quality",
    ),
    "standard": _standard(),
    "fast": Preset(
        {"orchestrator": "sonnet", "architect": "opus", "implementer": "sonnet", "review_fixer": "sonnet"},
        (("general", "opus", _ALWAYS), ("security", "opus", _HIGH_RISK)),
        None,
        "aggressive",
    ),
}

NAMES = tuple(sorted(PRESETS))


def installed_providers() -> List[str]:
    """Each built-in fitted provider whose CLI is on PATH, in fitting order."""
    from .providers import get_provider

    found: List[str] = []
    for name in FITTED_PROVIDERS:
        try:
            if get_provider(name).which():
                found.append(name)
        except Exception:  # a lookup that fails is a CLI that is not there
            continue
    return found


def describe_installed(installed: Sequence[str]) -> str:
    """``claude, codex``, or what to say when neither is there."""
    return ", ".join(installed) if installed else "no installed CLI"


def describe(name: Optional[str], source: str, installed: Sequence[str]) -> str:
    """``quality (global; fitted to claude, codex)``, as ``config show`` and ``doctor`` say it."""
    if name is None:
        return "none (the global file names an unknown preset; nothing fitted)"
    return "%s (%s; fitted to %s)" % (name, source, describe_installed(installed))


def with_preset(layer: Optional[Dict[str, Any]], name: str) -> Dict[str, Any]:
    """``layer`` naming ``name``, minus every ``GOVERNED`` key it set.

    What the file already held beside those stays: choosing a preset is not a
    reason to lose ``review.parallel: false``. A mapping emptied by the removal
    goes too, as ``prune_layer`` drops one.

    A preset sets a role's ``provider`` and ``model`` and nothing else, so only
    those go: a role that still holds ``options`` or ``model_tiers`` stays set,
    and ``compose`` notes that it was not fitted.
    """
    rest = copy.deepcopy(layer or {})
    for role in config_mod.KNOWN_ROLES:
        spec = rest.get(role)
        if not isinstance(spec, dict):
            rest.pop(role, None)
            continue
        spec.pop("provider", None)
        spec.pop("model", None)
        if not spec:
            rest.pop(role)
    for dotted in GOVERNED:
        if dotted in config_mod.KNOWN_ROLES:
            continue
        parts = dotted.split(".")
        parents = [rest]
        for part in parts[:-1]:
            child = parents[-1].get(part)
            if not isinstance(child, dict):
                break
            parents.append(child)
        else:
            parents[-1].pop(parts[-1], None)
            for depth in range(len(parents) - 1, 0, -1):
                if not parents[depth]:
                    parents[depth - 1].pop(parts[depth - 1], None)
    version = rest.pop("version", config_mod.CONFIG_VERSION)
    rest.pop("preset", None)
    return {"version": version, "preset": name, **rest}


def _family(provider: str, claude_family: str) -> str:
    return _OFFLINE_FAMILY.get(provider, claude_family)


def _deal(preset: Preset, pool: Sequence[str], implementer: Optional[str] = None) -> List[str]:
    """The provider each seat lands on: round the pool, from the implementer's.

    A single always-running seat from the implementer's own vendor is not an
    independent review, so a preset with one starts with the other vendor.
    ``implementer`` is the provider a file put the implementer on; without one
    the implementer is fitted, to ``pool[0]``.
    """
    if implementer is not None and implementer in pool:
        at = list(pool).index(implementer)
        pool = list(pool[at:]) + list(pool[:at])
    start = 0
    always = [seat for seat in preset.seats if seat[2] == _ALWAYS]
    if len(always) == 1 and len(pool) > 1:
        start = 1
    return [pool[(start + index) % len(pool)] for index in range(len(preset.seats))]


def expand(name: str, installed: Sequence[str], implementer: Optional[str] = None) -> Fit:
    """``name`` as values of existing keys, fitted to ``installed``.

    With neither CLI installed the preset expands as written: nothing better
    is known, and ``doctor`` already reports the missing CLIs. ``implementer``
    is the provider a file set the implementer to, which the panel is dealt
    around instead of the fitted one.
    """
    preset = PRESETS[name]
    pool = [provider for provider in FITTED_PROVIDERS if provider in installed] or list(FITTED_PROVIDERS)
    missing = ", ".join(provider for provider in FITTED_PROVIDERS if provider not in pool)
    values: Dict[str, Any] = {}
    notes: List[str] = []
    subjects: List[str] = []

    role_provider = pool[0]
    for role in config_mod.KNOWN_ROLES:
        family = _family(role_provider, preset.roles[role])
        values[role] = {"provider": role_provider, "model": {"family": family, "version": "latest"}}
        if role_provider != FITTED_PROVIDERS[0]:
            notes.append("%s not found on PATH: %s went to %s (%s)" % (missing, role, role_provider, family))
            subjects.append(role)

    written = _deal(preset, FITTED_PROVIDERS, implementer)
    dealt = _deal(preset, pool, implementer)
    panel: Dict[str, List[Dict[str, Any]]] = {"reviewers": []}
    seen: Dict[Tuple[str, str, str, str], str] = {}
    seat_notes: List[str] = []
    for index, (seat, provider, intended) in enumerate(zip(preset.seats, dealt, written, strict=True), 1):
        role, claude_family, when = seat
        family = _family(provider, claude_family)
        key = (provider, family, role, when)
        if key in seen:
            seat_notes.append(
                "%s not found on PATH: reviewer seat %d (%s) was not added; it would repeat %s"
                % (missing, index, role, seen[key])
            )
            continue
        reviewer_id = config_mod.suggest_reviewer_id(panel, provider, role)
        seen[key] = reviewer_id
        panel["reviewers"].append(config_mod.make_reviewer(reviewer_id, provider, family, role, when=when))
        if provider != intended:
            seat_notes.append(
                "%s not found on PATH: reviewer seat %d (%s) went to %s as %s (%s)"
                % (missing, index, role, provider, reviewer_id, family)
            )
    notes.extend(seat_notes)
    subjects.extend(["reviewers"] * len(seat_notes))
    values["reviewers"] = panel["reviewers"]

    if preset.design_review is not None:
        values["review"] = {"design": {"enabled": preset.design_review}}
    if preset.level is not None:
        values["optimization"] = {"level": preset.level}
    return Fit(values, notes, subjects)


def render_notes(fit: Fit) -> str:
    """The notes, one ``note:`` line each; empty when there are none."""
    return "\n".join("note: %s" % note for note in fit.notes)
