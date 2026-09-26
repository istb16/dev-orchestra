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
from orchestrator import review as review_mod
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


class TestTheReviewedFileList(unittest.TestCase):
    """Built from git's own listing rather than from the diff text.

    A mode-only change is the clearest case and the one that cannot be
    tested end to end: `git update-index --chmod` stages a mode, while
    `git diff HEAD` reads the working tree, and whether the two disagree
    depends on `core.filemode` -- which is false on Windows and true
    elsewhere. So the rule is tested against the record git produces.
    """

    def reviewed(self, tracked, withheld=(), suppressed=(), untracked=()):
        return review_mod._reviewed_files(tracked, withheld, suppressed, untracked)

    def test_a_mode_only_change_is_counted(self):
        """`0\t0\trun.sh`: git reports it, and the diff for it is `old mode` /
        `new mode` with no `+++` header to find."""
        self.assertEqual(self.reviewed([{"path": "run.sh", "added": 0, "deleted": 0}]), ["run.sh"])

    def test_a_binary_change_is_counted(self):
        """`-\t-\tlogo.png`: no counts, and "Binary files ... differ" for a body."""
        entry = {"path": "logo.png", "added": None, "deleted": None}
        self.assertEqual(self.reviewed([entry]), ["logo.png"])

    def test_a_withheld_file_is_left_out(self):
        tracked = [{"path": "app.py"}, {"path": "yarn.lock", "pattern": "*.lock"}]
        self.assertEqual(self.reviewed(tracked, withheld=[{"path": "yarn.lock"}]), ["app.py"])

    def test_something_not_under_review_is_left_out(self):
        tracked = [{"path": "app.py"}, {"path": ".dev-orchestra.yaml"}]
        self.assertEqual(self.reviewed(tracked, suppressed=[".dev-orchestra.yaml"]), ["app.py"])

    def test_untracked_files_are_added_back(self):
        """They are diffed separately, by name; git's listing of tracked
        changes cannot know about them."""
        self.assertEqual(self.reviewed([{"path": "app.py"}], untracked=["new.py"]), ["app.py", "new.py"])

    def test_a_path_is_listed_once(self):
        tracked = [{"path": "app.py"}, {"path": "app.py"}]
        self.assertEqual(self.reviewed(tracked, untracked=["app.py"]), ["app.py"])


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
        paths = ["f%d.py" % index for index in range(9)]
        plan = decide(paths=paths, lines=4, level="aggressive")
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

    def test_balanced_reduces_a_small_low_risk_change_too(self):
        """Restricting this to `aggressive` made it unreachable exactly where
        it was needed: a high-risk hit escalates to `quality`, and `quality` is
        not `aggressive`, so in a repository where `*.tf` matches on most
        rounds the dial could not fire at all. Measured over eleven real rounds
        at `balanced`: reduced zero times."""
        self.assertEqual(decide(paths=["a.py"], lines=1, level="balanced").reviewer_limit, 1)

    def test_quality_never_reduces_the_panel(self):
        """`quality` is the level that means "spend what it takes"."""
        self.assertIsNone(decide(paths=["a.py"], lines=1, level="quality").reviewer_limit)

    def test_a_small_high_risk_change_keeps_the_panel(self):
        """What actually stops a reduction, and the reason it is safe to let
        `balanced` reduce at all: risk, not size."""
        plan = decide(paths=["app/auth.py"], lines=1, level="balanced")
        self.assertIsNone(plan.reviewer_limit)
        self.assertEqual(plan.level, "quality")

    def test_too_many_files_keeps_the_panel(self):
        paths = ["f%d.py" % index for index in range(9)]
        plan = decide(paths=paths, lines=9, level="aggressive")
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
        self.workspace = self.cli_workspace()
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

    def test_a_refused_round_is_written_down(self):
        """Nothing ran, which is exactly why it has to be recorded: a skipped
        round is the largest thing the level ever saves, and a saving that
        leaves no trace cannot be counted."""
        run_cli("state", "record", "test", "failed")
        run_cli("review", "snapshot")
        run_cli("review", "run")
        report = json.loads(run_cli("optimization", "report", "--json")[1])
        self.assertEqual((report["rounds"], report["ran"], report["refused"]), (1, 0, 1))
        self.assertEqual(report["gates"], {"refuse": 1})

    def test_a_forced_round_is_recorded_as_having_run(self):
        run_cli("state", "record", "test", "failed")
        run_cli("review", "snapshot")
        run_cli("review", "run", "--force")
        report = json.loads(run_cli("optimization", "report", "--json")[1])
        self.assertEqual((report["ran"], report["refused"]), (1, 0))

    def test_a_refused_round_spends_no_review_budget(self):
        """It is not an attempt. Charging for one would make the gate cost the
        round it just declined to run."""
        run_cli("state", "record", "test", "failed")
        run_cli("review", "snapshot")
        run_cli("review", "run")
        tokens = json.loads(run_cli("tokens", "show", "--json")[1])
        self.assertEqual(tokens["totals"]["runs"], 0)

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

    def test_balanced_reduces_the_panel_for_a_small_low_risk_change(self):
        """The default level, which is the one almost every run uses."""
        run_cli("state", "record", "test", "ok")
        run_cli("review", "snapshot")
        _, out, _ = run_cli("review", "run")
        self.assertIn("1 successful", out)

    def test_a_high_risk_change_still_gets_the_whole_panel(self):
        self.write("auth.py", "def login():\n    return True\n")
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
        events = ws.read_json(self.cli_workspace().state_path, {}).get("events") or []
        reviews = [e for e in events if e.get("stage") == "review" and "optimization" in e]
        self.assertTrue(reviews)
        self.assertEqual(reviews[-1]["optimization"]["reviewer_limit"], 1)


# --------------------------------------------------------------------------- report


def round_event(status="ok", reviewers=1, billed=1000, **plan):
    settings = {
        "requested_level": "balanced",
        "level": "balanced",
        "escalated": False,
        "gate": "allow",
        "test_status": "ok",
        "reviewer_limit": None,
    }
    settings.update(plan)
    return {
        "stage": "review",
        "status": status,
        "optimization": settings,
        "reviewers": [{"usage": {"billed_tokens": billed}} for _ in range(reviewers)],
    }


def design_event(reviewers=1, billed=1000, status="ok"):
    """A design round as `_run_design_review` records it: no `optimization`
    block, because no level decided anything for a plan."""
    return {
        "stage": "design_review",
        "status": status,
        "reviewers": [{"usage": {"billed_tokens": billed}} for _ in range(reviewers)],
    }


class TestTheReport(unittest.TestCase):
    """A level's effect is a rate, not a number: how often it refused, how
    often it cut the panel. Counting that is the whole point of recording a
    round nobody ran."""

    def test_nothing_recorded_reports_nothing_rather_than_zeroes(self):
        report = opt.summarise_rounds([])
        self.assertEqual(report["rounds"], 0)
        self.assertEqual(report["billed_per_round"], None)

    def test_events_from_other_stages_are_not_rounds(self):
        events = [{"stage": "test", "status": "ok"}, {"stage": "implementer", "status": "ok"}]
        self.assertEqual(opt.summarise_rounds(events)["rounds"], 0)

    def test_a_review_without_a_plan_is_not_counted(self):
        """Rounds recorded before the level existed have nothing to say about
        it, and counting them would dilute every rate below."""
        self.assertEqual(opt.summarise_rounds([{"stage": "review", "status": "ok"}])["rounds"], 0)

    def test_a_refused_round_is_counted_but_spent_nothing(self):
        report = opt.summarise_rounds([round_event(status=opt.REFUSED, reviewers=0, gate="refuse")])
        self.assertEqual((report["rounds"], report["ran"], report["refused"]), (1, 0, 1))
        self.assertEqual(report["billed_tokens"], 0)

    def test_the_saving_is_the_mean_of_the_rounds_that_did_run(self):
        """What a round that did not happen would have cost is unknowable, so
        the estimate is the closest honest stand-in -- and is labelled one."""
        events = [
            round_event(reviewers=2, billed=1000),
            round_event(reviewers=2, billed=1000),
            round_event(status=opt.REFUSED, reviewers=0, gate="refuse"),
        ]
        report = opt.summarise_rounds(events)
        self.assertEqual(report["billed_per_round"], 2000)
        self.assertEqual(report["estimated_saving"], 2000)

    def test_the_two_refusals_are_counted_apart_and_only_one_is_priced(self):
        """A round refused for the size of its change was never going to be an
        average round, so pricing it at the mean of the rounds that ran would
        understate what was not spent. It is counted and left unpriced."""
        gate = round_event(status=opt.REFUSED, reviewers=0, gate="refuse")
        gate["refused_by"] = "gate"
        context = round_event(status=opt.REFUSED, reviewers=0)
        context["refused_by"] = "context"
        report = opt.summarise_rounds([round_event(reviewers=2, billed=1000), gate, context])
        self.assertEqual(report["refused"], 2)
        self.assertEqual(report["refused_by"], {"gate": 1, "context": 1})
        self.assertEqual(report["billed_per_round"], 2000)
        self.assertEqual(report["estimated_saving"], 2000)

    def test_a_refusal_recorded_before_the_reason_existed_is_the_gates(self):
        """Nothing else refused a round then, so reading it as the gate's is
        reading it as what it was."""
        report = opt.summarise_rounds([round_event(status=opt.REFUSED, reviewers=0, gate="refuse")])
        self.assertEqual(report["refused_by"], {"gate": 1})

    def test_a_refused_design_round_is_counted_apart_from_the_ones_that_ran(self):
        """The design tally counts rounds that ran, so a refused one would
        otherwise be invisible in the one report that says what review cost."""
        report = opt.summarise_rounds([design_event(), design_event(status=opt.REFUSED, reviewers=0)])
        self.assertEqual(report["design_rounds"], 1)
        self.assertEqual(report["design_refused"], 1)

    def test_no_round_ever_ran_means_no_estimate_rather_than_zero(self):
        """Two refusals and nothing to compare them against is not a saving of
        zero; it is a saving nobody can size yet."""
        report = opt.summarise_rounds([round_event(status=opt.REFUSED, reviewers=0)] * 2)
        self.assertIsNone(report["billed_per_round"])
        self.assertEqual(report["estimated_saving"], 0)

    def test_levels_and_gates_are_counted_separately(self):
        events = [
            round_event(level="aggressive", gate="allow"),
            round_event(level="aggressive", gate="warn"),
            round_event(level="quality", gate="allow", escalated=True),
        ]
        report = opt.summarise_rounds(events)
        self.assertEqual(report["levels"], {"aggressive": 2, "quality": 1})
        self.assertEqual(report["gates"], {"allow": 2, "warn": 1})
        self.assertEqual(report["escalated"], 1)

    def test_the_patterns_that_forced_an_escalation_are_named(self):
        """A dial escalated out of existence on every round looks identical in
        a count to one that never fires. Only the pattern says which -- and
        whether it is the one to replace."""
        event = round_event(escalated=True, level="quality")
        event["optimization"]["high_risk"] = [
            {"path": "infra/main.tf", "pattern": "*.tf"},
            {"path": "infra/dns.tf", "pattern": "*.tf"},
            {"path": ".github/workflows/ci.yml", "pattern": ".github/workflows/*"},
        ]
        report = opt.summarise_rounds([event])
        self.assertEqual(report["escalation_patterns"], {"*.tf": 2, ".github/workflows/*": 1})

    def test_every_round_escalating_is_reported_as_such(self):
        """Measured on a real repository: `aggressive` was configured and the
        level never once applied, because terraform is touched constantly."""
        report = opt.summarise_rounds([round_event(escalated=True)] * 3)
        self.assertTrue(report["always_escalated"])

    def test_one_round_escaping_escalation_is_not_always(self):
        report = opt.summarise_rounds([round_event(escalated=True), round_event()])
        self.assertFalse(report["always_escalated"])

    def test_no_rounds_at_all_is_not_always_escalated(self):
        self.assertFalse(opt.summarise_rounds([])["always_escalated"])

    def test_a_reduced_panel_is_counted(self):
        events = [round_event(reviewer_limit=1), round_event(reviewer_limit=None)]
        self.assertEqual(opt.summarise_rounds(events)["panel_reduced"], 1)

    def test_rounds_with_no_test_result_are_counted_apart(self):
        """The difference between "the level had no effect" and "the gate was
        never given anything to act on", which the totals alone hide."""
        events = [round_event(test_status=""), round_event(test_status="ok")]
        self.assertEqual(opt.summarise_rounds(events)["rounds_without_a_test_result"], 1)

    def test_a_reviewer_that_reported_no_usage_is_counted_but_not_summed(self):
        event = round_event(reviewers=0)
        event["reviewers"] = [{"usage": {}}, {"usage": {"billed_tokens": 500}}]
        report = opt.summarise_rounds([event])
        self.assertEqual((report["reviewer_runs"], report["measured_runs"]), (2, 1))
        self.assertEqual(report["billed_tokens"], 500)

    def test_junk_in_the_log_does_not_stop_the_report(self):
        """It is read from a file other processes append to; a report that
        crashes on one bad entry is a report nobody trusts."""
        events = ["not an event", None, {"stage": "review"}, round_event()]
        self.assertEqual(opt.summarise_rounds(events)["rounds"], 1)

    def test_a_design_round_is_counted_apart_from_a_code_round(self):
        """Measured on one workflow: two design rounds and 350,429 billed
        tokens existed in `tokens show` and nowhere in this report."""
        events = [
            design_event(reviewers=2, billed=1000),
            design_event(reviewers=2, billed=1000),
            round_event(reviewers=2, billed=500),
        ]
        report = opt.summarise_rounds(events)
        self.assertEqual(report["design_rounds"], 2)
        self.assertEqual((report["design_reviewer_runs"], report["design_measured_runs"]), (4, 4))
        self.assertEqual(report["design_billed_tokens"], 4000)
        self.assertEqual(report["design_billed_per_round"], 2000)

    def test_the_code_figures_do_not_move_when_a_design_round_is_present(self):
        """`reviewer_runs` already means code review and `optimization report
        --json` is a public format, so the new cost arrives as new keys. A
        figure that silently grew would be a different bug, not a fix."""
        code = [round_event(reviewers=2, billed=500), round_event(reviewers=2, billed=500)]
        alone = opt.summarise_rounds(code)
        beside = opt.summarise_rounds([*code, design_event(reviewers=2, billed=9000)])
        for key in ("rounds", "ran", "reviewer_runs", "measured_runs", "billed_tokens"):
            self.assertEqual(beside[key], alone[key], key)
        self.assertEqual(beside["reviewer_runs"], 4)
        self.assertEqual(beside["billed_per_round"], 1000)

    def test_design_rounds_alone_leave_the_code_figures_at_zero(self):
        """Nothing was reviewed against a diff, so no level was asked anything
        -- and the levels tally has to stay empty rather than claim one."""
        report = opt.summarise_rounds([design_event(reviewers=2, billed=700)])
        self.assertEqual((report["rounds"], report["ran"], report["reviewer_runs"]), (0, 0, 0))
        self.assertEqual(report["billed_tokens"], 0)
        self.assertIsNone(report["billed_per_round"])
        self.assertEqual(report["levels"], {})
        self.assertEqual(report["design_billed_tokens"], 1400)

    def test_a_design_run_that_reported_no_usage_is_counted_but_not_summed(self):
        event = design_event(reviewers=0)
        event["reviewers"] = [{"usage": {}}, {"usage": {"billed_tokens": 500}}]
        report = opt.summarise_rounds([event])
        self.assertEqual((report["design_reviewer_runs"], report["design_measured_runs"]), (2, 1))
        self.assertEqual(report["design_billed_tokens"], 500)

    def test_no_design_round_reports_no_design_spend(self):
        report = opt.summarise_rounds([round_event(reviewers=2, billed=500)])
        self.assertEqual(report["design_rounds"], 0)
        self.assertEqual(report["design_reviewer_runs"], 0)
        self.assertEqual(report["design_billed_tokens"], 0)
        self.assertIsNone(report["design_billed_per_round"])

    def test_a_round_that_did_not_finish_is_not_a_round(self):
        """A round that raised or was abandoned after a kill billed nothing, so
        counting it in the divisor reports half of what the round that did run
        cost -- a per-round figure halved by a round that never ran."""
        events = [
            design_event(reviewers=2, billed=175_214),
            {"stage": "design_review", "status": "failed", "error": "boom"},
            {"stage": "design_review", "status": "abandoned", "reason": "killed"},
        ]
        report = opt.summarise_rounds(events)
        self.assertEqual(report["design_rounds"], 1)
        self.assertEqual(report["design_billed_tokens"], 350_428)
        self.assertEqual(report["design_billed_per_round"], 350_428)

    def test_a_round_where_every_reviewer_failed_still_ran(self):
        """It cost its attempt: a round with zero runs, not a round that did
        not happen -- and the one worth printing, since it produced nothing."""
        report = opt.summarise_rounds([design_event(reviewers=0)])
        self.assertEqual((report["design_rounds"], report["design_reviewer_runs"]), (1, 0))
        self.assertIsNone(report["design_billed_per_round"])


def round_with(usages, status="ok"):
    """A round whose reviewer usage is spelled out, rather than counted up."""
    event = round_event(status=status, reviewers=0)
    event["reviewers"] = [{"usage": dict(usage)} for usage in usages]
    return event


def design_with(usages):
    event = design_event(reviewers=0)
    event["reviewers"] = [{"usage": dict(usage)} for usage in usages]
    return event


class TestToolActivityInTheReport(unittest.TestCase):
    """Per run, over the runs that reported -- never over every reviewer run.

    Codex reports no tool activity, so a panel of one Claude and one Codex
    divided by `reviewer_runs` halves the figure for no reason but the panel's
    composition. The comparison this measurement exists for -- fewer tool calls
    once reviewers are handed the context they were re-reading -- would then
    move whenever a reviewer is added or dropped.
    """

    def test_a_silent_reviewer_stays_out_of_the_denominator(self):
        events = [round_with([{"tool_uses": 4, "tool_output_chars": 900}, {"billed_tokens": 100}])]
        report = opt.summarise_rounds(events)
        self.assertEqual(report["reviewer_runs"], 2)
        self.assertEqual(report["tool_reported_runs"], 1)
        self.assertEqual(report["tool_uses"], 4)
        self.assertEqual(report["tool_uses_per_run"], 4.0)
        self.assertEqual(report["tool_output_chars_per_run"], 900.0)

    def test_a_reviewer_that_used_no_tools_is_in_it(self):
        """A measured zero is the result this counting exists to find."""
        events = [round_with([{"tool_uses": 6, "tool_output_chars": 600}, {"tool_uses": 0}])]
        report = opt.summarise_rounds(events)
        self.assertEqual(report["tool_reported_runs"], 2)
        self.assertEqual(report["tool_uses_per_run"], 3.0)

    def test_nobody_reporting_gives_no_average_rather_than_zero(self):
        report = opt.summarise_rounds([round_event(reviewers=2, billed=500)])
        self.assertEqual(report["tool_reported_runs"], 0)
        self.assertIsNone(report["tool_uses_per_run"])
        self.assertIsNone(report["tool_output_chars_per_run"])

    def test_a_refused_round_contributes_nothing(self):
        events = [round_with([{"tool_uses": 4}], status=opt.REFUSED)]
        self.assertEqual(opt.summarise_rounds(events)["tool_reported_runs"], 0)

    def test_a_boolean_is_not_a_count(self):
        """``True`` is an ``int`` in Python, and would report one tool use."""
        report = opt.summarise_rounds([round_with([{"tool_uses": True}])])
        self.assertEqual(report["tool_reported_runs"], 0)

    def test_design_rounds_are_counted_under_their_own_keys(self):
        """The same split the billed figures already keep: a plan and a diff
        are not the same unit of work."""
        events = [
            round_with([{"tool_uses": 2, "tool_output_chars": 100}]),
            design_with([{"tool_uses": 10, "tool_output_chars": 5000}]),
        ]
        report = opt.summarise_rounds(events)
        self.assertEqual(report["tool_uses_per_run"], 2.0)
        self.assertEqual(report["design_tool_uses_per_run"], 10.0)
        self.assertEqual(report["design_tool_output_chars_per_run"], 5000.0)


@unittest.skipUnless(has_git(), "git is required")
class TestTheReportCommand(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace()
        self.workspace.ensure()

    def test_an_empty_log_says_so_instead_of_printing_a_table_of_zeroes(self):
        code, out, _ = run_cli("optimization", "report")
        self.assertEqual(code, 0)
        self.assertIn("No review rounds recorded", out)

    def test_it_reads_the_run_log_rather_than_the_ledger(self):
        """`budget reset` clears the ledger; the question spans workflows."""
        self.workspace.record_event("review", "ok", round_event(reviewers=2, billed=700))
        run_cli("budget", "reset")
        report = json.loads(run_cli("optimization", "report", "--json")[1])
        self.assertEqual(report["billed_tokens"], 1400)

    def test_it_reads_every_workflow_rather_than_only_this_one(self):
        """The reason it reads the run log at all is that a level's effect is a
        rate, and a rate needs rounds. 0.4.0 split the run log per workflow,
        which quietly narrowed this to a handful of rounds -- the very thing
        reading the log instead of the ledger was meant to avoid."""
        from orchestrator import workspace as ws_mod

        self.workspace.record_event("review", "ok", round_event(reviewers=1, billed=500))
        other = ws_mod.Workspace(self.project, workflow="elsewhere").ensure()
        other.record_event("review", "ok", round_event(reviewers=1, billed=700))
        report = json.loads(run_cli("optimization", "report", "--json")[1])
        self.assertEqual(report["rounds"], 2)
        self.assertEqual(report["billed_tokens"], 1200)

    def test_one_workflow_can_still_be_asked_about_on_its_own(self):
        """ "What did the level do in this piece of work" is a fair question
        too; it is just not the one the bare command answers."""
        from orchestrator import workspace as ws_mod

        self.workspace.record_event("review", "ok", round_event(reviewers=1, billed=500))
        other = ws_mod.Workspace(self.project, workflow="elsewhere").ensure()
        other.record_event("review", "ok", round_event(reviewers=1, billed=700))
        report = json.loads(run_cli("--workflow", "elsewhere", "optimization", "report", "--json")[1])
        self.assertEqual(report["rounds"], 1)
        self.assertEqual(report["billed_tokens"], 700)

    def test_the_empty_message_names_where_it_looked(self):
        _, out, _ = run_cli("optimization", "report")
        self.assertIn(".ai", out)

    def test_the_refusal_notice_names_the_command_that_fixes_it(self):
        self.workspace.record_event("review", "ok", round_event(test_status=""))
        _, out, _ = run_cli("optimization", "report")
        self.assertIn("state record test", out)

    def test_a_round_with_a_test_result_gets_no_notice(self):
        self.workspace.record_event("review", "ok", round_event(test_status="ok"))
        _, out, _ = run_cli("optimization", "report")
        self.assertNotIn("state record test", out)

    def test_the_report_says_the_level_never_applied(self):
        for _ in range(2):
            self.workspace.record_event("review", "ok", round_event(escalated=True, level="quality"))
        _, out, _ = run_cli("optimization", "report")
        self.assertIn("every round escalated", out)
        self.assertIn("never applied", out)

    def test_a_refused_round_shows_up_in_the_summary(self):
        """It ran nothing, so it appears nowhere else in the final report --
        and what was skipped is exactly what that report has to name."""
        self.workspace.record_event("review", opt.REFUSED, round_event(status=opt.REFUSED, reviewers=0))
        _, out, _ = run_cli("summary")
        self.assertIn("Optimization:", out)
        self.assertIn("1 round(s) not run", out)

    def test_a_reduced_panel_shows_up_in_the_summary(self):
        """One reviewer is one opinion. A report that does not say so reads
        exactly like a report of two independent ones."""
        self.workspace.record_event("review", "ok", round_event(reviewer_limit=1))
        _, out, _ = run_cli("summary")
        self.assertIn("cut to one reviewer", out)

    def test_an_ordinary_run_gets_no_optimization_section(self):
        """Nothing was skipped or cut, so there is nothing to report."""
        self.workspace.record_event("review", "ok", round_event())
        _, out, _ = run_cli("summary")
        self.assertNotIn("Optimization:", out)

    def test_the_tool_row_prints_the_denominator_it_divided_by(self):
        """Uses per run over half a panel is a different claim from the same
        figure over all of it, and only the count beside it says which."""
        event = round_with([{"tool_uses": 4, "tool_output_chars": 900}, {"billed_tokens": 5}])
        self.workspace.record_event("review", "ok", event)
        _, out, _ = run_cli("optimization", "report")
        self.assertIn("Tool activity", out)
        self.assertIn("1 of 2 run(s) reported", out)
        self.assertIn("not source read", out)

    def test_a_log_with_no_tool_activity_gets_no_tool_row(self):
        """Nothing reported is not an average of zero, and a row of zeroes
        would read as one."""
        self.workspace.record_event("review", "ok", round_event(reviewers=2, billed=700))
        _, out, _ = run_cli("optimization", "report")
        self.assertNotIn("Tool activity", out)

    def test_the_estimate_says_it_is_one(self):
        self.workspace.record_event("review", "ok", round_event(reviewers=1, billed=900))
        self.workspace.record_event("review", opt.REFUSED, round_event(status=opt.REFUSED, reviewers=0))
        _, out, _ = run_cli("optimization", "report")
        self.assertIn("Estimated saving", out)
        self.assertIn("unknowable", out)

    def test_both_stages_and_their_sum_are_shown(self):
        """One `Reviewer runs:` number that silently meant code review only is
        the bug: it answered 8 for a workflow that had run 12 reviewers."""
        self.workspace.record_event("review", "ok", round_event(reviewers=2, billed=1000))
        self.workspace.record_event("design_review", "ok", design_event(reviewers=2, billed=3000))
        _, out, _ = run_cli("optimization", "report")
        self.assertIn("Reviewer runs: 4 (4 reported usage), 8,000 billed", out)
        self.assertIn("code review", out)
        self.assertIn("2 (2 reported usage), 2,000 billed over 1 round(s)", out)
        self.assertIn("design review", out)
        self.assertIn("6,000 billed over 1 round(s)", out)

    def test_a_project_with_design_review_off_sees_no_design_line(self):
        """A `0` row for a stage that never ran is noise pretending to be a
        measurement."""
        self.workspace.record_event("review", "ok", round_event(reviewers=2, billed=1000))
        _, out, _ = run_cli("optimization", "report")
        self.assertNotIn("design", out)
        self.assertIn("Reviewer runs: 2 (2 reported usage), 2,000 billed", out)
        self.assertIn("2,000 billed per round that ran", out)

    def test_design_rounds_alone_are_reported_without_claiming_a_level(self):
        """No level decided anything for a plan, so there is no `levels in
        force` to print -- but 350,429 billed tokens still have to appear."""
        self.workspace.record_event("design_review", "ok", design_event(reviewers=2, billed=3000))
        code, out, _ = run_cli("optimization", "report")
        self.assertEqual(code, 0)
        self.assertNotIn("levels in force", out)
        self.assertNotIn("Review rounds recorded", out)
        self.assertIn("design review", out)
        self.assertIn("6,000 billed over 1 round(s)", out)

    def test_a_design_round_alone_is_not_an_empty_log(self):
        """It says what review cost, and a design round cost something."""
        self.workspace.record_event("design_review", "ok", design_event(reviewers=1, billed=800))
        _, out, _ = run_cli("optimization", "report")
        self.assertNotIn("No review rounds recorded", out)

    def test_a_design_round_that_failed_prints_no_design_row(self):
        """`design review 0 (0 reported usage), 0 billed over 1 round(s)` is
        the zero pretending to be a measurement this report exists to avoid."""
        self.workspace.record_event("design_review", "failed", {"error": "boom"})
        _, out, _ = run_cli("optimization", "report")
        self.assertNotIn("design review", out)
        self.assertIn("No review rounds recorded", out)

    def test_no_round_with_context_prints_no_context_block(self):
        self.workspace.record_event("review", "ok", context_round(1000, [{"billed_tokens": 100}]))
        _, out, _ = run_cli("optimization", "report")
        self.assertNotIn("Surrounding context", out)
        report = json.loads(run_cli("optimization", "report", "--json")[1])
        self.assertEqual(report["by_context"]["with"]["rounds"], 0)
        self.assertEqual(report["by_context"]["without"]["rounds"], 1)

    def test_a_round_with_context_prints_both_groups_and_the_per_run_lines(self):
        usage = {"billed_tokens": 1000, "tool_uses": 2, "tool_output_chars": 500}
        with_context = context_round(2000, [usage, usage], adopted=300, trimmed=40)
        self.workspace.record_event("review", "ok", with_context)
        self.workspace.record_event("review", "ok", context_round(2000, [usage]))
        _, out, _ = run_cli("optimization", "report")
        self.assertIn("Surrounding context (review.context.surrounding), code review rounds only:", out)
        self.assertIn("with context", out)
        self.assertIn("without context", out)
        self.assertIn("300 context chars adopted, 40 left out", out)
        per_run = "per run and 1k chars of change (1 sized round(s), 2,000 chars): "
        self.assertIn(per_run + "500.0 billed over 2 billed run(s)", out)
        self.assertIn("250.0 observed output chars over 1 reporting run(s)", out)
        self.assertIn("compare the per-run lines", out)
        self.assertIn("Codex reports no tool activity", out)


def context_round(change_chars, usages, adopted=0, trimmed=0, summary=True, measurement=None, statuses=None):
    """A code round as `review run` records it with `review.context.surrounding` on.

    ``measurement`` is the block `review run --surrounding` adds, and with it
    each reviewer entry gets the id, provider, model, role and status a pair
    is compared on; ``statuses`` spells those out per entry.
    """
    event = round_with(usages)
    for index, run in enumerate(event["reviewers"]):
        if change_chars is not None:
            run["change_chars"] = change_chars
        if measurement is not None:
            run.update(
                {
                    "id": "r%d" % (index + 1),
                    "provider": "claude",
                    "model": "opus",
                    "role": "general",
                    "status": "ok",
                }
            )
        if statuses is not None:
            run["status"] = statuses[index]
    if measurement is not None:
        event["measurement"] = measurement
    if summary and (adopted or trimmed):
        event["surrounding"] = {
            "mode": "enclosing",
            "adopted_chars": adopted,
            "trimmed_chars": trimmed,
            "adopted": 1 if adopted else 0,
            "trimmed": 1 if trimmed else 0,
        }
    return event


class TestRoundsWithAndWithoutContext(unittest.TestCase):
    """The split `optimization report` compares, normalised by size and panel.

    The raw figures move with how big each change was and with how many
    reviewers ran it, and neither has anything to do with the context. So the
    comparison is per run and per 1k chars of change, over the rounds that
    recorded their size and the runs that reported each figure.
    """

    def test_a_round_is_split_on_its_own_summary(self):
        report = opt.summarise_rounds(
            [
                context_round(1000, [{"billed_tokens": 100}], adopted=50),
                context_round(1000, [{"billed_tokens": 100}]),
            ]
        )
        self.assertEqual(report["by_context"]["with"]["rounds"], 1)
        self.assertEqual(report["by_context"]["with"]["adopted_chars"], 50)
        self.assertEqual(report["by_context"]["without"]["rounds"], 1)

    def test_without_a_summary_the_reviewer_entries_decide(self):
        event = context_round(1000, [{"billed_tokens": 100}])
        event["reviewers"][0]["surrounding"] = {"mode": "enclosing", "adopted_chars": 70, "trimmed_chars": 0}
        report = opt.summarise_rounds([event])
        self.assertEqual(report["by_context"]["with"]["rounds"], 1)
        self.assertEqual(report["by_context"]["with"]["adopted_chars"], 70)

    def test_a_round_that_adopted_nothing_is_a_round_without(self):
        """File delivery, or a budget already spent: the setting was on and
        no reviewer was shown any context."""
        report = opt.summarise_rounds([context_round(1000, [{"billed_tokens": 100}], trimmed=900)])
        self.assertEqual(report["by_context"]["with"]["rounds"], 0)
        self.assertEqual(report["by_context"]["without"]["rounds"], 1)
        self.assertEqual(report["by_context"]["without"]["trimmed_chars"], 900)

    def test_refused_and_design_rounds_are_in_neither_group(self):
        refused = context_round(1000, [{"billed_tokens": 100}], adopted=50)
        refused["status"] = opt.REFUSED
        design = design_event()
        design["surrounding"] = {"adopted_chars": 50}
        report = opt.summarise_rounds([refused, design])
        for name in ("with", "without"):
            self.assertEqual(report["by_context"][name]["rounds"], 0)

    def test_nobody_reporting_tools_gives_no_tool_figures(self):
        report = opt.summarise_rounds([context_round(1000, [{"billed_tokens": 100}], adopted=5)])
        group = report["by_context"]["with"]
        self.assertIsNone(group["tool_uses_per_run"])
        self.assertIsNone(group["tool_output_chars_per_run"])
        self.assertIsNone(group["tool_output_chars_per_run_per_1k_change_chars"])
        self.assertEqual(group["billed_per_run_per_1k_change_chars"], 100.0)

    def test_a_round_with_no_recorded_size_is_left_out_of_the_normalised_figures(self):
        report = opt.summarise_rounds(
            [
                context_round(1000, [{"billed_tokens": 100}], adopted=5),
                context_round(None, [{"billed_tokens": 900}], adopted=5),
            ]
        )
        group = report["by_context"]["with"]
        self.assertEqual((group["rounds"], group["sized_rounds"]), (2, 1))
        self.assertEqual(group["billed_tokens"], 1000)
        self.assertEqual(group["sized_billed_tokens"], 100)
        self.assertEqual(group["change_chars"], 1000)
        self.assertEqual(group["billed_per_run_per_1k_change_chars"], 100.0)

    def test_the_per_run_figures_do_not_move_with_the_panel(self):
        """A panel cut to one reviewer is ordinary for a small change, and
        must not read as the context halving what a round costs."""
        usage = {"billed_tokens": 1000, "tool_uses": 3, "tool_output_chars": 600}
        report = opt.summarise_rounds(
            [
                context_round(2000, [usage, usage], adopted=100),
                context_round(2000, [usage]),
            ]
        )
        with_, without = report["by_context"]["with"], report["by_context"]["without"]
        self.assertNotEqual(with_["billed_tokens"], without["billed_tokens"])
        self.assertEqual(with_["billed_per_run_per_1k_change_chars"], 500.0)
        self.assertEqual(
            with_["billed_per_run_per_1k_change_chars"], without["billed_per_run_per_1k_change_chars"]
        )
        self.assertEqual(
            with_["tool_output_chars_per_run_per_1k_change_chars"],
            without["tool_output_chars_per_run_per_1k_change_chars"],
        )

    def test_a_run_that_billed_nothing_is_not_in_the_billed_weight(self):
        report = opt.summarise_rounds([context_round(1000, [{"billed_tokens": 400}, {}], adopted=5)])
        group = report["by_context"]["with"]
        self.assertEqual(group["billed_run_change_chars"], 1000)
        self.assertEqual(group["sized_billed_runs"], 1)
        self.assertEqual(group["billed_per_run_per_1k_change_chars"], 400.0)

    def test_a_run_reporting_no_tools_is_not_in_the_tool_weight(self):
        """Codex reports billed tokens and no tool activity at all."""
        claude = {"billed_tokens": 100, "tool_uses": 2, "tool_output_chars": 300}
        codex = {"billed_tokens": 100}
        group = opt.summarise_rounds([context_round(1000, [claude, codex], adopted=5)])["by_context"]["with"]
        self.assertEqual(group["billed_run_change_chars"], 2000)
        self.assertEqual(group["tool_run_change_chars"], 1000)
        self.assertEqual(group["tool_output_chars_per_run_per_1k_change_chars"], 300.0)


def measured(
    side, workflow="wf", snapshot="s" * 64, tree="t" * 40, head="h" * 40, base=None, epoch="e1", **inputs
):
    """The `measurement` block of a `review run --surrounding` event."""
    values = {
        "context_sha256": "",
        "max_findings": 12,
        "inline_chars": 60000,
        "max_chars": 200000,
        "force": False,
    }
    values.update(inputs)
    return {
        "surrounding": side,
        "snapshot": snapshot,
        "tree": tree,
        "head": head,
        "base": base,
        "workflow": workflow,
        "epoch": epoch,
        "rerun": False,
        "inputs": values,
    }


def paired_round(side, usages, at="2026-09-01T00:00:00Z", adopted=None, statuses=None, **block):
    """One side of a pair; the enclosing side adopts context unless told otherwise."""
    if adopted is None:
        adopted = 100 if side == "enclosing" else 0
    event = context_round(
        1000, usages, adopted=adopted, measurement=measured(side, **block), statuses=statuses
    )
    event["at"] = at
    return event


CLAUDE = {"billed_tokens": 1000, "tool_uses": 3, "tool_output_chars": 900}
CODEX = {"billed_tokens": 2000}


class TestPairedOnOneSnapshot(unittest.TestCase):
    """Two runs of one frozen snapshot, one with the context and one without.

    A pair is listed whatever it holds and counted only when nothing but the
    context differed: the panel, a delivered review from every run, the other
    prompt inputs -- and the context side adopted something.
    """

    def paired(self, *events):
        return opt.summarise_rounds(list(events))["paired"]

    def test_the_two_sides_of_one_snapshot_are_a_pair(self):
        paired = self.paired(
            paired_round("none", [dict(CLAUDE, billed_tokens=1200, tool_uses=5)]),
            paired_round("enclosing", [CLAUDE]),
        )
        self.assertEqual((paired["pairs_listed"], paired["pairs_total"]), (1, 1))
        (pair,) = paired["pairs"]
        self.assertTrue(pair["counted"])
        self.assertEqual(pair["workflow"], "wf")
        self.assertEqual(pair["snapshot"], "s" * 64)
        member = {"id": "r1", "provider": "claude", "model": "opus", "role": "general"}
        self.assertEqual(pair["panel"], [member])
        self.assertIsNone(pair["without_panel"])
        self.assertEqual(pair["adopted_chars"], 100)
        self.assertEqual(pair["change_chars"], 1000)
        expected = {"billed_per_run": -200.0, "tool_uses_per_run": -2.0, "tool_output_chars_per_run": 0.0}
        self.assertEqual(pair["delta"], expected)
        self.assertEqual(paired["with"]["billed_per_run"], 1000.0)
        self.assertEqual(paired["without"]["billed_per_run"], 1200.0)
        self.assertEqual(paired["delta"]["billed_per_run"], -200.0)
        self.assertEqual(paired["note"], opt.PAIRED_NOTE)

    def test_another_tree_or_workflow_is_not_the_same_snapshot(self):
        for other in ({"tree": "u" * 40}, {"workflow": "elsewhere"}, {"snapshot": "x" * 64}):
            paired = self.paired(paired_round("none", [CLAUDE]), paired_round("enclosing", [CLAUDE], **other))
            self.assertEqual(paired["pairs"], [], other)

    def test_another_epoch_is_still_a_pair_and_both_are_shown(self):
        paired = self.paired(
            paired_round("none", [CLAUDE], epoch="e1"), paired_round("enclosing", [CLAUDE], epoch="e2")
        )
        (pair,) = paired["pairs"]
        self.assertEqual(pair["epoch"], {"with": "e2", "without": "e1"})
        self.assertTrue(pair["counted"])

    def test_a_round_without_the_block_is_never_paired(self):
        plain = context_round(1000, [CLAUDE])
        paired = self.paired(plain, paired_round("enclosing", [CLAUDE]))
        self.assertEqual(paired["pairs_listed"], 0)
        self.assertEqual(paired["pairs_total"], 0)
        self.assertIsNone(paired["with"]["billed_per_run"])
        self.assertIsNone(paired["delta"]["billed_per_run"])

    def test_one_side_alone_is_not_a_pair(self):
        self.assertEqual(self.paired(paired_round("enclosing", [CLAUDE]))["pairs"], [])

    def test_the_latest_run_of_a_side_is_the_one_paired(self):
        paired = self.paired(
            paired_round("none", [dict(CLAUDE, billed_tokens=5000)], at="2026-09-01T00:00:00Z"),
            paired_round("none", [dict(CLAUDE, billed_tokens=1500)], at="2026-09-02T00:00:00Z"),
            paired_round("enclosing", [CLAUDE], at="2026-09-03T00:00:00Z"),
        )
        (pair,) = paired["pairs"]
        self.assertEqual(pair["without"]["billed_tokens"], 1500)
        self.assertEqual(pair["without"]["at"], "2026-09-02T00:00:00Z")

    def test_an_enclosing_run_that_adopted_nothing_is_listed_and_not_counted(self):
        paired = self.paired(paired_round("none", [CLAUDE]), paired_round("enclosing", [CLAUDE], adopted=0))
        (pair,) = paired["pairs"]
        self.assertTrue(pair["nothing_adopted"])
        self.assertFalse(pair["counted"])
        self.assertEqual(paired["pairs_total"], 0)
        self.assertEqual(paired["with"]["reviewer_runs"], 0)

    def test_the_same_id_on_another_model_is_another_panel(self):
        without = paired_round("none", [CLAUDE])
        without["reviewers"][0]["model"] = "sonnet"
        (pair,) = self.paired(without, paired_round("enclosing", [CLAUDE]))["pairs"]
        self.assertFalse(pair["same_panel"])
        self.assertFalse(pair["counted"])
        self.assertEqual(
            pair["without_panel"], [{"id": "r1", "provider": "claude", "model": "sonnet", "role": "general"}]
        )

    def test_the_same_reviewer_in_another_role_is_another_panel(self):
        """The role picks the prompt, so the two runs differed in more than the context."""
        without = paired_round("none", [CLAUDE])
        without["reviewers"][0]["role"] = "security"
        (pair,) = self.paired(without, paired_round("enclosing", [CLAUDE]))["pairs"]
        self.assertFalse(pair["same_panel"])
        self.assertFalse(pair["counted"])
        self.assertEqual(pair["without_panel"][0]["role"], "security")

    def test_a_run_that_did_not_deliver_leaves_the_pair_out(self):
        paired = self.paired(
            paired_round("none", [CLAUDE, CODEX]),
            paired_round("enclosing", [CLAUDE, CODEX], statuses=["ok", "failed"]),
        )
        (pair,) = paired["pairs"]
        self.assertTrue(pair["same_panel"])
        self.assertFalse(pair["delivered"])
        self.assertEqual(pair["undelivered"], [{"side": "with", "id": "r2", "status": "failed"}])
        self.assertFalse(pair["counted"])
        self.assertEqual(paired["pairs_total"], 0)

    def test_other_prompt_inputs_leave_the_pair_out(self):
        paired = self.paired(
            paired_round("none", [CLAUDE]),
            paired_round("enclosing", [CLAUDE], context_sha256="c" * 64, max_findings=8),
        )
        (pair,) = paired["pairs"]
        self.assertFalse(pair["same_inputs"])
        self.assertEqual(pair["inputs_differ"], ["context", "max_findings"])
        self.assertFalse(pair["counted"])

    def test_a_run_that_reported_no_usage_is_not_in_the_billed_divisor(self):
        (pair,) = self.paired(paired_round("none", [CLAUDE, {}]), paired_round("enclosing", [CLAUDE, {}]))[
            "pairs"
        ]
        self.assertEqual((pair["with"]["reviewer_runs"], pair["with"]["measured_runs"]), (2, 1))
        self.assertEqual(pair["with"]["billed_per_run"], 1000.0)

    def test_a_codex_run_is_not_in_the_tool_divisor(self):
        (pair,) = self.paired(
            paired_round("none", [CLAUDE, CODEX]), paired_round("enclosing", [CLAUDE, CODEX])
        )["pairs"]
        self.assertEqual(pair["with"]["tool_reported_runs"], 1)
        self.assertEqual(pair["with"]["tool_uses_per_run"], 3.0)
        self.assertEqual(pair["with"]["billed_per_run"], 1500.0)

    def test_a_side_with_nothing_to_divide_gives_no_delta(self):
        (pair,) = self.paired(paired_round("none", [CODEX]), paired_round("enclosing", [CODEX]))["pairs"]
        self.assertIsNone(pair["delta"]["tool_uses_per_run"])
        self.assertEqual(pair["delta"]["billed_per_run"], 0.0)

    def test_refused_and_design_rounds_are_not_paired(self):
        refused = paired_round("enclosing", [CLAUDE])
        refused["status"] = opt.REFUSED
        design = design_event()
        design["measurement"] = measured("enclosing")
        paired = self.paired(paired_round("none", [CLAUDE]), refused, design)
        self.assertEqual(paired["pairs"], [])

    def test_the_context_split_does_not_move(self):
        events = [paired_round("none", [CLAUDE]), paired_round("enclosing", [CLAUDE])]
        plain = [context_round(1000, [CLAUDE]), context_round(1000, [CLAUDE], adopted=100)]
        self.assertEqual(
            opt.summarise_rounds(events)["by_context"], opt.summarise_rounds(plain)["by_context"]
        )


@unittest.skipUnless(has_git(), "git is required")
class TestThePairedBlockInTheReport(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace()
        self.workspace.ensure()

    def record(self, *events):
        for event in events:
            self.workspace.record_event("review", event["status"], event)

    def test_a_counted_pair_is_printed_with_its_delta_and_its_limits(self):
        self.record(
            paired_round("none", [dict(CLAUDE, billed_tokens=1200)], workflow=self.workspace.workflow),
            paired_round("enclosing", [CLAUDE], workflow=self.workspace.workflow),
        )
        _, out, _ = run_cli("optimization", "report")
        self.assertIn("Paired on one snapshot (--surrounding none vs enclosing", out)
        self.assertIn("  %s in %s   change 1,000 chars" % ("s" * 12, self.workspace.workflow), out)
        self.assertIn("      with:    1 run(s), 1,000 billed, 1,000.0 per run", out)
        self.assertIn("      delta:   -200.0 billed/run, 0.0 use(s)/run", out)
        self.assertIn("  total, 1 pair(s) counted", out)
        self.assertIn("small-sample observation, not a statistical result", out)
        report = json.loads(run_cli("optimization", "report", "--json")[1])
        self.assertEqual(report["paired"]["pairs_total"], 1)

    def test_each_reason_a_pair_is_left_out_is_named(self):
        without = paired_round("none", [CLAUDE, CODEX])
        without["reviewers"][1]["model"] = "gpt-4.1"
        self.record(
            without,
            paired_round(
                "enclosing", [CLAUDE, CODEX], adopted=0, statuses=["ok", "failed"], context_sha256="c"
            ),
        )
        _, out, _ = run_cli("optimization", "report")
        differ = "panels differ (without: r1 (claude, opus, general), r2 (claude, gpt-4.1, general))"
        self.assertIn(differ, out)
        self.assertIn("not delivered (with: r2 failed)", out)
        self.assertIn("inputs differ (context)", out)
        self.assertIn("nothing adopted", out)
        self.assertIn("  total, 0 pair(s) counted", out)

    def test_no_pair_prints_no_block(self):
        self.record(paired_round("enclosing", [CLAUDE]))
        _, out, _ = run_cli("optimization", "report")
        self.assertNotIn("Paired on one snapshot", out)

    def test_a_flat_workspace_names_no_workflow(self):
        self.record(
            paired_round("none", [CLAUDE], workflow=""), paired_round("enclosing", [CLAUDE], workflow="")
        )
        _, out, _ = run_cli("optimization", "report")
        self.assertIn("  %s   change 1,000 chars" % ("s" * 12), out)
        self.assertNotIn("%s in " % ("s" * 12), out)


# --------------------------------------------------------------------------- scorecard

SHA = "a" * 64


def scored_run(reviewer_id, status="ok", billed=100, cost=None, snapshot=SHA[:12]):
    """A reviewer entry as a review event records it."""
    usage = {"billed_tokens": billed, "cost_usd": cost, "measured": bool(billed)}
    return {"id": reviewer_id, "status": status, "snapshot": snapshot, "usage": usage}


def scored_event(runs, round_id="r1", iteration=1, surrounding=None, rerun=False, stage="review"):
    event = {"stage": stage, "status": "ok", "iteration": iteration, "round_id": round_id, "reviewers": runs}
    if surrounding:
        event["measurement"] = {"surrounding": surrounding, "rerun": rerun}
    return event


def scored_finding(fid, key, reported_by, triage="needs-triage", set_at=None, duplicates=()):
    finding = {"id": fid, "key": key, "reported_by": list(reported_by), "triage": triage}
    if set_at:
        finding["triage_set_at"] = set_at
    if duplicates:
        finding["possible_duplicates"] = list(duplicates)
    return finding


def scored_round(findings=(), sha=SHA, round_id="r1", iteration=1, live=True, at="2026-09-01T00:00:00Z"):
    """A consolidated report in the shape ``review.recorded_rounds`` returns it."""
    snapshot = {"sha256": sha}
    if round_id:
        snapshot["round_id"] = round_id
    return {
        "generated_at": at,
        "iteration": iteration,
        "snapshot": snapshot,
        "findings": list(findings),
        "live": live,
    }


def scorecard(events, rounds, stage="code"):
    return opt.reviewer_scorecard([{"workflow": "w", "stage": stage, "events": events, "rounds": rounds}])


class TestMatchingEventsToRounds(unittest.TestCase):
    """Cost comes from the events and findings from the reports, so each
    figure is only honest if both describe the same rounds."""

    def test_a_round_id_matches_only_the_report_of_that_round(self):
        rounds = [scored_round(round_id="r1", live=False), scored_round(round_id="r2")]
        card = scorecard([scored_event([scored_run("a")], round_id="r1")], rounds)["code"]
        self.assertEqual((card["rounds_recorded"], card["rounds_read"]), (1, 1))

    def test_an_event_without_a_round_id_matches_the_one_report_of_its_sha(self):
        """An event from before events carried the id, over a report that has one."""
        event = scored_event([scored_run("a")], round_id="")
        self.assertEqual(scorecard([event], [scored_round(round_id="rx")])["code"]["rounds_read"], 1)

    def test_an_unstamped_event_matches_the_live_report_of_its_iteration(self):
        event = scored_event([scored_run("a", snapshot="")], round_id="")
        self.assertEqual(scorecard([event], [scored_round()])["code"]["rounds_read"], 1)
        other = scored_event([scored_run("a", snapshot="")], round_id="", iteration=2)
        self.assertEqual(scorecard([other], [scored_round()])["code"]["rounds_read"], 0)

    def test_a_stamped_event_takes_the_live_report_before_an_unstamped_one(self):
        """Round 1 unstamped and round 2 stamped, and only round 2's report left."""
        first = scored_event([scored_run("a", snapshot="", billed=5)], round_id="")
        second = scored_event([scored_run("a", billed=7)], round_id="")
        card = scorecard([first, second], [scored_round()])["code"]
        self.assertEqual((card["rounds_recorded"], card["rounds_read"]), (2, 1))
        self.assertEqual(card["panel"]["billed_tokens"], 7)

    def test_unstamped_events_of_one_iteration_are_one_round(self):
        events = [scored_event([scored_run("a", snapshot="")], round_id="") for _ in range(3)]
        card = scorecard(events, [scored_round()])["code"]
        self.assertEqual((card["rounds_recorded"], card["rounds_read"]), (1, 1))
        self.assertEqual(card["panel"]["runs"], 3)

    def test_two_reports_of_one_sha_leave_an_event_without_a_round_id_unread(self):
        """Taking the newer would pin the old round's cost on the new round's findings."""
        rounds = [scored_round(round_id="", live=False), scored_round(round_id="r2")]
        old = scored_event([scored_run("a", billed=5)], round_id="")
        new = scored_event([scored_run("a", billed=7)], round_id="r2")
        card = scorecard([old, new], rounds)["code"]
        self.assertEqual((card["rounds_recorded"], card["rounds_read"]), (2, 1))
        self.assertEqual(card["panel"]["billed_tokens"], 7)
        alone = scorecard([old], rounds)["code"]
        self.assertEqual(alone["rounds_read"], 0)
        self.assertEqual(alone["panel"]["billed_tokens"], 0)

    def test_one_sha_frozen_twice_is_two_rounds_with_their_own_cost(self):
        rounds = [
            scored_round([scored_finding("F1", "k1", ["a"], "accepted")], round_id="r1", live=False),
            scored_round([scored_finding("F1", "k2", ["a"], "accepted")], round_id="r2"),
        ]
        events = [
            scored_event([scored_run("a", billed=5)], round_id="r1"),
            scored_event([scored_run("a", billed=7)], round_id="r2"),
        ]
        card = scorecard(events, rounds)["code"]
        self.assertEqual((card["rounds_recorded"], card["rounds_read"]), (2, 2))
        self.assertEqual(card["panel"]["billed_tokens"], 12)
        self.assertEqual(card["panel"]["accepted"], 2)

    def test_a_measured_pair_is_one_round_paid_twice(self):
        """The findings and triage are the second run's, the cost is both runs'."""
        first = [scored_run("a", billed=5), scored_run("b", billed=5)]
        second = [scored_run("a", billed=7), scored_run("b", billed=7)]
        events = [
            scored_event(first, surrounding="none"),
            scored_event(second, surrounding="enclosing", rerun=True),
        ]
        rounds = [scored_round([scored_finding("F1", "second", ["a"], "accepted")])]
        card = scorecard(events, rounds)["code"]
        self.assertEqual((card["rounds_recorded"], card["rounds_read"]), (1, 1))
        self.assertEqual(card["rerun_rounds"], 1)
        self.assertEqual(card["panel"]["billed_tokens"], 24)
        self.assertEqual(card["panel"]["runs"], 4)
        self.assertEqual(card["findings"], 1)

    def test_a_measured_pair_with_no_report_is_one_hole(self):
        events = [
            scored_event([scored_run("a", billed=5)], surrounding="none"),
            scored_event([scored_run("a", billed=7)], surrounding="enclosing", rerun=True),
        ]
        card = scorecard(events, [])["code"]
        self.assertEqual((card["rounds_recorded"], card["rounds_read"]), (1, 0))
        self.assertEqual(card["panel"]["billed_tokens"], 0)

    def test_a_rerun_with_only_is_the_same_round(self):
        events = [
            scored_event([scored_run("a", billed=5), scored_run("b", status="failed", billed=0)]),
            scored_event([scored_run("b", billed=7)]),
        ]
        card = scorecard(events, [scored_round()])["code"]
        self.assertEqual(card["rounds_recorded"], 1)
        self.assertEqual(card["panel"]["runs"], 3)
        self.assertEqual(card["panel"]["billed_tokens"], 12)
        self.assertEqual(card["reviewers"]["b"]["runs"], 2)

    def test_a_round_nobody_reviewed_is_counted_but_not_a_hole(self):
        """There was never a report to read, and its runs were still paid for."""
        event = scored_event([scored_run("a", status="failed", billed=0), scored_run("b", status="failed")])
        card = scorecard([event], [])["code"]
        self.assertEqual((card["rounds_recorded"], card["rounds_read"], card["rounds_unreviewed"]), (1, 0, 1))
        self.assertEqual(card["panel"]["runs"], 2)
        self.assertEqual(card["panel"]["failed_runs"], 2)
        self.assertEqual(card["panel"]["billed_tokens"], 100)

    def test_a_round_nobody_reviewed_is_not_read_even_with_a_report(self):
        """A code round keeps its id whether or not anyone returned, so it has one."""
        rounds = [scored_round([scored_finding("F1", "k", ["a"], "accepted")])]
        event = scored_event([scored_run("a", status="failed", billed=0)])
        card = scorecard([event], rounds)["code"]
        self.assertEqual((card["rounds_recorded"], card["rounds_read"], card["rounds_unreviewed"]), (1, 0, 1))
        self.assertEqual(card["findings"], 0)
        self.assertEqual(card["workflows_read"], 0)
        self.assertEqual((card["panel"]["runs"], card["panel"]["failed_runs"]), (1, 1))

    def test_a_round_with_no_report_leaves_its_cost_out_too(self):
        events = [
            scored_event([scored_run("a", billed=5)], round_id="r1"),
            scored_event([scored_run("a")], round_id="r2"),
        ]
        card = scorecard(events, [scored_round(round_id="r1")])["code"]
        self.assertEqual(card["rounds_recorded"] - card["rounds_read"] - card["rounds_unreviewed"], 1)
        self.assertEqual(card["panel"]["billed_tokens"], 5)

    def test_an_event_that_did_not_finish_is_not_a_round(self):
        event = dict(scored_event([scored_run("a")]), status="failed")
        self.assertEqual(scorecard([event], [scored_round()])["code"]["rounds_recorded"], 0)


class TestCountingFindingsOnce(unittest.TestCase):
    """A finding nobody fixed comes back every round, carrying its triage."""

    def outcome(self, first, second):
        rounds = [
            scored_round([first], round_id="r1", live=False, at="2026-09-01T00:00:00Z"),
            scored_round([second], round_id="r2", at="2026-09-02T00:00:00Z"),
        ]
        events = [
            scored_event([scored_run("a")], round_id="r1"),
            scored_event([scored_run("a")], round_id="r2"),
        ]
        panel = scorecard(events, rounds)["code"]["panel"]
        self.assertEqual(panel["reported"], 1)
        return {name for name in ("accepted", "rejected", "duplicate", "open") if panel[name]}

    def test_a_finding_in_two_rounds_is_one_finding(self):
        accepted = scored_finding("F1", "k", ["a"], "accepted", set_at="t1")
        self.assertEqual(self.outcome(accepted, accepted), {"accepted"})

    def test_a_rebuilt_default_does_not_undo_an_acceptance(self):
        self.assertEqual(
            self.outcome(scored_finding("F1", "k", ["a"], "accepted"), scored_finding("F2", "k", ["a"])),
            {"accepted"},
        )

    def test_putting_it_back_to_needs_triage_withdraws_the_acceptance(self):
        reopened = scored_finding("F1", "k", ["a"], "needs-triage", set_at="t2")
        self.assertEqual(self.outcome(scored_finding("F1", "k", ["a"], "accepted"), reopened), {"open"})

    def test_needs_investigation_is_a_decision_with_or_without_the_stamp(self):
        for set_at in (None, "t2"):
            later = scored_finding("F1", "k", ["a"], "needs-investigation", set_at=set_at)
            self.assertEqual(self.outcome(scored_finding("F1", "k", ["a"], "accepted"), later), {"open"})

    def test_a_later_decision_wins(self):
        accepted = scored_finding("F1", "k", ["a"], "accepted")
        rejected = scored_finding("F1", "k", ["a"], "rejected")
        self.assertEqual(self.outcome(accepted, rejected), {"rejected"})

    def test_rounds_written_in_one_second_are_ordered_by_their_events(self):
        """The archive lists rounds by file name, and ``generated_at`` ties."""
        rounds = [
            scored_round([scored_finding("F1", "k", ["a"], "rejected")], round_id="r2", live=False),
            scored_round([scored_finding("F1", "k", ["a"], "accepted")], round_id="r1", live=False),
        ]
        events = [
            scored_event([scored_run("a")], round_id="r1"),
            scored_event([scored_run("a")], round_id="r2"),
        ]
        panel = scorecard(events, rounds)["code"]["panel"]
        self.assertEqual((panel["accepted"], panel["rejected"]), (0, 1))

    def test_never_decided_is_open(self):
        undecided = scored_finding("F1", "k", ["a"])
        self.assertEqual(self.outcome(undecided, undecided), {"open"})

    def test_a_finding_reported_by_two_counts_once_for_each_and_once_for_the_panel(self):
        rounds = [scored_round([scored_finding("F1", "k", ["a", "b"], "accepted")])]
        card = scorecard([scored_event([scored_run("a"), scored_run("b")])], rounds)["code"]
        self.assertEqual(card["reviewers"]["a"]["reported"], 1)
        self.assertEqual(card["reviewers"]["b"]["reported"], 1)
        self.assertEqual(card["panel"]["reported"], 1)
        self.assertEqual(card["reviewers"]["a"]["alone"], 0)


class TestFoundAlone(unittest.TestCase):
    """An upper bound on what dropping a reviewer would lose."""

    def card(self, *findings):
        rounds = [scored_round(list(findings))]
        return scorecard([scored_event([scored_run("a"), scored_run("b")])], rounds)["code"]["reviewers"]

    def test_a_finding_nobody_linked_is_found_alone(self):
        card = self.card(scored_finding("F1", "k1", ["a"], "accepted"))
        self.assertEqual((card["a"]["alone"], card["a"]["alone_accepted"]), (1, 1))

    def test_the_other_reviewers_copy_triaged_duplicate_takes_it_out(self):
        card = self.card(
            scored_finding("F1", "k1", ["a"], "accepted", duplicates=["F2"]),
            scored_finding("F2", "k2", ["b"], "duplicate", duplicates=["F1"]),
        )
        self.assertEqual(card["a"]["alone"], 0)
        self.assertEqual(card["b"]["alone"], 0)

    def test_being_the_duplicate_of_an_accepted_one_takes_it_out_too(self):
        card = self.card(
            scored_finding("F1", "k1", ["a"], "duplicate", duplicates=["F2"]),
            scored_finding("F2", "k2", ["b"], "accepted", duplicates=["F1"]),
        )
        self.assertEqual(card["a"]["alone"], 0)

    def test_a_link_between_one_reviewers_own_findings_does_not(self):
        card = self.card(
            scored_finding("F1", "k1", ["a"], "accepted", duplicates=["F2"]),
            scored_finding("F2", "k2", ["a"], "duplicate", duplicates=["F1"]),
        )
        self.assertEqual(card["a"]["alone"], 2)

    def test_a_link_nobody_triaged_duplicate_does_not(self):
        card = self.card(
            scored_finding("F1", "k1", ["a"], "accepted", duplicates=["F2"]),
            scored_finding("F2", "k2", ["b"], "accepted", duplicates=["F1"]),
        )
        self.assertEqual(card["a"]["alone"], 1)

    def test_ids_are_resolved_inside_their_own_round(self):
        """F2 in the second round is a different finding from F2 in the first."""
        first = scored_round(
            [
                scored_finding("F1", "k1", ["a"], "accepted", duplicates=["F2"]),
                scored_finding("F2", "k2", ["a"], "accepted"),
            ],
            round_id="r1",
            live=False,
        )
        second = scored_round([scored_finding("F2", "k3", ["b"], "duplicate")], round_id="r2")
        events = [
            scored_event([scored_run("a")], round_id="r1"),
            scored_event([scored_run("b")], round_id="r2"),
        ]
        card = scorecard(events, [first, second])["code"]["reviewers"]
        self.assertEqual(card["a"]["alone"], 2)


class TestScorecardFigures(unittest.TestCase):
    def card(self, accepted=0, rejected=0, duplicate=0, runs=None):
        findings = []
        for status, count in (("accepted", accepted), ("rejected", rejected), ("duplicate", duplicate)):
            findings += [scored_finding("F", "%s%d" % (status, n), ["a"], status) for n in range(count)]
        event = scored_event(runs or [scored_run("a", billed=1000, cost=1.0)])
        return scorecard([event], [scored_round(findings)])["code"]

    def test_a_rate_needs_ten_decided_findings(self):
        self.assertIsNone(self.card(accepted=5, rejected=4)["reviewers"]["a"]["rejection_rate"])
        self.assertEqual(self.card(accepted=6, rejected=4)["reviewers"]["a"]["rejection_rate"], 0.4)

    def test_a_per_accepted_figure_needs_ten_accepted(self):
        few = self.card(accepted=9, rejected=5)["reviewers"]["a"]
        self.assertIsNone(few["billed_per_accepted"])
        self.assertIsNone(few["cost_per_accepted"])
        enough = self.card(accepted=10)["reviewers"]["a"]
        self.assertEqual(enough["billed_per_accepted"], 100)
        self.assertEqual(enough["cost_per_accepted"], 0.1)

    def test_a_reviewer_that_prices_nothing_has_no_cost_per_accepted(self):
        """Codex: zero dollars is not a price, and $0 per accepted would say it was."""
        card = self.card(accepted=10, runs=[scored_run("a", billed=1000, cost=None)])["reviewers"]["a"]
        self.assertEqual(card["billed_per_accepted"], 100)
        self.assertIsNone(card["cost_per_accepted"])
        self.assertEqual(card["priced_runs"], 0)

    def test_a_failed_run_is_a_run_that_cost_nothing(self):
        card = self.card(runs=[scored_run("a", billed=1000), scored_run("b", status="failed", billed=0)])
        self.assertEqual((card["reviewers"]["b"]["runs"], card["reviewers"]["b"]["failed_runs"]), (1, 1))
        self.assertEqual(card["reviewers"]["b"]["billed_tokens"], 0)
        self.assertEqual(card["panel"]["runs"], 2)

    def test_code_and_design_are_kept_apart_and_added_only_in_the_total(self):
        code = {
            "workflow": "w",
            "stage": "code",
            "events": [scored_event([scored_run("a", billed=10)])],
            "rounds": [scored_round([scored_finding("F1", "k", ["a"], "accepted")])],
        }
        design = {
            "workflow": "w",
            "stage": "design",
            "events": [scored_event([scored_run("a", billed=20)], stage="design_review")],
            "rounds": [scored_round([scored_finding("F1", "k", ["a"], "accepted")])],
        }
        card = opt.reviewer_scorecard([code, design])
        self.assertEqual(card["code"]["panel"]["billed_tokens"], 10)
        self.assertEqual(card["design"]["panel"]["billed_tokens"], 20)
        self.assertEqual(card["total"]["billed_tokens"], 30)
        self.assertEqual(card["total"]["accepted"], 2)
        self.assertEqual(card["total"]["rounds_read"], 2)

    def test_the_total_of_one_stage_is_that_stage(self):
        code = {
            "workflow": "w",
            "stage": "code",
            "events": [scored_event([scored_run("a", billed=10)])],
            "rounds": [scored_round([scored_finding("F1", "k", ["a"], "rejected")])],
        }
        card = opt.reviewer_scorecard([code])
        for field in ("billed_tokens", "runs", "reported", "rejected"):
            self.assertEqual(card["total"][field], card["code"]["panel"][field])
        self.assertEqual(card["total"]["rounds_recorded"], card["code"]["rounds_recorded"])


@unittest.skipUnless(has_git(), "git is required")
class TestWhatTheSnapshotReportsAsChanged(IsolatedCase):
    """The lists the risk check and the size threshold are computed from."""

    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app/auth.py", "def check():\n    return True\n")
        self.write("app/main.py", "x = 1\n")
        self.commit_all("init")
        self.workspace = self.cli_workspace()

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

    def test_a_binary_file_is_counted(self):
        """Git prints "Binary files a/x and b/x differ" and no `+++` header,
        so reading the list out of the diff text missed it -- and a change to
        three images and one source file counted as one file, small enough for
        a reduced panel."""
        with open(os.path.join(self.project, "logo.png"), "wb") as handle:
            handle.write(b"\x00\x01original")
        self.commit_all("image")
        with open(os.path.join(self.project, "logo.png"), "wb") as handle:
            handle.write(b"\x00\x09changed")
        meta = self.snapshot()
        self.assertIn("logo.png", meta["files"])
        self.assertIn("logo.png", meta["changed_paths"])

    def test_an_untracked_file_is_still_counted(self):
        """It is diffed separately, by name; git's listing of tracked changes
        cannot know about it."""
        self.write("brand_new.py", "x = 1\n")
        meta = self.snapshot()
        self.assertIn("brand_new.py", meta["files"])

    def test_the_orchestrators_own_files_are_not_counted(self):
        self.write(".dev-orchestra.yaml", "version: 1\n")
        self.write("app/main.py", "x = 2\n")
        meta = self.snapshot()
        self.assertNotIn(".dev-orchestra.yaml", meta["files"])
        self.assertIn("app/main.py", meta["files"])

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
        self.workspace = self.cli_workspace()

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
