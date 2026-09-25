"""Surrounding context: the Python symbol enclosing each hunk, handed to reviewers.

Three properties carry the whole feature, and most tests here are about one
of them. What a reviewer is shown comes from the tree the diff was taken from,
never from whatever the working tree says later. Whatever is left out -- for
the budget, for a file delivery, for a reason extraction could not be exact --
is left out by name, in the prompt and in every report. And with the setting
off nothing changes at all: the prompt is the bytes it always was.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from helpers import IsolatedCase, has_git

from orchestrator import cli
from orchestrator import config as config_mod
from orchestrator import context as context_mod
from orchestrator import optimization as opt_mod
from orchestrator import review as review_mod
from orchestrator import workspace as ws


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def reviewer(reviewer_id, provider="mock", role="general"):
    return config_mod.make_reviewer(reviewer_id, provider, "small", role)


def block(path, body, start=1, gitline=None, plus=None, extra=""):
    """One diff block for ``path``, with ``body`` as its only hunk."""
    return "%s\n%sindex 1111111..2222222 100644\n--- a/%s\n+++ %s\n@@ -%d,9 +%d,9 @@\n%s" % (
        gitline or "diff --git a/%s b/%s" % (path, path),
        extra,
        path,
        plus or "b/%s" % path,
        start,
        start,
        body,
    )


EDIT = " def a():\n-    return 1\n+    return 2\n \n"


# --------------------------------------------------------------------------- the diff


class TestChangedLines(unittest.TestCase):
    def test_a_prefixed_path_is_found(self):
        pairs, skipped = context_mod.changed_lines(block("x.py", EDIT), ["x.py"])
        self.assertEqual(pairs, {"x.py": [(1, 2), (2, 2)]})
        self.assertEqual(skipped, [])

    def test_an_unprefixed_path_is_found(self):
        diff = block("x.py", EDIT, gitline="diff --git x.py x.py", plus="x.py")
        self.assertEqual(list(context_mod.changed_lines(diff, ["x.py"])[0]), ["x.py"])

    def test_a_mnemonic_prefix_is_found(self):
        diff = block("x.py", EDIT, gitline="diff --git i/x.py w/x.py", plus="w/x.py")
        self.assertEqual(list(context_mod.changed_lines(diff, ["x.py"])[0]), ["x.py"])

    def test_a_quoted_path_is_unquoted(self):
        name = "\u65e5.py"
        quoted = '"b/\\346\\227\\245.py"'
        gitline = 'diff --git "a/\\346\\227\\245.py" %s' % quoted
        diff = block(name, EDIT, gitline=gitline, plus=quoted)
        self.assertEqual(list(context_mod.changed_lines(diff, [name])[0]), [name])

    def test_a_deleted_file_is_skipped(self):
        diff = (
            "diff --git a/x.py b/x.py\ndeleted file mode 100644\n--- a/x.py\n+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n-a\n-b\n"
        )
        pairs, skipped = context_mod.changed_lines(diff, ["x.py"])
        self.assertEqual(pairs, {})
        self.assertEqual(skipped, [{"path": "x.py", "reason": "deleted"}])

    def test_a_path_the_snapshot_does_not_list_is_skipped(self):
        pairs, skipped = context_mod.changed_lines(block("x.py", EDIT), ["y.py"])
        self.assertEqual(pairs, {})
        self.assertEqual(skipped[0]["reason"], "path not in snapshot files")

    def test_a_plus_plus_plus_line_inside_a_hunk_is_content(self):
        body = " def a():\n+++ b/y.py\n     pass\n"
        pairs, _ = context_mod.changed_lines(block("x.py", body), ["x.py", "y.py"])
        self.assertEqual(pairs, {"x.py": [(2, 2)]})

    def test_no_newline_marker_moves_nothing(self):
        marker = "\\ No newline at end of file\n"
        body = " def a():\n-    return 1\n" + marker + "+    return 2\n" + marker
        pairs, _ = context_mod.changed_lines(block("x.py", body), ["x.py"])
        self.assertEqual(pairs["x.py"], [(1, 2), (2, 2)])

    def test_a_deletion_is_the_pair_it_sat_between_and_moves_nothing(self):
        body = " a = 1\n-b = 2\n-c = 3\n d = 4\n"
        pairs, _ = context_mod.changed_lines(block("x.py", body, start=10), ["x.py"])
        self.assertEqual(pairs["x.py"], [(10, 11)])

    def test_a_deletion_then_an_addition(self):
        body = " a = 1\n-b = 2\n+b = 3\n+c = 4\n d = 5\n"
        pairs, _ = context_mod.changed_lines(block("x.py", body), ["x.py"])
        self.assertEqual(pairs["x.py"], [(1, 2), (2, 2), (3, 3)])

    def test_a_binary_block_is_ignored(self):
        diff = "diff --git a/x.png b/x.png\nindex 1..2 100644\nBinary files a/x.png and b/x.png differ\n"
        self.assertEqual(context_mod.changed_lines(diff, ["x.png"]), ({}, []))

    def test_a_new_file_is_skipped_because_the_diff_is_all_of_it(self):
        diff = (
            "diff --git a/new.py b/new.py\nnew file mode 100644\nindex 0000000..1111111\n"
            "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+def f():\n+    pass\n"
        )
        pairs, skipped = context_mod.changed_lines(diff, ["new.py"])
        self.assertEqual(pairs, {})
        self.assertEqual(skipped, [{"path": "new.py", "reason": context_mod.NEW_FILE}])

    def test_a_dev_null_old_side_alone_is_a_new_file_too(self):
        diff = "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
        self.assertEqual(context_mod.changed_lines(diff, ["new.py"])[1][0]["reason"], context_mod.NEW_FILE)

    # -- x.py and b/x.py, both in the snapshot

    BOTH = ("x.py", "b/x.py")

    def test_both_paths_prefixed_x_py_is_x_py(self):
        diff = block("x.py", EDIT)
        self.assertEqual(list(context_mod.changed_lines(diff, self.BOTH)[0]), ["x.py"])

    def test_both_paths_prefixed_b_x_py_is_b_x_py(self):
        diff = block("b/x.py", EDIT)
        self.assertEqual(list(context_mod.changed_lines(diff, self.BOTH)[0]), ["b/x.py"])

    def test_both_paths_unprefixed_b_x_py_is_b_x_py(self):
        diff = block("b/x.py", EDIT, gitline="diff --git b/x.py b/x.py", plus="b/x.py")
        self.assertEqual(list(context_mod.changed_lines(diff, self.BOTH)[0]), ["b/x.py"])

    def test_a_rename_target_wins(self):
        diff = block(
            "old.py",
            EDIT,
            gitline="diff --git a/old.py b/x.py",
            plus="b/x.py",
            extra="similarity index 90%\nrename from old.py\nrename to x.py\n",
        )
        self.assertEqual(list(context_mod.changed_lines(diff, self.BOTH)[0]), ["x.py"])

    def test_both_paths_and_nothing_to_settle_it_skips_both_by_name(self):
        diff = block("x.py", EDIT, gitline="diff --git something else", plus="b/x.py")
        pairs, skipped = context_mod.changed_lines(diff, self.BOTH)
        self.assertEqual(pairs, {})
        reason = "ambiguous path (x.py or b/x.py)"
        self.assertEqual(
            skipped,
            [{"path": "x.py", "reason": reason}, {"path": "b/x.py", "reason": reason}],
        )


class TestUnquote(unittest.TestCase):
    def test_escapes(self):
        self.assertEqual(context_mod._unquote_c('"a\\tb\\\\c\\"d"'), 'a\tb\\c"d')
        self.assertEqual(context_mod._unquote_c('"\\346\\227\\245"'), "\u65e5")

    def test_not_utf8_is_refused(self):
        self.assertIsNone(context_mod._unquote_c('"\\377"'))

    def test_unquoted_is_refused(self):
        self.assertIsNone(context_mod._unquote_c("plain"))


# --------------------------------------------------------------------------- the source

SOURCE = """import os


def top():
    return 1


class Thing:
    LIMIT = 3

    @property
    def size(self):
        return self.LIMIT

    async def fetch(self):
        def inner():
            return 2
        return inner()
"""


def symbols(candidates):
    return [(c["symbol"], c["kind"], c["relation"], c["start"], c["end"]) for c in candidates]


def found_in(source, pairs):
    return symbols(context_mod.enclosing_nodes(source, pairs))


class TestEnclosingNodes(unittest.TestCase):
    def test_a_function(self):
        self.assertEqual(found_in(SOURCE, [(5, 5)]), [("top", "function", "encloses", 4, 5)])

    def test_a_method_starts_at_its_decorator(self):
        self.assertEqual(found_in(SOURCE, [(13, 13)]), [("Thing.size", "method", "encloses", 11, 13)])

    def test_an_async_method(self):
        self.assertEqual(found_in(SOURCE, [(18, 18)])[0][:2], ("Thing.fetch", "method"))

    def test_the_smallest_enclosing_symbol_wins(self):
        inner = ("Thing.fetch.inner", "function", "encloses", 16, 17)
        self.assertEqual(found_in(SOURCE, [(17, 17)]), [inner])

    def test_a_class_attribute_is_enclosed_by_the_class(self):
        self.assertEqual(found_in(SOURCE, [(9, 9)]), [("Thing", "class", "encloses", 8, 18)])

    def test_a_class_and_a_method_in_it_are_both_kept(self):
        found = found_in(SOURCE, [(9, 9), (13, 13)])
        self.assertEqual([entry[0] for entry in found], ["Thing", "Thing.size"])

    def test_module_level_has_nothing(self):
        self.assertEqual(found_in(SOURCE, [(1, 1)]), [])

    def test_a_syntax_error_raises(self):
        with self.assertRaises(SyntaxError):
            context_mod.enclosing_nodes("def (:\n", [(1, 1)])

    def test_the_text_is_the_symbols_lines(self):
        (found,) = context_mod.enclosing_nodes(SOURCE, [(5, 5)], "x.py")
        self.assertEqual(found["path"], "x.py")
        self.assertEqual(found["text"], "def top():\n    return 1")
        self.assertEqual(found["chars"], len(found["text"]))

    # -- deletions

    A_B = "def a():\n    x = 1\n    return x\n\n\ndef b():\n    return 2\n"

    def test_deleting_a_first_statement_is_enclosed(self):
        self.assertEqual(found_in(self.A_B, [(1, 2)]), [("a", "function", "encloses", 1, 3)])

    def test_deleting_a_whole_function_between_two_makes_both_adjacent(self):
        source = "def a():\n    return 1\ndef b():\n    return 2\n"
        self.assertEqual(
            found_in(source, [(2, 3)]),
            [("a", "function", "adjacent", 1, 2), ("b", "function", "adjacent", 3, 4)],
        )

    def test_deleting_a_last_statement_is_adjacent(self):
        self.assertEqual(found_in(self.A_B, [(3, 4)]), [("a", "function", "adjacent", 1, 3)])

    def test_deleting_the_function_at_the_end_of_the_file(self):
        source = "def a():\n    return 1\n"
        self.assertEqual(found_in(source, [(2, 3)]), [("a", "function", "adjacent", 1, 2)])

    def test_deleting_the_function_at_the_top_of_the_file(self):
        source = "def b():\n    return 2\n"
        self.assertEqual(found_in(source, [(0, 1)]), [("b", "function", "adjacent", 1, 2)])

    def test_enclosing_wins_over_adjacent_for_the_same_symbol(self):
        self.assertEqual(found_in(self.A_B, [(3, 4), (2, 2)]), [("a", "function", "encloses", 1, 3)])

    # -- symbols the diff already shows whole

    def test_an_added_function_is_not_a_candidate(self):
        self.assertEqual(found_in(self.A_B, [(6, 6), (7, 7)]), [])

    def test_an_added_method_makes_its_class_the_candidate(self):
        source = "class K:\n    def old(self):\n        return 1\n\n    def new(self):\n        return 2\n"
        self.assertEqual(found_in(source, [(5, 5), (6, 6)]), [("K", "class", "encloses", 1, 6)])


# --------------------------------------------------------------------------- extraction


class FakeGit:
    """``ws.git`` for ``extract``: a tree listing, blob bodies, working-tree hashes."""

    def __init__(self, entries, sources, hashes=None, hash_fails=(), list_fails=()):
        self.entries = entries
        self.sources = sources
        self.hashes = hashes or {}
        self.hash_fails = set(hash_fails)
        #: Paths whose listing fails; "*" fails any call naming more than one path.
        self.list_fails = set(list_fails)
        self.calls = []

    def __call__(self, args, cwd, **_):
        self.calls.append(list(args))
        if args[0] == "ls-tree":
            wanted = args[5:]
            if ("*" in self.list_fails and len(wanted) > 1) or any(p in self.list_fails for p in wanted):
                return 127, "", "The filename or extension is too long"
            out = "".join(
                "%s %s %s %7s\t%s\0" % (mode, kind, blob, size, path)
                for path, (mode, kind, blob, size) in self.entries.items()
                if path in wanted
            )
            return 0, out, ""
        if args[0] == "show":
            path = args[1].split(":", 1)[1]
            if path in self.sources:
                return 0, self.sources[path], ""
            return 128, "", "fatal: bad object"
        if args[0] == "hash-object":
            paths = args[2:]
            if any(path in self.hash_fails for path in paths):
                return 128, "", "fatal: could not open"
            return 0, "".join("%s\n" % self.hashes.get(p, self.entries[p][2]) for p in paths), ""
        raise AssertionError("unexpected git call %r" % (args,))


TOP_EDIT = " def top():\n-    return 0\n+    return 1\n"


def blob(letter, size="100"):
    return ("100644", "blob", letter * 40, size)


class TestExtract(unittest.TestCase):
    def extract(self, git, diff=None, files=("x.py",), tree="t" * 40, working=False):
        diff = diff or block("x.py", TOP_EDIT, start=4)
        return context_mod.extract("/repo", tree, diff, list(files), working, git)

    def reasons(self, frozen):
        return {entry["path"]: entry["reason"] for entry in frozen["skipped"]}

    def two_files(self, **fake):
        git = FakeGit({"x.py": blob("a"), "y.py": blob("f")}, {"x.py": SOURCE, "y.py": SOURCE}, **fake)
        diff = block("x.py", TOP_EDIT, start=4) + block("y.py", TOP_EDIT, start=4)
        return git, self.extract(git, diff, ["x.py", "y.py"], working=True)

    def test_a_python_file_gives_its_candidate(self):
        frozen = self.extract(FakeGit({"x.py": blob("a")}, {"x.py": SOURCE}))
        self.assertEqual(symbols(frozen["candidates"]), [("top", "function", "encloses", 4, 5)])
        self.assertEqual(frozen["candidates"][0]["chars"], len(frozen["candidates"][0]["text"]))
        self.assertEqual(frozen["skipped"], [])
        self.assertEqual(frozen["language"], {"python": [".py"]})

    def test_another_language_is_not_extracted(self):
        git = FakeGit({}, {})
        frozen = self.extract(git, block("web/app.js", EDIT), ["web/app.js"])
        self.assertEqual(self.reasons(frozen), {"web/app.js": "not python"})
        self.assertEqual(git.calls, [])

    def test_the_reasons_a_file_cannot_be_read(self):
        entries = {
            "link.py": ("120000", "blob", "c" * 40, "12"),
            "sub.py": ("160000", "commit", "d" * 40, "-"),
            "big.py": blob("e", "512001"),
            "lost.py": blob("a"),
            "latin.py": blob("b"),
            "broken.py": blob("c"),
            "flat.py": blob("d"),
        }
        sources = {"latin.py": "x = '\ufffd'\n", "broken.py": "def (:\n", "flat.py": "x = 1\ny = 2\n"}
        paths = ["gone.py", *entries]
        diff = "".join(block(path, EDIT) for path in paths)
        frozen = self.extract(FakeGit(entries, sources), diff, paths)
        self.assertEqual(
            self.reasons(frozen),
            {
                "gone.py": "not in tree",
                "link.py": "symlink",
                "sub.py": "submodule",
                "big.py": "file over 512,000 bytes",
                "lost.py": "unreadable",
                "latin.py": "not utf-8",
                "broken.py": "syntax error",
                "flat.py": "no enclosing symbol beyond the diff",
            },
        )
        self.assertEqual(frozen["candidates"], [])
        self.assertEqual([entry["path"] for entry in frozen["skipped"]], sorted(paths))

    def test_a_failed_tree_listing_is_retried_per_path(self):
        """A command line too long for one call is not a file missing from the tree."""
        git, frozen = self.two_files(list_fails=["*"])
        self.assertEqual(frozen["skipped"], [])
        self.assertEqual([c["path"] for c in frozen["candidates"]], ["x.py", "y.py"])
        listed = [call[5:] for call in git.calls if call[0] == "ls-tree"]
        self.assertEqual(listed, [["x.py", "y.py"], ["x.py"], ["y.py"]])

    def test_a_path_that_still_cannot_be_listed_says_so(self):
        _, frozen = self.two_files(list_fails=["*", "x.py"])
        self.assertEqual(self.reasons(frozen), {"x.py": "tree listing failed"})
        self.assertEqual([c["path"] for c in frozen["candidates"]], ["y.py"])

    def test_no_tree_skips_every_python_file(self):
        git = FakeGit({}, {})
        frozen = self.extract(git, tree="")
        self.assertEqual(self.reasons(frozen), {"x.py": "no tree object (git write-tree failed)"})
        self.assertEqual(git.calls, [])

    def test_a_file_edited_after_the_tree_was_written_is_dropped(self):
        git, frozen = self.two_files(hashes={"x.py": "0" * 40})
        self.assertEqual(self.reasons(frozen), {"x.py": context_mod.DRIFTED})
        self.assertEqual([c["path"] for c in frozen["candidates"]], ["y.py"])
        hashed = [call for call in git.calls if call[0] == "hash-object"]
        self.assertEqual(hashed, [["hash-object", "--", "x.py", "y.py"]])

    def test_a_failed_hash_is_retried_per_path_and_only_the_failure_drops(self):
        git, frozen = self.two_files(hash_fails=["x.py"])
        self.assertEqual(self.reasons(frozen), {"x.py": context_mod.DRIFTED})
        self.assertEqual([c["path"] for c in frozen["candidates"]], ["y.py"])
        hashed = [call for call in git.calls if call[0] == "hash-object"]
        self.assertEqual(hashed[1:], [["hash-object", "--", "x.py"], ["hash-object", "--", "y.py"]])

    def test_a_tree_to_tree_round_hashes_nothing(self):
        git = FakeGit({"x.py": blob("a")}, {"x.py": SOURCE}, hashes={"x.py": "0" * 40})
        frozen = self.extract(git, working=False)
        self.assertEqual(len(frozen["candidates"]), 1)
        self.assertFalse(any(call[0] == "hash-object" for call in git.calls))

    def test_candidates_are_in_path_and_line_order(self):
        git = FakeGit({"b.py": blob("b"), "a.py": blob("a")}, {"a.py": SOURCE, "b.py": SOURCE})
        size = "-        return 0\n+        return self.LIMIT\n"
        diff = block("b.py", TOP_EDIT, start=4) + block("b.py", size, start=13)
        diff += block("a.py", TOP_EDIT, start=4)
        frozen = self.extract(git, diff, ["a.py", "b.py"])
        self.assertEqual(
            [(c["path"], c["start"]) for c in frozen["candidates"]],
            [("a.py", 4), ("b.py", 4), ("b.py", 11)],
        )


# --------------------------------------------------------------------------- adoption


def candidate(path, symbol, start, end, chars, relation="encloses", kind="function"):
    return {
        "path": path,
        "symbol": symbol,
        "kind": kind,
        "relation": relation,
        "start": start,
        "end": end,
        "chars": chars,
        "text": "x" * chars,
    }


class AdoptCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = ws.Workspace(self.project, workflow="test").ensure()
        self.meta = {"sha256": "abc", "surrounding": {"mode": "enclosing"}}

    def freeze(self, candidates, skipped=(), sha="abc"):
        frozen = {"sha256": sha, "mode": "enclosing", "candidates": list(candidates)}
        frozen["skipped"] = list(skipped)
        ws.write_json(self.workspace.surrounding_path, frozen)

    def adopt(
        self, cap=1000, change=0, max_chars=0, inline=0, delivery="inline", mode="enclosing", meta=None
    ):
        meta = self.meta if meta is None else meta
        return context_mod.adopt(self.workspace, meta, mode, cap, change, max_chars, inline, delivery)


class TestAdopt(AdoptCase):
    def test_enclosing_first_then_smallest_first(self):
        self.freeze(
            [
                candidate("a.py", "big", 1, 10, 3000),
                candidate("a.py", "small", 20, 22, 1000),
                candidate("a.py", "edge", 30, 31, 500, relation="adjacent"),
            ]
        )
        adoption = self.adopt(cap=4800)
        self.assertEqual([c["symbol"] for c in adoption.adopted], ["big", "small"])
        self.assertEqual([(c["symbol"], c["reason"]) for c in adoption.trimmed], [("edge", "budget")])
        self.assertEqual((adoption.adopted_chars, adoption.trimmed_chars, adoption.budget), (4000, 500, 4800))

    def test_the_budget_is_spent_on_the_block_as_rendered(self):
        self.freeze([candidate("a.py", "f", 1, 2, 300), candidate("a.py", "g", 5, 6, 300)])
        both = self.adopt(cap=100_000)
        self.assertEqual(len(both.adopted), 2)
        self.assertEqual(both.context_chars, len(context_mod.render_surrounding(both)) + 2)
        self.assertGreater(both.context_chars, both.adopted_chars)
        self.assertEqual(len(self.adopt(cap=both.context_chars).adopted), 2)
        # The source alone would fit; its headings and fences would not.
        one = self.adopt(cap=both.context_chars - 1)
        self.assertEqual([c["symbol"] for c in one.adopted], ["f"])
        self.assertLessEqual(one.context_chars, one.budget)
        self.assertEqual(self.adopt(cap=300).adopted, [])

    def test_a_long_left_out_list_does_not_crowd_out_what_fits(self):
        """Eighty names would cost more than the budget; twenty leave room for the symbol."""
        small = candidate("a.py", "f", 1, 2, 100)
        rest = [candidate("b.py", "g%d" % n, n * 10, n * 10 + 1, 5000) for n in range(1, 81)]
        self.freeze([small, *rest])
        adoption = self.adopt(cap=3000)
        self.assertEqual([c["symbol"] for c in adoption.adopted], ["f"])
        self.assertEqual(len(adoption.trimmed), 80)
        self.assertLessEqual(adoption.context_chars, adoption.budget)
        block = context_mod.render_surrounding(adoption)
        named = [line for line in block.splitlines() if line.startswith("- b.py:")]
        self.assertEqual(len(named), context_mod.LEFT_OUT_SHOWN)
        self.assertIn("- and %d more" % (80 - context_mod.LEFT_OUT_SHOWN), block)
        # The reports still name every one.
        self.assertEqual(len(adoption.record()["trimmed"]), 80)

    def test_nothing_that_fits_says_why(self):
        self.freeze([candidate("a.py", "f", 1, 2, 5000)])
        adoption = self.adopt(cap=1000)
        self.assertEqual(adoption.adopted, [])
        self.assertEqual(adoption.reason, context_mod.NO_FIT)
        block = context_mod.render_surrounding(adoption)
        self.assertIn("nothing was added -- %s." % context_mod.NO_FIT, block)

    def test_nothing_adopted_adds_nothing_to_the_budget(self):
        self.freeze([candidate("a.py", "f", 1, 2, 300)])
        self.assertEqual(self.adopt(cap=10).context_chars, 0)
        self.assertEqual(self.adopt(delivery="file").context_chars, 0)

    def test_the_budget_is_capped_by_what_max_chars_leaves(self):
        self.freeze([candidate("a.py", "f", 1, 2, 150)])
        adoption = self.adopt(cap=5000, change=1000, max_chars=2000)
        self.assertEqual(adoption.budget, 1000)
        self.assertEqual(len(adoption.adopted), 1)
        self.assertLessEqual(1000 + adoption.context_chars, 2000)

    def test_the_budget_is_capped_by_what_inline_chars_leaves(self):
        self.freeze([candidate("a.py", "f", 1, 2, 150)])
        adoption = self.adopt(cap=1000, change=1000, max_chars=5000, inline=1100)
        self.assertEqual(adoption.budget, 100)
        self.assertEqual(adoption.adopted, [])
        self.assertEqual(adoption.trimmed[0]["reason"], "budget")

    def test_no_budget_left_trims_everything_and_says_why(self):
        self.freeze([candidate("a.py", "f", 1, 2, 10), candidate("a.py", "g", 5, 6, 20)])
        adoption = self.adopt(cap=1000, change=1000, max_chars=1000)
        self.assertEqual(adoption.adopted, [])
        self.assertEqual([c["reason"] for c in adoption.trimmed], ["budget", "budget"])
        self.assertEqual(adoption.reason, context_mod.NO_BUDGET)

    def test_a_file_delivery_trims_everything_by_name(self):
        self.freeze([candidate("a.py", "f", 1, 2, 10)])
        adoption = self.adopt(delivery="file")
        self.assertEqual(adoption.adopted, [])
        self.assertEqual([(c["symbol"], c["reason"]) for c in adoption.trimmed], [("f", "file delivery")])
        self.assertEqual(adoption.reason, context_mod.FILE_DELIVERY)

    def test_nothing_frozen_is_not_frozen(self):
        adoption = self.adopt()
        self.assertEqual((adoption.mode, adoption.reason), ("enclosing", context_mod.NOT_FROZEN))

    def test_a_frozen_file_for_another_snapshot_is_not_frozen(self):
        self.freeze([candidate("a.py", "f", 1, 2, 10)], sha="other")
        self.assertEqual(self.adopt().reason, context_mod.NOT_FROZEN)

    def test_a_snapshot_taken_with_the_setting_off_is_not_frozen(self):
        self.freeze([candidate("a.py", "f", 1, 2, 10)])
        self.assertEqual(self.adopt(meta={"sha256": "abc"}).reason, context_mod.NOT_FROZEN)

    def test_no_candidates_says_so(self):
        self.freeze([], skipped=[{"path": "a.js", "reason": "not python"}])
        adoption = self.adopt()
        self.assertEqual(adoption.reason, context_mod.NO_CANDIDATES)
        self.assertEqual(adoption.skipped, [{"path": "a.js", "reason": "not python"}])

    def test_the_setting_off_adopts_nothing_and_records_nothing(self):
        self.freeze([candidate("a.py", "f", 1, 2, 10)])
        adoption = self.adopt(mode="none")
        self.assertEqual(adoption.mode, "none")
        self.assertEqual(context_mod.render_surrounding(adoption), "")
        self.assertEqual(self.adopt(mode=False).mode, "none")

    def test_a_class_over_the_budget_leaves_its_method_in(self):
        self.freeze(
            [
                candidate("a.py", "K", 1, 20, 5000, kind="class"),
                candidate("a.py", "K.m", 5, 8, 1000, kind="method"),
            ]
        )
        adoption = self.adopt(cap=2000)
        self.assertEqual([c["symbol"] for c in adoption.adopted], ["K.m"])
        self.assertEqual([c["symbol"] for c in adoption.trimmed], ["K"])

    def test_a_class_that_fits_folds_its_method_in(self):
        self.freeze(
            [
                candidate("a.py", "K", 1, 20, 500, kind="class"),
                candidate("a.py", "K.m", 5, 8, 100, kind="method"),
            ]
        )
        adoption = self.adopt(cap=1000)
        self.assertEqual([c["symbol"] for c in adoption.adopted], ["K"])
        self.assertEqual(adoption.adopted[0]["includes"], ["K.m"])
        self.assertEqual(adoption.adopted_chars, 500)
        self.assertEqual(adoption.trimmed, [])

    def test_an_adjacent_method_inside_an_adopted_class_is_folded_in(self):
        # A deletion just past the class's last line makes its last method
        # adjacent; a hunk elsewhere in the class makes the class enclosing.
        self.freeze(
            [
                candidate("a.py", "K", 1, 20, 500, kind="class"),
                candidate("a.py", "K.last", 15, 20, 100, relation="adjacent", kind="method"),
            ]
        )
        adoption = self.adopt(cap=5000)
        self.assertEqual([c["symbol"] for c in adoption.adopted], ["K"])
        self.assertEqual(adoption.adopted[0]["includes"], ["K.last"])
        self.assertEqual((adoption.adopted_chars, adoption.trimmed), (500, []))
        self.assertEqual(context_mod.render_surrounding(adoption).count("\n### "), 1)

    def test_the_record_carries_no_source(self):
        self.freeze([candidate("a.py", "f", 1, 2, 10)])
        record = self.adopt().record()
        self.assertNotIn("text", record["adopted"][0])
        self.assertEqual(
            sorted(record),
            [
                "adopted",
                "adopted_chars",
                "budget",
                "context_chars",
                "mode",
                "reason",
                "skipped",
                "trimmed",
                "trimmed_chars",
            ],
        )
        self.assertEqual(
            self.adopt().summary(),
            {"mode": "enclosing", "adopted_chars": 10, "trimmed_chars": 0, "adopted": 1, "trimmed": 0},
        )


class TestTheSurroundingModeSetting(unittest.TestCase):
    def test_modes(self):
        for value, expected in (
            ("enclosing", "enclosing"),
            (" Enclosing ", "enclosing"),
            ("none", "none"),
            (None, "none"),
            (False, "none"),
            (True, "none"),
            ("window", "none"),
        ):
            self.assertEqual(context_mod.surrounding_mode(value), expected, value)


# --------------------------------------------------------------------------- the prompt block


class TestRenderSurrounding(AdoptCase):
    def render(self, **adopt):
        return context_mod.render_surrounding(self.adopt(**adopt))

    def test_a_heading_per_symbol(self):
        self.freeze([candidate("a.py", "f", 3, 9, 1912)])
        text = self.render(cap=5000)
        self.assertTrue(text.startswith("Surrounding context (review.context.surrounding: enclosing). The"))
        self.assertIn("### a.py:3-9 f (function, 1,912 chars)\n```python\n", text)
        self.assertNotIn("adjacent", text)
        self.assertNotIn("Left out", text)

    def test_includes_are_named(self):
        self.freeze(
            [
                candidate("a.py", "K", 1, 20, 50, kind="class"),
                candidate("a.py", "K.m", 5, 8, 10, kind="method"),
            ]
        )
        self.assertIn("### a.py:1-20 K (class, 50 chars; includes K.m)", self.render())

    def test_adjacent_is_marked_and_explained(self):
        self.freeze([candidate("a.py", "S.open", 90, 140, 10, relation="adjacent", kind="method")])
        text = self.render()
        self.assertIn("(method, 10 chars; adjacent to a deletion, not enclosing it)", text)
        self.assertIn('A symbol marked "adjacent" sits next to a deletion', text)

    def test_the_left_out_list_names_the_limit(self):
        self.freeze([candidate("a.py", "f", 1, 2, 10), candidate("b.py", "g", 1, 2, 9000)])
        text = self.render(cap=2000)
        self.assertIn("### a.py:1-2 f", text)
        self.assertIn("Left out (over review.context.surrounding_chars, 2,000): read these yourself", text)
        self.assertIn("- b.py:1-2 g (function, 9,000 chars)", text)

    def test_a_budget_cut_by_max_chars_says_so(self):
        self.freeze([candidate("a.py", "f", 1, 2, 10), candidate("b.py", "g", 1, 2, 9000)])
        text = self.render(cap=5000, change=1000, max_chars=3000)
        self.assertIn("### a.py:1-2 f", text)
        self.assertIn("Left out (over the 2,000 chars review.context.max_chars / inline_chars left)", text)

    def test_not_extracted_is_counted_and_the_curable_ones_named(self):
        reason = "ambiguous path (x.py or b/x.py)"
        skipped = [
            {"path": "a.js", "reason": "not python"},
            {"path": "b.js", "reason": "not python"},
            {"path": "n.py", "reason": context_mod.NEW_FILE},
            {"path": "x.py", "reason": reason},
            {"path": "b/x.py", "reason": reason},
            {"path": "d.py", "reason": context_mod.DRIFTED},
        ]
        self.freeze([candidate("a.py", "f", 1, 2, 10)], skipped=skipped)
        self.assertIn(
            "Not extracted: 6 file(s) (not python: 2, new file: 1, ambiguous path: 2 -- x.py, b/x.py, "
            "changed while the snapshot was taken: 1 -- d.py).",
            self.render(),
        )

    def test_nothing_adopted_says_why_and_names_what_was_left_out(self):
        self.freeze([candidate("a.py", "f", 12, 88, 3100, kind="class")])
        self.assertEqual(
            self.render(delivery="file"),
            "Surrounding context (review.context.surrounding: enclosing): nothing was added -- the change "
            "body is handed over as a file.\n"
            "Left out (file delivery): read these yourself if a hunk needs them.\n"
            "- a.py:12-88 f (class, 3,100 chars)",
        )

    def test_a_fence_inside_the_source_does_not_close_the_block(self):
        body = 'class User:\n    DOC = """\n```\nnot a fence\n```\n"""'
        self.freeze([dict(candidate("a.py", "User", 1, 6, len(body), kind="class"), text=body)])
        self.assertIn("````python\n%s\n````" % body, self.render())


# --------------------------------------------------------------------------- the prompt, off


class TestTheSettingOffChangesNothing(AdoptCase):
    def test_the_prompt_is_byte_for_byte_the_same(self):
        diff_text = "diff --git a/x b/x\n+y\n"
        before = review_mod.build_review_prompt(reviewer("r1"), self.workspace, diff_text)
        for surrounding in (None, context_mod.Adoption()):
            after = review_mod.build_review_prompt(
                reviewer("r1"), self.workspace, diff_text, surrounding=surrounding
            )
            self.assertEqual(after, before)

    def test_a_design_prompt_never_carries_context(self):
        self.freeze([candidate("a.py", "f", 1, 2, 10)])
        ws.write_text(self.workspace.snapshot_path, "plan body")
        ws.write_json(self.workspace.snapshot_meta_path, {"sha256": "abc"})
        runs = review_mod.run_reviews(
            [reviewer("r1")],
            self.workspace,
            parallel=False,
            prompt_for=lambda r: review_mod.BuiltPrompt("design prompt", "inline", 9),
            surrounding=self.adopt(),
        )
        self.assertNotIn("surrounding", runs[0].to_dict())


# --------------------------------------------------------------------------- end to end

APP = """def add(a, b):
    return a + b


def sub(a, b):
    return a - b


class Box:
    SIZE = 1

    def open(self):
        return "open"
"""


@unittest.skipUnless(has_git(), "git is required")
class GitCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", APP)
        self.commit_all("init")
        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        self.workspace = self.cli_workspace()

    def enable(self, **settings):
        run_cli("config", "set", "review.context.surrounding", "enclosing")
        for key, value in settings.items():
            run_cli("config", "set", "review.context.%s" % key, str(value))

    def edit_add(self, body="return a * b"):
        self.write("app.py", APP.replace("return a + b", body))

    def frozen(self):
        return ws.read_json(self.workspace.surrounding_path, {})

    def meta(self):
        return ws.read_json(self.workspace.snapshot_meta_path, {})

    def consolidated(self):
        return ws.read_json(self.workspace.consolidated_json_path, {})

    def report(self):
        return ws.read_text(self.workspace.consolidated_md_path)

    def last_review_event(self):
        events = self.workspace.read_state().get("events") or []
        return [event for event in events if event.get("stage") == "review"][-1]

    def prompts_of(self, *argv):
        """Run `review run` and return every code-review prompt it built."""
        built = []
        original = review_mod.build_review_prompt

        def spy(*args, **kwargs):
            result = original(*args, **kwargs)
            built.append(result.text)
            return result

        with mock.patch.object(review_mod, "build_review_prompt", side_effect=spy):
            code, out, err = run_cli("review", "run", *argv)
        return code, out, err, built


class TestEndToEnd(GitCase):
    def test_on_from_snapshot_to_status(self):
        self.enable()
        self.edit_add()
        code, out, _ = run_cli("review", "snapshot")
        self.assertEqual(code, 0)
        self.assertIn("  context:  enclosing -- 1 symbol(s)", out)
        self.assertIn("adopted at review run within review.context.surrounding_chars (60,000)", out)
        meta, frozen = self.meta(), self.frozen()
        self.assertEqual(frozen["sha256"], meta["sha256"])
        self.assertRegex(frozen["tree"], r"^[0-9a-f]{40}$")
        self.assertEqual(meta["surrounding"]["tree"], frozen["tree"])
        relative = self.workspace.relative(self.workspace.surrounding_path)
        self.assertEqual(meta["surrounding"]["path"], relative)
        self.assertEqual([c["symbol"] for c in frozen["candidates"]], ["add"])

        code, out, _, built = self.prompts_of()
        self.assertEqual(code, 0)
        self.assertIn("Surrounding context: 1 symbol(s)", out)
        self.assertIn("### app.py:1-2 add (function,", built[0])
        self.assertIn("def add(a, b):\n    return a * b\n```", built[0])

        data = self.consolidated()
        self.assertTrue(data["surrounding"]["shared"])
        self.assertEqual([c["symbol"] for c in data["surrounding"]["adopted"]], ["add"])
        self.assertEqual(data["reviewers"][0]["surrounding"]["adopted"][0]["symbol"], "add")
        self.assertIn("- Surrounding context: enclosing -- 1 symbol(s)", self.report())
        self.assertIn("## Surrounding context\n\nShown to every reviewer (1 symbol(s)", self.report())
        self.assertIn("- app.py:1-2 add (function,", self.report())

        self.assertIn("surrounding context: enclosing,", run_cli("review", "status")[1])
        status = json.loads(run_cli("review", "status", "--json")[1])
        self.assertTrue(status["surrounding"]["shared"])

        event = self.last_review_event()
        self.assertEqual(event["surrounding"]["adopted"], 1)
        self.assertGreater(event["surrounding"]["adopted_chars"], 0)

    def test_a_round_whose_every_reviewer_failed_first_is_not_a_round_with_context(self):
        self.enable()
        self.edit_add()
        run_cli("review", "snapshot")
        with mock.patch.object(review_mod, "get_provider", side_effect=RuntimeError("no provider")):
            run_cli("review", "run")
        event = self.last_review_event()
        self.assertEqual([run["status"] for run in event["reviewers"]], ["failed"])
        self.assertNotIn("surrounding", event)
        self.assertFalse(opt_mod._with_context(event))

    def test_a_file_not_extracted_is_named_in_the_report_and_status(self):
        self.write("bad.py", "def ok():\n    return 1\n")
        self.commit_all("bad")
        self.write("bad.py", "def ok(:\n    return 2\n")
        self.enable()
        self.edit_add()
        run_cli("review", "snapshot")
        self.assertIn({"path": "bad.py", "reason": "syntax error"}, self.frozen()["skipped"])
        run_cli("review", "run")
        self.assertIn("Not extracted (1 file(s)):\n- `bad.py` -- syntax error", self.report())
        self.assertIn("  not extracted: bad.py -- syntax error", run_cli("review", "status")[1])

    def test_off_writes_nothing_new_anywhere(self):
        self.edit_add()
        code, out, _ = run_cli("review", "snapshot")
        self.assertEqual(code, 0)
        self.assertNotIn("context:", out)
        self.assertFalse(os.path.isfile(self.workspace.surrounding_path))
        self.assertNotIn("surrounding", self.meta())
        self.assertNotIn("surrounding", json.loads(run_cli("review", "snapshot", "--json")[1]))
        _, out, _, built = self.prompts_of()
        self.assertNotIn("Surrounding context", out)
        self.assertNotIn("Surrounding context", built[0])
        data = self.consolidated()
        self.assertNotIn("surrounding", data)
        self.assertNotIn("surrounding", data["reviewers"][0])
        self.assertNotIn("Surrounding", self.report())
        self.assertNotIn("surrounding", json.loads(run_cli("review", "status", "--json")[1]))
        self.assertNotIn("surrounding", self.last_review_event())

    def test_off_with_incremental_off_writes_no_tree(self):
        run_cli("config", "set", "review.incremental_rounds", "false")
        self.edit_add()
        with mock.patch.object(review_mod, "_write_tree", side_effect=AssertionError("wrote a tree")):
            code, _, err = run_cli("review", "snapshot")
        self.assertEqual(code, 0, err)

    def test_turning_it_off_removes_an_older_frozen_file(self):
        self.enable()
        self.edit_add()
        run_cli("review", "snapshot")
        self.assertTrue(os.path.isfile(self.workspace.surrounding_path))
        run_cli("config", "set", "review.context.surrounding", "off")
        run_cli("review", "snapshot")
        self.assertFalse(os.path.isfile(self.workspace.surrounding_path))

    def test_off_in_yaml_means_none(self):
        run_cli("config", "set", "review.context.surrounding", "off")
        loaded = config_mod.load(self.project)
        self.assertEqual(context_mod.surrounding_mode(loaded.context_settings()["surrounding"]), "none")
        problems = [p for p in config_mod.validate(loaded.data) if "review.context" in p]
        self.assertEqual(problems, [])

    def test_a_snapshot_taken_with_it_off_is_not_frozen(self):
        self.edit_add()
        run_cli("review", "snapshot")
        self.enable()
        _, out, _, built = self.prompts_of()
        self.assertIn("Surrounding context: nothing adopted -- not frozen", out)
        self.assertIn("nothing was added -- not frozen", built[0])
        self.assertEqual(self.consolidated()["surrounding"]["adopted"], [])

    def test_the_design_review_is_left_alone(self):
        self.enable()
        self.edit_add()
        run_cli("review", "snapshot")
        design = review_mod.build_design_review_prompt(reviewer("r1"), self.workspace.design_review(), "plan")
        self.assertNotIn("Surrounding context", design.text)


class TestFrozenAtTheSnapshot(GitCase):
    def test_editing_after_the_snapshot_changes_nothing_a_reviewer_sees(self):
        self.enable()
        self.edit_add()
        run_cli("review", "snapshot")
        self.edit_add("later = 1\n    return a * b")
        _, _, _, built = self.prompts_of()
        self.assertNotIn("later = 1", built[0])
        self.assertIn("return a * b", built[0])
        self.assertNotIn("later = 1", json.dumps(self.frozen()))

    def test_an_untracked_new_file_is_named_and_nothing_drifts(self):
        for incremental in ("true", "false"):
            run_cli("config", "set", "review.incremental_rounds", incremental)
            self.enable()
            self.edit_add()
            self.write("new.py", "def one():\n    return 1\n\n\ndef two():\n    return 2\n")
            run_cli("review", "snapshot")
            meta, frozen = self.meta(), self.frozen()
            self.assertIn("new.py", meta["untracked_included"])
            self.assertIn({"path": "new.py", "reason": context_mod.NEW_FILE}, frozen["skipped"])
            self.assertEqual([c["path"] for c in frozen["candidates"]], ["app.py"])
            self.assertFalse(any(entry["reason"] == context_mod.DRIFTED for entry in frozen["skipped"]))
            if incremental == "false":
                self.assertEqual(meta["tree"], "")
                self.assertRegex(meta["surrounding"]["tree"], r"^[0-9a-f]{40}$")
            self.write("new.py", "def one():\n    return 3\n")
            self.edit_add("return a // b")
            _, _, _, built = self.prompts_of()
            self.assertIn("return a * b", built[-1])
            self.assertNotIn("return a // b", built[-1])

    def test_a_file_edited_between_the_tree_and_the_hash_is_dropped(self):
        self.write("lib.py", "def mul(a, b):\n    return a * b\n")
        self.commit_all("lib")
        self.edit_add()
        self.write("lib.py", "def mul(a, b):\n    return b * a\n")
        tree = review_mod._write_tree(self.project)
        _, diff, _ = review_mod._diff(self.project, ["HEAD"], [])
        self.edit_add("return 0")
        frozen = context_mod.extract(self.project, tree, diff, ["app.py", "lib.py"], working_tree_diff=True)
        self.assertEqual(frozen["skipped"], [{"path": "app.py", "reason": context_mod.DRIFTED}])
        self.assertEqual([c["path"] for c in frozen["candidates"]], ["lib.py"])

    def test_both_x_py_and_b_x_py_are_read_from_their_own_file(self):
        self.write("x.py", "def top_level():\n    return 1\n")
        self.write("b/x.py", "def nested_one():\n    return 1\n")
        self.commit_all("two")
        self.write("x.py", "def top_level():\n    return 2\n")
        self.write("b/x.py", "def nested_one():\n    return 2\n")
        self.enable()
        run_cli("review", "snapshot")
        by_path = {c["path"]: c for c in self.frozen()["candidates"]}
        self.assertEqual(by_path["x.py"]["symbol"], "top_level")
        self.assertIn("return 2", by_path["x.py"]["text"])
        self.assertEqual(by_path["b/x.py"]["symbol"], "nested_one")

    def test_a_renamed_file_is_read_under_its_new_name(self):
        body = "".join("def f%d():\n    return %d\n\n\n" % (i, i) for i in range(20))
        self.write("old.py", body)
        self.commit_all("old")
        self.git("mv", "old.py", "new.py")
        self.write("new.py", body.replace("return 3\n", "return 33\n"))
        self.enable()
        run_cli("review", "snapshot")
        self.assertIn("new.py", self.meta()["files"])
        self.assertEqual([(c["path"], c["symbol"]) for c in self.frozen()["candidates"]], [("new.py", "f3")])

    def test_a_symlink_is_never_followed(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        for name, secret in (("one.py", "SECRET_ONE"), ("two.py", "SECRET_TWO")):
            with open(os.path.join(outside, name), "w", encoding="utf-8") as handle:
                handle.write("def leak():\n    return '%s'\n" % secret)
        link = os.path.join(self.project, "link.py")
        try:
            os.symlink(os.path.join(outside, "one.py"), link)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        self.commit_all("link")
        listed = subprocess.run(
            ["git", "ls-tree", "HEAD", "--", "link.py"], cwd=self.project, capture_output=True, text=True
        ).stdout
        if not listed.startswith("120000"):
            self.skipTest("git here does not record symlinks as symlinks")
        os.unlink(link)
        os.symlink(os.path.join(outside, "two.py"), link)
        self.enable()
        run_cli("review", "snapshot")
        self.assertIn({"path": "link.py", "reason": "symlink"}, self.frozen()["skipped"])
        _, _, _, built = self.prompts_of()
        for text in (json.dumps(self.frozen()), built[0]):
            self.assertNotIn("SECRET", text)


class TestBudgetAndDelivery(GitCase):
    def test_a_trimmed_symbol_is_named_everywhere(self):
        # The method made the larger of the two, so the one left out is known.
        shut = '"%s"' % ("shut" * 100)
        self.write("app.py", APP.replace("return a + b", "return a * b").replace('"open"', shut))
        self.enable()
        run_cli("review", "snapshot")
        self.assertEqual(len(self.frozen()["candidates"]), 2)
        both = context_mod.adopt(self.workspace, self.meta(), "enclosing", 60_000, 0, 0, 0, "inline")
        self.assertEqual(len(both.adopted), 2)
        run_cli("config", "set", "review.context.surrounding_chars", str(both.context_chars - 1))
        _, out, _, built = self.prompts_of()
        data = self.consolidated()
        self.assertEqual(len(data["surrounding"]["adopted"]), 1)
        (left,) = data["surrounding"]["trimmed"]
        self.assertEqual(left["reason"], "budget")
        name = "%s:%s-%s %s" % (left["path"], left["start"], left["end"], left["symbol"])
        self.assertIn("1 left out (budget)", out)
        self.assertIn("Left out (over review.context.surrounding_chars", built[0])
        self.assertIn(name, built[0])
        self.assertIn("Left out (budget):\n- %s" % name, self.report())
        self.assertIn("  left out: %s" % name, run_cli("review", "status")[1])
        run = data["reviewers"][0]
        self.assertEqual(run["budget_chars"], run["change_chars"] + data["surrounding"]["context_chars"])
        self.assertGreater(data["surrounding"]["context_chars"], data["surrounding"]["adopted_chars"])
        self.assertFalse(run["over_budget"])
        self.assertEqual(data["coverage"]["round"], "complete")

    def test_everything_trimmed_is_still_a_complete_round(self):
        self.edit_add()
        self.enable(surrounding_chars=1)
        run_cli("review", "snapshot")
        run_cli("review", "run")
        data = self.consolidated()
        self.assertEqual(data["surrounding"]["adopted"], [])
        self.assertEqual(len(data["surrounding"]["trimmed"]), 1)
        self.assertEqual(data["coverage"]["round"], "complete")
        self.assertEqual(data["reviewers"][0]["status"], "ok")

    def test_a_file_delivery_adds_nothing_and_names_it_all(self):
        self.edit_add()
        self.enable(inline_chars=10)
        run_cli("review", "snapshot")
        _, _, _, built = self.prompts_of()
        self.assertIn("nothing was added -- the change body is handed over as a file", built[0])
        self.assertIn("Left out (file delivery): read these yourself", built[0])
        self.assertIn("app.py:1-2 add", built[0])
        data = self.consolidated()
        self.assertEqual(data["surrounding"]["trimmed"][0]["reason"], "file delivery")
        self.assertEqual([run["status"] for run in data["reviewers"]], ["partial"])
        self.assertEqual(data["coverage"]["round"], "unverified")
        self.assertIn("Left out (file delivery):\n- app.py:1-2 add", self.report())
        self.assertIn("  left out: app.py:1-2 add", run_cli("review", "status")[1])


class TestReviewersThatDisagree(GitCase):
    def setUp(self):
        super().setUp()
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m2", "--role", "general")

    def test_a_rerun_with_another_setting_is_reported_per_reviewer(self):
        self.enable()
        self.edit_add()
        run_cli("review", "snapshot")
        run_cli("review", "run")
        self.assertTrue(self.consolidated()["surrounding"]["shared"])
        run_cli("config", "set", "review.context.surrounding_chars", "1")
        run_cli("review", "run", "--only", "m2")
        surrounding = self.consolidated()["surrounding"]
        self.assertFalse(surrounding["shared"])
        self.assertEqual(sorted(surrounding["by_reviewer"]), ["m1", "m2"])
        self.assertIn("- Surrounding context: differs by reviewer -- ", self.report())
        self.assertIn("### m1", self.report())
        self.assertIn("### m2", self.report())
        out = run_cli("review", "status")[1]
        self.assertIn("surrounding context (m1):", out)
        self.assertIn("surrounding context (m2):", out)

        run_cli("config", "set", "review.context.surrounding", "none")
        run_cli("review", "run", "--only", "m2")
        self.assertIsNone(self.consolidated()["surrounding"]["by_reviewer"]["m2"])
        self.assertIn("None (ran with review.context.surrounding none).", self.report())

    def test_a_reviewer_that_failed_before_its_prompt_is_not_counted(self):
        self.edit_add()
        review_mod.create_snapshot(self.workspace, surrounding="enclosing")
        adoption = context_mod.adopt(self.workspace, self.meta(), "enclosing", 60_000, 100, 0, 0, "inline")
        runs = review_mod.run_reviews(
            [reviewer("m1"), reviewer("m2", provider="no-such-provider")],
            self.workspace,
            parallel=False,
            surrounding=adoption,
        )
        entries = [run.to_dict() for run in runs]
        self.assertEqual(entries[1]["status"], "failed")
        self.assertEqual(entries[1]["snapshot"], "")
        self.assertNotIn("surrounding", entries[1])
        data = review_mod.build_consolidation(self.workspace, entries, [])
        self.assertTrue(data["surrounding"]["shared"])
        self.assertNotIn("by_reviewer", data["surrounding"])
        text = review_mod.render_consolidation(data)
        self.assertIn("## Surrounding context", text)
        self.assertNotIn("### m2", text)
        self.assertIn("FAILED [general] m2", text)


FINDING = """## Finding
- Severity: high
- File: app.py
- Line: 2
- Category: correctness
- Problem: add multiplies
- Impact: every caller gets a product
- Evidence: return a * b
- Recommended fix: add them
"""


class TestTheFixRound(GitCase):
    def test_a_fix_round_gets_the_symbol_around_the_fix_as_frozen(self):
        mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(mock_dir)
        with open(os.path.join(mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING)
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = mock_dir
        self.enable()
        self.edit_add()
        run_cli("review", "snapshot")
        run_cli("review", "run")
        run_cli("review", "triage", "F1", "--status", "accepted")
        self.edit_add("return a + b + 0")
        run_cli("review", "snapshot")
        self.assertTrue(self.meta()["incremental_from"])
        self.assertEqual([c["symbol"] for c in self.frozen()["candidates"]], ["add"])
        self.edit_add("return 42")
        _, _, _, built = self.prompts_of()
        self.assertIn("return a + b + 0", built[0])
        self.assertNotIn("return 42", built[0])


if __name__ == "__main__":
    unittest.main()
