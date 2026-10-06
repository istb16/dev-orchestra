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
import shutil
import unittest
from typing import Any, Dict, List

from helpers import REPO_ROOT, IsolatedCase, is_link, make_dir_link, remove_link

validate_skill = importlib.import_module("validate_skill")

SKILL_PATH = validate_skill.SKILL_PATH

_NOT_COPIED = shutil.ignore_patterns(".git", "__pycache__", ".ai", "tests")


def ignore_for_copy(directory, names):
    """What a plugin cache copy of the tree leaves out, and every link in it."""
    ignored = set(_NOT_COPIED(directory, names))
    return ignored | {name for name in names if is_link(os.path.join(directory, name))}


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

    def test_the_shipped_root_manifest_validates(self):
        self.assertEqual(validate_skill.check_antigravity(), [])
        manifest = load(validate_skill.ANTIGRAVITY_PLUGIN)
        self.assertLessEqual(set(manifest), validate_skill.ANTIGRAVITY_FIELDS)
        self.assertEqual(manifest["$schema"], validate_skill.ANTIGRAVITY_SCHEMA)

    def test_every_manifest_is_json(self):
        for relative in (
            validate_skill.CLAUDE_PLUGIN,
            validate_skill.CLAUDE_MARKETPLACE,
            validate_skill.CODEX_PLUGIN,
            validate_skill.CODEX_MARKETPLACE,
            validate_skill.ANTIGRAVITY_PLUGIN,
        ):
            self.assertIsInstance(load(relative), dict, relative)

    def test_both_hosts_declare_the_same_plugin(self):
        """All three: Claude Code, Codex and Antigravity, whose schema has no version."""
        claude = load(validate_skill.CLAUDE_PLUGIN)
        for relative, fields in (
            (validate_skill.CODEX_PLUGIN, ("name", "version", "description")),
            (validate_skill.ANTIGRAVITY_PLUGIN, ("name", "description")),
        ):
            other = load(relative)
            for field in fields:
                self.assertEqual(claude[field], other[field], "%s: %s" % (relative, field))

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


class TestClaudeHooks(IsolatedCase):
    """The reply-language hooks reach Claude Code only, through its manifest (#254)."""

    def test_claude_manifest_points_at_hooks_file(self):
        self.assertEqual(load(validate_skill.CLAUDE_PLUGIN)["hooks"], "./" + validate_skill.CLAUDE_HOOKS)
        self.assertEqual(validate_skill.check_claude_hooks(), [])
        hooks = load(validate_skill.CLAUDE_HOOKS)["hooks"]
        self.assertEqual(sorted(hooks), ["SessionStart", "Stop", "UserPromptSubmit"])
        self.assertEqual(hooks["SessionStart"][0]["matcher"], "compact|resume")
        for groups in hooks.values():
            for entry in groups[0]["hooks"]:
                self.assertTrue(entry["command"].startswith('sh "${CLAUDE_PLUGIN_ROOT}/hooks/run" '))

    def test_no_root_hooks_json(self):
        """Antigravity loads a root hooks.json by itself; Codex is not pointed at any."""
        self.assertFalse(os.path.lexists(os.path.join(REPO_ROOT, "hooks.json")))
        self.assertFalse(os.path.lexists(os.path.join(REPO_ROOT, "hooks", "hooks.json")))
        self.assertNotIn("hooks", load(validate_skill.CODEX_PLUGIN))
        self.assertNotIn("hooks", load(validate_skill.ANTIGRAVITY_PLUGIN))

    def check_with(self, **files: Any) -> List[str]:
        """``check_claude_hooks`` with ``manifest`` and ``hooks`` read as given:
        a dict or list as JSON, a str as written, None as a missing file."""
        original = validate_skill._read
        paths = {"manifest": validate_skill.CLAUDE_PLUGIN, "hooks": validate_skill.CLAUDE_HOOKS}
        replaced = {paths[key]: value for key, value in files.items()}

        def fake(path):
            if path not in replaced:
                return original(path)
            value = replaced[path]
            if value is None:
                raise FileNotFoundError(path)
            return value if isinstance(value, str) else json.dumps(value)

        setattr(validate_skill, "_read", fake)
        try:
            return validate_skill.check_claude_hooks()
        finally:
            setattr(validate_skill, "_read", original)

    def test_a_hook_that_skips_the_wrapper_is_reported(self):
        hooks = load(validate_skill.CLAUDE_HOOKS)
        hooks["hooks"]["Stop"][0]["hooks"][0]["command"] = 'python "${CLAUDE_PLUGIN_ROOT}/scripts/hooks/x.py"'
        expected = "hooks/claude-code.json: every Stop hook must run ${CLAUDE_PLUGIN_ROOT}/hooks/run"
        self.assertEqual(self.check_with(hooks=hooks), [expected])

    def test_each_broken_manifest_entry_is_reported(self):
        manifest = load(validate_skill.CLAUDE_PLUGIN)
        plugin = validate_skill.CLAUDE_PLUGIN
        for label, data, message in (
            (
                "no hooks key",
                {key: value for key, value in manifest.items() if key != "hooks"},
                "%s must point at hooks/claude-code.json with a hooks path" % plugin,
            ),
            (
                "a list",
                dict(manifest, hooks=["./hooks/claude-code.json"]),
                "%s: hooks must be a single path string, not list" % plugin,
            ),
            (
                "another path",
                dict(manifest, hooks="./hooks/other.json"),
                "%s: hooks points at './hooks/other.json'; expected ./hooks/claude-code.json" % plugin,
            ),
        ):
            self.assertEqual(self.check_with(manifest=data), [message], label)

    def test_each_broken_hooks_file_is_reported(self):
        relative = validate_skill.CLAUDE_HOOKS
        good = load(relative)["hooks"]
        self.assertEqual(
            self.check_with(hooks=None),
            ["%s points at a missing hooks file: ./%s" % (validate_skill.CLAUDE_PLUGIN, relative)],
        )
        problems = self.check_with(hooks="{not json")
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("%s is not valid JSON: " % relative), problems)
        empty = "%s must hold a non-empty hooks object" % relative
        for label, data in (("no events", {"hooks": {}}), ("not a mapping", {"hooks": []}), ("a list", [])):
            self.assertEqual(self.check_with(hooks=data), [empty], label)
        stop = "%s: every Stop hook must run ${CLAUDE_PLUGIN_ROOT}/hooks/run" % relative
        for label, groups in (
            ("groups not a list", {"hooks": []}),
            ("a group not a mapping", ["x"]),
            ("entries not a list", [{"hooks": "x"}]),
            ("an entry not a mapping", [{"hooks": ["x"]}]),
            ("a command not a string", [{"hooks": [{"type": "command", "command": 1}]}]),
        ):
            self.assertEqual(self.check_with(hooks={"hooks": dict(good, Stop=groups)}), [stop], label)

    @unittest.skipIf(os.name == "nt", "POSIX sh and PATH semantics")
    def test_hooks_run_wrapper_exits_0_without_python(self):
        import subprocess

        empty = os.path.join(self.tmp, "empty-bin")
        os.makedirs(empty)
        sh = shutil.which("sh") or "/bin/sh"
        result = subprocess.run(
            [sh, os.path.join(REPO_ROOT, "hooks", "run"), "stop"],
            input=b'{"hook_event_name": "Stop"}',
            capture_output=True,
            env={"PATH": empty},
            timeout=60,
        )
        self.assertEqual((result.returncode, result.stdout), (0, b""))

    def run_wrapper(self, path: str, home: str) -> Any:
        """``sh hooks/run prompt`` on PATH ``path``, in a session that typed the skill."""
        import subprocess

        sh = shutil.which("sh") or "/bin/sh"
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s",
            "cwd": self.project,
            "prompt": "/dev-orchestra:dev-orchestra go",
        }
        env = {"PATH": path, "HOME": home, "DEV_ORCHESTRA_HOME": self.config_home}
        return subprocess.run(
            [sh, os.path.join(REPO_ROOT, "hooks", "run"), "prompt"],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            env=env,
            cwd=self.project,
            timeout=60,
        )

    def write_reply_language(self, tag: str) -> None:
        from orchestrator import config as config_mod

        config_mod.write_config_file(
            config_mod.global_config_path(), {"version": 1, "language": {"reply": tag}}, "global"
        )

    @unittest.skipIf(os.name == "nt", "POSIX sh and PATH semantics")
    def test_hooks_run_wrapper_forwards_the_event_and_stdin(self):
        import sys

        self.write_reply_language("ko")
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        os.symlink(sys.executable, os.path.join(bin_dir, "python3"))
        result = self.run_wrapper(bin_dir, self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("language.reply: ko.", output["hookSpecificOutput"]["additionalContext"])

    @unittest.skipIf(os.name == "nt", "POSIX sh and PATH semantics")
    def test_hooks_run_wrapper_skips_a_python3_that_is_too_old(self):
        import sys

        self.write_reply_language("ja")
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        old = os.path.join(bin_dir, "python3")
        with open(old, "w", encoding="utf-8", newline="\n") as handle:
            # Fails the version probe, and would print if the wrapper ran it anyway.
            handle.write("#!/bin/sh\necho old-python\nexit 1\n")
        os.chmod(old, 0o755)
        os.symlink(sys.executable, os.path.join(bin_dir, "python"))
        result = self.run_wrapper(bin_dir, self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(b"old-python", result.stdout)
        self.assertIn("Japanese", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_the_wrapper_keeps_lf_endings(self):
        with open(os.path.join(REPO_ROOT, "hooks", "run"), "rb") as handle:
            self.assertNotIn(b"\r\n", handle.read())
        self.assertIn("hooks/run text eol=lf", read(".gitattributes"))


class TestMalformedManifests(IsolatedCase):
    """A broken manifest must be reported, not raise part way through."""

    def override(self, relative, payload):
        original = validate_skill._read

        def fake(path):
            if path == relative:
                return json.dumps(payload)
            return original(path)

        setattr(validate_skill, "_read", fake)
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

        setattr(validate_skill, "_read", fake)
        self.addCleanup(setattr, validate_skill, "_read", original)
        problems = validate_skill.check_manifests()
        self.assertTrue(any("is not valid JSON" in problem for problem in problems), problems)

    def test_a_broken_root_manifest_hides_nothing_else(self):
        """Antigravity's manifest is checked on its own, so a broken one
        leaves the Claude and Codex problems reported, and the other way round."""
        manifest = load(validate_skill.CODEX_PLUGIN)
        manifest["version"] = "9.9.9"
        self.override(validate_skill.CODEX_PLUGIN, manifest)
        broken = os.path.join(self.tmp, "plugin.json")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        # An absolute name wins over the root it is joined to.
        self.addCleanup(setattr, validate_skill, "ANTIGRAVITY_PLUGIN", validate_skill.ANTIGRAVITY_PLUGIN)
        setattr(validate_skill, "ANTIGRAVITY_PLUGIN", broken)

        expected = validate_skill.check_manifests(skill_version())
        self.assertTrue(expected)
        problems = validate_skill.check()
        for problem in expected:
            self.assertIn(problem, problems)
        self.assertTrue(any("is not valid JSON" in problem for problem in problems), problems)


class TestAntigravityManifest(IsolatedCase):
    """``check_antigravity`` against a throwaway plugin root."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "plugin")
        os.makedirs(os.path.join(self.root, os.path.dirname(SKILL_PATH)))
        with open(os.path.join(self.root, SKILL_PATH), "w", encoding="utf-8") as handle:
            handle.write("---\nname: dev-orchestra\n---\n")
        self.manifest: Dict[str, Any] = {
            "$schema": validate_skill.ANTIGRAVITY_SCHEMA,
            "name": "dev-orchestra",
            "description": "d",
        }

    def check(self, text=None):
        if text is None:
            text = json.dumps(self.manifest)
        with open(os.path.join(self.root, "plugin.json"), "w", encoding="utf-8") as handle:
            handle.write(text)
        return validate_skill.check_antigravity(root=self.root)

    def touch(self, relative, directory=False):
        path = os.path.join(self.root, relative)
        if directory:
            os.makedirs(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{}\n")

    def assert_reported(self, problems, fragment):
        self.assertTrue(any(fragment in problem for problem in problems), problems)

    def test_a_minimal_root_passes(self):
        self.assertEqual(self.check(), [])

    def test_an_undocumented_key_is_named(self):
        self.manifest["author"] = {"name": "someone"}
        self.assert_reported(self.check(), "'author' is not a field Antigravity reads")

    def test_a_wrong_name_is_reported(self):
        self.manifest["name"] = "orchestra"
        self.assert_reported(self.check(), "declares name 'orchestra'")

    def test_a_missing_name_is_reported_as_required(self):
        del self.manifest["name"]
        self.assert_reported(self.check(), "name is required")

    def test_a_name_outside_the_pattern_is_reported(self):
        self.manifest["name"] = "dev orchestra"
        self.assert_reported(self.check(), validate_skill.ANTIGRAVITY_NAME_RE.pattern)

    def test_a_name_with_a_trailing_newline_is_outside_the_pattern(self):
        self.manifest["name"] = "dev-orchestra\n"
        self.assert_reported(self.check(), validate_skill.ANTIGRAVITY_NAME_RE.pattern)

    def test_another_schema_is_reported(self):
        self.manifest["$schema"] = "https://example.invalid/plugin.json"
        self.assert_reported(self.check(), "$schema is")

    def test_a_manifest_without_schema_passes(self):
        del self.manifest["$schema"]
        self.assertEqual(self.check(), [])

    def test_a_version_is_not_in_the_schema(self):
        self.manifest["version"] = "1.0.0"
        self.assert_reported(self.check(), "'version' is not a field Antigravity reads")

    def test_a_comment_is_not_json(self):
        """Antigravity reads JSONC; this file is kept to strict JSON."""
        self.assert_reported(self.check('// the manifest\n{"name": "dev-orchestra"}\n'), "is not valid JSON")

    def test_a_manifest_that_is_not_an_object_is_reported(self):
        self.assert_reported(self.check("[]"), "must contain a JSON object")

    def test_the_skill_must_be_where_antigravity_looks(self):
        os.remove(os.path.join(self.root, SKILL_PATH))
        self.assert_reported(self.check(), "%s is missing" % SKILL_PATH)

    def test_each_entry_antigravity_would_load_is_reported(self):
        for entry in validate_skill.ANTIGRAVITY_AUTOLOAD:
            with self.subTest(entry=entry):
                self.touch(entry, directory=entry == "rules")
                self.assert_reported(self.check(), "%s would be auto-loaded" % entry)

    def test_an_agent_file_is_reported_and_a_yaml_one_is_not(self):
        self.touch("agents/openai.yaml")
        self.assertEqual(self.check(), [])
        self.touch("agents/x.md")
        self.assert_reported(self.check(), "agents/x.md would be auto-loaded")

    def test_an_upper_case_agent_file_is_reported(self):
        """The PowerShell installer's *.md filter refuses it on Windows."""
        self.touch("agents/x.MD")
        self.assert_reported(self.check(), "agents/x.MD would be auto-loaded")

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "needs a POSIX user that mode 000 stops")
    def test_an_unreadable_agents_directory_is_reported(self):
        agents = os.path.join(self.root, "agents")
        os.makedirs(agents)
        os.chmod(agents, 0)
        try:
            self.assert_reported(self.check(), "cannot list agents/")
        finally:
            # Before tearDown removes the tree.
            os.chmod(agents, 0o755)


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
        import subprocess
        import sys

        version = skill_version()
        copy = os.path.join(self.tmp, "plugin-cache", "dev-orchestra", version)
        shutil.copytree(REPO_ROOT, copy, ignore=ignore_for_copy)
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
            for item in ("plugin.json", "skills", ".claude-plugin", ".codex-plugin"):
                self.assertIn(item, script, "%s: %s" % (relative, item))

    def test_the_copy_does_not_follow_a_link_in_the_tree(self):
        """A project install made in the checkout is a link back into it."""
        tree = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(tree, "real"))
        link = os.path.join(tree, "loop")
        make_dir_link(link, tree)
        try:
            copy = os.path.join(self.tmp, "copy")
            shutil.copytree(tree, copy, ignore=ignore_for_copy)
            self.assertEqual(os.listdir(copy), ["real"])
        finally:
            remove_link(link)


class TestPluginDocumentation(IsolatedCase):
    def test_both_readmes_document_every_plugin_install(self):
        for relative, untrusted in (
            ("README.md", "untrusted branch"),
            ("README.ja.md", "信頼できないブランチ"),
        ):
            text = read(relative)
            self.assertIn("/plugin marketplace add istb16/dev-orchestra", text, relative)
            self.assertIn("codex plugin marketplace add istb16/dev-orchestra", text, relative)
            self.assertIn("install.sh --antigravity", text, relative)
            self.assertIn("install.ps1 -Antigravity", text, relative)
            self.assertIn(untrusted, text, relative)

    def test_the_other_antigravity_routes_and_their_caveat_are_documented(self):
        for relative in ("README.md", "README.ja.md", "references/workflow.md"):
            text = read(relative)
            self.assertIn("agy plugin install", text, relative)
            self.assertIn("plugins.json", text, relative)
        # Neither reference may drop that doctor does not look at plugins.json.
        for relative, caveat in (
            ("references/workflow.md", "checks only the two installer locations, not `plugins.json`"),
            ("references/cli.md", "registered through a `plugins.json` entry"),
        ):
            self.assertIn(caveat, read(relative), relative)
        self.assertIn("is not checked by `doctor`", read("references/cli.md"))

    def test_the_checkout_install_covers_antigravity(self):
        for relative in ("references/workflow.md", "docs/ja/references/workflow.md"):
            text = read(relative)
            self.assertIn("**Antigravity:**", text, relative)
            self.assertIn("--antigravity --project", text, relative)


if __name__ == "__main__":
    unittest.main()
