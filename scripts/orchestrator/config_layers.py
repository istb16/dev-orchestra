"""Where a ``config`` writer finds the layer it edits, and how it rewrites the panel.

Only ``cli_config`` and the setup wizard write a configuration file, so these
helpers live apart from the ones every command shares (``cli_common``).
"""

from __future__ import annotations

import copy
import os
import re
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from . import config as config_mod
from . import presets as presets_mod
from .providers import warned_provider


def _resolve_scope(requested: Optional[str], start: Optional[str] = None) -> str:
    if requested in ("global", "project"):
        return requested
    return "project" if config_mod.find_project_config(start) else "global"


def _layer_path(scope: str, start: Optional[str] = None) -> str:
    return config_mod.global_config_path() if scope == "global" else config_mod.project_config_path(start)


def _read_layer(scope: str, start: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """The layer a writer is about to edit: what is on disk, and nothing else.

    The global layer used to be seeded with the whole of ``default_config()``,
    so the first ``config set`` froze every default beside the one value that
    was asked for. ``version`` identifies the file format rather than
    configuring anything, so it is the one key a writer supplies -- including
    into a file that predates it, which is the only way an edit to such a file
    can leave a file this loader's contract describes.
    """
    path = _layer_path(scope, start)
    layer = config_mod.read_config_file(path) if os.path.isfile(path) else {}
    layer.setdefault("version", config_mod.CONFIG_VERSION)
    return path, layer


def _layer_base(scope: str, start: Optional[str] = None) -> Dict[str, Any]:
    """What would be in force if this layer did not exist.

    The global file is shared by every project on the machine, so no project's
    values may flow into it: its base is the built-in defaults alone. A project
    file sits on top of the global one, so its base is the defaults plus that.
    ``load()`` is the wrong answer for either -- it includes the layer being
    edited, and for the global layer it includes whichever project happens to
    be the working directory.

    This mirrors the layer order in ``config.load``; a third layer would have
    to be added in both places. It leaves out the preset's expansion:
    ``_fitted_base`` is the base with it, through ``config.compose``, and
    ``_prune_base`` is where the two agree.
    """
    defaults = config_mod.default_config()
    if scope == "global":
        return defaults
    global_path = config_mod.global_config_path()
    if not os.path.isfile(global_path):
        return defaults
    return config_mod.deep_merge(defaults, config_mod.read_config_file(global_path))


def _global_file() -> Dict[str, Any]:
    path = config_mod.global_config_path()
    return config_mod.read_config_file(path) if os.path.isfile(path) else {}


def _fitted_base(scope: str, layer: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What ``load()`` gives without this layer, the preset's fit included.

    The global layer's base is the defaults plus the expansion of its own
    preset, read as ``config.compose`` reads it (``standard`` when it names
    none, nothing for a name it does not know); a project layer's is
    ``load()`` without the project file. The panel is still dealt around the
    implementer ``layer`` -- the one being edited -- sets, as it is in force.
    """
    installed = presets_mod.installed_providers()
    if scope == "global":
        return config_mod.compose({"preset": _global_file().get("preset")}, {}, installed, layer)[0]
    return config_mod.compose(_global_file(), {}, installed, layer)[0]


def _agreed(first: Dict[str, Any], second: Dict[str, Any], whole: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """The values ``first`` and ``second`` both hold; the keys in ``whole`` only when equal whole."""
    agreed: Dict[str, Any] = {}
    for key, value in first.items():
        if key not in second:
            continue
        other = second[key]
        if isinstance(value, dict) and isinstance(other, dict) and key not in whole:
            agreed[key] = _agreed(value, other)
        elif value == other:
            agreed[key] = copy.deepcopy(value)
    return agreed


def _prune_base(scope: str, start: Optional[str], layer: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """What ``config prune`` compares ``layer`` against: ``(base, held)``.

    A value may go only when the built-in defaults and the preset's fit both
    say it: a governed key the file stops setting falls through to the fit,
    and nothing may be pruned for equalling one machine's fit either. A role
    counts only whole, since dropping its last field hands it to the fit.

    A project file that sets the implementer deals the panel around it, so
    for the global layer the fit that project sees has to agree as well.

    Nor may ``reviewers`` go while it is what keeps the design rounds off
    the preset's design panel (``_reviewers_hold_design_panel``): it governs
    that panel too. ``held`` names it when it would otherwise have gone.
    """
    roles = config_mod.KNOWN_ROLES
    base = _agreed(_layer_base(scope, start), _fitted_base(scope, layer), roles)
    if scope == "global":
        project_path = config_mod.find_project_config(start)
        project = config_mod.read_config_file(project_path) if project_path else {}
        if config_mod.mentions(project, "implementer"):
            with_project = config_mod.deep_merge(layer, project)
            base = _agreed(base, _fitted_base(scope, with_project), roles)
    held: List[str] = []
    if _reviewers_hold_design_panel(scope, layer) and base.pop("reviewers", None) == layer["reviewers"]:
        held.append("reviewers")
    return base, held


def _reviewers_hold_design_panel(scope: str, layer: Dict[str, Any]) -> bool:
    """Whether this layer's ``reviewers`` is all that keeps design rounds off the fit's design panel.

    Without it the design panel would be the preset's
    (``config.design_panel_follows_fit``), so writing or dropping it moves
    the design rounds as well as the code rounds.
    """
    if layer.get("reviewers") is None:
        return False
    without = {key: value for key, value in layer.items() if key != "reviewers"}
    return config_mod.design_panel_is_fit(_compose_preview(scope, without)[1].design_origins)


#: Sentinel for "the layer holds nothing here at all", which ``get_path`` cannot
#: otherwise distinguish from a value that happens to be falsy.
_UNSET = object()


def _seed_list(layer: Dict[str, Any], list_path: str, base: Dict[str, Any]) -> bool:
    """Give the layer the whole list before one entry of it is edited.

    A list replaces the one below it wholesale (``deep_merge``), so changing
    one entry is also a decision about the others: they have to be copied from
    what this layer was inheriting, which is ``base`` and never the effective
    configuration -- a global file must not end up holding the panel of
    whichever project the command was run in.

    A path neither side knows is left alone, for ``set_path`` to reject as it
    always has -- and so is one this layer already holds something else at. A
    scalar where a list is expected is the same kind of mistake, and seeding
    over it would replace a value the user wrote instead of refusing.

    Returns True when it seeded, so a writer can say it froze the panel.
    """
    if config_mod.get_path(layer, list_path, _UNSET) is not _UNSET:
        return False
    inherited = config_mod.get_path(base, list_path)
    if isinstance(inherited, list):
        config_mod.set_path(layer, list_path, copy.deepcopy(inherited))
        return True
    return False


def _compose_preview(scope: str, layer: Dict[str, Any]) -> Tuple[Any, ...]:
    """``config.compose`` of this layer as it would be saved: ``(data, fit, preset, source)``."""
    installed = presets_mod.installed_providers()
    if scope == "global":
        return config_mod.compose(layer, {}, installed)
    return config_mod.compose(_global_file(), layer, installed)


#: The one panel-wide problem that names an entry only as an example: which
#: high-risk reviewer it names says nothing about whether a write caused it.
_NO_RISK_PATTERN = "optimization.high_risk_paths: no pattern in force"

#: How a problem names a panel entry: an extra by its file and position, any
#: other reviewer by its place in the panel -- the design panel's first, so
#: ``review.design.reviewers[2]`` is not read as the code panel's entry.
_ENTRY_LABEL = re.compile(
    r"(review\.design\.)?(?:reviewers_extra\[(\d+)\] in the (global|project) file|reviewers\[(\d+)\])"
)

#: Where a panel problem starts: either panel, or the high-risk patterns rule.
_PANEL_PROBLEMS = ("reviewers", config_mod.DESIGN_PANEL.reviewers, _NO_RISK_PATTERN)


class _PanelIds(NamedTuple):
    """Each panel's ids, by position, as a problem's entry labels count them."""

    code: List[Optional[str]]
    design: List[Optional[str]]


def _ids(panel: Any) -> List[Optional[str]]:
    return [
        reviewer.get("id") if isinstance(reviewer, dict) and isinstance(reviewer.get("id"), str) else None
        for reviewer in (panel if isinstance(panel, list) else [])
    ]


def _panel_problems(scope: str, layer: Dict[str, Any]) -> Tuple[List[str], _PanelIds]:
    """The panel problems ``load()`` would raise with this layer saved, and the panels' ids."""
    data, fit, _preset, _source = _compose_preview(scope, layer)
    global_layer, project_layer = (layer, None) if scope == "global" else (_global_file(), layer)
    problems = config_mod.validate(
        data,
        project_layer=project_layer,
        global_layer=global_layer,
        origins=fit.origins,
        design_origins=fit.design_origins,
    )
    ids = _PanelIds(
        _ids(data.get("reviewers")), _ids(config_mod.get_path(data, config_mod.DESIGN_PANEL.reviewers))
    )
    return [problem for problem in problems if problem.startswith(_PANEL_PROBLEMS)], ids


def _problem_key(problem: str, ids: _PanelIds, removed: Any = None) -> Optional[str]:
    """A problem with each entry it names said by what stays the same across a write.

    A reviewer of a panel by its id, which a removal or a seeded list does
    not move; an extra by its file and position, less one past an extra the
    write ``removed`` -- and None for a problem of that extra itself, which
    the write took away.
    """
    if problem.startswith(_NO_RISK_PATTERN):
        return _NO_RISK_PATTERN
    gone = False

    def entry(match: "re.Match[str]") -> str:
        nonlocal gone
        prefix = match.group(1) or ""
        if match.group(4) is not None:
            index = int(match.group(4))
            panel = ids.design if prefix else ids.code
            name = panel[index] if index < len(panel) else None
            return "%sreviewers{%s}" % (prefix, name if name is not None else "#%d" % index)
        position, layer = int(match.group(2)), match.group(3)
        key = prefix + "reviewers_extra"
        if removed is not None and removed.key == key and removed.layer == layer:
            if position == removed.position:
                gone = True
            elif position > removed.position:
                position -= 1
        return "%s[%d] in the %s file" % (key, position, layer)

    key = _ENTRY_LABEL.sub(entry, problem)
    return None if gone else key


def _renamed_in_place(old_ids: List[Optional[str]], new_ids: List[Optional[str]]) -> List[Optional[str]]:
    """``new_ids`` with a reviewer renamed in place called by its old id: it is still the one it was."""
    kept = set(new_ids)
    return [
        old_ids[index]
        if name not in old_ids and index < len(old_ids) and old_ids[index] not in kept
        else name
        for index, name in enumerate(new_ids)
    ]


def _panel_write_problems(
    scope: str,
    before: Dict[str, Any],
    after: Dict[str, Any],
    removed: Any = None,
) -> List[str]:
    """The panel problems a write from ``before`` to ``after`` would introduce.

    The one check every writer of a panel path makes before writing: the
    configuration is composed as it would be saved, both files and the
    extras included, so nothing is written that ``load()`` then refuses.
    Only what the write itself introduced is refused, so an already broken
    file stays fixable -- a problem counts as already there only for the
    same entry, so the same mistake made on another one is still refused.
    ``removed`` is the origin of an extra the write took out of its file.
    """
    old_problems, old_ids = _panel_problems(scope, before)
    new_problems, new_ids = _panel_problems(scope, after)
    # A reviewer renamed in place is still the one it was.
    names = _PanelIds(
        _renamed_in_place(old_ids.code, new_ids.code), _renamed_in_place(old_ids.design, new_ids.design)
    )
    known = {_problem_key(problem, old_ids, removed) for problem in old_problems}
    return [problem for problem in new_problems if _problem_key(problem, names) not in known]


def _frozen_panel_note(
    seeded: bool,
    path: str,
    base: Dict[str, Any],
    scope: str,
    start: Optional[str] = None,
    layer: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """What a writer says when seeding ``reviewers`` took the panel off the fit.

    Only when no file under the written one listed the reviewers before:
    copying a list another file already chose freezes nothing that was
    following a preset. A project file is not under the global one.

    ``layer`` is the file as seeded. When the list is all that keeps the
    design rounds off the preset's design panel, it moved them as well, and
    the note says so.
    """
    if not seeded:
        return None
    loaded = config_mod.load(start, validate_result=False)
    layers = (loaded.global_layer, loaded.project_layer) if scope == "project" else (loaded.global_layer,)
    if not loaded.preset or any(listed.get("reviewers") is not None for listed in layers):
        return None
    message = "note: %s now lists the reviewers; the panel no longer follows preset %s's fit (recorded %s)"
    note = message % (config_mod.shown_location(path), loaded.preset, _recorded(base.get("reviewers")))
    if layer is not None and _reviewers_hold_design_panel(scope, layer):
        moved = "; design rounds now run this list without when, not preset %s's design panel"
        note += moved % loaded.preset
    return note


def _recorded(reviewers: Any) -> str:
    """``claude-general opus, codex-general recommended-coding``: what a frozen note says was copied."""
    recorded = ", ".join(
        "%s %s" % (reviewer.get("id"), (reviewer.get("model") or {}).get("family", "default"))
        for reviewer in reviewers or []
        if isinstance(reviewer, dict)
    )
    return recorded or "none"


#: What a writer says of the seats it left out of a panel copied into a project file.
_NOT_COPIED = "not copied into %s: %s -- a reviewer on %s is taken only from the global config"


def _global_lists(key: str) -> bool:
    """Whether the global file lists the panel at ``key`` itself."""
    return config_mod.get_path(_global_file(), key) is not None


def _without_warned_seats(
    reviewers: List[Any], scope: str, chosen: Callable[[int], bool]
) -> Tuple[List[Any], List[Tuple[str, str]]]:
    """``(kept, dropped)``: the seats copied into a file, and ``(id, provider)`` of those left out.

    The one rule both panels' seeding follows. A project file may not hold a
    reviewer on a warned provider, so a seat on one is left out of a copy into
    a project file -- unless ``chosen(index)`` says a file listed it: the user
    chose that seat, it is copied whole, and the write's warnings say the
    project file will have it refused. Every seat is kept in global scope.
    """
    if scope != "project":
        return list(reviewers), []
    kept: List[Any] = []
    dropped: List[Tuple[str, str]] = []
    for index, reviewer in enumerate(reviewers):
        provider = str(reviewer.get("provider") or "") if isinstance(reviewer, dict) else ""
        if warned_provider(provider) and not chosen(index):
            dropped.append((str(reviewer.get("id")), provider))
        else:
            kept.append(reviewer)
    return kept, dropped


def _not_copied(path: str, dropped: List[Tuple[str, str]]) -> str:
    """``_NOT_COPIED`` for the seats ``_without_warned_seats`` left out of ``path``."""
    ids = ", ".join(reviewer_id for reviewer_id, _provider in dropped)
    providers = ", ".join(sorted({provider for _id, provider in dropped}))
    return _NOT_COPIED % (os.path.basename(path), ids, providers)


def _seed_panel(
    scope: str, layer: Dict[str, Any], base: Dict[str, Any], path: str, start: Optional[str] = None
) -> Tuple[Optional[str], Optional[str]]:
    """``_seed_list`` for ``reviewers``, and what the writer says about it.

    A project file may not hold a reviewer on a warned provider, so when the
    panel copied into one is the global preset's fit -- the global file lists
    no reviewers -- those seats are left out of the copy and named. A panel
    the global file lists is copied whole (``_without_warned_seats``).

    Returns the note for a write that succeeds, and the note for one that
    fails -- a selector or an index that named a seat left out would
    otherwise read as a plain "not found".
    """
    dropped: List[Tuple[str, str]] = []
    reviewers = base.get("reviewers")
    if scope == "project" and isinstance(reviewers, list):
        listed = _global_lists(config_mod.CODE_PANEL.reviewers)
        kept, dropped = _without_warned_seats(reviewers, scope, lambda _index: listed)
        base = dict(base, reviewers=kept)
    seeded = _seed_list(layer, "reviewers", base)
    frozen = _frozen_panel_note(seeded, path, base, scope, start, layer)
    if not (seeded and dropped):
        return frozen, None
    left_out = _not_copied(path, dropped)
    if frozen:
        frozen += "; " + left_out
    return frozen, "note: " + left_out
