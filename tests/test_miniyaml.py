"""The YAML subset codec used when PyYAML is not installed."""

from __future__ import annotations

import unittest

from helpers import IsolatedCase

from orchestrator import miniyaml
from orchestrator.miniyaml import _emit, _parse_node, _read_lines


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


if __name__ == "__main__":
    unittest.main()
