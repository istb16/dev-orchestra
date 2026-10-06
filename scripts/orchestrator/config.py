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
import os
import re
import sys
from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional, Sequence, Tuple, Union

from . import miniyaml
from . import optimization as opt_mod
from .review_common import DEFAULT_EXCLUDE

if TYPE_CHECKING:
    from .presets import Fit

# Two imports stay inside the functions that need them, and each one points
# back here. The provider registry: importing it runs its bootstrap, which asks
# this module for ``user_providers_dir`` and imports every user adapter, and
# configuration must stay loadable without the provider registry. Presets:
# ``presets`` builds its presets from ``default_config`` when it is imported,
# so it can only be imported once this module is complete.

CONFIG_VERSION = 1
#: Ceiling on ``workspace.stale_notice_days`` (100 years). Commands load the
#: configuration unvalidated, so an unbounded integer would reach every one.
STALE_NOTICE_MAX_DAYS = 36500
APP_DIR_NAME = "dev-orchestra"
PROJECT_CONFIG_NAMES = (
    ".dev-orchestra.yaml",
    ".dev-orchestra.yml",
    ".dev-orchestra.json",
)

KNOWN_ROLES = ("orchestrator", "architect", "implementer", "review_fixer")

#: Roles whose runs are plan or review, never implement. Reviewers are too.
READ_ONLY_ROLES = ("orchestrator", "architect")

#: The write roles, which ``project_write_refusals`` looks at.
WRITE_ROLES = tuple(role for role in KNOWN_ROLES if role not in READ_ONLY_ROLES)

_MISSING = object()

#: Tier names are typed on a command line and read in a report, so they are
#: kept to the shape of a word rather than allowed to be a sentence.
_TIER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

BUILTIN_ROLES = (
    "general",
    "security",
    "performance",
    "test",
    "architecture",
    "database",
    "frontend",
    "backend",
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def is_valid_reviewer_id(value: Any) -> bool:
    """The same rule ``validate`` applies, exposed for input-time checking."""
    return isinstance(value, str) and bool(_ID_RE.match(value))


def design_review_mode(value: Any) -> str:
    """``on``, ``off`` or ``auto`` for a ``review.design.enabled`` value.

    Null means the default, ``auto``. Anything else keeps the truth test the
    readers applied before ``auto`` existed, so a quoted ``"true"`` or a ``1``
    loaded without validation is still on and a ``0`` or ``""`` still off.
    """
    if value is None:
        return "auto"
    if isinstance(value, str) and value.strip().lower() == "auto":
        return "auto"
    return "on" if value else "off"


#: A BCP 47 language tag, loosely: a primary subtag of two or three letters
#: and any number of further subtags. Enough to refuse a sentence or a typo
#: such as ``japanese``, without a registry to check every subtag against.
_LANGUAGE_TAG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$")


def normalise_language_tag(value: Any) -> Optional[str]:
    """``value`` as a tag with its primary subtag in lower case, or None if it is not one."""
    if not isinstance(value, str) or not _LANGUAGE_TAG_RE.match(value):
        return None
    primary, _sep, rest = value.partition("-")
    return primary.lower() + ("-" + rest if rest else "")


def language_settings_of(data: Dict[str, Any]) -> Dict[str, Any]:
    """The ``language`` block of ``data``, with anything absent filled in.

    An explicit null means the default, as it does in ``design_settings``. A
    ``reply`` that is not a tag reads as unset, and only ``rewrite: false``
    turns the check off: commands and the hooks read the files unvalidated,
    and a value ``validate`` would refuse must not switch anything on.
    """
    settings = default_config()["language"]
    configured = data.get("language")
    if isinstance(configured, dict):
        settings["reply"] = normalise_language_tag(configured.get("reply"))
        settings["rewrite"] = configured.get("rewrite") is not False
    return settings


def default_config() -> Dict[str, Any]:
    """Recommended out-of-the-box configuration."""
    return {
        "version": CONFIG_VERSION,
        "orchestrator": {
            "provider": "claude",
            "model": {"family": "sonnet", "version": "latest"},
        },
        "architect": {
            "provider": "claude",
            "model": {"family": "fable", "version": "latest"},
        },
        "implementer": {
            "provider": "claude",
            "model": {"family": "opus", "version": "latest"},
        },
        "review_fixer": {
            "provider": "claude",
            "model": {"family": "opus", "version": "latest"},
        },
        "reviewers": [
            {
                "id": "claude-general",
                "provider": "claude",
                "model": {"family": "opus", "version": "latest"},
                "role": "general",
            },
            {
                "id": "codex-general",
                "provider": "codex",
                "model": {"family": "recommended-coding", "version": "latest"},
                "role": "general",
            },
            {
                "id": "claude-security",
                "provider": "claude",
                "model": {"family": "sonnet", "version": "latest"},
                "high_risk_model": {"family": "opus", "version": "latest"},
                "role": "security",
            },
            {
                "id": "claude-test",
                "provider": "claude",
                "model": {"family": "sonnet", "version": "latest"},
                "role": "test",
            },
        ],
        "review": {
            "max_review_iterations": 2,
            "parallel": True,
            "re_review_severities": ["critical", "high"],
            "timeout_seconds": 1800,
            # A wedged agent stops producing output while a slow one keeps
            # ticking, so this catches a stall in minutes instead of half an
            # hour -- but only for providers that stream progress at all.
            "idle_timeout_seconds": 300,
            # Generated and vendored files whose diff body is withheld from
            # reviewers. A list replaces this wholesale, so [] reviews
            # everything; see review.DEFAULT_EXCLUDE for why these.
            "exclude": list(DEFAULT_EXCLUDE),
            # A second round diffs against what the first round reviewed, so
            # re-review sees the fix instead of the whole change again. The
            # findings the fix was meant to address ride along with it.
            "incremental_rounds": True,
            # How many findings each reviewer is asked for. Output costs several
            # times what input does, and a reviewer's output is billed again as
            # the fixer's brief, so an uncapped reviewer costs twice over. 0
            # lifts the cap, and null lets optimization.level decide. Nothing
            # that comes back is ever dropped.
            "max_findings": None,
            # max_chars: the largest change body a review round will send at
            # all -- the diff, or the plan and the request it answers. 400,000
            # chars is roughly 100k tokens -- half a 200k window spent on the
            # change alone -- and four times the largest prompt this repository
            # has recorded (99,814 chars), so it refuses nothing anyone has
            # run. Over it, `review run` exits 3 rather than reviewing part of
            # a change and calling the round complete.
            #
            # inline_chars: how much of that body goes into the prompt itself.
            # Equal to max_chars, and deliberately so: the 120,000 this
            # replaces had no measured basis -- the prompt reaches the CLI on
            # stdin, so no argv length is involved -- and what handing the body
            # over as a file buys is a review this tool cannot verify. Equal
            # means one boundary: at or under it the round runs and is
            # complete, over it the round is refused, and a forced round is
            # partial. Setting it lower is the explicit choice of somebody who
            # will not pay for very large prompts, and costs them a `partial`
            # round for every change between the two numbers; setting it higher
            # than max_chars is allowed and means the same thing it always did.
            #
            # surrounding: "enclosing" also hands each reviewer the Python
            # function, method or class enclosing every hunk, frozen with the
            # snapshot; "none" hands over the diff alone. Off: measured on one snapshot at
            # a time (#130), it did not make a review cheaper.
            # surrounding_chars caps what that adds. Measured on one snapshot
            # (#130), 15,000 left the cost per run where it was, while 60,000
            # added what it carried -- about 29% on a 90,000-character change.
            "context": {
                "max_chars": 400_000,
                "inline_chars": 400_000,
                "surrounding": "none",
                "surrounding_chars": 15_000,
            },
            # Review the plan with the same panel before any code is written.
            # "auto" by default: a round runs for a plan that names a
            # high-risk path anywhere or 6 or more code files in Files to
            # Modify, or once a round has already run for the workflow, and
            # is skipped for the rest. Measured, the
            # design loop was 40-50% of each workflow that had one, which a
            # risky or large plan earns and a small, contained one does not.
            # `true` always reviews the plan, `false` never does. A file may
            # give the design round a panel of its own with `reviewers` here,
            # or add to the one it inherits with `reviewers_extra`; with
            # neither, a design round runs the preset's fitted design panel,
            # or the code panel with `when` ignored where a file lists one.
            "design": {"enabled": "auto", "max_iterations": 2},
        },
        # Implementation waits for the user's explicit approval of the plan.
        # On by default because the point is to catch a plan the user never
        # saw; `false` is for pipelines nobody is watching (CI, batch runs),
        # where nobody could answer the question anyway. Top-level rather
        # than under review.design: approval matters whether or not the
        # panel reviewed the plan.
        #
        # ``resume`` bounds `run architect --resume`, which continues the
        # architect's last session to revise the plan. An hour is how long the
        # CLI kept the prompt cache when this was measured; past it, a resumed
        # run pays to rebuild the context anyway. No context cap until one is
        # measured to hurt a revision.
        "design": {
            "require_approval": True,
            "resume": {"max_age_seconds": 3600, "max_context_tokens": None},
        },
        # How hard to try to be cheap. See orchestrator/optimization.py: the
        # level gates a review of a tree whose tests are recorded as failing,
        # decides whether a small change gets one reviewer or the whole panel,
        # and sets the findings cap when review.max_findings is unset. A
        # change touching a high-risk path escalates to `quality` whatever is
        # configured here -- that part is not negotiable, only its patterns.
        "optimization": {
            "level": opt_mod.DEFAULT_LEVEL,
            "high_risk_paths": list(opt_mod.DEFAULT_HIGH_RISK_PATHS),
            # Added to high_risk_paths rather than replacing it, so a
            # repository can name the one path the defaults miss. Its hits
            # escalate exactly as the others do.
            "extra_high_risk_paths": [],
            "low_risk_max_files": opt_mod.DEFAULT_LOW_RISK_MAX_FILES,
            "low_risk_max_lines": opt_mod.DEFAULT_LOW_RISK_MAX_LINES,
            # A security, test or architecture reviewer sits out a round with
            # nothing for it, in both stages and at every level -- see
            # optimization.relevance_records. false runs every seat as before.
            "skip_unneeded_roles": True,
            # What the security and architecture rules look for. Replaced whole
            # by a list, or added to by the extra_ keys, as high_risk_paths is.
            "security_paths": list(opt_mod.DEFAULT_SECURITY_PATHS),
            "extra_security_paths": [],
            "architecture_paths": list(opt_mod.DEFAULT_ARCHITECTURE_PATHS),
            "extra_architecture_paths": [],
        },
        "budgets": {
            "architect": 3,
            "implementer": 5,
            "review_fixer": 4,
            "test": 8,
            "total_delegated_runs": 40,
            "max_runtime_seconds": 14400,
            "max_repeats_without_progress": 2,
        },
        "workspace": {
            "dir": ".ai",
            # The first command of a new workflow notes the others quiet for
            # this many days or more. Nothing is deleted; 0 turns it off.
            "stale_notice_days": 30,
        },
        # The language the orchestrator answers the user in, as a BCP 47 tag
        # (`ja`, `zh-TW`, `ko`, `en`). Null leaves it to SKILL.md rule 11 and
        # keeps the Claude Code hooks silent. ``rewrite: false`` keeps
        # the reminders and drops the Stop-hook check, for a check that
        # misjudges someone's replies.
        "language": {"reply": None, "rewrite": True},
    }


class ConfigError(ValueError):
    """Raised for malformed or invalid configuration."""


# --------------------------------------------------------------------------- paths


def global_config_dir() -> str:
    override = os.environ.get("DEV_ORCHESTRA_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(os.path.abspath(os.path.expanduser(base)), APP_DIR_NAME)


def user_providers_dir() -> str:
    """Where a user's own provider adapters live; loaded by the provider registry.

    Under the config directory, which ``DEV_ORCHESTRA_HOME`` moves and
    ``DEV_ORCHESTRA_CONFIG`` does not: that one names a file, which may sit in
    a project checkout, and code beside it is not the user's to import.
    """
    return os.path.join(global_config_dir(), "providers")


def user_providers_hint() -> str:
    """Where a missing provider's adapter would come from, and whether it can."""
    # lazy: the provider registry; see the note at the top of this module
    from .providers import USER_PROVIDERS_DISABLED_ENV, user_providers_disabled

    hint = "user adapters load from %s" % shown_location(user_providers_dir())
    if user_providers_disabled():
        hint += " (disabled by %s)" % USER_PROVIDERS_DISABLED_ENV
    return hint


def global_config_path() -> str:
    explicit = os.environ.get("DEV_ORCHESTRA_CONFIG")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return os.path.join(global_config_dir(), "config.yaml")


def on_windows() -> bool:
    """Whether this process runs on Windows; tests replace it."""
    return sys.platform.startswith("win")


def real_location(path: str) -> str:
    """Where ``path`` really is on disk; tests replace it."""
    return os.path.realpath(path)


def _path_key(path: str) -> str:
    # Windows paths compare case-insensitively and either separator works;
    # spelled out rather than ``normcase`` so it does not depend on the host.
    return path.lower().replace("/", "\\")


_PACKAGES_KEY = "\\appdata\\local\\packages\\"


def stored_elsewhere(path: str) -> Optional[str]:
    """The real location of ``path`` when Windows keeps it somewhere else.

    A Microsoft Store Python has the files it writes under the user's AppData
    quietly redirected into its package folder, so the path we built is only
    right from inside this process. ``None`` off Windows and whenever the file
    is where it seems to be. Links that merely rename a folder above the file,
    such as 8.3 short names, cancel out: the comparison is against the real
    location of an anchor -- the user profile, which is never redirected, or
    the file's own folder when it sits outside the profile.
    """
    if not on_windows():
        return None
    home = os.path.expanduser("~")
    try:
        rel = os.path.relpath(path, home)
    except ValueError:  # another drive
        rel = os.pardir
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        anchor = os.path.dirname(path)
        try:
            rel = os.path.relpath(path, anchor)
        except ValueError:
            rel = os.path.basename(path)
    else:
        anchor = home
    expected = real_location(anchor) if rel == os.curdir else os.path.join(real_location(anchor), rel)
    real = real_location(path)
    if _path_key(real) != _path_key(expected):
        return real
    if _PACKAGES_KEY in _path_key(real) and _PACKAGES_KEY not in _path_key(path):
        return real
    return None


def package_redirected(path: str) -> Optional[str]:
    """``stored_elsewhere``, only when a Microsoft Store package is what moves it.

    A link the user made leaves the real location elsewhere too, but a write
    through it lands where they meant it to.
    """
    real = stored_elsewhere(path)
    if real is not None and _PACKAGES_KEY in _path_key(real) and _PACKAGES_KEY not in _path_key(path):
        return real
    return None


def shown_location(path: str) -> str:
    """``path`` as printed: with its real location when that differs."""
    return describe_location(path, stored_elsewhere(path))


def describe_location(path: Any, real: Optional[str]) -> str:
    """``path``, and ``real`` beside it when there is one; for a renderer that
    reads a location ``stored_elsewhere`` already found."""
    if real is None:
        return str(path)
    return "%s (stored at %s)" % (path, real)


def find_project_config(start: Optional[str] = None) -> Optional[str]:
    """Walk up from ``start`` looking for a project override file.

    The walk stops at the repository root: any ``.git`` entry, so a
    worktree's or a submodule's ``.git`` file ends it too, rather than
    letting a parent directory's file configure the checkout.
    """
    for current in _up_to_repository_root(start):
        for name in PROJECT_CONFIG_NAMES:
            candidate = os.path.join(current, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def _up_to_repository_root(start: Optional[str] = None) -> List[str]:
    """``start`` and each parent up to the first holding a ``.git`` entry, or to the
    filesystem root when none does. A walk rather than ``git rev-parse``: a
    reply-language hook runs it on every prompt."""
    current = os.path.abspath(start or os.getcwd())
    walked = [current]
    while not os.path.lexists(os.path.join(current, ".git")):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        walked.append(current)
    return walked


def repository_root(start: Optional[str] = None) -> Optional[str]:
    """The nearest directory up from ``start`` holding a ``.git`` entry, as
    ``find_project_config`` stops at; None outside a repository."""
    top = _up_to_repository_root(start)[-1]
    return top if os.path.lexists(os.path.join(top, ".git")) else None


def project_config_path(start: Optional[str] = None) -> str:
    """Where a *new* project override would be written."""
    existing = find_project_config(start)
    if existing:
        return existing
    return os.path.join(os.path.abspath(start or os.getcwd()), PROJECT_CONFIG_NAMES[0])


# --------------------------------------------------------------------------- io


def read_config_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
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


def write_config_file(path: str, data: Dict[str, Any], scope: str = "") -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
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

    def workspace_dir(self, root: str) -> str:
        workspace = (self.data.get("workspace") or {}).get("dir") or ".ai"
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


#: Settings whose value is a matter of taste rather than a recommendation, so
#: a difference from the default says nothing worth reporting.
_TASTE = ("version", "reviewers", "workspace", "language")

#: Settings whose default is empty and which only ever add to another one, so
#: any value in a file was put there by someone. No release ever seeded a file
#: with them, which is the one thing a pinned report is looking for.
_ADDITIONS = (
    "optimization.extra_high_risk_paths",
    "optimization.extra_security_paths",
    "optimization.extra_architecture_paths",
)


def pinned_differences(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Settings this configuration fixes at a value the defaults have moved off.

    Before 0.6.0 every writer seeded the file with the whole of
    `default_config()`, so a default that was later improved never reached an
    existing installation: the file kept answering with the number that was
    current when it was written. That happened -- the low-risk thresholds were
    raised in 0.4.2 and every config written before it went on reporting the
    old pair, so the panel reduction the release was for could not fire.
    Writers are sparse now, but the files those releases wrote are still on
    disk, so this report is still what finds them.

    Reported, never corrected. "Chose 2 deliberately" and "inherited 2 from an
    older default" are the same two characters on disk, and silently rewriting
    the first would be worse than leaving the second to be noticed.
    `config prune` does it on request, which is a different thing.

    Lists are compared by length, not contents: `high_risk_paths` is thirty
    entries and nobody reads a diff of it in a diagnostic. `reviewers` is not
    compared at all (`_TASTE`): a panel is the user's own, and holding it
    against the default one would report every installation that added a
    reviewer. Nor is an addition (`_ADDITIONS`) such as
    `extra_high_risk_paths`: it arrived after writers went sparse, so a value
    in a file is always one somebody added, never an inherited default.
    """
    differences: List[Dict[str, Any]] = []

    def walk(current: Any, default: Any, path: str) -> None:
        if isinstance(default, dict):
            if not isinstance(current, dict):
                return
            for key, value in default.items():
                if path == "" and key in _TASTE:
                    continue
                name = ("%s.%s" % (path, key)) if path else str(key)
                if name in _ADDITIONS:
                    continue
                if key in current:
                    walk(current[key], value, name)
            return
        if isinstance(default, list):
            if isinstance(current, list) and len(current) != len(default):
                differences.append(
                    {
                        "setting": path,
                        "value": "%d entries" % len(current),
                        "default": "%d entries" % len(default),
                    }
                )
            return
        if current != default:
            differences.append({"setting": path, "value": current, "default": default})

    walk(data, default_config(), "")
    return sorted(differences, key=lambda entry: entry["setting"])


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

    For the files written before 0.6.0, which hold every default beside the
    handful of values their owner actually chose. Nothing on disk distinguishes
    the two, so "equal to what this layer inherits" is the only evidence there
    is -- which is why this is a command a user runs rather than something a
    writer does on its way past.

    ``base`` is what would be in force without this layer, not the built-in
    defaults: a project file may hold a value equal to a default precisely to
    cancel a global one, and comparing it against the defaults would throw that
    away. Lists are dropped only when equal whole, because that is the unit
    ``deep_merge`` replaces. ``version`` identifies the file format rather than
    configuring anything, so it survives -- and is supplied when the old file
    never had one.
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
    data = deep_merge(deep_merge(deep_merge(defaults, values), global_layer), project_layer)
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

    data, fit, preset, source = compose(global_layer, project_layer, presets.installed_providers())
    loaded = LoadedConfig(
        data,
        global_found,
        ppath,
        not (global_found or ppath),
        global_layer=copy.deepcopy(global_layer),
        project_layer=copy.deepcopy(project_layer),
        preset=preset,
        preset_source=source,
        preset_notes=fit.notes,
        files_data=deep_merge(deep_merge(default_config(), global_layer), project_layer),
        reviewer_origins=fit.origins,
        design_reviewer_origins=fit.design_origins,
    )
    if validate_result:
        problems = validate(
            data,
            project_layer=project_layer,
            global_layer=global_layer,
            origins=fit.origins,
            design_origins=fit.design_origins,
        )
        if problems:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems))
    return loaded


# --------------------------------------------------------------------------- validation


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
    seen = set()
    for index, reviewer in enumerate(reviewers):
        origin = origins[index] if origins is not None and index < len(origins) else None
        own = origin is None or (not origin.extra and origin.design == design)
        if origin is not None and origin.extra:
            label = _origin_label(origin, index)
        else:
            label = "%s[%d]" % (keys.reviewers, index)
        if not isinstance(reviewer, dict):
            if own:
                problems.append("%s: must be a mapping" % label)
            continue
        rid = reviewer.get("id")
        if not isinstance(rid, str) or not _ID_RE.match(rid):
            if own:
                problems.append("%s: id must match [a-z0-9][a-z0-9._-]* (got %r)" % (label, rid))
        elif rid in seen:
            problems.append("%s: duplicate reviewer id %r" % (label, rid))
        else:
            seen.add(rid)
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


def _validate_review(review: Any) -> List[str]:
    if not isinstance(review, dict):
        return ["review: must be a mapping"]
    problems: List[str] = []
    iterations = review.get("max_review_iterations", 2)
    if not _int_at_least(iterations, 0):
        problems.append("review.max_review_iterations: must be a non-negative integer")
    timeout = review.get("timeout_seconds", 1800)
    if not _int_at_least(timeout, 1):
        problems.append("review.timeout_seconds: must be a positive integer")
    idle = review.get("idle_timeout_seconds")
    if idle is not None and not _int_at_least(idle, 1):
        problems.append("review.idle_timeout_seconds: must be a positive integer or null")
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
        problems.append("language.reply: must be a language tag such as ja, zh-TW, ko or en, or null")
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
    seen = set()
    for index, reviewer in enumerate(extras):
        label = "%s[%d] in the %s file" % (panel_keys.extras, index, name)
        if not isinstance(reviewer, dict):
            problems.append("%s: must be a mapping" % label)
            continue
        rid = reviewer.get("id")
        if not isinstance(rid, str) or not _ID_RE.match(rid):
            problems.append("%s: id must match [a-z0-9][a-z0-9._-]* (got %r)" % (label, rid))
        elif rid in seen:
            problems.append("%s: duplicate reviewer id %r" % (label, rid))
        else:
            seen.add(rid)
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


def _reviewer_seats(
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


def _read_only_seats(
    data: Dict[str, Any], design_origins: Optional[Sequence[ReviewerOrigin]] = None
) -> List[SeatRun]:
    """The read-only roles and their tiers, then every reviewer (``_reviewer_seats``)."""
    return role_seats(data, READ_ONLY_ROLES) + _reviewer_seats(data, design_origins)


def default_reviewer_family(provider: str) -> str:
    """The family a new reviewer on ``provider`` gets when none is named."""
    return {"claude": "opus", "agy": "default"}.get(provider, "recommended-coding")


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


def set_path(data: Dict[str, Any], dotted: str, value: Any) -> Dict[str, Any]:
    """Set ``a.b.c`` (and ``reviewers[0].role``) to ``value`` in place."""
    node: Any = data
    parts = _split_path(dotted)
    for part in parts[:-1]:
        node = _descend(node, part, create=True)
    last = parts[-1]
    if isinstance(last, int):
        if not isinstance(node, list):
            raise ConfigError("%s: not a list" % dotted)
        node[last] = value
    else:
        if not isinstance(node, dict):
            raise ConfigError("%s: not a mapping" % dotted)
        node[last] = value
    return data


def pop_path(data: Dict[str, Any], dotted: str) -> None:
    """Remove the mapping key at ``a.b.c`` in place, and each parent it leaves empty.

    A path that is not there, or runs through something other than a
    mapping, is left as it is.
    """
    parts = dotted.split(".")
    parents = [data]
    for part in parts[:-1]:
        child = parents[-1].get(part)
        if not isinstance(child, dict):
            return
        parents.append(child)
    parents[-1].pop(parts[-1], None)
    for depth in range(len(parents) - 1, 0, -1):
        if not parents[depth]:
            parents[depth - 1].pop(parts[depth - 1], None)


def get_path(data: Dict[str, Any], dotted: str, default: Any = None) -> Any:
    try:
        parts = _split_path(dotted)
    except ConfigError:
        return default
    return _get_parts(data, parts, default)


def _names_null(data: Dict[str, Any], parts: Sequence[Any]) -> bool:
    """Whether ``data`` writes a null at ``parts``, for a key whose null replaces."""
    if tuple(parts) not in _NULL_REPLACES:
        return False
    parent = _get_parts(data, parts[:-1], _MISSING)
    return isinstance(parent, dict) and parts[-1] in parent and parent[parts[-1]] is None


def _get_parts(data: Dict[str, Any], parts: Sequence[Any], default: Any) -> Any:
    node: Any = data
    try:
        for part in parts:
            node = _descend(node, part, create=False)
    except (KeyError, IndexError, TypeError, ConfigError):
        return default
    return node


def _split_path(dotted: str) -> List[Any]:
    parts: List[Any] = []
    for chunk in dotted.split("."):
        match = re.match(r"^([^\[\]]*)((?:\[\d+\])*)$", chunk)
        if not match:
            raise ConfigError("malformed path segment %r" % chunk)
        name, indexes = match.groups()
        if name:
            parts.append(name)
        for raw_index in re.findall(r"\[(\d+)\]", indexes or ""):
            parts.append(int(raw_index))
    if not parts:
        raise ConfigError("empty configuration path")
    return parts


def _descend(node: Any, part: Any, create: bool) -> Any:
    if isinstance(part, int):
        if not isinstance(node, list):
            raise ConfigError("expected a list at index %d" % part)
        return node[part]
    if not isinstance(node, dict):
        raise ConfigError("expected a mapping at %r" % part)
    if part not in node or node[part] is None:
        if not create:
            raise KeyError(part)
        node[part] = {}
    return node[part]


def coerce_scalar(text: str) -> Any:
    """Turn a CLI-supplied string into the natural scalar (int, bool, list, str)."""
    if not text.strip():
        return ""
    if "\n" in text:
        return miniyaml.loads(text)
    return miniyaml.parse_scalar(text)


# --------------------------------------------------------------------------- reviewers


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
