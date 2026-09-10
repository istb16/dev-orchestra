"""Plugin packaging: both hosts must find the same skill and the same scripts.

Claude Code and Codex install a plugin by copying the repository into their own
cache, so anything the skill reaches for has to live inside this directory and
be reachable from the manifests -- not from a checkout path that only exists on
a developer's machine.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import unittest

from helpers import REPO_ROOT, IsolatedCase

validate_skill = importlib.import_module("validate_skill")

SKILL_PATH = validate_skill.SKILL_PATH


def read(relative):
    with open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


def load(relative):
    return json.loads(read(relative))


def skill_version():
    """Read the version rather than pinning it: releases bump it everywhere."""
    front, _ = validate_skill.parse_frontmatter(read(SKILL_PATH))
    return str(front["version"])


class TestManifests(IsolatedCase):
    def test_the_shipped_manifests_validate(self):
        self.assertEqual(validate_skill.check_manifests(skill_version()), [])

    def test_every_manifest_is_json(self):
        for relative in (
            validate_skill.CLAUDE_PLUGIN,
            validate_skill.CLAUDE_MARKETPLACE,
            validate_skill.CODEX_PLUGIN,
            validate_skill.CODEX_MARKETPLACE,
        ):
            self.assertIsInstance(load(relative), dict, relative)

    def test_both_hosts_declare_the_same_plugin(self):
        claude = load(validate_skill.CLAUDE_PLUGIN)
        codex = load(validate_skill.CODEX_PLUGIN)
        for field in ("name", "version", "description"):
            self.assertEqual(claude[field], codex[field], field)

    def test_the_version_matches_the_skill(self):
        for relative in (validate_skill.CLAUDE_PLUGIN, validate_skill.CODEX_PLUGIN):
            self.assertEqual(load(relative)["version"], skill_version(), relative)

    def test_a_version_drift_is_reported(self):
        problems = validate_skill.check_manifests("9.9.9")
        self.assertTrue(problems)
        self.assertTrue(any("version mismatch" in problem for problem in problems), problems)

    def test_marketplaces_point_at_this_repository_root(self):
        claude_entry = load(validate_skill.CLAUDE_MARKETPLACE)["plugins"][0]
        self.assertEqual(claude_entry["source"], "./")
        codex_entry = load(validate_skill.CODEX_MARKETPLACE)["plugins"][0]
        self.assertEqual(codex_entry["source"], {"source": "local", "path": "./"})

    def test_neither_marketplace_is_an_official_one(self):
        """The issue is explicit: distribution is from this repository only."""
        for relative in (validate_skill.CLAUDE_MARKETPLACE, validate_skill.CODEX_MARKETPLACE):
            self.assertEqual(load(relative)["name"], "dev-orchestra", relative)


class TestMalformedManifests(IsolatedCase):
    """A broken manifest must be reported, not raise part way through."""

    def override(self, relative, payload):
        original = validate_skill._read

        def fake(path):
            if path == relative:
                return json.dumps(payload)
            return original(path)

        validate_skill._read = fake
        self.addCleanup(setattr, validate_skill, "_read", original)

    def test_a_manifest_that_is_not_an_object_is_reported(self):
        self.override(validate_skill.CODEX_PLUGIN, ["not", "an", "object"])
        problems = validate_skill.check_manifests()
        self.assertIn("%s must contain a JSON object" % validate_skill.CODEX_PLUGIN, problems)

    def test_a_skills_array_is_reported(self):
        """Claude Code would accept it; Codex takes a single path string."""
        manifest = load(validate_skill.CODEX_PLUGIN)
        manifest["skills"] = ["./skills/"]
        self.override(validate_skill.CODEX_PLUGIN, manifest)
        problems = validate_skill.check_manifests()
        self.assertTrue(any("single path string" in problem for problem in problems), problems)

    def test_a_malformed_marketplace_entry_is_reported(self):
        marketplace = load(validate_skill.CODEX_MARKETPLACE)
        marketplace["plugins"] = ["dev-orchestra"]
        self.override(validate_skill.CODEX_MARKETPLACE, marketplace)
        problems = validate_skill.check_manifests()
        self.assertIn("%s has no entry for dev-orchestra" % validate_skill.CODEX_MARKETPLACE, problems)

    def test_invalid_json_is_reported(self):
        original = validate_skill._read

        def fake(path):
            if path == validate_skill.CLAUDE_PLUGIN:
                return "{not json"
            return original(path)

        validate_skill._read = fake
        self.addCleanup(setattr, validate_skill, "_read", original)
        problems = validate_skill.check_manifests()
        self.assertTrue(any("is not valid JSON" in problem for problem in problems), problems)


class TestSkillDiscovery(IsolatedCase):
    def test_the_skill_lives_where_both_hosts_look(self):
        """Codex only scans skills/<name>/SKILL.md; a root SKILL.md is invisible."""
        self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, SKILL_PATH)))
        self.assertEqual(SKILL_PATH, "skills/dev-orchestra/SKILL.md")
        self.assertFalse(os.path.isfile(os.path.join(REPO_ROOT, "SKILL.md")))

    def test_the_skill_directory_matches_the_frontmatter_name(self):
        front, _ = validate_skill.parse_frontmatter(read(SKILL_PATH))
        self.assertEqual(front["name"], os.path.basename(os.path.dirname(SKILL_PATH)))

    def test_the_declared_skills_path_contains_it(self):
        for relative in (validate_skill.CLAUDE_PLUGIN, validate_skill.CODEX_PLUGIN):
            declared = load(relative)["skills"].lstrip("./").rstrip("/")
            self.assertTrue(SKILL_PATH.startswith(declared + "/"), relative)


class TestInstalledLayout(IsolatedCase):
    """Everything the skill invokes must be inside the installed plugin."""

    def test_the_skill_only_references_paths_inside_the_plugin(self):
        _, body = validate_skill.parse_frontmatter(read(SKILL_PATH))
        for match in re.findall(r"PLUGIN_ROOT/([A-Za-z0-9_./-]+)", body):
            self.assertTrue(os.path.exists(os.path.join(REPO_ROOT, match)), match)

    def test_the_skill_names_the_plugin_root_variable(self):
        text = read(SKILL_PATH)
        self.assertIn("CLAUDE_PLUGIN_ROOT", text)
        self.assertNotIn("SKILL_DIR", text)

    def test_the_helper_cli_runs_from_a_copy_of_the_repository(self):
        """A plugin install is a copy: no path may resolve back to a checkout."""
        import shutil
        import subprocess
        import sys

        version = skill_version()
        copy = os.path.join(self.tmp, "plugin-cache", "dev-orchestra", version)
        shutil.copytree(
            REPO_ROOT,
            copy,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".ai", "tests"),
        )
        result = subprocess.run(
            [sys.executable, os.path.join(copy, "scripts", "dev_orchestra.py"), "--version"],
            capture_output=True,
            text=True,
            cwd=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(version, result.stdout)

    def test_the_copying_installer_ships_the_manifests(self):
        for relative in ("install/install.sh", "install/install.ps1"):
            script = read(relative)
            for item in ("skills", ".claude-plugin", ".codex-plugin"):
                self.assertIn(item, script, "%s: %s" % (relative, item))


class TestPluginDocumentation(IsolatedCase):
    def test_both_readmes_document_both_plugin_installs(self):
        for relative in ("README.md", "README.ja.md"):
            text = read(relative)
            self.assertIn("/plugin marketplace add istb16/dev-orchestra", text, relative)
            self.assertIn("codex plugin marketplace add istb16/dev-orchestra", text, relative)


if __name__ == "__main__":
    unittest.main()
