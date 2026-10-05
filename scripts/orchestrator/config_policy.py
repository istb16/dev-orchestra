"""Which read-only seats and write-role options are refused or warned about.

What comes from the project file is refused where it could loosen a run; a
read-only seat on a provider that cannot be held to reading is warned about.
Importing this module loads the provider registry.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from .config import (
    WRITE_ROLES,
    LoadedConfig,
    ReviewerOrigin,
    _read_only_seats,
    _reviewer_seats,
    role_seats,
)
from .providers import _warned_provider, get_provider, unenforced_warning

# --------------------------------------------------------------------------- read-only raw arguments


class RawArgs(NamedTuple):
    """The raw ``options.args`` one read-only run would get, and whose they are."""

    #: ``architect``, ``architect.model_tiers.light`` or ``reviewers[0]``.
    label: str
    #: ``architect``, ``architect (tier light)`` or ``reviewer <id>``.
    display: str
    #: Set for a reviewer only.
    reviewer_id: str
    provider: str
    args: List[str]
    #: ``project``, ``global`` or ``default``.
    layer: str
    #: The layer ``provider`` came from, by the same rule.
    provider_layer: str = "default"
    #: ``design`` for a design panel seat, else ``code`` (``SeatRun.panel``).
    panel: str = "code"


def _string_args(spec: Dict[str, Any]) -> List[str]:
    options = spec.get("options")
    args = options.get("args") if isinstance(options, dict) else None
    return [str(item) for item in args] if isinstance(args, list) else []


def read_only_raw_args(loaded: LoadedConfig) -> List[RawArgs]:
    """Every read-only run's configured ``options.args``, with its layer.

    Broken entries are skipped: ``validate`` reports those already.
    """
    found: List[RawArgs] = []
    for seat in _read_only_seats(loaded.data, loaded.design_reviewer_origins):
        args = _string_args(seat.spec)
        if seat.kind == "role":
            layer = loaded.layer_of("%s.options.args" % seat.role)
            provider_layer = loaded.layer_of([seat.role, "provider"])
        elif seat.kind == "tier":
            entry = seat.entry or {}
            # A tier's ``options`` replaces the role's whole, so the args are
            # the tier's own when it has options at all.
            if "options" in entry:
                layer = loaded.layer_of([seat.role, "model_tiers", seat.tier, "options", "args"])
            else:
                layer = loaded.layer_of("%s.options.args" % seat.role)
            provider_layer = loaded.layer_of(_tier_provider_path(seat.role, seat.tier, entry))
        else:
            if seat.panel == "design":
                origin = loaded.design_reviewer_origin(seat.position)
            else:
                origin = loaded.reviewer_origin(seat.position)
            if not origin.extra:
                layer = loaded.layer_of("%s[%d].options.args" % (origin.key, origin.position))
                # The list is replaced whole, so every reviewer in it came
                # with the file that set it.
                provider_layer = loaded.layer_of(origin.key)
            else:
                # An extra is the one file's whole entry; ``layer_of`` would ask
                # the project file first for an index into the global list.
                layer = origin.layer if args else "default"
                provider_layer = origin.layer
        provider = str(seat.spec.get("provider") or "")
        found.append(
            RawArgs(
                seat.label, seat.display, seat.reviewer_id, provider, args, layer, provider_layer, seat.panel
            )
        )
    return found


def _tier_provider_path(role: str, tier: str, entry: Dict[str, Any]) -> List[str]:
    """Where a tier's provider is set: the tier, when it names one, else the role."""
    return [role, "model_tiers", tier, "provider"] if entry.get("provider") else [role, "provider"]


def _project_refused(loaded: LoadedConfig) -> List[Tuple[RawArgs, str]]:
    """Read-only runs whose raw arguments come from the project file.

    Refused whatever they are, ``--add-dir`` included. The project file can be
    committed, and it is read without asking, so it can come with the branch
    under review -- and a branch that names its own reviewers' directories
    can widen what they read to anything the user can.
    """
    name = os.path.basename(loaded.project_path or "") or "the project file"
    return [
        (
            entry,
            "%s: options.args is set in the project config (%s); read-only roles take raw "
            "arguments only from the global config or from --extra" % (entry.display, name),
        )
        for entry in read_only_raw_args(loaded)
        if entry.layer == "project" and entry.args
    ]


def _project_seat_refused(loaded: LoadedConfig) -> List[Tuple[RawArgs, str]]:
    """Read-only seats on a warned provider whose provider came with the project file.

    Refused for the reason project raw arguments are: a branch under review
    could otherwise choose its own write-capable reviewer. The same seat from
    the global file or the global preset's fit runs, warned.
    """
    return [
        (entry, message)
        for entry, message in _project_provider_seats(loaded)
        if _warned_provider(entry.provider) is not None
    ]


def _project_provider_seats(loaded: LoadedConfig) -> List[Tuple[RawArgs, str]]:
    """Every read-only seat whose provider came with the project file, with
    the refusal it meets should that provider not hold it to reading."""
    name = os.path.basename(loaded.project_path or "") or "the project file"
    seats: List[Tuple[RawArgs, str]] = []
    for entry in read_only_raw_args(loaded):
        if entry.provider_layer != "project":
            continue
        if entry.reviewer_id or entry.label.startswith(("reviewers[", "review.design.reviewers[")):
            message = project_reviewer_refusal(entry.display, entry.provider, name)
        else:
            path = _seat_provider_path(loaded, entry.label)
            message = project_seat_refusal(entry.display, entry.provider, name, path)
        seats.append((entry, message))
    return seats


def project_provider_refusals(loaded: LoadedConfig) -> Dict[str, str]:
    """``label`` -> the refusal of a read-only seat whose provider came with
    the project file, to apply when that provider reports, live, a status in
    ``WARNED_ENFORCEMENT``.

    For the run paths: an adapter whose report is not static is not asked by
    ``project_raw_arg_refusals``, which never starts a CLI, so the run that
    has the live report decides.
    """
    return {entry.label: message for entry, message in _project_provider_seats(loaded)}


def reviewer_provider_refusals(loaded: LoadedConfig, panel: str = "code") -> Dict[str, str]:
    """The same, by reviewer id, for the review path of ``panel`` (``code`` or ``design``)."""
    return _panel_view(loaded, panel, _project_provider_seats(loaded))


def _panel_view(loaded: LoadedConfig, panel: str, pairs: Sequence[Tuple[RawArgs, str]]) -> Dict[str, str]:
    """``pairs`` by reviewer id, over the seats of one panel.

    Ids repeat across the two panels, so a refusal is looked up in the panel
    that runs. A design seat copied from the code panel that runs as the code
    seat of its id does has no seat of its own (``config._reviewer_seats``),
    and meets that code seat's refusal; a seat a file wrote under
    ``review.design`` always has its own.
    """
    code = _by_key([(entry.reviewer_id, message) for entry, message in pairs if entry.panel == "code"])
    if panel != "design":
        return {key: message for key, message in code.items() if key}
    own = _by_key([(entry.reviewer_id, message) for entry, message in pairs if entry.panel == "design"])
    seats = _reviewer_seats(loaded.data, loaded.design_reviewer_origins)
    seated = {seat.reviewer_id for seat in seats if seat.panel == "design"}
    view = {key: message for key, message in code.items() if key and key not in seated}
    view.update((key, message) for key, message in own.items() if key)
    return view


def project_seat_refusal(display: str, provider: str, file_name: str, provider_path: str) -> str:
    """Why a read-only role or tier on ``provider`` is not taken from the project file."""
    return (
        "%s: provider %s is set in the project config (%s); a read-only seat on %s is taken "
        "only from the global config -- if this is intended, run `dev-orchestra config set "
        "--scope global %s %s` and remove it from %s"
        % (display, provider, file_name, provider, provider_path, provider, file_name)
    )


def project_reviewer_refusal(display: str, provider: str, file_name: str) -> str:
    """Why a reviewer on ``provider`` is not taken from the project file."""
    return (
        "%s: the reviewers list comes from the project config (%s) and this reviewer is on %s; "
        "reviewers on %s are taken only from the global config -- if this is intended, add it "
        "there with `dev-orchestra reviewer add --scope global --provider %s` and remove it from %s"
        % (display, file_name, provider, provider, provider, file_name)
    )


def _seat_provider_path(loaded: LoadedConfig, label: str) -> str:
    """The dotted key that names a role's or a tier's provider."""
    role, _, tier = label.partition(".model_tiers.")
    if tier:
        tiers = (loaded.data.get(role) or {}).get("model_tiers") or {}
        entry = tiers.get(tier) if isinstance(tiers, dict) else None
        if isinstance(entry, dict) and entry.get("provider"):
            return "%s.model_tiers.%s.provider" % (role, tier)
    return "%s.provider" % role


def _all_refused(loaded: LoadedConfig) -> List[Tuple[RawArgs, str]]:
    return _project_refused(loaded) + _project_seat_refused(loaded)


def _by_key(pairs: Sequence[Tuple[str, str]]) -> Dict[str, str]:
    """One message per key: two refusals of one run are said together."""
    merged: Dict[str, str] = {}
    for key, message in pairs:
        merged[key] = "%s; %s" % (merged[key], message) if key in merged else message
    return merged


def project_raw_arg_refusals(loaded: LoadedConfig) -> Dict[str, str]:
    """``label`` -> why that run is refused. Empty when nothing is."""
    return _by_key([(entry.label, message) for entry, message in _all_refused(loaded)])


def reviewer_raw_arg_refusals(loaded: LoadedConfig, panel: str = "code") -> Dict[str, str]:
    """The same refusals, by reviewer id, for the review path of ``panel``."""
    return _panel_view(loaded, panel, _all_refused(loaded))


def read_only_enforcement_warnings(
    data: Dict[str, Any],
    refused: Sequence[str] = (),
    design_origins: Optional[Sequence[ReviewerOrigin]] = None,
) -> List[str]:
    """One line per read-only seat whose provider cannot be held to reading.

    ``data`` is merged configuration, so the wizard can ask about its scratch.
    ``which()`` is not asked: such a provider's status is static, so the line
    is the same whether or not its CLI is installed. ``refused`` holds the
    labels already refused (``project_raw_arg_refusals``), which are not also
    warned about. ``design_origins`` tells a design seat a file wrote from a
    copy of its code seat (``_reviewer_seats``).
    """
    warned = _enforcement_warned_seats(data, refused, design_origins)
    return [line for _label, _reviewer_id, line in warned]


def _enforcement_warned_seats(
    data: Dict[str, Any],
    refused: Sequence[str] = (),
    design_origins: Optional[Sequence[ReviewerOrigin]] = None,
) -> List[Tuple[str, str, str]]:
    """``(label, reviewer id, line)`` for every warned read-only seat.

    The seats ``doctor`` reports enforcement for: the read-only roles, their
    tiers that change provider, and every reviewer of either panel.
    """
    seats = [seat for seat in _read_only_seats(data, design_origins) if not seat.same_provider]
    found: List[Tuple[str, str, str]] = []
    for seat in seats:
        if seat.label in refused:
            continue
        name = str(seat.spec.get("provider") or "")
        enforcement = _warned_provider(name)
        if enforcement is not None:
            line = "%s: %s" % (seat.display, unenforced_warning(name, enforcement))
            found.append((seat.label, seat.reviewer_id, line))
    return found


def reviewer_enforcement_warnings(
    data: Dict[str, Any],
    refused: Sequence[str] = (),
    panel: str = "code",
    design_origins: Optional[Sequence[ReviewerOrigin]] = None,
) -> Dict[str, str]:
    """The warned reviewers' lines by reviewer id, for the review path of ``panel``.

    A design seat with no seat of its own warns as its code seat does; see
    ``_panel_view``.
    """
    warned = [entry for entry in _enforcement_warned_seats(data, refused, design_origins) if entry[1]]
    design_seats = [seat for seat in _reviewer_seats(data, design_origins) if seat.panel == "design"]
    design_labels = {seat.label for seat in design_seats}
    code = {reviewer_id: line for label, reviewer_id, line in warned if label not in design_labels}
    if panel != "design":
        return code
    seated = {seat.reviewer_id for seat in design_seats}
    lines = {reviewer_id: line for reviewer_id, line in code.items() if reviewer_id not in seated}
    lines.update((reviewer_id, line) for label, reviewer_id, line in warned if label in design_labels)
    return lines


def project_write_refusals(loaded: LoadedConfig) -> Dict[str, str]:
    """``label`` -> why that implement-mode run is refused. Empty when nothing is.

    On a provider with ``local_only_options``, no project-file option of a
    write role is honoured: neither one of those options, named whatever its
    value, nor any raw argument, whatever it is. No flag spelling is looked
    at, so none can slip past.
    """
    name = os.path.basename(loaded.project_path or "") or "the project file"
    refused: Dict[str, str] = {}
    for seat in role_seats(loaded.data, WRITE_ROLES):
        if seat.kind == "role":
            options_path: List[Any] = [seat.role, "options"]
        # A tier's ``options`` replaces the role's whole; one that changes
        # provider without any has none at all.
        elif "options" in (seat.entry or {}):
            options_path = [seat.role, "model_tiers", seat.tier, "options"]
        elif not seat.same_provider:
            continue
        else:
            options_path = [seat.role, "options"]
        provider = str(seat.spec.get("provider") or "")
        try:
            local_only = list(get_provider(provider).local_only_options) if provider else []
        except Exception:
            continue  # an unknown provider or a broken adapter is validate's to report
        if not local_only:
            continue
        named = [key for key in local_only if loaded.layer_of([*options_path, key]) == "project"]
        args = loaded.layer_of([*options_path, "args"]) == "project" and bool(_string_args(seat.spec))
        if not named and not args:
            continue
        keys = " / ".join(["options.%s" % key for key in local_only] + ["options.args"])
        refused[seat.label] = (
            "%s: %s is set in the project config (%s); on %s the permission bypass and raw "
            "arguments are taken only from the global config or from --extra"
            % (seat.display, keys, name, provider)
        )
    return refused


def read_only_arg_warnings(loaded: LoadedConfig) -> List[str]:
    """What a read-only run would refuse, said before anything is run.

    Deliberately not part of ``validate``: that makes ``load`` raise, and
    every command -- ``status``, ``budget`` -- would stop over one role's raw
    arguments when only that role's runs are refused.

    Also the refusals of the other two kinds that come from the project file:
    a read-only seat on a warned provider, and a write role's options on a
    provider that takes them only from the global config.
    """
    warnings = [message for _entry, message in _all_refused(loaded)]
    warnings.extend(project_write_refusals(loaded).values())
    for entry in read_only_raw_args(loaded):
        if entry.layer == "project" or not entry.args:
            continue
        try:
            problems = get_provider(entry.provider).refused_read_only_args(entry.args, "options.args")
        except Exception:
            continue  # an unknown provider or a broken adapter is validate's to report
        warnings.extend("%s: %s" % (entry.display, problem) for problem in problems)
    return warnings
