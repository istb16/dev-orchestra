"""A specialist sits out a round that has nothing for it, in both stages.

`security`, `test` and `architecture` seats that always run are asked one
more question every round: does this change -- or this plan -- give them
anything to read? Only evidence of absence leaves one out, doubt about the
evidence means run, and the level in force never changes the answer: a
`quality` round judges exactly as an `aggressive` one does. A high-risk hit,
a declaration, a carried finding and `--only` keep the seat before its rule
is asked.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git
from test_design_review import DesignReviewCase

from orchestrator import cli, cli_review, optimization_render, presets
from orchestrator import config as config_mod
from orchestrator import optimization as opt
from orchestrator import optimization_report as opt_report
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


def seat(reviewer_id, role="general", **fields):
    reviewer = {"id": reviewer_id, "provider": "mock", "model": {"family": "small"}, "role": role}
    reviewer.update(fields)
    return reviewer


def settings(**overrides):
    values = dict(config_mod.default_config()["optimization"])
    values.update(overrides)
    return values


def code_records(panel, reviewed, paths=None, stage_settings=None, **keep):
    """``relevance_records`` for a code round over ``reviewed``, with no high-risk hit
    unless ``keep`` passes one."""
    evidence = opt.code_evidence(list(reviewed if paths is None else paths), list(reviewed))
    return opt.relevance_records("code", panel, evidence, stage_settings or settings(), **keep)


def plan(files, proposed="Change what the files below say."):
    lines = ["# Plan", "", "## Proposed Change", proposed, "", "## Files to Modify"]
    lines += ["- `%s`" % path for path in files]
    return "\n".join(lines) + "\n"


def decide(panel, paths, level="balanced", reviewed=None):
    """``decide`` on a small change of ``paths``, every one of them reviewed by default."""
    reviewed = list(paths) if reviewed is None else reviewed
    return opt.decide(
        settings(level=level),
        {},
        list(paths),
        3,
        "ok",
        len(panel),
        panel=panel,
        reviewed=reviewed,
    )


def design_records(panel, text, stage_settings=None, **keep):
    scan = review_mod.plan_tokens(text)
    evidence = opt.design_evidence(scan, "ok")
    return opt.relevance_records("design", panel, evidence, stage_settings or settings(), **keep)


def by_id(records):
    return {record["id"]: record for record in records}


#: A security seat that opts in to its rule; one that does not is never judged.
SEC = seat("sec", "security", relevance="security")
SECURITY = [seat("gen"), SEC]
TEST = [seat("gen"), seat("t", "test")]
ARCHITECTURE = [seat("gen"), seat("a", "architecture")]
SPECIALISTS = [seat("gen"), SEC, seat("t", "test"), seat("a", "architecture")]


# --------------------------------------------------------------------------- security


class TestSecurityCode(unittest.TestCase):
    def test_security_code_needed_on_security_path(self):
        record = by_id(code_records(SECURITY, ["src/api/routes.py"]))["sec"]
        self.assertTrue(record["runs"])
        self.assertEqual(record["when"], opt.WHEN_RELEVANCE)
        self.assertEqual(record["reason"], "src/api/routes.py matches *api*")

    def test_security_code_needed_on_manifest(self):
        record = by_id(code_records(SECURITY, ["requirements.txt"]))["sec"]
        self.assertTrue(record["runs"])
        self.assertIn("requirements.txt matches", record["reason"])

    def test_security_code_kept_on_high_risk_hit_and_level_escalated(self):
        plan_ = decide(SECURITY, ["src/auth.py"])
        self.assertEqual(plan_.level, "quality")
        record = by_id(plan_.conditional)["sec"]
        self.assertTrue(record["runs"])
        self.assertEqual(record["reason"], "src/auth.py matches *auth*")

    def test_security_code_left_out_on_plain_module_with_reason(self):
        record = by_id(code_records(SECURITY, ["src/calc.py"]))["sec"]
        self.assertFalse(record["runs"])
        self.assertEqual(
            record["reason"],
            "no security-relevant path changed (optimization.security_paths)",
        )

    def test_security_seat_is_never_judged_without_opting_in(self):
        """The rule reads path names only, so a plain security seat always runs."""
        panel = [seat("gen"), seat("sec", "security")]
        for path in ("src/calc.py", "src/user.py", "src/utils.py"):
            with self.subTest(path=path):
                records = code_records(panel, [path])
                self.assertEqual(records, [])
                self.assertTrue(opt.qualifies(panel[1], records))
        self.assertEqual(design_records(panel, plan(["src/calc.py"])), [])
        self.assertIsNone(opt.relevance_rule(seat("sec", "security")))
        self.assertIsNone(opt.relevance_rule(seat("sec", "security", relevance="always")))
        self.assertEqual(opt.relevance_rule(SEC), "security")

    def test_another_role_opts_in_to_the_security_rule(self):
        """Opting in is not reserved to the security role."""
        perf = seat("p", "performance", relevance="security")
        self.assertEqual(opt.relevance_rule(perf), "security")
        panel = [seat("gen"), perf]
        record = by_id(code_records(panel, ["src/calc.py"]))["p"]
        self.assertFalse(record["runs"])
        self.assertIn("(optimization.security_paths)", record["reason"])
        self.assertTrue(by_id(code_records(panel, ["src/api/routes.py"]))["p"]["runs"])

    def test_default_security_paths_cover_data_and_network_modules(self):
        for path in ("src/db.py", "src/files.py", "src/client.py", "src/query.py", "src/fetch_url.py"):
            with self.subTest(path=path):
                self.assertTrue(by_id(code_records(SECURITY, [path]))["sec"]["runs"])

    def test_security_code_user_patterns_replace_defaults_but_risk_patterns_still_count(self):
        own = settings(security_paths=["*vault*"])

        def record(path):
            return by_id(code_records(SECURITY, [path], stage_settings=own))["sec"]

        self.assertFalse(record("src/api/routes.py")["runs"])
        self.assertTrue(record("src/vault.py")["runs"])
        # No hit is passed in, so only the rule can have kept it: through the
        # high-risk patterns, which replacing security_paths does not remove.
        kept = record("src/auth.py")
        self.assertTrue(kept["runs"])
        self.assertEqual(kept["reason"], "src/auth.py matches *auth*")

    def test_a_withheld_path_counts_for_security(self):
        """Judged on the whole change, as risk is: a withheld lockfile is still one."""
        records = code_records(SECURITY, ["src/calc.py"], paths=["src/calc.py", "package-lock.json"])
        self.assertTrue(by_id(records)["sec"]["runs"])


class TestSecurityDesign(unittest.TestCase):
    def test_security_design_needed_on_token_outside_files_section(self):
        text = plan(["src/calc.py"], proposed="Route it through `src/api/routes.py` as well.")
        record = by_id(design_records(SECURITY, text))["sec"]
        self.assertTrue(record["runs"])
        self.assertIn("src/api/routes.py", record["reason"])

    def test_security_design_left_out_when_no_token_matches(self):
        record = by_id(design_records(SECURITY, plan(["src/calc.py"])))["sec"]
        self.assertFalse(record["runs"])
        self.assertEqual(
            record["reason"],
            "no security-relevant token in the plan (optimization.security_paths)",
        )

    def test_security_design_matches_mixed_case_tokens(self):
        for path in ("Src/Auth.py", "SRC/API/Routes.py"):
            with self.subTest(path=path):
                record = by_id(design_records(SECURITY, plan([path])))["sec"]
                self.assertTrue(record["runs"], record)
                self.assertTrue(record["reason"].startswith("%s matches " % path), record)

    def test_security_design_needed_on_doubt_no_files_section(self):
        """Nothing is judged, so the seat runs as it always did."""
        text = "# Plan\n\n## Proposed Change\nTouch `src/calc.py`.\n"
        records = design_records(SECURITY, text)
        self.assertEqual(records, [])
        self.assertTrue(opt.qualifies(SECURITY[1], records))


# --------------------------------------------------------------------------- test


class TestTestRole(unittest.TestCase):
    def test_test_code_left_out_docs_only(self):
        record = by_id(code_records(TEST, ["README.md", "docs/guide.md", "references/cli.md"]))["t"]
        self.assertFalse(record["runs"])
        self.assertEqual(
            record["reason"],
            "docs-only change: every reviewed file is documentation",
        )

    def test_test_code_needed_tests_only_change(self):
        record = by_id(code_records(TEST, ["tests/test_calc.py"]))["t"]
        self.assertTrue(record["runs"])
        self.assertEqual(record["reason"], "tests/test_calc.py is not documentation")

    def test_test_code_needed_one_code_file_among_docs(self):
        record = by_id(code_records(TEST, ["README.md", "src/calc.py"]))["t"]
        self.assertTrue(record["runs"])
        self.assertEqual(record["reason"], "src/calc.py is not documentation")

    def test_test_design_left_out_docs_only_plan(self):
        record = by_id(design_records(TEST, plan(["docs/guide.md", "README.md"])))["t"]
        self.assertFalse(record["runs"])
        self.assertEqual(
            record["reason"],
            "docs-only plan: Files to Modify names no code or test file",
        )

    def test_test_design_needed_when_plan_names_test_file(self):
        record = by_id(design_records(TEST, plan(["docs/guide.md", "tests/test_calc.py"])))["t"]
        self.assertTrue(record["runs"])
        self.assertEqual(record["reason"], "Files to Modify names a test: tests/test_calc.py")

    def test_test_design_needed_on_glob_doubt(self):
        self.assertEqual(design_records(TEST, plan(["docs/*.md"])), [])


# --------------------------------------------------------------------------- architecture


class TestArchitecture(unittest.TestCase):
    def test_architecture_code_needed_six_files(self):
        files = ["src/m%d.py" % index for index in range(6)]
        record = by_id(code_records(ARCHITECTURE, files))["a"]
        self.assertTrue(record["runs"])
        self.assertEqual(record["reason"], "6 files reviewed")

    def test_architecture_code_needed_two_top_dirs(self):
        record = by_id(code_records(ARCHITECTURE, ["src/calc.py", "lib/util.py"]))["a"]
        self.assertTrue(record["runs"])
        self.assertEqual(record["reason"], "spans 2 directories (src, lib)")

    def test_architecture_code_needed_contract_path(self):
        for path, pattern in (("src/schema.py", "*schema*"), ("src/cli.py", "*cli*")):
            with self.subTest(path=path):
                record = by_id(code_records(ARCHITECTURE, [path]))["a"]
                self.assertTrue(record["runs"])
                self.assertEqual(record["reason"], "%s matches %s" % (path, pattern))

    def test_architecture_code_left_out_single_dir_small_no_contract(self):
        record = by_id(code_records(ARCHITECTURE, ["src/calc.py", "src/calc_more.py"]))["a"]
        self.assertFalse(record["runs"])
        self.assertEqual(
            record["reason"],
            "1 directory, 2 file(s), no contract, schema, config or CLI path "
            "(optimization.architecture_paths)",
        )

    def test_architecture_code_root_files_count_as_one_dir(self):
        self.assertFalse(by_id(code_records(ARCHITECTURE, ["calc.py", "util.py"]))["a"]["runs"])
        self.assertTrue(by_id(code_records(ARCHITECTURE, ["calc.py", "src/util.py"]))["a"]["runs"])

    def test_architecture_code_documentation_spans_no_directory(self):
        """A doc beside one module does not make the change cross a boundary."""
        self.assertFalse(by_id(code_records(ARCHITECTURE, ["src/calc.py", "docs/calc.md"]))["a"]["runs"])

    def test_code_paths_are_normalised_before_judging(self):
        """Backslashes and a leading ``./`` read as the POSIX path they name."""
        for paths in (["docs\\guide.md"], ["./README.md"], ["docs\\guide.md", "./README.md"]):
            with self.subTest(paths=paths):
                self.assertFalse(by_id(code_records(TEST, paths))["t"]["runs"])
        record = by_id(code_records(ARCHITECTURE, ["src\\calc.py", "lib\\util.py"]))["a"]
        self.assertEqual((record["runs"], record["reason"]), (True, "spans 2 directories (src, lib)"))
        self.assertFalse(by_id(code_records(ARCHITECTURE, ["src\\calc.py", "./src/more.py"]))["a"]["runs"])

    def test_root_rst_and_txt_are_documentation_in_a_plan(self):
        files = opt.plan_files(review_mod.plan_tokens(plan(["NOTES.rst", "CHANGES.txt", "src/calc.py"])))
        self.assertEqual(files.code, ["src/calc.py"])
        record = by_id(design_records(TEST, plan(["NOTES.rst", "CHANGES.txt"])))["t"]
        self.assertFalse(record["runs"], record)

    def test_architecture_design_needed_many_code_files(self):
        files = ["src/m%d.py" % index for index in range(6)]
        record = by_id(design_records(ARCHITECTURE, plan(files)))["a"]
        self.assertTrue(record["runs"])
        self.assertEqual(record["reason"], "6 code files in Files to Modify")

    def test_architecture_design_needed_two_dirs(self):
        record = by_id(design_records(ARCHITECTURE, plan(["src/calc.py", "lib/util.py"])))["a"]
        self.assertTrue(record["runs"])
        self.assertEqual(record["reason"], "spans 2 directories (src, lib)")

    def test_architecture_design_left_out_single_module_plan(self):
        record = by_id(design_records(ARCHITECTURE, plan(["src/calc.py", "tests/test_calc.py"])))["a"]
        self.assertFalse(record["runs"])
        self.assertTrue(record["reason"].startswith("1 directory, 1 code file(s), no contract"), record)

    def test_architecture_design_needed_on_directory_token_doubt(self):
        self.assertEqual(design_records(ARCHITECTURE, plan(["src/calc/"])), [])


# --------------------------------------------------------------------------- levels


class TestLevels(unittest.TestCase):
    def docs_only(self, level):
        return decide(SPECIALISTS, ["README.md"], level=level)

    def test_quality_configured_still_skips_unneeded_role_code(self):
        plan_ = self.docs_only("quality")
        records = by_id(plan_.conditional)
        self.assertEqual(plan_.level, "quality")
        self.assertFalse(records["t"]["runs"])
        self.assertFalse(records["a"]["runs"])
        self.assertNotIn("gen", records)
        self.assertIsNone(plan_.reviewer_limit)
        running = [reviewer["id"] for reviewer in SPECIALISTS if opt.qualifies(reviewer, plan_.conditional)]
        self.assertEqual(running, ["gen"])

    def test_quality_configured_still_skips_unneeded_role_design(self):
        scan = review_mod.plan_tokens(plan(["docs/guide.md"]))
        design = opt.decide_design_round(settings(level="quality"), scan, "ok", SPECIALISTS)
        self.assertEqual(design.level, "quality")
        left = {record["id"] for record in design.left_out()}
        self.assertEqual(left, {"sec", "t", "a"})

    def test_aggressive_and_balanced_judge_the_same_as_quality(self):
        quality = self.docs_only("quality").conditional
        for level in ("aggressive", "balanced"):
            with self.subTest(level=level):
                self.assertEqual(self.docs_only(level).conditional, quality)

    def test_high_risk_hit_keeps_every_role_code(self):
        plan_ = decide(SPECIALISTS, ["docs/auth.md"], level="aggressive")
        self.assertEqual(plan_.level, "quality")
        records = by_id(plan_.conditional)
        self.assertEqual(sorted(records), ["a", "sec", "t"])
        for record in records.values():
            self.assertTrue(record["runs"])
            self.assertEqual(record["reason"], "docs/auth.md matches *auth*")

    def test_high_risk_hit_keeps_every_role_design(self):
        scan = review_mod.plan_tokens(plan(["docs/guide.md"], proposed="Explain `src/auth.py`."))
        design = opt.decide_design_round(settings(), scan, "ok", SPECIALISTS)
        self.assertTrue(design.is_high_risk)
        for record in design.conditional:
            self.assertTrue(record["runs"], record)
            self.assertEqual(record["reason"], "src/auth.py matches *auth*")

    def test_quality_level_no_panel_cut_but_relevance_applies(self):
        panel = [seat("gen"), seat("gen2"), SEC]
        plan_ = decide(panel, ["src/calc.py"], level="quality")
        self.assertIsNone(plan_.reviewer_limit)
        self.assertFalse(by_id(plan_.conditional)["sec"]["runs"])


# --------------------------------------------------------------------------- interactions


class TestInteractions(unittest.TestCase):
    def test_general_never_judged(self):
        self.assertEqual(code_records([seat("gen"), seat("gen2")], ["README.md"]), [])
        self.assertIsNone(opt.relevance_rule(seat("gen", relevance="security")))

    def test_when_high_risk_seat_not_judged_twice(self):
        plan_ = decide([seat("gen"), seat("sec", "security", when="high-risk")], ["src/calc.py"])
        self.assertEqual([(r["id"], r["when"]) for r in plan_.conditional], [("sec", "high-risk")])

    def test_when_paths_seat_not_judged(self):
        plan_ = decide([seat("gen"), seat("sec", "security", when={"paths": ["*.sql"]})], ["src/calc.py"])
        self.assertEqual([(r["id"], r["when"]) for r in plan_.conditional], [("sec", "paths")])

    def test_declared_keeps_every_role(self):
        for records in (
            code_records(SPECIALISTS, ["README.md"], declared=True),
            design_records(SPECIALISTS, plan(["README.md"]), declared=True),
        ):
            self.assertEqual(len(records), 3)
            for record in records:
                self.assertEqual((record["runs"], record["reason"]), (True, "declared with --high-risk"))

    def test_carried_finding_keeps_role(self):
        carried = [{"id": "F3", "reported_by": ["t"]}]
        for records in (
            code_records(TEST, ["README.md"], carried=carried),
            design_records(TEST, plan(["README.md"]), carried=carried),
        ):
            record = by_id(records)["t"]
            self.assertEqual((record["runs"], record["reason"]), (True, "has open accepted finding F3"))
        # A carried finding is the one relevance reason that keeps a code panel whole.
        self.assertTrue(opt.keeps_whole(code_records(TEST, ["README.md"], carried=carried)))

    def test_only_named_role_runs_with_reason(self):
        for records in (
            code_records(TEST, ["README.md"], only=True),
            design_records(TEST, plan(["README.md"]), only=True),
        ):
            self.assertEqual(by_id(records)["t"]["reason"], "named by --only")

    def test_skip_unneeded_roles_false_restores_today(self):
        off = settings(skip_unneeded_roles=False)
        self.assertEqual(code_records(SPECIALISTS, ["README.md"], stage_settings=off), [])
        self.assertEqual(design_records(SPECIALISTS, plan(["README.md"]), stage_settings=off), [])

    def test_relevance_always_opts_seat_out(self):
        panel = [seat("gen"), seat("sec", "security", relevance="always")]
        self.assertEqual(code_records(panel, ["src/calc.py"]), [])

    def test_user_role_not_judged_without_relevance(self):
        panel = [seat("gen"), seat("perf", "performance"), seat("db", "database")]
        self.assertEqual(code_records(panel, ["README.md"]), [])

    def test_user_role_opts_in_with_relevance_architecture(self):
        panel = [seat("gen"), seat("perf", "performance", relevance="architecture")]
        record = by_id(code_records(panel, ["src/calc.py"]))["perf"]
        self.assertFalse(record["runs"])
        self.assertIn("(optimization.architecture_paths)", record["reason"])

    def test_relevance_on_general_refused_by_validation(self):
        data = config_mod.default_config()
        data["reviewers"].append(seat("g9", relevance="security"))
        problems = config_mod.validate(data)
        found = [p for p in problems if "relevance: a general reviewer is never skipped" in p]
        self.assertTrue(found, problems)

    def test_relevance_unknown_value_refused(self):
        data = config_mod.default_config()
        data["reviewers"].append(seat("s9", "security", relevance="sometimes"))
        problems = config_mod.validate(data)
        self.assertTrue(any("relevance: must be one of" in problem for problem in problems), problems)

    def test_relevance_always_on_general_is_accepted(self):
        data = config_mod.default_config()
        data["reviewers"].append(seat("g9", relevance="always"))
        self.assertEqual(config_mod.validate(data), [])

    def test_empty_panel_guard_keeps_general_first(self):
        kept = (True, "kept: no other reviewer would run")
        # No general seat: the first configured one is kept.
        panel = [SEC, seat("t", "test")]
        records = by_id(opt.join_relevance(panel, [], code_records(panel, ["README.md"])))
        self.assertEqual((records["sec"]["runs"], records["sec"]["reason"]), kept)
        self.assertFalse(records["t"]["runs"])
        # A general seat left out by its own condition is the one kept.
        plan_ = decide([seat("t", "test"), seat("gen", when="high-risk")], ["README.md"])
        records = by_id(plan_.conditional)
        self.assertEqual((records["gen"]["runs"], records["gen"]["reason"]), kept)
        self.assertFalse(records["t"]["runs"])

    def test_relevance_then_low_risk_cut_counts_qualifying_only(self):
        three = [seat("gen"), seat("gen2"), SEC]
        self.assertEqual(decide(three, ["src/calc.py"]).reviewer_limit, 1)
        # One seat left after the rule: nothing to cut.
        self.assertIsNone(decide(SECURITY, ["src/calc.py"]).reviewer_limit)

    def test_relevance_yes_does_not_keep_whole(self):
        plan_ = decide(SECURITY, ["src/api/routes.py"])
        self.assertTrue(by_id(plan_.conditional)["sec"]["runs"])
        self.assertEqual(plan_.reviewer_limit, 1)

    def test_reason_is_bare_and_the_note_adds_the_hint(self):
        records = code_records(TEST, ["README.md"])
        self.assertEqual(records[0]["reason"], "docs-only change: every reviewed file is documentation")
        self.assertEqual(
            opt.conditional_notes(records),
            [
                "t (when: relevance) left out: docs-only change: every reviewed file is documentation; "
                "--only t to include it"
            ],
        )
        # A seat the rule kept, and a `when` seat left out, get no hint.
        kept = code_records(TEST, ["src/calc.py"])
        self.assertNotIn("--only", opt.conditional_notes(kept)[0])
        when = [{"id": "hr", "when": "high-risk", "runs": False, "reason": "no high-risk path matched"}]
        expected = ["hr (when: high-risk) left out: no high-risk path matched"]
        self.assertEqual(opt.conditional_notes(when), expected)

    def test_relevance_records_leave_the_callers_records_alone(self):
        """The empty-panel guard writes into a joined copy, never into its inputs."""
        panel = [seat("t", "test"), seat("gen", when="high-risk")]
        conditional = [
            {"id": "gen", "when": "high-risk", "runs": False, "reason": "no high-risk path matched"}
        ]
        before = [dict(record) for record in conditional]
        relevance = code_records(panel, ["README.md"])
        relevance_before = [dict(record) for record in relevance]
        joined = opt.join_relevance(panel, conditional, relevance)
        self.assertEqual(conditional, before)
        self.assertEqual(relevance, relevance_before)
        self.assertEqual(by_id(joined)["gen"]["reason"], "kept: no other reviewer would run")

    def test_empty_panel_guard_on_a_design_round(self):
        scan = review_mod.plan_tokens(plan(["docs/guide.md"]))
        design = opt.decide_design_round(settings(), scan, "ok", [SEC, seat("t", "test")])
        records = by_id(design.conditional)
        kept = (True, "kept: no other reviewer would run")
        self.assertEqual((records["sec"]["runs"], records["sec"]["reason"]), kept)
        self.assertFalse(records["t"]["runs"])

    def test_an_empty_reviewed_list_judges_nothing(self):
        plan_ = opt.decide(settings(), {}, ["README.md"], 3, "ok", 4, panel=SPECIALISTS, reviewed=[])
        self.assertEqual(plan_.conditional, [])
        self.assertEqual(opt.code_evidence(["README.md"], []).doubt, "no file was reviewed")

    def test_no_reviewed_list_judges_nothing(self):
        """A snapshot from before the list was recorded has no evidence of absence."""
        plan_ = opt.decide(settings(), {}, ["README.md"], 3, "ok", 4, panel=SPECIALISTS)
        self.assertEqual(plan_.conditional, [])


# --------------------------------------------------------------------------- reporting


def round_event(level, records, runs=("gen",), stage="review", billed=1000):
    event = {
        "stage": stage,
        "status": "ok",
        "reviewers": [{"id": name, "status": "ok", "usage": {"billed_tokens": billed}} for name in runs],
        "optimization": {"level": level, "conditional": records},
    }
    return event


def judged(name, runs, when=opt.WHEN_RELEVANCE):
    return {"id": name, "when": when, "runs": runs, "reason": "test"}


class TestSummarisingRounds(unittest.TestCase):
    def test_summarise_rounds_relevance_counts_and_saving(self):
        events = [
            round_event("balanced", [judged("sec", False), judged("t", True)], runs=("gen", "t")),
            round_event("balanced", [judged("sec", False), judged("t", False)]),
            round_event("quality", [judged("sec", False)], stage="design_review", billed=500),
        ]
        report = opt_report.summarise_rounds(events)
        relevance = report["relevance"]
        self.assertEqual((relevance["judged"], relevance["added"], relevance["left_out"]), (4, 1, 3))
        # Three code runs billed 3,000: 1,000 per run, three seats left out.
        self.assertEqual(relevance["estimated_saving"], 3000)
        design = report["design_relevance"]
        self.assertEqual((design["judged"], design["left_out"], design["estimated_saving"]), (1, 1, 500))
        # Not a conditional reviewer's decision.
        self.assertEqual(report["conditional"]["left_out"], 0)

    def test_summarise_rounds_relevance_left_out_by_level(self):
        events = [
            round_event("balanced", [judged("sec", False)]),
            round_event("quality", [judged("sec", False), judged("t", False)]),
            round_event("quality", [judged("t", True)]),
        ]
        relevance = opt_report.summarise_rounds(events)["relevance"]
        self.assertEqual(relevance["left_out_by_level"], {"balanced": 1, "quality": 2})

    def test_render_roles_skipped_rows_with_quality_count(self):
        events = [
            round_event("quality", [judged("sec", False), judged("t", True)], runs=("gen", "t")),
            round_event("quality", [judged("sec", False)], stage="design_review"),
        ]
        report = opt_report.summarise_rounds(events)
        levels = "\n".join(optimization_render._level_lines(report))
        self.assertIn("roles skipped", levels)
        self.assertIn("left out x1 of 2 judged (quality x1), est. 1,000 tokens", levels)
        spend = "\n".join(optimization_render._spend_lines(report))
        self.assertIn("  roles skipped", spend)
        self.assertIn("left out x1 of 1 judged (quality x1)", spend)

    def test_old_events_without_relevance_read_unchanged(self):
        events = [round_event("balanced", [judged("sec", False, when="high-risk")])]
        report = opt_report.summarise_rounds(events)
        self.assertEqual(report["relevance"]["judged"], 0)
        self.assertEqual(report["conditional"]["left_out"], 1)
        self.assertNotIn("roles skipped", "\n".join(optimization_render._level_lines(report)))
        old = {"stage": "design_review", "status": "ok", "reviewers": []}
        self.assertEqual(opt_report.summarise_rounds([old])["design_relevance"]["judged"], 0)


# --------------------------------------------------------------------------- the presets' panels


class TestThePresetPanels(unittest.TestCase):
    """What the fitted panels run: no preset opts a security seat in to its rule."""

    def test_standard_high_risk_round_runs_security_on_opus_once(self):
        panel = presets.expand("standard", ["claude", "codex"]).values["reviewers"]
        security = [reviewer for reviewer in panel if reviewer["role"] == "security"]
        self.assertEqual([reviewer["id"] for reviewer in security], ["claude-security"])
        self.assertFalse([reviewer for reviewer in panel if "when" in reviewer])
        switched, changed = opt.risk_model(security[0], True)
        self.assertTrue(changed)
        self.assertEqual(switched["model"]["family"], "opus")
        self.assertEqual(opt.risk_model(security[0], False)[0]["model"]["family"], "sonnet")

    def test_quality_preset_docs_only_round_leaves_out_test_and_architecture(self):
        """Its security seats do not opt in, so they run on a docs-only round too."""
        values = presets.expand("quality", ["claude", "codex"]).values
        left_out = {"claude-test": False, "claude-architecture": False}
        code = code_records(values["reviewers"], ["docs/guide.md"])
        self.assertEqual({record["id"]: record["runs"] for record in code}, left_out)
        design = design_records(values["review"]["design"]["reviewers"], plan(["docs/guide.md"]))
        self.assertEqual({record["id"]: record["runs"] for record in design}, left_out)


# --------------------------------------------------------------------------- through the CLI

DEFAULT_PANEL = ("claude-general", "codex-general", "claude-security", "claude-test")


@unittest.skipUnless(has_git(), "git is required")
class TestCodeRoundsThroughTheCli(IsolatedCase):
    """m1 general and m2 security, on a change to app.py that has nothing for m2."""

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
        for name in DEFAULT_PANEL:
            run_cli("reviewer", "remove", name)
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        argv = ("--provider", "mock", "--id", "m2", "--role", "security", "--relevance", "security")
        run_cli("reviewer", "add", *argv)
        run_cli("config", "set", "optimization.level", "quality")
        self.workspace = self.cli_workspace()

    def last_review_event(self):
        events = self.workspace.read_state().get("events") or []
        return [event for event in events if event.get("stage") == "review"][-1]

    def test_review_run_prints_relevance_notes_code(self):
        run_cli("review", "snapshot")
        code, out, err = run_cli("review", "run")
        self.assertEqual(code, 0, err)
        note = (
            "note: m2 (when: relevance) left out: no security-relevant path changed "
            "(optimization.security_paths); --only m2 to include it"
        )
        self.assertIn(note, err.splitlines())
        self.assertIn("1 successful, 0 failed", out)

    def test_code_event_conditional_has_relevance_record(self):
        run_cli("review", "snapshot")
        run_cli("review", "run")
        event = self.last_review_event()
        self.assertEqual([run["id"] for run in event["reviewers"]], ["m1"])
        records = event["optimization"]["conditional"]
        self.assertEqual([(r["id"], r["when"], r["runs"]) for r in records], [("m2", "relevance", False)])

    def test_status_optimization_line_shows_left_out_role(self):
        run_cli("review", "snapshot")
        _, out, _ = run_cli("status")
        self.assertIn("; m2 left out (no security-relevant path changed", out)
        # The record holds the rule's answer; the --only hint is the note's.
        self.assertNotIn("to include it", out)

    def test_status_preview_on_a_snapshot_without_files_judges_nobody(self):
        """A snapshot from before the list was recorded: doubt, so every seat runs."""
        run_cli("review", "snapshot")
        meta = self.workspace.read_snapshot_meta()
        meta.pop("files", None)
        with open(self.workspace.snapshot_meta_path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle)
        payload = json.loads(run_cli("status", "--json")[1])
        self.assertEqual(payload["optimization"]["conditional"], [])
        self.assertNotIn("m2 left out", run_cli("status")[1])

    def test_condition_excluded_reads_relevance_records_code(self):
        run_cli("review", "snapshot")
        self.assertEqual(run_cli("review", "run", "--only", "m2")[0], 0)
        self.assertTrue(os.path.isfile(self.workspace.reviewer_report_path("m2")))
        self.assertEqual(run_cli("review", "run")[0], 0)
        data = ws.read_json(self.workspace.consolidated_json_path, {})
        self.assertEqual([entry["id"] for entry in data["reviewers"]], ["m1"])
        # And a rebuild reads the reports that round did, not m2's earlier one.
        self.assertEqual(run_cli("review", "consolidate")[0], 0)
        data = ws.read_json(self.workspace.consolidated_json_path, {})
        self.assertTrue(data["findings"])
        for item in data["findings"]:
            self.assertNotIn("m2", item["reported_by"])

    def test_scorecard_left_out_rounds_counts_relevance(self):
        run_cli("review", "snapshot")
        run_cli("review", "run")
        run_cli("review", "triage", "F1", "--status", "accepted")
        card = json.loads(run_cli("optimization", "report", "--json")[1])["scorecard"]["code"]
        group = card["reviewers"]["m2"]
        self.assertEqual((group["runs"], group["left_out_rounds"], group["when"]), (0, 1, "relevance"))

    def test_a_security_seat_without_relevance_runs_on_a_plain_module(self):
        """Path names prove nothing about what a change does: by default the seat runs."""
        self.assertEqual(run_cli("reviewer", "set", "m2", "--relevance", "default")[0], 0)
        run_cli("review", "snapshot")
        code, out, err = run_cli("review", "run")
        self.assertEqual(code, 0, err)
        self.assertIn("2 successful, 0 failed", out)
        self.assertNotIn("(when: relevance)", err)

    def test_the_switch_off_runs_every_seat(self):
        run_cli("config", "set", "optimization.skip_unneeded_roles", "false")
        run_cli("review", "snapshot")
        code, out, err = run_cli("review", "run")
        self.assertEqual(code, 0, err)
        self.assertIn("2 successful, 0 failed", out)
        self.assertNotIn("(when: relevance)", err)


DOCS_ONLY_PLAN = """# Plan

## Proposed Change
Reword the guide.

## Files to Modify
- `docs/guide.md`
"""


class TestDesignRoundsThroughTheCli(DesignReviewCase):
    """m1 general and m2 architecture, on a plan that changes one document."""

    def last_design_event(self):
        events = self.workspace.read_state().get("events") or []
        return [event for event in events if event.get("stage") == "design_review"][-1]

    def test_review_run_prints_relevance_notes_design(self):
        self.write_plan(DOCS_ONLY_PLAN)
        code, out, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 0, err)
        head = "note: m2 (when: relevance) left out: "
        notes = [line for line in err.splitlines() if line.startswith(head)]
        self.assertEqual(len(notes), 1, err)
        self.assertTrue(notes[0].endswith("; --only m2 to include it"), notes)
        self.assertIn("1 successful, 0 failed", out)

    def test_design_event_has_optimization_block(self):
        self.write_plan(DOCS_ONLY_PLAN)
        run_cli("review", "run", "--design")
        event = self.last_design_event()
        block = event["optimization"]
        self.assertEqual(list(block), ["level", "high_risk", "declared", "conditional", "files"])
        records = block["conditional"]
        self.assertEqual([(r["id"], r["when"], r["runs"]) for r in records], [("m2", "relevance", False)])
        self.assertEqual((block["high_risk"], block["declared"], block["files"]), ([], False, 0))
        self.assertEqual([run["id"] for run in event["reviewers"]], ["m1"])

    def test_status_design_line_shows_left_out_role(self):
        self.write_plan(DOCS_ONLY_PLAN)
        _, out, _ = run_cli("status")
        line = next(line for line in out.splitlines() if line.startswith("Design review:"))
        self.assertIn("; m2 left out (", line)
        payload = json.loads(run_cli("status", "--json")[1])
        records = payload["design_review"]["optimization"]["conditional"]
        self.assertEqual([(r["id"], r["runs"]) for r in records], [("m2", False)])

    def test_condition_excluded_reads_relevance_records_design(self):
        self.write_plan(DOCS_ONLY_PLAN)
        self.assertEqual(run_cli("review", "run", "--design", "--only", "m2")[0], 0)
        self.assertEqual(run_cli("review", "run", "--design")[0], 0)
        self.assertEqual(run_cli("review", "consolidate", "--design")[0], 0)
        data = ws.read_json(self.design.consolidated_json_path, {})
        self.assertTrue(data["findings"])
        for item in data["findings"]:
            self.assertNotIn("m2", item["reported_by"])

    def carry_m2_finding(self):
        """A round in which only m2 reported F1, triaged accepted."""
        self.write_plan(DOCS_ONLY_PLAN)
        self.assertEqual(run_cli("review", "run", "--design", "--only", "m2")[0], 0)
        self.assertEqual(run_cli("review", "triage", "--design", "F1", "--status", "accepted")[0], 0)

    def m2_record(self):
        records = self.last_design_event()["optimization"]["conditional"]
        return next(record for record in records if record["id"] == "m2")

    def test_a_carried_design_finding_keeps_its_seat_on_the_same_plan(self):
        self.carry_m2_finding()
        code, out, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 0, err)
        self.assertIn("note: m2 (when: relevance) added: has open accepted finding F1", err.splitlines())
        self.assertIn("2 successful, 0 failed", out)
        self.assertEqual(self.m2_record()["reason"], "has open accepted finding F1")

    def test_a_carried_design_finding_keeps_its_seat_on_a_revised_plan(self):
        """The revision is meant to address it, so the seat must re-check it."""
        self.carry_m2_finding()
        self.write_plan(DOCS_ONLY_PLAN + "- `docs/other.md`\n")
        payload = json.loads(run_cli("review", "status", "--design", "--json")[1])
        preview = {record["id"]: record for record in payload["optimization"]["conditional"]}
        self.assertTrue(preview["m2"]["runs"], preview)
        code, out, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 0, err)
        self.assertIn("2 successful, 0 failed", out)
        record = self.m2_record()
        self.assertEqual((record["runs"], record["reason"]), (True, "has open accepted finding F1"))
        data = ws.read_json(self.design.consolidated_json_path, {})
        self.assertEqual(data["iteration"], 2)
        self.assertIn("m2", [name for item in data["findings"] for name in item["reported_by"]])

    def test_a_carried_finding_keeps_a_high_risk_seat_on_a_revised_plan(self):
        """`when: high-risk` is kept the same way: the revision no longer touches a risky path."""
        argv = ("--design", "--provider", "mock", "--id", "h1", "--when", "high-risk")
        self.assertEqual(run_cli("reviewer", "add", *argv)[0], 0)
        self.write_plan(plan(["src/auth.py"], "Check the session token before trusting it."))
        self.assertEqual(run_cli("review", "run", "--design", "--only", "h1")[0], 0)
        self.assertEqual(run_cli("review", "triage", "--design", "F1", "--status", "accepted")[0], 0)
        self.write_plan(DOCS_ONLY_PLAN)
        code, _out, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 0, err)
        self.assertIn("note: h1 (when: high-risk) added: has open accepted finding F1", err.splitlines())
        records = self.last_design_event()["optimization"]["conditional"]
        record = next(record for record in records if record["id"] == "h1")
        self.assertEqual((record["runs"], record["reason"]), (True, "has open accepted finding F1"))
        self.assertIn("h1", [run["id"] for run in self.last_design_event()["reviewers"]])

    def test_design_carried_without_a_report_or_with_another_lineage(self):
        self.assertEqual(cli_review._design_carried(self.workspace, "any"), [])
        self.carry_m2_finding()
        lineage = ws.read_json(self.design.consolidated_json_path, {})["lineage"]
        self.assertTrue(lineage)

        def carried(given):
            return [finding["id"] for finding in cli_review._design_carried(self.workspace, given)]

        self.assertEqual(carried(lineage), ["F1"])
        # No lineage to compare with: the live report's findings are carried.
        self.assertEqual(carried(""), ["F1"])
        self.assertEqual(cli_review._design_carried(self.workspace, lineage + "-other"), [])

    def test_a_reset_lineage_carries_nothing(self):
        self.carry_m2_finding()
        self.assertEqual(run_cli("budget", "reset")[0], 0)
        code, out, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 0, err)
        self.assertIn("1 successful, 0 failed", out)
        self.assertFalse(self.m2_record()["runs"])

    def test_a_plan_without_files_to_modify_judges_nobody(self):
        self.write_plan()
        code, out, err = run_cli("review", "run", "--design")
        self.assertEqual(code, 0, err)
        self.assertIn("2 successful, 0 failed", out)
        self.assertNotIn("(when: relevance)", err)


if __name__ == "__main__":
    unittest.main()
