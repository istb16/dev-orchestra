"""Reading and writing a configuration by dotted path, such as ``reviewers[0].role``.

``config`` re-exports its public names.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Sequence, Tuple

from . import miniyaml
from .config_common import ConfigError

#: ``(old, new)`` for each key that moved. The old name is still read from a
#: file, as the new one, whenever the same file does not set the new one.
RENAMED_KEYS: Tuple[Tuple[str, str], ...] = (("review.max_review_iterations", "review.code.max_iterations"),)


def current_name(dotted: str) -> str:
    """``dotted``, or the name it was renamed to."""
    for old, new in RENAMED_KEYS:
        if dotted == old:
            return new
    return dotted


def renamed_in(layer: Dict[str, Any]) -> List[Tuple[str, str, bool]]:
    """``(old, new, both_set)`` for each old name ``layer`` still uses.

    ``both_set`` is true when the same layer sets the new name too, so the
    old one is ignored. An old name whose new parent is not a mapping is left
    out: it is not read at all, and ``validate`` reports that parent.
    """
    found = []
    for old, new in RENAMED_KEYS:
        if _present(layer, old) and not _blocked(layer, new):
            found.append((old, new, _present(layer, new)))
    return found


def with_current_names(layer: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of ``layer`` with each old name moved to its new one.

    The new name wins when both are set, even to null; the old one is dropped
    either way. A null parent of the new name counts as absent. A parent that
    is set to anything else but a mapping leaves the layer as it is, for
    ``validate`` to refuse.
    """
    result = copy.deepcopy(layer)
    for old, new in RENAMED_KEYS:
        if not _present(result, old) or _blocked(result, new):
            continue
        if not _present(result, new):
            set_path(result, new, get_path(result, old))
        pop_path(result, old)
    return result


def drop_old_names(layer: Dict[str, Any], written: str) -> None:
    """Remove from ``layer``, in place, each old name that a write of ``written`` replaced.

    That is each old name whose new one is ``written`` or lies under it, and
    which the write left set, so a write of ``review.code`` that sets
    ``max_iterations`` clears ``review.max_review_iterations`` too. The file
    then sets the new name, and an old one there would only be ignored. A
    write that leaves the new name unset keeps the old one, still read as it.
    """
    for old, new in RENAMED_KEYS:
        if (new == written or new.startswith(written + ".")) and _present(layer, new):
            pop_path(layer, old)


def _present(layer: Dict[str, Any], dotted: str) -> bool:
    parent, _, last = dotted.rpartition(".")
    node = get_path(layer, parent) if parent else layer
    return isinstance(node, dict) and last in node


def _blocked(layer: Dict[str, Any], dotted: str) -> bool:
    """True when a parent of ``dotted`` is set to something other than a mapping."""
    node: Any = layer
    for part in dotted.split(".")[:-1]:
        node = node.get(part)
        if node is None:
            return False
        if not isinstance(node, dict):
            return True
    return False


def set_path(data: Dict[str, Any], dotted: str, value: Any) -> Dict[str, Any]:
    """Set ``a.b.c`` (and ``reviewers[0].role``) to ``value`` in place."""
    node: Any = data
    parts = split_path(dotted)
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
        parts = split_path(dotted)
    except ConfigError:
        return default
    return get_parts(data, parts, default)


def get_parts(data: Dict[str, Any], parts: Sequence[Any], default: Any) -> Any:
    """``get_path`` for a path already split, as ``split_path`` returns it."""
    node: Any = data
    try:
        for part in parts:
            node = _descend(node, part, create=False)
    except (KeyError, IndexError, TypeError, ConfigError):
        return default
    return node


def split_path(dotted: str) -> List[Any]:
    """``a.b[0].c`` as ``["a", "b", 0, "c"]``; a malformed path is a ``ConfigError``."""
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
