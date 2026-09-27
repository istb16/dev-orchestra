"""Every adapter must answer the call the orchestrator actually makes.

An adapter overrides ``_launch`` (it used to override ``run``, which is now
the read-only gate every adapter shares) to do something the base class
cannot: Codex
writes its final message to a file and reads it back, rather than having its
log scraped. An override like that has to repeat the base signature, and a
repeated signature drifts.

It drifted. ``idle_timeout`` was added to ``Provider.run`` and not to
``CodexProvider.run``, so every real call raised ``TypeError`` -- and
``options`` was accepted but never passed on, so a configured ``sandbox``
was silently replaced by the default. Neither was caught, because the suite
reviews with ``MockProvider``, which overrides ``run`` outright and therefore
exercises no adapter's signature but its own.

So these tests do not test a provider. They test the *seam* between the
orchestrator and every provider registered, present and future, and they do
it without starting a process.
"""

from __future__ import annotations

import inspect
import unittest

from helpers import CLAUDE_HELP, IsolatedCase

from orchestrator import execution, providers
from orchestrator.providers import base


class _Completed:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


#: Exactly what `run`/`review run` pass. Written out rather than generated
#: from the base signature, so a keyword the orchestrator sends and an
#: adapter forgets cannot both disappear from the test at once.
CALL = {
    "model_spec": {"family": "default", "version": "latest"},
    "timeout": 30,
    "extra_args": [],
    "env": {},
    "options": {},
    "idle_timeout": 5.0,
}


class _Outcome:
    """A finished child process, without there having been one."""

    def __init__(self):
        self.exit_code = 0
        self.stdout = "done"
        self.stderr = ""
        self.duration = 0.1
        self.timed_out = False
        self.stalled = False
        self.idle_for = 0.0
        self.orphans_possible = False

    @property
    def ok(self):
        return True


class TestEveryAdapterTakesTheWholeCall(IsolatedCase):
    def adapters(self):
        return [providers.get_provider(name) for name in providers.available_providers()]

    def test_every_run_accepts_every_keyword_the_cli_sends(self):
        """The TypeError this file exists for. A not-installed CLI returns
        early inside the base method, so nothing is spawned -- but the call
        has to get that far, which is the whole point."""
        for provider in self.adapters():
            provider.which = lambda: None
            with self.subTest(provider=provider.name):
                result = provider.run("prompt", base.MODE_REVIEW, self.project, **CALL)
                self.assertIsNotNone(result)

    def test_every_run_signature_covers_the_base_signature(self):
        """Caught statically as well, because an adapter can be added by
        someone who never runs it through the orchestrator."""
        expected = set(inspect.signature(base.Provider.run).parameters)
        for provider in self.adapters():
            with self.subTest(provider=provider.name):
                actual = set(inspect.signature(type(provider).run).parameters)
                self.assertEqual(expected - actual, set(), "%s.run is missing keywords" % provider.name)

    def test_every_launch_signature_covers_the_base_signature(self):
        """``_launch`` is where adapters override now, so it drifts the same way."""
        expected = set(inspect.signature(base.Provider._launch).parameters)
        for provider in self.adapters():
            with self.subTest(provider=provider.name):
                actual = set(inspect.signature(type(provider)._launch).parameters)
                self.assertEqual(expected - actual, set(), "%s._launch is missing keywords" % provider.name)

    def test_no_adapter_overrides_the_gate(self):
        """``run`` holds the read-only raw-argument gate; an adapter that
        overrides it runs without one."""
        for provider in self.adapters():
            with self.subTest(provider=provider.name):
                self.assertIs(type(provider).run, base.Provider.run)

    def test_a_not_installed_cli_is_reported_not_raised(self):
        for provider in self.adapters():
            if provider.name == "mock":
                continue  # the mock is always installed; that is its job
            provider.which = lambda: None
            with self.subTest(provider=provider.name):
                result = provider.run("prompt", base.MODE_REVIEW, self.project, **CALL)
                self.assertFalse(result.ok)
                self.assertFalse(result.invoked)


class TestUserAdaptersTakeTheWholeCall(TestEveryAdapterTakesTheWholeCall):
    """The same seam for an adapter from the user's config directory -- the
    documented minimal example, so a change to ``run`` that would break
    every adapter written from it fails here first. Loaded explicitly: the
    suite never reads the real user's directory."""

    def setUp(self):
        super().setUp()
        self.write_user_provider("mycli")
        providers.load_user_providers()

    def adapters(self):
        return [providers.get_provider("mycli")]


class TestUserAdaptersPassTheGate(IsolatedCase):
    """The documented template only implements ``build_command``, which
    forwards raw arguments untouched. The gate still holds, because it is in
    the ``run`` the template inherits."""

    def setUp(self):
        super().setUp()
        self.write_user_provider("mycli")
        providers.load_user_providers()
        self.seen = {}
        original = execution.execute
        self.addCleanup(setattr, execution, "execute", original)

        def execute(command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
            self.seen["command"] = list(command)
            return _Outcome()

        execution.execute = execute
        self.provider = providers.get_provider("mycli")
        self.provider.which = lambda: "mycli"
        self.provider.version = lambda: ("mycli 1.2.0", None)

    def test_an_unspecified_adapter_still_runs(self):
        self.assertEqual(self.provider.read_only_enforcement()["status"], "unspecified")
        result = self.provider.run("prompt", base.MODE_REVIEW, self.project)
        self.assertTrue(result.ok)
        self.assertIn("--read-only", self.seen["command"])

    def test_raw_arguments_are_refused_in_review(self):
        result = self.provider.run("prompt", base.MODE_REVIEW, self.project, options={"args": ["--yolo"]})
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.invoked)
        self.assertNotIn("command", self.seen)

    def test_raw_arguments_still_reach_implement(self):
        self.provider.run("prompt", base.MODE_IMPLEMENT, self.project, options={"args": ["--yolo"]})
        self.assertIn("--yolo", self.seen["command"])


class TestOptionsReachTheCommand(IsolatedCase):
    """An override that accepts ``options`` and does not forward them turns a
    configured policy into a default, with nothing said."""

    def setUp(self):
        super().setUp()
        self.seen = {}
        self.original = execution.execute
        self.addCleanup(self.restore)

    def restore(self):
        execution.execute = self.original

    def capture(self, provider):
        """Run the adapter with the child process replaced by a recording."""

        def execute(command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
            self.seen["command"] = list(command)
            self.seen["idle_timeout"] = idle_timeout
            return _Outcome()

        execution.execute = execute
        provider.which = lambda: provider.executable
        provider.version = lambda: ("test 1", None)
        provider.configured_model = lambda: "gpt-example-1"
        return provider

    def test_codex_passes_a_configured_sandbox_through_run(self):
        """`build_command` has always honoured `sandbox`; the bug was that
        `run` never handed it over. Tested through `run`, not `build_command`,
        because that is where the value was being dropped."""
        provider = self.capture(providers.get_provider("codex"))
        provider.run(
            "prompt",
            base.MODE_IMPLEMENT,
            self.project,
            model_spec={"family": "recommended-coding", "version": "latest"},
            options={"sandbox": "danger-full-access"},
            idle_timeout=7.0,
        )
        command = self.seen["command"]
        self.assertEqual(command[command.index("-s") + 1], "danger-full-access")

    def test_codex_passes_extra_args_from_options_through_run(self):
        provider = self.capture(providers.get_provider("codex"))
        provider.run(
            "prompt",
            base.MODE_IMPLEMENT,
            self.project,
            model_spec={"family": "recommended-coding", "version": "latest"},
            options={"args": ["--flag-from-config"]},
        )
        self.assertIn("--flag-from-config", self.seen["command"])

    def test_codex_refuses_options_args_in_review_through_run(self):
        """The gate sees the caller's arguments before `-o` is added, so the
        refusal is of the config's flag and no child is started."""
        provider = self.capture(providers.get_provider("codex"))
        result = provider.run(
            "prompt",
            base.MODE_REVIEW,
            self.project,
            model_spec={"family": "recommended-coding", "version": "latest"},
            options={"args": ["--flag-from-config"]},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.invoked)
        self.assertNotIn("command", self.seen)
        self.assertEqual(result.command, ["codex"])

    def test_codex_refuses_a_loosening_extra_arg_without_its_value(self):
        provider = self.capture(providers.get_provider("codex"))
        result = provider.run(
            "prompt",
            base.MODE_REVIEW,
            self.project,
            model_spec={"family": "recommended-coding", "version": "latest"},
            extra_args=["-sdanger-full-access"],
        )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("'-s'", result.stderr)
        self.assertNotIn("danger-full-access", result.stderr)
        self.assertNotIn("command", self.seen)

    def test_mock_refuses_options_args_in_review(self):
        """The mock overrides `_launch`, so it passes through the gate too --
        the path `review run` takes in the suite."""
        provider = providers.get_provider("mock")
        result = provider.run("prompt", base.MODE_REVIEW, self.project, options={"args": ["x"]})
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.invoked)
        self.assertIn("a bare value", result.stderr)

    def test_mock_still_runs_with_no_raw_arguments(self):
        result = providers.get_provider("mock").run("prompt", base.MODE_REVIEW, self.project)
        self.assertTrue(result.ok)

    def test_codex_still_captures_its_final_message_to_a_file(self):
        """The reason the override exists in the first place."""
        provider = self.capture(providers.get_provider("codex"))
        provider.run(
            "prompt",
            base.MODE_REVIEW,
            self.project,
            model_spec={"family": "recommended-coding", "version": "latest"},
        )
        self.assertIn("-o", self.seen["command"])

    def test_codex_asks_for_no_idle_deadline_whatever_is_requested(self):
        """Forwarding the keyword is not the same as honouring it. Codex does
        not stream progress, so an idle deadline would kill a working run;
        the adapter has to keep answering None."""
        provider = self.capture(providers.get_provider("codex"))
        provider.run(
            "prompt",
            base.MODE_REVIEW,
            self.project,
            model_spec={"family": "recommended-coding", "version": "latest"},
            idle_timeout=5.0,
        )
        self.assertIsNone(self.seen["idle_timeout"])

    def test_claude_does_honour_the_idle_deadline(self):
        """The other half of the same rule: Claude streams, so the deadline
        is real there. Without this the test above would pass on an adapter
        that ignored the keyword everywhere."""
        provider = self.capture(providers.get_provider("claude"))
        # A CLI that advertises its read-only flags; otherwise the run is
        # refused before anything is spawned.
        provider._capture = lambda command, timeout=30: _Completed(CLAUDE_HELP)
        provider.run(
            "prompt",
            base.MODE_REVIEW,
            self.project,
            model_spec={"family": "sonnet", "version": "latest"},
            idle_timeout=5.0,
        )
        self.assertEqual(self.seen["idle_timeout"], 5.0)


SESSION = "22222222-2222-4222-8222-222222222222"
PARSED_SESSION = "11111111-1111-4111-8111-111111111111"


class _BareAdapter(base.Provider):
    """An adapter with nothing but a command, the way one is written."""

    name = "bare"
    executable = "bare"

    def which(self):
        return "bare"

    def version(self):
        return "bare 1", None

    def _resolve_latest(self, family):
        return base.ResolvedModel(self.name, family, "latest", None, "default", "cli-default")

    def build_command(self, mode, resolved, cwd, extra_args=(), options=None):
        return ["bare", mode, *extra_args]


class _TodaysLaunch(_BareAdapter):
    """``_launch`` overridden with the signature it had before resuming."""

    def _launch(
        self,
        prompt,
        mode,
        cwd,
        model_spec=None,
        timeout=1800,
        extra_args=(),
        env=None,
        options=None,
        idle_timeout=None,
    ):
        return super()._launch(
            prompt,
            mode,
            cwd,
            model_spec=model_spec,
            timeout=timeout,
            extra_args=extra_args,
            env=env,
            options=options,
            idle_timeout=idle_timeout,
        )


class _ResumingAdapter(_BareAdapter):
    name = "resuming"
    supports_resume = True

    def __init__(self):
        super().__init__()
        self.rejected_calls = []
        self.parse_calls = 0
        self.launch_kwargs = []
        self.raise_in_rejected = False
        self.raise_in_parse = False
        self.raise_in_postprocess = False

    def resume_args(self, session_id):
        return ["--resume=%s" % session_id]

    def resume_rejected(self, outcome, mode, options, session_id):
        self.rejected_calls.append({"options": options, "session_id": session_id})
        if self.raise_in_rejected:
            raise RuntimeError("rejected reader broke")
        return outcome.stderr.startswith("No conversation") and session_id == SESSION

    def parse_session(self, outcome):
        self.parse_calls += 1
        if self.raise_in_parse:
            raise RuntimeError("session reader broke")
        return {"session_id": PARSED_SESSION, "context_tokens": 123, "init": {"tools": ["Read"]}}

    def postprocess(self, outcome, mode):
        if self.raise_in_postprocess:
            raise RuntimeError("answer reader broke")
        return super().postprocess(outcome, mode)

    def _launch(self, prompt, mode, cwd, **kwargs):
        self.launch_kwargs.append(dict(kwargs))
        return super()._launch(prompt, mode, cwd, **kwargs)


class _RejectedOutcome(_Outcome):
    def __init__(self):
        super().__init__()
        self.exit_code = 1
        self.stdout = ""
        self.stderr = "No conversation found with session ID: %s" % SESSION

    @property
    def ok(self):
        return False


class TestResumingThroughTheBase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.seen = {}
        self.outcome = _Outcome()
        original = execution.execute
        self.addCleanup(setattr, execution, "execute", original)

        def execute(command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
            self.seen["command"] = list(command)
            return self.outcome

        execution.execute = execute

    def test_the_default_says_unsupported_or_unspecified(self):
        self.assertEqual(base.Provider().resume_support(self.project)["status"], "unsupported")

        class Declares(base.Provider):
            supports_resume = True

        self.assertEqual(Declares().resume_support(self.project)["status"], "unspecified")

    def test_codex_and_mock_say_what_they_do(self):
        codex = providers.get_provider("codex")
        self.assertFalse(codex.supports_resume)
        self.assertEqual(codex.resume_support(self.project)["status"], "unsupported")
        self.assertEqual(providers.get_provider("mock").resume_support(self.project)["status"], "verified")

    def test_a_launch_with_todays_signature_still_runs_fresh(self):
        provider = _TodaysLaunch()
        for mode in (base.MODE_PLAN, base.MODE_REVIEW):
            with self.subTest(mode=mode):
                result = provider.run("prompt", mode, self.project, **CALL)
                self.assertTrue(result.ok)
        with self.assertRaises(TypeError):
            provider.run("prompt", base.MODE_PLAN, self.project, resume_session=SESSION, **CALL)

    def test_a_build_command_only_adapter_still_runs_fresh(self):
        result = _BareAdapter().run("prompt", base.MODE_PLAN, self.project, **CALL)
        self.assertTrue(result.ok)
        self.assertEqual(self.seen["command"], ["bare", base.MODE_PLAN])

    def test_command_line_refuses_to_resume_on_an_adapter_that_cannot(self):
        provider = _BareAdapter()
        resolved = provider.resolve_model(None)
        with self.assertRaises(NotImplementedError):
            provider.command_line(base.MODE_PLAN, resolved, self.project, resume_session=SESSION)

    def test_only_a_read_only_run_may_resume(self):
        with self.assertRaises(ValueError):
            _ResumingAdapter().run("prompt", base.MODE_IMPLEMENT, self.project, resume_session=SESSION)

    def test_a_rejected_resume_is_reported(self):
        provider = _ResumingAdapter()
        self.outcome = _RejectedOutcome()
        result = provider.run("prompt", base.MODE_PLAN, self.project, resume_session=SESSION, **CALL)
        self.assertFalse(result.ok)
        self.assertTrue(result.resume_rejected)
        self.assertEqual(result.session_id, PARSED_SESSION)
        self.assertEqual(result.context_tokens, 123)
        self.assertEqual(result.session_init, {"tools": ["Read"]})
        self.assertEqual(self.seen["command"][-1], "--resume=%s" % SESSION)
        self.assertIs(provider.rejected_calls[0]["options"], CALL["options"])
        self.assertEqual(provider.rejected_calls[0]["session_id"], SESSION)

    def test_a_rejected_resume_survives_an_unreadable_answer(self):
        provider = _ResumingAdapter()
        provider.raise_in_postprocess = True
        self.outcome = _RejectedOutcome()
        result = provider.run("prompt", base.MODE_PLAN, self.project, resume_session=SESSION, **CALL)
        self.assertFalse(result.ok)
        self.assertFalse(result.usage.measured)
        self.assertTrue(result.resume_rejected)
        self.assertEqual(result.session_id, PARSED_SESSION)
        self.assertEqual(result.session_init, {"tools": ["Read"]})

    def test_a_fresh_run_is_not_asked_about_rejection(self):
        provider = _ResumingAdapter()
        result = provider.run("prompt", base.MODE_PLAN, self.project, **CALL)
        self.assertTrue(result.ok)
        self.assertEqual(provider.rejected_calls, [])
        self.assertFalse(any(token.startswith("--resume") for token in self.seen["command"]))
        self.assertEqual(provider.parse_calls, 1)
        self.assertEqual(result.session_id, PARSED_SESSION)
        self.assertNotIn("resume_session", provider.launch_kwargs[0])

    def test_a_broken_rejection_reader_is_not_a_rejection(self):
        provider = _ResumingAdapter()
        provider.raise_in_rejected = True
        self.outcome = _RejectedOutcome()
        result = provider.run("prompt", base.MODE_PLAN, self.project, resume_session=SESSION, **CALL)
        self.assertFalse(result.resume_rejected)
        self.assertTrue(result.stderr.startswith("RuntimeError: rejected reader broke"))

    def test_a_broken_session_reader_leaves_the_fields_empty(self):
        provider = _ResumingAdapter()
        provider.raise_in_parse = True
        result = provider.run("prompt", base.MODE_PLAN, self.project, **CALL)
        self.assertIsNone(result.session_id)
        self.assertIsNone(result.context_tokens)
        self.assertIsNone(result.session_init)
        self.assertIn("RuntimeError: session reader broke", result.stderr)

    def test_a_raw_resume_argument_is_still_refused(self):
        provider = _ResumingAdapter()
        result = provider.run(
            "prompt",
            base.MODE_PLAN,
            self.project,
            options={"args": ["--resume=33333333-3333-4333-8333-333333333333"]},
            resume_session=SESSION,
        )
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.invoked)
        self.assertNotIn("command", self.seen)

    def test_the_session_fields_are_serialised(self):
        result = _ResumingAdapter().run("prompt", base.MODE_PLAN, self.project, **CALL)
        data = result.to_dict()
        for key in ("session_id", "context_tokens", "session_init", "resume_rejected"):
            self.assertIn(key, data)


if __name__ == "__main__":
    unittest.main()
