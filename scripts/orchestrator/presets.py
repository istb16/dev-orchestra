"""Setup presets, fitted to the CLIs installed on this machine.

A preset is one choice on the spend axis -- ``quality``, ``standard``,
``fast`` -- expressed as values of keys that already exist: the four roles,
the code and design reviewer panels, ``review.design.enabled`` and
``optimization.level`` (``GOVERNED``). The global file stores only ``preset: <name>``; the values are
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

A cheap seat runs on a provider's cheap model, and only Claude has one that
can be named offline, so it is dealt round the Claude seats alone and is not
added where Claude is not installed. A held seat is added only where its
reviewer can be held to reading, so it is never put on agy. Both are skipped
after the full seats are dealt, so a skipped seat moves no other one.

A seat may name a vendor. A Claude seat sits on Claude whenever Claude is in
the seat pool and is dealt with the untagged seats otherwise; a Codex seat
sits on Codex, else on the first other provider of the seat pool, else it is
not added. Vendor seats do not rotate with the implementer. A seat's
``high_risk_model`` is kept on Claude only, the one provider whose second
model can be named offline.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple, Union

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

#: The family a cheap seat takes on each provider that has one named offline.
_CHEAP_FAMILY = {CLAUDE: "sonnet"}

#: The providers a seat's ``high_risk_model`` is written on.
_HIGH_RISK_FAMILY_PROVIDERS = (CLAUDE,)

#: The read-only enforcement a user adapter's static report must give for it
#: to take the read-only roles and the reviewer seats.
SEAT_ENFORCEMENT = ("verified", "partial")

#: What a fit note adds when a read-only role or a reviewer seat lands on agy.
_AGY_ROLE_OPT_OUT = "; agy cannot be held to reading -- set %s in the global file to keep it off agy"
_AGY_SEAT_OPT_OUT = "; agy cannot be held to reading -- list %s in the global file to keep them off agy"

#: The fit note for a cheap or held seat that is not added: what is missing,
#: the panel word, the seat's number and role, and why.
_SEAT_SKIPPED = "%s not found on PATH: %s %d (%s) was not added; %s"

#: Why a held seat is not added on agy.
_AGY_UNHELD = "agy cannot be held to reading"

#: Why a Codex seat is not added where nothing but Claude can take it.
_SECOND_VENDOR = "it is a second vendor's opinion and nothing installed stands in for one"

#: The keys a preset sets. ``config setup --preset`` and the wizard's preset
#: path replace these in an existing file and keep everything else.
GOVERNED = (
    "orchestrator",
    "architect",
    "implementer",
    "review_fixer",
    "reviewers",
    "review.design.enabled",
    "review.design.reviewers",
    "optimization.level",
)

#: The preset in force when the global file names none.
DEFAULT = "standard"

_ALWAYS = "always"
_HIGH_RISK = "high-risk"


class Seat(NamedTuple):
    #: The review role.
    role: str
    #: The Claude model family; a cheap seat takes ``_CHEAP_FAMILY`` instead.
    family: str
    #: The condition it runs under.
    when: str
    #: True for a seat on a provider's cheap model, dealt round those alone.
    #: It takes no ``vendor``: the cheap providers are its placement.
    cheap: bool = False
    #: True for a seat added only where its reviewer can be held to reading.
    held: bool = False
    #: The Claude family a high-risk change runs it on, kept on Claude only.
    high_risk_family: Optional[str] = None
    #: ``claude`` or ``codex`` for a seat placed on that vendor; None to deal it.
    vendor: Optional[str] = None


class Preset(NamedTuple):
    #: role -> Claude model family.
    roles: Dict[str, str]
    #: The reviewer seats, dealt in this order.
    seats: Tuple[Seat, ...]
    #: ``review.design.enabled``, or None to leave it unset.
    design_review: Union[bool, str, None]
    #: ``optimization.level``, or None to leave it unset.
    level: Optional[str]
    #: The design reviewer seats, fitted as the code seats are.
    design_seats: Tuple[Seat, ...] = ()


class Fit(NamedTuple):
    """An expansion: the values it sets, and what was refitted and why."""

    values: Dict[str, Any]
    notes: List[str]
    #: Parallel to ``notes``: the key each one is about (a role,
    #: ``reviewers`` or ``review.design.reviewers``), so a caller can drop the
    #: notes for what a file sets.
    subjects: List[str]
    #: Parallel to the composed panel: where each reviewer was written.
    #: Filled by ``config.compose``; an expansion alone leaves it empty.
    origins: Tuple[config_mod.ReviewerOrigin, ...] = ()
    #: The same for the design panel, when the fit or a file sets one.
    design_origins: Tuple[config_mod.ReviewerOrigin, ...] = ()


#: ``standard``'s design panel. Not read from ``default_config()``, which has
#: none: a design round without one runs the code panel.
_STANDARD_DESIGN = (
    Seat("general", "sonnet", _ALWAYS, high_risk_family="opus", vendor=CLAUDE),
    Seat("security", "sonnet", _ALWAYS, held=True, vendor=CLAUDE),
    Seat("test", "sonnet", _ALWAYS, cheap=True, held=True),
)


def _standard() -> Preset:
    """``default_config()`` read back as a preset, so the two cannot drift.

    A default seat held by another vendor takes ``sonnet`` when it lands on
    Claude: a Claude-only machine then gets two models, not two copies of one.
    A seat with a ``high_risk_model`` is a Claude seat, since only Claude can
    name a second model offline; a Claude seat on the cheap family without one
    is a cheap seat; and every seat but a general one is held: only the
    general seats predate them, and agy keeps exactly those.
    """
    defaults = config_mod.default_config()
    seats = []
    for reviewer in defaults["reviewers"]:
        role = reviewer.get("role", "general")
        on_claude = reviewer["provider"] == CLAUDE
        family = reviewer["model"]["family"] if on_claude else "sonnet"
        high_risk = reviewer.get("high_risk_model")
        high_risk_family = high_risk.get("family") if on_claude and isinstance(high_risk, dict) else None
        cheap = on_claude and family == _CHEAP_FAMILY[CLAUDE] and not high_risk_family
        seats.append(
            Seat(
                role,
                family,
                str(reviewer.get("when") or _ALWAYS),
                cheap=cheap,
                held=role != "general",
                high_risk_family=high_risk_family,
                vendor=CLAUDE if high_risk_family else None,
            )
        )
    return Preset(
        {role: defaults[role]["model"]["family"] for role in config_mod.KNOWN_ROLES},
        tuple(seats),
        None,
        None,
        _STANDARD_DESIGN,
    )


PRESETS: Dict[str, Preset] = {
    "quality": Preset(
        {"orchestrator": "opus", "architect": "fable", "implementer": "fable", "review_fixer": "fable"},
        (
            Seat("general", "fable", _ALWAYS, vendor=CLAUDE),
            Seat("general", "fable", _ALWAYS, vendor=CODEX),
            Seat("security", "opus", _ALWAYS, vendor=CLAUDE),
            Seat("security", "opus", _ALWAYS, vendor=CODEX),
            Seat("architecture", "opus", _ALWAYS, vendor=CLAUDE),
            Seat("test", "opus", _ALWAYS, held=True, vendor=CLAUDE),
        ),
        "auto",
        "quality",
        (
            Seat("general", "sonnet", _ALWAYS, high_risk_family="opus", vendor=CLAUDE),
            Seat("general", "sonnet", _ALWAYS, vendor=CODEX),
            Seat("security", "opus", _ALWAYS, held=True, vendor=CLAUDE),
            Seat("test", "sonnet", _ALWAYS, cheap=True, held=True),
            Seat("architecture", "opus", _ALWAYS, held=True, vendor=CLAUDE),
        ),
    ),
    "standard": _standard(),
    "fast": Preset(
        {"orchestrator": "sonnet", "architect": "opus", "implementer": "sonnet", "review_fixer": "sonnet"},
        (Seat("general", "opus", _ALWAYS), Seat("security", "sonnet", _HIGH_RISK)),
        False,
        "aggressive",
        (Seat("general", "sonnet", _ALWAYS, vendor=CLAUDE),),
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
    # lazy: importing the registry runs the user adapters, and presets must be complete first
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


def _named_fit(name: str) -> UserFit:
    """:func:`user_fit` of the adapter called ``name``; not fitted when the
    name is unknown or its factory fails."""
    # lazy: importing the registry runs the user adapters, and presets must be complete first
    from .providers import get_provider

    try:
        return user_fit(get_provider(name))
    except Exception:  # an unknown name, or a factory that failed: not fitted
        return UserFit("", False, "")


def installed_providers() -> List[str]:
    """Each fitted provider whose CLI is on PATH: the built-ins in fitting
    order, then each opted-in user adapter by name."""
    # lazy: importing the registry runs the user adapters, and presets must be complete first
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


def suggestion_provider(installed: Sequence[str]) -> Optional[str]:
    """The provider ``config suggest-roles`` puts its reviewers on, or None.

    The first seat provider installed, else the first installed user adapter
    that may take a reviewer seat and is not warned about. Never agy: the
    reviewers go to the project file, which may not hold one on it.
    """
    for name in SEAT_PROVIDERS:
        if name in installed:
            return name
    for name in installed:
        if name in FITTED_PROVIDERS:
            continue
        # lazy: importing the registry runs the user adapters, and presets must be complete first
        from .providers import warned_provider

        if _named_fit(name).seats and not warned_provider(name):
            return name
    return None


def cheap_family(provider: str) -> str:
    """The family a suggested reviewer on ``provider`` takes: its cheap one where
    it has one, else the family a preset would give it."""
    if provider in _CHEAP_FAMILY:
        return _CHEAP_FAMILY[provider]
    if provider in _OFFLINE_FAMILY:
        return _OFFLINE_FAMILY[provider]
    return _named_fit(provider).family or config_mod.default_reviewer_family(provider)


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
        if dotted not in config_mod.KNOWN_ROLES:
            config_mod.pop_path(rest, dotted)
    version = rest.pop("version", config_mod.CONFIG_VERSION)
    rest.pop("preset", None)
    return {"version": version, "preset": name, **rest}


def _family(provider: str, claude_family: str, families: Dict[str, str]) -> str:
    return families.get(provider, claude_family)


def _is_dealt(seat: Seat, pool: Sequence[str]) -> bool:
    """Whether ``seat`` is dealt round ``pool``: a full seat with no vendor,
    or a Claude seat where Claude is not in the pool."""
    if seat.cheap:
        return False
    return seat.vendor is None or (seat.vendor == CLAUDE and CLAUDE not in pool)


def _place(seat: Seat, pool: Sequence[str]) -> Optional[str]:
    """Where a vendor seat that is not dealt sits in ``pool``, or None.

    A Claude seat sits on Claude. A Codex seat sits on Codex, else on the
    first provider of the pool that is not Claude, else nowhere.
    """
    if seat.vendor == CLAUDE:
        return CLAUDE
    if seat.vendor in pool:
        return seat.vendor
    return next((provider for provider in pool if provider != CLAUDE), None)


def _deal(
    seats: Sequence[Seat], pool: Sequence[str], implementer: Optional[str] = None
) -> List[Optional[str]]:
    """The provider each full seat lands on: round the pool, from the implementer's.

    A single always-running dealt seat from the implementer's own vendor is
    not an independent review, so a panel with one starts with the other
    vendor. ``implementer`` is the provider a file put the implementer on;
    without one the implementer is fitted, to ``pool[0]``. A vendor seat that
    is not dealt goes where ``_place`` puts it, so it moves no dealt seat.
    Cheap seats are not dealt here and get ``None``, so they move no full seat.
    """
    if implementer is not None and implementer in pool:
        at = list(pool).index(implementer)
        pool = list(pool[at:]) + list(pool[:at])
    start = 0
    always = [seat for seat in seats if _is_dealt(seat, pool) and seat.when == _ALWAYS]
    if len(always) == 1 and len(pool) > 1:
        start = 1
    dealt: List[Optional[str]] = []
    index = 0
    for seat in seats:
        if seat.cheap:
            dealt.append(None)
        elif not _is_dealt(seat, pool):
            dealt.append(_place(seat, pool))
        else:
            dealt.append(pool[(start + index) % len(pool)])
            index += 1
    return dealt


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
    fits = {user: _named_fit(user) for user in users}
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

    code_panel, code_notes = _fit_panel(
        preset.seats, seat_pool, implementer, families, missing, "reviewer seat", "reviewers"
    )
    notes.extend(code_notes)
    subjects.extend(["reviewers"] * len(code_notes))
    values["reviewers"] = code_panel

    design: Dict[str, Any] = {}
    if preset.design_review is not None:
        design["enabled"] = preset.design_review
    if preset.design_seats:
        design_panel, design_notes = _fit_panel(
            preset.design_seats,
            seat_pool,
            implementer,
            families,
            missing,
            "design reviewer seat",
            config_mod.DESIGN_PANEL.reviewers,
        )
        notes.extend(design_notes)
        subjects.extend([config_mod.DESIGN_PANEL.reviewers] * len(design_notes))
        design["reviewers"] = design_panel
    if design:
        values["review"] = {"design": design}
    if preset.level is not None:
        values["optimization"] = {"level": preset.level}
    return Fit(values, notes, subjects)


def _fit_panel(
    seats: Sequence[Seat],
    seat_pool: Sequence[str],
    implementer: Optional[str],
    families: Dict[str, str],
    missing: str,
    seat_word: str,
    panel_key: str,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """One panel's ``seats`` fitted to ``seat_pool``: ``(reviewers, notes)``.

    ``seat_word`` is how a note names a seat, and ``panel_key`` the list the
    agy opt-out tells the user to write.
    """
    for seat in seats:
        if seat.cheap and seat.vendor is not None:
            # Either tag places the seat, and the cheap one would win unseen.
            raise ValueError("a cheap seat takes no vendor: %r" % (seat,))
    written = _deal(seats, SEAT_PROVIDERS, implementer)
    dealt = _deal(seats, seat_pool, implementer)
    cheap_pool = [provider for provider in seat_pool if provider in _CHEAP_FAMILY]
    cheap_index = 0
    panel: Dict[str, List[Dict[str, Any]]] = {"reviewers": []}
    seen: Dict[Tuple[str, str, Optional[str], str, str], str] = {}
    seat_notes: List[str] = []
    for index, (seat, provider, intended) in enumerate(zip(seats, dealt, written, strict=True), 1):
        role, claude_family, when = seat.role, seat.family, seat.when
        if seat.cheap:
            if not cheap_pool:
                reason = "%s has no cheap model named offline" % describe_installed(seat_pool)
                seat_notes.append(_SEAT_SKIPPED % (missing, seat_word, index, role, reason))
                continue
            provider = intended = cheap_pool[cheap_index % len(cheap_pool)]
            cheap_index += 1
            family = _CHEAP_FAMILY[provider]
        elif provider is None:  # a vendor seat nothing installed can take
            seat_notes.append(_SEAT_SKIPPED % (missing, seat_word, index, role, _SECOND_VENDOR))
            continue
        else:
            family = _family(provider, claude_family, families)
        if seat.held and provider == AGY:
            seat_notes.append(_SEAT_SKIPPED % (missing, seat_word, index, role, _AGY_UNHELD))
            continue
        high_risk_family = seat.high_risk_family if provider in _HIGH_RISK_FAMILY_PROVIDERS else None
        key = (provider, family, high_risk_family, role, when)
        if key in seen:
            seat_notes.append(
                "%s not found on PATH: %s %d (%s) was not added; it would repeat %s"
                % (missing, seat_word, index, role, seen[key])
            )
            continue
        reviewer_id = config_mod.suggest_reviewer_id(panel, provider, role)
        seen[key] = reviewer_id
        panel["reviewers"].append(
            config_mod.make_reviewer(
                reviewer_id, provider, family, role, when=when, high_risk_family=high_risk_family
            )
        )
        if provider != intended:
            note = "%s not found on PATH: %s %d (%s) went to %s as %s (%s)" % (
                missing,
                seat_word,
                index,
                role,
                provider,
                reviewer_id,
                family,
            )
            if seat.high_risk_family and not high_risk_family:
                note += "; no high-risk model on %s" % provider
            if provider == AGY:
                note += _AGY_SEAT_OPT_OUT % panel_key
            seat_notes.append(note)
    return panel["reviewers"], seat_notes


def render_notes(fit: Fit) -> str:
    """The notes, one ``note:`` line each; empty when there are none."""
    return "\n".join("note: %s" % note for note in fit.notes)
