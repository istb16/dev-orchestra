"""Setup presets, fitted to the CLIs installed on this machine.

A preset is one choice on the spend axis -- ``quality``, ``standard``,
``fast`` -- expressed as values of keys that already exist: the four roles,
the reviewer panel, ``review.design.enabled`` and ``optimization.level``
(``GOVERNED``). The global file stores only ``preset: <name>``; the values are
worked out at load time against what is on PATH, so a machine without Codex
never carries a Codex reviewer that fails every round.

The built-in adapters take part, and a user adapter only when it declares
``preset_family``: otherwise it runs when a file names it, as it always has,
since nobody chose it for a seat and its read-only enforcement may be
``unspecified``. An opted-in user adapter takes the write roles only when
neither Claude, Codex nor agy is on PATH, and the read-only roles and the
reviewer seats only when neither Claude nor Codex is and its static report
says ``verified`` or ``partial``. ``which()`` is the whole of the detection:
a PATH lookup, no subprocess, so it is cheap enough for every ``load()``.

Codex gets ``recommended-coding`` in every slot, the one family its adapter
vouches for without running the CLI, so an expansion never needs the network
or a subprocess to be valid. agy gets ``default`` for the same reason, and a
user adapter the family it declares.

Write roles and read-only seats are fitted from different pools. agy cannot
be held to reading, so it takes the orchestrator, the architect and the
reviewer seats last of all: only when neither Claude, Codex nor an eligible
user adapter is on PATH, and warned wherever a global-file agy seat is.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import config as config_mod

CLAUDE = "claude"
CODEX = "codex"
AGY = "agy"

#: The providers a preset is fitted to, in the order roles prefer them. The
#: write roles are fitted from here.
FITTED_PROVIDERS = (CLAUDE, CODEX, AGY)

#: The providers the read-only roles and the reviewer seats are fitted from.
SEAT_PROVIDERS = (CLAUDE, CODEX)

#: The family each non-Claude provider gets, whatever the preset's tier.
_OFFLINE_FAMILY = {CODEX: "recommended-coding", AGY: "default"}

#: The read-only enforcement a user adapter's static report must give for it
#: to take the read-only roles and the reviewer seats.
SEAT_ENFORCEMENT = ("verified", "partial")

#: What a fit note adds when a read-only role or a reviewer seat lands on agy.
_AGY_ROLE_OPT_OUT = "; agy cannot be held to reading -- set %s in the global file to keep it off agy"
_AGY_SEAT_OPT_OUT = (
    "; agy cannot be held to reading -- list reviewers in the global file to keep them off agy"
)

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
    #: Parallel to the composed panel: where each reviewer was written.
    #: Filled by ``config.compose``; an expansion alone leaves it empty.
    origins: Tuple[config_mod.ReviewerOrigin, ...] = ()


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


class UserFit(NamedTuple):
    """What fitting makes of a user adapter's declaration."""

    #: The family it takes in every slot; empty when it is not fitted at all.
    family: str
    #: True when it may take the read-only roles and the reviewer seats too.
    seats: bool
    #: One sentence for ``doctor``; empty for an adapter that is not opted in.
    note: str


_WRITE_ONLY = "takes the write roles only, when neither Claude, Codex nor agy is installed"
_SEAT_BAR = "a read-only seat needs a static verified or partial report"


def user_fit(provider: Any) -> UserFit:
    """Whether presets may fit this user adapter, and to what. Never raises.

    ``preset_family`` and ``static_enforcement`` are read from the class, so
    an instance cannot opt itself in. A declaration that is not a non-empty
    string keeps the adapter out of every pool before its report is asked.
    The report is asked only when it is static: fitting runs at every load
    and must not start a CLI.
    """
    from .providers import describe_exception

    cls = type(provider)
    declared = getattr(cls, "preset_family", None)
    if declared is None:
        return UserFit("", False, "")
    if not isinstance(declared, str) or not declared.strip():
        return UserFit(
            "", False, "preset_family must be a non-empty string; the adapter is not fitted to presets"
        )
    family = declared.strip()
    if getattr(cls, "static_enforcement", False) is not True:
        reason = "its read-only report is not static (static_enforcement = False)"
        return UserFit(family, False, "family %s; %s: %s, and %s" % (family, _WRITE_ONLY, reason, _SEAT_BAR))
    try:
        report = provider.read_only_enforcement()
        status = report.get("status") if isinstance(report, Mapping) else None
    except Exception as exc:
        reason = "read_only_enforcement() raised %s" % describe_exception(exc)
        return UserFit(family, False, "family %s; %s: %s" % (family, _WRITE_ONLY, reason))
    if status in SEAT_ENFORCEMENT:
        return UserFit(
            family,
            True,
            "family %s; takes the read-only roles and reviewer seats when neither Claude nor Codex "
            "is installed (read-only is %s)" % (family, status),
        )
    reason = "its read-only enforcement is %s" % status
    return UserFit(family, False, "family %s; %s: %s, and %s" % (family, _WRITE_ONLY, reason, _SEAT_BAR))


def installed_providers() -> List[str]:
    """Each fitted provider whose CLI is on PATH: the built-ins in fitting
    order, then each opted-in user adapter by name."""
    from .providers import available_providers, get_provider, provider_origin

    found: List[str] = []
    for name in FITTED_PROVIDERS:
        try:
            if get_provider(name).which():
                found.append(name)
        except Exception:  # a lookup that fails is a CLI that is not there
            continue
    for name in available_providers():
        try:
            origin = provider_origin(name)
            if origin is None or origin.kind != "user":
                continue
            provider = get_provider(name)
            if user_fit(provider).family and provider.which():
                found.append(name)
        except Exception:  # likewise, and an adapter that fails is not fitted
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


def _family(provider: str, claude_family: str, families: Dict[str, str]) -> str:
    return families.get(provider, claude_family)


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

    Each pool is filled from the first tier with something installed. Write
    roles: Claude and Codex, then agy, then opted-in user adapters. Read-only
    roles and seats: Claude and Codex, then the user adapters eligible for a
    seat, then agy. With nothing fitted installed the preset expands as
    written: nothing better is known, and ``doctor`` already reports the
    missing CLIs. A name in ``installed`` that is not a built-in is looked up
    here, and one that cannot be is not fitted. ``implementer`` is the
    provider a file set the implementer to, which the panel is dealt around
    instead of the fitted one.
    """
    preset = PRESETS[name]
    users = sorted({provider for provider in installed if provider not in FITTED_PROVIDERS})
    fits: Dict[str, UserFit] = {}
    for user in users:
        try:
            from .providers import get_provider

            fits[user] = user_fit(get_provider(user))
        except Exception:  # an unknown name, or a factory that failed: not fitted
            fits[user] = UserFit("", False, "")
    fitted_users = [user for user in users if fits[user].family]
    builtin_pool = [provider for provider in FITTED_PROVIDERS if provider in installed]
    pool = builtin_pool or fitted_users or list(FITTED_PROVIDERS)
    builtin_seats = [provider for provider in SEAT_PROVIDERS if provider in installed]
    user_seats = [user for user in fitted_users if fits[user].seats]
    agy_seats = [AGY] if AGY in installed else []
    seat_pool = builtin_seats or user_seats or agy_seats or list(SEAT_PROVIDERS)
    families = {**_OFFLINE_FAMILY, **{user: fits[user].family for user in fitted_users}}
    missing = ", ".join(provider for provider in SEAT_PROVIDERS if provider not in seat_pool)
    values: Dict[str, Any] = {}
    notes: List[str] = []
    subjects: List[str] = []

    for role in config_mod.KNOWN_ROLES:
        read_only = role in config_mod.READ_ONLY_ROLES
        order = SEAT_PROVIDERS if read_only else FITTED_PROVIDERS
        role_provider = seat_pool[0] if read_only else pool[0]
        family = _family(role_provider, preset.roles[role], families)
        values[role] = {"provider": role_provider, "model": {"family": family, "version": "latest"}}
        if role_provider != order[0]:
            # The CLIs preferred to the one it went to, which are what is missing:
            # all of them for a provider from a later tier.
            at = order.index(role_provider) if role_provider in order else len(order)
            passed = ", ".join(order[:at])
            note = "%s not found on PATH: %s went to %s (%s)" % (passed, role, role_provider, family)
            if read_only and role_provider == AGY:
                note += _AGY_ROLE_OPT_OUT % role
            notes.append(note)
            subjects.append(role)

    written = _deal(preset, SEAT_PROVIDERS, implementer)
    dealt = _deal(preset, seat_pool, implementer)
    panel: Dict[str, List[Dict[str, Any]]] = {"reviewers": []}
    seen: Dict[Tuple[str, str, str, str], str] = {}
    seat_notes: List[str] = []
    for index, (seat, provider, intended) in enumerate(zip(preset.seats, dealt, written, strict=True), 1):
        role, claude_family, when = seat
        family = _family(provider, claude_family, families)
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
            note = "%s not found on PATH: reviewer seat %d (%s) went to %s as %s (%s)" % (
                missing,
                index,
                role,
                provider,
                reviewer_id,
                family,
            )
            if provider == AGY:
                note += _AGY_SEAT_OPT_OUT
            seat_notes.append(note)
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
