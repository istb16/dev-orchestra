"""Dependency-free YAML subset codec.

The orchestrator config is a small, regular document: nested mappings, lists of
mappings, and scalars. Rather than force a PyYAML install on every machine that
runs the skill, this module parses/emits exactly that subset, and transparently
defers to PyYAML when it happens to be installed.

Supported: nested block mappings, block sequences (``- item`` and ``- key: v``),
inline empty collections (``[]`` / ``{}``), inline scalar lists (``[a, b]``),
``#`` comments, single/double quoted strings, int/float/bool/null scalars.

Not supported (raises ``YamlError``): anchors, aliases, multi-document streams,
block scalars (``|`` / ``>``), complex keys, nested flow collections.
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Tuple

try:  # pragma: no cover - exercised only where PyYAML is installed
    import yaml as _pyyaml
except Exception:  # pragma: no cover
    _pyyaml = None


class YamlError(ValueError):
    """Raised when a document uses YAML features outside the supported subset."""


class _Line:
    __slots__ = ("content", "indent", "lineno")

    def __init__(self, indent: int, content: str, lineno: int) -> None:
        self.indent = indent
        self.content = content
        self.lineno = lineno

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "_Line(%d, %r, line %d)" % (self.indent, self.content, self.lineno)


def _strip_comment(raw: str) -> str:
    out: List[str] = []
    quote = ""
    for idx, ch in enumerate(raw):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (idx == 0 or raw[idx - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _read_lines(text: str) -> List[_Line]:
    lines: List[_Line] = []
    for lineno, raw in enumerate(text.replace("\t", "  ").splitlines(), 1):
        if raw.strip() in ("---", "..."):
            continue
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append(_Line(indent, stripped.strip(), lineno))
    return lines


_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?$")


def _parse_scalar(token: str) -> Any:
    token = token.strip()
    if token == "":
        return None
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        body = token[1:-1]
        if token[0] == '"':
            return body.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        return body.replace("''", "'")
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        if not inner:
            return []
        if "[" in inner or "{" in inner:
            raise YamlError("nested flow collections are not supported: %r" % token)
        return [_parse_scalar(part) for part in _split_flow(inner)]
    if token == "{}":
        return {}
    if token.startswith("{"):
        raise YamlError("inline mappings are not supported: %r" % token)
    lowered = token.lower()
    if lowered in ("null", "~"):
        return None
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if _INT_RE.match(token):
        return int(token)
    if _FLOAT_RE.match(token):
        return float(token)
    return token


def _split_flow(inner: str) -> List[str]:
    parts: List[str] = []
    buf: List[str] = []
    quote = ""
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _is_seq_line(line: _Line) -> bool:
    return line.content == "-" or line.content.startswith("- ")


def _parse_node(lines: List[_Line], i: int) -> Tuple[Any, int]:
    if _is_seq_line(lines[i]):
        return _parse_seq(lines, i, lines[i].indent)
    return _parse_map(lines, i, lines[i].indent)


def _parse_seq(lines: List[_Line], i: int, indent: int) -> Tuple[List[Any], int]:
    items: List[Any] = []
    while i < len(lines) and lines[i].indent == indent and _is_seq_line(lines[i]):
        rest = lines[i].content[1:].strip()
        child_indent = indent + 2
        if not rest:
            i += 1
            if i < len(lines) and lines[i].indent > indent:
                value, i = _parse_node(lines, i)
            else:
                value = None
            items.append(value)
            continue
        if not _split_key(rest)[1] and not _is_seq_line(_Line(0, rest, 0)):
            items.append(_parse_scalar(rest))
            i += 1
            continue
        sub: List[_Line] = [_Line(child_indent, rest, lines[i].lineno)]
        j = i + 1
        while j < len(lines) and lines[j].indent > indent:
            shifted = child_indent + (lines[j].indent - indent - 2)
            sub.append(_Line(max(shifted, child_indent), lines[j].content, lines[j].lineno))
            j += 1
        value, _ = _parse_node(sub, 0)
        items.append(value)
        i = j
    return items, i


def _parse_map(lines: List[_Line], i: int, indent: int) -> Tuple[dict, int]:
    out: dict = {}
    while i < len(lines) and lines[i].indent == indent:
        line = lines[i]
        if _is_seq_line(line):
            break
        content = line.content
        if content.endswith("|") or content.endswith(">"):
            raise YamlError("block scalars are not supported (line %d)" % line.lineno)
        key_part, sep, rest = _split_key(content)
        if not sep:
            raise YamlError("expected 'key: value' (line %d): %r" % (line.lineno, content))
        key = _parse_scalar(key_part)
        if not isinstance(key, str):
            key = str(key)
        rest = rest.strip()
        if rest:
            out[key] = _parse_scalar(rest)
            i += 1
            continue
        i += 1
        if i < len(lines) and lines[i].indent > indent:
            out[key], i = _parse_node(lines, i)
        elif i < len(lines) and lines[i].indent == indent and _is_seq_line(lines[i]):
            out[key], i = _parse_seq(lines, i, indent)
        else:
            out[key] = None
    return out, i


def _split_key(content: str) -> Tuple[str, str, str]:
    quote = ""
    for idx, ch in enumerate(content):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == ":" and (idx + 1 == len(content) or content[idx + 1] in " \t"):
            return content[:idx], ":", content[idx + 1 :]
    return content, "", ""


def loads(text: str) -> Any:
    """Parse a YAML (subset) or JSON document into Python data."""
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith("{") or stripped.startswith("["):
        return json.loads(stripped)
    if _pyyaml is not None:
        return _pyyaml.safe_load(text)
    lines = _read_lines(text)
    if not lines:
        return None
    value, consumed = _parse_node(lines, 0)
    if consumed != len(lines):
        raise YamlError(
            "unexpected indentation at line %d: %r" % (lines[consumed].lineno, lines[consumed].content)
        )
    return value


def parse_scalar(text: str) -> Any:
    """Parse a single scalar token (``opus``, ``2``, ``true``, ``[a, b]``)."""
    return _parse_scalar(text)


_PLAIN_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./@ +-]*$")


def _emit_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    reserved = text.lower() in ("true", "false", "yes", "no", "on", "off", "null", "~", "")
    if reserved or not _PLAIN_RE.match(text) or text != text.strip():
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return '"' + escaped + '"'
    return text


def _emit_inline(value: Any) -> str:
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    return _emit_scalar(value)


def _emit(value: Any, indent: int, out: List[str]) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            out.append(pad + "{}")
            return
        for key, item in value.items():
            if isinstance(item, dict) and item:
                out.append(pad + _emit_scalar(key) + ":")
                _emit(item, indent + 2, out)
            elif isinstance(item, list) and item:
                out.append(pad + _emit_scalar(key) + ":")
                _emit(item, indent + 2, out)
            else:
                out.append(pad + _emit_scalar(key) + ": " + _emit_inline(item))
        return
    if isinstance(value, list):
        if not value:
            out.append(pad + "[]")
            return
        for item in value:
            if isinstance(item, dict) and item:
                rendered: List[str] = []
                _emit(item, 0, rendered)
                out.append(pad + "- " + rendered[0])
                for extra in rendered[1:]:
                    out.append(pad + "  " + extra)
            elif isinstance(item, list) and item:
                rendered = []
                _emit(item, indent + 2, rendered)
                out.append(pad + "-")
                out.extend(rendered)
            else:
                out.append(pad + "- " + _emit_inline(item))
        return
    out.append(pad + _emit_scalar(value))


def dumps(value: Any) -> str:
    """Emit a YAML document for the supported subset."""
    out: List[str] = []
    _emit(value, 0, out)
    return "\n".join(out) + "\n"
