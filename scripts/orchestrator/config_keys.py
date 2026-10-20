"""Reading and writing a configuration by dotted path, such as ``reviewers[0].role``.

``config`` re-exports its public names.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

from . import miniyaml
from .config_common import ConfigError


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


def coerce_scalar(text: str, path: str = "") -> Any:
    """Turn a CLI-supplied string into the natural scalar (int, bool, list, str).

    A value that does not parse -- a block with an alias or a tab in its
    indentation, say -- is a ``ConfigError`` naming ``path``, the key it was
    given for, so a command refuses it with a message and not a traceback.
    """
    if not text.strip():
        return ""
    try:
        if "\n" in text:
            return miniyaml.loads(text)
        return miniyaml.parse_scalar(text)
    except Exception as exc:  # the subset's YamlError, JSON's or PyYAML's own
        raise ConfigError("%s: %s" % (path or "value", exc)) from exc
