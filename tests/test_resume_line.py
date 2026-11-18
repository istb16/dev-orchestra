"""The `Resume:` line of `summary` and its `resume` key in `--json`.

A new host session resolves to a new workflow, so the final report names the
id and how to pass it on: `--workflow <id>` or `DEV_ORCHESTRA_WORKFLOW`.
"""

from __future__ import annotations

import json
import unittest

from helpers import TEST_WORKFLOW, IsolatedCase, run_cli

from orchestrator import cli_workflow

RESUME_LINE = (
    "Resume:  in a new session, continue workflow %s: pass --workflow %s"
    " to every command (or set DEV_ORCHESTRA_WORKFLOW=%s)" % ((TEST_WORKFLOW,) * 3)
)


class TestResumeLine(IsolatedCase):
    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")

    def test_summary_text_has_resume(self):
        code, out, err = run_cli("summary")
        self.assertEqual(code, 0, err)
        lines = out.rstrip("\n").splitlines()
        self.assertEqual(lines[-1], RESUME_LINE)
        self.assertEqual(lines[-2], "")

    def test_summary_json_resume(self):
        code, out, err = run_cli("summary", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out)["resume"],
            {
                "workflow": TEST_WORKFLOW,
                "argument": "--workflow %s" % TEST_WORKFLOW,
                "env": {"DEV_ORCHESTRA_WORKFLOW": TEST_WORKFLOW},
            },
        )

    def test_global_flag_names_the_workflow(self):
        out = run_cli("--workflow", "other", "summary")[1]
        self.assertIn("pass --workflow other to every command", out)

    def test_no_workflow_id_has_no_resume(self):
        self.assertIsNone(cli_workflow._resume(""))


if __name__ == "__main__":
    unittest.main()
