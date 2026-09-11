"""Spending less on a review, without spending less on the wrong review.

Three savings hang off ``optimization.level``: not reviewing a tree whose
tests are recorded as failing, giving a small change one reviewer instead of
the panel, and asking each reviewer for fewer findings.

All three are ways of doing less, so every test here is really about the
limits on doing less. The gate must tell "the tests failed" from "nobody said"
-- refusing on the second would break every workflow that never wrote the
result down. The reduced panel must not fire on a change that is small and
dangerous, which is the shape most authorisation bugs arrive in. And the
escalation that protects both has to be visible, or a review that quietly did
half the work reads exactly like one that did all of it.
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
from orchestrator import ledger as ledger_mod
from orchestrator import optimization as opt
from orchestrator import workspace as ws


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


FINDING = """## Finding
- Severity: high
- File: app.py
- Line: 2
- Category: correctness
- Problem: subtraction where addition was intended
- Impact: every caller gets the wrong total
- Evidence: return a - b
- Fix: restore the addition
"""


def decide(paths=("app.py",), lines=10, test_status="", reviewers=2, reviewed_files=None, **overrides):
    settings = dict(config_mod.default_config()["optimization"])
    review = dict(config_mod.default_config()["review"])
    for key, value in overrides.items():
        (review if key in review else settings)[key] = value
    return opt.decide(
        settings,
        review,
        list(paths),
        lines,
        test_status,
        reviewers,
        reviewed_files=reviewed_files,
    )


# --------------------------------------------------------------------------- risk


class TestHighRiskPaths(unittest.TestCase):
    def test_the_usual_suspects_are_caught(self):
        for path in (
            "app/controllers/auth_controller.rb",
            "src/login.tsx",
            "lib/session_store.py",
            "app/policies/order_policy.rb",
            "config/secrets.yml",
            ".env.production",
            "billing/checkout.go",
            "db/migrate/003_drop_orders.rb",
            "db/schema.rb",
            "queries/report.sql",
            "Dockerfile",
            "infra/main.tf",
            ".github/workflows/release.yml",
        ):
            self.assertTrue(opt.high_risk_matches([path], opt.DEFAULT_HIGH_RISK_PATHS), path)

    def test_ordinary_code_is_not(self):
        for path in ("app/models/order.rb", "src/utils/format.ts", "tests/test_math.py"):
            self.assertFalse(opt.high_risk_matches([path], opt.DEFAULT_HIGH_RISK_PATHS), path)

    def test_it_over_matches_on_purpose(self):
        """`authors_controller.rb` is not an auth file, and matching it costs
        one extra reviewer. Missing `auth_controller.rb` costs an
        authorisation bug. The patterns err in the cheap direction."""
        self.assertTrue(opt.high_risk_matches(["app/authors_controller.rb"], ["*auth*"]))

    def test_the_match_names_the_file_and_the_pattern(self):
        hits = opt.high_risk_matches(["db/migrate/1.rb"], opt.DEFAULT_HIGH_RISK_PATHS)
        self.assertEqual(hits[0][0], "db/migrate/1.rb")
        self.assertTrue(hits[0][1])

    def test_one_file_is_reported_once_however_many_patterns_it_hits(self):
        hits = opt.high_risk_matches(["auth/session.sql"], opt.DEFAULT_HIGH_RISK_PATHS)
        self.assertEqual(len(hits), 1)

    def test_a_windows_path_is_matched_like_a_posix_one(self):
        """Snapshot metadata is written on whatever platform took it, and a
        review must not be careful on Linux and careless on Windows."""
        self.assertTrue(opt.high_risk_matches(["db\\migrate\\1.rb"], opt.DEFAULT_HIGH_RISK_PATHS))

    def test_junk_in_the_pattern_list_is_skipped(self):
        self.assertTrue(opt.high_risk_matches(["auth.py"], [None, "", 7, "*auth*"]))

    def test_an_empty_pattern_list_matches_nothing(self):
        self.assertFalse(opt.high_risk_matches(["db/migrate/1.rb"], []))


class TestPatternsNeedBothForms(unittest.TestCase):
    """``fnmatch`` has no ``**``, and a pattern containing a slash is matched
    against the whole path, so a nested-only pattern misses the root."""

    def test_deployment_directories_are_caught_at_the_repository_root(self):
        for path in ("k8s/service.yaml", "deploy/production.yaml", ".github/workflows/ci.yml"):
            self.assertTrue(opt.high_risk_matches([path], opt.DEFAULT_HIGH_RISK_PATHS), path)

    def test_and_still_caught_when_nested(self):
        for path in ("infra/k8s/svc.yaml", "ops/deploy/prod.yaml", "sub/.github/workflows/ci.yml"):
            self.assertTrue(opt.high_risk_matches([path], opt.DEFAULT_HIGH_RISK_PATHS), path)


class TestWhatIsJudgedAndWhatIsMeasured(unittest.TestCase):
    """Risk is judged from every path the change touches. Size is measured
    from the files a reviewer is actually shown. Using one list for both
    made a small change look big, or a dangerous one look safe."""

    def test_a_withheld_file_does_not_spend_the_file_budget(self):
        """A one-line fix beside a lockfile bump is a two-line review. The
        line count already excluded the lockfile; the file count did not."""
        plan = decide(
            paths=["app.py", "yarn.lock", "package-lock.json"],
            lines=4,
            level="aggressive",
            reviewed_files=1,
        )
        self.assertEqual(plan.reviewer_limit, 1)
        self.assertEqual(plan.files, 1)

    def test_a_withheld_file_is_still_judged_for_risk(self):
        """Withheld means its diff is not sent, not that it is harmless."""
        plan = decide(paths=["app.py", ".env"], lines=4, level="aggressive", reviewed_files=1)
        self.assertIsNone(plan.reviewer_limit)
        self.assertEqual(plan.level, "quality")

    def test_without_a_reviewed_count_the_path_list_is_used(self):
        plan = decide(paths=["a.py", "b.py", "c.py"], lines=4, level="aggressive")
        self.assertIsNone(plan.reviewer_limit)


class TestLevels(unittest.TestCase):
    def test_unknown_levels_fall_back_to_the_middle(self):
        for value in ("", None, "fast", 7, "QUALITY "):
            self.assertIn(opt.normalise_level(value), opt.LEVELS)
        self.assertEqual(opt.normalise_level("nonsense"), opt.DEFAULT_LEVEL)

    def test_a_level_is_read_case_insensitively(self):
        self.assertEqual(opt.normalise_level(" Quality "), "quality")

    def test_at_least_only_ever_raises(self):
        self.assertEqual(opt.at_least("aggressive", "quality"), "quality")
        self.assertEqual(opt.at_least("quality", "balanced"), "quality")


# --------------------------------------------------------------------------- the gate


class TestTheGate(unittest.TestCase):
    def test_a_recorded_failure_refuses(self):
        self.assertEqual(decide(test_status="failed").gate, opt.GATE_REFUSE)

    def test_a_recorded_pass_runs_silently(self):
        self.assertEqual(decide(test_status="ok").gate, opt.GATE_ALLOW)

    def test_nothing_recorded_warns_but_runs(self):
        """The whole reason the default can ship on: a workflow that never
        wrote a test result down keeps working, and is told how to opt in."""
        plan = decide(test_status="")
        self.assertEqual(plan.gate, opt.GATE_WARN)
        self.assertIn("state record test ok", plan.gate_note())

    def test_quality_never_gates(self):
        """`quality` means never skip a review to save money -- including the
        review of a red tree, where a reviewer may be how you find out why."""
        self.assertEqual(decide(test_status="failed", level="quality").gate, opt.GATE_ALLOW)
        self.assertEqual(decide(test_status="", level="quality").gate, opt.GATE_ALLOW)

    def test_the_words_for_failure_are_not_only_failed(self):
        for status in ("failed", "FAILED", "fail", "error", "red"):
            self.assertEqual(decide(test_status=status).gate, opt.GATE_REFUSE, status)

    def test_an_unrecognised_status_is_treated_as_recorded_not_as_failure(self):
        """A status this code has never heard of was still written by someone
        who ran something. Refusing on it would punish a vocabulary mismatch."""
        self.assertEqual(decide(test_status="partial").gate, opt.GATE_ALLOW)

    def test_the_refusal_says_how_to_get_past_it(self):
        note = decide(test_status="failed").gate_note()
        self.assertIn("--force", note)


# --------------------------------------------------------------------------- panel


class TestReducingThePanel(unittest.TestCase):
    def test_a_small_change_gets_one_reviewer_under_aggressive(self):
        self.assertEqual(decide(paths=["a.py"], lines=10, level="aggressive").reviewer_limit, 1)

    def test_balanced_never_reduces_the_panel(self):
        self.assertIsNone(decide(paths=["a.py"], lines=1, level="balanced").reviewer_limit)

    def test_too_many_files_keeps_the_panel(self):
        plan = decide(paths=["a.py", "b.py", "c.py"], lines=3, level="aggressive")
        self.assertIsNone(plan.reviewer_limit)

    def test_too_many_lines_keeps_the_panel(self):
        self.assertIsNone(decide(paths=["a.py"], lines=500, level="aggressive").reviewer_limit)

    def test_a_small_dangerous_change_keeps_the_panel(self):
        """One line in an auth file is the shape an authorisation bug arrives
        in. Size is exactly the wrong test for it."""
        plan = decide(paths=["app/auth.py"], lines=1, level="aggressive")
        self.assertIsNone(plan.reviewer_limit)
        self.assertEqual(plan.level, "quality")
        self.assertTrue(plan.escalated)

    def test_a_single_reviewer_panel_is_left_alone(self):
        self.assertIsNone(decide(paths=["a.py"], lines=1, level="aggressive", reviewers=1).reviewer_limit)

    def test_the_thresholds_are_configurable(self):
        plan = decide(
            paths=["a.py", "b.py", "c.py"],
            lines=300,
            level="aggressive",
            low_risk_max_files=5,
            low_risk_max_lines=400,
        )
        self.assertEqual(plan.reviewer_limit, 1)

    def test_a_nonsense_threshold_falls_back_to_the_default(self):
        plan = decide(paths=["a.py"], lines=10, level="aggressive", low_risk_max_files="two")
        self.assertEqual(plan.reviewer_limit, 1)

    def test_the_note_says_what_it_measured(self):
        note = decide(paths=["a.py"], lines=10, level="aggressive").reviewer_note()
        self.assertIn("1 file(s)", note)
        self.assertIn("10 line(s)", note)


class TestChoosingWhoStays(unittest.TestCase):
    def panel(self, *roles):
        return [{"id": "r%d" % i, "role": role} for i, role in enumerate(roles)]

    def test_a_general_reviewer_is_kept_over_a_specialist(self):
        """A lone security reviewer reports no correctness bugs: it was told
        not to look for them."""
        kept = opt.choose_reviewers(self.panel("security", "general"), 1)
        self.assertEqual([r["role"] for r in kept], ["general"])

    def test_configuration_order_decides_among_equals(self):
        kept = opt.choose_reviewers(self.panel("general", "general"), 1)
        self.assertEqual([r["id"] for r in kept], ["r0"])

    def test_with_no_general_reviewer_the_first_configured_one_stays(self):
        kept = opt.choose_reviewers(self.panel("security", "performance"), 1)
        self.assertEqual([r["id"] for r in kept], ["r0"])

    def test_a_limit_at_or_above_the_panel_changes_nothing(self):
        panel = self.panel("security", "general")
        self.assertEqual(opt.choose_reviewers(panel, 2), panel)
        self.assertEqual(opt.choose_reviewers(panel, None), panel)

    def test_the_kept_reviewers_stay_in_configuration_order(self):
        kept = opt.choose_reviewers(self.panel("security", "general", "general"), 2)
        self.assertEqual([r["id"] for r in kept], ["r1", "r2"])


# --------------------------------------------------------------------------- cap


class TestTheFindingsCap(unittest.TestCase):
    def test_each_level_asks_for_a_different_number(self):
        for level in opt.LEVELS:
            self.assertEqual(decide(level=level).max_findings, opt.MAX_FINDINGS_BY_LEVEL[level])

    def test_an_explicit_cap_overrides_the_level(self):
        self.assertEqual(decide(level="aggressive", max_findings=9).max_findings, 9)

    def test_an_explicit_zero_is_honoured_as_no_cap(self):
        """`0` is a decision, not a missing value, and must not read as one."""
        self.assertEqual(decide(level="quality", max_findings=0).max_findings, 0)

    def test_an_escalated_change_gets_the_quality_cap(self):
        self.assertEqual(decide(paths=["auth.py"], level="aggressive").max_findings, 10)


# --------------------------------------------------------------------------- config


class TestConfiguration(IsolatedCase):
    def check(self, section):
        data = config_mod.default_config()
        data["optimization"].update(section)
        return config_mod.validate(data)

    def test_the_defaults_validate(self):
        self.assertEqual(config_mod.validate(config_mod.default_config()), [])

    def test_the_default_level_is_balanced(self):
        self.assertEqual(config_mod.default_config()["optimization"]["level"], "balanced")

    def test_max_findings_ships_unset_so_the_level_decides(self):
        self.assertIsNone(config_mod.default_config()["review"]["max_findings"])

    def test_every_level_is_accepted(self):
        for level in opt.LEVELS:
            self.assertEqual(self.check({"level": level}), [], level)

    def test_an_unknown_level_is_rejected(self):
        self.assertTrue(any("optimization.level" in p for p in self.check({"level": "cheap"})))

    def test_high_risk_paths_must_be_a_list_of_strings(self):
        self.assertTrue(any("high_risk_paths" in p for p in self.check({"high_risk_paths": "*auth*"})))
        self.assertTrue(any("high_risk_paths[0]" in p for p in self.check({"high_risk_paths": [""]})))

    def test_an_empty_high_risk_list_is_allowed(self):
        """Someone whose paths this list cannot describe can replace it; the
        escalation itself is what they cannot switch off."""
        self.assertEqual(self.check({"high_risk_paths": []}), [])

    def test_thresholds_must_be_non_negative_integers(self):
        self.assertTrue(any("low_risk_max_files" in p for p in self.check({"low_risk_max_files": -1})))
        self.assertTrue(any("low_risk_max_lines" in p for p in self.check({"low_risk_max_lines": "50"})))

    def test_a_non_mapping_section_is_rejected(self):
        data = config_mod.default_config()
        data["optimization"] = []
        self.assertTrue(any("optimization" in p for p in config_mod.validate(data)))


class TestRecordedStages(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = ws.Workspace(self.project)
        self.workspace.ensure()

    def test_nothing_recorded_reads_as_the_empty_string(self):
        self.assertEqual(self.workspace.last_status("test"), "")

    def test_the_most_recent_record_wins(self):
        self.workspace.record_event("test", "failed")
        self.workspace.record_event("test", "ok")
        self.assertEqual(self.workspace.last_status("test"), "ok")

    def test_another_stage_does_not_answer_for_test(self):
        self.workspace.record_event("implementer", "failed")
        self.assertEqual(self.workspace.last_status("test"), "")


# --------------------------------------------------------------------------- pipeline


@unittest.skipUnless(has_git(), "git is required")
class TestTheGateInThePipeline(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.commit_all("init")
        self.write("app.py", "def add(a, b):\n    return a - b\n")

        mock_dir = os.path.join(self.tmp, "mock")
        os.makedirs(mock_dir)
        with open(os.path.join(mock_dir, "review.txt"), "w", encoding="utf-8") as handle:
            handle.write(FINDING)
        os.environ["DEV_ORCHESTRA_MOCK_DIR"] = mock_dir

        run_cli("config", "setup", "--defaults")
        run_cli("reviewer", "remove", "claude-general")
        run_cli("reviewer", "remove", "codex-general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "sec", "--role", "security")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "gen", "--role", "general")

    def set_level(self, level):
        data = config_mod.load(self.project).data
        data.setdefault("optimization", {})["level"] = level
        config_mod.write_config_file(config_mod.global_config_path(), data)

    def test_a_red_tree_is_refused(self):
        run_cli("state", "record", "test", "failed")
        run_cli("review", "snapshot")
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, ledger_mod.EXIT_BUDGET_EXHAUSTED)
        self.assertIn("refusing to review", err)

    def test_force_gets_past_the_gate(self):
        run_cli("state", "record", "test", "failed")
        run_cli("review", "snapshot")
        code, out, _ = run_cli("review", "run", "--force")
        self.assertEqual(code, 0)
        self.assertIn("successful", out)

    def test_a_green_tree_runs_without_a_note(self):
        run_cli("state", "record", "test", "ok")
        run_cli("review", "snapshot")
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertNotIn("no test result recorded", err)

    def test_an_unrecorded_tree_runs_with_a_note(self):
        run_cli("review", "snapshot")
        code, _, err = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertIn("no test result recorded", err)

    def test_nothing_is_delegated_when_the_gate_refuses(self):
        """The saving is the whole point: a refusal that still started the
        reviewers would cost exactly what it was meant to avoid."""
        run_cli("state", "record", "test", "failed")
        run_cli("review", "snapshot")
        run_cli("review", "run")
        _, out, _ = run_cli("tokens", "show", "--json")
        self.assertEqual(json.loads(out)["totals"]["runs"], 0)

    def test_status_reports_the_refusal_before_the_review_is_attempted(self):
        run_cli("state", "record", "test", "failed")
        run_cli("review", "snapshot")
        _, out, _ = run_cli("status", "--json")
        status = json.loads(out)
        self.assertEqual(status["optimization"]["gate"], "refuse")
        self.assertEqual(status["verdict"], "stop-and-report")


@unittest.skipUnless(has_git(), "git is required")
class TestThePanelInThePipeline(TestTheGateInThePipeline):
    def test_aggressive_runs_one_reviewer_on_a_small_change(self):
        self.set_level("aggressive")
        run_cli("state", "record", "test", "ok")
        run_cli("review", "snapshot")
        code, out, err = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertIn("1 successful, 0 failed", out)
        self.assertIn("low-risk change", err)

    def test_the_general_reviewer_is_the_one_that_runs(self):
        self.set_level("aggressive")
        run_cli("state", "record", "test", "ok")
        run_cli("review", "snapshot")
        _, out, _ = run_cli("review", "run", "--json")
        ran = [r["id"] for r in json.loads(out)["reviewers"]]
        self.assertEqual(ran, ["gen"])

    def test_a_high_risk_file_keeps_both_reviewers_and_says_why(self):
        self.set_level("aggressive")
        self.write("auth.py", "def check():\n    return True\n")
        run_cli("state", "record", "test", "ok")
        run_cli("review", "snapshot")
        code, out, err = run_cli("review", "run")
        self.assertEqual(code, 0)
        self.assertIn("2 successful", out)
        self.assertIn("aggressive", err)
        self.assertIn("auth.py", err)

    def test_balanced_keeps_both_reviewers(self):
        run_cli("state", "record", "test", "ok")
        run_cli("review", "snapshot")
        _, out, _ = run_cli("review", "run")
        self.assertIn("2 successful", out)

    def test_only_overrides_the_reduced_panel(self):
        """`--only` is someone naming the reviewers by hand. A level that
        overruled it would make the flag lie."""
        self.set_level("aggressive")
        run_cli("state", "record", "test", "ok")
        run_cli("review", "snapshot")
        _, out, _ = run_cli("review", "run", "--only", "sec", "gen", "--json")
        ran = sorted(r["id"] for r in json.loads(out)["reviewers"])
        self.assertEqual(ran, ["gen", "sec"])

    def test_the_decision_is_recorded_in_the_run_state(self):
        self.set_level("aggressive")
        run_cli("state", "record", "test", "ok")
        run_cli("review", "snapshot")
        run_cli("review", "run")
        events = ws.read_json(ws.Workspace(self.project).state_path, {}).get("events") or []
        reviews = [e for e in events if e.get("stage") == "review" and "optimization" in e]
        self.assertTrue(reviews)
        self.assertEqual(reviews[-1]["optimization"]["reviewer_limit"], 1)


@unittest.skipUnless(has_git(), "git is required")
class TestWhatTheSnapshotReportsAsChanged(IsolatedCase):
    """The lists the risk check and the size threshold are computed from."""

    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app/auth.py", "def check():\n    return True\n")
        self.write("app/main.py", "x = 1\n")
        self.commit_all("init")
        self.workspace = ws.Workspace(self.project)

    def snapshot(self):
        from orchestrator import review as review_mod

        return review_mod.create_snapshot(self.workspace)

    def test_a_deleted_file_is_reported_as_changed(self):
        """Its `+++` side is /dev/null, so reading only that side lost it --
        and deleting an auth file is not a smaller change than editing one."""
        os.unlink(os.path.join(self.project, "app/auth.py"))
        meta = self.snapshot()
        self.assertIn("app/auth.py", meta["files"])
        self.assertIn("app/auth.py", meta["changed_paths"])

    def test_deleting_a_high_risk_file_still_escalates(self):
        os.unlink(os.path.join(self.project, "app/auth.py"))
        meta = self.snapshot()
        plan = decide(paths=meta["changed_paths"], lines=2, level="aggressive")
        self.assertEqual(plan.level, "quality")

    def test_a_rename_reports_both_names(self):
        """The diff only carries where the file landed. A file renamed away
        from `auth.py` was an auth file until this commit."""
        self.git("mv", "app/auth.py", "app/helper.py")
        meta = self.snapshot()
        self.assertIn("app/helper.py", meta["changed_paths"])
        self.assertIn("app/auth.py", meta["changed_paths"])

    def test_a_withheld_file_is_in_the_paths_but_not_in_the_files(self):
        self.write("yarn.lock", "dep 1.0\n")
        self.commit_all("lock")
        self.write("yarn.lock", "dep 2.0\n")
        self.write("app/main.py", "x = 2\n")
        meta = self.snapshot()
        self.assertIn("yarn.lock", meta["changed_paths"])
        self.assertNotIn("yarn.lock", meta["files"])


@unittest.skipUnless(has_git(), "git is required")
class TestSnapshotLineCounts(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "a = 1\nb = 2\nc = 3\n")
        self.commit_all("init")
        self.workspace = ws.Workspace(self.project)

    def test_the_snapshot_counts_what_the_reviewer_will_see(self):
        self.write("app.py", "a = 1\nb = 9\nc = 3\nd = 4\n")
        meta = __import__("orchestrator.review", fromlist=["x"]).create_snapshot(self.workspace)
        self.assertEqual(meta["lines_added"], 2)
        self.assertEqual(meta["lines_deleted"], 1)

    def test_content_that_looks_like_a_header_is_still_counted(self):
        """A deleted `---` arrives as `----` and an added `++x` as `+++x`.
        Matching on those undercounts the change, and the count decides
        whether it is small enough for a single reviewer."""
        self.write("doc.md", "---\ntitle: x\n---\nbody\n")
        self.commit_all("doc")
        self.write("doc.md", "body\n")
        self.write("counter.c", "++i;\n+++j;\n")
        meta = __import__("orchestrator.review", fromlist=["x"]).create_snapshot(self.workspace)
        self.assertEqual(meta["lines_deleted"], 3)
        self.assertEqual(meta["lines_added"], 2)

    def test_a_withheld_file_contributes_no_lines(self):
        """A lockfile bump would otherwise make every dependency update look
        like a large change, when none of it is being reviewed."""
        self.write("yarn.lock", "\n".join("dep%d 1.0" % i for i in range(400)) + "\n")
        self.commit_all("lock")
        self.write("yarn.lock", "\n".join("dep%d 2.0" % i for i in range(400)) + "\n")
        self.write("app.py", "a = 1\nb = 2\nc = 4\n")
        meta = __import__("orchestrator.review", fromlist=["x"]).create_snapshot(self.workspace)
        self.assertTrue(meta["withheld"])
        self.assertLess(meta["lines_added"], 10)


if __name__ == "__main__":
    unittest.main()
