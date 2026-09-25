"""Surrounding context for code reviewers: the symbol enclosing each hunk.

A reviewer handed a diff sees three lines either side of every change and is
told to read any file it needs. It nearly always needs the function the hunk
sits in, and opens it itself -- at a cost this tool pays for once per reviewer
and once per round, and cannot see. ``review.context.surrounding: enclosing``
hands that function over with the diff instead.

Design rules enforced here:

* extracted when the snapshot is taken, from the git tree the diff was taken
  from, and frozen beside it -- never read from the working tree at run time,
  or a reviewer would be shown code the diff does not describe
* a symbol that cannot be extracted exactly -- drift, a symlink, an ambiguous
  path, a syntax error -- is left out and named with its reason, never guessed
* what does not fit the budget is left out by name: in the prompt, in the
  consolidated report and in ``review status``. A review shown part of its
  context must say which part
* Python only, through ``ast``. Other languages are not extracted rather than
  cut into line windows: a window that stops half way through a function is
  not "the enclosing symbol", and misleading context is worse than none

Nothing here changes what a round covers. Coverage is decided by whether the
diff was inlined, and context is extra to the diff, adopted or not.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import workspace as ws

SURROUNDING_MODES = ("none", "enclosing")

#: The same limit the untracked pass applies: larger than this is not source
#: anyone wants a reviewer to read whole.
MAX_FILE_BYTES = 512_000

#: What ``ws.git`` decodes an invalid UTF-8 byte to. A blob holding one is
#: not the text git stored, so no symbol is cut from it.
_REPLACEMENT = chr(0xFFFD)

NEW_FILE = "new file (the diff already shows all of it)"
DRIFTED = "changed while the snapshot was taken -- take it again"
NOT_FROZEN = "not frozen: the snapshot was taken with review.context.surrounding none -- take it again"
NO_CANDIDATES = "no python function, method or class encloses a hunk"
FILE_DELIVERY = "the change body is handed over as a file"
NO_BUDGET = "no budget left under review.context.max_chars / inline_chars"
NO_FIT = "no symbol fits within the budget"

#: How many left-out symbols the prompt names; the reports name them all. A
#: longer list would compete with the symbols for the budget it describes.
LEFT_OUT_SHOWN = 20

#: Skip reasons a fresh snapshot can cure, named by path rather than counted.
_NAMED = ("ambiguous path", "changed while the snapshot was taken")

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_RELATION_RANK = {"encloses": 0, "adjacent": 1}

#: What ``build_review_prompt`` puts between the notes after the diff.
_BLOCK_SEPARATOR = "\n\n"

#: The escapes ``core.quotePath`` writes, besides three octal digits.
_ESCAPES = {
    "\\": b"\\",
    '"': b'"',
    "a": b"\a",
    "b": b"\b",
    "f": b"\f",
    "n": b"\n",
    "r": b"\r",
    "t": b"\t",
    "v": b"\v",
}

ADJACENT_NOTE = 'A symbol marked "adjacent" sits next to a deletion that no surviving symbol encloses.'

Runner = Callable[..., "tuple[int, str, str]"]


def surrounding_mode(value: Any) -> str:
    """``review.context.surrounding`` as one of ``SURROUNDING_MODES``.

    ``off`` reaches here as ``False`` -- YAML reads it that way -- and means
    none. So does anything unrecognised: ``review snapshot`` and ``review
    status`` read the configuration without validating it, and a broken
    value must not switch extraction on.
    """
    if isinstance(value, str) and value.strip().lower() in SURROUNDING_MODES:
        return value.strip().lower()
    return "none"


# --------------------------------------------------------------------------- the diff


def _unquote_c(token: str) -> Optional[str]:
    """A path git quoted C-style (``core.quotePath``), or None if it is not one."""
    if len(token) < 2 or not (token.startswith('"') and token.endswith('"')):
        return None
    body = token[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out += char.encode("utf-8")
            index += 1
            continue
        if index + 1 >= len(body):
            return None
        following = body[index + 1]
        if following in _ESCAPES:
            out += _ESCAPES[following]
            index += 2
        elif re.match(r"[0-7]{3}", body[index + 1 : index + 4]):
            out.append(int(body[index + 1 : index + 4], 8) & 0xFF)
            index += 4
        else:
            return None
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _header_path(line: str, prefix: str) -> str:
    """The token after ``+++ `` / ``--- ``, as written, less a trailing tab."""
    return line[len(prefix) :].split("\t", 1)[0]


class _Block:
    def __init__(self, gitline: str) -> None:
        self.gitline = gitline
        self.minus = ""
        self.plus = ""
        self.new_file = False
        self.renamed_to = ""
        self.hunks = False
        self.pairs: "set[Tuple[int, int]]" = set()


def changed_lines(
    diff: str, files: Sequence[str]
) -> Tuple[Dict[str, List[Tuple[int, int]]], List[Dict[str, str]]]:
    """Where each file changed, on its new side, and the files that cannot be read.

    Each change is a pair ``(lo, hi)`` of new-side line numbers. An added line
    is ``(n, n)``. A deleted line is ``(n - 1, n)``: it sat between those two
    lines, and neither of them is the deleted one. Nothing is rounded -- line
    0 and a line past the end are in no symbol, which is what a deletion at
    the edge of a file is.

    The boundaries are ``_diff_line_counts``'s: ``diff --`` ends a hunk,
    ``@@`` starts one, and ``---`` / ``+++`` / ``new file mode`` /
    ``rename to`` are headers only outside a hunk.
    """
    wanted = set(files)
    pairs: Dict[str, "set[Tuple[int, int]]"] = {}
    skipped: List[Dict[str, str]] = []
    blocks: List[_Block] = []
    block: Optional[_Block] = None
    in_hunk = False
    new_line = 0
    for line in diff.split("\n"):
        if line.startswith("diff --"):
            block = _Block(line)
            blocks.append(block)
            in_hunk = False
            continue
        if block is None:
            continue
        if not in_hunk:
            match = _HUNK_RE.match(line)
            if match:
                in_hunk = True
                block.hunks = True
                new_line = int(match.group(1))
                # A zero-length new side is numbered by the line before it.
                if match.group(2) == "0":
                    new_line += 1
            elif line.startswith("--- "):
                block.minus = _header_path(line, "--- ")
            elif line.startswith("+++ "):
                block.plus = _header_path(line, "+++ ")
            elif line.startswith("new file mode"):
                block.new_file = True
            elif line.startswith("rename to ") or line.startswith("copy to "):
                block.renamed_to = line.split(" to ", 1)[1]
            continue
        if line.startswith("@@"):
            match = _HUNK_RE.match(line)
            if match:
                new_line = int(match.group(1)) + (1 if match.group(2) == "0" else 0)
            continue
        if line.startswith("+"):
            block.pairs.add((new_line, new_line))
            new_line += 1
        elif line.startswith("-"):
            block.pairs.add((new_line - 1, new_line))
        elif line.startswith(" "):
            new_line += 1
        # "\ No newline at end of file" and anything else moves nothing.

    for block in blocks:
        if not block.hunks or not block.pairs:
            continue  # binary, mode-only, or a pure rename
        path, reason = _block_path(block, wanted)
        if reason:
            for name in path.split("\0"):
                skipped.append({"path": name, "reason": reason})
            continue
        if block.new_file or block.minus == "/dev/null":
            skipped.append({"path": path, "reason": NEW_FILE})
            continue
        pairs.setdefault(path, set()).update(block.pairs)
    return {path: sorted(found) for path, found in pairs.items()}, skipped


def _block_path(block: _Block, wanted: "set[str]") -> Tuple[str, str]:
    """Which snapshot file a diff block is, or the reason it cannot be told.

    ``+++`` depends on configuration this tool does not set (``diff.noprefix``,
    ``diff.mnemonicPrefix``), so the prefix is not assumed. A path that is in
    the snapshot both with and without its first two characters -- ``x.py``
    and ``b/x.py`` -- is settled by the ``diff --git`` line, and left out by
    both names when that does not settle it. Two paths in a failure are
    joined with NUL.
    """
    if block.renamed_to:
        # Git never prefixes these.
        name = _unquote_c(block.renamed_to) if block.renamed_to.startswith('"') else block.renamed_to
        if name is None:
            return block.renamed_to, "quoted path"
        return (name, "") if name in wanted else (name, "path not in snapshot files")
    written = block.plus
    if not written:
        return "", "path not in snapshot files"
    quoted = written.startswith('"')
    raw = _unquote_c(written) if quoted else written
    if raw is None:
        return written, "quoted path"
    if raw == "/dev/null":
        minus = block.minus
        name = (_unquote_c(minus) if minus.startswith('"') else minus) or minus
        return (name[2:] if len(name) > 2 and name[1] == "/" else name), "deleted"
    stripped = raw[2:] if len(raw) > 2 and raw[1] == "/" else ""
    found = [name for name in (raw, stripped) if name and name in wanted]
    if not found:
        return raw, "path not in snapshot files"
    if len(found) == 1:
        return found[0], ""
    if block.gitline == "diff --git %s %s" % (written, written):
        return raw, ""
    inner = written[1:-1] if quoted else written
    for prefix in "aicw":
        other = "%s/%s" % (prefix, inner[2:])
        if quoted:
            other = '"%s"' % other
        if block.gitline == "diff --git %s %s" % (other, written):
            return stripped, ""
    return "%s\0%s" % (stripped, raw), "ambiguous path (%s or %s)" % (stripped, raw)


# --------------------------------------------------------------------------- the source


def _symbols(tree: ast.AST) -> List[Dict[str, Any]]:
    """Every function, method and class, with its span and qualified name."""
    found: List[Dict[str, Any]] = []

    def walk(node: ast.AST, names: List[str], parent_is_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                end = int(getattr(child, "end_lineno", None) or child.lineno)
                if isinstance(child, ast.ClassDef):
                    kind = "class"
                else:
                    kind = "method" if parent_is_class else "function"
                qualname = [*names, child.name]
                found.append({"symbol": ".".join(qualname), "kind": kind, "start": start, "end": end})
                walk(child, qualname, isinstance(child, ast.ClassDef))
            else:
                walk(child, names, parent_is_class)

    walk(tree, [], False)
    return found


def enclosing_nodes(source: str, pairs: Sequence[Tuple[int, int]], path: str = "") -> List[Dict[str, Any]]:
    """The symbols enclosing, or adjacent to, each changed pair.

    ``encloses``: the smallest symbol holding both ends of a pair. Failing
    that, the smallest symbol holding either end is ``adjacent`` -- a
    deletion sits on its edge and nothing that survived encloses it. A whole
    function deleted between A and B makes A and B adjacent, never enclosing.

    A symbol every line of which was added is not a candidate: it is already
    in the diff whole. The pair moves on to the symbol outside it, which is
    how a class gains a candidate when a method is added to it.

    Outer and inner symbols are both kept; which one a reviewer is shown is
    decided at adoption, against the budget. Raises ``SyntaxError`` or
    ``ValueError`` for a source ``ast`` will not parse.
    """
    symbols = _symbols(ast.parse(source))
    added = {lo for lo, hi in pairs if lo == hi}
    eligible = [s for s in symbols if not all(n in added for n in range(s["start"], s["end"] + 1))]

    def smallest(lo: int, hi: int) -> Optional[Dict[str, Any]]:
        hits = [s for s in eligible if s["start"] <= lo and hi <= s["end"]]
        return min(hits, key=lambda s: (s["end"] - s["start"], -s["start"])) if hits else None

    chosen: Dict[Tuple[int, int], Dict[str, Any]] = {}

    def keep(symbol: Dict[str, Any], relation: str) -> None:
        key = (symbol["start"], symbol["end"])
        if key not in chosen or relation == "encloses":
            chosen[key] = dict(symbol, relation=relation)

    for lo, hi in pairs:
        outer = smallest(lo, hi)
        if outer is not None:
            keep(outer, "encloses")
            continue
        for end in (lo, hi):
            edge = smallest(end, end)
            if edge is not None:
                keep(edge, "adjacent")

    lines = source.split("\n")
    candidates = []
    for key in sorted(chosen):
        symbol = chosen[key]
        text = "\n".join(lines[symbol["start"] - 1 : symbol["end"]])
        candidates.append(
            {
                "path": path,
                "symbol": symbol["symbol"],
                "kind": symbol["kind"],
                "relation": symbol["relation"],
                "start": symbol["start"],
                "end": symbol["end"],
                "chars": len(text),
                "text": text,
            }
        )
    return candidates


def extract(
    root: str,
    tree: str,
    diff: str,
    files: Sequence[str],
    working_tree_diff: bool,
    run: Runner = ws.git,
) -> Dict[str, Any]:
    """The candidates of one snapshot, read from ``tree`` and never from disk.

    ``working_tree_diff`` says the diff was taken against the working tree,
    after ``tree`` was written. A file edited in between would make the
    frozen symbol disagree with the frozen diff, so every such file is hashed
    as it is now and compared with its blob in ``tree``. ``hash-object`` is
    used rather than ``git diff <tree>`` because it does not go through the
    index: a file only the throwaway index of ``_write_tree`` knows about --
    an untracked file, one removed with ``rm --cached`` -- is not mistaken
    for a deleted one. A tree-to-tree round has nothing to check: its new
    side *is* ``tree``.
    """
    pairs, skipped = changed_lines(diff, files)
    for path in list(pairs):
        if not path.endswith(".py"):
            skipped.append({"path": path, "reason": "not python"})
            del pairs[path]
    candidates: List[Dict[str, Any]] = []
    blobs: Dict[str, str] = {}
    if pairs and not tree:
        skipped += [{"path": path, "reason": "no tree object (git write-tree failed)"} for path in pairs]
        pairs = {}
    if pairs:
        listed, unlisted = _list_tree(root, tree, sorted(pairs), run)
        by_path: Dict[str, List[Dict[str, Any]]] = {}
        for path in sorted(pairs):
            entry = listed.get(path)
            if entry is None:
                reason = "tree listing failed" if path in unlisted else "not in tree"
                skipped.append({"path": path, "reason": reason})
                continue
            mode, kind, blob, size = entry
            if mode == "120000":
                skipped.append({"path": path, "reason": "symlink"})
                continue
            if kind != "blob":
                skipped.append({"path": path, "reason": "submodule"})
                continue
            if size.isdigit() and int(size) > MAX_FILE_BYTES:
                skipped.append({"path": path, "reason": "file over {:,} bytes".format(MAX_FILE_BYTES)})
                continue
            shown, text, _ = run(["show", "%s:%s" % (tree, path)], root)
            if shown != 0:
                skipped.append({"path": path, "reason": "unreadable"})
                continue
            if _REPLACEMENT in text:
                skipped.append({"path": path, "reason": "not utf-8"})
                continue
            try:
                found = enclosing_nodes(text, pairs[path], path)
            except (SyntaxError, ValueError, RecursionError):
                skipped.append({"path": path, "reason": "syntax error"})
                continue
            if not found:
                skipped.append({"path": path, "reason": "no enclosing symbol beyond the diff"})
                continue
            by_path[path] = found
            blobs[path] = blob
        if working_tree_diff and by_path:
            for path in _drifted(root, blobs, run):
                del by_path[path]
                skipped.append({"path": path, "reason": DRIFTED})
        for path in sorted(by_path):
            candidates += by_path[path]
    candidates.sort(key=lambda c: (c["path"], c["start"], c["end"]))
    unique: Dict[str, Dict[str, str]] = {}
    for entry in skipped:
        unique.setdefault(entry["path"], entry)
    return {
        "mode": "enclosing",
        "tree": tree,
        "language": {"python": [".py"]},
        "candidates": candidates,
        "skipped": [unique[path] for path in sorted(unique)],
    }


def _list_tree(
    root: str, tree: str, paths: List[str], run: Runner
) -> Tuple[Dict[str, Tuple[str, str, str, str]], "set[str]"]:
    """``ls-tree`` entries of ``paths`` in ``tree``, and the paths it could not list.

    One call for all of them; when that fails -- on Windows a change touching
    enough files makes the command line too long to start -- one call per
    path, so a failed listing is not reported as a file missing from the tree.
    """

    def listing(names: List[str]) -> Optional[Dict[str, Tuple[str, str, str, str]]]:
        code, out, _ = run(["ls-tree", "-z", "-l", tree, "--", *names], root)
        if code != 0:
            return None
        found: Dict[str, Tuple[str, str, str, str]] = {}
        for record in out.split("\0"):
            if "\t" not in record:
                continue
            head, name = record.split("\t", 1)
            fields = head.split()
            if len(fields) >= 4:
                found[name] = (fields[0], fields[1], fields[2], fields[3])
        return found

    listed = listing(paths)
    if listed is not None:
        return listed, set()
    listed, unlisted = {}, set()
    for path in paths:
        found = listing([path])
        if found is None:
            unlisted.add(path)
        else:
            listed.update(found)
    return listed, unlisted


def _drifted(root: str, blobs: Dict[str, str], run: Runner) -> List[str]:
    """The paths whose working-tree content is no longer their blob in the tree.

    One call for all of them; when it fails -- git stops at the first file it
    cannot read -- one call per path, and a path that still fails counts as
    changed, because it certainly is not what was frozen.
    """
    paths = sorted(blobs)
    code, out, _ = run(["hash-object", "--", *paths], root)
    hashes = out.split()
    if code == 0 and len(hashes) == len(paths):
        return [path for path, found in zip(paths, hashes) if found != blobs[path]]
    drifted = []
    for path in paths:
        code, out, _ = run(["hash-object", "--", path], root)
        if code != 0 or out.strip() != blobs[path]:
            drifted.append(path)
    return drifted


# --------------------------------------------------------------------------- adoption


class Adoption:
    """What one round hands its reviewers, and what it leaves out by name."""

    def __init__(
        self,
        mode: str = "none",
        reason: str = "",
        budget: int = 0,
        adopted: Optional[List[Dict[str, Any]]] = None,
        trimmed: Optional[List[Dict[str, Any]]] = None,
        skipped: Optional[List[Dict[str, Any]]] = None,
        surrounding_chars: int = 0,
    ) -> None:
        self.mode = mode
        self.reason = reason
        self.budget = budget
        self.adopted = adopted or []
        self.trimmed = trimmed or []
        self.skipped = skipped or []
        #: The configured cap, kept so a "left out" line can say which limit
        #: the budget was: the cap itself, or what max_chars left of it.
        self.surrounding_chars = surrounding_chars

    @property
    def adopted_chars(self) -> int:
        return sum(int(c["chars"]) for c in self.adopted)

    @property
    def trimmed_chars(self) -> int:
        return sum(int(c["chars"]) for c in self.trimmed)

    @property
    def context_chars(self) -> int:
        """What the adopted context adds to the prompt, as the prompt carries it.

        The whole rendered block -- headings, fences, the notes and the
        left-out list -- and the blank line that sets it apart, since that is
        what the limits measure. Nothing when nothing was adopted: the block is
        then a note, like the others beside the diff.
        """
        if not self.adopted:
            return 0
        return len(_BLOCK_SEPARATOR) + len(render_surrounding(self))

    def record(self) -> Dict[str, Any]:
        """The run entry's form: everything but the source text."""
        return dict(self._fields(), context_chars=self.context_chars)

    def _fields(self) -> Dict[str, Any]:
        """``record`` without ``context_chars``, which is measured on the
        rendered block -- and rendering reads these, so they cannot include it."""
        return {
            "mode": self.mode,
            "reason": self.reason,
            "budget": self.budget,
            "adopted_chars": self.adopted_chars,
            "trimmed_chars": self.trimmed_chars,
            "adopted": [_without_text(c) for c in self.adopted],
            "trimmed": [_without_text(c) for c in self.trimmed],
            "skipped": [dict(entry) for entry in self.skipped],
        }

    def summary(self) -> Dict[str, Any]:
        """The round event's form: sizes and counts, no names."""
        return {
            "mode": self.mode,
            "adopted_chars": self.adopted_chars,
            "trimmed_chars": self.trimmed_chars,
            "adopted": len(self.adopted),
            "trimmed": len(self.trimmed),
        }


def _without_text(candidate: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("path", "symbol", "kind", "relation", "start", "end", "chars", "includes", "reason")
    return {key: candidate[key] for key in keys if key in candidate}


def adopt(
    workspace: ws.Workspace,
    meta: Dict[str, Any],
    mode: Any,
    surrounding_chars: Any,
    change_chars: int,
    max_chars: int,
    inline_chars: int,
    delivery: str,
) -> Adoption:
    """Choose, within the budget, which frozen candidates a round hands over.

    The budget is ``surrounding_chars``, less whatever it would take past
    ``max_chars`` or ``inline_chars`` with the diff beside it -- so the change
    and its context together never exceed either limit, and adding context
    never refuses a round or turns an inlined diff into a file. It is spent on
    the block as rendered (``Adoption.context_chars``), not on the source
    alone, because the headings and fences reach the prompt too.

    Enclosing symbols first, then adjacent ones; smallest first within each,
    which gives the most hunks their symbol under a fixed budget. An outer
    symbol costs only what it adds to the inner ones already taken, and
    taking it folds them into its ``includes``; a symbol inside one already
    taken is folded the same way. So no text is shown twice.
    """
    if surrounding_mode(mode) != "enclosing":
        return Adoption()
    cap = surrounding_chars
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        cap = _default_surrounding_chars()
    frozen = ws.read_json(workspace.surrounding_path, {}) or {}
    marked = meta.get("surrounding") if isinstance(meta.get("surrounding"), dict) else {}
    if (
        str(marked.get("mode") or "") != "enclosing"
        or not isinstance(frozen, dict)
        or not frozen.get("sha256")
        or frozen.get("sha256") != meta.get("sha256")
    ):
        return Adoption("enclosing", NOT_FROZEN, surrounding_chars=cap)
    skipped = [entry for entry in frozen.get("skipped") or [] if isinstance(entry, dict)]
    candidates = [dict(c) for c in frozen.get("candidates") or [] if isinstance(c, dict) and "chars" in c]
    if not candidates:
        return Adoption("enclosing", NO_CANDIDATES, skipped=skipped, surrounding_chars=cap)
    ordered = sorted(
        candidates,
        key=lambda c: (_RELATION_RANK.get(str(c.get("relation")), 1), int(c["chars"]), c["path"], c["start"]),
    )
    if delivery == "file":
        trimmed = [dict(c, reason="file delivery") for c in ordered]
        return Adoption("enclosing", FILE_DELIVERY, 0, [], trimmed, skipped, cap)
    budget = cap
    if max_chars > 0:
        budget = min(budget, max_chars - change_chars)
    if inline_chars > 0:
        budget = min(budget, inline_chars - change_chars)
    if budget <= 0:
        trimmed = [dict(c, reason="budget") for c in ordered]
        return Adoption("enclosing", NO_BUDGET, 0, [], trimmed, skipped, cap)

    chosen: List[Dict[str, Any]] = []
    adoption = Adoption("enclosing", "", budget, [], _arrange(ordered, [])[1], skipped, cap)
    for candidate in ordered:
        # Already shown, inside a symbol taken before it.
        if any(_contains(c, candidate) for c in chosen):
            continue
        adopted, trimmed = _arrange(ordered, [*chosen, candidate])
        trial = Adoption("enclosing", "", budget, adopted, trimmed, skipped, cap)
        if trial.context_chars > budget:
            continue
        chosen.append(candidate)
        adoption = trial
    if not adoption.adopted:
        adoption.reason = NO_FIT
    return adoption


def _arrange(
    ordered: Sequence[Dict[str, Any]], chosen: Sequence[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(adopted, trimmed)`` for the candidates chosen so far.

    A chosen symbol inside another chosen one is not adopted on its own, and
    every candidate inside an adopted symbol -- chosen or left out -- is named
    in its ``includes`` instead of being shown or listed again.
    """
    outer = [c for c in chosen if not any(_contains(o, c) for o in chosen)]
    adopted = []
    for symbol in outer:
        inner = sorted((c for c in ordered if _contains(symbol, c)), key=lambda c: (c["start"], c["end"]))
        entry = {key: value for key, value in symbol.items() if key != "includes"}
        names = list(dict.fromkeys(c["symbol"] for c in inner if c["symbol"] != symbol["symbol"]))
        if names:
            entry["includes"] = names
        adopted.append(entry)
    adopted.sort(key=lambda c: (c["path"], c["start"], c["end"]))
    taken = {id(c) for c in chosen}
    trimmed = [
        dict(c, reason="budget")
        for c in ordered
        if id(c) not in taken and not any(_contains(o, c) for o in outer)
    ]
    return adopted, trimmed


def _contains(outer: Dict[str, Any], inner: Dict[str, Any]) -> bool:
    return (
        outer is not inner
        and outer["path"] == inner["path"]
        and outer["start"] <= inner["start"]
        and inner["end"] <= outer["end"]
    )


def _default_surrounding_chars() -> int:
    from .config import default_config

    return int(default_config()["review"]["context"]["surrounding_chars"])


# --------------------------------------------------------------------------- words


def describe(candidate: Dict[str, Any]) -> str:
    """``path:start-end symbol (kind, N chars[; includes ...][; adjacent ...])``."""
    detail = "%s, %s chars" % (candidate.get("kind"), "{:,}".format(int(candidate.get("chars") or 0)))
    if candidate.get("includes"):
        detail += "; includes %s" % ", ".join(candidate["includes"])
    if candidate.get("relation") == "adjacent":
        detail += "; adjacent to a deletion, not enclosing it"
    return "%s:%s-%s %s (%s)" % (
        candidate.get("path"),
        candidate.get("start"),
        candidate.get("end"),
        candidate.get("symbol"),
        detail,
    )


def trim_reasons(record: Dict[str, Any]) -> str:
    """The reasons symbols were left out, each once, in the order they occur."""
    reasons = [str(c.get("reason") or "budget") for c in record.get("trimmed") or [] if isinstance(c, dict)]
    return ", ".join(dict.fromkeys(reasons))


def summary(record: Dict[str, Any]) -> str:
    """One record in a line: what was adopted, and how much was left out why."""
    adopted = record.get("adopted") or []
    trimmed = record.get("trimmed") or []
    if adopted:
        line = "%d symbol(s), %s chars adopted" % (
            len(adopted),
            "{:,}".format(int(record.get("adopted_chars") or 0)),
        )
        if trimmed:
            line += "; %d left out (%s chars, %s)" % (
                len(trimmed),
                "{:,}".format(int(record.get("trimmed_chars") or 0)),
                trim_reasons(record),
            )
        return line
    line = "nothing adopted (%s)" % (record.get("reason") or "no budget")
    if trimmed:
        line += "; %d left out (%s)" % (len(trimmed), trim_reasons(record))
    return line


def brief(record: Dict[str, Any]) -> str:
    """The shorter form, for one reviewer among several."""
    adopted = record.get("adopted") or []
    trimmed = record.get("trimmed") or []
    if not adopted:
        return "nothing adopted" + (", %d left out" % len(trimmed) if trimmed else "")
    line = "%d symbol(s), %s chars adopted" % (
        len(adopted),
        "{:,}".format(int(record.get("adopted_chars") or 0)),
    )
    return line + (", %d left out" % len(trimmed) if trimmed else "")


def status_line(record: Dict[str, Any]) -> str:
    """``review status``'s form, which leads with the size."""
    line = "enclosing, %s chars adopted" % "{:,}".format(int(record.get("adopted_chars") or 0))
    trimmed = record.get("trimmed") or []
    if trimmed:
        line += ", %d symbol(s) left out (%s)" % (len(trimmed), trim_reasons(record))
    if not record.get("adopted") and record.get("reason"):
        line += " -- %s" % record["reason"]
    return line


def left_out_heading(record: Dict[str, Any], reason: str) -> str:
    """Why a group of symbols was left out, naming the limit that did it."""
    if reason == "file delivery":
        return "file delivery"
    if record.get("reason") == NO_BUDGET:
        return NO_BUDGET
    budget = int(record.get("budget") or 0)
    cap = int(record.get("surrounding_chars") or 0)
    if cap and budget < cap:
        return "over the {:,} chars review.context.max_chars / inline_chars left".format(budget)
    return "over review.context.surrounding_chars, {:,}".format(budget)


def not_extracted(skipped: Sequence[Dict[str, Any]]) -> str:
    """``Not extracted: N file(s) (reason: n, ...).``, or "" for none.

    Counted, not listed: a repository in another language would otherwise get
    a line per file on every prompt. The reasons a fresh snapshot cures are
    the exception and carry their paths, because they are the ones to act on.
    """
    if not skipped:
        return ""
    groups: Dict[str, List[str]] = {}
    for entry in skipped:
        label = str(entry.get("reason") or "?").split(" (", 1)[0].split(" -- ", 1)[0]
        groups.setdefault(label, []).append(str(entry.get("path")))
    parts = []
    for label, paths in groups.items():
        part = "%s: %d" % (label, len(paths))
        if label in _NAMED:
            part += " -- %s" % ", ".join(paths)
        parts.append(part)
    return "Not extracted: %d file(s) (%s)." % (len(skipped), ", ".join(parts))


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def render_surrounding(adoption: Optional[Adoption]) -> str:
    """The prompt block, or "" when the setting is off -- byte for byte today's prompt."""
    if adoption is None or adoption.mode != "enclosing":
        return ""
    shown = adoption.adopted + adoption.trimmed
    adjacent = any(c.get("relation") == "adjacent" for c in shown)
    record = dict(adoption._fields(), surrounding_chars=adoption.surrounding_chars)
    lines: List[str] = []
    if adoption.adopted:
        lines += [
            "Surrounding context (review.context.surrounding: enclosing). The function, method or",
            "class enclosing each hunk, read from the tree the diff was taken from. It is shown so you",
            "open fewer files; it is not part of the change. Review only the diff above.",
        ]
        if adjacent:
            lines.append(ADJACENT_NOTE)
        for candidate in adoption.adopted:
            text = str(candidate.get("text") or "")
            fence = _fence(text)
            lines += ["", "### %s" % describe(candidate), "%spython" % fence, text, fence]
        if adoption.trimmed or adoption.skipped:
            lines.append("")
    else:
        lines.append(
            "Surrounding context (review.context.surrounding: enclosing): nothing was added -- %s."
            % adoption.reason
        )
        if adjacent:
            lines.append(ADJACENT_NOTE)
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in adoption.trimmed[:LEFT_OUT_SHOWN]:
        groups.setdefault(str(candidate.get("reason") or "budget"), []).append(candidate)
    for reason, members in groups.items():
        lines.append(
            "Left out (%s): read these yourself if a hunk needs them." % left_out_heading(record, reason)
        )
        lines += ["- %s" % describe(candidate) for candidate in members]
    unnamed = len(adoption.trimmed) - LEFT_OUT_SHOWN
    if unnamed > 0:
        lines.append("- and %d more, named in `review status` and consolidated.md" % unnamed)
    note = not_extracted(adoption.skipped)
    if note:
        lines.append(note)
    return "\n".join(lines)
