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
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Dict

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

    def test_no_workflow_uses_the_container_itself(self):
        """A caller with no workflow to name still gets a usable workspace."""
        workspace = ws.Workspace(self.project)
        self.assertEqual(workspace.dir, workspace.container)


class TestRefusingTheOldLayout(IsolatedCase):
    """A flat `.ai/` from before 0.4.0 is no longer adopted, and never touched.

    The refusal is the whole of the way through: it names what it found and
    says what to do, and it moves nothing -- where the files go is the user's
    call.
    """

    PLAN = "# the plan"
    STATE: ClassVar[Dict[str, Any]] = {"version": 1, "runs": []}

    def setUp(self):
        super().setUp()
        self.container = os.path.join(self.project, ".ai")
        os.makedirs(self.container)
        with open(os.path.join(self.container, "plan.md"), "w", encoding="utf-8") as handle:
            handle.write(self.PLAN)
        ws.write_json(os.path.join(self.container, "state.json"), self.STATE)

    def assert_untouched(self):
        with open(os.path.join(self.container, "plan.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), self.PLAN)
        self.assertEqual(ws.read_json(os.path.join(self.container, "state.json")), self.STATE)

    def test_a_flat_layout_is_refused_before_anything_is_written(self):
        code, _, err = run_cli("state", "show")
        self.assertEqual(code, 2)
        self.assertIn("before 0.4.0", err)
        self.assertIn("(plan.md, state.json)", err)
        self.assertIn("0.20.0", err)
        self.assertIn(os.path.join(".ai", "pre-0.4.0"), err)
        self.assertFalse(os.path.exists(os.path.join(self.container, "workflows")))
        self.assertFalse(os.path.exists(wf.pointer_path(self.container)))
        self.assert_untouched()

    def test_every_command_that_uses_a_workflow_is_refused(self):
        """The guard is in ``_workspace``; each of these reaches it its own way."""
        self.write("fresh.md", "do it\n")
        commands = (
            ("status",),
            ("summary",),
            ("budget", "show"),
            ("budget", "consume", "test"),
            ("budget", "reset"),
            ("tokens", "show"),
            ("jobs", "list"),
            ("state", "record", "test", "ok"),
            ("progress", "record", "test", "--signature", "same"),
            ("optimization", "report"),
            ("design", "approve"),
            ("review", "show"),
            ("run", "implementer", "--prompt-file", "fresh.md"),
        )
        for command in commands:
            with self.subTest(command=command):
                code, out, err = run_cli(*command)
                self.assertEqual(code, 2, out + err)
                self.assertIn("before 0.4.0", err)
                self.assertFalse(os.path.exists(os.path.join(self.container, "workflows")))
                self.assertFalse(os.path.exists(wf.pointer_path(self.container)))
        self.assert_untouched()

    def test_workflow_use_writes_no_pointer(self):
        code, _, err = run_cli("workflow", "use", "w1")
        self.assertEqual(code, 2)
        self.assertIn("before 0.4.0", err)
        self.assertFalse(os.path.exists(wf.pointer_path(self.container)))
        self.assert_untouched()

    def test_workflow_remove_deletes_nothing(self):
        kept = os.path.join(self.container, "workflows", "w1")
        os.makedirs(kept)
        code, _, err = run_cli("--workflow", "w2", "workflow", "remove", "w1", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("before 0.4.0", err)
        self.assertTrue(os.path.isdir(kept))
        self.assert_untouched()

    def test_leftovers_beside_a_workflow_directory_are_refused_too(self):
        """What a 0.20.0 adoption left behind when the destination had the file."""
        destination = os.path.join(self.container, "workflows", "w1")
        os.makedirs(destination)
        with open(os.path.join(destination, "plan.md"), "w", encoding="utf-8") as handle:
            handle.write("# the newer plan")
        code, _, err = run_cli("--workflow", "w1", "state", "show")
        self.assertEqual(code, 2)
        self.assertIn("(plan.md, state.json)", err)
        with open(os.path.join(destination, "plan.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "# the newer plan")
        self.assertEqual(os.listdir(destination), ["plan.md"])
        self.assert_untouched()

    def test_a_lone_empty_directory_is_refused(self):
        for name in ("plan.md", "state.json"):
            os.remove(os.path.join(self.container, name))
        for name in ("jobs", "reviews"):
            with self.subTest(entry=name):
                os.makedirs(os.path.join(self.container, name))
                code, _, err = run_cli("state", "show")
                self.assertEqual(code, 2)
                self.assertIn("(%s)" % name, err)
                self.assertTrue(os.path.isdir(os.path.join(self.container, name)))
                os.rmdir(os.path.join(self.container, name))

    def test_the_refusal_names_every_entry_found(self):
        for name in ("execution", "reviews", "jobs"):
            os.makedirs(os.path.join(self.container, name))
        code, _, err = run_cli("state", "show")
        self.assertEqual(code, 2)
        self.assertIn("(%s)" % ", ".join(wf.LEGACY_ENTRIES), err)

    def test_workflow_list_still_runs_and_notes_the_entries(self):
        code, out, err = run_cli("workflow", "list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(set(json.loads(out)), {"current", "workflows"})
        self.assertIn("also holds plan.md, state.json from the layout used before 0.4.0", err)
        code, _, err = run_cli("workflow", "list")
        self.assertEqual(code, 0)
        self.assertIn("before 0.4.0", err)
        self.assert_untouched()

    def test_config_and_workflow_show_still_run(self):
        for command in (("config", "show"), ("workflow", "show")):
            with self.subTest(command=command):
                code, out, err = run_cli(*command)
                self.assertEqual(code, 0, out + err)
        self.assert_untouched()

    def test_moving_them_aside_lifts_the_refusal(self):
        aside = os.path.join(self.container, "pre-0.4.0")
        os.makedirs(aside)
        for name in ("plan.md", "state.json"):
            os.replace(os.path.join(self.container, name), os.path.join(aside, name))
        code, out, err = run_cli("state", "show")
        self.assertEqual(code, 0, out + err)
        with open(os.path.join(aside, "plan.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), self.PLAN)
        self.assertEqual(ws.read_json(os.path.join(aside, "state.json")), self.STATE)

    def test_a_detached_worker_is_refused_the_same_way(self):
        self.write("fresh.md", "plan it\n")
        job_file = os.path.join(self.container, "workflows", "test", "jobs", "manual.json")
        argv = ["run", "architect", "--prompt-file", "fresh.md", "--output", ".ai/plan.md"]
        code, _, err = run_cli(*argv, "--job-file", job_file)
        self.assertEqual(code, 2)
        self.assertIn("before 0.4.0", err)
        self.assertFalse(os.path.exists(os.path.join(self.container, "workflows")))
        self.assert_untouched()


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


NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


def stamp(days_ago, now=None):
    return ((now or NOW) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class StaleCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.container = os.path.join(self.project, ".ai")

    def seed(self, workflow, updated_at="", started_at="", in_flight=None):
        """A workflow whose ``state.json`` holds exactly these stamps."""
        ledger: Dict[str, Any] = {}
        if started_at:
            ledger["started_at"] = started_at
        if in_flight:
            ledger["in_flight"] = in_flight
        state: Dict[str, Any] = {"version": 1, "runs": [], "ledger": ledger}
        if updated_at:
            state["updated_at"] = updated_at
        ws.Workspace(self.project, workflow=workflow).ensure().write_state(state)

    def seed_raw(self, workflow, content):
        workspace = ws.Workspace(self.project, workflow=workflow).ensure()
        with open(workspace.state_path, "w", encoding="utf-8") as handle:
            handle.write(content)


class TestNotingWorkflowsThatWentQuiet(StaleCase):
    """Old workflows pile up by design; they are named, never removed."""

    def stale(self, days=30, workflow="mine"):
        return wf.stale_elsewhere(self.container, workflow, days, now=NOW)

    def test_nothing_is_named_when_everything_is_recent(self):
        self.seed("a", updated_at=stamp(1))
        self.seed("b", updated_at=stamp(1))
        self.assertEqual(self.stale(), [])

    def test_the_threshold_is_inclusive(self):
        self.seed("old", updated_at=stamp(31))
        self.seed("recent", updated_at=stamp(29))
        self.seed("exact", updated_at=stamp(30))
        self.assertEqual(self.stale(), ["old", "exact"])

    def test_the_current_workflow_is_left_out(self):
        self.seed("mine", updated_at=stamp(100))
        self.assertEqual(self.stale(), [])

    def test_a_workflow_with_a_stage_in_flight_is_left_out(self):
        """Accepted limitation: an abandoned stage is never cleared from the
        outside, so a workflow left mid-stage is never named here either. A
        date cannot tell it from one still running."""
        self.seed("busy", updated_at=stamp(100), in_flight={"implementer": {"pid": 1}})
        self.assertEqual(self.stale(), [])

    def test_no_evidence_is_not_staleness(self):
        ws.Workspace(self.project, workflow="bare").ensure()
        self.seed("empty")
        self.seed("vague", updated_at="yesterday")
        self.assertEqual(self.stale(), [])

    def test_started_at_answers_when_updated_at_is_missing(self):
        self.seed("started", started_at=stamp(40))
        self.assertEqual(self.stale(), ["started"])

    def test_a_malformed_sibling_is_skipped_not_raised(self):
        self.seed_raw("listed", "[1]")
        self.seed_raw(
            "garbled",
            json.dumps({"ledger": {"last_activity_monotonic": "soon", "total_delegated_runs": "many"}}),
        )
        self.seed("old", updated_at=stamp(40))
        self.assertEqual(self.stale(), ["old"])

    def test_the_oldest_comes_first(self):
        self.seed("b", updated_at=stamp(50))
        self.seed("a", updated_at=stamp(40))
        self.seed("c", updated_at=stamp(60))
        self.assertEqual(self.stale(), ["c", "b", "a"])

    def test_zero_turns_it_off(self):
        self.seed("old", updated_at=stamp(400))
        self.assertEqual(self.stale(days=0), [])

    def test_a_huge_threshold_does_not_raise(self):
        """Seconds are compared; no timedelta is built from the number."""
        self.seed("old", updated_at=stamp(400))
        self.assertEqual(self.stale(days=10**12), [])
        self.assertEqual(self.stale(days=1), ["old"])

    def test_create_dir_says_whether_this_call_created_it(self):
        self.assertTrue(wf.create_dir(self.container, "w1"))
        self.assertFalse(wf.create_dir(self.container, "w1"))

    def test_listing_reads_a_malformed_state_as_nothing_recorded(self):
        """A behaviour change for `workflow list`: a row, not a traceback."""
        self.seed_raw("listed", "[1]")
        (entry,) = wf.listing(self.container)
        self.assertEqual(entry["workflow"], "listed")
        self.assertEqual((entry["updated_at"], entry["started_at"], entry["in_flight"]), ("", "", []))
        self.assertEqual(entry["runs"], 0)


class TestTheStaleNoteThroughTheCli(StaleCase):
    def ago(self, days):
        return stamp(days, datetime.now(timezone.utc))

    def config(self, days):
        self.write(".dev-orchestra.yaml", "version: 1\nworkspace:\n  stale_notice_days: %s\n" % days)

    def test_the_first_command_of_a_new_workflow_notes_the_quiet_ones(self):
        self.seed("old", updated_at=self.ago(40))
        code, _, err = run_cli("--workflow", "fresh", "state", "record", "architect", "ok")
        self.assertEqual(code, 0)
        self.assertIn("note: 1 workflow has not been active for 30 days or more (old)", err)
        self.assertIn('"workflow list"', err)
        self.assertIn('"workflow remove <id> --yes"', err)
        self.assertTrue(os.path.isdir(wf.workflow_dir(self.container, "old")))

    def test_it_is_said_once(self):
        self.seed("old", updated_at=self.ago(40))
        run_cli("--workflow", "fresh", "state", "record", "architect", "ok")
        _, _, err = run_cli("--workflow", "fresh", "state", "record", "architect", "ok")
        self.assertNotIn("note:", err)

    def test_zero_silences_it(self):
        self.config(0)
        self.seed("old", updated_at=self.ago(40))
        _, _, err = run_cli("--workflow", "fresh", "state", "record", "architect", "ok")
        self.assertNotIn("note:", err)

    def test_a_higher_threshold_leaves_a_younger_workflow_out(self):
        self.config(200)
        self.seed("old", updated_at=self.ago(100))
        _, _, err = run_cli("--workflow", "fresh", "state", "record", "architect", "ok")
        self.assertNotIn("note:", err)

    def test_a_lower_threshold_names_a_younger_workflow(self):
        self.config(5)
        self.seed("old", updated_at=self.ago(10))
        _, _, err = run_cli("--workflow", "fresh", "state", "record", "architect", "ok")
        self.assertIn("note: 1 workflow has not been active for 5 days or more (old)", err)

    def test_json_output_stays_clean(self):
        self.seed("old", updated_at=self.ago(40))
        code, out, err = run_cli("--workflow", "fresh", "state", "show", "--json")
        self.assertEqual(code, 0)
        json.loads(out)
        self.assertIn("note:", err)

    def test_at_most_five_are_named(self):
        for number in range(6):
            self.seed("old%d" % number, updated_at=self.ago(60 - number))
        _, _, err = run_cli("--workflow", "fresh", "state", "record", "architect", "ok")
        self.assertIn("note: 6 workflows have not been active", err)
        self.assertIn("(old0, old1, old2, old3, old4, ...)", err)
        self.assertNotIn("old5", err)

    def test_workflow_list_says_nothing(self):
        """It is where the note sends the user, so it must not repeat it."""
        self.seed("old", updated_at=self.ago(40))
        self.seed("older", updated_at=self.ago(80))
        code, _, err = run_cli("--workflow", "fresh", "workflow", "list")
        self.assertEqual(code, 0)
        self.assertNotIn("note:", err)

    def test_a_corrupt_sibling_does_not_stop_the_command(self):
        self.seed_raw("bad", "[1]")
        self.seed("old", updated_at=self.ago(40))
        code, _, err = run_cli("--workflow", "fresh", "state", "record", "architect", "ok")
        self.assertEqual(code, 0)
        state = ws.read_json(os.path.join(wf.workflow_dir(self.container, "fresh"), "state.json"), {})
        self.assertEqual([event["stage"] for event in state["events"]], ["architect"])
        self.assertTrue(os.path.isfile(os.path.join(self.container, ".gitignore")))
        self.assertIn("(old)", err)
        self.assertNotIn("bad", err)

    def test_a_corrupt_sibling_does_not_stop_the_readers_of_every_workflow(self):
        # Not "[]": an empty list is falsy and already reads as nothing.
        self.seed_raw("bad", "[1]")
        self.seed("old", updated_at=self.ago(40))
        run_cli("--workflow", "fresh", "state", "record", "architect", "ok")
        for command in (("workflow", "list"), ("optimization", "report")):
            with self.subTest(command=command):
                # No --workflow: that is when the report reads every workflow.
                code, out, err = run_cli(*command)
                self.assertEqual(code, 0, out + err)

    def test_an_out_of_range_threshold_falls_back_to_the_default(self):
        self.config(1000000000)
        self.seed("old", updated_at=self.ago(31))
        code, _, err = run_cli("--workflow", "fresh", "state", "record", "architect", "ok")
        self.assertEqual(code, 0)
        self.assertIn("note: 1 workflow has not been active for 30 days or more (old)", err)
        _, out, _ = run_cli("config", "validate")
        self.assertIn("workspace.stale_notice_days: must be 36500 or less (100 years)", out)


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
