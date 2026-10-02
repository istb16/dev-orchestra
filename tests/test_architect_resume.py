"""`run architect --resume`: revising the plan by continuing the architect's session.

Driven through the CLI with the architect on the mock provider, which records
every run it is asked for in ``$DEV_ORCHESTRA_MOCK_TRACE``: the mode, the
command, the session it resumed and how long the prompt was. The two prompts
differ in length, so the trace says which one a run was sent.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest

from helpers import IsolatedCase
from test_cli import run_cli

from orchestrator import cli
from orchestrator import jobs as jobs_mod
from orchestrator import ledger as ledger_mod
from orchestrator import workspace as ws
from orchestrator.providers.base import ModelResolutionError, RunResult
from orchestrator.providers.mock import MockProvider

FRESH = "Revise the plan: the original request, in full, and the findings.\n"
RESUME = "Revise: findings only.\n"
UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class ResumeCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "architect.provider", "mock")
        run_cli("config", "set", "budgets.architect", "20")
        self.trace_path = os.path.join(self.tmp, "trace.jsonl")
        os.environ["DEV_ORCHESTRA_MOCK_TRACE"] = self.trace_path
        self.write("fresh.md", FRESH)
        self.write("resume.md", RESUME)

    def trace(self):
        if not os.path.isfile(self.trace_path):
            return []
        with open(self.trace_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def revise(self, *extra, resume=True):
        argv = ["run", "architect", "--prompt-file", "fresh.md", "--output", ".ai/plan.md"]
        if resume:
            argv += ["--resume", "--resume-prompt-file", "resume.md"]
        return run_cli(*argv, *extra)

    def events(self):
        state = self.cli_workspace().read_state()
        return [event for event in state.get("events") or [] if event.get("stage") == "architect"]

    def last(self):
        return self.events()[-1]

    def edit_last(self, **changes):
        workspace = self.cli_workspace()
        state = workspace.read_state()
        runs = [event for event in state["events"] if event.get("stage") == "architect"]
        runs[-1].update(changes)
        workspace.write_state(state)

    def attempts(self):
        return json.loads(run_cli("budget", "show", "--json")[1])["budgets"]["architect"]["used"]

    def tokens(self):
        return json.loads(run_cli("tokens", "show", "--json")[1])


class TestTheArgumentsAreChecked(ResumeCase):
    def assert_refused(self, code, err, text):
        self.assertEqual(code, 2)
        self.assertIn(text, err)
        self.assertEqual(self.attempts(), 0)
        self.assertEqual(self.trace(), [])

    def test_only_the_architect_resumes(self):
        run_cli("config", "set", "implementer.provider", "mock")
        for role in ("implementer", "claude-general"):
            with self.subTest(role=role):
                code, _, err = run_cli(
                    "run", role, "--prompt", "x", "--resume", "--resume-prompt-file", "resume.md"
                )
                self.assertEqual(code, 2)
                self.assertIn("--resume applies to architect", err)

    def test_the_two_prompt_flags_go_together(self):
        code, _, err = run_cli("run", "architect", "--prompt", "x", "--resume-prompt-file", "resume.md")
        self.assert_refused(code, err, "--resume-prompt-file needs --resume")
        code, _, err = run_cli("run", "architect", "--prompt", "x", "--resume", "--output", ".ai/plan.md")
        self.assert_refused(code, err, "--resume needs --resume-prompt-file")

    def test_only_one_prompt_can_come_from_stdin(self):
        saved = sys.stdin
        self.addCleanup(setattr, sys, "stdin", saved)
        refusal = "--resume-prompt-file - needs the fresh prompt from --prompt or a file: stdin is read once"
        resume = ["--resume", "--resume-prompt-file", "-", "--output", ".ai/plan.md"]
        for fresh in (["--prompt-file", "-"], []):
            with self.subTest(fresh=fresh):
                sys.stdin = io.StringIO("piped\n")
                code, _, err = run_cli("run", "architect", *fresh, *resume)
                self.assert_refused(code, err, refusal)
        sys.stdin = io.StringIO("revise\n")
        code, _, err = run_cli("run", "architect", "--prompt-file", "fresh.md", *resume)
        self.assertEqual(code, 0, err)

    def test_implement_mode_is_refused_as_it_always_was(self):
        code, _, err = self.revise("--mode", "implement")
        self.assert_refused(code, err, "runs are read-only")

    def test_review_mode_is_refused(self):
        for extra in ((), ("--print-command",), ("--detach",)):
            with self.subTest(extra=extra):
                code, _, err = self.revise("--mode", "review", *extra)
                self.assert_refused(code, err, "--resume applies to architect in plan mode")

    def test_a_worker_checks_again(self):
        job_file = os.path.join(jobs_mod.jobs_dir(self.cli_workspace()), "manual.json")
        jobs_mod.write_job(self.cli_workspace(), {"id": "manual", "stage": "architect", "status": "running"})
        code, _, _ = self.revise("--mode", "review", "--job-file", job_file)
        self.assertEqual(code, 2)
        self.assertIn("in plan mode", ws.read_json(job_file)["error"])

    def test_the_output_has_to_be_the_plan(self):
        code, _, err = run_cli(
            "run", "architect", "--prompt-file", "fresh.md", "--resume", "--resume-prompt-file", "resume.md"
        )
        self.assert_refused(code, err, "--resume revises this workflow's plan: --output is missing")

        job_file = os.path.join(jobs_mod.jobs_dir(self.cli_workspace()), "manual.json")
        jobs_mod.write_job(self.cli_workspace(), {"id": "manual", "stage": "architect", "status": "running"})
        argv = [
            "run",
            "architect",
            "--prompt-file",
            "fresh.md",
            "--resume",
            "--resume-prompt-file",
            "resume.md",
            "--output",
            ".ai/execution/architect-report.md",
        ]
        code, _, err = run_cli(*argv)
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "--resume revises this workflow's plan: --output must be .ai/plan.md")
        code, _, err = run_cli(*argv, "--job-file", job_file)
        error = ws.read_json(job_file)["error"]
        for text in (err, error):
            self.assertNotIn("architect-report.md", text)
            self.assertNotIn("workflows", text)

        other_plan = ws.Workspace(self.project, workflow="other").plan_path
        code, _, err = run_cli(*argv[:-1], other_plan)
        self.assertEqual(code, 2)
        self.assertNotIn(other_plan, err)

    def test_the_plan_is_accepted_through_a_linked_directory(self):
        # The spelling macOS gives a temporary directory (/var for
        # /private/var): the same plan, reached through a link.
        link = os.path.join(os.path.dirname(self.project), "linked-project")
        try:
            os.symlink(self.project, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("cannot create a directory link here: %s" % exc)
        # The link sits in the test's temporary directory, which tearDown
        # removes whole -- the link with it, never what it points to.
        plan = self.cli_workspace().plan_path
        output = os.path.join(link, os.path.relpath(plan, self.project))
        code, _, err = run_cli(
            "run",
            "architect",
            "--prompt-file",
            "fresh.md",
            "--resume",
            "--resume-prompt-file",
            "resume.md",
            "--output",
            output,
        )
        self.assertEqual(code, 0, err)

    def test_the_plan_is_accepted_either_way_it_is_named(self):
        for output in (".ai/plan.md", self.cli_workspace().plan_path):
            with self.subTest(output=output):
                code, _, _ = run_cli(
                    "run",
                    "architect",
                    "--prompt-file",
                    "fresh.md",
                    "--resume",
                    "--resume-prompt-file",
                    "resume.md",
                    "--output",
                    output,
                )
                self.assertEqual(code, 0)

    def test_a_raw_resume_argument_never_reaches_the_command(self):
        code, _, err = self.revise("--extra", "--resume=55555555-5555-4555-8555-555555555555")
        self.assertEqual(code, 2)
        self.assertIn("'--resume'", err)
        self.assertEqual(self.trace(), [])


class TestResumingAndChaining(ResumeCase):
    def test_the_first_revision_request_runs_fresh(self):
        code, _, err = self.revise()
        self.assertEqual(code, 0)
        event = self.last()
        self.assertEqual(event["resume"]["mode"], "fresh")
        self.assertEqual(event["resume"]["reason"], "no earlier architect run in this workflow")
        self.assertRegex(event["session_id"], UUID_RE)
        self.assertIn("running fresh: no earlier architect run in this workflow", err)
        self.assertIsNone(self.trace()[-1]["resume_session"])
        self.assertEqual(self.trace()[-1]["prompt_chars"], len(FRESH))
        self.assertNotIn("architect:resumed", self.tokens()["by_label"])

    def test_later_revisions_resume_and_chain(self):
        self.revise()
        first = self.last()["session_id"]
        code, _, err = self.revise()
        self.assertEqual(code, 0)
        second = self.last()
        self.assertEqual(second["resume"]["mode"], "resumed")
        self.assertEqual(second["resume"]["resumed_from"], first)
        self.assertEqual(second["resume"]["outcome"], "ok")
        self.assertIn("note: resuming the last architect session", err)
        run = self.trace()[-1]
        self.assertEqual(run["resume_session"], first)
        self.assertEqual(run["command"][-1], "--resume=%s" % first)
        self.assertEqual(run["prompt_chars"], len(RESUME))
        self.assertIn("architect:resumed", self.tokens()["by_label"])

        self.revise()
        self.assertEqual(self.last()["resume"]["resumed_from"], second["session_id"])

    def test_every_run_records_what_comparing_needs(self):
        self.revise(resume=False)
        event = self.last()
        for key in ("session_id", "context_tokens", "cost_usd", "cache_read_tokens"):
            self.assertIn(key, event)
        self.assertNotIn("resume", event)

    def test_a_tier_keeps_its_label(self):
        run_cli("config", "set", "architect.model_tiers.light.model.family", "small")
        self.revise()
        self.revise("--tier", "light")
        self.assertEqual(self.last()["resume"]["mode"], "resumed")
        labels = self.tokens()["by_label"]
        self.assertIn("architect:light", labels)
        self.assertNotIn("architect:resumed", labels)


class TestFallingBackToFresh(ResumeCase):
    def setUp(self):
        super().setUp()
        self.revise()

    def fallback(self, reason):
        code, out, err = self.revise()
        self.assertEqual(code, 0)
        event = self.last()
        self.assertEqual(event["resume"]["mode"], "fresh")
        self.assertEqual(event["resume"]["reason"], reason)
        self.assertIn(reason, cli._RESUME_REASONS)
        self.assertIsNone(self.trace()[-1]["resume_session"])
        self.assertEqual(self.trace()[-1]["prompt_chars"], len(FRESH))
        return out, err

    def test_a_recorded_mode_other_than_plan(self):
        self.edit_last(mode="implement")
        self.fallback("the last architect run is not resumable: its recorded mode is not plan")

    def test_a_recorded_provider_that_differs_is_not_repeated(self):
        self.edit_last(provider="evil provider")
        _, err = self.fallback("the last architect run is not resumable: its recorded provider differs")
        self.assertNotIn("evil", err)
        self.assertNotIn("evil", json.dumps(self.last()))

    def test_an_output_other_than_the_plan(self):
        self.edit_last(output="-")
        self.fallback("the last architect run is not resumable: its output is not this workflow's plan")

    def test_a_session_id_that_is_not_a_uuid_is_not_repeated(self):
        self.edit_last(session_id="--permission-mode")
        _, err = self.fallback("the last architect run is not resumable: its session id is not a UUID")
        self.assertNotIn("--permission-mode", err)
        self.assertNotIn("--permission-mode", json.dumps(self.trace()[-1]))

    def test_no_session_id(self):
        self.edit_last(session_id=None)
        self.fallback("the last architect run is not resumable: it has no session id")

    def test_an_old_run(self):
        self.edit_last(at="2026-01-01T00:00:00Z")
        _, err = self.fallback("the last architect run is older than design.resume.max_age_seconds")
        self.assertNotIn("3600", err)

    def test_a_zero_age_limit_always_runs_fresh(self):
        run_cli("config", "set", "design.resume.max_age_seconds", "0")
        self.fallback("the last architect run is older than design.resume.max_age_seconds")

    def test_a_context_over_the_cap(self):
        self.edit_last(context_tokens=5000)
        run_cli("config", "set", "design.resume.max_context_tokens", "100")
        self.fallback("the last architect run's context exceeds design.resume.max_context_tokens")

    def test_an_unknown_context_under_a_cap(self):
        run_cli("config", "set", "design.resume.max_context_tokens", "100")
        for context in (None, "50", True):
            with self.subTest(context=context):
                self.edit_last(context_tokens=context)
                self.fallback(
                    "the last architect run's context is unknown and design.resume.max_context_tokens is set"
                )

    def test_an_unverified_provider(self):
        def unverified(provider, root):
            return {"status": "unverified", "detail": "mock 0 has not been verified"}

        self.addCleanup(setattr, MockProvider, "resume_support", MockProvider.resume_support)
        setattr(MockProvider, "resume_support", unverified)
        _, err = self.fallback("the provider cannot resume a session (unverified)")
        self.assertIn("note: mock 0 has not been verified", err)
        self.assertNotIn("mock 0 has not been verified", json.dumps(self.last()))
        self.assertEqual(self.attempts(), 2)

    def test_a_provider_that_cannot_resume(self):
        original = MockProvider.supports_resume
        self.addCleanup(setattr, MockProvider, "supports_resume", original)
        MockProvider.supports_resume = False
        self.fallback("the provider cannot resume a session")

    def test_the_report_counts_the_phrases_as_they_are(self):
        self.edit_last(provider="evil provider")
        self.revise()
        report = json.loads(run_cli("optimization", "report", "--json")[1])
        reasons = report["architect_revisions"]["fallbacks"]["reasons"]
        self.assertTrue(reasons)
        for reason in reasons:
            self.assertIn(reason, cli._RESUME_REASONS)


class TestTheCliRejectsTheSession(ResumeCase):
    def setUp(self):
        super().setUp()
        self.revise()

    def test_it_runs_fresh_once_and_records_both(self):
        os.environ["DEV_ORCHESTRA_MOCK_RESUME"] = "reject"
        code, _, err = self.revise()
        self.assertEqual(code, 0)
        rejected, retried = self.events()[-2:]
        self.assertEqual(rejected["status"], "failed")
        self.assertEqual(rejected["resume"]["outcome"], "rejected")
        self.assertEqual(rejected["billed_tokens"], 0)
        self.assertNotIn("error", rejected)
        self.assertEqual(retried["status"], "ok")
        self.assertEqual(retried["resume"]["mode"], "fresh")
        self.assertEqual(retried["resume"]["reason"], "the CLI rejected the session it was asked to resume")
        self.assertEqual(self.attempts(), 3)
        self.assertEqual(self.tokens()["by_stage"]["architect"]["runs"], 3)
        runs = self.trace()[-2:]
        self.assertIsNotNone(runs[0]["resume_session"])
        self.assertIsNone(runs[1]["resume_session"])
        self.assertEqual(runs[1]["prompt_chars"], len(FRESH))
        self.assertTrue(os.path.isfile(self.cli_workspace().plan_path))
        self.assertNotIn("No conversation found", json.dumps(self.events()))
        self.assertIn("the CLI rejected the session", err)
        # Neither the first revision nor the retry continued a session.
        self.assertNotIn("architect:resumed", self.tokens()["by_label"])

    def worker_job(self):
        """A job record as a parent writes it. The worker itself is started
        with --force (see `_detached_argv`); the record says the user did not."""
        job_file = os.path.join(jobs_mod.jobs_dir(self.cli_workspace()), "manual.json")
        jobs_mod.write_job(self.cli_workspace(), {"id": "manual", "stage": "architect", "status": "running"})
        return job_file

    def assert_failed_for_no_budget(self, job_file):
        job = ws.read_json(job_file)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], cli._RESUME_NO_BUDGET)
        book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))
        self.assertEqual(book.summary()["in_flight"], {})

    def test_no_budget_left_means_no_second_run(self):
        run_cli("config", "set", "budgets.architect", "2")
        os.environ["DEV_ORCHESTRA_MOCK_RESUME"] = "reject"
        code, _, err = self.revise()
        self.assertEqual(code, cli.ledger_mod.EXIT_BUDGET_EXHAUSTED)
        self.assertEqual(self.last()["resume"]["outcome"], "rejected")
        self.assertIn("running fresh would spend an attempt", err)
        self.assertEqual(len(self.trace()), 2)
        rejected = err.index("note: %s" % cli._RESUME_REJECTED)
        no_budget = err.index(cli._RESUME_NO_BUDGET)
        self.assertLess(rejected, no_budget)
        self.assertLess(no_budget, err.index("refusing to run architect:"))

    def test_a_budget_spent_between_check_and_consume_still_says_why(self):
        os.environ["DEV_ORCHESTRA_MOCK_RESUME"] = "reject"
        consumed = []
        original = cli.ledger_mod.Ledger.consume

        def consume(book, stage, force=False):
            consumed.append(stage)
            # The first is this run's own attempt; the second is the retry's.
            if len(consumed) == 2:
                raise cli.ledger_mod.BudgetExhausted("spent elsewhere")
            return original(book, stage, force=force)

        self.addCleanup(setattr, cli.ledger_mod.Ledger, "consume", original)
        setattr(cli.ledger_mod.Ledger, "consume", consume)
        code, _, err = self.revise()
        self.assertEqual(code, cli.ledger_mod.EXIT_BUDGET_EXHAUSTED)
        rejected = err.index("note: %s" % cli._RESUME_REJECTED)
        no_budget = err.index(cli._RESUME_NO_BUDGET)
        self.assertLess(rejected, no_budget)
        self.assertLess(no_budget, err.index("spent elsewhere"))
        self.assertEqual(self.last()["resume"]["outcome"], "rejected")

    def test_a_worker_with_no_budget_left_fails_its_job(self):
        run_cli("config", "set", "budgets.architect", "2")
        os.environ["DEV_ORCHESTRA_MOCK_RESUME"] = "reject"
        # The parent spends the attempt before handing over; a worker does not.
        run_cli("budget", "consume", "architect")
        job_file = self.worker_job()
        code, _, _ = self.revise("--force", "--job-file", job_file)
        self.assertEqual(code, cli.ledger_mod.EXIT_BUDGET_EXHAUSTED)
        self.assert_failed_for_no_budget(job_file)
        self.assertEqual(self.last()["resume"]["outcome"], "rejected")

    def test_a_worker_whose_budget_is_spent_before_the_retry_fails_its_job(self):
        os.environ["DEV_ORCHESTRA_MOCK_RESUME"] = "reject"
        original = cli.ledger_mod.Ledger.consume

        def consume(book, stage, force=False):
            # A worker skips the run's own attempt, so this is the retry's.
            raise cli.ledger_mod.BudgetExhausted("spent elsewhere")

        self.addCleanup(setattr, cli.ledger_mod.Ledger, "consume", original)
        setattr(cli.ledger_mod.Ledger, "consume", consume)
        job_file = self.worker_job()
        code, _, err = self.revise("--force", "--job-file", job_file)
        self.assertEqual(code, cli.ledger_mod.EXIT_BUDGET_EXHAUSTED)
        self.assert_failed_for_no_budget(job_file)
        self.assertIn("spent elsewhere", err)

    def test_force_runs_fresh_past_the_budget(self):
        run_cli("config", "set", "budgets.architect", "2")
        os.environ["DEV_ORCHESTRA_MOCK_RESUME"] = "reject"
        code, _, _ = self.revise("--force")
        self.assertEqual(code, 0)
        self.assertEqual(self.last()["resume"]["reason"], cli._RESUME_REJECTED)

    def test_the_environment_is_cleaned_up_after_a_test(self):
        # Order-independent: whichever test ran before, nothing it set leaks.
        self.assertNotIn("DEV_ORCHESTRA_MOCK_RESUME", os.environ)


NOT_SUCCEEDED = "the last architect run is not resumable: it did not succeed"


class TestFailuresAreNotRetried(ResumeCase):
    def setUp(self):
        super().setUp()
        self.revise()

    def test_a_failed_resumed_run_is_reported_and_the_next_goes_fresh(self):
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "1"
        code, _, _ = self.revise()
        self.assertEqual(code, 1)
        self.assertEqual(self.last()["status"], "failed")
        self.assertEqual(self.last()["resume"]["mode"], "resumed")
        self.assertEqual(self.attempts(), 2)
        del os.environ["DEV_ORCHESTRA_MOCK_FAIL"]
        self.revise()
        self.assertEqual(self.last()["resume"]["reason"], NOT_SUCCEEDED)

    def test_a_stalled_resumed_run_is_reported_and_the_next_goes_fresh(self):
        os.environ["DEV_ORCHESTRA_MOCK_RESUME"] = "stall"
        self.revise()
        self.assertEqual(self.last()["status"], "stalled")
        self.assertEqual(self.attempts(), 2)
        self.revise()
        self.assertEqual(self.last()["resume"]["reason"], NOT_SUCCEEDED)


class TestPrintCommand(ResumeCase):
    def test_the_resumed_command_is_printed_and_nothing_spent(self):
        self.revise()
        session = self.last()["session_id"]
        code, out, _ = self.revise("--print-command")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith("--resume=%s" % session))
        self.assertEqual(self.attempts(), 1)


TRUSTED_DETAIL = "mock 1 is trusted to resume as newer than mock 0, verified on 2026-10-01 (built-in)"
SANDBOX_UNCONFIRMED = (
    "the forked session's filesystem sandbox could not be confirmed read-only (workspace-write); "
    "its output is not used"
)
REFUSED = "codex: the session to resume was not started in this workspace"


class TestATrustedVersion(ResumeCase):
    """A version newer than one that passed resumes, and says it was not checked."""

    def setUp(self):
        super().setUp()

        def trusted(provider, root):
            return {"status": "trusted", "detail": TRUSTED_DETAIL, "newer_than": "mock 0"}

        self.addCleanup(setattr, MockProvider, "resume_support", MockProvider.resume_support)
        setattr(MockProvider, "resume_support", trusted)
        self.revise()

    def test_it_resumes_and_records_the_trust_without_a_version(self):
        job_file = os.path.join(jobs_mod.jobs_dir(self.cli_workspace()), "manual.json")
        jobs_mod.write_job(self.cli_workspace(), {"id": "manual", "stage": "architect", "status": "running"})
        code, _, err = self.revise("--job-file", job_file)
        self.assertEqual(code, 0, err)
        event = self.last()
        self.assertEqual(event["resume"]["mode"], "resumed")
        self.assertEqual(event["resume"]["trust"], "newer")
        job = ws.read_json(job_file)
        self.assertEqual(job["resume"]["trust"], "newer")
        for record in (event, job):
            self.assertNotIn("mock 0", json.dumps(record))
            self.assertNotIn("mock 1", json.dumps(record))
        self.assertIn("note: resuming the last architect session\nnote: %s" % TRUSTED_DETAIL, err)
        self.assertIsNotNone(self.trace()[-1]["resume_session"])

    def test_the_report_counts_it_as_resumed(self):
        self.revise()
        report = json.loads(run_cli("optimization", "report", "--json")[1])
        self.assertEqual(report["architect_revisions"]["resumed"]["runs"], 1)

    def test_an_exactly_verified_run_records_no_trust(self):
        setattr(MockProvider, "resume_support", lambda provider, root: {"status": "verified", "detail": ""})
        self.revise()
        self.assertEqual(self.last()["resume"]["mode"], "resumed")
        self.assertNotIn("trust", self.last()["resume"])


class TestTheAdapterJudgesTheResumedRun(ResumeCase):
    def setUp(self):
        super().setUp()
        self.revise()

    def resumed_returns(self, result, fresh=None):
        original = MockProvider._launch

        def launch(provider, prompt, mode, cwd, **kwargs):
            if kwargs.get("resume_session") is not None:
                return result
            if fresh is not None:
                return fresh()
            return original(provider, prompt, mode, cwd, **kwargs)

        self.addCleanup(setattr, MockProvider, "_launch", original)
        setattr(MockProvider, "_launch", launch)

    def test_an_unconfirmed_sandbox_fails_the_run_and_the_next_goes_fresh(self):
        plan = self.cli_workspace().plan_path
        with open(plan, "w", encoding="utf-8") as handle:
            handle.write("the earlier plan\n")
        self.resumed_returns(RunResult(False, 1, "", SANDBOX_UNCONFIRMED, ["mock"], 0.1))
        code, _, _ = self.revise()
        self.assertEqual(code, 1)
        self.assertEqual(self.last()["status"], "failed")
        self.assertEqual(self.last()["resume"]["mode"], "resumed")
        with open(plan, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "the earlier plan\n")
        self.revise()
        self.assertEqual(self.last()["resume"]["reason"], NOT_SUCCEEDED)

    def test_an_adapter_refusal_runs_fresh_once_and_says_why(self):
        refusal = RunResult(False, 2, "", REFUSED, ["mock"], 0.0, invoked=False, resume_rejected=True)
        self.resumed_returns(refusal)
        code, _, err = self.revise()
        self.assertEqual(code, 0, err)
        rejected, retried = self.events()[-2:]
        self.assertEqual(rejected["status"], "failed")
        self.assertEqual(rejected["resume"]["outcome"], "rejected")
        self.assertEqual(retried["status"], "ok")
        self.assertEqual(retried["resume"]["reason"], cli._RESUME_REJECTED)
        self.assertIn("note: %s\nnote: %s" % (cli._RESUME_REJECTED, REFUSED), err)
        self.assertLess(
            err.index("note: resuming the last architect session"),
            err.index("note: %s\nnote: %s" % (cli._RESUME_REJECTED, REFUSED)),
        )
        self.assertNotIn(REFUSED, json.dumps(self.events()))
        # Nothing ran for the refusal, so the fresh run spends its attempt.
        self.assertEqual(self.attempts(), 2)
        self.assertIsNone(self.trace()[-1]["resume_session"])

    def test_an_adapter_refusal_with_one_attempt_left_still_runs_fresh(self):
        run_cli("config", "set", "budgets.architect", "2")
        refusal = RunResult(False, 2, "", REFUSED, ["mock"], 0.0, invoked=False, resume_rejected=True)
        self.resumed_returns(refusal)
        code, _, err = self.revise()
        self.assertEqual(code, 0, err)
        self.assertNotIn("running fresh would spend an attempt", err)
        self.assertEqual(self.last()["status"], "ok")
        self.assertEqual(self.attempts(), 2)

    def test_a_fresh_retry_whose_model_cannot_be_resolved_fails_once(self):
        rejected = RunResult(
            False, 1, "", "No conversation found", ["mock"], 0.1, invoked=True, resume_rejected=True
        )

        def unresolvable():
            raise ModelResolutionError("no model for the fresh run")

        self.resumed_returns(rejected, fresh=unresolvable)
        job_file = os.path.join(jobs_mod.jobs_dir(self.cli_workspace()), "manual.json")
        jobs_mod.write_job(self.cli_workspace(), {"id": "manual", "stage": "architect", "status": "running"})
        before = len(self.events())
        code, _, err = self.revise("--job-file", job_file)
        self.assertEqual(code, 2)
        added = self.events()[before:]
        self.assertEqual([event["status"] for event in added], ["failed", "failed"])
        self.assertEqual(added[0]["resume"]["outcome"], "rejected")
        self.assertEqual(added[1]["error"], "no model for the fresh run")
        for event in added:
            self.assertNotIn("charge_skipped", event)
        book = ledger_mod.Ledger(self.cli_workspace(), dict(ledger_mod.DEFAULT_BUDGETS))
        self.assertEqual(book.summary()["in_flight"], {})
        job = ws.read_json(job_file)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "no model for the fresh run")
        self.assertLess(err.index("note: %s" % cli._RESUME_REJECTED), err.index("no model for the fresh run"))
        # setUp's foreground run spent one. A worker does not consume its first
        # attempt -- its parent did -- but the retry asks the budget like any
        # run, and the job carries no `force`, so it spent the second.
        self.assertEqual(self.attempts(), 2)


class TestCodexIsNotResumed(ResumeCase):
    def test_codex_with_resume_off_runs_fresh(self):
        from orchestrator.providers.codex import CodexProvider

        self.addCleanup(setattr, CodexProvider, "supports_resume", CodexProvider.supports_resume)
        CodexProvider.supports_resume = False
        run_cli("config", "set", "architect.provider", "codex")
        run_cli("config", "set", "architect.model.family", "recommended-coding")
        _, _, err = self.revise("--print-command")
        self.assertIn("running fresh: the provider cannot resume a session", err)


class TestTheReport(ResumeCase):
    def test_mock_costs_nothing_so_there_is_no_ratio(self):
        self.revise()
        self.revise()
        code, out, _ = run_cli("optimization", "report", "--json")
        self.assertEqual(code, 0)
        resumed = json.loads(out)["architect_revisions"]["resumed"]
        self.assertEqual(resumed["runs"], 1)
        self.assertEqual(resumed["ratio_runs"], 0)
        self.assertIsNone(resumed["cost_ratio_mean"])
        code, out, _ = run_cli("optimization", "report")
        self.assertEqual(code, 0)
        self.assertIn("n/a (initial run has no usable cost)", out)

    def test_failed_attempts_are_shown_without_a_ratio(self):
        group = {
            "runs": 0,
            "priced_runs": 0,
            "ratio_runs": 0,
            "cost_ratio_mean": None,
            "cost_per_completed_ratio": None,
            "failed_attempts": {"stalled": 2, "rejected": 1, "failed": 3, "priced": 4},
        }
        revisions = {"resumed": group, "fresh": dict(group), "fallbacks": {"reasons": {}}}
        rows = cli._revision_rows(revisions)
        for row in rows[1:3]:
            self.assertIn("n/a", row)
            self.assertIn("2 stalled + 1 rejected + 3 failed attempts (4 priced)", row)


class TestDetached(ResumeCase):
    def wait(self, job_id):
        _, out, _ = run_cli("jobs", "wait", job_id, "--timeout", "60", "--json")
        return json.loads(out)

    def detach(self, *extra):
        code, out, _ = self.revise("--detach", "--json", *extra)
        self.assertEqual(code, 0)
        return json.loads(out)

    def test_the_worker_argv_keeps_its_own_options_ahead_of_extra(self):
        args = cli.build_parser().parse_args(
            [
                "run",
                "architect",
                "--prompt-file",
                "fresh.md",
                "--resume",
                "--resume-prompt-file",
                "resume.md",
                "--output",
                ".ai/plan.md",
                "--detach",
                "--extra",
                "--add-dir",
                "/x",
            ]
        )
        argv = cli._detached_argv(args, "architect", "named")
        self.assertLess(argv.index("--resume"), argv.index("--extra"))
        self.assertLess(argv.index("--resume-prompt-file"), argv.index("--extra"))
        copy = "/jobs/1.resume-prompt"
        filled = [copy if token == jobs_mod.RESUME_PROMPT_FILE else token for token in argv]
        parsed = cli.build_parser().parse_args(filled)
        self.assertEqual(parsed.resume_prompt_file, copy)
        self.assertTrue(parsed.resume)
        self.assertEqual(parsed.extra, ["--add-dir", "/x"])
        self.assertEqual(parsed.workflow, "named")
        self.assertEqual(parsed.command, "run")

    def test_a_detached_revision_resumes(self):
        self.revise()
        job = self.detach()
        self.assertIs(job["force"], False)
        self.assertTrue(job["resume_prompt_file"].endswith("%s.resume-prompt" % job["id"]))
        self.assertEqual(self.wait(job["id"])["status"], "succeeded")
        self.assertEqual(self.last()["resume"]["mode"], "resumed")

    def test_an_edit_after_the_parent_read_it_does_not_reach_the_run(self):
        self.revise()
        job = self.detach()
        self.write("resume.md", RESUME * 10)
        self.wait(job["id"])
        self.assertEqual(self.trace()[-1]["prompt_chars"], len(RESUME))

    def test_the_parent_does_not_look_up_the_session(self):
        self.revise()
        code, out, err = self.revise("--detach", "--json")
        self.assertEqual(code, 0, err)
        self.wait(json.loads(out)["id"])
        self.assertNotIn("note: resuming", err)
        self.assertNotIn("note: --resume requested", err)

    def test_force_is_recorded(self):
        self.revise()
        self.assertIs(self.detach("--force")["force"], True)

    def test_a_rejection_is_retried_in_the_worker(self):
        self.revise()
        os.environ["DEV_ORCHESTRA_MOCK_RESUME"] = "reject"
        job = self.wait(self.detach()["id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["resume"]["mode"], "fresh")
        self.assertNotIn("No conversation found", json.dumps(job))
        self.assertEqual(len(self.events()), 3)

    def test_a_rejection_without_budget_fails_the_job(self):
        self.revise()
        run_cli("config", "set", "budgets.architect", "2")
        os.environ["DEV_ORCHESTRA_MOCK_RESUME"] = "reject"
        job = self.wait(self.detach()["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("would spend an attempt", job["error"])


if __name__ == "__main__":
    unittest.main()
