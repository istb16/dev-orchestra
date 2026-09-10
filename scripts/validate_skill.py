#!/usr/bin/env python3
"""Validate the skill package: frontmatter, size, links, and version coherence.

Run standalone (``python scripts/validate_skill.py``) or via the test suite.
Exits non-zero when a problem is found.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestrator import miniyaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAX_SKILL_LINES = 500
MAX_DESCRIPTION_CHARS = 1024
REQUIRED_FRONTMATTER = ("name", "description")
REQUIRED_FILES = (
    "SKILL.md",
    "README.md",
    "README.ja.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    ".gitignore",
    "scripts/dev_orchestra.py",
    "references/architecture.md",
    "references/configuration.md",
    "references/providers.md",
    "references/workflow.md",
    "references/reviews.md",
    "references/cli.md",
    "examples/config.example.yaml",
    "install/install.sh",
    "install/install.ps1",
)

#: Dated snapshot ids must never appear in the skill's defaults or docs as if
#: they were configuration. Matches things like "claude-opus-5-20260101".
SNAPSHOT_RE = re.compile(r"\b[a-z][a-z0-9.\-]*-\d{6,}\b")


def _read(path: str) -> str:
    with open(os.path.join(REPO_ROOT, path), "r", encoding="utf-8") as handle:
        return handle.read()


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    if not text.startswith("---"):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    end = text.find("\n---", 3)
    if end == -1:
        raise ValueError("SKILL.md frontmatter is not terminated")
    raw = text[3:end].strip("\n")
    body = text[end + 4 :]
    data = miniyaml.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("SKILL.md frontmatter must be a mapping")
    return data, body


def check() -> List[str]:
    problems: List[str] = []

    for relative in REQUIRED_FILES:
        if not os.path.isfile(os.path.join(REPO_ROOT, relative)):
            problems.append("missing required file: %s" % relative)

    try:
        text = _read("SKILL.md")
    except OSError as exc:
        return [*problems, str(exc)]

    try:
        front, body = parse_frontmatter(text)
    except ValueError as exc:
        return [*problems, str(exc)]

    for key in REQUIRED_FRONTMATTER:
        if not front.get(key):
            problems.append("SKILL.md frontmatter is missing %r" % key)

    name = str(front.get("name", ""))
    if name and not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
        problems.append("skill name %r should be lowercase-with-hyphens" % name)

    description = str(front.get("description", ""))
    if len(description) > MAX_DESCRIPTION_CHARS:
        problems.append(
            "description is %d chars; keep it under %d" % (len(description), MAX_DESCRIPTION_CHARS)
        )
    lowered = description.lower()
    for trigger in ("implement", "review", "orchestrat", "configur"):
        if trigger not in lowered:
            problems.append("description should mention %r so the skill triggers" % trigger)
    if " not for " not in lowered and "not for" not in lowered:
        problems.append("description should say when NOT to use the skill")

    line_count = len(text.splitlines())
    if line_count > MAX_SKILL_LINES:
        problems.append("SKILL.md is %d lines; keep it under %d" % (line_count, MAX_SKILL_LINES))

    # Every referenced reference file must exist.
    for match in re.findall(r"`(references/[a-z0-9_.-]+\.md)`", body):
        if not os.path.isfile(os.path.join(REPO_ROOT, match)):
            problems.append("SKILL.md references a missing file: %s" % match)

    # Versions must agree across the places that declare one.
    declared = str(front.get("version", ""))
    if declared:
        for relative, pattern in (
            ("scripts/orchestrator/__init__.py", r'__version__ = "([^"]+)"'),
            ("scripts/orchestrator/cli.py", r'__version__ = "([^"]+)"'),
            ("agents/openai.yaml", r"^version: (.+)$"),
        ):
            try:
                found = re.search(pattern, _read(relative), re.MULTILINE)
            except OSError:
                continue
            if found and found.group(1).strip() != declared:
                problems.append(
                    "version mismatch: SKILL.md says %s, %s says %s"
                    % (declared, relative, found.group(1).strip())
                )
        if declared not in _read("CHANGELOG.md"):
            problems.append("CHANGELOG.md has no entry for version %s" % declared)

    # The translated README must not silently drift out of the doc set.
    try:
        english, japanese = _read("README.md"), _read("README.ja.md")
    except OSError:
        english = japanese = ""
    if english and japanese:
        if "README.ja.md" not in english or "README.md" not in japanese:
            problems.append("README.md and README.ja.md must link to each other")
        for anchor in ("mermaid", "dev-orchestra config setup", "MIT"):
            if anchor not in japanese:
                problems.append("README.ja.md is missing %r" % anchor)

    # No dated model snapshots anywhere in the skill's own defaults.
    for relative in ("scripts/orchestrator/config.py", "examples/config.example.yaml"):
        try:
            content = _read(relative)
        except OSError:
            continue
        for candidate in SNAPSHOT_RE.findall(content):
            if candidate.startswith(("claude-", "gpt-", "o1-", "o3-", "gemini-")):
                problems.append("%s hard-codes a dated model id: %s" % (relative, candidate))

    return problems


def main() -> int:
    problems = check()
    if problems:
        sys.stderr.write("Skill validation failed:\n")
        for problem in problems:
            sys.stderr.write("  - %s\n" % problem)
        return 1
    print("Skill validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
