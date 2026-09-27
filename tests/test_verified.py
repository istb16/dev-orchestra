"""The per-machine record of CLI versions whose resumed sessions stay read-only."""

from __future__ import annotations

import os
import unittest

from helpers import CLAUDE_HELP, IsolatedCase

from orchestrator import config, verified
from orchestrator.providers.claude import READ_ONLY_MECHANISM, ClaudeProvider

VERSION = "9.9.9 (Claude Code)"
MECHANISM = "--flags that hold it"


def record_pass(root, version=VERSION, mechanism=MECHANISM, checks=verified.REQUIRED_RESUME_CHECKS):
    return verified.record_pass("claude", version, mechanism, list(checks), "sonnet", root)


class TestRecord(IsolatedCase):
    def test_a_pass_reads_back(self):
        path = record_pass(self.project)
        data, problem = verified.read("claude", self.project)
        self.assertIsNone(problem)
        entry = data["versions"][VERSION]
        self.assertEqual(entry["read_only_mechanism"], MECHANISM)
        self.assertEqual(entry["checks"], list(verified.REQUIRED_RESUME_CHECKS))
        self.assertEqual(path, verified.record_path("claude"))

    def test_lookup_needs_version_mechanism_and_every_check(self):
        record_pass(self.project)
        self.assertEqual(verified.lookup("claude", VERSION, MECHANISM, self.project)["status"], "passed")
        found = verified.lookup("claude", "1.0.0 (Claude Code)", MECHANISM, self.project)
        self.assertEqual(found["status"], "absent")
        self.assertEqual(verified.lookup("claude", VERSION, "--other", self.project)["status"], "absent")
        record_pass(self.project, checks=verified.REQUIRED_RESUME_CHECKS[:-1])
        self.assertEqual(verified.lookup("claude", VERSION, MECHANISM, self.project)["status"], "absent")

    def test_a_failure_replaces_a_pass_and_back(self):
        record_pass(self.project)
        verified.record_fail("claude", VERSION, ["ignores repository hooks on resume"], self.project)
        data, _ = verified.read("claude", self.project)
        self.assertNotIn(VERSION, data["versions"])
        self.assertEqual(data["failed"][VERSION]["checks"], ["ignores repository hooks on resume"])
        self.assertEqual(verified.lookup("claude", VERSION, MECHANISM, self.project)["status"], "failed")
        record_pass(self.project)
        data, _ = verified.read("claude", self.project)
        self.assertNotIn(VERSION, data["failed"])
        self.assertEqual(verified.lookup("claude", VERSION, MECHANISM, self.project)["status"], "passed")

    def test_a_broken_record_is_a_problem_not_a_pass(self):
        path = verified.record_path("claude")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        data, problem = verified.read("claude", self.project)
        self.assertIsNone(data)
        self.assertTrue(problem)
        found = verified.lookup("claude", VERSION, MECHANISM, self.project)
        self.assertEqual(found["status"], "absent")
        self.assertTrue(found["problem"])

    def test_no_record_is_no_problem(self):
        self.assertEqual(verified.read("claude", self.project), (None, None))


class TestRecordPath(IsolatedCase):
    def test_it_follows_the_config_home_only(self):
        self.assertTrue(verified.record_path("claude").startswith(config.global_config_dir()))
        other = os.path.join(self.tmp, "elsewhere")
        os.environ["DEV_ORCHESTRA_HOME"] = other
        expected = os.path.join(other, "verified", "claude-resume.json")
        self.assertEqual(verified.record_path("claude"), expected)
        before = verified.record_path("claude")
        os.environ["DEV_ORCHESTRA_CONFIG"] = os.path.join(self.project, "config.yaml")
        os.chdir(self.tmp)
        self.assertEqual(verified.record_path("claude"), before)


class TestOutside(IsolatedCase):
    def test_outside_and_inside(self):
        self.assertTrue(verified.outside(os.path.join(self.config_home, "x.json"), self.project))
        self.assertFalse(verified.outside(self.project, self.project))
        self.assertFalse(verified.outside(os.path.join(self.project, "sub", "x.json"), self.project))

    def test_a_relative_home_resolves_inside(self):
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(".ai", "home")
        self.assertFalse(verified.outside(verified.record_path("claude"), self.project))

    def test_a_symlink_into_the_workspace_is_inside(self):
        link = os.path.join(self.tmp, "link")
        try:
            os.symlink(self.project, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks cannot be created here")
        self.assertFalse(verified.outside(os.path.join(link, "x.json"), self.project))

    def test_a_record_inside_is_neither_read_nor_written(self):
        record_pass(self.project)
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(self.project, ".ai", "home")
        with self.assertRaises(verified.VerifiedRecordError):
            record_pass(self.project)
        with self.assertRaises(verified.VerifiedRecordError):
            verified.record_fail("claude", VERSION, ["x"], self.project)
        self.assertFalse(os.path.exists(verified.record_path("claude")))
        data, problem = verified.read("claude", self.project)
        self.assertIsNone(data)
        self.assertIn("inside the workspace", problem)


class _Completed:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


class TestClaudeReadsTheRecord(IsolatedCase):
    def provider(self):
        provider = ClaudeProvider()
        provider._capture = lambda command, timeout=30: _Completed(CLAUDE_HELP)
        provider.version = lambda: (VERSION, None)
        return provider

    def test_a_record_inside_the_workspace_is_ignored(self):
        inside = os.path.join(self.project, ".ai", "home")
        path = os.path.join(inside, "verified", "claude-resume.json")
        os.makedirs(os.path.dirname(path))
        from orchestrator import workspace as ws

        ws.write_json(
            path,
            {
                "schema": verified.SCHEMA,
                "provider": "claude",
                "versions": {
                    VERSION: {
                        "verified_at": "2026-09-27T00:00:00Z",
                        "read_only_mechanism": READ_ONLY_MECHANISM,
                        "checks": list(verified.REQUIRED_RESUME_CHECKS),
                    }
                },
                "failed": {},
            },
        )
        os.environ["DEV_ORCHESTRA_HOME"] = inside
        support = self.provider().resume_support(self.project)
        self.assertEqual(support["status"], "unverified")
        self.assertIn("inside the workspace", support["detail"])

        os.environ["DEV_ORCHESTRA_HOME"] = self.config_home
        record_pass(self.project, mechanism=READ_ONLY_MECHANISM)
        self.assertEqual(self.provider().resume_support(self.project)["status"], "verified")


if __name__ == "__main__":
    unittest.main()
