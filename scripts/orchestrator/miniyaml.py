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
from typing import Any, Iterator, List, Tuple

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


def _quote_end(text: str, idx: int) -> int:
    """Index just past the quoted string that opens at ``text[idx]``; -1 if unclosed.

    Inside double quotes a backslash escapes the next character; inside single
    quotes ``''`` is a quote, not the end.
    """
    quote = text[idx]
    j = idx + 1
    while j < len(text):
        ch = text[j]
        if quote == '"' and ch == "\\":
            j += 2
            continue
        if ch == quote:
            if quote == "'" and text[j + 1 : j + 2] == "'":
                j += 2
                continue
            return j + 1
        j += 1
    return -1


def _unquoted(text: str) -> Iterator[Tuple[int, str]]:
    """Yield ``(index, char)`` for each character outside quoted strings.

    A quote opens a string only where a scalar can start (line start, after a
    blank, ``[`` or ``,``), so the apostrophe in ``it's`` is an ordinary
    character. An unclosed quote runs to the end of the text.
    """
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if ch in "\"'" and (idx == 0 or text[idx - 1] in " \t[,"):
            end = _quote_end(text, idx)
            if end < 0:
                return
            idx = end
            continue
        yield idx, ch
        idx += 1


def _strip_comment(raw: str) -> str:
    for idx, ch in _unquoted(raw):
        if ch == "#" and (idx == 0 or raw[idx - 1] in " \t"):
            return raw[:idx].rstrip()
    return raw.rstrip()


def _read_lines(text: str) -> List[_Line]:
    lines: List[_Line] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if raw.strip() in ("---", "..."):
            continue
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        body = stripped.lstrip(" ")
        if body.startswith("\t"):
            # YAML forbids tabs in indentation; a tab anywhere else is kept as is.
            raise YamlError("tabs cannot be used for indentation (line %d)" % lineno)
        lines.append(_Line(len(stripped) - len(body), body.strip(), lineno))
    return lines


_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?$")

# The escapes of a double-quoted string, decoded in one pass so that the ``\\``
# of ``C:\\new`` is a backslash and never leaves a ``\n`` behind.
_ESCAPE_RE = re.compile(r"\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.?)", re.DOTALL)
_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}


def _unescape(match: "re.Match[str]") -> str:
    code = match.group(1)
    if len(code) > 1:
        return chr(int(code[1:], 16))
    if code in _ESCAPES:
        return _ESCAPES[code]
    raise YamlError(
        "unknown escape %r in a double-quoted string; write \\\\ for a backslash "
        "or use single quotes" % match.group(0)
    )


def _parse_quoted(token: str) -> str:
    if _quote_end(token, 0) != len(token):
        raise YamlError("malformed quoted string: %r" % token)
    body = token[1:-1]
    if token[0] == '"':
        return _ESCAPE_RE.sub(_unescape, body)
    return body.replace("''", "'")


def _parse_scalar(token: str, strict: bool = True) -> Any:
    """Read one scalar or inline list; ``strict=False`` is for command-line values.

    Not strict, a value this parser would refuse in a file for how it starts
    (an unclosed quote, say) is taken as the plain string it was typed as.
    """
    token = token.strip()
    if token == "":
        return None
    if token[0] in "\"'":
        try:
            return _parse_quoted(token)
        except YamlError:
            if strict:
                raise
            return token
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        if not inner:
            return []
        if "[" in inner or "{" in inner:
            raise YamlError("nested flow collections are not supported: %r" % token)
        return [_parse_scalar(part, strict) for part in _split_flow(inner)]
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
    if lowered in (".inf", "+.inf", "-.inf", ".nan"):
        return float(lowered.replace(".", ""))
    if _INT_RE.match(token):
        return int(token)
    if _FLOAT_RE.match(token):
        return float(token)
    return token


def _split_flow(inner: str) -> List[str]:
    parts: List[str] = []
    start = 0
    for idx, ch in _unquoted(inner):
        if ch == ",":
            parts.append(inner[start:idx])
            start = idx + 1
    parts.append(inner[start:])
    return [p.strip() for p in parts if p.strip()]


def _is_seq_line(line: _Line) -> bool:
    return line.content == "-" or line.content[:2] in ("- ", "-\t")


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
    for idx, ch in _unquoted(content):
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
    return _parse_scalar(text, strict=False)


_PLAIN_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./@ +-]*$")
# PyYAML reads more plain forms as numbers or dates than this parser does
# (``0x1F``, ``1_000``, ``2026-10-06``), so a string that starts like a number
# is quoted: the file then reads the same with or without PyYAML.
_NUMBERISH_RE = re.compile(r"^\.?\d")
_EMIT_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r"}


def _escape_char(ch: str) -> str:
    if ch in _EMIT_ESCAPES:
        return _EMIT_ESCAPES[ch]
    code = ord(ch)
    # Other control characters, and the ones splitlines() breaks a line at.
    if code < 0x20 or code == 0x7F or ch in "\x85\u2028\u2029":
        return "\\x%02x" % code if code < 0x100 else "\\u%04x" % code
    return ch


def _emit_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value != value:
            return ".nan"
        if value in (float("inf"), float("-inf")):
            return ".inf" if value > 0 else "-.inf"
        text = repr(value)
        # ``1e+20`` has no dot, and would read back as a string.
        return text if "." in text else text.replace("e", ".0e")
    if isinstance(value, int):
        return str(value)
    text = str(value)
    plain = (
        _PLAIN_RE.match(text)
        and text == text.strip()
        and not _NUMBERISH_RE.match(text)
        and isinstance(_parse_scalar(text), str)
    )
    if plain:
        return text
    return '"' + "".join(_escape_char(ch) for ch in text) + '"'


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
