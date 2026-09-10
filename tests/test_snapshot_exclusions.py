"""Withholding generated and vendored files from the review diff.

A reviewer reads a diff to judge code somebody wrote. A lockfile, a bundle and
a recorded snapshot were not written, cost the same tokens as real code, and
are sent once per reviewer and once per round -- so a routine dependency bump
regularly costs more than the change it accompanies.

The risk being tested against is the obvious one: an exclusion that hides real
work. Every test here is therefore about the *visibility* of what was withheld
as much as the saving. Withheld is not hidden -- the file is still named to the
reviewer, with how many lines changed, and one flag brings it back in full.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git

from orchestrator import cli
from orchestrator import config as config_mod
from orchestrator import review as review_mod
from orchestrator import workspace as ws


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


LOCKFILE = json.dumps({"packages": {"p%d" % i: {"version": "1.0.%d" % i} for i in range(200)}}, indent=2)
LOCKFILE_BUMPED = json.dumps(
    {"packages": {"p%d" % i: {"version": "2.0.%d" % i} for i in range(200)}}, indent=2
)


# --------------------------------------------------------------------------- matching


class TestPatternMatching(unittest.TestCase):
    def test_a_pattern_without_a_slash_matches_at_any_depth(self):
        """``*.lock`` has to catch a lockfile wherever it lives."""
        self.assertEqual(review_mod.withholds("Gemfile.lock", ["*.lock"]), "*.lock")
        self.assertEqual(review_mod.withholds("api/Gemfile.lock", ["*.lock"]), "*.lock")

    def test_a_pattern_with_a_slash_is_matched_against_the_whole_path(self):
        self.assertEqual(review_mod.withholds("dist/app.js", ["dist/*"]), "dist/*")
        self.assertFalse(review_mod.withholds("src/dist_helper.py", ["dist/*"]))

    def test_a_nested_directory_needs_its_own_pattern(self):
        """The defaults spell out both forms rather than implying a ``**``."""
        self.assertFalse(review_mod.withholds("packages/a/dist/x.js", ["dist/*"]))
        self.assertEqual(review_mod.withholds("packages/a/dist/x.js", ["*/dist/*"]), "*/dist/*")

    def test_matching_is_case_sensitive_on_every_platform(self):
        """A snapshot taken on Windows must contain what Linux would produce."""
        self.assertFalse(review_mod.withholds("APP.MIN.JS", ["*.min.js"]))

    def test_the_first_matching_pattern_is_the_one_reported(self):
        self.assertEqual(review_mod.withholds("x.lock", ["*.lock", "x.lock"]), "*.lock")

    def test_no_patterns_withholds_nothing(self):
        self.assertFalse(review_mod.withholds("package-lock.json", []))

    def test_junk_in_the_pattern_list_is_skipped_not_raised_on(self):
        self.assertEqual(review_mod.withholds("a.lock", [None, "", 7, "*.lock"]), "*.lock")

    def test_source_files_are_never_withheld_by_the_defaults(self):
        for path in ("app.py", "src/main.rs", "lib/dist_utils.go", "build_config.py", "app/vendor.ts"):
            self.assertFalse(review_mod.withholds(path, review_mod.DEFAULT_EXCLUDE), path)

    def test_the_defaults_leave_out_anything_ambiguous(self):
        """``build/`` is hand-written often enough that hiding it is worse than
        paying for it. Silently dropping real work is the failure to avoid."""
        self.assertFalse(review_mod.withholds("build/main.c", review_mod.DEFAULT_EXCLUDE))


class TestNumstatParsing(unittest.TestCase):
    def test_a_plain_change(self):
        self.assertEqual(review_mod._parse_numstat("3\t1\tapp.py\0"), [(3, 1, "app.py")])

    def test_a_binary_file_has_no_counts_and_is_not_counted_as_zero(self):
        self.assertEqual(review_mod._parse_numstat("-\t-\tlogo.png\0"), [(None, None, "logo.png")])

    def test_a_rename_reports_the_new_path(self):
        """The readable form is ``src/{old => new}.py``, which cannot be
        unpicked for a path containing a brace. The NUL form can."""
        self.assertEqual(
            review_mod._parse_numstat("0\t0\t\0src/old.py\0src/new.py\0"), [(0, 0, "src/new.py")]
        )

    def test_records_after_a_rename_are_still_read(self):
        parsed = review_mod._parse_numstat("0\t0\t\0a.py\0b.py\0" + "5\t2\tc.py\0")
        self.assertEqual(parsed, [(0, 0, "b.py"), (5, 2, "c.py")])

    def test_empty_output_is_no_records_rather_than_an_error(self):
        self.assertEqual(review_mod._parse_numstat(""), [])


class TestWithheldRendering(unittest.TestCase):
    def test_the_note_names_the_file_and_its_size(self):
        note = review_mod.render_withheld(
            [{"path": "package-lock.json", "pattern": "*", "added": 400, "deleted": 12}]
        )
        self.assertIn("package-lock.json", note)
        self.assertIn("+400 -12", note)

    def test_a_binary_file_says_binary_rather_than_a_made_up_count(self):
        note = review_mod.render_withheld([{"path": "logo.png", "added": None, "deleted": None}])
        self.assertIn("binary", note)

    def test_nothing_withheld_produces_no_note(self):
        self.assertEqual(review_mod.render_withheld([]), "")

    def test_an_uncounted_file_makes_the_total_a_lower_bound(self):
        line = review_mod.withheld_lines([{"added": 10, "deleted": 2}, {"added": None, "deleted": None}])
        self.assertEqual(line, "12+")


# --------------------------------------------------------------------------- snapshot


@unittest.skipUnless(has_git(), "git is required for review snapshots")
class TestSnapshotExclusions(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.write("package-lock.json", LOCKFILE)
        self.write("dist/bundle.min.js", "var a=1;\n")
        self.commit_all("init")
        self.workspace = ws.Workspace(self.project).ensure()

    def change_everything(self):
        self.write("app.py", "def add(a, b):\n    return a - b\n")
        self.write("package-lock.json", LOCKFILE_BUMPED)
        self.write("dist/bundle.min.js", "var a=2;var b=3;\n")

    def test_the_withheld_body_is_absent_but_the_source_diff_is_there(self):
        self.change_everything()
        review_mod.create_snapshot(self.workspace)
        diff = ws.read_text(self.workspace.snapshot_path)
        self.assertIn("return a - b", diff)
        self.assertNotIn("2.0.1", diff)
        self.assertNotIn("var b=3", diff)

    def test_the_withheld_file_is_still_named_with_its_line_counts(self):
        """Withheld is not hidden: a review that turns on a dependency version
        has to be able to go and read it."""
        self.change_everything()
        meta = review_mod.create_snapshot(self.workspace)
        withheld = {entry["path"]: entry for entry in meta["withheld"]}
        self.assertEqual(sorted(withheld), ["dist/bundle.min.js", "package-lock.json"])
        self.assertEqual(withheld["package-lock.json"]["added"], 200)
        self.assertEqual(withheld["package-lock.json"]["pattern"], "package-lock.json")

    def test_the_saving_is_the_point_of_the_exercise(self):
        self.change_everything()
        withheld = review_mod.create_snapshot(self.workspace)
        everything = review_mod.create_snapshot(self.workspace, exclude=())
        self.assertLess(withheld["bytes"] * 10, everything["bytes"])

    def test_passing_no_patterns_reviews_everything(self):
        self.change_everything()
        meta = review_mod.create_snapshot(self.workspace, exclude=())
        self.assertEqual(meta["withheld"], [])
        self.assertIn("package-lock.json", meta["files"])
        self.assertIn("2.0.1", ws.read_text(self.workspace.snapshot_path))

    def test_the_patterns_in_force_are_recorded_with_the_snapshot(self):
        """A snapshot has to be able to explain itself after the fact."""
        self.change_everything()
        meta = review_mod.create_snapshot(self.workspace, exclude=["*.json"])
        self.assertEqual(meta["exclude_patterns"], ["*.json"])

    def test_an_untracked_generated_file_is_withheld_too(self):
        self.write("yarn.lock", "# lots of resolved versions\n" * 50)
        meta = review_mod.create_snapshot(self.workspace)
        self.assertIn("yarn.lock", [entry["path"] for entry in meta["withheld"]])
        self.assertNotIn("yarn.lock", meta["untracked_included"])
        self.assertNotIn("resolved versions", ws.read_text(self.workspace.snapshot_path))

    def test_an_untracked_source_file_is_still_included(self):
        self.write("new_module.py", "print('hi')\n")
        meta = review_mod.create_snapshot(self.workspace)
        self.assertIn("new_module.py", meta["untracked_included"])
        self.assertEqual(meta["withheld"], [])

    def test_a_change_that_is_entirely_generated_is_empty_but_explains_itself(self):
        """An empty snapshot for this reason is not the same as no changes."""
        self.write("package-lock.json", LOCKFILE_BUMPED)
        meta = review_mod.create_snapshot(self.workspace)
        self.assertTrue(meta["empty"])
        self.assertTrue(meta["withheld"])
        with self.assertRaises(review_mod.ReviewError) as ctx:
            review_mod.run_reviews([{"id": "r", "provider": "mock"}], self.workspace)
        self.assertIn("withheld", str(ctx.exception))
        self.assertIn("--no-exclude", str(ctx.exception))

    def test_a_binary_file_is_withheld_without_inventing_a_line_count(self):
        self.write("logo.min.js", "\x00\x01binary-ish\x00")
        self.commit_all("add binary")
        with open(os.path.join(self.project, "logo.min.js"), "wb") as handle:
            handle.write(b"\x00\x02changed\x00")
        meta = review_mod.create_snapshot(self.workspace)
        entry = next(e for e in meta["withheld"] if e["path"] == "logo.min.js")
        self.assertIsNone(entry["added"])

    def test_a_moved_file_is_a_rename_rather_than_a_delete_plus_an_add(self):
        """Rename detection is a saving in its own right: a moved file costs a
        header instead of twice its length.

        It needs the move to be staged. An unstaged ``mv`` leaves git with a
        deletion and an untracked file, which are two unrelated facts as far as
        ``git diff`` is concerned -- so that case still costs full price, and
        this asserts the case that can be improved rather than the one that
        cannot.
        """
        self.git("mv", "app.py", "renamed.py")
        review_mod.create_snapshot(self.workspace)
        diff = ws.read_text(self.workspace.snapshot_path)
        self.assertIn("rename from app.py", diff)
        self.assertNotIn("-def add(a, b):", diff)

    def test_the_reviewer_prompt_carries_the_withheld_list(self):
        self.change_everything()
        review_mod.create_snapshot(self.workspace)
        prompt = review_mod.build_review_prompt(
            {"id": "r1", "role": "general"},
            self.workspace,
            ws.read_text(self.workspace.snapshot_path),
        )
        self.assertIn("package-lock.json", prompt)
        self.assertIn("withheld", prompt)
        # The note is a list of names, not an essay: it rides along on every
        # reviewer prompt in every round.
        self.assertLess(len(review_mod.render_withheld([{"path": "a.lock", "added": 1, "deleted": 1}])), 200)

    def test_a_snapshot_with_nothing_withheld_says_nothing_about_it(self):
        self.write("app.py", "def add(a, b):\n    return a * b\n")
        review_mod.create_snapshot(self.workspace)
        prompt = review_mod.build_review_prompt(
            {"id": "r1", "role": "general"},
            self.workspace,
            ws.read_text(self.workspace.snapshot_path),
        )
        self.assertNotIn("withheld", prompt)


# --------------------------------------------------------------------------- config


class TestExcludeConfiguration(IsolatedCase):
    def test_the_default_configuration_ships_the_patterns(self):
        data = config_mod.default_config()
        self.assertIn("package-lock.json", data["review"]["exclude"])
        self.assertEqual(config_mod.validate(data), [])

    def test_a_project_can_replace_the_list_wholesale(self):
        """Lists replace rather than merge, so a project can opt out with []."""
        merged = config_mod.deep_merge(config_mod.default_config(), {"review": {"exclude": []}})
        self.assertEqual(merged["review"]["exclude"], [])
        self.assertEqual(config_mod.validate(merged), [])

    def test_a_malformed_pattern_list_is_a_configuration_error(self):
        data = config_mod.default_config()
        data["review"]["exclude"] = "*.lock"
        self.assertTrue(any("review.exclude" in problem for problem in config_mod.validate(data)))

    def test_a_blank_pattern_is_refused_rather_than_matching_everything(self):
        data = config_mod.default_config()
        data["review"]["exclude"] = ["  "]
        self.assertTrue(any("review.exclude[0]" in problem for problem in config_mod.validate(data)))


@unittest.skipUnless(has_git(), "git is required for review snapshots")
class TestSnapshotCommand(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.write("package-lock.json", LOCKFILE)
        self.commit_all("init")
        run_cli("config", "setup", "--defaults")
        self.write("app.py", "def add(a, b):\n    return a - b\n")
        self.write("package-lock.json", LOCKFILE_BUMPED)

    def test_the_command_names_what_it_withheld(self):
        code, out, _ = run_cli("review", "snapshot")
        self.assertEqual(code, 0)
        self.assertIn("withheld", out)
        self.assertIn("package-lock.json", out)
        self.assertIn("--no-exclude", out)

    def test_no_exclude_sends_everything(self):
        _, withheld, _ = run_cli("review", "snapshot", "--json")
        _, everything, _ = run_cli("review", "snapshot", "--no-exclude", "--json")
        self.assertEqual(json.loads(everything)["withheld"], [])
        self.assertLess(json.loads(withheld)["bytes"] * 10, json.loads(everything)["bytes"])

    def test_the_configured_list_is_what_the_command_applies(self):
        run_cli("config", "set", "--raw", "review.exclude", "[]")
        payload = json.loads(run_cli("review", "snapshot", "--json")[1])
        self.assertEqual(payload["withheld"], [])

    def test_review_run_snapshots_with_the_same_exclusions(self):
        """The implicit snapshot must not review more than the explicit one."""
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        run_cli("review", "run")
        meta = ws.read_json(ws.Workspace(self.project).snapshot_meta_path, {})
        self.assertIn("package-lock.json", [entry["path"] for entry in meta["withheld"]])


if __name__ == "__main__":
    unittest.main()
