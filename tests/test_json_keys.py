"""The top-level keys of every command's ``--json`` output (#242).

The compatibility promise lets a key be added but never removed or renamed,
so each command is held to the keys it prints today as a subset: a new key
passes, a missing one fails. The commands are the ones cli.md lists between
its ``json-commands`` markers, and a command added there without keys here
fails too. references/json.md documents each command's keys, and is held to
the same list: every key here is in its table, and every key its table says
is always printed is here.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import unittest
from typing import Any, Dict, List, Tuple

from helpers import REPO_ROOT, IsolatedCase, has_git, run_cli
from test_units import plan

from orchestrator import approval as approval_mod
from orchestrator import jobs as jobs_mod
from orchestrator import units as units_mod
from orchestrator import workspace as ws
from orchestrator import worktrees as worktrees_mod

FINDING = """## Finding
- Severity: high
- File: app.py
- Line: 2
- Category: correctness
- Problem: subtraction was used where addition was intended
- Impact: every caller gets the wrong total
- Evidence: return a - b
- Recommended fix: restore the addition
"""

JOB_ID = "implementer-1"

# Command -> (the arguments after it, the keys its object holds). For a
# command that prints a list or a mapping keyed by name, the keys are those
# of each entry, as LISTS and MAPPINGS below say.
KEYS: Dict[str, Tuple[List[str], List[str]]] = {
    "config show": ([], ["config", "global", "preset", "project", "providers", "reviewer_origins", "source"]),
    "config validate": ([], ["problems", "valid", "warnings"]),
    "config suggest-roles": (
        [],
        ["files", "notes", "root", "skipped", "source", "suggestions", "truncated", "written"],
    ),
    "model list": (
        [],
        ["adapter_error", "families", "fallback_updated", "installed", "models", "origin", "version"],
    ),
    "reviewer list": ([], ["id", "model", "origin", "provider", "role"]),
    "doctor": (
        ["--fast"],
        [
            "antigravity",
            "config",
            "design_panel_source",
            "design_reviewers",
            "language",
            "notes",
            "platform",
            "problems",
            "providers",
            "reviewers",
            "roles",
            "user_providers",
        ],
    ),
    "hooks status": (
        [],
        [
            "error",
            "events",
            "hooks_disabled",
            "plugin_root",
            "python",
            "reasons",
            "relay",
            "settings_path",
            "status",
        ],
    ),
    "run": (
        ["implementer", "--prompt", "go"],
        [
            "answered",
            "duration_seconds",
            "exit_code",
            "model",
            "output",
            "session_id",
            "stage",
            "stalled",
            "status",
            "timed_out",
        ],
    ),
    "review snapshot": (
        [],
        [
            "base",
            "base_commit",
            "bytes",
            "change_chars",
            "changed_paths",
            "condition_paths",
            "empty",
            "exclude_patterns",
            "files",
            "full_diff",
            "generated_at",
            "head",
            "incremental_from",
            "lines_added",
            "lines_deleted",
            "max_chars",
            "over_context",
            "round_id",
            "sha256",
            "strategy",
            "tree",
            "untracked_included",
            "withheld",
        ],
    ),
    "review run": ([], ["counts", "failed", "ok", "optimization", "partial", "reviewers"]),
    "review consolidate": (
        [],
        [
            "counts",
            "coverage",
            "duplicate_candidates",
            "findings",
            "generated_at",
            "iteration",
            "lineage",
            "reviewers",
            "snapshot",
        ],
    ),
    "review show": (
        [],
        [
            "counts",
            "coverage",
            "duplicate_candidates",
            "findings",
            "generated_at",
            "iteration",
            "lineage",
            "reviewers",
            "snapshot",
        ],
    ),
    "review status": (
        [],
        [
            "accepted_count",
            "blocking",
            "blocking_count",
            "counts",
            "coverage",
            "final_fix",
            "final_fix_pending",
            "iteration",
            "iteration_budget_exhausted",
            "max_review_iterations",
            "over_budget",
            "re_review_recommended",
            "reviewers_partial",
        ],
    ),
    "design approve": (
        [],
        [
            "already_approved",
            "approved_at",
            "decisions_sha256",
            "design_round",
            "open_findings",
            "open_findings_of_current_plan",
            "plan",
            "sha256",
            "workflow",
        ],
    ),
    "design questions show": (
        [],
        ["decisions_file", "mode", "pending", "questions", "rounds", "state", "workflow"],
    ),
    "state show": ([], ["events", "ledger", "runs", "updated_at", "version"]),
    "jobs list": ([], ["id", "output_file", "stage", "started_at", "status"]),
    "jobs show": ([JOB_ID], ["id", "output_file", "stage", "started_at", "status"]),
    "jobs wait": ([JOB_ID], ["id", "output_file", "stage", "started_at", "status"]),
    "budget show": (
        [],
        [
            "budgets",
            "in_flight",
            "passes",
            "runtime",
            "runtime_remaining_seconds",
            "signatures",
            "stalls",
            "started_at",
            "tokens",
            "total_delegated_runs",
        ],
    ),
    "tokens show": ([], ["by_label", "by_stage", "complete", "priced", "totals"]),
    "optimization report": (
        [],
        [
            "always_escalated",
            "architect_revisions",
            "billed_per_round",
            "billed_tokens",
            "by_context",
            "conditional",
            "design_billed_per_round",
            "design_billed_tokens",
            "design_measured_runs",
            "design_refused",
            "design_relevance",
            "design_reviewer_runs",
            "design_rounds",
            "design_tool_output_chars",
            "design_tool_output_chars_per_run",
            "design_tool_output_reported_runs",
            "design_tool_reported_runs",
            "design_tool_uses",
            "design_tool_uses_per_run",
            "escalated",
            "escalation_patterns",
            "estimated_saving",
            "gates",
            "levels",
            "measured_runs",
            "paired",
            "panel_reduced",
            "ran",
            "refused",
            "refused_by",
            "relevance",
            "reviewer_runs",
            "rounds",
            "rounds_without_a_test_result",
            "scorecard",
            "tool_output_chars",
            "tool_output_chars_per_run",
            "tool_output_reported_runs",
            "tool_reported_runs",
            "tool_uses",
            "tool_uses_per_run",
        ],
    ),
    "progress record": (["test", "--signature", "1 failed"], ["repeats", "stage", "stop"]),
    "workflow list": ([], ["current", "workflows"]),
    "workflow show": ([], ["also_active", "container", "dir", "origin", "root", "workflow"]),
    "status": (
        [],
        [
            "abandoned_stages",
            "also_active",
            "budgets",
            "design_approval",
            "design_questions",
            "design_review",
            "in_flight",
            "optimization",
            "progress",
            "reasons",
            "review",
            "runtime",
            "runtime_remaining_seconds",
            "stalls",
            "tokens",
            "total_delegated_runs",
            "verdict",
            "workflow",
        ],
    ),
    "summary": ([], ["counts", "design_counts", "models", "reviewers", "skipped", "stages", "tokens"]),
    # Last: the plan with units they need replaces the approved one.
    "units status": ([], ["failed", "plan_sha256", "ready", "units"]),
    "units merge": (["one"], ["files", "outside_files", "status", "unit"]),
}

# A list of entries, or a mapping of name to entry: the keys are each entry's.
LISTS = ("reviewer list", "jobs list")
MAPPINGS = ("model list",)

JSON_COMMANDS = re.compile(r"<!-- json-commands: start -->(.*?)<!-- json-commands: end -->", re.DOTALL)


def documented() -> List[str]:
    text = (pathlib.Path(REPO_ROOT) / "references" / "cli.md").read_text(encoding="utf-8")
    match = JSON_COMMANDS.search(text)
    assert match is not None
    return re.findall(r"`([a-z -]+)`", match.group(1))


SECTION = re.compile(r"^### `([a-z -]+)`\n(.*?)(?=^#{2,3} |\Z)", re.MULTILINE | re.DOTALL)
ROW = re.compile(r"^\| `(\w+)` \| [^|]* \| (.*) \|$", re.MULTILINE)


def documented_shapes() -> Dict[str, Tuple[str, Dict[str, bool]]]:
    """json.md: command -> (the first word under its heading, key -> always printed).

    A key whose meaning starts with "Only" is printed only then.
    """
    text = (pathlib.Path(REPO_ROOT) / "references" / "json.md").read_text(encoding="utf-8")
    shapes: Dict[str, Tuple[str, Dict[str, bool]]] = {}
    for name, body in SECTION.findall(text):
        rows = {key: not meaning.startswith("Only") for key, meaning in ROW.findall(body)}
        shapes[name] = (body.split(None, 1)[0].rstrip(".,:"), rows)
    return shapes


class TestTheListIsCovered(unittest.TestCase):
    def test_every_documented_json_command_has_its_keys_here(self):
        self.assertEqual(sorted(documented()), sorted(KEYS))

    def test_the_json_reference_has_the_keys_here(self):
        shapes = documented_shapes()
        self.assertEqual(sorted(shapes), sorted(KEYS))
        for name, (_, keys) in KEYS.items():
            shape, rows = shapes[name]
            with self.subTest(command=name):
                expected = "List" if name in LISTS else "Mapping" if name in MAPPINGS else "Object"
                self.assertEqual(shape, expected)
                self.assertEqual(sorted(set(keys) - set(rows)), [], "printed, not in references/json.md")
                always = sorted(key for key, present in rows.items() if present)
                self.assertEqual(
                    sorted(set(always) - set(keys)), [], "documented as always printed, not here"
                )


@unittest.skipUnless(has_git(), "git is required for review snapshot")
class TestJsonTopLevelKeys(IsolatedCase):
    """One cheap state in which every command has something to print."""

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
        for reviewer in ("claude-general", "codex-general", "claude-security", "claude-test"):
            run_cli("reviewer", "remove", reviewer)
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        run_cli("config", "set", "implementer.provider", "mock")
        workspace = self.cli_workspace()
        job = {
            "id": JOB_ID,
            "stage": "implementer",
            "status": "succeeded",
            "started_at": ws.utcnow(),
            "output_file": jobs_mod.output_path(workspace, JOB_ID),
        }
        jobs_mod.write_job(workspace, job)
        self.plan_path = workspace.plan_path

    def add_finished_unit(self, result: str = "") -> None:
        """A plan with unit ``one``, succeeded, for `units merge`.

        Its result changed nothing, or with ``result`` wrote that into
        ``app.py``; the working tree is left as it was.
        """
        workspace = self.cli_workspace()
        text = plan([{"name": "one", "files": ["app.py"]}])
        ws.write_text(self.plan_path, text)
        with units_mod.locked(workspace):
            state = units_mod.load_state(workspace)
            state["plan_sha256"] = approval_mod.plan_digest(text)
            units_mod.save_state(workspace, state)
        excludes = worktrees_mod.excludes(workspace)
        tree = ws.write_tree(self.project, excludes)
        changed = tree
        if result:
            before = ws.read_text(os.path.join(self.project, "app.py"))
            self.write("app.py", result)
            changed = ws.write_tree(self.project, excludes)
            self.write("app.py", before)
        self.assertTrue(tree and changed)
        record = {"status": units_mod.SUCCEEDED, "base_tree": tree, "result_tree": changed, "links": []}
        units_mod.update(workspace, "one", record)

    def entries(self, name: str, payload: Any) -> List[Any]:
        if name in LISTS:
            self.assertIsInstance(payload, list)
            return payload
        if name in MAPPINGS:
            self.assertIsInstance(payload, dict)
            return list(payload.values())
        return [payload]

    def test_each_command_keeps_its_keys(self):
        # In this order: review run reads the snapshot, consolidate the run.
        for name, (arguments, keys) in KEYS.items():
            if name == "design approve":
                # Only now: a plan waiting for approval would stop `run`.
                ws.write_text(self.plan_path, "# Plan\n")
            if name == "units status":
                self.add_finished_unit()
            with self.subTest(command=name):
                code, out, err = run_cli(*name.split(), *arguments, "--json")
                self.assertEqual(code, 0, err)
                entries = self.entries(name, json.loads(out))
                self.assertTrue(entries, "nothing to check")
                for entry in entries:
                    self.assertIsInstance(entry, dict)
                    missing = sorted(set(keys) - set(entry))
                    self.assertEqual(missing, [], "%s --json lost %s" % (name, ", ".join(missing)))

    def test_units_merge_conflicted_and_resolved_keep_their_keys(self):
        self.add_finished_unit(result="unit\n")
        self.write("app.py", "main\n")
        code, out, err = run_cli("units", "merge", "one", "--json")
        self.assertEqual(code, units_mod.EXIT_CONFLICTS, err)
        payload = json.loads(out)
        self.assertEqual(sorted({"brief", "conflicts", "outside_files", "status", "unit"} - set(payload)), [])
        self.assertEqual((payload["status"], payload["conflicts"]), (units_mod.CONFLICTED, ["app.py"]))
        self.write("app.py", "resolved\n")
        code, out, err = run_cli("units", "merge", "one", "--resolved", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(sorted({"resolved_files", "status", "unit"} - set(payload)), [])
        self.assertEqual(payload["resolved_files"], ["app.py"])

    def test_jobs_wait_with_several_ids_lists_them(self):
        workspace = self.cli_workspace()
        other = JOB_ID + "-2"
        jobs_mod.write_job(
            workspace,
            {
                "id": other,
                "stage": "implementer",
                "status": "succeeded",
                "started_at": ws.utcnow(),
                "output_file": jobs_mod.output_path(workspace, other),
            },
        )
        code, out, err = run_cli("jobs", "wait", JOB_ID, other, "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(sorted(payload), ["jobs"])
        self.assertEqual([job["id"] for job in payload["jobs"]], [JOB_ID, other])
        for job in payload["jobs"]:
            missing = sorted(set(KEYS["jobs wait"][1]) - set(job))
            self.assertEqual(missing, [], "jobs wait --json lost %s" % ", ".join(missing))

    def test_model_list_names_every_built_in_provider(self):
        payload = json.loads(run_cli("model", "list", "--json")[1])
        for name in ("agy", "claude", "codex", "mock"):
            with self.subTest(provider=name):
                self.assertIn(name, payload)


if __name__ == "__main__":
    unittest.main()
