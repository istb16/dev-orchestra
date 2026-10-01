"""`config suggest-roles`: path-scoped specialists proposed from a repository's files.

No model is asked. The only subprocess is one `git ls-files -z` inside a git
repository, and nothing is written without `--write`.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Iterable, Optional
from unittest import mock

from helpers import IsolatedCase, has_git, user_adapter_source

from orchestrator import cli, presets, suggest
from orchestrator import config as config_mod
from orchestrator import optimization as opt_mod
from orchestrator import workspace as ws
from orchestrator.review_common import DEFAULT_EXCLUDE


def run_cli(*argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def listing(paths, exclude=DEFAULT_EXCLUDE, workspace_dir=".ai"):
    return suggest.build_listing("/repo", "git", paths, exclude, workspace_dir)


def reasons(skipped):
    return {entry.role: entry.reason for entry in skipped}


NO_DEPS: frozenset[str] = frozenset()


def proposed(paths, panel=(), deps: Optional[Iterable[str]] = NO_DEPS, labels=()):
    """``{role: patterns}`` for ``paths``, and the skip reasons; ``deps=None`` is an unread package.json."""
    package_deps = None if deps is None else set(deps)
    found, skipped = suggest.suggest(listing(paths), list(panel), package_deps, labels)
    return {s.role: s.paths for s in found}, reasons(skipped)


#: Enough unrelated files that no small signal is most of the repository.
FILLER = ["lib/mod%d.py" % index for index in range(20)]

SVELTEKIT = [
    "src/routes/+page.svelte",
    "src/routes/+page.ts",
    "src/routes/+layout.ts",
    "src/routes/+page.server.ts",
]


class TestRules(unittest.TestCase):
    def test_database_from_a_migrations_directory(self):
        found, _ = proposed(["db/migrations/001.py", "db/migrations/002.py", *FILLER])
        self.assertEqual(found["database"], ["migrations/*", "*/migrations/*"])

    def test_one_sql_file_is_not_enough_and_two_are(self):
        found, skipped = proposed(["a.sql", *FILLER])
        self.assertNotIn("database", found)
        self.assertIn("no signal", skipped["database"])
        found, _ = proposed(["a.sql", "b/c.sql", *FILLER])
        self.assertEqual(found["database"], ["*.sql"])

    def test_a_directory_keeps_the_case_it_is_written_in(self):
        found, _ = proposed(["Migrations/1.cs", "Migrations/2.cs", *FILLER])
        self.assertEqual(found["database"], ["Migrations/*", "*/Migrations/*"])

    def test_two_casings_give_two_pairs(self):
        paths = ["a/migrations/1.py", "a/migrations/2.py", "b/Migrations/1.cs", "b/Migrations/2.cs"]
        found, _ = proposed([*paths, *FILLER])
        self.assertEqual(
            found["database"], ["migrations/*", "*/migrations/*", "Migrations/*", "*/Migrations/*"]
        )

    def test_schema_prisma_in_the_case_found(self):
        found, _ = proposed(["Schema.prisma", *FILLER])
        self.assertEqual(found["database"], ["Schema.prisma"])

    def test_each_sql_casing_is_a_pattern(self):
        found, _ = proposed(["a.sql", "b.SQL", *FILLER])
        self.assertEqual(found["database"], ["*.sql", "*.SQL"])

    def test_frontend_from_component_files(self):
        found, _ = proposed(["web/App.tsx", "web/Nav.vue", *FILLER])
        self.assertEqual(found["frontend"], ["*.tsx", "*.vue"])

    def test_dependencies_with_no_component_files_are_a_skip_with_a_reason(self):
        found, skipped = proposed(FILLER, deps={"react", "lodash"})
        self.assertNotIn("frontend", found)
        self.assertIn("package.json lists react", skipped["frontend"])

    def test_dependencies_are_evidence_beside_component_files(self):
        found, _ = suggest.suggest(listing(["a.jsx", "b.jsx", *FILLER]), [], {"next"})
        self.assertIn("package.json lists next", found[0].evidence)

    def test_express_style_server_and_routes_are_backend(self):
        paths = ["server/index.js", "server/app.js", "routes/users.js", "routes/items.js", *FILLER]
        found, _ = proposed(paths)
        self.assertEqual(found["backend"], ["server/*", "*/server/*", "routes/*", "*/routes/*"])

    def test_backend_names_root_manifests_in_the_evidence_only(self):
        found, _ = suggest.suggest(listing(["api/a.go", "api/b.go", "go.mod", *FILLER]), [], set())
        self.assertEqual(found[0].paths, ["api/*", "*/api/*"])
        self.assertIn("at the root: go.mod", found[0].evidence)

    def test_a_sveltekit_tree_is_not_backend(self):
        for deps in (set(), {"@sveltejs/kit"}):
            with self.subTest(deps=deps):
                found, skipped = proposed([*SVELTEKIT, *FILLER], deps=deps)
                self.assertNotIn("backend", found)
                self.assertIn("no signal", skipped["backend"])

    def test_routes_is_not_backend_under_sveltekit_or_remix(self):
        paths = ["app/routes/a.ts", "app/routes/b.ts", *FILLER]
        self.assertIn("backend", proposed(paths)[0])
        for deps in ({"@sveltejs/kit"}, {"@remix-run/node"}):
            with self.subTest(deps=deps):
                self.assertNotIn("backend", proposed(paths, deps=deps)[0])

    def test_routes_is_not_backend_when_package_json_was_ignored_and_there_is_a_frontend(self):
        paths = ["app/routes/a.ts", "app/routes/b.ts", "ui/A.tsx", "ui/B.tsx", *FILLER]
        self.assertIn("backend", proposed(paths, deps=set())[0])
        found, _ = proposed(paths, deps=None)
        self.assertNotIn("backend", found)
        self.assertIn("frontend", found)

    def test_same_named_directories_of_one_file_each_do_not_add_up(self):
        paths = ["a/migrations/1.py", "b/migrations/2.py", "x/api/a.go", "y/api/b.go", *FILLER]
        found, skipped = proposed(paths)
        self.assertNotIn("database", found)
        self.assertNotIn("backend", found)
        self.assertIn("no signal", skipped["database"])
        self.assertIn("no signal", skipped["backend"])

    def test_a_kept_directory_sharing_a_dropped_ones_name_is_named_by_its_path(self):
        # src/routes/api/ is SvelteKit's; backend/api/ is a backend's.
        paths = ["backend/api/a.py", "backend/api/b.py", "src/routes/api/+server.ts"]
        paths += ["src/routes/api/x/+server.ts", *SVELTEKIT, *FILLER]
        found, _ = proposed(paths, deps={"@sveltejs/kit"})
        self.assertEqual(found["backend"], ["backend/*", "*/backend/*", "backend/api/*"])
        hits = opt_mod.high_risk_matches(["src/routes/api/+server.ts"], found["backend"])
        self.assertEqual(hits, [])

    def test_tests_give_no_evidence(self):
        found, _ = proposed(["tests/a.sql", "tests/b.sql", "spec/api/x.rb", "spec/api/y.rb", *FILLER])
        self.assertEqual(found, {})

    def test_patterns_matching_most_files_are_skipped(self):
        found, skipped = proposed(["a.sql", "b.sql", "c.sql", "README.md"])
        self.assertNotIn("database", found)
        self.assertEqual(
            skipped["database"], "would join most rounds (75% of files match), which keeps the panel whole"
        )

    def test_a_role_on_the_panel_is_skipped_and_named(self):
        on_panel = config_mod.make_reviewer("claude-database", "claude", "sonnet", "database")
        for label in ("fit", "global extra", "project extra"):
            with self.subTest(label=label):
                found, skipped = proposed(["a.sql", "b.sql", *FILLER], panel=[on_panel], labels=[label])
                self.assertNotIn("database", found)
                self.assertEqual(skipped["database"], "already on the panel: claude-database (%s)" % label)

    def test_too_many_name_variants_is_a_skip(self):
        # Seven spellings of `migrations`, two patterns each.
        names = ["migrations", "Migrations", "MIGRATIONS", "mIgrations", "miGrations", "migRations"]
        names.append("migrAtions")
        paths = ["%s/%d.py" % (name, index) for name in names for index in (1, 2)]
        paths += FILLER * 3
        found, skipped = proposed(paths)
        self.assertNotIn("database", found)
        self.assertEqual(skipped["database"], "too many name variants (14 patterns)")

    def test_the_roles_it_never_proposes_are_named(self):
        _, skipped = proposed(FILLER)
        for role in ("general", "security", "architecture", "performance", "test"):
            self.assertIn("not a path specialist", skipped[role])

    def test_ids_are_unique_against_the_panel_and_each_other(self):
        panel = [config_mod.make_reviewer("claude-database", "claude", "opus", "general")]
        found, _ = suggest.suggest(listing(["a.sql", "b.sql", "x/A.tsx", "x/B.tsx", *FILLER]), panel, set())
        made = suggest.reviewers_for(found, panel, "claude", "sonnet")
        self.assertEqual([r["id"] for r in made], ["claude-database-2", "claude-frontend"])
        self.assertEqual(made[0]["when"], {"paths": ["*.sql"]})
        self.assertEqual(made[0]["model"], {"family": "sonnet", "version": "latest"})


class TestEvidence(unittest.TestCase):
    def test_generated_trees_give_no_evidence(self):
        paths = ["generated/schema.sql", "generated/b.sql", "src/__generated__/a.tsx", "gen/b.tsx", *FILLER]
        found, _ = proposed(paths)
        self.assertEqual(found, {})

    def test_a_withheld_file_is_not_evidence_but_is_counted(self):
        exclude = [*DEFAULT_EXCLUDE, "seed/*"]
        paths = ["a.sql", "b.sql", "seed/big.sql", *FILLER]
        found, _ = suggest.suggest(listing(paths, exclude), [], set())
        self.assertEqual(found[0].evidence, ["*.sql: 2 files, e.g. a.sql, b.sql"])
        self.assertEqual((found[0].matches, found[0].withheld_matches), (3, 1))
        # What a run would match on: the withheld path is in condition_paths.
        hits = opt_mod.high_risk_matches(["seed/big.sql"], found[0].paths)
        self.assertEqual(hits, [("seed/big.sql", "*.sql")])

    def test_a_withheld_file_counts_toward_the_share(self):
        exclude = [*DEFAULT_EXCLUDE, "seed/*"]
        paths = ["a.sql", "b.sql", *("seed/%d.sql" % i for i in range(5)), "x.py", "y.py"]
        _, skipped = suggest.suggest(listing(paths, exclude), [], set())
        self.assertIn("78% of files match", reasons(skipped)["database"])

    def test_the_orchestrators_own_files_are_not_listed(self):
        built = listing([".ai/workflows/x/a.sql", ".dev-orchestra.yaml", "sub/.dev-orchestra.yml", "a.py"])
        self.assertEqual(built.listed, ["a.py"])

    def test_an_absolute_workspace_dir_inside_the_root(self):
        root = os.path.abspath("/repo")
        work = os.path.join(root, "work")
        built = suggest.build_listing(root, "git", ["work/a.sql", "a.py"], DEFAULT_EXCLUDE, work)
        self.assertEqual(built.listed, ["a.py"])

    def test_the_evidence_cap(self):
        with mock.patch.object(suggest, "EVIDENCE_LIMIT", 3):
            built = listing(["a.py", "b.py", "c.py", "d.sql", "e.sql"])
        self.assertEqual(built.evidence, ["a.py", "b.py", "c.py"])
        self.assertEqual(len(built.listed), 5)
        self.assertTrue(built.truncated)
        self.assertIn("evidence read from the first 3 files", built.notes)


class CliCase(IsolatedCase):
    """Claude alone installed, so the fitted panel holds no specialist."""

    def setUp(self):
        super().setUp()
        self.fake_clis(claude=True)
        # The command reports the root it resolved; on macOS the temporary
        # directory is a symlink (/var -> /private/var), so compare real paths.
        self.project = os.path.realpath(self.project)

    def project_file(self):
        return os.path.join(self.project, ".dev-orchestra.yaml")

    def touch(self, *relatives):
        for relative in relatives:
            self.write(relative, "x\n")

    def database_tree(self):
        self.touch("db/migrations/001.sql", "db/migrations/002.sql", *FILLER)

    def fake_repo(self):
        """A `.git` entry with no repository behind it: only `ws.git` is asked."""
        os.makedirs(os.path.join(self.project, ".git"))

    def fake_git(self, code=0, out="", err=""):
        """Replace ``ws.git`` and return the list of calls it receives."""
        calls = []

        def fake(args, cwd, timeout=60, env=None):
            calls.append((list(args), cwd, timeout))
            return code, out, err

        patcher = mock.patch.object(ws, "git", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def listed_by_git(self, *paths):
        self.fake_repo()
        return self.fake_git(out="".join(path + "\0" for path in paths))


class TestListing(CliCase):
    @unittest.skipUnless(has_git(), "git not installed")
    def test_only_tracked_files_and_no_vendored_dot_or_workspace_evidence(self):
        self.init_git_repo()
        self.write(".gitignore", "ignored/\n")
        self.touch("a.sql", "b.sql", "vendor/x.tsx", "vendor/y.tsx", ".hidden/c.tsx", ".hidden/d.tsx")
        self.touch(".ai/e.tsx", ".ai/f.tsx", *FILLER)
        self.git("add", "-A")
        self.git("add", "-f", ".ai/e.tsx", ".ai/f.tsx")
        self.commit_all()
        self.touch("ignored/g.tsx", "ignored/h.tsx", "untracked/i.tsx", "untracked/j.tsx")
        code, out, err = run_cli("config", "suggest-roles", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["source"], "git")
        self.assertEqual([s["role"] for s in data["suggestions"]], ["database"])
        # vendor/ and .hidden/ are listed but say nothing; .ai/ is the orchestrator's.
        self.assertEqual(data["files"], 2 + 4 + 1 + len(FILLER))

    def test_failing_git_inside_a_repository_is_an_error_and_nothing_is_walked(self):
        self.fake_repo()
        self.fake_git(128, "", "fatal: not a git repository")
        self.touch("a.sql", "b.sql", *FILLER)
        with mock.patch.object(suggest.os, "walk", side_effect=AssertionError("walked")):
            code, out, err = run_cli("config", "suggest-roles")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("git ls-files failed in %s: fatal: not a git repository" % self.project, err)

    def test_a_timeout_is_an_error(self):
        self.fake_repo()
        self.fake_git(127, "", "Command '['git', 'ls-files', '-z']' timed out after 30 seconds")
        code, out, err = run_cli("config", "suggest-roles")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("timed out", err)

    def test_output_over_the_bound_is_an_error(self):
        self.listed_by_git("a.sql", "b.sql", *FILLER)
        with mock.patch.object(suggest, "MAX_LISTING_BYTES", 10):
            code, out, err = run_cli("config", "suggest-roles")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("git ls-files printed more than 10 bytes", err)

    def test_the_bound_counts_bytes_not_characters(self):
        # Seven characters, eleven bytes in UTF-8.
        self.listed_by_git("ああ.sql")
        with mock.patch.object(suggest, "MAX_LISTING_BYTES", 10):
            code, out, err = run_cli("config", "suggest-roles")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("git ls-files printed more than 10 bytes", err)

    def test_git_runs_once_with_a_timeout(self):
        calls = self.listed_by_git("a.sql", "b.sql", *FILLER)
        code, _, err = run_cli("config", "suggest-roles")
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [(["ls-files", "-z"], self.project, suggest.GIT_TIMEOUT)])

    def test_the_evidence_cap_keeps_the_full_list_for_shares_and_package_json(self):
        # package.json sorts past the cap and is still found tracked; its
        # @sveltejs/kit keeps routes/ out of the backend.
        paths = ["app/routes/a.ts", "app/routes/b.ts", "src/A.tsx", "src/B.tsx", *FILLER, "package.json"]
        self.listed_by_git(*paths)
        self.touch(*paths[:-1])
        self.write("package.json", json.dumps({"devDependencies": {"@sveltejs/kit": "2"}}))
        with mock.patch.object(suggest, "EVIDENCE_LIMIT", 4):
            code, out, err = run_cli("config", "suggest-roles", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIn("evidence read from the first 4 files", data["notes"])
        self.assertTrue(data["truncated"])
        self.assertEqual(data["files"], len(paths))
        self.assertEqual([s["role"] for s in data["suggestions"]], ["frontend"])
        self.assertIn("package.json lists @sveltejs/kit", data["suggestions"][0]["evidence"])
        self.assertIn("no signal", {s["role"]: s["reason"] for s in data["skipped"]}["backend"])

    def test_the_walk_prunes_depth_dot_directories_and_skip_dirs(self):
        deep = "/".join("d%d" % level for level in range(9))
        self.touch("a.sql", ".hidden/b.sql", "node_modules/c.sql", "%s/e.sql" % deep, "d0/f.py")
        paths, truncated = suggest.walk_paths(self.project)
        self.assertFalse(truncated)
        self.assertEqual(sorted(paths), ["a.sql", "d0/f.py"])

    def test_the_walk_stops_at_its_file_cap(self):
        self.touch("a.py", "b.py", "c.py")
        with mock.patch.object(suggest, "WALK_FILE_LIMIT", 2):
            paths, truncated = suggest.walk_paths(self.project)
        self.assertEqual((len(paths), truncated), (2, True))

    def test_the_walk_stops_at_its_deadline(self):
        self.touch("a.py", "b.py")
        with mock.patch.object(suggest, "WALK_SECONDS", 0):
            paths, truncated = suggest.walk_paths(self.project)
        self.assertEqual((paths, truncated), ([], True))

    def test_the_deadline_bounds_directories_with_no_files(self):
        for index in range(3):
            os.makedirs(os.path.join(self.project, "empty%d" % index, "deeper"))
        with mock.patch.object(suggest, "WALK_SECONDS", 0):
            paths, truncated = suggest.walk_paths(self.project)
        self.assertEqual((paths, truncated), ([], True))

    def test_outside_git_the_walk_says_the_shares_are_approximate(self):
        self.database_tree()
        with mock.patch.object(suggest.ws, "git", side_effect=AssertionError("git ran")):
            code, out, err = run_cli("config", "suggest-roles")
        self.assertEqual(code, 0, err)
        self.assertIn("is not a git repository, so it was walked; the shares are approximate", out)
        self.assertIn("claude-database", out)


class TestPackageJson(CliCase):
    def deps(self, source="walk", listed=("package.json",)):
        built = suggest.build_listing(self.project, source, list(listed), DEFAULT_EXCLUDE, ".ai")
        return suggest.read_package_deps(self.project, built)

    def test_none_is_no_dependencies(self):
        self.assertEqual(self.deps(), (set(), ""))

    def test_dependencies_of_every_kind_are_read(self):
        manifest = {"dependencies": {"react": "1"}, "devDependencies": {"vue": "3"}}
        self.write("package.json", json.dumps(manifest))
        self.assertEqual(self.deps(), ({"react", "vue"}, ""))

    def test_more_than_a_megabyte_is_a_note(self):
        self.write("package.json", " " * (suggest.PACKAGE_JSON_LIMIT + 1))
        deps, note = self.deps()
        self.assertIsNone(deps)
        self.assertIn("larger than 1 MiB", note)

    def test_invalid_json_is_a_note(self):
        self.write("package.json", "{nope")
        self.assertEqual(
            self.deps(), (None, "package.json is not valid JSON, so its dependencies were not read")
        )

    def test_not_an_object_is_a_note(self):
        self.write("package.json", "[1]")
        self.assertEqual(
            self.deps(), (None, "package.json is not a JSON object, so its dependencies were not read")
        )

    def test_untracked_in_git_is_ignored(self):
        self.write("package.json", json.dumps({"dependencies": {"react": "1"}}))
        deps, note = self.deps("git", listed=["a.py"])
        self.assertIsNone(deps)
        self.assertIn("not tracked", note)

    def test_a_symlink_is_ignored(self):
        target = self.write("real.json", json.dumps({"dependencies": {"react": "1"}}))
        try:
            os.symlink(target, os.path.join(self.project, "package.json"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks cannot be made here")
        deps, note = self.deps()
        self.assertIsNone(deps)
        self.assertIn("not a regular file", note)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no os.mkfifo")
    def test_a_fifo_is_ignored_without_blocking(self):
        os.mkfifo(os.path.join(self.project, "package.json"))
        deps, note = self.deps()
        self.assertIsNone(deps)
        self.assertIn("not a regular file", note)

    def test_a_file_swapped_after_the_check_is_ignored(self):
        self.write("package.json", json.dumps({"dependencies": {"react": "1"}}))
        real_fstat = os.fstat

        def other_inode(fd):
            result = real_fstat(fd)
            fields = list(result)
            fields[1] = result.st_ino + 1  # st_ino
            return os.stat_result(fields)

        with mock.patch.object(suggest.os, "fstat", other_inode):
            deps, note = self.deps()
        self.assertIsNone(deps)
        self.assertIn("not a regular file", note)


class TestCommand(CliCase):
    def test_without_write_nothing_changes(self):
        self.database_tree()
        config_mod.write_config_file(self.project_file(), {"version": 1}, "project")
        with open(self.project_file(), "rb") as handle:
            before = handle.read()
        code, out, err = run_cli("config", "suggest-roles")
        self.assertEqual(code, 0, err)
        self.assertIn("claude-database  claude / sonnet  role database", out)
        self.assertIn("when.paths: migrations/*, */migrations/*, *.sql", out)
        self.assertIn("matches 2 of %d files" % (2 + len(FILLER)), out)
        self.assertIn(suggest.FOOTER, out)
        nothing = "Nothing was written; --write adds them to %s's reviewers_extra" % self.project_file()
        self.assertIn(nothing, out)
        with open(self.project_file(), "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_write_from_a_subdirectory_creates_the_file_at_the_root(self):
        self.database_tree()
        self.listed_by_git("db/migrations/001.sql", "db/migrations/002.sql", *FILLER)
        sub = os.path.join(self.project, "db")
        code, out, err = run_cli("--cwd", sub, "config", "suggest-roles", "--write")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(os.path.join(sub, ".dev-orchestra.yaml")))
        added = "Added claude-database to %s as extras; the panel still follows" % self.project_file()
        self.assertIn(added, out)
        loaded = config_mod.load(self.project)
        extra = loaded.reviewers()[-1]
        self.assertEqual(loaded.reviewer_origins[-1].label(), "project extra")
        self.assertEqual(extra["when"], {"paths": ["migrations/*", "*/migrations/*", "*.sql"]})
        # A second run finds it on the panel.
        code, out, err = run_cli("config", "suggest-roles", "--write")
        self.assertEqual(code, 0, err)
        self.assertIn("already on the panel: claude-database (project extra)", out)
        self.assertIn("Nothing to add.", out)

    def test_a_project_file_away_from_the_root_refuses_write(self):
        self.database_tree()
        self.listed_by_git("db/migrations/001.sql", "db/migrations/002.sql", *FILLER)
        sub = os.path.join(self.project, "db")
        stray = os.path.join(sub, ".dev-orchestra.yaml")
        config_mod.write_config_file(stray, {"version": 1}, "project")
        with open(stray, "rb") as handle:
            before = handle.read()
        code, out, err = run_cli("--cwd", sub, "config", "suggest-roles", "--write")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("the project file in force here is %s" % stray, err)
        with open(stray, "rb") as handle:
            self.assertEqual(handle.read(), before)
        code, out, err = run_cli("--cwd", sub, "config", "suggest-roles")
        self.assertEqual(code, 0, err)
        self.assertIn("(refused from here: %s is not at %s)" % (stray, self.project), out)

    def test_a_project_file_above_the_root_refuses_write(self):
        repo = os.path.join(self.project, "inner")
        os.makedirs(os.path.join(repo, "db", "migrations"))
        self.touch("inner/db/migrations/1.sql", "inner/db/migrations/2.sql")
        # A worktree's `.git` is a file, which find_project_config walks past.
        self.write("inner/.git", "gitdir: elsewhere\n")
        self.fake_git(out="db/migrations/1.sql\0db/migrations/2.sql\0" + "".join(p + "\0" for p in FILLER))
        config_mod.write_config_file(self.project_file(), {"version": 1}, "project")
        code, out, err = run_cli("--cwd", repo, "config", "suggest-roles", "--write")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("but the files are listed from %s" % repo, err)

    def test_write_into_a_file_that_lists_reviewers(self):
        self.database_tree()
        always = config_mod.make_reviewer("claude-general", "claude", "opus")
        config_mod.write_config_file(self.project_file(), {"version": 1, "reviewers": [always]}, "project")
        code, out, err = run_cli("config", "suggest-roles", "--write")
        self.assertEqual(code, 0, err)
        self.assertIn("as extras, after the reviewers it lists", out)
        layer = config_mod.read_config_file(self.project_file())
        self.assertEqual(layer["reviewers"], [always])
        self.assertEqual([r["id"] for r in layer["reviewers_extra"]], ["claude-database"])

    def test_provider_agy_is_refused_before_git_runs(self):
        self.fake_repo()
        calls = self.fake_git(128, "", "fatal")
        code, out, err = run_cli("config", "suggest-roles", "--provider", "agy")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("reviewers on agy are taken only from the global config", err)
        self.assertEqual(calls, [])

    def test_extras_that_are_not_a_list_refuse_write_before_git_runs(self):
        self.fake_repo()
        calls = self.fake_git(out="a.sql\0b.sql\0")
        self.write(".dev-orchestra.yaml", "version: 1\nreviewers_extra: 3\n")
        code, out, err = run_cli("config", "suggest-roles", "--write")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("reviewers_extra: must be a list", err)
        self.assertEqual(calls, [])

    def test_no_eligible_provider_refuses_write(self):
        self.fake_clis()
        self.database_tree()
        code, out, err = run_cli("config", "suggest-roles", "--write")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("no CLI a project reviewer can run on is installed (claude, codex)", err)
        self.assertFalse(os.path.exists(self.project_file()))
        code, out, err = run_cli("config", "suggest-roles")
        self.assertEqual(code, 0, err)
        self.assertIn("note: no CLI a project reviewer can run on is installed", out)

    def test_the_family_is_the_providers_cheap_one(self):
        self.database_tree()
        for installed, provider, family in (
            ({"claude": True}, "claude", "sonnet"),
            ({"codex": True}, "codex", "recommended-coding"),
        ):
            with self.subTest(provider=provider):
                self.fake_clis(**installed)
                code, out, err = run_cli("config", "suggest-roles", "--json")
                self.assertEqual(code, 0, err)
                (entry,) = json.loads(out)["suggestions"]
                self.assertEqual((entry["provider"], entry["family"]), (provider, family))

    def test_model_and_provider_flags(self):
        self.database_tree()
        code, out, err = run_cli("config", "suggest-roles", "--json", "--provider", "codex", "--model", "x")
        self.assertEqual(code, 0, err)
        (entry,) = json.loads(out)["suggestions"]
        self.assertEqual((entry["id"], entry["provider"], entry["family"]), ("codex-database", "codex", "x"))

    def test_an_empty_listed_panel_refuses_write(self):
        self.database_tree()
        self.write(".dev-orchestra.yaml", "version: 1\nreviewers: []\n")
        before = config_mod.read_config_file(self.project_file())
        code, out, err = run_cli("config", "suggest-roles", "--write")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("at least one reviewer must run always", err)
        self.assertEqual(config_mod.read_config_file(self.project_file()), before)

    def test_malformed_values_fall_back_with_a_note(self):
        self.database_tree()
        for text, note in (
            ("review: 3", "review.exclude is not a list of strings"),
            ("review:\n  exclude: 5", "review.exclude is not a list of strings"),
            ('workspace: "x"', "workspace.dir is not a non-empty string"),
            ("workspace:\n  dir: 7", "workspace.dir is not a non-empty string"),
        ):
            with self.subTest(text=text):
                self.write(".dev-orchestra.yaml", "version: 1\n%s\n" % text)
                code, out, err = run_cli("config", "suggest-roles")
                self.assertEqual(code, 0, err)
                self.assertIn("note: %s" % note, out)
                self.assertIn("claude-database", out)

    def test_a_panel_that_is_not_a_list(self):
        self.database_tree()
        self.write(".dev-orchestra.yaml", "version: 1\nreviewers: 5\n")
        code, out, err = run_cli("config", "suggest-roles")
        self.assertEqual(code, 0, err)
        self.assertIn("note: reviewers is not a list; the panel was treated as empty", out)
        code, out, err = run_cli("config", "suggest-roles", "--write")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("the panel in force is not a list", err)

    def test_json_write_prints_one_object_and_notes_on_stderr(self):
        self.database_tree()
        self.write("package.json", "{nope")
        code, out, err = run_cli("config", "suggest-roles", "--json", "--write")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["written"], self.project_file())
        self.assertEqual(data["suggestions"][0]["id"], "claude-database")
        self.assertEqual(data["suggestions"][0]["matches"], 2)
        self.assertEqual(data["suggestions"][0]["withheld_matches"], 0)
        self.assertEqual(
            set(data), {"root", "source", "files", "truncated", "notes", "suggestions", "skipped", "written"}
        )
        self.assertIn("note: package.json is not valid JSON", err)

    def test_a_refusal_with_json_leaves_stdout_empty(self):
        self.database_tree()
        code, out, _ = run_cli("config", "suggest-roles", "--json", "--provider", "agy")
        self.assertEqual((code, out), (2, ""))

    def test_no_subprocess_but_git(self):
        self.listed_by_git("db/migrations/001.sql", "db/migrations/002.sql", *FILLER)
        self.database_tree()

        def refuse(*args, **kwargs):
            raise AssertionError("subprocess started: %r" % (args,))

        with mock.patch.object(subprocess, "run", refuse), mock.patch.object(subprocess, "Popen", refuse):
            code, _, err = run_cli("config", "suggest-roles", "--write")
        self.assertEqual(code, 0, err)
        extras = config_mod.read_config_file(self.project_file())["reviewers_extra"]
        self.assertEqual([r["id"] for r in extras], ["claude-database"])


class TestPresetHelpers(IsolatedCase):
    def test_suggestion_provider(self):
        self.assertEqual(presets.suggestion_provider(["claude", "codex"]), "claude")
        self.assertEqual(presets.suggestion_provider(["codex"]), "codex")
        self.assertIsNone(presets.suggestion_provider(["agy"]))
        self.assertIsNone(presets.suggestion_provider([]))

    def test_a_user_adapter_that_may_take_a_seat(self):
        declared = 'preset_family = "big"'
        self.write_user_provider("seated", user_adapter_source("seated", declared, "verified"))
        self.write_user_provider("writer", user_adapter_source("writer", declared))
        self.load_user_providers()
        self.assertEqual(presets.suggestion_provider(["agy", "writer", "seated"]), "seated")
        self.assertIsNone(presets.suggestion_provider(["writer"]))
        self.assertEqual(presets.cheap_family("seated"), "big")

    def test_cheap_family(self):
        self.assertEqual(presets.cheap_family("claude"), "sonnet")
        self.assertEqual(presets.cheap_family("codex"), "recommended-coding")
        self.assertEqual(presets.cheap_family("nobody"), config_mod.default_reviewer_family("nobody"))


if __name__ == "__main__":
    unittest.main()
