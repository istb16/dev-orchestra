"""Configuration loading, layering, validation and persistence.

Precedence (highest first):

1. project config   -- ``.dev-orchestra.yaml`` found by walking up from cwd
2. global config    -- OS-appropriate user config directory
3. the global file's preset (``standard`` when it names none), fitted to the
   installed CLIs -- minus every role a file sets and, when a file lists
   ``reviewers``, the panel (``compose``; see ``presets``)
4. built-in defaults

Only *model families* and a *version policy* are persisted. Concrete model ids
are resolved at run time by the provider adapters so that the configuration
keeps following whatever the CLI currently considers "latest".
"""

from __future__ import annotations

import copy
import json
import os
from typing import TYPE_CHECKING, Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple, Union

from . import miniyaml
from .config_common import (  # noqa: F401 - re-exported
    BUILTIN_ROLES,
    CONFIG_VERSION,
    KNOWN_ROLES,
    PROGRESS_INTERVAL_MAX,
    PROGRESS_INTERVAL_MIN,
    PROMPTED_ROLES,
    READ_ONLY_ROLES,
    RUN_TIMEOUT_DEFAULTS,
    STALE_NOTICE_MAX_DAYS,
    WRITE_ROLES,
    WRITE_RUN_IDLE_TIMEOUT_SECONDS,
    ConfigError,
    Implementation,
    Progress,
    approval_mode,
    approval_mode_shown,
    ask_questions_mode,
    default_config,
    design_review_mode,
    implementation_settings_of,
    is_valid_reviewer_id,
    language_settings_of,
    normalise_language_tag,
    progress_settings_of,
    progress_updates_mode,
    trusted_project_layer,
    whole_seconds,
    workspace_dir_in,
    workspace_dir_of,
)
from .config_keys import (  # noqa: F401 - re-exported
    coerce_scalar,
    get_parts,
    get_path,
    pop_path,
    set_path,
    split_path,
)
from .config_paths import (  # noqa: F401 - re-exported
    APP_DIR_NAME,
    PROJECT_CONFIG_NAMES,
    describe_location,
    find_project_config,
    global_config_dir,
    global_config_path,
    package_redirected,
    project_config_path,
    repository_root,
    shown_location,
    user_providers_dir,
    user_providers_hint,
)
from .config_reviewers import (  # noqa: F401 - re-exported
    CODE_PANEL,
    DESIGN_PANEL,
    PanelKeys,
    ReviewerOrigin,
    add_extra_reviewer,
    add_reviewer,
    default_reviewer_family,
    find_reviewer,
    make_reviewer,
    remove_reviewer,
    suggest_reviewer_id,
)
from .config_seats import (  # noqa: F401 - re-exported
    SeatRun,
    merge_tier,
    read_only_seats,
    reviewer_seats,
    role_seats,
    same_run,
)
from .config_validation import unknown_key_warnings, unknown_keys, validate  # noqa: F401 - re-exported
from .review_common import SEVERITIES

if TYPE_CHECKING:
    from .presets import Fit

# Two imports stay inside the functions that need them, and each one points
# back here. The provider registry: importing it runs its bootstrap, which asks
# ``config_paths`` for ``user_providers_dir`` and imports every user adapter, and
# configuration must stay loadable without the provider registry. Presets:
# ``presets`` builds its presets from ``default_config`` when it is imported,
# so it can only be imported once this module is complete.

_MISSING = object()


# --------------------------------------------------------------------------- io


def read_config_file(path: str) -> Dict[str, Any]:
    # utf-8-sig: Notepad and PowerShell 5's `Out-File -Encoding utf8` start the
    # file with a BOM, which would otherwise become part of the first key.
    with open(path, "r", encoding="utf-8-sig") as handle:
        raw = handle.read()
    try:
        data = miniyaml.loads(raw)
    except Exception as exc:
        raise ConfigError("%s: %s" % (path, exc)) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("%s: top level must be a mapping" % path)
    return data


def layer_below(scope: str = "") -> str:
    """What a layer inherits from, named the way a message can use it.

    A project file sits on the global one, not on the built-in defaults, and it
    is the file that usually gets committed and read by the whole team -- so
    the header it carries has to say which of the two it follows.
    """
    if scope == "global":
        return "the built-in defaults"
    if scope == "project":
        return "the global layer"
    return "the layer below"


def _no_json_type(path: str) -> Callable[[Any], Any]:
    """``json.dumps``' ``default``: refuse a value JSON has no type for, naming it."""

    def refuse(value: Any) -> Any:
        raise ConfigError(
            "%s: JSON has no type for %r (%s); quote it to keep it as text, or keep the setting "
            "in a YAML file" % (shown_location(path), value, type(value).__name__)
        )

    return refuse


def write_config_file(path: str, data: Dict[str, Any], scope: str = "") -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if path.lower().endswith(".json"):
        # A `.dev-orchestra.json` is also read by editors, jq and CI checks,
        # none of which take YAML or a comment; so JSON, and no header.
        # Strict JSON: `Infinity` and `NaN` are Python's, and jq, editors and
        # this tool's own reader would refuse the file. Checked before the
        # file is opened, so a refusal leaves it as it was. Nor a date, which
        # PyYAML reads `2026-10-06` in a block value as: JSON has no type for it.
        refuse = _no_json_type(path)
        try:
            text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False, default=refuse)
        except ConfigError:
            raise  # `refuse`'s own, which names the value
        except ValueError:
            raise ConfigError(
                "%s: JSON has no infinite or not-a-number value (.inf, .nan); "
                "write a finite number, or keep the setting in a YAML file" % shown_location(path)
            ) from None
        except TypeError as exc:  # a key JSON cannot hold, which `default` is not asked about
            shown = shown_location(path)
            raise ConfigError("%s: %s; keep the setting in a YAML file" % (shown, exc)) from None
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
        return
    text = miniyaml.dumps(data)
    header = (
        "# dev-orchestra configuration\n"
        "# Only what you set is stored; everything else follows %s.\n"
        "# Model families + a version policy are stored here on purpose: concrete\n"
        "# model ids are resolved by the provider adapters at run time.\n" % layer_below(scope)
    )
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(header + text)


# --------------------------------------------------------------------------- merge


#: Keys an explicit null in the higher layer sets to null, where any other
#: null keeps the value below. A project's ``language.reply: null`` must undo
#: a global tag, so replies there follow the user's language again; this is
#: also why ``layer_of`` counts that null as the project's value.
_NULL_REPLACES = frozenset({("language", "reply")})


def deep_merge(base: Any, override: Any, _path: Tuple[str, ...] = ()) -> Any:
    """Merge ``override`` onto ``base``.

    Mappings merge key-by-key. Lists (notably ``reviewers``) replace wholesale,
    so a project override can define a completely different review panel. A
    null keeps ``base``, except at a key in ``_NULL_REPLACES``.
    """
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            if key in base:
                merged[key] = deep_merge(base.get(key), value, (*_path, key))
            else:
                merged[key] = copy.deepcopy(value)
        return merged
    if override is None:
        return None if _path in _NULL_REPLACES else copy.deepcopy(base)
    return copy.deepcopy(override)


def fold_extras(
    base: Any,
    global_layer: Dict[str, Any],
    project_layer: Dict[str, Any],
    panel_keys: PanelKeys = CODE_PANEL,
    base_origins: Optional[Sequence[ReviewerOrigin]] = None,
) -> Tuple[Any, List[ReviewerOrigin], List[str]]:
    """The panel with each file's extras after it: ``(reviewers, origins, notes)``.

    ``base`` is the merged list at ``panel_keys.reviewers``: the project
    file's, else the global file's, else the fit. A project list replaces the
    global extras along with the global list; otherwise the global extras
    come first, then the project's. Only mapping entries are folded, and only
    from a file whose key is a list: ``validate`` reports the rest, once, per
    file. ``base_origins`` is where each base entry was written when no file
    lists the panel -- the design panel copied from the code panel keeps the
    code panel's.

    The base keeps every id. An extra whose id is already taken runs under a
    new one, with a note, and is never dropped for anything else.
    """
    original = base
    if base is None:
        base = []
    if not isinstance(base, list):
        return original, [], []
    if get_path(project_layer, panel_keys.reviewers) is not None:
        base_layer, sources = "project", (("project", project_layer),)
    else:
        base_layer = "global" if get_path(global_layer, panel_keys.reviewers) is not None else "default"
        sources = (("global", global_layer), ("project", project_layer))
    if base_layer == "default" and base_origins is not None:
        origins = list(base_origins)[: len(base)]
        origins += [
            ReviewerOrigin("default", CODE_PANEL.reviewers, i) for i in range(len(origins), len(base))
        ]
    else:
        origins = [ReviewerOrigin(base_layer, panel_keys.reviewers, index) for index in range(len(base))]
    panel = list(base)
    notes: List[str] = []
    owners: Dict[str, ReviewerOrigin] = {}
    for origin, reviewer in zip(origins, panel, strict=True):
        if isinstance(reviewer, dict) and isinstance(reviewer.get("id"), str):
            owners.setdefault(reviewer["id"], origin)
    layer_extras = [(name, get_path(layer, panel_keys.extras)) for name, layer in sources]
    # Every id an extra writes, folded yet or not: a new name never takes one,
    # or the extra that wrote it would be renamed in turn.
    written = [
        {"id": extra["id"]}
        for _name, extras in layer_extras
        if isinstance(extras, list)
        for extra in extras
        if isinstance(extra, dict) and isinstance(extra.get("id"), str)
    ]
    for layer_name, extras in layer_extras:
        if not isinstance(extras, list):
            continue
        for index, extra in enumerate(extras):
            if not isinstance(extra, dict):
                continue
            origin = ReviewerOrigin(layer_name, panel_keys.extras, index)
            entry = copy.deepcopy(extra)
            taken = entry.get("id")
            if isinstance(taken, str) and taken in owners:
                provider, role = str(entry.get("provider") or ""), str(entry.get("role") or "general")
                renamed = suggest_reviewer_id({"reviewers": panel + written}, provider, role)
                entry["id"] = renamed
                notes.append(
                    "%s[%d] in the %s file: id %s is taken by %s; it runs as %s "
                    "(reviewer set %s --id <name> keeps a name)"
                    % (panel_keys.extras, index, layer_name, taken, _owner(owners[taken]), renamed, renamed)
                )
            if isinstance(entry.get("id"), str):
                owners.setdefault(entry["id"], origin)
            panel.append(entry)
            origins.append(origin)
    if len(panel) == len(base):
        return original, origins, notes
    return panel, origins, notes


def _owner(origin: ReviewerOrigin) -> str:
    """The entry an id belongs to, the way a fold note names it."""
    if origin.extra:
        return "%s[%d] in the %s file" % (origin.key, origin.position, origin.layer)
    if origin.layer == "default":
        return "the fitted panel"
    if origin.design:
        return "the %s file's design reviewers" % origin.layer
    return "the %s file's reviewers" % origin.layer


def without_when(reviewer: Any) -> Any:
    """A copy of a code panel seat for a design panel: its ``when`` removed.

    A design round that has no panel of its own runs every code seat, so a
    seat copied from there keeps running every round instead of silently
    becoming conditional.
    """
    if not isinstance(reviewer, dict):
        return copy.deepcopy(reviewer)
    return {key: copy.deepcopy(value) for key, value in reviewer.items() if key != "when"}


def sets_design_panel(layer: Dict[str, Any]) -> bool:
    """Whether a file writes ``review.design.reviewers`` or ``reviewers_extra``."""
    return any(get_path(layer, key) is not None for key in DESIGN_PANEL)


def design_panel_follows_fit(global_layer: Dict[str, Any], project_layer: Dict[str, Any]) -> bool:
    """Whether the preset's design panel, when it has one, is the design panel's base.

    The one rule for it: a file that lists ``reviewers`` keeps the design
    rounds on its copy of them, as before presets had a design panel, and a
    file that lists ``review.design.reviewers`` replaces the fit's list. So
    either list, in either file, takes the fit's design panel out.
    """
    return not any(
        layer.get("reviewers") is not None or get_path(layer, DESIGN_PANEL.reviewers) is not None
        for layer in (global_layer, project_layer)
    )


def design_panel_is_fit(origins: Sequence["ReviewerOrigin"]) -> bool:
    """Whether a composed design panel's ``origins`` say its base is the preset's fit."""
    return any(origin.layer == "default" and origin.key == DESIGN_PANEL.reviewers for origin in origins)


#: What a note says when a seat moved to another CLI lost its high-risk model.
HIGH_RISK_MODEL_REMOVED = "its high_risk_model was removed"


def move_reviewer_provider(reviewer: Dict[str, Any], provider: str, keep_high_risk: bool = False) -> bool:
    """Put ``reviewer`` on ``provider``; True when its ``high_risk_model`` went with the move.

    A high-risk model is the old CLI's word for a model, as a family is, and
    has no default to reset to, so it goes when the provider changes --
    unless ``keep_high_risk``: the caller is setting another.
    """
    previous = reviewer.get("provider")
    reviewer["provider"] = provider
    if provider == previous or keep_high_risk or "high_risk_model" not in reviewer:
        return False
    reviewer.pop("high_risk_model")
    return True


def compose_design_panel(
    data: Dict[str, Any],
    code_origins: Sequence[ReviewerOrigin],
    global_layer: Dict[str, Any],
    project_layer: Dict[str, Any],
) -> Tuple[List[ReviewerOrigin], List[str]]:
    """Put the design panel into ``data`` when there is one: ``(origins, notes)``.

    With no file setting ``review.design.reviewers`` or ``reviewers_extra``
    and no design list from the fit there is no design panel, and nothing is
    written: a design round runs the code panel with ``when`` ignored, as it
    always has. Otherwise the base is the project file's list, else the
    global file's, else the fit's, else a copy of the composed code panel
    with every ``when`` removed; then each file's design extras are folded
    in, as ``fold_extras`` folds the code panel's.
    """
    layers = (global_layer, project_layer)
    file_listed = any(get_path(layer, DESIGN_PANEL.reviewers) is not None for layer in layers)
    fitted = design_panel_follows_fit(global_layer, project_layer) and isinstance(
        get_path(data, DESIGN_PANEL.reviewers), list
    )
    if not (fitted or sets_design_panel(global_layer) or sets_design_panel(project_layer)):
        return [], []
    review = data.setdefault("review", {})
    if not isinstance(review, dict):
        return [], []
    design = review.get("design")
    if design is None:
        design = review["design"] = {}
    elif not isinstance(design, dict):
        # Left as it is, for validation to report: replacing it would hide
        # the mistake along with the design panel another file set.
        return [], []
    design.pop("reviewers_extra", None)
    base_origins: Optional[Sequence[ReviewerOrigin]] = None
    if file_listed or fitted:
        # The fit's list has no file origins, so ``fold_extras`` labels it the fit's.
        base = design.get("reviewers")
    else:
        code = data.get("reviewers")
        base = [without_when(reviewer) for reviewer in code] if isinstance(code, list) else []
        base_origins = code_origins
    panel, origins, notes = fold_extras(base, global_layer, project_layer, DESIGN_PANEL, base_origins)
    design["reviewers"] = panel if panel is not None else []
    return origins, notes


class LoadedConfig:
    """A resolved configuration plus provenance about where it came from."""

    def __init__(
        self,
        data: Dict[str, Any],
        global_path: Optional[str],
        project_path: Optional[str],
        used_defaults: bool,
        global_layer: Optional[Dict[str, Any]] = None,
        project_layer: Optional[Dict[str, Any]] = None,
        preset: Optional[str] = None,
        preset_source: str = "implicit",
        preset_notes: Optional[List[str]] = None,
        files_data: Optional[Dict[str, Any]] = None,
        reviewer_origins: Optional[Sequence[ReviewerOrigin]] = None,
        design_reviewer_origins: Optional[Sequence[ReviewerOrigin]] = None,
        installed: Optional[Sequence[str]] = None,
        start: Optional[str] = None,
    ) -> None:
        self.data = data
        self.global_path = global_path
        self.project_path = project_path
        self.used_defaults = used_defaults
        #: Each file as it was read, before merging -- kept to say where a
        #: merged value came from.
        self.global_layer = global_layer or {}
        self.project_layer = project_layer or {}
        #: The preset in force, or None when the global file names an unknown
        #: one; ``global``, ``implicit`` (it names none) or ``invalid``.
        self.preset = preset
        self.preset_source = preset_source
        #: What the fit changed and why, for ``config show`` and ``doctor``.
        self.preset_notes = list(preset_notes or [])
        #: The defaults and the files, without the preset's expansion.
        self.files_data = files_data if files_data is not None else data
        #: Parallel to ``reviewers()``: where each one was written.
        self.reviewer_origins = list(reviewer_origins or [])
        #: Parallel to ``design_reviewers()`` when there is a design panel.
        self.design_reviewer_origins = list(design_reviewer_origins or [])
        #: The CLIs the preset was fitted to; None when not composed by ``compose_loaded``.
        self.installed = list(installed) if installed is not None else None
        #: Where this was loaded from: None is the working directory.
        self.start = start
        self._root: Optional[str] = None

    @property
    def root(self) -> str:
        """The repository the commands run in, from ``start``; git is asked once, on first use.

        Its top level, or ``start`` itself outside one. The workspace is
        resolved in it, and what the project file may not set there is
        reported against it, so the two cannot disagree.
        """
        if self._root is None:
            # lazy: only the commands that resolve a workspace pay for git
            from . import workspace as ws_mod

            self._root = ws_mod.repo_root(self.start)
        return self._root

    @property
    def exists(self) -> bool:
        """True when at least one config file was found on disk."""
        return bool(self.global_path or self.project_path)

    def layer_of(self, path: Union[str, Sequence[Any]]) -> str:
        """``project``, ``global`` or ``default``: the layer a value came from.

        Exact because of how layers merge: mappings merge key by key, and a
        list or a tier's ``options`` is replaced whole. So when the project
        file names the key, the merged value is the project's; otherwise it is
        the global file's, if that names it.

        ``path`` is dotted, or a list of literal keys: a tier name may contain
        a dot, and splitting it would look up a key nobody wrote -- labeling a
        project value ``default``.

        A value the preset's expansion supplied reads as ``default``: no file
        names it. Presets never set ``options``, so the raw-argument checks
        that ask this are unaffected.
        """
        parts = split_path(path) if isinstance(path, str) else list(path)
        for name, layer in (("project", self.project_layer), ("global", self.global_layer)):
            if get_parts(layer, parts, _MISSING) is not _MISSING or _names_null(layer, parts):
                return name
        return "default"

    def role(self, name: str, tier: Optional[str] = None) -> Dict[str, Any]:
        """One role's spec, optionally as one of its tiers.

        A tier is not a different role: same job, same prompt, different
        model. Which is why it is a per-role override rather than a second
        role -- routing a simple fix to a cheaper model should not mean
        maintaining two definitions of what the implementer is.
        """
        spec = self.data.get(name)
        if not isinstance(spec, dict):
            raise ConfigError("role %r is not configured" % name)
        if not tier:
            return spec
        tiers = spec.get("model_tiers")
        entry = tiers.get(tier) if isinstance(tiers, dict) else None
        if not isinstance(entry, dict):
            known = sorted(tiers) if isinstance(tiers, dict) else []
            raise ConfigError(
                "%s has no tier %r%s"
                % (name, tier, " (known: %s)" % ", ".join(known) if known else " (none configured)")
            )
        return merge_tier(spec, entry)

    def tiers(self, name: str) -> Dict[str, Any]:
        spec = self.data.get(name)
        tiers = spec.get("model_tiers") if isinstance(spec, dict) else None
        return dict(tiers) if isinstance(tiers, dict) else {}

    def reviewers(self) -> List[Dict[str, Any]]:
        return list(self.data.get("reviewers") or [])

    def reviewer_origin(self, index: int) -> ReviewerOrigin:
        """Where reviewer ``index`` was written.

        A ``LoadedConfig`` built without origins has folded nothing, so its
        whole panel came with the file that names ``reviewers``.
        """
        if 0 <= index < len(self.reviewer_origins):
            return self.reviewer_origins[index]
        return ReviewerOrigin(self.layer_of("reviewers"), "reviewers", index)

    def has_design_panel(self) -> bool:
        """Whether a file or the fit set a design panel, so ``review.design.reviewers`` is composed."""
        design = (self.data.get("review") or {}).get("design")
        return isinstance(design, dict) and design.get("reviewers") is not None

    def design_reviewers(self) -> List[Dict[str, Any]]:
        """The panel a design round runs.

        The composed ``review.design.reviewers`` when a file or the fit sets
        a design panel; otherwise the code panel, each seat copied without its
        ``when``, which is what a design round has always run.
        """
        if self.has_design_panel():
            panel = self.data["review"]["design"]["reviewers"]
            return list(panel) if isinstance(panel, list) else []
        return [without_when(reviewer) for reviewer in self.reviewers()]

    def design_reviewer_origin(self, index: int) -> ReviewerOrigin:
        """Where design reviewer ``index`` was written; a copied code seat keeps its own origin."""
        if not self.has_design_panel():
            return self.reviewer_origin(index)
        if 0 <= index < len(self.design_reviewer_origins):
            return self.design_reviewer_origins[index]
        return ReviewerOrigin(self.layer_of(DESIGN_PANEL.reviewers), DESIGN_PANEL.reviewers, index)

    @property
    def design_panel_source(self) -> str:
        """Where the design panel's base comes from: ``project``, ``global``, ``fit`` or ``code``.

        ``fit`` is the preset's design panel, when no file lists either panel.
        ``code`` is the code panel, copied without ``when``: the case with no
        design panel at all, and the one where a file adds design extras only.
        """
        layer = self.layer_of(DESIGN_PANEL.reviewers)
        if layer in ("project", "global"):
            return layer
        if design_panel_is_fit(self.design_reviewer_origins):
            return "fit"
        return "code"

    def review_settings(self) -> Dict[str, Any]:
        settings = default_config()["review"]
        settings.update(self.data.get("review") or {})
        return settings

    def blocking_severities(self) -> Tuple[str, ...]:
        """``review.re_review_severities`` in lower case, or the default for anything unusable.

        Commands load unvalidated, and a value ``validate`` would refuse must
        not stop gating: a scalar ``critical`` would otherwise be iterated a
        letter at a time, and ``[crit]`` would match no finding at all.
        """
        fallback = tuple(default_config()["review"]["re_review_severities"])
        review = self.data.get("review")
        value = review.get("re_review_severities") if isinstance(review, dict) else None
        if not isinstance(value, list) or not value:
            return fallback
        names = tuple(item.strip().lower() if isinstance(item, str) else item for item in value)
        if not all(name in SEVERITIES for name in names):
            return fallback
        return names

    def whole_change_recheck(self) -> bool:
        """``review.whole_change_recheck``, or the default for anything but a boolean.

        Commands load unvalidated, as for ``blocking_severities``: a string
        ``"false"`` would otherwise read as true.
        """
        review = self.data.get("review")
        value = review.get("whole_change_recheck") if isinstance(review, dict) else None
        if not isinstance(value, bool):
            return bool(default_config()["review"]["whole_change_recheck"])
        return value

    def design_review_settings(self) -> Dict[str, Any]:
        """The ``review.design`` block, with anything absent filled in.

        Its own accessor rather than a lookup inside ``review_settings``: that
        one updates shallowly, so a file naming only ``enabled`` would drop
        ``max_iterations`` and refuse the first round.
        """
        settings = default_config()["review"]["design"]
        configured = (self.data.get("review") or {}).get("design")
        if isinstance(configured, dict):
            settings.update(configured)
        return settings

    def max_review_iterations(self) -> int:
        """``review.max_review_iterations``: how many code review rounds the budget allows."""
        return int(self.review_settings()["max_review_iterations"])

    def design_max_iterations(self) -> int:
        """``review.design.max_iterations``: how many design review rounds the budget allows."""
        return int(self.design_review_settings()["max_iterations"])

    def design_settings(self) -> Dict[str, Any]:
        """The top-level ``design`` block, with anything absent filled in."""
        settings = default_config()["design"]
        configured = self.data.get("design")
        if isinstance(configured, dict):
            # An explicit ``null`` means "use the default", as it does in
            # ``context_settings``. Copied over as it is, it would read as
            # false and turn the approval gate off without anyone choosing to.
            settings.update(
                {key: value for key, value in configured.items() if value is not None and key != "resume"}
            )
            # Nested, so merged key by key: a file naming one limit keeps the
            # other's default. ``max_age_seconds: null`` is the default too,
            # and ``max_context_tokens: null`` is already "no cap".
            resume = configured.get("resume")
            if isinstance(resume, dict):
                settings["resume"].update({key: value for key, value in resume.items() if value is not None})
        return settings

    def context_settings(self) -> Dict[str, Any]:
        """The ``review.context`` block, with anything absent filled in.

        Its own accessor for the reason ``design_review_settings`` has one:
        ``review_settings`` updates shallowly, so a file naming one key of
        this block would drop the rest of it.
        """
        settings = default_config()["review"]["context"]
        configured = (self.data.get("review") or {}).get("context")
        if isinstance(configured, dict):
            # An explicit ``null`` means "use the default", which is what it
            # already means on ``review.max_findings`` next door, and the one
            # thing it must not mean is "no limit": the zero
            # ``over_context`` reads that way is for a configuration written
            # before the setting existed, and a file that names the key is not
            # that. Dropped here rather than rejected in ``validate`` so both
            # keys accept the same file.
            settings.update({key: value for key, value in configured.items() if value is not None})
        return settings

    def language_settings(self) -> Dict[str, Any]:
        """The ``language`` block, with anything absent filled in; see ``language_settings_of``."""
        return language_settings_of(self.data)

    def optimization_settings(self) -> Dict[str, Any]:
        settings = default_config()["optimization"]
        settings.update(self.data.get("optimization") or {})
        return settings

    def workspace_dir(self, root: Optional[str] = None) -> str:
        """The workspace container in ``root``, by default the commands' own (``root``)."""
        root = self.root if root is None else root
        workspace = workspace_dir_in(root, self.data, self.global_layer, self.project_layer)
        if os.path.isabs(workspace):
            return workspace
        return os.path.join(root, workspace)

    def stale_notice_days(self) -> int:
        """``workspace.stale_notice_days``, or the default for anything unusable.

        Commands load unvalidated, so a value ``validate`` would refuse still
        arrives here; the default keeps it from reaching the scan.
        """
        fallback = default_config()["workspace"]["stale_notice_days"]
        value = (self.data.get("workspace") or {}).get("stale_notice_days")
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= STALE_NOTICE_MAX_DAYS:
            return fallback
        return value

    def progress_settings(self) -> Progress:
        """``progress_settings_of`` the composed data."""
        return progress_settings_of(self.data)

    def implementation_settings(self) -> Implementation:
        """``implementation_settings_of`` the composed data."""
        return implementation_settings_of(self.data)

    def without_project(self) -> Optional["LoadedConfig"]:
        """The configuration the global file, its preset's fit and the defaults give alone.

        Fitted to the CLIs this one was, without asking for them again. None
        when the global file does not compose alone: ``validate`` reports it.
        """
        # lazy: presets; see the note at the top of this module
        from . import presets

        installed = self.installed if self.installed is not None else presets.installed_providers()
        try:
            return compose_loaded(self.global_layer, {}, installed, self.global_path, None, self.start)
        except Exception:
            return None


def validate_loaded(loaded: LoadedConfig) -> List[str]:
    """``validate`` for a ``LoadedConfig``: its data, both its layers and both its panels' origins."""
    return validate(
        loaded.data,
        project_layer=loaded.project_layer,
        global_layer=loaded.global_layer,
        origins=loaded.reviewer_origins,
        design_origins=loaded.design_reviewer_origins,
    )


class RunTimeout(NamedTuple):
    """The total deadline of one ``run`` of a role, and where it was set."""

    seconds: int
    #: ``project``, ``global`` or ``default``.
    source: str


def run_timeout(loaded: LoadedConfig, role: str) -> RunTimeout:
    """``run.timeout_seconds.<role>``, from the project file, else the global one, else the default.

    A value ``validate`` would refuse reads as the default: ``config show``
    loads unvalidated, and must not report a deadline no run would get. A
    null in a file merges to the default, and reads as one.
    """
    return _timeout_at(loaded, ["run", "timeout_seconds", role], RUN_TIMEOUT_DEFAULTS[role])


def review_timeout(loaded: LoadedConfig) -> RunTimeout:
    """``review.timeout_seconds``: the deadline of a review round, and of ``run <reviewer>``.

    Read the way ``run_timeout`` reads a role's, so a value ``validate``
    would refuse -- or a ``review`` that is not a mapping -- reads as the
    default rather than reaching a deadline or a summary line.
    """
    return _timeout_at(loaded, ["review", "timeout_seconds"], default_config()["review"]["timeout_seconds"])


#: The ``source`` of an idle deadline raised to ``WRITE_RUN_IDLE_TIMEOUT_SECONDS``.
WRITE_RUN_MINIMUM = "write-run minimum"


def idle_timeout(loaded: LoadedConfig, mode: str) -> RunTimeout:
    """The no-output deadline of a run in ``mode``, and where it was set.

    ``review.idle_timeout_seconds``, read as ``run_timeout`` reads a deadline:
    a null in a file, or a value ``validate`` would refuse, reads as the
    default. A run that may change files (``mode`` ``implement``) gets at least
    ``WRITE_RUN_IDLE_TIMEOUT_SECONDS``, with ``WRITE_RUN_MINIMUM`` as its
    source when that is what it got: such a run is expected to run the tests
    or a build, and Claude's stream is silent until a command returns, so a
    suite longer than the shared value would be killed as a stall (#273).

    A role's ``options.idle_timeout`` and ``--idle-timeout`` come before this,
    and are the caller's to apply.
    """
    # lazy: the provider registry; see the note at the top of this module
    from .providers import MODE_IMPLEMENT

    shared = _timeout_at(
        loaded, ["review", "idle_timeout_seconds"], default_config()["review"]["idle_timeout_seconds"]
    )
    if mode == MODE_IMPLEMENT and shared.seconds < WRITE_RUN_IDLE_TIMEOUT_SECONDS:
        return RunTimeout(WRITE_RUN_IDLE_TIMEOUT_SECONDS, WRITE_RUN_MINIMUM)
    return shared


def _timeout_at(loaded: LoadedConfig, key: List[str], fallback: int) -> RunTimeout:
    value = get_parts(loaded.data, key, None)
    if not whole_seconds(value):
        return RunTimeout(fallback, "default")
    for name, layer in (("project", loaded.project_layer), ("global", loaded.global_layer)):
        if get_parts(layer, key, None) is not None:
            return RunTimeout(value, name)
    return RunTimeout(value, "default")


def referenced_providers(data: Dict[str, Any]) -> List[str]:
    """Every provider name a configuration refers to: roles, tiers and both reviewer panels."""
    names = set()
    specs: List[Any] = [data.get(role) for role in KNOWN_ROLES]
    for spec in list(specs):
        tiers = spec.get("model_tiers") if isinstance(spec, dict) else None
        if isinstance(tiers, dict):
            specs.extend(tiers.values())
    for key in (CODE_PANEL.reviewers, DESIGN_PANEL.reviewers):
        reviewers = get_path(data, key)
        if isinstance(reviewers, list):
            specs.extend(reviewers)
    for spec in specs:
        provider = spec.get("provider") if isinstance(spec, dict) else None
        if isinstance(provider, str) and provider:
            names.add(provider)
    return sorted(names)


def prune_layer(layer: Dict[str, Any], base: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """``layer`` with everything ``base`` already says dropped from it.

    What an adjusted preset setup saves is what differs from the fit, so the
    setup wizard drops the governed values the fit already holds.

    ``base`` is what would be in force without this layer. Lists are dropped
    only when equal whole, because that is the unit ``deep_merge`` replaces.
    ``version`` identifies the file format rather than configuring anything,
    so it survives -- and is supplied when the layer has none.
    """
    dropped: List[Dict[str, Any]] = []

    def walk(current: Dict[str, Any], reference: Dict[str, Any], path: str) -> Dict[str, Any]:
        kept: Dict[str, Any] = {}
        for key, value in current.items():
            name = ("%s.%s" % (path, key)) if path else str(key)
            if not path and key == "version":
                kept[key] = value
                continue
            if key not in reference:
                kept[key] = value
                continue
            inherited = reference[key]
            if isinstance(value, dict) and isinstance(inherited, dict):
                remaining = walk(value, inherited, name)
                # A mapping emptied by its children is itself inherited, and an
                # empty one in the file would only read as "set to nothing".
                if remaining:
                    kept[key] = remaining
                continue
            if value == inherited:
                dropped.append({"setting": name, "value": value})
                continue
            kept[key] = value
        return kept

    pruned = walk(layer, base if isinstance(base, dict) else {}, "")
    pruned.setdefault("version", CONFIG_VERSION)
    return pruned, dropped


def mentions(layer: Dict[str, Any], key: str) -> bool:
    """True when ``layer`` sets anything at ``key``; ``{}`` and null set nothing."""
    value = layer.get(key)
    return value is not None and value != {}


def compose(
    global_layer: Dict[str, Any],
    project_layer: Dict[str, Any],
    installed: Sequence[str],
    around: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Fit, Optional[str], str]:
    """The effective configuration: ``(data, fit, preset, source)``.

    The one place that knows the layer order: the defaults, then the global
    file's preset fitted to ``installed``, then the global file, then the
    project file. A project file's ``preset`` is never expanded (``validate``
    refuses it). The expansion loses every role a file sets any field of, and
    the whole panel when a file lists ``reviewers``: those resolve exactly as
    they did before presets existed, on the provider the files put them on.

    ``fit`` carries only the notes that still apply, plus one for each role a
    file took out of a fit that would have changed it. ``source`` is
    ``global``, ``implicit`` (the file names no preset, so ``standard``) or
    ``invalid`` (an unknown name, so nothing is expanded).

    ``around`` is a layer left out of the result whose implementer still
    counts when the panel is dealt: a writer's base is composed without the
    layer it edits, but the panel it copies is the one in force with it.
    """
    # lazy: presets; see the note at the top of this module
    from . import presets

    named = global_layer.get("preset")
    if named is None:
        name: Optional[str] = presets.DEFAULT
        source = "implicit"
    elif isinstance(named, str) and named in presets.PRESETS:
        name = named
        source = "global"
    else:
        name = None
        source = "invalid"
    layers = (("the project file", project_layer), ("the global file", global_layer))
    # A file that sets the implementer takes it out of the fit, so the panel is
    # dealt around the provider the files put it on.
    implementer = None
    if any(mentions(layer, "implementer") for layer in (project_layer, global_layer, around or {})):
        files = deep_merge(
            deep_merge(deep_merge(default_config(), global_layer), project_layer), around or {}
        )
        spec = files.get("implementer")
        implementer = spec.get("provider") if isinstance(spec, dict) else None
    fit = presets.expand(name, installed, implementer) if name else presets.Fit({}, [], [])

    values = copy.deepcopy(fit.values)
    listed = any(layer.get("reviewers") is not None for _label, layer in layers)
    follows_fit = design_panel_follows_fit(global_layer, project_layer)
    if listed:
        values.pop("reviewers", None)
    if not follows_fit:
        # Either list, in either file, takes the fit's design panel out.
        pop_path(values, DESIGN_PANEL.reviewers)
    defaults = default_config()
    unfitted: List[str] = []
    for role in KNOWN_ROLES:
        where = next((label for label, layer in layers if mentions(layer, role)), None)
        if where is None or role not in values:
            continue
        unfitted.append(role)
        if values.pop(role) != defaults[role]:
            fit.notes.append("%s is set by %s and was not fitted" % (role, where))
            fit.subjects.append("")
    dropped = set(unfitted)
    if listed:
        dropped.add("reviewers")
    if not follows_fit:
        # The fit's design panel went, and its notes with it.
        dropped.add(DESIGN_PANEL.reviewers)
    kept = [
        (note, subject)
        for note, subject in zip(fit.notes, fit.subjects, strict=True)
        if subject not in dropped
    ]
    # Without what only the global file may set: a project file can come with
    # the branch under review (``config_trust``).
    trusted = trusted_project_layer(project_layer)
    data = deep_merge(deep_merge(deep_merge(defaults, values), global_layer), trusted)
    # Each file's extras join the panel it inherits, so every reader of
    # ``reviewers`` sees them; ``origins`` keeps whose each one is.
    reviewers, origins, folded = fold_extras(data.get("reviewers"), global_layer, project_layer)
    data.pop("reviewers_extra", None)
    if reviewers is not data.get("reviewers"):
        data["reviewers"] = reviewers
    kept += [(note, "reviewers_extra") for note in folded]
    # The design panel, when a file or the fit sets one; built on the code panel just
    # composed, so a design panel that copies it copies the one in force.
    design_origins, design_folded = compose_design_panel(data, origins, global_layer, project_layer)
    kept += [(note, DESIGN_PANEL.extras) for note in design_folded]
    fit = presets.Fit(
        values,
        [note for note, _ in kept],
        [subject for _, subject in kept],
        tuple(origins),
        tuple(design_origins),
    )
    return data, fit, name, source


def compose_loaded(
    global_layer: Dict[str, Any],
    project_layer: Dict[str, Any],
    installed: Sequence[str],
    global_path: Optional[str] = None,
    project_path: Optional[str] = None,
    start: Optional[str] = None,
) -> LoadedConfig:
    """``compose``'s result as a ``LoadedConfig``: the one place one is wired up from layers.

    ``load`` builds its result here, and ``config_policy`` the same files
    without the project one, so a field added to either is in both.
    """
    data, fit, preset, source = compose(global_layer, project_layer, installed)
    return LoadedConfig(
        data,
        global_path,
        project_path,
        not (global_path or project_path),
        global_layer=copy.deepcopy(global_layer),
        project_layer=copy.deepcopy(project_layer),
        preset=preset,
        preset_source=source,
        preset_notes=fit.notes,
        files_data=deep_merge(deep_merge(default_config(), global_layer), project_layer),
        reviewer_origins=fit.origins,
        design_reviewer_origins=fit.design_origins,
        installed=installed,
        start=start,
    )


def load(start: Optional[str] = None, validate_result: bool = True) -> LoadedConfig:
    """Load the layered configuration for the project rooted at ``start``."""
    # lazy: presets; see the note at the top of this module
    from . import presets

    gpath = global_config_path()
    global_found = gpath if os.path.isfile(gpath) else None
    global_layer: Dict[str, Any] = {}
    if global_found:
        global_layer = read_config_file(global_found)

    ppath = find_project_config(start)
    project_layer: Dict[str, Any] = {}
    if ppath:
        project_layer = read_config_file(ppath)

    loaded = compose_loaded(
        global_layer, project_layer, presets.installed_providers(), global_found, ppath, start
    )
    if validate_result:
        problems = validate_loaded(loaded)
        if problems:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems))
    return loaded


# --------------------------------------------------------------------------- editing


def _names_null(data: Dict[str, Any], parts: Sequence[Any]) -> bool:
    """Whether ``data`` writes a null at ``parts``, for a key whose null replaces."""
    if tuple(parts) not in _NULL_REPLACES:
        return False
    parent = get_parts(data, parts[:-1], _MISSING)
    return isinstance(parent, dict) and parts[-1] in parent and parent[parts[-1]] is None
