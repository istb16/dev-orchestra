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
        },
        "budgets": {
            "architect": 3,
            "implementer": 5,
            "review_fixer": 4,
            "test": 8,
            "total_delegated_runs": 40,
            "max_runtime_seconds": 7200,
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


def write_config_file(path: str, data: Dict[str, Any]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    text = miniyaml.dumps(data)
    header = (
        "# dev-orchestra configuration\n"
        "# Model families + a version policy are stored here on purpose: concrete\n"
        "# model ids are resolved by the provider adapters at run time.\n"
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

    def role(self, name: str) -> Dict[str, Any]:
        spec = self.data.get(name)
        if not isinstance(spec, dict):
            raise ConfigError("role %r is not configured" % name)
        return spec

    def reviewers(self) -> List[Dict[str, Any]]:
        return list(self.data.get("reviewers") or [])

    def review_settings(self) -> Dict[str, Any]:
        settings = default_config()["review"]
        settings.update(self.data.get("review") or {})
        return settings

    def workspace_dir(self, root: str) -> str:
        workspace = (self.data.get("workspace") or {}).get("dir") or ".ai"
        if os.path.isabs(workspace):
            return workspace
        return os.path.join(root, workspace)


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


def _validate_role(spec: Dict[str, Any], providers: List[str]) -> List[str]:
    problems: List[str] = []
    provider = spec.get("provider")
    if not isinstance(provider, str) or not provider:
        problems.append("provider is required")
    elif provider not in providers:
        problems.append("unknown provider %r (known: %s)" % (provider, ", ".join(providers)))

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
    problems.extend(_validate_role_options(spec, provider, providers))
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
