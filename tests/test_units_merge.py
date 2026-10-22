"""``units merge``: a unit's result brought into the working tree, file by file."""

from __future__ import annotations

import json
import os
import unittest
from typing import Any, Dict, List, Optional, Union
from unittest import mock

from helpers import IsolatedCase, has_git, run_cli
from test_units import plan

from orchestrator import approval as approval_mod
from orchestrator import review_round_context, review_snapshot, units_merge
from orchestrator import units as units_mod
from orchestrator import workspace as ws
from orchestrator import worktrees as worktrees_mod

A_PY = "a1\na2\na3\na4\na5\na6\na7\n"


@unittest.skipUnless(has_git(), "git not available")
class MergeCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("a.py", A_PY)
        self.write("b.py", "b1\n")
        self.write_bytes("image.bin", b"\0base")
        self.commit_all("base")
        self.workspace = self.cli_workspace()
        self.head = self.git("rev-parse", "HEAD").stdout
        self.set_plan(
            [
                {"name": "one", "files": ["a.py"]},
                {"name": "two", "files": ["b.py", "image.bin"]},
            ]
        )

    def write_bytes(self, relative: str, content: bytes) -> None:
        with open(os.path.join(self.project, relative), "wb") as handle:
            handle.write(content)

    def read(self, relative: str) -> Optional[bytes]:
        path = os.path.join(self.project, relative)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as handle:
            return handle.read()

    def put(self, relative: str, content: Optional[Union[str, bytes]]) -> None:
        if content is None:
            os.unlink(os.path.join(self.project, relative))
        elif isinstance(content, bytes):
            self.write_bytes(relative, content)
        else:
            self.write(relative, content)

    def set_plan(self, entries: List[Dict[str, Any]]) -> None:
        text = plan(entries)
        ws.write_text(self.workspace.plan_path, text)
        with units_mod.locked(self.workspace):
            state = units_mod.load_state(self.workspace)
            state["plan_sha256"] = approval_mod.plan_digest(text)
            units_mod.save_state(self.workspace, state)

    def unit(
        self, name: str, changes: Dict[str, Optional[Union[str, bytes]]], status: str = units_mod.SUCCEEDED
    ) -> None:
        """Record unit ``name`` as having made ``changes`` to the tree as it is; the tree is put back."""
        excludes = worktrees_mod.excludes(self.workspace)
        base = ws.write_tree(self.project, excludes)
        saved = {path: self.read(path) for path in changes}
        for path, content in changes.items():
            self.put(path, content)
        result = ws.write_tree(self.project, excludes)
        for path, content in saved.items():
            self.put(path, content)
        self.assertTrue(base and result)
        units_mod.update(
            self.workspace, name, {"status": status, "base_tree": base, "result_tree": result, "links": []}
        )

    def record(self, name: str) -> Dict[str, Any]:
        return units_mod.load_state(self.workspace)["units"][name]

    def assert_index_and_head_untouched(self) -> None:
        self.assertEqual(self.git("diff", "--cached", "--name-only").stdout, "")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, self.head)
        self.assertEqual(self.git("branch", "--list").stdout.count("\n"), 1)


class TestMerge(MergeCase):
    def test_units_merge_clean_disjoint(self):
        self.unit("one", {"a.py": A_PY.replace("a1", "one1")})
        self.unit("two", {"b.py": "b1\ntwo\n"})
        code, out, err = run_cli("units", "merge", "one")
        self.assertEqual(code, 0, err)
        self.assertIn("merged one", out)
        code, _, err = run_cli("units", "merge", "two")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read("a.py"), A_PY.replace("a1", "one1").encode())
        self.assertEqual(self.read("b.py"), b"b1\ntwo\n")
        for name in ("one", "two"):
            self.assertEqual(self.record(name)["status"], units_mod.MERGED)
            self.assertEqual(self.record(name)["outside_files"], [])
        self.assert_index_and_head_untouched()
        events = [e for e in self.workspace.read_state().get("events") or [] if e.get("stage") == "units"]
        self.assertEqual([(e["status"], e["unit"]) for e in events], [("merged", "one"), ("merged", "two")])

    def test_units_merge_same_file_non_overlapping_hunks(self):
        self.unit("one", {"a.py": A_PY.replace("a7", "unit7")})
        self.write("a.py", A_PY.replace("a1", "main1"))
        code, _, err = run_cli("units", "merge", "one")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read("a.py"), A_PY.replace("a1", "main1").replace("a7", "unit7").encode())
        self.assert_index_and_head_untouched()

    def test_units_merge_conflict_writes_brief_exit_7(self):
        self.unit("one", {"a.py": A_PY.replace("a1", "one1")})
        self.unit("two", {"a.py": A_PY.replace("a1", "two1")})
        self.assertEqual(run_cli("units", "merge", "one")[0], 0)
        code, out, err = run_cli("units", "merge", "two")
        self.assertEqual(code, units_mod.EXIT_CONFLICTS, err)
        self.assertEqual(code, 7)
        content = (self.read("a.py") or b"").decode()
        self.assertIn("<<<<<<< main\n", content)
        self.assertIn(">>>>>>> unit:two\n", content)
        record = self.record("two")
        self.assertEqual(record["status"], units_mod.CONFLICTED)
        self.assertEqual(record["conflicts"], ["a.py"])
        brief = ws.read_text(os.path.join(self.workspace.units_dir, "two-conflicts.md"))
        self.assertIn("`a.py`", brief)
        self.assertIn("### Unit: two", brief)
        self.assertIn("### Unit: one", brief)
        self.assertIn("Something.", brief)  # the plan's goal
        self.assertIn("two-conflicts.md", out)
        self.assert_index_and_head_untouched()
        # Merging again does not merge over the markers.
        self.assertEqual(run_cli("units", "merge", "two")[0], 7)

    def test_units_merge_resolved_refuses_remaining_markers(self):
        self.unit("one", {"a.py": A_PY.replace("a1", "one1")})
        self.unit("two", {"a.py": A_PY.replace("a1", "two1")})
        run_cli("units", "merge", "one")
        self.assertEqual(run_cli("units", "merge", "two")[0], 7)
        code, _, err = run_cli("units", "merge", "two", "--resolved")
        self.assertEqual(code, 2)
        self.assertIn("a.py", err)
        self.assertEqual(self.record("two")["status"], units_mod.CONFLICTED)
        self.write("a.py", A_PY.replace("a1", "one1 two1"))
        code, _, err = run_cli("units", "merge", "two", "--resolved")
        self.assertEqual(code, 0, err)
        record = self.record("two")
        self.assertEqual(record["status"], units_mod.MERGED)
        self.assertEqual(record["resolved_files"], ["a.py"])
        events = [e for e in self.workspace.read_state().get("events") or [] if e.get("stage") == "units"]
        self.assertEqual(events[-1]["status"], "resolved")

    def test_units_merge_binary_conflict_keeps_main_saves_theirs(self):
        self.unit("two", {"image.bin": b"\0unit"})
        self.write_bytes("image.bin", b"\0main")
        code, _, err = run_cli("units", "merge", "two")
        self.assertEqual(code, 7, err)
        self.assertEqual(self.read("image.bin"), b"\0main")
        theirs = os.path.join(self.workspace.units_dir, "two", "theirs", "image.bin")
        with open(theirs, "rb") as handle:
            self.assertEqual(handle.read(), b"\0unit")
        brief = ws.read_text(os.path.join(self.workspace.units_dir, "two-conflicts.md"))
        self.assertIn("a binary file", brief)
        self.assertIn("theirs/image.bin", brief)

    def test_units_merge_refuses_failed_unit(self):
        self.unit("one", {"a.py": A_PY.replace("a1", "one1")}, status=units_mod.FAILED)
        code, _, err = run_cli("units", "merge", "one")
        self.assertEqual(code, 2)
        self.assertIn("failed", err)
        self.assertEqual(self.read("a.py"), A_PY.encode())
        self.assertEqual(self.record("one")["status"], units_mod.FAILED)

    def test_units_merge_in_place_unit_noop(self):
        units_mod.update(self.workspace, "one", {"status": units_mod.MERGED, "in_place": True})
        before = self.git("status", "--porcelain").stdout
        code, out, err = run_cli("units", "merge", "one")
        self.assertEqual(code, 0, err)
        self.assertIn("already merged", out)
        self.assertEqual(self.git("status", "--porcelain").stdout, before)
        self.assertEqual(self.read("a.py"), A_PY.encode())

    def test_units_merge_records_outside_files(self):
        self.unit("one", {"a.py": A_PY.replace("a1", "one1"), "c.py": "new\n"})
        code, out, err = run_cli("units", "merge", "one", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["outside_files"], ["c.py"])
        self.assertEqual(self.record("one")["outside_files"], ["c.py"])
        self.assertEqual(self.read("c.py"), b"new\n")

    def test_units_merge_atomic_on_error(self):
        self.unit("one", {"a.py": A_PY.replace("a1", "one1"), "b.py": "b1\nmore\n"})
        original = units_merge._blob

        def failing(root: str, path: str, blob: str, checkout: bool = False) -> bytes:
            if path == "b.py":
                raise units_merge.MergeError("cat-file failed")
            return original(root, path, blob, checkout)

        with mock.patch.object(units_merge, "_blob", failing):
            code, _, err = run_cli("units", "merge", "one")
        self.assertEqual(code, 1)
        self.assertIn("cat-file failed", err)
        self.assertEqual(self.read("a.py"), A_PY.encode())
        self.assertEqual(self.read("b.py"), b"b1\n")
        self.assertEqual(self.record("one")["status"], units_mod.SUCCEEDED)


class TestRoundContext(MergeCase):
    def test_round_context_names_resolved_files_first_round(self):
        self.unit("one", {"a.py": A_PY.replace("a1", "one1")})
        units_mod.update(self.workspace, "one", {"resolved_files": ["a.py"], "outside_files": ["c.py"]})
        self.write("a.py", A_PY.replace("a1", "one1"))
        meta = review_snapshot.create_snapshot(self.workspace)
        self.assertFalse(meta.get("incremental_from"))
        self.assertEqual(meta["units"], {"one": {"resolved_files": ["a.py"], "outside_files": ["c.py"]}})
        text = review_round_context.render_round_context(self.workspace, meta)
        self.assertIn("work units", text)
        self.assertIn("merge conflicts between units in:\n- a.py (unit one)", text)
        self.assertIn("did not list:\n- c.py (unit one)", text)

    def test_round_context_unchanged_without_units(self):
        self.write("a.py", A_PY.replace("a1", "main1"))
        meta = review_snapshot.create_snapshot(self.workspace)
        self.assertNotIn("units", meta)
        self.assertEqual(review_round_context.render_round_context(self.workspace, meta), "")
        incremental = {"incremental_from": "t" * 40, "full_diff": ".ai/full.diff"}
        self.assertEqual(
            review_round_context.render_round_context(self.workspace, incremental),
            "Re-review. The diff above is the fix only, not the whole change.\n"
            "Whole change frozen at .ai/full.diff -- read it if you need the context.",
        )
        self.assertEqual(
            review_round_context.render_round_context(self.workspace, {**incremental, "units": {}}),
            review_round_context.render_round_context(self.workspace, incremental),
        )


if __name__ == "__main__":
    unittest.main()
