"""The per-machine record of CLI versions whose resumed sessions stay read-only."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from helpers import CLAUDE_HELP, IsolatedCase, present

from orchestrator import config, verified
from orchestrator import workspace as ws
from orchestrator.providers.claude import READ_ONLY_MECHANISM, ClaudeProvider

VERSION = "1.0.0 (Claude Code)"
MECHANISM = "--flags that hold it"


def record_pass(root, version=VERSION, mechanism=MECHANISM, checks=verified.REQUIRED_RESUME_CHECKS):
    return verified.record_pass("claude", version, mechanism, list(checks), "sonnet", root)


class TestRecord(IsolatedCase):
    def test_a_pass_reads_back(self):
        path = record_pass(self.project)
        data, problem = verified.read("claude", self.project)
        assert data is not None
        self.assertIsNone(problem)
        entry = data["versions"][VERSION]
        self.assertEqual(entry["read_only_mechanism"], MECHANISM)
        self.assertEqual(entry["checks"], list(verified.REQUIRED_RESUME_CHECKS))
        self.assertEqual(path, verified.record_path("claude"))

    def test_lookup_needs_version_mechanism_and_every_check(self):
        record_pass(self.project)
        self.assertEqual(verified.lookup("claude", VERSION, MECHANISM, self.project)["status"], "passed")
        found = verified.lookup("claude", "1.0.1 (Claude Code)", MECHANISM, self.project)
        self.assertEqual(found["status"], "absent")
        self.assertEqual(verified.lookup("claude", VERSION, "--other", self.project)["status"], "absent")
        record_pass(self.project, checks=verified.REQUIRED_RESUME_CHECKS[:-1])
        self.assertEqual(verified.lookup("claude", VERSION, MECHANISM, self.project)["status"], "absent")

    def test_a_failure_replaces_a_pass_and_back(self):
        record_pass(self.project)
        verified.record_fail("claude", VERSION, ["ignores repository hooks on resume"], self.project)
        data, _ = verified.read("claude", self.project)
        assert data is not None
        self.assertNotIn(VERSION, data["versions"])
        self.assertEqual(data["failed"][VERSION]["checks"], ["ignores repository hooks on resume"])
        self.assertEqual(verified.lookup("claude", VERSION, MECHANISM, self.project)["status"], "failed")
        record_pass(self.project)
        data, _ = verified.read("claude", self.project)
        assert data is not None
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
        self.assertIn("inside the workspace", present(problem))


class TestSmokeRecord(IsolatedCase):
    """Which CLI versions went through scripts/smoke_live.py on this machine."""

    def record(self, version, failed=(), skipped=(), at="2026-09-01T00:00:00Z"):
        with mock.patch.object(verified.ws, "utcnow", return_value=at):
            return verified.record_smoke("claude", version, list(failed), list(skipped), self.project)

    def status(self, version=VERSION):
        return verified.smoke_status("claude", version, self.project)

    def test_a_pass_reads_back(self):
        path = self.record(VERSION)
        self.assertEqual(path, verified.smoke_record_path("claude"))
        self.assertNotEqual(path, verified.record_path("claude"))
        found = self.status()
        self.assertEqual(found["status"], "passed")
        self.assertEqual(found["entry"]["checked_at"], "2026-09-01T00:00:00Z")
        self.assertEqual(found["last_passed"], {"version": VERSION, "checked_at": "2026-09-01T00:00:00Z"})
        self.assertIsNone(found["problem"])
        self.assertFalse(os.path.exists(verified.record_path("claude")))

    def test_skipped_checks_still_pass(self):
        self.record(VERSION, skipped=["stays confined (symlink)"])
        found = self.status()
        self.assertEqual(found["status"], "passed")
        self.assertEqual(found["entry"]["skipped"], ["stays confined (symlink)"])
        self.assertEqual(found["entry"]["failed"], [])

    def test_a_failure(self):
        self.record(VERSION, failed=["reports what it spent"])
        found = self.status()
        self.assertEqual(found["status"], "failed")
        self.assertFalse(found["entry"]["ok"])
        self.assertEqual(found["entry"]["failed"], ["reports what it spent"])
        self.assertIsNone(found["last_passed"])

    def test_a_newer_version_is_absent_and_names_the_last_pass(self):
        self.record("1.1.0 (Claude Code)", at="2026-08-01T00:00:00Z")
        self.record("1.2.0 (Claude Code)", at="2026-09-01T00:00:00Z")
        self.record("1.3.0 (Claude Code)", failed=["x"], at="2026-09-10T00:00:00Z")
        found = self.status()
        self.assertEqual(found["status"], "absent")
        self.assertIsNone(found["entry"])
        self.assertEqual(found["last_passed"]["version"], "1.2.0 (Claude Code)")

    def test_no_record_is_never(self):
        self.assertEqual(
            self.status(), {"status": "absent", "entry": None, "last_passed": None, "problem": None}
        )

    def test_a_record_inside_is_neither_read_nor_written(self):
        self.record(VERSION)
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(self.project, ".ai", "home")
        with self.assertRaises(verified.VerifiedRecordError):
            self.record(VERSION)
        self.assertFalse(os.path.exists(verified.smoke_record_path("claude")))
        found = self.status()
        self.assertEqual(found["status"], "absent")
        self.assertIn("inside the workspace", found["problem"])

    def test_an_unknown_schema_is_ignored(self):
        path = verified.smoke_record_path("claude")
        os.makedirs(os.path.dirname(path))
        ws.write_json(path, {"schema": 99, "versions": {VERSION: {"ok": True, "checked_at": "x"}}})
        found = self.status()
        self.assertEqual(found["status"], "absent")
        self.assertIsNone(found["last_passed"])
        self.assertTrue(found["problem"])
        self.record(VERSION)
        self.assertEqual(self.status()["status"], "passed")


class TestParseVersion(unittest.TestCase):
    def test_the_three_real_formats(self):
        self.assertEqual(verified.parse_version("2.1.285 (Claude Code)"), (2, 1, 285))
        self.assertEqual(verified.parse_version("codex-cli 0.156.1"), (0, 156, 1))
        self.assertEqual(verified.parse_version("1.2.14"), (1, 2, 14))

    def test_a_pre_release_or_garbage_does_not_parse(self):
        for text in (
            "2.1.286-beta.1 (Claude Code)",
            "codex-cli 0.157.0-alpha.2",
            "2.1.286-beta.1 (build 2026.09.30)",
            "codex-cli 0.157.0-alpha.2 1.0",
            "nightly",
            "7",
            "",
            None,
        ):
            with self.subTest(text=text):
                self.assertIsNone(verified.parse_version(text))


def claude(number):
    return "%s (Claude Code)" % number


def entry(mechanism=MECHANISM, checks=verified.REQUIRED_RESUME_CHECKS, at="2026-09-01T00:00:00Z"):
    return {"verified_at": at, "read_only_mechanism": mechanism, "checks": list(checks)}


class TestResumeTrust(IsolatedCase):
    """The rule: exact passes, then newer than a pass, unless a failure here outranks it."""

    def trust(self, number, table=None, **kwargs):
        built_in = {claude(name): value for name, value in (table or {}).items()}
        return verified.resume_trust("claude", claude(number), MECHANISM, self.project, built_in, **kwargs)

    def passed_here(self, number):
        record_pass(self.project, version=claude(number))

    def failed_here(self, number):
        verified.record_fail("claude", claude(number), ["resumes read-only"], self.project)

    def assert_newer(self, found, number, source):
        self.assertEqual(found["status"], "newer")
        self.assertEqual(found["newer_than"], claude(number))
        self.assertEqual(found["source"], source)

    def assert_blocked(self, found, number):
        self.assertEqual(found["status"], "failed")
        self.assertEqual(found["blocked_by"], claude(number))

    def test_newer_than_the_table(self):
        self.assert_newer(self.trust("2.1.286", {"2.1.285": entry()}), "2.1.285", "built-in")

    def test_newer_than_the_record(self):
        self.passed_here("2.1.285")
        found = self.trust("2.1.286")
        self.assert_newer(found, "2.1.285", "record")
        self.assertIsNotNone(found["entry"])

    def test_the_record_wins_a_tie(self):
        self.passed_here("2.1.285")
        self.assert_newer(self.trust("2.1.286", {"2.1.285": entry()}), "2.1.285", "record")

    def test_older_than_every_pass_is_absent(self):
        self.assertEqual(self.trust("2.1.284", {"2.1.285": entry()})["status"], "absent")

    def test_an_unparsable_version_is_absent(self):
        self.assertEqual(self.trust("nightly", {"2.1.285": entry()})["status"], "absent")

    def test_between_two_passes_it_is_newer_than_the_lower(self):
        found = self.trust("2.1.284", {"2.1.283": entry(), "2.1.285": entry()})
        self.assert_newer(found, "2.1.283", "built-in")

    def test_an_exact_pass_is_passed(self):
        found = self.trust("2.1.285", {"2.1.285": entry()})
        self.assertEqual(found["status"], "passed")
        self.assertEqual(found["source"], "built-in")
        self.passed_here("2.1.285")
        self.assertEqual(self.trust("2.1.285", {"2.1.285": entry()})["source"], "record")

    def test_an_exact_local_failure_wins(self):
        self.failed_here("2.1.286")
        found = self.trust("2.1.286", {"2.1.286": entry()})
        self.assertEqual(found["status"], "failed")
        self.assertIsNone(found["blocked_by"])

    def test_a_failure_above_the_last_local_pass_blocks(self):
        self.passed_here("2.1.283")
        self.failed_here("2.1.284")
        self.assert_blocked(self.trust("2.1.286", {"2.1.285": entry()}), "2.1.284")

    def test_a_failure_below_a_local_pass_is_superseded(self):
        self.failed_here("2.1.283")
        self.passed_here("2.1.284")
        self.assert_newer(self.trust("2.1.286"), "2.1.284", "record")

    def test_a_failure_at_the_tables_version_blocks(self):
        self.failed_here("2.1.285")
        self.assert_blocked(self.trust("2.1.286", {"2.1.285": entry()}), "2.1.285")

    def test_a_failure_below_the_tables_version_blocks(self):
        self.failed_here("2.1.284")
        self.assert_blocked(self.trust("2.1.286", {"2.1.285": entry()}), "2.1.284")

    def test_an_exact_built_in_version_is_blocked_by_an_older_local_failure(self):
        self.failed_here("2.1.284")
        self.assert_blocked(self.trust("2.1.285", {"2.1.285": entry()}), "2.1.284")

    def test_an_exact_local_pass_outranks_an_older_local_failure(self):
        self.failed_here("2.1.284")
        self.passed_here("2.1.285")
        found = self.trust("2.1.285", {"2.1.285": entry()})
        self.assertEqual(found["status"], "passed")
        self.assertEqual(found["source"], "record")

    def test_a_failure_newer_than_the_current_version_does_not_block(self):
        self.failed_here("2.1.287")
        self.assert_newer(self.trust("2.1.286", {"2.1.285": entry()}), "2.1.285", "built-in")

    def test_an_unparsable_local_failure_blocks(self):
        verified.record_fail("claude", "nightly", ["resumes read-only"], self.project)
        found = self.trust("2.1.286", {"2.1.285": entry()})
        self.assertEqual(found["status"], "failed")
        self.assertEqual(found["blocked_by"], "nightly")

    def test_an_unparsable_version_with_a_local_failure_fails(self):
        self.failed_here("2.1.284")
        found = self.trust("nightly", {"2.1.285": entry()})
        self.assertEqual(found["status"], "failed")
        # It cannot be ordered, so no failure is named as lying below it.
        self.assertTrue(found["unordered"])
        self.assertIsNone(found["blocked_by"])
        detail = ClaudeProvider().resume_report(claude("nightly"), found)["detail"]
        self.assertIn("cannot be ordered against the versions checked here", detail)
        self.assertNotIn("2.1.284", detail)

    def test_a_pass_vouches_for_its_own_major_version_only(self):
        table = {"2.1.285": entry()}
        self.assert_newer(self.trust("2.9.0", table), "2.1.285", "built-in")
        self.assertEqual(self.trust("3.0.0", table)["status"], "absent")
        self.passed_here("2.1.285")
        self.assertEqual(self.trust("3.0.0")["status"], "absent")
        self.assert_newer(self.trust("3.0.1", {"3.0.0": entry(), "2.9.9": entry()}), "3.0.0", "built-in")

    def test_a_malformed_failure_entry_trusts_only_an_exact_built_in_version(self):
        self.passed_here("2.1.285")
        path = verified.record_path("claude")
        data = ws.read_json(path)
        data["failed"] = {claude("2.1.284"): "resumes read-only"}
        ws.write_json(path, data)
        table = {"2.1.285": entry()}
        found = self.trust("2.1.286", table)
        self.assertEqual(found["status"], "absent")
        self.assertIn("malformed failure entry", found["problem"])
        self.assertEqual(self.trust("2.1.285", table)["source"], "built-in")
        data["failed"] = ["2.1.284"]
        ws.write_json(path, data)
        self.assertEqual(self.trust("2.1.286", table)["status"], "absent")

    def test_a_stale_mechanism_is_not_a_candidate(self):
        found = self.trust("2.1.286", {"2.1.285": entry(mechanism="--other")})
        self.assertEqual(found["status"], "absent")
        found = self.trust("2.1.286", {"2.1.284": entry(), "2.1.285": entry(mechanism="--other")})
        self.assert_newer(found, "2.1.284", "built-in")

    def test_an_adapters_own_required_checks(self):
        table = {"2.1.285": entry(checks=["forks the session"])}
        self.assertEqual(self.trust("2.1.286", table)["status"], "absent")
        found = self.trust("2.1.286", table, required=("forks the session",))
        self.assert_newer(found, "2.1.285", "built-in")

    def test_a_record_inside_the_workspace_trusts_only_an_exact_built_in_version(self):
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(self.project, ".ai", "home")
        table = {"2.1.285": entry()}
        found = self.trust("2.1.285", table)
        self.assertEqual(found["status"], "passed")
        self.assertEqual(found["source"], "built-in")
        found = self.trust("2.1.286", table)
        self.assertEqual(found["status"], "absent")
        self.assertIn("inside the workspace", found["problem"])


class _Completed:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


class TestClaudeReadsTheRecord(IsolatedCase):
    def provider(self):
        provider = ClaudeProvider()
        setattr(provider, "_capture", lambda command, timeout=30: _Completed(CLAUDE_HELP))
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
