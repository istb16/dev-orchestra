#!/usr/bin/env python3
"""Validate the skill package: frontmatter, size, links, and version coherence.

Run standalone (``python scripts/validate_skill.py``) or via the test suite.
Exits non-zero when a problem is found.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestrator import miniyaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The skill document lives inside ``skills/`` so that Claude Code and Codex
#: both discover it from an installed plugin; Codex only scans
#: ``skills/<name>/SKILL.md`` and ignores a SKILL.md at the plugin root.
SKILL_NAME = "dev-orchestra"
SKILL_PATH = "skills/%s/SKILL.md" % SKILL_NAME
CLAUDE_PLUGIN = ".claude-plugin/plugin.json"
CLAUDE_MARKETPLACE = ".claude-plugin/marketplace.json"
CODEX_PLUGIN = ".codex-plugin/plugin.json"
CODEX_MARKETPLACE = ".agents/plugins/marketplace.json"

MAX_SKILL_LINES = 500

#: SKILL.md is resident for the whole session, in every session, so its size
#: is a running cost rather than a one-off. Lines are a poor proxy for that --
#: a table row and a paragraph cost very differently -- so the budget is in
#: characters, roughly four to a token. The ceiling has room above the current
#: document for a rule worth adding; it is not a target to grow into.
MAX_SKILL_CHARS = 12_500
MAX_DESCRIPTION_CHARS = 1024
REQUIRED_FRONTMATTER = ("name", "description")
REQUIRED_FILES = (
    SKILL_PATH,
    CLAUDE_PLUGIN,
    CLAUDE_MARKETPLACE,
    CODEX_PLUGIN,
    CODEX_MARKETPLACE,
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
    "references/limits.md",
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


def _load_json(relative: str) -> Any:
    return json.loads(_read(relative))


def _plugin_root(source: str) -> str:
    """Resolve a marketplace plugin source against the marketplace root.

    Both hosts treat the marketplace root as the directory the manifest is
    published from -- this repository root, not the folder holding the JSON.
    """
    return os.path.normpath(os.path.join(REPO_ROOT, source))


def _entry_for(marketplace: Dict[str, Any]) -> Any:
    """The marketplace's own entry for this plugin, or ``None``.

    Entries that are not objects are skipped rather than indexed into: a
    malformed manifest must produce a message, not a traceback.
    """
    entries = marketplace.get("plugins") or []
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == SKILL_NAME:
            return entry
    return None


def check_manifests(version: str = "") -> List[str]:
    """The plugin and marketplace manifests of both hosts must agree.

    Claude Code reads ``.claude-plugin/``; Codex reads ``.codex-plugin/`` plus
    a marketplace under ``.agents/plugins/``. Everything else -- skill,
    scripts, references -- is shared, so the two manifests must not drift
    apart from each other or from the skill they ship.
    """
    problems: List[str] = []

    manifests = {}
    for relative in (CLAUDE_PLUGIN, CLAUDE_MARKETPLACE, CODEX_PLUGIN, CODEX_MARKETPLACE):
        try:
            manifests[relative] = _load_json(relative)
        except OSError as exc:
            problems.append(str(exc))
        except ValueError as exc:
            problems.append("%s is not valid JSON: %s" % (relative, exc))
    if len(manifests) < 4:
        return problems

    # Everything below indexes into these; a manifest that is not an object at
    # all should be reported, not raise part way through the run.
    for relative, manifest in sorted(manifests.items()):
        if not isinstance(manifest, dict):
            problems.append("%s must contain a JSON object" % relative)
    if problems:
        return problems

    for relative in (CLAUDE_PLUGIN, CODEX_PLUGIN):
        manifest = manifests[relative]
        if manifest.get("name") != SKILL_NAME:
            problems.append("%s declares name %r; expected %r" % (relative, manifest.get("name"), SKILL_NAME))
        if version and str(manifest.get("version", "")) != version:
            problems.append(
                "version mismatch: %s says %s, %s says %s"
                % (SKILL_PATH, version, relative, manifest.get("version"))
            )
        skills = manifest.get("skills")
        if not skills:
            problems.append("%s must declare a skills path" % relative)
        elif not isinstance(skills, str):
            # Codex takes a single path string here, so an array would be
            # accepted by Claude Code and silently ignored by Codex.
            problems.append(
                "%s: skills must be a single path string, not %s" % (relative, type(skills).__name__)
            )
        elif not os.path.isdir(os.path.join(REPO_ROOT, skills.lstrip("./"))):
            problems.append("%s points at a missing skills directory: %s" % (relative, skills))

    # A SKILL.md at the plugin root would be a second copy of the skill for
    # Claude Code (which discovers both) and invisible to Codex (which only
    # scans skills/<name>/).
    if os.path.isfile(os.path.join(REPO_ROOT, "SKILL.md")):
        problems.append("SKILL.md must live at %s only" % SKILL_PATH)

    claude_market = manifests[CLAUDE_MARKETPLACE]
    if claude_market.get("name") != SKILL_NAME:
        problems.append("%s declares marketplace name %r" % (CLAUDE_MARKETPLACE, claude_market.get("name")))
    if not isinstance(claude_market.get("owner"), dict) or not claude_market["owner"].get("name"):
        problems.append("%s needs an owner with a name" % CLAUDE_MARKETPLACE)
    entry = _entry_for(claude_market)
    if entry is None:
        problems.append("%s has no entry for %s" % (CLAUDE_MARKETPLACE, SKILL_NAME))
    else:
        source = entry.get("source")
        if not isinstance(source, str):
            problems.append("%s: the entry must be sourced from this repository" % CLAUDE_MARKETPLACE)
        elif not os.path.isfile(os.path.join(_plugin_root(source), CLAUDE_PLUGIN)):
            problems.append("%s: source %r has no %s" % (CLAUDE_MARKETPLACE, source, CLAUDE_PLUGIN))
        if version and str(entry.get("version", version)) != version:
            problems.append(
                "version mismatch: %s says %s, %s entry says %s"
                % (SKILL_PATH, version, CLAUDE_MARKETPLACE, entry.get("version"))
            )

    codex_market = manifests[CODEX_MARKETPLACE]
    if codex_market.get("name") != SKILL_NAME:
        problems.append("%s declares marketplace name %r" % (CODEX_MARKETPLACE, codex_market.get("name")))
    entry = _entry_for(codex_market)
    if entry is None:
        problems.append("%s has no entry for %s" % (CODEX_MARKETPLACE, SKILL_NAME))
    else:
        source = entry.get("source")
        if not isinstance(source, dict) or source.get("source") != "local":
            problems.append("%s: the entry needs a local source object" % CODEX_MARKETPLACE)
        elif not os.path.isfile(os.path.join(_plugin_root(source.get("path", "")), CODEX_PLUGIN)):
            problems.append(
                "%s: source path %r has no %s" % (CODEX_MARKETPLACE, source.get("path"), CODEX_PLUGIN)
            )
        policy = entry.get("policy") or {}
        if policy.get("installation") not in ("NOT_AVAILABLE", "AVAILABLE", "INSTALLED_BY_DEFAULT"):
            problems.append("%s: entry needs policy.installation" % CODEX_MARKETPLACE)
        if policy.get("authentication") not in ("ON_INSTALL", "ON_USE"):
            problems.append("%s: entry needs policy.authentication" % CODEX_MARKETPLACE)
        if not entry.get("category"):
            problems.append("%s: entry needs a category" % CODEX_MARKETPLACE)

    return problems


def check() -> List[str]:
    problems: List[str] = []

    for relative in REQUIRED_FILES:
        if not os.path.isfile(os.path.join(REPO_ROOT, relative)):
            problems.append("missing required file: %s" % relative)

    try:
        text = _read(SKILL_PATH)
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
        problems.append("%s is %d lines; keep it under %d" % (SKILL_PATH, line_count, MAX_SKILL_LINES))

    if len(text) > MAX_SKILL_CHARS:
        problems.append(
            "%s is %d characters (about %d tokens); keep it under %d"
            % (SKILL_PATH, len(text), len(text) // 4, MAX_SKILL_CHARS)
        )

    # Every referenced reference file must exist.
    for match in re.findall(r"`(references/[a-z0-9_.-]+\.md)`", body):
        if not os.path.isfile(os.path.join(REPO_ROOT, match)):
            problems.append("%s references a missing file: %s" % (SKILL_PATH, match))

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
                    "version mismatch: %s says %s, %s says %s"
                    % (SKILL_PATH, declared, relative, found.group(1).strip())
                )
        if declared not in _read("CHANGELOG.md"):
            problems.append("CHANGELOG.md has no entry for version %s" % declared)

    problems.extend(check_manifests(declared))

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
