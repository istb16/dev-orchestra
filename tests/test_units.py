"""Work units: the ``## Units`` block of a plan, its checks and its schedule."""

from __future__ import annotations

import json
import os
import unittest
from typing import Any, Dict, List, Optional

from helpers import IsolatedCase, has_git, run_cli

from orchestrator import units as units_mod
from orchestrator import workspace as ws


def plan(entries: List[Dict[str, Any]], sections: Optional[List[str]] = None) -> str:
    """A plan with a ``## Units`` block of ``entries`` and a section per unit."""
    lines = ["# Plan", "", "## Goal", "Something.", "", "## Files to Modify", "- `scripts/a.py`"]
    lines += ["", "## Units", "```yaml", "units:"]
    for entry in entries:
        lines.append("  - name: %s" % entry["name"])
        lines.append("    kind: %s" % entry.get("kind", "feature"))
        lines.append("    files: [%s]" % ", ".join(entry["files"]))
        lines.append("    depends_on: [%s]" % ", ".join(entry.get("depends_on", [])))
    lines += ["```", "", "## Implementation Steps"]
    names = sections if sections is not None else [str(entry["name"]) for entry in entries]
    for name in names:
        lines += ["### Unit: %s" % name, "1. Do %s." % name, ""]
    return "\n".join(lines) + "\n"


class UnitsCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace()

    def write_plan(self, text: str) -> None:
        ws.write_text(self.workspace.plan_path, text)

    def check(self, text: str):
        self.write_plan(text)
        return run_cli("units", "check")


class TestParse(UnitsCase):
    def test_units_parse_no_units_section(self):
        text = "# Plan\n\n## Goal\nOne thing.\n\n## Implementation Steps\n1. Do it.\n"
        self.assertEqual(units_mod.parse(text), [])
        code, out, err = self.check(text)
        self.assertEqual(code, 0, err)
        self.assertIn("single run", out)

    def test_units_parse_reads_entries(self):
        text = plan(
            [
                {"name": "a", "files": ["scripts/a.py"]},
                {"name": "b", "kind": "docs", "files": ["./docs/*.md"], "depends_on": ["a"]},
            ]
        )
        found = units_mod.parse(text)
        self.assertEqual([unit.name for unit in found], ["a", "b"])
        self.assertEqual(found[1].kind, "docs")
        self.assertEqual(found[1].files, ["docs/*.md"])
        self.assertEqual(found[1].depends_on, ["a"])
        self.assertIn("1. Do b.", units_mod.section(text, "b"))
        self.assertNotIn("Do a", units_mod.section(text, "b"))
        self.assertEqual(units_mod.section(text, "c"), "")

    def test_units_headings_read_as_the_plan_review_reads_them(self):
        text = plan([{"name": "a", "files": ["x"]}, {"name": "b", "files": ["y"]}])
        # Indented with closing hashes, still a heading; under a ~~~ fence, none.
        text = text.replace("### Unit: b\n", "   ### Unit: b ###\n~~~\n## Not a heading\n~~~\n")
        self.assertIn("Do a.", units_mod.section(text, "a"))
        self.assertNotIn("Do b.", units_mod.section(text, "a"))
        found = units_mod.section(text, "b")
        self.assertIn("## Not a heading", found)
        self.assertIn("Do b.", found)

    def test_units_parse_reads_the_block_in_any_fence(self):
        text = plan([{"name": "a", "files": ["x"]}])
        for opening, closing in (("~~~yaml", "~~~"), ("````yaml", "````"), ("``` yaml title", "```")):
            with self.subTest(opening=opening):
                fenced = text.replace("```yaml\n", opening + "\n").replace("```\n\n", closing + "\n\n")
                self.assertEqual([unit.name for unit in units_mod.parse(fenced)], ["a"])
        # A ```yaml inside another fence is not the block.
        nested = text.replace("```yaml\n", "~~~\n```yaml\nunits: []\n```\n~~~\n```yaml\n")
        self.assertEqual([unit.name for unit in units_mod.parse(nested)], ["a"])

    def test_units_parse_refuses_a_bad_entry(self):
        for entry in (
            {"name": "Bad Name", "files": ["x"]},
            {"name": "a", "kind": "chore", "files": ["x"]},
            {"name": "a", "files": []},
        ):
            with self.subTest(entry=entry):
                with self.assertRaises(units_mod.UnitsError):
                    units_mod.parse(plan([entry]))
                code, _, err = self.check(plan([entry]))
                self.assertEqual(code, 2, err)

    def test_units_check_missing_section(self):
        code, _, err = self.check(plan([{"name": "a", "files": ["x"]}], sections=[]))
        self.assertEqual(code, 2)
        self.assertIn("### Unit: a", err)


class TestCheck(UnitsCase):
    def test_units_check_unknown_dependency(self):
        code, _, err = self.check(plan([{"name": "a", "files": ["x"], "depends_on": ["ghost"]}]))
        self.assertEqual(code, 2)
        self.assertIn("ghost", err)

    def test_units_check_cycle(self):
        code, _, err = self.check(
            plan(
                [
                    {"name": "a", "files": ["x"], "depends_on": ["c"]},
                    {"name": "b", "files": ["y"], "depends_on": ["a"]},
                    {"name": "c", "files": ["z"], "depends_on": ["b"]},
                ]
            )
        )
        self.assertEqual(code, 2)
        self.assertIn("cycle", err)
        self.assertIn("a -> c -> b -> a", err)

    def test_units_check_duplicate_name(self):
        code, _, err = self.check(plan([{"name": "a", "files": ["x"]}, {"name": "a", "files": ["y"]}]))
        self.assertEqual(code, 2)
        self.assertIn("duplicate", err)

    def test_units_check_parallel_overlap(self):
        pair = [{"name": "a", "files": ["scripts/a.py"]}, {"name": "b", "files": ["scripts/a.py"]}]
        code, _, err = self.check(plan(pair))
        self.assertEqual(code, 2)
        self.assertIn("a and b may run in parallel", err)
        chained = [pair[0], dict(pair[1], depends_on=["a"])]
        code, out, err = self.check(plan(chained))
        self.assertEqual(code, 0, err)
        self.assertIn("wave 2: b", out)

    def test_units_check_glob_overlap(self):
        for first, second in (
            ("scripts/*.py", "scripts/a.py"),
            ("scripts/orchestrator/*.py", "scripts/**"),
        ):
            with self.subTest(first=first, second=second):
                code, _, err = self.check(
                    plan([{"name": "a", "files": [first]}, {"name": "b", "files": [second]}])
                )
                self.assertEqual(code, 2)
                self.assertIn("may run in parallel", err)
        self.assertFalse(units_mod.overlaps("scripts/*.py", "tests/test_a.py"))
        self.assertFalse(units_mod.overlaps("scripts/*.py", "tests/*.py"))

    def test_a_directory_overlaps_what_is_under_it(self):
        for first, second in (
            ("tests", "tests/a.py"),
            ("tests/", "tests/a.py"),
            ("scripts/orchestrator", "scripts/orchestrator/units.py"),
            ("tests/", "tests"),
            ("tests", "tests/unit/*.py"),
            ("tests/", "tests/unit/*.py"),
        ):
            with self.subTest(first=first, second=second):
                self.assertTrue(units_mod.overlaps(first, second))
                self.assertTrue(units_mod.overlaps(second, first))
        code, _, err = self.check(
            plan(
                [
                    {"name": "a", "files": ["scripts/orchestrator"]},
                    {"name": "b", "files": ["scripts/orchestrator/units.py"]},
                ]
            )
        )
        self.assertEqual(code, 2)
        self.assertIn("may run in parallel", err)
        for first, second in (
            ("tests/a.py", "tests2/b.py"),
            ("tests", "tests2/b.py"),
            ("tests", "tests2/*.py"),
            ("tests/a.py", "tests/b.py"),
        ):
            with self.subTest(first=first, second=second):
                self.assertFalse(units_mod.overlaps(first, second))
                self.assertFalse(units_mod.overlaps(second, first))

    def test_units_check_integration_must_be_last(self):
        entries = [
            {"name": "a", "files": ["x"]},
            {"name": "b", "files": ["y"]},
            {"name": "glue", "kind": "integration", "files": ["z"], "depends_on": ["a"]},
        ]
        code, _, err = self.check(plan(entries))
        self.assertEqual(code, 2)
        self.assertIn("glue: an integration unit must depend on every other unit", err)
        entries[2]["depends_on"] = ["a", "b"]
        code, _, err = self.check(plan(entries))
        self.assertEqual(code, 0, err)

    def test_units_check_one_integration_unit(self):
        entries = [
            {"name": "a", "files": ["x"]},
            {"name": "i1", "kind": "integration", "files": ["y"], "depends_on": ["a"]},
            {"name": "i2", "kind": "integration", "files": ["z"], "depends_on": ["a", "i1"]},
        ]
        code, _, err = self.check(plan(entries))
        self.assertEqual(code, 2)
        self.assertIn("more than one integration unit", err)

    def test_units_check_hub_path_in_parallel_unit(self):
        entries = [
            {"name": "a", "files": ["scripts/a.py", "CHANGELOG.md"]},
            {"name": "b", "files": ["scripts/b.py"]},
        ]
        code, _, err = self.check(plan(entries))
        self.assertEqual(code, 2)
        self.assertIn("a: CHANGELOG.md is a hub file", err)
        entries[0]["files"] = ["scripts/a.py"]
        entries.append(
            {"name": "glue", "kind": "integration", "files": ["CHANGELOG.md"], "depends_on": ["a", "b"]}
        )
        code, _, err = self.check(plan(entries))
        self.assertEqual(code, 0, err)
        # One unit alone may list a hub file: nothing runs beside it.
        code, _, err = self.check(plan([{"name": "solo", "files": ["CHANGELOG.md"]}]))
        self.assertEqual(code, 0, err)

    def test_units_check_project_hub_paths(self):
        problems = units_mod.validate(
            units_mod.parse(plan([{"name": "a", "files": ["docs/ja/x.md"]}, {"name": "b", "files": ["y"]}])),
            ["docs/ja/**"],
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("hub file", problems[0])

    def test_units_validate_reads_directories_from_the_known_files(self):
        found = units_mod.parse(
            plan([{"name": "a", "files": ["docs/*.md"]}, {"name": "b", "files": ["docs/guides"]}])
        )
        # By text alone they look apart, as they did before.
        self.assertEqual(units_mod.validate(found, []), [])
        self.assertEqual(
            units_mod.validate(found, [], known_files=["docs/guides/intro.txt", "docs/x.md"]), []
        )
        problems = units_mod.validate(found, [], known_files=["docs/guides/intro.md"])
        self.assertEqual(len(problems), 1)
        self.assertIn("a and b may run in parallel", problems[0])

    def test_units_validate_refuses_a_directory_holding_a_hub_file(self):
        known = ["CHANGELOG.md", "packages/core/CHANGELOG.md", "packages/core/x.py", "y.py"]
        hubs = ["CHANGELOG.md", "**/CHANGELOG.md"]
        for listed, held in ((".", "CHANGELOG.md"), ("packages/core", "packages/core/CHANGELOG.md")):
            with self.subTest(listed=listed):
                found = units_mod.parse(
                    plan([{"name": "a", "files": [listed]}, {"name": "b", "files": ["other/z.py"]}])
                )
                self.assertEqual(units_mod.validate(found, hubs), [])
                problems = units_mod.validate(found, hubs, known_files=known)
                self.assertEqual(len(problems), 1)
                self.assertIn("a: %s holds hub file %s" % (listed, held), problems[0])
        found = units_mod.parse(
            plan([{"name": "a", "files": ["y.py"]}, {"name": "b", "files": ["other/z.py"]}])
        )
        self.assertEqual(units_mod.validate(found, hubs, known_files=known), [])

    @unittest.skipUnless(has_git(), "needs git")
    def test_units_check_lists_the_repository_files(self):
        self.init_git_repo()
        self.write("docs/guides/intro.md", "intro\n")
        self.git("add", "docs/guides/intro.md")
        self.write("pkg/CHANGELOG.md", "untracked\n")
        code, _, err = self.check(
            plan([{"name": "a", "files": ["docs/*.md"]}, {"name": "b", "files": ["docs/guides"]}])
        )
        self.assertEqual(code, 2)
        self.assertIn("a and b may run in parallel", err)
        code, _, err = self.check(plan([{"name": "a", "files": ["pkg"]}, {"name": "b", "files": ["y.py"]}]))
        self.assertEqual(code, 2)
        self.assertIn("a: pkg holds hub file pkg/CHANGELOG.md", err)
        known = units_mod.known_files(self.project) or []
        self.assertIn("docs/guides/intro.md", known)
        self.assertIn("pkg/CHANGELOG.md", known)

    def test_units_check_warns_on_unlisted_file(self):
        code, _, err = self.check(plan([{"name": "a", "files": ["scripts/other.py"]}]))
        self.assertEqual(code, 0, err)
        self.assertIn("warning: a: scripts/other.py is not in Files to Modify", err)


class TestSchedule(UnitsCase):
    def test_units_schedule_waves(self):
        found = units_mod.parse(
            plan(
                [
                    {"name": "a", "files": ["x"]},
                    {"name": "b", "files": ["y"]},
                    {"name": "c", "files": ["z"]},
                    {"name": "d", "files": ["w"], "depends_on": ["a"]},
                    {"name": "e", "kind": "integration", "files": ["v"], "depends_on": ["b", "c", "d"]},
                ]
            )
        )
        self.assertEqual(units_mod.schedule(found, 2), [["a", "b"], ["c", "d"], ["e"]])
        self.assertEqual(units_mod.schedule(found, 1), [["a"], ["b"], ["c"], ["d"], ["e"]])
        self.assertEqual(units_mod.schedule(found, 8), [["a", "b", "c"], ["d"], ["e"]])

    def test_units_check_prints_the_waves_by_max_parallel(self):
        self.write(".dev-orchestra.yaml", "version: 1\nimplementation:\n  max_parallel: 1\n")
        code, out, err = self.check(plan([{"name": "a", "files": ["x"]}, {"name": "b", "files": ["y"]}]))
        self.assertEqual(code, 0, err)
        self.assertIn("wave 1: a", out)
        self.assertIn("wave 2: b", out)


class TestInPlace(UnitsCase):
    def units(self):
        return units_mod.parse(
            plan(
                [
                    {"name": "a", "files": ["x"]},
                    {"name": "b", "files": ["y"], "depends_on": ["a"]},
                    {"name": "c", "files": ["z"]},
                ]
            )
        )

    def test_units_in_place_only_when_nothing_could_run_beside(self):
        found = self.units()
        state: Dict[str, Any] = {"units": {}}
        # c does not depend on a, so a could run beside it.
        self.assertFalse(units_mod.runs_in_place(found, state, "a"))
        state = {"units": {"c": {"status": "merged"}}}
        self.assertTrue(units_mod.runs_in_place(found, state, "a"))
        state = {"units": {"c": {"status": "running"}}}
        self.assertFalse(units_mod.runs_in_place(found, state, "a"))
        state = {"units": {"a": {"status": "merged"}, "c": {"status": "merged"}}}
        self.assertTrue(units_mod.runs_in_place(found, state, "b"))


class TestStatus(UnitsCase):
    def test_units_status_reports_ready_and_records(self):
        text = plan([{"name": "a", "files": ["x"]}, {"name": "b", "files": ["y"], "depends_on": ["a"]}])
        self.write_plan(text)
        with units_mod.locked(self.workspace):
            state = units_mod.load_state(self.workspace)
            state["plan_sha256"] = units_mod.pass_key(self.workspace, "implementer")
            state["units"]["a"] = {"status": "failed", "job": "job-1"}
            units_mod.save_state(self.workspace, state)
        code, out, err = run_cli("units", "status", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual([row["status"] for row in data["units"]], ["failed", "pending"])
        self.assertEqual(data["ready"], [])
        self.assertTrue(data["failed"])
        code, out, _ = run_cli("units", "status")
        self.assertIn("The implement stage failed", out)

    def test_units_status_ignores_records_of_another_plan(self):
        self.write_plan(plan([{"name": "a", "files": ["x"]}]))
        ws.write_json(self.workspace.units_path, {"plan_sha256": "old", "units": {"a": {"status": "merged"}}})
        code, out, err = run_cli("units", "status", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["ready"], ["a"])


class TestPassKey(UnitsCase):
    def test_units_pass_key(self):
        self.assertEqual(units_mod.pass_key(self.workspace, "architect"), "")
        self.write_plan("# Plan\n")
        key = units_mod.pass_key(self.workspace, "implementer")
        self.assertEqual(len(key), 64)
        state = self.workspace.read_state()
        state["design_approval"] = {"sha256": key}
        self.workspace.write_state(state)
        self.assertEqual(units_mod.pass_key(self.workspace, "architect"), key)


class TestJoin(UnitsCase):
    def architect_used(self) -> int:
        return json.loads(run_cli("budget", "show", "--json")[1])["budgets"]["architect"]["used"]

    def test_units_join_moves_headings_down_as_the_plan_review_reads_them(self):
        part = plan([{"name": "parser", "files": ["scripts/a.py"]}])
        part += "\n   ## Notes ##\nText.\n~~~\n## x\n~~~\n"
        joined = units_mod.join([("parser", part)])
        # "# Plan" goes to level 4, so every heading moves down three.
        self.assertIn("\n   ##### Notes ##\n", joined)
        self.assertIn("\n~~~\n## x\n~~~\n", joined)
        self.assertIn("Text.", units_mod.section(joined, "parser"))

    def test_units_join_builds_plan(self):
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "architect.provider", "mock")
        run_cli("config", "set", "budgets.architect", "5")
        # One design pass: the units' architects take one attempt between them, a rerun another.
        for name in ("parser", "docs", "parser"):
            code, _, err = run_cli("run", "architect", "--unit", name, "--prompt", "go")
            self.assertEqual(code, 0, err)
        self.assertEqual(self.architect_used(), 2)

        parts = [
            "parser=" + self.write("parts/parser.md", plan([{"name": "parser", "files": ["scripts/a.py"]}])),
            "docs="
            + self.write("parts/docs.md", plan([{"name": "docs", "kind": "docs", "files": ["docs/a.md"]}])),
        ]
        code, out, err = run_cli(
            "units", "join", "--part", parts[0], "--part", parts[1], "--output", self.workspace.plan_path
        )
        self.assertEqual(code, 0, err)
        self.assertIn("joined 2 unit plans", out)
        joined = ws.read_text(self.workspace.plan_path)
        self.assertEqual([unit.name for unit in units_mod.parse(joined)], ["parser", "docs"])
        self.assertIn("Do parser.", units_mod.section(joined, "parser"))
        self.assertIn("Do docs.", units_mod.section(joined, "docs"))
        self.assertNotIn("Do docs.", units_mod.section(joined, "parser"))
        code, out, err = run_cli("units", "check")
        self.assertEqual(code, 0, err)
        self.assertIn("2 units in 1 waves", out)

        # An approval is a new design pass: its first piece spends an attempt.
        code, _, err = run_cli("design", "approve")
        self.assertEqual(code, 0, err)
        code, _, err = run_cli("run", "architect", "--unit", "parser", "--prompt", "go")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.architect_used(), 3)

        # Parts whose units cannot run side by side are refused, and nothing is written.
        clash = "clash=" + self.write("parts/clash.md", plan([{"name": "clash", "files": ["scripts/a.py"]}]))
        output = os.path.join(self.project, "clash-plan.md")
        code, _, err = run_cli("units", "join", "--part", parts[0], "--part", clash, "--output", output)
        self.assertEqual(code, 2)
        self.assertIn("may run in parallel", err)
        self.assertFalse(os.path.exists(output))


if __name__ == "__main__":
    unittest.main()
