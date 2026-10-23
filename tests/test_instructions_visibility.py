"""Where the instructions in force and ``implementation.unit_tests`` are shown.

`status`, `summary`, `units status`, `config show` and `doctor` each say so
when a key is set, and print nothing new when none is.
"""

from __future__ import annotations

import json
import unittest

from helpers import IsolatedCase, run_cli
from test_units import plan

from orchestrator import workspace as ws

ORCHESTRATOR_TEXT = "Ask before touching the migrations."
IMPLEMENTER_TEXT = "Use the fixtures under spec/fixtures."

CONFIGURED = (
    "instructions:\n"
    "  orchestrator: %s\n"
    "  implementer:\n"
    "    file: docs/implementer.md\n"
    "  reviewers:\n"
    "    file: docs/missing.md\n" % ORCHESTRATOR_TEXT
)
IN_FORCE = ["orchestrator", "implementer", "reviewers"]


class VisibilityCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")

    def configure(self, body: str) -> None:
        self.write(".dev-orchestra.yaml", "version: 1\n" + body)

    def configure_instructions(self) -> None:
        self.write("docs/implementer.md", IMPLEMENTER_TEXT + "\n")
        self.configure(CONFIGURED)

    def json_of(self, *argv: str):
        code, out, err = run_cli(*argv, "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)


class TestStatus(VisibilityCase):
    def test_status_lines(self):
        self.configure_instructions()
        out = run_cli("status")[1]
        line = next(line for line in out.splitlines() if line.startswith("Instructions: "))
        self.assertIn("orchestrator (project, inline, %d chars)" % len(ORCHESTRATOR_TEXT), line)
        self.assertIn("implementer (project, docs/implementer.md, %d chars)" % len(IMPLEMENTER_TEXT), line)
        self.assertIn("reviewers (project, docs/missing.md, 0 chars: not found)", line)
        self.assertIn(
            "Orchestrator instruction (follow it; the skill's rules come first):\n  %s\n" % ORCHESTRATOR_TEXT,
            out,
        )

        found = self.json_of("status")["instructions"]
        self.assertEqual(found["orchestrator"], ORCHESTRATOR_TEXT)
        self.assertEqual([row["role"] for row in found["in_force"]], IN_FORCE)
        self.assertEqual(found["in_force"][2]["problem"], "not found")

    def test_status_without_instructions(self):
        out = run_cli("status")[1]
        self.assertNotIn("Instructions:", out)
        self.assertNotIn("Orchestrator instruction", out)
        self.assertEqual(self.json_of("status")["instructions"], {"in_force": [], "orchestrator": ""})

    def test_pending_approval_names_the_instructions(self):
        workspace = self.cli_workspace()
        ws.write_text(workspace.plan_path, plan([{"name": "a", "files": ["x"]}]))
        plain = "Plan approval: pending -- present %s to the user and ask;" % workspace.relative(
            workspace.plan_path
        )
        self.assertIn(plain, run_cli("status")[1])
        self.configure_instructions()
        out = run_cli("status")[1]
        self.assertNotIn(plain, out)
        self.assertIn(" and the instructions in force to the user and ask;", out)

    def test_status_unit_tests(self):
        self.assertNotIn("Unit tests:", run_cli("status")[1])
        self.assertEqual(self.json_of("status")["implementation"], {"unit_tests": "worktree"})
        self.configure("implementation:\n  unit_tests: after_merge\n")
        self.assertIn("Unit tests: after_merge", run_cli("status")[1])
        self.assertEqual(self.json_of("status")["implementation"], {"unit_tests": "after_merge"})


class TestUnitsStatus(VisibilityCase):
    def test_units_status(self):
        workspace = self.cli_workspace()
        ws.write_text(workspace.plan_path, plan([{"name": "a", "files": ["x"]}]))
        self.assertEqual(self.json_of("units", "status")["unit_tests"], "worktree")
        self.assertNotIn("Unit tests:", run_cli("units", "status")[1])
        self.configure("implementation:\n  unit_tests: after_merge\n")
        self.assertEqual(self.json_of("units", "status")["unit_tests"], "after_merge")
        self.assertIn("Unit tests: after_merge", run_cli("units", "status")[1])


class TestSummary(VisibilityCase):
    def test_summary(self):
        self.assertEqual(self.json_of("summary")["instructions"], [])
        self.assertNotIn("Instructions:", run_cli("summary")[1])
        self.configure_instructions()
        rows = self.json_of("summary")["instructions"]
        self.assertEqual([row["role"] for row in rows], IN_FORCE)
        out = run_cli("summary")[1]
        expected = "Instructions:\n  orchestrator (project, inline, %d chars)\n" % len(ORCHESTRATOR_TEXT)
        self.assertIn(expected, out)
        self.assertIn("  reviewers (project, docs/missing.md, 0 chars: not found)", out)


class TestConfigShowAndDoctor(VisibilityCase):
    def test_config_show_and_doctor(self):
        self.assertNotIn("Orchestrator instruction:", run_cli("config", "show")[1])
        self.configure_instructions()
        out = run_cli("config", "show")[1]
        self.assertIn("  Instructions\n", out)
        self.assertIn("    implementer (project, docs/implementer.md, %d chars)" % len(IMPLEMENTER_TEXT), out)
        self.assertIn("    Orchestrator instruction:\n      %s\n" % ORCHESTRATOR_TEXT, out)

        notes = json.loads(run_cli("doctor", "--fast", "--json")[1])["notes"]
        missing = [note for note in notes if note.startswith("instructions for reviewers ")]
        self.assertEqual(len(missing), 1, notes)
        self.assertIn("docs/missing.md not found", missing[0])
        self.assertFalse(any(note.startswith("instructions for implementer ") for note in notes))


if __name__ == "__main__":
    unittest.main()
