"""`review show --brief` and `review show <ids>`: triage without the whole report.

The brief view is the report's head and one line per finding; the ids form
prints only the findings asked for. Both read the same consolidated.json the
full report does, so these tests write one directly instead of running a
round.
"""

from __future__ import annotations

import copy
import json
import unittest

from helpers import IsolatedCase, run_cli

from orchestrator import review as review_mod
from orchestrator import review_consolidation
from orchestrator import workspace as ws

LONG_PROBLEM = "a" * 200

REPORT = {
    "generated_at": "2026-01-01T00:00:00Z",
    "iteration": 1,
    "snapshot": {"sha256": "abcdef0123456789"},
    "counts": {
        "reviewers_ok": 2,
        "reviewers_total": 2,
        "findings_total": 2,
        "duplicates_merged": 0,
        "duplicate_candidates": 1,
    },
    "reviewers": [
        {"id": "m1", "role": "general", "provider": "mock", "model": "", "status": "ok", "findings": 1},
        {"id": "m2", "role": "security", "provider": "mock", "model": "", "status": "ok", "findings": 1},
    ],
    "duplicate_candidates": [{"ids": ["F1", "F2"], "file": "app.py", "shared_code": ["count += 1"]}],
    "findings": [
        {
            "id": "F1",
            "severity": "high",
            "category": "correctness",
            "file": "app.py",
            "line": 10,
            "reported_by": ["m1"],
            "possible_duplicates": ["F2"],
            "triage": "needs-triage",
            "problem": "the counter is never reset\nso the second run starts where the first stopped",
            "impact": "wrong totals",
            "evidence": "count += 1",
            "recommended_fix": "reset it in start()",
        },
        {
            "id": "F2",
            "severity": "medium",
            "category": "security",
            "file": "app.py",
            "line": 12,
            "reported_by": ["m2"],
            "possible_duplicates": ["F1"],
            "triage": "accepted",
            "problem": LONG_PROBLEM,
            "impact": "a leak",
            "evidence": "log(count)",
            "recommended_fix": "drop the log line",
        },
    ],
    "pre_existing": [
        {
            "id": "P1",
            "severity": "low",
            "file": "old.py",
            "line": 1,
            "reported_by": ["m1"],
            "problem": "an old problem",
            "impact": "none new",
            "recommended_fix": "later",
        }
    ],
}

BRIEF_FINDING_KEYS = {
    "id",
    "severity",
    "category",
    "file",
    "line",
    "reported_by",
    "triage",
    "problem",
    "possible_duplicates",
}


class ReviewBriefTest(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace()
        ws.write_json(self.workspace.consolidated_json_path, copy.deepcopy(REPORT))

    def test_brief_text(self):
        code, out, err = run_cli("review", "show", "--brief")
        self.assertEqual(code, 0, err)
        expected = "\n".join(
            [
                "Source: %s" % self.workspace.relative(self.workspace.consolidated_json_path),
                "# Consolidated review",
                "",
                "- Generated: 2026-01-01T00:00:00Z",
                "- Iteration: 1",
                "- Snapshot: abcdef012345",
                "- Reviewers: 2 ok / 2 total",
                "- Findings: 2 (0 duplicate report(s) merged)",
                "- Pre-existing (not triaged): 1",
                "",
                "## Possible duplicates (confirm during triage)",
                "",
                "- F1 ~ F2  (app.py)",
                "",
                "## Findings",
                "",
                "F1  HIGH  app.py:10  [m1]  needs-triage  the counter is never reset...",
                "F2  MEDIUM  app.py:12  [m2]  accepted  %s..." % ("a" * 137),
                "",
                "Full finding: review show <id> [<id> ...]",
                "",
            ]
        )
        self.assertEqual(out, expected)
        for line in out.splitlines():
            self.assertNotIn("reset it in start()", line)

    def test_brief_json_keys(self):
        code, out, err = run_cli("review", "show", "--brief", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(
            set(data),
            {"iteration", "counts", "duplicate_candidates", "findings", "pre_existing_count"},
        )
        self.assertEqual(data["pre_existing_count"], 1)
        self.assertEqual([entry["id"] for entry in data["findings"]], ["F1", "F2"])
        for entry in data["findings"]:
            self.assertEqual(set(entry), BRIEF_FINDING_KEYS)
        self.assertEqual(data["findings"][0]["problem"], "the counter is never reset...")
        self.assertEqual(len(data["findings"][1]["problem"]), 140)

    def test_show_ids_prints_only_those(self):
        code, out, err = run_cli("review", "show", "F2")
        self.assertEqual(code, 0, err)
        self.assertIn("### F2 [MEDIUM] security", out)
        self.assertIn("- Recommended fix: drop the log line", out)
        self.assertNotIn("### F1 ", out)
        self.assertEqual(out.count("### F2 "), 1)
        self.assertNotIn("# Consolidated review", out)

    def test_repeated_ids_print_once(self):
        code, out, err = run_cli("review", "show", "F2", "F2")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count("### F2 "), 1)

        code, out, err = run_cli("review", "show", "--brief", "F2", "F2")
        self.assertEqual(code, 0, err)
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("F2  ")]), 1)
        self.assertNotIn("F1  HIGH", out)

    def test_brief_with_ids_in_text(self):
        code, out, err = run_cli("review", "show", "--brief", "F1")
        self.assertEqual(code, 0, err)
        self.assertIn("# Consolidated review", out)
        self.assertIn("F1  HIGH  app.py:10", out)
        self.assertNotIn("F2  MEDIUM", out)
        self.assertNotIn("reset it in start()", out)
        self.assertIn("Full finding: review show <id> [<id> ...]", out)

    def test_duplicate_pairs_follow_the_shown_findings(self):
        """A pair with a finding the view hides is left out of it (text and JSON)."""
        code, out, err = run_cli("review", "show", "--brief", "--accepted")
        self.assertEqual(code, 0, err)
        self.assertNotIn("F1 ~ F2", out)
        self.assertNotIn("## Possible duplicates", out)
        data = json.loads(run_cli("review", "show", "--brief", "--accepted", "--json")[1])
        self.assertEqual(data["duplicate_candidates"], [])

        data = json.loads(run_cli("review", "show", "--brief", "F1", "F2", "--json")[1])
        self.assertEqual([pair["ids"] for pair in data["duplicate_candidates"]], [["F1", "F2"]])
        self.assertIn("- F1 ~ F2  (app.py)", run_cli("review", "show", "--brief", "F2", "F1")[1])

    def test_show_ids_json_filters_findings(self):
        full = json.loads(run_cli("review", "show", "--json")[1])
        code, out, err = run_cli("review", "show", "F2", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(set(data), set(full))
        self.assertEqual([finding["id"] for finding in data["findings"]], ["F2"])
        self.assertEqual(data["pre_existing"], full["pre_existing"])

    def test_unknown_id_exits_2(self):
        code, out, err = run_cli("review", "show", "F1", "F9")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("F9", err)
        # A pre-existing finding is not triaged, so it is not picked by id.
        self.assertEqual(run_cli("review", "show", "P1")[0], 2)

    def test_brief_with_design_and_accepted(self):
        design = self.workspace.design_review().ensure()
        ws.write_json(design.consolidated_json_path, copy.deepcopy(REPORT))
        code, out, err = run_cli("review", "show", "--design", "--brief", "--accepted")
        self.assertEqual(code, 0, err)
        self.assertIn("F2  MEDIUM", out)
        self.assertNotIn("F1  HIGH", out)
        self.assertIn("Full finding: review show --design <id> [<id> ...]", out)

        code, out, _ = run_cli("review", "show", "--design", "--brief", "--accepted", "--json", "F2")
        self.assertEqual(code, 0)
        self.assertEqual([entry["id"] for entry in json.loads(out)["findings"]], ["F2"])

        # Not accepted, so not among the findings --accepted leaves.
        self.assertEqual(run_cli("review", "show", "--design", "--accepted", "F1")[0], 2)

    def test_brief_with_nothing_accepted(self):
        report = copy.deepcopy(REPORT)
        report["findings"][1]["triage"] = "rejected"
        ws.write_json(self.workspace.consolidated_json_path, report)
        code, out, err = run_cli("review", "show", "--brief", "--accepted")
        self.assertEqual(code, 0, err)
        self.assertIn("## Findings\n\nNo findings were reported.\n", out)
        self.assertNotIn("Full finding:", out)


class OneLineTest(unittest.TestCase):
    def test_cases(self):
        cases = [
            ("a" * 140, "a" * 140),
            ("a" * 141, "a" * 137 + "..."),
            ("short\nmore", "short..."),
            ("  short  \n", "short"),
            ("", ""),
            (None, ""),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(review_consolidation._one_line(text), expected)

    def test_brief_entries(self):
        data = {"findings": [{"id": "F1", "file": "a.py", "line": None, "reported_by": ["m1"]}]}
        self.assertEqual(review_mod.brief_findings(data)[0]["problem"], "")
        out = review_mod.render_brief(data)
        self.assertIn("F1  MEDIUM  a.py:n/a  [m1]  needs-triage  \n", out)
        data["findings"][0]["line"] = ""
        self.assertIn("a.py:n/a", review_mod.render_brief(data))

        out = review_mod.render_brief({"findings": []})
        self.assertTrue(out.endswith("## Findings\n\nNo findings were reported.\n"), out)
        self.assertNotIn("Full finding:", out)


if __name__ == "__main__":
    unittest.main()
