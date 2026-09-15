"""One directory per workflow, and which workflow a command belongs to.

`.ai/` used to be one directory per project. Two sessions working in the same
checkout therefore shared `plan.md`, the review reports, the budgets and the
round counter -- and neither announced itself, so the first session's plan was
simply overwritten and its budget spent by the other one.

Two halves to the fix, and both are tested here: artifacts move to
`.ai/workflows/<id>/`, and the id is resolved the same way from every command
in one session. The third thing this must not do is *imply* that parallel work
is now safe: the working tree is still shared, so `active_elsewhere` has to
keep saying so.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git

from orchestrator import cli
from orchestrator import workflow as wf
from orchestrator import workspace as ws


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestTheId(unittest.TestCase):
    def test_a_session_always_resolves_to_the_same_workflow(self):
        """The reason a session id is preferred over the pointer file: it needs
        no agreement between processes, so two sessions cannot race for it."""
        self.assertEqual(wf.from_session("session-a"), wf.from_session("session-a"))

    def test_different_sessions_are_different_workflows(self):
        self.assertNotEqual(wf.from_session("session-a"), wf.from_session("session-b"))

    def test_the_session_itself_is_not_the_directory_name(self):
        """Another tool's internal identifier does not belong in our paths."""
        self.assertNotIn("session-a", wf.from_session("session-a"))

    def test_an_id_that_could_escape_the_container_is_refused(self):
        for value in ("..", "../x", "a/b", "a\\b", "", "   ", ".hidden"):
            with self.assertRaises(wf.WorkflowError):
                wf.normalise(value)

    def test_a_readable_id_is_allowed(self):
        """`--workflow auth-fix` is the point of accepting names at all."""
        self.assertEqual(wf.normalise("auth-fix"), "auth-fix")

    def test_new_ids_differ(self):
        self.assertNotEqual(wf.new_id(), wf.new_id())


class TestResolution(IsolatedCase):
    def container(self):
        return os.path.join(self.project, ".ai")

    def test_an_explicit_id_wins_over_everything(self):
        env = {wf.WORKFLOW_ENV: "from-env", "CLAUDE_CODE_SESSION_ID": "s"}
        self.assertEqual(wf.resolve(self.container(), "asked", env), ("asked", "requested"))

    def test_the_environment_wins_over_the_session(self):
        env = {wf.WORKFLOW_ENV: "from-env", "CLAUDE_CODE_SESSION_ID": "s"}
        self.assertEqual(wf.resolve(self.container(), "", env), ("from-env", "environment"))

    def test_the_session_wins_over_the_pointer(self):
        """Two sessions in one directory must not both answer "the pointer"."""
        wf.write_pointer(self.container(), "remembered")
        workflow, origin = wf.resolve(self.container(), "", {"CLAUDE_CODE_SESSION_ID": "s"})
        self.assertEqual(origin, "session")
        self.assertNotEqual(workflow, "remembered")

    def test_the_pointer_answers_when_the_host_exports_no_session(self):
        """Codex exports none, so this is the ordinary path there."""
        wf.write_pointer(self.container(), "remembered")
        self.assertEqual(wf.resolve(self.container(), "", {}), ("remembered", "pointer"))

    def test_a_first_run_with_nothing_to_go_on_gets_a_new_id(self):
        workflow, origin = wf.resolve(self.container(), "", {})
        self.assertEqual(origin, "new")
        self.assertTrue(workflow)

    def test_ensure_records_what_it_resolved(self):
        workflow = wf.ensure(self.container(), "", {"CLAUDE_CODE_SESSION_ID": "s"})
        self.assertEqual(wf.read_pointer(self.container()), workflow)

    def test_a_corrupt_pointer_is_ignored_rather_than_obeyed(self):
        ws.write_json(wf.pointer_path(self.container()), {"workflow": "../escape"})
        self.assertEqual(wf.read_pointer(self.container()), "")


class TestTheLayout(IsolatedCase):
    def test_a_workflow_gets_its_own_directory(self):
        workspace = ws.Workspace(self.project, workflow="w1").ensure()
        self.assertTrue(workspace.dir.endswith(os.path.join("workflows", "w1")))
        self.assertTrue(os.path.isdir(workspace.reviews_dir))

    def test_two_workflows_do_not_share_a_plan(self):
        one = ws.Workspace(self.project, workflow="w1").ensure()
        two = ws.Workspace(self.project, workflow="w2").ensure()
        self.assertNotEqual(one.plan_path, two.plan_path)

    def test_the_ignore_file_covers_the_whole_container(self):
        """One file the project has already seen, not one per workflow."""
        workspace = ws.Workspace(self.project, workflow="w1").ensure()
        self.assertTrue(os.path.isfile(os.path.join(workspace.container, ".gitignore")))
        self.assertFalse(os.path.isfile(os.path.join(workspace.dir, ".gitignore")))

    def test_no_workflow_is_the_flat_layout(self):
        """A caller with no workflow to name still gets a usable workspace."""
        workspace = ws.Workspace(self.project)
        self.assertEqual(workspace.dir, workspace.container)


class TestAdoptingTheOldLayout(IsolatedCase):
    """An upgrade must not strand a workflow that was already running."""

    def setUp(self):
        super().setUp()
        self.container = os.path.join(self.project, ".ai")
        os.makedirs(os.path.join(self.container, "reviews"))
        with open(os.path.join(self.container, "plan.md"), "w", encoding="utf-8") as handle:
            handle.write("# the plan")
        ws.write_json(os.path.join(self.container, "state.json"), {"version": 1, "runs": []})

    def test_the_artifacts_move_into_the_workflow(self):
        moved = wf.migrate(self.container, "w1")
        self.assertIn("plan.md", moved)
        destination = os.path.join(self.container, "workflows", "w1", "plan.md")
        with open(destination, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "# the plan")

    def test_nothing_is_left_behind_at_the_top_level(self):
        wf.migrate(self.container, "w1")
        self.assertFalse(os.path.exists(os.path.join(self.container, "plan.md")))

    def test_it_happens_once(self):
        wf.migrate(self.container, "w1")
        self.assertEqual(wf.migrate(self.container, "w2"), [])

    def test_an_existing_file_is_not_overwritten(self):
        destination = os.path.join(self.container, "workflows", "w1")
        os.makedirs(destination)
        with open(os.path.join(destination, "plan.md"), "w", encoding="utf-8") as handle:
            handle.write("# the newer plan")
        wf.migrate(self.container, "w1")
        with open(os.path.join(destination, "plan.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "# the newer plan")

    def test_a_fresh_project_has_nothing_to_adopt(self):
        import shutil

        shutil.rmtree(self.container)
        self.assertEqual(wf.migrate(self.container, "w1"), [])


class TestSayingTheTreeIsStillShared(IsolatedCase):
    """Separate directories separate the reports, not the files under review.

    This is the one claim the new layout could be read as making and cannot
    keep, so it is stated out loud instead.
    """

    def setUp(self):
        super().setUp()
        self.container = os.path.join(self.project, ".ai")

    def active(self, workflow, in_flight=None):
        import time

        workspace = ws.Workspace(self.project, workflow=workflow).ensure()
        workspace.write_state(
            {
                "version": 1,
                "runs": [],
                "ledger": {
                    "last_activity_monotonic": time.time(),
                    "in_flight": in_flight or {},
                },
            }
        )

    def test_another_live_workflow_is_named(self):
        self.active("other", {"implementer": {"pid": 1}})
        self.assertEqual(wf.active_elsewhere(self.container, "mine"), ["other"])

    def test_our_own_workflow_is_not_a_warning(self):
        self.active("mine", {"implementer": {"pid": 1}})
        self.assertEqual(wf.active_elsewhere(self.container, "mine"), [])

    def test_a_workflow_that_went_quiet_is_not_a_warning(self):
        """Yesterday's workflow is not someone typing in the next window."""
        workspace = ws.Workspace(self.project, workflow="old").ensure()
        workspace.write_state({"version": 1, "runs": [], "ledger": {"last_activity_monotonic": 1.0}})
        self.assertEqual(wf.active_elsewhere(self.container, "mine"), [])


@unittest.skipUnless(has_git(), "git is required")
class TestThroughTheCli(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "a = 1\n")
        self.commit_all("init")
        run_cli("config", "setup", "--defaults")

    def workflow_dir(self, workflow):
        """The path, without creating it -- `cli_workspace` ensures."""
        return wf.workflow_dir(os.path.join(self.project, ".ai"), workflow)

    def test_the_snapshot_lands_in_this_workflow(self):
        run_cli("--workflow", "w1", "review", "snapshot")
        self.assertTrue(os.path.isfile(self.cli_workspace("w1").snapshot_path))

    def test_another_workflow_does_not_see_it(self):
        run_cli("--workflow", "w1", "review", "snapshot")
        self.assertFalse(os.path.isfile(self.cli_workspace("w2").snapshot_path))

    def test_show_reports_the_id_and_where_it_came_from(self):
        code, out, _ = run_cli("--workflow", "w1", "workflow", "show")
        self.assertEqual(code, 0)
        self.assertIn("w1", out)
        self.assertIn("requested", out)

    def test_list_marks_the_current_one(self):
        run_cli("--workflow", "w1", "review", "snapshot")
        run_cli("--workflow", "w2", "review", "snapshot")
        code, out, _ = run_cli("--workflow", "w1", "workflow", "list")
        self.assertEqual(code, 0)
        self.assertIn("w1", out)
        self.assertIn("w2", out)
        self.assertIn("current", out)

    def test_list_as_json_names_the_current_one(self):
        run_cli("--workflow", "w1", "review", "snapshot")
        _, out, _ = run_cli("--workflow", "w1", "workflow", "list", "--json")
        self.assertEqual(json.loads(out)["current"], "w1")

    def test_an_invalid_id_is_refused_rather_than_written(self):
        code, _, err = run_cli("--workflow", "../escape", "review", "snapshot")
        self.assertEqual(code, 2)
        self.assertIn("invalid workflow id", err)

    def test_use_pins_an_id_for_hosts_without_a_session(self):
        run_cli("workflow", "use", "pinned")
        self.assertEqual(wf.read_pointer(os.path.join(self.project, ".ai")), "pinned")

    def test_remove_needs_consent(self):
        run_cli("--workflow", "w1", "review", "snapshot")
        code, _, err = run_cli("--workflow", "w2", "workflow", "remove", "w1")
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)
        self.assertTrue(os.path.isdir(self.workflow_dir("w1")))

    def test_remove_deletes_the_directory(self):
        run_cli("--workflow", "w1", "review", "snapshot")
        code, _, _ = run_cli("--workflow", "w2", "workflow", "remove", "w1", "--yes")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.isdir(self.workflow_dir("w1")))

    def test_removing_the_workflow_you_are_in_is_refused(self):
        """It would delete the report the next command is about to read."""
        run_cli("--workflow", "w1", "review", "snapshot")
        code, _, _ = run_cli("--workflow", "w1", "workflow", "remove", "w1", "--yes")
        self.assertEqual(code, 2)
        self.assertTrue(os.path.isdir(self.workflow_dir("w1")))


if __name__ == "__main__":
    unittest.main()
