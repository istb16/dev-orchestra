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
import difflib
import json
import os
from typing import TYPE_CHECKING, Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple, Union

from . import miniyaml
from . import optimization as opt_mod
from .clocks import MAX_SECONDS, is_seconds
from .config_common import (
    _ID_RE,
    _TIER_NAME_RE,
    BUILTIN_ROLES,
    CONFIG_VERSION,
    KNOWN_ROLES,
    READ_ONLY_ROLES,
    RUN_TIMEOUT_DEFAULTS,
    STALE_NOTICE_MAX_DAYS,
    WRITE_ROLES,
    WRITE_RUN_IDLE_TIMEOUT_SECONDS,
    ConfigError,
    ask_questions_mode,
    default_config,
    design_review_mode,
    is_valid_reviewer_id,
    language_settings_of,
    normalise_language_tag,
    trusted_project_layer,
    workspace_dir_in,
    workspace_dir_of,
)
from .config_keys import _get_parts, _split_path, coerce_scalar, get_path, pop_path, set_path
from .config_paths import (
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
from .config_reviewers import (
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
from .review_common import SEVERITIES

if TYPE_CHECKING:
    from .presets import Fit

# Two imports stay inside the functions that need them, and each one points
# back here. The provider registry: importing it runs its bootstrap, which asks
# this module for ``user_providers_dir`` and imports every user adapter, and
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


def _origin_label(origin: ReviewerOrigin, index: int) -> str:
    """How a problem names a panel entry: its file for an extra, else ``reviewers[i]``."""
    if origin.extra:
        return "%s[%d] in the %s file" % (origin.key, origin.position, origin.layer)
    if origin.design:
        return "%s[%d]" % (DESIGN_PANEL.reviewers, index)
    return "reviewers[%d]" % index


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
        parts = _split_path(path) if isinstance(path, str) else list(path)
        for name, layer in (("project", self.project_layer), ("global", self.global_layer)):
            if _get_parts(layer, parts, _MISSING) is not _MISSING or _names_null(layer, parts):
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
    value = _get_parts(loaded.data, key, None)
    if not _whole_seconds(value):
        return RunTimeout(fallback, "default")
    for name, layer in (("project", loaded.project_layer), ("global", loaded.global_layer)):
        if _get_parts(layer, key, None) is not None:
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


# --------------------------------------------------------------------------- validation


#: How a deadline too long to wait for is refused.
_TOO_LONG = "must be %d or less" % MAX_SECONDS


def _whole_seconds(value: Any) -> bool:
    """A deadline in whole seconds: a positive int, not a bool, that a wait can hold."""
    return isinstance(value, int) and is_seconds(value)


def _int_at_least(value: Any, minimum: int) -> bool:
    """An int, not a bool, and at least ``minimum``."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def validate(
    data: Dict[str, Any],
    known_providers: Optional[List[str]] = None,
    project_layer: Optional[Dict[str, Any]] = None,
    global_layer: Optional[Dict[str, Any]] = None,
    origins: Optional[Sequence[ReviewerOrigin]] = None,
    design_origins: Optional[Sequence[ReviewerOrigin]] = None,
) -> List[str]:
    """Return a list of human-readable problems; empty means valid.

    ``project_layer`` is the project file on its own, for the one rule merged
    data cannot express: only the global file can name a preset. It and
    ``global_layer`` also have their ``reviewers_extra`` checked, entry by
    entry, whether or not the extras made it into the panel. ``origins`` is
    parallel to the folded ``reviewers``: an extra's entry is checked there
    and not again here, while the panel-wide rules cover it.
    ``design_origins`` is the same for ``review.design.reviewers``, whose
    design extras are checked per file too.
    """
    # lazy: presets and the provider registry; see the note at the top of this module
    from . import presets
    from .providers import available_providers

    providers = known_providers if known_providers is not None else available_providers()
    problems = _validate_version_and_preset(data, project_layer, presets.NAMES)
    problems.extend(_validate_roles(data, providers))
    problems.extend(_validate_reviewers(data, providers, origins, global_layer, project_layer))
    problems.extend(_validate_design_panel(data, providers, design_origins, global_layer, project_layer))
    for key, check in (
        ("run", _validate_run),
        ("review", _validate_review),
        ("design", _validate_design),
        ("optimization", _validate_optimization),
        ("budgets", _validate_budgets),
        ("workspace", _validate_workspace),
        ("language", _validate_language),
    ):
        block = data.get(key)
        if block is not None:
            problems.extend(check(block))
    return problems


def validate_loaded(loaded: LoadedConfig) -> List[str]:
    """``validate`` for a ``LoadedConfig``: its data, both its layers and both its panels' origins."""
    return validate(
        loaded.data,
        project_layer=loaded.project_layer,
        global_layer=loaded.global_layer,
        origins=loaded.reviewer_origins,
        design_origins=loaded.design_reviewer_origins,
    )


#: A mapping whose keys are the user's own: provider options, which the
#: adapter validates, tier names, budgets, and run timeouts, whose keys
#: ``validate`` already checks as roles.
_FREE = "free"


def _key_schema() -> Dict[str, Any]:
    """The keys a file may hold, as nested mappings; None is a leaf, ``_FREE`` any keys.

    A list of reviewer entries is ``[schema]``. Built from ``default_config``,
    so a key added there is known here without a second list to keep.
    """

    def shape(value: Any) -> Any:
        return {key: shape(item) for key, item in value.items()} if isinstance(value, dict) else None

    model = {"family": None, "version": None, "id": None}
    role = {"provider": None, "model": model, "options": _FREE, "model_tiers": _FREE}
    # when is a leaf: its keys are validate's to refuse. high_risk_model is a
    # model block; the provider and options validate refuses in it are known
    # here, so they are reported once.
    reviewer = {
        "id": None,
        "provider": None,
        "model": model,
        "options": _FREE,
        "role": None,
        "high_risk_model": dict(model, provider=None, options=None),
        "when": None,
        "relevance": None,
    }
    schema = shape(default_config())
    schema.update(dict.fromkeys(KNOWN_ROLES, role))
    schema.update({"preset": None, "reviewers": [reviewer], "reviewers_extra": [reviewer], "budgets": _FREE})
    schema["run"]["timeout_seconds"] = _FREE
    schema["review"]["design"].update({"reviewers": [reviewer], "reviewers_extra": [reviewer]})
    return schema


def unknown_keys(layer: Dict[str, Any]) -> List[Tuple[str, Optional[str]]]:
    """Each key in ``layer`` nothing reads, dotted, with the known key it most resembles.

    A typo such as ``reveiw`` or ``review.timout_seconds`` is otherwise valid
    and silently ignored. Reported, never refused: a key a later release adds
    must not make an older one reject the file. Only mappings are walked; a
    value of the wrong type is ``validate``'s to report.
    """
    found: List[Tuple[str, Optional[str]]] = []

    def walk(node: Any, schema: Any, path: str) -> None:
        if isinstance(schema, list) and isinstance(node, list):
            for index, entry in enumerate(node):
                walk(entry, schema[0], "%s[%d]" % (path, index))
            return
        if not isinstance(schema, dict) or not isinstance(node, dict):
            return
        for key, value in node.items():
            name = "%s.%s" % (path, key) if path else str(key)
            if key in schema:
                walk(value, schema[key], name)
                continue
            close = difflib.get_close_matches(str(key), [str(known) for known in schema], n=1, cutoff=0.75)
            found.append((name, ("%s.%s" % (path, close[0]) if path else close[0]) if close else None))

    walk(layer, _key_schema(), "")
    return found


def unknown_key_warnings(global_layer: Dict[str, Any], project_layer: Dict[str, Any]) -> List[str]:
    """``unknown_keys`` of each file, one line apiece, naming the file."""
    warnings: List[str] = []
    for name, layer in (("global", global_layer), ("project", project_layer)):
        for key, close in unknown_keys(layer):
            hint = " (did you mean %s?)" % close if close else ""
            warnings.append("%s in the %s file: unknown key, ignored%s" % (key, name, hint))
    return warnings


def _validate_version_and_preset(
    data: Dict[str, Any], project_layer: Optional[Dict[str, Any]], preset_names: Sequence[str]
) -> List[str]:
    problems: List[str] = []
    version = data.get("version")
    if version != CONFIG_VERSION:
        problems.append("version must be %d (got %r)" % (CONFIG_VERSION, version))

    preset = data.get("preset")
    if preset is not None and not (isinstance(preset, str) and preset in preset_names):
        problems.append("preset: unknown %r (known: %s)" % (preset, ", ".join(preset_names)))
    if project_layer and project_layer.get("preset") is not None:
        problems.append("preset: only the global file can name a preset for now")
    return problems


def _validate_roles(data: Dict[str, Any], providers: List[str]) -> List[str]:
    problems: List[str] = []
    for role in KNOWN_ROLES:
        spec = data.get(role)
        if not isinstance(spec, dict):
            problems.append("%s: missing role definition" % role)
            continue
        problems.extend("%s: %s" % (role, msg) for msg in _validate_role(spec, providers))
        problems.extend(_validate_tiers(spec, role, providers))
    return problems


def _validate_reviewers(
    data: Dict[str, Any],
    providers: List[str],
    origins: Optional[Sequence[ReviewerOrigin]],
    global_layer: Optional[Dict[str, Any]],
    project_layer: Optional[Dict[str, Any]],
) -> List[str]:
    problems: List[str] = []
    reviewers = data.get("reviewers")
    if reviewers is None:
        reviewers = []
    if not isinstance(reviewers, list):
        problems.append("reviewers: must be a list (use [] for none)")
    else:
        problems.extend(_validate_panel_entries(reviewers, origins, providers, CODE_PANEL))
        problems.extend(_validate_conditions(reviewers, data.get("optimization"), origins))
    for name, layer in (("global", global_layer), ("project", project_layer)):
        if layer:
            problems.extend(_validate_extras_file(layer, name, providers))
    return problems


def _entry_identity(reviewer: Any, label: str, seen: Set[str], own: bool = True) -> Tuple[bool, List[str]]:
    """``(is_mapping, problems)``: an entry's shape and id, its id added to ``seen``.

    A duplicate id is always reported; a missing mapping or a malformed id
    only for an ``own`` entry.
    """
    if not isinstance(reviewer, dict):
        return False, ["%s: must be a mapping" % label] if own else []
    problems: List[str] = []
    rid = reviewer.get("id")
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        if own:
            problems.append("%s: id must match [a-z0-9][a-z0-9._-]* (got %r)" % (label, rid))
    elif rid in seen:
        problems.append("%s: duplicate reviewer id %r" % (label, rid))
    else:
        seen.add(rid)
    return True, problems


def _validate_panel_entries(
    reviewers: List[Any],
    origins: Optional[Sequence[ReviewerOrigin]],
    providers: List[str],
    keys: PanelKeys,
) -> List[str]:
    """Each entry of a composed panel: its shape, its id, and the entry itself.

    Every id counts toward the duplicates. Only the panel's own entries are
    checked further, by their origin: an extra is checked in its file, and a
    design panel's seat copied from the code panel was checked with the code
    panel. Without origins -- data that was never composed -- every entry is
    the panel's own, as a file wrote it.
    """
    design = keys == DESIGN_PANEL
    problems: List[str] = []
    seen: Set[str] = set()
    for index, reviewer in enumerate(reviewers):
        origin = origins[index] if origins is not None and index < len(origins) else None
        own = origin is None or (not origin.extra and origin.design == design)
        if origin is not None and origin.extra:
            label = _origin_label(origin, index)
        else:
            label = "%s[%d]" % (keys.reviewers, index)
        mapping, identity = _entry_identity(reviewer, label, seen, own)
        problems.extend(identity)
        if not mapping:
            continue
        if own:
            problems.extend(_validate_reviewer_entry(reviewer, label, label + ".", providers, design=design))
    return problems


def _validate_design_panel(
    data: Dict[str, Any],
    providers: List[str],
    origins: Optional[Sequence[ReviewerOrigin]],
    global_layer: Optional[Dict[str, Any]],
    project_layer: Optional[Dict[str, Any]],
) -> List[str]:
    """The design panel, when a file sets one: what ``_validate_reviewers`` checks of the code panel.

    Its own entries are checked here; a seat it copied from the code panel was
    checked there, and a design extra in its file -- both known by their
    origin (``_validate_panel_entries``). A plan has no changed paths, so
    ``when: paths`` is refused; and one seat must always run.
    """
    problems: List[str] = []
    review = data.get("review")
    design = review.get("design") if isinstance(review, dict) else None
    reviewers = design.get("reviewers") if isinstance(design, dict) else None
    # A design panel that is not a list is ``_validate_review_design``'s to report.
    if isinstance(reviewers, list):
        problems.extend(_validate_panel_entries(reviewers, origins, providers, DESIGN_PANEL))
        problems.extend(_validate_conditions(reviewers, data.get("optimization"), origins, design=True))
    for name, layer in (("global", global_layer), ("project", project_layer)):
        if layer:
            problems.extend(_validate_extras_file(layer, name, providers, DESIGN_PANEL))
    return problems


def _validate_run(run: Any) -> List[str]:
    if not isinstance(run, dict):
        return ["run: must be a mapping"]
    timeouts = run.get("timeout_seconds", {})
    if not isinstance(timeouts, dict):
        return ["run.timeout_seconds: must be a mapping of role to seconds"]
    problems: List[str] = []
    for role, value in timeouts.items():
        if role not in KNOWN_ROLES:
            problems.append(
                "run.timeout_seconds.%s: unknown role (known: %s)" % (role, ", ".join(KNOWN_ROLES))
            )
        elif not _int_at_least(value, 1):
            problems.append("run.timeout_seconds.%s: must be a positive integer" % role)
        elif not _whole_seconds(value):
            problems.append("run.timeout_seconds.%s: %s" % (role, _TOO_LONG))
    return problems


def _validate_review(review: Any) -> List[str]:
    if not isinstance(review, dict):
        return ["review: must be a mapping"]
    problems: List[str] = []
    iterations = review.get("max_review_iterations", 2)
    if not _int_at_least(iterations, 0):
        problems.append("review.max_review_iterations: must be a non-negative integer")
    parallel = review.get("parallel")
    if parallel is not None and not isinstance(parallel, bool):
        problems.append("review.parallel: must be true or false")
    problems.extend(_validate_severities(review.get("re_review_severities")))
    timeout = review.get("timeout_seconds", 1800)
    if not _int_at_least(timeout, 1):
        problems.append("review.timeout_seconds: must be a positive integer")
    elif not _whole_seconds(timeout):
        problems.append("review.timeout_seconds: %s" % _TOO_LONG)
    idle = review.get("idle_timeout_seconds")
    if idle is not None and not _int_at_least(idle, 1):
        problems.append("review.idle_timeout_seconds: must be a positive integer or null")
    elif idle is not None and not _whole_seconds(idle):
        problems.append("review.idle_timeout_seconds: %s" % _TOO_LONG)
    incremental = review.get("incremental_rounds")
    if incremental is not None and not isinstance(incremental, bool):
        problems.append("review.incremental_rounds: must be true or false")
    findings_cap = review.get("max_findings")
    if findings_cap is not None and not _int_at_least(findings_cap, 0):
        problems.append("review.max_findings: must be a non-negative integer (0 = no cap)")
    design = review.get("design")
    if design is not None:
        problems.extend(_validate_review_design(design))
    context = review.get("context")
    if context is not None:
        problems.extend(_validate_review_context(context))
    exclude = review.get("exclude")
    if exclude is not None:
        if not isinstance(exclude, list):
            problems.append("review.exclude: must be a list of glob patterns (use [] for none)")
        else:
            for index, pattern in enumerate(exclude):
                if not isinstance(pattern, str) or not pattern.strip():
                    problems.append(
                        "review.exclude[%d]: must be a non-empty string (got %r)" % (index, pattern)
                    )
    return problems


def _validate_severities(severities: Any) -> List[str]:
    """``review.re_review_severities``: a non-empty list drawn from ``SEVERITIES``, in any case.

    ``[]`` is refused rather than read as "block nothing": a review that
    blocks on nothing is not one somebody should get from an empty list.
    """
    if severities is None:
        return []
    choices = ", ".join(SEVERITIES)
    if not isinstance(severities, list):
        return ["review.re_review_severities: must be a list of %s (got %r)" % (choices, severities)]
    if not severities:
        return ["review.re_review_severities: must name at least one severity; [] would block nothing"]
    return [
        "review.re_review_severities[%d]: must be one of %s (got %r)" % (index, choices, name)
        for index, name in enumerate(severities)
        if not isinstance(name, str) or name.strip().lower() not in SEVERITIES
    ]


def _validate_review_design(design: Any) -> List[str]:
    if not isinstance(design, dict):
        return ["review.design: must be a mapping"]
    problems: List[str] = []
    enabled = design.get("enabled")
    if isinstance(enabled, str):
        known = enabled.strip().lower() == "auto"
    else:
        known = enabled is None or isinstance(enabled, bool)
    if not known:
        problems.append("review.design.enabled: must be true, false or auto")
    rounds = design.get("max_iterations")
    if rounds is not None and not _int_at_least(rounds, 0):
        problems.append("review.design.max_iterations: must be a non-negative integer")
    # The entries are checked with the panel; here only the shape, for data
    # validated without being composed.
    for key in ("reviewers", "reviewers_extra"):
        value = design.get(key)
        if value is not None and not isinstance(value, list):
            problems.append("review.design.%s: must be a list (use [] for none)" % key)
    return problems


def _validate_review_context(context: Any) -> List[str]:
    if not isinstance(context, dict):
        return ["review.context: must be a mapping"]
    problems: List[str] = []
    # No cross-check between the two. `inline_chars` above
    # `max_chars` only says "nothing is ever handed over as a
    # file except a forced round", and below it says "I will
    # not pay for a prompt that large" -- both are things
    # somebody means, and neither is a mistake to warn about.
    for key in ("max_chars", "inline_chars", "surrounding_chars"):
        value = context.get(key)
        if value is not None and not _int_at_least(value, 1):
            problems.append("review.context.%s: must be a positive integer" % key)
    # `off` is read as false by YAML, so false means none too;
    # true names no mode and is refused rather than guessed at.
    surrounding = context.get("surrounding")
    if isinstance(surrounding, str):
        known = surrounding.strip().lower() in ("none", "enclosing")
    else:
        known = surrounding is None or surrounding is False
    if not known:
        problems.append("review.context.surrounding: must be one of enclosing, none")
    return problems


def _validate_design(design: Any) -> List[str]:
    if not isinstance(design, dict):
        return ["design: must be a mapping"]
    problems: List[str] = []
    approval = design.get("require_approval")
    if approval is not None and not isinstance(approval, bool):
        problems.append("design.require_approval: must be true or false")
    ask = design.get("ask_questions")
    if isinstance(ask, str):
        known = ask.strip().lower() == "auto"
    else:
        known = ask is None or isinstance(ask, bool)
    if not known:
        problems.append("design.ask_questions: must be true, false or auto")
    rounds = design.get("max_question_rounds")
    if rounds is not None and not _int_at_least(rounds, 1):
        problems.append("design.max_question_rounds: must be a positive integer")
    resume = design.get("resume")
    if resume is not None:
        if not isinstance(resume, dict):
            problems.append("design.resume: must be a mapping")
        else:
            age = resume.get("max_age_seconds")
            if age is not None and not _int_at_least(age, 0):
                problems.append("design.resume.max_age_seconds: must be a non-negative integer")
            cap = resume.get("max_context_tokens")
            if cap is not None and not _int_at_least(cap, 1):
                problems.append("design.resume.max_context_tokens: must be a positive integer or null")
    return problems


def _validate_budgets(budgets: Any) -> List[str]:
    if not isinstance(budgets, dict):
        return ["budgets: must be a mapping"]
    problems: List[str] = []
    for key, value in budgets.items():
        if value is None:
            continue
        if not _int_at_least(value, 0):
            problems.append("budgets.%s: must be a non-negative integer or null" % key)
    return problems


def _validate_workspace(workspace: Any) -> List[str]:
    if not isinstance(workspace, dict):
        return ["workspace: must be a mapping"]
    problems: List[str] = []
    directory = workspace.get("dir")
    if directory is not None and not (isinstance(directory, str) and directory.strip()):
        problems.append("workspace.dir: must be a non-empty string (got %r)" % (directory,))
    days = workspace.get("stale_notice_days")
    if days is not None:
        if not _int_at_least(days, 0):
            problems.append("workspace.stale_notice_days: must be a non-negative integer (0 = off)")
        elif days > STALE_NOTICE_MAX_DAYS:
            problems.append(
                "workspace.stale_notice_days: must be %d or less (100 years)" % STALE_NOTICE_MAX_DAYS
            )
    return problems


def _validate_language(language: Any) -> List[str]:
    if not isinstance(language, dict):
        return ["language: must be a mapping"]
    problems: List[str] = []
    reply = language.get("reply")
    if reply is not None and normalise_language_tag(reply) is None:
        message = "language.reply: must be a language tag such as ja, zh-TW, ko or en, or null"
        if isinstance(reply, bool):
            # YAML reads a bare no, yes, on or off as a boolean; `no` is Norwegian.
            message += ' (got %s: quote a tag such as "no" to keep it a string)' % str(reply).lower()
        problems.append(message)
    rewrite = language.get("rewrite")
    if rewrite is not None and not isinstance(rewrite, bool):
        problems.append("language.rewrite: must be true or false")
    return problems


def _validate_reviewer_entry(
    reviewer: Dict[str, Any], label: str, when_prefix: str, providers: List[str], design: bool = False
) -> List[str]:
    """One reviewer entry's role, provider, model, ``high_risk_model``, ``relevance`` and ``when``.

    Its id is the caller's to check, against the list it is unique in.
    ``when_prefix`` is what goes before the ``when`` key in a message.
    ``design`` is a design panel's entry, which may not be ``when: paths``.
    """
    problems: List[str] = []
    role_name = reviewer.get("role", "general")
    if not isinstance(role_name, str) or not role_name.strip():
        problems.append("%s: role must be a non-empty string" % label)
    problems.extend("%s: %s" % (label, msg) for msg in _validate_role(reviewer, providers))
    if "high_risk_model" in reviewer:
        found = _validate_high_risk_model(reviewer["high_risk_model"])
        problems.extend("%s: %s" % (label, msg) for msg in found)
    problems.extend(_validate_relevance(reviewer, when_prefix))
    when = reviewer.get("when")
    if design and opt_mod.is_paths_condition(when):
        problems.append(
            "%swhen: a design reviewer cannot be when: paths -- a plan has no changed paths "
            "(use when: high-risk, or list it under reviewers for the code review)" % when_prefix
        )
    else:
        problems.extend(_validate_when(when, when_prefix))
    return problems


def _validate_high_risk_model(model: Any) -> List[str]:
    """A seat's ``high_risk_model``: a model block, as ``model`` is, and nothing else.

    The provider never changes on a high-risk round and the options stay the
    seat's, so a ``provider`` or ``options`` inside it is refused rather than
    ignored.
    """
    if not isinstance(model, dict):
        return ["high_risk_model must be a mapping with 'family' and 'version'"]
    problems = [
        "high_risk_model.%s: not allowed -- the provider and options stay the seat's own" % key
        for key in ("provider", "options")
        if key in model
    ]
    problems.extend("high_risk_" + message for message in _validate_model(model))
    return problems


def _validate_relevance(reviewer: Dict[str, Any], prefix: str) -> List[str]:
    """A seat's ``relevance``: one of the rules or ``always``, and no rule on a general seat."""
    if "relevance" not in reviewer:
        return []
    value = reviewer.get("relevance")
    choices = (*opt_mod.RELEVANCE_RULES, opt_mod.RELEVANCE_ALWAYS)
    if not isinstance(value, str) or value.strip().lower() not in choices:
        return ["%srelevance: must be one of %s (got %r)" % (prefix, ", ".join(choices), value)]
    role = reviewer.get("role", "general")
    if value.strip().lower() != opt_mod.RELEVANCE_ALWAYS and str(role).strip().lower() == "general":
        return [
            "%srelevance: a general reviewer is never skipped, so it takes no relevance rule (got %s)"
            % (prefix, value)
        ]
    return []


def _validate_when(when: Any, prefix: str) -> List[str]:
    """A reviewer's ``when``: one of the strings, or a mapping with ``paths``.

    Refuses exactly the forms ``optimization.reviewer_condition`` reads as
    ``always``, by asking the same predicate, so a config that validates is
    never read differently from how it was written. ``prefix`` goes before
    the key in each message.
    """
    if when is None:
        return []
    if isinstance(when, str):
        if when.strip().lower() in opt_mod.REVIEWER_CONDITIONS:
            return []
    elif isinstance(when, dict):
        if opt_mod.is_paths_condition(when):
            return []
        if set(when) != {"paths"}:
            keys = ", ".join(sorted(str(key) for key in when)) or "none"
            return ["%swhen: a when mapping takes paths only (got keys: %s)" % (prefix, keys)]
        patterns = when.get("paths")
        if not isinstance(patterns, list) or not patterns:
            return ["%swhen.paths: must be a non-empty list of glob patterns" % prefix]
        return [
            "%swhen.paths[%d]: must be a non-empty string (got %r)" % (prefix, index, pattern)
            for index, pattern in enumerate(patterns)
            if not isinstance(pattern, str) or not pattern.strip()
        ]
    conditions = ", ".join(opt_mod.REVIEWER_CONDITIONS)
    return ["%swhen: must be one of %s, or a mapping with paths" % (prefix, conditions)]


def _validate_extras_file(
    layer: Dict[str, Any], name: str, providers: List[str], panel_keys: PanelKeys = CODE_PANEL
) -> List[str]:
    """One file's extras of one panel, entry by entry: the only check of an extra's entry.

    Run on each file whether or not its extras joined the panel, so a global
    list that a project ``reviewers`` list replaces is still checked.
    """
    extras = get_path(layer, panel_keys.extras)
    if extras is None:
        return []
    if not isinstance(extras, list):
        return ["%s in the %s file: must be a list (use [] for none)" % (panel_keys.extras, name)]
    design = panel_keys == DESIGN_PANEL
    problems: List[str] = []
    seen: Set[str] = set()
    for index, reviewer in enumerate(extras):
        label = "%s[%d] in the %s file" % (panel_keys.extras, index, name)
        mapping, identity = _entry_identity(reviewer, label, seen)
        problems.extend(identity)
        if not mapping:
            continue
        # The label names the file, so the key is said after it.
        problems.extend(_validate_reviewer_entry(reviewer, label, label + ": ", providers, design=design))
    return problems


def _validate_conditions(
    reviewers: List[Any],
    optimization: Any,
    origins: Optional[Sequence[ReviewerOrigin]] = None,
    design: bool = False,
) -> List[str]:
    """What a panel of conditional reviewers needs to be able to run at all.

    One reviewer that always runs, or a round that matched nothing would have
    nobody to review it; and a pattern for the high-risk ones to be judged
    by, or they would never run. A path-scoped reviewer brings its own
    patterns, so only the first rule applies to it. ``review run`` validates
    before it reads a snapshot, so either mistake stops there rather than
    inside a round.

    The second rule holds only for a reviewer a file wrote. A seat from the
    fit or the built-in panel stays without patterns and runs on the rounds
    declared with ``review run --high-risk``; without ``origins`` every
    reviewer counts as written. ``design`` checks a design panel, named as such.
    """
    entries = [(index, reviewer) for index, reviewer in enumerate(reviewers) if isinstance(reviewer, dict)]
    always = opt_mod.WHEN_ALWAYS
    conditional = [index for index, reviewer in entries if opt_mod.reviewer_condition(reviewer) != always]
    high_risk = [
        index
        for index, reviewer in entries
        if opt_mod.reviewer_condition(reviewer) == opt_mod.WHEN_HIGH_RISK
        and not (origins is not None and index < len(origins) and origins[index].layer == "default")
    ]
    problems: List[str] = []
    key = DESIGN_PANEL.reviewers if design else CODE_PANEL.reviewers
    if entries and len(conditional) == len(entries):
        problems.append(
            "%s: at least one reviewer must run always; every reviewer is conditional "
            "(when: high-risk or when: paths)" % key
        )
    if high_risk and isinstance(optimization, dict) and not opt_mod.risk_patterns(optimization):
        first = high_risk[0]
        origin = origins[first] if origins is not None and first < len(origins) else None
        if origin is not None and (origin.extra or not design):
            label = _origin_label(origin, first)
        else:
            label = "%s[%d]" % (key, first)
        problems.append(
            "optimization.high_risk_paths: no pattern in force, but %s is when: high-risk "
            "and would never run; add patterns to high_risk_paths or extra_high_risk_paths" % label
        )
    return problems


def _validate_optimization(data: Any) -> List[str]:
    problems: List[str] = []
    if not isinstance(data, dict):
        return ["optimization: must be a mapping"]
    levels = opt_mod.LEVELS
    level = data.get("level")
    if level is not None and (not isinstance(level, str) or level.strip().lower() not in levels):
        problems.append("optimization.level: must be one of %s" % ", ".join(sorted(levels)))
    skip = data.get("skip_unneeded_roles")
    if skip is not None and not isinstance(skip, bool):
        problems.append("optimization.skip_unneeded_roles: must be true or false")
    for key in (
        "high_risk_paths",
        "extra_high_risk_paths",
        "security_paths",
        "extra_security_paths",
        "architecture_paths",
        "extra_architecture_paths",
    ):
        patterns = data.get(key)
        if patterns is None:
            continue
        if not isinstance(patterns, list):
            problems.append("optimization.%s: must be a list of glob patterns (use [] for none)" % key)
            continue
        for index, pattern in enumerate(patterns):
            if not isinstance(pattern, str) or not pattern.strip():
                problems.append(
                    "optimization.%s[%d]: must be a non-empty string (got %r)" % (key, index, pattern)
                )
    for key in ("low_risk_max_files", "low_risk_max_lines"):
        value = data.get(key)
        if value is not None and not _int_at_least(value, 0):
            problems.append("optimization.%s: must be a non-negative integer" % key)
    return problems


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


def _validate_tiers(spec: Dict[str, Any], label: str, providers: List[str]) -> List[str]:
    """Every tier is checked as the role it would become.

    A tier is only ever used by name, so a broken one fails at the moment
    somebody routes work to it -- which is the worst moment to find out. It
    is validated here as a whole merged role, by the same rules.
    """
    tiers = spec.get("model_tiers")
    if tiers is None:
        return []
    if not isinstance(tiers, dict):
        return ["%s.model_tiers: must be a mapping of tier name to overrides" % label]
    problems: List[str] = []
    for name, entry in tiers.items():
        if not isinstance(name, str) or not _TIER_NAME_RE.match(name):
            problems.append("%s.model_tiers: %r is not a usable tier name" % (label, name))
            continue
        if not isinstance(entry, dict):
            problems.append("%s.model_tiers.%s: must be a mapping" % (label, name))
            continue
        if not entry:
            problems.append("%s.model_tiers.%s: sets nothing, so it is not a tier" % (label, name))
            continue
        unknown = sorted(set(entry) - {"provider", "model", "options"})
        if unknown:
            problems.append(
                "%s.model_tiers.%s: only provider, model and options can be overridden (got %s)"
                % (label, name, ", ".join(unknown))
            )
            continue
        problems.extend(
            "%s.model_tiers.%s: %s" % (label, name, message)
            for message in _validate_role(merge_tier(spec, entry), providers)
        )
    return problems


def _validate_role(spec: Dict[str, Any], providers: List[str]) -> List[str]:
    problems: List[str] = []
    provider = spec.get("provider")
    if not isinstance(provider, str) or not provider:
        problems.append("provider is required")
    elif provider not in providers:
        problems.append(
            "unknown provider %r (known: %s; %s)" % (provider, ", ".join(providers), user_providers_hint())
        )

    # Options first, because the model checks below return early. A role may
    # legitimately omit `model` -- that is how you let a CLI pick its own --
    # and doing so used to skip option validation entirely, so a typo in a
    # sandbox policy passed `config validate` and was discovered at run time.
    problems.extend(_validate_role_options(spec, provider, providers))

    model = spec.get("model")
    if model is None:
        return problems
    if not isinstance(model, dict):
        problems.append("model must be a mapping with 'family' and 'version'")
        return problems
    problems.extend(_validate_model(model))
    return problems


def _validate_model(model: Dict[str, Any]) -> List[str]:
    """A model block's family and version; each message starts ``model``."""
    problems: List[str] = []
    family = model.get("family")
    if family is not None and (not isinstance(family, str) or not family.strip()):
        problems.append("model.family must be a non-empty string")
    version = model.get("version", "latest")
    if not isinstance(version, str) or version not in ("latest", "pinned"):
        problems.append("model.version must be 'latest' or 'pinned' (got %r)" % (version,))
    if version == "pinned" and not model.get("id"):
        problems.append("model.version is 'pinned' but model.id is missing")
    return problems


def _validate_role_options(spec: Dict[str, Any], provider: Any, providers: List[str]) -> List[str]:
    """Options are provider-specific, so the adapter validates them.

    Only consulted when a role actually sets ``options``: for the common case
    this keeps configuration checks from shelling out to a CLI at all.
    """
    if "options" not in spec:
        return []
    if not isinstance(provider, str) or provider not in providers:
        return []
    # lazy: the provider registry; see the note at the top of this module
    from .providers import get_provider

    try:
        return get_provider(provider).validate_options(spec.get("options"))
    except Exception as exc:  # a broken adapter must not hide the rest of the report
        return ["options could not be validated: %s" % exc]


# --------------------------------------------------------------------------- editing


def _names_null(data: Dict[str, Any], parts: Sequence[Any]) -> bool:
    """Whether ``data`` writes a null at ``parts``, for a key whose null replaces."""
    if tuple(parts) not in _NULL_REPLACES:
        return False
    parent = _get_parts(data, parts[:-1], _MISSING)
    return isinstance(parent, dict) and parts[-1] in parent and parent[parts[-1]] is None
