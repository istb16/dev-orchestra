"""Which read-only seats and write-role options are refused or warned about.

What comes from the project file is refused where it could loosen a run; a
read-only seat on a provider that cannot be held to reading is warned about.
So is a project file that loosens a review or approval gate it may still set.
Importing this module loads the provider registry.
"""

from __future__ import annotations

import math
import os
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import config_trust, presets
from . import optimization as opt_mod
from .config import (
    WRITE_ROLES,
    LoadedConfig,
    ReviewerOrigin,
    _read_only_seats,
    _reviewer_seats,
    compose_loaded,
    design_review_mode,
    get_path,
    role_seats,
    workspace_dir_in,
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

    Also the refusals of the other kinds that come from the project file:
    a read-only seat on a warned provider, and a write role's options on a
    provider that takes them only from the global config. The settings only
    the global config may make are ``project_ignored``'s, which each caller
    adds as its report needs them.
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


# --------------------------------------------------------------------------- global-only settings


def _shown(value: Any) -> str:
    """A value as it is written in YAML: ``false``, not ``False``."""
    return str(value).lower() if isinstance(value, bool) else str(value)


class Ignored(NamedTuple):
    """A setting the project file makes that only the global config may, as reported."""

    line: str
    #: Whether, taken, it would have loosened what is in force: a ``false``
    #: approval over a required one, or a workspace other than the one used.
    #: ``doctor`` reports only these as problems; the rest change nothing.
    loosens: bool


def project_ignored(loaded: LoadedConfig) -> List[Ignored]:
    """One entry per setting the project file makes that only the global config may.

    ``config.trusted_project_layer`` has already left them out, a
    ``workspace.dir`` a link takes out of the repository where the workspace
    is resolved in ``loaded.root``; this says so, in the same repository,
    with what is in force instead and the command that would set it.
    """
    if not loaded.project_layer:
        return []  # nothing to leave out, and no repository to ask git for
    name = os.path.basename(loaded.project_path or "") or "the project file"
    used = workspace_dir_in(loaded.root, loaded.data, loaded.global_layer, loaded.project_layer)
    entries: List[Ignored] = []
    for key, value in config_trust.ignored(loaded.project_layer, loaded.root):
        how = ""
        if key == config_trust.WORKSPACE_DIR and not config_trust.outside_repository(value):
            if value == used:
                continue  # the global file names the same directory, so nothing moved
            how = ", where a link takes it outside the repository,"
        if key == config_trust.APPROVAL:
            required = bool(loaded.design_settings().get("require_approval"))
            why = "plan approval is taken only from the global config, so it stays %s" % (
                "required" if required else "not required"
            )
            loosens = required and not value
        else:
            why = "a workspace outside the repository is taken only from the global config, so %s is used" % (
                used
            )
            loosens = value != used
        entries.append(
            Ignored(
                "%s: %s is set in the project config (%s)%s and is ignored; %s -- if this is intended, run "
                "`dev-orchestra config set --scope global %s %s` and remove it from %s"
                % (key, _shown(value), name, how, why, key, _shown(value), name),
                loosens,
            )
        )
    return entries


def project_ignored_warnings(loaded: LoadedConfig) -> List[str]:
    """``project_ignored`` as lines: what ``config validate`` and ``review run`` warn about."""
    return [entry.line for entry in project_ignored(loaded)]


def global_only_write(dotted: str, value: Any, file_name: str = "the project file") -> str:
    """Why ``config set --scope project <dotted> <value>`` writes nothing, or "".

    Also how ``config set`` with no scope tells such a write goes to the
    global file: the same rule answers both.
    """
    found = config_trust.written_ignored(dotted, value)
    if not found:
        return ""
    key, ignored_value = found[0]
    what = key if key == config_trust.APPROVAL else "a workspace.dir outside the repository"
    return (
        "%s is taken only from the global config: the project config (%s) can come with the branch "
        "under review -- run `dev-orchestra config set --scope global %s %s` instead"
        % (what, file_name, key, _shown(ignored_value))
    )


def approval_ignored_note(loaded: LoadedConfig) -> str:
    """The ``note:`` a refused ``run implementer`` adds when the project file tried to turn approval off."""
    for key, value in config_trust.ignored(loaded.project_layer):
        if key == config_trust.APPROVAL and not value:
            name = os.path.basename(loaded.project_path or "") or "the project file"
            return (
                "note: design.require_approval: %s in the project config (%s) is ignored; only the "
                "global config can turn plan approval off" % (_shown(value), name)
            )
    return ""


# --------------------------------------------------------------------------- loosened gates


#: ``review.design.enabled`` from the least to the most careful.
_DESIGN_MODES = ("off", "auto", "on")


class _Gate(NamedTuple):
    """One review or approval gate a project file may set, and how it gets looser."""

    #: The key the notice names.
    key: str
    #: The keys a project file writes to move it; any one counts.
    written: Tuple[str, ...]
    #: ``(in force, without the project file)`` -> what got looser, or "".
    looser: Callable[[LoadedConfig, LoadedConfig], str]


def _ids(panel: Sequence[Any]) -> List[str]:
    return [str(seat["id"]) for seat in panel if isinstance(seat, dict) and seat.get("id")]


def _dropped(now: Sequence[Any], before: Sequence[Any]) -> List[Any]:
    """What ``before`` held that ``now`` does not, in ``before``'s order."""
    kept = set(now)
    return [item for item in before if item not in kept]


def _listed(items: Sequence[Any]) -> str:
    shown = ", ".join(str(item) for item in items[:5])
    return shown + (" (and %d more)" % (len(items) - 5) if len(items) > 5 else "")


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _code_panel(now: LoadedConfig, before: LoadedConfig) -> str:
    dropped = _dropped(_ids(now.reviewers()), _ids(before.reviewers()))
    return "drops %s from the code review panel" % _listed(dropped) if dropped else ""


def _design_panel(now: LoadedConfig, before: LoadedConfig) -> str:
    if not (now.has_design_panel() or before.has_design_panel()):
        return ""  # both run the code panel, which is reported on its own
    dropped = _dropped(_ids(now.design_reviewers()), _ids(before.design_reviewers()))
    return "drops %s from the design review panel" % _listed(dropped) if dropped else ""


def _lower(read: Callable[[LoadedConfig], Any]) -> Callable[[LoadedConfig, LoadedConfig], str]:
    """A count that lets more through as it goes down."""

    def looser(now: LoadedConfig, before: LoadedConfig) -> str:
        new, old = _int(read(now)), _int(read(before))
        if new is None or old is None or new >= old:
            return ""
        return "lowers it to %d from %d" % (new, old)

    return looser


def _higher(read: Callable[[LoadedConfig], Any]) -> Callable[[LoadedConfig, LoadedConfig], str]:
    """A threshold that lets more through as it goes up."""

    def looser(now: LoadedConfig, before: LoadedConfig) -> str:
        new, old = _int(read(now)), _int(read(before))
        if new is None or old is None or new <= old:
            return ""
        return "raises it to %d from %d" % (new, old)

    return looser


def _severities(loaded: LoadedConfig) -> List[str]:
    """As ``review status`` reads them: in lower case, the default pair for anything unusable."""
    return list(loaded.blocking_severities())


def _re_review(now: LoadedConfig, before: LoadedConfig) -> str:
    dropped = _dropped(_severities(now), _severities(before))
    return "no longer asks for a re-review over %s findings" % _listed(dropped) if dropped else ""


def _exclude(loaded: LoadedConfig) -> List[str]:
    configured = loaded.review_settings().get("exclude")
    return [item for item in configured if isinstance(item, str)] if isinstance(configured, list) else []


def _withheld(now: LoadedConfig, before: LoadedConfig) -> str:
    added = _dropped(_exclude(before), _exclude(now))
    return "also withholds the diff of %s from reviewers" % _listed(added) if added else ""


def _design_mode(now: LoadedConfig, before: LoadedConfig) -> str:
    new = design_review_mode(now.design_review_settings().get("enabled"))
    old = design_review_mode(before.design_review_settings().get("enabled"))
    if _DESIGN_MODES.index(new) >= _DESIGN_MODES.index(old):
        return ""
    return "turns it %s from %s" % (new, old)


def _findings_cap(loaded: LoadedConfig) -> float:
    """``review.max_findings`` as a run asks for it; 0 lifts the cap."""
    cap = opt_mod.findings_cap(loaded.optimization_settings(), loaded.review_settings())
    return math.inf if cap == 0 else cap


def _fewer_findings(now: LoadedConfig, before: LoadedConfig) -> str:
    new, old = _findings_cap(now), _findings_cap(before)
    if new >= old:
        return ""
    return "asks each reviewer for at most %d findings, not %s" % (
        new,
        "any number" if old == math.inf else "%d" % old,
    )


def _level(now: LoadedConfig, before: LoadedConfig) -> str:
    new = opt_mod.normalise_level(now.optimization_settings().get("level"))
    old = opt_mod.normalise_level(before.optimization_settings().get("level"))
    if opt_mod.LEVELS.index(new) >= opt_mod.LEVELS.index(old):
        return ""
    return "lowers it to %s from %s" % (new, old)


def _patterns(read: Callable[[Dict[str, Any]], List[str]]) -> Callable[[LoadedConfig, LoadedConfig], str]:
    """A pattern list, with its ``extra_`` list, that catches less as it loses entries."""

    def looser(now: LoadedConfig, before: LoadedConfig) -> str:
        dropped = _dropped(read(now.optimization_settings()), read(before.optimization_settings()))
        return "drops the patterns %s" % _listed(dropped) if dropped else ""

    return looser


def _skips(now: LoadedConfig, before: LoadedConfig) -> str:
    new = now.optimization_settings().get("skip_unneeded_roles") is not False
    old = before.optimization_settings().get("skip_unneeded_roles") is not False
    return "turns it on" if new and not old else ""


#: Every review gate a project file may still set, and how it gets looser.
#: Read through the accessors the runs read it through, so a value no run
#: would take as written (a string count, an unknown level) is compared as
#: the run would take it; ``validate`` reports the value itself.
_GATES = (
    _Gate("reviewers", ("reviewers",), _code_panel),
    _Gate("review.design.reviewers", ("reviewers", "review.design.reviewers"), _design_panel),
    _Gate(
        "review.max_review_iterations",
        ("review.max_review_iterations",),
        _lower(lambda loaded: loaded.review_settings().get("max_review_iterations")),
    ),
    _Gate("review.re_review_severities", ("review.re_review_severities",), _re_review),
    _Gate("review.exclude", ("review.exclude",), _withheld),
    _Gate("review.max_findings", ("review.max_findings",), _fewer_findings),
    _Gate("review.design.enabled", ("review.design.enabled",), _design_mode),
    _Gate(
        "review.design.max_iterations",
        ("review.design.max_iterations",),
        _lower(lambda loaded: loaded.design_review_settings().get("max_iterations")),
    ),
    _Gate("optimization.level", ("optimization.level",), _level),
    _Gate(
        "optimization.high_risk_paths",
        ("optimization.high_risk_paths", "optimization.extra_high_risk_paths"),
        _patterns(opt_mod.risk_patterns),
    ),
    _Gate(
        "optimization.low_risk_max_files",
        ("optimization.low_risk_max_files",),
        _higher(lambda loaded: loaded.optimization_settings().get("low_risk_max_files")),
    ),
    _Gate(
        "optimization.low_risk_max_lines",
        ("optimization.low_risk_max_lines",),
        _higher(lambda loaded: loaded.optimization_settings().get("low_risk_max_lines")),
    ),
    _Gate("optimization.skip_unneeded_roles", ("optimization.skip_unneeded_roles",), _skips),
    _Gate(
        "optimization.security_paths",
        ("optimization.security_paths", "optimization.extra_security_paths"),
        _patterns(opt_mod.security_patterns),
    ),
    _Gate(
        "optimization.architecture_paths",
        ("optimization.architecture_paths", "optimization.extra_architecture_paths"),
        _patterns(opt_mod.architecture_patterns),
    ),
)


def _without_project(loaded: LoadedConfig) -> Optional[LoadedConfig]:
    """The configuration the global file, its preset's fit and the defaults give alone.

    Fitted to the CLIs ``loaded`` was, without asking for them again.
    """
    installed = loaded.installed if loaded.installed is not None else presets.installed_providers()
    try:
        return compose_loaded(loaded.global_layer, {}, installed, loaded.global_path, None, loaded.start)
    except Exception:
        return None  # a global file that does not compose is validate's to report


def project_loosening_notices(loaded: LoadedConfig) -> List[str]:
    """One line per review gate the project file makes looser.

    Looser than the same configuration without the project file: the global
    file, the global preset's fit and the defaults. Only a gate the project
    file writes is compared, so a panel dealt differently around a project's
    implementer is not one. These still take effect -- a repository may
    review less on purpose -- but the file can come with the branch under
    review, so ``doctor``, ``config validate`` and ``review run`` say so.
    """
    project = loaded.project_layer
    written = {key for gate in _GATES for key in gate.written if get_path(project, key) is not None}
    if not written:
        return []
    before = _without_project(loaded)
    if before is None:
        return []
    name = os.path.basename(loaded.project_path or "") or "the project file"
    notices: List[str] = []
    for gate in _GATES:
        if not written.intersection(gate.written):
            continue
        try:
            detail = gate.looser(loaded, before)
        except Exception:
            continue  # a value no run could read is validate's to report
        if detail:
            notices.append(
                "%s: the project config (%s) %s; that file can come with the branch under review, "
                "so check that this is intended" % (gate.key, name, detail)
            )
    return notices
