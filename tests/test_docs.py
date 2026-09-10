"""Documentation must be copy-pasteable.

Every YAML block in the docs is something a user will paste into a config file,
so it has to parse with the *bundled* parser -- not merely with PyYAML, which is
optional. Flow-style examples used to look fine in review and then fail on any
machine without PyYAML installed.
"""

from __future__ import annotations

import pathlib
import re
import unittest

from helpers import REPO_ROOT, IsolatedCase

from orchestrator import miniyaml
from orchestrator.miniyaml import _parse_node, _read_lines

YAML_FENCE = re.compile(r"^```ya?ml\s*$(.*?)^```\s*$", re.MULTILINE | re.DOTALL)


def documentation_files():
    names = ["README.md", "README.ja.md", "SKILL.md", "CONTRIBUTING.md", "CHANGELOG.md"]
    paths = [pathlib.Path(REPO_ROOT) / name for name in names]
    paths += sorted((pathlib.Path(REPO_ROOT) / "references").glob("*.md"))
    return [path for path in paths if path.is_file()]


def parse_with_bundled_parser(text: str):
    """Parse using the built-in subset parser even where PyYAML exists."""
    lines = _read_lines(text)
    if not lines:
        return None
    value, consumed = _parse_node(lines, 0)
    if consumed != len(lines):
        raise miniyaml.YamlError("unconsumed lines from %d" % consumed)
    return value


class TestDocumentedYaml(IsolatedCase):
    def test_every_yaml_block_parses_without_pyyaml(self):
        failures = []
        blocks = 0
        for path in documentation_files():
            text = path.read_text(encoding="utf-8")
            for index, block in enumerate(YAML_FENCE.findall(text), 1):
                blocks += 1
                try:
                    parse_with_bundled_parser(block)
                except Exception as exc:  # collected and reported together
                    failures.append("%s block %d: %s" % (path.relative_to(REPO_ROOT), index, exc))
        self.assertGreater(blocks, 5, "expected the docs to contain YAML examples")
        self.assertEqual(failures, [], "\n".join(failures))

    def test_no_documented_block_uses_flow_mappings(self):
        """The bundled parser rejects them, so they must not be documented."""
        offenders = []
        for path in documentation_files():
            for index, block in enumerate(YAML_FENCE.findall(path.read_text(encoding="utf-8")), 1):
                for line in block.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#") or "{}" in stripped:
                        continue
                    if re.search(r":\s*\{", stripped):
                        offenders.append("%s block %d: %s" % (path.name, index, stripped[:60]))
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_shipped_examples_parse_too(self):
        for name in ("config.example.yaml", "project-override.example.yaml"):
            path = pathlib.Path(REPO_ROOT) / "examples" / name
            data = parse_with_bundled_parser(path.read_text(encoding="utf-8"))
            self.assertEqual(data["version"], 1, name)

    def test_the_agent_manifest_parses_too(self):
        path = pathlib.Path(REPO_ROOT) / "agents" / "openai.yaml"
        self.assertEqual(parse_with_bundled_parser(path.read_text(encoding="utf-8"))["version"], "0.1.0")

    def test_readme_config_example_is_a_valid_configuration(self):
        """The main README block is not just parseable, it is usable."""
        from orchestrator import config as config_mod

        text = (pathlib.Path(REPO_ROOT) / "README.md").read_text(encoding="utf-8")
        for block in YAML_FENCE.findall(text):
            data = parse_with_bundled_parser(block)
            if isinstance(data, dict) and "orchestrator" in data and "reviewers" in data:
                self.assertEqual(config_mod.validate(data), [])
                return
        self.fail("README.md no longer contains a full configuration example")


if __name__ == "__main__":
    unittest.main()
