"""Re-review sees the fix, not the whole change again.

Round 2 used to re-diff everything against HEAD, so the second round cost the
same as the first -- once per reviewer -- to look at a one-line fix. It now
diffs against the tree the previous round actually reviewed.

The subtlety worth testing is not the saving but the *premise*. A reviewer is
stateless and sees no other reviewer's output, so a fix diff on its own is a
change with no stated purpose: "is this correct" cannot be answered without
knowing what it was correcting. So an incremental round also carries the
findings the fix was meant to address and a pointer to the frozen whole
change -- and it only happens at all when the previous snapshot was really
reviewed, because otherwise there is no round to be incremental to.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git

from orchestrator import cli
from orchestrator import config as config_mod
from orchestrator import review as review_mod
from orchestrator import workspace as ws

FINDING = """## Finding
- Severity: high
- File: service.py
- Line: 2
- Category: correctness
- Problem: op0 multiplies by zero and always returns zero
- Impact: every caller of op0 gets 0
- Evidence: return x * 0
- Recommended fix: keep the addition for the zero case
"""


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def body(operator):
    return "\n".join("def op%d(x):\n    return x %s %d\n" % (i, operator, i) for i in range(60))


@unittest.skipUnless(has_git(), "git is required for review snapshots")
class RoundCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("service.py", body("+"))
        self.commit_all("init")
        mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(mock_dir)
        with open(os.path.join(mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING)
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = mock_dir
        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        self.workspace = self.cli_workspace()

    def implement(self):
        """A change that touches a lot, so a second full diff is expensive."""
        self.write("service.py", body("*"))

    def first_round(self, triage=True):
        self.implement()
        meta = review_mod.create_snapshot(self.workspace)
        run_cli("review", "run")
        if triage:
            run_cli("review", "triage", "F1", "--status", "accepted")
        return meta

    def fix(self):
        text = ws.read_text(os.path.join(self.project, "service.py"))
        self.write("service.py", text.replace("return x * 0", "return x + 0", 1))


# --------------------------------------------------------------------------- tree


class TestWorkingTreeObject(RoundCase):
    def test_the_working_tree_is_recorded_as_a_tree_object(self):
        self.implement()
        meta = review_mod.create_snapshot(self.workspace)
        self.assertRegex(meta["tree"], r"^[0-9a-f]{40}$")

    def test_the_users_own_index_is_left_alone(self):
        """It writes through a throwaway index, the way `git stash create` does.

        Staging the user's work as a side effect of taking a snapshot would be
        a genuinely nasty surprise, so this asserts nothing was staged rather
        than merely that the snapshot worked.
        """
        self.implement()
        self.write("brand_new.py", "x = 1\n")
        review_mod.create_snapshot(self.workspace)

        def git(*args):
            return subprocess.run(["git", *args], cwd=self.project, capture_output=True, text=True).stdout

        self.assertEqual(git("diff", "--cached", "--name-only").strip(), "")
        status = git("status", "--porcelain")
        # " M" is an unstaged modification; "M " would mean it had been staged.
        self.assertIn(" M service.py", status)
        self.assertIn("?? brand_new.py", status)

    def test_an_untracked_file_is_part_of_the_tree(self):
        """A new module is usually the most important part of a change, and the
        next round has to be able to diff against it."""
        self.write("new_module.py", "print('hi')\n")
        first = review_mod.create_snapshot(self.workspace)["tree"]
        self.write("new_module.py", "print('hi there')\n")
        second = review_mod.create_snapshot(self.workspace)["tree"]
        self.assertTrue(first and second)
        self.assertNotEqual(first, second)

    def test_a_repository_with_no_commits_still_gets_a_tree(self):
        fresh = os.path.join(self.tmp, "fresh")
        os.makedirs(fresh)
        subprocess.run(["git", "init", "-q"], cwd=fresh, check=True, capture_output=True)
        with open(os.path.join(fresh, "a.py"), "w", encoding="utf-8") as handle:
            handle.write("x = 1\n")
        self.assertRegex(review_mod._write_tree(fresh), r"^[0-9a-f]{40}$")


# --------------------------------------------------------------------------- scope


class TestIncrementalScope(RoundCase):
    def test_a_second_round_diffs_only_the_fix(self):
        first = self.first_round()
        self.fix()
        second = review_mod.create_snapshot(self.workspace)
        self.assertTrue(second["incremental_from"])
        self.assertLess(second["bytes"] * 5, first["bytes"])
        diff = ws.read_text(self.workspace.snapshot_path)
        self.assertIn("return x + 0", diff)
        # op1..op59 changed in round 1 and are not part of the fix.
        self.assertNotIn("op59", diff)

    def test_the_whole_change_is_kept_where_a_reviewer_can_read_it(self):
        """The fix diff is the cheap part; the context has to stay reachable."""
        self.first_round()
        self.fix()
        meta = review_mod.create_snapshot(self.workspace)
        self.assertTrue(meta["full_diff"])
        full = ws.read_text(os.path.join(self.project, meta["full_diff"]))
        self.assertIn("op59", full)

    def test_a_re_snapshot_of_an_unreviewed_round_is_still_the_whole_change(self):
        """Re-snapshotting because a reviewer failed is not a new round, and
        must not silently reduce to an empty diff."""
        self.implement()
        first = review_mod.create_snapshot(self.workspace)
        second = review_mod.create_snapshot(self.workspace)
        self.assertFalse(second["incremental_from"])
        self.assertEqual(first["sha256"], second["sha256"])

    def test_nothing_changed_since_the_reviewed_round_falls_back_to_the_whole_change(self):
        """An empty incremental diff would say "the fixer did nothing" in a way
        that reads as "there is nothing to review"."""
        first = self.first_round()
        second = review_mod.create_snapshot(self.workspace)
        self.assertFalse(second["incremental_from"])
        self.assertEqual(second["sha256"], first["sha256"])

    def test_an_explicit_base_is_never_incremental(self):
        """--base is the caller stating the comparison; it wins."""
        self.first_round()
        self.fix()
        meta = review_mod.create_snapshot(self.workspace, base="HEAD")
        self.assertFalse(meta["incremental_from"])
        self.assertIn("op59", ws.read_text(self.workspace.snapshot_path))

    def test_the_feature_can_be_turned_off(self):
        self.first_round()
        self.fix()
        meta = review_mod.create_snapshot(self.workspace, incremental=False)
        self.assertFalse(meta["incremental_from"])
        self.assertIn("op59", ws.read_text(self.workspace.snapshot_path))

    def test_a_file_created_by_the_fix_appears_in_the_incremental_diff(self):
        self.first_round()
        self.write("helper.py", "def help():\n    return 1\n")
        meta = review_mod.create_snapshot(self.workspace)
        self.assertTrue(meta["incremental_from"])
        self.assertIn("helper.py", ws.read_text(self.workspace.snapshot_path))

    def test_an_untracked_file_is_not_counted_twice(self):
        """Both trees already contain it, so the untracked pass must not add it
        again -- that would duplicate every hunk."""
        self.implement()
        self.write("new_module.py", "print('one')\n")
        review_mod.create_snapshot(self.workspace)
        run_cli("review", "run")
        self.write("new_module.py", "print('two')\n")
        review_mod.create_snapshot(self.workspace)
        diff = ws.read_text(self.workspace.snapshot_path)
        self.assertEqual(diff.count("+print('two')"), 1)


class TestRulesThatMustNotLapseInRoundTwo(RoundCase):
    """Both trees are built with ``git add -A``, so anything the untracked pass
    used to filter has to be filtered again for a tree-to-tree diff. Each of
    these held in round 1 and silently stopped holding in round 2."""

    def test_no_untracked_is_honoured_on_an_incremental_round(self):
        self.first_round()
        self.fix()
        self.write("brand_new.py", "print('new')\n")
        meta = review_mod.create_snapshot(self.workspace, include_untracked=False)
        self.assertTrue(meta["incremental_from"])
        self.assertNotIn("brand_new.py", meta["files"])
        self.assertNotIn("brand_new.py", ws.read_text(self.workspace.snapshot_path))

    def test_the_flag_actually_toggles_rather_than_always_excluding(self):
        self.first_round()
        self.fix()
        self.write("brand_new.py", "print('new')\n")
        meta = review_mod.create_snapshot(self.workspace)
        self.assertIn("brand_new.py", meta["files"])

    def test_a_suppressed_file_is_not_reported_as_withheld(self):
        """Withheld means "part of the change, body not sent". These are not
        part of the change at all, so claiming otherwise would be a lie."""
        self.first_round()
        self.fix()
        self.write("extra.lock", "generated\n")
        meta = review_mod.create_snapshot(self.workspace, include_untracked=False)
        self.assertEqual([entry["path"] for entry in meta["withheld"]], [])

    def test_the_orchestrators_own_config_stays_out_of_an_incremental_diff(self):
        """SKILL.md tells the orchestrator to run `config set` mid-workflow, so
        the config changing between rounds is a normal event, not an abuse."""
        self.write(".dev-orchestra.yaml", "version: 1\n")
        self.first_round()
        self.fix()
        self.write(".dev-orchestra.yaml", "version: 1\nreview:\n  timeout_seconds: 900\n")
        meta = review_mod.create_snapshot(self.workspace)
        self.assertTrue(meta["incremental_from"])
        self.assertNotIn(".dev-orchestra.yaml", meta["files"])
        self.assertNotIn("timeout_seconds", ws.read_text(self.workspace.snapshot_path))

    def test_the_workspace_stays_out_of_an_incremental_diff(self):
        """It is normally gitignored, but the invariant must not depend on that:
        the docs invite a team to commit the workspace instead."""
        self.first_round()
        self.fix()
        os.remove(os.path.join(self.project, ".ai", ".gitignore"))
        meta = review_mod.create_snapshot(self.workspace)
        self.assertFalse([name for name in meta["files"] if name.startswith(".ai/")])


class TestNarrowingNeedsAPremise(RoundCase):
    """Scope is only narrowed together with the brief that explains it. A round
    that follows a review producing nothing to fix has no such brief, so it
    takes the whole change -- it is reviewing new work, not checking a fix."""

    def clean_round(self):
        """A first round that finds nothing."""
        os.environ["DEV_ORCHESTRA_MOCK_RESPONSE"] = "NO_FINDINGS\n"
        os.environ.pop("DEV_ORCHESTRA_MOCK_DIR", None)
        self.addCleanup(os.environ.pop, "DEV_ORCHESTRA_MOCK_RESPONSE", None)
        self.implement()
        review_mod.create_snapshot(self.workspace)
        run_cli("review", "run")

    def test_a_round_after_a_clean_review_takes_the_whole_change(self):
        self.clean_round()
        self.write("service.py", body("*") + "\ndef extra(x):\n    return x\n")
        meta = review_mod.create_snapshot(self.workspace)
        self.assertFalse(meta["incremental_from"])
        self.assertIn("op59", ws.read_text(self.workspace.snapshot_path))

    def test_a_round_after_an_accepted_finding_still_narrows(self):
        """The guard must not have disabled the feature outright."""
        self.first_round()
        self.fix()
        self.assertTrue(review_mod.create_snapshot(self.workspace)["incremental_from"])


class TestReviewingABranchAgainstABase(RoundCase):
    """`--base master` is how a branch is reviewed, and it used to switch
    narrowing off entirely.

    The base decides what the *first* round covers. Whether a *later* round
    may narrow to the fix is a separate question, and conflating them meant
    the ordinary way to use this tool re-sent the whole branch every round.
    On one real three-round review the diff grew 1,867 -> 2,472 -> 3,228 lines
    while the findings fell 11 -> 6 -> 5: $0.20 per finding became $1.00.
    """

    def base(self):
        """A commit to review against, with the change left uncommitted."""
        return self.git("rev-parse", "HEAD").stdout.strip()

    def test_the_second_round_against_a_base_sees_only_the_fix(self):
        head = self.base()
        self.implement()
        review_mod.create_snapshot(self.workspace, base=head)
        run_cli("review", "run")
        run_cli("review", "triage", "F1", "--status", "accepted")
        self.fix()
        meta = review_mod.create_snapshot(self.workspace, base=head)
        self.assertTrue(meta["incremental_from"])
        self.assertEqual(meta["files"], ["service.py"])
        self.assertLess(meta["bytes"], 400)

    def test_the_whole_branch_is_still_there_to_read(self):
        """Narrowed, not hidden: the full diff against the base is frozen
        beside it."""
        head = self.base()
        self.implement()
        review_mod.create_snapshot(self.workspace, base=head)
        run_cli("review", "run")
        run_cli("review", "triage", "F1", "--status", "accepted")
        self.fix()
        meta = review_mod.create_snapshot(self.workspace, base=head)
        self.assertTrue(meta["full_diff"])
        full = ws.read_text(self.workspace.full_snapshot_path)
        self.assertGreater(len(full), meta["bytes"])

    def test_changing_the_base_takes_the_whole_change_again(self):
        """A different base is a different definition of what is under review.
        Narrowing to a fix for the old one would answer the old question."""
        head = self.base()
        self.implement()
        review_mod.create_snapshot(self.workspace, base=head)
        run_cli("review", "run")
        run_cli("review", "triage", "F1", "--status", "accepted")
        self.fix()
        meta = review_mod.create_snapshot(self.workspace, base="HEAD")
        self.assertFalse(meta["incremental_from"])

    def test_dropping_the_base_takes_the_whole_change_again(self):
        head = self.base()
        self.implement()
        review_mod.create_snapshot(self.workspace, base=head)
        run_cli("review", "run")
        run_cli("review", "triage", "F1", "--status", "accepted")
        self.fix()
        self.assertFalse(review_mod.create_snapshot(self.workspace)["incremental_from"])


class TestTreeIsOnlyWrittenWhenItWillBeUsed(RoundCase):
    """Writing the tree hashes every untracked-but-not-ignored file into the
    object database. A round that has said it will not use one should not pay
    for it, or leave the objects behind."""

    def test_a_normal_round_records_one(self):
        self.implement()
        self.assertTrue(review_mod.create_snapshot(self.workspace)["tree"])

    def test_an_explicit_base_records_one_too(self):
        """It did not, and that was the bug: a branch review never narrowed."""
        self.implement()
        self.assertTrue(review_mod.create_snapshot(self.workspace, base="HEAD")["tree"])

    def test_the_feature_being_off_does_not(self):
        self.implement()
        self.assertEqual(review_mod.create_snapshot(self.workspace, incremental=False)["tree"], "")

    def test_the_round_after_one_falls_back_to_the_whole_change(self):
        """No tree to compare against is the safe direction to fail in."""
        self.implement()
        review_mod.create_snapshot(self.workspace, incremental=False)
        run_cli("review", "run")
        run_cli("review", "triage", "F1", "--status", "accepted")
        self.fix()
        meta = review_mod.create_snapshot(self.workspace)
        self.assertFalse(meta["incremental_from"])
        self.assertIn("op59", ws.read_text(self.workspace.snapshot_path))


class TestFullDiffCompanion(RoundCase):
    def test_it_withholds_what_the_first_round_withheld(self):
        """The prompt invites a reviewer to open this file. Recomputing the
        exclusions against the incremental round's list -- which only covers
        what the fix touched -- would put the lockfile back in it."""
        self.write("package-lock.json", json.dumps({"v": list(range(100))}))
        self.first_round()
        self.write("package-lock.json", json.dumps({"v": list(range(300))}))
        self.fix()
        meta = review_mod.create_snapshot(self.workspace)
        self.assertTrue(meta["full_diff"])
        full = ws.read_text(os.path.join(self.project, meta["full_diff"]))
        self.assertIn("op59", full)
        self.assertNotIn("package-lock.json", full)


class TestUnresolvableRevision(RoundCase):
    def test_a_revision_git_cannot_resolve_is_reported_not_swallowed(self):
        """Reporting it as "nothing to review" sends someone hunting through
        their own change for something that was never missing."""
        self.implement()
        with self.assertRaises(review_mod.ReviewError) as ctx:
            review_mod.create_snapshot(self.workspace, base="no-such-branch")
        self.assertIn("no-such-branch", str(ctx.exception))

    def test_the_command_exits_two_rather_than_claiming_an_empty_change(self):
        self.implement()
        code, out, err = run_cli("review", "snapshot", "--base", "no-such-branch")
        self.assertEqual(code, 2)
        self.assertNotIn("nothing to review", out)
        self.assertIn("no-such-branch", err)

    def test_a_repository_with_no_commits_is_still_the_benign_case(self):
        fresh = os.path.join(self.tmp, "empty-repo")
        os.makedirs(fresh)
        subprocess.run(["git", "init", "-q"], cwd=fresh, check=True, capture_output=True)
        with open(os.path.join(fresh, "a.py"), "w", encoding="utf-8") as handle:
            handle.write("x = 1\n")
        meta = review_mod.create_snapshot(ws.Workspace(fresh).ensure())
        self.assertIn("no HEAD commit", meta["strategy"])
        self.assertIn("a.py", meta["untracked_included"])

    def test_exclusions_still_apply_to_an_incremental_round(self):
        self.first_round()
        self.write("package-lock.json", json.dumps({"v": list(range(300))}))
        meta = review_mod.create_snapshot(self.workspace)
        self.assertTrue(meta["incremental_from"])
        self.assertIn("package-lock.json", [entry["path"] for entry in meta["withheld"]])
        self.assertNotIn("package-lock.json", ws.read_text(self.workspace.snapshot_path))

    def test_the_round_counter_still_advances(self):
        """The iteration budget only stops a loop if the counter moves."""
        self.first_round()
        self.assertEqual(review_mod.next_iteration(self.workspace), 1)
        self.fix()
        review_mod.create_snapshot(self.workspace)
        self.assertEqual(review_mod.next_iteration(self.workspace), 2)


# --------------------------------------------------------------------------- premise


class TestRereviewPremise(RoundCase):
    def prompt(self):
        return review_mod.build_review_prompt(
            {"id": "m1", "role": "general"},
            self.workspace,
            ws.read_text(self.workspace.snapshot_path),
        ).text

    def test_a_first_round_says_nothing_about_re_review(self):
        self.implement()
        review_mod.create_snapshot(self.workspace)
        self.assertNotIn("re-review", self.prompt())

    def test_a_re_review_states_that_the_diff_is_only_the_fix(self):
        """Otherwise the reviewer reads a partial diff as the whole change."""
        self.first_round()
        self.fix()
        review_mod.create_snapshot(self.workspace)
        prompt = self.prompt()
        self.assertIn("the fix only, not the whole change", prompt)
        self.assertIn("review-target-full.diff", prompt)

    def test_a_re_review_is_told_what_the_fix_was_for(self):
        self.first_round()
        self.fix()
        review_mod.create_snapshot(self.workspace)
        prompt = self.prompt()
        self.assertIn("The fix was meant to address:", prompt)
        self.assertIn("op0 multiplies by zero", prompt)
        self.assertIn("Do not assume a listed item was real", prompt)

    def test_only_accepted_findings_are_carried_over(self):
        """A rejected finding is not part of the brief the fixer worked from --
        and with no brief at all, the round takes the whole change rather than
        sending a fragment with nothing to judge it against."""
        self.first_round(triage=False)
        run_cli("review", "triage", "F1", "--status", "rejected")
        self.fix()
        meta = review_mod.create_snapshot(self.workspace)
        self.assertFalse(meta["incremental_from"])
        self.assertNotIn("The fix was meant to address:", self.prompt())
        self.assertIn("op59", ws.read_text(self.workspace.snapshot_path))

    def test_the_brief_does_not_say_who_reported_what(self):
        """Reviewers still do not see each other's output: the findings arrive
        as the brief the fixer worked from, not as a peer's opinion in play."""
        self.first_round()
        self.fix()
        review_mod.create_snapshot(self.workspace)
        context = review_mod.render_round_context(
            self.workspace, ws.read_json(self.workspace.snapshot_meta_path, {})
        )
        self.assertNotIn("m1", context)
        self.assertNotIn("Reported by", context)

    def test_the_premise_costs_a_line_per_finding(self):
        """It replaces thousands of tokens of re-sent diff; it must not grow
        into an essay of its own."""
        self.first_round()
        self.fix()
        review_mod.create_snapshot(self.workspace)
        context = review_mod.render_round_context(
            self.workspace, ws.read_json(self.workspace.snapshot_meta_path, {})
        )
        self.assertLess(len(context), 700)


# --------------------------------------------------------------------------- cli


class TestSnapshotCommandRounds(RoundCase):
    def test_the_command_says_the_scope_narrowed(self):
        """A quieter review has to announce itself, or it looks like a bug."""
        self.first_round()
        self.fix()
        code, out, _ = run_cli("review", "snapshot")
        self.assertEqual(code, 0)
        self.assertIn("what changed since the last reviewed round", out)
        self.assertIn("findings the fix was meant to address", out)

    def test_full_forces_the_whole_change(self):
        self.first_round()
        self.fix()
        payload = json.loads(run_cli("review", "snapshot", "--full", "--json")[1])
        self.assertFalse(payload["incremental_from"])
        self.assertIn("op59", ws.read_text(self.workspace.snapshot_path))

    def test_the_setting_turns_it_off_for_a_project(self):
        self.first_round()
        self.fix()
        run_cli("config", "set", "review.incremental_rounds", "false")
        payload = json.loads(run_cli("review", "snapshot", "--json")[1])
        self.assertFalse(payload["incremental_from"])

    def test_review_run_snapshots_the_same_way(self):
        self.first_round()
        self.fix()
        os.remove(self.workspace.snapshot_path)
        run_cli("review", "run")
        meta = ws.read_json(self.workspace.snapshot_meta_path, {})
        self.assertTrue(meta["incremental_from"])


class TestCoverageSurvivesTheNarrowing(RoundCase):
    """The narrowing is exactly what would launder an unread first round.

    Round 1 goes over the inline limit and is handed to the reviewer as a
    file. Its finding is real, gets accepted and gets fixed -- and round 2,
    by design, shows the reviewer the fix and nothing else. So round 2 inlines
    its whole change body, answers NO_FINDINGS and is a clean round, while
    four fifths of the change has still never been read by anybody. Only the
    whole change's own state, carried forward, says so.
    """

    #: The inline limit this case configures -- the old default, which is
    #: small enough that git can produce a diff over it in the time a test
    #: has. The rule under test is the same one whatever the limit is set to.
    INLINE_LIMIT = 120_000

    def setUp(self):
        super().setUp()
        # Four rounds below, each from a new snapshot, so each advances the
        # counter. The budget being spent is a different test.
        run_cli("config", "set", "review.max_review_iterations", "9")
        # What this test needs is a round handed over as a file, and the
        # shipped 400,000 would want a diff three times the size of this one.
        run_cli("config", "set", "review.context.inline_chars", str(self.INLINE_LIMIT))

    def bulk(self, lines):
        return "".join('BULK_%04d = "%s"\n' % (index, "y" * 100) for index in range(lines))

    def oversize(self):
        """More change than fits in a prompt. 1,200 lines of ~115 characters
        clears `INLINE_LIMIT` with room to spare, and stays a diff git can
        produce in the time a test has."""
        self.write("bulk.py", self.bulk(1200))

    def clean_answers(self):
        os.environ["DEV_ORCHESTRA_MOCK_RESPONSE"] = "NO_FINDINGS\n"
        os.environ.pop("DEV_ORCHESTRA_MOCK_DIR", None)

    def coverage(self):
        return json.loads(run_cli("review", "show", "--json")[1])["coverage"]

    def test_a_fix_only_round_cannot_launder_a_first_round_nobody_read(self):
        # 1. A change too large to inline. The reviewer is handed a path.
        self.implement()
        self.oversize()
        review_mod.create_snapshot(self.workspace)
        code, out, _ = run_cli("review", "run")
        self.assertEqual(code, 1)
        self.assertIn("PARTIAL", out)
        first = self.coverage()
        self.assertEqual(first["round"], "unverified")
        self.assertEqual(first["change"], "unverified")
        self.assertEqual(first["unverified_since"], 1)
        self.assertGreater(first["change_chars"], self.INLINE_LIMIT)
        self.assertEqual(first["inline_chars"], self.INLINE_LIMIT)

        # 2. Its finding is real: triage it, fix it. The next snapshot narrows
        #    to the fix, which is the whole point of an incremental round.
        run_cli("review", "triage", "F1", "--status", "accepted")
        self.fix()
        meta = review_mod.create_snapshot(self.workspace)
        self.assertTrue(meta["incremental_from"])
        self.assertNotIn("BULK_0500", ws.read_text(self.workspace.snapshot_path))

        # 3. The fix inlines, the reviewer finds nothing, the round is clean --
        #    and the change is still unverified from round 1.
        self.clean_answers()
        code, _, _ = run_cli("review", "run")
        self.assertEqual(code, 0)
        second = self.coverage()
        self.assertEqual(second["round"], "complete")
        self.assertEqual(second["change"], "unverified")
        self.assertEqual(second["unverified_since"], 1)
        status = json.loads(run_cli("review", "status", "--json")[1])
        self.assertEqual(status["coverage"]["change"], "unverified")
        self.assertIn("--full", run_cli("review", "status")[1])

        # 4. --full re-sends the whole change, which still does not fit. The
        #    mark stays where it was rather than moving to this round.
        run_cli("review", "snapshot", "--full")
        self.assertEqual(run_cli("review", "run")[0], 1)
        third = self.coverage()
        self.assertEqual(third["round"], "unverified")
        self.assertEqual(third["unverified_since"], 1)

        # 5. Narrow the change until it fits, and one --full round clears it.
        self.write("bulk.py", self.bulk(2))
        run_cli("review", "snapshot", "--full")
        self.assertEqual(run_cli("review", "run")[0], 0)
        fourth = self.coverage()
        self.assertEqual(fourth["change"], "complete")
        self.assertIsNone(fourth["unverified_since"])


class TestIncrementalConfiguration(IsolatedCase):
    def test_it_is_on_by_default(self):
        self.assertIs(config_mod.default_config()["review"]["incremental_rounds"], True)

    def test_a_non_boolean_is_a_configuration_error(self):
        data = config_mod.default_config()
        data["review"]["incremental_rounds"] = "yes"
        self.assertTrue(any("incremental_rounds" in problem for problem in config_mod.validate(data)))


if __name__ == "__main__":
    unittest.main()
