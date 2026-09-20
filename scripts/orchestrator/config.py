"""Configuration loading, layering, validation and persistence.

Precedence (highest first):

1. project config   -- ``.dev-orchestra.yaml`` found by walking up from cwd
2. global config    -- OS-appropriate user config directory
3. built-in defaults

Only *model families* and a *version policy* are persisted. Concrete model ids
are resolved at run time by the provider adapters so that the configuration
keeps following whatever the CLI currently considers "latest".
"""

from __future__ import annotations

import copy
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from . import miniyaml

CONFIG_VERSION = 1
APP_DIR_NAME = "dev-orchestra"
PROJECT_CONFIG_NAMES = (
    ".dev-orchestra.yaml",
    ".dev-orchestra.yml",
    ".dev-orchestra.json",
)

KNOWN_ROLES = ("orchestrator", "architect", "implementer", "review_fixer")

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


def _default_exclude() -> Tuple[str, ...]:
    """The review module owns the list; this module only persists it.

    Imported late because :mod:`orchestrator.review` imports the provider
    registry, and configuration must stay loadable without it.
    """
    from .review import DEFAULT_EXCLUDE

    return DEFAULT_EXCLUDE


def _optimization():
    """Late, for the same reason as ``_default_exclude``."""
    from . import optimization

    return optimization


def is_valid_reviewer_id(value: Any) -> bool:
    """The same rule ``validate`` applies, exposed for input-time checking."""
    return isinstance(value, str) and bool(_ID_RE.match(value))


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
            "exclude": list(_default_exclude()),
            # A second round diffs against what the first round reviewed, so
            # re-review sees the fix instead of the whole change again. The
            # findings the fix was meant to address ride along with it.
            "incremental_rounds": True,
            # How many findings a reviewer is asked for. Output is billed at
            # several times the input rate, and a reviewer's output is billed
            # again as the fixer's brief, so an uncapped reviewer costs twice
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
            "context": {"max_chars": 400_000, "inline_chars": 400_000},
            # Review the plan with the same panel before any code is written.
            # Off by default: turning it on adds a reviewer run per panel
            # member per round plus an architect re-run, which is a real cost
            # to impose on every existing workflow, and nothing about the
            # current behaviour changes while it stays off. Whoever wants it
            # says so once -- `config set review.design.enabled true`.
            "design": {"enabled": False, "max_iterations": 2},
        },
        # How hard to try to be cheap. See orchestrator/optimization.py: the
        # level gates a review of a tree whose tests are recorded as failing,
        # decides whether a small change gets one reviewer or the whole panel,
        # and sets the findings cap when review.max_findings is unset. A
        # change touching a high-risk path escalates to `quality` whatever is
        # configured here -- that part is not negotiable, only its patterns.
        "optimization": {
            "level": _optimization().DEFAULT_LEVEL,
            "high_risk_paths": list(_optimization().DEFAULT_HIGH_RISK_PATHS),
            "low_risk_max_files": _optimization().DEFAULT_LOW_RISK_MAX_FILES,
            "low_risk_max_lines": _optimization().DEFAULT_LOW_RISK_MAX_LINES,
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
        },
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


def global_config_path() -> str:
    explicit = os.environ.get("DEV_ORCHESTRA_CONFIG")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return os.path.join(global_config_dir(), "config.yaml")


def find_project_config(start: Optional[str] = None) -> Optional[str]:
    """Walk up from ``start`` looking for a project override file."""
    current = os.path.abspath(start or os.getcwd())
    while True:
        for name in PROJECT_CONFIG_NAMES:
            candidate = os.path.join(current, name)
            if os.path.isfile(candidate):
                return candidate
        if os.path.isdir(os.path.join(current, ".git")):
            return None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


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


def deep_merge(base: Any, override: Any) -> Any:
    """Merge ``override`` onto ``base``.

    Mappings merge key-by-key. Lists (notably ``reviewers``) replace wholesale,
    so a project override can define a completely different review panel.
    """
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = deep_merge(base.get(key), value) if key in base else copy.deepcopy(value)
        return merged
    if override is None:
        return copy.deepcopy(base)
    return copy.deepcopy(override)


class LoadedConfig:
    """A resolved configuration plus provenance about where it came from."""

    def __init__(
        self,
        data: Dict[str, Any],
        global_path: Optional[str],
        project_path: Optional[str],
        used_defaults: bool,
    ) -> None:
        self.data = data
        self.global_path = global_path
        self.project_path = project_path
        self.used_defaults = used_defaults

    @property
    def exists(self) -> bool:
        """True when at least one config file was found on disk."""
        return bool(self.global_path or self.project_path)

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

    def optimization_settings(self) -> Dict[str, Any]:
        settings = default_config()["optimization"]
        settings.update(self.data.get("optimization") or {})
        return settings

    def workspace_dir(self, root: str) -> str:
        workspace = (self.data.get("workspace") or {}).get("dir") or ".ai"
        if os.path.isabs(workspace):
            return workspace
        return os.path.join(root, workspace)


#: Settings whose value is a matter of taste rather than a recommendation, so
#: a difference from the default says nothing worth reporting.
_TASTE = ("version", "reviewers", "workspace")


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
    reviewer.
    """
    differences: List[Dict[str, Any]] = []

    def walk(current: Any, default: Any, path: str) -> None:
        if isinstance(default, dict):
            if not isinstance(current, dict):
                return
            for key, value in default.items():
                if path == "" and key in _TASTE:
                    continue
                if key in current:
                    walk(current[key], value, ("%s.%s" % (path, key)) if path else str(key))
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


def load(start: Optional[str] = None, validate_result: bool = True) -> LoadedConfig:
    """Load the layered configuration for the project rooted at ``start``."""
    data = default_config()
    gpath = global_config_path()
    global_found = gpath if os.path.isfile(gpath) else None
    if global_found:
        data = deep_merge(data, read_config_file(global_found))

    ppath = find_project_config(start)
    if ppath:
        data = deep_merge(data, read_config_file(ppath))

    loaded = LoadedConfig(data, global_found, ppath, not (global_found or ppath))
    if validate_result:
        problems = validate(data)
        if problems:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems))
    return loaded


# --------------------------------------------------------------------------- validation


def validate(data: Dict[str, Any], known_providers: Optional[List[str]] = None) -> List[str]:
    """Return a list of human-readable problems; empty means valid."""
    from .providers import available_providers

    providers = known_providers if known_providers is not None else available_providers()
    problems: List[str] = []

    version = data.get("version")
    if version != CONFIG_VERSION:
        problems.append("version must be %d (got %r)" % (CONFIG_VERSION, version))

    for role in KNOWN_ROLES:
        spec = data.get(role)
        if not isinstance(spec, dict):
            problems.append("%s: missing role definition" % role)
            continue
        problems.extend("%s: %s" % (role, msg) for msg in _validate_role(spec, providers))
        problems.extend(_validate_tiers(spec, role, providers))

    reviewers = data.get("reviewers")
    if reviewers is None:
        reviewers = []
    if not isinstance(reviewers, list):
        problems.append("reviewers: must be a list (use [] for none)")
    else:
        seen = set()
        for index, reviewer in enumerate(reviewers):
            label = "reviewers[%d]" % index
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
            role_name = reviewer.get("role", "general")
            if not isinstance(role_name, str) or not role_name.strip():
                problems.append("%s: role must be a non-empty string" % label)
            problems.extend("%s: %s" % (label, msg) for msg in _validate_role(reviewer, providers))

    review = data.get("review")
    if review is not None:
        if not isinstance(review, dict):
            problems.append("review: must be a mapping")
        else:
            iterations = review.get("max_review_iterations", 2)
            if not isinstance(iterations, int) or isinstance(iterations, bool) or iterations < 0:
                problems.append("review.max_review_iterations: must be a non-negative integer")
            timeout = review.get("timeout_seconds", 1800)
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                problems.append("review.timeout_seconds: must be a positive integer")
            idle = review.get("idle_timeout_seconds")
            if idle is not None and (not isinstance(idle, int) or isinstance(idle, bool) or idle <= 0):
                problems.append("review.idle_timeout_seconds: must be a positive integer or null")
            incremental = review.get("incremental_rounds")
            if incremental is not None and not isinstance(incremental, bool):
                problems.append("review.incremental_rounds: must be true or false")
            findings_cap = review.get("max_findings")
            if findings_cap is not None and (
                not isinstance(findings_cap, int) or isinstance(findings_cap, bool) or findings_cap < 0
            ):
                problems.append("review.max_findings: must be a non-negative integer (0 = no cap)")
            design = review.get("design")
            if design is not None:
                if not isinstance(design, dict):
                    problems.append("review.design: must be a mapping")
                else:
                    enabled = design.get("enabled")
                    if enabled is not None and not isinstance(enabled, bool):
                        problems.append("review.design.enabled: must be true or false")
                    rounds = design.get("max_iterations")
                    if rounds is not None and (
                        not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 0
                    ):
                        problems.append("review.design.max_iterations: must be a non-negative integer")
            context = review.get("context")
            if context is not None:
                if not isinstance(context, dict):
                    problems.append("review.context: must be a mapping")
                else:
                    # No cross-check between the two. `inline_chars` above
                    # `max_chars` only says "nothing is ever handed over as a
                    # file except a forced round", and below it says "I will
                    # not pay for a prompt that large" -- both are things
                    # somebody means, and neither is a mistake to warn about.
                    for key in ("max_chars", "inline_chars"):
                        value = context.get(key)
                        if value is not None and (
                            not isinstance(value, int) or isinstance(value, bool) or value < 1
                        ):
                            problems.append("review.context.%s: must be a positive integer" % key)
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

    optimization = data.get("optimization")
    if optimization is not None:
        problems.extend(_validate_optimization(optimization))

    budgets = data.get("budgets")
    if budgets is not None:
        if not isinstance(budgets, dict):
            problems.append("budgets: must be a mapping")
        else:
            for key, value in budgets.items():
                if value is None:
                    continue
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    problems.append("budgets.%s: must be a non-negative integer or null" % key)
    return problems


def _validate_optimization(data: Any) -> List[str]:
    problems: List[str] = []
    if not isinstance(data, dict):
        return ["optimization: must be a mapping"]
    levels = _optimization().LEVELS
    level = data.get("level")
    if level is not None and (not isinstance(level, str) or level.strip().lower() not in levels):
        problems.append("optimization.level: must be one of %s" % ", ".join(sorted(levels)))
    patterns = data.get("high_risk_paths")
    if patterns is not None:
        if not isinstance(patterns, list):
            problems.append("optimization.high_risk_paths: must be a list of glob patterns (use [] for none)")
        else:
            for index, pattern in enumerate(patterns):
                if not isinstance(pattern, str) or not pattern.strip():
                    problems.append(
                        "optimization.high_risk_paths[%d]: must be a non-empty string (got %r)"
                        % (index, pattern)
                    )
    for key in ("low_risk_max_files", "low_risk_max_lines"):
        value = data.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
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
        problems.append("unknown provider %r (known: %s)" % (provider, ", ".join(providers)))

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


def get_path(data: Dict[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = data
    try:
        for part in _split_path(dotted):
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
) -> Dict[str, Any]:
    reviewer: Dict[str, Any] = {"id": reviewer_id, "provider": provider}
    model: Dict[str, Any] = {}
    if family:
        model["family"] = family
    model["version"] = version
    if model_id:
        model["id"] = model_id
    reviewer["model"] = model
    reviewer["role"] = role
    return reviewer


def add_reviewer(data: Dict[str, Any], reviewer: Dict[str, Any]) -> Dict[str, Any]:
    reviewers = data.setdefault("reviewers", [])
    if not isinstance(reviewers, list):
        raise ConfigError("reviewers: must be a list")
    if any(isinstance(item, dict) and item.get("id") == reviewer.get("id") for item in reviewers):
        raise ConfigError("reviewer id %r already exists" % reviewer.get("id"))
    reviewers.append(reviewer)
    return data


def remove_reviewer(data: Dict[str, Any], selector: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Remove a reviewer by id, by ``role`` name, or by 1-based position."""
    reviewers = data.get("reviewers") or []
    if not isinstance(reviewers, list):
        raise ConfigError("reviewers: must be a list")

    index = _find_reviewer_index(reviewers, selector)
    removed = reviewers.pop(index)
    data["reviewers"] = reviewers
    return data, removed


def find_reviewer(data: Dict[str, Any], selector: str) -> Tuple[int, Dict[str, Any]]:
    reviewers = data.get("reviewers") or []
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
