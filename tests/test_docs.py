"""Documentation must be copy-pasteable.

Every YAML block in the docs is something a user will paste into a config file,
so it has to parse with the *bundled* parser -- not merely with PyYAML, which is
optional. Flow-style examples used to look fine in review and then fail on any
machine without PyYAML installed.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import unittest
from typing import Dict, List, Tuple

from helpers import REPO_ROOT, USER_ADAPTER_SOURCE, IsolatedCase

from orchestrator import cli, cli_run, miniyaml
from orchestrator.miniyaml import _parse_node, _read_lines

JA_REFERENCES = pathlib.Path(REPO_ROOT) / "docs" / "ja" / "references"
JA_WORKFLOW = "docs/ja/references/workflow.md"
JA_PROVIDERS = "docs/ja/references/providers.md"

YAML_FENCE = re.compile(r"^```ya?ml\s*$(.*?)^```\s*$", re.MULTILINE | re.DOTALL)


def documentation_files():
    names = [
        "README.md",
        "README.ja.md",
        "skills/dev-orchestra/SKILL.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
    ]
    paths = [pathlib.Path(REPO_ROOT) / name for name in names]
    paths += sorted((pathlib.Path(REPO_ROOT) / "references").glob("*.md"))
    paths += sorted(JA_REFERENCES.glob("*.md"))
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
            assert data is not None
            self.assertEqual(data["version"], 1, name)

    def test_the_agent_manifest_parses_too(self):
        """Checked against SKILL.md's frontmatter -- the source of truth every
        other version is compared to -- not a literal: this is one of seven
        files a release has to bump, and a test pinned to a number is one that
        fails on every release for no reason."""
        import importlib

        validate_skill = importlib.import_module("validate_skill")
        skill = pathlib.Path(REPO_ROOT) / validate_skill.SKILL_PATH
        front, _ = validate_skill.parse_frontmatter(skill.read_text(encoding="utf-8"))

        path = pathlib.Path(REPO_ROOT) / "agents" / "openai.yaml"
        parsed = parse_with_bundled_parser(path.read_text(encoding="utf-8"))
        assert parsed is not None
        self.assertEqual(str(parsed["version"]), str(front["version"]))

    def test_coverage_is_defined_where_the_orchestrator_is_told_to_read_it(self):
        """`review status` prints these words and `consolidated.json` stores
        them, so the reference has to define them rather than mention them."""
        text = (pathlib.Path(REPO_ROOT) / "references" / "reviews.md").read_text(encoding="utf-8")
        self.assertIn("## Coverage", text)
        for term in (
            "`coverage.round`",
            "`coverage.change`",
            "`coverage.unverified_since`",
            "`coverage.inline_chars`",
        ):
            self.assertIn(term, text)
        self.assertIn("`partial`", text)
        self.assertIn("snapshot --full", text)

    def test_surrounding_context_is_defined_with_its_conclusion_on_re_fetching(self):
        """The prompt and every report name what was left out, and point here
        for why; `limits.md` holds the answer to "does it stop re-reading"."""
        references = pathlib.Path(REPO_ROOT) / "references"
        reviews = (references / "reviews.md").read_text(encoding="utf-8")
        self.assertIn("## Surrounding context", reviews)
        self.assertIn("review.context.surrounding", reviews)
        limits = (references / "limits.md").read_text(encoding="utf-8")
        self.assertIn("### Surrounding context within the budget", limits)
        self.assertIn("reported as tool activity, not counted, not limited", limits)

    def test_the_documented_user_adapter_is_the_one_the_contract_test_runs(self):
        """Copied from the docs by anyone writing their first adapter, so it
        has to be the example `test_provider_contract` holds to the seam."""
        text = (pathlib.Path(REPO_ROOT) / "references" / "providers.md").read_text(encoding="utf-8")
        self.assertIn(USER_ADAPTER_SOURCE.strip(), text)

    def test_every_resume_fallback_reason_is_documented(self):
        """`resume.reason` is one of these phrases and nothing else, so the
        table a user reads it against has to list every one."""

        text = (pathlib.Path(REPO_ROOT) / "references" / "cli.md").read_text(encoding="utf-8")
        for reason in cli_run._RESUME_REASONS:
            with self.subTest(reason=reason):
                self.assertIn("| `%s` |" % reason, text)

    def test_the_trust_a_resumed_run_records_is_documented(self):
        text = (pathlib.Path(REPO_ROOT) / "references" / "cli.md").read_text(encoding="utf-8")
        self.assertIn("`resume.trust", text)

    def test_the_resume_trust_note_names_the_id_it_repeats(self):
        """The session id read from `state.json` is recorded, so the note on
        trusting that file must not say nothing read from it is repeated."""
        text = (pathlib.Path(REPO_ROOT) / "references" / "limits.md").read_text(encoding="utf-8")
        note = text.split("* **`state.json` is trusted to name the session", 1)[1].split("\n* ", 1)[0]
        self.assertIn("`resume.resumed_from`", note)
        self.assertNotIn("no value read from it is ever", note)

    def test_the_release_steps_say_to_update_the_resume_table(self):
        """A version smoke_live.py cleared on a maintainer's machine reaches
        users only through the table, so a release has to copy it there."""
        text = (pathlib.Path(REPO_ROOT) / "CONTRIBUTING.md").read_text(encoding="utf-8")
        releases = text.split("## Releases", 1)[1].split("\n## ", 1)[0]
        self.assertIn("VERIFIED_RESUME", releases)

    def test_reference_config_example_is_a_valid_configuration(self):
        """The full example in the configuration reference is not just
        parseable, it is usable."""
        from orchestrator import config as config_mod

        text = (pathlib.Path(REPO_ROOT) / "references" / "configuration.md").read_text(encoding="utf-8")
        full = 0
        for block in YAML_FENCE.findall(text):
            data = parse_with_bundled_parser(block)
            if isinstance(data, dict) and "orchestrator" in data and "reviewers" in data:
                full += 1
                with self.subTest(block=full):
                    self.assertEqual(config_mod.validate(data), [])
        self.assertTrue(full, "references/configuration.md no longer contains a full configuration example")


MARKDOWN_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


class TestReadmeLinks(unittest.TestCase):
    """The README is the way into the references, in both languages."""

    READMES = ("README.md", "README.ja.md")

    def test_every_reference_document_is_linked_from_both_readmes(self):
        references = sorted(path.name for path in (pathlib.Path(REPO_ROOT) / "references").glob("*.md"))
        self.assertTrue(references)
        for readme in self.READMES:
            text = (pathlib.Path(REPO_ROOT) / readme).read_text(encoding="utf-8")
            for name in references:
                with self.subTest(readme=readme, reference=name):
                    self.assertIn("](references/%s)" % name, text)

    def test_every_relative_link_in_the_readmes_resolves(self):
        for readme in self.READMES:
            text = (pathlib.Path(REPO_ROOT) / readme).read_text(encoding="utf-8")
            for target in MARKDOWN_LINK.findall(text):
                if "://" in target or target.startswith("mailto:"):
                    continue
                with self.subTest(readme=readme, target=target):
                    self.assertTrue((pathlib.Path(REPO_ROOT) / target).exists(), target)


class TestCompatibilityPromise(unittest.TestCase):
    """The promise is written once, in the README, and the rest point to it."""

    def read(self, relative: str) -> str:
        return (pathlib.Path(REPO_ROOT) / relative).read_text(encoding="utf-8")

    def section(self, text: str, heading: str) -> str:
        """From ``heading`` to the next heading of the same or a higher level."""
        level = len(heading) - len(heading.lstrip("#"))
        start = text.index(heading + "\n")
        following = re.compile(r"^#{1,%d} " % level, re.MULTILINE)
        found = following.search(text, start + len(heading))
        return text[start : found.start() if found else len(text)]

    def paragraph(self, text: str, opening: str) -> str:
        """The paragraph that begins with ``opening``, up to the next blank line."""
        start = text.index(opening)
        end = text.find("\n\n", start)
        return text[start : end if end != -1 else len(text)]

    def test_the_compatibility_promise_is_stated_once_and_pointed_to(self):
        self.assertIn("\n## Compatibility\n", self.read("README.md"))
        self.assertIn("\n## 互換性\n", self.read("README.ja.md"))
        changelog = self.read("CHANGELOG.md")
        preamble = changelog[: changelog.index("## [Unreleased]")]
        releases = self.section(self.read("CONTRIBUTING.md"), "## Releases")
        stability = self.section(self.read("references/providers.md"), "### Interface stability")
        ja_stability = self.section(self.read(JA_PROVIDERS), "### インターフェースの安定性")
        formats = self.paragraph(self.read("references/workflow.md"), "**How the formats change.**")
        ja_formats = self.paragraph(self.read(JA_WORKFLOW), "**形式の変え方。**")
        upgrading = self.section(self.read("README.md"), "## Upgrading")
        ja_upgrading = self.section(self.read("README.ja.md"), "## アップグレード")
        # The pointer itself, not the word: "Compatibility" turns up elsewhere
        # (a plan template heading), which would pass with the pointer gone.
        pointers = {
            "CHANGELOG.md preamble": (preamble, '"Compatibility" in `README.md`'),
            "CONTRIBUTING.md Releases": (releases, '"Compatibility" in `README.md`'),
            "workflow.md formats": (formats, '"Compatibility" in the README'),
            "providers.md Interface stability": (stability, '("Compatibility" in the README)'),
            "README.md Upgrading": (upgrading, "(#compatibility)"),
            "ja workflow.md formats": (ja_formats, "README の「互換性」"),
            "ja providers.md interface stability": (ja_stability, "（README の「互換性」）"),  # noqa: RUF001
            "README.ja.md upgrading": (ja_upgrading, "(#互換性)"),
        }
        for name, (text, pointer) in pointers.items():
            with self.subTest(document=name):
                self.assertIn(pointer, text)
        adapters = (
            ("CHANGELOG.md preamble", preamble),
            ("providers.md", stability),
            ("ja providers.md", ja_stability),
        )
        for name, text in adapters:
            with self.subTest(document=name):
                self.assertIn("User adapters", text)

    def test_no_doc_describes_the_removed_adoption(self):
        """CHANGELOG.md is left out: the released entries stay as they were."""
        stale = (
            "adopts the flat",
            "ships with a migration",
            "Adopted the previous",
            "migrate()",
            "is adopted into",
            "ワークフローに引き継がれる",
        )
        paths = [pathlib.Path(REPO_ROOT) / name for name in ("README.md", "README.ja.md", "CONTRIBUTING.md")]
        paths += sorted((pathlib.Path(REPO_ROOT) / "references").glob("*.md"))
        paths += sorted(JA_REFERENCES.glob("*.md"))
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for phrase in stale:
                with self.subTest(document=path.name, phrase=phrase):
                    self.assertNotIn(phrase, text)


class TestJapaneseReferences(unittest.TestCase):
    """A translation of every reference, which says what it was made from.

    The English files are what the skill reads and stay authoritative; the
    Japanese ones are for people. The header's sha256 is of the English file
    the translation was brought up to date with, so an English change that the
    translation has not followed fails here instead of quietly going stale.
    """

    def setUp(self):
        import sys

        scripts = str(pathlib.Path(REPO_ROOT) / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import stamp_translation

        self.stamp = stamp_translation

    def references(self):
        return sorted((pathlib.Path(REPO_ROOT) / "references").glob("*.md"))

    def test_every_reference_has_a_translation(self):
        for source in self.references():
            with self.subTest(reference=source.name):
                self.assertTrue((JA_REFERENCES / source.name).is_file(), source.name)

    def test_every_translation_is_of_the_english_as_it_is_now(self):
        for source in self.references():
            translation = JA_REFERENCES / source.name
            if not translation.is_file():
                continue
            with self.subTest(reference=source.name):
                header = self.stamp.HEADER.match(translation.read_text(encoding="utf-8"))
                self.assertIsNotNone(header, "%s has no translated-from header" % translation.name)
                assert header is not None
                self.assertEqual(header.group("source"), "references/%s" % source.name)
                self.assertEqual(
                    header.group("digest"),
                    self.stamp.digest_of(source),
                    "references/%s changed since docs/ja/references/%s was translated: bring the "
                    "translation up to date, then run `python scripts/stamp_translation.py "
                    "docs/ja/references/%s`" % (source.name, source.name, source.name),
                )

    def test_the_translations_keep_the_code_blocks_of_the_english(self):
        """Commands, config and prompt templates are not translated."""
        fence = re.compile(r"^```.*?^```\s*$", re.MULTILINE | re.DOTALL)
        for source in self.references():
            translation = JA_REFERENCES / source.name
            if not translation.is_file():
                continue
            with self.subTest(reference=source.name):
                self.assertEqual(
                    fence.findall(translation.read_text(encoding="utf-8")),
                    fence.findall(source.read_text(encoding="utf-8")),
                )

    def test_every_relative_link_in_the_translations_resolves(self):
        for translation in sorted(JA_REFERENCES.glob("*.md")):
            text = translation.read_text(encoding="utf-8")
            for target in MARKDOWN_LINK.findall(text):
                if "://" in target or target.startswith("mailto:"):
                    continue
                with self.subTest(translation=translation.name, target=target):
                    self.assertTrue((translation.parent / target).exists(), target)

    def test_every_in_page_anchor_in_the_translations_exists(self):
        anchor = re.compile(r"\]\((?P<file>[^)#\s]*)#(?P<id>[^)\s]+)\)")
        for translation in sorted(JA_REFERENCES.glob("*.md")):
            for match in anchor.finditer(translation.read_text(encoding="utf-8")):
                target = translation.parent / match.group("file") if match.group("file") else translation
                if not target.is_file() or target.parent != JA_REFERENCES:
                    continue
                with self.subTest(translation=translation.name, link=match.group(0)):
                    self.assertIn('<a id="%s"></a>' % match.group("id"), target.read_text(encoding="utf-8"))

    def test_the_japanese_readme_links_every_translation(self):
        text = (pathlib.Path(REPO_ROOT) / "README.ja.md").read_text(encoding="utf-8")
        for source in self.references():
            with self.subTest(reference=source.name):
                self.assertIn("](docs/ja/references/%s)" % source.name, text)


class TestReferenceContents(unittest.TestCase):
    """The long references open with a table of contents that matches their
    headings, in both languages, and every link in it lands on a section."""

    NAMES = ("reviews", "cli", "configuration", "limits", "workflow", "providers")

    def setUp(self):
        import sys

        scripts = str(pathlib.Path(REPO_ROOT) / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import doc_contents

        self.contents = doc_contents

    def documents(self):
        for name in self.NAMES:
            yield pathlib.Path(REPO_ROOT) / "references" / ("%s.md" % name)
            yield JA_REFERENCES / ("%s.md" % name)

    def test_the_contents_are_those_of_the_headings(self):
        for path in self.documents():
            text = path.read_text(encoding="utf-8")
            with self.subTest(document=str(path.relative_to(REPO_ROOT))):
                self.assertIn(self.contents.START, text)
                self.assertEqual(
                    self.contents.with_contents(text, self.contents.language_of(path)),
                    text,
                    "run python scripts/doc_contents.py %s" % path.relative_to(REPO_ROOT),
                )

    def test_every_link_in_the_contents_has_a_section(self):
        """Checked against what the page will have, not against the function
        that wrote the links: the explicit anchors a translation carries, and
        the English headings under GitHub's own naming."""
        for path in self.documents():
            text = path.read_text(encoding="utf-8")
            block = text.split(self.contents.START, 1)[1].split(self.contents.END, 1)[0]
            if self.contents.language_of(path) == "ja":
                anchors = set(re.findall(r'^<a id="([^"]+)"></a>$', text, re.MULTILINE))
            else:
                body = text.split(self.contents.END, 1)[1]
                anchors = {
                    self.contents.slug(title)
                    for title in re.findall(r"^#{2,3} (.+?)\s*$", body, re.MULTILINE)
                }
            for target in re.findall(r"\]\(#([^)]+)\)", block):
                with self.subTest(document=path.name, anchor=target):
                    self.assertIn(target, anchors)

    def test_every_translated_heading_carries_its_english_anchor(self):
        """Without one, its link falls back to a slug of the Japanese title and
        no longer lands where the English one does."""
        for name in self.NAMES:
            lines = (JA_REFERENCES / ("%s.md" % name)).read_text(encoding="utf-8").split("\n")
            fence = False
            for number, line in enumerate(lines):
                if line.lstrip().startswith(("```", "~~~")):
                    fence = not fence
                if fence or not re.match(r"^#{2,3} ", line):
                    continue
                with self.subTest(translation=name, heading=line):
                    self.assertRegex(lines[number - 2] if number >= 2 else "", r'^<a id="[^"]+"></a>$')


def leaf_commands() -> Dict[Tuple[str, ...], argparse.ArgumentParser]:
    """Every runnable command of the parser, as its words: ``("review", "run")``."""
    found: Dict[Tuple[str, ...], argparse.ArgumentParser] = {}

    def walk(parser: argparse.ArgumentParser, words: Tuple[str, ...]) -> None:
        groups = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
        if not groups:
            found[words] = parser
            return
        for group in groups:
            for name, child in group.choices.items():
                walk(child, (*words, name))

    walk(cli.build_parser(), ())
    return found


JSON_COMMANDS = re.compile(r"<!-- json-commands: start -->(.*?)<!-- json-commands: end -->", re.DOTALL)


class TestTheJsonCommandList(unittest.TestCase):
    """cli.md names the commands that take ``--json`` (#286); the parser decides."""

    def documented(self, relative: str) -> List[str]:
        text = (pathlib.Path(REPO_ROOT) / relative).read_text(encoding="utf-8")
        match = JSON_COMMANDS.search(text)
        assert match is not None, relative
        return re.findall(r"`([a-z -]+)`", match.group(1))

    def test_the_list_is_the_parsers(self):
        expected = sorted(
            " ".join(words)
            for words, parser in leaf_commands().items()
            if any("--json" in action.option_strings for action in parser._actions)
        )
        for relative in ("references/cli.md", "docs/ja/references/cli.md"):
            with self.subTest(file=relative):
                self.assertEqual(sorted(self.documented(relative)), expected)


class TestCliSignatures(unittest.TestCase):
    """Every flag a command takes is in its signature in cli.md (#297)."""

    ROW = re.compile(r"^\| `([^`]*)`", re.M)

    def signatures(self, relative: str) -> List[str]:
        text = (pathlib.Path(REPO_ROOT) / relative).read_text(encoding="utf-8")
        return self.ROW.findall(text)

    def test_each_signature_names_every_flag(self):
        for relative in ("references/cli.md", "docs/ja/references/cli.md"):
            signatures = self.signatures(relative)
            for words, parser in leaf_commands().items():
                command = " ".join(words)
                with self.subTest(file=relative, command=command):
                    mine = [sig for sig in signatures if re.match(r"%s(?=[ ]|$)" % re.escape(command), sig)]
                    self.assertTrue(mine, "no signature row for %s" % command)
                    joined = " ".join(mine)
                    flags = {
                        option
                        for action in parser._actions
                        if action.help != argparse.SUPPRESS
                        for option in action.option_strings
                        if option.startswith("--") and option != "--help"
                    }
                    missing = sorted(
                        flag for flag in flags if not re.search(re.escape(flag) + r"(?![A-Za-z-])", joined)
                    )
                    self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
