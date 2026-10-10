"""The YAML subset codec used when PyYAML is not installed."""

from __future__ import annotations

import unittest
from unittest import mock

from helpers import IsolatedCase

from orchestrator import miniyaml
from orchestrator.miniyaml import _emit, _parse_node, _read_lines

try:  # compared against where it is installed; the bundled parser is tested either way
    import yaml
except ImportError:  # pragma: no cover - depends on the machine
    yaml = None


def parse_without_pyyaml(text: str):
    """Exercise the built-in parser even on machines that have PyYAML."""
    lines = _read_lines(text)
    value, consumed = _parse_node(lines, 0)
    assert consumed == len(lines), "unconsumed lines"
    return value


def dump(value) -> str:
    out = []
    _emit(value, 0, out)
    return "\n".join(out) + "\n"


SAMPLE = """
# leading comment
version: 1
orchestrator:
  provider: claude
  model:
    family: sonnet
    version: latest   # trailing comment
reviewers:
  - id: a
    provider: claude
    model:
      family: opus
      version: latest
    role: general
  - id: b
    provider: codex
    role: security
review:
  parallel: true
  max_review_iterations: 2
  severities: [critical, high]
empty_list: []
empty_map: {}
nothing: null
quoted: "yes"
"""


class TestParsing(IsolatedCase):
    def test_nested_mappings_and_sequences(self):
        data = parse_without_pyyaml(SAMPLE)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["orchestrator"]["model"]["family"], "sonnet")
        self.assertEqual([r["id"] for r in data["reviewers"]], ["a", "b"])
        self.assertEqual(data["reviewers"][0]["model"]["version"], "latest")
        self.assertEqual(data["reviewers"][1]["role"], "security")

    def test_scalar_types(self):
        data = parse_without_pyyaml(SAMPLE)
        self.assertIs(data["review"]["parallel"], True)
        self.assertEqual(data["review"]["max_review_iterations"], 2)
        self.assertEqual(data["review"]["severities"], ["critical", "high"])
        self.assertEqual(data["empty_list"], [])
        self.assertEqual(data["empty_map"], {})
        self.assertIsNone(data["nothing"])
        self.assertEqual(data["quoted"], "yes")

    def test_comment_inside_a_quoted_string_is_kept(self):
        data = parse_without_pyyaml('note: "a # b"\n')
        self.assertEqual(data["note"], "a # b")

    def test_round_trip(self):
        data = parse_without_pyyaml(SAMPLE)
        self.assertEqual(parse_without_pyyaml(dump(data)), data)

    def test_reserved_words_are_quoted_on_emit(self):
        self.assertEqual(parse_without_pyyaml(dump({"a": "yes"}))["a"], "yes")
        self.assertEqual(parse_without_pyyaml(dump({"a": "null"}))["a"], "null")

    def test_dotted_values_survive(self):
        self.assertEqual(parse_without_pyyaml(dump({"dir": ".ai"}))["dir"], ".ai")

    def test_block_scalars_are_rejected_loudly(self):
        with self.assertRaises(miniyaml.YamlError):
            parse_without_pyyaml("text: |\n  hello\n")

    def test_missing_colon_is_rejected(self):
        with self.assertRaises(miniyaml.YamlError):
            parse_without_pyyaml("just a sentence\n")

    def test_json_is_accepted_by_loads(self):
        self.assertEqual(miniyaml.loads('{"a": 1}'), {"a": 1})

    def test_empty_document(self):
        self.assertIsNone(miniyaml.loads("   \n"))

    def test_parse_scalar_helper(self):
        self.assertEqual(miniyaml.parse_scalar("2"), 2)
        self.assertEqual(miniyaml.parse_scalar("opus"), "opus")


class TestRefusals(IsolatedCase):
    """What the built-in parser cannot read is refused, never dropped (#278)."""

    def test_sequence_items_indented_past_the_dash(self):
        data = parse_without_pyyaml(
            "reviewers:\n-   id: a\n    provider: b\n    model:\n      family: x\n- id: c\n"
        )
        self.assertEqual(
            data["reviewers"], [{"id": "a", "provider": "b", "model": {"family": "x"}}, {"id": "c"}]
        )

    def test_nested_sequence_in_an_item(self):
        self.assertEqual(parse_without_pyyaml("- - a\n  - b\n- c\n"), [["a", "b"], "c"])

    def test_item_line_out_of_column_is_refused(self):
        for text in ("- a: 1\n b: 2\n", "-   a: 1\n  b: 2\n", "-   a:\n  b: 1\n"):
            with self.assertRaises(miniyaml.YamlError, msg=text):
                parse_without_pyyaml(text)

    def test_anchors_aliases_tags_and_merge_keys_are_refused(self):
        for text in (
            "a: &x 1\n",
            "a: *x\n",
            "paths:\n  - *.sql\n",
            "paths: [*.sql]\n",
            "&x a: 1\n",
            "a: !!str 1\n",
            "<<: {}\n",
        ):
            with self.assertRaises(miniyaml.YamlError, msg=text):
                parse_without_pyyaml(text)

    def test_quoted_pattern_is_fine(self):
        self.assertEqual(parse_without_pyyaml('paths:\n  - "*.sql"\n')["paths"], ["*.sql"])

    def test_every_block_scalar_form_is_refused(self):
        for text in ("a: |-\n  x\n", "a: >2\n  x\n", "- |\n  x\n"):
            with self.assertRaises(miniyaml.YamlError, msg=text):
                parse_without_pyyaml(text)

    def test_plain_value_ending_in_a_bar_is_text(self):
        self.assertEqual(parse_without_pyyaml("a: x |\n")["a"], "x |")

    def test_unclosed_inline_list_is_refused(self):
        with self.assertRaises(miniyaml.YamlError):
            parse_without_pyyaml("a: [x\n")

    def test_one_document_with_markers_is_read(self):
        self.assertEqual(parse_without_pyyaml("---\na: 1\n...\n"), {"a": 1})

    def test_multi_document_stream_is_refused(self):
        for text in ("a: 1\n---\nb: 2\n", "---\na: 1\n---\n", "a: 1\n...\nb: 2\n", "--- a: 1\n"):
            with self.assertRaises(miniyaml.YamlError, msg=text):
                parse_without_pyyaml(text)

    def test_error_names_the_line(self):
        with self.assertRaisesRegex(miniyaml.YamlError, r"aliases.*line 3"):
            parse_without_pyyaml("a: 1\npaths:\n  - *.sql\n")

    def test_command_line_values_may_start_with_a_star(self):
        self.assertEqual(miniyaml.parse_scalar("*.sql"), "*.sql")
        self.assertEqual(miniyaml.parse_scalar("[*.sql, '*.SQL']"), ["*.sql", "*.SQL"])

    def test_written_star_pattern_reads_back(self):
        self.assertEqual(parse_without_pyyaml(dump({"paths": ["*.sql", "&x"]})), {"paths": ["*.sql", "&x"]})


# Pieces that each broke, or could break, a value on the way out and back in.
TRICKY_PIECES = [
    "",
    "a",
    "C:\\work\\new",
    "\\",
    "\\\\n",
    "\\n",
    "\n",
    "\t",
    "\r",
    '"',
    "'",
    "it's",
    "#",
    " # ",
    ": ",
    "- ",
    "[a, b]",
    "{}",
    "123",
    "1.0",
    ".5",
    "-1",
    "1e5",
    "0x1F",
    "2026-10-06",
    "yes",
    "null",
    "~",
    ".inf",
    "*.sql",
    "&x",
    "\x00",
    "\x1b",
    "\u2028",
    "\x85",
    "日本語",
    " ",
]


class TestRoundTrip(IsolatedCase):
    def assertRoundTrips(self, value):
        text = dump({"k": value, "list": [value], "nested": {value or "key": value}})
        back = parse_without_pyyaml(text)
        self.assertEqual(back, {"k": value, "list": [value], "nested": {value or "key": value}}, text)

    def test_windows_path_keeps_its_backslashes(self):
        # `\\n` used to be read as a backslash and a newline (#275).
        self.assertEqual(parse_without_pyyaml(dump({"p": "C:\\work\\new"}))["p"], "C:\\work\\new")
        self.assertEqual(parse_without_pyyaml('p: "C:\\\\work\\\\new"\n')["p"], "C:\\work\\new")

    def test_number_looking_strings_stay_strings(self):
        for text in ("123", "1.0", ".5", "007", "1_000", "0x1F", "2026-10-06", "0.22.0"):
            emitted = dump({"v": text})
            self.assertEqual(emitted, 'v: "%s"\n' % text)
            self.assertEqual(parse_without_pyyaml(emitted)["v"], text)

    def test_plain_strings_stay_unquoted(self):
        self.assertEqual(dump({"dir": ".ai", "m": "gpt-5.1", "o": "o3"}), "dir: .ai\nm: gpt-5.1\no: o3\n")

    def test_numbers_round_trip(self):
        for number in (0, -3, 1.5, 1e20, 1e-07, float("inf"), float("-inf")):
            self.assertEqual(parse_without_pyyaml(dump({"n": number}))["n"], number)
        self.assertEqual(dump({"n": 1e20}), "n: 1.0e+20\n")
        nan = parse_without_pyyaml(dump({"n": float("nan")}))["n"]
        self.assertNotEqual(nan, nan)

    def test_tab_inside_a_value_is_kept(self):
        self.assertEqual(parse_without_pyyaml('a: "x\ty"\n')["a"], "x\ty")
        self.assertEqual(parse_without_pyyaml("a: x\ty\n")["a"], "x\ty")
        self.assertEqual(parse_without_pyyaml("a:\t1\n")["a"], 1)

    def test_tab_indentation_is_rejected(self):
        with self.assertRaises(miniyaml.YamlError):
            parse_without_pyyaml("a:\n\tb: 1\n")

    def test_double_quoted_escapes(self):
        data = parse_without_pyyaml('a: "\\t\\"\\x41\\u00e9\\U0001F600\\/\\\\"\n')
        self.assertEqual(data["a"], '\t"A\u00e9\U0001f600/\\')

    def test_unknown_escape_is_rejected(self):
        with self.assertRaises(miniyaml.YamlError):
            parse_without_pyyaml('p: "C:\\work"\n')

    def test_single_quoted_doubles_its_quote(self):
        self.assertEqual(parse_without_pyyaml("a: 'it''s \\n'\n")["a"], "it's \\n")

    def test_comment_after_an_escaped_quote_is_stripped(self):
        data = parse_without_pyyaml('a: "x\\" # y" # real comment\n')
        self.assertEqual(data["a"], 'x" # y')

    def test_apostrophe_in_a_plain_value_does_not_hide_a_comment(self):
        self.assertEqual(parse_without_pyyaml("note: it's fine # comment\n")["note"], "it's fine")

    def test_text_after_a_closing_quote_is_rejected(self):
        for text in ('a: "x" y\n', "a: 'x\n"):
            with self.assertRaises(miniyaml.YamlError, msg=text):
                parse_without_pyyaml(text)

    def test_quoted_flow_items_keep_their_commas(self):
        self.assertEqual(parse_without_pyyaml("a: ['x, y', \"z\"]\n")["a"], ["x, y", "z"])

    def test_command_line_value_with_an_unclosed_quote_is_a_string(self):
        self.assertEqual(miniyaml.parse_scalar("'abc"), "'abc")

    def test_every_tricky_string_round_trips(self):
        for first in TRICKY_PIECES:
            self.assertRoundTrips(first)
            for second in TRICKY_PIECES:
                self.assertRoundTrips(first + second)

    def test_an_indicator_the_plain_pattern_lets_through_is_quoted(self):
        """The emitter quotes whatever the reader would refuse, not only what the pattern excludes."""
        with mock.patch.dict(miniyaml._INDICATORS, {"a": "a test indicator"}):
            text = dump({"k": "abc"})
        self.assertEqual(text, 'k: "abc"\n')
        self.assertEqual(parse_without_pyyaml(text), {"k": "abc"})


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class TestReadByPyYAML(IsolatedCase):
    """What the emitter writes reads the same with PyYAML as without it."""

    def pyyaml(self, text):
        assert yaml is not None
        return yaml.safe_load(text)

    def assertSameReading(self, value):
        text = dump({"k": value, "list": [value], "nested": {value or "key": value}})
        expected = {"k": value, "list": [value], "nested": {value or "key": value}}
        self.assertEqual(self.pyyaml(text), expected, text)
        self.assertEqual(parse_without_pyyaml(text), expected, text)

    def test_every_tricky_string(self):
        for first in TRICKY_PIECES:
            self.assertSameReading(first)
            for second in TRICKY_PIECES:
                self.assertSameReading(first + second)

    def test_number_looking_strings(self):
        for text in ("123", "1.0", ".5", "007", "1_000", "0x1F", "2026-10-06", "0.22.0", "1e5", "-1"):
            self.assertSameReading(text)

    def test_numbers(self):
        for number in (0, -3, 1.5, 1e20, 1e-07, float("inf"), float("-inf")):
            with self.subTest(number=number):
                text = dump({"n": number})
                self.assertEqual(self.pyyaml(text)["n"], number, text)
        nan = self.pyyaml(dump({"n": float("nan")}))["n"]
        self.assertNotEqual(nan, nan)


if __name__ == "__main__":
    unittest.main()
