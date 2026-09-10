"""The skill package itself: frontmatter, docs, installers, portability."""

from __future__ import annotations

import importlib
import os
import re
import unittest

from helpers import REPO_ROOT, IsolatedCase

from orchestrator import miniyaml

# Imported by name rather than with a plain `import`: it lives in scripts/,
# which only goes on sys.path when helpers is imported above. An import
# sorter would happily move a normal import statement ahead of that.
validate_skill = importlib.import_module("validate_skill")

# Assembled at run time so this file does not trip its own check.
_LOCAL_PATH_RE = re.compile(r"[Cc]:[\\/]" + "Use" + r"rs[\\/]|/ho" + r"me/[a-z]")


def _has_local_path(content):
    return bool(_LOCAL_PATH_RE.search(content))


def read(relative):
    with open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


class TestValidator(IsolatedCase):
    def test_the_shipped_skill_validates(self):
        self.assertEqual(validate_skill.check(), [])

    def test_validator_rejects_missing_frontmatter(self):
        with self.assertRaises(ValueError):
            validate_skill.parse_frontmatter("# No frontmatter here\n")

    def test_validator_rejects_unterminated_frontmatter(self):
        with self.assertRaises(ValueError):
            validate_skill.parse_frontmatter("---\nname: x\n")


class TestSkillDocument(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.text = read("SKILL.md")
        self.front, self.body = validate_skill.parse_frontmatter(self.text)

    def test_frontmatter_fields(self):
        self.assertEqual(self.front["name"], "ai-dev-orchestrator")
        self.assertEqual(self.front["license"], "MIT")
        self.assertTrue(self.front["description"])

    def test_description_covers_the_intended_triggers(self):
        description = self.front["description"].lower()
        for phrase in ("implement", "investigate", "review", "orchestrat", "configur", "model"):
            self.assertIn(phrase, description)

    def test_description_scopes_out_casual_questions(self):
        self.assertIn("not for", self.front["description"].lower())

    def test_stays_under_the_size_budget(self):
        self.assertLess(len(self.text.splitlines()), validate_skill.MAX_SKILL_LINES)

    def test_states_the_non_negotiable_rules(self):
        body = self.body.lower()
        for rule in (
            "never hard-code a dated model id",
            "reviewers are read-only",
            "fix only triaged-accepted findings",
            "never print or store credentials",
        ):
            self.assertIn(rule, body)

    def test_documents_stage_selection_both_ways(self):
        self.assertIn("Skip", self.body)
        self.assertIn("**Run**", self.body)

    def test_every_referenced_document_exists(self):
        for match in re.findall(r"`(references/[a-z0-9_.-]+\.md)`", self.body):
            self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, match)), match)

    def test_no_dated_model_ids_in_the_skill(self):
        for candidate in validate_skill.SNAPSHOT_RE.findall(self.text):
            self.assertFalse(candidate.startswith(("claude-", "gpt-")), candidate)


class TestDocumentation(IsolatedCase):
    def test_readme_covers_every_required_section(self):
        readme = read("README.md")
        for heading in (
            "Why this exists",
            "Architecture",
            "Requirements",
            "Installation",
            "Initial setup",
            "Usage",
            "Configuration",
            "Model selection",
            "Reviewers",
            "Example workflows",
            "Troubleshooting",
            "Security",
            "Supported platforms",
            "Upgrading",
            "Uninstalling",
            "Versioning",
            "Contributing",
            "License",
        ):
            self.assertIn(heading, readme, heading)

    def test_readme_has_an_architecture_diagram(self):
        self.assertIn("```mermaid", read("README.md"))

    def test_example_config_parses_and_is_valid(self):
        from orchestrator import config as config_mod

        data = miniyaml.loads(read("examples/config.example.yaml"))
        self.assertEqual(config_mod.validate(data), [])

    def test_example_project_override_parses(self):
        data = miniyaml.loads(read("examples/project-override.example.yaml"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["reviewers"]), 3)

    def test_agent_manifest_parses_and_points_at_skill_md(self):
        data = miniyaml.loads(read("agents/openai.yaml"))
        self.assertEqual(data["name"], "ai-dev-orchestrator")
        self.assertEqual(data["instructions"]["file"], "../SKILL.md")

    def test_changelog_documents_the_current_version(self):
        front, _ = validate_skill.parse_frontmatter(read("SKILL.md"))
        self.assertIn(str(front["version"]), read("CHANGELOG.md"))

    def test_cli_reference_documents_every_top_level_command(self):
        from orchestrator import cli

        reference = read("references/cli.md")
        parser = cli.build_parser()
        actions = [action for action in parser._actions if hasattr(action, "choices") and action.choices]
        commands = [c for action in actions for c in action.choices]
        for command in commands:
            self.assertIn(command, reference, command)


class TestPortability(IsolatedCase):
    def test_skill_md_is_not_duplicated_anywhere(self):
        """Installers must point at SKILL.md, never copy its content."""
        marker = "You are the **Orchestrator**"
        hits = []
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", ".ai")]
            for name in filenames:
                if not name.endswith((".md", ".yaml", ".yml", ".sh", ".ps1")):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as handle:
                    if marker in handle.read():
                        hits.append(os.path.relpath(path, REPO_ROOT))
        self.assertEqual(hits, ["SKILL.md"])

    def test_installers_exist_for_both_hosts_and_platforms(self):
        for relative in (
            "install/install.sh",
            "install/install.ps1",
            "install/uninstall.sh",
            "install/uninstall.ps1",
            "bin/ai-orchestrator",
            "bin/ai-orchestrator.ps1",
        ):
            self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, relative)), relative)

    def test_posix_installer_handles_both_hosts(self):
        script = read("install/install.sh")
        self.assertIn("--codex", script)
        self.assertIn(".claude/skills", script)
        self.assertIn("BEGIN", script)  # idempotent marked block

    def test_installers_keep_a_project_install_out_of_the_host_repo(self):
        for relative in ("install/install.sh", "install/install.ps1"):
            self.assertIn("info/exclude", read(relative), relative)

    def test_entry_point_needs_no_third_party_packages(self):
        """The whole package must import with only the standard library."""
        import subprocess
        import sys

        code = (
            "import sys; sys.path.insert(0, %r);"
            "import orchestrator.cli, orchestrator.review, orchestrator.wizard,"
            "orchestrator.doctor, orchestrator.providers;"
            "print('ok')" % os.path.join(REPO_ROOT, "scripts")
        )
        result = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gitignore_keeps_local_config_and_artifacts_out(self):
        ignore = read(".gitignore")
        for pattern in (".ai/", ".ai-orchestrator.yaml", "__pycache__/"):
            self.assertIn(pattern, ignore)

    def test_repository_contains_no_absolute_local_paths(self):
        """Nothing machine-specific should be committable."""
        offenders = []
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", ".ai", ".tmpcfg")]
            for name in filenames:
                if not name.endswith((".py", ".md", ".yaml", ".yml", ".sh", ".ps1", ".json")):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
                if _has_local_path(content):
                    offenders.append(os.path.relpath(path, REPO_ROOT))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
